# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""The corporation overview and corporation pages: targets, statuses, attribution rules and query budgets."""

from datetime import datetime, timezone as dt_timezone
from decimal import Decimal
from unittest import mock

from allianceauth.eveonline.models import EveCharacter
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from fleetops.models import AttendanceRecord, FleetOperation, ReviewStatus
from fleetops.services.statistics import OTHER_CORPORATIONS, OVERVIEW_SORTS, corporation_overview, corporation_statistics

from . import factories as f

UTC = dt_timezone.utc
Status = FleetOperation.Status

MARCH = (2026, 3)
# The statistics managers of the page tests live in a corporation of their own.
STAFF_CORP = (2_000_777, "Staff Corp")


def utc(*args):
    return datetime(*args, tzinfo=UTC)


def clock(moment):
    """Make "now" a point in EVE time; a patch started before the test client logs in keeps the login valid."""
    return mock.patch("django.utils.timezone.now", return_value=moment)


def by_corporation(overview):
    return {row.corporation_id: row for row in overview.rows}


class AllianceFixtureMixin:
    """Corporation A has three mains, corporation B one; the FC is a main of A without PAP.

    PAP of March 2026: alice 4 (main and an alt, which lives in corporation B), bob 1, carol 3.
    """

    def setUp(self):
        super().setUp()
        self.strat = f.fleet_type("StratOp", "2.00")
        self.roam = f.fleet_type("Roam", "1.00")
        self.fc = f.member("fc", perms=f.FC_PERMS)
        self.alice = f.member("alice")
        self.bob = f.member("bob")
        self.carol = f.member("carol", corporation=f.OTHER_CORP)
        self.alice_alt = f.add_alt(self.alice, "Alice Alt", corporation=f.OTHER_CORP, alliance=f.FOREIGN_ALLIANCE)
        self.op1 = f.closed_operation(self.fc, utc(2026, 3, 5, 18), type_obj=self.strat)
        self.op2 = f.closed_operation(self.fc, utc(2026, 3, 10, 19), type_obj=self.roam)
        f.add_attendance(self.op1, self.alice, value=2)
        f.add_attendance(self.op1, self.alice, self.alice_alt, value=2)
        f.add_attendance(self.op2, self.bob, value=1)
        f.add_attendance(self.op1, self.carol, value=3)

    def overview(self, **kwargs):
        return corporation_overview(*MARCH, **kwargs)


