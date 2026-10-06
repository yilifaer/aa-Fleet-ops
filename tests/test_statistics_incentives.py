"""Tests for member/corporation/FC statistics, FC incentives and dashboard metrics."""

import random
import unittest
from datetime import date, datetime, timedelta, timezone as dt_timezone
from decimal import Decimal
from unittest import mock
from zoneinfo import ZoneInfo

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from fleetops.calculations import calculate_payouts, corporation_average
from fleetops.models import (
    AttendanceRecord,
    AuditLog,
    FleetMemberState,
    FleetOperation,
    FleetType,
    IncentivePeriod,
    MonthlyFCStatistic,
    OperationRoleAssignment,
)
from fleetops.services.dashboard import dashboard_metrics
from fleetops.services.incentives import finalize_period, rebuild_period, set_waiver, unlock_period
from fleetops.services.operations import create_manual_fleet
from fleetops.services.roles import add_role_assignment
from fleetops.services.statistics import (
    corporation_statistics,
    fc_operations_queryset,
    fc_statistics,
    member_statistics,
    month_bounds,
)

from . import factories as f

UTC = dt_timezone.utc
Status = FleetOperation.Status
Role = OperationRoleAssignment.Role
PeriodStatus = IncentivePeriod.Status


def utc(*args):
    return datetime(*args, tzinfo=UTC)


def closed_op(fc_user, started_at, *, type_obj=None, **extra):
    return f.create_operation(fc_user, status=Status.CLOSED, started_at=started_at, type_obj=type_obj, **extra)


def add_role(operation, user, *, character=None, role=Role.BACKSEAT_FC, grants_fc_credit=True):
    character = character or f.main_of(user)
    main = f.main_of(user)
    return OperationRoleAssignment.objects.create(
        operation=operation,
        role=role,
        character_id=character.character_id,
        character_name=character.character_name,
        main_character_id=main.character_id,
        main_character_name=main.character_name,
        auth_user=user,
        corporation_id=main.corporation_id,
        corporation_name=main.corporation_name,
        grants_fc_credit=grants_fc_credit,
    )


def add_state(operation, user, character, *, ship_type_id, ship_type_name):
    seen = operation.started_at
    return FleetMemberState.objects.create(
        operation=operation,
        character_id=character.character_id,
        character_name=character.character_name,
        main_character_id=f.main_of(user).character_id,
        main_character_name=f.main_of(user).character_name,
        auth_user=user,
        corporation_id=character.corporation_id,
        corporation_name=character.corporation_name,
        ship_type_id=ship_type_id,
        ship_type_name=ship_type_name,
        first_seen=seen,
        last_seen=seen,
    )


def stat_for(period, user):
    return MonthlyFCStatistic.objects.get(period=period, fc_user=user)


def audit_actions(obj):
    return list(
        AuditLog.objects.filter(object_type=obj.__class__.__name__, object_id=str(obj.pk))
        .order_by("pk")
        .values_list("action", flat=True)
    )


# ---------------------------------------------------------------------------
# Pure calculations
# ---------------------------------------------------------------------------


class CorporationAverageCalculationTests(TestCase):
    def test_fractional_average(self):
        self.assertAlmostEqual(corporation_average(7, 4), 1.75)

    def test_no_mains_is_zero(self):
        self.assertEqual(corporation_average(12, 0), 0.0)


class PayoutCalculationTests(TestCase):
    @staticmethod
    def row(user_id, points, eligible=True, waived=False):
        return {"user_id": user_id, "points": points, "eligible": eligible, "waived": waived}

    def test_shares_are_floored_and_remainder_goes_to_top_scorer(self):
        rows = [self.row(1, Decimal("4")), self.row(2, Decimal("2"))]
        self.assertEqual(calculate_payouts(1000, rows), {1: 667, 2: 333})

    def test_waived_top_scorer_never_receives_remainder(self):
        rows = [
            self.row(1, Decimal("5"), waived=True),
            self.row(2, Decimal("2")),
            self.row(3, Decimal("1")),
        ]
        self.assertEqual(calculate_payouts(100, rows), {1: 0, 2: 67, 3: 33})

    def test_ineligible_top_scorer_never_receives_remainder(self):
        rows = [
            self.row(1, Decimal("50"), eligible=False),
            self.row(2, Decimal("1")),
            self.row(3, Decimal("1")),
            self.row(4, Decimal("1")),
        ]
        self.assertEqual(calculate_payouts(100, rows), {1: 0, 2: 34, 3: 33, 4: 33})

    def test_tie_break_is_lowest_user_id_regardless_of_row_order(self):
        rows = [self.row(30, Decimal("1.5")), self.row(7, Decimal("1.5")), self.row(12, Decimal("1.5"))]
        self.assertEqual(calculate_payouts(1_000_000_001, rows), {7: 333_333_335, 12: 333_333_333, 30: 333_333_333})

    def test_decimal_points_are_not_float_rounded(self):
        rows = [self.row(1, Decimal("0.10")), self.row(2, Decimal("0.20"))]
        self.assertEqual(calculate_payouts(300, rows), {1: 100, 2: 200})

    def test_zero_budget_pays_nothing(self):
        rows = [self.row(1, Decimal("3")), self.row(2, Decimal("1"))]
        self.assertEqual(calculate_payouts(0, rows), {1: 0, 2: 0})

    def test_everyone_waived_pays_nothing(self):
        rows = [self.row(1, Decimal("3"), waived=True), self.row(2, Decimal("1"), waived=True)]
        self.assertEqual(calculate_payouts(500, rows), {1: 0, 2: 0})

    def test_payouts_always_sum_to_budget(self):
        rng = random.Random(1234)
        for _ in range(200):
            rows = [
                self.row(
                    user_id,
                    Decimal(rng.randint(0, 2000)) / 100,
                    eligible=rng.random() > 0.2,
                    waived=rng.random() < 0.15,
                )
                for user_id in rng.sample(range(1, 500), rng.randint(1, 12))
            ]
            budget = rng.randint(0, 5_000_000_000)
            payouts = calculate_payouts(budget, rows)
            paying = [r for r in rows if r["eligible"] and not r["waived"] and r["points"] > 0]
            self.assertEqual(sum(payouts.values()), budget if paying else 0)
            for r in rows:
                if r not in paying:
                    self.assertEqual(payouts[r["user_id"]], 0)
                self.assertGreaterEqual(payouts[r["user_id"]], 0)


# ---------------------------------------------------------------------------
# Month boundaries
# ---------------------------------------------------------------------------


class MonthBoundsTests(TestCase):
    def test_regular_month(self):
        self.assertEqual(month_bounds(2026, 4), (utc(2026, 4, 1), utc(2026, 5, 1)))

    def test_december_rolls_over_to_next_year(self):
        self.assertEqual(month_bounds(2025, 12), (utc(2025, 12, 1), utc(2026, 1, 1)))

    def test_bounds_are_timezone_aware(self):
        start, end = month_bounds(2026, 2)
        self.assertTrue(timezone.is_aware(start))
        self.assertTrue(timezone.is_aware(end))
        self.assertEqual(end - start, timedelta(days=28))

    # Known issue: month bounds follow the active Django time zone instead of UTC (EVE time)
    @unittest.expectedFailure
    def test_bounds_stay_utc_when_a_local_time_zone_is_active(self):
        with timezone.override(ZoneInfo("Europe/Berlin")):
            start, end = month_bounds(2026, 4)
        self.assertEqual(start, utc(2026, 4, 1))
        self.assertEqual(end, utc(2026, 5, 1))


# ---------------------------------------------------------------------------
# Corporation statistics
# ---------------------------------------------------------------------------


