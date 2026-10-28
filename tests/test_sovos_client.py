import json
import logging

import httpx
import pytest

from sovos_bridge.sovos import (
    OAuth2ClientCredentials, SovosAuthError, SovosClientError, SovosConnectionError,
)

from conftest import accepted


def test_envio_ok_y_cabeceras(fake):
    fake.queue("POST", "/v1/documents", accepted("abc"))
    assert fake.client().send_document("UEsDB") == "abc"
    req = fake.requests[0]
    assert req.headers["Authorization"] == "Bearer token-secreto"
    assert req.headers["Content-Type"] == "application/json"
    assert req.headers["x-correlationId"]
    assert json.loads(req.content) == {"data": "UEsDB", "dataEncoding": "base64"}


def test_correlation_id_unico_por_request(fake):
    fake.queue("POST", "/v1/documents", httpx.Response(503), accepted())
    fake.client().send_document("x")
    ids = [r.headers["x-correlationId"] for r in fake.requests]
    assert len(set(ids)) == 2


@pytest.mark.parametrize("status", [408, 429])
def test_408_429_esperan_60s(fake, status):
    fake.queue("POST", "/v1/documents", httpx.Response(status), accepted())
    assert fake.client().send_document("x") == "doc-123"
    assert fake.sleeps == [60.0]


def test_429_respeta_retry_after_mayor(fake):
    fake.queue("GET", "/notifications", httpx.Response(429, headers={"Retry-After": "120"}),
               httpx.Response(200, json={"data": []}))
    fake.client().document_notifications("d")
    assert fake.sleeps == [120.0]


def test_5xx_backoff_exponencial_en_consultas(fake):
    fake.queue("GET", "/notifications", httpx.Response(500), httpx.Response(502), httpx.Response(500),
               httpx.Response(200, json={"data": []}))
    fake.client(max_retries=5).document_notifications("d")
    assert fake.sleeps == [2.0, 4.0, 8.0]


def test_post_503_se_reintenta(fake):
    fake.queue("POST", "/v1/documents", httpx.Response(503), httpx.Response(502), accepted())
    assert fake.client().send_document("x") == "doc-123"
    assert fake.sleeps == [2.0, 4.0]


def test_post_500_es_incierto_y_no_se_reintenta(fake):
    fake.queue("POST", "/v1/documents", httpx.Response(500), accepted())
    with pytest.raises(SovosConnectionError) as e:
        fake.client().send_document("x")
    assert e.value.transmitted is True
    assert len(fake.sent("/v1/documents")) == 1


def test_post_timeout_de_lectura_es_incierto(fake):
    fake.queue("POST", "/v1/documents", httpx.ReadTimeout("timeout"))
    with pytest.raises(SovosConnectionError) as e:
        fake.client().send_document("x")
    assert e.value.transmitted is True
    assert len(fake.requests) == 1


def test_post_sin_conexion_se_reintenta_y_no_es_incierto(fake):
    fake.queue("POST", "/v1/documents", httpx.ConnectError("caido"))
    with pytest.raises(SovosConnectionError) as e:
        fake.client(max_retries=2).send_document("x")
    assert e.value.transmitted is False
    assert len(fake.requests) == 3


def test_post_503_persistente_no_es_incierto(fake):
    fake.queue("POST", "/v1/documents", httpx.Response(503))
    with pytest.raises(SovosConnectionError) as e:
        fake.client(max_retries=2).send_document("x")
    assert e.value.transmitted is False


def test_4xx_no_se_reintenta(fake):
    fake.queue("POST", "/v1/documents", httpx.Response(400, json={"error": "SBD inválido"}))
    with pytest.raises(SovosClientError) as e:
        fake.client().send_document("x")
    assert e.value.status == 400 and "SBD inválido" in e.value.body
    assert len(fake.requests) == 1 and fake.sleeps == []


def test_401_renueva_token_una_vez(fake):
    calls = []

    class Prov:
        def get_token(self):
            calls.append("get")
            return f"t{len(calls)}"

        def invalidate(self):
            calls.append("inv")

    fake.queue("POST", "/v1/documents", httpx.Response(401), accepted())
    c = fake.client()
    c.auth = Prov()
    assert c.send_document("x") == "doc-123"
    assert calls == ["get", "inv", "get"]


def test_401_persistente(fake):
    fake.queue("POST", "/v1/documents", httpx.Response(401))
    with pytest.raises(SovosAuthError):
        fake.client().send_document("x")


def test_ack_en_lote(fake):
    fake.queue("PUT", "/v1/notifications/PE", httpx.Response(200, json={"success": True}))
    fake.client().acknowledge(["n1", "n2"])
    assert json.loads(fake.requests[0].content) == [
        {"status": "read", "notificationId": "n1"}, {"status": "read", "notificationId": "n2"}]


def test_consulta_global_parametros(fake):
    fake.queue("GET", "/v1/notifications/PE", httpx.Response(200, json={"data": []}))
    fake.client().list_notifications(page=2)
    q = fake.requests[0].url.params
    assert (q["taxId"], q["sourceSystemId"], q["page"], q["perPage"]) == \
        ("20437310841", "CN_SystemERP_Polling", "2", "50")


def test_descarga_externa_sin_token(fake):
    fake.queue("GET", "files.example.com", httpx.Response(200, content=b"%PDF-1.4"))
    assert fake.client().download("https://files.example.com/x.pdf") == b"%PDF-1.4"
    assert "Authorization" not in fake.requests[0].headers


class _Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def test_oauth2_cache_y_renovacion(caplog):
    hits = []

    def handler(req):
        hits.append(req)
        return httpx.Response(200, json={"access_token": f"tok{len(hits)}", "expires_in": 3600})

    clock = _Clock()
    prov = OAuth2ClientCredentials("https://auth.test/token", "cid", "super-secreto",
                                   http=httpx.Client(transport=httpx.MockTransport(handler)), clock=clock)
    with caplog.at_level(logging.DEBUG):
        assert prov.get_token() == "tok1"
        assert prov.get_token() == "tok1"          # caché
        clock.t = 3600 - 59                          # dentro del margen de 60 s
        assert prov.get_token() == "tok2"
        prov.invalidate()
        assert prov.get_token() == "tok3"
    assert len(hits) == 3
    assert b"grant_type=client_credentials" in hits[0].content
    assert "super-secreto" not in caplog.text and "tok1" not in caplog.text


def test_oauth2_error_no_expone_cuerpo():
    http = httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(401, text="invalid client_secret=super-secreto")))
    prov = OAuth2ClientCredentials("https://auth.test/token", "cid", "super-secreto", http=http)
    with pytest.raises(SovosAuthError) as e:
        prov.get_token()
    assert "super-secreto" not in str(e.value)
