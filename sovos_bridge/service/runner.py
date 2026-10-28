"""Servicio continuo: hilo de bandeja (envíos) e hilo de consulta de estados."""

from __future__ import annotations

import logging
import signal
import threading

from sovos_bridge.config import Settings
from sovos_bridge.service.pipeline import Pipeline
from sovos_bridge.service.status import StatusPoller
from sovos_bridge.service.watcher import InboxWatcher

log = logging.getLogger(__name__)


class Service:
    def __init__(self, settings: Settings, pipeline: Pipeline, poller: StatusPoller | None):
        self.s = settings
        self.pipeline = pipeline
        self.poller = poller
        self.watcher = InboxWatcher(settings.inbox_dir, settings.file_stable_seconds)
        self.stop_event = threading.Event()

    def inbox_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                for path in self.watcher.ready_files():
                    if self.stop_event.is_set():
                        break
                    try:
                        self.pipeline.process(path)
                    except PermissionError:
                        # En Windows el archivo sigue abierto por el sistema Java.
                        log.info("%s aún está en uso; se reintenta en la próxima revisión", path.name)
                    except Exception:
                        log.exception("Error inesperado procesando %s", path.name)
                    finally:
                        self.watcher.forget(path)
            except Exception:
                log.exception("Error revisando la bandeja %s", self.s.inbox_dir)
            self.stop_event.wait(self.s.poll_seconds)

    def status_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                self.poller.run_once()
            except Exception:
                log.exception("Error consultando estados en Sovos")
            self.stop_event.wait(self.s.status_poll_seconds)

    def run(self) -> None:
        log.info("Servicio iniciado. Bandeja=%s Trabajo=%s DRY_RUN=%s", self.s.inbox_dir, self.s.work_dir,
                 self.s.dry_run)
        self.pipeline.recover()

        def _stop(signum, _frame):
            log.info("Señal %s recibida; deteniendo el servicio", signum)
            self.stop_event.set()

        for sig in (signal.SIGINT, signal.SIGTERM, getattr(signal, "SIGBREAK", None)):
            if sig is not None:
                try:
                    signal.signal(sig, _stop)
                except ValueError:
                    pass  # no se está en el hilo principal (pruebas)

        threads = [threading.Thread(target=self.inbox_loop, name="bandeja", daemon=True)]
        if self.poller is not None:
            threads.append(threading.Thread(target=self.status_loop, name="estados", daemon=True))
        for t in threads:
            t.start()
        try:
            while not self.stop_event.is_set():
                self.stop_event.wait(1)
        except KeyboardInterrupt:
            self.stop_event.set()
        for t in threads:
            t.join(timeout=60)
        log.info("Servicio detenido")
