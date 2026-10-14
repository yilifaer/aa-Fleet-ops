# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""What the statistics exports contain: the rows and columns of every dataset and the download around them."""

import calendar
import csv
import html
import io
import re
import zipfile
from datetime import datetime, timedelta, timezone as dt_timezone
from decimal import Decimal
from unittest import mock
from xml.etree import ElementTree

from django.db import connection
from django.test import RequestFactory, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from fleetops.models import AuditLog, FleetType, IncentivePeriod, MonthlyFCStatistic
from fleetops.services import export_datasets as datasets
from fleetops.services import exports
from fleetops.services import members as member_service
from fleetops.services import statistics as statistics_service
from fleetops.services.assessment import MonthTarget
from fleetops.services.export_datasets import export_for_request, get_dataset
from fleetops.services.periods import current_year_month, month_ref, shift_month
from fleetops.services.stats_types import (
    CorporationOverview,
    CorporationRow,
    FcRow,
    MemberFleetRow,
    MemberRow,
    MemberTable,
)

from . import factories as f

UTC = dt_timezone.utc
ALL_STATS = f.MEMBER_PERMS + ["fleetops.view_all_stats"]
INCENTIVES = f.MEMBER_PERMS + ["fleetops.manage_incentives"]
MAIN_NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}

# The services the pages use; the tests that compare a file with the page call them directly. The names are bound at
# import time, before any test patches the modules.
REAL_OVERVIEW = statistics_service.corporation_overview
REAL_FC_ROWS = statistics_service.fc_rows
REAL_MEMBER_ROWS = member_service.member_rows
REAL_MEMBER_FLEETS = member_service.member_fleets


def request_for(viewer, **params):
    request = RequestFactory().get("/export/", params)
    request.user = viewer
    return request


def build(slug, viewer, **params):
    """``(table, file name, scope)`` of a dataset for the viewer."""
    request = request_for(viewer, **params)
    dataset = get_dataset(slug)
    scope = dataset.check(request)
    table, filename = dataset.build(request, scope)
    return table, filename, scope


def column_titles(table):
    return [column.title for column in table.columns]


def about(table):
    return dict(table.about)


def csv_rows(response):
    text = b"".join(response.streaming_content).decode("utf-8-sig")
    return list(csv.reader(io.StringIO(text, newline="")))


def workbook_rows(response, part="xl/worksheets/sheet1.xml"):
    """Rows of a worksheet as lists of cell texts (empty cells as ``""``), by column letter position."""
    archive = zipfile.ZipFile(io.BytesIO(response.content))
    root = ElementTree.fromstring(archive.read(part))
    rows = []
    for row in root.find("m:sheetData", MAIN_NS):
        cells = {}
        for cell in row:
            letters = re.match(r"[A-Z]+", cell.get("r")).group(0)
            text = "".join(t.text or "" for t in cell.iter("{%s}t" % MAIN_NS["m"])) or (cell.findtext("m:v", "", MAIN_NS))
            cells[letters] = text
        width = max((_column_index(letters) for letters in cells), default=-1) + 1
        rows.append([cells.get(exports.column_letter(index), "") for index in range(width)])
    return rows


def _column_index(letters):
    index = 0
    for char in letters:
        index = index * 26 + ord(char) - 64
    return index - 1


def months_ago(count):
    return shift_month(*current_year_month(), -count)


def at(year_month, day=10, hour=12, minute=0):
    return datetime(year_month[0], year_month[1], day, hour, minute, tzinfo=UTC)


def member_row(**values):
    defaults = dict(
        user_id=1,
        main_character_id=90_000_001,
        main_character_name="Alice",
        corporation_id=f.DEFAULT_CORP[0],
        corporation_name=f.DEFAULT_CORP[1],
        corporation_ticker="TEST",
        alliance_id=f.DEFAULT_ALLIANCE[0],
        alliance_name=f.DEFAULT_ALLIANCE[1],
        registered_at=datetime(2025, 1, 2, 3, 4, tzinfo=UTC),
    )
    defaults.update(values)
    return MemberRow(**defaults)


def corporation_row(name, ticker, **values):
    defaults = dict(
        corporation_id=None,
        corporation_name=name,
        corporation_ticker=ticker,
        alliance_id=f.DEFAULT_ALLIANCE[0],
        alliance_name=f.DEFAULT_ALLIANCE[1],
        mains=0,
        mains_with_pap=0,
        mains_below_minimum=0,
        total_pap=0,
        fleets=0,
        by_fleet_type={},
        average=Decimal("0.00"),
        status="none",
    )
    defaults.update(values)
    return CorporationRow(**defaults)


class DatasetTestCase(TestCase):
    def setUp(self):
        self.lead = f.member("lead", perms=ALL_STATS + INCENTIVES, main_name="Lead 主管")
        self.stratop = f.fleet_type("StratOp", "1.00")
        self.roam = f.fleet_type("Roam", "0.50")
        self.month = months_ago(1)
        self.ref = month_ref(*self.month)


