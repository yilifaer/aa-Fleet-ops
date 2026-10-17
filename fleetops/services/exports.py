# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""CSV and XLSX writers for statistics exports.

Both writers take a :class:`Table` and return a download response. The CSV is
streamed and starts with a UTF-8 BOM so Excel reads non-ASCII names; the XLSX
workbook is written with the standard library only (no spreadsheet package is
installed with Alliance Auth).
"""

from __future__ import annotations

import csv
import re
import unicodedata
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from io import BytesIO
from xml.sax.saxutils import escape
from zipfile import ZIP_DEFLATED, ZipFile

from django.http import HttpResponse, StreamingHttpResponse
from django.utils.http import content_disposition_header
from django.utils.text import slugify
from django.utils.translation import gettext, pgettext

UTC = timezone.utc
MAX_EXPORT_ROWS = 200_000

FORMATS = ("csv", "xlsx")
COLUMN_KINDS = ("text", "int", "decimal", "datetime", "bool")

CSV_CONTENT_TYPE = "text/csv; charset=utf-8"
XLSX_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

BOM = "\ufeff"
DATETIME_FORMAT = "%Y-%m-%d %H:%M"
MAX_CELL_CHARS = 32767
MAX_SHEET_NAME = 31

# Spreadsheet programs evaluate a cell starting with one of these as a formula.
# The full-width forms are included for East Asian versions of Excel and WPS.
FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r", "\uff1d", "\uff0b", "\uff0d", "\uff20")

_CSV_CHUNK_ROWS = 500
_XLSX_CHUNK_ROWS = 1000

# Characters XML 1.0 cannot carry. They are dropped from CSV cells as well, where
# a lone surrogate would also break the UTF-8 stream.
_INVALID_CHARS = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]")
# Excel decodes "_xHHHH_" in strings; "_x005F" escapes the underscore.
_EXCEL_ESCAPE = re.compile(r"_(x[0-9A-Fa-f]{4}_)")
_SHEET_NAME_ILLEGAL = re.compile(r"[\[\]:*?/\\]")
_FILENAME_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")
_PLAIN_NUMBER = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)")

_TRUNCATED = object()


@dataclass(slots=True)
class Column:
    key: str
    title: str
    kind: str = "text"

    def __post_init__(self):
        if self.kind not in COLUMN_KINDS:
            raise ValueError(f"Unknown column kind {self.kind!r} for {self.key!r}.")


@dataclass(slots=True)
class Table:
    """Rows are sequences in column order or mappings keyed by ``Column.key``.

    ``about`` holds (label, value) pairs for the XLSX "About" sheet. ``footer`` holds rows written after the
    last data row, and after the note of a cut export, so a total always covers every row of the file's scope
    (bold in XLSX).
    """

    name: str
    columns: list[Column]
    rows: Iterable[Sequence | Mapping]
    about: list[tuple[str, object]] = field(default_factory=list)
    footer: list[Sequence | Mapping] = field(default_factory=list)


def export_filename(dataset: str, *parts) -> str:
    """ASCII file name without extension, e.g. ``fleetops-members-alliance-2026-10``."""
    slugs = (slugify(str(part)) for part in ("fleetops", dataset, *parts) if part not in (None, ""))
    return "-".join(slug for slug in slugs if slug)


def truncation_note() -> str:
    return gettext("Export truncated at %(rows)s rows. Choose a shorter period.") % {"rows": MAX_EXPORT_ROWS}


def export_response(table: Table, fmt: str, filename: str):
    if fmt == "csv":
        return csv_response(table, filename)
    if fmt == "xlsx":
        return xlsx_response(table, filename)
    raise ValueError(f"Unsupported export format {fmt!r}.")


def csv_response(table: Table, filename: str) -> StreamingHttpResponse:
    response = StreamingHttpResponse(_csv_chunks(table), content_type=CSV_CONTENT_TYPE)
    return _as_download(response, filename, "csv")


def xlsx_response(table: Table, filename: str) -> HttpResponse:
    response = HttpResponse(build_xlsx(table), content_type=XLSX_CONTENT_TYPE)
    return _as_download(response, filename, "xlsx")


def _as_download(response, filename, extension):
    response["Content-Disposition"] = content_disposition_header(True, _attachment_name(filename, extension))
    response["Cache-Control"] = "private, no-store"
    return response


def _attachment_name(filename, extension):
    stem = str(filename or "")
    if stem.lower().endswith(f".{extension}"):
        stem = stem[: -len(extension) - 1]
    stem = unicodedata.normalize("NFKD", stem).encode("ascii", "ignore").decode("ascii")
    stem = _FILENAME_UNSAFE.sub("-", stem).strip("-.") or "fleetops-export"
    return f"{stem}.{extension}"


# Cell values


def cell_value(value, kind: str = "text"):
    """Normalise a value to ``str`` (text), ``Decimal`` or ``int`` (numbers), or None (empty)."""
    if value is None:
        return None
    if isinstance(value, bool) or (kind == "bool" and not isinstance(value, str)):
        return pgettext("FleetOps", "Yes") if value else pgettext("FleetOps", "No")
    if isinstance(value, datetime):
        return format_datetime(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, int):
        return value
    if isinstance(value, Decimal):
        return value if value.is_finite() else str(value)
    if isinstance(value, float):
        number = Decimal(repr(value))
        return number if number.is_finite() else repr(value)
    text = str(value)
    if kind in ("int", "decimal") and _PLAIN_NUMBER.fullmatch(text.strip()):
        return Decimal(text.strip())
    return text


def format_datetime(value: datetime) -> str:
    """``YYYY-MM-DD HH:MM`` in UTC (EVE time); naive values are taken as UTC."""
    if value.tzinfo is not None:
        value = value.astimezone(UTC)
    return value.strftime(DATETIME_FORMAT)


def _number_text(value):
    return format(value, "f") if isinstance(value, Decimal) else str(value)


def _row_values(row, columns):
    if isinstance(row, Mapping):
        return [row.get(column.key) for column in columns]
    return row


def _row_cells(row, columns):
    count = len(columns)
    return [
        cell_value(value, columns[index].kind if index < count else "text")
        for index, value in enumerate(_row_values(row, columns))
    ]


def _bounded(rows):
    limit = MAX_EXPORT_ROWS
    for index, row in enumerate(rows):
        if index >= limit:
            yield _TRUNCATED
            return
        yield row


# CSV


class _Echo:
    def write(self, value):
        return value


def csv_text(value) -> str:
    """CSV cell text with a leading ``'`` on text that a spreadsheet would run as a formula."""
    if value is None:
        return ""
    if isinstance(value, (int, Decimal)):
        return _number_text(value)
    text = _INVALID_CHARS.sub("", value)
    if text.startswith(FORMULA_PREFIXES):
        return f"'{text}"
    return text


