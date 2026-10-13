"""Kicks from the fleet page: one member, everyone outside a system, and capsule pilots."""

from unittest import mock

from django.contrib.messages import get_messages
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from kombu.exceptions import OperationalError as QueueError

from fleetops.constants import FLEET_READ_SCOPE
from fleetops.models import AuditLog, FleetMemberState, FleetOperation, OperationAction
from fleetops.providers.esi import FleetESIError
from fleetops.services.fleet_controls import (
    NO_ESI_FLEET,
    NO_WRITE_TOKEN,
    NOT_ACTIVE,
    NOT_IN_FLEET,
    PROTECTED,
    KickRefused,
    kick_member,
    kick_members,
    run_kick_batch,
)
from fleetops.services.tracking import sync_operation

from . import factories as f
from .fleet_sim import FleetSim

Status = FleetOperation.Status
JITA, AMAMAKE, RANCER = 30000142, 30002537, 30003504
CAPSULE, GUARDIAN, RIFTER = 670, 11987, 587


def message_texts(response):
    return [str(message) for message in get_messages(response.wsgi_request)]


class KickTestCase(TestCase):
    def setUp(self):
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)
        self.fc = f.create_user("fc", perms=f.FC_PERMS, main_name="FC Main")
        self.fc_char = f.main_of(self.fc)
        self.boss_alt = f.add_alt(self.fc, "Boss Alt")
        self.token = f.add_token(self.fc, self.fc_char)
        self.operation = f.create_operation(self.fc, fleet_boss_character_id=self.boss_alt.character_id)
        self.pilot = f.create_user("pilot", perms=f.MEMBER_PERMS, main_name="Pilot Main")
        self.pilot_char = f.main_of(self.pilot)

        # The FC sits in a capsule in Jita: it is never a capsule target nor kicked.
        self.state(self.fc_char, JITA, ship=(CAPSULE, "Capsule"), fleet_role="fleet_commander")
        self.state(self.boss_alt, JITA)
        self.pilot_state = self.state(self.pilot_char, AMAMAKE, accumulated_seconds=3900)
        self.pod_state = self.state(2_119_000_101, JITA, ship=(CAPSULE, "Capsule"), name="Podded Pilot")
        self.state(2_119_000_102, RANCER, name="Gone Pilot", active=False)

        self.kick = self.patch("fleetops.services.fleet_controls.kick_fleet_member")
        self.sync = self.patch("fleetops.services.fleet_controls.sync_operation")
        self.client.force_login(self.fc)

    def patch(self, target, **kwargs):
        patcher = mock.patch(target, **kwargs)
        mocked = patcher.start()
        self.addCleanup(patcher.stop)
        return mocked

    def state(self, character, system_id, *, ship=(GUARDIAN, "Guardian"), accumulated_seconds=600, **extra):
        names = {JITA: "Jita", AMAMAKE: "Amamake", RANCER: "Rancer"}
        return f.member_state(
            self.operation,
            character,
            accumulated_seconds=accumulated_seconds,
            solar_system_id=system_id,
            solar_system_name=names[system_id],
            ship_type_id=ship[0],
            ship_type_name=ship[1],
            **extra,
        )

    def kicked_ids(self):
        return [call.args[3] for call in self.kick.call_args_list]

    def detail(self, user=None):
        if user is not None:
            self.client.force_login(user)
        return self.client.get(reverse("fleetops:operation_detail", args=[self.operation.uuid]))