class CorporationsDatasetTests(DatasetTestCase):
    def overview(self, **values):
        alpha = corporation_row(
            "Alpha Corp",
            "ALPHA",
            corporation_id=1,
            mains=4,
            mains_with_pap=3,
            mains_below_minimum=1,
            total_pap=10,
            fleets=5,
            by_fleet_type={self.stratop.pk: 8, self.roam.pk: 2},
            average=Decimal("2.50"),
            status="met",
        )
        beta = corporation_row(
            "Beta 军团",
            "BETA",
            corporation_id=2,
            mains=2,
            mains_with_pap=1,
            mains_below_minimum=1,
            total_pap=1,
            fleets=1,
            by_fleet_type={self.stratop.pk: 1},
            average=Decimal("0.50"),
            status="missed",
        )
        other = corporation_row("Other corporations", "", total_pap=3, by_fleet_type={self.roam.pk: 3}, is_other=True)
        defaults = dict(
            month=self.ref,
            target=MonthTarget(Decimal("2.50"), 1, None),
            rows=[alpha, beta],
            other=other,
            fleet_types=[self.stratop, self.roam],
            sort="name",
            alliance_filter_configured=True,
            alliance_pap=14,
            member_pap=11,
            mains=6,
            mains_with_pap=4,
            average=Decimal("1.83"),
            corporations_met=1,
            corporations_with_target=2,
            not_counted={"excluded": 1, "pending": 2, "rejected": 0, "running": 0},
        )
        defaults.update(values)
        return CorporationOverview(**defaults)

    def build(self, overview, **params):
        year, month = self.month
        params.setdefault("year", year)
        params.setdefault("month", month)
        with mock.patch.object(statistics_service, "corporation_overview", create=True, return_value=overview) as service:
            table, filename, scope = build("corporations", self.lead, **params)
        return table, filename, scope, service

    def test_columns_and_rows(self):
        table, filename, scope, service = self.build(self.overview())

        self.assertEqual(
            column_titles(table),
            [
                "Corporation",
                "Ticker",
                "Alliance",
                "Mains",
                "Mains with PAP",
                "Mains below minimum",
                "Total PAP",
                "Average PAP per main",
                "Target",
                "Status",
                "StratOp",
                "Roam",
            ],
        )
        rows = list(table.rows)
        self.assertEqual(
            rows[0],
            ["Alpha Corp", "ALPHA", f.DEFAULT_ALLIANCE[1], 4, 3, 1, 10, Decimal("2.50"), Decimal("2.50"), "Target met", 8, 2],
        )
        self.assertEqual(
            rows[1],
            ["Beta 军团", "BETA", f.DEFAULT_ALLIANCE[1], 2, 1, 1, 1, Decimal("0.50"), Decimal("2.50"), "Below target", 1, 0],
        )
        self.assertEqual(len(rows), 4)
        self.assertEqual(filename, f"fleetops-corporations-alliance-{self.month[0]:04d}-{self.month[1]:02d}")
        service.assert_called_once_with(*self.month, sort="name")

    def test_other_corporations_row_and_alliance_total_make_the_columns_add_up(self):
        table, *_ = self.build(self.overview())

        rows = list(table.rows)
        self.assertEqual(rows[2], ["Other corporations", "", "", None, None, None, 3, None, None, "", 0, 3])
        total = rows[3]
        self.assertEqual(total[0], "Alliance total")
        self.assertEqual(total[3:7], [6, 4, 2, 14])
        self.assertEqual(total[7], Decimal("1.83"))
        self.assertEqual(total[8], Decimal("2.50"))
        self.assertEqual(total[10:], [9, 5])
        # The total row is the sum of the listed rows and the other corporations.
        self.assertEqual(total[6], sum(row[6] for row in rows[:3]))
        self.assertEqual(total[10], sum(row[10] for row in rows[:3]))
        self.assertEqual(total[11], sum(row[11] for row in rows[:3]))

    def test_without_other_corporations_there_is_no_such_row(self):
        table, *_ = self.build(self.overview(other=None, alliance_pap=11))

        self.assertEqual([row[0] for row in table.rows], ["Alpha Corp", "Beta 军团", "Alliance total"])

    def test_without_a_target_the_target_and_status_cells_are_empty(self):
        overview = self.overview(target=MonthTarget(None, 1, None), corporations_with_target=0)
        for row in overview.rows:
            row.status = "none"
        table, *_ = self.build(overview)

        rows = list(table.rows)
        self.assertIsNone(rows[0][8])
        self.assertEqual(rows[0][9], "")
        self.assertEqual(about(table)["Corporation average target (PAP per main)"], "No target")
        self.assertNotIn("Corporations meeting the target", about(table))

    def test_the_sort_is_passed_to_the_service(self):
        *_, service = self.build(self.overview(), sort="-average")

        service.assert_called_once_with(*self.month, sort="-average")

    def test_fleet_types_missing_from_the_overview_still_get_a_column(self):
        extra = f.fleet_type("Hauling", "0.10")
        overview = self.overview()
        overview.rows[0].by_fleet_type[extra.pk] = 5

        table, *_ = self.build(overview)

        self.assertEqual(column_titles(table)[-3:], ["StratOp", "Roam", "Hauling"])
        self.assertEqual(list(table.rows)[0][-3:], [8, 2, 5])

    def test_about_sheet(self):
        table, *_ = self.build(self.overview())

        info = about(table)
        self.assertEqual(info["Dataset"], "Corporations")
        self.assertEqual(info["Scope"], "Alliance")
        self.assertEqual(info["Month (EVE time)"], f"{self.month[0]:04d}-{self.month[1]:02d}")
        self.assertEqual(info["Exported by"], "Lead 主管")
        self.assertIsInstance(info["Exported at (EVE)"], datetime)
        self.assertEqual(info["Corporation average target (PAP per main)"], Decimal("2.50"))
        self.assertEqual(info["Member minimum PAP"], 1)
        self.assertEqual(info["Corporations meeting the target"], "1 of 2")
        self.assertEqual(info["Fleets not counted"], "1 excluded; 2 waiting for approval")
        self.assertIn("month the fleet started (EVE time)", info["Counting rule"])
        self.assertEqual(table.name, "Corporations")

    def test_a_month_without_pap_lists_the_corporations_and_the_total(self):
        table, *_ = build("corporations", self.lead, year=self.month[0], month=self.month[1])

        self.assertEqual(len(table.columns), 10)
        rows = list(table.rows)
        self.assertEqual(rows[-1][0], "Alliance total")
        self.assertEqual(rows[-1][6], 0)
        self.assertEqual(rows[0][0], f.DEFAULT_CORP[1])

    def test_the_file_is_a_download(self):
        year, month = self.month
        with mock.patch.object(statistics_service, "corporation_overview", create=True, return_value=self.overview()):
            response = export_for_request(request_for(self.lead, year=year, month=month), "corporations", "csv")

        rows = csv_rows(response)
        self.assertEqual(rows[0][:4], ["Corporation", "Ticker", "Alliance", "Mains"])
        self.assertEqual(rows[2][0], "Beta 军团")
        self.assertEqual(rows[2][7], "0.50")
        self.assertEqual(rows[1][8], "2.50")


