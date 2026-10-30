"""Operaciones de soporte: estado de un comprobante, reintentos y reporte diario."""

from __future__ import annotations

import csv
import logging
from collections import Counter
from pathlib import Path

from sovos_bridge.config import Settings
from sovos_bridge.service import files
from sovos_bridge.storage import Database, Estado, now_iso

log = logging.getLogger(__name__)

REPORT_COLUMNS = [
    "clave", "tipo", "serie_numero", "estado", "fecha_emision", "fecha_emision_original", "moneda", "importe_total",
    "document_id", "codigo_sovos", "codigo_sunat", "mensaje", "enviado_en", "creado_en", "actualizado_en",
]


def status_text(db: Database, serie_numero: str) -> str:
    rows = db.find(serie_numero)
    if not rows:
        return f"No hay registros para {serie_numero}"
    out: list[str] = []
    for r in rows:
        out += [
            f"Comprobante : {r['clave']}",
            f"Estado      : {r['estado']}" + ("  (ENVÍO INCIERTO)" if r["envio_incierto"] else ""),
            f"documentId  : {r['document_id'] or '-'}",
            f"Emisión     : {r['fecha_emision'] or '-'}   Total: {r['moneda'] or ''} {r['importe_total'] or '-'}",
            f"Código      : Sovos={r['codigo_sovos'] or '-'}  SUNAT={r['codigo_sunat'] or '-'}",
            f"Mensaje     : {r['mensaje'] or '-'}",
            f"TXT         : {r['archivo'] or '-'}",
            f"XML / PDF   : {r['xml_path'] or '-'} / {r['pdf_path'] or '-'}",
            f"Enviado     : {r['enviado_en'] or '-'}   Intentos de envío: {r['intentos_envio']}",
            "Historial:",
            *[f"  {e['ts']}  {e['estado'] or '':<16} {e['detalle'] or ''}" for e in db.events(r["clave"])],
            "",
        ]
    return "\n".join(out)


RETRYABLE = (Estado.ERROR_CONEXION, Estado.VALIDACION_ERROR, Estado.RECHAZADO)


def retry_one(s: Settings, db: Database, row, *, forzar: bool = False) -> tuple[bool, str]:
    """Reintenta un comprobante. Devuelve (se hizo algo, descripción).

    - ERROR_CONEXION con documentId (Sovos lo tiene): vuelve a consultar estado, NO reenvía.
    - Envío incierto: solo con `forzar` (tras verificar en el portal que Sovos no lo tiene).
    - Resto (ERROR_CONEXION sin documentId, VALIDACION_ERROR, RECHAZADO): el TXT vuelve a la bandeja.
    """
    clave = row["clave"]
    if row["estado"] not in RETRYABLE:
        return False, f"{clave}: OMITIDO, el estado {row['estado']} no se reintenta"
    txt = Path(row["archivo"] or "")
    if row["estado"] == Estado.ERROR_CONEXION and row["document_id"]:
        dest = None
        if txt.exists():
            files.remove_error_log(txt)
            dest = files.move(txt, s.processing_dir)
        db.set_state(clave, Estado.EN_PROCESO, "retry-errors: se reanuda la consulta de estado")
        db.update_fields(clave, enviado_en=now_iso(), **({"archivo": str(dest)} if dest else {}))
        return True, f"{clave}: se reanuda la consulta de estado (documentId {row['document_id']})"
    if row["envio_incierto"] and not forzar:
        return False, f"{clave}: OMITIDO, envío incierto. Verificar en Sovos y usar --forzar si no existe"
    if not txt.exists():
        return False, f"{clave}: OMITIDO, no se encontró el TXT {txt}"
    if row["envio_incierto"]:
        db.update_fields(clave, envio_incierto=0, envio_iniciado_en=None)
        db.add_event(clave, "retry-errors --forzar: se libera el envío incierto (verificado por TI)")
    files.remove_error_log(txt)
    dest = files.move(txt, s.inbox_dir)
    db.update_fields(clave, archivo=str(dest))
    return True, f"{clave}: devuelto a la bandeja para reenvío ({row['estado']})"


def retry_errors(s: Settings, db: Database, *, forzar: bool = False, todos: bool = False) -> list[str]:
    """Reintenta errores de conexión; con `todos`, también VALIDACION_ERROR y RECHAZADO ya corregidos."""
    estados = [Estado.ERROR_CONEXION] + ([Estado.VALIDACION_ERROR, Estado.RECHAZADO] if todos else [])
    acciones = [retry_one(s, db, row, forzar=forzar)[1] for row in db.by_states(*estados)]
    for a in acciones:
        log.info("retry-errors: %s", a)
    return acciones


def daily_report(s: Settings, db: Database, day: str) -> tuple[Path, Counter]:
    rows = db.day_rows(day)
    s.reports_dir.mkdir(parents=True, exist_ok=True)
    path = s.reports_dir / f"reporte_{day}.csv"
    with path.open("w", newline="", encoding="utf-8-sig") as f:   # BOM para que Excel lea las tildes
        w = csv.writer(f, delimiter=";")
        w.writerow(REPORT_COLUMNS)
        for r in rows:
            w.writerow([r[c] if r[c] is not None else "" for c in REPORT_COLUMNS])
    return path, Counter(r["estado"] for r in rows)


def completar_clientes(db: Database) -> tuple[int, int]:
    """Completa cliente (documento completo, tipo y nombre) y referencia de notas leyendo cada TXT.

    Para comprobantes guardados cuando la base enmascaraba el documento del cliente.
    Devuelve (actualizados, sin TXT legible).
    """
    from sovos_bridge.parser import ParseError, parse_file, parse_filename

    hechos = faltan = 0
    for row in db.search(per_page=1_000_000)[0]:
        if row["tipo"] == "RC" or (row["cliente_nombre"] and "*" not in (row["cliente_doc"] or "")):
            continue
        path = Path(row["archivo"] or "")
        if not path.is_file():
            faltan += 1
            continue
        try:
            doc = parse_file(path, parse_filename(path.name))
        except (ParseError, OSError, ValueError):
            faltan += 1
            continue
        db.update_fields(row["clave"], cliente_doc=doc.cliente.numero_documento,
                         cliente_tipo_doc=doc.cliente.tipo_documento, cliente_nombre=doc.cliente.razon_social,
                         referencia=doc.referencia.serie_numero if doc.referencia else None)
        hechos += 1
    return hechos, faltan
