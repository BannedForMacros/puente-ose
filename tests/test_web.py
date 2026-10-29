"""Panel web: consultas, acciones (mismas reglas que la CLI) y seguridad del servidor."""

import shutil

import httpx
import pytest
from pydantic import SecretStr

from sovos_bridge.service.pipeline import Pipeline
from sovos_bridge.storage import Estado
from sovos_bridge.web import PanelApp, environment_label, start_panel
from sovos_bridge.web.server import PanelError

from conftest import BOLETA_TXT, accepted, application_response, notification, notifications_response

K = "20437310841-03-BT05-00125304"


def enviar(settings, db, fake):
    """Procesa la boleta de muestra hasta ENVIADO con Sovos simulado."""
    fake.queue("POST", "/v1/documents", accepted("doc-123"))
    dest = settings.inbox_dir / BOLETA_TXT.name
    shutil.copy(BOLETA_TXT, dest)
    Pipeline(settings, db, fake.client()).process(dest)
    return db.get(K)


def rechazar(settings, db, fake):
    enviar(settings, db, fake)
    txt = settings.error_dir / BOLETA_TXT.name
    shutil.move(str(settings.processing_dir / BOLETA_TXT.name), txt)
    db.update_fields(K, archivo=str(txt))
    db.set_state(K, Estado.RECHAZADO, "rechazo", codigo_sunat="2325",
                 mensaje="[2325] El certificado usado no es el comunicado a SUNAT")


@pytest.fixture
def server(settings, db, fake):
    """Panel real en un puerto libre; devuelve (cliente HTTP, app)."""
    app = PanelApp(settings.model_copy(update={"ui_port": 0}), db, fake.client())
    srv, _ = start_panel(app)
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    with httpx.Client(base_url=base, timeout=5) as http:
        yield http, app
    srv.shutdown()


# --- lógica ------------------------------------------------------------------
def test_resumen_cuenta_estados_y_bandeja(settings, db, fake):
    enviar(settings, db, fake)
    (settings.inbox_dir / "20437310841-03-BT05-00000001.txt").write_text("x")
    s = PanelApp(settings, db, fake.client()).summary()
    assert s["estados"]["ENVIADO"] == 1 and s["estados"]["APROBADO"] == 0
    assert s["bandeja"] == 1
    assert s["ambiente"] == "Pruebas" and s["puede_consultar"]
    assert s["servicio"]["activo"] is False


def test_etiqueta_de_ambiente(settings):
    def env(**kw):
        return environment_label(settings.model_copy(update=kw))
    assert env(sovos_base_url="https://api.sovos.com") == "Producción"
    assert env(sovos_base_url="https://api-test.sovos.com") == "Pruebas"
    assert env(sovos_base_url="https://api.sovos.com", dry_run=True) == "En seco"


def test_lista_filtra_ordena_y_no_acepta_columnas_arbitrarias(settings, db, fake):
    enviar(settings, db, fake)
    app = PanelApp(settings, db, None)
    assert app.documents({"q": "bt05"})["total"] == 1
    assert app.documents({"estado": "APROBADO"})["total"] == 0
    assert app.documents({"estado": "ENVIADO,RECHAZADO"})["total"] == 1
    assert app.documents({"tipo": "01"})["total"] == 0
    assert app.documents({"desde": "2026-10-23"})["total"] == 0
    r = app.documents({"sort": "estado; DROP TABLE documentos", "per_page": "9999"})
    assert r["total"] == 1 and r["per_page"] == 200
    item = r["items"][0]
    assert item["cliente_doc"].startswith("****") and item["reintento"] is None


def test_detalle_trae_historial_y_tipo_de_reintento(settings, db, fake):
    rechazar(settings, db, fake)
    d = PanelApp(settings, db, None).document(K)
    assert d["reintento"] == "reenvio" and d["archivos"]["txt"] and d["archivos"]["sbd"]
    assert [e["estado"] for e in d["eventos"]][-1] == "RECHAZADO"
    with pytest.raises(PanelError):
        PanelApp(settings, db, None).document("no-existe")


def test_reintento_devuelve_el_txt_a_la_bandeja(settings, db, fake):
    rechazar(settings, db, fake)
    r = PanelApp(settings, db, None).retry(K, forzar=False)
    assert r["ok"] and (settings.inbox_dir / BOLETA_TXT.name).exists()