class OverviewDefinitionTests(AllianceFixtureMixin, TestCase):
    def test_rows_totals_and_averages(self):
        overview = self.overview()

        rows = by_corporation(overview)
        self.assertEqual(set(rows), {f.DEFAULT_CORP[0], f.OTHER_CORP[0]})
        a, b = rows[f.DEFAULT_CORP[0]], rows[f.OTHER_CORP[0]]
        self.assertEqual((a.mains, a.mains_with_pap, a.total_pap, a.fleets), (3, 2, 5, 2))
        self.assertEqual(a.average, Decimal("1.67"))
        self.assertEqual((b.mains, b.mains_with_pap, b.total_pap, b.fleets), (1, 1, 3, 1))
        self.assertEqual(b.average, Decimal("3.00"))
        self.assertEqual(a.corporation_name, f.DEFAULT_CORP[1])
        self.assertEqual(a.alliance_id, f.DEFAULT_ALLIANCE[0])
        self.assertIsNone(overview.other)

    def test_alliance_totals(self):
        overview = self.overview()

        self.assertEqual(overview.alliance_pap, 8)
        self.assertEqual(overview.member_pap, 8)
        self.assertEqual((overview.mains, overview.mains_with_pap), (4, 3))
        self.assertEqual(overview.average, Decimal("2.00"))
        self.assertEqual(overview.month.label, "2026-03")

    def test_fleet_type_totals_by_corporation(self):
        overview = self.overview()

        a = by_corporation(overview)[f.DEFAULT_CORP[0]]
        self.assertEqual(a.by_fleet_type, {self.strat.pk: 4, self.roam.pk: 1})
        # Types with the same sort order are listed by name.
        self.assertEqual([fleet_type.name for fleet_type in overview.fleet_types], ["Roam", "StratOp"])

    def test_an_alt_in_another_corporation_or_alliance_counts_for_the_main(self):
        rows = by_corporation(self.overview())

        # Alice's alt lives in corporation B and a foreign alliance; its PAP belongs to alice's corporation A.
        self.assertEqual(rows[f.DEFAULT_CORP[0]].total_pap, 5)
        self.assertEqual(rows[f.OTHER_CORP[0]].total_pap, 3)
        self.assertEqual(rows[f.OTHER_CORP[0]].mains, 1)

    def test_corporation_totals_keep_the_corporation_at_pap_time(self):
        # Dave earned PAP in corporation B and has since moved to corporation A.
        dave = f.member("dave", corporation=f.OTHER_CORP)
        f.add_attendance(self.op2, dave, value=2)
        EveCharacter.objects.filter(pk=f.main_of(dave).pk).update(
            corporation_id=f.DEFAULT_CORP[0], corporation_name=f.DEFAULT_CORP[1]
        )

        rows = by_corporation(self.overview())

        a, b = rows[f.DEFAULT_CORP[0]], rows[f.OTHER_CORP[0]]
        # The total stays where it was earned; the member counts follow the current main.
        self.assertEqual((a.total_pap, a.mains, a.mains_with_pap), (5, 4, 3))
        self.assertEqual((b.total_pap, b.mains, b.mains_with_pap), (5, 1, 1))
        self.assertEqual(b.average, Decimal("5.00"))

    def test_mains_exclude_alts_disabled_accounts_biomassed_mains_and_users_without_a_main(self):
        f.member("gone", active=False)
        f.member("biomass", corporation=f.DOOMHEIM_CORP)
        f.member("nomain", with_main=False)

        overview = self.overview()

        self.assertEqual(overview.mains, 4)
        self.assertEqual(by_corporation(overview)[f.DEFAULT_CORP[0]].mains, 3)
        self.assertNotIn(f.DOOMHEIM_CORP[0], by_corporation(overview))

    def test_a_corporation_with_mains_and_no_pap_is_listed(self):
        f.member("quiet", corporation=f.THIRD_CORP)

        row = by_corporation(self.overview())[f.THIRD_CORP[0]]

        self.assertEqual((row.mains, row.mains_with_pap, row.total_pap, row.average), (1, 0, 0, Decimal("0.00")))

    def test_pap_outside_the_member_corporations_is_summed_in_one_row(self):
        # Ghost's account was disabled; a deleted user's PAP has no corporation.
        ghost = f.member("ghost", corporation=f.THIRD_CORP)
        f.add_attendance(self.op1, ghost, value=4)
        ghost.is_active = False
        ghost.save(update_fields=["is_active"])
        orphan = f.add_attendance(self.op2, self.bob, f.add_alt(self.bob, "Bob Alt"), value=1)
        AttendanceRecord.objects.filter(pk=orphan.pk).update(auth_user=None, corporation_id=None)

        overview = self.overview()

        other = overview.other
        self.assertTrue(other.is_other)
        self.assertEqual(other.corporation_name, OTHER_CORPORATIONS)
        self.assertEqual((other.total_pap, other.fleets, other.mains), (5, 2, 0))
        self.assertEqual(other.by_fleet_type, {self.strat.pk: 4, self.roam.pk: 1})
        self.assertIsNone(other.corporation_id)
        self.assertEqual(other.status, "none")
        # The columns add up to the alliance total, and the ghost is no member.
        self.assertEqual(sum(row.total_pap for row in overview.rows) + other.total_pap, overview.alliance_pap)
        self.assertEqual(overview.alliance_pap, 8 + 5)
        self.assertEqual(overview.member_pap, 8)
        self.assertEqual(overview.mains, 4)

    def test_a_fleet_with_pap_from_two_outside_corporations_is_one_fleet_in_the_other_row(self):
        ghosts = [f.member("ghost_a", corporation=f.THIRD_CORP), f.member("ghost_b", corporation=STAFF_CORP)]
        for ghost, value in zip(ghosts, (2, 3)):
            f.add_attendance(self.op2, ghost, value=value)
            ghost.is_active = False
            ghost.save(update_fields=["is_active"])

        other = self.overview().other

        self.assertEqual((other.total_pap, other.fleets), (5, 1))

    def test_the_alliance_filter_hides_departed_users_and_their_corporations(self):
        f.settings(history_alliance_ids=str(f.DEFAULT_ALLIANCE[0]))
        departed = f.member("departed", corporation=f.THIRD_CORP, alliance=f.FOREIGN_ALLIANCE)
        f.add_attendance(self.op1, departed, value=9)

        overview = self.overview()

        self.assertNotIn(f.THIRD_CORP[0], by_corporation(overview))
        self.assertIsNone(overview.other)
        self.assertEqual(overview.alliance_pap, 8)
        self.assertEqual(overview.mains, 4)
        self.assertTrue(overview.alliance_filter_configured)

    def test_without_member_alliance_ids_everybody_is_a_member(self):
        f.member("outsider", corporation=f.THIRD_CORP, alliance=f.FOREIGN_ALLIANCE)

        overview = self.overview()

        self.assertFalse(overview.alliance_filter_configured)
        self.assertIn(f.THIRD_CORP[0], by_corporation(overview))
        self.assertEqual(overview.mains, 5)

    def test_fleets_that_do_not_count_are_left_out_and_summarised(self):
        shorter = f.closed_operation(self.fc, utc(2026, 3, 12, 18), type_obj=self.strat, counts_for_stats=False)
        pending = f.closed_operation(self.fc, utc(2026, 3, 13, 18), review_status=ReviewStatus.PENDING)
        rejected = f.closed_operation(self.fc, utc(2026, 3, 14, 18), review_status=ReviewStatus.REJECTED)
        running = f.create_operation(self.fc, status=Status.ACTIVE, started_at=utc(2026, 3, 15, 18))
        for operation in (shorter, pending, rejected, running):
            f.add_attendance(operation, self.alice, value=5)

        overview = self.overview()

        self.assertEqual(overview.alliance_pap, 8)
        self.assertEqual(overview.not_counted, {"excluded": 1, "pending": 1, "rejected": 1, "running": 1})

    def test_pap_that_is_not_granted_does_not_count(self):
        f.add_attendance(self.op2, self.carol, value=7, granted=False)

        self.assertEqual(self.overview().alliance_pap, 8)

    def test_the_month_is_utc(self):
        late = f.closed_operation(self.fc, utc(2026, 3, 31, 23, 30))
        f.add_attendance(late, self.bob, value=6)
        for zone in ("Europe/Berlin", "Asia/Shanghai", "America/Los_Angeles"):
            with self.subTest(zone=zone), override_settings(TIME_ZONE=zone):
                self.assertEqual(corporation_overview(2026, 3).alliance_pap, 14)
                self.assertEqual(corporation_overview(2026, 4).alliance_pap, 0)

    def test_future_month_is_empty_without_statuses(self):
        f.statistics_target(2026, 1, "2.00")
        with clock(utc(2026, 3, 20, 12)):
            overview = corporation_overview(2026, 6)

        self.assertEqual(overview.alliance_pap, 0)
        self.assertEqual({row.status for row in overview.rows}, {"none"})
        self.assertEqual(overview.corporations_with_target, 0)

    def test_half_up_on_the_third_decimal(self):
        for index in range(7):
            f.member(f"extra{index}", corporation=f.THIRD_CORP)
        pilot = f.member("pilot", corporation=f.THIRD_CORP)
        f.add_attendance(self.op1, pilot, value=5)

        row = by_corporation(self.overview())[f.THIRD_CORP[0]]

        # 5 / 8 = 0.625 rounds up to 0.63 (half even would give 0.62).
        self.assertEqual((row.mains, row.total_pap), (8, 5))
        self.assertEqual(row.average, Decimal("0.63"))


