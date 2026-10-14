# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

import csv
import io
import zipfile
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from unittest import mock
from xml.etree import ElementTree

from django.http import HttpResponse, StreamingHttpResponse
from django.test import SimpleTestCase
from django.utils.translation import gettext_lazy

from fleetops.services import exports
from fleetops.services.exports import (
    Column,
    Table,
    cell_value,
    column_letter,
    csv_response,
    export_filename,
    export_response,
    sheet_name,
    xlsx_response,
)

BOM = chr(0xFEFF)
MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
NS = {"m": MAIN}
XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"

COLUMNS = [
    Column("name", "Main character"),
    Column("pap", "PAP", "decimal"),
    Column("fleets", "Fleets", "int"),
]


def member_table(rows, **kwargs):
    kwargs.setdefault("name", "Members")
    return Table(columns=list(kwargs.pop("columns", COLUMNS)), rows=rows, **kwargs)


def csv_bytes(response):
    return b"".join(response.streaming_content)


def csv_rows(response):
    text = csv_bytes(response).decode("utf-8-sig")
    return list(csv.reader(io.StringIO(text, newline="")))


def open_workbook(response):
    return zipfile.ZipFile(io.BytesIO(response.content))


def sheet_cells(archive, part="xl/worksheets/sheet1.xml"):
    """{ref: (value, type, style)} plus the row numbers in document order."""
    root = ElementTree.fromstring(archive.read(part))
    cells, row_numbers = {}, []
    for row in root.find("m:sheetData", NS):
        row_numbers.append(int(row.get("r")))
        for cell in row:
            if cell.get("t") == "inlineStr":
                value = "".join(t.text or "" for t in cell.iter(f"{{{MAIN}}}t"))
            else:
                value = cell.find("m:v", NS).text
            cells[cell.get("r")] = (value, cell.get("t"), cell.get("s"))
    return cells, row_numbers


def counting(rows, consumed):
    for row in rows:
        consumed.append(row)
        yield row


class ColumnAndTableTests(SimpleTestCase):
    def test_unknown_column_kind_is_rejected(self):
        with self.assertRaisesMessage(ValueError, "Unknown column kind 'money'"):
            Column("isk", "ISK", "money")

    def test_table_about_defaults_to_empty(self):
        self.assertEqual(Table("Members", COLUMNS, []).about, [])


class CellValueTests(SimpleTestCase):
    def test_none_is_empty(self):
        self.assertIsNone(cell_value(None))
        self.assertIsNone(cell_value(None, "bool"))

    def test_bools_are_yes_or_no(self):
        self.assertEqual(cell_value(True), "Yes")
        self.assertEqual(cell_value(False, "int"), "No")
        self.assertEqual(cell_value(1, "bool"), "Yes")
        self.assertEqual(cell_value(0, "bool"), "No")
        # Text already in a bool column is kept, not judged by truthiness.
        self.assertEqual(cell_value("No", "bool"), "No")

    def test_aware_datetimes_are_written_in_utc(self):
        china = timezone(timedelta(hours=8))
        self.assertEqual(cell_value(datetime(2026, 10, 1, 7, 30, tzinfo=china), "datetime"), "2026-09-30 23:30")

    def test_naive_datetimes_are_taken_as_utc(self):
        self.assertEqual(cell_value(datetime(2026, 10, 1, 7, 30, 59)), "2026-10-01 07:30")

    def test_dates_are_iso(self):
        self.assertEqual(cell_value(date(2026, 2, 3), "datetime"), "2026-02-03")

    def test_numbers_are_kept(self):
        self.assertEqual(cell_value(12, "int"), 12)
        self.assertEqual(cell_value(Decimal("1.50"), "decimal"), Decimal("1.50"))
        self.assertEqual(cell_value(0.1, "decimal"), Decimal("0.1"))
        self.assertEqual(cell_value(-5), -5)

    def test_non_finite_numbers_become_text(self):
        self.assertEqual(cell_value(Decimal("NaN")), "NaN")
        self.assertEqual(cell_value(float("inf")), "inf")

    def test_plain_numeric_strings_in_numeric_columns_become_numbers(self):
        self.assertEqual(cell_value("12", "int"), Decimal("12"))
        self.assertEqual(cell_value(" -3.5 ", "decimal"), Decimal("-3.5"))
        self.assertEqual(cell_value("12", "text"), "12")
        for value in ("1e400000", "12 PAP", "=1+1", ""):
            with self.subTest(value=value):
                self.assertEqual(cell_value(value, "int"), value)

    def test_lazy_strings_are_resolved(self):
        self.assertEqual(cell_value(gettext_lazy("Fleets")), "Fleets")


