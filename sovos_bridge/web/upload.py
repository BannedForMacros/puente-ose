"""Carga masiva de TXT desde el panel: revisar primero, encolar después.

Cada archivo pasa por las mismas reglas que el servicio (nombre, parser,
validador) y se cruza con la base para no reenviar lo que Sovos ya tiene.
Confirmar deja los TXT en la bandeja de preparación (service/bandeja.py): nada
sale a Sovos hasta liberarlos desde el panel, donde se separan especiales, se
corrige la fecha de lo vencido y las notas esperan a su comprobante.

Además del número de comprobante, cada archivo se identifica por su huella
SHA-256: así se detecta si es exactamente el mismo archivo subido antes o si
su contenido cambió respecto al que ya se envió. Cada carga confirmada deja
una constancia (quién, cuándo, desde qué equipo y la huella de cada archivo).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import os
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from pathlib import Path

from sovos_bridge.config import Settings
from sovos_bridge.logging_setup import mask_doc
from sovos_bridge.parser import ParseError, parse_filename, parse_text, standard_name
from sovos_bridge.parser.tokenizer import decode_bytes
from sovos_bridge.service import bandeja as bj
from sovos_bridge.storage import Database, Estado
from sovos_bridge.validation import validate

MAX_FILES = 500
MAX_FILE_BYTES = 512 * 1024

# Estados en los que Sovos ya tiene el comprobante: no se vuelve a encolar.
YA_EN_SOVOS = {Estado.APROBADO, Estado.ENVIADO, Estado.EN_PROCESO, Estado.DUPLICADO, Estado.EN_RESUMEN}


@dataclass
class FileCheck:
    nombre: str
    resultado: str                     # valido | advertencia | error | ya_enviado | en_cola | en_bandeja
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
    sha256: str = ""
    bytes: int = 0
    especiales: list[str] = field(default_factory=list)
    vence: str = ""                    # último día que SUNAT lo acepta con su fecha original
    fuera_plazo: bool = False
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


def enqueue(s: Settings, db: Database, archivos: list[dict], *, usuario: str = "",
            origen: str = "", separar_especiales: bool = True) -> tuple[list[FileCheck], int]:
    """Vuelve a revisar (el estado pudo cambiar), deja los válidos en la bandeja de
    preparación y guarda la constancia de la carga. Devuelve las revisiones y el N.º de carga."""
    contenidos = {str(a.get("nombre") or ""): a.get("contenido") for a in archivos}
    checks = check_files(s, db, archivos)
    usuario = usuario or "local"
    carga_id = db.save_upload(usuario=usuario, origen=origen, archivos=[
        {"nombre": c.nombre, "nombre_final": c.nombre_final, "clave": c.clave, "sha256": c.sha256,
         "bytes": c.bytes, "encolado": c.encolable, "resultado": "en_bandeja" if c.encolable else c.resultado,
         "detalle": "; ".join(c.mensajes)} for c in checks])
    tray = bj.Bandeja(s, db)
    for c in checks:
        if not c.encolable:
            continue
        estado = tray.add(data=_decode(contenidos[c.nombre]), nombre_final=c.nombre_final, doc=c._doc,
                          sha256=c.sha256, separar=separar_especiales, carga_id=carga_id,
                          cliente_doc=c.cliente_doc, usuario=usuario)
        c.mensajes.insert(0, "Separado en la bandeja: no se envía hasta que lo liberes" if estado == bj.SEPARADO
                          else "En la bandeja: se envía cuando lo liberes")
    return checks, carga_id


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
    c.sha256, c.bytes = hashlib.sha256(data).hexdigest(), len(data)
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
    c._doc = doc            # para guardarlo en la bandeja; no viaja al panel
    v = validate(doc, info, s.emisor_ruc)
    if not v.ok:
        c.mensajes = v.errores + c.mensajes
        return c

    prev = db.get(info.clave)
    antes = _uploaded_before(db, c)
    igual_al_enviado = _same_as_stored(prev, c.sha256)
    if prev is not None and prev["estado"] in YA_EN_SOVOS:
        c.resultado = "ya_enviado"
        c.mensajes.insert(0, f"Ya está en Sovos con estado {prev['estado']}; no se vuelve a enviar")
        if igual_al_enviado is False and prev["estado"] == Estado.APROBADO:
            c.mensajes.insert(1, "Su contenido es distinto al del comprobante aprobado: un número aprobado no se "
                                 "puede volver a emitir; si hay un error, corregirlo con una nota de crédito")
        elif igual_al_enviado:
            c.mensajes.insert(1, "Es exactamente el mismo archivo que ya se envió")
        c.mensajes += antes
        return c
    if prev is not None and prev["envio_incierto"]:
        c.mensajes.insert(0, "Tiene un envío previo incierto: resolverlo desde su detalle antes de subirlo")
        return c
    staged = db.staged_one(info.clave)
    if staged is not None and staged["estado"] in bj.EN_BANDEJA:
        c.resultado = "en_bandeja"
        c.mensajes.insert(0, "Ya está en la bandeja de preparación" + (
            "" if staged["sha256"] == c.sha256 else
            " con otro contenido: descártalo ahí primero si quieres reemplazarlo"))
        return c
    if c.nombre_final.lower() in en_cola:
        c.resultado = "en_cola"
        c.mensajes.insert(0, "Ya hay un archivo con este comprobante esperando en la bandeja")
        return c

    avisos = antes + list(v.advertencias)
    c.reenvio = prev is not None
    if c.reenvio:
        c.mensajes.insert(0, f"Ya se intentó antes ({prev['estado']}); se volverá a enviar")
        if igual_al_enviado:
            motivo = f": {prev['mensaje']}" if prev["mensaje"] else ""
            avisos.insert(0, f"Es idéntico al archivo que terminó {prev['estado']}{motivo}. Si no se corrigió "
                             "nada (ni el TXT ni el comprobante que referencia), volverá a fallar")
    try:
        c.dias_desde_emision = (date.today() - date.fromisoformat(doc.fecha_emision)).days
    except ValueError:
        pass
    try:
        vence = date.fromisoformat(doc.fecha_emision) + timedelta(days=bj.plazo_dias(s, doc.serie_numero))
        c.vence, c.fuera_plazo = vence.isoformat(), vence < date.today()
        if c.fuera_plazo:
            avisos.insert(0, f"Fuera de plazo: SUNAT lo aceptaba hasta el {vence:%d/%m/%Y}. "
                             "En la bandeja podrás asignarle una fecha nueva")
        elif vence == date.today():
            avisos.insert(0, "Vence hoy: conviene enviarlo primero")
    except ValueError:
        pass
    c.especiales = bj.especiales(doc)
    if c.especiales:
        avisos.append(f"Tiene {', '.join(c.especiales)}: se puede separar para no enviarlo todavía")
    c.mensajes += avisos
    c.resultado = "advertencia" if avisos else "valido"
    return c


def _uploaded_before(db: Database, c: FileCheck) -> list[str]:
    prev = db.last_upload_of(sha256=c.sha256)
    if prev is not None:
        return [f"Este mismo archivo ya se subió el {_fecha_hora(prev['ts'])} por {prev['usuario']} "
                f"(carga N.º {prev['carga_id']})"]
    prev = db.last_upload_of(clave=c.clave)
    if prev is not None:
        return [f"Este comprobante ya se subió el {_fecha_hora(prev['ts'])} por {prev['usuario']} "
                f"(carga N.º {prev['carga_id']}) con un contenido distinto"]
    return []


def _same_as_stored(row, sha256: str) -> bool | None:
    """Compara con el TXT que el puente ya procesó. None si no hay con qué comparar."""
    if row is None or not row["archivo"]:
        return None
    try:
        return hashlib.sha256(Path(row["archivo"]).read_bytes()).hexdigest() == sha256
    except OSError:
        return None


def _fecha_hora(ts: str) -> str:
    return f"{ts[8:10]}/{ts[5:7]}/{ts[:4]} a las {ts[11:16]}" if len(ts) >= 16 else ts


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