def test_reintento_incierto_exige_forzar(settings, db, fake):
    enviar(settings, db, fake)
    txt = settings.error_dir / BOLETA_TXT.name
    shutil.move(str(settings.processing_dir / BOLETA_TXT.name), txt)
    db.set_state(K, Estado.ERROR_CONEXION, "500", document_id=None, envio_incierto=1)
    db.update_fields(K, archivo=str(txt))
    app = PanelApp(settings, db, None)
    assert app.document(K)["reintento"] == "forzado"
    assert app.retry(K, forzar=False)["ok"] is False
    assert app.retry(K, forzar=True)["ok"] is True


def test_estado_aprobado_no_se_reintenta(settings, db, fake):
    enviar(settings, db, fake)
    db.set_state(K, Estado.APROBADO, "ok")
    assert PanelApp(settings, db, None).retry(K, forzar=True)["ok"] is False


def test_consulta_a_sovos_actualiza_pendientes(settings, db, fake):
    enviar(settings, db, fake)
    fake.queue("GET", "/v1/documents/PE/doc-123/notifications", notifications_response(
        notification("n1", "RE", govt="2325", cloud="209",
                     content=application_response("RE", "209", "2325", "El certificado usado no es el comunicado a SUNAT")),
    ))
    fake.queue("PUT", "/v1/notifications/PE", httpx.Response(200, json={"success": True}))
    r = PanelApp(settings, db, fake.client()).consult(K)
    assert r["actualizado"] and r["documento"]["estado"] == "RECHAZADO"
    assert r["notificaciones"][0]["mensaje"] == "El certificado usado no es el comunicado a SUNAT"


def test_consulta_sin_document_id_o_en_seco(settings, db, fake):
    rechazar(settings, db, fake)
    db.set_state(K, Estado.RECHAZADO, "x", document_id=None)
    with pytest.raises(PanelError, match="documentId"):
        PanelApp(settings, db, fake.client()).consult(K)
    with pytest.raises(PanelError, match="seco"):
        PanelApp(settings, db, None).consult(K)


def test_descarga_solo_dentro_de_las_carpetas_del_puente(settings, db, fake, tmp_path):
    enviar(settings, db, fake)
    afuera = tmp_path / "secreto.xml"
    afuera.write_text("x")
    db.update_fields(K, xml_path=str(afuera))
    with pytest.raises(PanelError, match="fuera"):
        PanelApp(settings, db, None).file_for(K, "xml")
    assert PanelApp(settings, db, None).file_for(K, "sbd").suffix == ".xml"


# --- HTTP y seguridad -------------------------------------------------------
def test_sirve_la_pagina_con_cabeceras_de_seguridad(server):
    http, _ = server
    r = http.get("/")
    assert r.status_code == 200 and "Puente Sovos" in r.text
    assert "default-src 'self'" in r.headers["content-security-policy"]
    assert r.headers["x-frame-options"] == "DENY"
    assert http.get("/static/app.js").status_code == 200
    assert http.get("/static/../server.py").status_code == 404


def test_api_y_acciones_por_http(server, settings, db, fake):
    http, _ = server
    rechazar(settings, db, fake)
    assert http.get("/api/summary").json()["estados"]["RECHAZADO"] == 1
    assert http.get("/api/documents", params={"estado": "RECHAZADO"}).json()["total"] == 1
    # Sin la cabecera del panel, la acción se rechaza (protección contra otros sitios).
    assert http.post(f"/api/documents/{K}/retry", json={}).status_code == 403
    r = http.post(f"/api/documents/{K}/retry", json={}, headers={"X-Sovos-Panel": "1"})
    assert r.status_code == 200 and r.json()["ok"]
    assert http.get(f"/api/documents/{K}/archivo/sbd").status_code == 200
    assert http.get("/api/documents/no-existe").status_code == 404
    csv = http.get("/api/export.csv")
    assert csv.status_code == 200 and K in csv.content.decode("utf-8-sig")


def test_con_clave_exige_autenticacion(settings, db):
    s = settings.model_copy(update={"ui_port": 0, "ui_password": SecretStr("clave-segura")})
    srv, _ = start_panel(PanelApp(s, db, None))
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        assert httpx.get(f"{base}/api/summary").status_code == 401
        assert httpx.get(f"{base}/api/summary", auth=("admin", "mala")).status_code == 401
        assert httpx.get(f"{base}/api/summary", auth=("admin", "clave-segura")).status_code == 200
    finally:
        srv.shutdown()


def test_no_se_expone_en_la_red_sin_clave(settings, db):
    s = settings.model_copy(update={"ui_port": 0, "ui_host": "0.0.0.0"})
    assert start_panel(PanelApp(s, db, None)) is None
