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
    assert len(bj.fechas_disponibles(settings, "boleta", HOY)) == settings.plazo_boleta_individual_dias + 1


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
    settings.boletas_por_resumen = False    # envío una por una (flujo diario)
    txt = BOLETA_TXT.read_bytes().replace(b"[1000|", b"[9998|").replace(b"|10|", b"|30|")
    assert add(settings, db, BOLETA_TXT.name, txt) == bj.SEPARADO
    tray = Bandeja(settings, db)
    hoy = date.fromisoformat(db.staged_one(BOLETA)["fecha_emision"])
    assert not tray.release([BOLETA], "Ana", hoy=hoy).hechos
    assert tray.release([BOLETA], "Ana", incluir_separados=True, hoy=hoy).hechos == [BOLETA]


def test_nota_espera_a_su_comprobante(settings, db):
    settings.boletas_por_resumen = False    # envío una por una (flujo diario)
    settings.plazo_boleta_individual_dias = 3650   # la nota de muestra es de marzo; aquí solo importa la espera
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
    settings.boletas_por_resumen = False    # envío una por una (flujo diario)
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


def test_fecha_original_queda_registrada_al_enviar(settings, db, fake):
    from conftest import accepted
    from sovos_bridge.service.pipeline import Pipeline
    settings.boletas_por_resumen = False
    add(settings, db, FACTURA_TXT.name, FACTURA_TXT.read_bytes())
    tray = Bandeja(settings, db)
    tray.assign_date([FACTURA], "2026-06-09", "Ana", hoy=HOY)
    tray.release([FACTURA], "Ana", hoy=HOY)
    fake.queue("POST", "/v1/documents", accepted("doc-1"))
    Pipeline(settings, db, fake.client()).process(settings.inbox_dir / FACTURA_TXT.name)
    row = db.get(FACTURA)
    assert row["fecha_emision"] == "2026-06-09" and row["fecha_emision_original"] == "2026-06-03"


def test_nota_copiada_a_la_carpeta_espera_a_su_comprobante(settings, db):
    settings.boletas_por_resumen = False
    settings.plazo_boleta_dias = 3650
    settings.inbox_dir.mkdir(parents=True, exist_ok=True)
    tray = Bandeja(settings, db)
    nota = settings.inbox_dir / f"{NOTA}.txt"
    nota.write_text(note_text(), encoding="utf-8")

    # Copiadas juntas: la nota no sale mientras su boleta siga en la carpeta
    (settings.inbox_dir / BOLETA_TXT.name).write_bytes(BOLETA_TXT.read_bytes())
    assert tray.hold_if_reference_pending(nota) == "esperar" and nota.exists()

    # La boleta ya se envió pero SUNAT aún no la aprueba: la nota pasa a la bandeja esperando
    (settings.inbox_dir / BOLETA_TXT.name).unlink()
    db.reserve(BOLETA, tipo="03", serie_numero="BT05-00125304", archivo="x")
    db.set_state(BOLETA, Estado.ENVIADO, "enviado")
    assert tray.hold_if_reference_pending(nota) == "retenida"
    assert not nota.exists() and db.staged_one(NOTA)["estado"] == bj.ESPERANDO

    # Aprobada la boleta, el servicio suelta la nota y esta ya puede enviarse
    db.set_state(BOLETA, Estado.APROBADO, "ok")
    hoy = date.fromisoformat(db.staged_one(NOTA)["fecha_emision"])
    assert tray.release_waiting(hoy) == 1
    assert nota.exists() and tray.hold_if_reference_pending(nota) == "enviar"


def test_nota_de_comprobante_anterior_al_puente_sale_directo(settings, db):
    settings.inbox_dir.mkdir(parents=True, exist_ok=True)
    nota = settings.inbox_dir / f"{NOTA}.txt"
    nota.write_text(note_text(), encoding="utf-8")
    assert Bandeja(settings, db).hold_if_reference_pending(nota) == "enviar"
    boleta = settings.inbox_dir / BOLETA_TXT.name
    boleta.write_bytes(BOLETA_TXT.read_bytes())
    assert Bandeja(settings, db).hold_if_reference_pending(boleta) == "enviar"


# --- plazo de las boletas que salen solas (SUNAT 1079) ------------------------------
def test_boleta_suelta_usa_su_propio_plazo(settings, db):
    settings.boletas_por_resumen = False
    settings.plazo_boleta_individual_dias = 3
    assert len(bj.fechas_disponibles(settings, "boleta", HOY)) == 4      # hoy y 3 días atrás
    settings.boletas_por_resumen = True
    assert len(bj.fechas_disponibles(settings, "boleta", HOY)) == settings.plazo_boleta_dias + 1


