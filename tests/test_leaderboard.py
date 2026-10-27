# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""The PAP leaderboard: places, ties, the viewer's rank, who sees which board and the query budget."""

import html
import re
from datetime import datetime, timezone as dt_timezone
from unittest import mock

from allianceauth.eveonline.models import EveCharacter
from django.db import connection
from django.test import Client, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from fleetops.services.members import leaderboard

from . import factories as f

UTC = dt_timezone.utc

CORP = f.CORP_MANAGEMENT_PERMS
ALL_STATS = f.MEMBER_PERMS + ["fleetops.view_all_stats"]


def utc(*args):
    return datetime(*args, tzinfo=UTC)


def clock(moment):
    """The site clock stands at ``moment``."""
    return mock.patch("django.utils.timezone.now", return_value=moment)


def new_fc(username="fc"):
    """An FC whose account is disabled, so the FC is not a member in the statistics."""
    return f.member(username, main_name=username.upper(), perms=f.FC_PERMS, active=False)


def fleet(fc, when, *pilots, type_obj=None, **extra):
    """A counted fleet with PAP for ``(user, value)`` pairs (or users, one PAP each)."""
    operation = f.closed_operation(fc, when, type_obj=type_obj, **extra)
    for pilot in pilots:
        user, value = pilot if isinstance(pilot, tuple) else (pilot, 1)
        f.add_attendance(operation, user, value=value)
    return operation


def names(rows):
    return [row.main_character_name for row in rows]


def set_alliances(*alliances):
    f.settings(history_alliance_ids=",".join(str(alliance[0]) for alliance in alliances))


class LeaderboardTestCase(TestCase):
    def setUp(self):
        patcher = clock(utc(2026, 4, 15, 12))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.fc = new_fc()

    def pilots(self, spec, *, when=utc(2026, 3, 10, 18), **kwargs):
        """Members with PAP in March 2026: ``{name: pap}``; one fleet per pilot unless ``fleets`` says more."""
        users = {}
        for name, value in spec.items():
            users[name] = f.member(name.lower(), main_name=name, **kwargs)
            if value:
                fleet(self.fc, when, (users[name], value))
        return users


