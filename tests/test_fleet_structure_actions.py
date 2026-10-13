"""Wing and squad changes from the Fleet tab, against the in-memory EVE fleet: create, rename, delete."""

from datetime import timedelta
from unittest import mock

from django.contrib.messages import constants as message_levels
from django.contrib.messages import get_messages
from django.core.cache import cache
from django.template.loader import render_to_string
from django.test import Client, RequestFactory, TestCase
from django.urls import reverse
from django.utils import timezone

from fleetops.models import AuditLog, FleetOperation
from fleetops.services import fleet_jobs, fleet_overlay
from fleetops.services.fleet_access import (
    FLEET_GONE,
    NO_WRITE_TOKEN,
    NOT_ACTIVE,
    WRITE_BUSY,
    FleetManageError,
    write_state,
)
from fleetops.services.fleet_manage import add_squad, add_wing, delete_squad, delete_wing, rename_squad, rename_wing
from fleetops.services.fleet_rules import NAME_LENGTH_MESSAGE, normalize_wings
from fleetops.services.fleet_view import FleetView, MemberView, SquadView, WingView
from fleetops.services.tracking import sync_operation

from . import factories as f
from .fleet_sim import FleetSim

Status = FleetOperation.Status
EndReason = FleetOperation.EndReason
JSON = {"HTTP_ACCEPT": "application/json"}
BROWSER_FORM = {"HTTP_ACCEPT": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}
SECRET = "token=access-secret"


class FleetWriteTestCase(TestCase):
    """An FC's fleet in the in-memory EVE, tracked by FleetOps with its wings read."""

    wings = (("Main", ("Alpha", "Bravo")), ("Logi", ("Logi",)))
    # The reads after a change are replaced by a mock unless a test looks at the queued tasks.
    patch_refresh = True

    def setUp(self):
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)
        self.fc = f.create_user("write-fc", perms=f.FC_PERMS, main_name="Write FC")
        self.fc_char = f.main_of(self.fc)
        self.token = f.add_token(self.fc, self.fc_char)
        self.lead = f.create_user("write-lead", perms=f.FC_LEAD_PERMS, main_name="Write Lead")
        f.add_token(self.lead, f.main_of(self.lead))
        self.sim = FleetSim()
        self.sim.install(self)
        self.fleet = self.sim.create_fleet(self.fc_char.character_id, wings=self.wings)
        self.operation = f.create_operation(self.fc, esi_fleet_id=self.fleet.fleet_id)
        if self.patch_refresh:
            self.schedule = self.patch("fleetops.services.fleet_refresh.schedule_refresh")
        self.read_fleet()

    def patch(self, target, **kwargs):
        patcher = mock.patch(target, **kwargs)
        mocked = patcher.start()
        self.addCleanup(patcher.stop)
        return mocked

    def pilot(self, name, *, squad=None, role="squad_member", wing=None):
        """An Alliance Auth pilot who joins the EVE fleet in ``squad`` (a name) or as wing commander of ``wing``."""
        character = f.main_of(f.create_user(main_name=name))
        if role == "wing_commander":
            self.fleet.add_member(character.character_id, role, self.fleet.wing_id(wing))
        else:
            squad_id = self.fleet.squad_id(squad) if squad else None
            wing_id = self.fleet.squad(squad_id)[0]["id"] if squad_id else None
            self.fleet.add_member(character.character_id, role, wing_id, squad_id)
        return character

    def read_fleet(self, *, at=None):
        """What tracking does: read the members, keep the wings with their read time."""
        sync_operation(self.operation, fresh=True)
        FleetOperation.objects.filter(pk=self.operation.pk).update(
            fleet_wings=normalize_wings(self.fleet.structure()), fleet_wings_read_at=at or timezone.now()
        )
        self.operation.refresh_from_db()

    def url(self, name, **kwargs):
        return reverse(f"fleetops:{name}", kwargs={"operation_uuid": self.operation.uuid, **kwargs})

    def post(self, name, data=None, *, user=None, headers=JSON, **kwargs):
        client = Client()
        client.force_login(user or self.fc)
        return client.post(self.url(name, **kwargs), data or {}, **headers)

    def entries(self, action):
        return list(AuditLog.objects.filter(action=action).order_by("pk"))

    def structure(self):
        self.operation.refresh_from_db()
        return fleet_overlay.current_structure(self.operation)

    def assert_nothing_sent(self):
        self.assertEqual(self.sim.writes(), [])
        self.assertFalse(AuditLog.objects.filter(action__startswith="fleet.").exists())


