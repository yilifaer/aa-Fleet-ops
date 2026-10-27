# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""FC statistics: the rows shared by the FC pages and the incentive periods, and their pages."""

from collections import defaultdict
from datetime import datetime, timezone as dt_timezone
from decimal import Decimal

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from fleetops.models import FleetOperation, IncentivePeriod, MonthlyFCStatistic, OperationRoleAssignment, ReviewStatus
from fleetops.services.counting import counted_operations
from fleetops.services.history import current_member_user_ids
from fleetops.services.incentives import rebuild_period
from fleetops.services.periods import month_bounds
from fleetops.services.statistics import (
    credited_operations,
    credited_user_ids,
    fc_breakdown,
    fc_points,
    fc_rows,
    fc_statistics,
    group_operations_by_fc,
)

from . import factories as f

UTC = dt_timezone.utc
Status = FleetOperation.Status
Role = OperationRoleAssignment.Role
MARCH = (2026, 3)


def utc(*args):
    return datetime(*args, tzinfo=UTC)


def closed_op(fc_user, started_at, *, type_obj=None, **extra):
    return f.create_operation(fc_user, status=Status.CLOSED, started_at=started_at, type_obj=type_obj, **extra)


def credit(operation, user, *, role=Role.BACKSEAT_FC, grants_fc_credit=True, character=None):
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


class FcFixtureMixin:
    def setUp(self):
        super().setUp()
        self.strat = f.fleet_type("StratOp", "2.50")
        self.roam = f.fleet_type("Roam", "1.15")
        self.fc = f.member("alpha", perms=f.FC_PERMS, main_name="Alpha FC")
        self.co_fc = f.member("beta", perms=f.FC_PERMS, main_name="Beta FC")
        self.third = f.member("gamma", perms=f.FC_PERMS, main_name="Gamma FC")
        self.op1 = closed_op(self.fc, utc(2026, 3, 2, 18), type_obj=self.strat)
        self.op2 = closed_op(self.fc, utc(2026, 3, 9, 18), type_obj=self.roam, attendance_multiplier=2)
        self.op3 = closed_op(self.co_fc, utc(2026, 3, 16, 18), type_obj=self.strat, attendance_multiplier=3)
        credit(self.op1, self.co_fc)
        credit(self.op3, self.third)

    def rows(self):
        return {row.user.pk: row for row in fc_rows(*MARCH)}


