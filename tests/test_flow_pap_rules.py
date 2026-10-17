# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""End-to-end path of the PAP rules release through the web pages.

An FC starts a fleet on the in-memory ESI fleet, pilots join, leave and come back between polls,
register through PAP links and get kicked. The fleet ends with the FC's multiplier choice, PAP is
granted from time in fleet and the per-player limit, manual and pasted PAP wait for an FC lead,
and the statistics credit each player's main. Nothing leaves the test process.
"""

import uuid
from contextlib import contextmanager
from datetime import timedelta, timezone as dt_timezone
from types import SimpleNamespace
from unittest import mock

from django.contrib.messages import get_messages
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from fleetops.constants import CLICK_SCOPES
from fleetops.models import (
    AttendanceRecord,
    AuditLog,
    FleetMemberEvent,
    FleetMemberState,
    FleetOperation,
    PapLink,
    PapRegistration,
    ReviewStatus,
)
from fleetops.providers import esi as esi_provider
from fleetops.services.statistics import corporation_statistics, fc_statistics, member_statistics
from fleetops.tasks import track_operation

from . import factories as f
from .link_fakes import setUpModule, tearDownModule  # noqa: F401 - unittest runs them around the module
from .fleet_sim import FleetSim
from .test_pap_links import FakeLocationAPI

UTC = dt_timezone.utc
Status = FleetOperation.Status
Source = AttendanceRecord.Source
Event = FleetMemberEvent.EventType
JITA, AMAMAKE = 30000142, 30002537
# Pilots Alliance Auth does not know.
OUTSIDE_PILOT = (2_119_777_001, "Outside Pilot")
FOREIGN_PILOT = (2_119_777_002, "Foreign Pilot")


def flashed(response):
    return [str(message) for message in get_messages(response.wsgi_request)]


class FakePost:
    """A prepared POST operation of django-esi; the flow checks that it bypasses the response cache."""

    def __init__(self, answer):
        self.answer = answer

    def result(self, use_etag=True, use_cache=True, store_cache=True, **kwargs):
        assert not use_cache and not store_cache, "POST answers must not come from django-esi's cache"
        return self.answer


class FakeUniverseAPI:
    """``esi.client.Universe`` and ``esi.client.Character`` for the pilots Alliance Auth does not know."""

    def __init__(self, *pilots):
        self.pilots = dict(pilots)
        self.id_lookups = []

    def PostUniverseIds(self, *, body):
        self.id_lookups.append(list(body))
        wanted = {name.casefold() for name in body}
        return FakePost(
            {"characters": [{"id": cid, "name": name} for cid, name in self.pilots.items() if name.casefold() in wanted]}
        )

    def PostUniverseNames(self, *, body):
        return FakePost(
            [{"id": cid, "name": self.pilots[cid], "category": "character"} for cid in body if cid in self.pilots]
        )

    def PostCharactersAffiliation(self, *, body):
        return FakePost([{"character_id": cid, "corporation_id": 98_000_001} for cid in body if cid in self.pilots])


class PapRulesFlowTests(TestCase):
    def setUp(self):
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)
        # The fleet ran a few hours ago: frozen times stay before the tokens were made, so none needs a refresh.
        self.t0 = (timezone.now() - timedelta(hours=5)).astimezone(UTC).replace(second=0, microsecond=0)
        f.settings(srp_auto_create=False, pap_minimum_minutes=20, attendance_limit=1)
        f.multiplier_tiers()
        self.stratop = f.fleet_type("StratOp", "2.50")

        self.lead = f.create_user("lead", perms=f.FC_LEAD_PERMS, main_name="Fleet Lead")
        self.fc = f.create_user("fc", perms=f.FC_PERMS, main_name="Flow FC")
        # The FC flies an alt as fleet boss and has the main in fleet too.
        self.boss = f.add_alt(self.fc, "Flow Boss")
        f.add_token(self.fc, self.boss)
        self.alpha = f.create_user("alpha", perms=f.MEMBER_PERMS, main_name="Alpha Main")
        # An alt in another corporation and alliance; its PAP and statistics belong to the main.
        self.alpha_alt = f.add_alt(self.alpha, "Alpha Alt", corporation=f.OTHER_CORP, alliance=f.FOREIGN_ALLIANCE)
        self.bravo = f.create_user("bravo", perms=f.MEMBER_PERMS, main_name="Bravo Main")
        self.delta = f.create_user("delta", perms=f.MEMBER_PERMS, main_name="Delta Main")
        self.echo = f.create_user("echo", perms=f.MEMBER_PERMS, main_name="Echo Main")
        self.foxtrot = f.create_user("foxtrot", perms=f.MEMBER_PERMS, main_name="Foxtrot Main")
        self.golf = f.create_user("golf", perms=f.MEMBER_PERMS, main_name="Golf Main")
        self.golf_alt = f.add_alt(self.golf, "Golf Alt", corporation=f.OTHER_CORP, alliance=f.FOREIGN_ALLIANCE)
        self.hotel = f.create_user("hotel", perms=f.MEMBER_PERMS, main_name="Hotel Main")
        self.india = f.create_user("india", perms=f.MEMBER_PERMS, main_name="India Main")
        for user in (self.delta, self.echo):
            f.add_token(user, f.main_of(user), scopes=CLICK_SCOPES)

        self.sim = FleetSim()
        self.location = FakeLocationAPI()
        self.location.pilot(f.main_of(self.delta), online=True)
        self.location.pilot(f.main_of(self.echo), online=False)
        self.universe = FakeUniverseAPI(OUTSIDE_PILOT, FOREIGN_PILOT)
        client = SimpleNamespace(
            Fleets=self.sim, Location=self.location, Universe=self.universe, Character=self.universe
        )
        patcher = mock.patch.object(esi_provider, "esi", SimpleNamespace(client=client))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.fleet = self.sim.create_fleet(self.boss.character_id)
        self.fleet.update_member(self.boss.character_id, join_time=self.minute(-10))

    # Helpers

    def minute(self, minutes):
        return self.t0 + timedelta(minutes=minutes)

    @contextmanager
    def at(self, minutes):
        with mock.patch("django.utils.timezone.now", return_value=self.minute(minutes)):
            yield

    def join(self, character, minutes, **details):
        character_id = getattr(character, "character_id", character)
        self.fleet.add_member(character_id, join_time=self.minute(minutes), **details)

    def leave(self, character):
        self.fleet.remove_member(getattr(character, "character_id", character))

    def poll(self, minutes):
        # The django-esi cache window has passed since the previous poll. The name lookup of pilots
        # Alliance Auth does not know runs once the poll's transaction has committed.
        self.sim.expire_cache()
        with self.at(minutes), self.captureOnCommitCallbacks(execute=True):
            return track_operation(self.operation.pk)

    def state(self, character):
        return FleetMemberState.objects.get(operation=self.operation, character_id=character.character_id)

    def row(self, character, source=Source.AUTOMATIC):
        return AttendanceRecord.objects.get(operation=self.operation, character_id=character.character_id, source=source)

    def detail(self, user, minutes):
        self.client.force_login(user)
        with self.at(minutes):
            return self.client.get(reverse("fleetops:operation_detail", args=[self.operation.uuid]))

    def post(self, user, minutes, name, data=None, args=None):
        self.client.force_login(user)
        with self.at(minutes):
            return self.client.post(reverse(name, args=[self.operation.uuid] if args is None else args), data or {})

    # The running fleet

    def start_fleet(self):
        fc_main = f.main_of(self.fc)
        self.join(fc_main, -5)
        self.join(self.alpha_alt, -2)
        self.join(f.main_of(self.bravo), 0)
        self.join(f.main_of(self.foxtrot), 0)
        self.join(self.golf_alt, 0, solar_system_id=AMAMAKE)
        self.join(OUTSIDE_PILOT[0], 0)
        self.client.force_login(self.fc)
        data = {
            "request_id": str(uuid.uuid4()),
            "operation_mode": "attendance_only",
            "fleet_timing": "now",
            "fc_character_id": str(self.boss.character_id),
            "fleet_type": str(self.stratop.pk),
            "formup": "Jita",
        }
        with self.at(0):
            response = self.client.post(reverse("fleetops:start_fleet"), data)
        self.operation = FleetOperation.objects.get(start_request_id=data["request_id"])
        self.assertRedirects(
            response, reverse("fleetops:operation_detail", args=[self.operation.uuid]), fetch_redirect_response=False
        )
        self.assertEqual((self.operation.status, self.operation.esi_fleet_id), (Status.ACTIVE, self.fleet.fleet_id))
        # The fleet keeps the PAP rules of its start.
        self.assertEqual((self.operation.pap_minimum_minutes, self.operation.pap_character_limit), (20, 1))
        self.assertEqual(AuditLog.objects.filter(action="fleet.start").count(), 1)

    def run_fleet(self):
        """Start the fleet and fly it for 100 minutes; it is still active afterwards."""
        self.start_fleet()
        self.assertEqual(self.poll(5), 7)

        # Single kick: Foxtrot leaves after 5 minutes.
        response = self.post(
            self.fc, 5, "fleetops:kick_member", args=[self.operation.uuid, f.main_of(self.foxtrot).character_id]
        )
        self.assertIn("Kicked Foxtrot Main (Test Corp One).", flashed(response))
        self.assertNotIn(f.main_of(self.foxtrot).character_id, self.fleet.members)
        self.assertFalse(self.state(f.main_of(self.foxtrot)).is_active)

        # Echo flies 2 minutes, Bravo leaves and comes back.
        self.join(f.main_of(self.echo), 8)
        self.poll(10)
        self.leave(f.main_of(self.echo))
        self.leave(f.main_of(self.bravo))
        self.join(f.main_of(self.alpha), 20)
        self.poll(20)
        self.join(f.main_of(self.bravo), 25)
        self.poll(30)

        # PAP links: one that needs the character online, one for pilots out of the game.
        self.post(self.fc, 30, "fleetops:create_pap_link", {"minutes": "30", "require_online": "on"})
        self.post(self.fc, 30, "fleetops:create_pap_link", {"minutes": "30"})
        online_link, offline_link = PapLink.objects.filter(operation=self.operation).order_by("pk")
        self.assertEqual((online_link.require_online, offline_link.require_online), (True, False))
        response = self.post(
            self.delta, 35, "fleetops:pap_link_register",
            {"character_ids": [str(f.main_of(self.delta).character_id)]}, args=[online_link.code],
        )
        self.assertEqual(response.status_code, 302)
        response = self.post(
            self.echo, 35, "fleetops:pap_link_register",
            {"character_ids": [str(f.main_of(self.echo).character_id)]}, args=[online_link.code],
        )
        self.assertIn("Not logged in to the game", " ".join(flashed(response)))
        self.post(
            self.echo, 36, "fleetops:pap_link_register",
            {"character_ids": [str(f.main_of(self.echo).character_id)]}, args=[offline_link.code],
        )
        registered = dict(PapRegistration.objects.filter(operation=self.operation).values_list("character_id", "online"))
        self.assertEqual(
            registered, {f.main_of(self.delta).character_id: True, f.main_of(self.echo).character_id: None}
        )

        # A special role with FC credit for a pilot who is no FC.
        response = self.post(
            self.fc, 38, "fleetops:add_operation_role",
            {"role": "backseat_fc", "character_id": str(f.main_of(self.bravo).character_id)},
        )
        self.assertIn("Bravo Main assigned as Back Seat FC; FC credit granted.", flashed(response))

        # Kick everyone outside the FC's system: the preview picks Jita and lists Golf's alt.
        self.poll(40)
        self.client.force_login(self.fc)
        with self.at(40):
            preview = self.client.get(reverse("fleetops:kick_preview", args=[self.operation.uuid]), {"mode": "system"})
        preview = preview.json()
        self.assertEqual((preview["selected_system_id"], preview["fc_system_id"]), (JITA, JITA))
        self.assertEqual([member["name"] for member in preview["members"]], ["Golf Alt"])
        response = self.post(
            self.fc, 40, "fleetops:kick_outside_system",
            {"system_id": str(JITA), "character_ids": [str(self.golf_alt.character_id)]},
        )
        self.assertIn(f"Kicked 1 member outside {JITA}.", flashed(response))
        self.assertFalse(self.state(self.golf_alt).is_active)

        # Alpha's alt leaves and rejoins between two polls; only the time since the rejoin counts.
        self.leave(self.alpha_alt)
        self.join(self.alpha_alt, 45)
        self.poll(50)
        self.poll(100)

    def test_running_fleet_tracks_time_and_shows_members_their_own_pap(self):
        self.run_fleet()

        minutes = lambda character: self.state(character).accumulated_seconds // 60  # noqa: E731
        self.assertEqual(minutes(self.boss), 100)
        self.assertEqual(minutes(f.main_of(self.foxtrot)), 5)
        self.assertEqual(minutes(f.main_of(self.echo)), 2)
        # Bravo: 10 minutes, then from the rejoin at minute 25 on.
        self.assertEqual(minutes(f.main_of(self.bravo)), 10 + 75)
        self.assertEqual(minutes(self.golf_alt), 40)
        # Alpha's alt: 0-40 and 45-100, the five minutes away between two polls are not counted.
        self.assertEqual(minutes(self.alpha_alt), 40 + 55)
        self.assertEqual(minutes(f.main_of(self.alpha)), 80)
        # The member list shows the alt's own corporation.
        self.assertEqual(self.state(self.golf_alt).corporation_name, "Test Corp Two")

        # JOIN and REJOIN events keep ESI's join time.
        bravo_events = list(
            FleetMemberEvent.objects.filter(
                operation=self.operation, character_id=f.main_of(self.bravo).character_id
            ).order_by("pk").values_list("event_type", "new_value")
        )
        self.assertEqual(bravo_events[0], (Event.JOIN, self.minute(0).strftime("%Y-%m-%dT%H:%M:%SZ")))
        self.assertIn((Event.REJOIN, self.minute(25).strftime("%Y-%m-%dT%H:%M:%SZ")), bravo_events)
        # Tracking writes no PAP while the fleet runs.
        self.assertFalse(AttendanceRecord.objects.filter(operation=self.operation).exists())

        # VIS(2): a member sees only their own row of the PAP preview, not the member list.
        page = self.detail(self.bravo, 100)
        self.assertEqual(page.status_code, 200)
        self.assertEqual([c.character_name for c in page.context["pap_preview"]], ["Bravo Main"])
        self.assertEqual(page.context["pap_unregistered"], [])
        self.assertContains(page, 'id="composition-hidden"')
        self.assertNotContains(page, "Alpha Main")
        self.client.force_login(self.alpha)
        with self.at(100):
            live = self.client.get(reverse("fleetops:live_pap"))
        self.assertContains(live, "Your character <strong>Alpha Alt</strong> is in <strong>Flow Boss</strong>'s fleet")
        self.assertContains(live, "your character Alpha Main gets the PAP.")
        # The FC sees everybody, the pilot Alliance Auth does not know included.
        page = self.detail(self.fc, 100)
        self.assertEqual(len(page.context["pap_preview"]), 9)
        self.assertEqual(page.context["pap_unregistered"], [OUTSIDE_PILOT])
        self.assertContains(page, 'id="endFleetModal"')

    def end_fleet(self):
        # 100 minutes allow 1x or 2x; the server checks the choice whatever the page showed.
        response = self.post(self.fc, 100, "fleetops:end_fleet", {"attendance_multiplier": "3"})
        self.assertIn("3x needs the fleet to run longer than 180 minutes. The fleet was not ended.", flashed(response))
        response = self.post(self.fc, 100, "fleetops:end_fleet")
        self.assertIn(
            "This fleet now runs longer than 90 minutes; choose the PAP multiplier. The fleet was not ended.",
            flashed(response),
        )
        self.operation.refresh_from_db()
        self.assertEqual(self.operation.status, Status.ACTIVE)
        self.sim.expire_cache()
        response = self.post(self.fc, 100, "fleetops:end_fleet", {"attendance_multiplier": "2"})
        self.assertIn("Fleet ended. PAP finalized at 2x.", flashed(response))
        self.operation.refresh_from_db()
        self.assertEqual(
            (self.operation.status, self.operation.attendance_multiplier, self.operation.multiplier_source),
            (Status.CLOSED, 2, FleetOperation.MultiplierSource.FC),
        )
        self.assertFalse(self.operation.multiplier_pending)

    def test_close_grants_pap_then_corrections_approvals_statistics_and_override(self):
        self.run_fleet()
        self.end_fleet()
        year, month = self.t0.year, self.t0.month

        # PAP at close: time in fleet, PAP links and one character per player.
        granted = {
            row.character_name: (row.source, row.attendance_value)
            for row in AttendanceRecord.objects.filter(operation=self.operation, granted=True)
        }
        self.assertEqual(
            granted,
            {
                # The FC character comes before the FC's main.
                "Flow Boss": (Source.AUTOMATIC, 2),
                # The main comes before the alt with more time in fleet.
                "Alpha Main": (Source.AUTOMATIC, 2),
                "Bravo Main": (Source.AUTOMATIC, 2),
                # Two minutes in fleet, but registered through a PAP link.
                "Echo Main": (Source.AUTOMATIC, 2),
                "Delta Main": (Source.CLICK, 2),
                "Golf Alt": (Source.AUTOMATIC, 2),
            },
        )
        self.assertTrue(self.row(f.main_of(self.fc)).capped)
        self.assertTrue(self.row(self.alpha_alt).capped)
        self.assertTrue(self.row(f.main_of(self.foxtrot)).below_minimum)
        self.assertEqual(self.row(self.alpha_alt).tracked_seconds, 95 * 60)
        # Statistics stay with the main's corporation, whatever corporation the alt is in.
        self.assertEqual(self.row(self.golf_alt).corporation_name, "Test Corp One")
        self.assertEqual(self.row(self.golf_alt).main_character_name, "Golf Main")
        self.assertFalse(AttendanceRecord.objects.filter(character_id=OUTSIDE_PILOT[0]).exists())
        end = AuditLog.objects.get(action="fleet.end", object_id=str(self.operation.pk))
        self.assertEqual(end.new_value["pap"]["granted"], 6)
        self.assertEqual(end.new_value["pap"]["unregistered"], 1)

        # The closed fleet lists the join times and the PAP outcome of every character.
        page = self.detail(self.fc, 105)
        self.assertContains(page, f"joined {self.minute(25):%H:%M} EVE")
        self.assertContains(page, "Capped (limit)")
        self.assertContains(page, "Below minimum")
        self.assertContains(page, "Recalculate PAP")

        # An FC's manual PAP by name and a pasted roster wait for an FC lead.
        response = self.post(
            self.fc, 110, "fleetops:add_manual_attendance",
            {"character_name": "india main", "attendance_value": "1", "duplicate_action": "keep", "notes": "Scout"},
        )
        self.assertIn("Manual PAP saved. It counts once an FC lead approves it.", flashed(response))
        manual = self.row(f.main_of(self.india), Source.MANUAL)
        self.assertEqual((manual.review_status, manual.granted), (ReviewStatus.PENDING, False))

        roster = "\n".join(["Hotel Main", "Alpha Alt", "Foreign Pilot", "Nobody Atall", "Flow Boss"])
        response = self.post(
            self.fc, 112, "fleetops:roster_import",
            {"text": roster, "attendance_value": "1", "duplicate_action": "keep", "notes": "Roster"},
            args=[self.operation.uuid],
        )
        statuses = {item["row"].character_name: item["row"].status for item in response.context["preview_rows"]}
        self.assertEqual(
            statuses,
            {
                "Hotel Main": "add",
                # Alpha already has PAP with the main.
                "Alpha Alt": "capped",
                "Foreign Pilot": "not_in_auth",
                "Nobody Atall": "not_found",
                "Flow Boss": "has_pap",
            },
        )
        self.assertEqual(self.universe.id_lookups, [["Foreign Pilot", "Nobody Atall"]])
        hotel_index = next(
            item["index"] for item in response.context["preview_rows"] if item["row"].character_name == "Hotel Main"
        )
        response = self.post(
            self.fc, 113, "fleetops:roster_import",
            {"step": "import", "payload": response.context["payload"], "rows": [str(hotel_index)]},
            args=[self.operation.uuid],
        )
        self.assertIn("Imported PAP for 1 character. It counts once an FC lead approves it.", flashed(response))
        imported = self.row(f.main_of(self.hotel), Source.IMPORTED)
        self.assertEqual((imported.review_status, imported.granted), (ReviewStatus.PENDING, False))
        self.assertEqual(AuditLog.objects.filter(action="attendance.roster_import").count(), 1)
        self.assertEqual(member_statistics(self.india, year, month)["total"], 0)

        # The FC lead approves both rows from the approvals page.
        self.client.force_login(self.lead)
        with self.at(115):
            queue = self.client.get(reverse("fleetops:approval_queue"))
        self.assertContains(queue, "India Main")
        self.assertContains(queue, "Hotel Main")
        response = self.post(
            self.lead, 115, "fleetops:review_attendance",
            {"ids": [str(manual.pk), str(imported.pk)], "action": "approve", "note": ""}, args=[],
        )
        self.assertIn("2 PAP rows approved.", flashed(response))
        manual.refresh_from_db()
        imported.refresh_from_db()
        self.assertEqual((manual.review_status, manual.granted), (ReviewStatus.APPROVED, True))
        self.assertTrue(imported.granted)
        self.assertEqual(member_statistics(self.india, year, month)["total"], 1)

        # Statistics credit the main and the main's corporation.
        alpha_stats = member_statistics(self.alpha, year, month)
        self.assertEqual((alpha_stats["total"], alpha_stats["unique_fleets"]), (2, 1))
        self.assertEqual([row["character_name"] for row in alpha_stats["characters"]], ["Alpha Main"])
        golf_stats = member_statistics(self.golf, year, month)
        self.assertEqual([row["character_name"] for row in golf_stats["characters"]], ["Golf Alt"])
        corporation = corporation_statistics(f.DEFAULT_CORP[0], year, month)
        # Six tracked or linked PAP at 2x, plus the two approved rows at 1.
        self.assertEqual(corporation["total_attendance"], 6 * 2 + 2)
        self.assertEqual(corporation_statistics(f.OTHER_CORP[0], year, month)["total_attendance"], 0)

        # FC points are the fleet type's weight times the multiplier, also for the special role.
        self.assertEqual(
            fc_statistics(self.fc, year, month),
            {"fleet_count": 1, "total_points": 5.0, "breakdown": {"StratOp": {"count": 1, "points": 5.0}}},
        )
        self.assertEqual(fc_statistics(self.bravo, year, month)["total_points"], 5.0)
        self.assertEqual(member_statistics(self.bravo, year, month)["roles"][0]["label"], "Back Seat FC")

        # After the close the FC stays within the duration rule; an FC lead may go beyond it with a reason.
        response = self.post(self.fc, 120, "fleetops:set_attendance_multiplier", {"attendance_multiplier": "3"})
        self.assertIn("The PAP multiplier was not changed.", " ".join(flashed(response)))
        response = self.post(self.lead, 120, "fleetops:set_attendance_multiplier", {"attendance_multiplier": "3"})
        self.assertIn("The PAP multiplier was not changed.", " ".join(flashed(response)))
        self.operation.refresh_from_db()
        self.assertEqual(self.operation.attendance_multiplier, 2)
        response = self.post(
            self.lead, 121, "fleetops:set_attendance_multiplier",
            {"attendance_multiplier": "3", "reason": "Structure timer ran long"},
        )
        self.assertIn("PAP multiplier set to 3x; PAP recalculated (6 granted).", flashed(response))
        self.operation.refresh_from_db()
        self.assertEqual(
            (self.operation.attendance_multiplier, self.operation.multiplier_source),
            (3, FleetOperation.MultiplierSource.OVERRIDE),
        )
        entry = AuditLog.objects.filter(action="attendance.multiplier").latest("pk")
        self.assertEqual((entry.actor, entry.reason), (self.lead, "Structure timer ran long"))
        self.assertEqual(self.row(f.main_of(self.bravo)).attendance_value, 3)
        # Manual rows keep their own value.
        manual.refresh_from_db()
        self.assertEqual(manual.attendance_value, 1)
        self.assertEqual(fc_statistics(self.fc, year, month)["total_points"], 7.5)
        self.assertEqual(member_statistics(self.alpha, year, month)["total"], 3)


class PapLinkFleetFlowTests(TestCase):
    """A fleet without ESI fleet: PAP only by link, and it counts once an FC lead approves it."""

    def setUp(self):
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)
        self.t0 = (timezone.now() - timedelta(hours=3)).astimezone(UTC).replace(second=0, microsecond=0)
        f.settings(srp_auto_create=False, pap_minimum_minutes=20, attendance_limit=1)
        f.multiplier_tiers()
        self.fleet_type = f.fleet_type("Roam", "1.50")
        self.lead = f.create_user("lead", perms=f.FC_LEAD_PERMS, main_name="Fleet Lead")
        self.fc = f.create_user("fc", perms=f.FC_PERMS, main_name="Link FC")
        self.pilot = f.create_user("pilot", perms=f.MEMBER_PERMS, main_name="Pilot Main")
        self.alt = f.add_alt(self.pilot, "Pilot Alt", corporation=f.OTHER_CORP, alliance=f.FOREIGN_ALLIANCE)
        self.location = FakeLocationAPI()
        for character in (f.main_of(self.pilot), self.alt):
            f.add_token(self.pilot, character, scopes=CLICK_SCOPES)
            self.location.pilot(character)
        self.fleets = FleetSim()
        client = SimpleNamespace(Fleets=self.fleets, Location=self.location)
        patcher = mock.patch.object(esi_provider, "esi", SimpleNamespace(client=client))
        patcher.start()
        self.addCleanup(patcher.stop)

    @contextmanager
    def at(self, minutes):
        with mock.patch("django.utils.timezone.now", return_value=self.t0 + timedelta(minutes=minutes)):
            yield

    def test_link_only_fleet_from_start_to_statistics(self):
        self.client.force_login(self.fc)
        data = {
            "request_id": str(uuid.uuid4()),
            "fc_character_id": str(f.main_of(self.fc).character_id),
            "fleet_type": str(self.fleet_type.pk),
            "formup": "Home",
            "link_minutes": "45",
            "require_online": "on",
        }
        with self.at(0):
            response = self.client.post(reverse("fleetops:start_link_only_fleet"), data)
        operation = FleetOperation.objects.get(link_only=True)
        self.assertRedirects(
            response, reverse("fleetops:operation_detail", args=[operation.uuid]), fetch_redirect_response=False
        )
        self.assertEqual(
            (operation.status, operation.esi_fleet_id, operation.review_status),
            (Status.ACTIVE, None, ReviewStatus.PENDING),
        )
        self.assertEqual((operation.pap_minimum_minutes, operation.pap_character_limit), (20, 1))
        start = AuditLog.objects.get(action="fleet.start")
        self.assertTrue(start.new_value["link_only"])
        link = operation.pap_links.get()

        # Both characters register; the per-player limit keeps the main.
        self.client.force_login(self.pilot)
        with self.at(10):
            self.client.post(
                reverse("fleetops:pap_link_register", args=[link.code]),
                {"character_ids": [str(self.alt.character_id), str(f.main_of(self.pilot).character_id)]},
            )
        self.assertEqual(operation.pap_registrations.count(), 2)
        with self.at(10):
            page = self.client.get(reverse("fleetops:operation_detail", args=[operation.uuid]))
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, 'id="badge-link-only"')

        # Ended after 40 minutes: a plain end at 1x, no ESI call.
        self.client.force_login(self.fc)
        with self.at(40):
            response = self.client.post(reverse("fleetops:end_fleet", args=[operation.uuid]))
        self.assertIn("Fleet ended. PAP finalized at 1x.", flashed(response))
        self.assertEqual(self.fleets.requests, [])
        granted = AttendanceRecord.objects.filter(operation=operation, granted=True)
        self.assertEqual([(row.character_name, row.source) for row in granted], [("Pilot Main", Source.CLICK)])
        self.assertTrue(AttendanceRecord.objects.get(operation=operation, character_id=self.alt.character_id).capped)
        year, month = self.t0.year, self.t0.month
        self.assertEqual(member_statistics(self.pilot, year, month)["total"], 0)
        self.assertEqual(fc_statistics(self.fc, year, month)["fleet_count"], 0)

        # The FC lead sees it in the approvals queue as a PAP-link fleet and approves it.
        self.client.force_login(self.lead)
        with self.at(50):
            queue = self.client.get(reverse("fleetops:approval_queue"))
        self.assertContains(queue, '<span class="fo-pill fo-pill-info">PAP link</span>')
        with self.at(50):
            response = self.client.post(
                reverse("fleetops:review_operation", args=[operation.uuid]),
                {"action": "approve", "note": "", "include_attendance": "on"},
            )
        self.assertIn("Fleet approved.", flashed(response))
        self.assertEqual(member_statistics(self.pilot, year, month)["total"], 1)
        self.assertEqual(fc_statistics(self.fc, year, month)["total_points"], 1.5)