class AddWingTests(FleetWriteTestCase):
    def test_a_wing_is_created_with_the_fc_characters_token(self):
        # A fleet lead acts; EVE only accepts the boss's token.
        response = self.post("fleet_wing_add", user=self.lead)

        self.assertEqual(response.status_code, 200)
        body = response.json()
        wing_id = self.fleet.wings[-1]["id"]
        self.assertEqual(
            {key: body[key] for key in ("ok", "message", "level", "data")},
            {"ok": True, "message": "Wing created.", "level": "success", "data": {"wing_id": wing_id}},
        )
        self.assertIn("tree_html", body["state"])
        [request] = self.sim.writes()
        self.assertEqual((request.name, request.body), ("PostFleetsFleetIdWings", None))
        self.assertEqual(request.character_id, self.fc_char.character_id)
        self.assertEqual(request.params["fleet_id"], self.fleet.fleet_id)
        # The fleet view shows the wing before ESI does.
        wings, _members = self.structure()
        self.assertEqual(wings[-1], {"id": wing_id, "name": "Wing", "squads": [], "pending": True})
        [entry] = self.entries("fleet.wing_create")
        self.assertEqual(entry.actor, self.lead)
        self.assertEqual(
            entry.new_value, {"wing_id": wing_id, "name": "", "result": "ok", "summary": "Wing created."}
        )
        self.assertEqual((entry.object_type, entry.object_id), ("FleetOperation", str(self.operation.pk)))
        self.schedule.assert_called_once_with(self.operation)

    def test_a_named_wing_is_renamed_in_the_same_request(self):
        response = self.post("fleet_wing_add", {"name": " Cap  Logi "})

        self.assertEqual(response.json()["message"], "Wing “Cap Logi” created.")
        wing_id = response.json()["data"]["wing_id"]
        self.assertEqual(
            [(request.name, request.body) for request in self.sim.writes()],
            [("PostFleetsFleetIdWings", None), ("PutFleetsFleetIdWingsWingId", {"name": "Cap Logi"})],
        )
        self.assertEqual(self.fleet.wing(wing_id)["name"], "Cap Logi")
        self.assertEqual(self.structure()[0][-1]["name"], "Cap Logi")
        self.assertEqual(self.entries("fleet.wing_create")[0].new_value["name"], "Cap Logi")

    def test_a_failed_rename_keeps_the_new_wing(self):
        self.sim.fail("PutFleetsFleetIdWingsWingId", status=502)

        response = self.post("fleet_wing_add", {"name": "Logi"})

        body = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual((body["ok"], body["level"]), (True, "warning"))
        self.assertEqual(
            body["message"],
            "Wing created, but renaming failed: Could not rename the wing: ESI returned HTTP 502 (Bad Gateway).",
        )
        # The scripts open the name editor on the new wing again.
        self.assertEqual(body["data"], {"wing_id": self.fleet.wings[-1]["id"]})
        self.assertEqual(len(self.fleet.wings), 3)
        self.assertEqual(self.entries("fleet.wing_create")[0].new_value["result"], "ok")
        rename = self.entries("fleet.wing_rename")[0].new_value
        self.assertEqual((rename["result"], rename["name"]), ("failed", "Logi"))
        self.assertIn("HTTP 502", rename["error"])

    def test_names_are_checked_before_anything_is_sent(self):
        for name in ("Eleven char", "​​"):
            with self.subTest(name=name):
                response = self.post("fleet_wing_add", {"name": name})
                self.assertEqual((response.status_code, response.json()["message"]), (400, NAME_LENGTH_MESSAGE))
        self.assert_nothing_sent()

    def test_eve_allows_five_wings(self):
        for name in ("Three", "Four", "Five"):
            self.fleet.add_wing(name)
        self.read_fleet()

        response = self.post("fleet_wing_add")

        self.assertEqual((response.status_code, response.json()["message"]), (409, "EVE allows at most 5 wings."))
        self.assert_nothing_sent()

    def test_esi_failures_show_the_short_text(self):
        self.sim.fail("PostFleetsFleetIdWings", status=502)

        response = self.post("fleet_wing_add", {"name": "Logi"})

        text = "Could not create a wing: ESI returned HTTP 502 (Bad Gateway)."
        self.assertEqual((response.status_code, response.json()["message"]), (502, text))
        self.assertEqual(response.json()["level"], "error")
        [entry] = self.entries("fleet.wing_create")
        self.assertEqual(entry.new_value, {"name": "Logi", "result": "failed", "summary": text, "error": text})
        self.assertEqual(fleet_overlay.expectations(self.operation.pk), [])
        self.schedule.assert_not_called()

    def test_unexpected_errors_never_show_their_text(self):
        self.patch("fleetops.services.fleet_manage.create_fleet_wing", side_effect=RuntimeError(SECRET))

        with self.assertLogs("extensions", level="ERROR"):
            response = self.post("fleet_wing_add")

        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["message"], "Creating a wing failed unexpectedly (RuntimeError).")
        self.assertNotIn(SECRET, response.content.decode())
        self.assertNotIn(SECRET, str(self.entries("fleet.wing_create")[0].new_value))



