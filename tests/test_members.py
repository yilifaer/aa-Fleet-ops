# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""The members table, the inactive list and the members pages of the statistics."""

import html
import re
from datetime import datetime, timezone as dt_timezone
from unittest import mock

from allianceauth.eveonline.models import EveCharacter
from django.db import connection
from django.test import Client, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import NoReverseMatch, reverse

from fleetops.models import FleetOperation, ReviewStatus
from fleetops.services import members as member_stats
from fleetops.services.members import fill_last_pap, member_rows, scope_corporations, type_columns

from . import factories as f

UTC = dt_timezone.utc
Status = FleetOperation.Status

CORP = f.CORP_MANAGEMENT_PERMS
ALL_STATS = f.MEMBER_PERMS + ["fleetops.view_all_stats"]


def utc(*args):
    return datetime(*args, tzinfo=UTC)


def clock(moment):
    """The site clock stands at ``moment``."""
    return mock.patch("django.utils.timezone.now", return_value=moment)


def pilot_of(pilot):
    """``(user, value, character)`` of a pilot given as a user, ``(user, value)`` or ``(user, value, character)``."""
    if not isinstance(pilot, tuple):
        return pilot, 1, None
    user, value, *rest = pilot
    return user, value, rest[0] if rest else None


def fleet(fc, when, *pilots, type_obj=None, **extra):
    """A counted fleet started at ``when`` (an hour long) with PAP for each pilot."""
    operation = f.closed_operation(fc, when, type_obj=type_obj, **extra)
    for pilot in pilots:
        user, value, character = pilot_of(pilot)
        f.add_attendance(operation, user, character, value=value)
    return operation


def new_fc(username="fc"):
    """An FC whose account is disabled, so the FC is not a member in the statistics."""
    return f.member(username, main_name=username.upper(), perms=f.FC_PERMS, active=False)


def names(rows):
    return [row.main_character_name for row in rows]


def previous_month_link(page):
    """The href of the "Previous month" link of a page, unescaped; None when the page has none."""
    found = re.search(r'href="([^"]*)"[^>]*>\u2039 Previous month', html.unescape(page))
    return found.group(1) if found else None


def set_alliances(*alliances):
    f.settings(history_alliance_ids=",".join(str(alliance[0]) for alliance in alliances))


class MembersTestCase(TestCase):
    """Four members in two corporations and two counted fleets in March 2026."""

    def setUp(self):
        # March 2026 is over; tests of a running or future month set their own clock.
        patcher = clock(utc(2026, 4, 15, 12))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.fc = new_fc()
        self.strat = f.fleet_type("StratOp", "2.00")
        self.roam = f.fleet_type("Roam", "1.00")
        self.alice = f.member("alice", main_name="Alice")
        self.alice_alt = f.add_alt(self.alice, "Alice Alt", corporation=f.OTHER_CORP, alliance=f.FOREIGN_ALLIANCE)
        self.bob = f.member("bob", main_name="Bob", corporation=f.OTHER_CORP)
        self.carol = f.member("carol", main_name="Carol")
        self.dave = f.member("dave", main_name="Dave")
        self.op1 = fleet(
            self.fc, utc(2026, 3, 5, 18), (self.alice, 2), (self.alice, 2, self.alice_alt), (self.bob, 3), self.dave,
            type_obj=self.strat,
        )
        self.op2 = fleet(self.fc, utc(2026, 3, 9, 19), self.alice, (self.bob, 3), type_obj=self.roam)

    def table(self, **kwargs):
        return member_rows(2026, 3, **kwargs)

    def row(self, user, **kwargs):
        return next(row for row in self.table(**kwargs).rows if row.user_id == user.pk)


