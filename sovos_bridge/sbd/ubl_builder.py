"""Document -> UBL 2.1 (sin firma), replicando el XML del puente anterior.

Obtenido por ingeniería inversa: con el TXT de samples/txt este módulo genera
los mismos nodos, en el mismo orden y con los mismos atributos que el XML
aceptado por SUNAT en samples/xml (ver tests/test_ubl_reverse.py). Los valores
se copian tal cual del TXT, sin recalcular.

Diferencias intencionales con el puente anterior:
  - No se incluye ext:UBLExtensions (la firma la pone Sovos).
  - Los textos se escriben en UTF-8 y lxml los escapa una sola vez
    (el puente anterior generaba NI&amp;#209;OS en vez de NIÑOS).
"""

from __future__ import annotations

from decimal import Decimal

from lxml import etree

from sovos_bridge.parser.models import Document, Party, Tax, dec
from sovos_bridge.sbd.namespaces import CAC, CBC, ROOTS
from sovos_bridge.validation.catalogs import AFECTACION_GRATUITA, BASE_POR_TRIBUTO, TRIBUTOS

UNECE = "United Nations Economic Commission for Europe"
CAT = "urn:pe:gob:sunat:cpe:see:gem:catalogos:catalogo"


class _B:
    """Atajos para crear nodos cbc/cac."""

    def __init__(self, currency: str):
        self.cur = currency

    @staticmethod
    def cbc(parent, tag: str, text: str | None = None, **attrs) -> etree._Element:
        e = etree.SubElement(parent, f"{{{CBC}}}{tag}", {k: v for k, v in attrs.items() if v is not None})
        if text is not None:
            e.text = text
        return e

    @staticmethod
    def cac(parent, tag: str) -> etree._Element:
        return etree.SubElement(parent, f"{{{CAC}}}{tag}")

    def amount(self, parent, tag: str, value: str) -> etree._Element:
        return self.cbc(parent, tag, value, currencyID=self.cur)


def root_name(doc: Document, note_root: str = "CreditNote") -> str:
    if doc.tipo_comprobante == "07":
        return "CreditNote" if note_root == "CreditNote" else "Invoice"
    if doc.tipo_comprobante == "08":
        return "DebitNote" if note_root == "CreditNote" else "Invoice"
    return "Invoice"