class OverviewStatusTests(AllianceFixtureMixin, TestCase):
    """Green when the target is reached, amber while the month runs, red only for finished months below the target."""

    def setUp(self):
        super().setUp()
        # Corporation A: 5 PAP over 3 mains (1.67); corporation B: 3 PAP over 1 main (3.00).
        f.statistics_target(2026, 1, "2.50", minimum=2)

    def statuses(self, now):
        with clock(now):
            overview = corporation_overview(*MARCH)
        return {row.corporation_id: row.status for row in overview.rows}, overview

    def test_finished_month_is_red_below_the_target_and_green_above(self):
        statuses, overview = self.statuses(utc(2026, 4, 2, 12))

        self.assertEqual(statuses, {f.DEFAULT_CORP[0]: "missed", f.OTHER_CORP[0]: "met"})
        self.assertEqual((overview.corporations_met, overview.corporations_with_target), (1, 2))

    def test_running_month_is_amber_until_the_target_is_reached(self):
        statuses, overview = self.statuses(utc(2026, 3, 20, 12))

        self.assertEqual(statuses, {f.DEFAULT_CORP[0]: "in_progress", f.OTHER_CORP[0]: "met"})
        self.assertTrue(overview.month.is_current)

    def test_a_corporation_that_reaches_the_target_in_the_running_month_turns_green(self):
        f.add_attendance(f.closed_operation(self.fc, utc(2026, 3, 18, 18)), self.bob, value=3)

        statuses, _ = self.statuses(utc(2026, 3, 20, 12))

        # 8 PAP are more than the 7.5 PAP three mains need.
        self.assertEqual(statuses[f.DEFAULT_CORP[0]], "met")

    def test_the_target_is_compared_exactly(self):
        f.statistics_target(2026, 3, "1.67")
        # 1.67 x 3 mains = 5.01 PAP; 5 PAP is short even though the displayed average is 1.67.
        statuses, overview = self.statuses(utc(2026, 4, 2, 12))

        self.assertEqual(by_corporation(overview)[f.DEFAULT_CORP[0]].average, Decimal("1.67"))
        self.assertEqual(statuses[f.DEFAULT_CORP[0]], "missed")

    def test_without_a_target_there_is_no_status(self):
        from fleetops.models import StatisticsTarget

        StatisticsTarget.objects.all().delete()
        statuses, overview = self.statuses(utc(2026, 4, 2, 12))

        self.assertEqual(set(statuses.values()), {"none"})
        self.assertEqual(overview.corporations_with_target, 0)
        self.assertIsNone(overview.target.rule)

    def test_target_zero_is_always_met(self):
        f.statistics_target(2026, 3, "0")
        statuses, _ = self.statuses(utc(2026, 4, 2, 12))

        self.assertEqual(set(statuses.values()), {"met"})

    def test_a_target_applies_until_the_next_one(self):
        f.statistics_target(2026, 4, "10")
        statuses, _ = self.statuses(utc(2026, 5, 2, 12))

        # March still uses the target of January.
        self.assertEqual(statuses[f.OTHER_CORP[0]], "met")
        with clock(utc(2026, 5, 2, 12)):
            april = corporation_overview(2026, 4)
        self.assertEqual(april.target.average, Decimal("10.00"))

    def test_members_below_the_minimum_follow_the_same_colours(self):
        # Minimum 2: alice (4) and carol (3) are fine; the FC (0) and bob (1) are below.
        _, finished = self.statuses(utc(2026, 4, 2, 12))
        _, running = self.statuses(utc(2026, 3, 20, 12))

        a = by_corporation(finished)[f.DEFAULT_CORP[0]]
        self.assertEqual(a.mains_below_minimum, 2)
        self.assertEqual(by_corporation(finished)[f.OTHER_CORP[0]].mains_below_minimum, 0)
        self.assertEqual(by_corporation(running)[f.DEFAULT_CORP[0]].mains_below_minimum, 2)

    def test_member_minimum_zero_lists_nobody(self):
        f.statistics_target(2026, 3, "2.50", minimum=0)

        _, overview = self.statuses(utc(2026, 4, 2, 12))

        self.assertEqual(sum(row.mains_below_minimum for row in overview.rows), 0)

    def test_the_minimum_counts_personal_pap_in_any_corporation(self):
        # Bob also earns PAP on an alt that is in another corporation; it is bob's PAP.
        bob_alt = f.add_alt(self.bob, "Bob Alt", corporation=f.THIRD_CORP)
        f.add_attendance(self.op1, self.bob, bob_alt, value=1)

        _, overview = self.statuses(utc(2026, 4, 2, 12))

        # Bob now has 2 PAP: only the FC is below the minimum of 2.
        self.assertEqual(by_corporation(overview)[f.DEFAULT_CORP[0]].mains_below_minimum, 1)