def _csv_chunks(table: Table) -> Iterator[str]:
    writer = csv.writer(_Echo(), dialect="excel")
    columns = table.columns
    lines = [BOM, writer.writerow([csv_text(cell_value(column.title)) for column in columns])]
    for row in _bounded(table.rows):
        if row is _TRUNCATED:
            lines.append(writer.writerow([truncation_note()]))
            break
        lines.append(writer.writerow([csv_text(cell) for cell in _row_cells(row, columns)]))
        if len(lines) >= _CSV_CHUNK_ROWS:
            yield "".join(lines)
            lines = []
    for row in table.footer:
        lines.append(writer.writerow([csv_text(cell) for cell in _row_cells(row, columns)]))
    if lines:
        yield "".join(lines)


# XLSX

_XML_DECLARATION = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
_MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PACKAGE_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_WORKSHEET_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"

_CONTENT_TYPES = (
    _XML_DECLARATION + '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/xl/workbook.xml" '
    'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
    f'<Override PartName="/xl/worksheets/sheet1.xml" ContentType="{_WORKSHEET_TYPE}"/>'
    f'<Override PartName="/xl/worksheets/sheet2.xml" ContentType="{_WORKSHEET_TYPE}"/>'
    '<Override PartName="/xl/styles.xml" '
    'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
    "</Types>"
)