class LeaderboardServiceTests(LeaderboardTestCase):
    def board(self, **kwargs):
        return leaderboard(2026, 3, **kwargs)

    def test_default_size_is_the_setting(self):
        f.settings(leaderboard_size=3)
        self.pilots({"A": 9, "B": 8, "C": 7, "D": 6, "E": 5})

        board = self.board()

        self.assertEqual(board.size, 3)
        self.assertEqual(names(board.rows), ["A", "B", "C"])
        self.assertEqual(board.ranked_count, 5)

    def test_default_setting_is_twenty_places(self):
        self.pilots({f"P{index:02d}": index + 1 for index in range(25)})

        board = self.board()

        self.assertEqual((board.size, len(board.rows)), (20, 20))
        self.assertEqual(board.rows[0].main_character_name, "P24")
        self.assertEqual(board.ranked_count, 25)

    def test_explicit_size(self):
        f.settings(leaderboard_size=3)
        self.pilots({"A": 9, "B": 8, "C": 7, "D": 6})

        self.assertEqual(names(self.board(size=2).rows), ["A", "B"])
        self.assertEqual(names(self.board(size=10).rows), ["A", "B", "C", "D"])
        self.assertEqual(self.board(size=2).size, 2)

    def test_ties_on_the_last_place_are_all_shown(self):
        f.settings(leaderboard_size=3)
        self.pilots({"A": 9, "B": 7, "C": 5, "D": 5, "E": 5, "F": 2})

        board = self.board()

        self.assertEqual(names(board.rows), ["A", "B", "C", "D", "E"])
        self.assertEqual([row.rank for row in board.rows], [1, 2, 3, 3, 3])
        self.assertEqual(board.ranked_count, 6)

    def test_places_skipped_by_a_tie_are_not_filled(self):
        self.pilots({"A": 9, "B": 5, "C": 5, "D": 4, "E": 3})

        # Ranks are 1, 2, 2, 4, 5: with three places the third place belongs to the tie, so D (rank 4) is out.
        board = self.board(size=3)

        self.assertEqual([(row.main_character_name, row.rank) for row in board.rows], [("A", 1), ("B", 2), ("C", 2)])

    def test_a_tie_for_the_first_places_is_shown_whole(self):
        self.pilots({"A": 5, "B": 5, "C": 5, "D": 1})

        board = self.board(size=2)

        self.assertEqual(names(board.rows), ["A", "B", "C"])
        self.assertEqual({row.rank for row in board.rows}, {1})

    def test_members_without_pap_are_never_shown(self):
        self.pilots({"A": 3, "B": 0, "C": 0})

        board = self.board(size=10)

        self.assertEqual(names(board.rows), ["A"])
        self.assertEqual(board.ranked_count, 1)

    def test_nobody_with_pap(self):
        self.pilots({"A": 0})

        board = self.board()

        self.assertEqual((board.rows, board.ranked_count, board.viewer_row), ([], 0, None))

    def test_tied_members_are_ordered_by_fleets_then_name(self):
        users = self.pilots({"Zed": 0, "Amy": 0, "Cat": 0, "Bob": 0})
        fleet(self.fc, utc(2026, 3, 3, 18), (users["Zed"], 2), (users["Amy"], 1), (users["Cat"], 1), (users["Bob"], 2))
        fleet(self.fc, utc(2026, 3, 4, 18), (users["Amy"], 1), (users["Cat"], 1))

        board = self.board(size=10)

        # All four have 2 PAP; Amy and Cat were in two fleets, Bob and Zed in one.
        self.assertEqual(names(board.rows), ["Amy", "Cat", "Bob", "Zed"])
        self.assertEqual({row.rank for row in board.rows}, {1})

    def test_rows_carry_the_totals_of_the_member(self):
        users = self.pilots({"A": 4})
        alt = f.add_alt(users["A"], "A Alt", corporation=f.OTHER_CORP, alliance=f.FOREIGN_ALLIANCE)
        operation = f.closed_operation(self.fc, utc(2026, 3, 20, 18))
        f.add_attendance(operation, users["A"], alt, value=3)

        row = self.board().rows[0]

        # The alt in another corporation and alliance adds to the main.
        self.assertEqual((row.pap, row.fleets, row.characters, row.rank), (7, 2, 2, 1))
        self.assertEqual(row.user_id, users["A"].pk)
        self.assertEqual((row.corporation_id, row.corporation_name), f.DEFAULT_CORP)

    def test_uncounted_fleets_do_not_count(self):
        users = self.pilots({"A": 1})
        fleet(self.fc, utc(2026, 3, 11, 18), (users["A"], 9), review_status="pending")
        fleet(self.fc, utc(2026, 3, 12, 18), (users["A"], 9), counts_for_stats=False)

        self.assertEqual(self.board().rows[0].pap, 1)

    def test_users_outside_the_population_are_not_listed(self):
        self.pilots({"A": 1})
        self.pilots({"Gone": 9}, active=False)
        self.pilots({"Biomassed": 9}, corporation=f.DOOMHEIM_CORP, alliance=None)

        self.assertEqual(names(self.board().rows), ["A"])

    def test_departed_members_are_hidden_with_member_alliances(self):
        self.pilots({"A": 1, "Leaver": 9}, alliance=f.FOREIGN_ALLIANCE)
        self.pilots({"B": 2})
        self.assertEqual(names(self.board().rows), ["Leaver", "B", "A"])

        set_alliances(f.DEFAULT_ALLIANCE)

        self.assertEqual(names(self.board().rows), ["B"])

    def test_alliance_board_and_corporation_board(self):
        self.pilots({"A1": 5, "A2": 3})
        self.pilots({"B1": 9, "B2": 1}, corporation=f.OTHER_CORP)

        alliance = self.board()
        corporation = self.board(corporation_id=f.DEFAULT_CORP[0])

        self.assertEqual(names(alliance.rows), ["B1", "A1", "A2", "B2"])
        self.assertEqual((alliance.scope_label, alliance.corporation_id), ("Alliance", None))
        self.assertEqual(names(corporation.rows), ["A1", "A2"])
        self.assertEqual([row.rank for row in corporation.rows], [1, 2])
        self.assertEqual((corporation.scope_label, corporation.corporation_id), (f.DEFAULT_CORP[1], f.DEFAULT_CORP[0]))
        self.assertEqual(corporation.ranked_count, 2)

    def test_corporation_board_uses_the_current_members(self):
        users = self.pilots({"Mover": 5, "Stayer": 3})
        main = f.main_of(users["Mover"])
        main.corporation_id, main.corporation_name = f.OTHER_CORP
        main.save()

        self.assertEqual(names(self.board(corporation_id=f.DEFAULT_CORP[0]).rows), ["Stayer"])
        moved = self.board(corporation_id=f.OTHER_CORP[0])
        self.assertEqual([(row.main_character_name, row.pap) for row in moved.rows], [("Mover", 5)])

    def test_label_of_a_corporation_without_members(self):
        board = self.board(corporation_id=9_999_999)

        self.assertEqual((board.rows, board.scope_label), ([], "Corporation"))

    def test_the_month_is_eve_time(self):
        users = self.pilots({"A": 0})
        fleet(self.fc, utc(2026, 3, 31, 23, 30), (users["A"], 3))
        fleet(self.fc, utc(2026, 4, 1, 0, 0), (users["A"], 5))

        for zone in ("UTC", "Asia/Shanghai", "America/Los_Angeles"):
            with self.subTest(zone=zone), override_settings(TIME_ZONE=zone):
                self.assertEqual(self.board().rows[0].pap, 3)
                self.assertEqual(leaderboard(2026, 4).rows[0].pap, 5)

    def test_a_future_month_is_empty(self):
        self.pilots({"A": 5})

        self.assertEqual(leaderboard(2026, 9).rows, [])

    def test_the_month_is_on_the_board(self):
        self.assertEqual(self.board().month.label, "2026-03")


