"""Ingeniería inversa: el UBL generado debe ser igual al XML aceptado por SUNAT.

El XML de referencia lo generó el puente anterior (firmado y aceptado). Se
compara nodo por nodo (ruta, atributos y texto), ignorando la firma
(ext:UBLExtensions) y normalizando el doble escape del puente anterior
(NI&#209;OS -> NIÑOS) y los espacios finales.
"""

import html

import pytest
from lxml import etree

from sovos_bridge.parser import parse_file
from sovos_bridge.sbd import build_ubl

from conftest import BOLETA_MULTI_TXT, BOLETA_MULTI_XML, BOLETA_TXT, BOLETA_XML, FACTURA_TXT, FACTURA_XML


def flat(root):
    out = []
    for e in root.iter():
        if not isinstance(e.tag, str):
            continue
        path = "/".join(etree.QName(a).localname for a in reversed(list(e.iterancestors())))
        path = f"{path}/{etree.QName(e).localname}"
        if "UBLExtensions" in path:
            continue
        text = html.unescape((e.text or "").strip()) if len(e) == 0 else ""
        out.append((path, tuple(sorted(e.attrib.items())), text))
    return out


@pytest.mark.parametrize("txt,xml", [(BOLETA_MULTI_TXT, BOLETA_MULTI_XML), (FACTURA_TXT, FACTURA_XML)],
                         ids=["boleta-3-items", "factura"])
def test_identico_al_xml_aceptado(txt, xml):
    generado = flat(build_ubl(parse_file(txt)))
    referencia = flat(etree.parse(str(xml)).getroot())
    assert generado == referencia


# La boleta BT05 (TXT) y BT06 (XML) son comprobantes distintos de la misma
# terapia: se ignoran solo los valores propios de cada comprobante.
PROPIOS = {
    "Invoice/ID", "Invoice/IssueDate", "Invoice/IssueTime", "Invoice/DueDate",
    "Invoice/Signature/ID", "Invoice/Signature/DigitalSignatureAttachment/ExternalReference/URI",
    "Invoice/AccountingCustomerParty/Party/PartyIdentification/ID",
    "Invoice/AccountingCustomerParty/Party/PartyLegalEntity/RegistrationName",
    "Invoice/AccountingCustomerParty/Party/PartyLegalEntity/RegistrationAddress/CityName",
    "Invoice/AccountingCustomerParty/Party/PartyLegalEntity/RegistrationAddress/District",
    "Invoice/AccountingCustomerParty/Party/PartyLegalEntity/RegistrationAddress/AddressLine/Line",
}


def test_boleta_misma_estructura_que_bt06():
    def norm(rows):
        return [(p, a, "" if p in PROPIOS else t) for p, a, t in rows]

    generado = norm(flat(build_ubl(parse_file(BOLETA_TXT))))
    referencia = norm(flat(etree.parse(str(BOLETA_XML)).getroot()))
    assert generado == referencia


def test_caracteres_especiales_se_escapan_una_sola_vez():
    xml = etree.tostring(build_ubl(parse_file(BOLETA_MULTI_TXT)), encoding="UTF-8")
    assert "TERAPIA ACUÁTICA – PISCINA NIÑO".encode() in xml
    assert b"&amp;#" not in xml and b"&#" not in xml


def test_ampersand_valido():
    xml = etree.tostring(build_ubl(parse_file(FACTURA_TXT)), encoding="UTF-8")
    assert b"INTEGRA &amp; SALUD MEDIC" in xml
