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
from sovos_bridge.sbd.namespaces import ENC, SBD, SCI, SVS
from sovos_bridge.sbd.ubl_builder import build_ubl

# Tipo -> (Mapping.OutputSchema, MetaSchema, Mapping.TransformDocument).
# Sovos resuelve el producto contratado como pe_<OutputSchema>_<MetaSchema>_<Version>
# (ej. pe_invoice_receipt_2.1 para boletas, pe_creditNote__2.1 para NC), según
# los productos de la organización y la BT05-00125054 aceptada en producción.
MAPPING = {
    "01": ("invoice", "", "SCI-TO-LEGAL_INVOICE"),
    "03": ("invoice", "receipt", "SCI-TO-LEGAL_INVOICE"),
    "07": ("creditNote", "", "SCI-TO-LEGAL_CREDIT_NOTE"),
    "08": ("debitNote", "", "SCI-TO-LEGAL_DEBIT_NOTE"),
}
PRODUCT_VERSION = "2.1"

# Firma propia: documento legal ya firmado; Sovos solo lo mapea y lo transmite.
LEGAL_TRANSFORM = {
    "01": "LEGAL-TO-SCI_INVOICE",
    "03": "LEGAL-TO-SCI_INVOICE",
    "07": "LEGAL-TO-SCI_CREDIT_NOTE",   # sin probar aún en producción
    "08": "LEGAL-TO-SCI_DEBIT_NOTE",    # sin probar aún en producción
}

# La guía de Perú indica Standard Invoice-2 y Type Invoice también para notas.
DOC_STANDARD = "urn:oasis:names:specification:ubl:schema:xsd:Invoice-2"
DOC_TYPE = "Invoice"


@dataclass(frozen=True)
class SbdOptions:
    sender_system_id: str = "DefaultSystemERP"
    note_root: str = "CreditNote"
    business_category: str = "auto"
    sub_schema: str = ""
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
    """SBD con el SCI: Sovos mapea, firma con el certificado configurado en su portal y transmite."""
    output_schema, meta_schema, transform = MAPPING[doc.tipo_comprobante]
    root, bs = _envelope(doc, opts, now, output_schema, meta_schema, transform)
    _business_process(bs, "Default")
    sd = etree.SubElement(root, f"{{{SVS}}}SovosDocument")
    sci = etree.SubElement(sd, f"{{{SCI}}}SovosCanonicalInvoice")
    sci.append(build_ubl(doc, note_root=opts.note_root, forma_pago_defecto=opts.forma_pago_defecto, sovos_sci=True))
    return root


def build_sbd_signed(doc: Document, signed_xml: bytes, opts: SbdOptions = SbdOptions(),
                     now: datetime | None = None) -> etree._Element:
    """SBD con el XML legal ya firmado por el puente: Sovos solo mapea y transmite (no vuelve a firmar)."""
    output_schema, meta_schema, _ = MAPPING[doc.tipo_comprobante]
    root, bs = _envelope(doc, opts, now, output_schema, meta_schema, LEGAL_TRANSFORM[doc.tipo_comprobante])
    _business_process(bs, "Mapping")
    _business_process(bs, "Transmission")
    sd = etree.SubElement(root, f"{{{SVS}}}SovosDocument")
    legal = etree.SubElement(sd, f"{{{SVS}}}SovosLegalDocument")
    emb = etree.SubElement(etree.SubElement(legal, f"{{{ENC}}}Base64Document", nsmap={"enc": ENC}),
                           f"{{{ENC}}}EmbeddedDocument", mimeCode="application/xml", fileName=f"{doc.clave}.xml")
    emb.text = base64.b64encode(signed_xml).decode("ascii")
    return root


def _business_process(bs, service: str) -> None:
    bp = _e(bs, "Scope")
    _e(bp, "Type", "BusinessProcess")
    _e(bp, "InstanceIdentifier", "")
    _e(_e(bp, "BusinessService"), "BusinessServiceName", service)


