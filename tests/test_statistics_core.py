"""Tests for the month aggregates the statistics pages and exports are built on, and for UTC month handling."""

import re
from datetime import date, datetime, timezone as dt_timezone
from decimal import Decimal
from pathlib import Path

from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext

import fleetops.services
from fleetops.models import AuditLog, FleetOperation, FleetType, ReviewStatus
from fleetops.services import members
from fleetops.services.audit import audit_event
from fleetops.services.periods import month_bounds
from fleetops.services.statistics import (
    UserTotals,
    attendance_queryset,
    fleet_types_in,
    member_statistics,
    not_counted_summary,
    user_fleet_type_totals,
    user_totals,
)
from fleetops.services.statistics import month_bounds as statistics_month_bounds
from fleetops.services.stats_types import (
    CorporationOverview,
    CorporationRow,
    FcRow,
    Leaderboard,
    MemberFleetRow,
    MemberRow,
    MemberTable,
)

from . import factories as f

UTC = dt_timezone.utc
Status = FleetOperation.Status


def utc(*args):
    return datetime(*args, tzinfo=UTC)


class MonthBoundsReExportTests(TestCase):
    def test_statistics_uses_the_periods_function(self):
        self.assertIs(statistics_month_bounds, month_bounds)

    def test_attendance_queryset_selects_the_utc_month(self):
        fc = f.create_user(perms=f.FC_PERMS)
        alice = f.member("alice")
        inside = f.add_attendance(f.closed_operation(fc, utc(2026, 3, 31, 23, 30)), alice)
        f.add_attendance(f.closed_operation(fc, utc(2026, 4, 1, 0, 0)), alice)
        f.add_attendance(f.closed_operation(fc, utc(2026, 2, 28, 23, 59, 59)), alice)
        for zone in ("Europe/Berlin", "Asia/Shanghai", "America/Los_Angeles"):
            with self.subTest(zone=zone), override_settings(TIME_ZONE=zone):
                self.assertEqual(list(attendance_queryset(2026, 3)), [inside])
                self.assertEqual(attendance_queryset(2026, 4).count(), 1)


class UserTotalsTests(TestCase):
    def setUp(self):
        self.fc = f.create_user(perms=f.FC_PERMS)
        self.strat = f.fleet_type("StratOp", "2.00")
        self.roam = f.fleet_type("Roam", "1.00")
        self.alice = f.member("alice")
        self.bob = f.member("bob", corporation=f.OTHER_CORP)
        self.alice_alt = f.add_alt(self.alice, "Alice Alt", corporation=f.OTHER_CORP)
        self.op1 = f.closed_operation(self.fc, utc(2026, 3, 5, 18), type_obj=self.strat)
        self.op2 = f.closed_operation(self.fc, utc(2026, 3, 9, 19), type_obj=self.roam)
        f.add_attendance(self.op1, self.alice, value=2)
        f.add_attendance(self.op1, self.alice, self.alice_alt, value=2)
        f.add_attendance(self.op2, self.alice, value=1)
        f.add_attendance(self.op1, self.bob, value=3)

    def test_pap_fleets_and_characters_per_user(self):
        totals = user_totals(attendance_queryset(2026, 3))
        self.assertEqual(totals[self.alice.pk], UserTotals(pap=5, fleets=2, characters=2))
        self.assertEqual(totals[self.bob.pk], UserTotals(pap=3, fleets=1, characters=1))
        self.assertEqual(set(totals), {self.alice.pk, self.bob.pk})

    def test_a_month_without_pap_is_empty(self):
        self.assertEqual(user_totals(attendance_queryset(2026, 4)), {})

    def test_rows_without_a_user_are_left_out(self):
        row = f.add_attendance(self.op2, self.bob)
        row.auth_user = None
        row.save()
        totals = user_totals(attendance_queryset(2026, 3))
        self.assertEqual(totals[self.bob.pk].pap, 3)

    def test_a_queryset_can_be_narrowed_first(self):
        base = attendance_queryset(2026, 3).filter(auth_user_id__in=[self.bob.pk])
        self.assertEqual(set(user_totals(base)), {self.bob.pk})

    def test_not_granted_pap_does_not_count(self):
        f.add_attendance(self.op2, self.bob, value=7, granted=False)
        self.assertEqual(user_totals(attendance_queryset(2026, 3))[self.bob.pk].pap, 3)

    def test_fleet_type_totals(self):
        totals = user_fleet_type_totals(attendance_queryset(2026, 3))
        self.assertEqual(totals[self.alice.pk], {self.strat.pk: 4, self.roam.pk: 1})
        self.assertEqual(totals[self.bob.pk], {self.strat.pk: 3})

    def test_one_query_each(self):
        base = attendance_queryset(2026, 3)
        with CaptureQueriesContext(connection) as queries:
            user_totals(base)
        self.assertEqual(len(queries), 1)
        with CaptureQueriesContext(connection) as queries:
            user_fleet_type_totals(base)
        self.assertEqual(len(queries), 1)

    def test_query_count_does_not_grow_with_the_number_of_users(self):
        base = attendance_queryset(2026, 3)
        with CaptureQueriesContext(connection) as small:
            user_totals(base)
            user_fleet_type_totals(base)
        for index in range(20):
            f.add_attendance(self.op2, f.member(f"extra{index}"))
        with CaptureQueriesContext(connection) as large:
            result = user_totals(base)
            user_fleet_type_totals(base)
        self.assertGreaterEqual(len(result), 22)
        self.assertEqual(len(small), len(large))