class RefreshAfterChangeTests(FleetWriteTestCase):
    patch_refresh = False

    def test_the_fleet_is_read_now_and_after_the_esi_cache(self):
        apply_async = self.patch("fleetops.tasks.refresh_fleet_structure.apply_async")

        for change in (
            lambda: add_wing(self.operation, actor=self.fc),
            lambda: rename_wing(self.operation, self.fleet.wing_id("Main"), "Fleet A", actor=self.fc),
            lambda: delete_squad(self.operation, self.fleet.squad_id("Bravo"), actor=self.fc),
        ):
            apply_async.reset_mock()
            self.assertTrue(change().ok)
            self.assertEqual(
                [call.kwargs["countdown"] for call in apply_async.call_args_list], [0, fleet_overlay.ESI_READ_LAG]
            )
            self.assertEqual({call.args[0] for call in apply_async.call_args_list}, {(self.operation.pk,)})

    def test_the_refresh_task_reads_the_fleet_again(self):
        # Celery runs eagerly in the tests: both reads happen at once.
        reads = len(self.sim.requests_of("GetFleetsFleetIdMembers"))

        add_wing(self.operation, actor=self.fc)

        self.assertGreater(len(self.sim.requests_of("GetFleetsFleetIdMembers")), reads)


class RenameAndDeleteWingTests(FleetWriteTestCase):
    def test_rename(self):
        main = self.fleet.wing_id("Main")

        response = self.post("fleet_wing_rename", {"name": "Fleet A"}, wing_id=main)

        self.assertEqual(response.json()["message"], "Wing “Main” renamed to “Fleet A”.")
        [request] = self.sim.writes()
        self.assertEqual(
            (request.name, request.params["wing_id"], request.body),
            ("PutFleetsFleetIdWingsWingId", main, {"name": "Fleet A"}),
        )
        self.assertEqual(self.fleet.wing(main)["name"], "Fleet A")
        wing = self.structure()[0][0]
        self.assertEqual((wing["name"], wing["pending"]), ("Fleet A", True))
        [entry] = self.entries("fleet.wing_rename")
        self.assertEqual(entry.old_value, {"wing_id": main, "name": "Main"})
        self.assertEqual(
            entry.new_value,
            {"wing_id": main, "name": "Fleet A", "result": "ok", "summary": "Wing “Main” renamed to “Fleet A”."},
        )
        self.schedule.assert_called_once_with(self.operation)

    def test_the_same_name_sends_nothing(self):
        response = self.post("fleet_wing_rename", {"name": " Main "}, wing_id=self.fleet.wing_id("Main"))

        self.assertEqual((response.status_code, response.json()["level"]), (200, "info"))
        self.assertEqual(response.json()["message"], "The wing is already called “Main”.")
        self.assert_nothing_sent()

    def test_a_wing_that_is_gone_refreshes_the_view(self):
        response = self.post("fleet_wing_rename", {"name": "Fleet A"}, wing_id=4_000_000_001)

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["message"], "That wing no longer exists. The fleet view has been refreshed.")
        self.schedule.assert_called_once_with(self.operation)
        self.assert_nothing_sent()

    def test_without_a_wings_read_esi_decides(self):
        FleetOperation.objects.filter(pk=self.operation.pk).update(fleet_wings=[], fleet_wings_read_at=None)

        response = self.post("fleet_wing_rename", {"name": "Fleet A"}, wing_id=4_000_000_001)

        # ESI answers 404; the FC character is still the boss, so the wing is gone.
        text = "Could not rename the wing: ESI returned HTTP 404 (Wing not found)."
        self.assertEqual((response.status_code, response.json()["message"]), (404, text))
        self.assertEqual(len(self.sim.requests_of("GetCharactersCharacterIdFleet")), 1)
        self.assertEqual(self.entries("fleet.wing_rename")[0].new_value["error"], text)
        self.schedule.assert_called_once_with(self.operation)
        self.assertTrue(write_state(self.operation, self.fc).allowed)

    def test_delete_an_empty_wing(self):
        spare = self.fleet.add_wing("Spare", squads=2)
        self.read_fleet()

        response = self.post("fleet_wing_delete", wing_id=spare)

        self.assertEqual(response.json()["message"], "Wing “Spare” deleted.")
        self.assertEqual([request.name for request in self.sim.writes()], ["DeleteFleetsFleetIdWingsWingId"])
        self.assertNotIn(spare, [wing["id"] for wing in self.fleet.wings])
        self.assertNotIn(spare, [wing["id"] for wing in self.structure()[0]])
        [entry] = self.entries("fleet.wing_delete")
        self.assertEqual(entry.old_value, {"wing_id": spare, "name": "Spare"})
        self.assertEqual(entry.new_value, {"result": "ok", "summary": "Wing “Spare” deleted."})

    def test_only_empty_wings_are_deleted(self):
        self.pilot("Squad Pilot", squad="Alpha")
        self.pilot("Logi Lead", role="wing_commander", wing="Logi")
        self.read_fleet()
        message = "Only empty wings can be deleted: move the wing commander and all squad members first."

        for wing in ("Main", "Logi"):
            with self.subTest(wing=wing):
                response = self.post("fleet_wing_delete", wing_id=self.fleet.wing_id(wing))
                self.assertEqual((response.status_code, response.json()["message"]), (409, message))
        self.assert_nothing_sent()

    def test_members_count_even_without_a_wings_read(self):
        self.pilot("Squad Pilot", squad="Alpha")
        self.read_fleet()
        FleetOperation.objects.filter(pk=self.operation.pk).update(fleet_wings=[], fleet_wings_read_at=None)

        response = self.post("fleet_wing_delete", wing_id=self.fleet.wing_id("Main"))

        self.assertEqual(response.status_code, 409)
        self.assert_nothing_sent()

    def test_esi_refuses_a_wing_that_filled_up_since_the_last_read(self):
        self.pilot("Late Pilot", squad="Logi")

        response = self.post("fleet_wing_delete", wing_id=self.fleet.wing_id("Logi"))

        text = "Could not delete the wing: ESI returned HTTP 400 (Only empty wings can be deleted)."
        self.assertEqual((response.status_code, response.json()["message"]), (502, text))
        self.assertEqual(self.entries("fleet.wing_delete")[0].new_value["result"], "failed")