def test_boleta_vencida_que_llega_a_la_carpeta_no_se_envia(settings, db):
    """Caso BT05-00125925: emitida el 10/11 y enviada el 26/11 con su fecha original -> 1079."""
    emision = date.fromisoformat(parse_text(BOLETA_TXT.read_text(encoding="utf-8"),
                                            parse_filename(BOLETA_TXT.name)).fecha_emision)
    settings.plazo_boleta_individual_dias = 3
    path = settings.inbox_dir / BOLETA_TXT.name
    path.write_bytes(BOLETA_TXT.read_bytes())
    tray = Bandeja(settings, db)

    assert not tray.hold_if_out_of_term(path, hoy=emision)              # del día: sale
    assert path.exists()
    hoy = date.fromordinal(emision.toordinal() + 16)
    assert tray.hold_if_out_of_term(path, hoy=hoy)                      # 16 días después: se retiene
    assert not path.exists()
    r = row(settings, db, BOLETA, hoy)
    assert r["grupo"] == "fuera_plazo" and "Asígnale una fecha nueva" in r["mensaje"]
    assert any("No se envió" in e["detalle"] for e in db.events(BOLETA))


def test_reenviar_un_rechazo_1079_no_lo_manda_con_la_fecha_vieja(settings, db):
    from sovos_bridge.service.operations import retry_one
    settings.plazo_boleta_individual_dias = 3
    txt = settings.error_dir / BOLETA_TXT.name
    txt.write_bytes(BOLETA_TXT.read_bytes())
    db.reserve(BOLETA, tipo="03", serie_numero="BT05-00125304", archivo=str(txt))
    db.set_state(BOLETA, Estado.RECHAZADO, "[1079]", codigo_sunat="1079")

    ok, msg = retry_one(settings, db, db.get(BOLETA))
    assert ok and "fuera de plazo" in msg
    assert not (settings.inbox_dir / BOLETA_TXT.name).exists()          # no se reenvió
    assert txt.exists()                                                 # el original queda donde estaba
    assert db.staged_one(BOLETA)["estado"] == bj.PREPARADO


def test_lo_que_ya_esta_en_sovos_no_se_retiene(settings, db):
    path = settings.inbox_dir / BOLETA_TXT.name
    path.write_bytes(BOLETA_TXT.read_bytes())
    db.reserve(BOLETA, tipo="03", serie_numero="BT05-00125304", archivo=str(path))
    db.set_state(BOLETA, Estado.APROBADO, "ok")
    assert not Bandeja(settings, db).hold_if_out_of_term(path, hoy=date(2030, 1, 1))
    assert path.exists()                                                # el pipeline lo marca duplicado


# --- fechas automáticas y traer rechazados por rango --------------------------------
HOY_AUTO = date(2026, 11, 27)


def boleta(settings, db, numero: int, fecha: str, serie: str = "BT05"):
    """Boleta de muestra con otro número y otra fecha de emisión, dejada en la bandeja."""
    sn = f"{serie}-{numero:08d}"
    txt = BOLETA_TXT.read_text(encoding="utf-8").replace("BT05-00125304", sn)
    orig = parse_text(txt, parse_filename(f"{RUC}-03-{sn}.txt")).fecha_emision
    data = txt.replace(f"|{orig}|", f"|{fecha}|", 1).encode()
    add(settings, db, f"{RUC}-03-{sn}.txt", data)
    return f"{RUC}-03-{sn}"


def test_fechas_automaticas_respetan_la_numeracion(settings, db):
    settings.plazo_boleta_individual_dias = 3            # aceptadas hoy: 24/11 a 27/11
    viejas = [boleta(settings, db, n, f) for n, f in ((100, "2026-11-10"), (101, "2026-11-12"), (102, "2026-11-13"))]
    # un número mayor ya salió con fecha 25/11: las viejas no pueden quedar después de esa fecha
    db.reserve(f"{RUC}-03-BT05-00000150", tipo="03", serie_numero="BT05-00000150", archivo="x", fecha_emision="2026-11-25")
    db.set_state(f"{RUC}-03-BT05-00000150", Estado.APROBADO, "ok")

    plan = Bandeja(settings, db).plan_fechas(viejas, HOY_AUTO)
    assert [p["fecha_nueva"] for p in plan] == ["2026-11-25"] * 3
    assert not any(p["aviso"] for p in plan)

    r = Bandeja(settings, db).assign_auto(viejas, "Ana", HOY_AUTO)
    assert sorted(r.hechos) == sorted(viejas)
    assert {row(settings, db, c, HOY_AUTO)["fecha_nueva"] for c in viejas} == {"2026-11-25"}
    assert all(row(settings, db, c, HOY_AUTO)["grupo"] == "listo" for c in viejas)