class SingleKickTests(KickTestCase):
    def url(self, character_id):
        return reverse("fleetops:kick_member", args=[self.operation.uuid, character_id])

    def test_kick_from_the_member_list(self):
        response = self.client.post(self.url(self.pilot_char.character_id))

        self.assertRedirects(response, reverse("fleetops:operation_detail", args=[self.operation.uuid]))
        self.kick.assert_called_once_with(
            self.fc, self.fc_char.character_id, self.operation.esi_fleet_id, self.pilot_char.character_id
        )
        self.assertIn("Kicked Pilot Main (Test Corp One).", message_texts(response))
        step = self.operation.actions.get(action="kick_member")
        self.assertEqual((step.status, step.error_message), (OperationAction.Status.SUCCESS, "Kicked Pilot Main (Test Corp One)."))
        entry = AuditLog.objects.get(action="fleet.kick_member")
        self.assertEqual(entry.actor, self.fc)
        self.assertEqual(
            entry.new_value,
            {
                "character_id": self.pilot_char.character_id,
                "character_name": "Pilot Main",
                "corporation_id": f.DEFAULT_CORP[0],
                "corporation_name": "Test Corp One",
                "kicked": True,
                "error": "",
            },
        )
        # One read of the fleet past django-esi's cache shows the kick at once.
        self.sync.assert_called_once()
        self.assertEqual(self.sync.call_args.kwargs, {"fresh": True})

    def test_a_failed_kick_shows_the_short_error(self):
        self.kick.side_effect = FleetESIError(
            "KICK_FORBIDDEN", "ESI denied permission to kick this fleet member: ESI returned HTTP 403 (Forbidden).", 403
        )

        response = self.client.post(self.url(self.pilot_char.character_id))

        text = (
            "Could not kick Pilot Main (Test Corp One): ESI denied permission to kick this fleet member: "
            "ESI returned HTTP 403 (Forbidden)."
        )
        self.assertIn(text, message_texts(response))
        step = self.operation.actions.get(action="kick_member")
        self.assertEqual((step.status, step.error_message), (OperationAction.Status.FAILED, text))
        entry = AuditLog.objects.get(action="fleet.kick_member")
        self.assertIs(entry.new_value["kicked"], False)
        self.assertIn("HTTP 403", entry.new_value["error"])
        self.sync.assert_not_called()

    def test_a_member_who_already_left(self):
        self.kick.side_effect = FleetESIError("MEMBER_NOT_FOUND", "Fleet member 1 was not found: ESI returned HTTP 404.", 404)

        response = self.client.post(self.url(self.pilot_char.character_id))

        self.assertIn("Could not kick Pilot Main (Test Corp One): already left the fleet.", message_texts(response))
        self.sync.assert_called_once()

    def test_unexpected_error_texts_are_never_shown(self):
        self.kick.side_effect = RuntimeError("token=abc123secret")

        response = self.client.post(self.url(self.pilot_char.character_id))

        self.assertIn(
            "Could not kick Pilot Main (Test Corp One): Fleet kick failed unexpectedly (RuntimeError).",
            message_texts(response),
        )
        self.assertNotIn("abc123secret", self.operation.actions.get(action="kick_member").error_message)
        self.assertNotIn("abc123secret", str(AuditLog.objects.get(action="fleet.kick_member").new_value))

    def test_the_fc_and_the_fleet_boss_are_never_kicked(self):
        for character in (self.fc_char, self.boss_alt):
            with self.subTest(character=character.character_name):
                response = self.client.post(self.url(character.character_id))
                self.assertIn(PROTECTED, message_texts(response))
        self.kick.assert_not_called()
        self.assertFalse(AuditLog.objects.filter(action="fleet.kick_member").exists())
        self.assertFalse(self.operation.actions.filter(action="kick_member").exists())

    def test_only_members_in_an_active_esi_fleet_are_kicked(self):
        for character_id in (2_119_000_102, 999):
            with self.subTest(character_id=character_id), self.assertRaisesMessage(KickRefused, NOT_IN_FLEET):
                kick_member(self.operation, character_id, actor=self.fc)
        for status in (Status.ENDING, Status.CLOSED, Status.CANCELLED):
            with self.subTest(status=status), self.assertRaisesMessage(KickRefused, NOT_ACTIVE):
                self.operation.status = status
                kick_member(self.operation, self.pilot_char.character_id, actor=self.fc)
        self.operation.status = Status.ACTIVE
        self.operation.esi_fleet_id = None
        with self.assertRaisesMessage(KickRefused, NO_ESI_FLEET):
            kick_member(self.operation, self.pilot_char.character_id, actor=self.fc)
        self.kick.assert_not_called()

    def test_closed_fleets_refuse_the_kick_on_the_page(self):
        FleetOperation.objects.filter(pk=self.operation.pk).update(status=Status.CLOSED)

        response = self.client.post(self.url(self.pilot_char.character_id))

        self.assertIn(NOT_ACTIVE, message_texts(response))
        self.kick.assert_not_called()

    def test_only_the_fleets_managers_kick(self):
        other_fc = f.create_user("other_fc", perms=f.FC_PERMS)
        for user, status in ((other_fc, 404), (self.pilot, 403)):
            with self.subTest(user=user.username):
                self.client.force_login(user)
                self.assertEqual(self.client.post(self.url(self.pilot_char.character_id)).status_code, status)
        self.kick.assert_not_called()
        self.client.force_login(f.create_user("lead", perms=f.FC_LEAD_PERMS))
        self.client.post(self.url(self.pilot_char.character_id))
        self.assertEqual(self.kicked_ids(), [self.pilot_char.character_id])