class FleetTypesInTests(TestCase):
    def test_display_order_and_one_query(self):
        late = FleetType.objects.create(name="Alpha", point_weight=Decimal("1"), sort_order=5)
        first = FleetType.objects.create(name="Zulu", point_weight=Decimal("1"), sort_order=1)
        tie_b = FleetType.objects.create(name="Bravo", point_weight=Decimal("1"), sort_order=3)
        tie_a = FleetType.objects.create(name="Aaron", point_weight=Decimal("1"), sort_order=3)
        FleetType.objects.create(name="Unused", point_weight=Decimal("1"), sort_order=0)
        with CaptureQueriesContext(connection) as queries:
            result = fleet_types_in([late.pk, first.pk, tie_b.pk, tie_a.pk, late.pk])
        self.assertEqual(result, [first, tie_a, tie_b, late])
        self.assertEqual(len(queries), 1)

    def test_empty_selection_needs_no_query(self):
        with CaptureQueriesContext(connection) as queries:
            self.assertEqual(fleet_types_in([]), [])
            self.assertEqual(fleet_types_in([None]), [])
        self.assertEqual(len(queries), 0)

    def test_accepts_a_set_and_a_generator(self):
        fleet_type = f.fleet_type("StratOp")
        self.assertEqual(fleet_types_in({fleet_type.pk}), [fleet_type])
        self.assertEqual(fleet_types_in(pk for pk in [fleet_type.pk]), [fleet_type])


class NotCountedSummaryTests(TestCase):
    def setUp(self):
        self.fc = f.create_user(perms=f.FC_PERMS)

    def test_empty_month(self):
        self.assertEqual(
            not_counted_summary(2026, 3), {"excluded": 0, "pending": 0, "rejected": 0, "running": 0}
        )

    def test_counts_by_reason(self):
        started = utc(2026, 3, 10, 18)
        f.closed_operation(self.fc, started)
        f.closed_operation(self.fc, started, counts_for_stats=False)
        f.closed_operation(self.fc, started, counts_for_stats=False, stats_mode=FleetOperation.StatsMode.EXCLUDE)
        f.closed_operation(self.fc, started, review_status=ReviewStatus.PENDING)
        f.closed_operation(self.fc, started, review_status=ReviewStatus.REJECTED)
        f.create_operation(self.fc, status=Status.ACTIVE, started_at=started)
        f.create_operation(self.fc, status=Status.ENDING, started_at=started)
        self.assertEqual(
            not_counted_summary(2026, 3), {"excluded": 2, "pending": 1, "rejected": 1, "running": 2}
        )

    def test_a_pending_fleet_is_counted_once(self):
        f.closed_operation(self.fc, utc(2026, 3, 10), review_status=ReviewStatus.PENDING, counts_for_stats=False)
        self.assertEqual(
            not_counted_summary(2026, 3), {"excluded": 0, "pending": 1, "rejected": 0, "running": 0}
        )

    def test_only_fleets_started_in_the_utc_month(self):
        f.closed_operation(self.fc, utc(2026, 3, 31, 23, 59), counts_for_stats=False)
        f.closed_operation(self.fc, utc(2026, 4, 1, 0, 0), counts_for_stats=False)
        f.closed_operation(self.fc, utc(2026, 2, 28, 23, 59), counts_for_stats=False)
        with override_settings(TIME_ZONE="Asia/Shanghai"):
            self.assertEqual(not_counted_summary(2026, 3)["excluded"], 1)
            self.assertEqual(not_counted_summary(2026, 4)["excluded"], 1)

    def test_scheduled_and_cancelled_fleets_are_not_listed(self):
        f.create_scheduled_operation(self.fc)
        f.create_operation(self.fc, status=Status.CANCELLED, started_at=utc(2026, 3, 10))
        self.assertEqual(sum(not_counted_summary(2026, 3).values()), 0)

    def test_one_query(self):
        f.closed_operation(self.fc, utc(2026, 3, 10), counts_for_stats=False)
        with CaptureQueriesContext(connection) as queries:
            not_counted_summary(2026, 3)
        self.assertEqual(len(queries), 1)


class DailyActivityTests(TestCase):
    def test_days_follow_eve_time_under_a_local_time_zone(self):
        fc = f.create_user(perms=f.FC_PERMS)
        pilot = f.member("pilot")
        late = f.closed_operation(fc, utc(2026, 3, 31, 23, 30))
        early = f.closed_operation(fc, utc(2026, 3, 1, 0, 30))
        f.add_attendance(late, pilot, value=2)
        f.add_attendance(early, pilot, value=1)
        for zone in ("Asia/Shanghai", "America/Los_Angeles", "Europe/Berlin"):
            with self.subTest(zone=zone), override_settings(TIME_ZONE=zone):
                daily = {row["operation__started_at__date"]: row["attendance"] for row in member_statistics(pilot, 2026, 3)["daily"]}
                self.assertEqual(daily, {date(2026, 3, 1): 1, date(2026, 3, 31): 2})


