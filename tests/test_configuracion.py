import base64
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import SecretStr

from sovos_bridge.config import load_settings, update_env_file
from sovos_bridge.service.configuracion import ConfigError, Configuracion, pendientes
from sovos_bridge.sovos import SovosAuthError
from sovos_bridge.web import PanelApp
from sovos_bridge.web.server import PanelError

from conftest import BOLETA_TXT
from test_signer import make_pfx


@pytest.fixture
def env(tmp_path):
    p = tmp_path / ".env"
    p.write_text("# comentario\nSOVOS_CLIENT_ID=viejo\nDRY_RUN=false\n", encoding="utf-8")
    return p


def cfg(settings, env, reloads=None):
    return Configuracion(settings, env, lambda: reloads.append(1) if reloads is not None else None)


def b64(path):
    return base64.b64encode(path.read_bytes()).decode()


def test_update_env_conserva_lo_demas_y_cita_lo_especial(env):
    update_env_file(env, {"SOVOS_CLIENT_ID": "nuevo", "CERT_PASSWORD": "a$b #c", "INBOX_DIR": r"C:\fact\txt"})
    text = env.read_text(encoding="utf-8")
    assert text.startswith("# comentario\nSOVOS_CLIENT_ID=nuevo\nDRY_RUN=false\n")
    assert "CERT_PASSWORD='a$b #c'" in text and "INBOX_DIR=C:\\fact\\txt" in text
    s = load_settings(env, override=True)
    assert s.cert_password.get_secret_value() == "a$b #c" and str(s.inbox_dir) == r"C:\fact\txt"
    with pytest.raises(ValueError):
        update_env_file(env, {"CERT_PASSWORD": "con'comilla"})


def test_pendientes_al_instalar_sin_secretos(settings):
    s = settings.model_copy(update={"sovos_auth_mode": "oauth2", "sovos_client_secret": SecretStr(""),
                                    "sovos_signing_mode": "propio",
                                    "cert_path": None})
    assert {p.clave for p in pendientes(s)} == {"sovos", "certificado"}
    s2 = s.model_copy(update={"inbox_dir": settings.inbox_dir / "no-existe"})
    assert "carpeta" in {p.clave for p in pendientes(s2)}


def test_certificado_valido_se_guarda_y_aplica(settings, env, tmp_path):
    pfx, _ = make_pfx(tmp_path)
    reloads = []
    r = cfg(settings, env, reloads).guardar_certificado(b64(pfx), "clinica.pfx", "clave", "Ana")
    assert r["serie"] == "5868e484" and reloads == [1]
    s = load_settings(env, override=True)
    assert s.sovos_signing_mode == "propio" and s.cert_password.get_secret_value() == "clave"
    assert s.cert_path.read_bytes() == pfx.read_bytes() and s.cert_path.parent == settings.work_dir / "cert"


@pytest.mark.parametrize("caso, esperado", [
    ("clave", "contraseña"),
    ("ruc", "no es del RUC"),
    ("vencido", "no está vigente"),
])
def test_certificado_invalido_no_se_guarda(settings, env, tmp_path, caso, esperado):
    if caso == "vencido":
        pfx, _ = make_pfx(tmp_path, start=datetime.now(UTC) - timedelta(days=400))
    else:
        pfx, _ = make_pfx(tmp_path)
    s = settings.model_copy(update={"emisor_ruc": "20100000001"}) if caso == "ruc" else settings
    with pytest.raises(ConfigError, match=esperado):
        cfg(s, env).guardar_certificado(b64(pfx), "x.pfx", "mala" if caso == "clave" else "clave", "Ana")
    assert "CERT_PATH" not in env.read_text(encoding="utf-8")


def test_sovos_se_prueba_antes_de_guardar(settings, env):
    c = cfg(settings, env)
    def rechaza(*_):
        raise SovosAuthError("credenciales inválidas (401)")
    with pytest.raises(ConfigError, match="no aceptó"):
        c.guardar_sovos("produccion", "id", "secreto", "Ana", probar=rechaza)
    assert "SOVOS_CLIENT_SECRET" not in env.read_text(encoding="utf-8")
    c.guardar_sovos("produccion", "id", "secreto", "Ana", probar=lambda *a: None)
    s = load_settings(env, override=True)
    assert s.sovos_base_url == "https://api.sovos.com" and s.sovos_client_secret.get_secret_value() == "secreto"


def test_carpeta_con_txt_pide_decision_y_los_pasa_a_la_bandeja(settings, db, env, tmp_path):
    carpeta = tmp_path / "salida_java"
    carpeta.mkdir()
    (carpeta / BOLETA_TXT.name).write_bytes(BOLETA_TXT.read_bytes())
    app = PanelApp(settings, db, None)
    app.attach(env_file=env)
    body = {"ruta": str(carpeta), "responsable": "Ana"}
    r = app.config_action("carpeta", body, "")
    assert r["decision"] and r["txt"] == 1 and "INBOX_DIR" not in env.read_text(encoding="utf-8")

    r = app.config_action("carpeta", {**body, "existentes": "bandeja"}, "")
    assert r["importados"]["en_bandeja"] == 1
    assert not list(carpeta.glob("*.txt"))                       # la carpeta vigilada queda limpia
    assert db.staged_one("20437310841-03-BT05-00125304") is not None
    assert f"INBOX_DIR={carpeta}" in env.read_text(encoding="utf-8").replace("'", "")


def test_configurar_exige_responsable_y_reporta_errores(settings, db, env):
    app = PanelApp(settings, db, None)
    app.attach(env_file=env)
    with pytest.raises(PanelError):
        app.config_action("clave", {"nueva": "12345678"}, "")
    with pytest.raises(PanelError):
        app.config_action("clave", {"nueva": "corta", "responsable": "Ana"}, "")


def test_clave_del_panel_rige_al_instante(settings, db, env):
    app = PanelApp(settings, db, None)
    app.attach(env_file=env, reload=lambda: app.apply(load_settings(env, override=True), None, None))
    assert app.auth_expected() == ""
    app.config_action("clave", {"nueva": "clave-segura-1", "responsable": "Ana"}, "")
    assert base64.b64decode(app.auth_expected()).decode() == f"{app.s.ui_user}:clave-segura-1"
