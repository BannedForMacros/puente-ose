"""Panel web del puente: estados de los comprobantes, detalle, reintentos y consulta a Sovos.

Servidor de la librería estándar (sin dependencias nuevas). Corre como hilo
dentro del servicio (`run`) o solo (`ui`). Las acciones usan las mismas reglas
que la línea de comandos (`retry_one`, `StatusPoller`), así que el panel no
puede provocar un envío duplicado que la CLI no permitiría.

Seguridad:
  - Fuera de 127.0.0.1 exige UI_PASSWORD (HTTP Basic): muestra datos de pacientes.
  - Las acciones (POST) exigen la cabecera X-Sovos-Panel, que un sitio externo
    no puede enviar sin CORS (y aquí no se habilita CORS).
  - Las descargas solo sirven archivos dentro de WORK_DIR o INBOX_DIR.
"""

from __future__ import annotations

import base64
import csv
import hmac
import io
import json
import logging
import mimetypes
import re
import sqlite3
import threading
from datetime import date, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from sovos_bridge import __version__
from sovos_bridge.config import Settings, env_path
from sovos_bridge.service.bandeja import EN_BANDEJA, Bandeja
from sovos_bridge.service.configuracion import ConfigError, Configuracion, pendientes
from sovos_bridge.service.resumen import Resumenes
from sovos_bridge.sbd import Signer, SigningError
from sovos_bridge.service.operations import REPORT_COLUMNS, RETRYABLE, retry_errors, retry_one
from sovos_bridge.service.status import StatusPoller
from sovos_bridge.sovos import SovosClient, SovosError, parse_notifications
from sovos_bridge.storage import PENDIENTES, Database, Estado
from sovos_bridge.web import upload

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).with_name("static")
STATIC_FILES = {"app.css", "app.js", "favicon.svg"}
ACTION_HEADER = "X-Sovos-Panel"
MAX_BODY = 64_000
MAX_UPLOAD_BODY = 30 * 1024 * 1024
HEARTBEAT_STALE_SECONDS = 120
FILE_KINDS = {"txt": "archivo", "xml": "xml_path", "pdf": "pdf_path", "sbd": "sbd_path"}
_CONFIG_ACTIONS = {"certificado", "sovos", "carpeta", "clave"}
MAX_CONFIG_BODY = 1024 * 1024
_BANDEJA_ACTIONS = {"liberar", "fecha", "separar", "descartar", "traer-rechazados"}
_CARGA_ROUTE = re.compile(r"^/api/cargas/(\d+)\.csv$")
_DOC_ROUTE = re.compile(r"^/api/documents/([^/]+)(?:/(retry|consultar|archivo/(txt|xml|pdf|sbd)))?$")


class PanelError(Exception):
    def __init__(self, status: HTTPStatus, message: str):
        self.status = status
        super().__init__(message)


def heartbeat_path(s: Settings) -> Path:
    return s.db_path.parent / "heartbeat"


def environment_label(s: Settings) -> str:
    if s.dry_run:
        return "En seco"
    host = s.sovos_base_url.lower()
    if "test" in host or "sandbox" in host:
        return "Pruebas"
    return "Producción" if host else "Sin configurar"


def _is_loopback(host: str) -> bool:
    return host in ("127.0.0.1", "localhost", "::1")