class MemberRowsTests(MembersTestCase):
    def test_zero_pap_members_are_listed(self):
        table = self.table()

        self.assertCountEqual(names(table.rows), ["Alice", "Bob", "Carol", "Dave"])
        carol = self.row(self.carol)
        self.assertEqual((carol.pap, carol.fleets, carol.characters, carol.rank, carol.by_fleet_type), (0, 0, 0, None, {}))

    def test_alts_count_for_the_main(self):
        alice = self.row(self.alice)

        # The alt is in another corporation and alliance; its PAP still belongs to the main.
        self.assertEqual((alice.pap, alice.fleets, alice.characters), (5, 2, 2))
        self.assertEqual((alice.corporation_id, alice.corporation_ticker), (f.DEFAULT_CORP[0], "TEST "))
        self.assertEqual(alice.alliance_name, f.DEFAULT_ALLIANCE[1])
        self.assertEqual(self.row(self.bob).characters, 1)

    def test_row_carries_the_identity_of_the_main(self):
        alice = self.row(self.alice)

        self.assertEqual(alice.main_character_id, f.main_of(self.alice).character_id)
        self.assertEqual(alice.corporation_name, f.DEFAULT_CORP[1])
        self.assertEqual(alice.alliance_id, f.DEFAULT_ALLIANCE[0])
        self.assertEqual(alice.registered_at, self.alice.date_joined)

    def test_pap_per_fleet_type(self):
        self.assertEqual(self.row(self.alice).by_fleet_type, {self.strat.pk: 4, self.roam.pk: 1})
        self.assertEqual(self.row(self.bob).by_fleet_type, {self.strat.pk: 3, self.roam.pk: 3})
        self.assertEqual(self.row(self.dave).by_fleet_type, {self.strat.pk: 1})

    def test_fleet_types_are_those_with_pap_in_display_order(self):
        f.fleet_type("Unused", "1.00")
        table = self.table()

        self.assertEqual([fleet_type.name for fleet_type in table.fleet_types], ["Roam", "StratOp"])

    def test_default_order_is_pap_then_fleets_then_name(self):
        extra = f.member("erin", main_name="Aaron")
        fleet(self.fc, utc(2026, 3, 12, 18), (extra, 6))

        # Bob and Aaron both have 6 PAP; Bob was in two fleets.
        self.assertEqual(names(self.table().rows), ["Bob", "Aaron", "Alice", "Dave", "Carol"])

    def test_ranks_use_competition_ranking(self):
        pilots = [f.member(f"p{index}", main_name=f"Pilot {index}") for index in range(4)]
        fleet(self.fc, utc(2026, 4, 1, 12), (pilots[0], 5), (pilots[1], 3), (pilots[2], 3), (pilots[3], 1))
        table = member_rows(2026, 4)

        ranks = {row.main_character_name: row.rank for row in table.rows}
        self.assertEqual(
            ranks, {"Pilot 0": 1, "Pilot 1": 2, "Pilot 2": 2, "Pilot 3": 4, "Alice": None, "Bob": None, "Carol": None, "Dave": None}
        )
        self.assertEqual([row.rank for row in table.rows[:4]], [1, 2, 2, 4])

    def test_members_without_pap_have_no_rank(self):
        self.assertIsNone(self.row(self.carol).rank)
        self.assertEqual(
            [self.row(user).rank for user in (self.bob, self.alice, self.dave)], [1, 2, 3]
        )

    def test_ranks_are_taken_over_the_whole_scope_before_filtering(self):
        table = self.table(query="dave")

        self.assertEqual(names(table.rows), ["Dave"])
        self.assertEqual(table.rows[0].rank, 3)

    def test_ranks_in_a_corporation_are_among_its_members(self):
        table = self.table(corporation_id=f.DEFAULT_CORP[0])

        self.assertEqual({row.main_character_name: row.rank for row in table.rows}, {"Alice": 1, "Dave": 2, "Carol": None})

    def test_counts_cover_the_whole_scope(self):
        f.statistics_target(2026, 1, minimum=2)

        table = self.table(status="inactive", query="car")

        self.assertEqual(names(table.rows), ["Carol"])
        self.assertEqual(table.counts, {"all": 4, "with_pap": 3, "inactive": 2})

    def test_uncounted_fleets_and_pap_are_ignored(self):
        pending = fleet(self.fc, utc(2026, 3, 10, 18), (self.carol, 4), review_status=ReviewStatus.PENDING)
        excluded = fleet(self.fc, utc(2026, 3, 11, 18), (self.carol, 4), counts_for_stats=False)
        running = f.create_operation(self.fc, started_at=utc(2026, 3, 12, 18))
        f.add_attendance(running, self.carol, value=4)
        f.add_attendance(f.closed_operation(self.fc, utc(2026, 3, 13, 18)), self.carol, value=4, granted=False)
        self.assertEqual(
            [pending.review_status, excluded.counts_for_stats, running.status], [ReviewStatus.PENDING, False, Status.ACTIVE]
        )

        self.assertEqual(self.row(self.carol).pap, 0)

    def test_users_outside_the_population_are_not_listed(self):
        gone = f.member("gone", active=False)
        biomassed = f.member("biomassed", corporation=f.DOOMHEIM_CORP, alliance=None)
        nomain = f.member("nomain", with_main=False)
        fleet(self.fc, utc(2026, 3, 14, 18), gone, biomassed, (nomain, 1, f.add_alt(nomain, "Nomain Alt")))

        self.assertCountEqual(names(self.table().rows), ["Alice", "Bob", "Carol", "Dave"])

    def test_departed_members_are_hidden_with_member_alliances(self):
        leaver = f.member("leaver", main_name="Leaver", alliance=f.FOREIGN_ALLIANCE)
        fleet(self.fc, utc(2026, 3, 14, 18), (leaver, 9))

        self.assertIn("Leaver", names(self.table().rows))
        set_alliances(f.DEFAULT_ALLIANCE)
        table = self.table()
        self.assertNotIn("Leaver", names(table.rows))
        self.assertEqual(table.counts["all"], 4)

    def test_corporation_scope_lists_the_current_members(self):
        table = self.table(corporation_id=f.OTHER_CORP[0])

        self.assertEqual(names(table.rows), ["Bob"])
        self.assertEqual(table.corporation_id, f.OTHER_CORP[0])
        self.assertEqual(table.counts, {"all": 1, "with_pap": 1, "inactive": 0})

    def test_member_who_changed_corporation_shows_all_pap_under_the_new_one(self):
        fleet(self.fc, utc(2026, 3, 20, 18), (self.bob, 2))
        # Bob's PAP was earned in Test Corp Two; his main moved to Test Corp Three.
        main = f.main_of(self.bob)
        main.corporation_id, main.corporation_name = f.THIRD_CORP
        main.save()

        self.assertEqual(self.table(corporation_id=f.OTHER_CORP[0]).rows, [])
        moved = self.table(corporation_id=f.THIRD_CORP[0]).rows
        self.assertEqual(names(moved), ["Bob"])
        self.assertEqual(moved[0].pap, 8)

    def test_unknown_corporation_is_empty(self):
        table = self.table(corporation_id=9_999_999)

        self.assertEqual(table.rows, [])
        self.assertEqual(table.counts, {"all": 0, "with_pap": 0, "inactive": 0})

    def test_no_target_means_a_minimum_of_one(self):
        table = self.table(status="inactive")

        self.assertEqual(names(table.rows), ["Carol"])
        self.assertEqual((table.target.minimum, table.target.rule), (1, None))

    def test_month_starts_and_ends_in_eve_time(self):
        fleet(self.fc, utc(2026, 3, 31, 23, 30), (self.carol, 3))
        fleet(self.fc, utc(2026, 4, 1, 0, 0), (self.carol, 5))
        fleet(self.fc, utc(2026, 2, 28, 23, 59), (self.carol, 7))

        for zone in ("UTC", "Asia/Shanghai", "America/Los_Angeles", "Pacific/Auckland"):
            with self.subTest(zone=zone), override_settings(TIME_ZONE=zone):
                self.assertEqual(self.row(self.carol).pap, 3)
                self.assertEqual(member_rows(2026, 4).rows[0].pap, 5)