class FcRowsTests(FcFixtureMixin, TestCase):
    def test_fleets_points_and_names(self):
        rows = self.rows()

        alpha, beta, gamma = (rows[user.pk] for user in (self.fc, self.co_fc, self.third))
        self.assertEqual((alpha.main_name, alpha.fleets), ("Alpha FC", 2))
        # 2.50 x 1 + 1.15 x 2
        self.assertEqual(alpha.points, Decimal("4.80"))
        # Beta runs op3 (2.50 x 3) and is credited on op1 (2.50 x 1).
        self.assertEqual((beta.fleets, beta.points), (2, Decimal("10.00")))
        self.assertEqual((gamma.fleets, gamma.points), (1, Decimal("7.50")))
        self.assertEqual(alpha.user_id, self.fc.pk)

    def test_rows_are_sorted_by_points(self):
        self.assertEqual([row.main_name for row in fc_rows(*MARCH)], ["Beta FC", "Gamma FC", "Alpha FC"])

    def test_equal_points_are_sorted_by_fleets_then_name(self):
        heavy = f.fleet_type("Heavy", "5.00")
        light = f.fleet_type("Light", "2.50")
        zed = f.member("zed", perms=f.FC_PERMS, main_name="Zed FC")
        amy = f.member("amy", perms=f.FC_PERMS, main_name="Amy FC")
        bert = f.member("bert", perms=f.FC_PERMS, main_name="bert FC")
        carl = f.member("carl", perms=f.FC_PERMS, main_name="Carl FC")
        closed_op(amy, utc(2026, 4, 2, 18), type_obj=heavy)
        for day in (3, 4):
            closed_op(zed, utc(2026, 4, day, 18), type_obj=light)
        closed_op(bert, utc(2026, 4, 5, 18), type_obj=heavy)
        closed_op(carl, utc(2026, 4, 6, 18), type_obj=heavy)

        names = [row.main_name for row in fc_rows(2026, 4)]

        # 5 points each: two fleets first, then the names without regard to the case.
        self.assertEqual(names, ["Zed FC", "Amy FC", "bert FC", "Carl FC"])

    def test_breakdown_by_fleet_type_in_type_order(self):
        rows = self.rows()

        self.assertEqual(list(rows[self.fc.pk].breakdown), ["Roam", "StratOp"])
        self.assertEqual(rows[self.fc.pk].breakdown["Roam"], {"count": 1, "points": Decimal("2.30")})
        self.assertEqual(rows[self.co_fc.pk].breakdown, {"StratOp": {"count": 2, "points": Decimal("10.00")}})

    def test_rows_equal_the_per_user_statistics(self):
        for user in (self.fc, self.co_fc, self.third):
            with self.subTest(user=user.username):
                row = self.rows()[user.pk]
                stat = fc_statistics(user, *MARCH)
                self.assertEqual(row.fleets, stat["fleet_count"])
                self.assertAlmostEqual(float(row.points), stat["total_points"])
                self.assertEqual(set(row.breakdown), set(stat["breakdown"]))
                for name, entry in row.breakdown.items():
                    self.assertEqual(entry["count"], stat["breakdown"][name]["count"])
                    self.assertAlmostEqual(float(entry["points"]), stat["breakdown"][name]["points"])

    def test_a_credit_is_counted_once_per_fleet(self):
        credit(self.op1, self.fc, role=Role.LOGI_ANCHOR)
        credit(self.op1, self.co_fc, role=Role.SNOWFLAKE)
        credit(self.op1, self.co_fc, role=Role.LOGI_ANCHOR, character=f.add_alt(self.co_fc, "Beta Alt"))

        rows = self.rows()

        self.assertEqual(rows[self.fc.pk].fleets, 2)
        self.assertEqual(rows[self.co_fc.pk].fleets, 2)
        self.assertEqual(rows[self.co_fc.pk].points, Decimal("10.00"))

    def test_roles_without_credit_give_none(self):
        member = f.member("plain", perms=f.MEMBER_PERMS)
        credit(self.op1, member, role=Role.LOGI_ANCHOR, grants_fc_credit=False)

        self.assertNotIn(member.pk, self.rows())

    def test_a_role_gives_credit_without_any_permission(self):
        member = f.member("plain", perms=f.MEMBER_PERMS, main_name="Plain Pilot")
        credit(self.op2, member)

        row = self.rows()[member.pk]

        self.assertEqual((row.main_name, row.fleets, row.points), ("Plain Pilot", 1, Decimal("2.30")))

    def test_points_are_the_weight_times_the_multiplier(self):
        # The weight is the one recorded with the fleet, not the type's weight today.
        self.strat.point_weight = Decimal("9.00")
        self.strat.save()

        self.assertEqual(self.rows()[self.third.pk].points, Decimal("7.50"))
        self.assertEqual(self.op3.fc_points, Decimal("7.50"))

    def test_a_pending_multiplier_counts_at_one(self):
        pending = closed_op(self.third, utc(2026, 3, 18, 18), type_obj=self.strat, multiplier_pending=True)

        self.assertEqual(pending.fc_points, Decimal("2.50"))
        self.assertEqual(self.rows()[self.third.pk].points, Decimal("10.00"))

    def test_only_counted_fleets_are_credited(self):
        idle = f.member("idle", perms=f.FC_PERMS)
        for status in (Status.DRAFT, Status.CANCELLED, Status.ACTIVE, Status.ENDING, Status.ERROR):
            f.create_operation(idle, status=status, started_at=utc(2026, 3, 5, 18))
        closed_op(idle, utc(2026, 3, 6, 18), review_status=ReviewStatus.PENDING)
        closed_op(idle, utc(2026, 3, 7, 18), review_status=ReviewStatus.REJECTED)
        closed_op(idle, utc(2026, 3, 8, 18), counts_for_stats=False)

        self.assertNotIn(idle.pk, self.rows())

    def test_the_month_is_the_utc_month(self):
        late = f.member("late", perms=f.FC_PERMS)
        closed_op(late, utc(2026, 3, 31, 23, 59, 59))
        closed_op(late, utc(2026, 4, 1, 0, 0, 0))

        self.assertEqual(self.rows()[late.pk].fleets, 1)
        self.assertEqual({row.user.pk: row for row in fc_rows(2026, 4)}[late.pk].fleets, 1)

    def test_fcs_who_left_the_member_alliances_are_hidden(self):
        f.settings(history_alliance_ids=str(f.DEFAULT_ALLIANCE[0]))
        departed = f.member("departed", perms=f.FC_PERMS, alliance=f.FOREIGN_ALLIANCE, corporation=f.OTHER_CORP)
        credit(self.op1, departed)
        closed_op(departed, utc(2026, 3, 4, 18))

        rows = self.rows()

        self.assertNotIn(departed.pk, rows)
        self.assertEqual(rows[self.fc.pk].fleets, 2)

    def test_a_fc_without_a_main_is_listed_by_user_name(self):
        nomad = f.member("nomad", perms=f.FC_PERMS, with_main=False)
        f.create_operation(
            nomad,
            fc_character=f.create_character("Nomad Pilot"),
            status=Status.CLOSED,
            started_at=utc(2026, 3, 11, 18),
            fc_main_character_id=None,
        )

        self.assertEqual(self.rows()[nomad.pk].main_name, "nomad")

    def test_an_empty_month(self):
        self.assertEqual(fc_rows(2025, 1), [])


