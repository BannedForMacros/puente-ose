"""Catálogos SUNAT usados por el armado del UBL y los controles."""

from __future__ import annotations

# Catálogo 01 - Tipo de comprobante
TIPO_COMPROBANTE = {
    "01": "Factura",
    "03": "Boleta de venta",
    "07": "Nota de crédito",
    "08": "Nota de débito",
}

# Catálogo 05 - Tributos: código -> (nombre, código internacional)
TRIBUTOS = {
    "1000": ("IGV", "VAT"),
    "1016": ("IVAP", "VAT"),
    "2000": ("ISC", "EXC"),
    "7152": ("ICBPER", "OTH"),
    "9995": ("EXP", "FRE"),
    "9996": ("GRA", "FRE"),
    "9997": ("EXO", "VAT"),
    "9998": ("INA", "FRE"),
    "9999": ("OTROS", "OTH"),
}

# Catálogo 14 (TXT) -> tributo del catálogo 05 cuya base informa
BASE_POR_TRIBUTO = {
    "1000": "1001",   # IGV       <- total gravadas
    "1016": "1001",   # IVAP      <- total gravadas
    "9998": "1002",   # inafectas
    "9997": "1003",   # exoneradas
    "9996": "1004",   # gratuitas
    "9995": "1000",   # exportación
}

# Catálogo 06 - Tipo de documento de identidad
TIPO_DOCUMENTO = {
    "0": "Sin documento / no domiciliado",
    "1": "DNI",
    "4": "Carné de extranjería",
    "6": "RUC",
    "7": "Pasaporte",
    "A": "Cédula diplomática",
    "B": "Doc. identidad país de residencia",
    "-": "Varios (boletas menores a S/ 700)",
}

# Catálogo 07 - Afectación del IGV
AFECTACION_IGV = {
    "10", "11", "12", "13", "14", "15", "16", "17",
    "20", "21", "30", "31", "32", "33", "34", "35", "36", "37", "40",
}
AFECTACION_GRATUITA = {"11", "12", "13", "14", "15", "16", "17", "21", "31", "32", "33", "34", "35", "36", "37"}

# Catálogo 03 - Unidades de medida (subconjunto habitual; ampliable)
UNIDADES = {
    "NIU", "ZZ", "KGM", "GRM", "LTR", "MLT", "MTR", "BX", "PK", "SET", "UND",
    "HUR", "DAY", "MON", "C62", "TNE", "BG", "BO", "CA", "DZN", "GLL", "PR",
}

# Catálogo 09 - Motivos de nota de crédito
MOTIVO_NC = {
    "01": "Anulación de la operación",
    "02": "Anulación por error en el RUC",
    "03": "Corrección por error en la descripción",
    "04": "Descuento global",
    "05": "Descuento por ítem",
    "06": "Devolución total",
    "07": "Devolución por ítem",
    "08": "Bonificación",
    "09": "Disminución en el valor",
    "10": "Otros conceptos",
    "11": "Ajustes de operaciones de exportación",
    "12": "Ajustes afectos al IVAP",
    "13": "Corrección del monto neto pendiente de pago y/o fechas de vencimiento",
}

# Catálogo 10 - Motivos de nota de débito
MOTIVO_ND = {
    "01": "Intereses por mora",
    "02": "Aumento en el valor",
    "03": "Penalidades / otros conceptos",
    "11": "Ajustes de operaciones de exportación",
    "12": "Ajustes afectos al IVAP",
}

# Catálogo 51 - Tipo de operación (los más comunes)
TIPO_OPERACION = {"0101", "0102", "0104", "0112", "0113", "0200", "0401", "1001", "1002", "1003", "1004", "2001"}
