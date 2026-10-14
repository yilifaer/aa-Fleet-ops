# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""The Fleet tab's state endpoint, the live ESI refresh, the Refresh button and the fleet page parts (F9, Q1)."""

import gzip
import json
from datetime import timedelta
from unittest import mock

from django.contrib.messages import get_messages
from django.core.cache import cache
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from fleetops.models import FleetMemberState, FleetOperation
from fleetops.services import fleet_jobs, fleet_refresh
from fleetops.services.fleet_access import FLEET_GONE, not_boss_message

from . import factories as f
from .fleet_sim import FleetSim

Status = FleetOperation.Status
JSON = {"HTTP_ACCEPT": "application/json"}
FORM = {"HTTP_ACCEPT": "text/html,application/xhtml+xml,*/*;q=0.8"}
JITA = 30000142
JITA_STATION = 60003760
STATE_KEYS = {
    "version", "server_time", "status", "last_esi_update", "structure_read_at", "stale", "job", "unchanged",
    "member_count", "max_members", "stat_last_esi", "write", "status_html", "tree_html", "composition_html",
    "changes_html", "members_html", "pap_html", "controls_html",
}


class FleetStateTestCase(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        f.settings(fleet_live_refresh_seconds=15)
        self.fc = f.create_user(perms=f.FC_PERMS, main_name="State Boss")
        self.fc_char = f.main_of(self.fc)
        f.add_token(self.fc, self.fc_char)
        self.lead = f.create_user(perms=f.FC_LEAD_PERMS)
        self.viewer = f.create_user(perms=f.MEMBER_PERMS + ["fleetops.view_all_fleets"])
        # An FC who does not fly this fleet and cannot see all fleets.
        self.other_fc = f.create_user(perms=f.MEMBER_PERMS + ["fleetops.start_fleet", "fleetops.manage_own_fleet"])
        self.member = f.create_user(perms=f.MEMBER_PERMS, main_name="Line Pilot")
        self.member_char = f.main_of(self.member)
        self.no_access = f.create_user()

        self.sim = FleetSim()
        self.sim.install(self)
        self.fleet = self.sim.create_fleet(self.fc_char.character_id, wings=(("Main", ("Alpha", "Bravo")),))
        self.fleet.add_member(self.member_char.character_id, station_id=JITA_STATION)
        old = timezone.now() - timedelta(minutes=2)
        self.operation = f.create_operation(self.fc, esi_fleet_id=self.fleet.fleet_id, last_esi_update=old)
        self.names = mock.patch("fleetops.services.tracking.queue_name_resolution")
        self.names.start()
        self.addCleanup(self.names.stop)
        self.sync()

    def sync(self):
        fleet_refresh.refresh_now(self.operation, min_interval=0)
        # Make the stored read older than the live refresh interval again.
        FleetOperation.objects.filter(pk=self.operation.pk).update(last_esi_update=timezone.now() - timedelta(minutes=2))
        self.operation.refresh_from_db()

    def client_for(self, user):
        client = Client()
        client.force_login(user)
        return client

    def state_url(self, operation=None):
        return reverse("fleetops:fleet_state", args=[(operation or self.operation).uuid])

    def refresh_url(self, operation=None):
        return reverse("fleetops:fleet_refresh", args=[(operation or self.operation).uuid])

    def member_reads(self):
        return len(self.sim.requests_of("GetFleetsFleetIdMembers"))


class StateVisibilityTests(FleetStateTestCase):
    """Q1 / VIS(2): the live tab is for the fleet's managers and view_all_fleets / manage_fleets."""

    def test_who_gets_the_state_of_an_open_fleet(self):
        for user, status in (
            (self.fc, 200),
            (self.lead, 200),
            (self.viewer, 200),
            (self.other_fc, 404),
            (self.member, 404),
            (self.no_access, 403),
        ):
            with self.subTest(user=user.username):
                self.assertEqual(self.client_for(user).get(self.state_url()).status_code, status)
        self.assertEqual(Client().get(self.state_url()).status_code, 302)

    def test_closed_fleets_show_their_composition_to_everyone_who_opens_the_page(self):
        FleetOperation.objects.filter(pk=self.operation.pk).update(status=Status.CLOSED, ended_at=timezone.now())

        response = self.client_for(self.member).get(self.state_url())

        self.assertEqual(response.status_code, 200)
        state = response.json()
        self.assertEqual(state["status"], "closed")
        self.assertIn("Last known ships of everyone tracked in this fleet.", state["composition_html"])
        for key in ("status_html", "tree_html", "changes_html", "members_html", "pap_html", "controls_html"):
            self.assertIsNone(state[key], key)
        self.assertFalse(state["write"]["allowed"])
        self.assertEqual(self.client_for(self.no_access).get(self.state_url()).status_code, 403)

    def test_viewers_get_a_read_only_state(self):
        state = self.client_for(self.viewer).get(self.state_url()).json()

        self.assertEqual(state["write"], {"allowed": False, "reason": "", "show_authorize": False})
        self.assertNotIn('id="fleet-refresh-form"', state["status_html"])
        self.assertNotIn("<form", state["tree_html"])
        self.assertIn("Line Pilot", state["members_html"])
        # The Fleet Control card is for the fleet's managers only.
        self.assertIsNone(state["controls_html"])


class StatePayloadTests(FleetStateTestCase):
    def test_full_and_unchanged_answers(self):
        fc = self.client_for(self.fc)

        response = fc.get(self.state_url())
        self.assertIn("no-store", response["Cache-Control"])
        state = response.json()
        self.assertEqual(set(state), STATE_KEYS)
        self.assertFalse(state["unchanged"])
        self.assertEqual((state["status"], state["member_count"], state["max_members"]), ("active", 2, 256))
        self.assertTrue(state["write"]["allowed"])
        self.assertTrue(state["server_time"].endswith("Z"))
        self.assertEqual(state["structure_read_at"][-1], "Z")
        self.assertIn('data-node="wing"', state["tree_html"])
        self.assertIn("Line Pilot", state["tree_html"])
        self.assertIn("id=\"fleet-eve-clock\"", state["status_html"])

        short = fc.get(self.state_url(), {"v": state["version"]}).json()
        self.assertEqual(
            set(short), {"unchanged", "version", "server_time", "status", "last_esi_update", "structure_read_at", "stale", "job"}
        )
        self.assertTrue(short["unchanged"])
        self.assertEqual(short["version"], state["version"])

        self.fleet.update_member(self.member_char.character_id, ship_type_id=670)
        self.sync()
        changed = fc.get(self.state_url(), {"v": state["version"]}).json()
        self.assertFalse(changed["unchanged"])
        self.assertNotEqual(changed["version"], state["version"])

    def test_the_members_and_pap_preview_follow_the_fleet(self):
        """The live payload refreshes the page's member list and PAP preview too, VIS(2) included."""
        state = self.client_for(self.fc).get(self.state_url()).json()

        members = state["members_html"]
        self.assertIn("Line Pilot", members)
        # #33a: wing and squad names, the fleet command position and docked members.
        self.assertIn("<td>Main</td><td>Alpha</td>", members)
        self.assertIn("<td>Fleet Command</td><td>—</td>", members)
        self.assertIn('title="Docked"', members)
        self.assertIn("<td>Squad Member</td>", members)
        self.assertIn("kick-member-form", members)
        self.assertIn("PAP preview", state["pap_html"])
        self.assertIn("Line Pilot", state["pap_html"])

    def test_page_parts_keep_vis2_for_viewers_who_may_not_see_the_members(self):
        from django.test import RequestFactory

        from fleetops.views_fleet_state import page_fragments

        f.add_attendance(self.operation, self.fc, source="manual")
        f.add_attendance(self.operation, self.member, source="manual")
        request = RequestFactory().get("/")
        request.user = self.member

        parts = page_fragments(request, self.operation)

        self.assertIn('id="composition-hidden"', parts["members_html"])
        self.assertNotIn("State Boss", parts["members_html"])
        self.assertIn("While the fleet is open you see the PAP of your own characters.", parts["pap_html"])
        self.assertIn("Line Pilot", parts["pap_html"])
        self.assertNotIn("State Boss", parts["pap_html"])

    def test_a_read_that_changes_nothing_shown_keeps_the_version(self):
        minute = timezone.now().replace(second=5, microsecond=0)
        states = FleetMemberState.objects.filter(operation=self.operation)
        states.update(accumulated_seconds=600, last_seen=minute)
        fc = self.client_for(self.fc)
        version = fc.get(self.state_url()).json()["version"]

        # A later read in the same minute: some seconds more in fleet; the page shows minutes.
        states.update(accumulated_seconds=620, last_seen=minute + timedelta(seconds=20))
        FleetOperation.objects.filter(pk=self.operation.pk).update(updated_at=timezone.now() + timedelta(seconds=20))
        self.assertTrue(fc.get(self.state_url(), {"v": version}).json()["unchanged"])

        # The next minute in fleet shows in the tree and the member list.
        states.update(accumulated_seconds=661)
        self.assertFalse(fc.get(self.state_url(), {"v": version}).json()["unchanged"])

    def test_answers_are_compressed_for_browsers_that_accept_them(self):
        fc = self.client_for(self.fc)

        for response in (
            fc.get(self.state_url(), HTTP_ACCEPT_ENCODING="gzip, deflate, br"),
            fc.post(self.refresh_url(), HTTP_ACCEPT_ENCODING="gzip", **JSON),
        ):
            self.assertEqual(response["Content-Encoding"], "gzip")
            self.assertIn("Accept-Encoding", response["Vary"])
            self.assertIn("no-store", response["Cache-Control"])
            body = json.loads(gzip.decompress(response.content))
            self.assertIn("tree_html", body.get("state") or body)
        self.assertNotIn("Content-Encoding", fc.get(self.state_url()))

    def test_header_numbers(self):
        state = self.client_for(self.fc).get(self.state_url()).json()

        self.assertNotEqual(state["stat_last_esi"], "—")
        self.assertTrue(state["last_esi_update"].endswith("Z"))

    def test_a_job_and_the_stale_flag_are_in_every_answer(self):
        from fleetops.services import fleet_jobs

        f.settings(stale_threshold=60)
        fc = self.client_for(self.fc)
        version = fc.get(self.state_url()).json()["version"]
        fleet_jobs.claim_job(self.operation, kind="move", label="Move 3 members", actor=self.fc, total=3)

        state = fc.get(self.state_url(), {"v": version}).json()

        self.assertFalse(state["unchanged"])
        self.assertTrue(state["stale"])
        self.assertEqual(state["job"]["label"], "Move 3 members")
        self.assertIn('id="fleet-job" data-job-state="queued"', state["status_html"])
        self.assertIn("Wait until “Move 3 members” has finished.", state["status_html"])
        self.assertFalse(state["write"]["allowed"])
        short = fc.get(self.state_url(), {"v": state["version"]}).json()
        self.assertTrue(short["unchanged"])
        self.assertEqual(short["job"]["label"], "Move 3 members")


class ControlCardTests(FleetStateTestCase):
    """The Fleet Control card above the tabs follows the fleet like the tabs do."""

    def detail(self, client):
        return client.get(reverse("fleetops:operation_detail", args=[self.operation.uuid])).content.decode()

    def test_the_capsule_button_follows_the_fleet(self):
        fc = self.client_for(self.fc)
        page = self.detail(fc)
        self.assertIn('<div id="fleet-control-fragment">', page)
        self.assertIn("disabled>Kick 0 Capsules</button>", page)
        version = fc.get(self.state_url()).json()["version"]

        # The pilot dies and tracking reads the capsule.
        self.fleet.update_member(self.member_char.character_id, ship_type_id=670)
        self.sync()
        state = fc.get(self.state_url(), {"v": version}).json()

        self.assertFalse(state["unchanged"])
        self.assertIn(">Kick 1 Capsule</button>", state["controls_html"])
        self.assertNotIn("disabled>Kick", state["controls_html"])
        self.assertIn('data-kick-names="Line Pilot"', state["controls_html"])
        self.assertTrue(state["controls_html"].startswith('<div class="card mb-3 border-warning" id="fleet-control">'))

    def test_the_card_unblocks_when_a_job_has_finished(self):
        fc = self.client_for(self.fc)
        job = fleet_jobs.claim_job(self.operation, kind="kick", label="Kick 11 members outside Jita", actor=self.fc, total=11)
        self.assertIn('id="kick-blocked"', self.detail(fc))
        state = fc.get(self.state_url()).json()
        self.assertIn("Wait until “Kick 11 members outside Jita” has finished.", state["controls_html"])

        fleet_jobs._finish(job, fleet_jobs.DONE)
        state = fc.get(self.state_url(), {"v": state["version"]}).json()

        self.assertFalse(state["unchanged"])
        self.assertNotIn('id="kick-blocked"', state["controls_html"])
        self.assertNotIn('id="kick-outside-system" disabled', state["controls_html"])

    def test_the_card_shows_a_boss_handover(self):
        fc = self.client_for(self.fc)
        version = fc.get(self.state_url()).json()["version"]
        FleetOperation.objects.filter(pk=self.operation.pk).update(
            missing_reason=FleetOperation.EndReason.BOSS_HANDOVER, missing_detail="Line Pilot"
        )

        state = fc.get(self.state_url(), {"v": version}).json()

        reason = not_boss_message(self.operation, "Line Pilot")
        self.assertIn(f'id="kick-blocked">{reason} Nobody can be kicked now.</div>', state["controls_html"])
        self.assertIn(f'id="kick-outside-system" disabled title="{reason}"', state["controls_html"])

    def test_the_card_buttons_work_after_a_swap(self):
        page = self.detail(self.client_for(self.fc))

        # The kick list opens from listeners on the document, so replaced buttons keep working.
        self.assertIn("document.addEventListener('submit',event=>{", page)
        self.assertIn("event.target.closest('.kick-list-open')", page)
        self.assertNotIn("querySelectorAll('.kick-list-trigger')", page)


class LiveRefreshTests(FleetStateTestCase):
    """While a manager watches an active fleet, the state endpoint reads it from ESI every so often."""

    def poll(self, user, **params):
        return self.client_for(user).get(self.state_url(), {"live": "1", **params}).json()

    def test_a_manager_watching_gets_a_fresh_read(self):
        reads = self.member_reads()
        self.fleet.update_member(self.member_char.character_id, ship_type_id=670)

        state = self.poll(self.fc)

        self.assertEqual(self.member_reads(), reads + 1)
        self.assertEqual(FleetMemberState.objects.get(character_id=self.member_char.character_id).ship_type_id, 670)
        self.assertIn("types/670/icon", state["tree_html"])

    def test_one_read_per_interval_however_many_managers_watch(self):
        reads = self.member_reads()
        self.poll(self.fc)
        FleetOperation.objects.filter(pk=self.operation.pk).update(last_esi_update=timezone.now() - timedelta(minutes=2))

        self.poll(self.lead)
        self.poll(self.fc)

        self.assertEqual(self.member_reads(), reads + 1)

    def test_no_read_when_tracking_read_the_fleet_recently(self):
        FleetOperation.objects.filter(pk=self.operation.pk).update(last_esi_update=timezone.now() - timedelta(seconds=5))
        reads = self.member_reads()

        self.poll(self.fc)

        self.assertEqual(self.member_reads(), reads)

    def test_no_read_for_viewers_without_live_or_when_turned_off(self):
        reads = self.member_reads()

        self.poll(self.viewer)
        self.client_for(self.fc).get(self.state_url(), {"live": "0"})
        f.settings(fleet_live_refresh_seconds=0)
        self.poll(self.fc)

        self.assertEqual(self.member_reads(), reads)

    def test_the_read_is_queued_once_per_window(self):
        with mock.patch("fleetops.services.fleet_refresh.schedule_refresh", return_value=True) as schedule:
            self.poll(self.fc)
        schedule.assert_called_once_with(mock.ANY, delays=(0,), dedupe_seconds=15)
        self.assertEqual(schedule.call_args.args[0].pk, self.operation.pk)


class RefreshButtonTests(FleetStateTestCase):
    def test_refresh_reads_the_fleet_and_answers_with_the_new_state(self):
        self.fleet.add_wing("Tackle", ["Tackle"])
        reads = self.member_reads()

        response = self.client_for(self.fc).post(self.refresh_url(), **JSON)

        self.assertEqual(response.status_code, 200)
        self.assertIn("no-store", response["Cache-Control"])
        answer = response.json()
        self.assertEqual((answer["ok"], answer["message"], answer["level"]), (True, "Fleet refreshed from ESI.", "success"))
        self.assertIn("Tackle", answer["state"]["tree_html"])
        self.assertEqual(self.member_reads(), reads + 1)

        again = self.client_for(self.fc).post(self.refresh_url(), **JSON).json()
        self.assertEqual((again["ok"], again["message"], again["level"]), (True, "Refreshed a moment ago.", "info"))
        self.assertEqual(self.member_reads(), reads + 1)

    def test_a_retry_right_after_a_failed_refresh_is_not_a_success(self):
        self.sim.fail("GetFleetsFleetIdMembers", status=500)
        fc = self.client_for(self.fc)
        self.assertEqual(fc.post(self.refresh_url(), **JSON).status_code, 502)
        reads = self.member_reads()

        response = fc.post(self.refresh_url(), **JSON)

        # Nothing was read again, and the failure of a moment ago still stands.
        self.assertEqual(response.status_code, 429)
        answer = response.json()
        self.assertEqual(
            (answer["ok"], answer["level"], answer["message"]),
            (False, "warning", "The last refresh failed a moment ago; try again in a few seconds."),
        )
        self.assertEqual(self.member_reads(), reads)

    def test_form_posts_return_to_the_fleet_tab(self):
        response = self.client_for(self.lead).post(self.refresh_url(), **FORM)

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response["Location"].endswith("#fleet"))
        self.assertEqual([str(m) for m in get_messages(response.wsgi_request)], ["Fleet refreshed from ESI."])

    def test_a_disbanded_fleet(self):
        self.sim.disband(self.fleet.fleet_id)

        response = self.client_for(self.fc).post(self.refresh_url(), **JSON)

        self.assertEqual(response.status_code, 409)
        answer = response.json()
        self.assertEqual(answer["message"], FLEET_GONE)
        self.assertFalse(answer["state"]["write"]["allowed"])
        self.assertIn(FLEET_GONE, answer["state"]["status_html"])

    def test_esi_errors(self):
        self.sim.fail("GetFleetsFleetIdMembers", status=502)

        response = self.client_for(self.fc).post(self.refresh_url(), **JSON)

        self.assertEqual(response.status_code, 502)
        message = response.json()["message"]
        self.assertTrue(message.startswith("Could not read fleet members: ESI returned HTTP 502"), message)
        self.assertIn(message, response.json()["state"]["status_html"])

    def test_only_active_fleets_and_their_managers(self):
        self.assertEqual(self.client_for(self.viewer).post(self.refresh_url(), **JSON).status_code, 403)
        self.assertEqual(self.client_for(self.other_fc).post(self.refresh_url(), **JSON).status_code, 404)
        self.assertEqual(self.client_for(self.fc).get(self.refresh_url()).status_code, 405)
        FleetOperation.objects.filter(pk=self.operation.pk).update(status=Status.ENDING)
        response = self.client_for(self.fc).post(self.refresh_url(), **JSON)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["message"], "The fleet is not active any more.")