def _fmt2(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.01")))


def build_ubl(doc: Document, *, note_root: str = "CreditNote", forma_pago_defecto: str = "Contado") -> etree._Element:
    name = root_name(doc, note_root)
    ns, prefix = ROOTS[name]
    b = _B(doc.moneda)
    root = etree.Element(f"{{{ns}}}{name}", nsmap={prefix: ns, "cac": CAC, "cbc": CBC})

    # --- cabecera ----------------------------------------------------------
    b.cbc(root, "UBLVersionID", "2.1")
    b.cbc(root, "CustomizationID", "2.0")
    b.cbc(root, "ID", doc.serie_numero)
    b.cbc(root, "IssueDate", doc.fecha_emision)
    if doc.hora_emision:
        b.cbc(root, "IssueTime", doc.hora_emision)
    if name == "Invoice":
        if doc.fecha_vencimiento:
            b.cbc(root, "DueDate", doc.fecha_vencimiento)
        b.cbc(root, "InvoiceTypeCode", doc.tipo_comprobante,
              listAgencyName="PE:SUNAT", listID=doc.tipo_operacion or None, listName="Tipo de Documento",
              listSchemeURI=CAT + "51", listURI=CAT + "01", name="Tipo de Operacion")
    for code, text in doc.leyendas:
        b.cbc(root, "Note", text, languageLocaleID=code)
    b.cbc(root, "DocumentCurrencyCode", doc.moneda,
          listAgencyName=UNECE, listID="ISO 4217 Alpha", listName="Currency")

    if doc.es_nota and doc.referencia:
        _reference(b, root, doc, name)

    _signature(b, root, doc)
    _supplier(b, root, doc.emisor)
    _customer(b, root, doc.cliente)

    if doc.tipo_comprobante == "01":
        pt = b.cac(root, "PaymentTerms")
        b.cbc(pt, "ID", "FormaPago")
        b.cbc(pt, "PaymentMeansID", doc.forma_pago or forma_pago_defecto)

    _tax_total(b, root, doc)

    total_tag = "RequestedMonetaryTotal" if name == "DebitNote" else "LegalMonetaryTotal"
    lmt = b.cac(root, total_tag)
    b.amount(lmt, "LineExtensionAmount", _line_extension_total(doc))
    b.amount(lmt, "TaxInclusiveAmount", doc.importe_total)
    b.amount(lmt, "PayableAmount", doc.importe_total)

    line_tag, qty_tag = {
        "Invoice": ("InvoiceLine", "InvoicedQuantity"),
        "CreditNote": ("CreditNoteLine", "CreditedQuantity"),
        "DebitNote": ("DebitNoteLine", "DebitedQuantity"),
    }[name]
    for item in doc.items:
        line = b.cac(root, line_tag)
        b.cbc(line, "ID", str(int(item.numero_item)) if item.numero_item.isdigit() else item.numero_item)
        b.cbc(line, qty_tag, item.cantidad, unitCode=item.unidad_medida,
              unitCodeListAgencyName=UNECE, unitCodeListID="UN/ECE rec 20")
        b.amount(line, "LineExtensionAmount", item.valor_venta)

        gratuito = any(t.afectacion in AFECTACION_GRATUITA for t in item.tributos)
        acp = b.cac(b.cac(line, "PricingReference"), "AlternativeConditionPrice")
        b.amount(acp, "PriceAmount", item.precio_unitario)
        b.cbc(acp, "PriceTypeCode", "02" if gratuito else "01",
              listAgencyName="PE:SUNAT", listName="Tipo de Precio", listURI=CAT + "16")

        tt = b.cac(line, "TaxTotal")
        b.amount(tt, "TaxAmount", item.total_impuestos)
        for t in item.tributos:
            st = b.cac(tt, "TaxSubtotal")
            b.amount(st, "TaxableAmount", t.base)
            b.amount(st, "TaxAmount", t.monto)
            tc = b.cac(st, "TaxCategory")
            if t.porcentaje:
                b.cbc(tc, "Percent", t.porcentaje)
            if t.afectacion:
                b.cbc(tc, "TaxExemptionReasonCode", t.afectacion,
                      listAgencyName="PE:SUNAT", listName="Afectacion del IGV", listURI=CAT + "07")
            _tax_scheme(b, tc, t.codigo)

        it = b.cac(line, "Item")
        b.cbc(it, "Description", item.descripcion)
        if item.codigo:
            b.cbc(b.cac(it, "SellersItemIdentification"), "ID", item.codigo)
        b.amount(b.cac(line, "Price"), "PriceAmount", item.valor_unitario)

    return root


def _reference(b: _B, root, doc: Document, name: str) -> None:
    ref = doc.referencia
    if name != "Invoice":
        dr = b.cac(root, "DiscrepancyResponse")
        b.cbc(dr, "ReferenceID", ref.serie_numero)
        b.cbc(dr, "ResponseCode", ref.codigo_motivo, listAgencyName="PE:SUNAT",
              listName="Tipo de nota de credito" if name == "CreditNote" else "Tipo de nota de debito",
              listURI=CAT + ("09" if name == "CreditNote" else "10"))
        b.cbc(dr, "Description", ref.descripcion_motivo or ref.codigo_motivo)
    br = b.cac(root, "BillingReference")
    idr = b.cac(br, "InvoiceDocumentReference")
    b.cbc(idr, "ID", ref.serie_numero)
    b.cbc(idr, "DocumentTypeCode", ref.tipo_comprobante, listAgencyName="PE:SUNAT",
          listName="Tipo de Documento", listURI=CAT + "01")
    if name == "Invoice":
        # Variante SCI con raíz Invoice (a confirmar con Sovos): el motivo viaja
        # en el documento de referencia porque Invoice no tiene DiscrepancyResponse.
        b.cbc(idr, "DocumentStatusCode", ref.codigo_motivo)
        if ref.descripcion_motivo:
            b.cbc(idr, "DocumentDescription", ref.descripcion_motivo)


def _signature(b: _B, root, doc: Document) -> None:
    sid = f"SIGN_{doc.clave}"
    sg = b.cac(root, "Signature")
    b.cbc(sg, "ID", sid)
    sp = b.cac(sg, "SignatoryParty")
    b.cbc(b.cac(sp, "PartyIdentification"), "ID", doc.emisor.numero_documento)
    b.cbc(b.cac(sp, "PartyName"), "Name", doc.emisor.nombre_comercial or doc.emisor.razon_social)
    b.cbc(b.cac(b.cac(sg, "DigitalSignatureAttachment"), "ExternalReference"), "URI", "#" + sid)


def _party_id(b: _B, party_el, p: Party) -> None:
    b.cbc(b.cac(party_el, "PartyIdentification"), "ID", p.numero_documento,
          schemeAgencyName="PE:SUNAT", schemeID=p.tipo_documento, schemeName="Documento de Identidad",
          schemeURI=CAT + "06")


def _address(b: _B, ple, p: Party, with_location: bool) -> None:
    values = (p.urbanizacion, p.provincia, p.departamento, p.distrito, p.direccion, p.pais)
    if not with_location and not any(values):
        return
    a = b.cac(ple, "RegistrationAddress")
    if with_location:
        if p.ubigeo:
            b.cbc(a, "ID", p.ubigeo, schemeAgencyName="PE:INEI", schemeName="Ubigeos")
        if p.codigo_establecimiento:
            b.cbc(a, "AddressTypeCode", p.codigo_establecimiento)
    if p.urbanizacion:
        b.cbc(a, "CitySubdivisionName", p.urbanizacion)
    if p.provincia:
        b.cbc(a, "CityName", p.provincia)
    if p.departamento:
        b.cbc(a, "CountrySubentity", p.departamento)
    if p.distrito:
        b.cbc(a, "District", p.distrito)
    if p.direccion:
        b.cbc(b.cac(a, "AddressLine"), "Line", p.direccion)
    if p.pais:
        b.cbc(b.cac(a, "Country"), "IdentificationCode", p.pais,
              listAgencyName=UNECE, listID="ISO 3166-1", listName="Country")


def _supplier(b: _B, root, p: Party) -> None:
    party = b.cac(b.cac(root, "AccountingSupplierParty"), "Party")
    _party_id(b, party, p)
    if p.nombre_comercial:
        b.cbc(b.cac(party, "PartyName"), "Name", p.nombre_comercial)
    ple = b.cac(party, "PartyLegalEntity")
    b.cbc(ple, "RegistrationName", p.razon_social)
    _address(b, ple, p, with_location=True)


def _customer(b: _B, root, p: Party) -> None:
    party = b.cac(b.cac(root, "AccountingCustomerParty"), "Party")
    _party_id(b, party, p)
    ple = b.cac(party, "PartyLegalEntity")
    b.cbc(ple, "RegistrationName", p.razon_social)
    _address(b, ple, p, with_location=False)


def _tax_scheme(b: _B, parent, code: str) -> None:
    nombre, tipo = TRIBUTOS.get(code, ("", ""))
    sc = b.cac(parent, "TaxScheme")
    b.cbc(sc, "ID", code, schemeAgencyName="PE:SUNAT", schemeName="Codigo de tributos", schemeURI=CAT + "05")
    b.cbc(sc, "Name", nombre)
    b.cbc(sc, "TaxTypeCode", tipo)


def _tax_total(b: _B, root, doc: Document) -> None:
    tt = b.cac(root, "TaxTotal")
    b.amount(tt, "TaxAmount", doc.total_impuestos)
    for t in doc.tributos:
        st = b.cac(tt, "TaxSubtotal")
        b.amount(st, "TaxableAmount", _base(doc, t))
        b.amount(st, "TaxAmount", t.monto)
        _tax_scheme(b, b.cac(st, "TaxCategory"), t.codigo)


def _base(doc: Document, t: Tax) -> str:
    return doc.totales.get(BASE_POR_TRIBUTO.get(t.codigo, ""), "") or "0.00"


def _line_extension_total(doc: Document) -> str:
    """Valor de venta: gravadas + inafectas + exoneradas + exportación.

    Si solo uno tiene monto (caso habitual) se copia su texto tal cual.
    """
    present = [doc.totales[c] for c in ("1001", "1002", "1003", "1000") if dec(doc.totales.get(c)) != 0]
    if len(present) == 1:
        return present[0]
    if not present:
        return doc.totales.get("1001") or "0.00"
    return _fmt2(sum((dec(v) for v in present), Decimal(0)))