def test_sin_numeros_mayores_toma_hoy_y_avisa_si_no_se_puede_ordenar(settings, db):
    settings.plazo_boleta_individual_dias = 3
    c = boleta(settings, db, 200, "2026-11-10", serie="BT01")
    assert Bandeja(settings, db).plan_fechas([c], HOY_AUTO)[0]["fecha_nueva"] == "2026-11-27"

    # un número mayor salió el 12/11 (fuera de lo que SUNAT acepta hoy): se usa hoy y se avisa
    db.reserve(f"{RUC}-03-BT01-00000300", tipo="03", serie_numero="BT01-00000300", archivo="x", fecha_emision="2026-11-12")
    db.set_state(f"{RUC}-03-BT01-00000300", Estado.APROBADO, "ok")
    p = Bandeja(settings, db).plan_fechas([c], HOY_AUTO)[0]
    assert p["fecha_nueva"] == "2026-11-27" and "orden de numeración" in p["aviso"]


def test_lo_que_esta_en_plazo_no_se_mueve(settings, db):
    settings.plazo_boleta_individual_dias = 3
    c = boleta(settings, db, 400, "2026-11-26")
    p = Bandeja(settings, db).plan_fechas([c], HOY_AUTO)[0]
    assert "dentro del plazo" in p["omitido"]


def test_traer_rechazados_por_rango_y_tipo(settings, db):
    def rechazado(clave, tipo, fecha):
        sn = clave.split("-", 2)[2]
        txt = settings.error_dir / f"{clave}.txt"
        txt.write_bytes(BOLETA_TXT.read_bytes().replace(b"BT05-00125304", sn.encode()))
        db.reserve(clave, tipo=tipo, serie_numero=sn, archivo=str(txt), fecha_emision=fecha)
        db.set_state(clave, Estado.RECHAZADO, "x", codigo_sunat="1079")

    rechazado(f"{RUC}-03-BT05-00000010", "03", "2026-11-10")
    rechazado(f"{RUC}-03-BT05-00000013", "03", "2026-11-13")
    rechazado(f"{RUC}-03-BT05-00000020", "03", "2026-11-20")
    tray = Bandeja(settings, db)
    assert len(tray.recuperables()) == 3

    r = tray.bring_back_rejected_by_date("Ana", desde="2026-11-10", hasta="2026-11-13", tipos=["03"])
    assert sorted(r.hechos) == [f"{RUC}-03-BT05-00000010", f"{RUC}-03-BT05-00000013"]
    assert [x["clave"] for x in tray.recuperables()] == [f"{RUC}-03-BT05-00000020"]
    assert not tray.bring_back_rejected_by_date("Ana", tipos=["01"]).hechos    # no hay facturas


def test_panel_fecha_automatica_y_traer_con_filtros(settings, db, monkeypatch):
    settings.plazo_boleta_individual_dias = 3
    monkeypatch.setattr(bj, "date", type("D", (date,), {"today": staticmethod(lambda: HOY_AUTO)}))
    c = boleta(settings, db, 500, "2026-11-10")
    app = PanelApp(settings, db, None)
    plan = app.bandeja_action("fecha-auto", {"claves": [c], "simular": True, "responsable": "Ana"}, "")["plan"]
    assert plan[0]["fecha_nueva"] == "2026-11-27"
    assert db.staged_one(c)["fecha_nueva"] is None                      # simular no cambia nada
    assert app.bandeja_action("fecha-auto", {"claves": [c], "responsable": "Ana"}, "")["hechos"] == [c]
    assert db.staged_one(c)["fecha_nueva"] == "2026-11-27"

    with pytest.raises(PanelError, match="posterior"):
        app.bandeja_action("traer-rechazados", {"desde": "2026-11-13", "hasta": "2026-11-10", "responsable": "Ana"}, "")
    with pytest.raises(PanelError, match="Tipos"):
        app.bandeja_action("traer-rechazados", {"tipos": ["99"], "responsable": "Ana"}, "")
    assert "recuperables" in app.bandeja()
