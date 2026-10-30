"""Excel (.xlsx) mínimo sin dependencias: varias hojas, encabezado fijo y autofiltro.

Las fechas van como fechas de Excel y los importes como números, para que la
contadora pueda filtrar, ordenar y sumar directamente.
"""

from __future__ import annotations

import io
import re
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any
from xml.sax.saxutils import escape

# Estilos (índices en cellXfs): 0 normal, 1 encabezado, 2 fecha, 3 importe, 4 fecha y hora, 5 título
TEXT, HEADER, DATE, MONEY, DATETIME, TITLE = range(6)
_ILLEGAL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_EPOCH = date(1899, 12, 30)


@dataclass
class Column:
    header: str
    kind: int = TEXT          # TEXT | DATE | MONEY | DATETIME
    width: float = 14


@dataclass
class Sheet:
    name: str
    columns: list[Column]
    rows: list[list[Any]] = field(default_factory=list)
    autofilter: bool = True
    # Bloques libres debajo o en lugar de la tabla: lista de filas (texto/número) con estilo opcional
    extra: list[tuple[int, list[Any]]] = field(default_factory=list)


def workbook(sheets: list[Sheet]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", _content_types(len(sheets)))
        z.writestr("_rels/.rels", _RELS)
        z.writestr("xl/workbook.xml", _workbook(sheets))
        z.writestr("xl/_rels/workbook.xml.rels", _workbook_rels(len(sheets)))
        z.writestr("xl/styles.xml", _STYLES)
        for i, sh in enumerate(sheets, start=1):
            z.writestr(f"xl/worksheets/sheet{i}.xml", _sheet(sh))
    return buf.getvalue()


# --- partes del paquete ---------------------------------------------------------
_RELS = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
         '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
         '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
         'Target="xl/workbook.xml"/></Relationships>')

_STYLES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
    '<numFmts count="3"><numFmt numFmtId="164" formatCode="dd/mm/yyyy"/>'
    '<numFmt numFmtId="165" formatCode="#,##0.00"/><numFmt numFmtId="166" formatCode="dd/mm/yyyy hh:mm"/></numFmts>'
    '<fonts count="3"><font><sz val="11"/><name val="Calibri"/></font>'
    '<font><b/><sz val="11"/><color rgb="FFFFFFFF"/><name val="Calibri"/></font>'
    '<font><b/><sz val="13"/><name val="Calibri"/></font></fonts>'
    '<fills count="3"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill>'
    '<fill><patternFill patternType="solid"><fgColor rgb="FF0E3A47"/><bgColor indexed="64"/></patternFill></fill></fills>'
    '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
    '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
    '<cellXfs count="6">'
    '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
    '<xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1">'
    '<alignment vertical="center" wrapText="1"/></xf>'
    '<xf numFmtId="164" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>'
    '<xf numFmtId="165" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>'
    '<xf numFmtId="166" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>'
    '<xf numFmtId="0" fontId="2" fillId="0" borderId="0" xfId="0" applyFont="1"/>'
    '</cellXfs><cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>'
)


def _content_types(n: int) -> str:
    sheets = "".join(f'<Override PartName="/xl/worksheets/sheet{i}.xml" '
                     'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
                     for i in range(1, n + 1))
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            '<Override PartName="/xl/styles.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
            f'{sheets}</Types>')


def _workbook(sheets: list[Sheet]) -> str:
    items = "".join(f'<sheet name="{escape(_sheet_name(s.name))}" sheetId="{i}" r:id="rId{i}"/>'
                    for i, s in enumerate(sheets, start=1))
    names = "".join(
        f'<definedName name="_xlnm._FilterDatabase" localSheetId="{i}" hidden="1">'
        f"'{escape(_sheet_name(s.name))}'!$A$1:${_col(len(s.columns))}${len(s.rows) + 1}</definedName>"
        for i, s in enumerate(sheets) if s.autofilter and s.rows)
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            f'<sheets>{items}</sheets>' + (f"<definedNames>{names}</definedNames>" if names else "") + '</workbook>')


def _workbook_rels(n: int) -> str:
    rels = "".join(f'<Relationship Id="rId{i}" '
                   'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
                   f'Target="worksheets/sheet{i}.xml"/>' for i in range(1, n + 1))
    rels += (f'<Relationship Id="rId{n + 1}" '
             'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>')
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">{rels}</Relationships>')


def _sheet(sh: Sheet) -> str:
    out = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
           '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">']
    if sh.columns:
        out.append('<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" '
                   'state="frozen"/></sheetView></sheetViews>')
        out.append("<cols>" + "".join(f'<col min="{i}" max="{i}" width="{c.width}" customWidth="1"/>'
                                      for i, c in enumerate(sh.columns, start=1)) + "</cols>")
    out.append("<sheetData>")
    r = 0
    if sh.columns:
        r += 1
        out.append(f'<row r="{r}" ht="30" customHeight="1">' + "".join(
            _cell(r, i, c.header, HEADER) for i, c in enumerate(sh.columns, start=1)) + "</row>")
        for values in sh.rows:
            r += 1
            out.append(f'<row r="{r}">' + "".join(
                _cell(r, i, v, c.kind) for i, (c, v) in enumerate(zip(sh.columns, values), start=1)) + "</row>")
    for style, values in sh.extra:
        r += 1
        out.append(f'<row r="{r}">' + "".join(_cell(r, i, v, style if i == 1 else _kind_of(v))
                                             for i, v in enumerate(values, start=1)) + "</row>")
    out.append("</sheetData>")
    if sh.autofilter and sh.columns and sh.rows:
        out.append(f'<autoFilter ref="A1:{_col(len(sh.columns))}{len(sh.rows) + 1}"/>')
    out.append("</worksheet>")
    return "".join(out)


def _kind_of(v: Any) -> int:
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return MONEY if isinstance(v, float) else TEXT
    return TEXT


def _cell(r: int, c: int, value: Any, kind: int) -> str:
    ref = f"{_col(c)}{r}"
    if value is None or value == "":
        return ""
    if kind in (DATE, DATETIME):
        serial = _serial(value)
        if serial is not None:
            return f'<c r="{ref}" s="{kind}"><v>{serial}</v></c>'
        kind = TEXT
    if kind == MONEY or (isinstance(value, (int, float)) and not isinstance(value, bool)):
        try:
            return f'<c r="{ref}" s="{MONEY if kind == MONEY else 0}"><v>{float(value)}</v></c>'
        except (TypeError, ValueError):
            pass
    text = escape(_ILLEGAL.sub("", str(value)))
    return f'<c r="{ref}" t="inlineStr" s="{kind if kind in (HEADER, TITLE) else 0}"><is><t xml:space="preserve">{text}</t></is></c>'


def _serial(value: Any) -> float | None:
    """Fecha (AAAA-MM-DD) o fecha y hora ISO -> número de serie de Excel."""
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, date):
        return float((value - _EPOCH).days)
    else:
        s = str(value)
        try:
            if len(s) == 10:
                return float((date.fromisoformat(s) - _EPOCH).days)
            dt = datetime.fromisoformat(s)
        except ValueError:
            return None
    dt = dt.replace(tzinfo=None)
    return (dt.date() - _EPOCH).days + (dt.hour * 3600 + dt.minute * 60 + dt.second) / 86400


def _col(n: int) -> str:
    s = ""
    while n:
        n, rem = divmod(n - 1, 26)
        s = chr(65 + rem) + s
    return s


def _sheet_name(name: str) -> str:
    return re.sub(r"[\[\]:*?/\\]", " ", name)[:31]
