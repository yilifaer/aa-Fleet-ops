# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Total rows of the SRP exports (FB13.3) and the footer rows of the writers they use."""

import csv
import io
import zipfile
from unittest import mock
from xml.etree import ElementTree

from django.test import SimpleTestCase, tag
from django.urls import reverse
from django.utils import translation

from fleetops.models import SrpClaim
from fleetops.services import exports
from fleetops.services.exports import Column, Table, csv_response, xlsx_response
from fleetops.srp import summaries

from . import factories as f
from . import test_srp_summaries as data
from .test_export_datasets import build, csv_rows, workbook_rows

M = data.M
PARAMS = {"year": 2026, "month": 3}
MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
COLUMNS = [Column("name", "Name"), Column("amount", "Amount", "int")]


def table(rows, footer=()):
    return Table("Totals", list(COLUMNS), rows, footer=list(footer))


class FooterWriterTests(SimpleTestCase):
    def test_a_table_has_no_footer_by_default(self):
        self.assertEqual(Table("Totals", COLUMNS, []).footer, [])

    def test_the_csv_writes_the_footer_after_the_rows(self):
        response = csv_response(table([["A", 1], ["B", 2]], [["Total", 3]]), "totals")
        rows = list(csv.reader(io.StringIO(b"".join(response.streaming_content).decode("utf-8-sig"))))

        self.assertEqual(rows, [["Name", "Amount"], ["A", "1"], ["B", "2"], ["Total", "3"]])

    def test_the_csv_footer_follows_the_note_of_a_cut_file(self):
        with mock.patch.object(exports, "MAX_EXPORT_ROWS", 2):
            response = csv_response(table([["A", 1], ["B", 2], ["C", 3]], [["Total", 6]]), "totals")
            rows = list(csv.reader(io.StringIO(b"".join(response.streaming_content).decode("utf-8-sig"))))
            note = exports.truncation_note()

        self.assertEqual(rows, [["Name", "Amount"], ["A", "1"], ["B", "2"], [note], ["Total", "6"]])

    def test_the_workbook_writes_the_footer_in_bold_after_the_rows(self):
        response = xlsx_response(table([["A", 1]], [["Total", 1], ["Count", 1]]), "totals")
        root = ElementTree.fromstring(zipfile.ZipFile(io.BytesIO(response.content)).read("xl/worksheets/sheet1.xml"))
        rows = root.find(f"{{{MAIN}}}sheetData")

        self.assertEqual([row.get("r") for row in rows], ["1", "2", "3", "4"])
        styles = [{cell.get("s") for cell in row} for row in rows]
        self.assertEqual(styles, [{"1"}, {None}, {"1"}, {"1"}])
        self.assertEqual(workbook_rows(response)[2:], [["Total", "1"], ["Count", "1"]])

    def test_the_workbook_footer_follows_the_note_of_a_cut_file(self):
        with mock.patch.object(exports, "MAX_EXPORT_ROWS", 1):
            response = xlsx_response(table([["A", 1], ["B", 2]], [["Total", 3]]), "totals")
            note = exports.truncation_note()

        self.assertEqual(workbook_rows(response), [["Name", "Amount"], ["A", "1"], [note], ["Total", "3"]])

    def test_a_footer_cell_that_is_empty_is_still_a_cell(self):
        response = xlsx_response(table([], [["Total", None]]), "totals")
        raw = zipfile.ZipFile(io.BytesIO(response.content)).read("xl/worksheets/sheet1.xml").decode()

        self.assertIn('<row r="2"><c r="A2" s="1" t="inlineStr">', raw)
        self.assertIn('<c r="B2" s="1"/></row>', raw)