class MembersDatasetTests(DatasetTestCase):
    def table(self, rows, **values):
        defaults = dict(
            rows=rows,
            month=self.ref,
            target=MonthTarget(Decimal("2.00"), 2, None),
            fleet_types=[self.stratop, self.roam],
            counts={"all": len(rows), "with_pap": len(rows), "inactive": 0},
            corporation_id=None,
        )
        defaults.update(values)
        return MemberTable(**defaults)

    def rows(self):
        return [
            member_row(
                user_id=1,
                main_character_name="测试飞行员",
                pap=5,
                fleets=3,
                characters=2,
                by_fleet_type={self.stratop.pk: 4, self.roam.pk: 1},
                rank=1,
                status="ok",
            ),
            member_row(user_id=2, main_character_name="Bob", pap=0, rank=None, status="below"),
        ]

    def build(self, table, viewer=None, **params):
        year, month = self.month
        params.setdefault("year", year)
        params.setdefault("month", month)

        def last_pap(rows):
            for row in rows:
                row.last_pap_at = datetime(2026, 1, 5, 20, 30, tzinfo=UTC) if row.pap else None

        with (
            mock.patch.object(member_service, "member_rows", return_value=table) as service,
            mock.patch.object(member_service, "fill_last_pap", side_effect=last_pap) as fill,
        ):
            built = build("members", viewer or self.lead, **params)
        return (*built, service, fill)

    def test_columns_and_rows(self):
        table, filename, scope, service, fill = self.build(self.table(self.rows()))

        self.assertEqual(
            column_titles(table),
            [
                "Rank",
                "Main character",
                "Corporation",
                "Ticker",
                "Alliance",
                "PAP",
                "Fleets",
                "Characters used",
                "Status",
                "Last PAP (EVE)",
                "Registered (EVE)",
                "StratOp",
                "Roam",
            ],
        )
        registered = datetime(2025, 1, 2, 3, 4, tzinfo=UTC)
        self.assertEqual(
            list(table.rows),
            [
                [1, "测试飞行员", f.DEFAULT_CORP[1], "TEST", f.DEFAULT_ALLIANCE[1], 5, 3, 2, "OK", datetime(2026, 1, 5, 20, 30, tzinfo=UTC), registered, 4, 1],
                [None, "Bob", f.DEFAULT_CORP[1], "TEST", f.DEFAULT_ALLIANCE[1], 0, 0, 0, "Below minimum", None, registered, 0, 0],
            ],
        )
        self.assertEqual(filename, f"fleetops-members-alliance-{self.month[0]:04d}-{self.month[1]:02d}")

    def test_the_filters_of_the_page_reach_the_service(self):
        *_, service, fill = self.build(self.table([]), status="inactive", q="  ali   ce ", sort="pap")

        # The ends are trimmed like on the page; the inner spaces stay, as the page searches for them.
        service.assert_called_once_with(*self.month, corporation_id=None, status="inactive", query="ali   ce", sort="pap")

    def test_a_long_search_text_is_cut_like_on_the_page_and_in_the_audit_entry(self):
        *_, scope, service, fill = self.build(self.table([]), q="x" * 3000)

        self.assertEqual(service.call_args.kwargs["query"], "x" * member_service.QUERY_LENGTH)
        self.assertEqual(scope.params["q"], "x" * 100)

    def test_defaults(self):
        *_, service, fill = self.build(self.table([]))

        service.assert_called_once_with(*self.month, corporation_id=None, status="all", query="", sort=None)

    def test_filters_the_page_would_ignore_are_ignored(self):
        *_, scope, service, fill = self.build(self.table([]), status="everything", sort="-banana")

        service.assert_called_once_with(*self.month, corporation_id=None, status="all", query="", sort=None)
        self.assertNotIn("status", scope.params)
        self.assertNotIn("sort", scope.params)

    def test_the_last_pap_of_every_member_is_filled_in(self):
        rows = self.rows()
        *_, service, fill = self.build(self.table(rows))

        fill.assert_called_once_with(rows)

    def test_a_corporation_manager_exports_the_own_corporation_with_the_corporation_parameter(self):
        manager = f.member("manager", perms=f.CORP_MANAGEMENT_PERMS)
        table, filename, scope, service, fill = self.build(
            self.table(self.rows(), corporation_id=f.DEFAULT_CORP[0]), viewer=manager, corporation=f.DEFAULT_CORP[0]
        )

        service.assert_called_once_with(
            *self.month, corporation_id=f.DEFAULT_CORP[0], status="all", query="", sort=None
        )
        self.assertEqual(about(table)["Scope"], f"Corporation {f.DEFAULT_CORP[1]}")
        self.assertIn(f"corporation-{f.DEFAULT_CORP[0]}", filename)

    def test_fleet_types_missing_from_the_table_get_a_column(self):
        extra = f.fleet_type("Hauling", "0.10")
        rows = self.rows()
        rows[0].by_fleet_type[extra.pk] = 7

        table, *_ = self.build(self.table(rows))

        self.assertEqual(column_titles(table)[-3:], ["StratOp", "Roam", "Hauling"])
        self.assertEqual(list(table.rows)[0][-3:], [4, 1, 7])
        self.assertEqual(list(table.rows)[1][-3:], [0, 0, 0])

    def test_about_sheet(self):
        table, *_ = self.build(self.table(self.rows()), status="inactive", q="bo", sort="pap")

        info = about(table)
        self.assertEqual(info["Dataset"], "Members")
        self.assertEqual(info["Scope"], "Alliance")
        self.assertEqual(info["Filters"], "Status: inactive; Name contains: bo; Sort: pap")
        self.assertEqual(info["Corporation average target (PAP per main)"], Decimal("2.00"))
        self.assertEqual(info["Member minimum PAP"], 2)

    def test_no_filters_means_no_filters_line(self):
        table, *_ = self.build(self.table(self.rows()))

        self.assertNotIn("Filters", about(table))

    def test_every_row_is_exported_not_a_page(self):
        rows = [member_row(user_id=n, main_character_name=f"Pilot {n}", pap=n) for n in range(1, 251)]
        table, *_ = self.build(self.table(rows))

        self.assertEqual(len(list(table.rows)), 250)

    def test_chinese_names_survive_both_formats(self):
        year, month = self.month
        for fmt in ("csv", "xlsx"):
            with self.subTest(fmt=fmt):
                with (
                    mock.patch.object(member_service, "member_rows", return_value=self.table(self.rows())),
                    mock.patch.object(member_service, "fill_last_pap"),
                ):
                    response = export_for_request(request_for(self.lead, year=year, month=month), "members", fmt)
                rows = csv_rows(response) if fmt == "csv" else workbook_rows(response)
                self.assertEqual(rows[1][1], "测试飞行员")
                self.assertEqual(rows[1][0], "1")
                self.assertEqual(rows[2][8], "Below minimum")


class MemberFleetsDatasetTests(DatasetTestCase):
    def test_columns_and_rows(self):
        target = f.member("target", main_name="目标 Pilot")
        fleets = [
            MemberFleetRow(
                operation_uuid="u-2",
                started_at=datetime(2026, 1, 8, 19, 0, tzinfo=UTC),
                ended_at=datetime(2026, 1, 8, 21, 0, tzinfo=UTC),
                fleet_type="StratOp",
                doctrine="Moa > Moa",
                fc_name="Alice",
                character_name="目标 Pilot",
                source="Automatic",
                pap=2,
            ),
            MemberFleetRow(
                operation_uuid="u-1",
                started_at=datetime(2026, 1, 2, 19, 0, tzinfo=UTC),
                ended_at=None,
                fleet_type="Roam",
                doctrine="",
                fc_name="Bob",
                character_name="Alt",
                source="PAP link",
                pap=1,
            ),
        ]
        year, month = self.month
        with mock.patch.object(member_service, "member_fleets", return_value=fleets) as service:
            table, filename, scope = build("member-fleets", self.lead, user=target.pk, year=year, month=month)

        service.assert_called_once_with(target, year, month, limit=exports.MAX_EXPORT_ROWS)
        self.assertEqual(
            column_titles(table),
            ["Fleet start (EVE)", "Fleet end (EVE)", "Fleet type", "Doctrine", "FC", "Character", "Source", "PAP"],
        )
        self.assertEqual(
            list(table.rows),
            [
                [fleets[0].started_at, fleets[0].ended_at, "StratOp", "Moa > Moa", "Alice", "目标 Pilot", "Automatic", 2],
                [fleets[1].started_at, None, "Roam", "", "Bob", "Alt", "PAP link", 1],
            ],
        )
        self.assertEqual(filename, f"fleetops-member-fleets-user-{target.pk}-{year:04d}-{month:02d}")
        self.assertEqual(about(table)["Scope"], "Member 目标 Pilot")

    def test_a_user_without_a_main_is_named_by_the_user_name(self):
        target = f.member("drifter", with_main=False)

        with mock.patch.object(member_service, "member_fleets", return_value=[]):
            with mock.patch.object(datasets.stats_access, "can_view_member", return_value=True):
                table, *_ = build("member-fleets", self.lead, user=target.pk)

        self.assertEqual(about(table)["Scope"], "Member drifter")


