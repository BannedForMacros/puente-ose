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
    txt = note_text(ref="REF|||")
    r = validate(parse_text(txt, info), info, RUC)
    assert any("documento de referencia" in e for e in r.errores)


def test_descuadre_grande_es_advertencia():
    txt = BOLETA_TXT.read_text(encoding="utf-8").replace("|30.00||0.00|", "|35.00||0.00|")
    info = parse_filename(BOLETA_TXT.name)
    r = validate(parse_text(txt, info), info, RUC)
    assert r.ok
    assert any(w.startswith("Descuadre: valor de venta + impuestos") for w in r.advertencias)
