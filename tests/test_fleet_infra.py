# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Fleet management groundwork: who may change a fleet, expected changes, refreshes, jobs and the write lock."""

import time
from datetime import timedelta
from unittest import mock

from celery.exceptions import Retry
from django.contrib.auth.models import User
from django.contrib.messages import get_messages
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from kombu.exceptions import OperationalError as QueueError

from fleetops import tasks
from fleetops.models import (
    AuditLog,
    DiscordWebhook,
    FleetMemberState,
    FleetOperation,
    OperationAction,
    ShipFunction,
    ShipFunctionRule,
)
from fleetops.providers.esi import FleetESIError
from fleetops.services import fleet_jobs, fleet_overlay, fleet_refresh
from fleetops.services.audit import config_snapshot
from fleetops.services.fleet_access import (
    FLEET_GONE,
    NO_WRITE_TOKEN,
    NOT_ACTIVE,
    WRITE_BUSY,
    FleetManageError,
    WriteState,
    can_view_fleet_tab,
    can_view_live_fleet,
    fc_has_write_token,
    require_writable,
    write_state,
)
from fleetops.services.composition import load_classifier
from fleetops.services.fleet_controls import KickRefused, kick_member, kick_members
from fleetops.services.tracking import sync_operation
from fleetops.templatetags.fleetops_operation import hides_details

from . import factories as f
from .fleet_sim import FleetSim

Status = FleetOperation.Status
EndReason = FleetOperation.EndReason
JITA, AMAMAKE = 30000142, 30002537

# Runners for the job tests, found by import path like the real ones.
RUNS = []
SECRET_TEXT = "token=access-secret"


def recording_runner(job, operation, payload, actor):
    RUNS.append((job["id"], operation.pk, payload, actor))
    for number in range(payload.get("steps", 0)):
        fleet_jobs.step(job, ok=number not in payload.get("fail", ()), message=f"Step {number + 1}")
    job["audit"] = {"created": payload.get("created", [])}


def stopping_runner(job, operation, payload, actor):
    fleet_jobs.step(job, ok=True, message="Renamed wing 1")
    raise fleet_jobs.JobStopped("The FC character is not the fleet boss any more.")


def crashing_runner(job, operation, payload, actor):
    raise RuntimeError(SECRET_TEXT)


def checking_runner(job, operation, payload, actor):
    fleet_jobs.ensure_writable(job, operation, actor)
    fleet_jobs.step(job, ok=True, message="Checked")
    FleetOperation.objects.filter(pk=operation.pk).update(status=Status.ENDING)
    fleet_jobs.ensure_writable(job, operation, actor)
    fleet_jobs.step(job, ok=True, message="Never reached")


TEST_RUNNERS = {
    **fleet_jobs.RUNNERS,
    "move": f"{__name__}.recording_runner",
    "template": f"{__name__}.stopping_runner",
    "invite": f"{__name__}.crashing_runner",
    "check": f"{__name__}.checking_runner",
}


class FleetTestCase(TestCase):
    def setUp(self):
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)
        RUNS.clear()
        self.fc = f.create_user("infra-fc", perms=f.FC_PERMS, main_name="Infra FC")
        self.fc_char = f.main_of(self.fc)
        self.token = f.add_token(self.fc, self.fc_char)
        self.operation = f.create_operation(self.fc)
        self.lead = f.create_user("infra-lead", perms=f.FC_LEAD_PERMS)
        self.other_fc = f.create_user("infra-other", perms=f.FC_PERMS)
        self.member = f.create_user("infra-member", perms=f.MEMBER_PERMS)
        f.member_state(self.operation, f.main_of(self.member))

    def patch(self, target, **kwargs):
        patcher = mock.patch(target, **kwargs)
        mocked = patcher.start()
        self.addCleanup(patcher.stop)
        return mocked

    def reload(self):
        self.operation.refresh_from_db()
        return self.operation

    def operation_with(self, **values):
        """Another fleet of the FC with these values (the factory always gives started fleets an ESI fleet)."""
        operation = f.create_operation(self.fc)
        FleetOperation.objects.filter(pk=operation.pk).update(**values)
        operation.refresh_from_db()
        return operation


class AccessTests(FleetTestCase):
    def test_who_sees_the_fleet_tab_of_an_open_fleet(self):
        viewer = f.create_user("infra-viewer", perms=[*f.MEMBER_PERMS, "fleetops.view_all_fleets"])
        for status in (Status.ACTIVE, Status.ENDING):
            FleetOperation.objects.filter(pk=self.operation.pk).update(status=status)
            self.reload()
            for user, expected in ((self.fc, True), (self.lead, True), (viewer, True), (self.other_fc, True),
                                   (self.member, False)):
                with self.subTest(status=status, user=user.username):
                    self.assertEqual(can_view_fleet_tab(user, self.operation), expected)
                    self.assertEqual(can_view_live_fleet(user, self.operation), expected)

    def test_members_see_the_composition_after_the_close(self):
        FleetOperation.objects.filter(pk=self.operation.pk).update(status=Status.CLOSED)
        stranger = f.create_user("infra-stranger", perms=f.MEMBER_PERMS)

        self.assertTrue(can_view_fleet_tab(self.member, self.reload()))
        self.assertFalse(can_view_live_fleet(self.member, self.operation))
        self.assertFalse(can_view_fleet_tab(stranger, self.operation))

    def test_fleets_without_an_esi_fleet_have_no_tab(self):
        cases = [
            {"is_manual": True},
            {"link_only": True},
            {"esi_fleet_id": None},
            {"status": Status.SCHEDULED},
            {"status": Status.CANCELLED},
        ]
        for values in cases:
            operation = self.operation_with(**values)
            with self.subTest(values=values):
                self.assertFalse(can_view_fleet_tab(self.lead, operation))

    def test_the_write_token_check_does_not_refresh(self):
        self.assertTrue(fc_has_write_token(self.operation))
        self.token.scopes.filter(name__contains="write").delete()
        self.assertFalse(fc_has_write_token(self.operation))