@dataclass(frozen=True)
class SummaryProduct:
    """Cómo pedirle a Sovos el resumen diario. Sovos no publica estos valores para Perú:
    se configuran (SOVOS_RC_*) y se confirman con el primer resumen aceptado."""
    output_schema: str = "summaryDocuments"
    meta_schema: str = ""
    transform: str = "LEGAL-TO-SCI_SUMMARY_DOCUMENTS"
    version: str = "2.0"


def build_sbd_summary(clave: str, ruc: str, signed_xml: bytes, opts: SbdOptions = SbdOptions(),
                      product: SummaryProduct = SummaryProduct(), now: datetime | None = None) -> etree._Element:
    """SBD del resumen diario ya firmado: Sovos solo lo mapea y lo transmite."""
    root, bs = _envelope_raw(ruc=ruc, receiver="", clave=clave, category="BusinessToConsumer", opts=opts, now=now,
                             output_schema=product.output_schema, meta_schema=product.meta_schema,
                             transform=product.transform, version=product.version)
    _business_process(bs, "Mapping")
    _business_process(bs, "Transmission")
    sd = etree.SubElement(root, f"{{{SVS}}}SovosDocument")
    legal = etree.SubElement(sd, f"{{{SVS}}}SovosLegalDocument")
    emb = etree.SubElement(etree.SubElement(legal, f"{{{ENC}}}Base64Document", nsmap={"enc": ENC}),
                           f"{{{ENC}}}EmbeddedDocument", mimeCode="application/xml", fileName=f"{clave}.xml")
    emb.text = base64.b64encode(signed_xml).decode("ascii")
    return root


def _envelope(doc: Document, opts: SbdOptions, now: datetime | None, output_schema: str, meta_schema: str,
              transform: str):
    category = business_category(doc) if opts.business_category == "auto" else opts.business_category
    return _envelope_raw(ruc=doc.ruc_emisor, receiver=doc.cliente.numero_documento or "", clave=doc.clave,
                         category=category, opts=opts, now=now, output_schema=output_schema,
                         meta_schema=meta_schema, transform=transform, version=PRODUCT_VERSION)


def _envelope_raw(*, ruc: str, receiver: str, clave: str, category: str, opts: SbdOptions, now: datetime | None,
                  output_schema: str, meta_schema: str, transform: str, version: str):
    now = now or datetime.now(UTC)
    root = etree.Element(f"{{{SBD}}}StandardBusinessDocument", nsmap={"sbd": SBD, "svs": SVS, "sci": SCI})
    h = _e(root, "StandardBusinessDocumentHeader")
    _e(h, "HeaderVersion", "1.0")

    sender = _e(h, "Sender")
    _e(sender, "Identifier", ruc, Authority="PE")
    _e(_e(sender, "ContactInformation"), "Contact", ruc)

    rcv = _e(h, "Receiver")
    _e(rcv, "Identifier", receiver, Authority="PE")

    di = _e(h, "DocumentIdentification")
    _e(di, "Standard", DOC_STANDARD)
    _e(di, "TypeVersion", "2.1")
    _e(di, "InstanceIdentifier", clave)
    _e(di, "Type", DOC_TYPE)
    _e(di, "MultipleType", "false")
    _e(di, "CreationDateAndTime", now.strftime("%Y-%m-%dT%H:%M:%SZ"))

    bs = _e(h, "BusinessScope")
    _scope(bs, "Country", "PE")
    _scope(bs, "Version", version)
    _scope(bs, "ProcessType", "Outbound")
    _scope(bs, "CompanyCode", ruc)
    _scope(bs, "SenderDocumentId", clave)
    _scope(bs, "SenderSystemId", opts.sender_system_id)
    _scope(bs, "BusinessCategory", category)
    _scope(bs, "Mapping.OutputSchema", output_schema)
    _scope(bs, "Mapping.TransformDocument", transform)
    if meta_schema:
        _scope(bs, "MetaSchema", meta_schema)
    if opts.sub_schema:
        _scope(bs, "SubSchema", opts.sub_schema)
    return root, bs


def serialize(root: etree._Element) -> bytes:
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8")


def to_base64(xml_bytes: bytes) -> str:
    return base64.b64encode(xml_bytes).decode("ascii")
