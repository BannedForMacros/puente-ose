import base64
from datetime import date

import pytest
from lxml import etree
from signxml import XMLVerifier
from cryptography.hazmat.primitives.serialization import Encoding

from sovos_bridge.parser import parse_filename, parse_text
from sovos_bridge.sbd import Signer
from sovos_bridge.sbd.summary_builder import ResumenId, build_summary
from sovos_bridge.service import resumen as rs
from sovos_bridge.service.bandeja import Bandeja
from sovos_bridge.service.status import StatusPoller
from sovos_bridge.storage import Estado

from conftest import BOLETA_TXT, RUC, accepted, application_response, decode_sbd, note_text, notification, notifications_response
from test_signer import make_pfx

BOLETA = f"{RUC}-03-BT05-00125304"
NOTA = f"{RUC}-07-BC01-00000001"
NS = {"s": "urn:sunat:names:specification:ubl:peru:schema:xsd:SummaryDocuments-1",
      "cbc": "urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2",
      "cac": "urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2",
      "sac": "urn:sunat:names:specification:ubl:peru:schema:xsd:SunatAggregateComponents-1"}


def boleta_doc():
    return parse_text(BOLETA_TXT.read_text(encoding="utf-8"), parse_filename(BOLETA_TXT.name))


def stage(settings, db, name, data):
    doc = parse_text(data.decode("utf-8"), parse_filename(name))
    Bandeja(settings, db).add(data=data, nombre_final=name, doc=doc, sha256="x", separar=False, carga_id=None,
                              cliente_doc="", usuario="Ana")
    return doc


def test_xml_del_resumen_y_firma_verificable(tmp_path):
    d = boleta_doc()
    rid = ResumenId("2026-10-30", 3)
    root = build_summary(rid, d.fecha_emision, RUC, "CLINICA", [d])
    x = lambda q: root.xpath(q, namespaces=NS)
    assert x("string(cbc:ID)") == "RC-20261030-3"
    assert x("string(cbc:ReferenceDate)") == d.fecha_emision and x("string(cbc:IssueDate)") == "2026-10-30"
    assert x("string(cbc:CustomizationID)") == "1.1"
    line = x("sac:SummaryDocumentsLine")[0]
    q = lambda e: line.xpath(e, namespaces=NS)
    assert q("string(cbc:DocumentTypeCode)") == "03" and q("string(cbc:ID)") == d.serie_numero
    assert q("string(cac:Status/cbc:ConditionCode)") == "1"
    assert q("string(sac:TotalAmount)") == f"{float(d.importe_total):.2f}"
    assert q("string(sac:BillingPayment[cbc:InstructionID='01']/cbc:PaidAmount)") == f"{float(d.totales['1001']):.2f}"
    assert q("string(cac:TaxTotal/cac:TaxSubtotal/cac:TaxCategory/cac:TaxScheme/cbc:ID)") == "1000"

    pfx, cert = make_pfx(tmp_path)
    signed = Signer.from_pkcs12(pfx, "clave").sign(root, f"SIGN_{rid.id}")
    XMLVerifier().verify(etree.tostring(signed), x509_cert=cert.public_bytes(Encoding.PEM).decode())


def test_nota_lleva_su_referencia():
    nota = parse_text(note_text(), parse_filename(f"{NOTA}.txt"))
    root = build_summary(ResumenId("2026-10-30", 1), nota.fecha_emision, RUC, "C", [nota])
    assert root.xpath("string(//cac:BillingReference/cac:InvoiceDocumentReference/cbc:ID)", namespaces=NS) == "BT05-00125304"


def test_resumen_exige_misma_fecha_y_maximo_500():
    d = boleta_doc()
    with pytest.raises(ValueError, match="deben ser del"):
        build_summary(ResumenId("2026-10-30", 1), "2026-01-01", RUC, "C", [d])


@pytest.fixture
def signer(tmp_path):
    pfx, _ = make_pfx(tmp_path)
    return Signer.from_pkcs12(pfx, "clave")