class FcsDatasetTests(DatasetTestCase):
    def rows(self):
        alice = f.member("alice", main_name="Alice")
        bob = f.member("bob", main_name="Bob 乙")
        return [
            FcRow(
                user=alice,
                main_name="Alice",
                fleets=3,
                points=Decimal("2.5"),
                breakdown={
                    "StratOp": {"count": 2, "points": Decimal("2")},
                    "Roam": {"count": 1, "points": 0.5},
                },
            ),
            FcRow(user=bob, main_name="Bob 乙", fleets=1, points=Decimal("1"), breakdown={"StratOp": {"count": 1, "points": 1.0}}),
        ]

    def build(self, rows, **params):
        year, month = self.month
        params.setdefault("year", year)
        params.setdefault("month", month)
        with mock.patch.object(statistics_service, "fc_rows", create=True, return_value=rows) as service:
            table, filename, scope = build("fcs", self.lead, **params)
        return table, filename, service

    def test_columns_and_rows(self):
        FleetType.objects.filter(pk=self.stratop.pk).update(sort_order=2)
        FleetType.objects.filter(pk=self.roam.pk).update(sort_order=1)

        table, filename, service = self.build(self.rows())

        service.assert_called_once_with(*self.month)
        # Fleet types in display order.
        self.assertEqual(
            column_titles(table),
            ["FC (main)", "Fleets", "FC points", "Roam fleets", "Roam points", "StratOp fleets", "StratOp points"],
        )
        self.assertEqual(
            list(table.rows),
            [
                ["Alice", 3, Decimal("2.50"), 1, Decimal("0.50"), 2, Decimal("2.00")],
                ["Bob 乙", 1, Decimal("1.00"), 0, Decimal("0.00"), 1, Decimal("1.00")],
            ],
        )
        self.assertEqual(filename, f"fleetops-fcs-alliance-{self.month[0]:04d}-{self.month[1]:02d}")

    def test_a_fleet_type_that_no_longer_exists_comes_last(self):
        rows = self.rows()
        rows[1].breakdown["Retired"] = {"count": 1, "points": Decimal("0.5")}

        table, *_ = self.build(rows)

        self.assertEqual(column_titles(table)[-2:], ["Retired fleets", "Retired points"])
        self.assertEqual(list(table.rows)[1][-2:], [1, Decimal("0.50")])
        self.assertEqual(list(table.rows)[0][-2:], [0, Decimal("0.00")])

    def test_no_fcs(self):
        table, *_ = self.build([])

        self.assertEqual(column_titles(table), ["FC (main)", "Fleets", "FC points"])
        self.assertEqual(list(table.rows), [])

    def test_a_month_without_fleets_has_the_columns_only(self):
        table, *_ = build("fcs", self.lead, year=self.month[0], month=self.month[1])

        self.assertEqual(len(table.columns), 3)
        self.assertEqual(list(table.rows), [])

    def test_about_sheet(self):
        table, *_ = self.build(self.rows())

        self.assertIn("FC points are the fleet type weight times the PAP multiplier", about(table)["Counting rule"])
        self.assertEqual(about(table)["Dataset"], "FCs")


