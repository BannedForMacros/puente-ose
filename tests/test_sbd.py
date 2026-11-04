from datetime import UTC, datetime

import pytest
from lxml import etree

from sovos_bridge.parser import parse_file, parse_filename, parse_text
from sovos_bridge.sbd import SbdOptions, build_sbd, serialize, validate_sbd
from sovos_bridge.sbd.namespaces import CAC, CBC, CRN, DBN, INV, SBD, SCI, SVS

from conftest import BOLETA_TXT, FACTURA_DETRACCION_TXT, FACTURA_TXT, NOTAS_TXT, note_text

NS = {"sbd": SBD, "svs": SVS, "sci": SCI}


def scopes(root):
    out = {}
    for sc in root.iterfind(".//sbd:BusinessScope/sbd:Scope", NS):
        t = sc.findtext("sbd:Type", namespaces=NS)
        out[t] = sc.findtext("sbd:Identifier", namespaces=NS) or sc.findtext(".//sbd:BusinessServiceName", namespaces=NS)
    return out


def nota(tipo, serie):
    info = parse_filename(f"20437310841-{tipo}-{serie}-00000001.txt")
    return parse_text(note_text(tipo, serie), info)


def test_boleta_header():
    root = build_sbd(parse_file(BOLETA_TXT), now=datetime(2026, 10, 28, 15, 0, tzinfo=UTC))
    validate_sbd(root)
    assert root.tag == f"{{{SBD}}}StandardBusinessDocument"
    h = root.find("sbd:StandardBusinessDocumentHeader", NS)
    assert h.findtext("sbd:HeaderVersion", namespaces=NS) == "1.0"
    sender = h.find("sbd:Sender/sbd:Identifier", NS)
    assert (sender.text, sender.get("Authority")) == ("20437310841", "PE")
    receiver = h.find("sbd:Receiver/sbd:Identifier", NS)
    assert (receiver.text, receiver.get("Authority")) == ("00000000", "PE")
    di = h.find("sbd:DocumentIdentification", NS)
    assert di.findtext("sbd:Standard", namespaces=NS) == "urn:oasis:names:specification:ubl:schema:xsd:Invoice-2"
    assert di.findtext("sbd:TypeVersion", namespaces=NS) == "2.1"
    assert di.findtext("sbd:Type", namespaces=NS) == "Invoice"
    assert di.findtext("sbd:CreationDateAndTime", namespaces=NS) == "2026-10-28T15:00:00Z"
    assert scopes(root) == {
        "Country": "PE", "Version": "2.1", "ProcessType": "Outbound", "CompanyCode": "20437310841",
        "SenderDocumentId": "20437310841-03-BT05-00125304", "SenderSystemId": "DefaultSystemERP",
        "BusinessCategory": "BusinessToConsumer", "Mapping.OutputSchema": "invoice", "MetaSchema": "receipt",
        "Mapping.TransformDocument": "SCI-TO-LEGAL_INVOICE", "BusinessProcess": "Default",
    }
    assert root.find("svs:SovosDocument/sci:SovosCanonicalInvoice", NS)[0].tag == f"{{{INV}}}Invoice"


def test_factura_b2b_y_forma_pago():
    root = build_sbd(parse_file(FACTURA_TXT))
    validate_sbd(root)
    assert scopes(root)["BusinessCategory"] == "BusinessToBusiness"
    assert b"<cbc:PaymentMeansID>Contado</cbc:PaymentMeansID>" in serialize(root)


@pytest.mark.parametrize("tipo,serie,raiz,schema,transform,categoria", [
    ("07", "BC01", CRN, "creditNote", "SCI-TO-LEGAL_CREDIT_NOTE", "BusinessToConsumer"),
    ("07", "FC01", CRN, "creditNote", "SCI-TO-LEGAL_CREDIT_NOTE", "BusinessToBusiness"),
    ("08", "BD01", DBN, "debitNote", "SCI-TO-LEGAL_DEBIT_NOTE", "BusinessToConsumer"),
])
def test_notas(tipo, serie, raiz, schema, transform, categoria):
    root = build_sbd(nota(tipo, serie))
    validate_sbd(root)
    sc = scopes(root)
    assert (sc["Mapping.OutputSchema"], sc["Mapping.TransformDocument"], sc["BusinessCategory"]) == \
        (schema, transform, categoria)
    ubl = root.find("svs:SovosDocument/sci:SovosCanonicalInvoice", NS)[0]
    assert ubl.tag.startswith(f"{{{raiz}}}")
    xml = serialize(root)
    assert b"<cbc:ReferenceID>BT05-00125304</cbc:ReferenceID>" in xml
    assert b"InvoiceDocumentReference" in xml


