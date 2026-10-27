"""Validación del SBD contra los XSD oficiales de Sovos (schemas/sovos)."""

from __future__ import annotations

import os
import threading
from functools import lru_cache
from pathlib import Path

from lxml import etree

_lock = threading.Lock()


class SchemaError(Exception):
    """El SBD no cumple el XSD de Sovos."""


def schema_dir() -> Path:
    env = os.environ.get("SOVOS_SCHEMA_DIR")
    if env:
        return Path(env)
    return Path(__file__).resolve().parents[2] / "schemas" / "sovos"


@lru_cache(maxsize=1)
def _schema() -> etree.XMLSchema:
    path = schema_dir() / "StandardBusinessDocumentHeader.xsd"
    if not path.exists():
        raise FileNotFoundError(f"No se encontró el XSD de Sovos en {path}")
    return etree.XMLSchema(etree.parse(str(path)))


def validate_sbd(root: etree._Element) -> None:
    """Valida que el SBD esté bien formado y cumpla el XSD. Lanza SchemaError."""
    # Reparsear garantiza que el XML serializado está bien formado.
    reparsed = etree.fromstring(etree.tostring(root))
    with _lock:
        schema = _schema()
        if not schema.validate(reparsed):
            errores = "; ".join(f"línea {e.line}: {e.message}" for e in list(schema.error_log)[:5])
            raise SchemaError(f"El SBD no cumple el esquema XSD de Sovos: {errores}")
