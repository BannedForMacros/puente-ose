"""Sobre SBD (Standard Business Document) de Sovos alrededor del UBL.

Estructura según los XSD oficiales (schemas/sovos) y la guía de Perú:

  sbd:StandardBusinessDocument
    sbd:StandardBusinessDocumentHeader   (Sender, Receiver, DocumentIdentification, BusinessScope)
    svs:SovosDocument
      sci:SovosCanonicalInvoice
        inv:Invoice | crn:CreditNote | dbn:DebitNote   (UBL 2.1 sin firma)
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import UTC, datetime

from lxml import etree

from sovos_bridge.parser.models import Document
from sovos_bridge.sbd.namespaces import SBD, SCI, SVS
from sovos_bridge.sbd.ubl_builder import build_ubl

# Tipo -> (Mapping.OutputSchema, Mapping.TransformDocument). Guía Sovos Perú.
MAPPING = {
    "01": ("UBLInvoice", "SCI-TO-LEGAL_INVOICE"),
    "03": ("UBLInvoice", "SCI-TO-LEGAL_INVOICE"),
    "07": ("UBLCreditNote", "SCI-TO-LEGAL_CREDIT_NOTE"),
    "08": ("UBLDebitNote", "SCI-TO-LEGAL_DEBIT_NOTE"),
}

# La guía de Perú indica Standard Invoice-2 y Type Invoice también para notas.
DOC_STANDARD = "urn:oasis:names:specification:ubl:schema:xsd:Invoice-2"
DOC_TYPE = "Invoice"


@dataclass(frozen=True)
class SbdOptions:
    sender_system_id: str = "DefaultSystemERP"
    note_root: str = "CreditNote"
    forma_pago_defecto: str = "Contado"


def business_category(doc: Document) -> str:
    """Serie B (boletas y sus notas) = B2C; serie F (facturas y sus notas) = B2B.

    Se decide por la serie y no por el documento del cliente: hay boletas
    emitidas a clientes con RUC (ej. BA01-00014480) que siguen siendo B2C.
    """
    return "BusinessToConsumer" if doc.serie.startswith("B") else "BusinessToBusiness"


def _e(parent, tag: str, text: str | None = None, **attrs) -> etree._Element:
    el = etree.SubElement(parent, f"{{{SBD}}}{tag}", attrs)
    if text is not None:
        el.text = text
    return el


def _scope(scope_parent, type_: str, identifier: str) -> None:
    sc = _e(scope_parent, "Scope")
    _e(sc, "Type", type_)
    _e(sc, "InstanceIdentifier", "")
    _e(sc, "Identifier", identifier)


def build_sbd(doc: Document, opts: SbdOptions = SbdOptions(), now: datetime | None = None) -> etree._Element:
    output_schema, transform = MAPPING[doc.tipo_comprobante]
    now = now or datetime.now(UTC)

    root = etree.Element(f"{{{SBD}}}StandardBusinessDocument", nsmap={"sbd": SBD, "svs": SVS, "sci": SCI})
    h = _e(root, "StandardBusinessDocumentHeader")
    _e(h, "HeaderVersion", "1.0")

    sender = _e(h, "Sender")
    _e(sender, "Identifier", doc.ruc_emisor, Authority="PE")
    _e(_e(sender, "ContactInformation"), "Contact", doc.ruc_emisor)

    receiver = _e(h, "Receiver")
    _e(receiver, "Identifier", doc.cliente.numero_documento or "", Authority="PE")

    di = _e(h, "DocumentIdentification")
    _e(di, "Standard", DOC_STANDARD)
    _e(di, "TypeVersion", "2.1")
    _e(di, "InstanceIdentifier", doc.clave)
    _e(di, "Type", DOC_TYPE)
    _e(di, "MultipleType", "false")
    _e(di, "CreationDateAndTime", now.strftime("%Y-%m-%dT%H:%M:%SZ"))

    bs = _e(h, "BusinessScope")
    _scope(bs, "Country", "PE")
    _scope(bs, "ProcessType", "Outbound")
    _scope(bs, "CompanyCode", doc.ruc_emisor)
    _scope(bs, "SenderDocumentId", doc.clave)
    _scope(bs, "SenderSystemId", opts.sender_system_id)
    _scope(bs, "BusinessCategory", business_category(doc))
    _scope(bs, "Mapping.OutputSchema", output_schema)
    _scope(bs, "Mapping.TransformDocument", transform)
    bp = _e(bs, "Scope")
    _e(bp, "Type", "BusinessProcess")
    _e(bp, "InstanceIdentifier", "")
    _e(_e(bp, "BusinessService"), "BusinessServiceName", "Default")

    sd = etree.SubElement(root, f"{{{SVS}}}SovosDocument")
    sci = etree.SubElement(sd, f"{{{SCI}}}SovosCanonicalInvoice")
    sci.append(build_ubl(doc, note_root=opts.note_root, forma_pago_defecto=opts.forma_pago_defecto))
    return root


def serialize(root: etree._Element) -> bytes:
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8")


def to_base64(xml_bytes: bytes) -> str:
    return base64.b64encode(xml_bytes).decode("ascii")