class InactiveTests(MembersTestCase):
    def test_minimum_boundary_pap_equal_to_the_minimum_is_ok(self):
        f.statistics_target(2026, 1, minimum=5)

        statuses = {row.main_character_name: row.status for row in self.table().rows}

        self.assertEqual(statuses, {"Alice": "ok", "Bob": "ok", "Carol": "below", "Dave": "below"})

    def test_inactive_filter_lists_members_below_the_minimum_lowest_first_then_by_name(self):
        f.statistics_target(2026, 1, minimum=6)
        zed = f.member("zed", main_name="Zed")
        fleet(self.fc, utc(2026, 3, 15, 18), (zed, 1))

        table = self.table(status="inactive")

        # Alice 5, Dave 1 and Zed 1, Carol 0; Bob (6) reaches the minimum.
        self.assertEqual(names(table.rows), ["Carol", "Dave", "Zed", "Alice"])
        self.assertTrue(all(row.status == "below" for row in table.rows))

    def test_active_filter_is_everybody_not_inactive(self):
        f.statistics_target(2026, 1, minimum=5)

        self.assertCountEqual(names(self.table(status="active").rows), ["Alice", "Bob"])

    def test_minimum_zero_lists_nobody(self):
        f.statistics_target(2026, 1, minimum=0)

        table = self.table(status="inactive")

        self.assertEqual(table.rows, [])
        self.assertEqual(table.counts["inactive"], 0)
        self.assertEqual({row.status for row in self.table().rows}, {"none"})
        self.assertEqual(len(self.table(status="active").rows), 4)

    def test_running_month_is_below_minimum_so_far_and_a_finished_month_is_below(self):
        with clock(utc(2026, 3, 20)):
            running = self.row(self.carol)
        with clock(utc(2026, 4, 2)):
            finished = self.row(self.carol)

        self.assertEqual(running.status, "below_so_far")
        self.assertEqual(finished.status, "below")

    def test_reaching_the_minimum_is_ok_in_a_running_month(self):
        with clock(utc(2026, 3, 20)):
            self.assertEqual(self.row(self.bob).status, "ok")

    def test_running_month_members_are_on_the_inactive_list(self):
        with clock(utc(2026, 3, 20)):
            self.assertEqual(names(self.table(status="inactive").rows), ["Carol"])

    def test_future_month_has_no_status_and_nobody_inactive(self):
        with clock(utc(2026, 1, 20)):
            table = self.table(status="inactive")
            self.assertEqual(table.rows, [])
            self.assertEqual({row.status for row in self.table().rows}, {"none"})

    def test_target_rules_apply_from_their_month(self):
        f.statistics_target(2026, 3, minimum=2)
        f.statistics_target(2026, 5, minimum=100)

        self.assertEqual(self.table().target.minimum, 2)
        self.assertEqual(names(self.table(status="inactive").rows), ["Carol", "Dave"])
        self.assertEqual(member_rows(2026, 4).target.minimum, 2)
        self.assertEqual(member_rows(2026, 5).target.minimum, 100)
        self.assertEqual(member_rows(2026, 2).target.minimum, 1)


class SearchAndSortTests(MembersTestCase):
    def test_search_is_a_case_insensitive_part_of_the_main_name(self):
        for query in ("al", "AL", "  alice  ", "ALICE"):
            with self.subTest(query=query):
                self.assertEqual(names(self.table(query=query).rows), ["Alice"])
        self.assertEqual(names(self.table(query="a").rows), ["Alice", "Dave", "Carol"])
        self.assertEqual(self.table(query="zzz").rows, [])

    def test_search_does_not_use_the_character_names_of_alts(self):
        self.assertEqual(self.table(query="Alt").rows, [])

    def test_search_characters_are_literal(self):
        odd = f.member("odd", main_name="100% Pilot_1")

        self.assertEqual([row.user_id for row in self.table(query="%").rows], [odd.pk])
        self.assertEqual([row.user_id for row in self.table(query="_").rows], [odd.pk])
        self.assertEqual(self.table(query="'; drop table").rows, [])

    def test_empty_search_lists_everybody(self):
        self.assertEqual(len(self.table(query="   ").rows), 4)

    def test_every_sort_key(self):
        cases = {
            "-pap": ["Bob", "Alice", "Dave", "Carol"],
            "pap": ["Carol", "Dave", "Alice", "Bob"],
            "name": ["Alice", "Bob", "Carol", "Dave"],
            "-name": ["Dave", "Carol", "Bob", "Alice"],
            "-fleets": ["Bob", "Alice", "Dave", "Carol"],
            "fleets": ["Carol", "Dave", "Alice", "Bob"],
            "corporation": ["Alice", "Carol", "Dave", "Bob"],
        }
        self.assertEqual(set(cases), member_stats.SORTS)
        for sort, expected in cases.items():
            with self.subTest(sort=sort):
                self.assertEqual(names(self.table(sort=sort).rows), expected)

    def test_invalid_sort_falls_back_to_the_default(self):
        default = names(self.table().rows)

        for sort in ("zzz", "-name;", "pap desc", "", None, "-corporation", "-PAP"):
            with self.subTest(sort=sort):
                self.assertEqual(names(self.table(sort=sort).rows), default)

    def test_the_inactive_list_defaults_to_lowest_first(self):
        f.statistics_target(2026, 1, minimum=6)

        self.assertEqual(names(self.table(status="inactive").rows), ["Carol", "Dave", "Alice"])
        self.assertEqual(names(self.table(status="inactive", sort="-pap").rows), ["Alice", "Dave", "Carol"])

    def test_invalid_status_is_all(self):
        self.assertEqual(len(self.table(status="zzz").rows), 4)
        self.assertEqual(len(self.table(status=None).rows), 4)

    def test_sorting_by_name_ignores_case(self):
        f.member("lower", main_name="aaron")

        self.assertEqual(names(self.table(sort="name").rows)[:3], ["aaron", "Alice", "Bob"])


class TypeColumnTests(MembersTestCase):
    def test_all_types_get_a_column_when_there_are_few(self):
        columns, other = type_columns(self.table())

        self.assertEqual([fleet_type.name for fleet_type in columns], ["Roam", "StratOp"])
        self.assertFalse(other)

    def test_the_types_with_most_pap_get_columns_and_the_rest_is_other(self):
        small = [f.fleet_type(f"Small {index}", "1.00") for index in range(5)]
        for index, fleet_type in enumerate(small):
            fleet(self.fc, utc(2026, 3, 20 + index, 18), (self.carol, index + 1), type_obj=fleet_type)
        table = self.table()

        columns, other = type_columns(table, limit=5)

        # StratOp has 8 PAP, Small 4 five, Roam and Small 3 four each and Small 2 three; the rest is "Other".
        self.assertTrue(other)
        self.assertEqual([fleet_type.name for fleet_type in columns], ["Roam", "Small 2", "Small 3", "Small 4", "StratOp"])

    def test_types_without_pap_in_the_rows_have_no_column(self):
        table = self.table(query="dave")

        columns, other = type_columns(table)

        self.assertEqual([fleet_type.name for fleet_type in columns], ["StratOp"])
        self.assertFalse(other)

    def test_no_pap_no_columns(self):
        self.assertEqual(type_columns(member_rows(2026, 8)), ([], False))


