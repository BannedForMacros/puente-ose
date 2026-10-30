"""Envío de boletas y sus notas en resúmenes diarios (SummaryDocuments) por Sovos.

Desde la bandeja: se agrupan por fecha de emisión (efectiva, si se les asignó una
nueva), en tramos de hasta 500, y cada tramo va en un resumen RC-AAAAMMDD-N
firmado por la clínica. El resumen se sigue como un documento más (tipo RC):
cuando SUNAT lo acepta o rechaza, el resultado se aplica a cada comprobante
incluido (propagar). Mientras tanto quedan EN_RESUMEN y no pueden reenviarse.

Reglas previas (lo que no cumple se queda en la bandeja con el motivo):
  - solo boletas y notas de boleta (serie B)
  - dentro del plazo del resumen (PLAZO_BOLETA_DIAS desde la emisión)
  - una nota solo va si su boleta ya está aprobada (si no, SUNAT rechaza el resumen entero)
  - nada que Sovos ya tenga
"""

from __future__ import annotations

import logging
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from lxml import etree

from sovos_bridge.config import Settings
from sovos_bridge.logging_setup import mask_doc
from sovos_bridge.parser import parse_filename, parse_text
from sovos_bridge.parser.models import Document, dec
from sovos_bridge.parser.tokenizer import decode_bytes
from sovos_bridge.sbd import SchemaError, Signer, SigningError, serialize, to_base64, validate_sbd
from sovos_bridge.sbd.sbd_builder import SbdOptions, SummaryProduct, build_sbd_summary
from sovos_bridge.sbd.summary_builder import MAX_LINEAS, ResumenId, build_summary
from sovos_bridge.service import files
from sovos_bridge.sovos import SovosAuthError, SovosClient, SovosClientError, SovosConnectionError
from sovos_bridge.storage import Database, Estado

log = logging.getLogger(__name__)

TIPO_RESUMEN = "RC"
YA_EN_SOVOS = {Estado.APROBADO, Estado.ENVIADO, Estado.EN_PROCESO, Estado.DUPLICADO, Estado.EN_RESUMEN}


@dataclass
class Envio:
    resumenes: list[dict[str, Any]] = field(default_factory=list)   # {id, fecha, comprobantes, document_id}
    incluidos: list[str] = field(default_factory=list)
    omitidos: list[dict[str, str]] = field(default_factory=list)
    error: str = ""


def es_familia_boleta(serie_numero: str) -> bool:
    return serie_numero[:1].upper() == "B"


def product(s: Settings) -> SummaryProduct:
    return SummaryProduct(output_schema=s.sovos_rc_output_schema, meta_schema=s.sovos_rc_meta_schema,
                          transform=s.sovos_rc_transform, version=s.sovos_rc_version)