class ExportFilenameTests(SimpleTestCase):
    def test_parts_are_joined_as_slugs(self):
        self.assertEqual(export_filename("members", "alliance", "2026-10"), "fleetops-members-alliance-2026-10")

    def test_non_ascii_and_empty_parts_are_dropped(self):
        self.assertEqual(
            export_filename("members", "Corp [ABC] 测试", None, "", "测试", 2026),
            "fleetops-members-corp-abc-2026",
        )

    def test_response_file_names_are_ascii_with_one_extension(self):
        table = member_table([])
        cases = [
            ("fleetops-members-2026-10", "fleetops-members-2026-10.csv"),
            ("report.CSV", "report.csv"),
            ("../evil name 测试.csv", "evil-name.csv"),
            ("Café", "Cafe.csv"),
            ("", "fleetops-export.csv"),
        ]
        for filename, expected in cases:
            with self.subTest(filename=filename):
                response = csv_response(table, filename)
                self.assertEqual(response["Content-Disposition"], f'attachment; filename="{expected}"')
        response = xlsx_response(table, "fleetops-members-2026-10.xlsx")
        self.assertEqual(response["Content-Disposition"], 'attachment; filename="fleetops-members-2026-10.xlsx"')


class CsvExportTests(SimpleTestCase):
    def test_response_headers(self):
        response = csv_response(member_table([]), "fleetops-members-2026-10")

        self.assertIsInstance(response, StreamingHttpResponse)
        self.assertEqual(response["Content-Type"], "text/csv; charset=utf-8")
        self.assertEqual(response["Content-Disposition"], 'attachment; filename="fleetops-members-2026-10.csv"')
        self.assertEqual(response["Cache-Control"], "private, no-store")

    def test_bom_header_row_and_crlf(self):
        raw = csv_bytes(csv_response(member_table([["Alice", Decimal("2"), 1]]), "x"))

        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))
        self.assertEqual(raw[3:], b"Main character,PAP,Fleets\r\nAlice,2,1\r\n")

    def test_unicode_names_round_trip(self):
        names = ["测试飞行员", "Ünïcødé Pilot", "Пилот"]
        rows = csv_rows(csv_response(member_table([[name, 1, 1] for name in names]), "x"))

        self.assertEqual(rows[0], ["Main character", "PAP", "Fleets"])
        self.assertEqual([row[0] for row in rows[1:]], names)

    def test_formula_injection_guard_on_text_cells(self):
        dangerous = ["=1+1", "+x", "-x", "@x", "\tx", "\rx", chr(0xFF1D) + "1+1", '=HYPERLINK("http://a")']
        safe = ["Alice", "a=b", " =1", "x-ray"]
        rows = csv_rows(csv_response(member_table([[value, 1, 1] for value in dangerous + safe]), "x"))

        self.assertEqual([row[0] for row in rows[1 : len(dangerous) + 1]], [f"'{value}" for value in dangerous])
        self.assertEqual([row[0] for row in rows[len(dangerous) + 1 :]], safe)

    def test_guard_applies_to_text_in_numeric_columns(self):
        rows = csv_rows(csv_response(member_table([["Alice", "-1+cmd", "=1"]]), "x"))

        self.assertEqual(rows[1], ["Alice", "'-1+cmd", "'=1"])

    def test_numbers_are_unquoted_and_never_guarded(self):
        table = member_table([["Alice", Decimal("-1.50"), -5], ["Bob", Decimal("1E+3"), "-7"]], columns=COLUMNS)
        raw = csv_bytes(csv_response(table, "x")).decode("utf-8-sig")

        self.assertIn("\r\nAlice,-1.50,-5\r\n", raw)
        self.assertIn("\r\nBob,1000,-7\r\n", raw)
        self.assertNotIn('"', raw)

    def test_datetimes_bools_and_none(self):
        columns = [
            Column("start", "Fleet start (EVE)", "datetime"),
            Column("eligible", "Eligible", "bool"),
            Column("note", "Note"),
        ]
        moscow = timezone(timedelta(hours=3))
        rows = csv_rows(
            csv_response(
                member_table(
                    [[datetime(2026, 10, 12, 21, 5, tzinfo=moscow), True, None], [None, False, ""]],
                    columns=columns,
                ),
                "x",
            )
        )

        self.assertEqual(rows[1:], [["2026-10-12 18:05", "Yes", ""], ["", "No", ""]])

    def test_mapping_rows_use_column_keys(self):
        rows = csv_rows(csv_response(member_table([{"fleets": 3, "name": "Alice", "ignored": "x"}]), "x"))

        self.assertEqual(rows[1], ["Alice", "", "3"])

    def test_rows_of_other_lengths_are_written_as_given(self):
        rows = csv_rows(csv_response(member_table([["Alice"], ["Bob", 1, 2, "=extra"]]), "x"))

        self.assertEqual(rows[1:], [["Alice"], ["Bob", "1", "2", "'=extra"]])

    def test_lazy_column_titles(self):
        table = member_table([], columns=[Column("pap", gettext_lazy("PAP"), "int")])

        self.assertEqual(csv_rows(csv_response(table, "x")), [["PAP"]])

    def test_invalid_characters_are_removed(self):
        raw = csv_bytes(csv_response(member_table([["a\x00b\x1f", 1, 1], ["x" + chr(0xD800), 1, 1]]), "x"))
        rows = list(csv.reader(io.StringIO(raw.decode("utf-8-sig"), newline="")))

        self.assertEqual([row[0] for row in rows[1:]], ["ab", "x"])

    def test_rows_are_read_while_streaming(self):
        consumed = []
        response = csv_response(member_table(counting([["Alice", 1, 1], ["Bob", 2, 2]], consumed)), "x")

        self.assertEqual(consumed, [])
        self.assertEqual(len(csv_rows(response)), 3)
        self.assertEqual(len(consumed), 2)

    def test_large_exports_are_streamed_in_chunks(self):
        response = csv_response(member_table([[f"Pilot {n}", n, n] for n in range(1200)]), "x")
        chunks = list(response.streaming_content)

        self.assertGreater(len(chunks), 1)
        text = b"".join(chunks).decode("utf-8-sig")
        self.assertEqual(text.count("\r\n"), 1201)
        self.assertTrue(text.endswith("Pilot 1199,1199,1199\r\n"))

    @mock.patch.object(exports, "MAX_EXPORT_ROWS", 5)
    def test_truncation_note_above_the_limit(self):
        consumed = []
        source = counting(([f"Pilot {n}", n, n] for n in range(100)), consumed)
        rows = csv_rows(csv_response(member_table(source), "x"))

        self.assertEqual(len(rows), 7)
        self.assertEqual([row[0] for row in rows[1:6]], [f"Pilot {n}" for n in range(5)])
        self.assertEqual(rows[6], ["Export truncated at 5 rows. Choose a shorter period."])
        # Reading stops at the first row past the limit.
        self.assertEqual(len(consumed), 6)

    @mock.patch.object(exports, "MAX_EXPORT_ROWS", 5)
    def test_no_note_at_exactly_the_limit(self):
        rows = csv_rows(csv_response(member_table([[f"Pilot {n}", n, n] for n in range(5)]), "x"))

        self.assertEqual(len(rows), 6)
        self.assertEqual(rows[-1], ["Pilot 4", "4", "4"])

    def test_default_limit(self):
        self.assertEqual(exports.MAX_EXPORT_ROWS, 200_000)
        self.assertEqual(exports.truncation_note(), "Export truncated at 200000 rows. Choose a shorter period.")