class FillLastPapTests(MembersTestCase):
    def test_last_pap_is_the_start_of_the_latest_counted_fleet(self):
        later = fleet(self.fc, utc(2026, 5, 2, 20), self.alice)
        fleet(self.fc, utc(2026, 6, 2, 20), (self.alice, 3), review_status=ReviewStatus.PENDING)
        fleet(self.fc, utc(2026, 6, 3, 20), (self.alice, 3), counts_for_stats=False)
        rows = self.table().rows

        fill_last_pap(rows)

        by_name = {row.main_character_name: row.last_pap_at for row in rows}
        self.assertEqual(by_name["Alice"], later.started_at)
        self.assertEqual(by_name["Bob"], self.op2.started_at)
        self.assertEqual(by_name["Dave"], self.op1.started_at)
        self.assertIsNone(by_name["Carol"])

    def test_last_pap_is_not_limited_to_the_month(self):
        rows = member_rows(2026, 8).rows

        fill_last_pap(rows)

        self.assertEqual({row.main_character_name: row.last_pap_at for row in rows}["Bob"], self.op2.started_at)

    def test_one_query_for_the_rows_given(self):
        rows = self.table().rows

        with CaptureQueriesContext(connection) as queries:
            fill_last_pap(rows)

        self.assertEqual(len(queries), 1)
        looked_up = re.search(r'"auth_user_id" IN \(([^)]*)\)', queries[0]["sql"].replace("`", '"')).group(1)
        self.assertEqual(sorted(int(part) for part in looked_up.split(",")), sorted(row.user_id for row in rows))

    def test_only_the_given_rows_are_looked_up(self):
        rows = self.table().rows
        bob = next(row for row in rows if row.main_character_name == "Bob")

        with CaptureQueriesContext(connection) as queries:
            fill_last_pap([bob])

        self.assertEqual(len(queries), 1)
        self.assertIn(f"IN ({self.bob.pk})", queries[0]["sql"])
        self.assertIsNotNone(bob.last_pap_at)
        self.assertIsNone(next(row for row in rows if row.main_character_name == "Dave").last_pap_at)

    def test_a_long_list_is_read_in_one_grouped_query_without_ids(self):
        rows = self.table().rows

        with mock.patch.object(member_stats, "LAST_PAP_CHUNK", 3):
            with CaptureQueriesContext(connection) as queries:
                fill_last_pap(rows)

        self.assertGreater(len(rows), 3)
        self.assertEqual(len(queries), 1)
        self.assertNotRegex(queries[0]["sql"].replace("`", '"'), r'"auth_user_id" IN \(')
        self.assertEqual(sum(row.last_pap_at is not None for row in rows), 3)

    def test_the_grouped_read_gives_the_dates_of_the_lookup_by_id(self):
        fleet(self.fc, utc(2026, 5, 2, 20), self.alice)
        fleet(self.fc, utc(2026, 6, 2, 20), (self.alice, 3), review_status=ReviewStatus.PENDING)
        fleet(self.fc, utc(2026, 6, 3, 20), (self.bob, 3), counts_for_stats=False)
        # PAP of a deleted account has no member to show it for.
        orphan = fleet(self.fc, utc(2026, 7, 1, 20), self.carol)
        orphan.attendance_records.update(auth_user=None)
        by_id, grouped = self.table().rows, self.table().rows

        fill_last_pap(by_id)
        with mock.patch.object(member_stats, "LAST_PAP_CHUNK", 1):
            fill_last_pap(grouped)

        self.assertEqual({row.user_id: row.last_pap_at for row in by_id}, {row.user_id: row.last_pap_at for row in grouped})
        self.assertEqual(
            {row.main_character_name: row.last_pap_at for row in grouped},
            {"Alice": utc(2026, 5, 2, 20), "Bob": self.op2.started_at, "Carol": None, "Dave": self.op1.started_at},
        )

    def test_the_chunk_size_is_five_hundred(self):
        self.assertEqual(member_stats.LAST_PAP_CHUNK, 500)

    def test_no_rows_no_queries(self):
        with CaptureQueriesContext(connection) as queries:
            fill_last_pap([])

        self.assertEqual(len(queries), 0)


class ScopeCorporationsTests(MembersTestCase):
    def test_corporations_with_members_by_name(self):
        self.assertEqual(scope_corporations(), [(f.DEFAULT_CORP[0], f.DEFAULT_CORP[1]), (f.OTHER_CORP[0], f.OTHER_CORP[1])])

    def test_corporations_without_members_in_scope_are_left_out(self):
        f.member("biomassed", corporation=f.DOOMHEIM_CORP)
        f.member("foreign", corporation=f.THIRD_CORP, alliance=f.FOREIGN_ALLIANCE)
        set_alliances(f.DEFAULT_ALLIANCE)

        self.assertEqual([corporation_id for corporation_id, _name in scope_corporations()], [f.DEFAULT_CORP[0], f.OTHER_CORP[0]])


