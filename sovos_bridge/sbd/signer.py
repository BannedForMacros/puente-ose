"""Firma digital del XML legal (UBL 2.1 SUNAT) con el certificado propio del emisor.

Se usa cuando SOVOS_SIGNING_MODE=propio: el puente firma el comprobante con el
certificado de la clínica registrado en SUNAT (SOL > Registro de certificado
digital) y Sovos solo lo transmite al OSE (BusinessService Mapping +
Transmission), sin volver a firmarlo. Comprobado en producción: Sovos presenta
el archivo byte por byte como se le envía.

Formato SUNAT: firma enveloped dentro de ext:UBLExtensions/ext:UBLExtension/
ext:ExtensionContent, Reference URI="", C14N 1.0 y certificado X.509 en KeyInfo.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import base64
import hashlib

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.serialization import Encoding, PrivateFormat, pkcs12
from cryptography.x509 import Certificate
from lxml import etree

EXT = "urn:oasis:names:specification:ubl:schema:xsd:CommonExtensionComponents-2"
DS = "http://www.w3.org/2000/09/xmldsig#"
C14N = "http://www.w3.org/TR/2001/REC-xml-c14n-20010315"
ENVELOPED = "http://www.w3.org/2000/09/xmldsig#enveloped-signature"
RSA_SHA256 = "http://www.w3.org/2001/04/xmldsig-more#rsa-sha256"
SHA256 = "http://www.w3.org/2001/04/xmlenc#sha256"


class SigningError(Exception):
    """El certificado no se pudo usar para firmar. El mensaje se muestra a TI."""


@dataclass(frozen=True)
class CertInfo:
    serial_hex: str
    subject: str
    not_before: datetime
    not_after: datetime


class Signer:
    def __init__(self, key, cert: Certificate, chain: list[Certificate] | None = None):
        self._key = key
        self._cert = cert
        self._chain = chain or []

    @classmethod
    def from_pkcs12(cls, path: Path, password: str) -> "Signer":
        try:
            data = Path(path).read_bytes()
        except OSError as exc:
            raise SigningError(f"No se pudo leer el certificado {path}: {exc.strerror}") from None
        try:
            key, cert, chain = pkcs12.load_key_and_certificates(data, password.encode() if password else None)
        except ValueError:
            raise SigningError("No se pudo abrir el certificado: contraseña incorrecta o archivo no es .pfx/.p12") from None
        if key is None or cert is None:
            raise SigningError("El archivo .pfx no contiene la clave privada y el certificado")
        return cls(key, cert, list(chain or []))

    def export_legacy(self, password: str) -> bytes:
        """Mismo certificado y clave en un .pfx con cifrado clásico (PBE-SHA1-3DES, MAC SHA1).

        Algunos sistemas Java no abren .pfx cifrados con AES-256/PBKDF2 y responden
        "keystore password was incorrect" aunque la contraseña sea correcta.
        """
        enc = (PrivateFormat.PKCS12.encryption_builder().kdf_rounds(2048)
               .key_cert_algorithm(pkcs12.PBES.PBESv1SHA1And3KeyTripleDESCBC)
               .hmac_hash(hashes.SHA1()).build(password.encode()))
        return pkcs12.serialize_key_and_certificates(b"certificado", self._key, self._cert, self._chain or None, enc)

    @property
    def info(self) -> CertInfo:
        c = self._cert
        return CertInfo(f"{c.serial_number:x}", c.subject.rfc4514_string(),
                        c.not_valid_before_utc, c.not_valid_after_utc)

    def check_validity(self, now: datetime | None = None) -> None:
        now = now or datetime.now(UTC)
        i = self.info
        if not (i.not_before <= now <= i.not_after):
            raise SigningError(f"El certificado no está vigente (válido del {i.not_before:%d/%m/%Y} "
                               f"al {i.not_after:%d/%m/%Y})")

    def sign(self, root: etree._Element, signature_id: str) -> etree._Element:
        """Devuelve una copia firmada del UBL. `root` debe traer ext:UBLExtensions/.../ExtensionContent.

        Estructura igual a la de SUNAT/Escontech: un solo Transform (enveloped-signature),
        C14N 1.0 inclusivo, RSA-SHA256 y el certificado en KeyInfo/X509Data.
        """
        self.check_validity()
        doc = etree.fromstring(etree.tostring(root))
        content = doc.find(f"{{{EXT}}}UBLExtensions/{{{EXT}}}UBLExtension/{{{EXT}}}ExtensionContent")
        if content is None:
            raise SigningError("El UBL no tiene ext:UBLExtensions/ext:ExtensionContent para la firma")

        # Digest del documento sin la firma (transform enveloped + C14N implícito).
        digest = base64.b64encode(hashlib.sha256(etree.tostring(doc, method="c14n")).digest()).decode()

        ds = lambda parent, tag, **attrs: etree.SubElement(parent, f"{{{DS}}}{tag}", attrs)
        sig = etree.SubElement(content, f"{{{DS}}}Signature", {"Id": signature_id}, nsmap={"ds": DS})
        si = ds(sig, "SignedInfo")
        ds(si, "CanonicalizationMethod", Algorithm=C14N)
        ds(si, "SignatureMethod", Algorithm=RSA_SHA256)
        ref = ds(si, "Reference", URI="")
        ds(ds(ref, "Transforms"), "Transform", Algorithm=ENVELOPED)
        ds(ref, "DigestMethod", Algorithm=SHA256)
        ds(ref, "DigestValue").text = digest

        # SignedInfo canonicalizado en su contexto (C14N inclusivo hereda los namespaces del documento).
        value = self._key.sign(etree.tostring(si, method="c14n"), padding.PKCS1v15(), hashes.SHA256())
        ds(sig, "SignatureValue").text = base64.b64encode(value).decode()
        cert_b64 = base64.b64encode(self._cert.public_bytes(Encoding.DER)).decode()
        ds(ds(ds(sig, "KeyInfo"), "X509Data"), "X509Certificate").text = cert_b64
        return doc
