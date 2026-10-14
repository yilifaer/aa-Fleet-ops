# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Structure templates: applying wing and squad names to a running fleet as a background job."""

from datetime import timedelta
from unittest import mock

from django.test import SimpleTestCase
from django.urls import reverse
from kombu.exceptions import OperationalError as QueueError

from fleetops.models import (
    AttendanceRecord,
    AuditLog,
    FleetMemberState,
    FleetOperation,
    FleetStructureTemplate,
    OperationAction,
)
from fleetops.providers import esi as esi_provider
from fleetops.services import fleet_jobs, fleet_overlay, fleet_templates, operations
from fleetops.services.fleet_access import NOT_ACTIVE, FleetManageError
from fleetops.services.fleet_overlay import ESI_READ_LAG
from fleetops.services.fleet_templates import apply_template, estimate, extra_plan, squad_plan, wing_plan

from . import factories as f
from .test_fleet_invites import JSON, SimFleetTestCase

STANDARD = [("Main", ["Alpha", "Bravo", "Charlie"]), ("Logi", ["Logi"]), ("Boosts", ["Boosts"]), ("Tackle", ["Tackle"])]
WRITES = (
    "PostFleetsFleetIdWings",
    "PutFleetsFleetIdWingsWingId",
    "DeleteFleetsFleetIdWingsWingId",
    "PostFleetsFleetIdWingsWingIdSquads",
    "PutFleetsFleetIdSquadsSquadId",
    "DeleteFleetsFleetIdSquadsSquadId",
)


def names(fleet):
    return [(wing["name"], [squad["name"] for squad in wing["squads"]]) for wing in fleet.structure()]


class TemplateJobTestCase(SimFleetTestCase):
    fleet_wings = (("Wing 1", ("Squad 1",)),)

    def setUp(self):
        super().setUp()
        self.sleep = self.patch("fleetops.services.fleet_templates.sleep")
        self.refresh_now = self.patch(
            "fleetops.services.fleet_refresh.refresh_now", wraps=fleet_templates.fleet_refresh.refresh_now
        )
        self.standard = FleetStructureTemplate.objects.get(name="Standard")

    def apply(self, template=None, **kwargs):
        return apply_template(self.operation, template or self.standard, actor=self.fc, **kwargs)

    def job(self):
        return fleet_jobs.current_job(self.operation.pk)

    def texts(self):
        return [message["text"] for message in self.job()["messages"]]

    def writes(self, name=None):
        return [request for request in self.sim.writes() if name is None or request.name == name]

    def record(self):
        return AuditLog.objects.get(action="fleet.template_job").new_value


