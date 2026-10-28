"""Flujo completo con el servidor Sovos simulado."""

import shutil
from datetime import datetime, timedelta

import httpx
from lxml import etree

from sovos_bridge.service.operations import daily_report, retry_errors, status_text
from sovos_bridge.service.pipeline import Pipeline
from sovos_bridge.service.status import StatusPoller
from sovos_bridge.service.watcher import InboxWatcher
from sovos_bridge.storage import Estado

from conftest import (
    BOLETA_TXT, accepted, application_response, decode_sbd, notification, notifications_response,
)

K = "20437310841-03-BT05-00125304"
XML_FIRMADO = b'<?xml version="1.0"?><Invoice xmlns="urn:oasis:names:specification:ubl:schema:xsd:Invoice-2"/>'
PDF = b"%PDF-1.4 boleta"


def drop(settings, src=BOLETA_TXT):
    dest = settings.inbox_dir / src.name
    shutil.copy(src, dest)
    return dest


def setup(settings, db, fake):
    client = fake.client()
    return Pipeline(settings, db, client), StatusPoller(settings, db, client)


def test_envio_aprobacion_y_descarga(settings, db, fake):
    pipe, poller = setup(settings, db, fake)
    fake.queue("POST", "/v1/documents", accepted("doc-123"))
    r = pipe.process(drop(settings))
    assert r.estado == Estado.ENVIADO
    row = db.get(K)
    assert row["document_id"] == "doc-123" and row["enviado_en"]
    assert (settings.processing_dir / BOLETA_TXT.name).exists()

    sbd = decode_sbd(fake.sent("/v1/documents", "POST")[0])
    assert etree.fromstring(sbd).tag.endswith("StandardBusinessDocument")
    assert "NIÑOS".encode() in sbd

    fake.queue("GET", "/v1/documents/PE/doc-123/notifications", notifications_response(
        notification("n1", "IP", cloud="101"),
        notification("n2", "AP", content=application_response(
            "AP", "200", attachments=[("legal.xml", "application/xml", XML_FIRMADO),
                                       ("boleta.pdf", "application/pdf", PDF)])),
    ))
    fake.queue("PUT", "/v1/notifications/PE", httpx.Response(200, json={"success": True}))
    poller.run_once()

    row = db.get(K)
    assert row["estado"] == Estado.APROBADO
    out = settings.output_dir / "2026" / "10"
    assert (out / f"{K}.xml").read_bytes() == XML_FIRMADO
    assert (out / f"{K}.pdf").read_bytes() == PDF
    assert (settings.processed_dir / BOLETA_TXT.name).exists()
    assert not list(settings.processing_dir.glob("*.txt"))
    put = fake.sent("/v1/notifications/PE", "PUT")
    assert len(put) == 1 and b'"n1"' in put[0].content and b'"n2"' in put[0].content
    assert db.unacked_notifications() == []


def test_rechazo_mueve_a_error_con_log(settings, db, fake):
    pipe, poller = setup(settings, db, fake)
    fake.queue("POST", "/v1/documents", accepted("doc-123"))
    pipe.process(drop(settings))
    fake.queue("GET", "/notifications", notifications_response(
        notification("n1", "RE", govt="2800", govt_msg="El dato ingresado en el tipo de documento de identidad del receptor no esta permitido.",
                     cloud="202")))
    fake.queue("PUT", "/v1/notifications/PE", httpx.Response(200))
    poller.run_once()

    row = db.get(K)
    assert row["estado"] == Estado.RECHAZADO and row["codigo_sunat"] == "2800"
    txt = settings.error_dir / BOLETA_TXT.name
    log = (settings.error_dir / (BOLETA_TXT.name + ".log")).read_text(encoding="utf-8")
    assert txt.exists()
    assert "RECHAZADO" in log and "2800" in log and "tipo de documento" in log


def test_duplicado_no_se_reenvia(settings, db, fake):
    pipe, poller = setup(settings, db, fake)
    fake.queue("POST", "/v1/documents", accepted("doc-123"))
    pipe.process(drop(settings))
    # El sistema Java vuelve a generar el mismo TXT mientras está pendiente
    r = pipe.process(drop(settings))
    assert r.estado == Estado.DUPLICADO
    assert len(fake.sent("/v1/documents", "POST")) == 1
    assert list(settings.duplicates_dir.glob("*.txt"))
    # ...y después de aprobado
    fake.queue("GET", "/notifications", notifications_response(
        notification("n1", "AP", cloud="209")))
    fake.queue("PUT", "/v1/notifications/PE", httpx.Response(200))
    poller.run_once()
    assert db.get(K)["estado"] == Estado.APROBADO
    r = pipe.process(drop(settings))
    assert r.estado == Estado.DUPLICADO
    assert len(fake.sent("/v1/documents", "POST")) == 1
    assert db.get(K)["estado"] == Estado.APROBADO