class WriteStateTests(FleetTestCase):
    def test_managers_may_change_an_active_fleet(self):
        for user in (self.fc, self.lead):
            with self.subTest(user=user.username):
                self.assertEqual(write_state(self.operation, user), WriteState(True))

    def test_people_who_cannot_manage_get_no_reason(self):
        viewer = f.create_user("infra-viewer", perms=[*f.MEMBER_PERMS, "fleetops.view_all_fleets"])
        for user in (self.other_fc, self.member, viewer, None):
            with self.subTest(user=getattr(user, "username", None)):
                self.assertEqual(write_state(self.operation, user), WriteState(False, ""))
        with self.assertRaises(FleetManageError) as ctx:
            require_writable(self.operation, self.member)
        self.assertEqual(ctx.exception.status, 404)

    def test_only_active_esi_fleets(self):
        cases = [
            {"status": Status.ENDING},
            {"status": Status.CLOSED},
            {"status": Status.SCHEDULED},
            {"is_manual": True},
            {"link_only": True},
            {"esi_fleet_id": None},
        ]
        for values in cases:
            operation = self.operation_with(**values)
            with self.subTest(values=values):
                self.assertEqual(write_state(operation, self.fc), WriteState(False, NOT_ACTIVE))

    def test_boss_handover_seen_by_tracking(self):
        FleetOperation.objects.filter(pk=self.operation.pk).update(
            missing_reason=EndReason.BOSS_HANDOVER, missing_detail="Boss Alt", fleet_missing_count=1
        )

        state = write_state(self.reload(), self.fc)

        self.assertEqual(
            state.reason,
            "Infra FC is not the fleet boss any more (current boss: Boss Alt). Fleet changes are disabled.",
        )
        self.assertFalse(state.allowed)
        with self.assertRaises(FleetManageError) as ctx:
            require_writable(self.operation, self.fc)
        self.assertEqual((ctx.exception.status, ctx.exception.message), (409, state.reason))

    def test_boss_handover_seen_by_a_boss_check(self):
        boss = f.add_alt(self.fc, "Boss Alt")
        detection = mock.Mock(fleet_id=self.operation.esi_fleet_id, fleet_boss_id=boss.character_id)
        detection.is_boss.return_value = False
        self.patch("fleetops.services.fleet_refresh.detect_character_fleet", return_value=detection)

        problem = fleet_refresh.check_boss(self.operation)

        self.assertEqual((problem.reason, problem.detail, problem.boss_id), (EndReason.BOSS_HANDOVER, "Boss Alt", boss.character_id))
        self.assertIn("(current boss: Boss Alt)", write_state(self.operation, self.lead).reason)
        # Tracking alone counts unreadable polls towards the automatic end.
        self.assertEqual(self.reload().fleet_missing_count, 0)

    def test_a_handover_wins_over_a_gone_fleet(self):
        FleetOperation.objects.filter(pk=self.operation.pk).update(missing_reason=EndReason.FLEET_GONE)
        cache.set(f"fleetops:fleet:{self.operation.pk}:boss", [EndReason.BOSS_HANDOVER, "New Boss", 5], 60)

        self.assertIn("current boss: New Boss", write_state(self.reload(), self.fc).reason)

    def test_gone_fleets(self):
        FleetOperation.objects.filter(pk=self.operation.pk).update(missing_reason=EndReason.FLEET_GONE)
        self.assertEqual(write_state(self.reload(), self.fc), WriteState(False, FLEET_GONE))

        FleetOperation.objects.filter(pk=self.operation.pk).update(missing_reason="")
        self.patch(
            "fleetops.services.fleet_refresh.detect_character_fleet",
            side_effect=FleetESIError("NOT_IN_FLEET", "Character is currently not in a fleet."),
        )
        fleet_refresh.check_boss(self.reload())
        self.assertEqual(write_state(self.operation, self.fc), WriteState(False, FLEET_GONE))

    def test_missing_write_token(self):
        self.token.scopes.filter(name__contains="write").delete()

        self.assertEqual(write_state(self.operation, self.fc), WriteState(False, NO_WRITE_TOKEN, True))
        # Only the FC user can add a token for the FC character.
        self.assertEqual(write_state(self.operation, self.lead), WriteState(False, NO_WRITE_TOKEN, False))
        self.assertIn("esi-fleets.write_fleet.v1", NO_WRITE_TOKEN)

    def test_a_running_job_blocks_everyone_but_itself(self):
        job = fleet_jobs.claim_job(self.operation, kind="template", label="Apply template Standard", actor=self.fc, total=5)

        self.assertEqual(
            write_state(self.operation, self.lead),
            WriteState(False, "Wait until “Apply template Standard” has finished."),
        )
        self.assertEqual(write_state(self.operation, self.fc, job_id=job["id"]), WriteState(True))