class ApplyTemplateTests(TemplateJobTestCase):
    def test_a_new_fleet_gets_the_standard_layout(self):
        result = self.apply()

        self.assertTrue(result.ok)
        self.assertEqual(result.message, "Applying template “Standard” in the background. Members are not moved.")
        self.assertEqual(names(self.fleet), STANDARD)
        job = self.job()
        self.assertEqual(job["id"], result.data["job_id"])
        self.assertEqual((job["kind"], job["label"], job["state"]), ("template", "Apply template Standard", "done"))
        self.assertEqual((job["done"], job["failed"], job["total"]), (10, 0, 10))
        self.assertEqual(
            self.texts(),
            [
                "Renamed wing “Wing 1” to “Main”.",
                "Created wing 2 “Logi”.",
                "Created wing 3 “Boosts”.",
                "Created wing 4 “Tackle”.",
                "Renamed squad “Squad 1” of Main to “Alpha”.",
                "Created squad “Bravo” in Main.",
                "Created squad “Charlie” in Main.",
                "Renamed squad “Squad 1” of Logi to “Logi”.",
                "Renamed squad “Squad 1” of Boosts to “Boosts”.",
                "Renamed squad “Squad 1” of Tackle to “Tackle”.",
            ],
        )
        # Every change went out with the FC character's token; nobody was moved or deleted.
        self.assertTrue(all(request.character_id == self.fc_char.character_id for request in self.writes()))
        self.assertEqual(self.sim.requests_of("PutFleetsFleetIdMembersMemberId"), [])
        self.assertEqual(self.writes("DeleteFleetsFleetIdSquadsSquadId") + self.writes("DeleteFleetsFleetIdWingsWingId"), [])
        # Waited for ESI's cache after creating wings and before the last read.
        self.assertEqual(self.sleep.call_args_list, [mock.call(ESI_READ_LAG), mock.call(ESI_READ_LAG)])
        self.refresh_now.assert_called_once_with(self.operation, min_interval=0)

    def test_the_job_is_recorded(self):
        self.apply()

        record = self.record()
        self.assertEqual(record["template"], "Standard")
        self.assertEqual(record["layout"], self.standard.layout)
        self.assertFalse(record["delete_extra"])
        self.assertEqual([entry["name"] for entry in record["created"]], ["Logi", "Boosts", "Tackle", "Bravo", "Charlie"])
        self.assertEqual(
            [(entry["kind"], entry["old"], entry["name"]) for entry in record["renamed"]],
            [("wing", "Wing 1", "Main"), ("squad", "Squad 1", "Alpha"), ("squad", "Squad 1", "Logi"),
             ("squad", "Squad 1", "Boosts"), ("squad", "Squad 1", "Tackle")],
        )
        self.assertEqual((record["deleted"], record["kept"], record["failures"]), ([], [], []))
        self.assertEqual((record["result"], record["done"], record["failed"]), ("ok", 10, 0))
        entry = AuditLog.objects.get(action="fleet.template_job")
        self.assertEqual(entry.actor, self.fc)
        step = self.operation.actions.get(action="fleet_template")
        self.assertEqual(step.status, OperationAction.Status.SUCCESS)
        self.schedule_refresh.assert_called_once_with(self.operation)

    def test_the_fleet_view_shows_the_names_before_esi_does(self):
        self.apply()
        self.operation.refresh_from_db()

        wings, _members = fleet_overlay.current_structure(self.operation)

        self.assertEqual([(wing["name"], [squad["name"] for squad in wing["squads"]]) for wing in wings], STANDARD)
        self.assertTrue(all(wing["pending"] for wing in wings))

    def test_wings_without_squads_of_their_own(self):
        self.sim.new_wing_squads = 0

        self.apply()

        self.assertEqual(names(self.fleet), STANDARD)
        self.assertEqual(len(self.writes("PostFleetsFleetIdWingsWingIdSquads")), 5)

    def test_a_stale_read_after_creating_wings_is_read_again(self):
        self.sim.stale_reads = 1
        # Only the job's own reads count: not the setup's, not the fleet read at the job's end.
        self.refresh_now.return_value = (True, "")
        self.sim.requests.clear()

        self.apply()

        # Without the second read the new wings' own squads would be missed and extra ones created.
        self.assertEqual(names(self.fleet), STANDARD)
        self.assertEqual(len(self.sim.requests_of("GetFleetsFleetIdWings")), 3)
        self.refresh_now.assert_called_once_with(self.operation, min_interval=0)

    def test_nothing_to_change(self):
        self.apply()
        self.sleep.reset_mock()
        self.sim.requests.clear()
        # Long enough later that ESI shows the first run's changes.
        fleet_overlay.forget(self.operation.pk)

        self.apply()

        job = self.job()
        self.assertEqual((job["done"], job["total"]), (1, 1))
        self.assertEqual(self.texts(), ["Nothing to change: the fleet already matches “Standard”."])
        self.assertEqual(self.writes(), [])
        self.sleep.assert_not_called()

    def test_recent_changes_are_waited_for(self):
        fleet_overlay.expect(
            self.operation.pk, {"kind": "wing_renamed", "wing_id": self.fleet.wing_id("Wing 1"), "name": "Old"}
        )

        self.apply()

        self.assertGreater(self.sleep.call_args_list[0].args[0], ESI_READ_LAG - 1)
        self.assertLessEqual(self.sleep.call_args_list[0].args[0], ESI_READ_LAG)


