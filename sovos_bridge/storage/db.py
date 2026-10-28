"""Persistencia en SQLite: estado de cada comprobante, eventos y notificaciones.

Garantía de envío único (Sovos NO detecta duplicados): la clave
RUC-tipo-serie-número es PRIMARY KEY y la reserva se hace en una transacción
BEGIN IMMEDIATE antes de llamar a Sovos. Un comprobante solo se reenvía si su
último estado demuestra que Sovos no lo tiene (rechazado, error de validación
o error de conexión ocurrido antes de transmitir).
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Iterator


class Estado(StrEnum):
    RECIBIDO = "RECIBIDO"
    VALIDACION_ERROR = "VALIDACION_ERROR"
    ENVIADO = "ENVIADO"
    EN_PROCESO = "EN_PROCESO"
    APROBADO = "APROBADO"
    RECHAZADO = "RECHAZADO"
    ERROR_CONEXION = "ERROR_CONEXION"
    DUPLICADO = "DUPLICADO"


PENDIENTES = (Estado.ENVIADO, Estado.EN_PROCESO)

SCHEMA = """
CREATE TABLE IF NOT EXISTS documentos (
    clave              TEXT PRIMARY KEY,          -- RUC-tipo-serie-número
    tipo               TEXT NOT NULL,
    serie_numero       TEXT NOT NULL,
    archivo            TEXT,
    estado             TEXT NOT NULL,
    document_id        TEXT,                      -- id asignado por Sovos
    fecha_emision      TEXT,
    moneda             TEXT,
    importe_total      TEXT,
    cliente_doc        TEXT,                      -- enmascarado
    envio_iniciado_en  TEXT,
    enviado_en         TEXT,
    envio_incierto     INTEGER NOT NULL DEFAULT 0,
    intentos_envio     INTEGER NOT NULL DEFAULT 0,
    intentos_estado    INTEGER NOT NULL DEFAULT 0,
    ultimo_estado_en   TEXT,
    codigo_sovos       TEXT,
    codigo_sunat       TEXT,
    mensaje            TEXT,
    sbd_path           TEXT,
    xml_path           TEXT,
    pdf_path           TEXT,
    creado_en          TEXT NOT NULL,
    actualizado_en     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_documentos_estado ON documentos(estado);
CREATE INDEX IF NOT EXISTS ix_documentos_serie ON documentos(serie_numero);
CREATE INDEX IF NOT EXISTS ix_documentos_docid ON documentos(document_id);

CREATE TABLE IF NOT EXISTS eventos (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    clave    TEXT NOT NULL,
    ts       TEXT NOT NULL,
    estado   TEXT,
    detalle  TEXT
);
CREATE INDEX IF NOT EXISTS ix_eventos_clave ON eventos(clave);

CREATE TABLE IF NOT EXISTS notificaciones (
    notification_id  TEXT PRIMARY KEY,
    clave            TEXT,
    document_id      TEXT,
    response_code    TEXT,
    recibida_en      TEXT NOT NULL,
    confirmada       INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_notif_confirmada ON notificaciones(confirmada);
"""


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


@dataclass(frozen=True)
class Reserva:
    """Resultado de intentar reservar un comprobante para envío."""
    accion: str                  # "nuevo" | "reintento" | "duplicado" | "incierto"
    estado_previo: str | None = None
    document_id: str | None = None

    @property
    def puede_enviar(self) -> bool:
        return self.accion in ("nuevo", "reintento")


class Database:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None, timeout=30)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=FULL")
            self._conn.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    # --- comprobantes -------------------------------------------------------
    def reserve(self, clave: str, *, tipo: str, serie_numero: str, archivo: str,
                fecha_emision: str = "", moneda: str = "", importe_total: str = "",
                cliente_doc: str = "") -> Reserva:
        ts = now_iso()
        with self._tx() as c:
            row = c.execute("SELECT * FROM documentos WHERE clave=?", (clave,)).fetchone()
            if row is None:
                c.execute(
                    "INSERT INTO documentos (clave, tipo, serie_numero, archivo, estado, fecha_emision, moneda,"
                    " importe_total, cliente_doc, creado_en, actualizado_en) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (clave, tipo, serie_numero, archivo, Estado.RECIBIDO, fecha_emision, moneda,
                     importe_total, cliente_doc, ts, ts),
                )
                self._event(c, clave, Estado.RECIBIDO, f"Archivo recibido: {archivo}")
                return Reserva("nuevo")

            estado = row["estado"]
            en_sovos = estado in (Estado.APROBADO, Estado.ENVIADO, Estado.EN_PROCESO, Estado.DUPLICADO)
            # ERROR_CONEXION con documentId = Sovos lo recibió pero no se obtuvo respuesta final.
            if en_sovos or (estado == Estado.ERROR_CONEXION and row["document_id"]):
                self._event(c, clave, None, f"TXT repetido recibido ({archivo}); estado actual {estado}, no se reenvía")
                return Reserva("duplicado", estado, row["document_id"])
            if row["envio_incierto"] or (estado == Estado.RECIBIDO and row["envio_iniciado_en"]):
                self._event(c, clave, None, f"TXT repetido ({archivo}) con envío previo incierto; requiere revisión")
                return Reserva("incierto", estado)

            # RECHAZADO (SUNAT no lo registró, el número puede reutilizarse), VALIDACION_ERROR,
            # ERROR_CONEXION antes de transmitir o RECIBIDO sin envío: se reprocesa.
            c.execute(
                "UPDATE documentos SET estado=?, archivo=?, fecha_emision=?, moneda=?, importe_total=?,"
                " cliente_doc=?, codigo_sovos=NULL, codigo_sunat=NULL, mensaje=NULL, document_id=NULL,"
                " envio_iniciado_en=NULL, envio_incierto=0, actualizado_en=? WHERE clave=?",
                (Estado.RECIBIDO, archivo, fecha_emision, moneda, importe_total, cliente_doc, ts, clave),
            )
            self._event(c, clave, Estado.RECIBIDO, f"Reprocesando (estado previo {estado}): {archivo}")
            return Reserva("reintento", estado)

    def mark_sending(self, clave: str) -> None:
        """Se llama justo antes del POST: si el proceso muere aquí, el envío queda incierto."""
        ts = now_iso()
        with self._tx() as c:
            c.execute("UPDATE documentos SET envio_iniciado_en=?, intentos_envio=intentos_envio+1,"
                      " actualizado_en=? WHERE clave=?", (ts, ts, clave))

    def mark_sent(self, clave: str, document_id: str) -> None:
        ts = now_iso()
        with self._tx() as c:
            c.execute("UPDATE documentos SET estado=?, document_id=?, enviado_en=?, envio_incierto=0,"
                      " intentos_estado=0, actualizado_en=? WHERE clave=?",
                      (Estado.ENVIADO, document_id, ts, ts, clave))
            self._event(c, clave, Estado.ENVIADO, f"Enviado a Sovos, documentId={document_id}")

    def set_state(self, clave: str, estado: Estado, detalle: str = "", **fields: Any) -> None:
        ts = now_iso()
        allowed = {
            "codigo_sovos", "codigo_sunat", "mensaje", "sbd_path", "xml_path", "pdf_path",
            "envio_incierto", "ultimo_estado_en", "document_id", "envio_iniciado_en",
        }
        bad = set(fields) - allowed
        if bad:
            raise ValueError(f"Campos no permitidos: {bad}")
        cols = ", ".join(f"{k}=?" for k in fields)
        sql = f"UPDATE documentos SET estado=?, actualizado_en=?{', ' + cols if cols else ''} WHERE clave=?"
        with self._tx() as c:
            c.execute(sql, (estado, ts, *fields.values(), clave))
            self._event(c, clave, estado, detalle)

    def update_fields(self, clave: str, **fields: Any) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        with self._tx() as c:
            c.execute(f"UPDATE documentos SET {cols}, actualizado_en=? WHERE clave=?",
                      (*fields.values(), now_iso(), clave))

    def touch_status_poll(self, clave: str) -> None:
        ts = now_iso()
        with self._tx() as c:
            c.execute("UPDATE documentos SET intentos_estado=intentos_estado+1, ultimo_estado_en=? WHERE clave=?",
                      (ts, clave))

    def add_event(self, clave: str, detalle: str, estado: str | None = None) -> None:
        with self._tx() as c:
            self._event(c, clave, estado, detalle)

    def _event(self, c: sqlite3.Connection, clave: str, estado: str | None, detalle: str) -> None:
        c.execute("INSERT INTO eventos (clave, ts, estado, detalle) VALUES (?,?,?,?)",
                  (clave, now_iso(), estado, detalle))

    # --- consultas ----------------------------------------------------------
    def get(self, clave: str) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute("SELECT * FROM documentos WHERE clave=?", (clave,)).fetchone()

    def find(self, serie_numero: str) -> list[sqlite3.Row]:
        """Busca por serie-número (BT05-00125304) o por clave completa."""
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM documentos WHERE serie_numero=? OR clave=? ORDER BY creado_en",
                (serie_numero.upper(), serie_numero.upper()),
            ).fetchall()

    def by_document_id(self, document_id: str) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute("SELECT * FROM documentos WHERE document_id=?", (document_id,)).fetchone()

    def by_states(self, *estados: str) -> list[sqlite3.Row]:
        marks = ",".join("?" * len(estados))
        with self._lock:
            return self._conn.execute(
                f"SELECT * FROM documentos WHERE estado IN ({marks}) ORDER BY creado_en", estados
            ).fetchall()

    def events(self, clave: str) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute("SELECT * FROM eventos WHERE clave=? ORDER BY id", (clave,)).fetchall()

    def day_rows(self, day: str) -> list[sqlite3.Row]:
        """Comprobantes recibidos o actualizados el día AAAA-MM-DD."""
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM documentos WHERE substr(creado_en,1,10)=? OR substr(actualizado_en,1,10)=?"
                " ORDER BY creado_en", (day, day),
            ).fetchall()

    # --- notificaciones -----------------------------------------------------
    def save_notification(self, notification_id: str, *, clave: str | None, document_id: str | None,
                          response_code: str | None) -> bool:
        """Registra la notificación; devuelve False si ya estaba procesada."""
        with self._tx() as c:
            cur = c.execute(
                "INSERT OR IGNORE INTO notificaciones (notification_id, clave, document_id, response_code, recibida_en)"
                " VALUES (?,?,?,?,?)", (notification_id, clave, document_id, response_code, now_iso()),
            )
            return cur.rowcount == 1

    def unacked_notifications(self, limit: int = 100) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT notification_id FROM notificaciones WHERE confirmada=0 ORDER BY recibida_en LIMIT ?", (limit,)
            ).fetchall()
        return [r[0] for r in rows]

    def mark_acked(self, ids: list[str]) -> None:
        if not ids:
            return
        with self._tx() as c:
            c.executemany("UPDATE notificaciones SET confirmada=1 WHERE notification_id=?", [(i,) for i in ids])
