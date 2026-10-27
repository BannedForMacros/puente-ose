"""Comunicación de baja: interfaz preparada.

La guía de Sovos para Perú menciona la baja (dentro de 7 días, si el
comprobante no fue entregado al comprador) pero no publica el formato del SBD.
El encabezado SBDH genérico admite `Transmission.OperationType = CANCEL`.
Cuando Sovos confirme el formato, implementar `build_baja_sbd` y conectar el
tipo RA en service/pipeline.py.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from lxml import etree


@dataclass
class ItemBaja:
    tipo_comprobante: str     # 01, 07, 08
    serie_numero: str         # FT01-00000758
    motivo: str


@dataclass
class ComunicacionBaja:
    ruc_emisor: str
    fecha_generacion: str     # AAAA-MM-DD
    fecha_referencia: str     # fecha de emisión de los comprobantes
    correlativo: str
    items: list[ItemBaja] = field(default_factory=list)

    @property
    def identificador(self) -> str:
        return f"RA-{self.fecha_generacion.replace('-', '')}-{self.correlativo}"


def build_baja_sbd(baja: ComunicacionBaja, sender_system_id: str) -> etree._Element:
    raise NotImplementedError(
        "Formato de comunicación de baja pendiente de confirmar con Sovos"
    )