class QueryBudgetTests(TestCase):
    def populate(self, fc, start, count, fleets):
        pilots = [f.member(f"member{start + index}", corporation=(f.DEFAULT_CORP, f.OTHER_CORP, f.THIRD_CORP)[index % 3]) for index in range(count)]
        for number in range(fleets):
            fleet(fc, utc(2026, 3, 1 + number % 27, 12 + number % 6), *pilots[number % 2 :: 2], type_obj=f.fleet_type(f"Type {number % 4}"))
        return pilots

    def queries(self, **kwargs):
        with CaptureQueriesContext(connection) as queries:
            table = member_rows(2026, 3, **kwargs)
            fill_last_pap(table.rows[: member_stats.PAGE_SIZE])
        return len(queries)

    def test_member_rows_run_the_same_queries_for_a_larger_dataset(self):
        fc = new_fc()
        self.populate(fc, 0, 12, 4)
        small = {"all": self.queries(), "corporation": self.queries(corporation_id=f.DEFAULT_CORP[0]), "inactive": self.queries(status="inactive", query="member")}

        self.populate(fc, 100, 36, 12)
        large = {"all": self.queries(), "corporation": self.queries(corporation_id=f.DEFAULT_CORP[0]), "inactive": self.queries(status="inactive", query="member")}

        self.assertEqual(small, large)
        self.assertLessEqual(max(large.values()), 9)

    def test_fill_last_pap_costs_one_query_however_many_members(self):
        fc = new_fc()
        pilots = self.populate(fc, 0, 6, 2)
        rows = member_rows(2026, 3).rows
        with mock.patch.object(member_stats, "LAST_PAP_CHUNK", 2):
            with CaptureQueriesContext(connection) as queries:
                fill_last_pap(rows)

        self.assertEqual(len(rows), len(pilots))
        self.assertEqual(len(queries), 1)


