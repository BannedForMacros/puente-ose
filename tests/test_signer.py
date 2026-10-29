"""Firma propia: el puente firma el UBL legal y Sovos solo lo transmite."""

import base64
from datetime import UTC, datetime, timedelta

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import BestAvailableEncryption, Encoding, pkcs12
from cryptography.x509.oid import NameOID
from lxml import etree
from pydantic import SecretStr
from signxml import XMLVerifier

from sovos_bridge.parser import parse_file
from sovos_bridge.sbd import Signer, SigningError, build_sbd_signed, build_ubl, validate_sbd
from sovos_bridge.sbd.namespaces import CAC, CBC, ENC, EXT, SBD
from sovos_bridge.service.pipeline import Pipeline
from sovos_bridge.storage import Estado

from conftest import BOLETA_TXT, accepted, decode_sbd

DS = "http://www.w3.org/2000/09/xmldsig#"
NS = {"sbd": SBD, "enc": ENC}


def make_pfx(tmp_path, password="clave", days=365, start=None):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "HOGAR CLINICA PRUEBA"),
                      x509.NameAttribute(NameOID.ORGANIZATIONAL_UNIT_NAME, "RUC 20437310841")])
    start = start or datetime.now(UTC) - timedelta(days=1)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(0x5868E484).not_valid_before(start).not_valid_after(start + timedelta(days=days))
            .sign(key, hashes.SHA256()))
    path = tmp_path / "clinica.pfx"
    path.write_bytes(pkcs12.serialize_key_and_certificates(b"clinica", key, cert, None,
                                                          BestAvailableEncryption(password.encode())))
    return path, cert


def test_firma_en_extension_content_y_verificable(tmp_path):
    pfx, cert = make_pfx(tmp_path)
    signer = Signer.from_pkcs12(pfx, "clave")
    assert signer.info.serial_hex == "5868e484"
    doc = parse_file(BOLETA_TXT)
    signed = signer.sign(build_ubl(doc, ubl_extensions=True), "SIGN_X")
    sig = signed.find(f"{{{EXT}}}UBLExtensions/{{{EXT}}}UBLExtension/{{{EXT}}}ExtensionContent/{{{DS}}}Signature")
    assert sig is not None and sig.get("Id") == "SIGN_X"
    data = etree.tostring(signed, xml_declaration=True, encoding="UTF-8")
    XMLVerifier().verify(data, x509_cert=cert.public_bytes(Encoding.PEM).decode())
    assert signed.findtext(f"{{{CAC}}}Signature/{{{CBC}}}ID") is not None


def test_certificado_vencido_o_clave_incorrecta(tmp_path):
    pfx, _ = make_pfx(tmp_path, start=datetime.now(UTC) - timedelta(days=10), days=5)
    with pytest.raises(SigningError, match="no está vigente"):
        Signer.from_pkcs12(pfx, "clave").sign(build_ubl(parse_file(BOLETA_TXT), ubl_extensions=True), "S")
    with pytest.raises(SigningError, match="contraseña"):
        Signer.from_pkcs12(pfx, "otra")


def test_sbd_firmado_pide_solo_mapping_y_transmission(tmp_path):
    pfx, _ = make_pfx(tmp_path)
    doc = parse_file(BOLETA_TXT)
    signed = Signer.from_pkcs12(pfx, "clave").sign(build_ubl(doc, ubl_extensions=True), "SIGN_X")
    xml = etree.tostring(signed, xml_declaration=True, encoding="UTF-8")
    root = build_sbd_signed(doc, xml)
    validate_sbd(root)
    services = [e.text for e in root.iterfind(".//sbd:BusinessServiceName", NS)]
    assert services == ["Mapping", "Transmission"]
    scopes = {s.findtext("sbd:Type", namespaces=NS): s.findtext("sbd:Identifier", namespaces=NS)
              for s in root.iterfind(".//sbd:Scope", NS)}
    assert scopes["Mapping.TransformDocument"] == "LEGAL-TO-SCI_INVOICE" and scopes["MetaSchema"] == "receipt"
    emb = root.find(".//enc:EmbeddedDocument", NS)
    assert base64.b64decode(emb.text) == xml   # Sovos recibe el archivo firmado sin cambios


def test_pipeline_con_firma_propia(settings, db, fake, tmp_path):
    pfx, cert = make_pfx(tmp_path)
    s = settings.model_copy(update={"sovos_signing_mode": "propio", "cert_path": pfx,
                                    "cert_password": SecretStr("clave")})
    fake.queue("POST", "/v1/documents", accepted("doc-propio"))
    src = s.inbox_dir / BOLETA_TXT.name
    src.write_bytes(BOLETA_TXT.read_bytes())
    r = Pipeline(s, db, fake.client()).process(src)
    assert r.estado == Estado.ENVIADO
    sbd = etree.fromstring(decode_sbd(fake.sent("/v1/documents", "POST")[0]))
    legal = base64.b64decode(sbd.find(".//enc:EmbeddedDocument", NS).text)
    XMLVerifier().verify(legal, x509_cert=cert.public_bytes(Encoding.PEM).decode())


def test_firma_propia_sin_certificado_no_arranca(settings, db):
    with pytest.raises(SigningError, match="CERT_PATH"):
        Pipeline(settings.model_copy(update={"sovos_signing_mode": "propio"}), db, None)


def test_reexportar_con_cifrado_clasico(tmp_path):
    pfx, cert = make_pfx(tmp_path)
    signer = Signer.from_pkcs12(pfx, "clave")
    legacy = tmp_path / "legacy.pfx"
    legacy.write_bytes(signer.export_legacy("clave"))
    again = Signer.from_pkcs12(legacy, "clave")
    assert again.info.serial_hex == signer.info.serial_hex
