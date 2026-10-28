import logging

import httpx

from sovos_bridge.logging_setup import RedactingFilter, mask_doc, mask_name, redact, register_sensitive
from sovos_bridge.service.pipeline import Pipeline

from conftest import BOLETA_TXT, accepted


def test_mascaras():
    assert mask_doc("12345678") == "******78"
    assert mask_name("JUAN PEREZ DIAZ") == "J. P. D."


def test_redact_dni_ruc_persona_y_token():
    t = redact("DNI 45678912 RUC 10456789121 empresa 20437310841 Bearer abc.def-123 client_secret=xyz")
    assert "45678912" not in t and "10456789121" not in t
    assert "20437310841" in t                     # RUC de empresa no se oculta
    assert "abc.def-123" not in t and "xyz" not in t


def test_correlativo_no_se_oculta():
    t = redact("20437310841-03-BA01-00014481 enviado; cliente 45678912")
    assert "BA01-00014481" in t and "45678912" not in t


def test_nombres_registrados():
    register_sensitive("MARIA LOPEZ QUISPE", "AV. LOS PINOS 456")
    t = redact("cliente MARIA LOPEZ QUISPE en AV. LOS PINOS 456")
    assert "MARIA" not in t and "PINOS" not in t and "M. L. Q." in t


def test_logs_del_pipeline_sin_datos_del_paciente(settings, db, fake, caplog):
    fake.queue("POST", "/v1/documents", accepted())
    pipe = Pipeline(settings, db, fake.client())
    src = settings.inbox_dir / BOLETA_TXT.name
    src.write_bytes(BOLETA_TXT.read_bytes())
    root = logging.getLogger()
    filt = RedactingFilter()
    caplog.handler.addFilter(filt)
    try:
        with caplog.at_level(logging.DEBUG):
            pipe.process(src)
    finally:
        caplog.handler.removeFilter(filt)
    text = caplog.text
    assert "PACIENTE DE PRUEBA" not in text and "CALLE EJEMPLO" not in text
    assert "token-secreto" not in text
    assert root is not None and isinstance(fake.requests[0], httpx.Request)