class LargerFleetTests(TemplateJobTestCase):
    fleet_wings = (
        ("Wing 1", ("Squad 1", "Squad 2", "Squad 3")),
        ("Wing 2", ("Squad 1",)),
        ("Wing 3", ("Squad 1",)),
    )

    def setUp(self):
        super().setUp()
        self.small = f.structure_template("Small", "Main: Alpha")
        self.wing1, self.wing3 = self.fleet.wing_id("Wing 1"), self.fleet.wing_id("Wing 3")
        self.squad3 = self.fleet.squad_id("Squad 3", wing="Wing 1")
        self.pilot_a = self.pilot("Squad Three Pilot")[1]
        self.pilot_b = self.pilot("Wing Three Pilot")[1]
        self.fleet.add_member(self.pilot_a.character_id, wing_id=self.wing1, squad_id=self.squad3)
        self.fleet.add_member(self.pilot_b.character_id, "wing_commander", wing_id=self.wing3)
        self.sync()

    def test_only_names_change_without_the_delete_option(self):
        self.apply(self.small)

        self.assertEqual(
            names(self.fleet),
            [("Main", ["Alpha", "Squad 2", "Squad 3"]), ("Wing 2", ["Squad 1"]), ("Wing 3", ["Squad 1"])],
        )
        self.assertEqual(len(self.writes()), 2)
        self.assertEqual((self.job()["done"], self.job()["total"]), (2, 2))
        # No wing was created, so the job waited only before its last read.
        self.assertEqual(self.sleep.call_count, 1)

    def test_the_delete_option_removes_only_empty_extras(self):
        self.apply(self.small, delete_extra=True)

        self.assertEqual(names(self.fleet), [("Main", ["Alpha", "Squad 3"]), ("Wing 3", ["Squad 1"])])
        self.assertEqual(
            self.texts(),
            [
                "Renamed wing “Wing 1” to “Main”.",
                "Renamed squad “Squad 1” of Main to “Alpha”.",
                "Deleted the empty squad “Squad 2” of Main.",
                "Kept the squad “Squad 3” of Main: it has members.",
                "Deleted the empty wing “Wing 2”.",
                "Kept the wing “Wing 3”: it has members.",
            ],
        )
        record = self.record()
        self.assertTrue(record["delete_extra"])
        self.assertEqual([entry["name"] for entry in record["deleted"]], ["Squad 2", "Wing 2"])
        self.assertEqual([entry["name"] for entry in record["kept"]], ["Squad 3", "Wing 3"])
        self.assertEqual((self.job()["done"], self.job()["total"]), (6, 6))

    def test_esi_refusing_a_delete_is_reported(self):
        # The pilot moved into Squad 2 in game; FleetOps has not read it yet.
        self.fleet.move_member(self.pilot_a.character_id, "squad_member", self.wing1, self.fleet.squad_id("Squad 2"))

        self.apply(self.small, delete_extra=True)

        job = self.job()
        self.assertEqual((job["state"], job["failed"]), ("done", 1))
        self.assertIn(
            "Squad “Squad 2” of Main: Could not delete the squad: ESI returned HTTP 400 (Only empty squads can be "
            "deleted).",
            self.texts(),
        )
        self.assertEqual(self.operation.actions.get(action="fleet_template").status, OperationAction.Status.FAILED)

    def test_estimate(self):
        wings = self.operation.fleet_wings
        layout = self.small.wings
        members = list(fleet_overlay.current_structure(self.operation)[1].values())

        self.assertEqual(estimate(wings, layout), 2)
        self.assertEqual(estimate(wings, layout, members=members, delete_extra=True), 6)
        self.assertEqual(estimate([], self.standard.wings), 10)
        self.assertEqual(estimate(wings[:1], [{"name": "Wing 1", "squads": ["Squad 1", "Squad 2", "Squad 3"]}]), 1)


