"""Vigilancia de la bandeja de entrada: toma solo archivos ya terminados de escribir."""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


@dataclass
class _Seen:
    size: int
    mtime: float
    stable_since: float


class InboxWatcher:
    """Un archivo se considera listo cuando su tamaño y fecha no cambian
    durante `stable_seconds` y entre dos revisiones consecutivas."""

    def __init__(self, inbox: Path, stable_seconds: float, clock: Callable[[], float] = time.monotonic):
        self.inbox = inbox
        self.stable_seconds = stable_seconds
        self.clock = clock
        self._seen: dict[Path, _Seen] = {}

    def ready_files(self) -> list[Path]:
        now = self.clock()
        current: dict[Path, _Seen] = {}
        ready: list[tuple[float, Path]] = []
        for path in self.inbox.glob("*"):
            if not path.is_file() or path.suffix.lower() != ".txt":
                continue
            try:
                st = path.stat()
            except OSError:
                continue
            prev = self._seen.get(path)
            if prev and prev.size == st.st_size and prev.mtime == st.st_mtime and st.st_size > 0:
                current[path] = prev
                if now - prev.stable_since >= self.stable_seconds:
                    ready.append((st.st_mtime, path))
            else:
                current[path] = _Seen(st.st_size, st.st_mtime, now)
        self._seen = current
        return [p for _, p in sorted(ready)]

    def forget(self, path: Path) -> None:
        self._seen.pop(path, None)
