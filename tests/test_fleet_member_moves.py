"""Member moves from the Fleet tab: positions, commander slots, bulk moves as a fleet job."""

from unittest import mock

from django.core.cache import cache
from kombu.exceptions import OperationalError as QueueError

from fleetops.models import AuditLog, FleetMemberState, FleetOperation, OperationAction
from fleetops.services import fleet_jobs, fleet_overlay
from fleetops.services import fleet_manage as fleet_manage_module
from fleetops.services.fleet_access import WRITE_BUSY, write_state
from fleetops.services.fleet_manage import (
    MAX_BULK_MOVES,
    ONE_COMMANDER,
    TOO_MANY_MOVES,
    MoveRequest,
    move_members,
)

from . import factories as f
from .test_fleet_structure_actions import BROWSER_FORM, FleetWriteTestCase

EndReason = FleetOperation.EndReason
MOVE = "PutFleetsFleetIdMembersMemberId"
NOT_BOSS = "Write FC is not the fleet boss any more (current boss: Boss Alt). Fleet changes are disabled."


class MoveTestCase(FleetWriteTestCase):
    def setUp(self):
        super().setUp()
        self.alpha_one = self.pilot("Alpha One", squad="Alpha")
        self.alpha_two = self.pilot("Alpha Two", squad="Alpha")
        self.bravo_one = self.pilot("Bravo One", squad="Bravo")
        self.read_fleet()
        self.main, self.logi = self.fleet.wing_id("Main"), self.fleet.wing_id("Logi")
        self.alpha, self.bravo = self.fleet.squad_id("Alpha"), self.fleet.squad_id("Bravo")

    def move(self, characters, role, wing_id=None, squad_id=None, *, user=None, headers=None):
        characters = characters if isinstance(characters, (list, tuple)) else [characters]
        data = {"character_ids": [str(getattr(c, "character_id", c)) for c in characters], "role": role}
        if wing_id:
            data["wing_id"] = str(wing_id)
        if squad_id:
            data["squad_id"] = str(squad_id)
        kwargs = {"headers": headers} if headers else {}
        return self.post("fleet_members_move", data, user=user, **kwargs)

    def moves_sent(self):
        return [(request.params["member_id"], request.body) for request in self.sim.requests_of(MOVE)]

    def answer(self, response):
        return response.status_code, response.json()["message"]

    def hand_over_boss(self):
        alt = f.add_alt(self.fc, "Boss Alt")
        self.fleet.add_member(alt.character_id)
        self.fleet.set_boss(alt.character_id)
        return alt