class SquadTests(FleetWriteTestCase):
    def test_add_a_squad(self):
        logi = self.fleet.wing_id("Logi")

        response = self.post("fleet_squad_add", wing_id=logi)

        squad_id = self.fleet.wing(logi)["squads"][-1]["id"]
        self.assertEqual(response.json()["message"], "Squad created in “Logi”.")
        self.assertEqual(response.json()["data"], {"wing_id": logi, "squad_id": squad_id})
        [request] = self.sim.writes()
        self.assertEqual(
            (request.name, request.params["wing_id"], request.body), ("PostFleetsFleetIdWingsWingIdSquads", logi, None)
        )
        wing = self.structure()[0][1]
        self.assertEqual(wing["squads"][-1], {"id": squad_id, "name": "Squad", "pending": True})
        self.assertEqual(
            self.entries("fleet.squad_create")[0].new_value,
            {"wing_id": logi, "squad_id": squad_id, "wing": "Logi", "name": "", "result": "ok",
             "summary": "Squad created in “Logi”."},
        )

    def test_add_a_named_squad(self):
        main = self.fleet.wing_id("Main")

        response = self.post("fleet_squad_add", {"name": "Charlie"}, wing_id=main)

        self.assertEqual(response.json()["message"], "Squad “Charlie” created in “Main”.")
        self.assertEqual(
            [request.name for request in self.sim.writes()],
            ["PostFleetsFleetIdWingsWingIdSquads", "PutFleetsFleetIdSquadsSquadId"],
        )
        self.assertEqual(self.fleet.squad_id("Charlie", "Main"), response.json()["data"]["squad_id"])

    def test_a_failed_squad_rename_keeps_the_squad(self):
        self.sim.fail("PutFleetsFleetIdSquadsSquadId", status=400, error="Invalid name")

        response = self.post("fleet_squad_add", {"name": "Charlie"}, wing_id=self.fleet.wing_id("Main"))

        self.assertEqual(response.json()["level"], "warning")
        self.assertEqual(
            response.json()["message"],
            "Squad created, but renaming failed: Could not rename the squad: ESI returned HTTP 400 (Invalid name).",
        )
        self.assertEqual(len(self.fleet.wing(self.fleet.wing_id("Main"))["squads"]), 3)

    def test_eve_allows_five_squads_per_wing(self):
        main = self.fleet.wing_id("Main")
        for name in ("Charlie", "Delta", "Echo"):
            self.fleet.add_squad(main, name)
        self.read_fleet()

        response = self.post("fleet_squad_add", wing_id=main)

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["message"], "EVE allows at most 5 squads per wing.")
        self.assert_nothing_sent()

    def test_squads_need_a_wing_that_exists(self):
        response = self.post("fleet_squad_add", wing_id=4_000_000_001)

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["message"], "That wing no longer exists. The fleet view has been refreshed.")
        self.assert_nothing_sent()

    def test_rename_a_squad(self):
        alpha = self.fleet.squad_id("Alpha")

        response = self.post("fleet_squad_rename", {"name": "Tackle"}, squad_id=alpha)

        self.assertEqual(response.json()["message"], "Squad “Alpha” renamed to “Tackle”.")
        [request] = self.sim.writes()
        self.assertEqual(
            (request.name, request.params["squad_id"], request.body),
            ("PutFleetsFleetIdSquadsSquadId", alpha, {"name": "Tackle"}),
        )
        self.assertEqual(self.structure()[0][0]["squads"][0], {"id": alpha, "name": "Tackle", "pending": True})
        [entry] = self.entries("fleet.squad_rename")
        self.assertEqual((entry.old_value, entry.new_value["name"]), ({"squad_id": alpha, "name": "Alpha"}, "Tackle"))

    def test_rename_needs_a_valid_name_and_squad(self):
        alpha = self.fleet.squad_id("Alpha")
        response = self.post("fleet_squad_rename", {"name": ""}, squad_id=alpha)
        self.assertEqual((response.status_code, response.json()["message"]), (400, NAME_LENGTH_MESSAGE))
        response = self.post("fleet_squad_rename", {"name": "Tackle"}, squad_id=4_000_000_001)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["message"], "That squad no longer exists. The fleet view has been refreshed.")
        self.assert_nothing_sent()

    def test_delete_an_empty_squad(self):
        bravo = self.fleet.squad_id("Bravo")

        response = self.post("fleet_squad_delete", squad_id=bravo)

        self.assertEqual(response.json()["message"], "Squad “Bravo” deleted.")
        self.assertEqual([request.name for request in self.sim.writes()], ["DeleteFleetsFleetIdSquadsSquadId"])
        self.assertEqual([squad["name"] for squad in self.structure()[0][0]["squads"]], ["Alpha"])
        [entry] = self.entries("fleet.squad_delete")
        self.assertEqual(entry.old_value, {"wing_id": self.fleet.wing_id("Main"), "squad_id": bravo, "name": "Bravo"})

    def test_only_empty_squads_are_deleted(self):
        self.pilot("Pilot One", squad="Alpha")
        self.pilot("Pilot Two", squad="Alpha", role="squad_commander")
        self.pilot("Pilot Three", squad="Bravo")
        self.read_fleet()

        response = self.post("fleet_squad_delete", squad_id=self.fleet.squad_id("Alpha"))
        self.assertEqual(
            (response.status_code, response.json()["message"]),
            (409, "Only empty squads can be deleted. Move its 2 members first."),
        )
        response = self.post("fleet_squad_delete", squad_id=self.fleet.squad_id("Bravo"))
        self.assertEqual(response.json()["message"], "Only empty squads can be deleted. Move its member first.")
        self.assert_nothing_sent()


