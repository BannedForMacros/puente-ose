from sovos_bridge.parser import parse_file, parse_filename, parse_text
from sovos_bridge.validation import ruc_valido, validate

from conftest import BOLETA_MULTI_TXT, BOLETA_TXT, FACTURA_TXT, RUC, note_text


def run(path):
    return validate(parse_file(path), parse_filename(path.name), RUC)


def test_muestras_pasan():
    for p in (BOLETA_TXT, BOLETA_MULTI_TXT, FACTURA_TXT):
        r = run(p)
        assert r.ok, r.errores


def test_redondeos_aceptados_por_sunat_no_bloquean():
    # BA01-00014481: IGV de ítems suma 95.14 y la cabecera dice 95.15; SUNAT lo aceptó.
    r = run(BOLETA_MULTI_TXT)
    assert r.ok
    assert any("Redondeo: suma de impuestos" in w for w in r.advertencias)


def test_ruc_digito_verificador():
    assert ruc_valido("20437310841") and ruc_valido("20523760689")
    assert not ruc_valido("20437310842") and not ruc_valido("123")


def test_nombre_y_contenido_no_coinciden(tmp_path):
    p = tmp_path / "20437310841-03-BT05-00999999.txt"
    p.write_bytes(BOLETA_TXT.read_bytes())
    r = run(p)
    assert any("no coincide" in e for e in r.errores)


def test_ruc_emisor_distinto():
    d = parse_file(BOLETA_TXT)
    r = validate(d, parse_filename(BOLETA_TXT.name), "20100000001")
    assert any("RUC emisor" in e for e in r.errores)


def test_serie_de_boleta_en_factura(tmp_path):
    txt = BOLETA_TXT.read_text(encoding="utf-8").replace("|16:22:53|03|", "|16:22:53|01|")
    info = parse_filename("20437310841-01-BT05-00125304.txt")
    r = validate(parse_text(txt, info), info, RUC)
    assert any("factura debe tener serie que inicie con F" in e for e in r.errores)


def test_nota_sin_referencia():
    info = parse_filename("20437310841-07-BC01-00000001.txt")
    txt = note_text(ref="DRF|||")
    r = validate(parse_text(txt, info), info, RUC)
    assert any("documento de referencia" in e for e in r.errores)


def test_descuadre_grande_es_advertencia():
    txt = BOLETA_TXT.read_text(encoding="utf-8").replace("|30.00||0.00|", "|35.00||0.00|")
    info = parse_filename(BOLETA_TXT.name)
    r = validate(parse_text(txt, info), info, RUC)
    assert r.ok
    assert any(w.startswith("Descuadre: valor de venta + impuestos") for w in r.advertencias)


_EMI = ("EMI|6|20437310841|HOGAR CLINICA SAN JUAN DE DIOS CHICLAYO|HOGAR CLINICA SAN JUAN DE DIOS CHICLAYO|140112|"
        "NRO. 10 CAS. PIMENTEL ||CHICLAYO|LAMBAYEQUE|PIMENTEL|PE|||0000")
_REC = "REC|6|20100047218|EMPRESA PRUEBA SAC|AV PRUEBA 1|LIMA|LIMA|LIMA|PE||"


def _factura(cab, dets, vence="2026-10-30", numero="00099901"):
    info = parse_filename(f"{RUC}-01-FT01-{numero}.txt")
    lines = [f"IDE|FT01-{numero}|2026-10-30|09:00:00|01|PEN||{vence}|", _EMI, _REC, cab, *dets]
    return validate(parse_text("\r\n".join(lines) + "\r\n", info), info, RUC)


def test_exonerado_e_inafecto_cuadran():
    r = _factura(
        "CAB|1001|100.00|1002|50.00|1003|30.00|1004|0.00|1000|0.00||||||[1000|18.00|9998|0.00|9997|0.00]||||198.00||"
        "0.00|0101|[1000|CIENTO NOVENTA Y OCHO CON 00/100]|18.00|",
        ["DET|001|A1|CONSULTA|1.00|ZZ|100.00|118.00|[1000|18.00|10|100.00|18.00]|100.00|||||18.00|",
         "DET|002|A2|INAFECTO|1.00|ZZ|50.00|50.00|[9998|0.00|30|50.00|0.00]|50.00|||||0.00|",
         "DET|003|A3|EXONERADO|1.00|NIU|30.00|30.00|[9997|0.00|20|30.00|0.00]|30.00|||||0.00|"])
    assert r.ok, r.errores
    assert not any("cuadre" in w.lower() for w in r.advertencias), r.advertencias


def test_gratuito_no_genera_descuadre():
    r = _factura(
        "CAB|1001|0.00|1002|0.00|1003|0.00|1004|50.00|1000|0.00||||||[9996|9.00]||||0.00||0.00|0101|"
        "[1000|CERO CON 00/100|1002|TRANSFERENCIA GRATUITA]|0.00|",
        ["DET|001|G1|CONSULTA GRATUITA|1.00|ZZ|0.00|59.00|[9996|9.00|11|50.00|18.00]|50.00|||||9.00|"])
    assert r.ok, r.errores
    assert not any("cuadre" in w.lower() for w in r.advertencias), r.advertencias


def test_detraccion_se_bloquea_hasta_tener_sus_datos():
    r = _factura(
        "CAB|1001|1000.00|1002|0.00|1003|0.00|1004|0.00|1000|0.00||||||[1000|180.00]||||1180.00||0.00|1001|"
        "[1000|MIL CIENTO OCHENTA CON 00/100|2006|OPERACION SUJETA A DETRACCION]|180.00|",
        ["DET|001|S1|SERVICIO|1.00|ZZ|1000.00|1180.00|[1000|180.00|10|1000.00|18.00]|1000.00|||||180.00|"])
    assert any("detracción" in e for e in r.errores)


def test_factura_grande_sin_detraccion_y_con_vencimiento_avisa():
    r = _factura(
        "CAB|1001|1000.00|1002|0.00|1003|0.00|1004|0.00|1000|0.00||||||[1000|180.00]||||1180.00||0.00|0101|"
        "[1000|MIL CIENTO OCHENTA CON 00/100]|180.00|",
        ["DET|001|S1|SERVICIO|1.00|ZZ|1000.00|1180.00|[1000|180.00|10|1000.00|18.00]|1000.00|||||180.00|"],
        vence="2026-11-29")
    assert r.ok, r.errores
    assert any("sin detracción" in w for w in r.advertencias)
    assert any("al crédito" in w for w in r.advertencias)
