"""Controles del comprobante antes de armar el SBD.

Criterio (acordado al replicar el puente anterior): los montos ya vienen
calculados por el sistema de facturación y se envían tal cual. Por eso:
  - Bloquean el envío (VALIDACION_ERROR) solo los problemas que impiden armar
    un comprobante coherente: datos obligatorios vacíos, nombre del archivo
    que no coincide con el contenido, RUC emisor distinto, serie inválida o
    nota sin documento de referencia.
  - Las diferencias de cuadre y los códigos fuera de catálogo se registran
    como advertencias en el log, sin detener el envío.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal

from sovos_bridge.parser.models import Document, dec
from sovos_bridge.parser.txt_parser import FileInfo
from sovos_bridge.validation import catalogs as cat

_SERIE = re.compile(r"^[FB][A-Z0-9]{3}$")
_NUMERO = re.compile(r"^\d{1,8}$")
_FECHA = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_HORA = re.compile(r"^\d{2}:\d{2}:\d{2}$")

TOLERANCIA_ITEM = Decimal("0.01")
TOLERANCIA_TOTAL = Decimal("1.00")


@dataclass
class ValidationResult:
    errores: list[str] = field(default_factory=list)
    advertencias: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errores


def ruc_valido(ruc: str) -> bool:
    """RUC de 11 dígitos con dígito verificador (módulo 11)."""
    if not re.fullmatch(r"\d{11}", ruc or ""):
        return False
    pesos = (5, 4, 3, 2, 7, 6, 5, 4, 3, 2)
    resto = 11 - sum(int(d) * p for d, p in zip(ruc[:10], pesos)) % 11
    return int(ruc[10]) == {10: 0, 11: 1}.get(resto, resto)


def validate(doc: Document, info: FileInfo, emisor_ruc: str) -> ValidationResult:
    r = ValidationResult(advertencias=list(doc.advertencias))
    e, w = r.errores, r.advertencias

    # --- identidad del comprobante -------------------------------------
    if doc.tipo_comprobante not in cat.TIPO_COMPROBANTE:
        e.append(f"Tipo de comprobante '{doc.tipo_comprobante}' no soportado (se esperaba 01, 03, 07 u 08)")
    if doc.tipo_comprobante != info.tipo:
        e.append(f"El tipo del archivo ({info.tipo}) no coincide con el del contenido IDE ({doc.tipo_comprobante})")
    if doc.serie_numero != info.serie_numero:
        e.append(f"La serie-número del archivo ({info.serie_numero}) no coincide con la del contenido IDE ({doc.serie_numero})")
    if not _SERIE.match(doc.serie):
        e.append(f"Serie '{doc.serie}' inválida: debe tener 4 caracteres e iniciar con F o B")
    elif doc.tipo_comprobante == "01" and not doc.serie.startswith("F"):
        e.append(f"Una factura debe tener serie que inicie con F (llegó {doc.serie})")
    elif doc.tipo_comprobante == "03" and not doc.serie.startswith("B"):
        e.append(f"Una boleta debe tener serie que inicie con B (llegó {doc.serie})")
    if not _NUMERO.match(doc.numero):
        e.append(f"Correlativo '{doc.numero}' inválido: debe ser numérico de hasta 8 dígitos")

    # --- emisor ----------------------------------------------------------
    if doc.ruc_emisor != emisor_ruc or info.ruc != emisor_ruc:
        e.append(f"El RUC emisor del TXT ({doc.ruc_emisor}) no corresponde al configurado ({emisor_ruc})")

    # --- datos obligatorios ---------------------------------------------
    if not _FECHA.match(doc.fecha_emision):
        e.append(f"Fecha de emisión '{doc.fecha_emision}' inválida (formato AAAA-MM-DD)")
    if doc.hora_emision and not _HORA.match(doc.hora_emision):
        w.append(f"Hora de emisión '{doc.hora_emision}' con formato inesperado")
    if not doc.moneda:
        e.append("Falta la moneda (IDE posición 5)")
    if not doc.cliente.razon_social:
        e.append("Falta el nombre o razón social del cliente (REC posición 3)")
    if not doc.items:
        e.append("El comprobante no tiene ítems (registros DET)")
    if not doc.importe_total:
        e.append("Falta el importe total (CAB posición 20)")
    if not doc.tributos:
        e.append("Falta la lista de tributos (CAB posición 16)")

    # --- documento del cliente (solo advertencias) ----------------------
    tdoc, ndoc = doc.cliente.tipo_documento, doc.cliente.numero_documento
    if tdoc not in cat.TIPO_DOCUMENTO:
        w.append(f"Tipo de documento del cliente '{tdoc}' fuera del catálogo 06")
    elif tdoc == "1" and not re.fullmatch(r"\d{8}", ndoc):
        w.append("El DNI del cliente no tiene 8 dígitos")
    elif tdoc == "6" and not ruc_valido(ndoc):
        w.append("El RUC del cliente no es válido (11 dígitos con dígito verificador)")
    if doc.tipo_comprobante == "01" and tdoc != "6":
        w.append("Factura emitida a un cliente sin RUC")

    # --- notas -----------------------------------------------------------
    if doc.es_nota:
        ref = doc.referencia
        if ref is None or not ref.serie_numero or not ref.tipo_comprobante or not ref.codigo_motivo:
            e.append("La nota no trae el documento de referencia completo (serie-número, tipo y motivo)")
        else:
            motivos = cat.MOTIVO_NC if doc.tipo_comprobante == "07" else cat.MOTIVO_ND
            if ref.codigo_motivo not in motivos:
                w.append(f"Motivo '{ref.codigo_motivo}' fuera del catálogo {'09' if doc.tipo_comprobante == '07' else '10'}")
            if ref.serie_numero[:1] != doc.serie[:1]:
                w.append(f"La nota {doc.serie} referencia a {ref.serie_numero}: la letra de serie no coincide")

    # --- tributos y catálogos (advertencias) ------------------------------
    for t in doc.tributos:
        if t.codigo not in cat.TRIBUTOS:
            e.append(f"Tributo '{t.codigo}' fuera del catálogo 05")
    for it in doc.items:
        n = it.numero_item
        if it.unidad_medida not in cat.UNIDADES:
            w.append(f"Ítem {n}: unidad '{it.unidad_medida}' no está en la lista habitual del catálogo 03")
        if not it.tributos:
            e.append(f"Ítem {n}: falta el tributo del ítem (DET posición 8)")
        for t in it.tributos:
            if t.codigo not in cat.TRIBUTOS:
                e.append(f"Ítem {n}: tributo '{t.codigo}' fuera del catálogo 05")
            if t.afectacion not in cat.AFECTACION_IGV:
                w.append(f"Ítem {n}: afectación IGV '{t.afectacion}' fuera del catálogo 07")
            if t.codigo == "1000" and t.porcentaje:
                esperado = (dec(t.base) * dec(t.porcentaje) / 100).quantize(Decimal("0.01"))
                if abs(esperado - dec(t.monto)) > TOLERANCIA_ITEM:
                    w.append(f"Ítem {n}: IGV {t.monto} difiere de base x {t.porcentaje}% = {esperado}")

    # --- cuadres (advertencias; los montos se envían tal cual) ------------
    suma_venta = sum((dec(i.valor_venta) for i in doc.items), Decimal(0))
    suma_igv_items = sum((dec(i.total_impuestos) for i in doc.items), Decimal(0))
    valor_venta = sum((dec(doc.totales.get(c)) for c in ("1001", "1002", "1003", "1000")), Decimal(0))
    total_imp = dec(doc.total_impuestos)
    total = dec(doc.importe_total)
    checks = [
        ("suma del valor de venta de los ítems", suma_venta, "valor de venta de cabecera", valor_venta),
        ("suma de impuestos de los ítems", suma_igv_items, "total de impuestos", total_imp),
        ("valor de venta + impuestos", valor_venta + total_imp, "importe total", total),
    ]
    for a_name, a, b_name, b in checks:
        diff = abs(a - b)
        if diff > TOLERANCIA_TOTAL:
            w.append(f"Descuadre: {a_name} ({a}) vs {b_name} ({b}), diferencia {diff}")
        elif diff:
            w.append(f"Redondeo: {a_name} ({a}) vs {b_name} ({b}), diferencia {diff}")
    for it in doc.items:
        q = dec(it.cantidad)
        diff = abs(dec(it.precio_unitario) * q - (dec(it.valor_venta) + dec(it.total_impuestos)))
        if diff >= Decimal("0.005"):
            w.append(f"Redondeo ítem {it.numero_item}: precio con IGV x cantidad difiere en {diff.quantize(Decimal('0.0001'))}")

    return r