class CorporationStatisticsTests(TestCase):
    def setUp(self):
        self.fc = f.create_user(perms=f.FC_PERMS)
        self.stratop = f.fleet_type("StratOp", "2.00")
        self.roam = f.fleet_type("Roam", "1.00")
        self.op1 = closed_op(self.fc, utc(2026, 3, 5, 18), type_obj=self.stratop)
        self.op2 = closed_op(self.fc, utc(2026, 3, 20, 19), type_obj=self.roam)
        self.alice = f.create_user("alice")
        self.bob = f.create_user("bob")

    def test_alts_are_not_in_the_denominator(self):
        alt_one = f.add_alt(self.alice, "Alice Alt One")
        alt_two = f.add_alt(self.alice, "Alice Alt Two")
        f.add_attendance(self.op1, self.alice)
        f.add_attendance(self.op1, self.alice, alt_one)
        f.add_attendance(self.op1, self.alice, alt_two)
        f.add_attendance(self.op1, self.bob)

        stats = corporation_statistics(f.DEFAULT_CORP[0], 2026, 3)

        # fc, alice and bob have mains in the corporation; alts do not count.
        self.assertEqual(stats["main_character_count"], 3)
        self.assertEqual(stats["total_attendance"], 4)
        self.assertAlmostEqual(stats["average_attendance"], 4 / 3)

    def test_alt_in_another_corporation_is_credited_to_the_main_corporation(self):
        foreign_alt = f.add_alt(self.alice, "Alice Foreign Alt", corporation=f.OTHER_CORP, alliance=f.FOREIGN_ALLIANCE)
        f.add_attendance(self.op1, self.alice, foreign_alt)

        own = corporation_statistics(f.DEFAULT_CORP[0], 2026, 3)
        other = corporation_statistics(f.OTHER_CORP[0], 2026, 3)

        self.assertEqual(own["total_attendance"], 1)
        self.assertEqual(other["total_attendance"], 0)
        self.assertEqual(other["main_character_count"], 0)
        self.assertEqual(other["average_attendance"], 0)

    def test_main_in_other_corporation_with_alt_here_is_not_counted_as_main(self):
        outsider = f.create_user("outsider", corporation=f.OTHER_CORP)
        f.add_alt(outsider, "Outsider Alt", corporation=f.DEFAULT_CORP)
        stats = corporation_statistics(f.DEFAULT_CORP[0], 2026, 3)
        self.assertEqual(stats["main_character_count"], 3)

    def test_capped_and_non_granted_rows_are_excluded(self):
        alt = f.add_alt(self.alice, "Alice Alt")
        f.add_attendance(self.op1, self.alice)
        f.add_attendance(self.op1, self.alice, alt, granted=False, capped=True)
        f.add_attendance(self.op2, self.bob, granted=False)

        stats = corporation_statistics(f.DEFAULT_CORP[0], 2026, 3)

        self.assertEqual(stats["total_attendance"], 1)
        self.assertEqual(stats["breakdown"], {"StratOp": 1})

    def test_attendance_value_is_summed_not_counted(self):
        f.add_attendance(self.op1, self.alice, value=3)
        f.add_attendance(self.op2, self.bob, value=2)
        stats = corporation_statistics(f.DEFAULT_CORP[0], 2026, 3)
        self.assertEqual(stats["total_attendance"], 5)

    def test_breakdown_and_average_per_fleet_type(self):
        f.add_attendance(self.op1, self.alice, value=2)
        f.add_attendance(self.op1, self.bob, value=2)
        f.add_attendance(self.op2, self.alice)

        stats = corporation_statistics(f.DEFAULT_CORP[0], 2026, 3)

        self.assertEqual(stats["breakdown"], {"StratOp": 4, "Roam": 1})
        self.assertAlmostEqual(stats["breakdown_average"]["StratOp"], 4 / 3)
        self.assertAlmostEqual(stats["breakdown_average"]["Roam"], 1 / 3)

    def test_month_boundaries_are_utc(self):
        last_second = closed_op(self.fc, utc(2026, 3, 31, 23, 59, 59))
        first_second = closed_op(self.fc, utc(2026, 4, 1, 0, 0, 0))
        before = closed_op(self.fc, utc(2026, 2, 28, 23, 59, 59))
        f.add_attendance(last_second, self.alice)
        f.add_attendance(first_second, self.alice, value=2)
        f.add_attendance(before, self.alice, value=3)

        self.assertEqual(corporation_statistics(f.DEFAULT_CORP[0], 2026, 3)["total_attendance"], 1)
        self.assertEqual(corporation_statistics(f.DEFAULT_CORP[0], 2026, 4)["total_attendance"], 2)
        self.assertEqual(corporation_statistics(f.DEFAULT_CORP[0], 2026, 2)["total_attendance"], 3)

    def test_users_outside_configured_alliances_are_hidden(self):
        f.settings(history_alliance_ids=str(f.DEFAULT_ALLIANCE[0]))
        departed = f.create_user("departed", corporation=f.OTHER_CORP, alliance=f.FOREIGN_ALLIANCE)
        record = f.add_attendance(self.op1, departed)
        # Recorded while the departed pilot was still in the corporation.
        AttendanceRecord.objects.filter(pk=record.pk).update(corporation_id=f.DEFAULT_CORP[0])
        f.add_attendance(self.op1, self.alice)

        stats = corporation_statistics(f.DEFAULT_CORP[0], 2026, 3)

        self.assertEqual(stats["total_attendance"], 1)

    # Known issue: month bounds follow the active Django time zone instead of UTC (EVE time)
    @unittest.expectedFailure
    @override_settings(TIME_ZONE="Europe/Berlin")
    def test_month_is_utc_even_with_local_site_time_zone(self):
        late = closed_op(self.fc, utc(2026, 3, 31, 23, 30))
        f.add_attendance(late, self.alice)
        self.assertEqual(corporation_statistics(f.DEFAULT_CORP[0], 2026, 3)["total_attendance"], 1)
        self.assertEqual(corporation_statistics(f.DEFAULT_CORP[0], 2026, 4)["total_attendance"], 0)


# ---------------------------------------------------------------------------
# Personal statistics
# ---------------------------------------------------------------------------


