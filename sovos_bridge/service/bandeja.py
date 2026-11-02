"""Bandeja de preparación: lo subido desde el panel espera aquí hasta que TI lo libera.

Recorrido: subir -> bandeja (nada se envía) -> liberar -> cola de envío (INBOX_DIR) -> Sovos.

Reglas:
  - Especiales (inafecto, exonerado, gratuito, exportación): se pueden separar al
    subirlos; no salen hasta que se liberen a propósito.
  - Plazo SUNAT: facturas y sus notas, PLAZO_FACTURA_DIAS; boletas y sus notas,
    PLAZO_BOLETA_DIAS (días calendario desde el día siguiente a la emisión). Lo
    vencido no se libera hasta asignarle una fecha nueva dentro del plazo. El TXT
    original se conserva sin cambios; solo la copia que va a la cola lleva la fecha nueva.
  - Notas: al liberarlas esperan a que el comprobante que modifican esté aprobado
    (SUNAT rechaza con 2119 si aún no lo tiene). El servicio las suelta solo.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import sqlite3
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from sovos_bridge.config import Settings
from sovos_bridge.parser import parse_file, parse_filename
from sovos_bridge.parser.layout import LAYOUTS_BY_TYPE
from sovos_bridge.parser.models import Document, dec
from sovos_bridge.parser.tokenizer import decode_bytes
from sovos_bridge.storage import Database, Estado
from sovos_bridge.storage.db import now_iso
from sovos_bridge.validation.catalogs import AFECTACION_GRATUITA

log = logging.getLogger(__name__)

PREPARADO = "PREPARADO"
SEPARADO = "SEPARADO"
ESPERANDO = "ESPERANDO_REFERENCIA"
EN_COLA = "EN_COLA"
EN_BANDEJA = (PREPARADO, SEPARADO, ESPERANDO)   # todavía no salieron hacia Sovos

ESPECIALES = ("inafecto", "exonerado", "gratuito", "exportacion")
# Rechazos que se corrigen desde la bandeja: 2108 fuera de fecha; 1079 boleta que solo
# puede informarse en un resumen diario.
RECHAZOS_RECUPERABLES = {"2108", "1079"}
_BOM = b"\xef\xbb\xbf"


# --- clasificación -------------------------------------------------------------
def especiales(doc: Document) -> list[str]:
    """Operaciones no gravadas o gratuitas presentes en el comprobante."""
    found: set[str] = set()
    for it in doc.items:
        for t in it.tributos:
            a = t.afectacion
            if t.codigo == "9998" or a.startswith("3"):
                found.add("inafecto")
            elif t.codigo == "9997" or a in ("20", "21"):
                found.add("exonerado")
            elif t.codigo == "9995" or a == "40":
                found.add("exportacion")
            if t.codigo == "9996" or a in AFECTACION_GRATUITA:
                found.add("gratuito")
    for code, name in (("1002", "inafecto"), ("1003", "exonerado"), ("1004", "gratuito"), ("1000", "exportacion")):
        if dec(doc.totales.get(code)) > 0:
            found.add(name)
    return [e for e in ESPECIALES if e in found]


def familia(serie_numero: str) -> str:
    """Las notas siguen el plazo del comprobante que modifican (serie F o B)."""
    return "factura" if serie_numero[:1].upper() == "F" else "boleta"


def plazo_dias(s: Settings, serie_numero: str) -> int:
    return s.plazo_factura_dias if familia(serie_numero) == "factura" else s.plazo_boleta_dias


def fechas_disponibles(s: Settings, fam: str, hoy: date | None = None) -> list[str]:
    """Fechas de emisión que SUNAT aún acepta hoy, de la más reciente a la más antigua."""
    hoy = hoy or date.today()
    dias = s.plazo_factura_dias if fam == "factura" else s.plazo_boleta_dias
    return [(hoy - timedelta(days=i)).isoformat() for i in range(dias + 1)]


def redate(data: bytes, tipo: str, nueva: str) -> bytes:
    """Copia del TXT con otra fecha de emisión. El vencimiento se corre los mismos días
    (si era igual a la emisión, queda igual a la nueva) para conservar el plazo de pago."""
    bom = data.startswith(_BOM)
    text, encoding = decode_bytes(data)
    ide = {name: pos for pos, name in LAYOUTS_BY_TYPE.get(tipo, LAYOUTS_BY_TYPE["03"])["IDE"].items()}
    out = []
    for line in text.splitlines(keepends=True):
        body = line.rstrip("\r\n")
        if body.startswith("IDE|"):
            f = body.split("|")
            pe, pv = ide["fecha_emision"], ide.get("fecha_vencimiento")
            vieja = f[pe]
            f[pe] = nueva
            if pv is not None and len(f) > pv and f[pv]:
                try:
                    delta = date.fromisoformat(nueva) - date.fromisoformat(vieja)
                    f[pv] = (date.fromisoformat(f[pv]) + delta).isoformat()
                except ValueError:
                    f[pv] = nueva
            line = "|".join(f) + line[len(body):]
        out.append(line)
    return (_BOM if bom else b"") + "".join(out).encode(encoding)


# --- vista para el panel ---------------------------------------------------------
@dataclass
class Resultado:
    hechos: list[str] = field(default_factory=list)
    omitidos: list[dict[str, str]] = field(default_factory=list)
    resumenes: list[dict[str, Any]] = field(default_factory=list)
    error: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"hechos": self.hechos, "omitidos": self.omitidos, "resumenes": self.resumenes, "error": self.error}


class Bandeja:
    def __init__(self, settings: Settings, db: Database, resumenes: Any = None):
        self.s = settings
        self.db = db
        self.resumenes = resumenes      # service.resumen.Resumenes: boletas por resumen diario

    # --- entrada --------------------------------------------------------------
    def add(self, *, data: bytes, nombre_final: str, doc: Document, sha256: str, separar: bool,
            carga_id: int | None, cliente_doc: str, usuario: str) -> str:
        """Guarda el TXT original en la bandeja. Devuelve el estado con que quedó."""
        self.s.bandeja_dir.mkdir(parents=True, exist_ok=True)
        path = self.s.bandeja_dir / nombre_final
        tmp = path.with_name(path.name + ".subiendo")
        tmp.write_bytes(data)
        os.replace(tmp, path)
        esp = especiales(doc)
        estado = SEPARADO if esp and separar else PREPARADO
        ref = doc.referencia
        self.db.stage({
            "clave": doc.clave, "tipo": doc.tipo_comprobante, "serie_numero": doc.serie_numero,
            "nombre_final": nombre_final, "archivo": str(path), "sha256": sha256,
            "fecha_emision": doc.fecha_emision, "fecha_nueva": None, "importe_total": doc.importe_total,
            "moneda": doc.moneda, "cliente_doc": cliente_doc,
            "ref_clave": f"{doc.ruc_emisor}-{ref.tipo_comprobante}-{ref.serie_numero}" if ref else None,
            "especiales": ",".join(esp), "estado": estado,
            "mensaje": f"Separado por tener {', '.join(esp)}" if estado == SEPARADO else None,
            "carga_id": carga_id, "liberado_en": None, "liberado_por": None,
        })
        self.db.add_event(doc.clave, "Queda en la bandeja de preparación"
                          + (f", separado por tener {', '.join(esp)}" if estado == SEPARADO else ""))
        return estado

    def bring_back_rejected_by_date(self, usuario: str) -> Resultado:
        """Trae a la bandeja los rechazados por fecha (2108) o que piden resumen diario (1079)."""
        r = Resultado()
        for row in self.db.by_states(Estado.RECHAZADO):
            if not self._recuperable(row):
                continue
            src = Path(row["archivo"] or "")
            if not src.is_file():
                r.omitidos.append({"clave": row["clave"], "motivo": "No se encontró su TXT"})
                continue
            doc = parse_file(src, parse_filename(src.name))
            data = src.read_bytes()
            self.add(data=data, nombre_final=src.name, doc=doc, sha256=hashlib.sha256(data).hexdigest(),
                     separar=False, carga_id=None, cliente_doc=row["cliente_doc"] or "", usuario=usuario)
            r.hechos.append(row["clave"])
        return r

    def _recuperable(self, row: sqlite3.Row) -> bool:
        if (row["codigo_sunat"] or "") not in RECHAZOS_RECUPERABLES:
            return False
        staged = self.db.staged_one(row["clave"])
        return staged is None or staged["estado"] not in EN_BANDEJA

    # --- lectura ----------------------------------------------------------------
    def rows(self, hoy: date | None = None) -> list[dict[str, Any]]:
        hoy = hoy or date.today()
        staged = {r["clave"]: r for r in self.db.staged(*EN_BANDEJA)}
        out = []
        for r in staged.values():
            out.append(self._describe(r, hoy, staged))
        out.sort(key=lambda x: (x["fecha_efectiva"], x["serie_numero"]))
        return out

    def _describe(self, r: sqlite3.Row, hoy: date, staged: dict[str, sqlite3.Row]) -> dict[str, Any]:
        efectiva = r["fecha_nueva"] or r["fecha_emision"]
        dias = plazo_dias(self.s, r["serie_numero"])
        try:
            vence = date.fromisoformat(efectiva) + timedelta(days=dias)
            restantes = (vence - hoy).days
        except ValueError:
            vence, restantes = None, -1
        ref = self._ref_state(r, staged) if r["ref_clave"] else None
        fuera = restantes < 0
        if r["estado"] == ESPERANDO:
            grupo = "esperando"
        elif r["estado"] == SEPARADO:
            grupo = "separado"
        elif fuera:
            grupo = "fuera_plazo"
        else:
            grupo = "listo"
        return {
            "clave": r["clave"], "tipo": r["tipo"], "serie_numero": r["serie_numero"],
            "familia": familia(r["serie_numero"]), "fecha_emision": r["fecha_emision"],
            "fecha_nueva": r["fecha_nueva"], "fecha_efectiva": efectiva, "importe_total": r["importe_total"],
            "moneda": r["moneda"], "cliente_doc": r["cliente_doc"],
            "especiales": [e for e in (r["especiales"] or "").split(",") if e],
            "estado": r["estado"], "mensaje": r["mensaje"], "grupo": grupo,
            "plazo_dias": dias, "vence": vence.isoformat() if vence else None, "dias_restantes": restantes,
            "fuera_plazo": fuera, "referencia": ref, "carga_id": r["carga_id"],
        }

    def _ref_state(self, r: sqlite3.Row, staged: dict[str, sqlite3.Row] | None = None) -> dict[str, str]:
        clave = r["ref_clave"]
        serie = clave.split("-", 2)[2] if clave.count("-") >= 2 else clave
        doc = self.db.get(clave)
        if doc is not None:
            return {"clave": clave, "serie_numero": serie, "estado": doc["estado"]}
        st = (staged or {}).get(clave) or self.db.staged_one(clave)
        if st is not None:
            return {"clave": clave, "serie_numero": serie, "estado": "EN_COLA" if st["estado"] == EN_COLA else "EN_BANDEJA"}
        return {"clave": clave, "serie_numero": serie, "estado": "DESCONOCIDA"}

    def summary(self, hoy: date | None = None) -> dict[str, Any]:
        hoy = hoy or date.today()
        rows = self.rows(hoy)
        rechazados_fecha = sum(1 for d in self.db.by_states(Estado.RECHAZADO) if self._recuperable(d))
        return {
            "filas": rows,
            "fechas": {f: fechas_disponibles(self.s, f, hoy) for f in ("factura", "boleta")},
            "plazos": {"factura": self.s.plazo_factura_dias, "boleta": self.s.plazo_boleta_dias},
            "hoy": hoy.isoformat(),
            "rechazados_por_fecha": rechazados_fecha,
            "boletas_por_resumen": self.s.boletas_por_resumen,
        }

    # --- acciones ---------------------------------------------------------------
    def release(self, claves: list[str], usuario: str, *, incluir_sin_referencia: bool = False,
                incluir_separados: bool = False, hoy: date | None = None) -> Resultado:
        """Pasa a la cola de envío, en orden de fecha. Las notas cuyo comprobante aún no está
        aprobado quedan esperando y el servicio las suelta cuando lo esté."""
        hoy = hoy or date.today()
        res = Resultado()
        para_resumen: list[sqlite3.Row] = []
        staged = {r["clave"]: r for r in self.db.staged(*EN_BANDEJA)}
        pedidos = [staged[c] for c in dict.fromkeys(claves) if c in staged]
        for c in claves:
            if c not in staged:
                res.omitidos.append({"clave": c, "motivo": "Ya no está en la bandeja"})
        pedidos.sort(key=lambda r: (r["fecha_nueva"] or r["fecha_emision"], r["serie_numero"]))
        for r in pedidos:
            d = self._describe(r, hoy, staged)
            if d["fuera_plazo"]:
                res.omitidos.append({"clave": r["clave"], "motivo": "Fuera de plazo: asígnale una fecha nueva"})
                continue
            if r["estado"] == SEPARADO and not incluir_separados:
                res.omitidos.append({"clave": r["clave"], "motivo": f"Separado ({', '.join(d['especiales'])})"})
                continue
            ref = d["referencia"]
            por_resumen = self.s.boletas_por_resumen and d["familia"] == "boleta"
            if ref is not None and ref["estado"] != Estado.APROBADO:
                if ref["estado"] == "DESCONOCIDA" and not incluir_sin_referencia:
                    res.omitidos.append({"clave": r["clave"], "motivo":
                                         f"{ref['serie_numero']} no pasó por el puente: confirma que ya fue emitido"})
                    continue
                if ref["estado"] != "DESCONOCIDA":
                    self.db.update_staged(r["clave"], estado=ESPERANDO, liberado_por=usuario,
                                          mensaje=f"Espera a que {ref['serie_numero']} esté aprobado")
                    self.db.add_event(r["clave"], f"Liberado por {usuario}; espera a que {ref['serie_numero']} "
                                                  "esté aprobado para enviarse")
                    res.hechos.append(r["clave"])
                    continue
            if por_resumen:
                para_resumen.append(r)
                continue
            self._to_queue(r, usuario)
            res.hechos.append(r["clave"])
        self._enviar_resumen(para_resumen, usuario, res, hoy)
        return res

    def _enviar_resumen(self, filas: list[sqlite3.Row], usuario: str, res: Resultado, hoy: date) -> None:
        if not filas:
            return
        if self.resumenes is None:
            res.omitidos += [{"clave": r["clave"], "motivo": "El resumen diario no está disponible"} for r in filas]
            return
        env = self.resumenes.enviar(filas, usuario, hoy)
        res.hechos += env.incluidos
        res.omitidos += env.omitidos
        res.resumenes += env.resumenes
        res.error = res.error or env.error

    def release_waiting(self, hoy: date | None = None) -> int:
        """Lo llama el servicio en cada vuelta: suelta las notas cuyo comprobante ya fue aprobado."""
        hoy = hoy or date.today()
        n = 0
        notas_resumen: list[sqlite3.Row] = []
        for r in self.db.staged(ESPERANDO):
            ref = self._ref_state(r)
            if ref["estado"] == Estado.APROBADO:
                if self._describe(r, hoy, {})["fuera_plazo"]:
                    self.db.update_staged(r["clave"], estado=PREPARADO,
                                          mensaje="Venció el plazo mientras esperaba: asígnale una fecha nueva")
                    continue
                if self.s.boletas_por_resumen and familia(r["serie_numero"]) == "boleta":
                    notas_resumen.append(r)
                    continue
                self._to_queue(r, r["liberado_por"] or "servicio")
                n += 1
            elif ref["estado"] in (Estado.RECHAZADO, Estado.VALIDACION_ERROR, Estado.DUPLICADO):
                msg = f"{ref['serie_numero']} terminó {ref['estado']}: la nota sigue esperando"
                if r["mensaje"] != msg:
                    self.db.update_staged(r["clave"], mensaje=msg)
        if notas_resumen:
            res = Resultado()
            self._enviar_resumen(notas_resumen, notas_resumen[0]["liberado_por"] or "servicio", res, hoy)
            n += len(res.hechos)
            if res.error:
                log.error("Bandeja: no se pudo enviar el resumen de notas: %s", res.error)
        if n:
            log.info("Bandeja: %d notas liberadas porque su comprobante ya fue aprobado", n)
        return n

    def assign_date(self, claves: list[str], fecha: str, usuario: str, hoy: date | None = None) -> Resultado:
        """fecha vacía = volver a la fecha original."""
        hoy = hoy or date.today()
        res = Resultado()
        for clave in dict.fromkeys(claves):
            r = self.db.staged_one(clave)
            if r is None or r["estado"] not in EN_BANDEJA:
                res.omitidos.append({"clave": clave, "motivo": "Ya no está en la bandeja"})
                continue
            if not fecha:
                self.db.update_staged(clave, fecha_nueva=None, mensaje=None)
                self.db.add_event(clave, f"Fecha de emisión devuelta a la original ({_dmy(r['fecha_emision'])}) "
                                         f"por {usuario}")
                res.hechos.append(clave)
                continue
            permitidas = fechas_disponibles(self.s, familia(r["serie_numero"]), hoy)
            if fecha not in permitidas:
                res.omitidos.append({"clave": clave, "motivo": f"{_dmy(fecha)} no está dentro del plazo de SUNAT"})
                continue
            if fecha < r["fecha_emision"]:
                res.omitidos.append({"clave": clave, "motivo": "La fecha nueva no puede ser anterior a la original"})
                continue
            self.db.update_staged(clave, fecha_nueva=fecha, mensaje=None)
            self.db.add_event(clave, f"Fecha de emisión cambiada de {_dmy(r['fecha_emision'])} a {_dmy(fecha)} "
                                     f"por {usuario} (fuera de plazo)")
            res.hechos.append(clave)
        return res

    def set_separated(self, claves: list[str], separar: bool, usuario: str) -> Resultado:
        res = Resultado()
        for clave in dict.fromkeys(claves):
            r = self.db.staged_one(clave)
            if r is None or r["estado"] not in (PREPARADO, SEPARADO):
                res.omitidos.append({"clave": clave, "motivo": "No se puede cambiar en su estado actual"})
                continue
            self.db.update_staged(clave, estado=SEPARADO if separar else PREPARADO,
                                  mensaje="Separado a mano" if separar else None)
            self.db.add_event(clave, f"{'Separado' if separar else 'Devuelto a la bandeja'} por {usuario}")
            res.hechos.append(clave)
        return res

    def discard(self, claves: list[str], usuario: str) -> Resultado:
        """Saca de la bandeja sin enviar; el TXT se guarda en bandeja/descartados."""
        res = Resultado()
        dest = self.s.bandeja_dir / "descartados"
        for clave in dict.fromkeys(claves):
            r = self.db.staged_one(clave)
            if r is None or r["estado"] not in EN_BANDEJA:
                res.omitidos.append({"clave": clave, "motivo": "Ya no está en la bandeja"})
                continue
            src = Path(r["archivo"])
            if src.is_file():
                dest.mkdir(parents=True, exist_ok=True)
                shutil.move(str(src), str(dest / src.name))
            self.db.unstage(clave)
            self.db.add_event(clave, f"Descartado de la bandeja por {usuario} (no se envió)")
            res.hechos.append(clave)
        return res

    # --- auxiliares ---------------------------------------------------------------
    def _to_queue(self, r: sqlite3.Row, usuario: str) -> None:
        data = Path(r["archivo"]).read_bytes()
        if r["fecha_nueva"]:
            data = redate(data, r["tipo"], r["fecha_nueva"])
        self.s.inbox_dir.mkdir(parents=True, exist_ok=True)
        final = self.s.inbox_dir / r["nombre_final"]
        tmp = final.with_name(final.name + ".subiendo")   # el servicio solo toma *.txt
        tmp.write_bytes(data)
        os.replace(tmp, final)
        self.db.update_staged(r["clave"], estado=EN_COLA, liberado_en=now_iso(), liberado_por=usuario, mensaje=None)
        extra = f" con fecha de emisión {_dmy(r['fecha_nueva'])}" if r["fecha_nueva"] else ""
        self.db.add_event(r["clave"], f"Liberado de la bandeja por {usuario}{extra}; pasa a la cola de envío")


def _dmy(iso: str | None) -> str:
    return f"{iso[8:10]}/{iso[5:7]}/{iso[:4]}" if iso and len(iso) >= 10 else (iso or "")