class OverlayTests(FleetTestCase):
    def setUp(self):
        super().setUp()
        self.wings = f.wings((11, "Main", ((21, "Alpha"), (22, "Bravo"))), (12, "Logi", ((23, "Logi"),)))
        read_at = timezone.now() - timedelta(seconds=30)
        FleetOperation.objects.filter(pk=self.operation.pk).update(
            fleet_wings=self.wings, fleet_wings_read_at=read_at, last_esi_update=read_at
        )
        self.reload()
        self.pilot = FleetMemberState.objects.get(operation=self.operation)
        FleetMemberState.objects.filter(pk=self.pilot.pk).update(fleet_role="squad_member", wing_id=11, squad_id=21)

    def structure(self):
        return fleet_overlay.current_structure(self.reload())

    def test_snapshot_without_changes(self):
        wings, members = self.structure()

        self.assertEqual([wing["name"] for wing in wings], ["Main", "Logi"])
        self.assertFalse(any(wing["pending"] for wing in wings))
        member = members[self.pilot.character_id]
        self.assertEqual(
            (member["role"], member["wing_id"], member["squad_id"], member["pending"], member["state"]),
            ("squad_member", 11, 21, False, self.pilot),
        )

    def test_structure_changes_are_applied_with_pending_markers(self):
        op = self.operation.pk
        fleet_overlay.expect(op, {"kind": "wing_created", "wing_id": 13, "name": ""})
        fleet_overlay.expect(op, {"kind": "squad_created", "wing_id": 13, "squad_id": 24, "name": "Tackle"})
        fleet_overlay.expect(op, {"kind": "wing_renamed", "wing_id": 12, "name": "Logistics"})
        fleet_overlay.expect(op, {"kind": "squad_renamed", "squad_id": 22, "name": "Charlie"})
        fleet_overlay.expect(op, {"kind": "squad_deleted", "squad_id": 23})

        wings, _members = self.structure()

        self.assertEqual(
            [(wing["id"], wing["name"], wing["pending"]) for wing in wings],
            [(11, "Main", False), (12, "Logistics", True), (13, "Wing", True)],
        )
        self.assertEqual(
            [(squad["name"], squad["pending"]) for squad in wings[0]["squads"]], [("Alpha", False), ("Charlie", True)]
        )
        self.assertEqual(wings[1]["squads"], [])
        self.assertEqual(wings[2]["squads"], [{"id": 24, "name": "Tackle", "pending": True}])

        fleet_overlay.expect(op, {"kind": "wing_deleted", "wing_id": 13})
        self.assertEqual([wing["id"] for wing in self.structure()[0]], [11, 12])

    def test_changes_are_idempotent(self):
        change = {"kind": "wing_created", "wing_id": 13, "name": "Tackle"}
        fleet_overlay.expect(self.operation.pk, change)
        fleet_overlay.expect(self.operation.pk, change)
        fleet_overlay.expect(self.operation.pk, {"kind": "squad_deleted", "squad_id": 99})

        wings, _members = self.structure()

        self.assertEqual([wing["name"] for wing in wings], ["Main", "Logi", "Tackle"])
        self.assertEqual(sum(len(wing["squads"]) for wing in wings), 3)
        # The stored snapshot itself is never changed.
        self.assertEqual(self.reload().fleet_wings, self.wings)

    def test_member_moves(self):
        fleet_overlay.expect(
            self.operation.pk,
            {"kind": "member_moved", "character_id": self.pilot.character_id, "role": "wing_commander", "wing_id": 12},
        )
        fleet_overlay.expect(
            self.operation.pk, {"kind": "member_moved", "character_id": 1, "role": "squad_member", "wing_id": 11}
        )

        _wings, members = self.structure()

        member = members[self.pilot.character_id]
        self.assertEqual((member["role"], member["wing_id"], member["squad_id"], member["pending"]), ("wing_commander", 12, -1, True))
        self.assertNotIn(1, members)

    def test_a_late_enough_read_replaces_a_change(self):
        now = time.time()
        fleet_overlay.expect(self.operation.pk, {"kind": "wing_renamed", "wing_id": 11, "name": "Old", "at": now - 20})
        fleet_overlay.expect(self.operation.pk, {"kind": "wing_renamed", "wing_id": 12, "name": "New", "at": now - 3})
        read_at = timezone.now() - timedelta(seconds=10)
        FleetOperation.objects.filter(pk=self.operation.pk).update(fleet_wings_read_at=read_at, last_esi_update=read_at)

        wings, _members = self.structure()

        # Read 10 s after the first change: ESI's state counts. Read before the second: still expected.
        self.assertEqual([wing["name"] for wing in wings], ["Main", "New"])

    def test_member_changes_follow_the_member_read(self):
        fleet_overlay.expect(
            self.operation.pk,
            {"kind": "member_moved", "character_id": self.pilot.character_id, "role": "squad_member", "wing_id": 11,
             "squad_id": 22, "at": time.time() - 10},
        )
        FleetOperation.objects.filter(pk=self.operation.pk).update(fleet_wings_read_at=None)
        self.assertEqual(self.structure()[1][self.pilot.character_id]["squad_id"], 22)

        FleetOperation.objects.filter(pk=self.operation.pk).update(last_esi_update=timezone.now())
        self.assertEqual(self.structure()[1][self.pilot.character_id]["squad_id"], 21)

    def test_changes_expire(self):
        fleet_overlay.expect(self.operation.pk, {"kind": "wing_renamed", "wing_id": 11, "name": "Gone", "at": time.time() - 31})
        fleet_overlay.expect(self.operation.pk, {"kind": "invited", "character_id": 5, "name": "Alt", "at": time.time() - 60})
        FleetOperation.objects.filter(pk=self.operation.pk).update(fleet_wings_read_at=None)

        self.assertEqual([change["kind"] for change in fleet_overlay.expectations(self.operation.pk)], ["invited"])
        self.assertEqual(self.structure()[0][0]["name"], "Main")

    def test_unknown_kinds_are_refused(self):
        with self.assertRaises(ValueError):
            fleet_overlay.expect(self.operation.pk, {"kind": "fleet_renamed"})

    def test_members_given_as_dicts(self):
        _wings, members = fleet_overlay.current_structure(
            self.operation, members=[{"character_id": 7, "name": "Seven", "role": "fleet_commander"}]
        )

        self.assertEqual(
            members[7] | {"state": None},
            {"character_id": 7, "name": "Seven", "character_name": "Seven", "role": "fleet_commander",
             "fleet_role": "fleet_commander", "wing_id": -1, "squad_id": -1, "pending": False, "state": None},
        )

    def test_pending_invitations(self):
        op = self.operation.pk
        fleet_overlay.expect(op, {"kind": "invited", "character_id": 5, "name": "Alt One", "role": "squad_member"})
        fleet_overlay.expect(op, {"kind": "invited", "character_id": 6, "name": "Alt Two", "role": "squad_member",
                                  "wing_id": 11, "squad_id": 21})
        fleet_overlay.expect(op, {"kind": "invited", "character_id": 5, "name": "Alt One", "role": "squad_member"})
        fleet_overlay.expect(op, {"kind": "invited", "character_id": 7, "name": "", "at": time.time() - 121})

        invitations = fleet_overlay.pending_invitations(op, active_ids=[6])

        self.assertEqual([(item["character_id"], item["name"]) for item in invitations], [(5, "Alt One")])
        # Invitations change nothing in the structure.
        self.assertEqual(len(self.structure()[0]), 2)


class SimOverlayTests(TestCase):
    """A move against a fleet whose ESI reads lag behind: the overlay shows it until a late read."""

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.fc = f.create_user(perms=f.FC_PERMS)
        self.fc_char = f.main_of(self.fc)
        f.add_token(self.fc, self.fc_char)
        self.sim = FleetSim(stale_reads=1)
        self.sim.install(self)
        self.fleet = self.sim.create_fleet(self.fc_char.character_id, wings=(("Main", ("Alpha", "Bravo")),))
        self.pilot_id = f.main_of(f.create_user(main_name="Overlay Pilot")).character_id
        self.fleet.add_member(self.pilot_id)
        self.operation = f.create_operation(self.fc, esi_fleet_id=self.fleet.fleet_id)
        sync_operation(self.operation)

    def test_the_move_shows_until_esi_does(self):
        from fleetops.providers.esi import move_fleet_member

        wing_id, bravo = self.fleet.wing_id("Main"), self.fleet.squad_id("Bravo")
        move_fleet_member(self.fc, self.fc_char.character_id, self.fleet.fleet_id, self.pilot_id, "squad_member", wing_id, bravo)
        fleet_overlay.expect(
            self.operation.pk,
            {"kind": "member_moved", "character_id": self.pilot_id, "role": "squad_member", "wing_id": wing_id, "squad_id": bravo},
        )

        sync_operation(self.operation, fresh=True)  # ESI still answers with the old squad
        state = FleetMemberState.objects.get(operation=self.operation, character_id=self.pilot_id)
        self.assertEqual(state.squad_id, self.fleet.squad_id("Alpha"))
        member = fleet_overlay.current_structure(self.operation)[1][self.pilot_id]
        self.assertEqual((member["squad_id"], member["pending"]), (bravo, True))

        # Seven seconds later ESI shows the move itself.
        later = timezone.now() + timedelta(seconds=7)
        with mock.patch("fleetops.services.tracking.timezone.now", return_value=later):
            sync_operation(self.operation, fresh=True)
        self.operation.refresh_from_db()
        member = fleet_overlay.current_structure(self.operation)[1][self.pilot_id]
        self.assertEqual((member["squad_id"], member["pending"]), (bravo, False))