class NoLocalDateLookupsTests(TestCase):
    def test_no_local_date_lookups_in_services_and_views(self):
        """``__year``, ``__month`` and ``__date`` convert to the site time zone; months and days must be EVE time."""
        pattern = re.compile(r"__(year|month|date)\b")
        package = Path(fleetops.services.__file__).parent.parent
        offenders = []
        for path in sorted([*package.glob("*.py"), *(package / "services").glob("*.py")]):
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
                if line.lstrip().startswith("#"):
                    continue
                if pattern.search(line):
                    offenders.append(f"{path.name}:{number}: {line.strip()}")
        # The daily list keeps its old key name; the value comes from utc_day().
        offenders = [line for line in offenders if "operation__started_at__date" not in line]
        self.assertEqual(offenders, [])


class AuditEventTests(TestCase):
    def test_writes_an_entry(self):
        actor = f.create_user()
        entry = audit_event(
            actor,
            "statistics.export",
            "Statistics",
            "members:alliance:2026-10",
            new_value={"format": "csv", "params": {"month": 10}},
        )
        stored = AuditLog.objects.get(pk=entry.pk)
        self.assertEqual(stored.actor, actor)
        self.assertEqual(stored.action, "statistics.export")
        self.assertEqual(stored.object_type, "Statistics")
        self.assertEqual(stored.object_id, "members:alliance:2026-10")
        self.assertEqual(stored.new_value, {"format": "csv", "params": {"month": 10}})
        self.assertIsNone(stored.old_value)
        self.assertEqual(stored.reason, "")

    def test_object_id_fits_the_column(self):
        entry = audit_event(None, "statistics.export", "Statistics", "x" * 300)
        self.assertEqual(len(AuditLog.objects.get(pk=entry.pk).object_id), 100)

    def test_numbers_are_stored_as_text(self):
        entry = audit_event(None, "statistics.export", "Statistics", 42, reason="Because")
        stored = AuditLog.objects.get(pk=entry.pk)
        self.assertEqual(stored.object_id, "42")
        self.assertEqual(stored.reason, "Because")


class SharedTypesTests(TestCase):
    """The result types and the member service signatures the pages and exports are written against."""

    def test_member_row_defaults(self):
        row = MemberRow(
            user_id=1,
            main_character_id=2,
            main_character_name="Alice",
            corporation_id=3,
            corporation_name="Corp",
            corporation_ticker="CORP",
            alliance_id=None,
            alliance_name="",
            registered_at=utc(2026, 1, 1),
        )
        self.assertEqual((row.pap, row.fleets, row.characters), (0, 0, 0))
        self.assertEqual(row.by_fleet_type, {})
        self.assertIsNone(row.rank)
        self.assertEqual(row.status, "ok")
        self.assertIsNone(row.last_pap_at)
        other = MemberRow(1, 2, "Alice", 3, "Corp", "CORP", None, "", utc(2026, 1, 1))
        other.by_fleet_type[1] = 5
        self.assertEqual(row.by_fleet_type, {})

    def test_corporation_row(self):
        row = CorporationRow(
            corporation_id=None,
            corporation_name="Other corporations",
            corporation_ticker="",
            alliance_id=None,
            alliance_name="",
            mains=0,
            mains_with_pap=0,
            mains_below_minimum=0,
            total_pap=4,
            fleets=1,
            by_fleet_type={},
            average=Decimal("0.00"),
            status="none",
            is_other=True,
        )
        self.assertTrue(row.is_other)

    def test_fc_row_user_id(self):
        user = f.create_user()
        row = FcRow(user=user, main_name="Boss", fleets=2, points=Decimal("3.00"))
        self.assertEqual(row.user_id, user.pk)
        self.assertEqual(row.breakdown, {})

    def test_other_types_exist(self):
        for cls in (CorporationOverview, Leaderboard, MemberFleetRow, MemberTable):
            with self.subTest(cls=cls.__name__):
                self.assertTrue(hasattr(cls, "__dataclass_fields__"))

    def test_member_service_signatures(self):
        table = members.member_rows(2026, 10, corporation_id=None, status="all", query="", sort=None)
        self.assertEqual(table.rows, [])
        self.assertEqual(table.month.label, "2026-10")
        self.assertEqual(table.counts, {"all": 0, "with_pap": 0, "inactive": 0})
        board = members.leaderboard(2026, 10, corporation_id=None, size=None)
        self.assertEqual(board.size, 20)
        self.assertEqual(board.rows, [])
        self.assertEqual(members.member_fleets(f.create_user(), 2026, 10, limit=5), [])
        self.assertIsNone(members.fill_last_pap([]))
        self.assertEqual(members.PAGE_SIZE, 100)
        self.assertIn("-pap", members.SORTS)
