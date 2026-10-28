import pytest

from sovos_bridge.parser import ParseError, parse_file, parse_filename, parse_text
from sovos_bridge.parser.tokenizer import decode_bytes, split_fields, split_group

from conftest import BOLETA_MULTI_TXT, BOLETA_TXT, note_text


def test_split_respeta_corchetes():
    assert split_fields("CAB|1001|[1000|4.58]|x||") == ["CAB", "1001", "[1000|4.58]", "x", "", ""]
    assert split_group("[1000|4.58|10|25.42|18.00]") == ["1000", "4.58", "10", "25.42", "18.00"]
    assert split_group("") == []


def test_nombre_de_archivo():
    info = parse_filename("20437310841-03-BT05-00125304.txt")
    assert (info.ruc, info.tipo, info.serie_numero) == ("20437310841", "03", "BT05-00125304")
    assert info.clave == "20437310841-03-BT05-00125304"
    assert parse_filename("20437310841-RA-20261022-1.txt").es_baja
    with pytest.raises(ParseError):
        parse_filename("boleta.txt")


def test_boleta_de_muestra():
    d = parse_file(BOLETA_TXT)
    assert d.serie_numero == "BT05-00125304" and d.tipo_comprobante == "03"
    assert (d.fecha_emision, d.hora_emision, d.fecha_vencimiento, d.moneda) == \
        ("2026-10-22", "16:22:53", "2026-10-22", "PEN")
    assert d.emisor.numero_documento == "20437310841"
    assert d.emisor.ubigeo == "140112" and d.emisor.codigo_establecimiento == "0000"
    assert (d.emisor.provincia, d.emisor.departamento, d.emisor.distrito) == ("CHICLAYO", "LAMBAYEQUE", "PIMENTEL")
    assert (d.cliente.tipo_documento, d.cliente.numero_documento) == ("1", "00000000")
    assert (d.cliente.provincia, d.cliente.departamento, d.cliente.distrito) == ("LAMBAYEQUE", "LAMBAYEQUE", "TUCUME")
    assert d.totales == {"1001": "25.42", "1002": "0.00", "1003": "0.00", "1004": "0.00", "1000": "0.00"}
    assert [(t.codigo, t.monto) for t in d.tributos] == [("1000", "4.58")]
    assert (d.importe_total, d.total_impuestos, d.tipo_operacion) == ("30.00", "4.58", "0101")
    assert d.leyendas == [("1000", "TREINTA CON 0/100")]
    it = d.items[0]
    assert it.descripcion == "TERAPIA FISICA NIÑOS - TRAUMATOLOGICA"
    assert (it.cantidad, it.unidad_medida, it.valor_unitario, it.precio_unitario) == \
        ("1.0000000000", "NIU", "25.4200000000", "29.9956000000")
    t = it.tributos[0]
    assert (t.codigo, t.monto, t.afectacion, t.base, t.porcentaje) == ("1000", "4.58", "10", "25.42", "18.00")
    assert (it.valor_venta, it.total_impuestos) == ("25.42", "4.58")


def test_varios_items():
    d = parse_file(BOLETA_MULTI_TXT)
    assert [i.numero_item for i in d.items] == ["001", "002", "003"]
    assert d.items[2].descripcion == "TERAPIA ACUÁTICA – PISCINA NIÑO"


def test_encoding_cp1252(tmp_path):
    raw = BOLETA_TXT.read_text(encoding="utf-8").encode("cp1252")
    p = tmp_path / BOLETA_TXT.name
    p.write_bytes(raw)
    d = parse_file(p)
    assert d.encoding == "cp1252"
    assert d.items[0].descripcion == "TERAPIA FISICA NIÑOS - TRAUMATOLOGICA"


def test_utf8_con_bom():
    text, enc = decode_bytes(b"\xef\xbb\xbfNI\xc3\x91OS")
    assert (text, enc) == ("NIÑOS", "utf-8")


def test_nota_con_referencia():
    info = parse_filename("20437310841-07-BC01-00000001.txt")
    d = parse_text(note_text(), info)
    assert d.es_nota and d.referencia.serie_numero == "BT05-00125304"
    assert (d.referencia.tipo_comprobante, d.referencia.codigo_motivo) == ("03", "01")


def test_faltan_registros():
    info = parse_filename("20437310841-03-BT05-00125304.txt")
    with pytest.raises(ParseError, match="DET"):
        parse_text("IDE|BT05-00125304|2026-10-22|16:22:53|03|PEN||2026-10-22|\nEMI|6|x\nREC|1|x\nCAB|x", info)


def test_campo_desconocido_con_valor_genera_advertencia():
    txt = BOLETA_TXT.read_text(encoding="utf-8").replace("|PEN||2026-10-22|", "|PEN|OC-555|2026-10-22|")
    d = parse_text(txt, parse_filename(BOLETA_TXT.name))
    assert any("IDE posición 6" in w for w in d.advertencias)
