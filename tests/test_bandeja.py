from datetime import date

import pytest

from sovos_bridge.parser import parse_filename, parse_text
from sovos_bridge.service import bandeja as bj
from sovos_bridge.service.bandeja import Bandeja
from sovos_bridge.storage import Estado
from sovos_bridge.web import PanelApp
from sovos_bridge.web.server import PanelError

from conftest import BOLETA_TXT, FACTURA_TXT, RUC, note_text

HOY = date(2026, 6, 10)            # la factura de muestra es del 03/06: vencida (plazo 3 días)
FACTURA = f"{RUC}-01-FT01-00000758"
BOLETA = f"{RUC}-03-BT05-00125304"
NOTA = f"{RUC}-07-BC01-00000001"


def add(settings, db, name, data, separar=True):
    info = parse_filename(name)
    doc = parse_text(data.decode("utf-8"), info)
    return Bandeja(settings, db).add(data=data, nombre_final=name, doc=doc, sha256="x", separar=separar,
                                      carga_id=None, cliente_doc="", usuario="Ana")


def row(settings, db, clave, hoy=HOY):
    return next(r for r in Bandeja(settings, db).rows(hoy) if r["clave"] == clave)


def test_detecta_especiales():
    txt = BOLETA_TXT.read_text(encoding="utf-8").replace("[1000|", "[9997|").replace("|10|", "|20|")
    doc = parse_text(txt, parse_filename(BOLETA_TXT.name))
    assert bj.especiales(doc) == ["exonerado"]
    assert bj.especiales(parse_text(BOLETA_TXT.read_text(encoding="utf-8"), parse_filename(BOLETA_TXT.name))) == []


def test_cambiar_fecha_corre_el_vencimiento_y_no_toca_lo_demas():
    data = FACTURA_TXT.read_bytes()
    out = bj.redate(data, "01", "2026-06-09")
    assert b"|2026-06-09|08:28:37|01|PEN||2026-06-09|" in out
    assert out.replace(b"2026-06-09", b"2026-06-03") == data
    nota = note_text().encode()
    assert b"|2026-06-09|" in bj.redate(nota, "07", "2026-06-09")


def test_fechas_disponibles_segun_plazo(settings):
    assert bj.fechas_disponibles(settings, "factura", HOY) == ["2026-06-10", "2026-06-09", "2026-06-08", "2026-06-07"]
    assert len(bj.fechas_disponibles(settings, "boleta", HOY)) == settings.plazo_boleta_dias + 1


def test_factura_vencida_no_sale_hasta_asignarle_fecha(settings, db):
    add(settings, db, FACTURA_TXT.name, FACTURA_TXT.read_bytes())
    tray = Bandeja(settings, db)
    assert row(settings, db, FACTURA)["grupo"] == "fuera_plazo"
    r = tray.release([FACTURA], "Ana", hoy=HOY)
    assert not r.hechos and "Fuera de plazo" in r.omitidos[0]["motivo"]

    assert tray.assign_date([FACTURA], "2026-06-01", "Ana", hoy=HOY).omitidos      # fuera del plazo
    assert tray.assign_date([FACTURA], "2026-06-08", "Ana", hoy=HOY).hechos
    assert row(settings, db, FACTURA)["grupo"] == "listo"

    assert tray.release([FACTURA], "Ana", hoy=HOY).hechos == [FACTURA]
    enviado = (settings.inbox_dir / FACTURA_TXT.name).read_bytes()
    assert b"|2026-06-08|08:28:37|" in enviado
    assert (settings.bandeja_dir / FACTURA_TXT.name).read_bytes() == FACTURA_TXT.read_bytes()   # original intacto
    assert db.staged_one(FACTURA)["estado"] == bj.EN_COLA
    assert any("cambiada de 03/06/2026 a 08/06/2026 por Ana" in e["detalle"] for e in db.events(FACTURA))