class MemberStatisticsTests(TestCase):
    def setUp(self):
        self.fc = f.create_user(perms=f.FC_PERMS)
        self.stratop = f.fleet_type("StratOp", "2.00")
        self.roam = FleetType.objects.create(name="Roaming Fleet", short_name="Roam", point_weight=Decimal("1.00"))
        self.pilot = f.create_user("pilot", perms=f.MEMBER_PERMS)
        self.main = f.main_of(self.pilot)
        self.alt = f.add_alt(self.pilot, "Pilot Alt", corporation=f.OTHER_CORP)

        self.op1 = closed_op(self.fc, utc(2026, 3, 3, 19), type_obj=self.stratop)
        self.op2 = closed_op(self.fc, utc(2026, 3, 3, 22), type_obj=self.roam)
        self.op3 = closed_op(self.fc, utc(2026, 3, 18, 20), type_obj=self.stratop)
        self.outside = closed_op(self.fc, utc(2026, 4, 1, 0, 0), type_obj=self.stratop)

        f.add_attendance(self.op1, self.pilot)
        f.add_attendance(self.op1, self.pilot, self.alt)
        f.add_attendance(self.op2, self.pilot)
        f.add_attendance(self.op3, self.pilot, self.alt, value=3)
        f.add_attendance(self.op3, self.pilot, granted=False, capped=True)
        f.add_attendance(self.outside, self.pilot, value=2)
        f.add_attendance(self.op1, f.create_user("someone"))

    def test_totals_for_selected_month(self):
        stats = member_statistics(self.pilot, 2026, 3)
        self.assertEqual(stats["total"], 6)
        self.assertEqual(stats["unique_fleets"], 3)

    def test_other_month_is_isolated(self):
        stats = member_statistics(self.pilot, 2026, 4)
        self.assertEqual(stats["total"], 2)
        self.assertEqual(stats["unique_fleets"], 1)
        self.assertEqual(stats["breakdown"], {"StratOp": 2})

    def test_empty_month(self):
        stats = member_statistics(self.pilot, 2025, 1)
        self.assertEqual(stats["total"], 0)
        self.assertEqual(stats["unique_fleets"], 0)
        self.assertEqual(stats["breakdown"], {})
        self.assertEqual(stats["characters"], [])
        self.assertEqual(stats["daily"], [])
        self.assertEqual(stats["top_ships"], [])
        self.assertEqual(stats["roles"], [])

    def test_fleet_type_breakdown_uses_display_name(self):
        stats = member_statistics(self.pilot, 2026, 3)
        self.assertEqual(stats["breakdown"], {"StratOp": 5, "Roam": 1})

    def test_characters_used(self):
        stats = member_statistics(self.pilot, 2026, 3)
        rows = [(r["character_name"], r["fleets"], r["attendance"]) for r in stats["characters"]]
        self.assertEqual(rows, [("Pilot Alt", 2, 4), (self.main.character_name, 2, 2)])

    def test_daily_activity(self):
        stats = member_statistics(self.pilot, 2026, 3)
        daily = {row["operation__started_at__date"]: row["attendance"] for row in stats["daily"]}
        self.assertEqual(daily, {date(2026, 3, 3): 3, date(2026, 3, 18): 3})
        self.assertEqual([row["operation__started_at__date"] for row in stats["daily"]], sorted(daily))

    def test_top_ships_count_fleets_not_rows(self):
        add_state(self.op1, self.pilot, self.main, ship_type_id=11987, ship_type_name="Guardian")
        add_state(self.op1, self.pilot, self.alt, ship_type_id=11987, ship_type_name="Guardian")
        add_state(self.op2, self.pilot, self.main, ship_type_id=11987, ship_type_name="Guardian")
        add_state(self.op3, self.pilot, self.alt, ship_type_id=17738, ship_type_name="Machariel")
        add_state(self.outside, self.pilot, self.main, ship_type_id=670, ship_type_name="Capsule")

        stats = member_statistics(self.pilot, 2026, 3)

        ships = [(s["ship_type_name"], s["uses"]) for s in stats["top_ships"]]
        self.assertEqual(ships, [("Guardian", 2), ("Machariel", 1)])
        self.assertIn("/types/11987/", stats["top_ships"][0]["image_url"])

    def test_special_roles(self):
        add_role(self.op1, self.pilot, role=Role.LOGI_ANCHOR, grants_fc_credit=False)
        add_role(self.op3, self.pilot, role=Role.LOGI_ANCHOR, grants_fc_credit=False)
        add_role(self.op2, self.pilot, role=Role.SNOWFLAKE, grants_fc_credit=False)
        add_role(self.outside, self.pilot, role=Role.BACKSEAT_FC, grants_fc_credit=False)

        stats = member_statistics(self.pilot, 2026, 3)

        roles = {r["key"]: (r["label"], r["count"]) for r in stats["roles"]}
        self.assertEqual(roles, {"logi_anchor": ("Logi Anchor", 2), "snowflake": ("Snowflake Member", 1)})

    def test_fc_summary_included(self):
        closed_op(self.pilot, utc(2026, 3, 9), type_obj=self.stratop)
        closed_op(self.pilot, utc(2026, 3, 10), type_obj=self.roam)
        stats = member_statistics(self.pilot, 2026, 3)
        self.assertEqual(stats["fc_fleet_count"], 2)
        self.assertAlmostEqual(stats["fc_points"], 3.0)

    def test_hidden_when_main_left_configured_alliances(self):
        f.settings(history_alliance_ids=str(f.FOREIGN_ALLIANCE[0]))
        self.assertEqual(member_statistics(self.pilot, 2026, 3)["total"], 0)

    def test_page_renders_breakdowns_for_selected_month(self):
        add_state(self.op3, self.pilot, self.alt, ship_type_id=17738, ship_type_name="Machariel")
        add_state(self.outside, self.pilot, self.main, ship_type_id=670, ship_type_name="Capsule")
        add_role(self.op1, self.pilot, role=Role.LOGI_ANCHOR, grants_fc_credit=False)

        self.client.force_login(self.pilot)
        response = self.client.get(reverse("fleetops:my_statistics"), {"year": 2026, "month": 3})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["stats"]["total"], 6)
        self.assertContains(response, "Pilot Alt")
        self.assertContains(response, "Machariel")
        self.assertNotContains(response, "Capsule")
        self.assertContains(response, "Logi Anchor")
        self.assertContains(response, "03 Mar")
        self.assertContains(response, "18 Mar")


# ---------------------------------------------------------------------------
# FC statistics
# ---------------------------------------------------------------------------


class FCStatisticsTests(TestCase):
    def setUp(self):
        self.stratop = f.fleet_type("StratOp", "2.50")
        self.roam = f.fleet_type("Roam", "1.00")
        self.fc = f.create_user("fc", perms=f.FC_PERMS)
        self.co_fc = f.create_user("cofc", perms=f.FC_PERMS)

    def test_counts_own_fleets_with_snapshot_points(self):
        closed_op(self.fc, utc(2026, 3, 1, 12), type_obj=self.stratop)
        closed_op(self.fc, utc(2026, 3, 2, 12), type_obj=self.roam)
        self.stratop.point_weight = Decimal("9.00")
        self.stratop.save()

        stats = fc_statistics(self.fc, 2026, 3)

        self.assertEqual(stats["fleet_count"], 2)
        self.assertAlmostEqual(stats["total_points"], 3.5)
        self.assertEqual(stats["breakdown"]["StratOp"], {"count": 1, "points": 2.5})
        self.assertEqual(stats["breakdown"]["Roam"], {"count": 1, "points": 1.0})

    def test_special_role_with_fc_credit_counts(self):
        op = closed_op(self.fc, utc(2026, 3, 4, 20), type_obj=self.stratop)
        add_role(op, self.co_fc, role=Role.BACKSEAT_FC)
        stats = fc_statistics(self.co_fc, 2026, 3)
        self.assertEqual(stats["fleet_count"], 1)
        self.assertAlmostEqual(stats["total_points"], 2.5)

    def test_special_role_without_fc_credit_does_not_count(self):
        op = closed_op(self.fc, utc(2026, 3, 4, 20), type_obj=self.stratop)
        add_role(op, self.co_fc, role=Role.LOGI_ANCHOR, grants_fc_credit=False)
        self.assertEqual(fc_statistics(self.co_fc, 2026, 3)["fleet_count"], 0)

    def test_user_counted_once_per_fleet_with_several_credit_sources(self):
        op = closed_op(self.fc, utc(2026, 3, 4, 20), type_obj=self.stratop)
        add_role(op, self.fc, role=Role.BACKSEAT_FC)
        add_role(op, self.co_fc, role=Role.BACKSEAT_FC)
        add_role(op, self.co_fc, role=Role.LOGI_ANCHOR)
        co_alt = f.add_alt(self.co_fc, "Co FC Alt")
        add_role(op, self.co_fc, character=co_alt, role=Role.SNOWFLAKE)

        for user in (self.fc, self.co_fc):
            stats = fc_statistics(user, 2026, 3)
            self.assertEqual(stats["fleet_count"], 1, user.username)
            self.assertAlmostEqual(stats["total_points"], 2.5)
            self.assertEqual(fc_operations_queryset(user, 2026, 3).count(), 1)

    def test_another_users_credit_on_the_same_fleet_is_not_shared(self):
        op = closed_op(self.fc, utc(2026, 3, 4, 20), type_obj=self.stratop)
        member = f.create_user("member", perms=f.MEMBER_PERMS)
        add_role(op, member, role=Role.LOGI_ANCHOR, grants_fc_credit=False)
        add_role(op, self.co_fc, role=Role.BACKSEAT_FC)
        self.assertEqual(fc_statistics(member, 2026, 3)["fleet_count"], 0)
        self.assertEqual(fc_statistics(self.co_fc, 2026, 3)["fleet_count"], 1)

    def test_draft_and_cancelled_are_excluded(self):
        f.create_operation(self.fc, status=Status.DRAFT, started_at=utc(2026, 3, 5))
        f.create_operation(self.fc, status=Status.CANCELLED, started_at=utc(2026, 3, 6))
        closed_op(self.fc, utc(2026, 3, 7))
        self.assertEqual(fc_statistics(self.fc, 2026, 3)["fleet_count"], 1)

    def test_other_months_excluded(self):
        closed_op(self.fc, utc(2026, 2, 28, 23, 59, 59))
        closed_op(self.fc, utc(2026, 3, 1, 0, 0, 0))
        closed_op(self.fc, utc(2026, 4, 1, 0, 0, 0))
        self.assertEqual(fc_statistics(self.fc, 2026, 3)["fleet_count"], 1)

    def test_role_credit_is_snapshotted_at_assignment_time(self):
        op = closed_op(self.fc, utc(2026, 3, 4, 20), type_obj=self.roam)
        assignment = add_role_assignment(op, actor=self.fc, role=Role.BACKSEAT_FC, character_id=f.main_of(self.co_fc).character_id)
        self.assertTrue(assignment.grants_fc_credit)

        self.co_fc.user_permissions.clear()
        self.assertEqual(fc_statistics(self.co_fc, 2026, 3)["fleet_count"], 1)

    def test_role_assignee_without_start_fleet_gets_no_credit(self):
        member = f.create_user("member", perms=f.MEMBER_PERMS)
        op = closed_op(self.fc, utc(2026, 3, 4, 20))
        assignment = add_role_assignment(op, actor=self.fc, role=Role.BACKSEAT_FC, character_id=f.main_of(member).character_id)
        self.assertFalse(assignment.grants_fc_credit)
        self.assertEqual(fc_statistics(member, 2026, 3)["fleet_count"], 0)

    def test_manual_fleet_counts(self):
        create_manual_fleet(
            user=self.fc,
            cleaned_data={
                "started_at": utc(2026, 3, 12, 19),
                "ended_at": utc(2026, 3, 12, 21),
                "fleet_type": self.stratop,
                "formup": "Jita",
            },
        )
        stats = fc_statistics(self.fc, 2026, 3)
        self.assertEqual(stats["fleet_count"], 1)
        self.assertAlmostEqual(stats["total_points"], 2.5)