class FailureTests(TemplateJobTestCase):
    def test_a_failed_call_is_reported_and_the_job_goes_on(self):
        self.sim.fail("PutFleetsFleetIdSquadsSquadId", status=502, times=1)

        self.apply()

        job = self.job()
        self.assertEqual((job["state"], job["done"], job["failed"]), ("done", 9, 1))
        self.assertEqual(
            self.texts()[4], "Main / Squad 1: Could not rename the squad: ESI returned HTTP 502 (Bad Gateway)."
        )
        self.assertEqual(
            self.record()["failures"],
            [{"unit": "Main / Squad 1", "error": "Could not rename the squad: ESI returned HTTP 502 (Bad Gateway)."}],
        )
        self.assertEqual(self.operation.actions.get(action="fleet_template").status, OperationAction.Status.FAILED)

    def test_a_wing_that_cannot_be_created(self):
        self.sim.fail("PostFleetsFleetIdWings", status=400, error="The fleet already has the maximum number of wings")

        self.apply()

        self.assertEqual(names(self.fleet), [("Main", ["Alpha", "Bravo", "Charlie"])])
        self.assertEqual(self.job()["failed"], 3)
        self.assertIn(
            "Wing 2 “Logi”: Could not create a wing: ESI returned HTTP 400 (The fleet already has the maximum number "
            "of wings).",
            self.texts(),
        )

    def test_a_new_wing_that_cannot_be_named(self):
        self.sim.fail("PutFleetsFleetIdWingsWingId", status=502, wing_id=2_073_700_000_002)

        self.apply()

        self.assertEqual(names(self.fleet)[1], ("Wing 2", ["Logi"]))
        self.assertIn(
            "Wing 2 was created, but not named “Logi”: Could not rename the wing: ESI returned HTTP 502 (Bad Gateway).",
            self.texts(),
        )
        created = self.record()["created"]
        self.assertIn({"kind": "wing", "wing_id": self.fleet.wing_id("Wing 2"), "name": ""}, created)

    def test_a_handover_stops_the_job(self):
        pilot = self.pilot("New Boss")[1]
        self.join(pilot)
        rename = esi_provider.rename_fleet_squad

        def handover_first(*args):
            self.fleet.set_boss(pilot.character_id)
            return rename(*args)

        with mock.patch("fleetops.services.fleet_templates.rename_fleet_squad", side_effect=handover_first):
            self.apply()

        job = self.job()
        self.assertEqual(job["state"], "failed")
        self.assertIn("is not the fleet boss any more (current boss: New Boss)", job["messages"][-1]["text"])
        # Four wing changes went through; the squads after the handover were never sent.
        self.assertEqual((job["done"], job["failed"]), (4, 1))
        self.assertEqual(self.writes("PostFleetsFleetIdWingsWingIdSquads"), [])
        record = self.record()
        self.assertEqual(record["result"], "failed")
        self.assertIn("not the fleet boss any more", record["error"])
        step = self.operation.actions.get(action="fleet_template")
        self.assertIn("Stopped:", step.error_message)

    def test_a_handover_before_the_first_read(self):
        pilot = self.pilot("New Boss")[1]
        self.join(pilot)
        self.fleet.set_boss(pilot.character_id)

        self.apply()

        job = self.job()
        self.assertEqual((job["state"], job["done"]), ("failed", 0))
        self.assertIn("current boss: New Boss", job["messages"][-1]["text"])
        self.assertEqual(self.writes(), [])

    def test_the_fleet_ending_stops_the_job(self):
        create = esi_provider.create_fleet_squad

        def end_fleet(*args):
            FleetOperation.objects.filter(pk=self.operation.pk).update(status=FleetOperation.Status.ENDING)
            return create(*args)

        with mock.patch("fleetops.services.fleet_templates.create_fleet_squad", side_effect=end_fleet):
            self.apply()

        job = self.job()
        self.assertEqual(job["state"], "failed")
        self.assertEqual(job["messages"][-1]["text"], NOT_ACTIVE)
        # The squad created before the fleet ended was recorded even though it was not named.
        self.assertEqual(self.record()["created"][-1]["name"], "")

    def test_losing_the_write_scope_stops_the_job(self):
        rename = esi_provider.rename_fleet_wing

        def drop_scope_after(*args):
            result = rename(*args)
            self.token.scopes.clear()
            return result

        with mock.patch("fleetops.services.fleet_templates.rename_fleet_wing", side_effect=drop_scope_after):
            self.apply()

        job = self.job()
        self.assertEqual((job["state"], job["done"]), ("failed", 1))
        self.assertIn("fleet write scope", job["messages"][-1]["text"])

    def test_the_error_limit_stops_the_job(self):
        self.sim.fail("PostFleetsFleetIdWings", status=420)

        self.apply()

        job = self.job()
        self.assertEqual((job["state"], job["failed"]), ("failed", 1))
        self.assertEqual(len(self.writes("PostFleetsFleetIdWings")), 1)

    def test_a_wing_deleted_in_game_during_the_job(self):
        create = esi_provider.create_fleet_wing
        main = self.fleet.wings[0]

        def delete_main_in_game(*args):
            wing_id = create(*args)
            if main in self.fleet.wings:
                self.fleet.wings.remove(main)
            return wing_id

        with mock.patch("fleetops.services.fleet_templates.create_fleet_wing", side_effect=delete_main_in_game):
            self.apply()

        self.assertIn("Wing “Main”: The wing is no longer in the fleet.", self.texts())
        self.assertEqual(self.job()["state"], "done")


