"""Layout del TXT: posición -> nombre de campo, por tipo de registro.

La posición 0 es el tipo de registro (IDE, EMI, ...). Para ajustar el layout
cuando lleguen nuevos ejemplos basta con editar este diccionario: el parser y
el armado del UBL usan siempre los nombres, nunca las posiciones.

Campos con nombre `desconocido_N` aún no están confirmados. Se conservan sin
interpretar y, si traen valor, se registra una advertencia en el log para
poder identificarlos con ejemplos reales.
"""

from __future__ import annotations

LAYOUTS: dict[str, dict[int, str]] = {
    "IDE": {
        1: "serie_numero",
        2: "fecha_emision",
        3: "hora_emision",
        4: "tipo_comprobante",
        5: "moneda",
        6: "desconocido_6",          # vacío en boletas; ¿forma de pago u orden de compra?
        7: "fecha_vencimiento",
        8: "forma_pago",             # Contado | Credito (vacío en los TXT antiguos: contado)
        9: "monto_pendiente",        # al crédito: monto que queda por pagar
    },
    "EMI": {
        1: "tipo_documento",
        2: "numero_documento",
        3: "razon_social",
        4: "nombre_comercial",
        5: "ubigeo",
        6: "direccion",
        7: "urbanizacion",           # vacío en los ejemplos
        8: "provincia",
        9: "departamento",
        10: "distrito",
        11: "pais",
        12: "desconocido_12",        # ¿teléfono?
        13: "desconocido_13",        # ¿correo?
        14: "codigo_establecimiento",
    },
    "REC": {
        1: "tipo_documento",
        2: "numero_documento",
        3: "razon_social",
        4: "direccion",
        5: "provincia",
        6: "departamento",
        7: "distrito",
        8: "pais",
        9: "telefono",
        10: "correo",
    },
    "CAB": {
        # pares código|monto (catálogo 14): 1001 gravadas, 1002 inafectas,
        # 1003 exoneradas, 1004 gratuitas, 1000 exportación
        1: "total_cod_1", 2: "total_monto_1",
        3: "total_cod_2", 4: "total_monto_2",
        5: "total_cod_3", 6: "total_monto_3",
        7: "total_cod_4", 8: "total_monto_4",
        9: "total_cod_5", 10: "total_monto_5",
        11: "desconocido_11", 12: "desconocido_12", 13: "desconocido_13",
        14: "desconocido_14", 15: "desconocido_15",
        16: "tributos",              # [código|monto|código|monto...]
        17: "desconocido_17", 18: "desconocido_18", 19: "desconocido_19",
        20: "importe_total",
        21: "desconocido_21",
        22: "desconocido_22",        # 0.00 en boletas; ¿otros cargos / anticipos?
        23: "tipo_operacion",        # catálogo 51
        24: "leyendas",              # [código|texto...]
        25: "total_impuestos",
    },
    "DET": {
        1: "numero_item",
        2: "codigo",
        3: "descripcion",
        4: "cantidad",
        5: "unidad_medida",
        6: "valor_unitario",
        7: "precio_unitario",
        8: "tributo",                # [código|monto|afectación|base|porcentaje]
        9: "valor_venta",
        10: "desconocido_10", 11: "desconocido_11",
        12: "desconocido_12", 13: "desconocido_13",
        14: "total_impuestos",
    },
    "CUO": {                         # una línea por cuota (pago al crédito)
        1: "cuota",                  # Cuota001, Cuota002...
        2: "monto",
        3: "fecha_vencimiento",
    },
    "ADI": {                         # datos adicionales: @@codBienServDetr, @@montoDetr... o texto libre
        1: "clave",
        2: "valor",
    },
}

# Notas de crédito/débito: el sistema Java usa un layout más corto en IDE, CAB
# y DET, y agrega DRF (documento de referencia). Confirmado con TXT reales de
# BC01, BC06, BC07 y FC01; los registros que no aparecen aquí usan LAYOUTS.
_NOTE_LAYOUTS: dict[str, dict[int, str]] = {
    "IDE": {
        1: "serie_numero",
        2: "fecha_emision",
        3: "hora_emision",
        4: "moneda",                 # sin tipo de comprobante: se toma del nombre
    },
    "CAB": {
        1: "total_cod_1", 2: "total_monto_1",
        3: "total_cod_2", 4: "total_monto_2",
        5: "desconocido_5", 6: "desconocido_6",
        7: "tributos",               # [código|monto...]
        8: "desconocido_8",
        9: "importe_total",
        10: "leyendas",              # [código|texto...]
        11: "total_impuestos",
    },
    "DET": {
        1: "numero_item",
        2: "codigo",
        3: "descripcion",
        4: "cantidad",
        5: "unidad_medida",
        6: "valor_unitario",
        7: "precio_unitario",
        8: "tributo",                # [código|monto|afectación|base|porcentaje]
        9: "valor_venta",
        10: "desconocido_10", 11: "desconocido_11", 12: "desconocido_12",
        13: "total_impuestos",
    },
    "DRF": {
        1: "tipo_comprobante",       # 01 / 03 del comprobante afectado
        2: "serie_numero",           # ej. BT01-00087912
        3: "codigo_motivo",          # catálogo 09 (NC) o 10 (ND)
        4: "descripcion_motivo",
    },
}

LAYOUTS_BY_TYPE: dict[str, dict[str, dict[int, str]]] = {
    "01": LAYOUTS,
    "03": LAYOUTS,
    "07": {**LAYOUTS, **_NOTE_LAYOUTS},
    "08": {**LAYOUTS, **_NOTE_LAYOUTS},  # nota de débito: sin ejemplo real aún, se asume igual a la NC
}

# Registros que pueden repetirse (una línea por ítem, cuota o dato adicional).
REPEATABLE = {"DET", "CUO", "ADI"}

# Registros obligatorios por tipo de comprobante.
REQUIRED = {
    "01": ("IDE", "EMI", "REC", "CAB", "DET"),
    "03": ("IDE", "EMI", "REC", "CAB", "DET"),
    "07": ("IDE", "EMI", "REC", "CAB", "DET", "DRF"),
    "08": ("IDE", "EMI", "REC", "CAB", "DET", "DRF"),
}
