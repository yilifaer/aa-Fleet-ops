# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""End-to-end path of the fleet management release through the Fleet tab.

An FC runs an active fleet on the in-memory ESI fleet from the web page: the tab polls its state,
wings and squads are added and renamed, members are moved one by one and as a background job,
kicked from the tree, invited from Alliance Auth, and structure templates are applied with and
without deleting extra units. A configuration manager classifies an unknown hull; the composition
counts the bridgeable mass with the titan rule. Plain members only see the composition once the
fleet closed, and after a boss handover every change is refused with the reason. Nothing leaves
the test process.
"""

from datetime import timedelta
from unittest import mock

from django.core.cache import cache
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from fleetops.forms_config import FleetOpsSettingsForm
from fleetops.models import AuditLog, FleetOperation, FleetStructureTemplate, ShipFunction, ShipFunctionRule
from fleetops.services import fleet_jobs, fleet_refresh
from fleetops.services.fleet_access import not_boss_message
from fleetops.services.fleet_view import fleet_panel

from . import factories as f
from .fleet_sim import FleetSim
from .test_fleet_composition import AVATAR, GUARDIAN, ORCA, RIFTER, TYPES, FakeSDE

Status = FleetOperation.Status
JSON = {"HTTP_ACCEPT": "application/json"}
JITA, AMAMAKE = 30000142, 30002537
JITA_STATION = 60003760
# A hull the SDE does not know (a new ship after a patch).
NEW_HULL = 999_999


def unit_names(fleet):
    return [(wing["name"], [squad["name"] for squad in wing["squads"]]) for wing in fleet.structure()]


class FleetTabFlowTestCase(TestCase):
    def setUp(self):
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)
        f.settings(fleet_live_refresh_seconds=15)
        names = mock.patch("fleetops.services.tracking.queue_name_resolution")
        names.start()
        self.addCleanup(names.stop)
        self.sleep = mock.patch("fleetops.services.fleet_templates.sleep")
        self.sleep.start()
        self.addCleanup(self.sleep.stop)

        self.fc = f.create_user("flow_fc", perms=f.FC_PERMS, main_name="Flow FC")
        self.fc_char = f.main_of(self.fc)
        f.add_token(self.fc, self.fc_char)
        self.lead = f.create_user("flow_lead", perms=f.FC_LEAD_PERMS, main_name="Flow Lead")
        self.member = f.create_user("flow_member", perms=f.MEMBER_PERMS, main_name="Line Member")
        self.member_char = f.main_of(self.member)
        self.alpha = f.create_user("flow_alpha", perms=f.MEMBER_PERMS, main_name="Alpha Pilot")
        self.bravo = f.create_user("flow_bravo", perms=f.MEMBER_PERMS, main_name="Bravo Pilot")
        self.charlie = f.create_user("flow_charlie", perms=f.MEMBER_PERMS, main_name="Charlie Pilot")
        # Q2: a registered character of an active user who has no FleetOps access at all.
        self.recruit = f.create_user("flow_recruit", main_name="Recruit Pilot")
        self.recruit_char = f.main_of(self.recruit)

        self.sim = FleetSim()
        self.sim.install(self)
        # A fleet as EVE forms one: the FC as fleet commander, Wing 1 with Squad 1.
        self.fleet = self.sim.create_fleet(self.fc_char.character_id)
        for user in (self.member, self.alpha, self.bravo, self.charlie):
            self.fleet.add_member(f.main_of(user).character_id, ship_type_id=RIFTER, solar_system_id=JITA)
        self.operation = f.create_operation(self.fc, esi_fleet_id=self.fleet.fleet_id)
        self.read_fleet()

    # Helpers

    def read_fleet(self):
        """One fresh read of members and wings, as tracking and the Refresh button make it."""
        ok, message = fleet_refresh.refresh_now(self.operation, min_interval=0)
        self.assertTrue(ok, message)
        self.operation.refresh_from_db()

    def client_for(self, user):
        client = Client()
        client.force_login(user)
        return client

    def url(self, name, *args):
        return reverse(f"fleetops:{name}", args=[self.operation.uuid, *args])

    def post(self, client, name, *args, data=None, status=200):
        response = client.post(self.url(name, *args), data or {}, **JSON)
        self.assertEqual(response.status_code, status, response.content[:500])
        self.assertIn("no-store", response["Cache-Control"])
        return response.json()

    def state(self, client, **params):
        response = client.get(self.url("fleet_state"), params, **JSON)
        self.assertEqual(response.status_code, 200)
        return response.json()

    def char(self, user):
        return f.main_of(user).character_id

    def position(self, user):
        return self.fleet.position(self.char(user))

    def writes(self):
        return len(self.sim.writes())


class ManagerFlowTests(FleetTabFlowTestCase):
    def test_the_fc_runs_the_fleet_from_the_fleet_tab(self):
        fc = self.client_for(self.fc)

        # The Fleet tab is the first tab of an active fleet; the page no longer reloads itself.
        page = fc.get(reverse("fleetops:operation_detail", args=[self.operation.uuid]))
        self.assertContains(page, 'id="fleet-root"')
        self.assertContains(page, 'data-bs-target="#fleet" id="fleet-tab"')
        self.assertNotContains(page, "location.reload")
        self.assertContains(page, "Wing 1")

        # Polling: a full state first, then the short answer while nothing changed.
        state = self.state(fc)
        self.assertFalse(state["unchanged"])
        self.assertTrue(state["write"]["allowed"])
        self.assertEqual(state["member_count"], 5)
        self.assertIn("Alpha Pilot", state["tree_html"])
        self.assertIn("Alpha Pilot", state["members_html"])
        version = state["version"]
        self.assertTrue(self.state(fc, v=version)["unchanged"])
        # With Live on, an old read makes the poll read the fleet from ESI once per window.
        FleetOperation.objects.filter(pk=self.operation.pk).update(last_esi_update=timezone.now() - timedelta(minutes=2))
        reads = len(self.sim.requests_of("GetFleetsFleetIdMembers"))
        self.state(fc, v=version, live="1")
        self.state(fc, v=version, live="1")
        self.assertEqual(len(self.sim.requests_of("GetFleetsFleetIdMembers")), reads + 1)

        # F2: add a wing, then rename it with the name editor.
        answer = self.post(fc, "fleet_wing_add")
        wing_id = answer["data"]["wing_id"]
        self.assertTrue(answer["ok"])
        self.assertNotEqual(answer["state"]["version"], version)
        answer = self.post(fc, "fleet_wing_rename", wing_id, data={"name": "Logi"})
        # The read after the create knows EVE's name of the new wing.
        self.assertEqual(answer["message"], "Wing “Wing 2” renamed to “Logi”.")
        self.assertEqual(self.fleet.wing(wing_id)["name"], "Logi")
        self.assertIn("Logi", answer["state"]["tree_html"])

        # F3: add a squad to it, named at once.
        answer = self.post(fc, "fleet_squad_add", wing_id, data={"name": "Reps"})
        reps = answer["data"]["squad_id"]
        self.assertEqual(self.fleet.squad(reps)[1]["name"], "Reps")
        self.assertEqual(answer["message"], "Squad “Reps” created in “Logi”.")

        # F5: one member moves in the request.
        answer = self.post(
            fc, "fleet_members_move",
            data={"character_ids": [self.char(self.alpha)], "role": "squad_member", "wing_id": wing_id, "squad_id": reps},
        )
        self.assertEqual(answer["message"], "Moved Alpha Pilot to Logi / Reps.")
        self.assertEqual(self.position(self.alpha), ("squad_member", wing_id, reps))

        # Two members move as a background job (run at once by the eager worker).
        answer = self.post(
            fc, "fleet_members_move",
            data={
                "character_ids": [self.char(self.bravo), self.char(self.charlie)],
                "role": "squad_member", "wing_id": wing_id, "squad_id": reps,
            },
        )
        self.assertEqual(answer["level"], "info")
        self.assertEqual(answer["message"], "Moving 2 members in the background.")
        job = fleet_jobs.current_job(self.operation.pk)
        self.assertEqual((job["kind"], job["state"], job["done"], job["failed"]), ("move", "done", 2, 0))
        self.assertEqual(self.position(self.bravo), ("squad_member", wing_id, reps))
        self.assertEqual(self.position(self.charlie), ("squad_member", wing_id, reps))
        move_job = AuditLog.objects.get(action="fleet.move_job")
        self.assertEqual((move_job.actor, move_job.new_value["ok"], move_job.new_value["result"]), (self.fc, 2, "ok"))
        self.assertEqual(self.operation.actions.get(action="fleet_move").status, "success")

        # Make a squad commander from the member menu.
        answer = self.post(
            fc, "fleet_members_move",
            data={"character_ids": [self.char(self.alpha)], "role": "squad_commander", "wing_id": wing_id, "squad_id": reps},
        )
        self.assertEqual(answer["message"], "Alpha Pilot is now squad commander of Logi / Reps.")
        self.assertEqual(self.position(self.alpha), ("squad_commander", wing_id, reps))

        # U3 in the tree: kick with the FC character's token; the fleet is read again in the background.
        answer = self.post(fc, "kick_member", self.char(self.charlie))
        self.assertEqual(answer["message"], "Kicked Charlie Pilot (Test Corp One).")
        self.assertNotIn(self.char(self.charlie), self.fleet.members)
        self.assertNotIn("Charlie Pilot", answer["state"]["tree_html"])
        self.assertNotIn("Charlie Pilot", answer["state"]["members_html"])

        # Every change went out with the FC character's token and is listed under recent changes.
        self.assertTrue(all(request.character_id == self.fc_char.character_id for request in self.sim.writes()))
        state = self.state(fc)
        for text in (
            "Recent fleet changes",
            "Kicked Charlie Pilot (Test Corp One).",
            "Alpha Pilot is now squad commander of Logi / Reps.",
            "Squad “Reps” created in “Logi”.",
        ):
            with self.subTest(text=text):
                self.assertIn(text, state["changes_html"])
        actions = set(AuditLog.objects.filter(action__startswith="fleet.").values_list("action", flat=True))
        self.assertTrue(
            {"fleet.wing_create", "fleet.wing_rename", "fleet.squad_create", "fleet.member_move", "fleet.move_job",
             "fleet.kick_member"} <= actions
        )
        # The tree shows the names ESI has now, wing by wing.
        self.operation.refresh_from_db()
        self.assertEqual(unit_names(self.fleet), [("Wing 1", ["Squad 1"]), ("Logi", ["Squad 1", "Reps"])])
        self.assertEqual(
            [(wing["name"], [squad["name"] for squad in wing["squads"]]) for wing in self.operation.fleet_wings],
            unit_names(self.fleet),
        )

    def test_invite_a_character_registered_in_alliance_auth(self):
        fc = self.client_for(self.fc)

        # Q2: found by name without FleetOps access; the FC's own characters with an empty search.
        found = fc.get(self.url("fleet_invite_candidates"), {"q": "recruit"}, **JSON).json()["candidates"]
        self.assertEqual([candidate["name"] for candidate in found], ["Recruit Pilot"])
        mine = fc.get(self.url("fleet_invite_candidates"), **JSON).json()["candidates"]
        self.assertEqual([candidate["name"] for candidate in mine], ["Flow FC"])

        answer = self.post(fc, "fleet_invite", data={"character_ids": [self.recruit_char.character_id]})
        self.assertTrue(answer["ok"], answer["message"])
        self.assertIn(self.recruit_char.character_id, self.fleet.invitations)
        self.assertIn("Invitation sent — waiting for Recruit Pilot", answer["state"]["tree_html"])
        entry = AuditLog.objects.get(action="fleet.invite")
        self.assertEqual((entry.new_value["character_id"], entry.new_value["result"]), (self.recruit_char.character_id, "ok"))

        # The pilot accepts in game: the next read shows them in the fleet and the invitation goes.
        self.sim.accept_invite(self.recruit_char.character_id, ship_type_id=RIFTER)
        self.read_fleet()
        state = self.state(fc)
        self.assertNotIn("Invitation sent", state["tree_html"])
        self.assertIn("Recruit Pilot", state["tree_html"])
        self.assertEqual(state["member_count"], 6)

        # Characters of inactive users cannot be invited.
        self.recruit.is_active = False
        self.recruit.save(update_fields=["is_active"])
        gone = f.add_alt(self.recruit, "Inactive Alt")
        answer = self.post(fc, "fleet_invite", data={"character_ids": [gone.character_id]}, status=400)
        self.assertIn("Inactive Alt is not registered in Alliance Auth by an active user", answer["message"])

    def test_structure_templates_with_and_without_deleting_extras(self):
        fc = self.client_for(self.fc)
        standard = FleetStructureTemplate.objects.get(name="Standard")
        # Three more wings as EVE creates them, and a fifth beyond the template with a pilot in it.
        for _ in range(3):
            self.fleet.add_wing()
        spare = self.fleet.add_wing("Spare")
        spare_squad = self.fleet.wing(spare)["squads"][0]["id"]
        self.fleet.move_member(self.char(self.bravo), "squad_member", spare, spare_squad)
        self.read_fleet()

        # Q3: the delete option is unticked by default; nothing is deleted then.
        page = fc.get(reverse("fleetops:operation_detail", args=[self.operation.uuid]))
        self.assertContains(page, "Also delete empty wings and squads that are not in the template")
        self.assertNotContains(page, 'id="fleet-template-delete-extra" checked')
        answer = self.post(fc, "fleet_apply_template", data={"template_id": standard.pk})
        self.assertEqual(answer["message"], "Applying template “Standard” in the background. Members are not moved.")
        self.assertEqual(
            unit_names(self.fleet),
            [("Main", ["Alpha", "Bravo", "Charlie"]), ("Logi", ["Logi"]), ("Boosts", ["Boosts"]),
             ("Tackle", ["Tackle"]), ("Spare", ["Squad 1"])],
        )
        self.assertEqual(self.sim.requests_of("PutFleetsFleetIdMembersMemberId"), [])
        record = AuditLog.objects.get(action="fleet.template_job").new_value
        self.assertEqual((record["template"], record["delete_extra"], record["deleted"]), ("Standard", False, []))
        self.assertEqual(self.operation.actions.get(action="fleet_template").status, "success")

        # Ticked: empty extras are deleted; a wing with a member stays and is reported.
        logi = self.fleet.wing_id("Logi")
        extra_squad = self.fleet.add_squad(logi, "Extra")
        self.read_fleet()
        answer = self.post(fc, "fleet_apply_template", data={"template_id": standard.pk, "delete_extra": "1"})
        self.assertTrue(answer["ok"], answer["message"])
        self.assertEqual(
            unit_names(self.fleet),
            [("Main", ["Alpha", "Bravo", "Charlie"]), ("Logi", ["Logi"]), ("Boosts", ["Boosts"]),
             ("Tackle", ["Tackle"]), ("Spare", ["Squad 1"])],
        )
        record = AuditLog.objects.filter(action="fleet.template_job").latest("pk").new_value
        self.assertTrue(record["delete_extra"])
        self.assertEqual([entry["squad_id"] for entry in record["deleted"]], [extra_squad])
        self.assertEqual([(entry["kind"], entry["name"]) for entry in record["kept"]], [("wing", "Spare")])
        self.assertEqual(self.position(self.bravo), ("squad_member", spare, spare_squad))
        job = fleet_jobs.current_job(self.operation.pk)
        self.assertIn("Kept the wing “Spare”: it has members.", [message["text"] for message in job["messages"]])


class CompositionFlowTests(FleetTabFlowTestCase):
    def setUp(self):
        super().setUp()
        FakeSDE().install(self)
        cache.delete_many([f"fleetops:sde:type:{NEW_HULL}"])
        self.fleet.update_member(self.fc_char.character_id, ship_type_id=GUARDIAN, solar_system_id=JITA)
        self.fleet.update_member(self.char(self.alpha), ship_type_id=AVATAR, solar_system_id=AMAMAKE)
        self.fleet.update_member(self.char(self.bravo), ship_type_id=ORCA, station_id=JITA_STATION)
        self.fleet.update_member(self.char(self.charlie), ship_type_id=NEW_HULL)
        self.read_fleet()

    def test_composition_bridge_mass_and_quick_classify(self):
        lead = self.client_for(self.lead)
        state = self.state(lead)
        html = state["composition_html"]

        # Q4: the titan rule leaves titans and the Orca out of the bridgeable mass.
        # The FC's Guardian and the line member's Rifter: 11,980,000 + 1,067,000 kg.
        bridgeable = TYPES[GUARDIAN][3] + TYPES[RIFTER][3]
        self.assertIn(f"<strong>{round(bridgeable):,} kg</strong> · 2 ships", html)
        self.assertIn("Not bridgeable: Industrial Command Ship 1, Titan 1", html)
        self.assertIn("1 ship without a known mass (not in the SDE).", html)
        self.assertIn('id="fleet-unclassified"', html)
        self.assertIn(f'data-type-id="{NEW_HULL}"', html)
        self.assertIn('class="fo-pill fo-pill-info" title="The FC character is in this system">FC</span>', html)
        self.assertIn("1 docked", html)

        # A configuration manager counts the new hull as Cyno straight from the panel.
        cyno = ShipFunction.objects.get(name="Cyno")
        answer = self.post(lead, "fleet_classify", data={"type_id": NEW_HULL, "function_id": cyno.pk})
        self.assertEqual(answer["message"], f"Type {NEW_HULL} now counts as Cyno.")
        rule = ShipFunctionRule.objects.get(kind="type", target_id=NEW_HULL)
        self.assertEqual(rule.function, cyno)
        self.assertTrue(AuditLog.objects.filter(action="configuration.create", object_type="ShipFunctionRule").exists())
        panel = fleet_panel(self.operation, self.lead)
        by_function = {row.label: row.count for row in panel["composition"].by_function}
        self.assertEqual(by_function["Cyno"], 1)
        self.assertNotIn("Unclassified", by_function)
        self.assertEqual(by_function["Fleet Command"], 1)
        self.assertEqual(by_function["Boosts"], 1)

        # An FC without manage_configuration sees no classify control and cannot post one.
        self.assertNotIn('class="d-flex gap-1 fleet-classify"', self.state(self.client_for(self.fc))["composition_html"])
        self.client_for(self.fc).post(self.url("fleet_classify"), {"type_id": AVATAR, "function_id": cyno.pk}, **JSON)
        self.assertFalse(ShipFunctionRule.objects.filter(kind="type", target_id=AVATAR).exists())

    def test_the_bridge_setting_names_its_groups(self):
        form = FleetOpsSettingsForm(instance=f.settings())
        self.assertIn("Now: Titan (30), Dreadnought (485), Freighter (513)", form.fields["bridge_excluded_group_ids"].help_text)


class VisibilityFlowTests(FleetTabFlowTestCase):
    def test_members_see_the_composition_only_after_the_close(self):
        member = self.client_for(self.member)
        detail = reverse("fleetops:operation_detail", args=[self.operation.uuid])

        # VIS(2): no live tree or composition while the fleet is open.
        page = member.get(detail)
        self.assertEqual(page.status_code, 200)
        self.assertNotContains(page, 'id="fleet-root"')
        self.assertEqual(member.get(self.url("fleet_state"), **JSON).status_code, 404)
        # Viewers of all fleets get the tree read-only; fleet managers may change it.
        viewer = f.create_user("flow_viewer", perms=f.MEMBER_PERMS + ["fleetops.view_all_fleets"])
        state = self.state(self.client_for(viewer))
        self.assertEqual(state["write"], {"allowed": False, "reason": "", "show_authorize": False})
        self.assertIn("Line Member", state["tree_html"])
        self.assertNotIn("<form", state["tree_html"])
        self.assertTrue(self.state(self.client_for(self.lead))["write"]["allowed"])

        FleetOperation.objects.filter(pk=self.operation.pk).update(status=Status.CLOSED, ended_at=timezone.now())

        page = member.get(detail)
        self.assertContains(page, 'id="fleet-root"')
        self.assertContains(page, ">Composition</button>")
        state = self.state(member)
        self.assertEqual(state["status"], "closed")
        self.assertIn("Last known ships of everyone tracked in this fleet.", state["composition_html"])
        self.assertIsNone(state["tree_html"])
        self.assertFalse(state["write"]["allowed"])
        # Closed fleets take no changes.
        self.assertEqual(member.post(self.url("fleet_wing_add"), **JSON).status_code, 403)
        answer = self.post(self.client_for(self.fc), "fleet_wing_add", status=409)
        self.assertEqual(answer["message"], "Fleet management is only available while the fleet is active.")


class BossHandoverFlowTests(FleetTabFlowTestCase):
    def test_every_change_is_refused_after_a_boss_handover(self):
        fc = self.client_for(self.fc)
        wing_id = self.fleet.wings[0]["id"]
        squad_id = self.fleet.wings[0]["squads"][0]["id"]
        standard = FleetStructureTemplate.objects.get(name="Standard")
        # The FC hands the boss role to a line pilot in game.
        self.fleet.set_boss(self.char(self.alpha))

        # The next change meets ESI's 404; one boss check names the new boss.
        reason = not_boss_message(self.operation, "Alpha Pilot")
        answer = self.post(fc, "fleet_wing_add", status=409)
        self.assertEqual(answer["message"], reason)
        self.assertEqual(AuditLog.objects.get(action="fleet.wing_create").new_value["result"], "failed")
        writes = self.writes()

        # From now on nothing reaches ESI: each change is refused with the reason.
        for name, args, data in (
            ("fleet_wing_add", (), {"name": "Logi"}),
            ("fleet_wing_rename", (wing_id,), {"name": "Main"}),
            ("fleet_wing_delete", (wing_id,), {}),
            ("fleet_squad_add", (wing_id,), {}),
            ("fleet_squad_rename", (squad_id,), {"name": "Alpha"}),
            ("fleet_squad_delete", (squad_id,), {}),
            ("fleet_members_move", (), {"character_ids": [self.char(self.bravo)], "role": "squad_commander",
                                        "wing_id": wing_id, "squad_id": squad_id}),
            ("fleet_members_move", (), {"character_ids": [self.char(self.bravo), self.char(self.charlie)],
                                        "role": "squad_member", "wing_id": wing_id, "squad_id": squad_id}),
            ("kick_member", (self.char(self.charlie),), {}),
            ("fleet_invite", (), {"character_ids": [self.recruit_char.character_id]}),
            ("fleet_apply_template", (), {"template_id": standard.pk}),
        ):
            with self.subTest(action=name, data=data):
                answer = self.post(fc, name, *args, data=data, status=409)
                self.assertEqual(answer["message"], reason)
        self.assertEqual(self.writes(), writes)
        self.assertIn(self.char(self.charlie), self.fleet.members)

        # The tab shows why and offers no controls; the member list's kick buttons say the same.
        state = self.state(fc)
        self.assertEqual(state["write"], {"allowed": False, "reason": reason, "show_authorize": False})
        self.assertIn(reason, state["status_html"])
        self.assertNotIn("<form", state["tree_html"])
        self.assertIn(f'title="{reason}"', state["members_html"].replace("&#x27;", "'"))
        page = fc.get(reverse("fleetops:operation_detail", args=[self.operation.uuid]))
        self.assertContains(page, 'id="kick-blocked"')
        self.assertContains(page, 'id="fleet-invite-open" data-needs-write disabled')
        # The toolbar is not redrawn by the poller, so it lists the templates anyway, behind a disabled button.
        self.assertContains(page, 'id="fleet-template-open" data-needs-write disabled')
        self.assertContains(page, f'<option value="{standard.pk}"')