def test_aprobado_espera_pdf(settings, db, fake):
    pipe, poller = setup(settings, db, fake)
    fake.queue("POST", "/v1/documents", accepted("doc-123"))
    pipe.process(drop(settings))
    fake.queue("GET", "/notifications", notifications_response(
        notification("n1", "AP", cloud="200", content=application_response(
            "AP", "200", attachments=[("legal.xml", "application/xml", XML_FIRMADO)]))),
        notifications_response(
            notification("n1", "AP", cloud="200"),
            notification("n2", "AP", cloud="207", content=application_response(
                "AP", "207", attachments=[("b.pdf", "application/pdf", PDF)]))))
    fake.queue("PUT", "/v1/notifications/PE", httpx.Response(200))
    poller.run_once()
    assert db.get(K)["estado"] == Estado.EN_PROCESO
    poller.run_once()
    row = db.get(K)
    assert row["estado"] == Estado.APROBADO and row["xml_path"] and row["pdf_path"]


def test_duplicado_segun_sovos(settings, db, fake):
    pipe, poller = setup(settings, db, fake)
    fake.queue("POST", "/v1/documents", accepted("doc-123"))
    pipe.process(drop(settings))
    fake.queue("GET", "/notifications", notifications_response(notification("n1", "RE", cloud="404")))
    fake.queue("PUT", "/v1/notifications/PE", httpx.Response(200))
    poller.run_once()
    assert db.get(K)["estado"] == Estado.DUPLICADO
    assert (settings.duplicates_dir / BOLETA_TXT.name).exists()


def test_timeout_de_estado(settings, db, fake):
    pipe, poller = setup(settings, db, fake)
    fake.queue("POST", "/v1/documents", accepted("doc-123"))
    pipe.process(drop(settings))
    old = (datetime.now().astimezone() - timedelta(minutes=61)).isoformat(timespec="seconds")
    db.update_fields(K, enviado_en=old)
    fake.queue("GET", "/notifications", notifications_response(notification("n1", "IP", cloud="104")))
    fake.queue("PUT", "/v1/notifications/PE", httpx.Response(200))
    poller.run_once()
    row = db.get(K)
    assert row["estado"] == Estado.ERROR_CONEXION and row["document_id"] == "doc-123"
    # retry-errors reanuda la consulta, no reenvía
    acciones = retry_errors(settings, db)
    assert "reanuda la consulta" in acciones[0]
    assert db.get(K)["estado"] == Estado.EN_PROCESO
    assert len(fake.sent("/v1/documents", "POST")) == 1


def test_error_de_conexion_antes_de_enviar_y_reintento(settings, db, fake):
    pipe, _ = setup(settings, db, fake)
    fake.queue("POST", "/v1/documents", httpx.ConnectError("sin red"))
    r = pipe.process(drop(settings))
    assert r.estado == Estado.ERROR_CONEXION
    assert not db.get(K)["envio_incierto"]
    assert (settings.error_dir / (BOLETA_TXT.name + ".log")).exists()

    retry_errors(settings, db)
    assert (settings.inbox_dir / BOLETA_TXT.name).exists()
    assert not (settings.error_dir / (BOLETA_TXT.name + ".log")).exists()
    fake._routes.clear()
    fake.queue("POST", "/v1/documents", accepted("doc-9"))
    r = pipe.process(settings.inbox_dir / BOLETA_TXT.name)
    assert r.estado == Estado.ENVIADO and db.get(K)["document_id"] == "doc-9"


def test_envio_incierto_requiere_forzar(settings, db, fake):
    pipe, _ = setup(settings, db, fake)
    fake.queue("POST", "/v1/documents", httpx.ReadTimeout("t"))
    pipe.process(drop(settings))
    assert db.get(K)["envio_incierto"] == 1
    assert "OMITIDO" in retry_errors(settings, db)[0]
    assert not (settings.inbox_dir / BOLETA_TXT.name).exists()
    retry_errors(settings, db, forzar=True)
    assert (settings.inbox_dir / BOLETA_TXT.name).exists()
    fake._routes.clear()
    fake.queue("POST", "/v1/documents", accepted("doc-2"))
    assert pipe.process(settings.inbox_dir / BOLETA_TXT.name).estado == Estado.ENVIADO


