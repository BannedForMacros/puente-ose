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
from pathlib import Path, PurePosixPath, PureWindowsPath
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

# Columnas por las que el panel web puede ordenar (nunca se interpola texto del usuario).
SORTABLE = ("serie_numero", "fecha_emision", "importe_total", "estado", "actualizado_en", "creado_en", "tipo")

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

-- Constancia de las cargas de TXT hechas desde el panel: quién, cuándo y qué archivo exacto.
CREATE TABLE IF NOT EXISTS cargas (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         TEXT NOT NULL,
    usuario    TEXT,
    origen     TEXT,                              -- IP del equipo que subió
    total      INTEGER NOT NULL,
    encolados  INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS cargas_archivos (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    carga_id      INTEGER NOT NULL REFERENCES cargas(id),
    nombre        TEXT NOT NULL,
    nombre_final  TEXT,
    clave         TEXT,
    sha256        TEXT,
    bytes         INTEGER,
    encolado      INTEGER NOT NULL,
    resultado     TEXT NOT NULL,
    detalle       TEXT
);
CREATE INDEX IF NOT EXISTS ix_cargas_archivos_carga ON cargas_archivos(carga_id);
CREATE INDEX IF NOT EXISTS ix_cargas_archivos_sha ON cargas_archivos(sha256);
CREATE INDEX IF NOT EXISTS ix_cargas_archivos_clave ON cargas_archivos(clave);

-- Bandeja de preparación: subidos desde el panel, esperando que TI los libere.
CREATE TABLE IF NOT EXISTS bandeja (
    clave          TEXT PRIMARY KEY,
    tipo           TEXT NOT NULL,
    serie_numero   TEXT NOT NULL,
    nombre_final   TEXT NOT NULL,
    archivo        TEXT NOT NULL,                 -- TXT original en WORK_DIR/bandeja
    sha256         TEXT,
    fecha_emision  TEXT NOT NULL,                 -- fecha original del TXT
    fecha_nueva    TEXT,                          -- asignada desde el panel (fuera de plazo)
    importe_total  TEXT,
    moneda         TEXT,
    cliente_doc    TEXT,
    ref_clave      TEXT,                          -- notas: comprobante que modifican
    especiales     TEXT NOT NULL DEFAULT '',      -- inafecto,exonerado,gratuito,exportacion
    estado         TEXT NOT NULL,                 -- PREPARADO | SEPARADO | ESPERANDO_REFERENCIA | EN_COLA
    mensaje        TEXT,
    carga_id       INTEGER,
    creado_en      TEXT NOT NULL,
    actualizado_en TEXT NOT NULL,
    liberado_en    TEXT,
    liberado_por   TEXT
);
CREATE INDEX IF NOT EXISTS ix_bandeja_estado ON bandeja(estado);
CREATE INDEX IF NOT EXISTS ix_bandeja_ref ON bandeja(ref_clave);
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

    def search(self, *, q: str = "", estados: tuple[str, ...] = (), tipo: str = "", desde: str = "",
               hasta: str = "", sort: str = "actualizado_en", desc: bool = True, page: int = 1,
               per_page: int = 25) -> tuple[list[sqlite3.Row], int]:
        """Búsqueda paginada para el panel web. `sort` solo acepta columnas de SORTABLE."""
        where, params = ["1=1"], []
        if q:
            like = f"%{q.strip().upper()}%"
            where.append("(UPPER(clave) LIKE ? OR UPPER(serie_numero) LIKE ? OR UPPER(IFNULL(mensaje,'')) LIKE ?"
                         " OR IFNULL(document_id,'') LIKE ? OR IFNULL(cliente_doc,'') LIKE ?)")
            params += [like, like, like, f"%{q.strip()}%", f"%{q.strip()}%"]
        if estados:
            where.append(f"estado IN ({','.join('?' * len(estados))})")
            params += list(estados)
        if tipo:
            where.append("tipo=?")
            params.append(tipo)
        if desde:
            where.append("fecha_emision>=?")
            params.append(desde)
        if hasta:
            where.append("fecha_emision<=?")
            params.append(hasta)
        col = sort if sort in SORTABLE else "actualizado_en"
        if col == "importe_total":
            col = "CAST(importe_total AS REAL)"   # se guarda como texto
        order = f"{col} {'DESC' if desc else 'ASC'}, clave"
        sql_where = " AND ".join(where)
        with self._lock:
            total = self._conn.execute(f"SELECT COUNT(*) FROM documentos WHERE {sql_where}", params).fetchone()[0]
            rows = self._conn.execute(
                f"SELECT * FROM documentos WHERE {sql_where} ORDER BY {order} LIMIT ? OFFSET ?",
                (*params, per_page, max(page - 1, 0) * per_page),
            ).fetchall()
        return rows, total

    def state_counts(self) -> dict[str, int]:
        with self._lock:
            rows = self._conn.execute("SELECT estado, COUNT(*) FROM documentos GROUP BY estado").fetchall()
        return {r[0]: r[1] for r in rows}

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

    # --- cargas desde el panel ----------------------------------------------
    def save_upload(self, *, usuario: str, origen: str, archivos: list[dict[str, Any]]) -> int:
        """Guarda la constancia de una carga y deja un evento en cada comprobante encolado."""
        ts = now_iso()
        encolados = sum(1 for a in archivos if a["encolado"])
        with self._tx() as c:
            carga_id = c.execute("INSERT INTO cargas (ts, usuario, origen, total, encolados) VALUES (?,?,?,?,?)",
                                 (ts, usuario, origen, len(archivos), encolados)).lastrowid
            c.executemany(
                "INSERT INTO cargas_archivos (carga_id, nombre, nombre_final, clave, sha256, bytes, encolado,"
                " resultado, detalle) VALUES (?,?,?,?,?,?,?,?,?)",
                [(carga_id, a["nombre"], a.get("nombre_final"), a.get("clave") or None, a.get("sha256"),
                  a.get("bytes"), int(a["encolado"]), a["resultado"], a.get("detalle", "")) for a in archivos])
            for a in archivos:
                if a["encolado"] and a.get("clave"):
                    self._event(c, a["clave"], None,
                                f"Subido desde el panel por {usuario} (carga N.º {carga_id}, archivo {a['nombre']})")
        return carga_id

    def uploads(self, limit: int = 20) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute("SELECT * FROM cargas ORDER BY id DESC LIMIT ?", (limit,)).fetchall()

    def upload(self, carga_id: int) -> tuple[sqlite3.Row | None, list[sqlite3.Row]]:
        with self._lock:
            head = self._conn.execute("SELECT * FROM cargas WHERE id=?", (carga_id,)).fetchone()
            files = self._conn.execute("SELECT * FROM cargas_archivos WHERE carga_id=? ORDER BY id",
                                       (carga_id,)).fetchall()
        return head, files

    def last_upload_of(self, *, sha256: str = "", clave: str = "") -> sqlite3.Row | None:
        """Última vez que se encoló este mismo archivo (por contenido) o este comprobante."""
        col, val = ("sha256", sha256) if sha256 else ("clave", clave)
        with self._lock:
            return self._conn.execute(
                f"SELECT a.*, c.ts, c.usuario FROM cargas_archivos a JOIN cargas c ON c.id=a.carga_id"
                f" WHERE a.{col}=? AND a.encolado=1 ORDER BY a.id DESC LIMIT 1", (val,),
            ).fetchone()

    # --- bandeja de preparación ---------------------------------------------
    def stage(self, row: dict[str, Any]) -> None:
        ts = now_iso()
        cols = [*row, "creado_en", "actualizado_en"]
        with self._tx() as c:
            c.execute(f"INSERT OR REPLACE INTO bandeja ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                      (*row.values(), ts, ts))

    def staged(self, *estados: str) -> list[sqlite3.Row]:
        with self._lock:
            if not estados:
                return self._conn.execute("SELECT * FROM bandeja ORDER BY fecha_emision, serie_numero").fetchall()
            marks = ",".join("?" * len(estados))
            return self._conn.execute(
                f"SELECT * FROM bandeja WHERE estado IN ({marks}) ORDER BY fecha_emision, serie_numero", estados
            ).fetchall()

    def staged_one(self, clave: str) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute("SELECT * FROM bandeja WHERE clave=?", (clave,)).fetchone()

    def update_staged(self, clave: str, **fields: Any) -> None:
        fields["actualizado_en"] = now_iso()
        sets = ", ".join(f"{k}=?" for k in fields)
        with self._tx() as c:
            c.execute(f"UPDATE bandeja SET {sets} WHERE clave=?", (*fields.values(), clave))

    def unstage(self, clave: str) -> None:
        with self._tx() as c:
            c.execute("DELETE FROM bandeja WHERE clave=?", (clave,))

    # --- traslado a otro equipo ---------------------------------------------
    _PATH_COLUMNS = {"documentos": ("archivo", "sbd_path", "xml_path", "pdf_path"), "bandeja": ("archivo",)}

    def relocate(self, old_root: str, new_root: Path) -> int:
        """Cambia las rutas guardadas de `old_root` (p. ej. la carpeta de trabajo de otra PC,
        en formato Mac/Linux o Windows) a `new_root`. Devuelve cuántas rutas cambió."""
        flavour = PureWindowsPath if "\\" in old_root or old_root[1:3] == ":\\" else PurePosixPath
        old = flavour(old_root)
        changed = 0
        with self._tx() as c:
            for table, cols in self._PATH_COLUMNS.items():
                for col in cols:
                    rows = c.execute(f"SELECT rowid, {col} FROM {table} WHERE {col} IS NOT NULL AND {col}<>''")
                    for rowid, value in rows.fetchall():
                        try:
                            rel = flavour(value).relative_to(old)
                        except ValueError:
                            continue
                        c.execute(f"UPDATE {table} SET {col}=? WHERE rowid=?", (str(new_root.joinpath(*rel.parts)), rowid))
                        changed += 1
        return changed