class CreditHelperTests(FcFixtureMixin, TestCase):
    def test_credited_user_ids(self):
        credit(self.op1, self.third)
        credit(self.op1, self.fc, role=Role.LOGI_ANCHOR)
        credit(self.op1, self.third, role=Role.SNOWFLAKE, grants_fc_credit=False)
        operation = next(op for op in credited_operations(*MARCH) if op.pk == self.op1.pk)

        self.assertEqual(credited_user_ids(operation), {self.fc.pk, self.co_fc.pk, self.third.pk})

    def test_credits_without_a_user_or_without_the_switch_are_ignored(self):
        member = f.member("plain", perms=f.MEMBER_PERMS)
        credit(self.op2, member, grants_fc_credit=False)
        orphan = credit(self.op2, member, role=Role.SNOWFLAKE)
        OperationRoleAssignment.objects.filter(pk=orphan.pk).update(auth_user=None)

        # Also with a prefetch that does not filter, as other callers may use.
        operation = FleetOperation.objects.prefetch_related("role_assignments").get(pk=self.op2.pk)

        self.assertEqual(credited_user_ids(operation), {self.fc.pk})

    def test_grouping_limits_to_the_given_members(self):
        operations = credited_operations(*MARCH)

        everyone = group_operations_by_fc(operations)
        limited = group_operations_by_fc(operations, {self.fc.pk, self.third.pk})

        self.assertEqual(set(everyone), {self.fc.pk, self.co_fc.pk, self.third.pk})
        self.assertEqual(set(limited), {self.fc.pk, self.third.pk})
        self.assertEqual([op.pk for op in everyone[self.co_fc.pk]], [self.op1.pk, self.op3.pk])

    def test_points_and_breakdown_of_operations(self):
        operations = credited_operations(*MARCH)

        self.assertEqual(fc_points(operations), Decimal("2.50") + Decimal("2.30") + Decimal("7.50"))
        self.assertEqual(fc_breakdown([]), {})
        self.assertEqual(fc_points([]), Decimal("0"))

    def test_two_queries_for_any_number_of_fleets(self):
        with CaptureQueriesContext(connection) as small:
            credited_operations(*MARCH)
        for index in range(15):
            operation = closed_op(self.fc, utc(2026, 3, 3 + index, 20), type_obj=self.roam)
            credit(operation, self.co_fc)
        with CaptureQueriesContext(connection) as large:
            operations = credited_operations(*MARCH)
        self.assertEqual(len(operations), 18)
        self.assertEqual((len(small), len(large)), (2, 2))


class FcRowsQueryBudgetTests(TestCase):
    def add_fcs(self, start, count):
        strat = f.fleet_type("StratOp", "2.00")
        for index in range(start, start + count):
            fc = f.member(f"fcb{index}", perms=f.FC_PERMS)
            helper = f.member(f"helper{index}")
            for day in range(3):
                operation = closed_op(fc, utc(2026, 3, 2 + day, 18), type_obj=strat)
                credit(operation, helper)

    def count(self, call):
        with CaptureQueriesContext(connection) as queries:
            call()
        return len(queries)

    def test_service(self):
        self.add_fcs(0, 2)
        small = self.count(lambda: fc_rows(*MARCH))
        self.add_fcs(2, 6)
        result = []
        large = self.count(lambda: result.append(fc_rows(*MARCH)))
        self.assertEqual(len(result[0]), 16)
        self.assertEqual(small, large)
        # Fleets, credit roles, users and the member alliance filter.
        self.assertLessEqual(small, 4)

    def test_service_with_the_alliance_filter(self):
        f.settings(history_alliance_ids=str(f.DEFAULT_ALLIANCE[0]))
        self.add_fcs(0, 2)
        small = self.count(lambda: fc_rows(*MARCH))
        self.add_fcs(2, 6)
        self.assertEqual(small, self.count(lambda: fc_rows(*MARCH)))
        self.assertLessEqual(small, 5)

    def test_page(self):
        lead = f.create_user("statslead", perms=f.MEMBER_PERMS + ["fleetops.view_all_stats"])
        self.client.force_login(lead)
        url = reverse("fleetops:all_fc_statistics")

        def page():
            self.assertEqual(self.client.get(url, {"year": 2026, "month": 3}).status_code, 200)

        self.add_fcs(0, 2)
        page()
        small = self.count(page)
        self.add_fcs(2, 6)
        self.assertEqual(small, self.count(page))
        # Alliance Auth's own page chrome (login state, user, permissions, menu) takes about 16 of these.
        self.assertLessEqual(small, 28)