def test_separados_no_salen_salvo_que_se_pida(settings, db):
    txt = BOLETA_TXT.read_bytes().replace(b"[1000|", b"[9998|").replace(b"|10|", b"|30|")
    assert add(settings, db, BOLETA_TXT.name, txt) == bj.SEPARADO
    tray = Bandeja(settings, db)
    hoy = date.fromisoformat(db.staged_one(BOLETA)["fecha_emision"])
    assert not tray.release([BOLETA], "Ana", hoy=hoy).hechos
    assert tray.release([BOLETA], "Ana", incluir_separados=True, hoy=hoy).hechos == [BOLETA]


def test_nota_espera_a_su_comprobante(settings, db):
    settings.plazo_boleta_dias = 3650      # la nota de muestra es de marzo; aquí solo importa la espera
    add(settings, db, BOLETA_TXT.name, BOLETA_TXT.read_bytes())
    add(settings, db, f"{NOTA}.txt", note_text().encode())
    tray = Bandeja(settings, db)
    hoy = date.fromisoformat(db.staged_one(BOLETA)["fecha_emision"])
    assert row(settings, db, NOTA, hoy)["referencia"]["estado"] == "EN_BANDEJA"

    r = tray.release([NOTA, BOLETA], "Ana", hoy=hoy)
    assert set(r.hechos) == {NOTA, BOLETA}
    assert [p.name for p in settings.inbox_dir.glob("*.txt")] == [BOLETA_TXT.name]
    assert db.staged_one(NOTA)["estado"] == bj.ESPERANDO

    assert tray.release_waiting(hoy) == 0          # la boleta aún no está aprobada
    db.reserve(BOLETA, tipo="03", serie_numero="BT05-00125304", archivo="x")
    db.set_state(BOLETA, Estado.APROBADO, "ok")
    assert tray.release_waiting(hoy) == 1
    assert (settings.inbox_dir / f"{NOTA}.txt").exists() and db.staged_one(NOTA)["estado"] == bj.EN_COLA


def test_nota_con_referencia_desconocida_pide_confirmacion(settings, db):
    settings.plazo_boleta_dias = 3650
    add(settings, db, f"{NOTA}.txt", note_text().encode())
    tray = Bandeja(settings, db)
    hoy = date.fromisoformat(db.staged_one(NOTA)["fecha_emision"])
    r = tray.release([NOTA], "Ana", hoy=hoy)
    assert not r.hechos and "no pasó por el puente" in r.omitidos[0]["motivo"]
    assert tray.release([NOTA], "Ana", incluir_sin_referencia=True, hoy=hoy).hechos == [NOTA]


def test_descartar_guarda_el_txt(settings, db):
    add(settings, db, BOLETA_TXT.name, BOLETA_TXT.read_bytes())
    assert Bandeja(settings, db).discard([BOLETA], "Ana").hechos == [BOLETA]
    assert db.staged_one(BOLETA) is None
    assert (settings.bandeja_dir / "descartados" / BOLETA_TXT.name).exists()


def test_traer_rechazados_por_fecha(settings, db):
    src = settings.error_dir / FACTURA_TXT.name
    src.write_bytes(FACTURA_TXT.read_bytes())
    db.reserve(FACTURA, tipo="01", serie_numero="FT01-00000758", archivo=str(src))
    db.set_state(FACTURA, Estado.RECHAZADO, "[2108] fuera de fecha", codigo_sunat="2108")
    tray = Bandeja(settings, db)
    assert tray.summary(HOY)["rechazados_por_fecha"] == 1
    assert tray.bring_back_rejected_by_date("Ana").hechos == [FACTURA]
    assert tray.summary(HOY)["rechazados_por_fecha"] == 0
    assert row(settings, db, FACTURA)["grupo"] == "fuera_plazo"


def test_acciones_por_panel_exigen_responsable(settings, db):
    add(settings, db, BOLETA_TXT.name, BOLETA_TXT.read_bytes())
    app = PanelApp(settings, db, None)
    with pytest.raises(PanelError):
        app.bandeja_action("separar", {"claves": [BOLETA]}, "")
    assert app.bandeja_action("separar", {"claves": [BOLETA], "responsable": "Ana"}, "")["hechos"] == [BOLETA]
    assert app.bandeja()["filas"][0]["grupo"] == "separado"