class AttendanceDatasetTests(DatasetTestCase):
    def setUp(self):
        super().setUp()
        self.fc = f.member("fc", perms=f.FC_PERMS, main_name="FC 指挥")
        self.pilot = f.member("pilot", perms=f.CORP_MANAGEMENT_PERMS, main_name="测试飞行员")
        self.stranger = f.member("stranger", perms=f.MEMBER_PERMS, corporation=f.OTHER_CORP, main_name="Stranger")
        STR = f.fleet_type("Strat Ops")
        FleetType.objects.filter(pk=STR.pk).update(short_name="STR")
        self.strat = FleetType.objects.get(pk=STR.pk)
        self.old = f.closed_operation(
            self.fc, at(self.month, day=3, hour=20), type_obj=self.roam, doctrine_name="Kikis"
        )
        self.new = f.closed_operation(
            self.fc,
            at(self.month, day=9, hour=23, minute=30),
            type_obj=self.strat,
            doctrine_name="Moa > Booster 蛾",
        )
        self.alt = f.add_alt(self.pilot, "Pilot Alt")
        f.add_attendance(self.old, self.pilot, value=1)
        f.add_attendance(self.new, self.pilot, value=3)
        f.add_attendance(self.new, self.pilot, self.alt, value=2, source="click")
        f.add_attendance(self.new, self.stranger, value=4)

    def rows(self, viewer, **params):
        table, filename, scope = build("attendance", viewer, **params)
        return table, list(table.rows), filename

    def test_columns(self):
        table, *_ = self.rows(self.pilot)

        self.assertEqual(
            column_titles(table),
            [
                "Fleet start (EVE)",
                "Fleet end (EVE)",
                "Fleet type",
                "Doctrine",
                "FC",
                "Character",
                "Main character",
                "Corporation (at PAP time)",
                "Source",
                "PAP",
                "Fleet ID",
            ],
        )
        self.assertEqual([column.kind for column in table.columns][9], "int")

    def test_personal_rows(self):
        table, rows, filename = self.rows(self.pilot)

        self.assertEqual(filename, "fleetops-attendance-me-all")
        # Newest fleet first, then by character name.
        self.assertEqual(
            rows,
            [
                [
                    self.new.started_at,
                    self.new.ended_at,
                    "STR",
                    "Moa > Booster 蛾",
                    "FC 指挥",
                    "Pilot Alt",
                    "测试飞行员",
                    f.DEFAULT_CORP[1],
                    "PAP link",
                    2,
                    str(self.new.uuid),
                ],
                [
                    self.new.started_at,
                    self.new.ended_at,
                    "STR",
                    "Moa > Booster 蛾",
                    "FC 指挥",
                    "测试飞行员",
                    "测试飞行员",
                    f.DEFAULT_CORP[1],
                    "Automatic",
                    3,
                    str(self.new.uuid),
                ],
                [
                    self.old.started_at,
                    self.old.ended_at,
                    "Roam",
                    "Kikis",
                    "FC 指挥",
                    "测试飞行员",
                    "测试飞行员",
                    f.DEFAULT_CORP[1],
                    "Automatic",
                    1,
                    str(self.old.uuid),
                ],
            ],
        )
        self.assertEqual(about(table)["Scope"], "Personal PAP of 测试飞行员")
        self.assertEqual(about(table)["Month (EVE time)"], "All retained")

    def test_the_fleet_type_is_the_short_name_when_there_is_one(self):
        _table, rows, _filename = self.rows(self.pilot)

        self.assertEqual({row[2] for row in rows}, {"STR", "Roam"})

    def test_a_month(self):
        year, month = self.month
        other = months_ago(2)
        elsewhere = f.closed_operation(self.fc, at(other, day=5))
        f.add_attendance(elsewhere, self.pilot, value=7)

        table, rows, filename = self.rows(self.pilot, year=year, month=month)
        _table, all_rows, _ = self.rows(self.pilot)

        self.assertEqual(len(rows), 3)
        self.assertEqual(len(all_rows), 4)
        self.assertEqual(filename, f"fleetops-attendance-me-{year:04d}-{month:02d}")
        self.assertEqual(about(table)["Month (EVE time)"], f"{year:04d}-{month:02d}")

    def test_the_month_is_an_eve_month(self):
        late = f.closed_operation(
            self.fc, datetime(self.month[0], self.month[1], 1, 0, 0, tzinfo=UTC) - timedelta(minutes=1)
        )
        f.add_attendance(late, self.pilot)
        year, month = self.month

        with override_settings(TIME_ZONE="Asia/Shanghai"):
            _table, rows, _ = self.rows(self.pilot, year=year, month=month)

        self.assertNotIn(str(late.uuid), {row[10] for row in rows})

    def test_the_corporation_history_is_the_snapshot_corporation_of_the_manager(self):
        table, rows, filename = self.rows(self.pilot, scope="corporation")

        self.assertEqual(len(rows), 3)
        self.assertEqual({row[7] for row in rows}, {f.DEFAULT_CORP[1]})
        self.assertEqual(filename, "fleetops-attendance-corporation-all")
        self.assertEqual(about(table)["Scope"], f"Corporation {f.DEFAULT_CORP[1]} (at PAP time)")

    def test_the_corporation_is_the_one_at_pap_time(self):
        # The pilot moved: the old PAP stays with the corporation it was earned in.
        main = f.main_of(self.pilot)
        main.corporation_id, main.corporation_name = f.THIRD_CORP
        main.save()

        _table, rows, _ = self.rows(self.pilot, scope="corporation")
        self.assertEqual({row[7] for row in rows}, set())
        _table, rows, _ = self.rows(self.pilot)
        self.assertEqual({row[7] for row in rows}, {f.DEFAULT_CORP[1]})

    def test_the_alliance_history_is_everybody(self):
        table, rows, filename = self.rows(self.lead, scope="alliance")

        self.assertEqual(len(rows), 4)
        self.assertEqual(filename, "fleetops-attendance-alliance-all")
        self.assertEqual(about(table)["Scope"], "Alliance")
        self.assertEqual({row[7] for row in rows}, {f.DEFAULT_CORP[1], f.OTHER_CORP[1]})

    def test_fleets_that_do_not_count_are_left_out(self):
        pending = f.closed_operation(self.fc, at(self.month, day=11), review_status="pending")
        excluded = f.closed_operation(self.fc, at(self.month, day=12), counts_for_stats=False)
        running = f.create_operation(self.fc, started_at=at(self.month, day=13))
        for operation in (pending, excluded, running):
            f.add_attendance(operation, self.pilot)
        f.add_attendance(self.old, self.pilot, self.alt, value=5, granted=False)

        _table, rows, _ = self.rows(self.pilot)

        self.assertEqual({row[10] for row in rows}, {str(self.new.uuid), str(self.old.uuid)})
        self.assertEqual(len(rows), 3)

    def test_users_who_left_the_alliance_are_left_out(self):
        f.settings(history_alliance_ids=str(f.DEFAULT_ALLIANCE[0]))
        leaver = f.member("leaver", alliance=f.FOREIGN_ALLIANCE, main_name="Leaver")
        f.add_attendance(self.new, leaver, value=9)

        _table, rows, _ = self.rows(self.lead, scope="alliance")

        self.assertNotIn("Leaver", {row[6] for row in rows})

    def test_pap_older_than_the_retention_window_is_left_out(self):
        f.settings(data_retention_days=365)
        ancient = f.closed_operation(self.fc, at(months_ago(14), day=2))
        f.add_attendance(ancient, self.pilot)

        _table, rows, _ = self.rows(self.pilot)

        self.assertNotIn(str(ancient.uuid), {row[10] for row in rows})
        self.assertEqual(about(self.rows(self.pilot)[0])["History window"], "The last 365 days")

    def test_the_export_is_cut_one_row_past_the_limit(self):
        for day in range(11, 17):
            f.add_attendance(f.closed_operation(self.fc, at(self.month, day=day)), self.pilot)

        with mock.patch.object(exports, "MAX_EXPORT_ROWS", 3):
            table, *_ = build("attendance", self.pilot)
            with CaptureQueriesContext(connection) as queries:
                rows = list(table.rows)

        self.assertEqual(len(rows), 4)
        self.assertTrue(any("LIMIT 4" in query["sql"] for query in queries.captured_queries))

    def test_the_rows_are_read_while_the_file_is_written(self):
        table, *_ = build("attendance", self.pilot)

        self.assertFalse(isinstance(table.rows, (list, tuple)))
        self.assertEqual(len(list(table.rows)), 3)

    def test_csv_download_has_the_bom_chinese_names_and_eve_times(self):
        with override_settings(TIME_ZONE="Asia/Shanghai"):
            response = export_for_request(request_for(self.pilot), "attendance", "csv")
            raw = b"".join(response.streaming_content)

        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))
        rows = list(csv.reader(io.StringIO(raw.decode("utf-8-sig"), newline="")))
        self.assertEqual(rows[1][0], self.new.started_at.strftime("%Y-%m-%d %H:%M"))
        self.assertTrue(rows[1][0].endswith("23:30"))
        self.assertEqual(rows[2][5], "测试飞行员")
        self.assertEqual(rows[2][3], "Moa > Booster 蛾")
        self.assertEqual(rows[1][4], "FC 指挥")

    def test_names_that_look_like_formulas_stay_text(self):
        trickster = f.member("trickster", main_name="=HYPERLINK(\"http://example.invalid\")")
        f.add_attendance(self.new, trickster, value=1)
        viewer = self.lead

        csv_response = export_for_request(request_for(viewer, scope="alliance"), "attendance", "csv")
        names = {row[5] for row in csv_rows(csv_response)[1:]}
        self.assertIn("'=HYPERLINK(\"http://example.invalid\")", names)
        self.assertNotIn("=HYPERLINK(\"http://example.invalid\")", names)

        xlsx_response = export_for_request(request_for(viewer, scope="alliance"), "attendance", "xlsx")
        archive = zipfile.ZipFile(io.BytesIO(xlsx_response.content))
        sheet = archive.read("xl/worksheets/sheet1.xml").decode()
        self.assertNotIn("<f>", sheet)
        self.assertIn("=HYPERLINK(", sheet)

    def test_xlsx_download_has_the_data_and_an_about_sheet(self):
        response = export_for_request(request_for(self.pilot), "attendance", "xlsx")

        self.assertEqual(response["Content-Type"], exports.XLSX_CONTENT_TYPE)
        rows = workbook_rows(response)
        self.assertEqual(rows[0][0], "Fleet start (EVE)")
        self.assertEqual(rows[1][5], "Pilot Alt")
        self.assertEqual(rows[2][5], "测试飞行员")
        self.assertEqual(rows[1][9], "2")
        info = {row[0]: row[1] for row in workbook_rows(response, "xl/worksheets/sheet2.xml")}
        self.assertEqual(info["Dataset"], "PAP history")
        self.assertEqual(info["Exported by"], "测试飞行员")
        self.assertRegex(info["Exported at (EVE)"], r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$")
        self.assertIn("Counting rule", info)

    def test_query_count_does_not_grow_with_the_rows(self):
        def queries_for(viewer):
            with CaptureQueriesContext(connection) as captured:
                response = export_for_request(request_for(viewer, scope="alliance"), "attendance", "csv")
                csv_rows(response)
            return len(captured), len(list(build("attendance", viewer, scope="alliance")[0].rows))

        queries_for(self.lead)  # fills the permission and settings caches
        small, small_rows = queries_for(self.lead)
        # Every day after the 10th that the month has: February has no 29th or 30th.
        for day in range(11, calendar.monthrange(*self.month)[1] + 1):
            operation = f.closed_operation(self.fc, at(self.month, day=day, hour=day % 24))
            f.add_attendance(operation, self.pilot)
            f.add_attendance(operation, self.stranger)
        large, large_rows = queries_for(self.lead)

        self.assertGreater(large_rows, small_rows * 3)
        self.assertEqual(large, small)
        self.assertLessEqual(large, 12)


class IncentivesDatasetTests(DatasetTestCase):
    def setUp(self):
        super().setUp()
        f.settings(incentive_enabled=True)
        self.period = IncentivePeriod.objects.create(
            year=self.month[0], month=self.month[1], status=IncentivePeriod.Status.REVIEW, budget=500_000_000, minimum_fleets=2
        )
        self.alice = f.member("alice", main_name="Alice 甲")
        self.nomain = f.member("nomain", with_main=False)
        MonthlyFCStatistic.objects.create(
            period=self.period,
            fc_user=self.alice,
            fleet_count=4,
            total_points=Decimal("5.50"),
            eligible=True,
            waived=False,
            calculated_payout=400_000_000,
            final_payout=390_000_000,
        )
        MonthlyFCStatistic.objects.create(
            period=self.period,
            fc_user=self.nomain,
            fleet_count=1,
            total_points=Decimal("1.00"),
            eligible=False,
            waived=True,
            calculated_payout=0,
            final_payout=0,
        )

    def test_columns_rows_and_file_name(self):
        table, filename, scope = build("incentives", self.lead, period=self.period.pk)

        self.assertEqual(
            column_titles(table),
            ["FC", "Fleets", "FC points", "Eligible", "Waived", "Calculated payout", "Final payout", "Period status"],
        )
        self.assertEqual(
            list(table.rows),
            [
                ["Alice 甲", 4, Decimal("5.50"), True, False, 400_000_000, 390_000_000, "Review"],
                ["nomain", 1, Decimal("1.00"), False, True, 0, 0, "Review"],
            ],
        )
        self.assertEqual(filename, f"fleetops-incentives-alliance-{self.month[0]:04d}-{self.month[1]:02d}")

    def test_booleans_are_yes_or_no_in_the_file(self):
        response = export_for_request(request_for(self.lead, period=self.period.pk), "incentives", "csv")

        rows = csv_rows(response)
        self.assertEqual(rows[1][3:5], ["Yes", "No"])
        self.assertEqual(rows[2][3:5], ["No", "Yes"])
        self.assertEqual(rows[1][2], "5.50")

    def test_about_sheet(self):
        table, *_ = build("incentives", self.lead, period=self.period.pk)

        info = about(table)
        self.assertEqual(info["Budget (ISK)"], 500_000_000)
        self.assertEqual(info["Minimum fleets"], 2)
        self.assertEqual(info["Period status"], "Review")
        self.assertNotIn("Finalized at (EVE)", info)
        self.assertEqual(info["Month (EVE time)"], f"{self.month[0]:04d}-{self.month[1]:02d}")

    def test_a_finalized_period_names_the_time(self):
        finalized = datetime(2026, 2, 3, 4, 5, tzinfo=UTC)
        IncentivePeriod.objects.filter(pk=self.period.pk).update(
            status=IncentivePeriod.Status.FINALIZED, finalized_at=finalized
        )

        table, *_ = build("incentives", self.lead, period=self.period.pk)

        self.assertEqual(about(table)["Finalized at (EVE)"], finalized)
        self.assertEqual(list(table.rows)[0][-1], "Finalized")

    def test_query_count_does_not_grow_with_the_fcs(self):
        def queries():
            with CaptureQueriesContext(connection) as captured:
                table, *_ = build("incentives", self.lead, period=self.period.pk)
                rows = list(table.rows)
            return len(captured), len(rows)

        queries()  # fills the permission and content type caches
        small, small_rows = queries()
        for index in range(6):
            fc = f.member(f"fc{index}", main_name=f"FC {index}")
            MonthlyFCStatistic.objects.create(period=self.period, fc_user=fc, fleet_count=1, total_points=Decimal("1.00"))
        large, large_rows = queries()

        self.assertEqual((small_rows, large_rows), (2, 8))
        self.assertEqual(large, small)


class DownloadTests(DatasetTestCase):
    """The route, the headers and the audit entry of a download."""

    def setUp(self):
        super().setUp()
        self.pilot = f.member("pilot", perms=f.MEMBER_PERMS, main_name="测试飞行员")
        operation = f.closed_operation(f.member("fc", main_name="FC"), at(self.month))
        f.add_attendance(operation, self.pilot)

    def get(self, user, slug, **params):
        self.client.force_login(user)
        return self.client.get(reverse("fleetops:export", args=[slug]), params)

    def test_csv_headers(self):
        response = self.get(self.pilot, "attendance", format="csv", scope="me")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/csv; charset=utf-8")
        self.assertEqual(response["Cache-Control"], "private, no-store")
        self.assertEqual(response["Content-Disposition"], 'attachment; filename="fleetops-attendance-me-all.csv"')
        self.assertTrue(b"".join(response.streaming_content).startswith(b"\xef\xbb\xbf"))

    def test_xlsx_headers(self):
        response = self.get(self.pilot, "attendance", format="xlsx", scope="me")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], exports.XLSX_CONTENT_TYPE)
        self.assertEqual(response["Cache-Control"], "private, no-store")
        self.assertEqual(response["Content-Disposition"], 'attachment; filename="fleetops-attendance-me-all.xlsx"')
        self.assertTrue(zipfile.is_zipfile(io.BytesIO(response.content)))

    def test_file_names_are_ascii_whatever_the_names_are(self):
        manager = f.member("manager", perms=f.CORP_MANAGEMENT_PERMS, corporation=(2_000_099, "测试 Corp [甲]"))
        response = self.get(manager, "attendance", format="csv", scope="corporation")

        self.assertTrue(response["Content-Disposition"].isascii())
        self.assertIn("fleetops-attendance-corporation-all.csv", response["Content-Disposition"])

    def test_the_download_is_audited(self):
        year, month = self.month
        self.get(self.pilot, "attendance", format="csv", scope="me", year=year, month=month)

        entry = AuditLog.objects.get(action="statistics.export")
        self.assertEqual(entry.actor, self.pilot)
        self.assertEqual(entry.object_type, "Statistics")
        self.assertEqual(entry.object_id, f"attendance:me:{year:04d}-{month:02d}")
        self.assertEqual(entry.new_value, {"format": "csv", "params": {"scope": "me", "year": year, "month": month}})

    def test_a_download_the_user_may_not_have_is_not_audited(self):
        self.assertEqual(self.get(self.pilot, "attendance", format="csv", scope="alliance").status_code, 403)
        self.assertEqual(self.get(self.pilot, "corporations", format="csv").status_code, 403)
        self.assertEqual(self.get(self.pilot, "attendance", format="csv", scope="world").status_code, 404)
        self.assertFalse(AuditLog.objects.filter(action="statistics.export").exists())

    def test_the_audit_entry_never_holds_the_file_or_the_names(self):
        self.get(self.pilot, "attendance", format="csv", scope="me")

        entry = AuditLog.objects.get(action="statistics.export")
        self.assertNotIn("测试飞行员", str(entry.new_value))

    def test_unknown_datasets_and_formats(self):
        self.assertEqual(self.get(self.pilot, "afat", format="csv").status_code, 404)
        self.assertEqual(self.get(self.pilot, "attendance", format="pdf").status_code, 404)
        self.assertEqual(self.get(self.pilot, "attendance").status_code, 404)

    def test_only_get_is_allowed(self):
        self.client.force_login(self.pilot)
        url = reverse("fleetops:export", args=["attendance"])

        for method in ("post", "put", "patch", "delete"):
            with self.subTest(method=method):
                self.assertEqual(getattr(self.client, method)(url, {"format": "csv"}).status_code, 405)
        self.assertFalse(AuditLog.objects.filter(action="statistics.export").exists())

    def test_an_export_for_the_incentives_uses_the_period(self):
        f.settings(incentive_enabled=True)
        manager = f.member("manager", perms=INCENTIVES)
        period = IncentivePeriod.objects.create(year=self.month[0], month=self.month[1])
        MonthlyFCStatistic.objects.create(period=period, fc_user=manager, fleet_count=2, total_points=Decimal("2.00"))

        response = self.get(manager, "incentives", format="csv", period=period.pk)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(csv_rows(response)[1][1], "2")