class IncentiveRebuildMatchesTests(FcFixtureMixin, TestCase):
    """The incentive periods are built from the same rows as the FC pages and give the numbers they always gave."""

    def setUp(self):
        super().setUp()
        f.settings(incentive_enabled=True)
        self.period = IncentivePeriod.objects.create(year=2026, month=3, budget=1000, minimum_fleets=2)
        self.erin = f.member("erin", perms=f.FC_PERMS)
        # Shared fleets, a role holder who is also the FC, decimal weights and multipliers.
        closed_op(self.erin, utc(2026, 3, 12, 18), type_obj=self.roam, attendance_multiplier=3)
        credit(self.op2, self.fc, role=Role.LOGI_ANCHOR)
        credit(self.op2, self.co_fc, role=Role.SNOWFLAKE)

    def legacy_rows(self, period):
        start, end = month_bounds(period.year, period.month)
        operations = list(
            counted_operations(FleetOperation.objects.filter(started_at__gte=start, started_at__lt=end))
            .select_related("fc_user", "fleet_type")
            .prefetch_related("role_assignments")
        )
        member_ids = current_member_user_ids()
        grouped = defaultdict(list)
        for op in operations:
            credited = {op.fc_user_id}
            credited.update(
                assignment.auth_user_id
                for assignment in op.role_assignments.all()
                if assignment.grants_fc_credit and assignment.auth_user_id
            )
            if member_ids is not None:
                credited &= member_ids
            for user_id in credited:
                grouped[user_id].append(op)
        rows = {}
        for user_id, ops in grouped.items():
            breakdown = defaultdict(lambda: {"count": 0, "points": 0.0})
            for op in ops:
                breakdown[str(op.fleet_type)]["count"] += 1
                breakdown[str(op.fleet_type)]["points"] += float(op.fc_points)
            rows[user_id] = (len(ops), sum((op.fc_points for op in ops), Decimal("0")), dict(breakdown))
        return rows

    def test_rebuild_gives_the_previous_totals(self):
        expected = self.legacy_rows(self.period)

        rebuild_period(self.period)

        stored = {row.fc_user_id: row for row in MonthlyFCStatistic.objects.filter(period=self.period)}
        self.assertEqual(set(stored), set(expected))
        for user_id, (fleets, points, breakdown) in expected.items():
            with self.subTest(user=user_id):
                self.assertEqual(stored[user_id].fleet_count, fleets)
                self.assertEqual(stored[user_id].total_points, points)
                self.assertEqual(set(stored[user_id].fleet_type_breakdown), set(breakdown))
                for name, entry in breakdown.items():
                    saved = stored[user_id].fleet_type_breakdown[name]
                    self.assertEqual(saved["count"], entry["count"])
                    self.assertAlmostEqual(saved["points"], entry["points"])

    def test_rebuild_and_the_fc_page_agree(self):
        rebuild_period(self.period)

        stored = {row.fc_user_id: row for row in MonthlyFCStatistic.objects.filter(period=self.period)}
        rows = self.rows()
        self.assertEqual(set(stored), set(rows))
        for user_id, row in rows.items():
            self.assertEqual((stored[user_id].fleet_count, stored[user_id].total_points), (row.fleets, row.points))

    def test_rebuild_with_the_alliance_filter(self):
        f.settings(history_alliance_ids=str(f.DEFAULT_ALLIANCE[0]))
        departed = f.member("departed", perms=f.FC_PERMS, alliance=f.FOREIGN_ALLIANCE, corporation=f.OTHER_CORP)
        credit(self.op1, departed)
        expected = self.legacy_rows(self.period)

        rebuild_period(self.period)

        self.assertNotIn(departed.pk, expected)
        self.assertEqual(
            set(MonthlyFCStatistic.objects.filter(period=self.period).values_list("fc_user_id", flat=True)),
            set(expected),
        )

    def test_the_fleet_type_breakdown_is_stored_as_plain_json(self):
        rebuild_period(self.period)

        saved = MonthlyFCStatistic.objects.get(period=self.period, fc_user=self.erin).fleet_type_breakdown

        # Each entry also says how many of its fleets count toward the minimum fleets.
        self.assertEqual(saved, {"Roam": {"count": 1, "points": 3.45, "toward_minimum": 1}})