class MembersPageTests(MembersTestCase):
    def setUp(self):
        super().setUp()
        self.stats = f.member("stats", main_name="Statsman", perms=ALL_STATS)
        self.manager = f.member("manager", main_name="Manager", perms=CORP)
        self.plain = f.member("plain", main_name="Plain", perms=f.MEMBER_PERMS)
        self.admin = f.member("admin", main_name="Admin", perms=ALL_STATS + ["fleetops.manage_configuration"])

    def get(self, user, name="alliance_members", *args, **params):
        params.setdefault("year", "2026")
        params.setdefault("month", "3")
        client = Client()
        client.force_login(user)
        return client.get(reverse(f"fleetops:{name}", args=args), params)

    def page(self, user, name="alliance_members", *args, **params):
        response = self.get(user, name, *args, **params)
        self.assertEqual(response.status_code, 200)
        return response

    def listed(self, response):
        return [item["row"].main_character_name for item in response.context["items"]]

    def test_alliance_page_lists_everybody_with_pap_and_status(self):
        response = self.page(self.stats)

        self.assertEqual(self.listed(response), ["Bob", "Alice", "Dave", "Admin", "Carol", "Manager", "Plain", "Statsman"])
        self.assertEqual(response.context["counts"], {"all": 8, "with_pap": 3, "inactive": 5})
        page = response.content.decode()
        self.assertIn("Members", page)
        self.assertIn("Alice", page)
        self.assertIn("PAP", page)

    def test_the_counts_cover_the_scope_and_the_filter_is_noted(self):
        f.statistics_target(2026, 1, minimum=5)

        everybody = self.page(self.stats).content.decode()
        filtered = self.page(self.stats, q="alice").content.decode()

        self.assertIn("<strong>8</strong> members", everybody)
        self.assertIn("<strong>3</strong> with PAP", everybody)
        self.assertIn("<strong>6</strong> below minimum", everybody)
        self.assertNotIn("shown</span>", everybody)
        self.assertIn("<strong>8</strong> members", filtered)
        self.assertIn("1 shown", filtered)

    def test_title_and_tab_follow_the_status_filter(self):
        members = self.page(self.stats)
        inactive = self.page(self.stats, status="inactive")

        self.assertEqual((members.context["title"], members.context["active_tab"]), ("Members", "members"))
        self.assertEqual((inactive.context["title"], inactive.context["active_tab"]), ("Inactive members", "inactive"))
        self.assertIn("Inactive members", inactive.content.decode())

    def test_inactive_page_lists_only_members_below_the_minimum(self):
        response = self.page(self.stats, status="inactive")

        self.assertEqual(sorted(self.listed(response)), ["Admin", "Carol", "Manager", "Plain", "Statsman"])

    def test_the_member_minimum_comes_from_the_monthly_target(self):
        f.statistics_target(2026, 1, minimum=5)

        response = self.page(self.stats, status="inactive")

        self.assertEqual(sorted(self.listed(response)), ["Admin", "Carol", "Dave", "Manager", "Plain", "Statsman"])
        self.assertIn("at least 5 PAP", response.content.decode())

    def test_without_a_target_the_page_points_admins_to_the_monthly_targets(self):
        admin_page = self.page(self.admin).content.decode()
        stats_page = self.page(self.stats).content.decode()

        self.assertIn("No monthly target is set", admin_page)
        self.assertIn(reverse("fleetops:configuration_list", args=["statistics-targets"]), admin_page)
        self.assertIn("No monthly target is set", stats_page)
        self.assertNotIn(reverse("fleetops:configuration_list", args=["statistics-targets"]), stats_page)

    def test_minimum_zero_is_explained(self):
        f.statistics_target(2026, 1, minimum=0)

        response = self.page(self.stats, status="inactive")

        self.assertEqual(self.listed(response), [])
        self.assertIn("The member minimum is 0 for this month", response.content.decode())

    def test_the_running_month_shows_amber_below_minimum_so_far(self):
        with clock(utc(2026, 3, 20, 12)):
            page = self.page(self.stats, status="inactive").content.decode()

        self.assertIn('class="badge text-bg-warning">Below minimum so far</span>', page)
        self.assertNotIn("text-bg-danger", page)
        self.assertIn("fleetops-status-row-progress", page)

    def test_a_finished_month_shows_red_below_minimum(self):
        with clock(utc(2026, 4, 2, 12)):
            page = self.page(self.stats, status="inactive").content.decode()

        self.assertIn('class="badge text-bg-danger">Below minimum</span>', page)
        self.assertNotIn("Below minimum so far</span>", page)
        self.assertIn("fleetops-status-row-missed", page)

    def test_reaching_the_minimum_is_green_in_a_running_month(self):
        with clock(utc(2026, 3, 20, 12)):
            page = self.page(self.stats, q="bob").content.decode()

        self.assertIn('class="badge text-bg-success">OK</span>', page)
        self.assertIn("fleetops-status-row-met", page)

    def test_invalid_filters_fall_back_to_the_defaults(self):
        response = self.page(self.stats, status="zzz", sort="-;--", page="abc", corporation="x", year="99999", month="13")

        context = response.context
        self.assertEqual((context["status"], context["sort"], context["corporation_id"]), ("all", "", None))
        self.assertEqual(len(context["items"]), 8)

    def test_page_beyond_the_range_shows_the_last_page(self):
        with mock.patch.object(member_stats, "PAGE_SIZE", 3):
            response = self.page(self.stats, page="99")

        self.assertEqual(response.context["page_obj"].number, 3)
        self.assertEqual(len(response.context["items"]), 2)

    def test_search_field_is_escaped(self):
        page = self.page(self.stats, q='"><script>alert(1)</script>').content.decode()

        self.assertNotIn("<script>alert(1)</script>", page)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", page)

    def test_names_are_escaped(self):
        f.member("evil", main_name="<img src=x onerror=alert(1)>")

        page = self.page(self.stats).content.decode()

        self.assertNotIn("<img src=x", page)
        self.assertIn("&lt;img src=x onerror=alert(1)&gt;", page)

    def test_corporation_filter_of_the_alliance_page(self):
        response = self.page(self.stats, corporation=str(f.OTHER_CORP[0]))

        self.assertEqual(self.listed(response), ["Bob"])
        self.assertEqual(response.context["corporation_name"], f.OTHER_CORP[1])
        self.assertFalse(response.context["show_corporation_column"])
        self.assertIn((f.OTHER_CORP[0], f.OTHER_CORP[1]), response.context["corporations"])

    def test_alliance_page_has_a_corporation_column(self):
        response = self.page(self.stats)

        self.assertTrue(response.context["show_corporation_column"])
        self.assertIn("[TEST ]", response.content.decode())

    def test_a_member_without_a_corporation_ticker_shows_the_corporation_name(self):
        EveCharacter.objects.filter(pk=f.main_of(self.bob).pk).update(corporation_ticker="")

        page = self.page(self.stats).content.decode()

        self.assertNotIn("[]", page)
        self.assertIn(f'<td title="{f.OTHER_CORP[1]}">{f.OTHER_CORP[1]}</td>', page)
        self.assertIn('<td title="Test Corp One">[TEST ]</td>', page)

    def test_members_are_linked_to_their_pages_with_the_month(self):
        page = html.unescape(self.page(self.stats, q="alice").content.decode())

        self.assertIn(f'href="{reverse("fleetops:member_detail", args=[self.alice.pk])}?year=2026&month=3"', page)

    def test_the_viewers_own_row_is_marked(self):
        response = self.page(self.stats, q="statsman")

        self.assertTrue(response.context["items"][0]["is_viewer"])
        self.assertIn(">You</span>", response.content.decode())

    def test_times_are_shown_in_eve_time(self):
        fleet(self.fc, utc(2026, 3, 31, 23, 30), self.carol)

        with override_settings(TIME_ZONE="Asia/Shanghai"):
            page = self.page(self.stats, q="carol").content.decode()

        self.assertIn("2026-03-31 23:30", page)
        self.assertIn(self.carol.date_joined.strftime("%Y-%m-%d"), page)

    def test_last_pap_is_looked_up_for_the_page_only(self):
        with mock.patch.object(member_stats, "PAGE_SIZE", 2):
            response = self.page(self.stats)

        shown = [item["row"] for item in response.context["items"]]
        everybody = response.context["table"].rows
        self.assertEqual([row.last_pap_at is not None for row in shown], [True, True])
        self.assertTrue(all(row.last_pap_at is None for row in everybody if row not in shown))

    def test_fleet_type_columns_and_other(self):
        for index in range(5):
            fleet(self.fc, utc(2026, 3, 20 + index, 18), (self.carol, index + 1), type_obj=f.fleet_type(f"Small {index}"))

        response = self.page(self.stats)

        context = response.context
        self.assertEqual(len(context["shown_types"]), 5)
        self.assertTrue(context["has_other"])
        item = next(item for item in context["items"] if item["row"].main_character_name == "Carol")
        self.assertEqual(item["other"], item["row"].pap - sum(item["cells"]))
        self.assertGreater(item["other"], 0)
        self.assertIn(">Other</th>", response.content.decode())

    def test_no_other_column_without_a_remainder(self):
        response = self.page(self.stats)

        self.assertFalse(response.context["has_other"])
        self.assertNotIn(">Other</th>", response.content.decode())

    def test_empty_message(self):
        page = self.page(self.stats, q="nobody-has-this-name").content.decode()

        self.assertIn("No members match the filter.", page)

    def test_a_month_older_than_the_retention_window_is_noted(self):
        old = self.page(self.stats, year="2025", month="3").content.decode()
        recent = self.page(self.stats).content.decode()

        self.assertIn("PAP older than 365 days is removed", old)
        self.assertNotIn("PAP older than", recent)

    def test_the_retention_note_names_the_configured_window(self):
        f.settings(data_retention_days=400)

        self.assertIn("PAP older than 400 days is removed", self.page(self.stats, year="2025", month="1").content.decode())
        self.assertNotIn("PAP older than", self.page(self.stats, year="2025", month="5").content.decode())

    def test_a_future_month_is_empty_of_status(self):
        with clock(utc(2026, 1, 20)):
            response = self.page(self.stats)

        self.assertEqual({item["row"].status for item in response.context["items"]}, {"none"})
        self.assertEqual(response.context["counts"]["inactive"], 0)

    def test_no_afat_comparison(self):
        with self.assertRaises(NoReverseMatch):
            reverse("fleetops:afat_comparison")
        page = self.page(self.stats).content.decode()
        self.assertNotIn("AFAT", page)

    def test_month_links_keep_the_filters(self):
        response = self.page(self.stats, status="inactive", sort="name", q="a", corporation=str(f.DEFAULT_CORP[0]))
        page = response.content.decode()

        previous = previous_month_link(page)
        self.assertIsNotNone(previous)
        for expected in ("status=inactive", "sort=name", "q=a", f"corporation={f.DEFAULT_CORP[0]}", "year=2026", "month=2"):
            self.assertIn(expected, previous)
        self.assertIn('<input type="hidden" name="status" value="inactive">', page)

    def test_sort_headers_toggle(self):
        response = self.page(self.stats)
        links = response.context["sort_next"]

        self.assertEqual(links, {"name": "name", "pap": "pap", "fleets": "-fleets", "corporation": "corporation"})
        self.assertEqual(response.context["sort_state"]["pap"], "descending")
        by_name = self.page(self.stats, sort="name")
        self.assertEqual(by_name.context["sort_next"]["name"], "-name")
        self.assertEqual(by_name.context["sort_state"]["name"], "ascending")
        descending = self.page(self.stats, sort="-name")
        self.assertEqual(descending.context["sort_next"]["name"], "name")
        page = html.unescape(by_name.content.decode())
        self.assertIn("sort=-name", page)
        self.assertIn('aria-sort="ascending"', page)

    def test_sort_links_keep_the_other_filters(self):
        page = html.unescape(self.page(self.stats, status="inactive", q="a").content.decode())

        link = re.search(r'href="(\?[^"]*sort=[^"]*)"', page)
        self.assertIsNotNone(link)
        self.assertIn("status=inactive", link.group(1))
        self.assertIn("q=a", link.group(1))
        self.assertNotIn("page=", link.group(1))

    def test_the_inactive_list_marks_the_lowest_first_sort(self):
        response = self.page(self.stats, status="inactive")

        self.assertEqual(response.context["sort_state"]["pap"], "ascending")
        self.assertEqual(response.context["sort_next"]["pap"], "-pap")


