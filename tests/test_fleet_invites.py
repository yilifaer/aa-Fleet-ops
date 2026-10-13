"""Inviting Alliance Auth characters into a running fleet: candidates, single invites and invite jobs."""

import time
from datetime import timedelta
from unittest import mock

from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from fleetops.models import AuditLog, FleetMemberState, FleetOperation, OperationAction
from fleetops.services import fleet_jobs, fleet_overlay
from fleetops.services.fleet_access import NO_WRITE_TOKEN, WRITE_BUSY, FleetManageError
from fleetops.services.fleet_invites import (
    MAX_INVITES_PER_JOB,
    invite_candidates,
    invite_characters,
    run_bulk_invite,
)
from fleetops.services.fleet_rules import normalize_wings
from fleetops.services.tracking import sync_operation

from . import factories as f
from .fleet_sim import FleetSim

JSON = {"HTTP_ACCEPT": "application/json"}
INVITE = "PostFleetsFleetIdMembers"


class SimFleetTestCase(TestCase):
    """An active fleet of an FC with a write token, served by the fleet simulator."""

    fleet_wings = (("Main", ("Alpha", "Bravo")),)
    sim_options = {}

    def setUp(self):
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)
        self.fc = f.create_user(perms=f.FC_PERMS, main_name="Sim FC")
        self.fc_char = f.main_of(self.fc)
        self.token = f.add_token(self.fc, self.fc_char)
        self.sim = FleetSim(**self.sim_options)
        self.sim.install(self)
        self.fleet = self.sim.create_fleet(self.fc_char.character_id, wings=self.fleet_wings)
        self.operation = f.create_operation(self.fc, esi_fleet_id=self.fleet.fleet_id)
        self.schedule_refresh = self.patch("fleetops.services.fleet_refresh.schedule_refresh")
        self.sync()

    def patch(self, target, **kwargs):
        patcher = mock.patch(target, **kwargs)
        mocked = patcher.start()
        self.addCleanup(patcher.stop)
        return mocked

    def sync(self):
        """Read members from the simulator and store its wings, as tracking does."""
        sync_operation(self.operation, fresh=True)
        FleetOperation.objects.filter(pk=self.operation.pk).update(
            fleet_wings=normalize_wings(self.fleet.structure()), fleet_wings_read_at=timezone.now()
        )
        self.operation.refresh_from_db()
        return self.operation

    def pilot(self, name, *, perms=None, active=True):
        user = f.create_user(main_name=name, perms=perms)
        if not active:
            user.is_active = False
            user.save(update_fields=["is_active"])
        return user, f.main_of(user)

    def join(self, character, **position):
        self.fleet.add_member(character.character_id, **position)
        return self.sync()

    def invites(self):
        return self.sim.requests_of(INVITE)


