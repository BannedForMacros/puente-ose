"""TXT del sistema de facturación -> modelo Document."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

from sovos_bridge.parser.layout import LAYOUTS, REPEATABLE, REQUIRED
from sovos_bridge.parser.models import Document, Item, Party, Reference, Tax
from sovos_bridge.parser.tokenizer import decode_bytes, pairs, split_fields, split_group

log = logging.getLogger(__name__)

_FILE_CPE = re.compile(r"^(\d{11})-(\d{2})-([A-Z0-9]{4})-(\d{1,8})\.txt$", re.IGNORECASE)
_FILE_BAJA = re.compile(r"^(\d{11})-(RA|RC|RR)-(\d{8})-(\d{1,5})\.txt$", re.IGNORECASE)
_ZERO = re.compile(r"^0+(\.0+)?$")


class ParseError(Exception):
    """Error de lectura del TXT. El mensaje se muestra al personal de TI."""


@dataclass(frozen=True)
class FileInfo:
    ruc: str
    tipo: str            # 01/03/07/08 o RA/RC/RR (bajas y resúmenes)
    serie_numero: str    # BT05-00125304, o AAAAMMDD-N para bajas
    es_baja: bool = False

    @property
    def clave(self) -> str:
        return f"{self.ruc}-{self.tipo}-{self.serie_numero}"


def parse_filename(name: str) -> FileInfo:
    """Detecta tipo y serie-número por el nombre: RUC-tipo-serie-correlativo.txt"""
    m = _FILE_CPE.match(name)
    if m:
        ruc, tipo, serie, num = m.groups()
        return FileInfo(ruc, tipo, f"{serie.upper()}-{num}")
    m = _FILE_BAJA.match(name)
    if m:
        ruc, tipo, fecha, num = m.groups()
        return FileInfo(ruc, tipo.upper(), f"{fecha}-{num}", es_baja=True)
    raise ParseError(
        f"El nombre del archivo '{name}' no sigue el patrón RUC-tipo-serie-correlativo.txt "
        "(ejemplo: 20437310841-03-BT05-00125304.txt)"
    )


def _named(record: str, fields: list[str]) -> dict[str, str]:
    layout = LAYOUTS[record]
    out: dict[str, str] = {}
    for pos, value in enumerate(fields[1:], start=1):
        name = layout.get(pos, f"extra_{pos}")
        out[name] = value.strip()
    return out


def _warn_unknown(record: str, values: dict[str, str], warnings: list[str]) -> None:
    for name, value in values.items():
        if (name.startswith("desconocido_") or name.startswith("extra_")) and value and not _ZERO.match(value):
            pos = name.rsplit("_", 1)[1]
            warnings.append(f"{record} posición {pos} trae un valor no mapeado: '{value}'")


def read_records(text: str) -> dict[str, list[dict[str, str]]]:
    records: dict[str, list[dict[str, str]]] = {}
    for n, line in enumerate(text.splitlines(), start=1):
        line = line.rstrip("\r\n")
        if not line.strip():
            continue
        fields = split_fields(line)
        rtype = fields[0].strip().upper()
        if rtype not in LAYOUTS:
            log.warning("Línea %d: tipo de registro desconocido '%s', se ignora", n, rtype)
            continue
        if rtype in records and rtype not in REPEATABLE:
            raise ParseError(f"Línea {n}: el registro {rtype} aparece más de una vez")
        records.setdefault(rtype, []).append(_named(rtype, fields))
    return records


def _party(values: dict[str, str]) -> Party:
    return Party(**{k: v for k, v in values.items() if k in Party.model_fields})


def parse_text(text: str, info: FileInfo, encoding: str = "utf-8") -> Document:
    records = read_records(text)
    required = REQUIRED.get(info.tipo)
    if required is None:
        raise ParseError(f"Tipo de comprobante '{info.tipo}' no soportado")
    missing = [r for r in required if r not in records]
    if missing:
        raise ParseError(f"Faltan registros obligatorios en el TXT: {', '.join(missing)}")

    warnings: list[str] = []
    ide = records["IDE"][0]
    emi = records["EMI"][0]
    rec = records["REC"][0]
    cab = records["CAB"][0]
    for name in ("IDE", "EMI", "CAB"):
        _warn_unknown(name, records[name][0], warnings)

    totales: dict[str, str] = {}
    for i in range(1, 6):
        code, amount = cab.get(f"total_cod_{i}", ""), cab.get(f"total_monto_{i}", "")
        if code:
            totales[code] = amount

    tributos = [Tax(codigo=c, monto=m) for c, m in pairs(split_group(cab.get("tributos", "")))]
    leyendas = pairs(split_group(cab.get("leyendas", "")))

    items: list[Item] = []
    for det in records["DET"]:
        _warn_unknown("DET", det, warnings)
        t = split_group(det.get("tributo", ""))
        item_taxes = [
            Tax(codigo=t[i], monto=t[i + 1], afectacion=t[i + 2], base=t[i + 3], porcentaje=t[i + 4])
            for i in range(0, len(t) - 4, 5)
        ]
        items.append(Item(
            numero_item=det.get("numero_item", ""),
            codigo=det.get("codigo", ""),
            descripcion=det.get("descripcion", ""),
            cantidad=det.get("cantidad", ""),
            unidad_medida=det.get("unidad_medida", ""),
            valor_unitario=det.get("valor_unitario", ""),
            precio_unitario=det.get("precio_unitario", ""),
            tributos=item_taxes,
            valor_venta=det.get("valor_venta", ""),
            total_impuestos=det.get("total_impuestos", ""),
            raw=det,
        ))

    referencia = None
    if "REF" in records:
        ref = records["REF"][0]
        referencia = Reference(
            serie_numero=ref.get("serie_numero", ""),
            tipo_comprobante=ref.get("tipo_comprobante", ""),
            codigo_motivo=ref.get("codigo_motivo", ""),
            descripcion_motivo=ref.get("descripcion_motivo", ""),
        )

    doc = Document(
        ruc_emisor=emi.get("numero_documento", ""),
        tipo_comprobante=ide.get("tipo_comprobante", ""),
        serie_numero=ide.get("serie_numero", "").upper(),
        fecha_emision=ide.get("fecha_emision", ""),
        hora_emision=ide.get("hora_emision", ""),
        fecha_vencimiento=ide.get("fecha_vencimiento", ""),
        moneda=ide.get("moneda", "") or "PEN",
        tipo_operacion=cab.get("tipo_operacion", ""),
        emisor=_party(emi),
        cliente=_party(rec),
        totales=totales,
        tributos=tributos,
        importe_total=cab.get("importe_total", ""),
        total_impuestos=cab.get("total_impuestos", ""),
        leyendas=leyendas,
        items=items,
        referencia=referencia,
        raw={k: v[0] for k, v in records.items() if k not in REPEATABLE},
        encoding=encoding,
        advertencias=warnings,
    )
    return doc


def parse_file(path: Path, info: FileInfo | None = None) -> Document:
    """Lee el TXT. `info` permite indicar el nombre original si el archivo fue renombrado."""
    info = info or parse_filename(path.name)
    if info.es_baja:
        raise ParseError(
            "Comunicación de baja / resumen: el formato de envío a Sovos aún no está "
            "confirmado; este archivo no se procesa automáticamente"
        )
    text, encoding = decode_bytes(path.read_bytes())
    return parse_text(text, info, encoding)
