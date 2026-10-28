"""Autenticación con Sovos, intercambiable.

El método definitivo está pendiente de confirmar con Sovos. Cualquier
proveedor debe implementar `get_token()` e `invalidate()`; el cliente HTTP
llama a `invalidate()` y reintenta una vez si recibe 401.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Protocol

import httpx

from sovos_bridge.config import Settings
from sovos_bridge.sovos.errors import SovosAuthError

log = logging.getLogger(__name__)


class TokenProvider(Protocol):
    def get_token(self) -> str: ...
    def invalidate(self) -> None: ...


class StaticTokenProvider:
    """Token fijo entregado por Sovos (SOVOS_STATIC_TOKEN)."""

    def __init__(self, token: str):
        if not token:
            raise SovosAuthError("SOVOS_STATIC_TOKEN está vacío")
        self._token = token

    def get_token(self) -> str:
        return self._token

    def invalidate(self) -> None:
        log.error("Sovos rechazó el token fijo (401); revisar SOVOS_STATIC_TOKEN")


class OAuth2ClientCredentials:
    """OAuth2 client credentials con caché y renovación antes de expirar."""

    SKEW_SECONDS = 60

    def __init__(self, token_url: str, client_id: str, client_secret: str, scope: str = "",
                 *, http: httpx.Client | None = None, clock: Callable[[], float] = time.monotonic,
                 timeout: float = 30):
        if not token_url or not client_id or not client_secret:
            raise SovosAuthError("Faltan SOVOS_TOKEN_URL, SOVOS_CLIENT_ID o SOVOS_CLIENT_SECRET")
        self._url = token_url
        self._client_id = client_id
        self._secret = client_secret
        self._scope = scope
        self._http = http or httpx.Client(timeout=timeout)
        self._clock = clock
        self._token: str | None = None
        self._expires_at = 0.0
        self._lock = threading.Lock()

    def get_token(self) -> str:
        with self._lock:
            if self._token and self._clock() < self._expires_at:
                return self._token
            self._token, self._expires_at = self._fetch()
            return self._token

    def invalidate(self) -> None:
        with self._lock:
            self._token = None
            self._expires_at = 0.0

    def _fetch(self) -> tuple[str, float]:
        data = {"grant_type": "client_credentials", "client_id": self._client_id,
                "client_secret": self._secret}
        if self._scope:
            data["scope"] = self._scope
        try:
            resp = self._http.post(self._url, data=data, headers={"Accept": "application/json"})
        except httpx.HTTPError as exc:
            raise SovosAuthError(f"No se pudo contactar el servidor de tokens: {type(exc).__name__}") from None
        if resp.status_code != 200:
            # No se registra el cuerpo: podría reflejar credenciales.
            raise SovosAuthError(f"El servidor de tokens respondió HTTP {resp.status_code}")
        try:
            body = resp.json()
            token = body["access_token"]
        except (ValueError, KeyError):
            raise SovosAuthError("Respuesta del servidor de tokens sin access_token") from None
        expires_in = float(body.get("expires_in") or 3600)
        log.info("Token de Sovos obtenido; vence en %d s", int(expires_in))
        return token, self._clock() + max(expires_in - self.SKEW_SECONDS, 0)


def build_token_provider(s: Settings, http: httpx.Client | None = None) -> TokenProvider:
    if s.sovos_auth_mode == "static":
        return StaticTokenProvider(s.sovos_static_token.get_secret_value())
    return OAuth2ClientCredentials(
        s.sovos_token_url, s.sovos_client_id, s.sovos_client_secret.get_secret_value(),
        s.sovos_scope, http=http, timeout=s.http_timeout_seconds,
    )
