# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""A month of PAP through the statistics pages, the exports and the history of the alliance.

Two corporations with mains and alts (one alt in another alliance), a corporation without PAP, a main who left
a corporation, a departed member, a corporation manager, a statistics manager, an FC lead and an ordinary
member. Fleets sit right on the month edges: 23:30 UTC on the last day of February and March and 00:30 UTC on
the first day of March and April. The site runs in a time zone far from UTC (once east, once west), so a
month, a day or a time taken from the site time zone instead of EVE time changes a total.

The tests follow the rules of docs/configuration.md: green when a target is reached, amber while the month runs, red only for
a finished month below the target; averages are per current main; corporation totals stay where the PAP was
earned; corporation managers see their own corporation only; the leaderboard follows the visibility setting;
a file holds what its page shows.
"""

import re
from datetime import datetime, timezone as dt_timezone
from decimal import Decimal
from unittest import mock
from zoneinfo import ZoneInfo

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse

from fleetops.models import AttendanceRecord, AuditLog, IncentivePeriod
from fleetops.services.assessment import status_label
from fleetops.services.incentives import rebuild_period

from . import factories as f
from .test_export_datasets import csv_rows, workbook_rows

UTC = dt_timezone.utc
NOW = datetime(2026, 4, 14, 12, 0, tzinfo=UTC)
FEBRUARY, MARCH, APRIL = (2026, 2), (2026, 3), (2026, 4)

ALL_STATS = f.MEMBER_PERMS + ["fleetops.view_all_stats"]
FLEET_LEAD = ALL_STATS + ["fleetops.view_all_fleets", "fleetops.manage_incentives"]
FOREIGN_CORP = (2_000_900, "Foreign Corp")
OLD_CORP = (2_000_777, "Old Corp")

ALPHA, BETA, THIRD = f.DEFAULT_CORP[1], f.OTHER_CORP[1], f.THIRD_CORP[1]


def utc(year, month, day, hour=19, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


def table_row(page, text):
    """The ``<tr>`` of the table row that contains ``text``."""
    for found in re.finditer(r"<tr\b[^>]*>.*?</tr>", page, re.S):
        if text in found.group(0):
            return found.group(0)
    raise AssertionError(f"No table row contains {text!r}.")


def trimmed(rows):
    """Rows without their trailing empty cells, so a file written by one writer compares with another's."""
    result = []
    for row in rows:
        row = list(row)
        while row and row[-1] == "":
            row.pop()
        result.append(row)
    return result