class ViewerRowTests(LeaderboardTestCase):
    def setUp(self):
        super().setUp()
        self.users = self.pilots({"A": 9, "B": 8, "C": 7, "D": 6, "E": 5, "F": 0})

    def test_viewer_outside_the_places_gets_a_row_with_the_rank(self):
        board = leaderboard(2026, 3, size=3, viewer=self.users["E"])

        row = board.viewer_row
        self.assertEqual((row.main_character_name, row.rank, row.pap), ("E", 5, 5))
        self.assertEqual(board.ranked_count, 5)

    def test_viewer_inside_the_places_has_no_extra_row(self):
        self.assertIsNone(leaderboard(2026, 3, size=3, viewer=self.users["B"]).viewer_row)

    def test_viewer_without_pap_has_no_row(self):
        self.assertIsNone(leaderboard(2026, 3, size=3, viewer=self.users["F"]).viewer_row)

    def test_a_user_id_is_accepted(self):
        self.assertEqual(leaderboard(2026, 3, size=3, viewer=self.users["D"].pk).viewer_row.rank, 4)

    def test_viewer_outside_the_scope_has_no_row(self):
        board = leaderboard(2026, 3, size=3, corporation_id=f.OTHER_CORP[0], viewer=self.users["E"])

        self.assertIsNone(board.viewer_row)

    def test_viewer_row_of_a_corporation_board(self):
        board = leaderboard(2026, 3, size=2, corporation_id=f.DEFAULT_CORP[0], viewer=self.users["D"])

        self.assertEqual(board.viewer_row.rank, 4)

    def test_without_a_viewer(self):
        self.assertIsNone(leaderboard(2026, 3, size=3).viewer_row)

    def test_a_viewer_tied_with_the_last_place_is_inside(self):
        tie = self.pilots({"G": 7})

        board = leaderboard(2026, 3, size=3, viewer=tie["G"])

        self.assertIsNone(board.viewer_row)
        self.assertIn("G", names(board.rows))