class SingleMoveTests(MoveTestCase):
    def test_each_position_sends_its_own_ids(self):
        cid = self.alpha_one.character_id
        self.fleet.move_member(self.fc_char.character_id, "squad_member", self.logi, self.fleet.squad_id("Logi"))
        self.read_fleet()
        cases = (
            (
                "squad_member", self.main, self.bravo,
                {"role": "squad_member", "wing_id": self.main, "squad_id": self.bravo},
                "Moved Alpha One to Main / Bravo.",
            ),
            (
                "squad_commander", self.main, self.alpha,
                {"role": "squad_commander", "wing_id": self.main, "squad_id": self.alpha},
                "Alpha One is now squad commander of Main / Alpha.",
            ),
            ("wing_commander", self.logi, None, {"role": "wing_commander", "wing_id": self.logi},
             "Alpha One is now wing commander of Logi."),
            ("fleet_commander", None, None, {"role": "fleet_commander"}, "Alpha One is now fleet commander."),
        )
        for role, wing_id, squad_id, body, message in cases:
            with self.subTest(role=role):
                response = self.move(self.alpha_one, role, wing_id, squad_id)
                self.assertEqual(self.answer(response), (200, message))
                self.assertEqual(self.moves_sent()[-1], (cid, body))
                self.assertEqual(self.sim.requests_of(MOVE)[-1].character_id, self.fc_char.character_id)
                self.assertEqual(self.fleet.position(cid)[0], role)
                member = self.structure()[1][cid]
                self.assertEqual((member["role"], member["pending"]), (role, True))
                self.read_fleet()

    def test_the_move_is_audited_with_ids_and_names(self):
        response = self.move(self.alpha_one, "squad_member", self.main, self.bravo, user=self.lead)

        self.assertEqual(response.json()["data"], {"character_ids": [self.alpha_one.character_id]})
        [entry] = AuditLog.objects.filter(action="fleet.member_move")
        self.assertEqual(entry.actor, self.lead)
        self.assertEqual(
            entry.old_value,
            {"character_id": self.alpha_one.character_id, "character_name": "Alpha One", "role": "squad_member",
             "wing_id": self.main, "squad_id": self.alpha, "wing": "Main", "squad": "Alpha"},
        )
        self.assertEqual(
            entry.new_value,
            {"role": "squad_member", "wing_id": self.main, "squad_id": self.bravo, "wing": "Main", "squad": "Bravo",
             "result": "ok", "summary": "Moved Alpha One to Main / Bravo."},
        )
        self.schedule.assert_called_once_with(self.operation)

    def test_commander_positions_hold_one_member(self):
        self.fleet.move_member(self.bravo_one.character_id, "squad_commander", self.main, self.bravo)
        lead = self.pilot("Logi Lead", role="wing_commander", wing="Logi")
        self.read_fleet()
        cases = (
            ("squad_commander", self.main, self.bravo, "Bravo already has a squad commander (Bravo One). Move them first."),
            ("wing_commander", self.logi, None, "Logi already has a wing commander (Logi Lead). Move them first."),
            ("fleet_commander", None, None, "The fleet already has a fleet commander (Write FC). Move them first."),
        )
        for role, wing_id, squad_id, message in cases:
            with self.subTest(role=role):
                self.assertEqual(self.answer(self.move(self.alpha_one, role, wing_id, squad_id)), (409, message))
        self.assert_nothing_sent()
        # The commander may be moved within the position's own rules.
        self.assertEqual(self.move(lead, "wing_commander", self.logi).json()["message"], "Logi Lead is already there.")

    def test_a_member_already_there(self):
        response = self.move(self.alpha_one, "squad_member", self.main, self.alpha)

        self.assertEqual(self.answer(response), (200, "Alpha One is already there."))
        self.assertEqual(response.json()["level"], "info")
        self.assert_nothing_sent()

    def test_a_member_who_left(self):
        self.fleet.remove_member(self.alpha_one.character_id)
        self.read_fleet()

        response = self.move(self.alpha_one, "squad_member", self.main, self.bravo)

        self.assertEqual(self.answer(response), (404, "Alpha One is no longer in the fleet."))
        self.schedule.assert_called_once_with(self.operation)
        self.assert_nothing_sent()

    def test_a_member_who_left_since_the_last_read(self):
        self.fleet.remove_member(self.alpha_one.character_id)

        response = self.move(self.alpha_one, "squad_member", self.main, self.bravo)

        text = "Could not move Alpha One: ESI returned HTTP 404 (Fleet member not found)."
        self.assertEqual(self.answer(response), (404, text))
        self.schedule.assert_called_once_with(self.operation)
        entry = AuditLog.objects.get(action="fleet.member_move")
        self.assertEqual((entry.new_value["result"], entry.new_value["error"]), ("failed", text))
        self.assertTrue(write_state(self.operation, self.fc).allowed)

    def test_role_and_ids_must_match(self):
        cases = (
            ("wing_commander", self.main, self.alpha, 400, "A wing commander has no squad."),
            ("squad_member", self.main, None, 400, "A squad member needs a wing and a squad."),
            ("squad_commander", self.logi, self.alpha, 404,
             "That squad is not part of the chosen wing. The fleet view has been refreshed."),
            ("squad_member", self.main, 4_000_000_009, 404,
             "That squad no longer exists. The fleet view has been refreshed."),
        )
        for role, wing_id, squad_id, status, message in cases:
            with self.subTest(role=role, squad_id=squad_id):
                self.assertEqual(self.answer(self.move(self.alpha_one, role, wing_id, squad_id)), (status, message))
        self.assert_nothing_sent()

    def test_the_boss_may_be_moved(self):
        response = self.move(self.fc_char, "squad_member", self.main, self.bravo)

        self.assertEqual(self.answer(response), (200, "Moved Write FC to Main / Bravo."))
        self.assertEqual(self.fleet.position(self.fc_char.character_id), ("squad_member", self.main, self.bravo))
        # Still the boss: only the position changed.
        self.assertTrue(write_state(self.operation, self.fc).allowed)

    def test_esi_rejections_show_the_short_text(self):
        self.sim.fail(MOVE, status=400, error="Squad is full")

        response = self.move(self.alpha_one, "squad_member", self.main, self.bravo)

        text = "Could not move Alpha One: ESI returned HTTP 400 (Squad is full)."
        self.assertEqual(self.answer(response), (502, text))
        self.assertEqual(AuditLog.objects.get(action="fleet.member_move").new_value["error"], text)
        self.assertEqual(fleet_overlay.expectations(self.operation.pk), [])

    def test_a_boss_handover_disables_the_moves(self):
        self.hand_over_boss()

        response = self.move(self.alpha_one, "squad_member", self.main, self.bravo)

        self.assertEqual(self.answer(response), (409, NOT_BOSS))
        self.assertEqual(write_state(self.operation, self.lead).reason, NOT_BOSS)

    def test_a_busy_fleet(self):
        cache.add(f"fleetops:fleet:{self.operation.pk}:write", 1, 60)

        self.assertEqual(self.answer(self.move(self.alpha_one, "squad_member", self.main, self.bravo)), (409, WRITE_BUSY))
        self.assert_nothing_sent()

    def test_form_posts_get_a_message(self):
        response = self.move(self.alpha_one, "squad_member", self.main, self.bravo, headers=BROWSER_FORM)

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response["Location"].endswith("#fleet"))
        self.assertEqual(self.fleet.position(self.alpha_one.character_id)[2], self.bravo)