# ---------------------------------------------------------------------------
# Statistics views
# ---------------------------------------------------------------------------


class StatisticsViewTests(TestCase):
    def setUp(self):
        self.member = f.create_user("member", perms=f.MEMBER_PERMS)
        self.corp_manager = f.create_user("corpmanager", perms=f.CORP_MANAGEMENT_PERMS)
        self.lead = f.create_user("lead", perms=f.MEMBER_PERMS + ["fleetops.view_all_stats"])
        self.fc = f.create_user("fc", perms=f.FC_PERMS)
        self.other_corp_pilot = f.create_user("othercorp", perms=f.MEMBER_PERMS, corporation=f.OTHER_CORP)
        self.stratop = f.fleet_type("StratOp", "2.00")

        self.march = closed_op(self.fc, utc(2025, 3, 10, 20), type_obj=self.stratop)
        f.add_attendance(self.march, self.member, value=2)
        f.add_attendance(self.march, self.other_corp_pilot)

    def get(self, user, name, *args, **params):
        if user is not None:
            self.client.force_login(user)
        return self.client.get(reverse(f"fleetops:{name}", args=args), params)

    def test_my_statistics_for_explicit_month(self):
        response = self.get(self.member, "my_statistics", year=2025, month=3)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["year"], 2025)
        self.assertEqual(response.context["month"], 3)
        self.assertEqual(response.context["stats"]["total"], 2)

        response = self.get(self.member, "my_statistics", year=2025, month=4)
        self.assertEqual(response.context["stats"]["total"], 0)

    def test_invalid_period_values_fall_back_to_current_month(self):
        now = timezone.now()
        bad_values = [
            {"year": "abc", "month": "3"},
            {"year": "2025", "month": "13"},
            {"year": "2025", "month": "0"},
            {"year": "1999", "month": "3"},
            {"year": "99999999999999999999999", "month": "1"},
            {"year": "2025", "month": "-1"},
            {"year": "", "month": ""},
        ]
        pages = [
            (self.member, "my_statistics", ()),
            (self.corp_manager, "corporation_statistics", ()),
            (self.lead, "all_corporation_statistics", ()),
            (self.lead, "corporation_statistics_detail", (f.DEFAULT_CORP[0],)),
            (self.lead, "all_fc_statistics", ()),
            (self.lead, "fc_statistics_detail", (self.fc.pk,)),
        ]
        for user, name, args in pages:
            for params in bad_values:
                with self.subTest(page=name, **params):
                    response = self.get(user, name, *args, **params)
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual((response.context["year"], response.context["month"]), (now.year, now.month))

    def test_my_statistics_requires_basic_access(self):
        nobody = f.create_user("nobody")
        self.assertEqual(self.get(nobody, "my_statistics").status_code, 403)

    def test_anonymous_is_redirected_to_login(self):
        for name, args in [
            ("my_statistics", ()),
            ("corporation_statistics", ()),
            ("all_corporation_statistics", ()),
            ("corporation_statistics_detail", (f.DEFAULT_CORP[0],)),
            ("all_fc_statistics", ()),
            ("fc_statistics_detail", (self.fc.pk,)),
            ("incentive_review", ()),
        ]:
            with self.subTest(page=name):
                response = self.get(None, name, *args)
                self.assertEqual(response.status_code, 302)
                self.assertIn("login", response["Location"])

    def test_corporation_statistics_shows_own_main_corporation(self):
        response = self.get(self.corp_manager, "corporation_statistics", year=2025, month=3)
        self.assertEqual(response.status_code, 200)
        stats = response.context["stats"]
        self.assertEqual(stats["corporation_id"], f.DEFAULT_CORP[0])
        self.assertEqual(stats["corporation_name"], f.DEFAULT_CORP[1])
        self.assertEqual(stats["total_attendance"], 2)
        # member, corpmanager, lead and fc have mains in the default corporation.
        self.assertEqual(stats["main_character_count"], 4)
        self.assertContains(response, "0.50")

    def test_corporation_statistics_requires_view_corp_stats(self):
        self.assertEqual(self.get(self.member, "corporation_statistics").status_code, 403)

    def test_corp_manager_cannot_open_other_corporations(self):
        self.assertEqual(self.get(self.corp_manager, "corporation_statistics_detail", f.OTHER_CORP[0]).status_code, 403)
        self.assertEqual(self.get(self.corp_manager, "corporation_statistics_detail", f.DEFAULT_CORP[0]).status_code, 403)
        self.assertEqual(self.get(self.corp_manager, "all_corporation_statistics").status_code, 403)
        self.assertEqual(self.get(self.corp_manager, "all_fc_statistics").status_code, 403)

    def test_view_all_stats_sees_every_corporation(self):
        response = self.get(self.lead, "all_corporation_statistics", year=2025, month=3)
        self.assertEqual(response.status_code, 200)
        rows = {r["corporation_id"]: r for r in response.context["rows"]}
        self.assertEqual(set(rows), {f.DEFAULT_CORP[0], f.OTHER_CORP[0]})
        self.assertEqual(rows[f.OTHER_CORP[0]]["total_attendance"], 1)
        self.assertEqual(rows[f.OTHER_CORP[0]]["average_attendance"], 1)

        response = self.get(self.lead, "corporation_statistics_detail", f.OTHER_CORP[0], year=2025, month=3)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["stats"]["corporation_name"], f.OTHER_CORP[1])
        self.assertEqual(response.context["stats"]["total_attendance"], 1)

    def test_unknown_corporation_is_404(self):
        self.assertEqual(self.get(self.lead, "corporation_statistics_detail", 98_765_432).status_code, 404)

    def test_all_fc_statistics_lists_role_credit_and_sorts_by_points(self):
        co_fc = f.create_user("cofc", perms=f.FC_PERMS)
        add_role(self.march, co_fc)
        closed_op(co_fc, utc(2025, 3, 12), type_obj=self.stratop)
        closed_op(co_fc, utc(2025, 3, 13), type_obj=self.stratop)
        closed_op(self.fc, utc(2025, 4, 1), type_obj=self.stratop)

        response = self.get(self.lead, "all_fc_statistics", year=2025, month=3)

        self.assertEqual(response.status_code, 200)
        rows = [(r["user"].username, r["fleet_count"], r["total_points"]) for r in response.context["rows"]]
        self.assertEqual(rows, [("cofc", 3, 6.0), ("fc", 1, 2.0)])

    def test_all_fc_statistics_skips_draft_and_cancelled_only_fcs(self):
        idle = f.create_user("idle", perms=f.FC_PERMS)
        f.create_operation(idle, status=Status.DRAFT, started_at=utc(2025, 3, 2))
        f.create_operation(idle, status=Status.CANCELLED, started_at=utc(2025, 3, 3))
        response = self.get(self.lead, "all_fc_statistics", year=2025, month=3)
        self.assertEqual([r["user"] for r in response.context["rows"]], [self.fc])

    def test_all_fc_statistics_requires_view_all_stats(self):
        self.assertEqual(self.get(self.fc, "all_fc_statistics").status_code, 403)

    def test_fc_can_open_own_detail_but_not_others(self):
        response = self.get(self.fc, "fc_statistics_detail", self.fc.pk, year=2025, month=3)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["stat"]["fleet_count"], 1)
        self.assertEqual(list(response.context["operations"]), [self.march])

        self.assertEqual(self.get(self.member, "fc_statistics_detail", self.fc.pk).status_code, 404)

    def test_fc_detail_lists_each_credited_fleet_once(self):
        add_role(self.march, self.fc, role=Role.BACKSEAT_FC)
        add_role(self.march, self.fc, role=Role.LOGI_ANCHOR)
        response = self.get(self.lead, "fc_statistics_detail", self.fc.pk, year=2025, month=3)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(list(response.context["operations"]), [self.march])
        self.assertEqual(response.context["stat"]["fleet_count"], 1)

    def test_fc_detail_unknown_user_is_404(self):
        self.assertEqual(self.get(self.lead, "fc_statistics_detail", 987_654).status_code, 404)