class LeaderboardQueryBudgetTests(LeaderboardTestCase):
    def count(self, **kwargs):
        with CaptureQueriesContext(connection) as queries:
            leaderboard(2026, 3, **kwargs)
        return len(queries)

    def populate(self, start, count):
        pilots = [
            f.member(f"load{start + index}", corporation=(f.DEFAULT_CORP, f.OTHER_CORP, f.THIRD_CORP)[index % 3])
            for index in range(count)
        ]
        for number in range(count // 2):
            fleet(self.fc, utc(2026, 3, 1 + number % 27, 12), *pilots[number % 2 :: 2])

    def test_the_same_queries_for_a_larger_dataset(self):
        self.populate(0, 9)
        viewer = f.member("viewer")
        small = (self.count(viewer=viewer), self.count(corporation_id=f.OTHER_CORP[0], viewer=viewer))

        self.populate(100, 27)
        large = (self.count(viewer=viewer), self.count(corporation_id=f.OTHER_CORP[0], viewer=viewer))

        self.assertEqual(small, large)
        self.assertLessEqual(max(large), 6)


class PageTestCase(LeaderboardTestCase):
    """Five members in two corporations with PAP in March 2026 and a client per role."""

    def setUp(self):
        super().setUp()
        self.plain = f.member("plain", main_name="Plain", perms=f.MEMBER_PERMS)
        self.manager = f.member("manager", main_name="Manager", perms=CORP)
        self.stats = f.member("stats", main_name="Statsman", perms=ALL_STATS)
        self.mate = f.member("mate", main_name="Mate")
        self.rival = f.member("rival", main_name="Rival", corporation=f.OTHER_CORP)
        fleet(
            self.fc,
            utc(2026, 3, 10, 18),
            (self.plain, 4),
            (self.manager, 3),
            (self.stats, 2),
            (self.mate, 6),
            (self.rival, 9),
        )

    def get(self, user, **params):
        params.setdefault("year", "2026")
        params.setdefault("month", "3")
        client = Client(raise_request_exception=False)
        client.force_login(user)
        return client.get(reverse("fleetops:leaderboard"), params)

    def boards(self, response):
        return {entry["title"]: [item["row"].main_character_name for item in entry["items"]] for entry in response.context["boards"]}


class LeaderboardPageTests(PageTestCase):
    def test_a_member_sees_the_alliance_board_and_their_own_corporation_board(self):
        response = self.get(self.plain)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            self.boards(response),
            {
                "Alliance: top 20": ["Rival", "Mate", "Plain", "Manager", "Statsman"],
                f"{f.DEFAULT_CORP[1]}: top 20": ["Mate", "Plain", "Manager", "Statsman"],
            },
        )
        self.assertEqual(response.context["corporations"], [])
        self.assertNotIn('name="corporation"', response.content.decode())

    def test_the_board_shows_rank_pap_and_fleets(self):
        page = html.unescape(self.get(self.plain).content.decode())

        self.assertIn("PAP Leaderboard", page)
        self.assertIn("2026-03", page)
        self.assertRegex(page, r"fleetops-rank\">1</span>")
        self.assertIn("<strong>9</strong>", page)

    def test_ranks_and_ties_are_shown(self):
        extra = f.member("extra", main_name="Extra")
        fleet(self.fc, utc(2026, 3, 11, 18), (extra, 6))

        ranks = [(item["row"].main_character_name, item["row"].rank) for item in self.get(self.plain).context["boards"][0]["items"]]

        self.assertEqual(ranks[:4], [("Rival", 1), ("Extra", 2), ("Mate", 2), ("Plain", 4)])

    def test_the_size_setting_limits_the_boards(self):
        f.settings(leaderboard_size=3)

        response = self.get(self.plain)

        self.assertEqual(self.boards(response)["Alliance: top 3"], ["Rival", "Mate", "Plain"])
        self.assertIn(f"{f.DEFAULT_CORP[1]}: top 3", self.boards(response))

    def test_the_viewer_outside_the_places_sees_their_rank(self):
        f.settings(leaderboard_size=3)

        page = self.get(self.stats).content.decode()

        self.assertIn("Your rank: <strong>#5</strong> of 5", page)
        self.assertIn("Your rank: <strong>#4</strong> of 4", page)

    def test_no_rank_line_inside_the_places(self):
        self.assertNotIn("Your rank", self.get(self.plain).content.decode())

    def test_the_viewer_is_highlighted_in_the_table(self):
        response = self.get(self.plain)

        self.assertTrue(all(item["is_viewer"] for entry in response.context["boards"] for item in entry["items"] if item["row"].user_id == self.plain.pk))
        page = response.content.decode()
        self.assertEqual(page.count('<tr class="table-active">'), 2)
        self.assertEqual(page.count(">You</span>"), 2)

    def test_ties_on_the_last_place_are_all_shown_on_the_page(self):
        f.settings(leaderboard_size=3)
        for index in range(3):
            tied = f.member(f"tie{index}", main_name=f"Tie {index}")
            fleet(self.fc, utc(2026, 3, 12, 18), (tied, 3))

        # Manager and the three Tie members share the fourth place with 3 PAP each.
        boards = self.boards(self.get(self.plain))

        self.assertEqual(boards["Alliance: top 3"], ["Rival", "Mate", "Plain"])
        f.settings(leaderboard_size=4)
        self.assertEqual(
            sorted(self.boards(self.get(self.plain))["Alliance: top 4"][3:]), ["Manager", "Tie 0", "Tie 1", "Tie 2"]
        )

    def test_a_board_without_pap_says_so(self):
        page = self.get(self.plain, year="2026", month="1").content.decode()

        self.assertEqual(page.count("Nobody has PAP in this month yet."), 2)

    def test_the_month_is_eve_time(self):
        fleet(self.fc, utc(2026, 3, 31, 23, 30), (self.plain, 40))

        with override_settings(TIME_ZONE="Asia/Shanghai"):
            march = self.boards(self.get(self.plain))["Alliance: top 20"]
            april = self.boards(self.get(self.plain, month="4"))["Alliance: top 20"]

        self.assertEqual(march[0], "Plain")
        self.assertEqual(april, [])

    def test_invalid_month_shows_the_current_month(self):
        page = self.get(self.plain, year="x", month="99").content.decode()

        self.assertIn("2026-04", page)

    def test_month_links_keep_the_chosen_corporation(self):
        page = self.get(self.stats, corporation=str(f.OTHER_CORP[0])).content.decode()

        self.assertIn('<input type="hidden" name="corporation" value="%d">' % f.OTHER_CORP[0], page)
        previous = re.search(r'href="([^"]*)"[^>]*aria-label="Previous month: ', html.unescape(page))
        self.assertIn(f"corporation={f.OTHER_CORP[0]}", previous.group(1))

    def test_a_month_older_than_the_retention_window_is_noted(self):
        old = self.get(self.plain, year="2025", month="3").content.decode()

        self.assertIn("PAP older than 365 days is removed", old)
        self.assertNotIn("PAP older than", self.get(self.plain).content.decode())

    def test_the_rank_line_links_to_the_viewers_own_statistics(self):
        f.settings(leaderboard_size=3)

        page = html.unescape(self.get(self.manager).content.decode())

        self.assertIn(f'href="{reverse("fleetops:member_detail", args=[self.manager.pk])}?year=2026&month=3">My statistics</a>', page)

    def test_a_member_without_a_corporation_ticker_shows_the_corporation_name(self):
        EveCharacter.objects.filter(pk=f.main_of(self.rival).pk).update(corporation_ticker="")

        page = self.get(self.plain).content.decode()

        self.assertNotIn("[]", page)
        self.assertIn(f'<td title="{f.OTHER_CORP[1]}">{f.OTHER_CORP[1]}</td>', page)
        self.assertIn('<td title="Test Corp One">[TEST ]</td>', page)

    def test_no_afat_anywhere(self):
        self.assertNotIn("AFAT", self.get(self.stats).content.decode())