class SrpTotalsTests(data.SummaryData):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.manager = f.create_user("tot_manager", perms=f.SRP_MANAGER_PERMS)

    def download(self, slug, fmt="csv", **params):
        self.client.force_login(self.manager)
        return self.client.get(reverse("fleetops:export", args=[slug]), {"format": fmt, **PARAMS, **params})

    def titled(self, table_, row):
        return dict(zip([column.title for column in table_.columns], row))

    def test_the_claims_file_ends_with_the_total_of_the_summary(self):
        table_, *_ = build("srp-claims", self.manager, **PARAMS)
        total = self.titled(table_, table_.footer[0])
        summary = summaries.alliance(2026, 3).totals

        self.assertEqual(len(table_.footer), 1)
        self.assertEqual(total["Claim"], "Total")
        self.assertEqual(total["Pilot (main)"], f"{summary.claims} claims")
        self.assertEqual(total["Suggested (ISK)"], summary.requested)
        self.assertEqual(total["Approved (ISK)"], summary.approved)
        # The loss values of the eight claims of the month (the withdrawn one is left out).
        self.assertEqual(total["Value (ISK)"], sum(claim.loss_value or 0 for claim in SrpClaim.objects.exclude(status="withdrawn")))
        for title in ("Loss time (EVE)", "Character", "Ship", "Status", "Within limit (ISK)", "Warnings"):
            self.assertIn(total[title], (None, ""), title)

    def test_the_total_is_the_last_row_of_both_files(self):
        csv_file = csv_rows(self.download("srp-claims"))
        workbook = workbook_rows(self.download("srp-claims", "xlsx"))
        summary = summaries.alliance(2026, 3).totals

        for rows in (csv_file, workbook):
            self.assertEqual(len(rows), 1 + summary.claims + 1)
            self.assertEqual(rows[-1][0], "Total")
            self.assertEqual(rows[-1][2], f"{summary.claims} claims")
            self.assertNotEqual(rows[-2][0], "Total")

    def test_a_cut_claims_file_still_ends_with_the_full_total(self):
        summary = summaries.alliance(2026, 3).totals

        with mock.patch.object(exports, "MAX_EXPORT_ROWS", 3):
            rows = csv_rows(self.download("srp-claims"))
            note = exports.truncation_note()

        self.assertEqual(rows[-2], [note])
        self.assertEqual(rows[-1][0], "Total")
        self.assertEqual(rows[-1][2], f"{summary.claims} claims")
        self.assertEqual(rows[-1][rows[0].index("Suggested (ISK)")], str(summary.requested))

    def test_the_total_follows_the_filters(self):
        table_, *_ = build("srp-claims", self.manager, status="paid", **PARAMS)
        total = self.titled(table_, table_.footer[0])
        paid = summaries.claims_totals(SrpClaim.objects.filter(status="paid", killmail_time__year=2026, killmail_time__month=3))

        self.assertEqual(total["Pilot (main)"], f"{paid.claims} claims")
        self.assertEqual(total["Suggested (ISK)"], paid.requested)
        self.assertEqual(total["Approved (ISK)"], paid.approved)

    def test_the_total_of_one_claim_is_singular(self):
        table_, *_ = build("srp-claims", self.manager, corporation=str(f.OTHER_CORP[0]), status="paid", **PARAMS)

        self.assertEqual(self.titled(table_, table_.footer[0])["Pilot (main)"], "1 claim")

    def test_the_total_of_a_corporation_is_that_corporations_summary(self):
        table_, *_ = build("srp-claims", self.manager, corporation=str(f.DEFAULT_CORP[0]), **PARAMS)
        total = self.titled(table_, table_.footer[0])
        row = summaries.corporation_row(f.DEFAULT_CORP[0], 2026, 3)

        self.assertEqual(total["Pilot (main)"], f"{row.totals.claims} claims")
        self.assertEqual(total["Suggested (ISK)"], row.totals.requested)
        self.assertEqual(total["Approved (ISK)"], row.totals.approved)

    def test_the_members_file_ends_with_the_total_of_the_summary(self):
        table_, *_ = build("srp-members", self.manager, **PARAMS)
        total = self.titled(table_, table_.footer[0])
        summary = summaries.alliance(2026, 3).totals

        self.assertEqual(total["Main character"], "Total")
        self.assertEqual(total["Corporation"], "")
        self.assertEqual(
            [total[title] for title in ("Claims", "Pending", "Approved claims", "Paid claims", "Rejected")],
            [summary.claims, summary.pending, summary.approved_count, summary.paid_count, summary.rejected],
        )
        self.assertEqual(
            [total[title] for title in ("Requested (ISK)", "Approved (ISK)", "Paid (ISK)", "Outstanding (ISK)")],
            [summary.requested, summary.approved, summary.paid, summary.outstanding],
        )

    def test_the_members_total_is_the_last_row_of_both_files(self):
        members = summaries.by_member(2026, 3)
        for rows in (csv_rows(self.download("srp-members")), workbook_rows(self.download("srp-members", "xlsx"))):
            self.assertEqual(len(rows), 1 + len(members) + 1)
            self.assertEqual(rows[-1][0], "Total")

    def test_an_empty_month_still_has_a_total_of_zero(self):
        rows = csv_rows(self.download("srp-members", year=2025, month=1))
        claims = csv_rows(self.download("srp-claims", year=2025, month=1))

        self.assertEqual(rows[-1][0], "Total")
        self.assertEqual(rows[-1][2], "0")
        self.assertEqual(claims[-1][0], "Total")
        self.assertEqual(claims[-1][2], "0 claims")


@tag("catalog")
class ChineseTotalsTests(data.SummaryData):
    """The words of the total row in the language of the downloader (needs the compiled catalog)."""

    def test_the_total_row_is_chinese(self):
        manager = f.create_user("zh_manager", perms=f.SRP_MANAGER_PERMS)
        self.client.force_login(manager)
        summary = summaries.alliance(2026, 3).totals

        with translation.override("zh-hans"):
            response = self.client.get(
                reverse("fleetops:export", args=["srp-claims"]), {"format": "csv", **PARAMS}, HTTP_ACCEPT_LANGUAGE="zh-hans"
            )
            rows = csv_rows(response)

        self.assertEqual(rows[-1][0], "合计")
        self.assertEqual(rows[-1][2], f"{summary.claims} 笔申请")
