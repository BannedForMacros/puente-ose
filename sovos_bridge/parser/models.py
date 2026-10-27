"""Modelo del comprobante leído del TXT.

Los montos se guardan como texto, exactamente como vienen en el TXT: el puente
anterior los copiaba tal cual al XML (con sus decimales) y SUNAT los aceptaba.
`dec()` permite usarlos como Decimal para controles y reportes.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

from pydantic import BaseModel, Field


def dec(value: str | None) -> Decimal:
    try:
        return Decimal((value or "0").strip() or "0")
    except InvalidOperation:
        return Decimal("0")


class Party(BaseModel):
    tipo_documento: str = ""
    numero_documento: str = ""
    razon_social: str = ""
    nombre_comercial: str = ""
    ubigeo: str = ""
    direccion: str = ""
    urbanizacion: str = ""
    provincia: str = ""
    departamento: str = ""
    distrito: str = ""
    pais: str = ""
    codigo_establecimiento: str = ""
    telefono: str = ""
    correo: str = ""


class Tax(BaseModel):
    """Un tributo: código catálogo 05, monto, base y datos de ítem."""
    codigo: str
    monto: str
    base: str = ""
    afectacion: str = ""     # catálogo 07 (solo ítems)
    porcentaje: str = ""     # solo ítems


class Item(BaseModel):
    numero_item: str
    codigo: str = ""
    descripcion: str = ""
    cantidad: str = ""
    unidad_medida: str = ""
    valor_unitario: str = ""
    precio_unitario: str = ""
    tributos: list[Tax] = Field(default_factory=list)
    valor_venta: str = ""
    total_impuestos: str = ""
    raw: dict[str, str] = Field(default_factory=dict)


class Reference(BaseModel):
    """Comprobante afectado por una nota de crédito o débito."""
    serie_numero: str
    tipo_comprobante: str
    codigo_motivo: str
    descripcion_motivo: str = ""


class Document(BaseModel):
    ruc_emisor: str
    tipo_comprobante: str        # 01, 03, 07, 08
    serie_numero: str            # BT05-00125304
    fecha_emision: str
    hora_emision: str = ""
    fecha_vencimiento: str = ""
    moneda: str = "PEN"
    tipo_operacion: str = ""
    forma_pago: str = ""
    emisor: Party
    cliente: Party
    totales: dict[str, str] = Field(default_factory=dict)   # código cat. 14 -> monto
    tributos: list[Tax] = Field(default_factory=list)
    importe_total: str = ""
    total_impuestos: str = ""
    leyendas: list[tuple[str, str]] = Field(default_factory=list)
    items: list[Item] = Field(default_factory=list)
    referencia: Reference | None = None
    raw: dict[str, dict[str, str]] = Field(default_factory=dict)
    encoding: str = "utf-8"
    advertencias: list[str] = Field(default_factory=list)

    @property
    def serie(self) -> str:
        return self.serie_numero.split("-", 1)[0]

    @property
    def numero(self) -> str:
        return self.serie_numero.split("-", 1)[1] if "-" in self.serie_numero else ""

    @property
    def clave(self) -> str:
        """Identificador único: RUC-tipo-serie-número (= SenderDocumentId)."""
        return f"{self.ruc_emisor}-{self.tipo_comprobante}-{self.serie_numero}"

    @property
    def es_nota(self) -> bool:
        return self.tipo_comprobante in ("07", "08")