class StartTests(TemplateJobTestCase):
    def test_who_may_apply_and_when(self):
        member = f.create_user(perms=f.MEMBER_PERMS)
        with self.assertRaises(FleetManageError) as ctx:
            apply_template(self.operation, self.standard, actor=member)
        self.assertEqual(ctx.exception.status, 404)

        inactive = f.structure_template("Old", "Main: Alpha", is_active=False)
        with self.assertRaises(FleetManageError) as ctx:
            self.apply(inactive)
        self.assertEqual((ctx.exception.status, ctx.exception.message), (404, fleet_templates.TEMPLATE_GONE))

        FleetStructureTemplate.objects.filter(pk=inactive.pk).update(is_active=True, layout="Main")
        inactive.refresh_from_db()
        with self.assertRaises(FleetManageError) as ctx:
            self.apply(inactive)
        self.assertEqual(ctx.exception.status, 400)
        self.assertIn("cannot be applied", ctx.exception.message)
        self.assertEqual(self.writes(), [])

    def test_one_job_at_a_time(self):
        delay = self.patch("fleetops.tasks.run_fleet_job.delay")
        self.apply()

        with self.assertRaises(FleetManageError) as ctx:
            self.apply()

        self.assertEqual(ctx.exception.message, "Wait until “Apply template Standard” has finished.")
        payload = delay.call_args.args[3]
        self.assertEqual(payload["wings"], self.standard.wings)
        self.assertEqual((payload["name"], payload["delete_extra"]), ("Standard", False))
        # The progress bar starts from what the stored fleet suggests.
        self.assertEqual(fleet_jobs.current_job(self.operation.pk)["total"], 10)

    def test_without_a_worker(self):
        self.patch("fleetops.tasks.run_fleet_job.delay", side_effect=QueueError("down"))

        with self.assertLogs("extensions", "ERROR"), self.assertRaises(FleetManageError) as ctx:
            self.apply()

        self.assertEqual(ctx.exception.status, 503)

    def test_layout_is_taken_when_the_job_starts(self):
        delay = self.patch("fleetops.tasks.run_fleet_job.delay")
        self.apply()
        FleetStructureTemplate.objects.filter(pk=self.standard.pk).update(layout="Other: One")

        fleet_jobs.run_job(*delay.call_args.args)

        self.assertEqual(names(self.fleet), STANDARD)


class PlanTests(SimpleTestCase):
    def test_plans(self):
        existing = [
            {"id": 1, "name": "Main", "squads": [{"id": 11, "name": "Alpha"}, {"id": 12, "name": "Squad 2"}]},
            {"id": 2, "name": "Wing 2", "squads": [{"id": 21, "name": "Squad 1"}]},
        ]
        layout = [{"name": "Main", "squads": ["Alpha", "Bravo", "Charlie"]}]

        self.assertEqual(wing_plan(existing, layout + [{"name": "Logi", "squads": ["Logi"]}] * 2),
                         [("rename", 1, existing[1], "Logi"), ("create", 2, None, "Logi")])
        self.assertEqual(squad_plan(existing[0]["squads"], layout[0]["squads"]),
                         [("rename", 1, existing[0]["squads"][1], "Bravo"), ("create", 2, None, "Charlie")])
        member = {"character_id": 5, "role": "squad_member", "wing_id": 2, "squad_id": 21}
        self.assertEqual(extra_plan(existing, [1], [{"name": "Main", "squads": ["Alpha"]}], [member]),
                         [("delete", "squad", existing[0]["squads"][1], existing[0]),
                          ("keep", "wing", existing[1], None)])


