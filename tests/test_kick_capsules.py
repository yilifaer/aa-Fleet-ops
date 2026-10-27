# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Which capsule pilots a capsule kick can remove."""

from unittest import mock

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from fleetops.models import FleetMemberState
from fleetops.services.fleet_controls import CAPSULES, kick_members, kickable_pod_members

from . import factories as f
from .test_operations_tracking import FLEET_ID

CAPSULE = 670
RIFTER = 587


def track(operation, character, ship_type_id=CAPSULE, active=True):
    now = timezone.now()
    return FleetMemberState.objects.create(
        operation=operation,
        character_id=character.character_id,
        character_name=character.character_name,
        ship_type_id=ship_type_id,
        first_seen=now,
        last_seen=now,
        is_active=active,
    )


class KickableCapsuleTests(TestCase):
    def setUp(self):
        self.fc = f.create_user(perms=f.FC_PERMS, main_name="Boss Main")
        self.fc_char = f.main_of(self.fc)
        self.boss_alt = f.add_alt(self.fc, "Boss Alt")
        self.operation = f.create_operation(
            self.fc, esi_fleet_id=FLEET_ID, fleet_boss_character_id=self.boss_alt.character_id
        )
        track(self.operation, self.fc_char)
        track(self.operation, self.boss_alt)
        self.pod = track(self.operation, f.create_character("Podded Pilot"))
        track(self.operation, f.create_character("Flying Pilot"), ship_type_id=RIFTER)
        track(self.operation, f.create_character("Gone Pod"), active=False)
        f.add_token(self.fc, self.fc_char)
        # The reads of the fleet after the kicks are not part of these tests.
        for target in ("fleetops.services.fleet_controls.sync_operation", "fleetops.services.fleet_refresh.schedule_refresh"):
            patcher = mock.patch(target)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client.force_login(self.fc)

    def detail(self):
        return self.client.get(reverse("fleetops:operation_detail", args=[self.operation.uuid]))

    def test_fc_and_fleet_boss_are_never_kickable(self):
        self.assertEqual([m.pk for m in kickable_pod_members(self.operation)], [self.pod.pk])

    def test_button_count_matches_the_kick(self):
        page = self.detail()

        self.assertEqual(page.context["pod_count"], 1)
        self.assertContains(page, "Kick 1 Capsule</button>")
        with mock.patch("fleetops.services.fleet_controls.kick_fleet_member") as kick:
            result = kick_members(self.operation, None, actor=self.fc, purpose=CAPSULES)

        self.assertEqual(result["target_count"], page.context["pod_count"])
        self.assertEqual([call.args[3] for call in kick.call_args_list], [self.pod.character_id])

    def test_button_is_disabled_when_only_the_fc_sits_in_a_capsule(self):
        self.pod.delete()

        page = self.detail()

        self.assertEqual(page.context["pod_count"], 0)
        self.assertContains(page, "disabled>Kick 0 Capsules</button>")

    def test_unexpected_kick_error_is_stored_without_its_text(self):
        with mock.patch(
            "fleetops.services.fleet_controls.kick_fleet_member", side_effect=RuntimeError("token=abc123secret")
        ):
            result = kick_members(self.operation, None, actor=self.fc, purpose=CAPSULES)

        self.assertEqual(result["failures"], [{"character": "Podded Pilot", "error": "Capsule kick failed unexpectedly (RuntimeError)."}])
        step = self.operation.actions.get(action="kick_capsules")
        self.assertNotIn("abc123secret", step.error_message)
        self.assertIn("Podded Pilot: Capsule kick failed unexpectedly (RuntimeError).", step.error_message)
