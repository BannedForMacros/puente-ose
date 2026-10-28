"""Cliente REST de Sovos Indirect Tax API (Perú).

Política de reintentos:
  - 408 y 429: esperar al menos 60 s (o Retry-After si es mayor) y reintentar.
  - 5xx: backoff exponencial (2, 4, 8, ... s; máximo 300 s).
  - Otros 4xx: error de la solicitud, no se reintenta.
  - 401: se renueva el token una vez y se reintenta.

En el POST de documentos (no idempotente: Sovos no detecta duplicados) solo
se reintenta cuando hay certeza de que Sovos no procesó la solicitud:
error de conexión antes de enviar, 408, 429, 502 y 503. Un timeout de lectura
o un 500/504 dejan el envío como "incierto" y se detiene.
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any, Callable

import httpx

from sovos_bridge.sovos.auth import TokenProvider
from sovos_bridge.sovos.errors import SovosAuthError, SovosClientError, SovosConnectionError

log = logging.getLogger(__name__)

RATE_LIMIT_WAIT = 60.0
BACKOFF_BASE = 2.0
BACKOFF_MAX = 300.0
POST_SAFE_5XX = {502, 503}
_NOT_SENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)


class SovosClient:
    def __init__(self, base_url: str, auth: TokenProvider, *, tax_id: str, polling_system_id: str,
                 timeout: float = 60, max_retries: int = 5, http: httpx.Client | None = None,
                 sleep: Callable[[float], None] = time.sleep):
        if not base_url:
            raise ValueError("SOVOS_BASE_URL no está configurado")
        self.base_url = base_url.rstrip("/")
        self.auth = auth
        self.tax_id = tax_id
        self.polling_system_id = polling_system_id
        self.max_retries = max_retries
        self._http = http or httpx.Client(timeout=timeout)
        self._sleep = sleep

    def close(self) -> None:
        self._http.close()

    # --- operaciones --------------------------------------------------------
    def send_document(self, sbd_base64: str) -> str:
        """POST /v1/documents. Devuelve el documentId asignado por Sovos."""
        resp = self._request("POST", "/v1/documents", json={"data": sbd_base64, "dataEncoding": "base64"},
                             idempotent=False)
        body = _json(resp)
        data = body.get("data") if isinstance(body.get("data"), dict) else body
        doc_id = data.get("documentId") or data.get("documentID") or data.get("id")
        if not doc_id:
            # Sovos aceptó (2xx) pero sin id: no se puede reenviar a ciegas.
            raise SovosConnectionError(f"Sovos respondió HTTP {resp.status_code} sin documentId",
                                       transmitted=True, status=resp.status_code)
        return str(doc_id)

    def document_notifications(self, document_id: str) -> Any:
        resp = self._request("GET", f"/v1/documents/PE/{document_id}/notifications",
                             params={"includeAcknowledged": "true"})
        return _json(resp)

    def list_notifications(self, page: int = 1, per_page: int = 50) -> Any:
        resp = self._request("GET", "/v1/notifications/PE", params={
            "taxId": self.tax_id, "sourceSystemId": self.polling_system_id,
            "page": page, "perPage": per_page,
        })
        return _json(resp)

    def acknowledge(self, notification_ids: list[str]) -> None:
        if not notification_ids:
            return
        self._request("PUT", "/v1/notifications/PE",
                      json=[{"status": "read", "notificationId": i} for i in notification_ids])

    def download(self, url: str) -> bytes:
        """Descarga un adjunto enlazado en la respuesta (XML/PDF).

        El token solo se envía si el enlace es del mismo host que la API; un
        enlace externo (p. ej. URL prefirmada) se descarga sin credenciales.
        """
        if httpx.URL(url).host in ("", httpx.URL(self.base_url).host):
            return self._request("GET", url).content
        try:
            resp = self._http.get(url)
        except httpx.HTTPError as exc:
            raise SovosConnectionError(f"No se pudo descargar el adjunto: {type(exc).__name__}",
                                       transmitted=False) from None
        if resp.status_code >= 400:
            raise SovosClientError(resp.status_code, "descarga de adjunto")
        return resp.content

    # --- núcleo HTTP --------------------------------------------------------
    def _request(self, method: str, path: str, *, json: Any = None, params: dict | None = None,
                 idempotent: bool = True) -> httpx.Response:
        url = path if path.startswith("http") else self.base_url + path
        refreshed = False
        attempt = 0
        while True:
            correlation = str(uuid.uuid4())
            headers = {
                "Authorization": f"Bearer {self.auth.get_token()}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "x-correlationId": correlation,
            }
            try:
                resp = self._http.request(method, url, json=json, params=params, headers=headers)
            except _NOT_SENT as exc:
                if attempt >= self.max_retries:
                    raise SovosConnectionError(f"Sin conexión con Sovos: {type(exc).__name__}",
                                               transmitted=False) from None
                self._wait(attempt, f"{method} {path}: {type(exc).__name__}, correlationId={correlation}")
                attempt += 1
                continue
            except httpx.TransportError as exc:
                if not idempotent or attempt >= self.max_retries:
                    raise SovosConnectionError(
                        f"Conexión interrumpida con Sovos tras enviar la solicitud: {type(exc).__name__}",
                        transmitted=True) from None
                self._wait(attempt, f"{method} {path}: {type(exc).__name__}, correlationId={correlation}")
                attempt += 1
                continue

            status = resp.status_code
            if status < 300:
                log.debug("%s %s -> %d (correlationId=%s)", method, path, status, correlation)
                return resp
            if status == 401 and not refreshed:
                log.warning("Sovos respondió 401; se renueva el token y se reintenta")
                self.auth.invalidate()
                refreshed = True
                continue
            if status in (401, 403):
                raise SovosAuthError(f"Sovos rechazó las credenciales (HTTP {status})")
            if status in (408, 429):
                if attempt >= self.max_retries:
                    raise SovosConnectionError(f"Sovos sigue respondiendo HTTP {status}", transmitted=False,
                                               status=status)
                wait = max(RATE_LIMIT_WAIT, _retry_after(resp))
                log.warning("%s %s -> HTTP %d; se espera %.0f s (correlationId=%s)",
                            method, path, status, wait, correlation)
                self._sleep(wait)
                attempt += 1
                continue
            if status >= 500:
                safe = idempotent or status in POST_SAFE_5XX
                if not safe:
                    raise SovosConnectionError(f"Sovos respondió HTTP {status} al recibir el documento",
                                               transmitted=True, status=status)
                if attempt >= self.max_retries:
                    raise SovosConnectionError(f"Sovos respondió HTTP {status} tras {attempt + 1} intentos",
                                               transmitted=False, status=status)
                self._wait(attempt, f"{method} {path} -> HTTP {status}, correlationId={correlation}")
                attempt += 1
                continue
            raise SovosClientError(status, resp.text)

    def _wait(self, attempt: int, reason: str) -> None:
        delay = min(BACKOFF_BASE * (2 ** attempt), BACKOFF_MAX)
        log.warning("%s; reintento en %.0f s", reason, delay)
        self._sleep(delay)


def _retry_after(resp: httpx.Response) -> float:
    try:
        return float(resp.headers.get("Retry-After", "0"))
    except ValueError:
        return 0.0


def _json(resp: httpx.Response) -> Any:
    if not resp.content:
        return {}
    try:
        return resp.json()
    except ValueError:
        return {}
