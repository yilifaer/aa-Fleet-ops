# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""The statistics page of one member: ranks, characters, fleet types, fleets, access and fleet links."""

import html
from datetime import datetime, timezone as dt_timezone
from unittest import mock

from django.db import connection
from django.test import Client, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from fleetops.models import AttendanceRecord, FleetOperation, ReviewStatus
from fleetops.services import members as member_stats
from fleetops.services.members import member_detail, member_fleets

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


def new_fc(username="fc"):
    """An FC whose account is disabled, so the FC is not a member in the statistics."""
    return f.member(username, main_name=username.upper(), perms=f.FC_PERMS, active=False)


def fleet(fc, when, *pilots, type_obj=None, **extra):
    """A counted fleet with PAP for ``(user, value)`` or ``(user, value, character)`` pilots (a user: one PAP)."""
    operation = f.closed_operation(fc, when, type_obj=type_obj, **extra)
    for pilot in pilots:
        if not isinstance(pilot, tuple):
            pilot = (pilot, 1)
        user, value, *rest = pilot
        f.add_attendance(operation, user, rest[0] if rest else None, value=value)
    return operation


def set_alliances(*alliances):
    f.settings(history_alliance_ids=",".join(str(alliance[0]) for alliance in alliances))


class DetailTestCase(TestCase):
    """Alice with an alt in March 2026, three other members and the fleets they flew."""

    def setUp(self):
        # March 2026 is over; tests of a running month set their own clock.
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
            self.fc,
            utc(2026, 3, 5, 18),
            (self.alice, 2),
            (self.alice, 2, self.alice_alt),
            (self.bob, 3),
            self.dave,
            type_obj=self.strat,
            doctrine_name="Armor Hurricanes",
        )
        self.op2 = fleet(self.fc, utc(2026, 3, 9, 19), self.alice, (self.bob, 3), type_obj=self.roam)


