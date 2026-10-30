"""Configuración desde el panel: certificado, conexión con Sovos, carpeta de TXT y clave.

Así la instalación no necesita llevar secretos: el instalador deja el puente
corriendo y TI carga el certificado y las credenciales desde el panel. Mientras
falte algo, el servicio sigue vivo (panel disponible) pero no procesa la carpeta
de TXT, y el panel muestra qué falta.

Cada cambio se valida antes de guardarse (el certificado se abre con su clave y
debe ser del RUC emisor y estar vigente; las credenciales de Sovos se prueban
pidiendo un token) y se aplica en caliente, sin reiniciar el servicio.
"""

from __future__ import annotations

import base64
import binascii
import logging
import os
import socket
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

from sovos_bridge.config import Settings, update_env_file
from sovos_bridge.sbd import Signer, SigningError
from sovos_bridge.sovos import OAuth2ClientCredentials, SovosError

log = logging.getLogger(__name__)

AMBIENTES = {
    "produccion": ("https://api.sovos.com", "https://api.sovos.com/oauth/token"),
    "pruebas": ("https://api-test.sovos.com", "https://api-test.sovos.com/oauth/token"),
}
MAX_CERT_BYTES = 200 * 1024
AVISO_VENCE_DIAS = 30


class ConfigError(ValueError):
    """El dato no se guardó; el mensaje se muestra tal cual en el panel."""


@dataclass
class Pendiente:
    clave: str        # certificado | sovos | carpeta
    texto: str


def pendientes(s: Settings, problema_firma: str = "") -> list[Pendiente]:
    """Lo que falta para que el puente pueda enviar."""
    out = []
    if s.sovos_auth_mode == "oauth2" and not (s.sovos_base_url and s.sovos_token_url and s.sovos_client_id
                                              and s.sovos_client_secret.get_secret_value()):
        out.append(Pendiente("sovos", "Falta la conexión con Sovos (Client ID y Client Secret)"))
    if s.sovos_signing_mode == "propio" and (problema_firma or not s.cert_path or not Path(s.cert_path).is_file()):
        out.append(Pendiente("certificado", problema_firma or "Falta cargar el certificado digital de la clínica"))
    if not inbox_configurada(s):
        out.append(Pendiente("carpeta", "Falta indicar la carpeta donde el sistema de facturación deja los TXT"))
    return out


def inbox_configurada(s: Settings) -> bool:
    return s.inbox_dir.is_absolute() and s.inbox_dir.is_dir()


def ambiente_de(s: Settings) -> str:
    for nombre, (base, _) in AMBIENTES.items():
        if s.sovos_base_url.rstrip("/") == base:
            return nombre
    return "otro" if s.sovos_base_url else ""


def urls_panel(s: Settings) -> list[dict[str, str]]:
    """Direcciones para entrar al panel, de la más cómoda a la más técnica."""
    port = "" if s.ui_port == 80 else f":{s.ui_port}"
    out = []
    if s.ui_alias:
        out.append({"url": f"http://{s.ui_alias}{port}/", "donde": "En este servidor"})
    if s.ui_host not in ("127.0.0.1", "localhost", "::1"):
        host = socket.gethostname().split(".")[0]
        out.append({"url": f"http://{host.lower()}{port}/", "donde": "Desde otras PC de la red"})
        try:
            ip = _lan_ip()
            if ip:
                out.append({"url": f"http://{ip}{port}/", "donde": "Si el nombre no responde"})
        except OSError:
            pass
    out.append({"url": f"http://localhost{port}/", "donde": "En este servidor (siempre funciona)"})
    return out


def _lan_ip() -> str:
    # Sin enviar nada: solo pregunta al sistema qué interfaz usaría para salir.
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.connect(("10.255.255.255", 1))
        ip = sock.getsockname()[0]
    return "" if ip.startswith("127.") else ip