_ROOT_RELS = (
    _XML_DECLARATION + f'<Relationships xmlns="{_PACKAGE_REL_NS}">'
    f'<Relationship Id="rId1" Type="{_REL_NS}/officeDocument" Target="xl/workbook.xml"/>'
    "</Relationships>"
)

_WORKBOOK_RELS = (
    _XML_DECLARATION + f'<Relationships xmlns="{_PACKAGE_REL_NS}">'
    f'<Relationship Id="rId1" Type="{_REL_NS}/worksheet" Target="worksheets/sheet1.xml"/>'
    f'<Relationship Id="rId2" Type="{_REL_NS}/worksheet" Target="worksheets/sheet2.xml"/>'
    f'<Relationship Id="rId3" Type="{_REL_NS}/styles" Target="styles.xml"/>'
    "</Relationships>"
)

# cellXfs: 0 = default, 1 = bold (header row and About labels).
_STYLES = (
    _XML_DECLARATION + f'<styleSheet xmlns="{_MAIN_NS}">'
    '<fonts count="2">'
    '<font><sz val="11"/><name val="Calibri"/><family val="2"/></font>'
    '<font><b/><sz val="11"/><name val="Calibri"/><family val="2"/></font>'
    "</fonts>"
    '<fills count="2"><fill><patternFill patternType="none"/></fill>'
    '<fill><patternFill patternType="gray125"/></fill></fills>'
    '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
    '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
    '<cellXfs count="2">'
    '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
    '<xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1"/>'
    "</cellXfs>"
    '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
    "</styleSheet>"
)

BOLD_STYLE = 1
COLUMN_WIDTH = 18
ABOUT_SHEET = "About"
# Excel refuses a data sheet called "History"; the About sheet is the second sheet, so its name is taken.
_RESERVED_SHEET_NAMES = {"about", "history"}
_ABOUT_WIDTHS = (28, 80)