class BulkMoveTests(MoveTestCase):
    def test_several_members_move_as_a_fleet_job(self):
        members = [self.alpha_one, self.alpha_two, self.fc_char]

        response = self.move(members, "squad_member", self.main, self.bravo, user=self.lead)

        self.assertEqual(self.answer(response), (200, "Moving 3 members in the background."))
        self.assertEqual(response.json()["level"], "info")
        job = fleet_jobs.current_job(self.operation.pk)
        self.assertEqual(response.json()["data"]["job_id"], job["id"])
        # The tests run Celery eagerly: the job has finished.
        self.assertEqual(
            (job["label"], job["state"], job["done"], job["failed"]), ("Move 3 members to Main / Bravo", "done", 3, 0)
        )
        self.assertEqual(
            [message["text"] for message in job["messages"]],
            ["Moved Alpha One to Main / Bravo.", "Moved Alpha Two to Main / Bravo.", "Moved Write FC to Main / Bravo."],
        )
        body = {"role": "squad_member", "wing_id": self.main, "squad_id": self.bravo}
        self.assertEqual(self.moves_sent(), [(member.character_id, body) for member in members])
        self.assertEqual({request.character_id for request in self.sim.requests_of(MOVE)}, {self.fc_char.character_id})
        structure = self.structure()[1]
        for member in members:
            moved = structure[member.character_id]
            self.assertEqual((moved["squad_id"], moved["pending"]), (self.bravo, True))
        step = self.operation.actions.get(action="fleet_move")
        self.assertEqual(
            (step.status, step.error_message), (OperationAction.Status.SUCCESS, "Move 3 members to Main / Bravo: 3 of 3 done.")
        )
        entry = AuditLog.objects.get(action="fleet.move_job")
        self.assertEqual(entry.actor, self.lead)
        self.assertEqual((entry.new_value["ok"], entry.new_value["failed"], entry.new_value["result"]), (3, [], "ok"))
        self.assertEqual(
            entry.new_value["moves"][0],
            {"character_id": self.alpha_one.character_id, "character_name": "Alpha One", "role": "squad_member",
             "wing_id": self.main, "squad_id": self.bravo, "result": "ok"},
        )
        self.assertFalse(AuditLog.objects.filter(action="fleet.member_move").exists())
        self.schedule.assert_called_once_with(self.operation)

    def test_members_already_there_or_gone_are_skipped(self):
        self.fleet.remove_member(self.alpha_two.character_id)
        self.read_fleet()

        members = [self.alpha_one, self.fc_char, self.alpha_two, self.bravo_one]

        response = self.move(members, "squad_member", self.main, self.bravo)

        self.assertEqual(
            response.json()["message"],
            "Moving 2 members in the background. Skipped: Alpha Two is no longer in the fleet; Bravo One is already there.",
        )
        self.assertEqual(
            [member_id for member_id, _body in self.moves_sent()],
            [self.alpha_one.character_id, self.fc_char.character_id],
        )

    def test_a_single_member_left_to_move_moves_at_once(self):
        response = self.move([self.alpha_one, self.bravo_one], "squad_member", self.main, self.bravo)

        self.assertEqual(response.json()["message"], "Moved Alpha One to Main / Bravo. Skipped: Bravo One is already there.")
        self.assertIsNone(fleet_jobs.current_job(self.operation.pk))
        self.assertTrue(AuditLog.objects.filter(action="fleet.member_move").exists())

    def test_nobody_to_move(self):
        response = self.move([self.bravo_one, 90_999_999], "squad_member", self.main, self.bravo)

        self.assertEqual(
            self.answer(response),
            (200, "Nobody was moved. Skipped: Bravo One is already there; Character 90999999 is no longer in the fleet."),
        )
        self.assert_nothing_sent()

    def test_at_most_fifty_members(self):
        ids = list(range(90_500_001, 90_500_002 + MAX_BULK_MOVES))

        response = self.move(ids, "squad_member", self.main, self.bravo)

        self.assertEqual(self.answer(response), (400, TOO_MANY_MOVES))
        self.assert_nothing_sent()

    def test_commander_positions_take_one_member(self):
        response = self.move([self.alpha_one, self.alpha_two], "squad_commander", self.main, self.bravo)

        self.assertEqual(self.answer(response), (400, ONE_COMMANDER))
        with self.assertRaisesMessage(Exception, ONE_COMMANDER):
            move_members(
                self.operation,
                [
                    MoveRequest(self.alpha_one.character_id, "fleet_commander"),
                    MoveRequest(self.alpha_two.character_id, "fleet_commander"),
                ],
                actor=self.fc,
            )
        self.assert_nothing_sent()

    def test_a_wrong_target_refuses_the_whole_move(self):
        response = self.move([self.alpha_one, self.alpha_two], "squad_member", self.logi, self.bravo)

        self.assertEqual(response.status_code, 404)
        self.assert_nothing_sent()

    def test_failed_moves_are_reported_and_the_job_goes_on(self):
        self.sim.fail(MOVE, status=400, error="Squad is full", member_id=self.alpha_two.character_id)

        self.move([self.alpha_one, self.alpha_two, self.fc_char], "squad_member", self.main, self.bravo)

        job = fleet_jobs.current_job(self.operation.pk)
        self.assertEqual((job["state"], job["done"], job["failed"]), ("done", 2, 1))
        error = "Could not move Alpha Two: ESI returned HTTP 400 (Squad is full)."
        self.assertEqual(job["messages"][1], {"ok": False, "text": error})
        self.assertEqual(self.fleet.position(self.fc_char.character_id)[2], self.bravo)
        entry = AuditLog.objects.get(action="fleet.move_job")
        self.assertEqual(entry.new_value["result"], "failed")
        self.assertEqual(
            entry.new_value["failed"],
            [{"character": "Alpha Two", "character_id": self.alpha_two.character_id, "error": error}],
        )
        step = self.operation.actions.get(action="fleet_move")
        self.assertEqual(step.status, OperationAction.Status.FAILED)
        self.assertEqual(step.error_message, "Move 3 members to Main / Bravo: 2 of 3 done, 1 failed.")

    def test_the_job_stops_after_a_boss_handover(self):
        self.hand_over_boss()

        self.move([self.alpha_one, self.alpha_two, self.fc_char], "squad_member", self.main, self.bravo)

        job = fleet_jobs.current_job(self.operation.pk)
        self.assertEqual((job["state"], job["done"], job["failed"]), ("failed", 0, 1))
        self.assertEqual(job["messages"][-1], {"ok": False, "text": NOT_BOSS})
        # One move reached ESI; the job stopped instead of trying the others.
        self.assertEqual(len(self.sim.requests_of(MOVE)), 1)
        entry = AuditLog.objects.get(action="fleet.move_job")
        self.assertEqual((entry.new_value["result"], entry.new_value["error"]), ("failed", NOT_BOSS))
        self.assertEqual(write_state(self.operation, self.fc).reason, NOT_BOSS)

    def test_the_job_stops_when_esi_forbids_the_writes(self):
        self.sim.fail(MOVE, status=403, error="Forbidden")

        self.move([self.alpha_one, self.alpha_two], "squad_member", self.main, self.bravo)

        job = fleet_jobs.current_job(self.operation.pk)
        self.assertEqual((job["state"], job["failed"]), ("failed", 1))
        self.assertEqual(len(self.sim.requests_of(MOVE)), 1)

    def test_a_member_who_left_fails_only_that_move(self):
        self.sim.fail(MOVE, status=404, error="Fleet member not found", member_id=self.alpha_one.character_id)

        self.move([self.alpha_one, self.alpha_two], "squad_member", self.main, self.bravo)

        job = fleet_jobs.current_job(self.operation.pk)
        self.assertEqual((job["state"], job["done"], job["failed"]), ("done", 1, 1))
        self.assertEqual(len(self.sim.requests_of("GetCharactersCharacterIdFleet")), 1)

    def test_each_move_is_checked_again_when_the_job_runs(self):
        delay = self.patch("fleetops.tasks.run_fleet_job.delay")
        self.move([self.alpha_one, self.alpha_two, self.fc_char], "squad_member", self.main, self.bravo)
        (operation_id, job_id, kind, payload, actor_id), _kwargs = delay.call_args
        self.assertEqual(kind, "move")
        self.assertEqual(
            payload["moves"][0], [self.alpha_one.character_id, "squad_member", self.main, self.bravo]
        )
        # Meanwhile one pilot left and the FC went to Bravo in game.
        self.fleet.remove_member(self.alpha_two.character_id)
        self.fleet.move_member(self.fc_char.character_id, "squad_member", self.main, self.bravo)
        self.read_fleet()
        self.assertFalse(
            FleetMemberState.objects.get(operation=self.operation, character_id=self.alpha_two.character_id).is_active
        )

        self.assertEqual(fleet_jobs.run_job(operation_id, job_id, kind, payload, actor_id), "done")

        job = fleet_jobs.current_job(self.operation.pk)
        self.assertEqual(
            [message["text"] for message in job["messages"]],
            ["Moved Alpha One to Main / Bravo.", "Alpha Two is no longer in the fleet.", "Write FC is already there."],
        )
        self.assertEqual([member_id for member_id, _body in self.moves_sent()], [self.alpha_one.character_id])

    def test_a_queued_job_checks_the_fleet_before_each_move(self):
        alt = f.add_alt(self.fc, "Boss Alt")
        cases = (
            ("closed", {"status": FleetOperation.Status.CLOSED}, "Fleet management is only available while the fleet is active."),
            ("boss handover", {"missing_reason": EndReason.BOSS_HANDOVER, "missing_detail": alt.character_name}, NOT_BOSS),
        )
        for name, values, reason in cases:
            with self.subTest(name):
                cache.clear()
                self.sim.requests.clear()
                delay = self.patch("fleetops.tasks.run_fleet_job.delay")
                self.move([self.alpha_one, self.alpha_two], "squad_member", self.main, self.bravo)
                args = delay.call_args.args
                # The fleet changes before the worker runs the job; the EVE fleet itself is still there.
                FleetOperation.objects.filter(pk=self.operation.pk).update(**values)

                self.assertEqual(fleet_jobs.run_job(*args), "failed")

                job = fleet_jobs.current_job(self.operation.pk)
                self.assertEqual((job["state"], job["messages"][-1]["text"]), ("failed", reason))
                self.assertEqual(self.sim.requests_of(MOVE), [])
                FleetOperation.objects.filter(pk=self.operation.pk).update(
                    status=FleetOperation.Status.ACTIVE, missing_reason="", missing_detail=""
                )

    def test_a_job_stops_when_the_fleet_closes_between_two_moves(self):
        delay = self.patch("fleetops.tasks.run_fleet_job.delay")
        self.move([self.alpha_one, self.alpha_two, self.fc_char], "squad_member", self.main, self.bravo)
        send = fleet_manage_module._send_move

        def send_then_close(*args, **kwargs):
            result = send(*args, **kwargs)
            FleetOperation.objects.filter(pk=self.operation.pk).update(status=FleetOperation.Status.CLOSED)
            return result

        with mock.patch.object(fleet_manage_module, "_send_move", side_effect=send_then_close):
            self.assertEqual(fleet_jobs.run_job(*delay.call_args.args), "failed")

        self.assertEqual(len(self.sim.requests_of(MOVE)), 1)

    def test_one_job_at_a_time(self):
        fleet_jobs.claim_job(self.operation, kind="template", label="Apply template Standard", actor=self.fc, total=4)

        response = self.move([self.alpha_one, self.alpha_two], "squad_member", self.main, self.bravo)

        self.assertEqual(self.answer(response), (409, "Wait until “Apply template Standard” has finished."))
        self.assert_nothing_sent()

    def test_without_a_worker_queue(self):
        self.patch("fleetops.tasks.run_fleet_job.delay", side_effect=QueueError("broker down"))

        response = self.move([self.alpha_one, self.alpha_two], "squad_member", self.main, self.bravo)

        self.assertEqual(self.answer(response), (503, fleet_jobs.WORKER_DOWN))
        self.assertIsNone(fleet_jobs.running_job(self.operation.pk))
        self.assert_nothing_sent()

    def test_unknown_wings_leave_the_check_to_esi(self):
        FleetOperation.objects.filter(pk=self.operation.pk).update(fleet_wings=[], fleet_wings_read_at=None)

        with mock.patch("fleetops.services.fleet_manage.fleet_jobs.start_job", wraps=fleet_jobs.start_job) as start:
            self.move([self.alpha_one, self.alpha_two], "squad_member", self.main, self.bravo)

        self.assertEqual(start.call_args.kwargs["label"], f"Move 2 members to Wing {self.main} / Squad {self.bravo}")
        self.assertEqual(len(self.sim.requests_of(MOVE)), 2)