class WriteStateTests(FleetWriteTestCase):
    """Every change checks first whether the fleet can be changed now; nothing reaches ESI otherwise."""

    def changes(self):
        main, alpha = self.fleet.wing_id("Main"), self.fleet.squad_id("Alpha")
        return (
            ("fleet_wing_add", {}, {}),
            ("fleet_wing_rename", {"name": "A"}, {"wing_id": main}),
            ("fleet_wing_delete", {}, {"wing_id": main}),
            ("fleet_squad_add", {}, {"wing_id": main}),
            ("fleet_squad_rename", {"name": "A"}, {"squad_id": alpha}),
            ("fleet_squad_delete", {}, {"squad_id": alpha}),
        )

    def assert_refused(self, status, message, user=None):
        for name, data, kwargs in self.changes():
            with self.subTest(name=name):
                response = self.post(name, data, user=user, **kwargs)
                self.assertEqual((response.status_code, response.json()["message"]), (status, message))
        self.assert_nothing_sent()

    def test_another_change_in_progress(self):
        cache.add(f"fleetops:fleet:{self.operation.pk}:write", 1, 60)
        self.assert_refused(409, WRITE_BUSY)

    def test_a_running_job(self):
        fleet_jobs.claim_job(self.operation, kind="move", label="Move 3 members", actor=self.fc, total=3)
        self.assert_refused(409, "Wait until “Move 3 members” has finished.")

    def test_missing_write_token(self):
        self.token.scopes.filter(name__contains="write").delete()
        self.assert_refused(409, NO_WRITE_TOKEN)

    def test_boss_handover_seen_by_tracking(self):
        FleetOperation.objects.filter(pk=self.operation.pk).update(
            missing_reason=EndReason.BOSS_HANDOVER, missing_detail="Boss Alt"
        )
        self.assert_refused(
            409, "Write FC is not the fleet boss any more (current boss: Boss Alt). Fleet changes are disabled."
        )

    def test_fleets_that_are_not_active(self):
        FleetOperation.objects.filter(pk=self.operation.pk).update(status=Status.ENDING)
        self.assert_refused(409, NOT_ACTIVE)

    def test_people_who_cannot_manage_the_fleet(self):
        other = f.create_user("write-other", perms=f.FC_PERMS)
        for name, data, kwargs in self.changes():
            with self.subTest(name=name):
                self.assertEqual(self.post(name, data, user=other, **kwargs).status_code, 404)
        with self.assertRaises(FleetManageError) as ctx:
            add_wing(self.operation, actor=other)
        self.assertEqual(ctx.exception.status, 404)
        self.assert_nothing_sent()