# ---------------------------------------------------------------------------
# Incentive services
# ---------------------------------------------------------------------------


class IncentiveRebuildTests(TestCase):
    def setUp(self):
        f.settings(incentive_enabled=True)
        self.stratop = f.fleet_type("StratOp", "2.00")
        self.roam = f.fleet_type("Roam", "1.00")
        self.fc1 = f.create_user("fc1", perms=f.FC_PERMS)
        self.fc2 = f.create_user("fc2", perms=f.FC_PERMS)
        self.fc3 = f.create_user("fc3", perms=f.FC_PERMS)
        self.period = IncentivePeriod.objects.create(year=2026, month=3, budget=1000, minimum_fleets=2)

    def test_rebuild_moves_period_to_review_with_policy_snapshot(self):
        closed_op(self.fc1, utc(2026, 3, 2))
        rebuild_period(self.period)
        self.period.refresh_from_db()
        self.assertEqual(self.period.status, PeriodStatus.REVIEW)
        self.assertEqual(self.period.policy_snapshot["minimum_fleets"], 2)
        self.assertEqual(self.period.policy_snapshot["fleet_type_weights"][str(self.stratop.pk)], "2.00")

    def test_eligibility_threshold_and_payout_split(self):
        closed_op(self.fc1, utc(2026, 3, 2), type_obj=self.roam)
        closed_op(self.fc1, utc(2026, 3, 3), type_obj=self.roam)
        closed_op(self.fc1, utc(2026, 3, 4), type_obj=self.stratop)
        closed_op(self.fc2, utc(2026, 3, 5), type_obj=self.roam)
        closed_op(self.fc2, utc(2026, 3, 6), type_obj=self.roam)
        closed_op(self.fc3, utc(2026, 3, 7), type_obj=FleetType.objects.create(name="Heavy", point_weight=Decimal("3.00")))

        rebuild_period(self.period)

        fc1, fc2, fc3 = (stat_for(self.period, u) for u in (self.fc1, self.fc2, self.fc3))
        self.assertEqual((fc1.fleet_count, fc1.total_points, fc1.eligible), (3, Decimal("4.00"), True))
        self.assertEqual((fc2.fleet_count, fc2.total_points, fc2.eligible), (2, Decimal("2.00"), True))
        self.assertEqual((fc3.fleet_count, fc3.total_points, fc3.eligible), (1, Decimal("3.00"), False))
        self.assertEqual((fc1.calculated_payout, fc2.calculated_payout, fc3.calculated_payout), (667, 333, 0))
        self.assertEqual(fc1.final_payout, fc1.calculated_payout)

    def test_once_eligible_all_fleets_of_the_month_count(self):
        for day in (1, 9, 17, 25):
            closed_op(self.fc1, utc(2026, 3, day), type_obj=self.stratop)
        rebuild_period(self.period)
        row = stat_for(self.period, self.fc1)
        self.assertEqual(row.fleet_count, 4)
        self.assertEqual(row.total_points, Decimal("8.00"))
        self.assertEqual(row.fleet_type_breakdown, {"StratOp": {"count": 4, "points": 8.0}})

    def test_exactly_minimum_fleets_is_eligible(self):
        closed_op(self.fc1, utc(2026, 3, 1))
        closed_op(self.fc1, utc(2026, 3, 2))
        closed_op(self.fc2, utc(2026, 3, 3))
        rebuild_period(self.period)
        self.assertTrue(stat_for(self.period, self.fc1).eligible)
        self.assertFalse(stat_for(self.period, self.fc2).eligible)
        self.assertEqual(stat_for(self.period, self.fc1).calculated_payout, 1000)

    def test_point_weight_snapshot_is_used(self):
        closed_op(self.fc1, utc(2026, 3, 1), type_obj=self.stratop)
        closed_op(self.fc1, utc(2026, 3, 2), type_obj=self.stratop)
        self.stratop.point_weight = Decimal("10.00")
        self.stratop.save()

        rebuild_period(self.period)

        self.assertEqual(stat_for(self.period, self.fc1).total_points, Decimal("4.00"))

    def test_draft_and_cancelled_fleets_are_excluded(self):
        f.create_operation(self.fc1, status=Status.DRAFT, started_at=utc(2026, 3, 1))
        f.create_operation(self.fc1, status=Status.CANCELLED, started_at=utc(2026, 3, 2))
        closed_op(self.fc1, utc(2026, 3, 3))
        rebuild_period(self.period)
        row = stat_for(self.period, self.fc1)
        self.assertEqual(row.fleet_count, 1)
        self.assertFalse(row.eligible)

    def test_fc_with_only_draft_or_cancelled_fleets_gets_no_row(self):
        f.create_operation(self.fc2, status=Status.CANCELLED, started_at=utc(2026, 3, 2))
        rebuild_period(self.period)
        self.assertFalse(MonthlyFCStatistic.objects.filter(period=self.period, fc_user=self.fc2).exists())

    def test_only_closed_fleets_count(self):
        closed_op(self.fc1, utc(2026, 3, 1))
        for status in (Status.ACTIVE, Status.STARTING, Status.ENDING, Status.ERROR):
            f.create_operation(self.fc1, status=status, started_at=utc(2026, 3, 2))
        rebuild_period(self.period)
        row = stat_for(self.period, self.fc1)
        self.assertEqual(row.fleet_count, 1)
        self.assertFalse(row.eligible)

    def test_fleets_outside_the_month_are_excluded(self):
        closed_op(self.fc1, utc(2026, 2, 28, 23, 59, 59))
        closed_op(self.fc1, utc(2026, 3, 1, 0, 0, 0))
        closed_op(self.fc1, utc(2026, 3, 31, 23, 59, 59))
        closed_op(self.fc1, utc(2026, 4, 1, 0, 0, 0))
        rebuild_period(self.period)
        self.assertEqual(stat_for(self.period, self.fc1).fleet_count, 2)

    def test_manual_fleets_count(self):
        for day in (5, 6):
            create_manual_fleet(
                user=self.fc1,
                cleaned_data={
                    "started_at": utc(2026, 3, day, 19),
                    "ended_at": utc(2026, 3, day, 21),
                    "fleet_type": self.stratop,
                    "formup": "Amarr",
                },
            )
        rebuild_period(self.period)
        row = stat_for(self.period, self.fc1)
        self.assertEqual((row.fleet_count, row.total_points, row.eligible), (2, Decimal("4.00"), True))
        self.assertEqual(row.calculated_payout, 1000)

    def test_special_role_credit_counts_once_per_fleet(self):
        op1 = closed_op(self.fc1, utc(2026, 3, 1), type_obj=self.stratop)
        op2 = closed_op(self.fc1, utc(2026, 3, 2), type_obj=self.roam)
        add_role(op1, self.fc1, role=Role.BACKSEAT_FC)
        add_role(op1, self.fc2, role=Role.BACKSEAT_FC)
        add_role(op1, self.fc2, role=Role.LOGI_ANCHOR)
        add_role(op2, self.fc2, role=Role.SNOWFLAKE)
        add_role(op2, self.fc3, role=Role.LOGI_ANCHOR, grants_fc_credit=False)

        rebuild_period(self.period)

        fc1, fc2 = stat_for(self.period, self.fc1), stat_for(self.period, self.fc2)
        self.assertEqual((fc1.fleet_count, fc1.total_points), (2, Decimal("3.00")))
        self.assertEqual((fc2.fleet_count, fc2.total_points), (2, Decimal("3.00")))
        self.assertFalse(MonthlyFCStatistic.objects.filter(period=self.period, fc_user=self.fc3).exists())
        self.assertEqual(fc1.calculated_payout + fc2.calculated_payout, 1000)

    def test_remainder_tie_break_goes_to_lowest_user_id(self):
        self.period.budget = 1001
        self.period.save()
        for user in (self.fc3, self.fc2, self.fc1):
            closed_op(user, utc(2026, 3, 1))
            closed_op(user, utc(2026, 3, 2))
        rebuild_period(self.period)
        payouts = {row.fc_user_id: row.calculated_payout for row in self.period.fc_statistics.all()}
        lowest = min(payouts)
        self.assertEqual(payouts[lowest], 335)
        self.assertEqual(sorted(v for k, v in payouts.items() if k != lowest), [333, 333])

    def test_ineligible_points_do_not_dilute_eligible_payouts(self):
        closed_op(self.fc1, utc(2026, 3, 1))
        closed_op(self.fc1, utc(2026, 3, 2))
        heavy = FleetType.objects.create(name="Heavy", point_weight=Decimal("50.00"))
        closed_op(self.fc2, utc(2026, 3, 3), type_obj=heavy)
        rebuild_period(self.period)
        self.assertEqual(stat_for(self.period, self.fc1).calculated_payout, 1000)
        self.assertEqual(stat_for(self.period, self.fc2).calculated_payout, 0)

    def test_rebuild_is_idempotent_and_drops_stale_rows(self):
        closed_op(self.fc1, utc(2026, 3, 1))
        op = closed_op(self.fc2, utc(2026, 3, 2))
        rebuild_period(self.period)
        rebuild_period(self.period)
        self.assertEqual(self.period.fc_statistics.count(), 2)

        op.status = Status.CANCELLED
        op.save()
        rebuild_period(self.period)

        self.assertEqual(list(self.period.fc_statistics.values_list("fc_user_id", flat=True)), [self.fc1.pk])

    def test_rebuild_refuses_finalized_period(self):
        closed_op(self.fc1, utc(2026, 3, 1))
        rebuild_period(self.period)
        finalize_period(self.period, self.fc1)
        closed_op(self.fc1, utc(2026, 3, 2))

        with self.assertRaises(ValueError):
            rebuild_period(self.period)

        self.assertEqual(stat_for(self.period, self.fc1).fleet_count, 1)