def test_nota_variante_raiz_invoice_valida_xsd():
    root = build_sbd(nota("07", "BC01"), SbdOptions(note_root="Invoice"))
    validate_sbd(root)
    assert root.find("svs:SovosDocument/sci:SovosCanonicalInvoice", NS)[0].tag == f"{{{INV}}}Invoice"


def test_sin_firma_y_bien_formado():
    xml = serialize(build_sbd(parse_file(BOLETA_TXT)))
    etree.fromstring(xml)
    assert b"UBLExtensions" not in xml and b"xmldsig" not in xml
    assert "NIÑOS".encode() in xml


@pytest.mark.parametrize("path", NOTAS_TXT, ids=lambda p: p.stem)
def test_notas_reales_validan_xsd(path):
    doc = parse_file(path)
    root = build_sbd(doc)
    validate_sbd(root)
    sc = scopes(root)
    assert (sc["Mapping.OutputSchema"], sc["Mapping.TransformDocument"]) == ("creditNote", "SCI-TO-LEGAL_CREDIT_NOTE")
    assert "MetaSchema" not in sc  # producto pe_creditNote__2.1
    assert sc["BusinessCategory"] == ("BusinessToBusiness" if doc.serie.startswith("F") else "BusinessToConsumer")
    cn = root.find("svs:SovosDocument/sci:SovosCanonicalInvoice", NS)[0]
    assert cn.tag == f"{{{CRN}}}CreditNote"
    ref = doc.referencia.serie_numero
    assert cn.findtext(f"{{{CAC}}}DiscrepancyResponse/{{{CBC}}}ReferenceID") == ref
    assert cn.findtext(f"{{{CAC}}}BillingReference/{{{CAC}}}InvoiceDocumentReference/{{{CBC}}}ID") == ref


def test_sci_sigue_el_ejemplo_oficial_de_sovos_peru():
    """Sin cac:Signature (la agrega Sovos), con ProfileID y PartyTaxScheme como en la colección Postman."""
    root = build_sbd(parse_file(BOLETA_TXT))
    validate_sbd(root)
    inv = root.find("svs:SovosDocument/sci:SovosCanonicalInvoice", NS)[0]
    assert inv.find(f"{{{CAC}}}Signature") is None
    assert inv.findtext(f"{{{CBC}}}ProfileID") == "0101"
    ids = [e.text for e in inv.iterfind(f".//{{{CAC}}}PartyTaxScheme/{{{CBC}}}CompanyID")]
    assert ids == ["20437310841", "00000000"]


def test_ubl_con_detraccion_y_cuotas():
    from sovos_bridge.sbd.ubl_builder import build_ubl
    ubl = build_ubl(parse_file(FACTURA_DETRACCION_TXT), ubl_extensions=True)
    ns = {"cbc": CBC, "cac": CAC}
    assert ubl.xpath("cbc:InvoiceTypeCode/@listID", namespaces=ns) == ["1001"]
    assert ubl.xpath("cbc:Note[@languageLocaleID='2006']/text()", namespaces=ns) == ["Operación sujeta a detracción"]
    pm = ubl.xpath("cac:PaymentMeans", namespaces=ns)[0]
    assert pm.xpath("cbc:ID/text()", namespaces=ns) == ["Detraccion"]
    assert pm.xpath("cac:PayeeFinancialAccount/cbc:ID/text()", namespaces=ns) == ["00-238-003229"]
    terms = [[e.text for e in t] for t in ubl.xpath("cac:PaymentTerms", namespaces=ns)]
    assert terms == [["Detraccion", "022", "12.00", "114.39"],
                     ["FormaPago", "Credito", "838.85"],
                     ["FormaPago", "Cuota001", "838.85", "2026-12-19"]]
    # PaymentMeans va antes que PaymentTerms y ambos antes de TaxTotal (orden del XSD)
    tags = [etree.QName(e).localname for e in ubl]
    assert tags.index("PaymentMeans") < tags.index("PaymentTerms") < tags.index("TaxTotal")
