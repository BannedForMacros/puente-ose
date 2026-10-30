"""Resumen diario de boletas (SummaryDocuments, SUNAT UBL 2.0 / versión 1.1).

Informa a SUNAT las boletas y sus notas emitidas en una misma fecha. Se usa
cuando una boleta ya no puede enviarse sola (SUNAT responde 1079 "Solo se puede
enviar el comprobante en un resumen diario"). Plazo: hasta el séptimo día
calendario siguiente a la emisión. Máximo 500 líneas por resumen.

  ID            RC-<fecha de generación AAAAMMDD>-<correlativo del día>
  ReferenceDate fecha de emisión de los comprobantes (todas las líneas iguales)
  IssueDate     fecha de generación del resumen
  Línea         tipo, serie-número, cliente, referencia (notas), estado 1=adicionar,
                importe total, importes por tipo de operación e IGV
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from lxml import etree

from sovos_bridge.parser.models import Document, dec
from sovos_bridge.validation.catalogs import AFECTACION_GRATUITA

NS = "urn:sunat:names:specification:ubl:peru:schema:xsd:SummaryDocuments-1"
CAC = "urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2"
CBC = "urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2"
EXT = "urn:oasis:names:specification:ubl:schema:xsd:CommonExtensionComponents-2"
SAC = "urn:sunat:names:specification:ubl:peru:schema:xsd:SunatAggregateComponents-1"
DS = "http://www.w3.org/2000/09/xmldsig#"

MAX_LINEAS = 500
ADICIONAR = "1"

# Catálogo 14 (TXT) -> InstructionID del resumen (catálogo 11)
_PAGOS = (("1001", "01"), ("1003", "02"), ("1002", "03"), ("1000", "04"), ("1004", "05"))
_TRIBUTOS = {"1000": ("IGV", "VAT"), "2000": ("ISC", "EXC"), "9999": ("OTROS", "OTH"), "7152": ("ICBPER", "OTH")}


@dataclass(frozen=True)
class ResumenId:
    fecha_generacion: str      # AAAA-MM-DD
    correlativo: int

    @property
    def id(self) -> str:
        return f"RC-{self.fecha_generacion.replace('-', '')}-{self.correlativo}"


def build_summary(rid: ResumenId, fecha_referencia: str, ruc: str, razon_social: str,
                  docs: list[Document]) -> etree._Element:
    if not docs:
        raise ValueError("El resumen no tiene comprobantes")
    if len(docs) > MAX_LINEAS:
        raise ValueError(f"Un resumen admite hasta {MAX_LINEAS} comprobantes")
    otras = {d.fecha_emision for d in docs} - {fecha_referencia}
    if otras:
        raise ValueError(f"Todos los comprobantes del resumen deben ser del {fecha_referencia}")

    root = etree.Element(f"{{{NS}}}SummaryDocuments",
                         nsmap={None: NS, "cac": CAC, "cbc": CBC, "ext": EXT, "sac": SAC, "ds": DS})
    cbc = lambda parent, tag, text, **a: _el(parent, CBC, tag, text, **a)
    cac = lambda parent, tag: _el(parent, CAC, tag)

    # La firma se inserta aquí (signer.Signer.sign).
    _el(_el(_el(root, EXT, "UBLExtensions"), EXT, "UBLExtension"), EXT, "ExtensionContent")
    cbc(root, "UBLVersionID", "2.0")
    cbc(root, "CustomizationID", "1.1")
    cbc(root, "ID", rid.id)
    cbc(root, "ReferenceDate", fecha_referencia)
    cbc(root, "IssueDate", rid.fecha_generacion)

    sig = cac(root, "Signature")
    cbc(sig, "ID", f"SIGN_{rid.id}")
    sp = cac(sig, "SignatoryParty")
    cbc(cac(sp, "PartyIdentification"), "ID", ruc)
    cbc(cac(sp, "PartyName"), "Name", razon_social)
    cbc(cac(cac(sig, "DigitalSignatureAttachment"), "ExternalReference"), "URI", f"#SIGN_{rid.id}")

    sup = cac(root, "AccountingSupplierParty")
    cbc(sup, "CustomerAssignedAccountID", ruc)
    cbc(sup, "AdditionalAccountID", "6")
    cbc(cac(cac(sup, "Party"), "PartyLegalEntity"), "RegistrationName", razon_social)

    for n, d in enumerate(docs, start=1):
        _line(root, n, d)
    return root


def _line(root, n: int, d: Document) -> None:
    line = _el(root, SAC, "SummaryDocumentsLine")
    cbc = lambda parent, tag, text, **a: _el(parent, CBC, tag, text, **a)
    cur = d.moneda or "PEN"
    cbc(line, "LineID", str(n))
    cbc(line, "DocumentTypeCode", d.tipo_comprobante)
    cbc(line, "ID", d.serie_numero)
    if d.cliente.numero_documento and d.cliente.tipo_documento not in ("", "-"):
        cust = _el(line, CAC, "AccountingCustomerParty")
        cbc(cust, "CustomerAssignedAccountID", d.cliente.numero_documento)
        cbc(cust, "AdditionalAccountID", d.cliente.tipo_documento)
    if d.es_nota and d.referencia:
        ref = _el(_el(line, CAC, "BillingReference"), CAC, "InvoiceDocumentReference")
        cbc(ref, "ID", d.referencia.serie_numero)
        cbc(ref, "DocumentTypeCode", d.referencia.tipo_comprobante)
    cbc(_el(line, CAC, "Status"), "ConditionCode", ADICIONAR)
    _el(line, SAC, "TotalAmount", _money(d.importe_total), currencyID=cur)

    pagos = _pagos(d)
    for code, amount in pagos:
        bp = _el(line, SAC, "BillingPayment")
        cbc(bp, "PaidAmount", _money(amount), currencyID=cur)
        cbc(bp, "InstructionID", code)

    impuestos = {t.codigo: dec(t.monto) for t in d.tributos if t.codigo in _TRIBUTOS}
    impuestos.setdefault("1000", Decimal(0))
    for code in ("1000", "2000", "7152", "9999"):
        if code not in impuestos:
            continue
        tt = _el(line, CAC, "TaxTotal")
        cbc(tt, "TaxAmount", _money(impuestos[code]), currencyID=cur)
        st = _el(tt, CAC, "TaxSubtotal")
        cbc(st, "TaxAmount", _money(impuestos[code]), currencyID=cur)
        sch = _el(_el(st, CAC, "TaxCategory"), CAC, "TaxScheme")
        nombre, tipo = _TRIBUTOS[code]
        cbc(sch, "ID", code)
        cbc(sch, "Name", nombre)
        cbc(sch, "TaxTypeCode", tipo)


def _pagos(d: Document) -> list[tuple[str, Decimal]]:
    """Importes por tipo de operación (gravadas siempre, aunque sea 0; el resto si hay)."""
    out = []
    for cat14, instr in _PAGOS:
        monto = dec(d.totales.get(cat14))
        if instr == "05" and not monto:
            # Gratuitas: si la cabecera no las trae, se toman de los ítems gratuitos.
            monto = sum((dec(i.valor_venta) for i in d.items
                         if any(t.afectacion in AFECTACION_GRATUITA for t in i.tributos)), Decimal(0))
        if instr == "01" or monto:
            out.append((instr, monto))
    return out


def _money(value) -> str:
    return f"{dec(str(value)):.2f}"


def _el(parent, ns: str, tag: str, text: str | None = None, **attrs) -> etree._Element:
    el = etree.SubElement(parent, f"{{{ns}}}{tag}", {k: v for k, v in attrs.items() if v is not None})
    if text is not None:
        el.text = text
    return el