class RefreshTests(FleetTestCase):
    def setUp(self):
        super().setUp()
        self.sync = self.patch("fleetops.services.fleet_refresh.sync_operation", return_value=3)

    def test_refresh_reads_the_fleet_past_the_cache(self):
        cache.set(f"fleetops:fleet:{self.operation.pk}:read_error", "old error", 60)

        self.assertEqual(fleet_refresh.refresh_now(self.operation), (True, "Fleet refreshed from ESI."))

        self.sync.assert_called_once_with(self.operation, fresh=True)
        self.assertEqual(fleet_refresh.read_problem(self.operation.pk), {"boss": None, "read_error": ""})

    def test_one_refresh_per_interval(self):
        fleet_refresh.refresh_now(self.operation)

        self.assertEqual(fleet_refresh.refresh_now(self.operation), (False, "Refreshed a moment ago."))
        self.assertEqual(fleet_refresh.refresh_now(self.operation, min_interval=0)[0], True)
        self.assertEqual(self.sync.call_count, 2)

    def test_a_fleet_that_closed_meanwhile(self):
        self.sync.return_value = None
        self.assertEqual(fleet_refresh.refresh_now(self.operation), (False, "The fleet is no longer open."))

    def test_read_errors_are_kept_for_the_page(self):
        self.sync.side_effect = FleetESIError("ESI_ERROR", "Could not read fleet members: ESI returned HTTP 502 (Bad Gateway).")

        ok, message = fleet_refresh.refresh_now(self.operation)

        self.assertFalse(ok)
        self.assertEqual(fleet_refresh.read_problem(self.operation.pk)["read_error"], message)
        self.assertIn("HTTP 502", message)

    def test_unexpected_errors_keep_only_a_safe_text(self):
        self.sync.side_effect = RuntimeError("token=access-secret")

        with self.assertLogs("extensions", "ERROR"):
            ok, message = fleet_refresh.refresh_now(self.operation)

        self.assertEqual((ok, message), (False, "Fleet refresh failed unexpectedly (RuntimeError)."))

    def test_a_404_checks_the_boss(self):
        self.sync.side_effect = FleetESIError("FLEET_NOT_FOUND", "Could not read fleet members: ESI returned HTTP 404 (Not Found).")
        detect = self.patch(
            "fleetops.services.fleet_refresh.detect_character_fleet",
            side_effect=FleetESIError("NOT_IN_FLEET", "Character is currently not in a fleet."),
        )

        self.assertEqual(fleet_refresh.refresh_now(self.operation), (False, FLEET_GONE))
        detect.assert_called_once_with(self.fc, self.fc_char.character_id, fresh=True)
        self.assertEqual(fleet_refresh.read_problem(self.operation.pk), {"boss": ("fleet_gone", ""), "read_error": ""})
        self.assertEqual(self.reload().fleet_missing_count, 0)

        # A later successful read clears the problem.
        self.sync.side_effect = None
        fleet_refresh.refresh_now(self.operation, min_interval=0)
        self.assertEqual(fleet_refresh.read_problem(self.operation.pk)["boss"], None)

    def test_a_404_while_still_the_boss(self):
        self.sync.side_effect = FleetESIError("FLEET_NOT_FOUND", "Could not read fleet members: ESI returned HTTP 404 (Not Found).")
        cache.set(f"fleetops:fleet:{self.operation.pk}:boss", ["fleet_gone", "", None], 60)
        detection = mock.Mock(fleet_id=self.operation.esi_fleet_id, fleet_boss_id=self.fc_char.character_id)
        detection.is_boss.return_value = True
        self.patch("fleetops.services.fleet_refresh.detect_character_fleet", return_value=detection)

        ok, message = fleet_refresh.refresh_now(self.operation)

        self.assertEqual((ok, message), (False, "Could not read fleet members: ESI returned HTTP 404 (Not Found)."))
        self.assertEqual(fleet_refresh.read_problem(self.operation.pk), {"boss": None, "read_error": message})

    def test_the_fc_in_another_fleet_or_a_failed_check(self):
        detection = mock.Mock(fleet_id=1, fleet_boss_id=self.fc_char.character_id)
        self.patch("fleetops.services.fleet_refresh.detect_character_fleet", return_value=detection)
        self.assertEqual(tuple(fleet_refresh.check_boss(self.operation)), (EndReason.FLEET_GONE, ""))

        cache.clear()
        self.patch(
            "fleetops.services.fleet_refresh.detect_character_fleet",
            side_effect=FleetESIError("ESI_ERROR", "Could not detect fleet: ESI returned HTTP 502 (Bad Gateway)."),
        )
        self.assertIsNone(fleet_refresh.check_boss(self.operation))
        self.assertIsNone(fleet_refresh.cached_boss_problem(self.operation.pk))

    def test_schedule_refresh_queues_a_read_now_and_after_the_esi_cache(self):
        apply_async = self.patch("fleetops.tasks.refresh_fleet_structure.apply_async")
        self.patch("fleetops.services.fleet_refresh.time.time", return_value=1_000.0)

        self.assertTrue(fleet_refresh.schedule_refresh(self.operation))

        # Each read knows when it is due, so a read that waited for a busy worker can be skipped.
        self.assertEqual(
            apply_async.call_args_list,
            [
                mock.call((self.operation.pk,), {"not_before": 1_000.0}, countdown=0),
                mock.call((self.operation.pk,), {"not_before": 1_006.0}, countdown=6),
            ],
        )

    def test_live_refreshes_are_deduplicated(self):
        apply_async = self.patch("fleetops.tasks.refresh_fleet_structure.apply_async")

        self.assertTrue(fleet_refresh.schedule_refresh(self.operation, delays=(0,), dedupe_seconds=15))
        self.assertFalse(fleet_refresh.schedule_refresh(self.operation, delays=(0,), dedupe_seconds=15))

        apply_async.assert_called_once_with((self.operation.pk,), {"not_before": mock.ANY}, countdown=0)

    def test_reads_that_waited_for_a_busy_worker_run_once(self):
        queued = []
        self.patch(
            "fleetops.tasks.refresh_fleet_structure.apply_async",
            side_effect=lambda args, kwargs, countdown: queued.append((args, kwargs)),
        )
        # A live refresh every window while the worker is busy with other tasks.
        for _window in range(5):
            cache.delete(f"fleetops:fleet:{self.operation.pk}:refresh_queued")
            fleet_refresh.schedule_refresh(self.operation, delays=(0,), dedupe_seconds=15)

        def read(operation, fresh):
            FleetOperation.objects.filter(pk=operation.pk).update(last_esi_update=timezone.now())
            return 3

        self.sync.side_effect = read
        results = [tasks.refresh_fleet_structure(*args, **kwargs) for args, kwargs in queued]

        # The first read is newer than all of them were due; the others do not read the fleet again.
        self.assertEqual(results, ["refreshed", "fresh", "fresh", "fresh", "fresh"])
        self.sync.assert_called_once()

    def test_the_read_after_a_change_is_never_skipped_for_an_older_read(self):
        FleetOperation.objects.filter(pk=self.operation.pk).update(last_esi_update=timezone.now())
        self.sync.side_effect = None

        due = time.time() + 6
        self.assertEqual(tasks.refresh_fleet_structure(self.operation.pk, not_before=due), "refreshed")
        self.sync.assert_called_once()

    def test_without_a_queue_the_fleet_is_read_once_in_the_request(self):
        self.patch("fleetops.tasks.refresh_fleet_structure.apply_async", side_effect=QueueError("broker down"))

        with self.assertLogs("extensions", "ERROR"):
            fleet_refresh.schedule_refresh(self.operation)

        self.sync.assert_called_once_with(self.operation, fresh=True)

    def test_the_refresh_task(self):
        self.assertEqual(tasks.refresh_fleet_structure(self.operation.pk), "refreshed")
        self.sync.side_effect = FleetESIError("ESI_ERROR", "Could not read fleet members: ESI returned HTTP 502 (Bad Gateway).")
        self.assertEqual(tasks.refresh_fleet_structure(self.operation.pk), "failed")
        self.assertEqual(tasks.refresh_fleet_structure(999_999), "missing")

        cache.add(fleet_refresh.refresh_task_lock_key(self.operation.pk), "1", 30)
        self.assertEqual(tasks.refresh_fleet_structure(self.operation.pk), "already-running")
        cache.clear()
        FleetOperation.objects.filter(pk=self.operation.pk).update(status=Status.CLOSED)
        self.assertEqual(tasks.refresh_fleet_structure(self.operation.pk), "inactive")
        self.assertEqual(self.sync.call_count, 2)
        self.assertIsNone(cache.get(fleet_refresh.refresh_task_lock_key(self.operation.pk)))