class NoAfatComparisonTests(DatasetTestCase):
    """The statistics are not compared with AFAT: there is no dataset, route or page for it."""

    def test_there_is_no_afat_dataset(self):
        self.assertNotIn("afat", datasets.DATASETS)
        self.assertIsNone(get_dataset("afat"))

    def test_there_is_no_afat_route(self):
        from django.urls import NoReverseMatch

        with self.assertRaises(NoReverseMatch):
            reverse("fleetops:afat_comparison")
        self.client.force_login(self.lead)
        self.assertEqual(self.client.get("/fleetops/statistics/afat/").status_code, 404)
        self.assertEqual(self.client.get(reverse("fleetops:export", args=["afat"]), {"format": "csv"}).status_code, 404)

    def test_no_afat_module_is_shipped(self):
        import importlib.util

        try:
            spec = importlib.util.find_spec("fleetops.integrations.afat")
        except ModuleNotFoundError:
            # Not even the integrations package exists.
            spec = None
        self.assertIsNone(spec)


class ExportButtonPeriodTests(TestCase):
    def test_the_incentive_buttons_name_the_period(self):
        from django.template.loader import render_to_string

        page = render_to_string(
            "fleetops/statistics/_export_buttons.html",
            {"dataset": "incentives", "export_period": 7},
            request=RequestFactory().get("/fleetops/incentives/?year=2026&month=3"),
        )

        base = reverse("fleetops:export", args=["incentives"])
        self.assertIn(f'href="{base}?format=xlsx&amp;period=7">Excel</a>', page)
        self.assertIn(f'href="{base}?format=csv&amp;period=7">CSV</a>', page)