class LeaderboardVisibilityTests(PageTestCase):
    """The leaderboard_visibility setting against the roles: "members" shows the boards to everyone with FleetOps access, "managers" only to statistics managers."""

    def test_members_setting_every_role_sees_the_leaderboard(self):
        f.settings(leaderboard_visibility="members")

        for user in (self.plain, self.manager, self.stats):
            with self.subTest(user=user.username):
                self.assertEqual(self.get(user).status_code, 200)

    def test_managers_setting_closes_the_leaderboard_for_members(self):
        f.settings(leaderboard_visibility="managers")

        self.assertEqual(self.get(self.plain).status_code, 403)
        self.assertEqual(self.get(self.manager).status_code, 200)
        self.assertEqual(self.get(self.stats).status_code, 200)

    def test_members_see_both_boards_by_default(self):
        self.assertEqual(len(self.get(self.plain).context["boards"]), 2)
        self.assertEqual(f.settings().leaderboard_visibility, "members")

    def test_a_corporation_manager_sees_only_their_corporation_when_the_board_is_for_managers(self):
        f.settings(leaderboard_visibility="managers")

        response = self.get(self.manager)

        self.assertEqual(list(self.boards(response)), [f"{f.DEFAULT_CORP[1]}: top 20"])
        self.assertEqual(response.context["corporations"], [])

    def test_a_corporation_manager_sees_the_alliance_board_while_members_may(self):
        self.assertEqual(len(self.get(self.manager).context["boards"]), 2)

    def test_view_all_stats_sees_the_alliance_whatever_the_setting(self):
        for visibility in ("members", "managers"):
            f.settings(leaderboard_visibility=visibility)
            with self.subTest(visibility=visibility):
                response = self.get(self.stats)
                self.assertEqual(response.status_code, 200)
                self.assertIn("Alliance: top 20", self.boards(response))

    def test_the_corporation_parameter_is_ignored_without_view_all_stats(self):
        for visibility in ("members", "managers"):
            f.settings(leaderboard_visibility=visibility)
            for user in (self.plain, self.manager):
                if visibility == "managers" and user is self.plain:
                    continue
                with self.subTest(visibility=visibility, user=user.username):
                    response = self.get(user, corporation=str(f.OTHER_CORP[0]))
                    self.assertEqual(response.status_code, 200)
                    self.assertIn(f"{f.DEFAULT_CORP[1]}: top 20", self.boards(response))
                    self.assertNotIn(f"{f.OTHER_CORP[1]}: top 20", self.boards(response))
                    self.assertEqual(response.context["corporation_id"], f.DEFAULT_CORP[0])

    def test_view_all_stats_chooses_the_corporation(self):
        response = self.get(self.stats, corporation=str(f.OTHER_CORP[0]))

        self.assertEqual(self.boards(response)[f"{f.OTHER_CORP[1]}: top 20"], ["Rival"])
        self.assertEqual(response.context["corporation_id"], f.OTHER_CORP[0])
        self.assertEqual(
            [corporation_id for corporation_id, _name in response.context["corporations"]],
            [f.DEFAULT_CORP[0], f.OTHER_CORP[0]],
        )
        page = response.content.decode()
        self.assertIn(f'<option value="{f.OTHER_CORP[0]}" selected>', page)

    def test_view_all_stats_defaults_to_their_own_corporation(self):
        self.assertEqual(self.get(self.stats).context["corporation_id"], f.DEFAULT_CORP[0])

    def test_an_invalid_corporation_falls_back_to_the_own_corporation(self):
        for value in ("x", "-5", "0", "", "1e3"):
            with self.subTest(value=value):
                self.assertEqual(self.get(self.stats, corporation=value).context["corporation_id"], f.DEFAULT_CORP[0])

    def test_an_unknown_corporation_gives_an_empty_board(self):
        response = self.get(self.stats, corporation="9999999")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.boards(response)["Corporation: top 20"], [])

    def test_the_navigation_item_follows_the_setting(self):
        link = reverse("fleetops:leaderboard")
        f.settings(leaderboard_visibility="managers")
        client = Client()
        client.force_login(self.plain)
        self.assertNotIn(f'href="{link}"', client.get(reverse("fleetops:dashboard")).content.decode())
        f.settings(leaderboard_visibility="members")
        self.assertIn(f'href="{link}"', client.get(reverse("fleetops:dashboard")).content.decode())