class PanelApp:
    """Lógica del panel, independiente del transporte HTTP (se prueba directo)."""

    def __init__(self, settings: Settings, db: Database, client: SovosClient | None = None,
                 poller: StatusPoller | None = None):
        self.s = settings
        self.db = db
        self.client = client
        self.poller = poller or (StatusPoller(settings, db, client) if client else None)
        self._sovos_lock = threading.Lock()
        self.env_file = env_path(None)
        self.lock: threading.RLock = threading.RLock()
        self._reload = lambda: None
        self._problema_firma = lambda: ""

    def attach(self, *, lock=None, reload=None, problema_firma=None, env_file=None) -> None:
        """Lo conecta el servicio: recarga en caliente y estado de la firma."""
        if lock is not None:
            self.lock = lock
        if reload is not None:
            self._reload = reload
        if problema_firma is not None:
            self._problema_firma = problema_firma
        if env_file is not None:
            self.env_file = Path(env_file)

    def apply(self, settings: Settings, client: SovosClient | None, poller: StatusPoller | None) -> None:
        self.s, self.client = settings, client
        self.poller = poller or (StatusPoller(settings, self.db, client) if client else None)

    def auth_expected(self) -> str:
        """Cabecera Basic esperada; se lee en cada pedido para que un cambio de clave rija al instante."""
        pw = self.s.ui_password.get_secret_value()
        return base64.b64encode(f"{self.s.ui_user}:{pw}".encode()).decode() if pw else ""

    # --- lecturas -----------------------------------------------------------
    def summary(self) -> dict[str, Any]:
        counts = self.db.state_counts()
        aprobados_hoy = [r for r in self.db.day_rows(date.today().isoformat()) if r["estado"] == Estado.APROBADO]
        inbox = [p for p in self.s.inbox_dir.glob("*") if p.is_file() and p.suffix.lower() == ".txt"] \
            if self.s.inbox_dir.exists() else []
        hb = heartbeat_path(self.s)
        last_beat = datetime.fromtimestamp(hb.stat().st_mtime).astimezone() if hb.exists() else None
        alive = bool(last_beat and (datetime.now().astimezone() - last_beat).total_seconds()
                     < max(HEARTBEAT_STALE_SECONDS, self.s.poll_seconds * 4))
        return {
            "version": __version__,
            "ambiente": environment_label(self.s),
            "ruc": self.s.emisor_ruc,
            "servicio": {"activo": alive, "ultima_revision": last_beat.isoformat(timespec="seconds")
                         if last_beat else None},
            "bandeja": len(inbox),
            "preparacion": len(self.db.staged(*EN_BANDEJA)),
            "config_pendiente": [x.__dict__ for x in pendientes(self.s, self._problema_firma())],
            "estados": {e.value: counts.get(e.value, 0) for e in Estado},
            "hoy": {"aprobados": len(aprobados_hoy),
                    "monto": f"{sum(_num(r['importe_total']) for r in aprobados_hoy):.2f}"},
            "puede_consultar": self.client is not None,
            "ahora": datetime.now().astimezone().isoformat(timespec="seconds"),
        }

    def documents(self, params: dict[str, str]) -> dict[str, Any]:
        page = _int(params.get("page"), 1, 1, 100_000)
        per_page = _int(params.get("per_page"), 25, 5, 200)
        estados = tuple(e for e in params.get("estado", "").split(",") if e in Estado.__members__)
        rows, total = self.db.search(
            q=params.get("q", "")[:100], estados=estados, tipo=params.get("tipo", "")[:2],
            desde=_date(params.get("desde")), hasta=_date(params.get("hasta")),
            sort=params.get("sort", "actualizado_en"), desc=params.get("dir", "desc") != "asc",
            page=page, per_page=per_page,
        )
        return {"items": [self._doc(r) for r in rows], "total": total, "page": page, "per_page": per_page}

    def document(self, clave: str) -> dict[str, Any]:
        row = self._row(clave)
        doc = self._doc(row)
        doc["eventos"] = [{"ts": e["ts"], "estado": e["estado"], "detalle": e["detalle"]}
                          for e in self.db.events(clave)]
        return doc

    def export_csv(self, params: dict[str, str]) -> bytes:
        params = {**params, "page": "1", "per_page": "200"}
        out = io.StringIO()
        w = csv.writer(out, delimiter=";")
        w.writerow(REPORT_COLUMNS)
        page = 1
        while True:
            params["page"] = str(page)
            rows, total = self.db.search(
                q=params.get("q", "")[:100],
                estados=tuple(e for e in params.get("estado", "").split(",") if e in Estado.__members__),
                tipo=params.get("tipo", "")[:2], desde=_date(params.get("desde")),
                hasta=_date(params.get("hasta")), sort=params.get("sort", "actualizado_en"),
                desc=params.get("dir", "desc") != "asc", page=page, per_page=200,
            )
            for r in rows:
                w.writerow([r[c] if r[c] is not None else "" for c in REPORT_COLUMNS])
            if page * 200 >= total:
                break
            page += 1
        return ("﻿" + out.getvalue()).encode("utf-8")   # BOM: Excel lee bien las tildes

    def file_for(self, clave: str, kind: str) -> Path:
        row = self._row(clave)
        raw = row[FILE_KINDS[kind]]
        if not raw:
            raise PanelError(HTTPStatus.NOT_FOUND, "Este comprobante no tiene ese archivo")
        path = Path(raw).resolve()
        roots = [self.s.work_dir.resolve(), self.s.inbox_dir.resolve()]
        if not any(path.is_relative_to(r) for r in roots):
            raise PanelError(HTTPStatus.FORBIDDEN, "El archivo está fuera de las carpetas del puente")
        if not path.is_file():
            raise PanelError(HTTPStatus.NOT_FOUND, f"El archivo ya no existe: {path.name}")
        return path

    # --- acciones -----------------------------------------------------------
    def retry(self, clave: str, forzar: bool) -> dict[str, Any]:
        row = self._row(clave)
        ok, msg = retry_one(self.s, self.db, row, forzar=forzar)
        log.info("Panel: reintento de %s (forzar=%s): %s", clave, forzar, msg)
        return {"ok": ok, "mensaje": _sin_clave(msg, clave), "documento": self.document(clave)}

    def retry_all(self, forzar: bool, todos: bool) -> dict[str, Any]:
        acciones = retry_errors(self.s, self.db, forzar=forzar, todos=todos)
        hechas = [a for a in acciones if "OMITIDO" not in a]
        return {"reintentados": len(hechas), "omitidos": len(acciones) - len(hechas), "detalle": acciones}

    def upload(self, body: dict[str, Any], usuario: str = "", origen: str = "") -> dict[str, Any]:
        """Carga masiva: modo "revisar" no toca nada; "encolar" deja los válidos en la bandeja
        y guarda la constancia de la carga."""
        archivos = body.get("archivos")
        if not isinstance(archivos, list) or not archivos:
            raise PanelError(HTTPStatus.BAD_REQUEST, "No se recibieron archivos")
        carga = None
        responsable = " ".join(str(body.get("responsable") or "").split())[:80]
        if body.get("modo") == "encolar":
            if not responsable:
                raise PanelError(HTTPStatus.BAD_REQUEST, "Indica quién sube los archivos: queda en la constancia")
            usuario = responsable if usuario in ("", "local", responsable) else f"{responsable} (acceso {usuario})"
        try:
            if body.get("modo") == "encolar":
                checks, carga_id = upload.enqueue(self.s, self.db, archivos, usuario=usuario, origen=origen,
                                                  separar_especiales=body.get("separar_especiales", True) is not False)
                log.info("Panel: carga N.º %d de %s (%s), %d de %d archivos encolados", carga_id, usuario,
                         origen, sum(c.encolable for c in checks), len(checks))
                carga = self._carga(self.db.upload(carga_id)[0])
            else:
                checks = upload.check_files(self.s, self.db, archivos)
        except ValueError as exc:
            raise PanelError(HTTPStatus.BAD_REQUEST, str(exc)) from None
        return {"archivos": upload.as_dicts(checks),
                "encolables": sum(c.encolable for c in checks), "carga": carga}

    # --- bandeja de preparación ---------------------------------------------
    def bandeja(self) -> dict[str, Any]:
        return Bandeja(self.s, self.db).summary()

    def bandeja_action(self, action: str, body: dict[str, Any], usuario: str) -> dict[str, Any]:
        responsable = " ".join(str(body.get("responsable") or "").split())[:80]
        if not responsable:
            raise PanelError(HTTPStatus.BAD_REQUEST, "Indica quién hace el cambio: queda en el historial")
        usuario = responsable if usuario in ("", "local", responsable) else f"{responsable} (acceso {usuario})"
        claves = body.get("claves") or []
        if not isinstance(claves, list) or not all(isinstance(c, str) for c in claves):
            raise PanelError(HTTPStatus.BAD_REQUEST, "Lista de comprobantes inválida")
        if action != "traer-rechazados" and not claves:
            raise PanelError(HTTPStatus.BAD_REQUEST, "No se eligió ningún comprobante")
        tray = Bandeja(self.s, self.db, self._resumenes())
        if action == "liberar" and any(x.clave == "carpeta" for x in pendientes(self.s)):
            raise PanelError(HTTPStatus.CONFLICT,
                             "Primero indica en Configuración la carpeta donde el sistema deja los TXT")
        if action == "liberar":
            r = tray.release(claves, usuario, incluir_sin_referencia=bool(body.get("incluir_sin_referencia")),
                             incluir_separados=bool(body.get("incluir_separados")))
        elif action == "fecha":
            r = tray.assign_date(claves, str(body.get("fecha") or ""), usuario)
        elif action == "separar":
            r = tray.set_separated(claves, bool(body.get("separar", True)), usuario)
        elif action == "descartar":
            r = tray.discard(claves, usuario)
        else:
            r = tray.bring_back_rejected_by_date(usuario)
        log.info("Panel: bandeja %s por %s, %d hechos, %d omitidos", action, usuario, len(r.hechos), len(r.omitidos))
        return r.as_dict()

    def _resumenes(self) -> Resumenes:
        """Boletas por resumen diario: firma con el certificado de la clínica y envía por el cliente actual."""
        signer = None
        if self.s.sovos_signing_mode == "propio" and self.s.cert_path:
            try:
                signer = Signer.from_pkcs12(Path(self.s.cert_path), self.s.cert_password.get_secret_value())
            except SigningError as exc:
                log.warning("Resumen diario sin certificado utilizable: %s", exc)
        return Resumenes(self.s, self.db, self.client, signer)

    # --- configuración ---------------------------------------------------------
    def _config(self) -> Configuracion:
        return Configuracion(self.s, self.env_file, self._reload, self._problema_firma)

    def config(self) -> dict[str, Any]:
        return self._config().estado()

    def config_action(self, action: str, body: dict[str, Any], usuario: str) -> dict[str, Any]:
        responsable = " ".join(str(body.get("responsable") or "").split())[:80]
        if not responsable:
            raise PanelError(HTTPStatus.BAD_REQUEST, "Indica quién hace el cambio: queda en el registro")
        usuario = responsable if usuario in ("", "local", responsable) else f"{responsable} (acceso {usuario})"
        cfg = self._config()
        try:
            if action == "certificado":
                r = cfg.guardar_certificado(str(body.get("contenido") or ""), str(body.get("nombre") or ""),
                                            str(body.get("clave") or ""), usuario)
            elif action == "sovos":
                r = cfg.guardar_sovos(str(body.get("ambiente") or ""), str(body.get("client_id") or ""),
                                      str(body.get("client_secret") or ""), usuario)
            elif action == "clave":
                cfg.cambiar_clave_panel(str(body.get("nueva") or ""), usuario)
                r = {}
            elif body.get("accion") == "revisar":
                r = cfg.revisar_carpeta(str(body.get("ruta") or ""))
            else:
                r = self._guardar_carpeta(cfg, str(body.get("ruta") or ""), body.get("existentes"), usuario)
        except ConfigError as exc:
            raise PanelError(HTTPStatus.BAD_REQUEST, str(exc)) from None
        return {**r, "estado": self._config().estado()}

    def _guardar_carpeta(self, cfg: Configuracion, ruta: str, existentes: Any, usuario: str) -> dict[str, Any]:
        """Si la carpeta ya tiene TXT, se pasan a la bandeja (recomendado) antes de vigilarla:
        si no, el servicio los enviaría todos sin revisar (fechas vencidas, inafectos...)."""
        carpeta = cfg.revisar_carpeta(ruta)
        if carpeta["txt"] and existentes not in ("bandeja", "dejar"):
            return {"decision": True, **carpeta}
        with self.lock:
            importados = self._importar_carpeta(Path(carpeta["ruta"]), usuario) \
                if carpeta["txt"] and existentes == "bandeja" else None
            r = cfg.guardar_carpeta(ruta, usuario)
        return {**r, "importados": importados}

    def _importar_carpeta(self, carpeta: Path, usuario: str) -> dict[str, Any]:
        archivos = sorted(f for f in carpeta.iterdir() if f.is_file() and f.suffix.lower() == ".txt")
        destino = self.s.work_dir / "importados" / datetime.now().strftime("%Y%m%d_%H%M%S")
        destino.mkdir(parents=True, exist_ok=True)
        cuenta = {"en_bandeja": 0, "separados": 0, "no_subidos": 0, "cargas": []}
        for i in range(0, len(archivos), upload.MAX_FILES):
            lote = archivos[i:i + upload.MAX_FILES]
            checks, carga_id = upload.enqueue(self.s, self.db, [
                {"nombre": f.name, "contenido": base64.b64encode(f.read_bytes()).decode()} for f in lote],
                usuario=usuario, origen=f"carpeta {carpeta}")
            cuenta["cargas"].append(carga_id)
            for c in checks:
                if c.encolable:
                    cuenta["en_bandeja"] += 1
                    cuenta["separados"] += c.mensajes[0].startswith("Separado")
                else:
                    cuenta["no_subidos"] += 1
            for f in lote:   # el original queda guardado aparte; la carpeta vigilada queda limpia
                f.replace(destino / f.name)
        log.info("Configuración: %d TXT de %s pasados a la bandeja por %s (originales en %s)",
                 cuenta["en_bandeja"], carpeta, usuario, destino)
        cuenta["originales"] = str(destino)
        return cuenta

    def uploads(self) -> dict[str, Any]:
        return {"cargas": [self._carga(r) for r in self.db.uploads(20)]}

    def upload_csv(self, carga_id: int) -> tuple[str, bytes]:
        head, files = self.db.upload(carga_id)
        if head is None:
            raise PanelError(HTTPStatus.NOT_FOUND, f"No existe la carga N.º {carga_id}")
        out = io.StringIO()
        w = csv.writer(out, delimiter=";")
        w.writerow(["Constancia de carga de TXT", f"N.º {head['id']}"])
        w.writerow(["Fecha y hora", head["ts"]])
        w.writerow(["Usuario", head["usuario"]])
        w.writerow(["Equipo (IP)", head["origen"]])
        w.writerow(["Archivos", head["total"], "Dejados en la bandeja", head["encolados"]])
        w.writerow([])
        w.writerow(["Archivo", "Guardado como", "Comprobante", "Resultado", "Detalle", "Bytes", "Huella SHA-256"])
        for f in files:
            w.writerow([f["nombre"], f["nombre_final"] or "", f["clave"] or "",
                        "Dejado en la bandeja" if f["encolado"] else f"No subido ({f['resultado']})",
                        f["detalle"] or "", f["bytes"] or "", f["sha256"] or ""])
        return f"constancia_carga_{head['id']:05d}.csv", ("\ufeff" + out.getvalue()).encode("utf-8")

    @staticmethod
    def _carga(r: sqlite3.Row) -> dict[str, Any]:
        return {"id": r["id"], "ts": r["ts"], "usuario": r["usuario"], "origen": r["origen"],
                "total": r["total"], "encolados": r["encolados"]}

    def consult(self, clave: str) -> dict[str, Any]:
        """Consulta a Sovos ahora. Si está pendiente, actualiza el estado como lo haría el servicio."""
        row = self._row(clave)
        if self.client is None:
            raise PanelError(HTTPStatus.CONFLICT, "Modo en seco: no hay conexión con Sovos")
        if not row["document_id"]:
            raise PanelError(HTTPStatus.CONFLICT, "Sovos aún no tiene este comprobante (sin documentId)")
        with self._sovos_lock:
            try:
                notifs = parse_notifications(self.client.document_notifications(row["document_id"]))
            except SovosError as exc:
                raise PanelError(HTTPStatus.BAD_GATEWAY, f"Sovos no respondió: {exc}") from None
            actualizado = False
            if row["estado"] in PENDIENTES and self.poller is not None:
                self.poller.handle(clave, notifs)
                self.poller.flush_acks()
                actualizado = True
        return {
            "actualizado": actualizado,
            "notificaciones": [{
                "codigo_sovos": n.response_code, "codigo_sunat": n.govt_code,
                "mensaje": n.govt_message, "estado_flujo": n.cloud_code,
                "fecha": _ms_iso(n.raw.get("createdDate")),
            } for n in notifs],
            "documento": self.document(clave),
        }

    # --- auxiliares ---------------------------------------------------------
    def _row(self, clave: str) -> sqlite3.Row:
        row = self.db.get(clave)
        if row is None:
            raise PanelError(HTTPStatus.NOT_FOUND, f"No existe el comprobante {clave}")
        return row

    def _doc(self, r: sqlite3.Row) -> dict[str, Any]:
        reintento = None
        if r["estado"] in RETRYABLE:
            if r["estado"] == Estado.ERROR_CONEXION and r["document_id"]:
                reintento = "consulta"
            elif r["envio_incierto"]:
                reintento = "forzado"
            else:
                reintento = "reenvio"
        return {
            "clave": r["clave"], "tipo": r["tipo"], "serie_numero": r["serie_numero"],
            "estado": r["estado"], "document_id": r["document_id"], "fecha_emision": r["fecha_emision"],
            "moneda": r["moneda"], "importe_total": r["importe_total"], "cliente_doc": r["cliente_doc"],
            "enviado_en": r["enviado_en"], "envio_incierto": bool(r["envio_incierto"]),
            "intentos_envio": r["intentos_envio"], "codigo_sovos": r["codigo_sovos"],
            "codigo_sunat": r["codigo_sunat"], "mensaje": r["mensaje"],
            "creado_en": r["creado_en"], "actualizado_en": r["actualizado_en"],
            "archivos": {k: bool(r[col]) and Path(r[col]).is_file() for k, col in FILE_KINDS.items()},
            "reintento": reintento,
        }