class MemberFleetsTests(DetailTestCase):
    def fleets(self, user=None, **kwargs):
        return member_fleets(user or self.alice, 2026, 3, **kwargs)

    def test_one_row_per_fleet_newest_first(self):
        rows = self.fleets()

        self.assertEqual([row.operation_uuid for row in rows], [str(self.op2.uuid), str(self.op1.uuid)])
        self.assertEqual([row.pap for row in rows], [1, 4])

    def test_the_characters_of_a_fleet_are_listed_and_their_pap_added(self):
        newest, oldest = self.fleets()

        self.assertEqual(newest.character_name, "Alice")
        self.assertEqual(oldest.character_name, "Alice, Alice Alt")

    def test_fleet_details(self):
        row = self.fleets()[1]

        self.assertEqual(row.started_at, utc(2026, 3, 5, 18))
        self.assertEqual(row.ended_at, utc(2026, 3, 5, 19))
        self.assertEqual((row.fleet_type, row.doctrine, row.source), ("StratOp", "Armor Hurricanes", "Automatic"))
        self.assertEqual(row.fc_name, "FC")

    def test_a_fleet_without_doctrine_and_end(self):
        operation = fleet(self.fc, utc(2026, 3, 12, 18), self.carol)
        FleetOperation.objects.filter(pk=operation.pk).update(ended_at=None)

        row = self.fleets(self.carol)[0]

        self.assertEqual((row.doctrine, row.fleet_type, row.ended_at), ("", "StratOp", None))

    def test_the_fleet_type_uses_the_short_name(self):
        type_obj = f.fleet_type("Home Defense")
        type_obj.short_name = "HD"
        type_obj.save()
        fleet(self.fc, utc(2026, 3, 13, 18), self.carol, type_obj=type_obj)

        self.assertEqual(self.fleets(self.carol)[0].fleet_type, "HD")

    def test_the_fc_is_named_by_main_character_else_by_character(self):
        fc_alt = f.add_alt(self.fc, "FC Alt")
        fleet(self.fc, utc(2026, 3, 14, 18), self.carol, fc_character=fc_alt, fc_main_character_name="")
        fleet(self.fc, utc(2026, 3, 15, 18), self.carol, fc_character=fc_alt)

        names = [row.fc_name for row in self.fleets(self.carol)]

        self.assertEqual(names, ["FC", "FC Alt"])

    def test_sources_of_a_fleet_are_listed(self):
        f.add_attendance(self.op1, self.alice, value=1, source=AttendanceRecord.Source.MANUAL)

        row = self.fleets()[1]

        self.assertEqual(row.source, "Automatic, Manual")
        self.assertEqual(row.pap, 5)

    def test_other_members_are_not_in_the_list(self):
        self.assertEqual([row.pap for row in self.fleets(self.dave)], [1])
        self.assertEqual(self.fleets(self.carol), [])

    def test_only_counted_fleets_and_pap_are_listed(self):
        fleet(self.fc, utc(2026, 3, 10, 18), (self.alice, 4), review_status=ReviewStatus.PENDING)
        fleet(self.fc, utc(2026, 3, 11, 18), (self.alice, 4), counts_for_stats=False)
        running = f.create_operation(self.fc, started_at=utc(2026, 3, 12, 18))
        f.add_attendance(running, self.alice, value=4)
        f.add_attendance(f.closed_operation(self.fc, utc(2026, 3, 13, 18)), self.alice, value=4, granted=False)

        self.assertEqual([row.pap for row in self.fleets()], [1, 4])

    def test_the_month_is_eve_time(self):
        fleet(self.fc, utc(2026, 3, 31, 23, 30), self.carol)
        fleet(self.fc, utc(2026, 4, 1, 0, 0), (self.carol, 3))

        for zone in ("UTC", "Asia/Shanghai", "America/Los_Angeles"):
            with self.subTest(zone=zone), override_settings(TIME_ZONE=zone):
                self.assertEqual([row.pap for row in self.fleets(self.carol)], [1])
                self.assertEqual([row.pap for row in member_fleets(self.carol, 2026, 4)], [3])

    def test_limit_counts_fleets_not_rows(self):
        rows = self.fleets(limit=1)

        # The newest fleet only, whatever number of characters were in the older one.
        self.assertEqual([row.operation_uuid for row in rows], [str(self.op2.uuid)])
        self.assertEqual(len(self.fleets(limit=2)), 2)
        self.assertEqual(self.fleets(limit=0), [])

    def test_the_default_limit_is_five_hundred(self):
        self.assertEqual(member_stats.FLEET_ROW_LIMIT, 500)

    def test_a_user_id_is_accepted(self):
        self.assertEqual(len(member_fleets(self.alice.pk, 2026, 3)), 2)

    def test_one_query_for_the_rows(self):
        with CaptureQueriesContext(connection) as queries:
            self.fleets()

        attendance = [query for query in queries if "fleetops_attendancerecord" in query["sql"]]
        self.assertEqual(len(attendance), 1)
        # Besides the settings that tell which alliances are members.
        self.assertEqual(len(queries), 2)