class IncentiveStateMachineTests(TestCase):
    def setUp(self):
        f.settings(incentive_enabled=True)
        self.manager = f.create_user("manager", perms=f.FC_LEAD_PERMS)
        self.fc1 = f.create_user("fc1", perms=f.FC_PERMS)
        self.fc2 = f.create_user("fc2", perms=f.FC_PERMS)
        self.period = IncentivePeriod.objects.create(year=2026, month=3, budget=900, minimum_fleets=1)
        weighted = f.fleet_type("StratOp", "2.00")
        closed_op(self.fc1, utc(2026, 3, 1), type_obj=weighted)
        closed_op(self.fc2, utc(2026, 3, 2), type_obj=f.fleet_type("Roam", "1.00"))

    def test_open_period_cannot_be_finalized(self):
        with self.assertRaises(ValueError):
            finalize_period(self.period, self.manager)
        self.period.refresh_from_db()
        self.assertEqual(self.period.status, PeriodStatus.OPEN)

    def test_review_period_can_be_finalized(self):
        rebuild_period(self.period)
        finalize_period(self.period, self.manager)
        self.period.refresh_from_db()
        self.assertEqual(self.period.status, PeriodStatus.FINALIZED)
        self.assertEqual(self.period.finalized_by, self.manager)
        self.assertIsNotNone(self.period.finalized_at)

    def test_finalized_period_cannot_be_finalized_again(self):
        rebuild_period(self.period)
        finalize_period(self.period, self.manager)
        with self.assertRaises(ValueError):
            finalize_period(self.period, self.manager)

    def test_unlock_returns_to_review_and_allows_recalculation(self):
        rebuild_period(self.period)
        finalize_period(self.period, self.manager)

        unlock_period(self.period)

        self.period.refresh_from_db()
        self.assertEqual(self.period.status, PeriodStatus.REVIEW)
        self.assertIsNone(self.period.finalized_at)
        self.assertIsNone(self.period.finalized_by)
        closed_op(self.fc2, utc(2026, 3, 9))
        rebuild_period(self.period)
        self.assertEqual(stat_for(self.period, self.fc2).fleet_count, 2)

    def test_unlock_only_applies_to_finalized_periods(self):
        try:
            unlock_period(self.period)
        except ValueError:
            pass
        self.period.refresh_from_db()
        self.assertEqual(self.period.status, PeriodStatus.OPEN)

    def test_waiver_recalculates_immediately(self):
        rebuild_period(self.period)
        self.assertEqual(stat_for(self.period, self.fc1).calculated_payout, 600)
        self.assertEqual(stat_for(self.period, self.fc2).calculated_payout, 300)

        row = set_waiver(self.period, self.fc1.pk, True)

        self.assertTrue(row.waived)
        self.assertEqual(row.calculated_payout, 0)
        self.assertEqual(stat_for(self.period, self.fc2).calculated_payout, 900)

        set_waiver(self.period, self.fc1.pk, False)
        self.assertEqual(stat_for(self.period, self.fc1).calculated_payout, 600)

    def test_waiver_survives_recalculation(self):
        rebuild_period(self.period)
        set_waiver(self.period, self.fc1.pk, True)
        rebuild_period(self.period)
        row = stat_for(self.period, self.fc1)
        self.assertTrue(row.waived)
        self.assertEqual(row.final_payout, 0)

    def test_waiver_blocked_when_finalized(self):
        rebuild_period(self.period)
        finalize_period(self.period, self.manager)
        with self.assertRaises(ValueError):
            set_waiver(self.period, self.fc1.pk, True)
        self.assertFalse(stat_for(self.period, self.fc1).waived)

    def test_waiver_requires_existing_statistic(self):
        with self.assertRaises(ValueError):
            set_waiver(self.period, self.fc1.pk, True)
        rebuild_period(self.period)
        outsider = f.create_user("outsider")
        with self.assertRaises(ValueError):
            set_waiver(self.period, outsider.pk, True)


# ---------------------------------------------------------------------------
# Incentive views
# ---------------------------------------------------------------------------