class BossCheckTests(FleetWriteTestCase):
    """ESI answers 404 to a fleet write of anyone but the boss: one fresh check tells why."""

    def test_after_a_boss_handover_the_controls_are_disabled(self):
        alt = f.add_alt(self.fc, "Boss Alt")
        self.fleet.add_member(alt.character_id)
        self.fleet.set_boss(alt.character_id)

        response = self.post("fleet_squad_rename", {"name": "Tackle"}, squad_id=self.fleet.squad_id("Alpha"))

        text = "Write FC is not the fleet boss any more (current boss: Boss Alt). Fleet changes are disabled."
        self.assertEqual((response.status_code, response.json()["message"]), (409, text))
        self.assertEqual(self.entries("fleet.squad_rename")[0].new_value["error"], text)
        self.assertEqual(write_state(self.operation, self.fc).reason, text)
        self.assertFalse(response.json()["state"]["write"]["allowed"])
        # The next change is refused before ESI.
        writes = len(self.sim.writes())
        self.assertEqual(self.post("fleet_wing_add").status_code, 409)
        self.assertEqual(len(self.sim.writes()), writes)

    def test_a_fleet_that_is_gone(self):
        self.sim.disband(self.fleet.fleet_id)

        response = self.post("fleet_wing_add")

        self.assertEqual((response.status_code, response.json()["message"]), (409, FLEET_GONE))
        self.assertEqual(write_state(self.operation, self.fc).reason, FLEET_GONE)


class ResponseTests(FleetWriteTestCase):
    def test_form_posts_return_to_the_fleet_tab(self):
        response = self.post("fleet_wing_add", {"name": "Logi"}, headers=BROWSER_FORM)

        self.assertEqual(response.status_code, 302)
        detail = reverse("fleetops:operation_detail", kwargs={"operation_uuid": self.operation.uuid})
        self.assertEqual(response["Location"], f"{detail}#fleet")
        self.assertEqual([str(message) for message in get_messages(response.wsgi_request)], ["Wing “Logi” created."])

        refused = self.post("fleet_wing_rename", {"name": "Twelve chars"}, headers=BROWSER_FORM,
                            wing_id=self.fleet.wing_id("Main"))
        messages = list(get_messages(refused.wsgi_request))
        self.assertEqual([(str(m), m.level) for m in messages], [(NAME_LENGTH_MESSAGE, message_levels.ERROR)])

    def test_csrf_and_post_are_required(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.fc)

        self.assertEqual(client.post(self.url("fleet_wing_add"), **JSON).status_code, 403)
        self.assertEqual(client.get(self.url("fleet_wing_add")).status_code, 405)
        self.assertEqual(self.sim.writes(), [])

    def test_the_answer_carries_the_new_state(self):
        response = self.post("fleet_wing_add")

        state = response.json()["state"]
        self.assertTrue(state["write"]["allowed"])
        self.assertEqual(state["status"], "active")
        self.assertIn("no-store", response["Cache-Control"])


