"""Lectura del TXT: encoding y separación de campos respetando corchetes."""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

# Secuencias típicas de UTF-8 leído como cp1252 (mojibake): Ã‘ = Ñ, Ã³ = ó ...
_MOJIBAKE_MARKERS = ("Ã", "Â", "â€")


def decode_bytes(raw: bytes) -> tuple[str, str]:
    """Devuelve (texto, encoding). Intenta UTF-8 estricto y cae a cp1252.

    El sistema Java genera hoy UTF-8, pero puede usar Windows-1252 según la
    configuración regional del servidor. cp1252 decodifica cualquier byte, así
    que se intenta en segundo lugar.
    """
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    try:
        text, encoding = raw.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        text, encoding = raw.decode("cp1252", errors="replace"), "cp1252"
    if any(m in text for m in _MOJIBAKE_MARKERS):
        log.warning("El TXT parece tener caracteres mal codificados (mojibake); revisar Ñ y tildes")
    return text, encoding


def split_fields(line: str) -> list[str]:
    """Separa por '|' sin cortar dentro de [ ... ].

    'A|[1|2]|B' -> ['A', '[1|2]', 'B']
    """
    fields: list[str] = []
    buf: list[str] = []
    depth = 0
    for ch in line:
        if ch == "[":
            depth += 1
        elif ch == "]" and depth > 0:
            depth -= 1
        if ch == "|" and depth == 0:
            fields.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    fields.append("".join(buf))
    return fields


def split_group(value: str) -> list[str]:
    """'[1000|4.58]' -> ['1000', '4.58'];  '' -> []"""
    value = (value or "").strip()
    if not value:
        return []
    if value.startswith("[") and value.endswith("]"):
        value = value[1:-1]
    return value.split("|")


def pairs(values: list[str]) -> list[tuple[str, str]]:
    """['a','1','b','2'] -> [('a','1'), ('b','2')]"""
    return [(values[i], values[i + 1]) for i in range(0, len(values) - 1, 2)]