class MemberDetailServiceTests(DetailTestCase):
    def detail(self, user=None, **kwargs):
        return member_detail(user or self.alice, 2026, 3, **kwargs)

    def test_totals_of_the_main_include_the_alts(self):
        row = self.detail().row

        self.assertEqual((row.pap, row.fleets, row.characters), (5, 2, 2))
        self.assertEqual(row.main_character_name, "Alice")
        self.assertEqual(row.corporation_id, f.DEFAULT_CORP[0])

    def test_ranks_in_the_alliance_and_in_the_corporation(self):
        detail = self.detail()

        # Bob 6, Alice 5, Dave 1 in the alliance; Alice 5, Dave 1 in her corporation.
        self.assertEqual((detail.alliance_rank, detail.alliance_ranked), (2, 3))
        self.assertEqual((detail.corporation_rank, detail.corporation_ranked), (1, 2))
        self.assertEqual(detail.row.rank, 2)
        self.assertEqual(self.detail(self.bob).corporation_rank, 1)
        self.assertEqual(self.detail(self.dave).alliance_rank, 3)
        self.assertEqual(self.detail(self.dave).corporation_rank, 2)

    def test_ties_share_the_rank(self):
        fleet(self.fc, utc(2026, 3, 20, 18), (self.carol, 5))

        self.assertEqual(self.detail(self.carol).alliance_rank, 2)
        self.assertEqual(self.detail().alliance_rank, 2)
        self.assertEqual(self.detail(self.dave).alliance_rank, 4)

    def test_no_pap_no_rank(self):
        detail = self.detail(self.carol)

        self.assertEqual((detail.alliance_rank, detail.corporation_rank), (None, None))
        self.assertEqual((detail.alliance_ranked, detail.corporation_ranked), (3, 2))
        self.assertEqual((detail.characters, detail.fleet_types, detail.fleets), ([], [], []))
        self.assertEqual(detail.row.pap, 0)

    def test_characters_used(self):
        characters = self.detail().characters

        self.assertEqual(
            [(character.character_name, character.pap, character.fleets) for character in characters],
            [("Alice", 3, 2), ("Alice Alt", 2, 1)],
        )
        self.assertEqual(characters[0].character_id, f.main_of(self.alice).character_id)
        self.assertEqual(characters[1].character_id, self.alice_alt.character_id)

    def test_pap_per_fleet_type_most_first(self):
        detail = self.detail()

        self.assertEqual([(fleet_type.name, pap) for fleet_type, pap in detail.fleet_types], [("StratOp", 4), ("Roam", 1)])
        self.assertEqual(detail.row.by_fleet_type, {self.strat.pk: 4, self.roam.pk: 1})

    def test_last_pap_and_registered(self):
        row = self.detail().row

        self.assertEqual(row.last_pap_at, utc(2026, 3, 9, 19))
        self.assertEqual(row.registered_at, self.alice.date_joined)

    def test_status_follows_the_minimum_and_the_month(self):
        f.statistics_target(2026, 1, minimum=5)
        f.statistics_target(2026, 3, minimum=6)

        self.assertEqual(self.detail().row.status, "below")
        with clock(utc(2026, 3, 20)):
            self.assertEqual(self.detail().row.status, "below_so_far")
            self.assertEqual(self.detail(self.bob).row.status, "ok")
        self.assertEqual(self.detail().target.minimum, 6)

    def test_fleets(self):
        detail = self.detail()

        self.assertEqual([row.pap for row in detail.fleets], [1, 4])
        self.assertFalse(detail.fleets_truncated)

    def test_fleets_are_capped_and_the_cap_is_flagged(self):
        detail = self.detail(fleet_limit=1)

        self.assertEqual(len(detail.fleets), 1)
        self.assertTrue(detail.fleets_truncated)
        self.assertEqual(detail.row.fleets, 2)

    def test_the_month(self):
        self.assertEqual(self.detail().month.label, "2026-03")

    def test_members_outside_the_population_have_no_detail(self):
        gone = f.member("gone", active=False)
        biomassed = f.member("biomassed", corporation=f.DOOMHEIM_CORP, alliance=None)
        nomain = f.member("nomain", with_main=False)

        for user in (gone, biomassed, nomain, self.fc):
            with self.subTest(user=user.username):
                self.assertIsNone(self.detail(user))
        self.assertIsNone(self.detail(999_999))

    def test_departed_members_have_no_detail(self):
        set_alliances(f.DEFAULT_ALLIANCE)
        leaver = f.member("leaver", alliance=f.FOREIGN_ALLIANCE)

        self.assertIsNone(self.detail(leaver))
        self.assertIsNotNone(self.detail())

    def test_a_user_id_is_accepted(self):
        self.assertEqual(self.detail(self.alice.pk).row.pap, 5)

    def test_the_corporation_is_the_current_one(self):
        main = f.main_of(self.alice)
        main.corporation_id, main.corporation_name = f.THIRD_CORP
        main.save()

        detail = self.detail()

        self.assertEqual(detail.row.corporation_name, f.THIRD_CORP[1])
        self.assertEqual((detail.corporation_rank, detail.corporation_ranked), (1, 1))

    def test_the_queries_do_not_depend_on_the_size_of_the_alliance(self):
        def count():
            with CaptureQueriesContext(connection) as queries:
                self.detail()
            return len(queries)

        small = count()
        pilots = [f.member(f"load{index}", corporation=(f.DEFAULT_CORP, f.OTHER_CORP)[index % 2]) for index in range(30)]
        for number in range(8):
            fleet(self.fc, utc(2026, 3, 1 + number, 12), self.alice, *pilots[number::3], type_obj=f.fleet_type(f"Type {number}"))
        large = count()

        self.assertEqual(small, large)
        self.assertLessEqual(large, 12)