class IncentiveViewTests(TestCase):
    def setUp(self):
        f.settings(incentive_enabled=True, incentive_minimum_fleets=4)
        self.manager = f.create_user("manager", perms=f.MEMBER_PERMS + ["fleetops.manage_incentives"])
        self.fc1 = f.create_user("fc1", perms=f.FC_PERMS)
        self.fc2 = f.create_user("fc2", perms=f.FC_PERMS)
        closed_op(self.fc1, utc(2026, 3, 1), type_obj=f.fleet_type("StratOp", "2.00"))
        closed_op(self.fc2, utc(2026, 3, 2), type_obj=f.fleet_type("Roam", "1.00"))
        self.period = IncentivePeriod.objects.create(year=2026, month=3, budget=300, minimum_fleets=1)
        self.client.force_login(self.manager)

    def post(self, name, *args, data=None):
        return self.client.post(reverse(f"fleetops:{name}", args=args), data or {})

    def test_review_page_for_explicit_month(self):
        response = self.client.get(reverse("fleetops:incentive_review"), {"year": 2026, "month": 3})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["period"], self.period)

    def test_review_page_offers_period_creation_with_default_minimum(self):
        response = self.client.get(reverse("fleetops:incentive_review"), {"year": 2026, "month": 4})
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.context["period"])
        self.assertEqual(response.context["form"].initial["minimum_fleets"], 4)

    def test_create_period_is_audited(self):
        response = self.client.post(
            reverse("fleetops:incentive_review") + "?year=2026&month=4",
            {"create_period": "1", "year": 2026, "month": 4, "budget": 5_000_000_000, "minimum_fleets": 3},
        )
        period = IncentivePeriod.objects.get(year=2026, month=4)
        self.assertRedirects(response, reverse("fleetops:incentive_review") + "?year=2026&month=4", fetch_redirect_response=False)
        self.assertEqual(period.status, PeriodStatus.OPEN)
        entry = AuditLog.objects.get(action="incentive.period_create")
        self.assertEqual(entry.actor, self.manager)
        self.assertEqual(entry.new_value, {"budget": 5_000_000_000, "minimum_fleets": 3})

    def test_duplicate_period_is_rejected(self):
        response = self.client.post(
            reverse("fleetops:incentive_review") + "?year=2026&month=3",
            {"create_period": "1", "year": 2026, "month": 3, "budget": 1, "minimum_fleets": 1},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(IncentivePeriod.objects.filter(year=2026, month=3).count(), 1)

    def test_recalculate_finalize_unlock_are_audited(self):
        self.post("incentive_recalculate", self.period.pk)
        self.period.refresh_from_db()
        self.assertEqual(self.period.status, PeriodStatus.REVIEW)
        self.assertEqual(self.period.fc_statistics.count(), 2)

        self.post("incentive_finalize", self.period.pk)
        self.period.refresh_from_db()
        self.assertEqual(self.period.status, PeriodStatus.FINALIZED)
        self.assertEqual(self.period.finalized_by, self.manager)

        self.post("incentive_unlock", self.period.pk)
        self.period.refresh_from_db()
        self.assertEqual(self.period.status, PeriodStatus.REVIEW)

        self.assertEqual(
            audit_actions(self.period),
            ["incentive.recalculate", "incentive.finalize", "incentive.unlock"],
        )
        recalc = AuditLog.objects.get(action="incentive.recalculate")
        self.assertEqual((recalc.old_value, recalc.new_value), ({"status": "open"}, {"status": "review"}))
        self.assertTrue(all(e.actor == self.manager for e in AuditLog.objects.filter(action__startswith="incentive.")))

    def test_waiver_view_recalculates_and_audits(self):
        rebuild_period(self.period)

        response = self.post("incentive_waiver", self.period.pk, self.fc1.pk, data={"waived": "1"})

        self.assertRedirects(response, reverse("fleetops:incentive_review") + "?year=2026&month=3", fetch_redirect_response=False)
        self.assertTrue(stat_for(self.period, self.fc1).waived)
        self.assertEqual(stat_for(self.period, self.fc2).final_payout, 300)
        entry = AuditLog.objects.get(action="incentive.waiver")
        self.assertEqual((entry.old_value, entry.new_value), ({"waived": False}, {"waived": True}))
        self.assertEqual(entry.object_type, "MonthlyFCStatistic")

        self.post("incentive_waiver", self.period.pk, self.fc1.pk, data={"waived": "0"})
        self.assertFalse(stat_for(self.period, self.fc1).waived)
        self.assertEqual(stat_for(self.period, self.fc1).final_payout, 200)

    def test_waiver_view_blocked_when_finalized(self):
        rebuild_period(self.period)
        finalize_period(self.period, self.manager)

        response = self.post("incentive_waiver", self.period.pk, self.fc1.pk, data={"waived": "1"})

        self.assertEqual(response.status_code, 302)
        self.assertFalse(stat_for(self.period, self.fc1).waived)
        self.assertFalse(AuditLog.objects.filter(action="incentive.waiver").exists())

    def test_waiver_for_unknown_fc_is_404(self):
        rebuild_period(self.period)
        response = self.post("incentive_waiver", self.period.pk, self.manager.pk, data={"waived": "1"})
        self.assertEqual(response.status_code, 404)

    def test_incentive_pages_require_manage_incentives(self):
        self.client.force_login(f.create_user("lead", perms=f.FC_PERMS + ["fleetops.view_all_stats"]))
        self.assertEqual(self.client.get(reverse("fleetops:incentive_review")).status_code, 403)
        for name, args in [
            ("incentive_recalculate", (self.period.pk,)),
            ("incentive_finalize", (self.period.pk,)),
            ("incentive_unlock", (self.period.pk,)),
            ("incentive_waiver", (self.period.pk, self.fc1.pk)),
        ]:
            with self.subTest(view=name):
                self.assertEqual(self.post(name, *args).status_code, 403)
        self.period.refresh_from_db()
        self.assertEqual(self.period.status, PeriodStatus.OPEN)
        self.assertFalse(MonthlyFCStatistic.objects.exists())

    def test_incentive_actions_require_post(self):
        for name in ("incentive_recalculate", "incentive_finalize", "incentive_unlock"):
            with self.subTest(view=name):
                self.assertEqual(self.client.get(reverse(f"fleetops:{name}", args=[self.period.pk])).status_code, 405)

    def test_recalculate_finalized_period_is_rejected_gracefully(self):
        rebuild_period(self.period)
        finalize_period(self.period, self.manager)
        self.client.raise_request_exception = False

        response = self.post("incentive_recalculate", self.period.pk)

        self.assertEqual(response.status_code, 302)
        self.period.refresh_from_db()
        self.assertEqual(self.period.status, PeriodStatus.FINALIZED)

    def test_finalize_open_period_is_rejected_gracefully(self):
        self.client.raise_request_exception = False

        response = self.post("incentive_finalize", self.period.pk)

        self.assertEqual(response.status_code, 302)
        self.period.refresh_from_db()
        self.assertEqual(self.period.status, PeriodStatus.OPEN)
        self.assertFalse(AuditLog.objects.filter(action="incentive.finalize").exists())

    def test_unlock_open_period_does_nothing(self):
        self.client.raise_request_exception = False

        self.post("incentive_unlock", self.period.pk)

        self.period.refresh_from_db()
        self.assertEqual(self.period.status, PeriodStatus.OPEN)
        self.assertFalse(AuditLog.objects.filter(action="incentive.unlock").exists())

    def test_waiver_after_fc_lost_all_fleets_does_not_crash(self):
        rebuild_period(self.period)
        FleetOperation.objects.filter(fc_user=self.fc1).update(status=Status.CANCELLED)
        self.client.raise_request_exception = False

        response = self.post("incentive_waiver", self.period.pk, self.fc1.pk, data={"waived": "1"})

        self.assertEqual(response.status_code, 302)
        self.assertEqual(stat_for(self.period, self.fc2).final_payout, 300)


class IncentiveDisabledTests(TestCase):
    def setUp(self):
        f.settings(incentive_enabled=False)
        self.manager = f.create_user("manager", perms=f.FC_LEAD_PERMS)
        self.fc = f.create_user("fc", perms=f.FC_PERMS)
        closed_op(self.fc, utc(2026, 3, 1))
        self.period = IncentivePeriod.objects.create(year=2026, month=3, budget=300, minimum_fleets=1)
        self.client.force_login(self.manager)

    def test_enabled_flag_is_persisted(self):
        self.assertFalse(f.settings().incentive_enabled)
        self.assertTrue(f.settings(incentive_enabled=True).incentive_enabled)

    def test_review_page_unavailable_when_disabled(self):
        response = self.client.get(reverse("fleetops:incentive_review"), {"year": 2026, "month": 3})
        self.assertIn(response.status_code, (302, 403, 404))

    def test_nothing_is_calculated_when_disabled(self):
        self.client.raise_request_exception = False
        self.client.post(reverse("fleetops:incentive_recalculate", args=[self.period.pk]))
        try:
            rebuild_period(self.period)
        except (ValueError, PermissionError):
            pass
        self.period.refresh_from_db()
        self.assertEqual(self.period.status, PeriodStatus.OPEN)
        self.assertFalse(MonthlyFCStatistic.objects.exists())

    def test_navigation_hides_incentive_link_when_disabled(self):
        response = self.client.get(reverse("fleetops:my_statistics"))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, reverse("fleetops:incentive_review"))

    def test_navigation_shows_incentive_link_when_enabled(self):
        f.settings(incentive_enabled=True)
        response = self.client.get(reverse("fleetops:my_statistics"))
        self.assertContains(response, reverse("fleetops:incentive_review"))


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

NOW = utc(2026, 5, 20, 12, 0)


