"""Servicio continuo: hilo de bandeja (envíos) e hilo de consulta de estados."""

from __future__ import annotations

import logging
import signal
import threading
from typing import Any, Callable

from sovos_bridge.config import Settings, load_settings
from sovos_bridge.service.bandeja import Bandeja
from sovos_bridge.service.resumen import Resumenes
from sovos_bridge.service.configuracion import pendientes
from sovos_bridge.service.pipeline import Pipeline
from sovos_bridge.service.status import StatusPoller
from sovos_bridge.service.watcher import InboxWatcher
from sovos_bridge.web import PanelApp, heartbeat_path, start_panel

log = logging.getLogger(__name__)


class Service:
    def __init__(self, settings: Settings, pipeline: Pipeline, poller: StatusPoller | None,
                 panel: PanelApp | None = None, env_file: str | None = None,
                 client_factory: Callable[[Settings], Any] | None = None):
        self.s = settings
        self.pipeline = pipeline
        self.poller = poller
        self.panel = panel
        self.env_file = env_file
        self.client_factory = client_factory
        self.watcher = InboxWatcher(settings.inbox_dir, settings.file_stable_seconds)
        self.bandeja = Bandeja(settings, pipeline.db, Resumenes(settings, pipeline.db, pipeline.client, pipeline.signer))
        self.stop_event = threading.Event()
        # Un cambio de configuración espera a que termine el TXT en curso.
        self.lock = threading.RLock()
        self._aviso_pendiente = ""
        if panel is not None:
            panel.attach(lock=self.lock, reload=self.reload, problema_firma=lambda: self.pipeline.problema_firma)

    def reload(self) -> None:
        """Vuelve a leer el .env (cambios hechos desde el panel) y rehace cliente, pipeline y consulta."""
        with self.lock:
            s = load_settings(self.env_file, override=True)
            s.ensure_dirs()
            db = self.pipeline.db
            client = None
            if not s.dry_run and self.client_factory and not any(p.clave == "sovos" for p in pendientes(s)):
                client = self.client_factory(s)
            self.s = s
            self.pipeline = Pipeline(s, db, client)
            self.poller = StatusPoller(s, db, client) if client is not None else None
            if self.watcher.inbox != s.inbox_dir:
                self.watcher = InboxWatcher(s.inbox_dir, s.file_stable_seconds)
            self.bandeja = Bandeja(s, db, Resumenes(s, db, client, self.pipeline.signer))
            if self.panel is not None:
                self.panel.apply(s, client, self.poller)
            self._aviso_pendiente = ""
            log.info("Configuración recargada")

    def _pendiente(self) -> str:
        faltan = pendientes(self.s, self.pipeline.problema_firma)
        texto = "; ".join(p.texto for p in faltan)
        if texto and texto != self._aviso_pendiente:
            log.warning("No se envía nada hasta completar la configuración en el panel: %s", texto)
        self._aviso_pendiente = texto
        return texto

    def _heartbeat(self) -> None:
        """El panel web muestra el servicio como activo mientras este archivo se actualice."""
        try:
            heartbeat_path(self.s).touch()
        except OSError:
            log.debug("No se pudo actualizar el heartbeat", exc_info=True)

    def inbox_loop(self) -> None:
        while not self.stop_event.is_set():
            self._heartbeat()
            if self._pendiente():
                self.stop_event.wait(self.s.poll_seconds)
                continue
            try:
                # Notas liberadas que esperaban a su comprobante: pasan a la cola si ya fue aprobado.
                self.bandeja.release_waiting()
            except Exception:
                log.exception("Error revisando las notas que esperan en la bandeja")
            try:
                for path in self.watcher.ready_files():
                    if self.stop_event.is_set():
                        break
                    try:
                        with self.lock:
                            # Una nota no sale hasta que el comprobante que modifica esté aprobado.
                            if self.bandeja.hold_if_reference_pending(path) != "enviar":
                                continue
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
                if self.poller is not None:
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

        panel = start_panel(self.panel) if self.panel is not None and self.s.ui_enabled else None
        threads = [threading.Thread(target=self.inbox_loop, name="bandeja", daemon=True),
                   threading.Thread(target=self.status_loop, name="estados", daemon=True)]
        for t in threads:
            t.start()
        try:
            while not self.stop_event.is_set():
                self.stop_event.wait(1)
        except KeyboardInterrupt:
            self.stop_event.set()
        for t in threads:
            t.join(timeout=60)
        if panel is not None:
            panel[0].shutdown()
        heartbeat_path(self.s).unlink(missing_ok=True)
        log.info("Servicio detenido")