class DetailPageTestCase(DetailTestCase):
    def setUp(self):
        super().setUp()
        self.plain = f.member("plain", main_name="Plain", perms=f.MEMBER_PERMS)
        self.manager = f.member("manager", main_name="Manager", perms=CORP)
        self.stats = f.member("stats", main_name="Statsman", perms=ALL_STATS)

    def client_for(self, viewer):
        client = Client(raise_request_exception=False)
        client.force_login(viewer)
        return client

    def get(self, viewer, target, client=None, **params):
        params.setdefault("year", "2026")
        params.setdefault("month", "3")
        client = client or self.client_for(viewer)
        return client.get(reverse("fleetops:member_detail", args=[getattr(target, "pk", target)]), params)

    def text(self, response):
        return html.unescape(response.content.decode())


class MemberDetailAccessTests(DetailPageTestCase):
    def test_looking_at_oneself_goes_to_my_statistics(self):
        response = self.get(self.plain, self.plain, year="2025", month="7")

        self.assertRedirects(
            response, reverse("fleetops:my_statistics") + "?year=2025&month=7", fetch_redirect_response=False
        )

    def test_a_corporation_manager_sees_the_members_of_their_corporation(self):
        self.assertEqual(self.get(self.manager, self.alice).status_code, 200)
        self.assertEqual(self.get(self.manager, self.carol).status_code, 200)

    def test_a_corporation_manager_does_not_see_other_corporations(self):
        self.assertEqual(self.get(self.manager, self.bob).status_code, 404)

    def test_view_all_stats_sees_every_member(self):
        for target in (self.alice, self.bob, self.carol, self.manager):
            with self.subTest(target=target.username):
                self.assertEqual(self.get(self.stats, target).status_code, 200)

    def test_a_member_cannot_see_others(self):
        self.assertEqual(self.get(self.plain, self.alice).status_code, 404)

    def test_users_without_access_get_nothing(self):
        nobody = f.member("nobody")

        self.assertEqual(self.get(nobody, self.alice).status_code, 403)

    def test_unknown_members_are_not_found(self):
        self.assertEqual(self.get(self.stats, 99_999_999).status_code, 404)

    def test_members_outside_the_population_are_not_found(self):
        gone = f.member("gone", active=False)
        biomassed = f.member("biomassed", corporation=f.DOOMHEIM_CORP, alliance=None)
        nomain = f.member("nomain", with_main=False)

        for target in (gone, biomassed, nomain):
            with self.subTest(target=target.username):
                self.assertEqual(self.get(self.stats, target).status_code, 404)
                self.assertEqual(self.get(self.manager, target).status_code, 404)

    def test_departed_members_are_not_found(self):
        set_alliances(f.DEFAULT_ALLIANCE)
        leaver = f.member("leaver", alliance=f.FOREIGN_ALLIANCE)

        self.assertEqual(self.get(self.stats, leaver).status_code, 404)
        self.assertEqual(self.get(self.stats, self.alice).status_code, 200)

    def test_a_corporation_manager_loses_a_member_who_changed_corporation(self):
        main = f.main_of(self.carol)
        main.corporation_id, main.corporation_name = f.OTHER_CORP
        main.save()

        self.assertEqual(self.get(self.manager, self.carol).status_code, 404)


