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
    http_timeout_seconds: float = 60
    http_max_retries: int = Field(5, ge=0)

    emisor_ruc: str = "20437310841"
    forma_pago_defecto: str = "Contado"

    inbox_dir: Path = Path("inbox")
    work_dir: Path = Path("work")

    poll_seconds: float = Field(10, gt=0)
    status_poll_seconds: float = Field(30, gt=0)
    max_status_minutes: float = Field(60, gt=0)
    file_stable_seconds: float = Field(3, ge=0)

    dry_run: bool = False
    log_level: str = "INFO"
    log_retention_days: int = 90

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
            self.reports_dir, self.db_path.parent,
        ):
            d.mkdir(parents=True, exist_ok=True)
        if self.dry_run:
            self.dry_run_dir.mkdir(parents=True, exist_ok=True)


def load_settings(env_file: str | os.PathLike | None = None) -> Settings:
    """Carga .env (si existe) y construye Settings desde el entorno."""
    load_dotenv(env_file or Path.cwd() / ".env", override=False)
    values = {}
    for name in Settings.model_fields:
        raw = os.environ.get(name.upper())
        if raw is not None and raw != "":
            values[name] = raw
    return Settings(**values)