def build_xlsx(table: Table) -> bytes:
    data_sheet = sheet_name(table.name)
    about_sheet = about_sheet_name()
    if data_sheet.casefold() in _RESERVED_SHEET_NAMES | {about_sheet.casefold()}:
        data_sheet = f"{data_sheet} data"
    buffer = BytesIO()
    with ZipFile(buffer, "w", ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", _CONTENT_TYPES)
        archive.writestr("_rels/.rels", _ROOT_RELS)
        archive.writestr("xl/workbook.xml", _workbook_xml(data_sheet, about_sheet))
        archive.writestr("xl/_rels/workbook.xml.rels", _WORKBOOK_RELS)
        archive.writestr("xl/styles.xml", _STYLES)
        with archive.open("xl/worksheets/sheet1.xml", "w", force_zip64=True) as stream:
            for chunk in _data_sheet_chunks(table):
                stream.write(chunk.encode("utf-8"))
        archive.writestr("xl/worksheets/sheet2.xml", "".join(_about_sheet_chunks(table, data_sheet)))
    return buffer.getvalue()


def sheet_name(name) -> str:
    """Excel sheet name: at most 31 characters, none of ``[]:*?/\\``, no edge apostrophes."""
    text = _SHEET_NAME_ILLEGAL.sub(" ", _INVALID_CHARS.sub("", str(name or "")))
    text = " ".join(text.split())[:MAX_SHEET_NAME].strip().strip("'").strip()
    return text or gettext("Export")


def about_sheet_name() -> str:
    """The name of the second sheet, in the language of whoever downloads the file."""
    return sheet_name(gettext("About"))


def column_letter(index: int) -> str:
    """Zero-based column index to an Excel column name (0 is A, 26 is AA)."""
    letters = ""
    index += 1
    while index:
        index, remainder = divmod(index - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


def xlsx_text(value: str) -> str:
    """Strip characters XML cannot carry and cut to Excel's cell limit (counted in UTF-16 units)."""
    text = _INVALID_CHARS.sub("", value)
    if len(text) > MAX_CELL_CHARS // 2:
        units = text.encode("utf-16-le")
        if len(units) > MAX_CELL_CHARS * 2:
            text = units[: MAX_CELL_CHARS * 2].decode("utf-16-le", "ignore")
    return text


def _workbook_xml(data_sheet, about_sheet=ABOUT_SHEET):
    return (
        _XML_DECLARATION + f'<workbook xmlns="{_MAIN_NS}" xmlns:r="{_REL_NS}">'
        '<bookViews><workbookView activeTab="0"/></bookViews>'
        "<sheets>"
        f'<sheet name="{_attr(data_sheet)}" sheetId="1" r:id="rId1"/>'
        f'<sheet name="{_attr(about_sheet)}" sheetId="2" r:id="rId2"/>'
        "</sheets></workbook>"
    )


def _attr(value):
    return escape(value, {'"': "&quot;"})


def _cell_xml(ref, value, style=None):
    """One cell. An empty value is a cell element without content, so every row keeps all its columns.

    Spreadsheet programs that skip a missing element (Quick Look, some viewers) would otherwise shift the
    cells of a row left, and a row without a rank would start in column A.
    """
    style_attr = f' s="{style}"' if style is not None else ""
    if value is None or value == "":
        return f'<c r="{ref}"{style_attr}/>'
    if not isinstance(value, str):
        return f'<c r="{ref}"{style_attr}><v>{_number_text(value)}</v></c>'
    text = _EXCEL_ESCAPE.sub(r"_x005F_\1", xlsx_text(value).replace("\r\n", "\n").replace("\r", "\n"))
    if not text:
        return f'<c r="{ref}"{style_attr}/>'
    space = ' xml:space="preserve"' if text != text.strip() else ""
    return f'<c r="{ref}"{style_attr} t="inlineStr"><is><t{space}>{escape(text)}</t></is></c>'


def _row_xml(number, cells, style=None):
    body = "".join(_cell_xml(f"{column_letter(index)}{number}", value, style) for index, value in enumerate(cells))
    return f'<row r="{number}">{body}</row>'


def _sheet_start(widths, frozen_header):
    parts = [_XML_DECLARATION, f'<worksheet xmlns="{_MAIN_NS}" xmlns:r="{_REL_NS}">']
    parts.append('<sheetViews><sheetView workbookViewId="0">')
    if frozen_header:
        parts.append('<pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/>')
        parts.append('<selection pane="bottomLeft" activeCell="A2" sqref="A2"/>')
    parts.append("</sheetView></sheetViews>")
    if widths:
        parts.append("<cols>")
        parts.extend(
            f'<col min="{first}" max="{last}" width="{width}" customWidth="1"/>' for first, last, width in widths
        )
        parts.append("</cols>")
    parts.append("<sheetData>")
    return "".join(parts)


_SHEET_END = "</sheetData></worksheet>"


def _data_sheet_chunks(table: Table) -> Iterator[str]:
    columns = table.columns
    widths = [(1, len(columns), COLUMN_WIDTH)] if columns else []
    header = [cell_value(column.title) for column in columns]
    lines = [_sheet_start(widths, frozen_header=True), _row_xml(1, header, BOLD_STYLE)]
    number = 1
    for row in _bounded(table.rows):
        number += 1
        if row is _TRUNCATED:
            lines.append(_row_xml(number, [truncation_note()]))
            break
        lines.append(_row_xml(number, _row_cells(row, columns)))
        if len(lines) >= _XLSX_CHUNK_ROWS:
            yield "".join(lines)
            lines = []
    for row in table.footer:
        number += 1
        lines.append(_row_xml(number, _row_cells(row, columns), BOLD_STYLE))
    lines.append(_SHEET_END)
    yield "".join(lines)


def _about_sheet_chunks(table: Table, data_sheet: str) -> Iterator[str]:
    widths = [(index + 1, index + 1, width) for index, width in enumerate(_ABOUT_WIDTHS)]
    yield _sheet_start(widths, frozen_header=False)
    about = list(table.about) or [(gettext("Dataset"), data_sheet)]
    for number, (label, value) in enumerate(about, start=1):
        yield (
            f'<row r="{number}">'
            f"{_cell_xml(f'A{number}', cell_value(label), BOLD_STYLE)}"
            f"{_cell_xml(f'B{number}', cell_value(value))}"
            "</row>"
        )
    yield _SHEET_END