class MemberListTests(KickTestCase):
    def kick_url(self, character):
        character_id = getattr(character, "character_id", character)
        return reverse("fleetops:kick_member", args=[self.operation.uuid, character_id])

    def test_time_in_fleet_and_kick_buttons(self):
        page = self.detail()

        self.assertContains(page, "<th>Time in fleet</th>", html=False)
        self.assertContains(page, '<td class="text-nowrap">1 h 05 min</td>', html=False)
        self.assertContains(page, self.kick_url(self.pilot_char))
        self.assertContains(page, self.kick_url(self.pod_state.character_id))
        self.assertNotContains(page, self.kick_url(self.fc_char))
        self.assertNotContains(page, self.kick_url(self.boss_alt))
        self.assertContains(page, 'data-kick-label="Pilot Main (Test Corp One)"')
        self.assertContains(page, 'id="kickMemberModal"')
        self.assertTrue(page.context["kick_token_ok"])
        self.assertNotContains(page, NO_WRITE_TOKEN)
        # Members without an Alliance Auth account keep their badge.
        self.assertContains(page, "Podded Pilot <span class=\"badge bg-secondary\">Not in Alliance Auth</span>", html=False)

    def test_buttons_are_disabled_without_a_write_token(self):
        self.token.scopes.set(self.token.scopes.filter(name=FLEET_READ_SCOPE))

        page = self.detail()

        self.assertFalse(page.context["kick_token_ok"])
        self.assertContains(page, f'disabled title="{NO_WRITE_TOKEN}">Kick</button>', html=False)
        self.assertContains(page, 'id="kick-token-missing"')
        self.assertContains(page, f'title="{NO_WRITE_TOKEN}" disabled>Kick 1 Capsule</button>', html=False)

    def test_closed_fleets_show_the_time_but_no_kicks(self):
        FleetOperation.objects.filter(pk=self.operation.pk).update(status=Status.CLOSED)

        page = self.detail()

        self.assertContains(page, "1 h 05 min")
        self.assertNotContains(page, "kick-member-form")
        self.assertNotContains(page, 'id="fleet-control"')
        self.assertFalse(page.context["kick_enabled"])

    def test_viewers_who_do_not_manage_the_fleet_get_no_kicks(self):
        viewer = f.create_user("viewer", perms=f.FC_PERMS)

        page = self.detail(viewer)

        self.assertContains(page, "Pilot Main")
        self.assertNotContains(page, "kick-member-form")
        self.assertNotContains(page, 'id="fleet-control"')

    def test_members_still_see_only_their_own_view_while_the_fleet_is_open(self):
        f.add_attendance(self.operation, self.pilot)

        page = self.detail(self.pilot)

        self.assertContains(page, 'id="composition-hidden"')
        self.assertNotContains(page, "Podded Pilot")
        self.assertNotContains(page, "kick-member-form")


    def test_kick_steps_name_nobody_to_members_while_the_fleet_is_open(self):
        # VIS(2): the step texts name kicked pilots and the system the fleet holds.
        self.client.post(reverse("fleetops:kick_member", args=[self.operation.uuid, self.pod_state.character_id]))
        self.client.post(
            reverse("fleetops:kick_outside_system", args=[self.operation.uuid]),
            {"system_id": JITA, "character_ids": [self.pilot_char.character_id]},
        )

        # The FC's page shows the kick messages first.
        fc_page = self.detail(self.fc)
        member_page = self.detail(self.pilot)
        # A pilot who only registered through a PAP link can open the fleet too.
        registered = f.create_user("registered", perms=f.MEMBER_PERMS)
        f.registration(self.operation, f.main_of(registered))
        registered_page = self.detail(registered)
        FleetOperation.objects.filter(pk=self.operation.pk).update(status=Status.CLOSED)
        closed_page = self.detail(self.pilot)

        for page in (member_page, registered_page):
            self.assertEqual(page.status_code, 200)
            self.assertContains(page, "Details are shown once the fleet has closed.", count=2)
        self.assertContains(member_page, 'id="composition-hidden"')
        for text in ("Kicked Podded Pilot", "outside Jita"):
            with self.subTest(text=text):
                self.assertNotContains(member_page, text)
                self.assertNotContains(registered_page, text)
                self.assertContains(fc_page, text)
                self.assertContains(closed_page, text)


