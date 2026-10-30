"""Configuración del servicio, leída desde variables de entorno y .env."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from pydantic import BaseModel, Field, SecretStr, field_validator


class Settings(BaseModel):
    sovos_base_url: str = ""
    sovos_auth_mode: Literal["oauth2", "static"] = "oauth2"
    sovos_token_url: str = ""
    sovos_client_id: str = ""
    sovos_client_secret: SecretStr = SecretStr("")
    sovos_scope: str = ""
    sovos_static_token: SecretStr = SecretStr("")
    sovos_sender_system_id: str = "DefaultSystemERP"
    sovos_polling_system_id: str = "CN_SystemERP_Polling"
    sovos_status_mode: Literal["document", "global"] = "document"
    sovos_note_root: Literal["CreditNote", "Invoice"] = "CreditNote"
    # auto = serie B -> BusinessToConsumer, serie F -> BusinessToBusiness
    sovos_business_category: Literal["auto", "BusinessToBusiness", "BusinessToConsumer"] = "auto"
    # Scope SubSchema: la guía de Sovos lo marca obligatorio ("-" si no aplica); vacío = no se envía
    sovos_sub_schema: str = ""
    # Firma: sovos = Sovos firma con el certificado de su portal; propio = el puente firma con
    # el certificado de la clínica registrado en SUNAT y Sovos solo transmite.
    sovos_signing_mode: Literal["sovos", "propio"] = "sovos"
    cert_path: Path | None = None
    cert_password: SecretStr = SecretStr("")
    http_timeout_seconds: float = 60
    http_max_retries: int = Field(5, ge=0)

    emisor_ruc: str = "20437310841"
    forma_pago_defecto: str = "Contado"
    # Días calendario, desde el día siguiente a la emisión, para que SUNAT reciba el comprobante.
    # Facturas y sus notas: 3. Boletas y sus notas: según lo indicado por contabilidad.
    plazo_factura_dias: int = Field(3, ge=0)
    # Boletas: SUNAT solo las acepta una por una si son recientes (si no, error 1079); por
    # resumen diario, hasta el séptimo día calendario siguiente a la emisión.
    plazo_boleta_dias: int = Field(7, ge=0)
    # La bandeja envía las boletas y sus notas en resúmenes diarios (uno por fecha de emisión)
    boletas_por_resumen: bool = True
    # Cómo pedirle a Sovos el resumen diario (no documentado para Perú; ver sbd.SummaryProduct)
    sovos_rc_output_schema: str = "summaryDocuments"
    sovos_rc_meta_schema: str = ""
    sovos_rc_transform: str = "LEGAL-TO-SCI_SUMMARY_DOCUMENTS"
    sovos_rc_version: str = "2.0"

    inbox_dir: Path = Path("inbox")
    work_dir: Path = Path("work")

    poll_seconds: float = Field(10, gt=0)
    status_poll_seconds: float = Field(30, gt=0)
    max_status_minutes: float = Field(60, gt=0)
    file_stable_seconds: float = Field(3, ge=0)

    dry_run: bool = False
    log_level: str = "INFO"
    log_retention_days: int = 90

    # Panel web. Fuera de 127.0.0.1 exige UI_PASSWORD (muestra datos de pacientes).
    ui_enabled: bool = True
    ui_host: str = "127.0.0.1"
    ui_port: int = Field(8765, ge=1, le=65535)
    ui_user: str = "admin"
    ui_password: SecretStr = SecretStr("")
    # Nombre para entrar al panel en vez de "localhost" (el instalador lo agrega al archivo hosts)
    ui_alias: str = ""

    @field_validator("sovos_base_url")
    @classmethod
    def _strip_slash(cls, v: str) -> str:
        return v.rstrip("/")

    # --- carpetas derivadas de WORK_DIR ---------------------------------
    @property
    def processing_dir(self) -> Path:
        return self.work_dir / "processing"

    @property
    def processed_dir(self) -> Path:
        return self.work_dir / "processed"

    @property
    def error_dir(self) -> Path:
        return self.work_dir / "error"

    @property
    def duplicates_dir(self) -> Path:
        return self.work_dir / "duplicates"

    @property
    def output_dir(self) -> Path:
        return self.work_dir / "output"

    @property
    def dry_run_dir(self) -> Path:
        return self.work_dir / "dry_run"

    @property
    def sbd_dir(self) -> Path:
        return self.work_dir / "sbd"

    @property
    def bandeja_dir(self) -> Path:
        """TXT subidos desde el panel que esperan a ser liberados (originales, sin cambios)."""
        return self.work_dir / "bandeja"

    @property
    def logs_dir(self) -> Path:
        return self.work_dir / "logs"

    @property
    def reports_dir(self) -> Path:
        return self.work_dir / "reports"

    @property
    def db_path(self) -> Path:
        return self.work_dir / "db" / "sovos_bridge.db"

    def ensure_dirs(self) -> None:
        for d in (
            self.inbox_dir, self.processing_dir, self.processed_dir, self.error_dir,
            self.duplicates_dir, self.output_dir, self.sbd_dir, self.logs_dir,
            self.reports_dir, self.db_path.parent, self.bandeja_dir,
        ):
            d.mkdir(parents=True, exist_ok=True)
        if self.dry_run:
            self.dry_run_dir.mkdir(parents=True, exist_ok=True)


def env_path(env_file: str | os.PathLike | None = None) -> Path:
    return Path(env_file) if env_file else Path.cwd() / ".env"


def load_settings(env_file: str | os.PathLike | None = None, override: bool = False) -> Settings:
    """Carga .env (si existe) y construye Settings desde el entorno.

    override=True se usa al recargar tras un cambio hecho desde el panel: el .env manda."""
    load_dotenv(env_path(env_file), override=override)
    values = {}
    for name in Settings.model_fields:
        raw = os.environ.get(name.upper())
        if raw is not None and raw != "":
            values[name] = raw
    return Settings(**values)


def update_env_file(path: Path, values: dict[str, str]) -> None:
    """Cambia o agrega claves del .env conservando el resto (comentarios y orden).

    Los valores con espacios o caracteres especiales van entre comillas simples, que
    python-dotenv toma literal ($ y # incluidos). Escritura atómica."""
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    pending = dict(values)
    out = []
    for line in lines:
        key = line.split("=", 1)[0].strip() if "=" in line and not line.lstrip().startswith("#") else None
        if key in pending:
            out.append(f"{key}={_env_quote(pending.pop(key))}")
        else:
            out.append(line)
    out += [f"{k}={_env_quote(v)}" for k, v in pending.items()]
    tmp = path.with_name(path.name + ".nuevo")
    tmp.write_text("\n".join(out) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _env_quote(value: str) -> str:
    if "'" in value:
        raise ValueError("El valor no puede contener comilla simple (')")
    if value == "" or all(c.isalnum() or c in "-_.:/\\@" for c in value):
        return value
    return f"'{value}'"