class OverviewSortTests(AllianceFixtureMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.zulu = f.member("zulu", corporation=f.THIRD_CORP, main_name="Zulu Pilot")
        f.add_attendance(self.op1, self.zulu, value=1)
        f.statistics_target(2026, 1, "2.00")
        # A: 5/3 = 1.67 (missed), B: 3/1 = 3.00 (met), C: 1/1 = 1.00 (missed).

    def order(self, sort):
        with clock(utc(2026, 4, 2, 12)):
            return [row.corporation_id for row in corporation_overview(*MARCH, sort=sort).rows]

    def test_every_sort_key(self):
        a, b, c = f.DEFAULT_CORP[0], f.OTHER_CORP[0], f.THIRD_CORP[0]
        # Names: Test Corp One, Test Corp Three, Test Corp Two.
        self.assertEqual(self.order("name"), [a, c, b])
        self.assertEqual(self.order("-name"), [b, c, a])
        self.assertEqual(self.order("average"), [c, a, b])
        self.assertEqual(self.order("-average"), [b, a, c])
        self.assertEqual(self.order("total"), [c, b, a])
        self.assertEqual(self.order("-total"), [a, b, c])
        self.assertEqual(self.order("mains"), [c, b, a])
        self.assertEqual(self.order("-mains"), [a, c, b])
        # Corporations below the target first, then by name.
        self.assertEqual(self.order("status"), [a, c, b])
        # The reverse: the corporations at the target first, the others by name.
        self.assertEqual(self.order("-status"), [b, a, c])

    def test_unknown_sort_falls_back_to_the_name(self):
        for sort in ("", "bogus", "-bogus", "average; drop", None):
            with self.subTest(sort=sort):
                with clock(utc(2026, 4, 2, 12)):
                    overview = corporation_overview(*MARCH, sort=sort)
                self.assertEqual(overview.sort, "name")
                self.assertEqual([row.corporation_id for row in overview.rows][0], f.DEFAULT_CORP[0])

    def test_the_sort_keys_are_listed(self):
        self.assertEqual(OVERVIEW_SORTS[0], "name")
        for sort in OVERVIEW_SORTS:
            self.assertEqual(corporation_overview(*MARCH, sort=sort).sort, sort)

    def test_equal_values_keep_the_name_order(self):
        f.add_attendance(self.op2, self.zulu, value=2)
        # C now has 3 PAP like B; with the same average the names decide.
        self.assertEqual(self.order("-total"), [f.DEFAULT_CORP[0], f.THIRD_CORP[0], f.OTHER_CORP[0]])


class OverviewQueryBudgetTests(TestCase):
    """The queries do not grow with the number of corporations, members or fleets."""

    def add_corporations(self, fc, strat, start, count, members):
        for index in range(start, start + count):
            corporation = (2_100_000 + index, f"Budget Corp {index}")
            op = f.closed_operation(fc, utc(2026, 3, 2 + index % 20, 18), type_obj=strat)
            for number in range(members):
                user = f.member(f"budget{index}-{number}", corporation=corporation)
                f.add_attendance(op, user, value=1 + number)

    def count(self, call):
        with CaptureQueriesContext(connection) as queries:
            call()
        return len(queries)

    def test_overview_service(self):
        fc = f.member("fc", perms=f.FC_PERMS)
        strat = f.fleet_type("StratOp", "2.00")
        f.fleet_type("Roam", "1.00")
        f.statistics_target(2026, 1, "2.00")
        self.add_corporations(fc, strat, 0, 2, 3)
        small = self.count(lambda: corporation_overview(*MARCH))
        self.add_corporations(fc, strat, 2, 4, 9)
        result = []
        large = self.count(lambda: result.append(corporation_overview(*MARCH)))
        self.assertEqual(len(result[0].rows), 7)
        self.assertEqual(small, large)
        self.assertLessEqual(small, 10)

    def test_overview_service_with_an_other_row(self):
        fc = f.member("fc", perms=f.FC_PERMS)
        strat = f.fleet_type("StratOp", "2.00")
        self.add_corporations(fc, strat, 0, 2, 3)
        ghost = f.member("ghost", corporation=f.THIRD_CORP)
        f.add_attendance(f.closed_operation(fc, utc(2026, 3, 3, 18), type_obj=strat), ghost)
        ghost.is_active = False
        ghost.save(update_fields=["is_active"])
        small = self.count(lambda: corporation_overview(*MARCH))
        self.add_corporations(fc, strat, 2, 4, 9)
        self.assertIsNotNone(corporation_overview(*MARCH).other)
        self.assertEqual(small, self.count(lambda: corporation_overview(*MARCH)))

    def test_overview_page(self):
        lead = f.create_user("statslead", perms=f.MEMBER_PERMS + ["fleetops.view_all_stats"])
        fc = f.member("fc", perms=f.FC_PERMS)
        strat = f.fleet_type("StratOp", "2.00")
        f.fleet_type("Roam", "1.00")
        f.statistics_target(2026, 1, "2.00")
        self.client.force_login(lead)
        url = reverse("fleetops:all_corporation_statistics")

        def page():
            self.assertEqual(self.client.get(url, {"year": 2026, "month": 3}).status_code, 200)

        self.add_corporations(fc, strat, 0, 2, 3)
        page()  # warms up the content types and permissions
        small = self.count(page)
        self.add_corporations(fc, strat, 2, 4, 9)
        self.assertEqual(small, self.count(page))
        # Alliance Auth's own page chrome (login state, user, permissions, menu) takes about 16 of these; the menu's
        # group lookup for viewers without start_fleet one more.
        self.assertLessEqual(small, 29)

    def test_corporation_page(self):
        corporation = (2_100_000, "Budget Corp 0")
        manager = f.create_user("manager", perms=f.CORP_MANAGEMENT_PERMS, corporation=corporation)
        fc = f.member("fc", perms=f.FC_PERMS)
        strat = f.fleet_type("StratOp", "2.00")
        f.fleet_type("Roam", "1.00")
        f.statistics_target(2026, 1, "2.00")
        self.client.force_login(manager)
        url = reverse("fleetops:corporation_statistics")

        def page():
            self.assertEqual(self.client.get(url, {"year": 2026, "month": 3}).status_code, 200)

        self.add_corporations(fc, strat, 0, 1, 3)
        page()
        small = self.count(page)
        for number in range(12):
            user = f.member(f"later{number}", corporation=corporation)
            f.add_attendance(f.closed_operation(fc, utc(2026, 3, 22, 18), type_obj=strat), user)
        self.assertEqual(self.client.get(url, {"year": 2026, "month": 3}).context["stats"]["main_character_count"], 16)
        self.assertEqual(small, self.count(page))
        # One more than before for the menu's group lookup (the manager has no start_fleet).
        self.assertLessEqual(small, 29)

    def test_corporation_statistics(self):
        fc = f.member("fc", perms=f.FC_PERMS)
        strat = f.fleet_type("StratOp", "2.00")
        f.fleet_type("Roam", "1.00")
        f.statistics_target(2026, 1, "2.00")
        self.add_corporations(fc, strat, 0, 1, 3)
        corporation_id = 2_100_000
        small = self.count(lambda: corporation_statistics(corporation_id, *MARCH))
        for number in range(12):
            user = f.member(f"later{number}", corporation=(corporation_id, "Budget Corp 0"))
            f.add_attendance(f.closed_operation(fc, utc(2026, 3, 22, 18), type_obj=strat), user)
        self.assertEqual(corporation_statistics(corporation_id, *MARCH)["main_character_count"], 15)
        self.assertEqual(small, self.count(lambda: corporation_statistics(corporation_id, *MARCH)))
        self.assertLessEqual(small, 8)


class CorporationStatisticsDataTests(AllianceFixtureMixin, TestCase):
    def test_the_existing_keys_keep_their_values(self):
        stats = corporation_statistics(f.DEFAULT_CORP[0], *MARCH)

        self.assertEqual(stats["corporation_id"], f.DEFAULT_CORP[0])
        self.assertEqual(stats["main_character_count"], 3)
        self.assertEqual(stats["total_attendance"], 5)
        self.assertAlmostEqual(stats["average_attendance"], 5 / 3)
        self.assertEqual(stats["breakdown"], {"StratOp": 4, "Roam": 1})
        self.assertAlmostEqual(stats["breakdown_average"]["StratOp"], 4 / 3)

    def test_the_new_keys(self):
        f.statistics_target(2026, 1, "2.50", minimum=2)
        with clock(utc(2026, 4, 2, 12)):
            stats = corporation_statistics(f.DEFAULT_CORP[0], *MARCH)

        self.assertEqual(stats["total_pap"], 5)
        self.assertEqual(stats["average"], Decimal("1.67"))
        self.assertEqual(stats["fleets"], 2)
        self.assertEqual((stats["mains_with_pap"], stats["mains_below_minimum"]), (2, 2))
        self.assertEqual(stats["member_pap"], 5)
        self.assertEqual(stats["status"], "missed")
        self.assertEqual(stats["target"].average, Decimal("2.50"))
        # 2.50 x 3 mains = 7.5 PAP; 5 are there.
        self.assertEqual(stats["target_pap"], Decimal("7.50"))
        self.assertEqual(stats["missing_pap"], 3)

    def test_status_follows_the_month(self):
        f.statistics_target(2026, 1, "2.50")
        with clock(utc(2026, 3, 20, 12)):
            running = corporation_statistics(f.DEFAULT_CORP[0], *MARCH)["status"]
        with clock(utc(2026, 4, 20, 12)):
            finished = corporation_statistics(f.DEFAULT_CORP[0], *MARCH)["status"]
            reached = corporation_statistics(f.OTHER_CORP[0], *MARCH)["status"]
        self.assertEqual((running, finished, reached), ("in_progress", "missed", "met"))

    def test_without_a_target(self):
        stats = corporation_statistics(f.DEFAULT_CORP[0], *MARCH)

        self.assertEqual(stats["status"], "none")
        self.assertIsNone(stats["target_pap"])
        self.assertEqual(stats["missing_pap"], 0)
        # The default minimum of one PAP: the FC has none.
        self.assertEqual(stats["mains_below_minimum"], 1)

    def test_a_corporation_without_members(self):
        f.statistics_target(2026, 1, "2.50")
        with clock(utc(2026, 4, 2, 12)):
            stats = corporation_statistics(f.THIRD_CORP[0], *MARCH)

        self.assertEqual((stats["main_character_count"], stats["total_pap"]), (0, 0))
        self.assertEqual(stats["average"], Decimal("0.00"))
        self.assertEqual(stats["status"], "no_members")
        self.assertEqual((stats["mains_with_pap"], stats["mains_below_minimum"]), (0, 0))

    def test_members_without_pap_are_below_the_minimum(self):
        for index in range(3):
            f.member(f"idle{index}")

        stats = corporation_statistics(f.DEFAULT_CORP[0], *MARCH)

        self.assertEqual((stats["main_character_count"], stats["mains_below_minimum"]), (6, 4))

    def test_the_minimum_counts_pap_in_any_corporation(self):
        # Bob moved from corporation B to A in the middle of the month; all his PAP counts for him.
        dave = f.member("dave", corporation=f.OTHER_CORP)
        f.add_attendance(self.op2, dave, value=2)
        EveCharacter.objects.filter(pk=f.main_of(dave).pk).update(
            corporation_id=f.DEFAULT_CORP[0], corporation_name=f.DEFAULT_CORP[1]
        )

        stats = corporation_statistics(f.DEFAULT_CORP[0], *MARCH)

        self.assertEqual(stats["total_pap"], 5)
        self.assertEqual(stats["member_pap"], 7)
        self.assertEqual(stats["mains_with_pap"], 3)

    def test_users_outside_the_member_alliances_are_hidden(self):
        f.settings(history_alliance_ids=str(f.DEFAULT_ALLIANCE[0]))
        departed = f.member("departed", corporation=f.OTHER_CORP, alliance=f.FOREIGN_ALLIANCE)
        f.add_attendance(self.op1, departed, value=9)

        stats = corporation_statistics(f.OTHER_CORP[0], *MARCH)

        self.assertEqual((stats["main_character_count"], stats["total_pap"]), (1, 3))


class OverviewPageTests(AllianceFixtureMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.lead = f.create_user("statslead", perms=f.MEMBER_PERMS + ["fleetops.view_all_stats"], corporation=STAFF_CORP)
        self.admin = f.create_user(
            "admin",
            perms=f.MEMBER_PERMS + ["fleetops.view_all_stats", "fleetops.manage_configuration"],
            corporation=STAFF_CORP,
        )

    def get(self, user, now=None, **params):
        patcher = clock(now or utc(2026, 4, 2, 12))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client.force_login(user)
        return self.client.get(reverse("fleetops:all_corporation_statistics"), {"year": 2026, "month": 3, **params})

    def test_rows_and_cards(self):
        response = self.get(self.lead)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, f.DEFAULT_CORP[1])
        self.assertContains(response, "Alliance PAP")
        self.assertContains(response, "PAP by corporation")
        self.assertContains(response, "2026-03 (EVE time)")
        overview = response.context["overview"]
        self.assertEqual(overview.alliance_pap, 8)
        # The two statistics managers are mains of a corporation of their own.
        self.assertEqual(len(response.context["table"]), 3)
        self.assertEqual(overview.mains, 6)

    def test_info_alert_when_member_alliance_ids_are_empty(self):
        response = self.get(self.lead)
        self.assertContains(response, "Member alliance IDs are not set")

        f.settings(history_alliance_ids=str(f.DEFAULT_ALLIANCE[0]))
        response = self.get(self.lead)
        self.assertNotContains(response, "Member alliance IDs are not set")

    def test_statuses_are_shown_as_words_and_colours(self):
        f.statistics_target(2026, 1, "2.50")

        response = self.get(self.lead)

        self.assertContains(response, "Below target")
        self.assertContains(response, "Target met")
        self.assertContains(response, "text-bg-danger")
        self.assertContains(response, "text-bg-success")
        self.assertContains(response, "fleetops-status-row-missed")
        self.assertContains(response, "fleetops-status-row-met")
        self.assertNotContains(response, "Not reached yet")

    def test_the_running_month_is_amber(self):
        f.statistics_target(2026, 1, "2.50")

        response = self.get(self.lead, now=utc(2026, 3, 20, 12))

        self.assertContains(response, "Not reached yet")
        self.assertContains(response, "text-bg-warning")
        self.assertContains(response, "fleetops-status-row-progress")
        self.assertNotContains(response, "Below target")
        self.assertNotContains(response, "text-bg-danger")
        self.assertContains(response, "Month in progress")

    def test_no_status_without_a_target(self):
        response = self.get(self.lead)

        self.assertNotContains(response, "Target met")
        self.assertNotContains(response, "Below target")
        self.assertContains(response, "No monthly target applies")

    def test_only_a_configuration_manager_is_offered_the_targets(self):
        targets = reverse("fleetops:configuration_list", args=["statistics-targets"])

        self.assertNotContains(self.get(self.lead), targets)
        self.assertContains(self.get(self.admin), targets)

    def test_other_corporations_row(self):
        ghost = f.member("ghost", corporation=f.THIRD_CORP)
        f.add_attendance(self.op1, ghost, value=4)
        ghost.is_active = False
        ghost.save(update_fields=["is_active"])

        response = self.get(self.lead)

        self.assertContains(response, OTHER_CORPORATIONS)
        self.assertEqual(response.context["overview"].other.total_pap, 4)
        # The other row has no link to a corporation page.
        self.assertNotContains(response, reverse("fleetops:corporation_statistics_detail", args=[f.THIRD_CORP[0]]))

    def test_not_counted_fleets_are_summarised(self):
        pending = f.closed_operation(self.fc, utc(2026, 3, 13, 18), review_status=ReviewStatus.PENDING)
        f.add_attendance(pending, self.bob)
        f.closed_operation(self.fc, utc(2026, 3, 14, 18), counts_for_stats=False)

        response = self.get(self.lead)

        self.assertContains(response, "Fleets not counted")
        self.assertContains(response, "1 excluded")
        self.assertContains(response, "1 pending approval")
        self.assertEqual(response.context["not_counted_total"], 2)

    def test_links_to_the_corporation_pages_and_member_lists(self):
        response = self.get(self.lead)

        detail = reverse("fleetops:corporation_statistics_detail", args=[f.DEFAULT_CORP[0]])
        members = reverse("fleetops:corporation_members_detail", args=[f.DEFAULT_CORP[0]])
        inactive = f"{members}?year=2026&amp;month=3&amp;status=inactive"
        self.assertContains(response, f"{detail}?year=2026&amp;month=3")
        self.assertContains(response, f"{members}?year=2026&amp;month=3")
        # The FC of corporation A has no PAP.
        self.assertContains(response, inactive)

    def test_export_buttons_point_to_the_corporations_dataset(self):
        response = self.get(self.lead, sort="-total")

        export = reverse("fleetops:export", args=["corporations"])
        self.assertContains(response, f"{export}?")
        self.assertContains(response, "format=xlsx")
        self.assertContains(response, "sort=-total")
        self.assertContains(response, "year=2026")

    def test_the_sort_comes_from_the_query_and_the_headers_turn_it_around(self):
        response = self.get(self.lead, sort="-average")

        self.assertEqual(response.context["sort"], "-average")
        self.assertEqual([row.corporation_id for row in response.context["overview"].rows][0], f.OTHER_CORP[0])
        self.assertEqual(response.context["sort_links"]["average"], "average")
        self.assertEqual(response.context["sort_links"]["total"], "-total")
        self.assertEqual(response.context["sort_links"]["name"], "name")
        self.assertContains(response, "sort=average")
        # The sort is kept when the month changes.
        self.assertEqual(response.context["preserved_params"], [("sort", "-average")])

    def test_every_header_link_keeps_its_own_sort(self):
        """A header link is a sort the page understands, so a second click never falls back to the name."""
        for sort in OVERVIEW_SORTS:
            with self.subTest(sort=sort):
                links = self.get(self.lead, sort=sort).context["sort_links"]
                for column, link in links.items():
                    self.assertIn(link, OVERVIEW_SORTS, column)
                    self.assertEqual(self.get(self.lead, sort=link).context["sort"], link, (sort, column))

    def test_the_status_header_turns_the_order_around_and_back(self):
        f.statistics_target(2026, 1, "2.00")
        first = self.get(self.lead, sort="status")

        self.assertEqual(first.context["sort"], "status")
        self.assertEqual(first.context["sort_links"]["status"], "-status")
        self.assertContains(first, "Status &#9650;")
        # The link of the sorted header is followed: the order is reversed, not dropped.
        second = self.get(self.lead, sort=first.context["sort_links"]["status"])
        self.assertEqual(second.context["sort"], "-status")
        statuses = [row.status for row in second.context["overview"].rows]
        self.assertEqual(statuses[0], "met")
        self.assertEqual(statuses, sorted(statuses, key=lambda status: status != "met"))
        self.assertContains(second, "Status &#9660;")
        self.assertEqual(second.context["sort_links"]["status"], "status")
        self.assertEqual(self.get(self.lead, sort="status").context["sort"], "status")

    def test_a_bad_sort_and_month_never_fail(self):
        for params in ({"sort": "<script>"}, {"year": "x"}, {"month": "13"}, {"sort": "-"}):
            with self.subTest(params=params):
                self.assertEqual(self.get(self.lead, **params).status_code, 200)

    def test_a_month_older_than_the_retention_window_is_noted(self):
        f.statistics_target(2025, 1, "2.00")

        response = self.get(self.lead, now=utc(2026, 10, 13, 12), year=2025, month=8)

        self.assertEqual(response.context["retention_days"], 365)
        self.assertContains(response, "PAP older than 365 days is removed, so this month may be incomplete.")

    def test_the_retention_note_names_the_configured_window(self):
        f.settings(data_retention_days=400)

        self.assertContains(self.get(self.lead, now=utc(2026, 10, 13, 12), year=2025, month=8), "PAP older than 400 days is removed")
        self.assertNotContains(self.get(self.lead, now=utc(2026, 10, 13, 12), year=2025, month=10), "PAP older than")

    def test_a_month_within_the_retention_window_has_no_note(self):
        response = self.get(self.lead)

        self.assertIsNone(response.context["retention_days"])
        self.assertNotContains(response, "PAP older than")

    def test_empty_month(self):
        response = self.get(self.lead, year=2020, month=1)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["overview"].alliance_pap, 0)