class CandidateTests(SimFleetTestCase):
    def setUp(self):
        super().setUp()
        self.manager, self.manager_main = self.pilot("Zed Manager", perms=f.FC_PERMS)
        self.alt = f.add_alt(self.manager, "Anna Alt")
        # Q2: any character an active Alliance Auth user registered, FleetOps access or not.
        self.plain, self.plain_main = self.pilot("Rifter Plain")
        self.inactive, self.inactive_main = self.pilot("Rifter Gone", active=False)
        self.orphan = f.create_character("Rifter Orphan")

    def search(self, query, **kwargs):
        return invite_candidates(query, actor=self.manager, operation=self.operation, **kwargs)

    def test_characters_of_active_users_are_found(self):
        found = self.search("rifter")

        self.assertEqual([candidate["name"] for candidate in found], ["Rifter Plain"])
        self.assertEqual(
            found[0],
            {
                "character_id": self.plain_main.character_id,
                "name": "Rifter Plain",
                "corporation": self.plain_main.corporation_name,
                "alliance": self.plain_main.alliance_name,
                "main_name": "Rifter Plain",
                "in_fleet": False,
                "invited": False,
                "is_mine": False,
            },
        )

    def test_searches_need_three_letters(self):
        self.assertEqual(self.search("ri"), [])
        self.assertEqual(self.search(" r  "), [])
        self.assertEqual([candidate["name"] for candidate in self.search("rif")], ["Rifter Plain"])

    def test_an_empty_search_lists_my_characters(self):
        mine = self.search("  ")

        self.assertEqual([candidate["name"] for candidate in mine], ["Anna Alt", "Zed Manager"])
        self.assertTrue(all(candidate["is_mine"] for candidate in mine))
        self.assertEqual({candidate["main_name"] for candidate in mine}, {"Zed Manager"})

    def test_members_and_pending_invitations_are_flagged(self):
        self.join(self.plain_main)
        alt_id = self.alt.character_id
        fleet_overlay.expect(
            self.operation.pk,
            {"kind": "invited", "character_id": alt_id, "name": "Anna Alt", "role": "squad_member",
             "wing_id": None, "squad_id": None},
        )

        flags = {candidate["name"]: (candidate["in_fleet"], candidate["invited"]) for candidate in self.search("")}
        self.assertEqual(flags, {"Anna Alt": (False, True), "Zed Manager": (False, False)})
        self.assertEqual(self.search("rifter")[0]["in_fleet"], True)

    def test_results_are_limited_and_sorted(self):
        for number in (3, 1, 2):
            self.pilot(f"Rifter Clone {number}")

        found = self.search("Rifter", limit=2)

        self.assertEqual([candidate["name"] for candidate in found], ["Rifter Clone 1", "Rifter Clone 2"])

    def test_candidates_view(self):
        response = self.client_for(self.fc).get(
            reverse("fleetops:fleet_invite_candidates", args=[self.operation.uuid]), {"q": "rifter"}, **JSON
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual([candidate["name"] for candidate in response.json()["candidates"]], ["Rifter Plain"])
        self.assertIn("no-store", response["Cache-Control"])

    def client_for(self, user):
        self.client.force_login(user)
        return self.client


class InviteTests(SimFleetTestCase):
    def setUp(self):
        super().setUp()
        self.pilot_user, self.pilot_main = self.pilot("Invited Pilot")
        self.alpha = self.fleet.squad_id("Alpha")
        self.bravo = self.fleet.squad_id("Bravo")
        self.main_wing = self.fleet.wing_id("Main")

    def invite(self, *characters, **kwargs):
        ids = [getattr(character, "character_id", character) for character in characters]
        return invite_characters(self.operation, ids, actor=self.fc, **kwargs)

    def test_one_invite_into_a_squad(self):
        result = self.invite(self.pilot_main, wing_id=self.main_wing, squad_id=self.bravo)

        self.assertTrue(result.ok)
        self.assertEqual(result.message, "Invitation sent to Invited Pilot (Main / Bravo).")
        self.assertEqual(result.data, {"character_ids": [self.pilot_main.character_id]})
        [request] = self.invites()
        # Sent with the FC character's token.
        self.assertEqual(request.character_id, self.fc_char.character_id)
        self.assertEqual(
            request.body,
            {"character_id": self.pilot_main.character_id, "role": "squad_member", "wing_id": self.main_wing,
             "squad_id": self.bravo},
        )
        self.assertIn(self.pilot_main.character_id, self.fleet.invitations)
        entry = AuditLog.objects.get(action="fleet.invite")
        self.assertEqual(entry.actor, self.fc)
        self.assertEqual(
            entry.new_value,
            {"character_id": self.pilot_main.character_id, "name": "Invited Pilot", "role": "squad_member",
             "wing_id": self.main_wing, "squad_id": self.bravo, "wing": "Main", "squad": "Bravo", "result": "ok",
             "summary": result.message},
        )
        # Single invites need no fleet read: nothing changes until the pilot accepts.
        self.schedule_refresh.assert_not_called()

    def test_without_a_squad_the_pilot_joins_any_squad(self):
        result = self.invite(self.pilot_main)

        self.assertEqual(result.message, "Invitation sent to Invited Pilot (any squad with free positions).")
        self.assertEqual(self.invites()[0].body, {"character_id": self.pilot_main.character_id, "role": "squad_member"})
        details = AuditLog.objects.get(action="fleet.invite").new_value
        self.assertEqual((details["wing"], details["squad"], details["squad_id"]), ("", "", None))

    def test_a_squad_alone_takes_its_wing(self):
        self.invite(self.pilot_main, squad_id=self.alpha)

        self.assertEqual(self.invites()[0].body["wing_id"], self.main_wing)

    def test_a_squad_that_is_gone(self):
        with self.assertRaises(FleetManageError) as ctx:
            self.invite(self.pilot_main, squad_id=9_999_999)

        self.assertEqual(ctx.exception.status, 404)
        self.assertEqual(ctx.exception.message, "That squad no longer exists. The fleet view has been refreshed.")
        self.assertEqual(self.invites(), [])
        self.schedule_refresh.assert_called_once_with(self.operation)

    def test_pending_invitation_until_the_pilot_joins(self):
        self.invite(self.pilot_main)
        pilot_id = self.pilot_main.character_id

        [pending] = fleet_overlay.pending_invitations(self.operation.pk, [])
        self.assertEqual((pending["character_id"], pending["name"]), (pilot_id, "Invited Pilot"))

        self.sim.accept_invite(pilot_id)
        self.sync()
        active = self.operation.member_states.filter(is_active=True).values_list("character_id", flat=True)
        self.assertIn(pilot_id, active)
        self.assertEqual(fleet_overlay.pending_invitations(self.operation.pk, active), [])

    def test_pending_invitation_ends_after_two_minutes(self):
        self.invite(self.pilot_main)
        later = time.time() + fleet_overlay.INVITE_TTL + 1

        with mock.patch("fleetops.services.fleet_overlay.time.time", return_value=later):
            self.assertEqual(fleet_overlay.pending_invitations(self.operation.pk, []), [])

    def test_characters_outside_alliance_auth_are_refused(self):
        orphan = f.create_character("Unknown Pilot")
        _gone_user, gone = self.pilot("Gone Pilot", active=False)

        result = self.invite(orphan, gone, 91_234_567)

        self.assertFalse(result.ok)
        self.assertEqual(result.status, 400)
        self.assertEqual(
            result.message,
            "Nobody was invited. Skipped: Unknown Pilot is not registered in Alliance Auth by an active user; Gone "
            "Pilot is not registered in Alliance Auth by an active user; Character 91234567 is not registered in "
            "Alliance Auth by an active user.",
        )
        self.assertEqual(self.invites(), [])

    def test_members_are_skipped(self):
        self.join(self.pilot_main)
        _other_user, other = self.pilot("Second Pilot")

        only_member = self.invite(self.pilot_main)
        self.assertTrue(only_member.ok)
        self.assertEqual(only_member.level, "info")
        self.assertEqual(only_member.message, "Nobody was invited. Skipped: Invited Pilot is already in the fleet.")

        result = self.invite(self.pilot_main, other)
        self.assertEqual(
            result.message, "Invitation sent to Second Pilot (any squad with free positions). Skipped: Invited Pilot "
            "is already in the fleet."
        )
        self.assertEqual([request.body["character_id"] for request in self.invites()], [other.character_id])

    def test_full_fleet(self):
        now = timezone.now()
        FleetMemberState.objects.bulk_create(
            FleetMemberState(operation=self.operation, character_id=95_000_000 + number, character_name=f"Pilot {number}",
                             first_seen=now, last_seen=now, is_active=True)
            for number in range(254)
        )
        _second_user, second = self.pilot("Second Pilot")

        with self.assertRaises(FleetManageError) as ctx:
            self.invite(self.pilot_main, second)
        self.assertEqual((ctx.exception.status, ctx.exception.message),
                         (409, "Only 1 more character fits into the fleet (EVE allows 256 members)."))

        FleetMemberState.objects.create(operation=self.operation, character_id=96_000_000, first_seen=now,
                                        last_seen=now, is_active=True)
        with self.assertRaises(FleetManageError) as ctx:
            self.invite(self.pilot_main)
        self.assertEqual(ctx.exception.message, "The fleet is full: EVE allows 256 members.")
        self.assertEqual(self.invites(), [])

    def test_esi_rejections_are_shown_and_audited(self):
        # The pilot is in another fleet already.
        other_fleet = self.sim.create_fleet(f.create_character("Other Boss").character_id)
        other_fleet.add_member(self.pilot_main.character_id)

        result = self.invite(self.pilot_main)

        self.assertFalse(result.ok)
        self.assertEqual(result.status, 502)
        self.assertEqual(
            result.message, "Could not invite Invited Pilot: ESI returned HTTP 400 (Character is already in a fleet)."
        )
        details = AuditLog.objects.get(action="fleet.invite").new_value
        self.assertEqual((details["result"], details["error"]), ("failed", result.message))
        self.assertEqual(fleet_overlay.pending_invitations(self.operation.pk, []), [])

    def test_a_cspa_charge(self):
        self.sim.fail(INVITE, status=400, error="Character has a CSPA charge")

        result = self.invite(self.pilot_main)

        self.assertEqual(result.message, "Could not invite Invited Pilot: ESI returned HTTP 400 (Character has a CSPA charge).")
        self.assertNotIn("access-", result.message)

    def test_boss_handover_found_by_the_invite(self):
        self.join(self.pilot_main)
        self.fleet.set_boss(self.pilot_main.character_id)
        _other_user, other = self.pilot("Second Pilot")

        result = self.invite(other)

        self.assertEqual(result.status, 409)
        self.assertIn("is not the fleet boss any more (current boss: Invited Pilot)", result.message)
        with self.assertRaises(FleetManageError) as ctx:
            self.invite(other)
        self.assertIn("is not the fleet boss any more", ctx.exception.message)

    def test_who_may_invite_and_when(self):
        member, _main = self.pilot("Plain Member", perms=f.MEMBER_PERMS)
        with self.assertRaises(FleetManageError) as ctx:
            invite_characters(self.operation, [self.pilot_main.character_id], actor=member)
        self.assertEqual(ctx.exception.status, 404)

        with fleet_jobs.fleet_write_lock(self.operation):
            with self.assertRaises(FleetManageError) as ctx:
                self.invite(self.pilot_main)
        self.assertEqual((ctx.exception.status, ctx.exception.message), (409, WRITE_BUSY))

        self.token.scopes.clear()
        with self.assertRaises(FleetManageError) as ctx:
            self.invite(self.pilot_main)
        self.assertEqual((ctx.exception.status, ctx.exception.message), (409, NO_WRITE_TOKEN))
        self.assertEqual(self.invites(), [])

    def test_request_checks(self):
        for ids, kwargs, message in (
            ([], {}, "Choose the characters to invite."),
            (list(range(91_000_000, 91_000_000 + MAX_INVITES_PER_JOB + 1)), {}, "Invite at most 50 characters at a time."),
            ([1, 2], {"role": "squad_commander", "wing_id": self.main_wing, "squad_id": self.alpha},
             "Only one character can be invited into a commander position."),
            ([1], {"role": "wing_commander"}, "A wing commander needs a wing."),
        ):
            with self.subTest(message=message):
                with self.assertRaises(FleetManageError) as ctx:
                    invite_characters(self.operation, ids, actor=self.fc, **kwargs)
                self.assertEqual((ctx.exception.status, ctx.exception.message), (400, message))

    def test_commander_positions(self):
        self.invite(self.pilot_main, role="squad_commander", wing_id=self.main_wing, squad_id=self.alpha)
        self.assertEqual(self.invites()[0].body["role"], "squad_commander")

        with self.assertRaises(FleetManageError) as ctx:
            self.invite(self.pilot_main, role="fleet_commander")
        self.assertEqual(ctx.exception.status, 409)
        self.assertEqual(ctx.exception.message, "The fleet already has a fleet commander (Sim FC). Move them first.")


class InviteJobTests(SimFleetTestCase):
    def setUp(self):
        super().setUp()
        self.pilots = [self.pilot(f"Job Pilot {number}")[1] for number in range(1, 4)]
        self.alpha = self.fleet.squad_id("Alpha")

    def ids(self):
        return [pilot.character_id for pilot in self.pilots]

    def test_several_invites_run_as_a_job(self):
        result = invite_characters(self.operation, self.ids(), actor=self.fc, squad_id=self.alpha)

        self.assertTrue(result.ok)
        self.assertEqual(result.message, "Inviting 3 characters in the background.")
        self.assertEqual(result.data["character_ids"], self.ids())
        job = fleet_jobs.current_job(self.operation.pk)
        self.assertEqual(job["id"], result.data["job_id"])
        self.assertEqual((job["kind"], job["label"]), ("invite", "Invite 3 characters into Main / Alpha"))
        self.assertEqual((job["state"], job["done"], job["failed"], job["total"]), ("done", 3, 0, 3))
        self.assertEqual(
            [message["text"] for message in job["messages"]],
            [f"Invitation sent to Job Pilot {number}." for number in range(1, 4)],
        )
        self.assertEqual([request.body["character_id"] for request in self.invites()], self.ids())
        self.assertTrue(all(request.character_id == self.fc_char.character_id for request in self.invites()))
        pending = fleet_overlay.pending_invitations(self.operation.pk, [])
        self.assertEqual({invitation["character_id"] for invitation in pending}, set(self.ids()))
        entry = AuditLog.objects.get(action="fleet.invite_job")
        self.assertEqual(entry.new_value["ok"], 3)
        self.assertEqual(entry.new_value["position"]["squad"], "Alpha")
        self.assertEqual([invite["result"] for invite in entry.new_value["invites"]], ["ok", "ok", "ok"])
        step = self.operation.actions.get(action="fleet_invite")
        self.assertEqual(step.status, OperationAction.Status.SUCCESS)
        self.schedule_refresh.assert_called_once()

    def test_a_failed_invite_does_not_stop_the_job(self):
        self.sim.fail(INVITE, status=400, error="Character has a CSPA charge", character_id=self.ids()[1])

        invite_characters(self.operation, self.ids(), actor=self.fc)

        job = fleet_jobs.current_job(self.operation.pk)
        self.assertEqual((job["state"], job["done"], job["failed"]), ("done", 2, 1))
        self.assertEqual(
            job["messages"][1],
            {"ok": False, "text": "Could not invite Job Pilot 2: ESI returned HTTP 400 (Character has a CSPA charge)."},
        )
        details = AuditLog.objects.get(action="fleet.invite_job").new_value
        self.assertEqual(details["failed"][0]["character"], "Job Pilot 2")
        self.assertEqual(self.operation.actions.get(action="fleet_invite").status, OperationAction.Status.FAILED)

    def test_a_handover_stops_the_job(self):
        self.join(self.pilots[0])
        self.fleet.set_boss(self.pilots[0].character_id)

        invite_characters(self.operation, self.ids()[1:], actor=self.fc)

        job = fleet_jobs.current_job(self.operation.pk)
        self.assertEqual((job["state"], job["done"], job["failed"]), ("failed", 0, 1))
        self.assertIn("is not the fleet boss any more (current boss: Job Pilot 1)", job["messages"][-1]["text"])
        self.assertEqual(len(self.invites()), 1)

    def test_error_limit_stops_the_job(self):
        self.sim.fail(INVITE, status=420)

        invite_characters(self.operation, self.ids(), actor=self.fc)

        job = fleet_jobs.current_job(self.operation.pk)
        self.assertEqual((job["state"], job["failed"]), ("failed", 1))
        self.assertEqual(len(self.invites()), 1)

    def test_pilots_who_joined_meanwhile_are_skipped(self):
        delay = self.patch("fleetops.tasks.run_fleet_job.delay")
        result = invite_characters(self.operation, self.ids(), actor=self.fc)
        self.join(self.pilots[1])

        operation_id, job_id, kind, payload, actor_id = delay.call_args.args
        fleet_jobs.run_job(operation_id, job_id, kind, payload, actor_id)

        job = fleet_jobs.current_job(self.operation.pk)
        self.assertEqual(job["id"], result.data["job_id"])
        self.assertEqual((job["done"], job["failed"]), (3, 0))
        self.assertEqual(job["messages"][1]["text"], "Job Pilot 2 is already in the fleet.")
        self.assertEqual(len(self.invites()), 2)

    def test_the_job_stops_when_the_squad_is_gone(self):
        delay = self.patch("fleetops.tasks.run_fleet_job.delay")
        invite_characters(self.operation, self.ids(), actor=self.fc, squad_id=self.alpha)
        wings = [{**wing, "squads": [squad for squad in wing["squads"] if squad["id"] != self.alpha]}
                 for wing in self.operation.fleet_wings]
        FleetOperation.objects.filter(pk=self.operation.pk).update(fleet_wings=wings)

        fleet_jobs.run_job(*delay.call_args.args)

        job = fleet_jobs.current_job(self.operation.pk)
        self.assertEqual((job["state"], job["failed"]), ("failed", 1))
        self.assertEqual(job["messages"][0]["text"], "Job Pilot 1: That squad no longer exists.")
        self.assertEqual(self.invites(), [])

    def test_a_job_stops_when_the_fleet_ends(self):
        job = fleet_jobs.claim_job(self.operation, kind="invite", label="Invite 3 characters", actor=self.fc, total=3)
        FleetOperation.objects.filter(pk=self.operation.pk).update(status=FleetOperation.Status.ENDING)
        payload = {"invites": [[pilot.character_id, pilot.character_name] for pilot in self.pilots],
                   "role": "squad_member", "wing_id": None, "squad_id": None}

        with self.assertRaises(fleet_jobs.JobStopped):
            run_bulk_invite(job, self.operation, payload, self.fc)

        self.assertEqual(self.invites(), [])
        self.assertEqual((job["audit"]["invites"], job["audit"]["ok"]), ([], 0))


class InviteViewTests(SimFleetTestCase):
    def setUp(self):
        super().setUp()
        self.pilot_user, self.pilot_main = self.pilot("View Pilot")
        self.url = reverse("fleetops:fleet_invite", args=[self.operation.uuid])

    def test_invite_from_the_page(self):
        self.client.force_login(self.fc)
        squad = self.fleet.squad_id("Bravo")

        response = self.client.post(
            self.url,
            {"character_ids": [str(self.pilot_main.character_id)], "squad_id": str(squad),
             "wing_id": str(self.fleet.wing_id("Main"))},
            **JSON,
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual((body["ok"], body["message"]), (True, "Invitation sent to View Pilot (Main / Bravo)."))
        self.assertEqual(body["state"]["status"], "active")
        self.assertEqual(self.invites()[0].body["squad_id"], squad)

    def test_form_post_returns_to_the_fleet_tab(self):
        self.client.force_login(self.fc)

        response = self.client.post(self.url, {"character_ids": ["91234567"]})

        self.assertRedirects(
            response, f"{reverse('fleetops:operation_detail', args=[self.operation.uuid])}#fleet",
            fetch_redirect_response=False,
        )

    def test_the_page_has_the_invite_dialog(self):
        self.client.force_login(self.fc)

        response = self.client.get(reverse("fleetops:operation_detail", args=[self.operation.uuid]))

        for text in ('id="fleet-invite-modal"', 'id="fleet-invite-form"', 'id="fleet-invite-search"',
                     'id="fleet-invite-results"', 'id="fleet-invite-squad"', 'name="wing_id" id="fleet-invite-wing"',
                     'id="fleet-invite-submit" disabled', 'id="fleet-invite-open" data-needs-write'):
            with self.subTest(text=text):
                self.assertContains(response, text)

    def test_viewers_without_rights_get_no_invite_dialog(self):
        viewer = f.create_user(perms=[*f.MEMBER_PERMS, "fleetops.view_all_fleets"])
        self.client.force_login(viewer)

        response = self.client.get(reverse("fleetops:operation_detail", args=[self.operation.uuid]))

        self.assertContains(response, 'id="fleet-root"')
        self.assertNotContains(response, 'id="fleet-invite-modal"')
        self.assertNotContains(response, 'id="fleet-invite-open"')
        self.assertEqual(self.client.post(self.url, {"character_ids": ["1"]}, **JSON).status_code, 403)

    def test_invite_dialog_is_disabled_while_the_fleet_cannot_be_changed(self):
        self.token.scopes.clear()
        self.client.force_login(self.fc)

        response = self.client.get(reverse("fleetops:operation_detail", args=[self.operation.uuid]))

        self.assertContains(response, 'id="fleet-invite-open" data-needs-write disabled')

    def test_closed_fleets_take_no_invites(self):
        FleetOperation.objects.filter(pk=self.operation.pk).update(
            status=FleetOperation.Status.CLOSED, ended_at=timezone.now() - timedelta(minutes=1)
        )
        self.client.force_login(self.fc)

        response = self.client.post(self.url, {"character_ids": [str(self.pilot_main.character_id)]}, **JSON)

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["message"], "Fleet management is only available while the fleet is active.")