class OutsideSystemTests(KickTestCase):
    preview_url = None

    def setUp(self):
        super().setUp()
        self.preview_url = reverse("fleetops:kick_preview", args=[self.operation.uuid])
        self.post_url = reverse("fleetops:kick_outside_system", args=[self.operation.uuid])

    def test_preview_defaults_to_the_fc_system(self):
        data = self.client.get(self.preview_url).json()

        self.assertEqual(
            data["systems"],
            [{"id": JITA, "name": "Jita", "count": 3}, {"id": AMAMAKE, "name": "Amamake", "count": 1}],
        )
        self.assertEqual((data["selected_system_id"], data["fc_system_id"]), (JITA, JITA))
        self.assertEqual(
            data["members"],
            [
                {
                    "character_id": self.pilot_char.character_id,
                    "name": "Pilot Main",
                    "corporation": "Test Corp One",
                    "ship": "Guardian",
                    "system": "Amamake",
                    "system_id": AMAMAKE,
                }
            ],
        )
        self.sync.assert_called_once()
        self.assertEqual(self.sync.call_args.kwargs, {"fresh": True})

    def test_preview_of_another_system_never_lists_the_fc_or_the_boss(self):
        data = self.client.get(self.preview_url, {"system_id": AMAMAKE}).json()

        self.assertEqual(data["selected_system_id"], AMAMAKE)
        self.assertEqual([member["name"] for member in data["members"]], ["Podded Pilot"])

    def test_preview_falls_back_to_the_most_populated_system(self):
        FleetMemberState.objects.filter(operation=self.operation, character_id=self.fc_char.character_id).delete()
        for number in range(3):
            self.state(2_119_000_200 + number, AMAMAKE, name=f"Amamake Pilot {number}")

        data = self.client.get(self.preview_url).json()

        self.assertIsNone(data["fc_system_id"])
        self.assertEqual(data["selected_system_id"], AMAMAKE)
        self.assertEqual([member["name"] for member in data["members"]], ["Podded Pilot"])

    def test_the_fleet_is_read_at_most_every_ten_seconds(self):
        self.client.get(self.preview_url)
        self.client.get(self.preview_url, {"system_id": AMAMAKE})
        self.assertEqual(self.sync.call_count, 1)

        cache.delete(f"fleetops:kickpreview:{self.operation.pk}")
        self.client.get(self.preview_url)
        self.assertEqual(self.sync.call_count, 2)

    def test_closed_fleets_have_an_empty_preview(self):
        FleetOperation.objects.filter(pk=self.operation.pk).update(status=Status.CLOSED)

        data = self.client.get(self.preview_url).json()

        self.assertEqual(data, {"systems": [], "selected_system_id": None, "fc_system_id": None, "members": []})
        self.sync.assert_not_called()

    def test_selected_members_outside_the_system_are_kicked(self):
        requested = [self.pilot_char.character_id, self.pod_state.character_id, self.fc_char.character_id, 999]

        response = self.client.post(self.post_url, {"system_id": JITA, "character_ids": requested})

        self.assertEqual(self.kicked_ids(), [self.pilot_char.character_id])
        texts = message_texts(response)
        self.assertIn("Kicked 1 member outside Jita.", texts)
        self.assertIn("3 selected members skipped: no longer in the fleet or no longer a target.", texts)
        entry = AuditLog.objects.get(action="fleet.kick_outside_system")
        self.assertEqual(entry.actor, self.fc)
        self.assertEqual(
            entry.new_value,
            {
                "system_id": JITA,
                "system_name": "Jita",
                "requested": [self.pilot_char.character_id, self.pod_state.character_id, self.fc_char.character_id, 999],
                "kicked": ["Pilot Main"],
                "kicked_ids": [self.pilot_char.character_id],
                "failures": [],
                "skipped": [self.pod_state.character_id, self.fc_char.character_id, 999],
            },
        )
        step = self.operation.actions.get(action="kick_outside_system")
        self.assertEqual(step.status, OperationAction.Status.SUCCESS)
        self.assertTrue(step.error_message.startswith("Kicked 1 member(s) outside Jita. 3 skipped"))
        self.sync.assert_called_once()

    def test_a_member_who_moved_into_the_kept_system_is_spared(self):
        self.client.get(self.preview_url)
        FleetMemberState.objects.filter(pk=self.pilot_state.pk).update(solar_system_id=JITA, solar_system_name="Jita")

        response = self.client.post(self.post_url, {"system_id": JITA, "character_ids": [self.pilot_char.character_id]})

        self.kick.assert_not_called()
        self.assertIn("Nobody was kicked: none of the selected members outside Jita can be kicked now.", message_texts(response))
        self.assertFalse(AuditLog.objects.filter(action="fleet.kick_outside_system").exists())

    def test_failures_are_reported_per_member(self):
        self.state(2_119_000_301, AMAMAKE, name="Second Pilot")

        def kick(user, fc_id, fleet_id, member_id):
            if member_id != self.pilot_char.character_id:
                raise RuntimeError("secret")

        self.kick.side_effect = kick

        response = self.client.post(
            self.post_url, {"system_id": JITA, "character_ids": [self.pilot_char.character_id, 2_119_000_301]}
        )

        self.assertIn("Kicked 1 member outside Jita; 1 failed.", message_texts(response))
        step = self.operation.actions.get(action="kick_outside_system")
        self.assertEqual(step.status, OperationAction.Status.FAILED)
        self.assertIn("Second Pilot: Kick outside system failed unexpectedly (RuntimeError).", step.error_message)
        failure = AuditLog.objects.get(action="fleet.kick_outside_system").new_value["failures"][0]
        self.assertEqual(failure["character_id"], 2_119_000_301)
        self.assertNotIn("secret", step.error_message)

    def test_more_than_ten_targets_are_kicked_in_the_background(self):
        ids = [2_119_000_400 + number for number in range(11)]
        for character_id in ids:
            self.state(character_id, AMAMAKE, name=f"Far Pilot {character_id}")
        delay = self.patch("fleetops.tasks.kick_members.delay")

        response = self.client.post(self.post_url, {"system_id": JITA, "character_ids": [*ids, self.fc_char.character_id]})

        delay.assert_called_once_with(
            self.operation.pk,
            ids,
            self.fc.pk,
            "outside_system",
            {"system_id": JITA, "system_name": "Jita", "skipped": [self.fc_char.character_id]},
        )
        self.kick.assert_not_called()
        self.assertIn(
            "Kicking 11 members outside Jita in the background; results appear under Operation Actions.",
            message_texts(response),
        )

    def test_background_batches_run_through_the_task(self):
        ids = [2_119_000_500 + number for number in range(11)]
        for character_id in ids:
            self.state(character_id, AMAMAKE, name=f"Far Pilot {character_id}")

        # Celery runs eagerly in the tests, so the task kicks right away.
        self.client.post(self.post_url, {"system_id": JITA, "character_ids": ids})

        self.assertEqual(self.kicked_ids(), ids)
        entry = AuditLog.objects.get(action="fleet.kick_outside_system")
        self.assertEqual((entry.actor, entry.new_value["system_name"], len(entry.new_value["kicked"])), (self.fc, "Jita", 11))
        self.assertEqual(self.operation.actions.get(action="kick_outside_system").status, OperationAction.Status.SUCCESS)
        self.sync.assert_called_once()

    def test_without_a_queue_the_batch_runs_in_the_request(self):
        ids = [2_119_000_600 + number for number in range(11)]
        for character_id in ids:
            self.state(character_id, AMAMAKE, name=f"Far Pilot {character_id}")
        self.patch("fleetops.tasks.kick_members.delay", side_effect=QueueError("broker down"))

        result = kick_members(
            self.operation, ids, actor=self.fc, purpose="outside_system", context={"system_id": JITA, "system_name": "Jita"}
        )

        self.assertFalse(result["queued"])
        self.assertEqual(self.kicked_ids(), ids)

    def test_background_batches_check_the_members_again(self):
        moved = self.state(2_119_000_701, AMAMAKE, name="Moved Pilot")
        FleetMemberState.objects.filter(pk=moved.pk).update(solar_system_id=JITA)
        ids = [self.pilot_char.character_id, moved.character_id, self.boss_alt.character_id]

        result = run_kick_batch(self.operation.pk, ids, self.fc.pk, "outside_system", {"system_id": JITA, "system_name": "Jita", "skipped": [5]})

        self.assertEqual(self.kicked_ids(), [self.pilot_char.character_id])
        self.assertEqual(result["skipped"], [5, moved.character_id, self.boss_alt.character_id])
        self.assertEqual(AuditLog.objects.get(action="fleet.kick_outside_system").actor, self.fc)

    def test_background_batches_of_a_fleet_that_ended_kick_nobody(self):
        FleetOperation.objects.filter(pk=self.operation.pk).update(status=Status.CLOSED)

        result = run_kick_batch(self.operation.pk, [self.pilot_char.character_id], self.fc.pk, "capsules", {})

        self.kick.assert_not_called()
        self.assertEqual(result["kicked"], [])
        step = self.operation.actions.get(action="kick_capsules")
        self.assertEqual((step.status, step.error_message), (OperationAction.Status.FAILED, f"Nobody was kicked. {NOT_ACTIVE}"))
        self.assertIsNone(run_kick_batch(999_999, [1], self.fc.pk, "capsules", {}))

    def test_a_system_and_a_selection_are_needed(self):
        no_system = self.client.post(self.post_url, {"character_ids": [self.pilot_char.character_id]})
        nobody = self.client.post(self.post_url, {"system_id": JITA})

        self.assertIn("Choose the system whose members stay in the fleet.", message_texts(no_system))
        self.assertIn("Nobody was kicked: no members were selected.", message_texts(nobody))
        self.kick.assert_not_called()