class Resumenes:
    def __init__(self, settings: Settings, db: Database, client: SovosClient | None, signer: Signer | None):
        self.s = settings
        self.db = db
        self.client = client
        self.signer = signer

    def enviar(self, staged: list[sqlite3.Row], usuario: str, hoy: date | None = None) -> Envio:
        """Arma, firma y envía los resúmenes para estas filas de la bandeja."""
        from sovos_bridge.service.bandeja import redate   # evita import circular

        hoy = hoy or date.today()
        env = Envio()
        if self.client is None or self.signer is None:
            env.error = "Falta la conexión con Sovos o el certificado: revisa Configuración"
            return env

        por_fecha: dict[str, list[tuple[sqlite3.Row, Document]]] = defaultdict(list)
        for r in staged:
            motivo = self._motivo_excluir(r, hoy)
            if motivo:
                env.omitidos.append({"clave": r["clave"], "motivo": motivo})
                continue
            data = Path(r["archivo"]).read_bytes()
            if r["fecha_nueva"]:
                data = redate(data, r["tipo"], r["fecha_nueva"])
            text, enc = decode_bytes(data)
            doc = parse_text(text, parse_filename(r["nombre_final"]), enc)
            por_fecha[doc.fecha_emision].append((r, doc))

        for fecha in sorted(por_fecha):
            items = sorted(por_fecha[fecha], key=lambda x: (x[1].tipo_comprobante != "03", x[1].serie_numero))
            for i in range(0, len(items), MAX_LINEAS):
                tramo = items[i:i + MAX_LINEAS]
                try:
                    self._enviar_tramo(fecha, tramo, usuario, hoy, env)
                except _EnvioFallido as exc:
                    env.error = str(exc)
                    for r, _ in tramo:
                        env.omitidos.append({"clave": r["clave"], "motivo": str(exc)})
                    return env   # si Sovos rechaza el formato, no tiene sentido seguir con las demás fechas
        return env

    # ------------------------------------------------------------------------
    def _motivo_excluir(self, r: sqlite3.Row, hoy: date) -> str:
        if not es_familia_boleta(r["serie_numero"]):
            return "Las facturas no van en resumen diario"
        efectiva = r["fecha_nueva"] or r["fecha_emision"]
        if date.fromisoformat(efectiva) + timedelta(days=self.s.plazo_boleta_dias) < hoy:
            return (f"Fuera del plazo del resumen diario ({self.s.plazo_boleta_dias} días desde la emisión): "
                    "asígnale una fecha nueva")
        prev = self.db.get(r["clave"])
        if prev is not None and prev["estado"] in YA_EN_SOVOS:
            return f"Ya está en Sovos ({prev['estado']})"
        if r["ref_clave"]:
            ref = self.db.get(r["ref_clave"])
            if ref is None or ref["estado"] != Estado.APROBADO:
                return "Su boleta aún no está aprobada"
        return ""

    def _enviar_tramo(self, fecha: str, tramo: list[tuple[sqlite3.Row, Document]], usuario: str, hoy: date,
                      env: Envio) -> None:
        ruc = self.s.emisor_ruc
        rid = ResumenId(hoy.isoformat(), self.db.next_summary_number(ruc, hoy.isoformat()))
        clave = f"{ruc}-{TIPO_RESUMEN}-{rid.id[3:]}"
        docs = [d for _, d in tramo]
        razon = docs[0].emisor.razon_social

        try:
            xml = build_summary(rid, fecha, ruc, razon, docs)
            signed = self.signer.sign(xml, f"SIGN_{rid.id}")
        except (ValueError, SigningError) as exc:
            raise _EnvioFallido(f"No se pudo armar el resumen {rid.id}: {exc}") from None
        # Solo ASCII, igual que los comprobantes: Sovos altera los bytes no ASCII.
        signed_xml = b'<?xml version="1.0" encoding="UTF-8"?>\n' + etree.tostring(
            signed, encoding="US-ASCII", xml_declaration=False)
        out = files.month_dir(self.s.output_dir)
        out.mkdir(parents=True, exist_ok=True)
        xml_path = out / f"{clave}.firmado-local.xml"
        xml_path.write_bytes(signed_xml)

        sbd = build_sbd_summary(clave, ruc, signed_xml, SbdOptions(sender_system_id=self.s.sovos_sender_system_id),
                                product(self.s))
        try:
            validate_sbd(sbd)
        except SchemaError as exc:
            raise _EnvioFallido(f"El sobre del resumen {rid.id} no es válido: {exc}") from None
        raw = serialize(sbd)
        sbd_path = files.month_dir(self.s.sbd_dir) / f"{clave}.sbd.xml"
        sbd_path.parent.mkdir(parents=True, exist_ok=True)
        sbd_path.write_bytes(raw)

        total = sum((dec(d.importe_total) for d in docs), dec("0"))
        self.db.reserve(clave, tipo=TIPO_RESUMEN, serie_numero=rid.id, archivo=str(xml_path), fecha_emision=fecha,
                        moneda="PEN", importe_total=f"{total:.2f}", cliente_doc="")
        self.db.update_fields(clave, sbd_path=str(sbd_path))
        self.db.add_event(clave, f"Resumen diario de {len(docs)} comprobantes del {fecha}, armado por {usuario}")
        self.db.mark_sending(clave)
        try:
            document_id = self.client.send_document(to_base64(raw))
        except SovosClientError as exc:
            msg = f"Sovos rechazó el resumen {rid.id} (HTTP {exc.status}): {exc.body[:300]}"
            self.db.set_state(clave, Estado.VALIDACION_ERROR, msg, mensaje=msg, envio_iniciado_en=None)
            raise _EnvioFallido(msg) from None
        except (SovosConnectionError, SovosAuthError) as exc:
            transmitted = getattr(exc, "transmitted", False)
            msg = f"Error de conexión enviando el resumen {rid.id}: {exc}"
            self.db.set_state(clave, Estado.ERROR_CONEXION, msg, mensaje=msg, envio_incierto=int(bool(transmitted)))
            raise _EnvioFallido(msg + (" (puede haber llegado: revisar antes de reintentar)" if transmitted else "")) from None

        self.db.mark_sent(clave, document_id)
        claves = [d.clave for d in docs]
        self.db.save_summary_lines(clave, claves)
        for r, d in tramo:
            self.db.reserve(d.clave, tipo=d.tipo_comprobante, serie_numero=d.serie_numero, archivo=r["archivo"],
                            fecha_emision=d.fecha_emision, moneda=d.moneda, importe_total=d.importe_total,
                            cliente_doc=mask_doc(d.cliente.numero_documento))
            self.db.set_state(d.clave, Estado.EN_RESUMEN, f"Incluido en el resumen diario {rid.id}",
                              mensaje=f"En el resumen diario {rid.id}; esperando a SUNAT", document_id=document_id)
            self.db.update_staged(d.clave, estado="EN_RESUMEN", liberado_por=usuario,
                                  mensaje=f"En el resumen diario {rid.id}")
        env.resumenes.append({"id": rid.id, "fecha": fecha, "comprobantes": len(docs), "document_id": document_id})
        env.incluidos += claves
        log.info("Resumen %s (%d comprobantes del %s) enviado a Sovos, documentId=%s", rid.id, len(docs), fecha,
                 document_id)


class _EnvioFallido(Exception):
    pass


def propagar(db: Database, resumen: sqlite3.Row, aprobado: bool, mensaje: str, codigo_sunat: str | None) -> int:
    """Aplica el resultado del resumen a cada comprobante incluido."""
    rid = resumen["serie_numero"]
    n = 0
    for clave in db.summary_lines(resumen["clave"]):
        row = db.get(clave)
        if row is None or row["estado"] != Estado.EN_RESUMEN:
            continue
        if aprobado:
            msg = f"Aceptado en el resumen diario {rid}"
            db.set_state(clave, Estado.APROBADO, msg, mensaje=msg, codigo_sunat=codigo_sunat or "0")
        else:
            msg = f"El resumen diario {rid} fue rechazado: {mensaje}"
            db.set_state(clave, Estado.RECHAZADO, msg, mensaje=msg, codigo_sunat=codigo_sunat)
            # Vuelve a la bandeja para corregir y reenviar
            if db.staged_one(clave) is not None:
                db.update_staged(clave, estado="PREPARADO", mensaje=msg)
        n += 1
    log.info("Resumen %s %s: aplicado a %d comprobantes", rid, "ACEPTADO" if aprobado else "RECHAZADO", n)
    return n