@mock.patch.dict(fleet_jobs.RUNNERS, TEST_RUNNERS)
class JobTests(FleetTestCase):
    def setUp(self):
        super().setUp()
        self.schedule = self.patch("fleetops.services.fleet_refresh.schedule_refresh")

    def start(self, kind="move", payload=None, **kwargs):
        values = {"label": "Move 3 members", "actor": self.fc, "total": 3, **kwargs}
        return fleet_jobs.start_job(self.operation, kind=kind, payload=payload or {"steps": 3}, **values)

    def test_a_job_runs_in_the_worker_and_is_recorded(self):
        job = self.start(payload={"steps": 3, "fail": [1], "created": ["Logi"]})

        # The tests run Celery eagerly: the job is finished when start_job returns.
        self.assertEqual(RUNS, [(job["id"], self.operation.pk, {"steps": 3, "fail": [1], "created": ["Logi"]}, self.fc)])
        self.assertEqual((job["state"], job["done"], job["failed"]), ("done", 2, 1))
        self.assertEqual([message["text"] for message in job["messages"]], ["Step 1", "Step 2", "Step 3"])
        self.assertEqual(fleet_jobs.current_job(self.operation.pk)["id"], job["id"])
        self.assertIsNone(fleet_jobs.running_job(self.operation.pk))
        step = self.operation.actions.get(action="fleet_move")
        self.assertEqual((step.status, step.error_message), (OperationAction.Status.FAILED, "Move 3 members: 2 of 3 done, 1 failed."))
        entry = AuditLog.objects.get(action="fleet.move_job")
        self.assertEqual(entry.actor, self.fc)
        self.assertEqual(
            entry.new_value,
            {"job_id": job["id"], "label": "Move 3 members", "total": 3, "done": 2, "failed": 1, "result": "failed",
             "summary": "Move 3 members: 2 of 3 done, 1 failed.", "created": ["Logi"]},
        )
        self.schedule.assert_called_once_with(self.operation)

    def test_the_job_is_queued_with_its_payload(self):
        delay = self.patch("fleetops.tasks.run_fleet_job.delay")

        job = self.start(payload={"moves": [[1, "squad_member", 11, 21]]})

        delay.assert_called_once_with(self.operation.pk, job["id"], "move", {"moves": [[1, "squad_member", 11, 21]]}, self.fc.pk)
        self.assertEqual((job["state"], job["kind"], job["total"], job["actor_id"]), ("queued", "move", 3, self.fc.pk))
        self.assertEqual(fleet_jobs.running_job(self.operation.pk)["id"], job["id"])

    def test_one_job_at_a_time(self):
        self.patch("fleetops.tasks.run_fleet_job.delay")
        self.start()

        with self.assertRaises(FleetManageError) as ctx:
            self.start(kind="template", label="Apply template Standard")

        self.assertEqual((ctx.exception.status, ctx.exception.message), (409, "Wait until “Move 3 members” has finished."))

    def test_a_job_waits_for_a_single_change(self):
        with fleet_jobs.fleet_write_lock(self.operation):
            with self.assertRaises(FleetManageError) as ctx:
                self.start()
        self.assertEqual(ctx.exception.message, WRITE_BUSY)
        self.assertIsNone(fleet_jobs.running_job(self.operation.pk))
        self.start()
        self.assertEqual(len(RUNS), 1)

    def test_without_a_worker_queue(self):
        self.patch("fleetops.tasks.run_fleet_job.delay", side_effect=QueueError("broker down"))

        with self.assertLogs("extensions", "ERROR"), self.assertRaises(FleetManageError) as ctx:
            self.start()

        self.assertEqual((ctx.exception.status, ctx.exception.message), (503, "The background worker is not available. Try again later."))
        self.assertIsNone(fleet_jobs.current_job(self.operation.pk))
        with fleet_jobs.fleet_write_lock(self.operation):
            pass

    def test_a_stopped_job(self):
        job = self.start(kind="template", label="Apply template Standard")

        self.assertEqual((job["state"], job["done"]), ("failed", 1))
        self.assertEqual(job["messages"][-1], {"ok": False, "text": "The FC character is not the fleet boss any more."})
        step = self.operation.actions.get(action="fleet_template")
        self.assertEqual(
            step.error_message,
            "Apply template Standard: 1 of 3 done. Stopped: The FC character is not the fleet boss any more.",
        )
        self.assertEqual(AuditLog.objects.get(action="fleet.template_job").new_value["result"], "failed")

    def test_a_crashing_runner_never_shows_the_exception_text(self):
        with self.assertLogs("extensions", "ERROR") as logs:
            job = self.start(kind="invite", label="Invite 2 characters")

        self.assertEqual(job["messages"][-1]["text"], "Invite 2 characters failed unexpectedly (RuntimeError).")
        self.assertNotIn("access-secret", "\n".join(logs.output))
        self.assertNotIn("access-secret", str(AuditLog.objects.get(action="fleet.invite_job").new_value))
        self.assertIsNone(fleet_jobs.running_job(self.operation.pk))

    def test_runners_stop_when_the_fleet_can_no_longer_be_changed(self):
        job = self.start(kind="check", label="Check")

        self.assertEqual([message["text"] for message in job["messages"]], ["Checked", NOT_ACTIVE])
        self.assertEqual(job["state"], "failed")

    def test_stale_and_replaced_jobs_are_skipped(self):
        delay = self.patch("fleetops.tasks.run_fleet_job.delay")
        job = self.start()
        args = delay.call_args.args

        self.assertEqual(fleet_jobs.run_job(*args[:1], "other-id", *args[2:]), "stale")
        self.assertEqual(fleet_jobs.run_job(*args), "done")
        self.assertEqual(fleet_jobs.run_job(*args), "stale")
        self.assertEqual(RUNS[0][0], job["id"])
        self.assertEqual(len(RUNS), 1)

    def test_a_job_of_a_deleted_fleet(self):
        delay = self.patch("fleetops.tasks.run_fleet_job.delay")
        self.start()
        operation_id = self.operation.pk
        self.operation.delete()

        self.assertEqual(fleet_jobs.run_job(*delay.call_args.args), "missing")

        self.assertEqual(fleet_jobs.current_job(operation_id)["messages"][-1]["text"], "The fleet no longer exists.")
        self.assertIsNone(fleet_jobs.running_job(operation_id))
        self.assertEqual(RUNS, [])

    def test_a_job_without_heartbeat_counts_as_stopped(self):
        self.patch("fleetops.tasks.run_fleet_job.delay")
        job = self.start()
        job["state"] = "running"
        job["updated_at"] = time.time() - 121
        cache.set(f"fleetops:fleet:{self.operation.pk}:job", job, 60)

        current = fleet_jobs.current_job(self.operation.pk)

        self.assertEqual((current["state"], current["messages"][-1]["text"]), ("failed", "The job stopped unexpectedly."))
        self.assertIsNone(fleet_jobs.running_job(self.operation.pk))
        self.start(kind="template", label="Apply template Standard")

    def test_a_queued_job_the_worker_never_started(self):
        self.patch("fleetops.tasks.run_fleet_job.delay")
        job = self.start()
        job["updated_at"] = time.time() - 120
        cache.set(f"fleetops:fleet:{self.operation.pk}:job", job, 60)
        self.assertEqual(fleet_jobs.current_job(self.operation.pk)["state"], "queued")

        job["updated_at"] = time.time() - 301
        cache.set(f"fleetops:fleet:{self.operation.pk}:job", job, 60)
        current = fleet_jobs.current_job(self.operation.pk)
        self.assertEqual(current["messages"][-1]["text"], "The background worker did not start the job.")
        self.assertIsNone(fleet_jobs.running_job(self.operation.pk))

    def test_finished_jobs_show_for_five_minutes(self):
        job = self.start()
        job["finished_at"] = time.time() - 299
        cache.set(f"fleetops:fleet:{self.operation.pk}:job", job, 60)
        self.assertIsNotNone(fleet_jobs.current_job(self.operation.pk))

        job["finished_at"] = time.time() - 301
        cache.set(f"fleetops:fleet:{self.operation.pk}:job", job, 60)
        self.assertIsNone(fleet_jobs.current_job(self.operation.pk))

    def test_a_lock_without_its_job_record_still_holds_the_fleet(self):
        cache.add(f"fleetops:fleet:{self.operation.pk}:job_lock", "lost-job", 60)

        with self.assertRaises(FleetManageError) as ctx:
            self.start()
        self.assertEqual(ctx.exception.message, "Wait until “the running job” has finished.")

    def test_a_job_whose_record_was_lost_frees_the_fleet_when_its_task_runs(self):
        delay = self.patch("fleetops.tasks.run_fleet_job.delay")
        self.start()
        cache.delete(f"fleetops:fleet:{self.operation.pk}:job")
        self.assertIsNotNone(fleet_jobs.running_job(self.operation.pk))

        self.assertEqual(fleet_jobs.run_job(*delay.call_args.args), "stale")

        self.assertIsNone(fleet_jobs.running_job(self.operation.pk))
        self.assertEqual(RUNS, [])

    def test_messages_keep_the_last_twenty(self):
        job = self.start(payload={"steps": 25}, total=25)
        self.assertEqual(len(job["messages"]), 20)
        self.assertEqual(job["messages"][0]["text"], "Step 6")
        self.assertEqual(job["done"], 25)

    def test_unknown_kinds(self):
        with self.assertRaises(ValueError):
            fleet_jobs.claim_job(self.operation, kind="disband", label="Disband", actor=self.fc, total=1)

    def test_stop_reasons(self):
        from esi.exceptions import ESIErrorLimitException

        for code, stops in (
            ("FLEET_WRITE_FORBIDDEN", True),
            ("FLEET_WRITE_LIMITED", True),
            ("MISSING_WRITE_SCOPE", True),
            ("FLEET_WRITE_NOT_FOUND", False),
            ("FLEET_WRITE_REJECTED", False),
            ("FLEET_WRITE_ERROR", False),
        ):
            with self.subTest(code=code):
                self.assertEqual(bool(fleet_jobs.stop_reason(FleetESIError(code, "Text"))), stops)
        self.assertEqual(fleet_jobs.stop_reason(ESIErrorLimitException(reset=30)), "ESI error limit reached; try again in 30 seconds.")
        self.assertEqual(fleet_jobs.stop_reason(RuntimeError("x")), "")