# Files that must hold what the pages show. They run against the services of the corporation, FC and member pages.


class MatchTestCase(TestCase):
    """Two corporations with three members, an FC and a lead in a third corporation, and a month with a target."""

    def setUp(self):
        self.lead = f.member("lead", perms=ALL_STATS + INCENTIVES, main_name="Lead", corporation=f.THIRD_CORP)
        self.fc = f.member("fc", perms=f.FC_PERMS, main_name="FC 指挥", corporation=f.THIRD_CORP)
        self.stratop = f.fleet_type("StratOp", "1.00")
        self.roam = f.fleet_type("Roam", "0.50")
        self.month = months_ago(1)
        self.earlier = months_ago(2)
        self.a1 = f.member("a1", main_name="Alpha 一")
        self.a2 = f.member("a2", main_name="Alpha 二")
        self.b1 = f.member("b1", main_name="Beta", corporation=f.OTHER_CORP)
        first = f.closed_operation(self.fc, at(self.month, day=4), type_obj=self.stratop)
        second = f.closed_operation(self.fc, at(self.month, day=18), type_obj=self.roam)
        f.add_attendance(first, self.a1, value=2)
        f.add_attendance(first, self.a2, value=1)
        f.add_attendance(first, self.b1, value=1)
        f.add_attendance(second, self.a1, value=1)
        f.statistics_target(*self.earlier, "1.50", minimum=2)
        self.first, self.second = first, second

    def download(self, slug, viewer=None, **params):
        year, month = self.month
        params.setdefault("year", year)
        params.setdefault("month", month)
        response = export_for_request(request_for(viewer or self.lead, **params), slug, "csv")
        return csv_rows(response)