class DashboardMetricsTests(TestCase):
    def setUp(self):
        patcher = mock.patch("django.utils.timezone.now", return_value=NOW)
        patcher.start()
        self.addCleanup(patcher.stop)

        self.fc = f.create_user("fc", perms=f.FC_PERMS)
        self.pilot = f.create_user("pilot", perms=f.MEMBER_PERMS)
        self.alt = f.add_alt(self.pilot, "Pilot Alt")
        self.stratop = FleetType.objects.create(name="StratOp", point_weight=Decimal("2"), sort_order=1)
        self.roam = FleetType.objects.create(name="Roam", point_weight=Decimal("1"), sort_order=2)

    def test_cards_cover_month_30_and_90_days(self):
        this_month = closed_op(self.fc, utc(2026, 5, 10), type_obj=self.stratop)
        last_month = closed_op(self.fc, utc(2026, 4, 25), type_obj=self.stratop)
        within_90 = closed_op(self.fc, utc(2026, 3, 15), type_obj=self.stratop)
        too_old = closed_op(self.fc, utc(2026, 1, 10), type_obj=self.stratop)
        f.add_attendance(this_month, self.pilot)
        f.add_attendance(this_month, self.pilot, self.alt, granted=False, capped=True)
        f.add_attendance(last_month, self.pilot, value=2)
        f.add_attendance(within_90, self.pilot)
        f.add_attendance(too_old, self.pilot)
        f.add_attendance(this_month, self.fc)

        cards = dashboard_metrics(self.pilot)["cards"]

        self.assertEqual([c["fleet_type"] for c in cards], [self.stratop, self.roam])
        self.assertEqual((cards[0]["month"], cards[0]["last30"], cards[0]["last90"]), (1, 3, 4))
        self.assertEqual((cards[1]["month"], cards[1]["last30"], cards[1]["last90"]), (0, 0, 0))

    def test_month_card_starts_at_first_of_month_utc(self):
        first_second = closed_op(self.fc, utc(2026, 5, 1, 0, 0, 0), type_obj=self.stratop)
        last_second = closed_op(self.fc, utc(2026, 4, 30, 23, 59, 59), type_obj=self.stratop)
        f.add_attendance(first_second, self.pilot)
        f.add_attendance(last_second, self.pilot, value=5)

        card = dashboard_metrics(self.pilot)["cards"][0]

        self.assertEqual(card["month"], 1)
        self.assertEqual(card["last30"], 6)

    def test_cards_limited_to_four_active_types_in_sort_order(self):
        FleetType.objects.create(name="Inactive", is_active=False, sort_order=0)
        FleetType.objects.create(name="Mining", sort_order=3)
        FleetType.objects.create(name="Home Defense", sort_order=4)
        FleetType.objects.create(name="Training", sort_order=5)

        names = [c["fleet_type"].name for c in dashboard_metrics(self.pilot)["cards"]]

        self.assertEqual(names, ["StratOp", "Roam", "Mining", "Home Defense"])

    def test_recent_participations_group_alts_and_keep_latest_ten(self):
        ops = [closed_op(self.fc, NOW - timedelta(days=i + 1), type_obj=self.roam) for i in range(12)]
        for op in ops:
            f.add_attendance(op, self.pilot)
        f.add_attendance(ops[0], self.pilot, self.alt, value=2)

        recent = dashboard_metrics(self.pilot)["recent"]

        self.assertEqual([r["operation"] for r in recent], ops[:10])
        self.assertEqual(recent[0]["attendance"], 3)
        self.assertCountEqual(recent[0]["character_names"], [f.main_of(self.pilot).character_name, "Pilot Alt"])
        self.assertEqual(recent[1]["attendance"], 1)

    def test_recent_excludes_non_granted_rows(self):
        op = closed_op(self.fc, NOW - timedelta(days=1))
        f.add_attendance(op, self.pilot, granted=False, capped=True)
        self.assertEqual(dashboard_metrics(self.pilot)["recent"], [])

    def test_top_ships_counted_per_fleet_and_capped_at_eight(self):
        ops = [closed_op(self.fc, NOW - timedelta(days=i + 1)) for i in range(3)]
        main = f.main_of(self.pilot)
        add_state(ops[0], self.pilot, main, ship_type_id=1, ship_type_name="Alpha")
        add_state(ops[0], self.pilot, self.alt, ship_type_id=1, ship_type_name="Alpha")
        add_state(ops[1], self.pilot, main, ship_type_id=1, ship_type_name="Alpha")
        add_state(ops[2], self.pilot, main, ship_type_id=1, ship_type_name="Alpha")
        add_state(ops[1], self.pilot, self.alt, ship_type_id=2, ship_type_name="Bravo")
        add_state(ops[2], self.pilot, self.alt, ship_type_id=2, ship_type_name="Bravo")
        for i in range(8):
            op = closed_op(self.fc, NOW - timedelta(days=10 + i))
            add_state(op, self.pilot, main, ship_type_id=100 + i, ship_type_name=f"Ship {i}")
        other = f.create_user("other")
        add_state(ops[0], other, f.main_of(other), ship_type_id=999, ship_type_name="Zeta")

        top = dashboard_metrics(self.pilot)["top_ships"]

        self.assertEqual(len(top), 8)
        self.assertEqual([(s["ship_type_name"], s["uses"]) for s in top[:3]], [("Alpha", 3), ("Bravo", 2), ("Ship 0", 1)])
        self.assertNotIn("Zeta", [s["ship_type_name"] for s in top])
        self.assertTrue(top[0]["image_url"].startswith("https://images.evetech.net/types/1/"))

    def test_hidden_when_main_left_configured_alliances(self):
        op = closed_op(self.fc, utc(2026, 5, 10), type_obj=self.stratop)
        f.add_attendance(op, self.pilot)
        f.settings(history_alliance_ids=str(f.FOREIGN_ALLIANCE[0]))
        metrics = dashboard_metrics(self.pilot)
        self.assertEqual(metrics["cards"][0]["month"], 0)
        self.assertEqual(metrics["recent"], [])


class DashboardViewTests(TestCase):
    def setUp(self):
        patcher = mock.patch("django.utils.timezone.now", return_value=NOW)
        patcher.start()
        self.addCleanup(patcher.stop)

        self.fc = f.create_user("fc", perms=f.FC_PERMS)
        self.other_fc = f.create_user("otherfc", perms=f.FC_PERMS)
        self.pilot = f.create_user("pilot", perms=f.MEMBER_PERMS)
        self.stratop = f.fleet_type("StratOp", "2.00")
        self.mine = f.create_operation(self.fc, started_at=NOW - timedelta(hours=1), type_obj=self.stratop)
        self.joined = f.create_operation(self.other_fc, started_at=NOW - timedelta(hours=2))
        self.foreign = f.create_operation(self.other_fc, started_at=NOW - timedelta(hours=3))
        f.add_attendance(self.joined, self.pilot)

    def active_for(self, user):
        self.client.force_login(user)
        response = self.client.get(reverse("fleetops:dashboard"))
        self.assertEqual(response.status_code, 200)
        return response, set(response.context["active_operations"])

    def test_requires_basic_access(self):
        self.client.force_login(f.create_user("nobody"))
        self.assertEqual(self.client.get(reverse("fleetops:dashboard")).status_code, 403)

    def test_member_sees_only_involved_active_fleets(self):
        _, active = self.active_for(self.pilot)
        self.assertEqual(active, {self.joined})

    def test_fc_sees_own_active_fleets(self):
        own_fleet_fc = f.create_user(
            "ownfleetfc", perms=["fleetops.basic_access", "fleetops.start_fleet", "fleetops.manage_own_fleet"]
        )
        mine = f.create_operation(own_fleet_fc, started_at=NOW - timedelta(minutes=30))
        _, active = self.active_for(own_fleet_fc)
        self.assertEqual(active, {mine})

    def test_closed_fleets_not_listed_as_active(self):
        self.joined.status = Status.CLOSED
        self.joined.save()
        _, active = self.active_for(self.pilot)
        self.assertEqual(active, set())

    def test_fleet_manager_sees_all_active_fleets(self):
        manager = f.create_user("manager", perms=f.MEMBER_PERMS + ["fleetops.manage_fleets"])
        _, active = self.active_for(manager)
        self.assertEqual(active, {self.mine, self.joined, self.foreign})

    def test_active_member_count(self):
        add_state(self.mine, self.pilot, f.main_of(self.pilot), ship_type_id=1, ship_type_name="Alpha")
        gone = add_state(self.mine, self.fc, f.main_of(self.fc), ship_type_id=1, ship_type_name="Alpha")
        gone.is_active = False
        gone.save()
        response, _ = self.active_for(self.fc)
        self.assertEqual(response.context["active_operations"][0].active_member_count, 1)

    def test_view_all_stats_alone_does_not_list_other_fleets(self):
        analyst = f.create_user("analyst", perms=f.MEMBER_PERMS + ["fleetops.view_all_stats"])
        _, active = self.active_for(analyst)
        self.assertEqual(active, set())

    def test_view_all_fleets_lists_every_active_fleet(self):
        viewer = f.create_user("viewer", perms=f.MEMBER_PERMS + ["fleetops.view_all_fleets"])
        _, active = self.active_for(viewer)
        self.assertEqual(active, {self.mine, self.joined, self.foreign})

    def test_fc_month_summary(self):
        roam = f.fleet_type("Roam", "1.00")
        closed_op(self.fc, utc(2026, 5, 2), type_obj=roam)
        closed_op(self.fc, utc(2026, 4, 30, 23), type_obj=roam)
        add_role(self.joined, self.fc)

        response, _ = self.active_for(self.fc)

        fc_stats = response.context["fc_stats"]
        self.assertEqual(fc_stats["fleet_count"], 3)
        self.assertAlmostEqual(fc_stats["total_points"], 5.0)
        self.assertEqual(fc_stats["breakdown"]["StratOp"]["count"], 2)
        self.assertContains(response, "5.00")

    def test_dashboard_cards_rendered(self):
        f.add_attendance(self.mine, self.pilot, value=2)
        response, _ = self.active_for(self.pilot)
        cards = response.context["dashboard_cards"]
        self.assertEqual(cards[0]["month"], 3)
        self.assertContains(response, "StratOp attendance this month")