class WriteLockTests(FleetTestCase):
    def test_one_change_at_a_time(self):
        with fleet_jobs.fleet_write_lock(self.operation):
            with self.assertRaises(FleetManageError) as ctx, fleet_jobs.fleet_write_lock(self.operation):
                pass
        self.assertEqual((ctx.exception.status, ctx.exception.message), (409, WRITE_BUSY))
        with fleet_jobs.fleet_write_lock(self.operation):
            pass

    def test_the_lock_is_released_after_an_error(self):
        with self.assertRaises(RuntimeError), fleet_jobs.fleet_write_lock(self.operation):
            raise RuntimeError("ESI down")
        self.assertIsNone(cache.get(f"fleetops:fleet:{self.operation.pk}:write"))

    def test_no_change_while_a_job_runs(self):
        fleet_jobs.claim_job(self.operation, kind="template", label="Apply template Standard", actor=self.fc, total=5)

        with self.assertRaises(FleetManageError) as ctx, fleet_jobs.fleet_write_lock(self.operation):
            pass

        self.assertEqual(ctx.exception.message, "Wait until “Apply template Standard” has finished.")
        self.assertIsNone(cache.get(f"fleetops:fleet:{self.operation.pk}:write"))


class KickLockTests(FleetTestCase):
    """Kicks share the write lock and the job runner with the other fleet changes."""

    def setUp(self):
        super().setUp()
        self.kick = self.patch("fleetops.services.fleet_controls.kick_fleet_member")
        self.sync = self.patch("fleetops.services.fleet_controls.sync_operation")
        self.schedule = self.patch("fleetops.services.fleet_refresh.schedule_refresh")
        self.pilot = f.main_of(self.member)
        FleetMemberState.objects.filter(operation=self.operation).update(solar_system_id=AMAMAKE)

    def far_pilots(self, count):
        ids = [2_119_600_000 + number for number in range(count)]
        for character_id in ids:
            f.member_state(self.operation, character_id, solar_system_id=AMAMAKE, name=f"Far {character_id}")
        return ids

    def test_a_single_kick_waits_for_a_running_job(self):
        fleet_jobs.claim_job(self.operation, kind="move", label="Move 3 members", actor=self.fc, total=3)

        with self.assertRaises(KickRefused) as ctx:
            kick_member(self.operation, self.pilot.character_id, actor=self.fc)

        self.assertEqual(str(ctx.exception), "Wait until “Move 3 members” has finished.")
        self.kick.assert_not_called()
        self.assertFalse(AuditLog.objects.filter(action="fleet.kick_member").exists())

    def test_a_single_kick_holds_the_write_lock(self):
        def kick(*args):
            # Meanwhile no other change can start.
            with self.assertRaises(FleetManageError):
                fleet_jobs.claim_job(self.operation, kind="move", label="Move", actor=self.fc, total=1)

        self.kick.side_effect = kick
        result = kick_member(self.operation, self.pilot.character_id, actor=self.fc)

        self.assertTrue(result.ok)
        self.assertIsNone(cache.get(f"fleetops:fleet:{self.operation.pk}:write"))

    def test_a_small_batch_waits_for_another_change(self):
        with fleet_jobs.fleet_write_lock(self.operation), self.assertRaises(KickRefused) as ctx:
            kick_members(self.operation, [self.pilot.character_id], actor=self.fc, purpose="outside_system",
                         context={"system_id": JITA, "system_name": "Jita"})

        self.assertEqual(str(ctx.exception), WRITE_BUSY)
        self.kick.assert_not_called()

    def test_a_big_batch_is_a_fleet_job(self):
        ids = self.far_pilots(11)

        result = kick_members(self.operation, ids, actor=self.fc, purpose="outside_system",
                              context={"system_id": JITA, "system_name": "Jita"})

        self.assertTrue(result["queued"])
        self.assertEqual([call.args[3] for call in self.kick.call_args_list], ids)
        job = fleet_jobs.current_job(self.operation.pk)
        self.assertEqual((job["kind"], job["label"], job["state"], job["done"], job["total"]),
                         ("kick", "Kick 11 members outside Jita", "done", 11, 11))
        self.assertEqual(job["messages"][-1]["text"], f"Kicked Far {ids[-1]} (unknown corporation).")
        # Kicks keep their own step and audit entry.
        self.assertTrue(self.operation.actions.filter(action="kick_outside_system").exists())
        self.assertFalse(self.operation.actions.filter(action="fleet_kick").exists())
        self.assertFalse(AuditLog.objects.filter(action="fleet.kick_job").exists())
        # The fleet is read at once and again after ESI's own cache, like after every fleet job.
        self.sync.assert_called_once()
        self.schedule.assert_called_once_with(self.operation, delays=(fleet_overlay.ESI_READ_LAG,))

    def queue_kick_job(self, ids):
        """Queue a kick job of ``ids`` without running it; returns the arguments of run_job."""
        delay = self.patch("fleetops.tasks.run_fleet_job.delay")
        result = kick_members(self.operation, ids, actor=self.fc, purpose="outside_system",
                              context={"system_id": JITA, "system_name": "Jita"})
        self.assertTrue(result["queued"])
        return delay.call_args.args

    def test_members_that_are_no_longer_targets_leave_the_job_total(self):
        ids = self.far_pilots(12)
        args = self.queue_kick_job(ids)
        # Three pilots warp into the kept system before the worker picks the job up.
        FleetMemberState.objects.filter(operation=self.operation, character_id__in=ids[:3]).update(
            solar_system_id=JITA
        )

        self.assertEqual(fleet_jobs.run_job(*args), "done")

        job = fleet_jobs.current_job(self.operation.pk)
        self.assertEqual((job["done"], job["failed"], job["total"]), (9, 0, 9))
        self.assertEqual(
            job["messages"][0], {"ok": True, "text": "3 skipped: no longer in the fleet or no longer a target."}
        )
        self.assertEqual([call.args[3] for call in self.kick.call_args_list], ids[3:])

    def test_a_job_for_a_fleet_that_ended_meanwhile_stops_with_the_reason(self):
        args = self.queue_kick_job(self.far_pilots(11))
        FleetOperation.objects.filter(pk=self.operation.pk).update(status=Status.ENDING)

        self.assertEqual(fleet_jobs.run_job(*args), "failed")

        self.kick.assert_not_called()
        job = fleet_jobs.current_job(self.operation.pk)
        self.assertEqual(job["messages"][-1], {"ok": False, "text": "Members can only be kicked from an active fleet."})
        entry = AuditLog.objects.get(action="fleet.kick_outside_system")
        self.assertEqual(entry.new_value["result"], "failed")
        self.assertEqual(
            entry.new_value["summary"], "Nobody was kicked. Members can only be kicked from an active fleet."
        )

    def test_a_big_batch_waits_for_a_running_job(self):
        ids = self.far_pilots(11)
        fleet_jobs.claim_job(self.operation, kind="template", label="Apply template Standard", actor=self.fc, total=5)

        with self.assertRaises(KickRefused) as ctx:
            kick_members(self.operation, ids, actor=self.fc, purpose="outside_system",
                         context={"system_id": JITA, "system_name": "Jita"})

        self.assertEqual(str(ctx.exception), "Wait until “Apply template Standard” has finished.")
        self.kick.assert_not_called()

    def test_failed_kicks_in_a_batch_job(self):
        ids = self.far_pilots(11)
        self.kick.side_effect = [None] * 10 + [FleetESIError("FLEET_WRITE_ERROR", "Could not kick fleet member: ESI returned HTTP 502 (Bad Gateway).")]

        kick_members(self.operation, ids, actor=self.fc, purpose="outside_system", context={"system_id": JITA, "system_name": "Jita"})

        job = fleet_jobs.current_job(self.operation.pk)
        self.assertEqual((job["done"], job["failed"]), (10, 1))
        self.assertEqual(
            job["messages"][-1],
            {"ok": False, "text": f"Far {ids[-1]}: Could not kick fleet member: ESI returned HTTP 502 (Bad Gateway)."},
        )

    def test_the_kick_page_shows_the_refusal(self):
        fleet_jobs.claim_job(self.operation, kind="move", label="Move 3 members", actor=self.fc, total=3)
        self.client.force_login(self.fc)

        response = self.client.post(reverse("fleetops:kick_member", args=[self.operation.uuid, self.pilot.character_id]))

        self.assertIn("Wait until “Move 3 members” has finished.", [str(m) for m in get_messages(response.wsgi_request)])

    def test_queued_batches_of_earlier_versions_retry_while_busy(self):
        fleet_jobs.claim_job(self.operation, kind="move", label="Move 3 members", actor=self.fc, total=3)

        with mock.patch.object(tasks.kick_members, "retry", side_effect=Retry()) as retry, self.assertRaises(Retry):
            tasks.kick_members(self.operation.pk, [self.pilot.character_id], self.fc.pk, "capsules")

        retry.assert_called_once_with(countdown=tasks.KICK_RETRY_SECONDS)