class TemplatePageTests(SimFleetTestCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.fc)
        self.page_url = reverse("fleetops:operation_detail", args=[self.operation.uuid])
        self.url = reverse("fleetops:fleet_apply_template", args=[self.operation.uuid])

    def test_the_page_offers_the_active_templates(self):
        f.structure_template("Hidden", "Main: Alpha", is_active=False)
        response = self.client.get(self.page_url)

        self.assertContains(response, 'id="fleet-template-open" data-needs-write')
        self.assertContains(response, 'data-layout="Main: Alpha, Bravo, Charlie\nLogi: Logi\nBoosts: Boosts\nTackle: Tackle"')
        self.assertNotContains(response, ">Hidden</option>")
        # Q3: deleting extra wings and squads is opt-in.
        self.assertContains(
            response,
            '<input class="form-check-input" type="checkbox" name="delete_extra" value="1" '
            'id="fleet-template-delete-extra"><label class="form-check-label" for="fleet-template-delete-extra">'
            "Also delete empty wings and squads that are not in the template</label>",
            html=False,
        )

    def test_without_templates_the_button_stays_off(self):
        FleetStructureTemplate.objects.update(is_active=False)

        response = self.client.get(self.page_url)

        self.assertContains(response, 'id="fleet-template-open" disabled title="No structure template is configured."')

    def test_the_delete_option_reaches_the_job(self):
        self.patch("fleetops.services.fleet_templates.sleep")
        f.structure_template("Small", "Main: Alpha")
        template = FleetStructureTemplate.objects.get(name="Small")
        self.fleet.add_wing("Extra")

        unticked = self.client.post(self.url, {"template_id": str(template.pk)}, **JSON)
        self.assertEqual(unticked.status_code, 200)
        self.assertEqual(unticked.json()["level"], "info")
        self.assertIn("Extra", [name for name, _squads in names(self.fleet)])

        self.assertEqual(fleet_jobs.current_job(self.operation.pk)["state"], "done")
        ticked = self.client.post(self.url, {"template_id": str(template.pk), "delete_extra": "on"}, **JSON)
        self.assertEqual(ticked.status_code, 200)
        self.assertEqual(names(self.fleet), [("Main", ["Alpha"])])


class EndDuringTheJobTests(TemplateJobTestCase):
    """The FC ends the fleet while a template job waits for its last read."""

    def test_pilots_who_joined_after_the_end_get_no_pap(self):
        self.patch("fleetops.services.tracking.queue_name_resolution")
        FleetOperation.objects.filter(pk=self.operation.pk).update(pap_minimum_minutes=0)
        late_user, late = self.pilot("Late Joiner", perms=f.MEMBER_PERMS)

        def end_then_join(seconds):
            if self.sleep.call_count < 2:
                return
            # During the last wait: the FC ends the fleet, then a pilot joins in game.
            with mock.patch("fleetops.services.operations._queue_end"):
                operations.request_fleet_end(FleetOperation.objects.get(pk=self.operation.pk), actor=self.fc)
            end = FleetOperation.objects.get(pk=self.operation.pk).end_requested_at
            self.sim.expire_cache()
            self.fleet.add_member(late.character_id, join_time=end + timedelta(seconds=1))

        self.sleep.side_effect = end_then_join

        self.apply()

        operation = FleetOperation.objects.get(pk=self.operation.pk)
        self.assertEqual(operation.status, FleetOperation.Status.ENDING)
        # The job's last read counted nobody past the end request and left the late pilot out.
        self.assertLessEqual(operation.last_esi_update, operation.end_requested_at)
        self.assertFalse(FleetMemberState.objects.filter(operation=operation, character_id=late.character_id).exists())
        operations.finish_requested_end(operation)
        self.assertFalse(AttendanceRecord.objects.filter(operation=operation, character_id=late.character_id).exists())