class ReadLagTests(FleetWriteTestCase):
    def test_a_rename_shows_until_a_late_enough_read(self):
        alpha = self.fleet.squad_id("Alpha")
        rename_squad(self.operation, alpha, "Tackle", actor=self.fc)

        # A read right after the change may still be ESI's cached answer.
        old = normalize_wings(self.fleet.structure())
        old[0]["squads"][0]["name"] = "Alpha"
        FleetOperation.objects.filter(pk=self.operation.pk).update(fleet_wings=old, fleet_wings_read_at=timezone.now())
        self.assertEqual(self.structure()[0][0]["squads"][0]["name"], "Tackle")

        self.read_fleet(at=timezone.now() + timedelta(seconds=7))
        self.assertEqual(self.structure()[0][0]["squads"][0], {"id": alpha, "name": "Tackle", "pending": False})

    def test_deleted_and_created_units_follow_the_same_rule(self):
        spare = self.fleet.add_wing("Spare", squads=0)
        self.read_fleet()
        self.assertTrue(delete_wing(self.operation, spare, actor=self.fc).ok)
        result = add_squad(self.operation, self.fleet.wing_id("Logi"), actor=self.fc, name="Heavy")

        # The stored wings are still the old ones; the fleet view already shows both changes.
        self.assertIn(spare, [wing["id"] for wing in self.operation.fleet_wings])
        wings = self.structure()[0]
        self.assertNotIn(spare, [wing["id"] for wing in wings])
        self.assertEqual(wings[1]["squads"][-1], {"id": result.data["squad_id"], "name": "Heavy", "pending": True})

        self.read_fleet(at=timezone.now() + timedelta(seconds=7))
        wings = self.structure()[0]
        self.assertNotIn(spare, [wing["id"] for wing in wings])
        self.assertEqual(wings[1]["squads"][-1], {"id": result.data["squad_id"], "name": "Heavy", "pending": False})