class JobStepVisibilityTests(TestCase):
    def test_job_steps_are_hidden_like_the_live_fleet(self):
        # VIS(2): job steps describe the running fleet, so members of an open fleet do not see their messages.
        steps = {f"fleet_{kind}" for kind in fleet_jobs.RUNNERS if kind not in fleet_jobs.SELF_RECORDING_KINDS}
        self.assertEqual(steps, {"fleet_template", "fleet_move", "fleet_invite"})
        for action in steps:
            with self.subTest(action=action):
                self.assertTrue(hides_details(OperationAction(action=action), False))
                self.assertFalse(hides_details(OperationAction(action=action), True))


class ConfigSnapshotTests(TestCase):
    def test_snapshot_values(self):
        role = f.ping_role("Sig B")
        other = f.ping_role("Sig A")
        channel = f.ping_channel("Ops", roles=[role, other])

        data = config_snapshot(channel)

        self.assertEqual(data["webhook_url"], "***redacted***")
        self.assertEqual(data["allowed_roles"], sorted([role.pk, other.pk]))
        rule = ShipFunctionRule.objects.get(kind="group", target_id=832)
        self.assertEqual(config_snapshot(rule)["function"], rule.function_id)
        self.assertNotIn("discord.com", str(data))

    def test_an_empty_webhook_url_stays_empty(self):
        self.assertEqual(config_snapshot(DiscordWebhook(name="Unsaved", webhook_url=""))["webhook_url"], "")