class XlsxExportTests(SimpleTestCase):
    def export(self, rows, **kwargs):
        return xlsx_response(member_table(rows, **kwargs), "fleetops-members-2026-10")

    def test_response_headers(self):
        response = self.export([])

        self.assertIsInstance(response, HttpResponse)
        self.assertNotIsInstance(response, StreamingHttpResponse)
        self.assertEqual(response["Content-Type"], exports.XLSX_CONTENT_TYPE)
        self.assertEqual(exports.XLSX_CONTENT_TYPE, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        self.assertEqual(response["Content-Disposition"], 'attachment; filename="fleetops-members-2026-10.xlsx"')
        self.assertEqual(response["Cache-Control"], "private, no-store")

    def test_archive_contains_the_required_parts(self):
        archive = open_workbook(self.export([["Alice", 1, 1]]))

        self.assertIsNone(archive.testzip())
        names = archive.namelist()
        self.assertEqual(names[0], "[Content_Types].xml")
        self.assertEqual(
            set(names),
            {
                "[Content_Types].xml",
                "_rels/.rels",
                "xl/workbook.xml",
                "xl/_rels/workbook.xml.rels",
                "xl/styles.xml",
                "xl/worksheets/sheet1.xml",
                "xl/worksheets/sheet2.xml",
            },
        )
        for name in names:
            with self.subTest(part=name):
                ElementTree.fromstring(archive.read(name))
                self.assertEqual(archive.getinfo(name).compress_type, zipfile.ZIP_DEFLATED)

    def test_content_types_and_relationships_resolve(self):
        archive = open_workbook(self.export([]))
        types = ElementTree.fromstring(archive.read("[Content_Types].xml"))
        overrides = {element.get("PartName").lstrip("/") for element in types if element.tag.endswith("Override")}
        self.assertEqual(
            overrides,
            {"xl/workbook.xml", "xl/styles.xml", "xl/worksheets/sheet1.xml", "xl/worksheets/sheet2.xml"},
        )

        root_rels = ElementTree.fromstring(archive.read("_rels/.rels"))
        self.assertEqual([rel.get("Target") for rel in root_rels], ["xl/workbook.xml"])

        workbook_rels = ElementTree.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
        targets = {rel.get("Id"): f"xl/{rel.get('Target')}" for rel in workbook_rels}
        self.assertTrue(set(targets.values()) <= set(archive.namelist()))

        workbook = ElementTree.fromstring(archive.read("xl/workbook.xml"))
        relationship_id = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
        sheets = [
            (sheet.get("name"), targets[sheet.get(relationship_id)]) for sheet in workbook.iter(f"{{{MAIN}}}sheet")
        ]
        self.assertEqual(sheets, [("Members", "xl/worksheets/sheet1.xml"), ("About", "xl/worksheets/sheet2.xml")])

    def test_worksheet_layout(self):
        archive = open_workbook(self.export([["Alice", 1, 1]]))
        root = ElementTree.fromstring(archive.read("xl/worksheets/sheet1.xml"))

        self.assertEqual([child.tag.split("}")[1] for child in root], ["sheetViews", "cols", "sheetData"])
        pane = root.find("m:sheetViews/m:sheetView/m:pane", NS)
        self.assertEqual(
            pane.attrib, {"ySplit": "1", "topLeftCell": "A2", "activePane": "bottomLeft", "state": "frozen"}
        )
        col = root.find("m:cols/m:col", NS)
        self.assertEqual((col.get("min"), col.get("max"), col.get("width")), ("1", "3", "18"))

    def test_header_row_uses_the_bold_style(self):
        archive = open_workbook(self.export([["Alice", 1, 1]]))
        cells, _rows = sheet_cells(archive)

        self.assertEqual(cells["A1"], ("Main character", "inlineStr", "1"))
        self.assertEqual(cells["C1"], ("Fleets", "inlineStr", "1"))
        self.assertIsNone(cells["A2"][2])

        styles = ElementTree.fromstring(archive.read("xl/styles.xml"))
        xfs = styles.findall("m:cellXfs/m:xf", NS)
        fonts = styles.findall("m:fonts/m:font", NS)
        fills = styles.findall("m:fills/m:fill/m:patternFill", NS)
        self.assertEqual(len(xfs), 2)
        self.assertIsNone(fonts[int(xfs[0].get("fontId"))].find("m:b", NS))
        self.assertIsNotNone(fonts[int(xfs[1].get("fontId"))].find("m:b", NS))
        self.assertEqual([fill.get("patternType") for fill in fills], ["none", "gray125"])
        self.assertEqual(len(styles.findall("m:borders/m:border", NS)), 1)

    def test_inline_strings_and_numeric_cells(self):
        archive = open_workbook(self.export([["测试飞行员", Decimal("12.50"), 3], ["Bob", Decimal("1E+3"), "4"]]))
        cells, rows = sheet_cells(archive)

        self.assertEqual(rows, [1, 2, 3])
        self.assertEqual(cells["A2"], ("测试飞行员", "inlineStr", None))
        self.assertEqual(cells["B2"], ("12.50", None, None))
        self.assertEqual(cells["C2"], ("3", None, None))
        self.assertEqual(cells["B3"], ("1000", None, None))
        self.assertEqual(cells["C3"], ("4", None, None))

    def test_formula_like_text_stays_text(self):
        archive = open_workbook(self.export([["=1+1", 1, 1], ["-x", 1, 1]]))
        cells, _rows = sheet_cells(archive)

        self.assertEqual(cells["A2"], ("=1+1", "inlineStr", None))
        self.assertEqual(cells["A3"], ("-x", "inlineStr", None))
        root = ElementTree.fromstring(archive.read("xl/worksheets/sheet1.xml"))
        self.assertEqual(root.findall(".//m:f", NS), [])

    def test_cell_references_past_column_z(self):
        columns = [Column(f"c{n}", f"Column {n}", "int") for n in range(28)]
        archive = open_workbook(self.export([list(range(28))], columns=columns))
        cells, _rows = sheet_cells(archive)

        self.assertEqual(cells["Z2"][0], "25")
        self.assertEqual(cells["AA2"][0], "26")
        self.assertEqual(cells["AB1"][0], "Column 27")

    def test_column_letters(self):
        cases = {0: "A", 25: "Z", 26: "AA", 51: "AZ", 52: "BA", 701: "ZZ", 702: "AAA", 16383: "XFD"}
        for index, letters in cases.items():
            with self.subTest(index=index):
                self.assertEqual(column_letter(index), letters)

    def test_empty_cells_are_omitted(self):
        archive = open_workbook(self.export([["Alice", None, ""]]))
        cells, rows = sheet_cells(archive)

        self.assertEqual(rows, [1, 2])
        self.assertIn("A2", cells)
        self.assertNotIn("B2", cells)
        self.assertNotIn("C2", cells)

    def test_datetimes_and_bools(self):
        columns = [Column("start", "Fleet start (EVE)", "datetime"), Column("waived", "Waived", "bool")]
        archive = open_workbook(
            self.export([[datetime(2026, 1, 1, 0, 30, tzinfo=timezone(timedelta(hours=1))), False]], columns=columns)
        )
        cells, _rows = sheet_cells(archive)

        self.assertEqual(cells["A2"], ("2025-12-31 23:30", "inlineStr", None))
        self.assertEqual(cells["B2"], ("No", "inlineStr", None))

    def test_invalid_xml_characters_are_stripped(self):
        value = "a\x00b\x08c\x0bd\x0ce\x1ff" + chr(0xFFFE) + chr(0xFFFF) + chr(0xDC00) + "\tg\nh"
        archive = open_workbook(self.export([[value, 1, 1], ["\x01\x02", 1, 1]]))
        cells, _rows = sheet_cells(archive)

        self.assertEqual(cells["A2"][0], "abcdef\tg\nh")
        self.assertNotIn("A3", cells)

    def test_carriage_returns_become_line_feeds(self):
        archive = open_workbook(self.export([["one\r\ntwo\rthree", 1, 1]]))
        cells, _rows = sheet_cells(archive)

        self.assertEqual(cells["A2"][0], "one\ntwo\nthree")

    def test_surrounding_whitespace_is_preserved(self):
        archive = open_workbook(self.export([[" padded ", 1, 1], ["plain", 1, 1]]))
        root = ElementTree.fromstring(archive.read("xl/worksheets/sheet1.xml"))
        texts = {cell.get("r"): cell.find("m:is/m:t", NS) for cell in root.iter(f"{{{MAIN}}}c") if cell.get("t")}

        self.assertEqual(texts["A2"].text, " padded ")
        self.assertEqual(texts["A2"].get(XML_SPACE), "preserve")
        self.assertIsNone(texts["A3"].get(XML_SPACE))

    def test_excel_escape_sequences_stay_literal(self):
        archive = open_workbook(self.export([["name_x0041_end", 1, 1]]))
        cells, _rows = sheet_cells(archive)

        self.assertEqual(cells["A2"][0], "name_x005F_x0041_end")

    def test_long_text_is_cut_to_the_excel_limit(self):
        emoji = chr(0x1F600)
        archive = open_workbook(self.export([["a" * 40000, 1, 1], [emoji * 20000, 1, 1], ["b" * 32767, 1, 1]]))
        cells, _rows = sheet_cells(archive)

        self.assertEqual(cells["A2"][0], "a" * 32767)
        # Excel counts UTF-16 units, so a cut never splits a surrogate pair.
        self.assertEqual(cells["A3"][0], emoji * 16383)
        self.assertEqual(cells["A4"][0], "b" * 32767)

    def test_sheet_names(self):
        cases = {
            "Members": "Members",
            "x" * 40: "x" * 31,
            "Fleet [A]: 1/2*?\\": "Fleet A 1 2",
            "'quoted'": "quoted",
            "": "Export",
            None: "Export",
            "[]": "Export",
        }
        for name, expected in cases.items():
            with self.subTest(name=name):
                self.assertEqual(sheet_name(name), expected)

    def test_workbook_sheet_names_are_safe_and_unique(self):
        cases = {
            "About": "About data",
            "history": "history data",
            'R&D "ops" <1>': 'R&D "ops" <1>',
            "Statistics for a very long corporation name": "Statistics for a very long corp",
        }
        for name, expected in cases.items():
            with self.subTest(name=name):
                archive = open_workbook(self.export([], name=name))
                workbook = ElementTree.fromstring(archive.read("xl/workbook.xml"))
                names = [sheet.get("name") for sheet in workbook.iter(f"{{{MAIN}}}sheet")]
                self.assertEqual(names, [expected, "About"])
                self.assertLessEqual(len(names[0]), 31)

    def test_about_sheet(self):
        about = [
            ("Dataset", "Members"),
            ("Month (EVE)", "2026-10"),
            ("Exported at (EVE)", datetime(2026, 10, 12, 8, 15, tzinfo=timezone.utc)),
            ("Target", Decimal("4.50")),
            ("Filters", ""),
        ]
        archive = open_workbook(self.export([], about=about))
        cells, rows = sheet_cells(archive, "xl/worksheets/sheet2.xml")

        self.assertEqual(rows, [1, 2, 3, 4, 5])
        self.assertEqual(cells["A1"], ("Dataset", "inlineStr", "1"))
        self.assertEqual(cells["B2"], ("2026-10", "inlineStr", None))
        self.assertEqual(cells["B3"][0], "2026-10-12 08:15")
        self.assertEqual(cells["B4"], ("4.50", None, None))
        self.assertNotIn("B5", cells)
        root = ElementTree.fromstring(archive.read("xl/worksheets/sheet2.xml"))
        self.assertIsNone(root.find("m:sheetViews/m:sheetView/m:pane", NS))

    def test_about_sheet_falls_back_to_the_dataset_name(self):
        archive = open_workbook(self.export([], name="Members"))
        cells, _rows = sheet_cells(archive, "xl/worksheets/sheet2.xml")

        self.assertEqual(cells["A1"][0], "Dataset")
        self.assertEqual(cells["B1"][0], "Members")

    def test_mapping_rows(self):
        archive = open_workbook(self.export([{"name": "Alice", "fleets": 2}]))
        cells, _rows = sheet_cells(archive)

        self.assertEqual(cells["A2"][0], "Alice")
        self.assertNotIn("B2", cells)
        self.assertEqual(cells["C2"][0], "2")

    def test_table_without_columns(self):
        archive = open_workbook(self.export([[]], columns=[]))
        root = ElementTree.fromstring(archive.read("xl/worksheets/sheet1.xml"))

        self.assertIsNone(root.find("m:cols", NS))
        self.assertEqual([row.get("r") for row in root.find("m:sheetData", NS)], ["1", "2"])

    @mock.patch.object(exports, "MAX_EXPORT_ROWS", 5)
    def test_truncation_note_above_the_limit(self):
        consumed = []
        archive = open_workbook(self.export(counting(([f"Pilot {n}", n, n] for n in range(50)), consumed)))
        cells, rows = sheet_cells(archive)

        self.assertEqual(rows, [1, 2, 3, 4, 5, 6, 7])
        self.assertEqual(cells["A6"][0], "Pilot 4")
        self.assertEqual(cells["A7"], ("Export truncated at 5 rows. Choose a shorter period.", "inlineStr", None))
        self.assertNotIn("B7", cells)
        self.assertEqual(len(consumed), 6)

    def test_large_sheet_is_written_in_chunks(self):
        archive = open_workbook(self.export([[f"Pilot {n}", n, n] for n in range(2500)]))
        cells, rows = sheet_cells(archive)

        self.assertEqual(len(rows), 2501)
        self.assertEqual(cells["A2501"][0], "Pilot 2499")


class ExportResponseTests(SimpleTestCase):
    def test_formats(self):
        self.assertEqual(exports.FORMATS, ("csv", "xlsx"))

    def test_dispatches_by_format(self):
        csv_export = export_response(member_table([["Alice", 1, 1]]), "csv", "fleetops-members-2026-10")
        xlsx_export = export_response(member_table([["Alice", 1, 1]]), "xlsx", "fleetops-members-2026-10")

        self.assertIsInstance(csv_export, StreamingHttpResponse)
        self.assertIn('filename="fleetops-members-2026-10.csv"', csv_export["Content-Disposition"])
        self.assertEqual(xlsx_export["Content-Type"], exports.XLSX_CONTENT_TYPE)
        self.assertIn('filename="fleetops-members-2026-10.xlsx"', xlsx_export["Content-Disposition"])
        self.assertTrue(zipfile.is_zipfile(io.BytesIO(xlsx_export.content)))

    def test_unknown_format_is_rejected(self):
        with self.assertRaisesMessage(ValueError, "Unsupported export format 'pdf'"):
            export_response(member_table([]), "pdf", "x")