class CapsuleTests(KickTestCase):
    def setUp(self):
        super().setUp()
        self.second_pod = self.state(2_119_000_801, AMAMAKE, ship=(CAPSULE, "Capsule"), name="Another Pod")
        self.url = reverse("fleetops:kick_capsules", args=[self.operation.uuid])

    def test_the_capsule_list_names_the_pilots(self):
        page = self.detail()

        self.assertContains(page, 'data-kick-names="Another Pod, Podded Pilot"')
        self.assertContains(page, "Kick 2 Capsules</button>")
        self.assertContains(page, 'id="kickListModal"')
        self.assertContains(page, reverse("fleetops:kick_preview", args=[self.operation.uuid]))

        data = self.client.get(reverse("fleetops:kick_preview", args=[self.operation.uuid]), {"mode": "capsules"}).json()
        self.assertEqual([member["name"] for member in data["members"]], ["Another Pod", "Podded Pilot"])
        self.assertEqual(data["systems"], [])

    def test_only_the_selected_capsule_pilots_are_kicked(self):
        response = self.client.post(
            self.url, {"character_ids": [self.second_pod.character_id, self.pilot_char.character_id]}
        )

        self.assertEqual(self.kicked_ids(), [self.second_pod.character_id])
        texts = message_texts(response)
        self.assertIn("Kicked 1 capsule member.", texts)
        self.assertIn("1 selected member skipped: no longer in the fleet or no longer a target.", texts)
        entry = AuditLog.objects.get(action="fleet.kick_capsules")
        self.assertEqual(entry.new_value["kicked"], ["Another Pod"])
        self.assertEqual(entry.new_value["skipped"], [self.pilot_char.character_id])

    def test_without_a_selection_every_capsule_pilot_is_kicked(self):
        response = self.client.post(self.url)

        self.assertCountEqual(self.kicked_ids(), [self.pod_state.character_id, self.second_pod.character_id])
        self.assertIn("Kicked 2 capsule members.", message_texts(response))
        self.sync.assert_called_once()

    def test_a_selection_that_is_no_longer_in_a_capsule_kicks_nobody(self):
        FleetMemberState.objects.filter(pk=self.second_pod.pk).update(ship_type_id=RIFTER, ship_type_name="Rifter")

        response = self.client.post(self.url, {"character_ids": [self.second_pod.character_id]})

        self.kick.assert_not_called()
        self.assertIn(
            "Nobody was kicked: none of the selected capsule members can be kicked now.", message_texts(response)
        )

    def test_fleets_without_an_esi_fleet_refuse(self):
        FleetOperation.objects.filter(pk=self.operation.pk).update(esi_fleet_id=None, link_only=True)

        response = self.client.post(self.url)

        self.assertIn(NO_ESI_FLEET, message_texts(response))
        self.kick.assert_not_called()