class AdminAuditTests(TestCase):
    def setUp(self):
        self.superuser = User.objects.create_superuser("infra-root", "root@example.com", "password")
        self.client.force_login(self.superuser)
        self.function = ShipFunction.objects.get(name="Logistics")

    def form_data(self, rules):
        data = {
            "name": "Logistics",
            "color": "#198754",
            "fleet_roles": "",
            "sort_order": 30,
            "is_active": "on",
            "rules-TOTAL_FORMS": len(rules),
            "rules-INITIAL_FORMS": sum(1 for rule in rules if rule.get("id")),
            "rules-MIN_NUM_FORMS": 0,
            "rules-MAX_NUM_FORMS": 1000,
        }
        for number, rule in enumerate(rules):
            for key, value in rule.items():
                data[f"rules-{number}-{key}"] = value
            data[f"rules-{number}-function"] = self.function.pk
        return data

    def test_inline_rules_are_audited_one_by_one(self):
        logi, frigate, fax = (self.function.rules.get(target_id=target) for target in (832, 1527, 1538))
        rules = [
            {"id": logi.pk, "kind": "group", "target_id": 832, "label": "Logistics Cruiser"},
            {"id": frigate.pk, "kind": "group", "target_id": 1527, "label": "Logistics Frigate", "DELETE": "on"},
            {"id": fax.pk, "kind": "group", "target_id": 1538, "label": "Force Auxiliary"},
            {"kind": "type", "target_id": 11987, "label": "Guardian"},
        ]

        response = self.client.post(reverse("admin:fleetops_shipfunction_change", args=[self.function.pk]), self.form_data(rules))

        self.assertEqual(response.status_code, 302)
        entries = {(entry.action, entry.object_type, entry.object_id): entry for entry in AuditLog.objects.all()}
        new_rule = ShipFunctionRule.objects.get(kind="type", target_id=11987)
        updated = entries[("configuration.update", "ShipFunctionRule", str(logi.pk))]
        self.assertEqual((updated.old_value["label"], updated.new_value["label"]), ("Logistics", "Logistics Cruiser"))
        deleted = entries[("configuration.delete", "ShipFunctionRule", str(frigate.pk))]
        self.assertEqual((deleted.old_value["target_id"], deleted.new_value), (1527, None))
        created = entries[("configuration.create", "ShipFunctionRule", str(new_rule.pk))]
        self.assertEqual((created.old_value, created.new_value["label"]), (None, "Guardian"))
        self.assertNotIn(("configuration.update", "ShipFunctionRule", str(fax.pk)), entries)
        self.assertIn(("configuration.update", "ShipFunction", str(self.function.pk)), entries)
        self.assertTrue(all(entry.actor == self.superuser for entry in entries.values()))

    def test_positions_typed_in_another_case_are_stored_as_the_classifier_reads_them(self):
        data = self.form_data([])
        data["fleet_roles"] = "Wing_Commander, FLEET_COMMANDER"

        response = self.client.post(reverse("admin:fleetops_shipfunction_change", args=[self.function.pk]), data)

        self.assertEqual(response.status_code, 302)
        self.function.refresh_from_db()
        self.assertEqual(self.function.fleet_roles, "fleet_commander,wing_commander")
        self.assertEqual(load_classifier().resolve(role="wing_commander", ship_type_id=587).pk, self.function.pk)
        entry = AuditLog.objects.get(action="configuration.update", object_type="ShipFunction")
        self.assertEqual(entry.new_value["fleet_roles"], "fleet_commander,wing_commander")

    def test_deleting_a_function_audits_its_rules(self):
        rules = {str(rule.pk): (rule.kind, rule.target_id, rule.label) for rule in self.function.rules.all()}
        self.assertTrue(rules)

        response = self.client.post(reverse("admin:fleetops_shipfunction_delete", args=[self.function.pk]), {"post": "yes"})

        self.assertEqual(response.status_code, 302)
        entries = AuditLog.objects.filter(action="configuration.delete", object_type="ShipFunctionRule")
        self.assertEqual(
            {entry.object_id: (entry.old_value["kind"], entry.old_value["target_id"], entry.old_value["label"]) for entry in entries},
            rules,
        )
        self.assertTrue(all(entry.actor == self.superuser and entry.new_value is None for entry in entries))
        self.assertTrue(
            AuditLog.objects.filter(action="configuration.delete", object_type="ShipFunction", object_id=str(self.function.pk)).exists()
        )

    def test_a_new_default_function_audits_the_old_one(self):
        data = self.form_data([])
        data["is_default"] = "on"
        dps = ShipFunction.objects.get(name="DPS")

        self.client.post(reverse("admin:fleetops_shipfunction_change", args=[self.function.pk]), data)

        demoted = AuditLog.objects.get(action="configuration.update", object_type="ShipFunction", object_id=str(dps.pk))
        self.assertEqual((demoted.old_value["is_default"], demoted.new_value["is_default"]), (True, False))

    def test_structure_templates_are_validated_and_audited(self):
        url = reverse("admin:fleetops_fleetstructuretemplate_add")
        data = {"name": "Roam", "layout": "Main: Alpha, Logistics 1", "description": "", "sort_order": 20, "is_active": "on"}

        response = self.client.post(url, data)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "is longer than 10 characters")

        data["layout"] = "Main: Alpha, Logi"
        self.assertEqual(self.client.post(url, data).status_code, 302)
        entry = AuditLog.objects.get(action="configuration.create", object_type="FleetStructureTemplate")
        self.assertEqual(entry.new_value["layout"], "Main: Alpha, Logi")