class CorporationPageTests(AllianceFixtureMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.manager = f.create_user("manager", perms=f.CORP_MANAGEMENT_PERMS)
        self.lead = f.create_user("statslead", perms=f.MEMBER_PERMS + ["fleetops.view_all_stats"], corporation=STAFF_CORP)
        f.statistics_target(2026, 1, "2.50", minimum=2)

    def get(self, user, name="corporation_statistics", *args, now=None, **params):
        patcher = clock(now or utc(2026, 4, 2, 12))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client.force_login(user)
        return self.client.get(reverse(f"fleetops:{name}", args=args), {"year": 2026, "month": 3, **params})

    def test_own_corporation_page(self):
        response = self.get(self.manager)

        self.assertEqual(response.status_code, 200)
        stats = response.context["stats"]
        # The fixture's three members, the manager and nobody else are mains of corporation A.
        self.assertEqual(stats["main_character_count"], 4)
        self.assertEqual(stats["total_pap"], 5)
        self.assertEqual(response.context["active_tab"], "overview")
        self.assertIsNone(response.context["tab_corporation_id"])
        self.assertContains(response, f.DEFAULT_CORP[1])
        self.assertContains(response, "Corporation PAP statistics")
        self.assertContains(response, "Below target")
        # The month is over: nothing can still be earned, so the card says by how much it fell short.
        self.assertContains(response, "5 PAP short")
        self.assertNotContains(response, "to go")
        self.assertContains(response, "fleetops-corporation-tabs")

    def test_the_page_says_how_the_two_attributions_work(self):
        response = self.get(self.manager)

        self.assertContains(response, "earned while the main was in this corporation")

    def test_card_numbers(self):
        # Corporation A: mains fc, alice, bob and the manager; 5 PAP, so the average is 1.25.
        response = self.get(self.manager)

        self.assertContains(response, "1.25")
        self.assertContains(response, "2 / 4")
        inactive = reverse("fleetops:corporation_members")
        self.assertContains(response, f"{inactive}?year=2026&amp;month=3&amp;status=inactive")

    def test_running_month_is_amber_here_too(self):
        response = self.get(self.manager, now=utc(2026, 3, 20, 12))

        self.assertContains(response, "Not reached yet")
        self.assertContains(response, "so far")
        self.assertContains(response, "5 PAP to go")
        self.assertNotContains(response, "PAP short")
        self.assertNotContains(response, "Below target")

    def test_a_month_older_than_the_retention_window_is_noted_on_both_corporation_pages(self):
        f.statistics_target(2025, 1, "2.00")
        old = utc(2026, 10, 13, 12)

        own = self.get(self.manager, now=old, year=2025, month=8)
        by_id = self.get(self.lead, "corporation_statistics_detail", f.DEFAULT_CORP[0], now=old, year=2025, month=8)

        for response in (own, by_id):
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.context["retention_days"], 365)
            self.assertContains(response, "PAP older than 365 days is removed, so this month may be incomplete.")
        self.assertNotContains(self.get(self.manager), "PAP older than")

    def test_view_all_stats_opens_any_corporation(self):
        response = self.get(self.lead, "corporation_statistics_detail", f.OTHER_CORP[0])

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["stats"]["corporation_name"], f.OTHER_CORP[1])
        self.assertEqual(response.context["tab_corporation_id"], f.OTHER_CORP[0])
        self.assertEqual(response.context["stats"]["status"], "met")
        self.assertContains(response, "Target met")
        self.assertContains(response, reverse("fleetops:all_corporation_statistics"))
        # The page exports the members of its corporation, not the overview of the alliance.
        self.assertContains(response, f"{reverse('fleetops:export', args=['members'])}?")
        self.assertContains(response, f"corporation={f.OTHER_CORP[0]}")
        self.assertNotContains(response, reverse("fleetops:export", args=["corporations"]))

    def test_a_corporation_manager_exports_the_members_of_the_own_corporation(self):
        response = self.get(self.manager)

        self.assertContains(response, f"{reverse('fleetops:export', args=['members'])}?")
        self.assertContains(response, f"corporation={f.DEFAULT_CORP[0]}")
        self.assertNotContains(response, reverse("fleetops:export", args=["corporations"]))

    def test_a_corporation_with_pap_but_no_members_still_has_a_page(self):
        ghost = f.member("ghost", corporation=f.THIRD_CORP)
        f.add_attendance(self.op1, ghost, value=4)
        ghost.is_active = False
        ghost.save(update_fields=["is_active"])

        response = self.get(self.lead, "corporation_statistics_detail", f.THIRD_CORP[0])

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["stats"]["total_pap"], 4)
        self.assertEqual(response.context["stats"]["corporation_name"], f.THIRD_CORP[1])
        self.assertContains(response, "no main characters in scope")

    def test_a_corporation_without_members_and_pap_is_not_found(self):
        self.assertEqual(self.get(self.lead, "corporation_statistics_detail", f.THIRD_CORP[0]).status_code, 404)

    def test_a_corporation_outside_the_member_alliances(self):
        f.settings(history_alliance_ids=str(f.FOREIGN_ALLIANCE[0]))

        response = self.get(self.manager)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["stats"]["main_character_count"], 0)
        self.assertContains(response, "not in the configured member alliances")
        self.assertEqual(response.context["stats"]["status"], "no_members")

    def test_month_selector_and_neighbours(self):
        response = self.get(self.manager)

        self.assertContains(response, "Month (EVE time)")
        self.assertEqual(response.context["previous_period"]["label"], "2026-02")
        self.assertEqual(response.context["next_period"]["label"], "2026-04")

    def test_future_month_has_no_status(self):
        response = self.get(self.manager, now=utc(2026, 2, 20, 12))

        self.assertEqual(response.context["stats"]["status"], "none")
        self.assertNotContains(response, "Below target")
        self.assertNotContains(response, "Not reached yet")