class Configuracion:
    def __init__(self, settings: Settings, env_file: Path, aplicar: Callable[[], None],
                 problema_firma: Callable[[], str] = lambda: ""):
        self.s = settings
        self.env_file = env_file
        self.aplicar = aplicar              # recarga el .env y rehace cliente, pipeline y poller
        self.problema_firma = problema_firma

    # --- lectura ----------------------------------------------------------------
    def estado(self) -> dict[str, Any]:
        s = self.s
        cert: dict[str, Any] = {"configurado": False, "problema": self.problema_firma()}
        if s.cert_path and Path(s.cert_path).is_file():
            try:
                i = Signer.from_pkcs12(Path(s.cert_path), s.cert_password.get_secret_value()).info
                dias = (i.not_after - datetime.now(UTC)).days
                cert.update(configurado=not cert["problema"], serie=i.serial_hex, titular=_cn(i.subject),
                            vigente_desde=i.not_before.date().isoformat(), vence=i.not_after.date().isoformat(),
                            dias=dias, archivo=Path(s.cert_path).name)
            except SigningError as exc:
                cert["problema"] = str(exc)
        inbox = {"ruta": str(s.inbox_dir) if s.inbox_dir.is_absolute() else "", "existe": inbox_configurada(s),
                 "txt": _count_txt(s.inbox_dir) if inbox_configurada(s) else 0}
        return {
            "pendientes": [p.__dict__ for p in pendientes(s, self.problema_firma())],
            "certificado": cert,
            "sovos": {"ambiente": ambiente_de(s), "client_id": s.sovos_client_id,
                      "secret_guardado": bool(s.sovos_client_secret.get_secret_value()),
                      "firma": s.sovos_signing_mode},
            "carpeta": inbox,
            "panel": {"urls": urls_panel(s), "usuario": s.ui_user,
                      "con_clave": bool(s.ui_password.get_secret_value())},
            "ruc": s.emisor_ruc,
        }

    # --- certificado ------------------------------------------------------------
    def guardar_certificado(self, contenido_b64: str, nombre: str, clave: str, usuario: str) -> dict[str, Any]:
        try:
            data = base64.b64decode(contenido_b64 or "", validate=True)
        except (binascii.Error, ValueError):
            raise ConfigError("El archivo no se pudo leer") from None
        if not data:
            raise ConfigError("Elige el archivo del certificado (.pfx o .p12)")
        if len(data) > MAX_CERT_BYTES:
            raise ConfigError("El archivo es demasiado grande para ser un certificado .pfx/.p12")
        if not clave:
            raise ConfigError("Escribe la contraseña del certificado")

        with tempfile.TemporaryDirectory() as tmp:
            probe = Path(tmp) / "cert.pfx"
            probe.write_bytes(data)
            try:
                signer = Signer.from_pkcs12(probe, clave)
            except SigningError as exc:
                raise ConfigError(str(exc)) from None
        i = signer.info
        if self.s.emisor_ruc not in i.subject:
            raise ConfigError(f"El certificado no es del RUC {self.s.emisor_ruc}: está a nombre de {_cn(i.subject)}")
        try:
            signer.check_validity()
        except SigningError as exc:
            raise ConfigError(str(exc)) from None

        cert_dir = self.s.work_dir / "cert"
        cert_dir.mkdir(parents=True, exist_ok=True)
        ext = ".p12" if nombre.lower().endswith(".p12") else ".pfx"
        dest = cert_dir / f"certificado_{i.serial_hex[:12]}{ext}"
        tmp_dest = dest.with_name(dest.name + ".nuevo")
        tmp_dest.write_bytes(data)
        os.replace(tmp_dest, dest)
        try:
            update_env_file(self.env_file, {"SOVOS_SIGNING_MODE": "propio", "CERT_PATH": str(dest),
                                            "CERT_PASSWORD": clave})
        except ValueError as exc:
            raise ConfigError(f"La contraseña del certificado no se puede guardar: {exc}") from None
        log.info("Configuración: certificado %s (serie %s, vence %s) cargado por %s",
                 dest.name, i.serial_hex, f"{i.not_after:%d/%m/%Y}", usuario)
        self.aplicar()
        dias = (i.not_after - datetime.now(UTC)).days
        avisos = []
        if dias <= AVISO_VENCE_DIAS:
            avisos.append(f"Vence en {dias} días: tramita la renovación")
        return {"serie": i.serial_hex, "titular": _cn(i.subject), "vence": i.not_after.date().isoformat(),
                "avisos": avisos}

    # --- Sovos ------------------------------------------------------------------
    def guardar_sovos(self, ambiente: str, client_id: str, client_secret: str, usuario: str,
                      probar: Callable[[str, str, str], None] | None = None) -> dict[str, Any]:
        if ambiente not in AMBIENTES:
            raise ConfigError("Elige el ambiente: producción o pruebas")
        client_id = client_id.strip()
        secret = client_secret.strip() or self.s.sovos_client_secret.get_secret_value()
        if not client_id or not secret:
            raise ConfigError("Escribe el Client ID y el Client Secret que entregó Sovos")
        base, token_url = AMBIENTES[ambiente]
        try:
            (probar or _probar_token)(token_url, client_id, secret)
        except SovosError as exc:
            raise ConfigError(f"Sovos no aceptó estas credenciales: {exc}") from None
        try:
            update_env_file(self.env_file, {"SOVOS_AUTH_MODE": "oauth2", "SOVOS_BASE_URL": base,
                                            "SOVOS_TOKEN_URL": token_url, "SOVOS_CLIENT_ID": client_id,
                                            "SOVOS_CLIENT_SECRET": secret})
        except ValueError as exc:
            raise ConfigError(str(exc)) from None
        log.info("Configuración: conexión con Sovos (%s, cliente %s) guardada por %s", ambiente, client_id, usuario)
        self.aplicar()
        return {"ambiente": ambiente}

    # --- carpeta de TXT ---------------------------------------------------------
    def revisar_carpeta(self, ruta: str) -> dict[str, Any]:
        p = _carpeta(ruta)
        return {"ruta": str(p), "txt": _count_txt(p)}

    def guardar_carpeta(self, ruta: str, usuario: str) -> dict[str, Any]:
        p = _carpeta(ruta)
        try:
            probe = p / f".puente_prueba_{os.getpid()}"
            probe.write_bytes(b"")
            probe.unlink()
        except OSError:
            raise ConfigError(f"El puente no puede escribir en {p}: revisa los permisos de la carpeta") from None
        update_env_file(self.env_file, {"INBOX_DIR": str(p)})
        log.info("Configuración: carpeta de TXT %s guardada por %s", p, usuario)
        self.aplicar()
        return {"ruta": str(p)}

    # --- clave del panel --------------------------------------------------------
    def cambiar_clave_panel(self, nueva: str, usuario: str) -> None:
        if len(nueva) < 8:
            raise ConfigError("La clave debe tener al menos 8 caracteres")
        try:
            update_env_file(self.env_file, {"UI_PASSWORD": nueva})
        except ValueError as exc:
            raise ConfigError(str(exc)) from None
        log.info("Configuración: clave del panel cambiada por %s", usuario)
        self.aplicar()


def _probar_token(token_url: str, client_id: str, secret: str) -> None:
    OAuth2ClientCredentials(token_url, client_id, secret, timeout=30).get_token()


def _carpeta(ruta: str) -> Path:
    ruta = (ruta or "").strip().strip('"')
    if not ruta:
        raise ConfigError("Escribe la ruta de la carpeta")
    p = Path(ruta)
    if not p.is_absolute():
        raise ConfigError("Escribe la ruta completa, por ejemplo C:\\facturacion\\salida_txt")
    if not p.is_dir():
        raise ConfigError(f"No existe la carpeta {p} en este servidor")
    return p


def _count_txt(p: Path) -> int:
    try:
        return sum(1 for f in p.iterdir() if f.is_file() and f.suffix.lower() == ".txt")
    except OSError:
        return 0


def _cn(subject: str) -> str:
    for part in subject.split(","):
        if part.startswith("CN="):
            return part[3:].replace("||USO TRIBUTARIO|| ", "").strip()
    return subject