def test_validacion_error(settings, db, fake, tmp_path):
    pipe, _ = setup(settings, db, fake)
    bad = tmp_path / "20437310841-03-BT05-00125304.txt"
    bad.write_bytes(BOLETA_TXT.read_bytes().replace(b"|PACIENTE DE PRUEBA|", b"||"))
    r = pipe.process(bad)
    assert r.estado == Estado.VALIDACION_ERROR
    assert db.get(K)["estado"] == Estado.VALIDACION_ERROR
    assert "razón social del cliente" in (settings.error_dir / (bad.name + ".log")).read_text(encoding="utf-8")
    assert fake.requests == []


def test_400_de_sovos(settings, db, fake):
    pipe, _ = setup(settings, db, fake)
    fake.queue("POST", "/v1/documents", httpx.Response(400, json={"message": "Invalid SBD"}))
    r = pipe.process(drop(settings))
    assert r.estado == Estado.VALIDACION_ERROR
    assert "Invalid SBD" in db.get(K)["mensaje"]
    # corregido, puede reenviarse (Sovos no lo registró)
    assert db.reserve(K, tipo="03", serie_numero="BT05-00125304", archivo="x").accion == "reintento"


def test_dry_run(settings, db, fake):
    s = settings.model_copy(update={"dry_run": True})
    s.ensure_dirs()
    r = Pipeline(s, db, None).process(drop(s))
    assert r.estado == "DRY_RUN"
    assert (s.dry_run_dir / f"{K}.sbd.xml").exists()
    assert db.get(K) is None and fake.requests == []


def test_nombre_invalido(settings, db, fake):
    pipe, _ = setup(settings, db, fake)
    p = settings.inbox_dir / "boleta.txt"
    p.write_text("x")
    assert pipe.process(p).estado == Estado.VALIDACION_ERROR
    assert (settings.error_dir / "boleta.txt.log").exists()


def test_recuperacion_tras_caida_durante_envio(settings, db, fake):
    pipe, _ = setup(settings, db, fake)
    work = settings.processing_dir / BOLETA_TXT.name
    shutil.copy(BOLETA_TXT, work)
    db.reserve(K, tipo="03", serie_numero="BT05-00125304", archivo=str(work))
    db.mark_sending(K)
    pipe.recover()
    row = db.get(K)
    assert row["estado"] == Estado.ERROR_CONEXION and row["envio_incierto"] == 1
    assert (settings.error_dir / BOLETA_TXT.name).exists()


def test_recuperacion_archivo_sin_registro(settings, db, fake):
    pipe, _ = setup(settings, db, fake)
    shutil.copy(BOLETA_TXT, settings.processing_dir / BOLETA_TXT.name)
    pipe.recover()
    assert (settings.inbox_dir / BOLETA_TXT.name).exists()


def test_watcher_espera_tamano_estable(tmp_path):
    t = [0.0]
    w = InboxWatcher(tmp_path, stable_seconds=3, clock=lambda: t[0])
    f = tmp_path / "20437310841-03-BT05-00125304.txt"
    f.write_text("IDE|")
    assert w.ready_files() == []          # primera vez que se ve
    t[0] = 1
    with f.open("a") as fh:
        fh.write("mas datos")             # sigue escribiéndose
    assert w.ready_files() == []
    t[0] = 2
    assert w.ready_files() == []          # estable, pero menos de 3 s
    t[0] = 5.5
    assert w.ready_files() == [f]
    (tmp_path / "otro.tmp").write_text("x")
    assert all(p.suffix == ".txt" for p in w.ready_files())


def test_status_y_reporte(settings, db, fake):
    pipe, _ = setup(settings, db, fake)
    fake.queue("POST", "/v1/documents", accepted("doc-123"))
    pipe.process(drop(settings))
    texto = status_text(db, "BT05-00125304")
    assert "ENVIADO" in texto and "doc-123" in texto
    path, counts = daily_report(settings, db, datetime.now().date().isoformat())
    assert counts[Estado.ENVIADO] == 1
    contenido = path.read_text(encoding="utf-8-sig")
    assert contenido.splitlines()[0].startswith("clave;tipo;serie_numero;estado")
    assert "PACIENTE" not in contenido and "00000000" not in contenido