def test_bandeja_envia_boletas_por_resumen_y_sunat_lo_acepta(settings, db, fake, signer):
    d = stage(settings, db, BOLETA_TXT.name, BOLETA_TXT.read_bytes())
    hoy = date.fromisoformat(d.fecha_emision)
    fake.queue("POST", "/v1/documents", accepted("doc-rc"))
    client = fake.client()
    tray = Bandeja(settings, db, rs.Resumenes(settings, db, client, signer))
    r = tray.release([BOLETA], "Ana", hoy=hoy)
    assert r.hechos == [BOLETA] and r.resumenes[0]["comprobantes"] == 1 and not r.error
    rc_clave = f"{RUC}-RC-{hoy:%Y%m%d}-1"
    assert db.get(rc_clave)["estado"] == Estado.ENVIADO and db.get(rc_clave)["tipo"] == "RC"
    assert db.get(BOLETA)["estado"] == Estado.EN_RESUMEN
    assert not list(settings.inbox_dir.glob("*.txt"))         # no va una por una
    sbd = decode_sbd(fake.sent("/v1/documents", "POST")[0])
    assert b"summaryDocuments" in sbd and b"LEGAL-TO-SCI_SUMMARY_DOCUMENTS" in sbd
    legal = base64.b64decode(etree.fromstring(sbd).xpath("string(//*[local-name()='EmbeddedDocument'])"))
    assert b"SummaryDocuments" in legal and b"SignatureValue" in legal and max(legal) < 128

    # SUNAT acepta el resumen: cada boleta queda aprobada
    fake.queue("GET", "/notifications", notifications_response(
        notification("n1", "AP", "doc-rc", content=application_response("AP", "200", "0", "El Resumen ha sido aceptado"))))
    StatusPoller(settings, db, client).run_once()
    assert db.get(rc_clave)["estado"] == Estado.APROBADO
    assert db.get(BOLETA)["estado"] == Estado.APROBADO and "RC-" in db.get(BOLETA)["mensaje"]


def test_resumen_rechazado_devuelve_las_boletas_a_la_bandeja(settings, db, fake, signer):
    d = stage(settings, db, BOLETA_TXT.name, BOLETA_TXT.read_bytes())
    hoy = date.fromisoformat(d.fecha_emision)
    fake.queue("POST", "/v1/documents", accepted("doc-rc"))
    client = fake.client()
    Bandeja(settings, db, rs.Resumenes(settings, db, client, signer)).release([BOLETA], "Ana", hoy=hoy)
    fake.queue("GET", "/notifications", notifications_response(
        notification("n1", "RE", "doc-rc", govt="2220", govt_msg="Error en el resumen")))
    StatusPoller(settings, db, client).run_once()
    assert db.get(BOLETA)["estado"] == Estado.RECHAZADO
    assert db.staged_one(BOLETA)["estado"] == "PREPARADO" and "rechazado" in db.staged_one(BOLETA)["mensaje"]


def test_sovos_rechaza_el_formato_no_toca_las_boletas(settings, db, fake, signer):
    import httpx
    d = stage(settings, db, BOLETA_TXT.name, BOLETA_TXT.read_bytes())
    hoy = date.fromisoformat(d.fecha_emision)
    fake.queue("POST", "/v1/documents", httpx.Response(500, json={"message": "unhandled exception"}))
    r = Bandeja(settings, db, rs.Resumenes(settings, db, fake.client(max_retries=0), signer)).release([BOLETA], "Ana", hoy=hoy)
    assert r.error and not r.hechos
    assert db.get(BOLETA) is None and db.staged_one(BOLETA)["estado"] == "PREPARADO"


def test_reglas_de_exclusion(settings, db, fake, signer):
    d = stage(settings, db, BOLETA_TXT.name, BOLETA_TXT.read_bytes())
    stage(settings, db, f"{NOTA}.txt", note_text().replace("2026-03-14", d.fecha_emision).encode())
    tray = Bandeja(settings, db, rs.Resumenes(settings, db, fake.client(), signer))
    hoy = date.fromisoformat(d.fecha_emision)
    lejos = date.fromordinal(hoy.toordinal() + 30)
    r = tray.release([BOLETA], "Ana", hoy=lejos)                       # fuera del plazo del resumen
    assert not r.hechos and "Fuera de plazo" in r.omitidos[0]["motivo"]
    # La nota no va mientras su boleta no esté aprobada: queda esperando
    r = tray.release([NOTA], "Ana", hoy=hoy)
    assert db.staged_one(NOTA)["estado"] == "ESPERANDO_REFERENCIA" and not fake.sent("/v1/documents")