class LeaderboardLinkTests(PageTestCase):
    def linked(self, user, **params):
        response = self.get(user, **params)
        return {
            item["row"].main_character_name
            for entry in response.context["boards"]
            for item in entry["items"]
            if item["link"]
        }

    def test_members_only_link_themselves(self):
        self.assertEqual(self.linked(self.plain), {"Plain"})

    def test_corporation_managers_link_their_corporation(self):
        self.assertEqual(self.linked(self.manager), {"Mate", "Plain", "Manager", "Statsman"})

    def test_statistics_managers_link_everybody(self):
        self.assertEqual(self.linked(self.stats), {"Rival", "Mate", "Plain", "Manager", "Statsman"})

    def test_links_carry_the_month(self):
        page = html.unescape(self.get(self.stats).content.decode())

        self.assertIn(f'href="{reverse("fleetops:member_detail", args=[self.rival.pk])}?year=2026&month=3"', page)

    def test_the_viewer_row_outside_the_places_is_linked_to_the_viewer(self):
        f.settings(leaderboard_size=3)

        response = self.get(self.manager)

        # Fourth in the alliance, but third and so inside the places of the corporation board.
        alliance, corporation = response.context["boards"]
        self.assertTrue(alliance["viewer_link"])
        self.assertIsNone(corporation["board"].viewer_row)
        self.assertIn("Your rank: <strong>#4</strong> of 5", response.content.decode())