class MemberDetailPageTests(DetailPageTestCase):
    def test_header_cards_and_tables(self):
        response = self.get(self.stats, self.alice)

        self.assertEqual(response.status_code, 200)
        page = self.text(response)
        self.assertIn("<h2 class=\"mb-1\">Alice</h2>", page)
        self.assertIn(f.DEFAULT_CORP[1], page)
        self.assertIn(f.DEFAULT_ALLIANCE[1], page)
        self.assertIn("2026-03", page)
        self.assertIn("#2 <small", page)
        self.assertIn("of 3 in the alliance", page)
        self.assertIn(f"#1 of 2 in {f.DEFAULT_CORP[1]}", page)
        # Both characters, the fleet types and the doctrine.
        for expected in ("Alice Alt", "StratOp", "Roam", "Armor Hurricanes", "Automatic"):
            self.assertIn(expected, page)
        self.assertEqual(response.context["row"].pap, 5)

    def test_the_main_character_is_marked(self):
        page = self.text(self.get(self.stats, self.alice))

        self.assertEqual(page.count(">Main</span>"), 1)

    def test_times_are_shown_in_eve_time(self):
        with override_settings(TIME_ZONE="Asia/Shanghai"):
            page = self.text(self.get(self.stats, self.alice))

        self.assertIn("2026-03-05 18:00", page)
        self.assertIn("2026-03-05 19:00", page)
        self.assertIn("2026-03-09 19:00", page)
        self.assertIn(f"Registered {self.alice.date_joined.strftime('%Y-%m-%d')}", page)
        self.assertIn("Last PAP 2026-03-09 19:00 EVE", page)

    def test_a_member_without_pap(self):
        page = self.text(self.get(self.stats, self.carol))

        self.assertIn("No PAP in this month.", page)
        self.assertIn("Ranked from the first PAP", page)
        self.assertIn("No characters used in this month.", page)

    def test_the_status_follows_the_colour_rule(self):
        f.statistics_target(2026, 1, minimum=6)

        with clock(utc(2026, 3, 20, 12)):
            running = self.text(self.get(self.stats, self.alice))
        finished = self.text(self.get(self.stats, self.alice))
        reached = self.text(self.get(self.stats, self.bob))

        self.assertIn('class="badge text-bg-warning">Below minimum so far</span>', running)
        self.assertIn('class="badge text-bg-danger">Below minimum</span>', finished)
        self.assertIn('class="badge text-bg-success">OK</span>', reached)
        self.assertIn("Minimum 6 PAP", finished)

    def test_no_minimum_no_status_badge(self):
        f.statistics_target(2026, 1, minimum=0)

        page = self.text(self.get(self.stats, self.alice))

        self.assertNotIn("Below minimum", page)
        self.assertIn("Minimum 0 PAP", page)

    def test_the_back_link_follows_the_role(self):
        stats = self.text(self.get(self.stats, self.alice))
        manager = self.text(self.get(self.manager, self.alice))

        self.assertIn(f'href="{reverse("fleetops:alliance_members")}?year=2026&month=3">Back to members', stats)
        self.assertIn(f'href="{reverse("fleetops:corporation_members")}?year=2026&month=3">Back to members', manager)

    def test_the_export_links_name_the_member(self):
        page = self.text(self.get(self.stats, self.alice))

        export = reverse("fleetops:export", args=["member-fleets"])
        for fmt in ("xlsx", "csv"):
            self.assertIn(f"{export}?year=2026&month=3&format={fmt}&user={self.alice.pk}", page)

    def test_names_are_escaped(self):
        evil = f.member("evil", main_name="<script>alert(1)</script>")
        fleet(self.fc, utc(2026, 3, 8, 18), evil, doctrine_name="<b>bold</b>")

        page = self.get(self.stats, evil).content.decode()

        self.assertNotIn("<script>alert(1)</script>", page)
        self.assertNotIn("<b>bold</b>", page)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", page)

    def test_fleets_beyond_the_cap_are_noted(self):
        with mock.patch.object(member_stats, "FLEET_ROW_LIMIT", 1):
            page = self.text(self.get(self.stats, self.alice))

        self.assertIn("The newest 1 of 2 fleets are listed.", page)

    def test_no_note_when_everything_is_listed(self):
        self.assertNotIn("fleets are listed", self.text(self.get(self.stats, self.alice)))

    def test_a_month_older_than_the_retention_window_is_noted(self):
        self.assertIn("PAP older than 365 days is removed", self.text(self.get(self.stats, self.alice, year="2025", month="3")))
        self.assertNotIn("PAP older than", self.text(self.get(self.stats, self.alice)))

    def test_the_month_selector_is_there(self):
        page = self.text(self.get(self.stats, self.alice, month="2"))

        self.assertIn("2026-02", page)
        self.assertIn("Month (EVE time)", page)


