"""Carga masiva de TXT desde el panel: revisar primero, encolar después.

Cada archivo pasa por las mismas reglas que el servicio (nombre, parser,
validador) y se cruza con la base para no reenviar lo que Sovos ya tiene.
Encolar solo deja el TXT en la bandeja: el servicio lo envía como cualquier
otro, con su control de duplicados.
"""

from __future__ import annotations

import base64
import binascii
import os
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path

from sovos_bridge.config import Settings
from sovos_bridge.logging_setup import mask_doc
from sovos_bridge.parser import ParseError, parse_filename, parse_text, standard_name
from sovos_bridge.parser.tokenizer import decode_bytes
from sovos_bridge.storage import Database, Estado
from sovos_bridge.validation import validate

MAX_FILES = 500
MAX_FILE_BYTES = 512 * 1024
DIAS_AVISO = 3   # boletas con más días desde la emisión: aviso de plazo SUNAT

# Estados en los que Sovos ya tiene el comprobante: no se vuelve a encolar.
YA_EN_SOVOS = {Estado.APROBADO, Estado.ENVIADO, Estado.EN_PROCESO, Estado.DUPLICADO}


@dataclass
class FileCheck:
    nombre: str
    resultado: str                     # valido | advertencia | error | ya_enviado | en_cola
    nombre_final: str = ""
    clave: str = ""
    serie_numero: str = ""
    tipo: str = ""
    fecha_emision: str = ""
    importe_total: str = ""
    moneda: str = ""
    cliente_doc: str = ""
    dias_desde_emision: int | None = None
    reenvio: bool = False              # ya existe con error/rechazo: el servicio lo reprocesa
    mensajes: list[str] = field(default_factory=list)

    @property
    def encolable(self) -> bool:
        return self.resultado in ("valido", "advertencia")


def check_files(s: Settings, db: Database, archivos: list[dict]) -> list[FileCheck]:
    if len(archivos) > MAX_FILES:
        raise ValueError(f"Se pueden revisar hasta {MAX_FILES} archivos por vez")
    vistos: set[str] = set()
    en_cola = _queued_names(s)
    out = []
    for a in archivos:
        c = _check_one(s, db, str(a.get("nombre") or ""), a.get("contenido"), en_cola)
        if c.clave and c.encolable:
            if c.clave in vistos:
                c.resultado = "error"
                c.mensajes.insert(0, "Repetido en esta misma carga")
            vistos.add(c.clave)
        out.append(c)
    return out


def enqueue(s: Settings, db: Database, archivos: list[dict]) -> list[FileCheck]:
    """Vuelve a revisar (el estado pudo cambiar) y deja en la bandeja solo los encolables."""
    contenidos = {str(a.get("nombre") or ""): a.get("contenido") for a in archivos}
    checks = check_files(s, db, archivos)
    s.inbox_dir.mkdir(parents=True, exist_ok=True)
    for c in checks:
        if not c.encolable:
            continue
        data = _decode(contenidos[c.nombre])
        final = s.inbox_dir / c.nombre_final
        tmp = final.with_name(final.name + ".subiendo")   # el servicio solo toma *.txt
        tmp.write_bytes(data)
        os.replace(tmp, final)
        c.mensajes.append("Dejado en la bandeja; el servicio lo enviará en la próxima revisión")
    return checks


def as_dicts(checks: list[FileCheck]) -> list[dict]:
    return [asdict(c) | {"encolable": c.encolable} for c in checks]


# --- auxiliares ----------------------------------------------------------------
def _check_one(s: Settings, db: Database, nombre: str, contenido, en_cola: set[str]) -> FileCheck:
    c = FileCheck(nombre=Path(nombre).name or "(sin nombre)", resultado="error")
    try:
        data = _decode(contenido)
    except ValueError as exc:
        c.mensajes.append(str(exc))
        return c
    try:
        info = parse_filename(c.nombre)
    except ParseError as exc:
        c.mensajes.append(str(exc))
        return c
    if info.es_baja:
        c.mensajes.append("Las comunicaciones de baja todavía no se envían por el puente")
        return c
    c.clave, c.serie_numero, c.tipo = info.clave, info.serie_numero, info.tipo
    c.nombre_final = standard_name(info)
    if c.nombre_final != c.nombre:
        c.mensajes.append(f"Se guardará como {c.nombre_final}")

    text, encoding = decode_bytes(data)
    try:
        doc = parse_text(text, info, encoding)
    except ParseError as exc:
        c.mensajes.append(str(exc))
        return c
    c.fecha_emision, c.importe_total, c.moneda = doc.fecha_emision, doc.importe_total, doc.moneda
    c.cliente_doc = mask_doc(doc.cliente.numero_documento)
    v = validate(doc, info, s.emisor_ruc)
    if not v.ok:
        c.mensajes = v.errores + c.mensajes
        return c

    prev = db.get(info.clave)
    if prev is not None and prev["estado"] in YA_EN_SOVOS:
        c.resultado = "ya_enviado"
        c.mensajes.insert(0, f"Ya está en Sovos con estado {prev['estado']}; no se vuelve a enviar")
        return c
    if prev is not None and prev["envio_incierto"]:
        c.mensajes.insert(0, "Tiene un envío previo incierto: resolverlo desde su detalle antes de subirlo")
        return c
    if c.nombre_final.lower() in en_cola:
        c.resultado = "en_cola"
        c.mensajes.insert(0, "Ya hay un archivo con este comprobante esperando en la bandeja")
        return c

    c.reenvio = prev is not None
    if c.reenvio:
        c.mensajes.insert(0, f"Ya se intentó antes ({prev['estado']}); se volverá a enviar")
    try:
        c.dias_desde_emision = (date.today() - date.fromisoformat(doc.fecha_emision)).days
    except ValueError:
        pass
    avisos = list(v.advertencias)
    if c.dias_desde_emision is not None and c.dias_desde_emision > DIAS_AVISO:
        avisos.insert(0, f"Emitido hace {c.dias_desde_emision} días: confirmar el plazo de envío con contabilidad")
    c.mensajes += avisos
    c.resultado = "advertencia" if avisos else "valido"
    return c


def _decode(contenido) -> bytes:
    if not isinstance(contenido, str) or not contenido:
        raise ValueError("El archivo llegó vacío")
    try:
        data = base64.b64decode(contenido, validate=True)
    except (binascii.Error, ValueError):
        raise ValueError("El archivo no se pudo leer (contenido inválido)") from None
    if not data.strip():
        raise ValueError("El archivo está vacío")
    if len(data) > MAX_FILE_BYTES:
        raise ValueError("El archivo supera 512 KB; no parece un TXT de comprobante")
    return data


def _queued_names(s: Settings) -> set[str]:
    names: set[str] = set()
    for d in (s.inbox_dir, s.processing_dir):
        if d.exists():
            for p in d.glob("*.txt"):
                try:
                    names.add(standard_name(parse_filename(p.name)).lower())
                except ParseError:
                    names.add(p.name.lower())
    return names