class PaginationTests(MembersTestCase):
    def setUp(self):
        super().setUp()
        self.stats = f.member("stats", main_name="Statsman", perms=ALL_STATS)
        self.client = Client()
        self.client.force_login(self.stats)

    def get(self, **params):
        params.setdefault("year", "2026")
        params.setdefault("month", "3")
        return self.client.get(reverse("fleetops:alliance_members"), params)

    def test_a_page_holds_one_hundred_rows(self):
        self.assertEqual(member_stats.PAGE_SIZE, 100)
        for index in range(104):
            f.member(f"bulk{index}", main_name=f"Bulk {index:03d}")

        first = self.get(status="inactive")
        second = self.get(status="inactive", page="2")

        self.assertEqual(len(first.context["items"]), 100)
        self.assertEqual(first.context["page_obj"].paginator.count, 106)
        self.assertEqual(len(second.context["items"]), 6)

    def test_page_links_keep_the_filters(self):
        with mock.patch.object(member_stats, "PAGE_SIZE", 2):
            for index in range(4):
                f.member(f"bulk{index}", main_name=f"Bulk {index}")
            response = self.get(status="inactive", sort="name", q="b")

        links = [html.unescape(link) for link in re.findall(r'class="page-link" href="([^"]*)"', response.content.decode())]
        self.assertTrue(links)
        for link in links:
            for expected in ("status=inactive", "sort=name", "q=b", "year=2026", "month=3"):
                self.assertIn(expected, link)
            self.assertIn("page=", link)

    def test_page_two_keeps_the_filter_in_effect(self):
        with mock.patch.object(member_stats, "PAGE_SIZE", 2):
            for index in range(4):
                f.member(f"bulk{index}", main_name=f"Bulk {index}")
            response = self.get(status="inactive", q="bulk", page="2")

        self.assertEqual(response.context["page_obj"].number, 2)
        self.assertEqual([item["row"].main_character_name for item in response.context["items"]], ["Bulk 2", "Bulk 3"])

    def test_month_selector_resets_the_page(self):
        with mock.patch.object(member_stats, "PAGE_SIZE", 2):
            for index in range(4):
                f.member(f"bulk{index}", main_name=f"Bulk {index}")
            page = self.get(status="inactive", page="2").content.decode()

        self.assertNotIn('name="page"', page)
        previous = previous_month_link(page)
        self.assertIsNotNone(previous)
        self.assertNotIn("page=", previous)

    def test_no_pager_for_a_single_page(self):
        self.assertNotIn('aria-label="Member pages"', self.get().content.decode())

    def test_many_pages_are_elided(self):
        with mock.patch.object(member_stats, "PAGE_SIZE", 1):
            for index in range(20):
                f.member(f"bulk{index}", main_name=f"Bulk {index}")
            response = self.get(page="10")

        self.assertIn(response.context["ellipsis"], response.context["page_range"])
        self.assertLess(len(response.context["page_range"]), 12)

    def test_the_query_count_does_not_grow_with_the_members(self):
        fc = new_fc("fc2")

        def populate(start, count, fleets):
            pilots = [
                f.member(f"load{start + index}", corporation=(f.DEFAULT_CORP, f.OTHER_CORP, f.THIRD_CORP)[index % 3])
                for index in range(count)
            ]
            for number in range(fleets):
                fleet(fc, utc(2026, 3, 1 + number % 27, 12), *pilots[number % 2 :: 2], type_obj=f.fleet_type(f"Type {number % 4}"))

        def count(**params):
            self.get(**params)
            with CaptureQueriesContext(connection) as queries:
                response = self.get(**params)
            self.assertEqual(response.status_code, 200)
            return len(queries)

        populate(0, 10, 4)
        small = {name: count(**params) for name, params in (("all", {}), ("inactive", {"status": "inactive"}), ("corporation", {"corporation": str(f.OTHER_CORP[0])}))}
        populate(100, 30, 12)
        large = {name: count(**params) for name, params in (("all", {}), ("inactive", {"status": "inactive"}), ("corporation", {"corporation": str(f.OTHER_CORP[0])}))}

        self.assertEqual(small, large)
        # Including the login, permission and menu queries of the framework.
        self.assertLessEqual(max(large.values()), 30)