# --- transporte HTTP --------------------------------------------------------
def make_handler(app: PanelApp):

    class Handler(BaseHTTPRequestHandler):
        server_version = f"PuenteSovos/{__version__}"
        sys_version = ""

        def log_message(self, fmt: str, *args) -> None:   # al log del puente, no a stderr
            log.debug("panel %s - " + fmt, self.address_string(), *args)

        # --- despacho ---------------------------------------------------
        def do_GET(self) -> None:
            self._dispatch("GET")

        def do_POST(self) -> None:
            self._dispatch("POST")

        def _dispatch(self, method: str) -> None:
            try:
                if app.auth_expected() and not self._authorized():
                    self._send(HTTPStatus.UNAUTHORIZED, b"Se requiere usuario y clave",
                               "text/plain; charset=utf-8",
                               {"WWW-Authenticate": 'Basic realm="Puente Sovos", charset="UTF-8"'})
                    return
                url = urlparse(self.path)
                params = {k: v[-1] for k, v in parse_qs(url.query).items()}
                if method == "POST" and self.headers.get(ACTION_HEADER) != "1":
                    raise PanelError(HTTPStatus.FORBIDDEN, "Acción rechazada: origen no permitido")
                self._route(method, url.path, params)
            except PanelError as exc:
                self._json({"error": str(exc)}, exc.status)
            except Exception:
                log.exception("Error en el panel web (%s %s)", method, self.path)
                self._json({"error": "Error interno del panel; revisar el log del puente"},
                           HTTPStatus.INTERNAL_SERVER_ERROR)

        def _route(self, method: str, path: str, params: dict[str, str]) -> None:
            if method == "GET" and path in ("/", "/index.html"):
                return self._static("index.html")
            if method == "GET" and path.startswith("/static/"):
                return self._static(path.removeprefix("/static/"))
            if method == "GET" and path == "/api/summary":
                return self._json(app.summary())
            if method == "GET" and path == "/api/documents":
                return self._json(app.documents(params))
            if method == "GET" and path == "/api/export.csv":
                name = f"comprobantes_{date.today().isoformat()}.csv"
                return self._send(HTTPStatus.OK, app.export_csv(params), "text/csv; charset=utf-8",
                                  {"Content-Disposition": f'attachment; filename="{name}"'})
            if method == "POST" and path == "/api/upload":
                return self._json(app.upload(self._body(MAX_UPLOAD_BODY), self._user(), self.client_address[0]))
            if method == "GET" and path == "/api/config":
                return self._json(app.config())
            if method == "POST" and path.startswith("/api/config/") and path[12:] in _CONFIG_ACTIONS:
                return self._json(app.config_action(path[12:], self._body(MAX_CONFIG_BODY), self._user()))
            if method == "GET" and path == "/api/bandeja":
                return self._json(app.bandeja())
            if method == "POST" and path.startswith("/api/bandeja/") and path[13:] in _BANDEJA_ACTIONS:
                return self._json(app.bandeja_action(path[13:], self._body(), self._user()))
            if method == "GET" and path == "/api/cargas":
                return self._json(app.uploads())
            m = _CARGA_ROUTE.match(path)
            if method == "GET" and m:
                name, data = app.upload_csv(int(m.group(1)))
                return self._send(HTTPStatus.OK, data, "text/csv; charset=utf-8",
                                  {"Content-Disposition": f'attachment; filename="{name}"'})
            if method == "POST" and path == "/api/retry-errors":
                body = self._body()
                return self._json(app.retry_all(bool(body.get("forzar")), bool(body.get("todos"))))
            m = _DOC_ROUTE.match(path)
            if m:
                clave, action, kind = unquote(m.group(1)), m.group(2), m.group(3)
                if method == "GET" and action is None:
                    return self._json(app.document(clave))
                if method == "GET" and kind:
                    f = app.file_for(clave, kind)
                    ctype = mimetypes.guess_type(f.name)[0] or "application/octet-stream"
                    disp = "inline" if kind == "pdf" else "attachment"
                    return self._send(HTTPStatus.OK, f.read_bytes(), ctype,
                                      {"Content-Disposition": f'{disp}; filename="{f.name}"'})
                if method == "POST" and action == "retry":
                    return self._json(app.retry(clave, bool(self._body().get("forzar"))))
                if method == "POST" and action == "consultar":
                    return self._json(app.consult(clave))
            raise PanelError(HTTPStatus.NOT_FOUND, "Ruta no encontrada")

        # --- utilidades -------------------------------------------------
        def _authorized(self) -> bool:
            header = self.headers.get("Authorization", "")
            return header.startswith("Basic ") and hmac.compare_digest(header[6:].strip(), app.auth_expected())

        def _user(self) -> str:
            """Usuario de HTTP Basic, o "local" si el panel se abrió en el mismo equipo sin clave."""
            header = self.headers.get("Authorization", "")
            if header.startswith("Basic "):
                try:
                    return base64.b64decode(header[6:].strip()).decode("utf-8").split(":", 1)[0] or "local"
                except (ValueError, UnicodeDecodeError):
                    pass
            return "local"

        def _body(self, max_bytes: int = MAX_BODY) -> dict[str, Any]:
            length = _int(self.headers.get("Content-Length"), 0, 0, 1 << 31)
            if length > max_bytes:
                raise PanelError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                                 f"La carga supera {max_bytes // (1024 * 1024) or 1} MB; súbela en partes")
            if not length:
                return {}
            try:
                data = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                raise PanelError(HTTPStatus.BAD_REQUEST, "El cuerpo no es JSON válido") from None
            return data if isinstance(data, dict) else {}

        def _static(self, name: str) -> None:
            if name != "index.html" and name not in STATIC_FILES:
                raise PanelError(HTTPStatus.NOT_FOUND, "Archivo no encontrado")
            data = (STATIC_DIR / name).read_bytes()
            ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
            if ctype.startswith("text/") or name.endswith(".js"):
                ctype += "; charset=utf-8"
            self._send(HTTPStatus.OK, data, ctype, {"Cache-Control": "no-cache"})

        def _json(self, payload: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
            self._send(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8", {"Cache-Control": "no-store"})

        def _send(self, status: HTTPStatus, body: bytes, ctype: str, headers: dict[str, str] | None = None) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy",
                             "default-src 'self'; style-src 'self' https://fonts.googleapis.com; "
                             "font-src https://fonts.gstatic.com; img-src 'self' data:; frame-ancestors 'none'")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

    return Handler


def start_panel(app: PanelApp) -> tuple[ThreadingHTTPServer, threading.Thread] | None:
    """Levanta el panel en un hilo. Devuelve None si la configuración no es segura."""
    s = app.s
    password = s.ui_password.get_secret_value()
    if not _is_loopback(s.ui_host) and not password:
        log.error("Panel web NO iniciado: UI_HOST=%s expone datos de pacientes en la red y UI_PASSWORD "
                  "está vacío. Defina UI_PASSWORD o use UI_HOST=127.0.0.1", s.ui_host)
        return None
    server = ThreadingHTTPServer((s.ui_host, s.ui_port), make_handler(app))
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, name="panel", daemon=True)
    thread.start()
    host = "localhost" if _is_loopback(s.ui_host) else s.ui_host
    log.info("Panel web en http://%s:%d%s", host, server.server_address[1],
             "" if password else " (sin clave: solo accesible desde este equipo)")
    return server, thread


# --- conversiones -----------------------------------------------------------
def _int(raw: str | None, default: int, lo: int, hi: int) -> int:
    try:
        return min(max(int(raw), lo), hi) if raw not in (None, "") else default
    except ValueError:
        return default


def _date(raw: str | None) -> str:
    try:
        return date.fromisoformat(raw).isoformat() if raw else ""
    except ValueError:
        return ""


def _num(raw: str | None) -> float:
    try:
        return float(raw or 0)
    except ValueError:
        return 0.0


def _ms_iso(ms: Any) -> str | None:
    try:
        return datetime.fromtimestamp(int(ms) / 1000).astimezone().isoformat(timespec="seconds")
    except (TypeError, ValueError, OSError):
        return None


def _sin_clave(msg: str, clave: str) -> str:
    return msg.removeprefix(f"{clave}: ")