class LeaderboardTabTests(PageTestCase):
    def test_members_have_no_corporation_tabs(self):
        self.assertNotIn("fleetops-corporation-tabs", self.get(self.plain).content.decode())

    def test_corporation_managers_get_the_tabs_of_their_corporation(self):
        response = self.get(self.manager)
        page = html.unescape(response.content.decode())

        self.assertIn("fleetops-corporation-tabs", page)
        self.assertIsNone(response.context["tab_corporation_id"])
        self.assertIn(f'href="{reverse("fleetops:corporation_statistics")}?year=2026&month=3"', page)
        self.assertIn('class="nav-link active" aria-current="page" href="%s' % reverse("fleetops:leaderboard"), page)

    def test_statistics_managers_get_the_tabs_of_the_chosen_corporation(self):
        response = self.get(self.stats, corporation=str(f.OTHER_CORP[0]))
        page = html.unescape(response.content.decode())

        self.assertEqual(response.context["tab_corporation_id"], f.OTHER_CORP[0])
        self.assertIn(
            f'href="{reverse("fleetops:corporation_members_detail", args=[f.OTHER_CORP[0]])}?year=2026&month=3"', page
        )

    def test_the_tab_links_do_not_lead_to_forbidden_pages(self):
        client = Client(raise_request_exception=False)
        for user in (self.manager, self.stats):
            client.force_login(user)
            page = html.unescape(client.get(reverse("fleetops:leaderboard"), {"year": 2026, "month": 3}).content.decode())
            tabs = re.search(r'<ul class="nav nav-tabs.*?</ul>', page, re.S).group(0)
            for href in re.findall(r'href="([^"]*)"', tabs):
                with self.subTest(user=user.username, href=href):
                    self.assertEqual(client.get(href).status_code, 200)


class LeaderboardPageBudgetTests(PageTestCase):
    def count(self, user, **params):
        params = {"year": "2026", "month": "3", **params}
        client = Client()
        client.force_login(user)
        client.get(reverse("fleetops:leaderboard"), params)
        with CaptureQueriesContext(connection) as queries:
            response = client.get(reverse("fleetops:leaderboard"), params)
        self.assertEqual(response.status_code, 200)
        return len(queries)

    def populate(self, start, count):
        pilots = [
            f.member(f"load{start + index}", corporation=(f.DEFAULT_CORP, f.OTHER_CORP, f.THIRD_CORP)[index % 3])
            for index in range(count)
        ]
        for number in range(count // 2):
            fleet(self.fc, utc(2026, 3, 1 + number % 27, 12), *pilots[number % 2 :: 2])

    def test_the_same_queries_for_a_larger_dataset(self):
        self.populate(0, 9)
        cases = {
            "member": (self.plain, {}),
            "manager": (self.manager, {}),
            "stats": (self.stats, {}),
            "stats other corporation": (self.stats, {"corporation": str(f.OTHER_CORP[0])}),
        }
        small = {name: self.count(user, **params) for name, (user, params) in cases.items()}

        self.populate(100, 27)
        large = {name: self.count(user, **params) for name, (user, params) in cases.items()}

        self.assertEqual(small, large)
        # Including the login, permission and menu queries of the framework.
        self.assertLessEqual(max(large.values()), 30)