class CorporationsFileMatchesPageTests(MatchTestCase):
    def test_file_holds_the_overview(self):
        overview = REAL_OVERVIEW(*self.month)
        rows = self.download("corporations")

        by_name = {row[0]: row for row in rows[1:]}
        self.assertEqual(rows[0][:10], [
            "Corporation", "Ticker", "Alliance", "Mains", "Mains with PAP", "Mains below minimum",
            "Total PAP", "Average PAP per main", "Target", "Status",
        ])
        for corporation in overview.rows:
            line = by_name[corporation.corporation_name]
            self.assertEqual(line[1], corporation.corporation_ticker)
            self.assertEqual(
                [int(value) for value in line[3:7]],
                [corporation.mains, corporation.mains_with_pap, corporation.mains_below_minimum, corporation.total_pap],
            )
            self.assertEqual(line[7], format(corporation.average, "f"))
        total = by_name["Alliance total"]
        self.assertEqual(int(total[6]), overview.alliance_pap)
        self.assertEqual(int(total[3]), overview.mains)
        self.assertEqual(total[7], format(overview.average, "f"))

    def test_numbers_follow_the_rules_of_the_statistics(self):
        rows = self.download("corporations")

        header = rows[0]
        by_name = {row[0]: row for row in rows[1:]}
        alpha, beta = by_name[f.DEFAULT_CORP[1]], by_name[f.OTHER_CORP[1]]
        # Alpha: 2 mains and 4 PAP; the target is 1.50 per main, so it is met. One of the two is below the minimum of 2.
        self.assertEqual([int(value) for value in alpha[3:7]], [2, 2, 1, 4])
        self.assertEqual((alpha[7], alpha[8], alpha[9]), ("2.00", "1.50", "Target met"))
        # Beta: 1 main and 1 PAP in a month that is over: below target.
        self.assertEqual([int(value) for value in beta[3:7]], [1, 1, 1, 1])
        self.assertEqual((beta[7], beta[9]), ("1.00", "Below target"))
        # The lead and the FC are mains of a third corporation without PAP.
        third = by_name[f.THIRD_CORP[1]]
        self.assertEqual([int(value) for value in third[3:7]], [2, 0, 2, 0])
        total = by_name["Alliance total"]
        self.assertEqual(int(total[3]), 5)
        self.assertEqual(int(total[6]), 5)
        self.assertEqual(int(total[header.index("StratOp")]), 4)
        self.assertEqual(int(total[header.index("Roam")]), 1)


class FcsFileMatchesPageTests(MatchTestCase):
    def test_file_holds_the_fc_rows(self):
        expected = REAL_FC_ROWS(*self.month)
        rows = self.download("fcs")

        self.assertEqual(len(rows) - 1, len(expected))
        for row, fc in zip(rows[1:], expected):
            self.assertEqual(row[0], fc.main_name)
            self.assertEqual(int(row[1]), fc.fleets)
            self.assertEqual(Decimal(row[2]), fc.points.quantize(Decimal("0.01")))

    def test_points_are_the_weights_times_the_multipliers(self):
        rows = self.download("fcs")

        self.assertEqual(rows[1][0], "FC 指挥")
        self.assertEqual((rows[1][1], rows[1][2]), ("2", "1.50"))


class MembersFileMatchesPageTests(MatchTestCase):
    def test_the_fixture_has_members(self):
        self.assertTrue(REAL_MEMBER_ROWS(*self.month).rows)

    def test_file_holds_the_member_rows(self):
        table = REAL_MEMBER_ROWS(*self.month)
        member_service.fill_last_pap(table.rows)
        rows = self.download("members")

        self.assertEqual(len(rows) - 1, len(table.rows))
        for line, row in zip(rows[1:], table.rows):
            self.assertEqual(line[1], row.main_character_name)
            self.assertEqual(line[0], "" if row.rank is None else str(row.rank))
            self.assertEqual((int(line[5]), int(line[6]), int(line[7])), (row.pap, row.fleets, row.characters))

    def test_the_link_of_the_page_exports_the_rows_the_page_lists_for_any_search_text(self):
        year, month = self.month
        self.client.force_login(self.lead)
        # Runs of spaces and tabs inside the text are not collapsed by the page, so the file must not collapse them.
        for text in ("alpha", "  alpha ", "alpha 一", "alpha  一", "alpha\t一", "a" * 300):
            with self.subTest(q=text):
                page = self.client.get(reverse("fleetops:alliance_members"), {"year": year, "month": month, "q": text})
                listed = [row.main_character_name for row in page.context["table"].rows]
                link = re.search(r'href="([^"]*/export/members/[^"]*format=csv[^"]*)"', page.content.decode()).group(1)

                rows = csv_rows(self.client.get(html.unescape(link)))

                self.assertEqual([line[1] for line in rows[1:]], listed)
        # The search that matches nothing on the page matches nothing in the file either.
        page = self.client.get(reverse("fleetops:alliance_members"), {"year": year, "month": month, "q": "alpha  一"})
        self.assertEqual(page.context["table"].rows, [])

    def test_filters_narrow_the_file_like_the_page(self):
        expected = REAL_MEMBER_ROWS(*self.month, status="inactive", sort="pap")
        rows = self.download("members", status="inactive", sort="pap")

        self.assertEqual([line[1] for line in rows[1:]], [row.main_character_name for row in expected.rows])
        self.assertTrue(all(line[8] in ("Below minimum", "Below minimum so far") for line in rows[1:]))

    def test_a_corporation_manager_gets_the_own_corporation(self):
        manager = f.member("manager", perms=f.CORP_MANAGEMENT_PERMS, main_name="Manager")
        rows = self.download("members", viewer=manager, corporation=f.DEFAULT_CORP[0])

        self.assertTrue(rows[1:])
        self.assertEqual({line[2] for line in rows[1:]}, {f.DEFAULT_CORP[1]})


class MemberFleetsFileMatchesPageTests(MatchTestCase):
    def test_the_fixture_has_fleets(self):
        self.assertTrue(REAL_MEMBER_FLEETS(self.a1, *self.month))

    def test_file_holds_the_fleets_of_the_page(self):
        fleets = REAL_MEMBER_FLEETS(self.a1, *self.month)
        rows = self.download("member-fleets", user=self.a1.pk)

        self.assertEqual(len(rows) - 1, len(fleets))
        self.assertEqual([int(line[7]) for line in rows[1:]], [fleet.pap for fleet in fleets])
        self.assertEqual(sum(int(line[7]) for line in rows[1:]), 3)
