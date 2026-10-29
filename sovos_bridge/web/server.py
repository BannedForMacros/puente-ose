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
from sovos_bridge.config import Settings
from sovos_bridge.service.operations import REPORT_COLUMNS, RETRYABLE, retry_errors, retry_one
from sovos_bridge.service.status import StatusPoller
from sovos_bridge.sovos import SovosClient, SovosError, parse_notifications
from sovos_bridge.storage import PENDIENTES, Database, Estado

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).with_name("static")
STATIC_FILES = {"app.css", "app.js", "favicon.svg"}
ACTION_HEADER = "X-Sovos-Panel"
HEARTBEAT_STALE_SECONDS = 120
FILE_KINDS = {"txt": "archivo", "xml": "xml_path", "pdf": "pdf_path", "sbd": "sbd_path"}
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
def make_handler(app: PanelApp, user: str, password: str):
    expected = base64.b64encode(f"{user}:{password}".encode()).decode() if password else ""

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
                if expected and not self._authorized():
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
            return header.startswith("Basic ") and hmac.compare_digest(header[6:].strip(), expected)

        def _body(self) -> dict[str, Any]:
            length = _int(self.headers.get("Content-Length"), 0, 0, 64_000)
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
    server = ThreadingHTTPServer((s.ui_host, s.ui_port), make_handler(app, s.ui_user, password))
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