class CorporationPageTests(MembersTestCase):
    def setUp(self):
        super().setUp()
        self.manager = f.member("manager", main_name="Manager", perms=CORP)
        self.stats = f.member("stats", main_name="Statsman", perms=ALL_STATS)
        self.plain = f.member("plain", main_name="Plain", perms=f.MEMBER_PERMS)

    def get(self, user, name, *args, **params):
        params.setdefault("year", "2026")
        params.setdefault("month", "3")
        client = Client(raise_request_exception=False)
        client.force_login(user)
        return client.get(reverse(f"fleetops:{name}", args=args), params)

    def listed(self, response):
        return sorted(item["row"].main_character_name for item in response.context["items"])

    def test_a_corporation_manager_sees_only_their_own_corporation(self):
        response = self.get(self.manager, "corporation_members")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.listed(response), ["Alice", "Carol", "Dave", "Manager", "Plain", "Statsman"])
        self.assertEqual(response.context["corporation_name"], f.DEFAULT_CORP[1])
        self.assertTrue(response.context["own_corporation"])
        self.assertFalse(response.context["show_corporation_column"])

    def test_the_inactive_list_of_a_corporation_manager_is_their_corporation_only(self):
        f.member("elsewhere", main_name="Elsewhere", corporation=f.OTHER_CORP)

        response = self.get(self.manager, "corporation_members", status="inactive")

        self.assertEqual(self.listed(response), ["Carol", "Manager", "Plain", "Statsman"])

    def test_a_corporation_manager_cannot_open_other_corporations_or_the_alliance(self):
        self.assertEqual(self.get(self.manager, "corporation_members_detail", f.OTHER_CORP[0]).status_code, 403)
        self.assertEqual(self.get(self.manager, "alliance_members").status_code, 403)

    def test_a_member_cannot_open_corporation_lists(self):
        self.assertEqual(self.get(self.plain, "corporation_members").status_code, 403)
        self.assertEqual(self.get(self.plain, "corporation_members_detail", f.DEFAULT_CORP[0]).status_code, 403)

    def test_statistics_managers_open_any_corporation_by_id(self):
        response = self.get(self.stats, "corporation_members_detail", f.OTHER_CORP[0])

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.listed(response), ["Bob"])
        self.assertEqual(response.context["corporation_name"], f.OTHER_CORP[1])
        self.assertEqual(response.context["tab_corporation_id"], f.OTHER_CORP[0])
        self.assertFalse(response.context["own_corporation"])

    def test_unknown_corporation_is_not_found(self):
        self.assertEqual(self.get(self.stats, "corporation_members_detail", 9_999_999).status_code, 404)

    def test_corporation_pages_list_the_current_members_with_all_their_pap(self):
        fleet(self.fc, utc(2026, 3, 20, 18), (self.bob, 2))
        main = f.main_of(self.bob)
        main.corporation_id, main.corporation_name = f.DEFAULT_CORP
        main.save()

        response = self.get(self.manager, "corporation_members")

        bob = next(item["row"] for item in response.context["items"] if item["row"].main_character_name == "Bob")
        self.assertEqual(bob.pap, 8)
        self.assertIn("listed under their current corporation", response.content.decode())

    def test_export_links_name_the_corporation(self):
        own = html.unescape(self.get(self.manager, "corporation_members", status="inactive").content.decode())
        by_id = html.unescape(self.get(self.stats, "corporation_members_detail", f.OTHER_CORP[0]).content.decode())
        alliance = html.unescape(self.get(self.stats, "alliance_members", status="inactive").content.decode())

        export = reverse("fleetops:export", args=["members"])
        own_links = re.findall(rf'href="({re.escape(export)}[^"]*)"', own)
        by_id_links = re.findall(rf'href="({re.escape(export)}[^"]*)"', by_id)
        alliance_links = re.findall(rf'href="({re.escape(export)}[^"]*)"', alliance)
        self.assertEqual(len(own_links), 2)
        for link in own_links:
            self.assertIn(f"corporation={f.DEFAULT_CORP[0]}", link)
            self.assertIn("status=inactive", link)
            self.assertIn("year=2026", link)
        for link in by_id_links:
            self.assertIn(f"corporation={f.OTHER_CORP[0]}", link)
        self.assertEqual(len(alliance_links), 2)
        for link in alliance_links:
            self.assertNotIn("corporation=", link)
            self.assertIn("status=inactive", link)
        self.assertEqual(sorted(re.findall(r"format=(\w+)", " ".join(own_links))), ["csv", "xlsx"])

    def test_alliance_export_keeps_the_chosen_corporation(self):
        page = html.unescape(self.get(self.stats, "alliance_members", corporation=str(f.OTHER_CORP[0])).content.decode())

        export = reverse("fleetops:export", args=["members"])
        links = re.findall(rf'href="({re.escape(export)}[^"]*)"', page)
        self.assertEqual(len(links), 2)
        self.assertTrue(all(f"corporation={f.OTHER_CORP[0]}" in link for link in links))

    def test_corporation_tabs_are_shown(self):
        page = html.unescape(self.get(self.manager, "corporation_members").content.decode())

        self.assertIn(f'href="{reverse("fleetops:corporation_statistics")}?year=2026&month=3"', page)
        self.assertIn("status=inactive", page)

    def test_a_corporation_outside_the_member_alliances_gets_a_notice_and_no_rows(self):
        stranger = f.member("stranger", perms=CORP, corporation=f.THIRD_CORP, alliance=f.FOREIGN_ALLIANCE)
        set_alliances(f.DEFAULT_ALLIANCE)

        response = self.get(stranger, "corporation_members")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["items"], [])
        self.assertIn("not in the configured member alliances", response.content.decode())
        self.assertNotIn("Alice", response.content.decode())

    def test_no_notice_for_a_corporation_in_the_member_alliances(self):
        set_alliances(f.DEFAULT_ALLIANCE)

        page = self.get(self.manager, "corporation_members").content.decode()

        self.assertNotIn("not in the configured member alliances", page)

    def test_a_corporation_manager_without_a_main_gets_nothing(self):
        nomad = f.create_user("nomad", perms=CORP, with_main=False)

        # Alliance Auth sends users without a main character to the dashboard.
        response = self.get(nomad, "corporation_members")

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/dashboard/")

    def test_the_query_count_does_not_grow_with_the_members(self):
        def count():
            self.get(self.manager, "corporation_members", status="inactive")
            with CaptureQueriesContext(connection) as queries:
                response = self.get(self.manager, "corporation_members", status="inactive")
            self.assertEqual(response.status_code, 200)
            return len(queries)

        for index in range(6):
            fleet(self.fc, utc(2026, 3, 20 + index, 12), f.member(f"first{index}"))
        small = count()
        for index in range(18):
            fleet(self.fc, utc(2026, 3, 1 + index, 12), f.member(f"second{index}"), f.member(f"third{index}"))
        large = count()

        self.assertEqual(small, large)
