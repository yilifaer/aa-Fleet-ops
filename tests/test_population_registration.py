# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Who is in a month's statistics: users who registered after the month are not.

A user whose Alliance Auth registration is at or after the end of a month (the next month's 1st, 00:00 UTC) is
not a main of that month: not in its member lists, leaderboard or exports, not in the corporation figures and
without a rank. The registration month and every later month are unchanged. The site runs in a time zone far
from UTC, so a boundary taken from the site clock instead of EVE time moves a user.
"""

from datetime import datetime, timezone as dt_timezone
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse

from fleetops.models import AttendanceRecord
from fleetops.services import population
from fleetops.services import members as member_service
from fleetops.services import statistics as statistics_service

from . import factories as f
from .test_export_datasets import csv_rows

UTC = dt_timezone.utc
NOW = datetime(2026, 4, 14, 12, 0, tzinfo=UTC)
MARCH, APRIL = (2026, 3), (2026, 4)
MARCH_END = datetime(2026, 4, 1, tzinfo=UTC)

LATE_CORP = (2_000_555, "Late Corp")
QUIET_CORP = (2_000_556, "Quiet Corp")
FOREIGN_CORP = (2_000_900, "Foreign Corp")
NOT_COUNTED = "Members who registered after this month are not counted in it."

ALL_STATS = f.MEMBER_PERMS + ["fleetops.view_all_stats"]


def utc(year, month, day, hour=19, minute=0, second=0):
    return datetime(year, month, day, hour, minute, second, tzinfo=UTC)


class RegistrationMonthTests:
    def setUp(self):
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)
        clock = mock.patch("django.utils.timezone.now", return_value=NOW)
        clock.start()
        self.addCleanup(clock.stop)
        f.settings(history_alliance_ids=str(f.DEFAULT_ALLIANCE[0]), leaderboard_visibility="members")
        strat = f.fleet_type("StratOp", "1.00")
        f.statistics_target(*MARCH, "1.00", minimum=1)

        self.viewer = f.member("viewer", perms=ALL_STATS, main_name="Viewer", corporation=f.THIRD_CORP)
        self.fc = f.member("fc", perms=f.FC_PERMS, main_name="Flow FC", corporation=f.THIRD_CORP)
        self.old = f.member("old", perms=f.MEMBER_PERMS, main_name="Old Hand", joined=utc(2026, 1, 10))
        self.mid = f.member("mid", perms=f.MEMBER_PERMS, main_name="Mid March", joined=utc(2026, 3, 20))
        self.last = f.member("last", main_name="Last Second", joined=utc(2026, 3, 31, 23, 59, 59))
        self.edge = f.member("edge", main_name="Exactly April", joined=utc(2026, 4, 1, 0, 0, 0))
        self.recruit = f.member("recruit", perms=f.MEMBER_PERMS, main_name="April Recruit", joined=utc(2026, 4, 2))

        # Every main of Late Corp registered in April, Quiet Corp's only main has no PAP at all, and a departed
        # mover earned its PAP in a corporation that has no members any more (the "Other corporations" row).
        self.late_corp = f.member("latecorp", main_name="Late Corp Pilot", corporation=LATE_CORP, joined=utc(2026, 4, 5))
        self.quiet = f.member("quiet", main_name="Quiet Pilot", corporation=QUIET_CORP, joined=utc(2026, 4, 6))
        self.mover = f.member("mover", main_name="Mover", corporation=f.THIRD_CORP)

        fleet = f.closed_operation(self.fc, utc(2026, 3, 12), type_obj=strat)
        for user, value in (
            (self.old, 3),
            (self.mid, 2),
            (self.recruit, 5),
            (self.edge, 1),
            (self.late_corp, 2),
            (self.mover, 1),
        ):
            f.add_attendance(fleet, user, value=value)
        AttendanceRecord.objects.filter(auth_user=self.mover).update(
            corporation_id=FOREIGN_CORP[0], corporation_name=FOREIGN_CORP[1]
        )
        april = f.closed_operation(self.fc, utc(2026, 4, 10), type_obj=strat)
        for user, value in ((self.old, 1), (self.recruit, 4)):
            f.add_attendance(april, user, value=value)

    def page(self, viewer, route, *args, month=MARCH, **params):
        self.client.force_login(viewer)
        response = self.client.get(reverse(f"fleetops:{route}", args=args), {"year": month[0], "month": month[1], **params})
        self.assertEqual(response.status_code, 200, route)
        return response

    def names(self, rows):
        return {row.main_character_name for row in rows}

    # The population itself

    def test_the_cut_off_is_the_first_instant_of_the_next_month(self):
        listed = {person.main_character_name for person in population.members(registered_before=MARCH_END)}
        self.assertIn("Last Second", listed)
        self.assertIn("Mid March", listed)
        self.assertNotIn("Exactly April", listed)
        self.assertNotIn("April Recruit", listed)

    def test_without_a_cut_off_everybody_counts(self):
        listed = {person.main_character_name for person in population.members()}
        self.assertTrue({"Exactly April", "April Recruit", "Last Second"} <= listed)

    def test_the_subquery_and_the_main_count_follow_the_cut_off(self):
        subquery = population.member_user_ids_subquery(registered_before=MARCH_END)
        ids = set(get_user_model().objects.filter(pk__in=subquery).values_list("pk", flat=True))
        self.assertEqual(ids & {self.last.pk, self.edge.pk, self.recruit.pk}, {self.last.pk})
        count = population.corporation_main_count(f.DEFAULT_CORP[0], registered_before=MARCH_END)
        self.assertEqual(count, population.corporation_main_count(f.DEFAULT_CORP[0]) - 2)

    # Member lists

    def test_a_finished_month_leaves_out_later_registrations(self):
        table = self.page(self.viewer, "alliance_members").context["table"]
        self.assertEqual(self.names(table.rows), {"Old Hand", "Mid March", "Last Second", "Viewer", "Flow FC", "Mover"})
        self.assertEqual(table.counts["all"], 6)
        self.assertEqual(table.counts["with_pap"], 3)

    def test_the_registration_month_and_later_months_are_unchanged(self):
        april = self.page(self.viewer, "alliance_members", month=APRIL).context["table"]
        self.assertTrue({"April Recruit", "Exactly April", "Old Hand"} <= self.names(april.rows))
        february = self.page(self.viewer, "alliance_members", month=(2026, 2)).context["table"]
        self.assertEqual(self.names(february.rows), {"Old Hand", "Viewer", "Flow FC", "Mover"})

    def test_the_inactive_list_leaves_out_later_registrations(self):
        table = self.page(self.viewer, "alliance_members", status="inactive").context["table"]
        self.assertEqual(self.names(table.rows), {"Last Second", "Viewer", "Flow FC"})
        april = self.page(self.viewer, "alliance_members", month=APRIL, status="inactive").context["table"]
        self.assertIn("Exactly April", self.names(april.rows))

    def test_the_leaderboard_leaves_out_later_registrations(self):
        board = self.page(self.viewer, "leaderboard").context["boards"][0]["board"]
        self.assertEqual([(row.main_character_name, row.rank) for row in board.rows], [("Old Hand", 1), ("Mid March", 2), ("Mover", 3)])
        self.assertEqual(board.ranked_count, 3)
        april = self.page(self.viewer, "leaderboard", month=APRIL).context["boards"][0]["board"]
        self.assertEqual([row.main_character_name for row in april.rows], ["April Recruit", "Old Hand"])

    def test_a_corporation_list_leaves_out_later_registrations(self):
        table = member_service.member_rows(2026, 3, corporation_id=f.DEFAULT_CORP[0])
        self.assertEqual(self.names(table.rows), {"Old Hand", "Mid March", "Last Second"})

    # Corporation figures

    def test_corporation_figures_do_not_count_later_registrations(self):
        stats = statistics_service.corporation_statistics(f.DEFAULT_CORP[0], 2026, 3)
        self.assertEqual(stats["main_character_count"], 3)
        self.assertEqual(stats["mains_with_pap"], 2)
        self.assertEqual(stats["mains_below_minimum"], 1)
        # The snapshot total keeps all PAP earned in the corporation, the average is per counted main.
        self.assertEqual(stats["total_pap"], 11)
        self.assertEqual(stats["average"], Decimal("3.67"))
        april = statistics_service.corporation_statistics(f.DEFAULT_CORP[0], 2026, 4)
        self.assertEqual(april["main_character_count"], 5)

    def test_the_overview_does_not_count_later_registrations(self):
        overview = self.page(self.viewer, "all_corporation_statistics").context["overview"]
        row = next(row for row in overview.rows if row.corporation_id == f.DEFAULT_CORP[0])
        self.assertEqual((row.mains, row.mains_with_pap, row.mains_below_minimum), (3, 2, 1))
        self.assertEqual(row.total_pap, 11)
        self.assertEqual(row.average, Decimal("3.67"))
        self.assertEqual(overview.mains, 6)
        self.assertEqual(statistics_service.corporation_overview(2026, 4).mains, 10)

    def test_the_running_month_includes_everybody_registered_so_far(self):
        overview = statistics_service.corporation_overview(2026, 4)
        row = next(row for row in overview.rows if row.corporation_id == f.DEFAULT_CORP[0])
        self.assertEqual(row.mains, 5)

    # Exports equal the pages

    def members_file(self, viewer=None, **params):
        self.client.force_login(viewer or self.viewer)
        response = self.client.get(
            reverse("fleetops:export", args=["members"]), {"format": "csv", "year": 2026, "month": 3, **params}
        )
        self.assertEqual(response.status_code, 200)
        return csv_rows(response)[1:]

    def test_the_members_export_equals_the_page(self):
        page = self.page(self.viewer, "alliance_members").context["table"]
        lines = self.members_file()
        self.assertEqual([line[1] for line in lines], [row.main_character_name for row in page.rows])
        self.assertEqual({line[1] for line in lines}, {"Old Hand", "Mid March", "Last Second", "Viewer", "Flow FC", "Mover"})

    def test_the_inactive_export_equals_the_page(self):
        page = self.page(self.viewer, "alliance_members", status="inactive", sort="pap").context["table"]
        lines = self.members_file(status="inactive", sort="pap")
        self.assertEqual([line[1] for line in lines], [row.main_character_name for row in page.rows])
        self.assertEqual({line[1] for line in lines}, {"Last Second", "Viewer", "Flow FC"})

    def test_the_corporation_members_export_equals_the_page(self):
        page = self.page(self.viewer, "corporation_members_detail", f.DEFAULT_CORP[0]).context["table"]
        lines = self.members_file(corporation=f.DEFAULT_CORP[0])
        self.assertEqual([line[1] for line in lines], [row.main_character_name for row in page.rows])
        self.assertEqual({line[1] for line in lines}, {"Old Hand", "Mid March", "Last Second"})

    def test_the_corporations_export_equals_the_page(self):
        overview = self.page(self.viewer, "all_corporation_statistics").context["overview"]
        self.client.force_login(self.viewer)
        response = self.client.get(
            reverse("fleetops:export", args=["corporations"]), {"format": "csv", "year": 2026, "month": 3}
        )
        by_name = {row[0]: row for row in csv_rows(response)}
        for row in overview.rows:
            line = by_name[row.corporation_name]
            self.assertEqual(
                [int(value) for value in line[3:7]],
                [row.mains, row.mains_with_pap, row.mains_below_minimum, row.total_pap],
            )
        self.assertEqual(by_name[f.DEFAULT_CORP[1]][3:6], ["3", "2", "1"])
        self.assertEqual(by_name[LATE_CORP[1]][3:7], ["0", "0", "0", "2"])

    # Corporations of later registrations only

    def test_a_corporation_of_later_registrations_keeps_its_row_and_total(self):
        overview = self.page(self.viewer, "all_corporation_statistics").context["overview"]
        rows = {row.corporation_name: row for row in overview.rows}
        late = rows[LATE_CORP[1]]
        self.assertEqual((late.mains, late.mains_with_pap, late.mains_below_minimum, late.total_pap), (0, 0, 0, 2))
        self.assertEqual(late.status, "no_members")
        # Quiet Corp has neither a main of the month nor PAP, so it has no row; later months list it.
        self.assertNotIn(QUIET_CORP[1], rows)
        self.assertIn(QUIET_CORP[1], {row.corporation_name for row in statistics_service.corporation_overview(2026, 4).rows})
        april = {row.corporation_name: row for row in statistics_service.corporation_overview(2026, 4).rows}
        self.assertEqual(april[LATE_CORP[1]].mains, 1)

    def test_other_corporations_stay_the_pap_of_corporations_without_current_members(self):
        overview = self.page(self.viewer, "all_corporation_statistics").context["overview"]
        self.assertEqual(overview.other.total_pap, 1)
        self.assertEqual(sum(row.total_pap for row in overview.rows) + overview.other.total_pap, overview.alliance_pap)
        self.assertEqual(overview.alliance_pap, 14)

    def test_the_corporation_figures_agree_with_the_overview_for_later_registrations(self):
        stats = statistics_service.corporation_statistics(LATE_CORP[0], 2026, 3)
        self.assertEqual((stats["main_character_count"], stats["total_pap"], stats["status"]), (0, 2, "no_members"))
        self.assertEqual(stats["mains_with_pap"], 0)
        self.assertEqual(stats["average"], Decimal("0.00"))
        page = self.page(self.viewer, "corporation_statistics_detail", LATE_CORP[0])
        self.assertEqual(page.context["stats"]["total_pap"], 2)

    # Ranks

    def test_my_statistics_has_no_rank_before_the_registration(self):
        self.assertIsNone(self.page(self.recruit, "my_statistics").context["rank_card"])
        card = self.page(self.recruit, "my_statistics", month=APRIL).context["rank_card"]
        self.assertEqual((card["rank"], card["of"]), (1, 2))
        self.assertEqual(self.page(self.old, "my_statistics").context["rank_card"]["rank"], 1)

    def test_my_statistics_has_no_minimum_status_before_the_registration(self):
        statuses = {
            name: self.page(user, "my_statistics").context["minimum_status"]
            for name, user in (("recruit", self.recruit), ("old", self.old), ("mid", self.mid))
        }
        self.assertEqual(statuses, {"recruit": "none", "old": "ok", "mid": "ok"})
        self.assertEqual(self.page(self.recruit, "my_statistics", month=APRIL).context["minimum_status"], "ok")

    def test_my_statistics_status_follows_the_boundary_second(self):
        for joined, expected in ((utc(2026, 3, 31, 23, 59, 59), "below"), (utc(2026, 4, 1, 0, 0, 0), "none")):
            user = f.member(f"b{joined.day}", perms=f.MEMBER_PERMS, joined=joined)
            self.assertEqual(self.page(user, "my_statistics").context["minimum_status"], expected)

    def test_the_member_detail_shows_no_rank_or_status_before_the_registration(self):
        detail = self.page(self.viewer, "member_detail", self.recruit.pk).context["detail"]
        self.assertIsNone(detail.alliance_rank)
        self.assertIsNone(detail.corporation_rank)
        self.assertEqual(detail.row.status, "none")
        self.assertEqual(detail.alliance_ranked, 3)
        april = self.page(self.viewer, "member_detail", self.recruit.pk, month=APRIL).context["detail"]
        self.assertEqual((april.alliance_rank, april.row.status), (1, "ok"))

    def test_the_member_detail_of_a_registered_member_keeps_its_rank(self):
        detail = self.page(self.viewer, "member_detail", self.mid.pk).context["detail"]
        self.assertEqual((detail.alliance_rank, detail.row.status), (2, "ok"))

    # Page text

    ROUTES = (
        ("alliance_members", ()),
        ("all_corporation_statistics", ()),
        ("leaderboard", ()),
        ("corporation_statistics_detail", (f.DEFAULT_CORP[0],)),
    )

    def test_finished_months_say_who_is_not_counted(self):
        for route, args in self.ROUTES:
            for month in (MARCH, (2026, 2)):
                with self.subTest(route=route, month=month):
                    self.assertContains(self.page(self.viewer, route, *args, month=month), NOT_COUNTED)

    def test_the_running_and_future_months_do_not_say_it(self):
        for route, args in self.ROUTES:
            for month in (APRIL, (2026, 5)):
                with self.subTest(route=route, month=month):
                    self.assertNotContains(self.page(self.viewer, route, *args, month=month), NOT_COUNTED)

    def test_the_member_detail_says_it_only_for_a_member_who_registered_later(self):
        self.assertContains(self.page(self.viewer, "member_detail", self.recruit.pk), NOT_COUNTED)
        self.assertNotContains(self.page(self.viewer, "member_detail", self.recruit.pk, month=APRIL), NOT_COUNTED)
        self.assertNotContains(self.page(self.viewer, "member_detail", self.mid.pk), NOT_COUNTED)
        self.assertNotContains(self.page(self.viewer, "member_detail", self.last.pk), NOT_COUNTED)
        self.assertContains(self.page(self.viewer, "member_detail", self.edge.pk), NOT_COUNTED)

    # Corporation scope

    def test_the_corporation_leaderboard_leaves_out_later_registrations(self):
        board = self.page(self.viewer, "leaderboard", corporation=f.DEFAULT_CORP[0]).context["boards"][-1]["board"]
        self.assertEqual(board.corporation_id, f.DEFAULT_CORP[0])
        self.assertEqual([(row.main_character_name, row.rank) for row in board.rows], [("Old Hand", 1), ("Mid March", 2)])
        self.assertEqual(board.ranked_count, 2)
        april = self.page(self.viewer, "leaderboard", month=APRIL, corporation=f.DEFAULT_CORP[0]).context["boards"][-1]
        self.assertEqual([row.main_character_name for row in april["board"].rows], ["April Recruit", "Old Hand"])

    def test_the_corporation_rank_leaves_out_later_registrations(self):
        corporation = f.DEFAULT_CORP[0]
        self.assertEqual(statistics_service.member_rank(self.old, 2026, 3, corporation_id=corporation), (1, 2))
        self.assertEqual(statistics_service.member_rank(self.mid, 2026, 3, corporation_id=corporation), (2, 2))
        self.assertEqual(statistics_service.member_rank(self.recruit, 2026, 3, corporation_id=corporation), (None, 2))
        self.assertEqual(statistics_service.member_rank(self.recruit, 2026, 4, corporation_id=corporation), (1, 2))
        self.assertEqual(statistics_service.member_rank(self.recruit, 2026, 3), (None, 3))


class RegistrationUtcTests(RegistrationMonthTests, TestCase):
    pass


@override_settings(TIME_ZONE="Asia/Shanghai")
class RegistrationEastTests(RegistrationMonthTests, TestCase):
    """UTC+8: 23:59:59 UTC on the last day of March is already April on the site clock."""