class FleetLinkTests(DetailPageTestCase):
    def link(self, operation):
        return reverse("fleetops:operation_detail", args=[operation.uuid])

    def test_corporation_managers_see_no_fleet_links(self):
        page = self.text(self.get(self.manager, self.alice))

        self.assertNotIn(self.link(self.op1), page)
        self.assertNotIn(self.link(self.op2), page)
        self.assertIn("2026-03-05 18:00", page)

    def test_statistics_managers_see_no_fleet_links_either(self):
        # view_all_stats and view_all_fleets stay separate permissions.
        page = self.text(self.get(self.stats, self.alice))

        self.assertNotIn(self.link(self.op1), page)

    def test_view_all_fleets_opens_the_fleets(self):
        viewer = f.member("viewer", main_name="Viewer", perms=ALL_STATS + ["fleetops.view_all_fleets"])

        response = self.get(viewer, self.alice)

        self.assertTrue(response.context["can_open_fleets"])
        page = self.text(response)
        self.assertIn(self.link(self.op1), page)
        self.assertIn(self.link(self.op2), page)

    def test_manage_fleets_opens_the_fleets(self):
        viewer = f.member("lead", main_name="Lead", perms=ALL_STATS + ["fleetops.manage_fleets"])

        self.assertIn(self.link(self.op1), self.text(self.get(viewer, self.alice)))

    def test_corporation_manager_with_view_all_fleets(self):
        viewer = f.member("fleetwatcher", main_name="Fleetwatcher", perms=CORP + ["fleetops.view_all_fleets"])

        self.assertIn(self.link(self.op1), self.text(self.get(viewer, self.alice)))

    def test_only_counted_fleets_are_listed(self):
        pending = fleet(self.fc, utc(2026, 3, 10, 18), (self.alice, 4), review_status=ReviewStatus.PENDING)
        excluded = fleet(self.fc, utc(2026, 3, 11, 18), (self.alice, 4), counts_for_stats=False)
        running = f.create_operation(self.fc, started_at=utc(2026, 3, 12, 18))
        f.add_attendance(running, self.alice, value=4)
        viewer = f.member("lead", main_name="Lead", perms=ALL_STATS + ["fleetops.manage_fleets"])

        page = self.text(self.get(viewer, self.alice))

        for operation in (self.op1, self.op2):
            self.assertIn(self.link(operation), page)
        for operation in (pending, excluded, running):
            self.assertNotIn(self.link(operation), page)
        self.assertEqual(self.get(viewer, self.alice).context["row"].pap, 5)


class MemberDetailBudgetTests(DetailPageTestCase):
    def count(self, viewer, target):
        client = self.client_for(viewer)
        self.get(viewer, target, client=client)
        with CaptureQueriesContext(connection) as queries:
            response = self.get(viewer, target, client=client)
        self.assertEqual(response.status_code, 200)
        return len(queries)

    def test_the_same_queries_for_a_larger_dataset(self):
        small = {"stats": self.count(self.stats, self.alice), "manager": self.count(self.manager, self.alice)}

        pilots = [f.member(f"load{index}", corporation=(f.DEFAULT_CORP, f.OTHER_CORP, f.THIRD_CORP)[index % 3]) for index in range(30)]
        alts = [f.add_alt(self.alice, f"Extra Alt {index}") for index in range(3)]
        for number in range(12):
            fleet(
                self.fc,
                utc(2026, 3, 1 + number, 12),
                (self.alice, 1, alts[number % 3]),
                *pilots[number % 3 :: 3],
                type_obj=f.fleet_type(f"Type {number % 5}"),
            )
        large = {"stats": self.count(self.stats, self.alice), "manager": self.count(self.manager, self.alice)}

        self.assertEqual(small, large)
        # Including the login, permission and menu queries of the framework, and the menu's group lookup for
        # viewers without start_fleet.
        self.assertLessEqual(max(large.values()), 35)