class FleetSimKickTests(TestCase):
    """A kick against the in-memory fleet: the member is gone and the page knows it at once."""

    def setUp(self):
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)
        self.fc = f.create_user("simfc", perms=f.FC_PERMS, main_name="Sim FC")
        self.fc_char = f.main_of(self.fc)
        f.add_token(self.fc, self.fc_char)
        self.sim = FleetSim()
        self.sim.install(self)
        self.fleet = self.sim.create_fleet(self.fc_char.character_id)
        self.pilot_char = f.main_of(f.create_user("simpilot", main_name="Sim Pilot"))
        self.fleet.add_member(self.pilot_char.character_id, solar_system_id=AMAMAKE)
        self.operation = f.create_operation(self.fc, esi_fleet_id=self.fleet.fleet_id)
        sync_operation(self.operation)
        self.client.force_login(self.fc)

    def test_the_kicked_member_leaves_the_list_at_once(self):
        response = self.client.post(
            reverse("fleetops:kick_member", args=[self.operation.uuid, self.pilot_char.character_id])
        )

        self.assertIn("Kicked Sim Pilot (Test Corp One).", message_texts(response))
        self.assertNotIn(self.pilot_char.character_id, self.fleet.members)
        # The member list was still in django-esi's cache; the read after the kick skipped it.
        state = FleetMemberState.objects.get(operation=self.operation, character_id=self.pilot_char.character_id)
        self.assertFalse(state.is_active)
        self.assertEqual(len(self.sim.requests_of("GetFleetsFleetIdMembers")), 2)

    def test_kicking_everyone_outside_the_fc_system(self):
        response = self.client.post(
            reverse("fleetops:kick_outside_system", args=[self.operation.uuid]),
            {"system_id": JITA, "character_ids": [self.pilot_char.character_id]},
        )

        # Without the SDE the system has no name.
        self.assertIn(f"Kicked 1 member outside {JITA}.", message_texts(response))
        self.assertEqual(list(self.fleet.members), [self.fc_char.character_id])