class FcPageTests(FcFixtureMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.lead = f.create_user("statslead", perms=f.MEMBER_PERMS + ["fleetops.view_all_stats"])

    def get(self, name, *args, user=None, **params):
        self.client.force_login(user or self.lead)
        return self.client.get(reverse(f"fleetops:{name}", args=args), {"year": 2026, "month": 3, **params})

    def test_overview_page(self):
        response = self.get("all_fc_statistics")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Alpha FC")
        self.assertContains(response, "Beta FC")
        self.assertContains(response, "FC points")
        self.assertContains(response, "weight times the PAP multiplier")
        self.assertContains(response, "2026-03 (EVE time)")
        self.assertEqual([row.main_name for row in response.context["rows"]], ["Beta FC", "Gamma FC", "Alpha FC"])
        # Three fleets were flown; two of them are credited to a second FC and so are in two rows.
        self.assertEqual(sum(row.fleets for row in response.context["rows"]), 5)
        self.assertEqual(response.context["total_fleets"], 3)
        # Every credited FC gets the points, so the points add up over the rows.
        self.assertEqual(response.context["total_points"], Decimal("22.30"))
        self.assertContains(response, reverse("fleetops:fc_statistics_detail", args=[self.fc.pk]))

    def test_a_fleet_credited_to_two_fcs_is_one_fleet_in_the_footer(self):
        FleetOperation.objects.all().delete()
        op = closed_op(self.fc, utc(2026, 3, 2, 18), type_obj=self.strat)
        credit(op, self.co_fc)

        response = self.get("all_fc_statistics")

        self.assertEqual([row.fleets for row in response.context["rows"]], [1, 1])
        self.assertEqual(response.context["total_fleets"], 1)
        self.assertEqual(response.context["total_points"], Decimal("5.00"))
        self.assertContains(response, '<td>2 FCs</td><td class="text-end">1</td><td class="text-end">5.00</td>', html=False)

    def test_the_rows_name_the_fleets_they_credit(self):
        rows = self.rows()

        self.assertEqual(rows[self.fc.pk].operation_ids, frozenset({self.op1.pk, self.op2.pk}))
        self.assertEqual(rows[self.co_fc.pk].operation_ids, frozenset({self.op1.pk, self.op3.pk}))
        self.assertEqual(rows[self.third.pk].operation_ids, frozenset({self.op3.pk}))

    def test_the_points_are_shown_with_two_places(self):
        response = self.get("all_fc_statistics")

        self.assertContains(response, "10.00")
        self.assertContains(response, "7.50")
        self.assertContains(response, "4.80")

    def test_export_buttons(self):
        response = self.get("all_fc_statistics")

        self.assertContains(response, reverse("fleetops:export", args=["fcs"]))
        self.assertContains(response, "format=xlsx")
        self.assertContains(response, "format=csv")

    def test_empty_month(self):
        response = self.get("all_fc_statistics", year=2020, month=1)

        self.assertContains(response, "No FC fleets in this month.")

    def test_detail_page_shows_weight_multiplier_and_how_each_fleet_is_credited(self):
        response = self.get("fc_statistics_detail", self.co_fc.pk)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Weight &times; multiplier")
        self.assertContains(response, "2.50 &times; 3")
        self.assertContains(response, "2.50 &times; 1")
        self.assertContains(response, "7.50")
        # op3 is his own fleet, op1 a credit.
        self.assertContains(response, "<td>FC</td>", html=False)
        self.assertContains(response, "<td>Special role</td>", html=False)
        self.assertEqual(response.context["stat"]["fleet_count"], 2)

    def test_detail_page_marks_a_pending_multiplier(self):
        closed_op(self.third, utc(2026, 3, 18, 18), type_obj=self.strat, multiplier_pending=True)

        response = self.get("fc_statistics_detail", self.third.pk)

        self.assertContains(response, "(pending)")

    def test_an_fc_opens_the_own_page_only(self):
        self.assertEqual(self.get("fc_statistics_detail", self.fc.pk, user=self.fc).status_code, 200)
        self.assertEqual(self.get("fc_statistics_detail", self.co_fc.pk, user=self.fc).status_code, 404)
        self.assertEqual(self.get("all_fc_statistics", user=self.fc).status_code, 403)
