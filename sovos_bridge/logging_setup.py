"""Logs rotativos diarios con enmascarado de datos de pacientes.

Los comprobantes son de una clínica: DNI, nombres y direcciones de pacientes
son datos de salud y no deben quedar en los logs. Se enmascaran por dos vías:
  1. Patrones: DNI (8 dígitos), RUC de persona natural (10xxxxxxxxx),
     tokens Bearer y secretos con forma clave=valor.
  2. Registro explícito: el pipeline registra el nombre, dirección y teléfono
     del cliente de cada TXT (`register_sensitive`) y el filtro los reemplaza.
"""

from __future__ import annotations

import logging
import re
import sys
import threading
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

# 8 dígitos sueltos = DNI. No aplica tras '-' para no ocultar correlativos (BT05-00125304).
_DNI = re.compile(r"(?<![\d.\-])(\d{8})(?![\d.])")
_RUC_PN = re.compile(r"(?<!\d)(1[05]\d{9})(?!\d)")
_BEARER = re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+")
_SECRET_KV = re.compile(r"(?i)\b(client_secret|access_token|password|token)(\s*[=:]\s*)(\"?)[^\s\"&,}]+")

_sensitive: set[str] = set()
_lock = threading.Lock()


def mask_doc(value: str) -> str:
    """Muestra solo los 2 últimos dígitos: 12345678 -> ******78."""
    value = value or ""
    return "*" * max(len(value) - 2, 0) + value[-2:]


def mask_name(value: str) -> str:
    """PACIENTE DE PRUEBA -> P. D. P."""
    return " ".join(p[0] + "." for p in (value or "").split() if p)


def register_sensitive(*values: str | None) -> None:
    """Registra textos (nombres, direcciones, teléfonos) a ocultar en los logs."""
    with _lock:
        for v in values:
            if v and len(v.strip()) >= 4:
                _sensitive.add(v.strip())


def redact(text: str) -> str:
    with _lock:
        values = sorted(_sensitive, key=len, reverse=True)
    for v in values:
        if v in text:
            text = text.replace(v, mask_name(v) if not v.isdigit() else mask_doc(v))
    text = _BEARER.sub(r"\1***", text)
    text = _SECRET_KV.sub(r"\1\2\3***", text)
    text = _RUC_PN.sub(lambda m: mask_doc(m.group(1)), text)
    text = _DNI.sub(lambda m: mask_doc(m.group(1)), text)
    return text


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        record.msg = redact(msg)
        record.args = None
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = redact(record.exc_text)
        return True


FORMAT = "%(asctime)s %(levelname)-7s [%(threadName)s] %(name)s: %(message)s"


def setup_logging(logs_dir: Path, level: str = "INFO", retention_days: int = 90,
                  console: bool = True) -> None:
    logs_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(level.upper())
    for h in list(root.handlers):
        root.removeHandler(h)

    redacting = RedactingFilter()
    fh = TimedRotatingFileHandler(
        logs_dir / "sovos_bridge.log", when="midnight", backupCount=retention_days,
        encoding="utf-8",
    )
    fh.suffix = "%Y-%m-%d"
    fh.setFormatter(logging.Formatter(FORMAT))
    fh.addFilter(redacting)
    root.addHandler(fh)

    if console:
        ch = logging.StreamHandler(sys.stdout)
        ch.setFormatter(logging.Formatter(FORMAT))
        ch.addFilter(redacting)
        root.addHandler(ch)

    # httpx registra URLs y cabeceras a nivel DEBUG/INFO; se limita a WARNING.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