class FleetPageTests(FleetStateTestCase):
    def page(self, user):
        response = self.client_for(user).get(reverse("fleetops:operation_detail", args=[self.operation.uuid]))
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def test_the_page_updates_itself_instead_of_reloading(self):
        page = self.page(self.fc)

        self.assertNotIn("location.reload", page)
        self.assertIn('data-poll-seconds="10" data-live-default="1"', page)
        self.assertIn('id="fleet-live-toggle" checked', page)
        self.assertIn('data-fleet-toggle="fleet"', page)
        # The kick confirmation survives a member list swap: it listens on the document.
        self.assertIn("window.FleetOpsKickConfirm", page)
        self.assertNotIn("querySelectorAll('.kick-member-form')", page)

    def test_member_table_names_positions(self):
        page = self.page(self.fc)

        self.assertIn("<td>Main</td><td>Alpha</td>", page)
        self.assertIn("<td>Fleet Command</td><td>—</td>", page)
        self.assertIn('fa-house text-muted" title="Docked"', page)

    def test_live_toggle_is_off_when_the_setting_is_zero(self):
        f.settings(fleet_live_refresh_seconds=0)

        page = self.page(self.fc)

        self.assertIn('data-live-default="0"', page)
        self.assertIn('id="fleet-live-toggle" disabled', page)

    def test_members_of_an_open_fleet_see_no_fleet_tab(self):
        page = self.page(self.member)

        self.assertNotIn('id="fleet-root"', page)
        self.assertIn('id="composition-hidden"', page)

    def test_ending_fleets_keep_the_tab_without_polling(self):
        FleetOperation.objects.filter(pk=self.operation.pk).update(status=Status.ENDING)

        page = self.page(self.fc)

        self.assertIn('data-status="ending"', page)
        self.assertNotIn('id="fleet-refresh-form"', page)