class StatisticsFlow:
    """The scenario and the checks; the two test classes below run it with a different site time zone."""

    zone = None

    def setUp(self):
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)
        clock = mock.patch("django.utils.timezone.now", return_value=NOW)
        clock.start()
        self.addCleanup(clock.stop)
        f.settings(history_alliance_ids=str(f.DEFAULT_ALLIANCE[0]), leaderboard_visibility="members", incentive_enabled=True)
        self.strat = f.fleet_type("StratOp", "1.00")
        self.roam = f.fleet_type("Roam", "0.50")

        # People. Everybody is a member of the alliance except `gone`, whose main is in another alliance.
        self.statsmgr = f.member("statsmgr", perms=ALL_STATS, main_name="Stats Manager", corporation=f.THIRD_CORP)
        self.lead = f.member("lead", perms=FLEET_LEAD, main_name="Fleet Lead", corporation=f.THIRD_CORP)
        self.fc = f.member("fc", perms=f.FC_PERMS, main_name="Flow FC", corporation=f.THIRD_CORP)
        self.mover = f.member("mover", perms=f.MEMBER_PERMS, main_name="Mover", corporation=f.THIRD_CORP)
        self.corpmgr = f.member("corpmgr", perms=f.CORP_MANAGEMENT_PERMS, main_name="Alpha Manager")
        self.a1 = f.member("a1", perms=f.MEMBER_PERMS, main_name="Alpha One")
        self.a2 = f.member("a2", perms=f.MEMBER_PERMS, main_name="Alpha 二")
        self.a3 = f.member("a3", perms=f.MEMBER_PERMS, main_name="Alpha Three")
        self.b1 = f.member("b1", perms=f.MEMBER_PERMS, main_name="Beta 一", corporation=f.OTHER_CORP)
        self.b2 = f.member("b2", perms=f.MEMBER_PERMS, main_name="Beta Two", corporation=f.OTHER_CORP)
        self.gone = f.member(
            "gone", main_name="Departed Pilot", corporation=FOREIGN_CORP, alliance=f.FOREIGN_ALLIANCE
        )
        # An alt of a1 in another corporation and another alliance.
        self.alt = f.add_alt(self.a1, "Alpha One Alt", corporation=FOREIGN_CORP, alliance=f.FOREIGN_ALLIANCE)

        self.f0 = self.fleet(utc(2026, 2, 28, 23, 30), self.strat, (self.a1, 4))
        self.f1 = self.fleet(
            utc(2026, 3, 1, 0, 30), self.strat, (self.a1, 2), (self.a1, 1, self.alt), self.a2, self.b1, self.gone
        )
        self.f2 = self.fleet(utc(2026, 3, 12), self.strat, self.a1, self.a2, self.b1, self.b2, self.mover)
        self.f3 = self.fleet(utc(2026, 3, 31, 23, 30), self.roam, self.a1, self.b2)
        self.f4 = self.fleet(utc(2026, 4, 1, 0, 30), self.strat, self.a1, self.a2, self.b1)
        self.f5 = self.fleet(utc(2026, 4, 10), self.strat, self.a1)
        self.f6 = self.fleet(utc(2026, 4, 12), self.roam, self.b1, self.b2)
        # Never counted: excluded from the statistics, and a fleet that is still running.
        self.excluded = self.fleet(utc(2026, 3, 20), self.strat, (self.a1, 5), counts_for_stats=False)
        self.running = f.create_operation(self.fc, started_at=NOW.replace(hour=11, minute=30), type_obj=self.strat)
        f.add_attendance(self.running, self.a2, value=3)
        # The mover earned its PAP in a corporation that has no members any more.
        AttendanceRecord.objects.filter(auth_user=self.mover).update(
            corporation_id=OLD_CORP[0], corporation_name=OLD_CORP[1]
        )
        f.statistics_target(*MARCH, "2.00", minimum=2)
        f.statistics_target(*APRIL, "1.00", minimum=1)

    # Helpers

    def fleet(self, when, type_obj, *pilots, **extra):
        """A counted fleet of the FC; a pilot is a user or ``(user, PAP)`` or ``(user, PAP, character)``."""
        operation = f.closed_operation(self.fc, when, type_obj=type_obj, **extra)
        for pilot in pilots:
            user, *rest = pilot if isinstance(pilot, tuple) else (pilot,)
            value = rest[0] if rest else 1
            character = rest[1] if len(rest) > 1 else None
            f.add_attendance(operation, user, character, value=value)
        return operation

    def page(self, viewer, route, *args, month=None, expect=200, **params):
        self.client.force_login(viewer)
        if month is not None:
            params.update(year=month[0], month=month[1])
        response = self.client.get(reverse(f"fleetops:{route}", args=args), params)
        self.assertEqual(response.status_code, expect, f"{route} {args} {params}")
        return response

    def html(self, viewer, route, *args, **kwargs):
        return self.page(viewer, route, *args, **kwargs).content.decode()

    def download(self, viewer, dataset, fmt="csv", expect=200, **params):
        self.client.force_login(viewer)
        response = self.client.get(reverse("fleetops:export", args=[dataset]), {"format": fmt, **params})
        self.assertEqual(response.status_code, expect, f"{dataset} {params}")
        return response

    def file_rows(self, viewer, dataset, **params):
        """The CSV rows of a download, after checking that the Excel file of the same request holds the same cells."""
        rows = csv_rows(self.download(viewer, dataset, "csv", **params))
        book = workbook_rows(self.download(viewer, dataset, "xlsx", **params))
        self.assertEqual(trimmed(book), trimmed(rows), f"{dataset}: Excel and CSV differ")
        return rows

    def overview(self, month):
        return self.page(self.statsmgr, "all_corporation_statistics", month=month).context["overview"]

    def by_name(self, rows):
        return {row.corporation_name: row for row in rows}

    # Time zone and month edges

    def test_months_are_eve_months_whatever_the_site_time_zone(self):
        totals = {}
        for month in (FEBRUARY, MARCH, APRIL):
            table = self.page(self.statsmgr, "alliance_members", month=month).context["table"]
            totals[month] = {row.main_character_name: row.pap for row in table.rows if row.pap}
        self.assertEqual(totals[FEBRUARY], {"Alpha One": 4})
        self.assertEqual(
            totals[MARCH],
            {"Alpha One": 5, "Alpha 二": 2, "Beta 一": 2, "Beta Two": 2, "Mover": 1},
        )
        self.assertEqual(totals[APRIL], {"Alpha One": 2, "Alpha 二": 1, "Beta 一": 2, "Beta Two": 1})

    def test_pages_show_eve_time(self):
        zone = ZoneInfo(self.zone)
        page = self.html(self.lead, "member_detail", self.a1.pk, month=MARCH)
        for fleet in (self.f1, self.f3):
            eve = fleet.started_at.strftime("%Y-%m-%d %H:%M")
            local = fleet.started_at.astimezone(zone).strftime("%Y-%m-%d %H:%M")
            self.assertIn(eve, page)
            self.assertNotEqual(eve, local)
            self.assertNotIn(local, page)
        history = self.html(self.statsmgr, "attendance_history_alliance", month=MARCH)
        self.assertIn("2026-03-31 23:30", history)
        self.assertIn("2026-03-01 00:30", history)
        self.assertNotIn(self.f3.started_at.astimezone(zone).strftime("%Y-%m-%d %H:%M"), history)

    def test_the_default_month_is_the_current_eve_month(self):
        page = self.page(self.statsmgr, "all_corporation_statistics")
        self.assertEqual(page.context["month_ref"].label, "2026-04")
        self.assertTrue(page.context["month_ref"].is_current)

    def test_the_dashboard_month_is_the_eve_month(self):
        # 23:30 UTC on the last day of March: already April on the site clock in the east.
        with mock.patch("django.utils.timezone.now", return_value=utc(2026, 3, 31, 23, 30)):
            response = self.page(self.a1, "dashboard")
        cards = {card["fleet_type"].name: card["month"] for card in response.context["dashboard_cards"]}
        self.assertEqual(cards, {"StratOp": 4, "Roam": 1})

    # Targets and the colour rule

    def test_a_finished_month_is_red_or_green_and_the_running_month_amber(self):
        march = self.html(self.statsmgr, "all_corporation_statistics", month=MARCH)
        alpha, beta, third = (table_row(march, name) for name in (ALPHA, BETA, THIRD))
        self.assertIn("fleetops-status-row-missed", alpha)
        self.assertIn("text-bg-danger", alpha)
        self.assertIn("Below target", alpha)
        self.assertIn("fleetops-status-row-met", beta)
        self.assertIn("text-bg-success", beta)
        self.assertIn("Target met", beta)
        self.assertIn("Below target", third)
        self.assertNotIn("text-bg-warning", march)

        april = self.html(self.statsmgr, "all_corporation_statistics", month=APRIL)
        alpha, beta, third = (table_row(april, name) for name in (ALPHA, BETA, THIRD))
        self.assertIn("text-bg-warning", alpha)
        self.assertIn("Not reached yet", alpha)
        self.assertIn("text-bg-success", beta)
        self.assertIn("Target met", beta)
        self.assertIn("Not reached yet", third)
        self.assertNotIn("text-bg-danger", april)

    def test_a_month_before_the_first_target_has_no_status(self):
        page = self.html(self.statsmgr, "all_corporation_statistics", month=FEBRUARY)
        self.assertIn("No monthly target applies to this month", page)
        self.assertNotIn("Below target", page)
        self.assertNotIn("Target met", page)
        rows = self.overview(FEBRUARY).rows
        self.assertEqual({row.status for row in rows}, {"none"})

    def test_averages_are_per_current_main_and_alts_count_for_the_main(self):
        overview = self.overview(MARCH)
        rows = self.by_name(overview.rows)
        # Four mains in Alpha; the alt in another alliance adds its PAP to the main (5 + 2 = 7).
        self.assertEqual((rows[ALPHA].mains, rows[ALPHA].total_pap, rows[ALPHA].average), (4, 7, Decimal("1.75")))
        self.assertEqual((rows[BETA].mains, rows[BETA].total_pap, rows[BETA].average), (2, 4, Decimal("2.00")))
        self.assertEqual((rows[THIRD].mains, rows[THIRD].total_pap, rows[THIRD].average), (4, 0, Decimal("0.00")))
        self.assertEqual((rows[ALPHA].status, rows[BETA].status, rows[THIRD].status), ("missed", "met", "missed"))
        self.assertEqual((rows[ALPHA].mains_with_pap, rows[ALPHA].mains_below_minimum), (2, 2))
        # PAP earned in a corporation without members is kept in one row so the totals add up.
        self.assertEqual((overview.other.corporation_name, overview.other.total_pap), ("Other corporations", 1))
        self.assertEqual(overview.alliance_pap, 12)
        self.assertEqual(overview.alliance_pap, sum(row.total_pap for row in overview.rows) + overview.other.total_pap)
        self.assertEqual((overview.mains, overview.mains_with_pap), (10, 5))
        self.assertEqual((overview.corporations_met, overview.corporations_with_target), (1, 3))

    def test_fleets_that_do_not_count_are_listed_and_not_added(self):
        april = self.overview(APRIL)
        self.assertEqual(april.not_counted["running"], 1)
        self.assertEqual(self.by_name(april.rows)[ALPHA].total_pap, 3)
        march = self.overview(MARCH)
        self.assertEqual(march.not_counted["excluded"], 1)
        self.assertEqual(self.by_name(march.rows)[ALPHA].total_pap, 7)
        page = self.html(self.statsmgr, "all_corporation_statistics", month=MARCH)
        self.assertIn("1 excluded", page)

    def test_the_corporation_totals_stay_where_the_pap_was_earned(self):
        # a2 joins Beta after earning 2 PAP in March as an Alpha main.
        main = self.a2.profile.main_character
        main.corporation_id, main.corporation_name = f.OTHER_CORP
        main.save()

        rows = self.by_name(self.overview(MARCH).rows)
        self.assertEqual((rows[ALPHA].mains, rows[ALPHA].total_pap, rows[ALPHA].average), (3, 7, Decimal("2.33")))
        self.assertEqual((rows[BETA].mains, rows[BETA].total_pap, rows[BETA].average), (3, 4, Decimal("1.33")))
        self.assertEqual((rows[ALPHA].status, rows[BETA].status), ("met", "missed"))
        # The members of Beta are the current ones with all their PAP, a2 included.
        table = self.page(self.statsmgr, "corporation_members_detail", f.OTHER_CORP[0], month=MARCH).context["table"]
        self.assertEqual({row.main_character_name: row.pap for row in table.rows}, {"Alpha 二": 2, "Beta 一": 2, "Beta Two": 2})
        # The corporation page counts the PAP where it was earned and the mains where they are now.
        stats = self.page(self.statsmgr, "corporation_statistics_detail", f.DEFAULT_CORP[0], month=MARCH).context["stats"]
        self.assertEqual((stats["main_character_count"], stats["total_pap"]), (3, 7))
        self.assertEqual(stats["average"], Decimal("2.33"))

    # Members and the inactive list

    def test_members_below_the_minimum_are_red_when_finished_and_amber_while_running(self):
        march = self.html(self.statsmgr, "alliance_members", month=MARCH, status="inactive")
        for name in ("Alpha Three", "Alpha Manager", "Stats Manager", "Fleet Lead", "Flow FC", "Mover"):
            row = table_row(march, name)
            self.assertIn("text-bg-danger", row, name)
            self.assertIn("Below minimum", row, name)
            self.assertNotIn("so far", row, name)
        self.assertNotIn("Alpha One", march)
        self.assertNotIn("Beta Two", march)
        self.assertNotIn("text-bg-warning", march)

        april = self.html(self.statsmgr, "alliance_members", month=APRIL, status="inactive")
        for name in ("Alpha Three", "Alpha Manager", "Stats Manager", "Fleet Lead", "Flow FC", "Mover"):
            row = table_row(april, name)
            self.assertIn("text-bg-warning", row, name)
            self.assertIn("Below minimum so far", row, name)
        self.assertNotIn("text-bg-danger", april)
        # Reaching the minimum is green: a member with exactly 2 PAP in March.
        everyone = self.html(self.statsmgr, "alliance_members", month=MARCH)
        self.assertIn("text-bg-success", table_row(everyone, "Alpha 二"))

    def test_the_corporation_manager_sees_the_inactive_list_of_the_own_corporation_only(self):
        table = self.page(self.corpmgr, "corporation_members", month=MARCH, status="inactive").context["table"]
        self.assertEqual(sorted(row.main_character_name for row in table.rows), ["Alpha Manager", "Alpha Three"])
        self.assertTrue(all(row.corporation_id == f.DEFAULT_CORP[0] for row in table.rows))
        # Nothing of the alliance or of another corporation (that one answers 404 with the reason).
        self.page(self.corpmgr, "alliance_members", month=MARCH, expect=403)
        self.page(self.corpmgr, "corporation_members_detail", f.OTHER_CORP[0], month=MARCH, expect=404)
        # The nav of the corporation manager lists no alliance pages.
        page = self.html(self.corpmgr, "corporation_members", month=MARCH)
        self.assertIsNone(re.search(rf'href="{re.escape(reverse("fleetops:alliance_members"))}(\?[^"]*)?"', page))
        self.assertNotIn("Test Corp Two", page)

    def test_the_statistics_manager_sees_every_corporation(self):
        table = self.page(self.statsmgr, "alliance_members", month=MARCH, status="inactive").context["table"]
        self.assertEqual({row.corporation_name for row in table.rows}, {ALPHA, THIRD})
        everyone = self.page(self.statsmgr, "alliance_members", month=MARCH).context["table"]
        self.assertEqual(len(everyone.rows), 10)
        self.assertNotIn("Departed Pilot", [row.main_character_name for row in everyone.rows])

    def test_a_member_cannot_open_the_member_pages(self):
        for route, args in (
            ("alliance_members", ()),
            ("corporation_members", ()),
            ("corporation_members_detail", (f.DEFAULT_CORP[0],)),
        ):
            self.page(self.a1, route, *args, month=MARCH, expect=403)

    # Leaderboard

    def test_the_leaderboard_for_everyone_with_access(self):
        context = self.page(self.a1, "leaderboard", month=MARCH).context
        alliance, corporation = (entry["board"] for entry in context["boards"])
        # Competition ranking: three members tied on 2 PAP, then the member with 1.
        self.assertEqual(
            [(row.rank, row.main_character_name, row.pap) for row in alliance.rows],
            [(1, "Alpha One", 5), (2, "Alpha 二", 2), (2, "Beta Two", 2), (2, "Beta 一", 2), (5, "Mover", 1)],
        )
        self.assertEqual([(row.rank, row.main_character_name) for row in corporation.rows], [(1, "Alpha One"), (2, "Alpha 二")])
        nav = self.html(self.a1, "my_statistics", month=MARCH)
        self.assertIn(reverse("fleetops:leaderboard"), nav)

    def test_the_leaderboard_for_statistics_managers_only(self):
        f.settings(leaderboard_visibility="managers")
        self.page(self.a1, "leaderboard", month=MARCH, expect=403)
        self.assertNotIn(reverse("fleetops:leaderboard"), self.html(self.a1, "my_statistics", month=MARCH))
        # A corporation manager sees the board of the own corporation, the statistics manager the alliance.
        boards = self.page(self.corpmgr, "leaderboard", month=MARCH).context["boards"]
        self.assertEqual([entry["board"].scope_label for entry in boards], [ALPHA])
        boards = self.page(self.statsmgr, "leaderboard", month=MARCH).context["boards"]
        self.assertEqual(len(boards), 1)
        self.assertEqual(boards[0]["board"].rows[0].main_character_name, "Alpha One")
        # The board of another corporation is a choice of the statistics manager.
        beta = self.page(self.statsmgr, "leaderboard", month=MARCH, corporation=f.OTHER_CORP[0]).context["boards"]
        self.assertIn(BETA, [entry["board"].scope_label for entry in beta])

    def test_ties_on_the_last_place_and_the_rank_of_the_viewer(self):
        f.settings(leaderboard_size=3)
        page = self.page(self.mover, "leaderboard", month=MARCH)
        board = page.context["boards"][0]["board"]
        # Three places are shown, the three members tied on the second place all are.
        self.assertEqual([row.main_character_name for row in board.rows], ["Alpha One", "Alpha 二", "Beta Two", "Beta 一"])
        self.assertEqual(board.viewer_row.rank, 5)
        content = page.content.decode()
        self.assertIn("Your rank", content)
        self.assertIn("#5", content)

    def test_my_statistics_shows_the_minimum_and_the_rank(self):
        march = self.page(self.a2, "my_statistics", month=MARCH).context
        self.assertEqual(march["minimum_status"], "ok")
        self.assertEqual((march["rank_card"]["rank"], march["rank_card"]["of"]), (2, 5))
        self.assertEqual(self.page(self.a3, "my_statistics", month=MARCH).context["minimum_status"], "below")
        self.assertEqual(self.page(self.a3, "my_statistics", month=APRIL).context["minimum_status"], "below_so_far")
        self.assertIsNone(self.page(self.a3, "my_statistics", month=MARCH).context["rank_card"])
        f.settings(leaderboard_visibility="managers")
        self.assertIsNone(self.page(self.a2, "my_statistics", month=MARCH).context["rank_card"])

    # Member detail and fleet links

    def test_member_detail_and_fleet_links_follow_the_permissions(self):
        link = reverse("fleetops:operation_detail", args=[self.f1.uuid])
        page = self.html(self.lead, "member_detail", self.a1.pk, month=MARCH)
        self.assertIn(link, page)
        # Only counted fleets are listed, and the alt is named as a character used.
        self.assertNotIn(str(self.excluded.uuid), page)
        self.assertNotIn(str(self.f0.uuid), page)
        self.assertIn("Alpha One Alt", page)

        for viewer in (self.statsmgr, self.corpmgr):
            page = self.html(viewer, "member_detail", self.a1.pk, month=MARCH)
            self.assertNotIn(link, page)
            self.assertIn("Alpha One", page)

    def test_member_detail_is_limited_to_the_members_the_viewer_may_see(self):
        self.page(self.corpmgr, "member_detail", self.a2.pk, month=MARCH)
        self.page(self.corpmgr, "member_detail", self.b1.pk, month=MARCH, expect=404)
        self.page(self.corpmgr, "member_detail", self.gone.pk, month=MARCH, expect=404)
        self.page(self.statsmgr, "member_detail", self.gone.pk, month=MARCH, expect=404)
        self.page(self.a1, "member_detail", self.a2.pk, month=MARCH, expect=404)
        own = self.page(self.a1, "member_detail", self.a1.pk, month=MARCH, expect=302)
        self.assertEqual(own["Location"], f"{reverse('fleetops:my_statistics')}?year=2026&month=3")

    # Exports equal the pages

    def test_the_corporations_file_holds_the_overview(self):
        overview = self.overview(MARCH)
        rows = self.file_rows(self.statsmgr, "corporations", year=2026, month=3)
        by_name = {row[0]: row for row in rows[1:]}
        for row in overview.rows:
            line = by_name[row.corporation_name]
            self.assertEqual(
                [int(value) for value in line[3:7]], [row.mains, row.mains_with_pap, row.mains_below_minimum, row.total_pap]
            )
            self.assertEqual(Decimal(line[7]), row.average)
            self.assertEqual(line[9], status_label(row.status))
        self.assertEqual(int(by_name["Other corporations"][6]), overview.other.total_pap)
        self.assertEqual(int(by_name["Alliance total"][6]), overview.alliance_pap)
        header = rows[0]
        self.assertEqual(int(by_name["Alliance total"][header.index("StratOp")]), 10)
        self.assertEqual(int(by_name["Alliance total"][header.index("Roam")]), 2)

    def test_the_members_file_holds_the_table_with_its_filters(self):
        table = self.page(self.statsmgr, "alliance_members", month=MARCH).context["table"]
        rows = self.file_rows(self.statsmgr, "members", year=2026, month=3)
        self.assertEqual(len(rows) - 1, len(table.rows))
        for line, row in zip(rows[1:], table.rows):
            self.assertEqual(line[1], row.main_character_name)
            self.assertEqual(line[0], "" if row.rank is None else str(row.rank))
            self.assertEqual((int(line[5]), int(line[6]), int(line[7])), (row.pap, row.fleets, row.characters))
            self.assertEqual(line[8], status_label(row.status))
        last = {line[1]: line[9] for line in rows[1:]}
        # The last PAP is the latest counted fleet of the member in EVE time, April 1 00:30 for a2 (not the running fleet).
        self.assertEqual(last["Alpha One"], "2026-04-10 19:00")
        self.assertEqual(last["Alpha 二"], "2026-04-01 00:30")

        inactive = self.page(self.statsmgr, "alliance_members", month=MARCH, status="inactive", sort="pap").context["table"]
        rows = self.file_rows(self.statsmgr, "members", year=2026, month=3, status="inactive", sort="pap")
        self.assertEqual([line[1] for line in rows[1:]], [row.main_character_name for row in inactive.rows])
        self.assertTrue(all(line[8] == "Below minimum" for line in rows[1:]))
        running = self.file_rows(self.statsmgr, "members", year=2026, month=4, status="inactive")
        self.assertTrue(all(line[8] == "Below minimum so far" for line in running[1:]))

    def test_the_corporation_manager_downloads_the_own_corporation_only(self):
        rows = self.file_rows(self.corpmgr, "members", year=2026, month=3, corporation=f.DEFAULT_CORP[0])
        self.assertEqual({line[2] for line in rows[1:]}, {ALPHA})
        self.assertEqual(len(rows) - 1, 4)
        self.download(self.corpmgr, "members", expect=403, year=2026, month=3)
        self.download(self.corpmgr, "members", expect=404, year=2026, month=3, corporation=f.OTHER_CORP[0])
        self.download(self.corpmgr, "corporations", expect=403, year=2026, month=3)
        self.download(self.a1, "members", expect=403, year=2026, month=3)

    def test_the_member_fleets_file_holds_the_fleets_of_the_page(self):
        fleets = self.page(self.lead, "member_detail", self.a1.pk, month=MARCH).context["detail"].fleets
        rows = self.file_rows(self.lead, "member-fleets", user=self.a1.pk, year=2026, month=3)
        self.assertEqual(len(rows) - 1, len(fleets))
        self.assertEqual(sum(int(line[7]) for line in rows[1:]), 5)
        self.assertEqual(sorted(line[0] for line in rows[1:]), ["2026-03-01 00:30", "2026-03-12 19:00", "2026-03-31 23:30"])
        self.download(self.corpmgr, "member-fleets", expect=404, user=self.b1.pk, year=2026, month=3)

    def test_the_fc_file_holds_the_fc_page(self):
        rows = self.page(self.statsmgr, "all_fc_statistics", month=MARCH).context["rows"]
        self.assertEqual([(row.main_name, row.fleets, row.points) for row in rows], [("Flow FC", 3, Decimal("2.5"))])
        lines = self.file_rows(self.statsmgr, "fcs", year=2026, month=3)
        self.assertEqual(lines[1][:3], ["Flow FC", "3", "2.50"])

    def test_the_history_file_holds_the_history_page(self):
        page = self.page(self.statsmgr, "attendance_history_alliance", month=MARCH)
        rows = self.file_rows(self.statsmgr, "attendance", scope="alliance", year=2026, month=3)
        self.assertEqual(len(rows) - 1, page.context["page"].paginator.count)
        self.assertEqual(sum(int(line[9]) for line in rows[1:]), page.context["total"])
        self.assertEqual(page.context["total"], 12)
        self.assertNotIn("Departed Pilot", {line[6] for line in rows[1:]})
        self.assertEqual(max(line[0] for line in rows[1:]), "2026-03-31 23:30")
        mine = self.file_rows(self.a1, "attendance", scope="me", year=2026, month=3)
        self.assertEqual(sum(int(line[9]) for line in mine[1:]), 5)

    def test_downloads_are_audited_and_named_by_month(self):
        response = self.download(self.statsmgr, "corporations", "xlsx", year=2026, month=3)
        self.assertIn("fleetops-corporations-alliance-2026-03.xlsx", response["Content-Disposition"])
        self.assertEqual(response["Cache-Control"], "private, no-store")
        entry = AuditLog.objects.filter(action="statistics.export").latest("pk")
        self.assertEqual(entry.object_id, "corporations:alliance:2026-03")
        self.assertEqual(entry.new_value["format"], "xlsx")
        self.assertEqual(entry.actor, self.statsmgr)

    # The export buttons of the pages

    def export_links(self, page):
        """The Excel and CSV links of a page, unescaped, by format."""
        links = [link.replace("&amp;", "&") for link in re.findall(r'href="([^"]*/statistics/export/[^"]*)"', page)]
        return {re.search(r"format=(\w+)", link).group(1): link for link in links}

    def follow(self, viewer, link):
        self.client.force_login(viewer)
        response = self.client.get(link)
        self.assertEqual(response.status_code, 200, link)
        return response

    def test_the_buttons_of_the_members_page_download_what_the_page_shows(self):
        page = self.page(self.corpmgr, "corporation_members", month=MARCH, status="inactive", sort="pap")
        links = self.export_links(page.content.decode())
        self.assertEqual(set(links), {"xlsx", "csv"})
        rows = csv_rows(self.follow(self.corpmgr, links["csv"]))
        self.assertEqual([line[1] for line in rows[1:]], [row.main_character_name for row in page.context["table"].rows])
        self.assertEqual({line[2] for line in rows[1:]}, {ALPHA})
        self.assertEqual(trimmed(workbook_rows(self.follow(self.corpmgr, links["xlsx"]))), trimmed(rows))

    def test_the_corporation_page_exports_the_members_of_its_corporation(self):
        page = self.html(self.statsmgr, "corporation_statistics_detail", f.OTHER_CORP[0], month=MARCH)
        rows = csv_rows(self.follow(self.statsmgr, self.export_links(page)["csv"]))
        self.assertEqual({line[2] for line in rows[1:]}, {BETA})
        self.assertEqual(len(rows) - 1, 2)
        own = self.html(self.corpmgr, "corporation_statistics", month=MARCH)
        rows = csv_rows(self.follow(self.corpmgr, self.export_links(own)["csv"]))
        self.assertEqual({line[2] for line in rows[1:]}, {ALPHA})

    def test_the_incentive_review_exports_the_period(self):
        period = IncentivePeriod.objects.create(year=2026, month=3, budget=1_000_000, minimum_fleets=1)
        rebuild_period(period)
        page = self.html(self.lead, "incentive_review", month=MARCH)
        links = self.export_links(page)
        self.assertEqual(set(links), {"xlsx", "csv"})
        self.assertIn(f"period={period.pk}", links["csv"])
        rows = csv_rows(self.follow(self.lead, links["csv"]))
        self.assertEqual([line[:3] for line in rows[1:]], [["Flow FC", "3", "2.50"]])
        self.assertEqual(rows[1][-1], "Review")
        self.assertEqual(trimmed(workbook_rows(self.follow(self.lead, links["xlsx"]))), trimmed(rows))
        self.download(self.statsmgr, "incentives", expect=403, period=period.pk)


    # History

    def test_history_paging_keeps_the_month_filter(self):
        with mock.patch("fleetops.views_history.HISTORY_PAGE_SIZE", 4):
            first = self.html(self.statsmgr, "attendance_history_alliance", month=MARCH)
            second = self.html(self.statsmgr, "attendance_history_alliance", month=MARCH, page=2)
            everything = self.html(self.statsmgr, "attendance_history_alliance")
        self.assertIn("Page 1 of 3", first)
        self.assertIn("Page 2 of 3", second)
        for page in (first, second):
            links = [link.replace("&amp;", "&") for link in re.findall(r'href="([^"]*page=[^"]*)"', page)]
            self.assertTrue(links)
            self.assertTrue(all("year=2026" in link and "month=3" in link for link in links), links)
            exports = [link.replace("&amp;", "&") for link in re.findall(r'href="([^"]*/export/[^"]*)"', page)]
            self.assertTrue(all("year=2026" in link and "month=3" in link for link in exports), exports)
        # Without a month the whole retention window is listed (March and April), and the file agrees.
        self.assertNotIn("Showing the fleets that started in", everything)
        rows = csv_rows(self.download(self.statsmgr, "attendance", scope="alliance"))
        self.assertEqual(len(rows) - 1, 11 + 7)

    def test_the_corporation_history_lists_the_pap_earned_in_the_corporation(self):
        page = self.page(self.corpmgr, "attendance_history_corporation", month=MARCH)
        self.assertEqual(page.context["total"], 7)
        rows = self.file_rows(self.corpmgr, "attendance", scope="corporation", year=2026, month=3)
        self.assertEqual({line[7] for line in rows[1:]}, {ALPHA})
        self.page(self.corpmgr, "attendance_history_alliance", month=MARCH, expect=403)
        self.download(self.corpmgr, "attendance", expect=403, scope="alliance", year=2026, month=3)


@override_settings(TIME_ZONE="Asia/Shanghai")
class StatisticsFlowEastTests(StatisticsFlow, TestCase):
    """UTC+8: 23:30 UTC on the last day of a month is 07:30 on the first day of the next month."""

    zone = "Asia/Shanghai"


@override_settings(TIME_ZONE="America/Los_Angeles")
class StatisticsFlowWestTests(StatisticsFlow, TestCase):
    """UTC-7/-8: 00:30 UTC on the first day of a month is the evening of the last day of the month before."""

    zone = "America/Los_Angeles"