class ActionTemplateTests(TestCase):
    """The wing, squad and member controls of the tree, rendered for a writable fleet."""

    def setUp(self):
        super().setUp()
        self.fc = f.create_user("tpl-fc", perms=f.FC_PERMS)
        self.operation = f.create_operation(self.fc)
        self.request = RequestFactory().get("/")
        self.request.user = self.fc

    def render(self, name, **context):
        values = {"operation": self.operation, "fleet_can_write": True, "fleet_view": FleetView(), **context}
        return render_to_string(f"fleetops/operation/{name}", values, request=self.request)

    def test_add_wing_and_its_limit(self):
        html = self.render("_fleet_wing_actions.html", wing=None)
        self.assertIn(reverse("fleetops:fleet_wing_add", args=[self.operation.uuid]), html)
        self.assertIn('data-fleet-add="wing"', html)

        full = self.render("_fleet_wing_actions.html", wing=None, fleet_view=FleetView(can_add_wing=False))
        self.assertNotIn("data-fleet-add", full)
        self.assertIn("EVE allows at most 5 wings.", full)
        self.assertEqual(self.render("_fleet_wing_actions.html", wing=None, fleet_can_write=False).strip(), "")

    def test_wing_controls(self):
        wing = WingView(id=11, name="<b>Main</b>", can_add_squad=True, is_empty=False)

        html = self.render("_fleet_wing_actions.html", wing=wing)

        self.assertIn(reverse("fleetops:fleet_wing_rename", args=[self.operation.uuid, 11]), html)
        self.assertIn('data-fleet-rename', html)
        self.assertIn('name="name" maxlength="10" value="&lt;b&gt;Main&lt;/b&gt;"', html)
        self.assertIn('data-fleet-add="squad" data-wing-id="11"', html)
        self.assertIn("data-fleet-rename-open", html)
        # Only empty wings can be deleted.
        self.assertNotIn(reverse("fleetops:fleet_wing_delete", args=[self.operation.uuid, 11]), html)
        self.assertNotIn("<b>Main</b>", html)

        empty_full = self.render(
            "_fleet_wing_actions.html", wing=WingView(id=11, name="Main", can_add_squad=False, is_empty=True)
        )
        self.assertIn(reverse("fleetops:fleet_wing_delete", args=[self.operation.uuid, 11]), empty_full)
        self.assertIn('data-confirm="Delete the wing Main?"', empty_full)
        self.assertNotIn("data-fleet-add", empty_full)
        self.assertIn("EVE allows at most 5 squads per wing.", empty_full)

    def test_squad_controls(self):
        html = self.render("_fleet_squad_actions.html", squad=SquadView(id=21, name="Alpha", is_empty=True))
        self.assertIn(reverse("fleetops:fleet_squad_rename", args=[self.operation.uuid, 21]), html)
        self.assertIn(reverse("fleetops:fleet_squad_delete", args=[self.operation.uuid, 21]), html)
        busy = self.render("_fleet_squad_actions.html", squad=SquadView(id=21, name="Alpha", is_empty=False))
        self.assertNotIn(reverse("fleetops:fleet_squad_delete", args=[self.operation.uuid, 21]), busy)
        self.assertEqual(
            self.render("_fleet_squad_actions.html", squad=SquadView(id=21, name="Alpha"), fleet_can_write=False).strip(),
            "",
        )

    def test_member_menu(self):
        member = MemberView(character_id=90_123, name="Pilot", role="squad_member", wing_id=11, squad_id=21)

        html = self.render("_fleet_member_actions.html", member=member)

        self.assertIn("data-fleet-move-open", html)
        self.assertIn('data-role="squad_commander" data-wing-id="11" data-squad-id="21">Make squad commander', html)
        self.assertIn('data-role="wing_commander" data-wing-id="11">Make wing commander', html)
        self.assertIn('data-role="fleet_commander">Make fleet commander', html)
        self.assertIn(reverse("fleetops:kick_member", args=[self.operation.uuid, 90_123]), html)

        boss = MemberView(character_id=90_124, name="Boss", role="fleet_commander", wing_id=-1, squad_id=-1, protected=True)
        html = self.render("_fleet_member_actions.html", member=boss)
        self.assertNotIn("Make fleet commander", html)
        self.assertNotIn("Make wing commander", html)
        self.assertNotIn("Make squad commander", html)
        # The FC character and the boss are never kicked; moving them is allowed.
        self.assertNotIn("Kick", html)
        self.assertIn("data-fleet-move-open", html)

        leader = MemberView(character_id=90_125, name="Lead", role="squad_commander", wing_id=11, squad_id=21)
        html = self.render("_fleet_member_actions.html", member=leader)
        self.assertIn('data-role="squad_member" data-wing-id="11" data-squad-id="21">Make squad member', html)
        self.assertNotIn("Make squad commander", html)

        wing_lead = MemberView(character_id=90_126, name="Wing Lead", role="wing_commander", wing_id=11, squad_id=None)
        html = self.render("_fleet_member_actions.html", member=wing_lead)
        self.assertNotIn("Make wing commander", html)
        self.assertNotIn("Make squad", html)
        self.assertEqual(self.render("_fleet_member_actions.html", member=member, fleet_can_write=False).strip(), "")

    def test_selection_bar_and_move_dialog(self):
        html = self.render("_fleet_bulk_bar.html", fleet_can_manage=True, fleet_live=True, fleet_can_write=False)

        for text in ('id="fleet-bulk-bar"', 'id="fleet-bulk-count"', 'id="fleet-bulk-clear"', 'id="fleet-bulk-move"',
                     'id="fleet-move-form"', 'id="fleet-move-modal"', 'id="fleet-move-targets"', "csrfmiddlewaretoken"):
            with self.subTest(text=text):
                self.assertIn(text, html)
        self.assertIn(f'action="{reverse("fleetops:fleet_members_move", args=[self.operation.uuid])}"', html)
        self.assertEqual(self.render("_fleet_bulk_bar.html", fleet_can_manage=False, fleet_live=True).strip(), "")
        self.assertEqual(self.render("_fleet_bulk_bar.html", fleet_can_manage=True, fleet_live=False).strip(), "")
