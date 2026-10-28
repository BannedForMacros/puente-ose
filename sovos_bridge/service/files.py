"""Movimiento de archivos entre carpetas y archivos .log de error."""

from __future__ import annotations

import os
import shutil
from datetime import datetime
from pathlib import Path


def unique_path(dest_dir: Path, name: str) -> Path:
    """dest_dir/name; si ya existe, agrega fecha-hora para no sobrescribir."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    target = dest_dir / name
    if not target.exists():
        return target
    stem, suffix = os.path.splitext(name)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    return dest_dir / f"{stem}.{stamp}{suffix}"


def move(src: Path, dest_dir: Path) -> Path:
    target = unique_path(dest_dir, src.name)
    shutil.move(str(src), str(target))
    return target


def error_log_path(txt: Path) -> Path:
    return txt.with_name(txt.name + ".log")


def write_error_log(txt: Path, titulo: str, detalle: list[str] | str, extra: dict[str, str] | None = None) -> Path:
    """Escribe <archivo>.txt.log junto al TXT con el motivo en español claro."""
    if isinstance(detalle, str):
        detalle = [detalle]
    lines = [
        f"Archivo : {txt.name}",
        f"Fecha   : {datetime.now():%Y-%m-%d %H:%M:%S}",
        f"Motivo  : {titulo}",
        "",
        "Detalle:",
        *[f"  - {d}" for d in detalle],
    ]
    if extra:
        lines += ["", *[f"{k}: {v}" for k, v in extra.items() if v]]
    path = error_log_path(txt)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def remove_error_log(txt: Path) -> None:
    log = error_log_path(txt)
    if log.exists():
        log.unlink()


def month_dir(base: Path, when: datetime | None = None) -> Path:
    when = when or datetime.now()
    d = base / f"{when:%Y}" / f"{when:%m}"
    d.mkdir(parents=True, exist_ok=True)
    return d
