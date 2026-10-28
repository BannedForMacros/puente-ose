"""Errores del cliente Sovos."""

from __future__ import annotations


class SovosError(Exception):
    """Base de errores de comunicación con Sovos."""


class SovosAuthError(SovosError):
    """No se pudo obtener el token o Sovos rechazó las credenciales."""


class SovosClientError(SovosError):
    """Respuesta 4xx (salvo 408/429): la solicitud tiene un problema, no se reintenta."""

    def __init__(self, status: int, body: str):
        self.status = status
        self.body = body[:2000]
        super().__init__(f"HTTP {status}: {self.body[:500]}")


class SovosConnectionError(SovosError):
    """Fallo de red o 5xx persistente.

    `transmitted` indica si la solicitud pudo llegar a Sovos (timeout de
    lectura, conexión cortada después de enviar, 500/504). En un POST de
    documento eso significa que Sovos podría haberlo registrado: el envío
    queda "incierto" y no se reintenta automáticamente, porque Sovos no
    detecta duplicados.
    """

    def __init__(self, message: str, *, transmitted: bool, status: int | None = None):
        self.transmitted = transmitted
        self.status = status
        super().__init__(message)
