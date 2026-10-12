"""Fleet boss detection with ``fleet_boss_id`` and a boss handover during a tracked fleet.

ESI (compatibility date 2025-11-06) names the fleet boss in ``fleet_boss_id`` of
GET /characters/{id}/fleet/. Only the boss can read and write /fleets/{id}/*; everyone else
gets a 404. The boss may sit in any position of the fleet.
"""

import uuid
from unittest import mock

from django.test import TestCase
from django.urls import reverse

from fleetops.models import AuditLog, FleetOperation, OperationAction
from fleetops.providers import esi as esi_provider
from fleetops.providers.esi import FleetESIError
from fleetops.services.operations import start_fleet
from fleetops.services.tracking import observe_unreadable_fleet, unreadable_fleet_warning
from fleetops.tasks import track_operation

from . import factories as f
from .test_operations_tracking import FLEET_ID, UNKNOWN_BOSS_ID, FakeESIMixin, http_error

Status = FleetOperation.Status
EndReason = FleetOperation.EndReason
OTHER_FLEET_ID = FLEET_ID + 1


class BossStartTestBase(FakeESIMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.fc = f.create_user(perms=f.FC_PERMS, main_name="Boss Main")
        self.fc_char = f.main_of(self.fc)
        f.add_token(self.fc, self.fc_char)
        self.fleet_type = f.fleet_type()
        self.esi.members[FLEET_ID] = [f.esi_member(self.fc_char)]
        f.settings(srp_auto_create=False)

    def start(self, character=None):
        character = character or self.fc_char
        return start_fleet(
            user=self.fc,
            cleaned_data={
                "operation_mode": "attendance_only",
                "fc_character_id": str(character.character_id),
                "fleet_type": self.fleet_type,
                "formup": "Jita",
            },
            request_id=uuid.uuid4(),
        )


class StartFleetBossTests(BossStartTestBase):
    def test_boss_is_accepted_in_every_fleet_position(self):
        for role in ("squad_member", "squad_commander", "wing_commander", "fleet_commander"):
            with self.subTest(role=role):
                FleetOperation.objects.all().delete()
                self.esi.put_in_fleet(self.fc_char, role=role, boss=self.fc_char)

                operation = self.start()

                self.assertEqual(operation.status, Status.ACTIVE)
                self.assertEqual(operation.fleet_boss_character_id, self.fc_char.character_id)

    def test_fleet_commander_who_is_not_the_boss_is_rejected_with_the_boss_name(self):
        boss = f.create_character("Real Boss")
        self.esi.put_in_fleet(self.fc_char, role="fleet_commander", boss=boss)

        with self.assertRaises(FleetESIError) as ctx:
            self.start()

        self.assertEqual(ctx.exception.code, "NOT_FLEET_BOSS")
        self.assertEqual(str(ctx.exception), "Boss Main is not the fleet boss. The current boss is Real Boss.")
        self.assertFalse(FleetOperation.objects.exists())

    def test_unknown_boss_is_named_by_its_character_id(self):
        self.esi.put_in_fleet(self.fc_char, role="squad_member", boss=UNKNOWN_BOSS_ID)

        with self.assertRaises(FleetESIError) as ctx:
            self.start()

        self.assertEqual(
            str(ctx.exception), f"Boss Main is not the fleet boss. The current boss is character {UNKNOWN_BOSS_ID}."
        )

    def test_alt_that_is_boss_starts_while_main_sits_in_fleet_command(self):
        alt = f.add_alt(self.fc, "Boss Alt")
        f.add_token(self.fc, alt)
        self.esi.put_in_fleet(self.fc_char, role="fleet_commander", boss=alt)
        self.esi.put_in_fleet(alt, role="squad_member", boss=alt)

        with self.assertRaises(FleetESIError):
            self.start(self.fc_char)
        operation = self.start(alt)

        self.assertEqual(operation.fc_character_id, alt.character_id)
        self.assertEqual(operation.fleet_boss_character_id, alt.character_id)


class DetectEndpointBossTests(BossStartTestBase):
    def detect(self, character=None):
        self.client.force_login(self.fc)
        character = character or self.fc_char
        return self.client.get(reverse("fleetops:detect_fleet", args=[character.character_id])).json()

    def test_boss_in_a_squad_is_reported_as_boss(self):
        self.esi.put_in_fleet(self.fc_char, role="squad_member", boss=self.fc_char)

        payload = self.detect()

        self.assertTrue(payload["ok"])
        self.assertTrue(payload["is_fleet_boss"])
        self.assertEqual(payload["role"], "squad_member")
        self.assertEqual(payload["fleet_boss_id"], self.fc_char.character_id)
        self.assertEqual(payload["message"], "")

    def test_fleet_commander_without_boss_role_names_the_boss(self):
        boss = f.create_character("Real Boss")
        self.esi.put_in_fleet(self.fc_char, role="fleet_commander", boss=boss)

        payload = self.detect()

        self.assertTrue(payload["ok"])
        self.assertFalse(payload["is_fleet_boss"])
        self.assertEqual(payload["message"], "Boss Main is not the fleet boss. The current boss is Real Boss.")

    def test_start_page_renders_the_boss_message_from_the_payload(self):
        self.client.force_login(self.fc)
        page = self.client.get(reverse("fleetops:start_fleet"))

        self.assertContains(page, "d.is_fleet_boss")
        self.assertContains(page, "d.message")


class HandoverTestBase(FakeESIMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.fc = f.create_user(perms=f.FC_PERMS, main_name="Boss Main")
        self.fc_char = f.main_of(self.fc)
        f.add_token(self.fc, self.fc_char)
        self.new_boss = f.create_character("New Boss")
        self.pilot_char = f.main_of(f.create_user(main_name="Line Pilot"))
        self.operation = f.create_operation(self.fc, esi_fleet_id=FLEET_ID)
        f.settings(auto_end_enabled=True, auto_end_missing_count=3)
        self.fleet_readable()
        self.assertEqual(self.poll(), 2)

    def poll(self):
        result = track_operation(self.operation.pk)
        self.operation.refresh_from_db()
        return result

    def fleet_readable(self):
        self.esi.put_in_fleet(self.fc_char, role="fleet_commander")
        self.esi.members[FLEET_ID] = [f.esi_member(self.fc_char), f.esi_member(self.pilot_char)]

    def hand_boss_over(self, boss=None):
        # The FC stays in fleet command, but another pilot holds the boss role: the FC token reads 404.
        self.esi.put_in_fleet(self.fc_char, role="fleet_commander", boss=boss or self.new_boss)
        self.esi.members[FLEET_ID] = http_error(404)

    def member_reads(self):
        return len(self.esi.calls_of("members"))


class BossHandoverTests(HandoverTestBase):
    def test_handover_is_detected_and_counted(self):
        self.hand_boss_over()

        self.assertEqual(self.poll(), "BOSS_HANDOVER")

        self.assertEqual(self.operation.status, Status.ACTIVE)
        self.assertEqual(self.operation.fleet_missing_count, 1)
        self.assertEqual(self.operation.missing_reason, EndReason.BOSS_HANDOVER)
        self.assertEqual(self.operation.missing_detail, "New Boss")
        self.assertEqual(self.operation.last_error, "Fleet boss handed over to New Boss.")

    def test_handover_check_reads_the_fc_fleet_without_the_response_cache(self):
        self.hand_boss_over()

        with mock.patch(
            "fleetops.services.tracking.detect_character_fleet", wraps=esi_provider.detect_character_fleet
        ) as detect:
            self.poll()

        detect.assert_called_once_with(self.fc, self.fc_char.character_id, fresh=True)

    def test_unknown_new_boss_is_named_by_its_character_id(self):
        self.hand_boss_over(boss=UNKNOWN_BOSS_ID)

        self.poll()

        self.assertEqual(self.operation.missing_detail, f"character {UNKNOWN_BOSS_ID}")

    def test_handing_the_boss_back_resets_the_count(self):
        self.hand_boss_over()
        self.poll()
        self.poll()
        self.assertEqual(self.operation.fleet_missing_count, 2)

        self.fleet_readable()
        self.assertEqual(self.poll(), 2)

        self.assertEqual(self.operation.status, Status.ACTIVE)
        self.assertEqual(self.operation.fleet_missing_count, 0)
        self.assertEqual(self.operation.missing_reason, "")
        self.assertEqual(self.operation.missing_detail, "")
        self.assertEqual(self.operation.last_error, "")

        self.hand_boss_over()
        self.poll()
        self.poll()
        self.assertEqual(self.operation.status, Status.ACTIVE)
        self.assertEqual(self.operation.fleet_missing_count, 2)

    def test_handover_reaching_the_threshold_ends_the_fleet(self):
        self.hand_boss_over()

        self.assertEqual(self.poll(), "BOSS_HANDOVER")
        self.assertEqual(self.poll(), "BOSS_HANDOVER")
        self.assertEqual(self.poll(), "auto-ended")

        self.assertEqual(self.operation.status, Status.CLOSED)
        self.assertFalse(self.operation.tracking_enabled)
        self.assertEqual(self.operation.end_reason, EndReason.BOSS_HANDOVER)
        self.assertEqual(self.operation.end_detail, "New Boss")
        self.assertEqual(self.operation.fleet_missing_count, 3)
        label = "Ended automatically: fleet boss handed over to New Boss"
        self.assertEqual(self.operation.end_reason_text, label)
        self.assertEqual(self.operation.last_error, label)
        self.assertEqual(OperationAction.objects.get(operation=self.operation, action="fleet_end").error_message, label)
        entry = AuditLog.objects.get(action="fleet.auto_end")
        self.assertIsNone(entry.actor)
        self.assertEqual(entry.reason, label)
        self.assertEqual(entry.new_value["end_reason"], "boss_handover")
        self.assertEqual(entry.new_value["end_detail"], "New Boss")

    def test_latest_observation_decides_the_end_reason(self):
        self.esi.members[FLEET_ID] = http_error(404)
        self.esi.character_fleets.clear()
        self.poll()
        self.hand_boss_over()
        self.poll()
        self.poll()

        self.assertEqual(self.operation.end_reason, EndReason.BOSS_HANDOVER)

    def test_fc_who_left_the_fleet_counts_as_fleet_gone(self):
        self.esi.put_in_fleet(self.fc_char, fleet_id=OTHER_FLEET_ID, role="fleet_commander")
        self.esi.members[FLEET_ID] = http_error(404)

        self.assertEqual(self.poll(), "FLEET_NOT_FOUND")

        self.assertEqual(self.operation.missing_reason, EndReason.FLEET_GONE)
        self.assertEqual(self.operation.fleet_missing_count, 1)

    def test_fc_who_joined_another_fleet_with_another_boss_counts_as_fleet_gone(self):
        # Only a boss change in the tracked fleet itself is a handover.
        self.esi.put_in_fleet(self.fc_char, fleet_id=OTHER_FLEET_ID, role="squad_member", boss=self.new_boss)
        self.esi.members[FLEET_ID] = http_error(404)

        self.assertEqual(self.poll(), "FLEET_NOT_FOUND")

        self.assertEqual(self.operation.missing_reason, EndReason.FLEET_GONE)
        self.assertEqual(self.operation.missing_detail, "")
        self.assertEqual(self.operation.fleet_missing_count, 1)
        self.assertEqual(self.operation.last_error, "The fleet could not be read: it no longer exists or the FC left it.")

    def test_disbanded_fleet_ends_as_fleet_gone(self):
        self.esi.character_fleets.clear()
        self.esi.members[FLEET_ID] = http_error(404)

        self.poll()
        self.poll()
        self.assertEqual(self.poll(), "auto-ended")

        self.assertEqual(self.operation.status, Status.CLOSED)
        self.assertEqual(self.operation.end_reason, EndReason.FLEET_GONE)
        self.assertEqual(self.operation.end_detail, "")
        self.assertEqual(
            self.operation.end_reason_text, "Ended automatically: the fleet no longer exists or the FC left it"
        )

    def test_transient_error_during_the_handover_check_is_not_counted(self):
        self.hand_boss_over()
        self.poll()
        for failure in (http_error(503), http_error(502), http_error(403), TimeoutError("slow")):
            with self.subTest(failure=repr(failure)):
                self.esi.character_fleets[self.fc_char.character_id] = failure

                self.assertEqual(self.poll(), "FLEET_CHECK_FAILED")

                self.assertEqual(self.operation.status, Status.ACTIVE)
                self.assertEqual(self.operation.fleet_missing_count, 1)
                self.assertEqual(self.operation.missing_reason, EndReason.BOSS_HANDOVER)

    def test_unexpected_error_during_the_handover_check_is_not_counted(self):
        self.hand_boss_over()
        with mock.patch(
            "fleetops.services.tracking.detect_character_fleet", side_effect=ValueError("odd payload")
        ):
            self.assertEqual(self.poll(), "FLEET_CHECK_FAILED")

        self.assertEqual(self.operation.fleet_missing_count, 0)
        self.assertEqual(self.operation.status, Status.ACTIVE)

    def test_observation_when_the_fc_is_still_boss_counts_as_fleet_gone(self):
        self.esi.put_in_fleet(self.fc_char, role="squad_member", boss=self.fc_char)

        self.assertEqual(observe_unreadable_fleet(self.operation), (EndReason.FLEET_GONE, ""))


class AutoEndDoubleCheckTests(HandoverTestBase):
    def test_successful_read_before_ending_cancels_the_auto_end(self):
        # 404, 404, 503, 404: the third 404 reaches the threshold, but the confirming read works.
        self.esi.character_fleets.clear()
        outcomes = iter([http_error(404), http_error(404), http_error(503), http_error(404)])
        confirming_read = [f.esi_member(self.fc_char), f.esi_member(self.pilot_char)]
        self.esi.members[FLEET_ID] = lambda: next(outcomes, confirming_read)

        self.assertEqual(self.poll(), "FLEET_NOT_FOUND")
        self.assertEqual(self.poll(), "FLEET_NOT_FOUND")
        self.assertEqual(self.poll(), "ESI_ERROR")
        self.assertEqual(self.operation.fleet_missing_count, 2)
        self.assertEqual(self.poll(), "auto-end-cancelled")

        self.assertEqual(self.operation.status, Status.ACTIVE)
        self.assertTrue(self.operation.tracking_enabled)
        self.assertEqual(self.operation.fleet_missing_count, 0)
        self.assertEqual(self.operation.missing_reason, "")
        self.assertEqual(self.operation.last_error, "")
        self.assertFalse(AuditLog.objects.filter(action="fleet.auto_end").exists())

    def test_failed_confirming_read_ends_without_another_final_sync(self):
        self.hand_boss_over()
        self.poll()
        self.poll()
        reads_before = self.member_reads()

        self.assertEqual(self.poll(), "auto-ended")

        # One read for the poll and one confirming read; the automatic end itself reads nothing.
        self.assertEqual(self.member_reads() - reads_before, 2)
        self.assertEqual(self.operation.status, Status.CLOSED)

    def test_transient_error_on_the_confirming_read_still_ends(self):
        self.esi.character_fleets.clear()
        outcomes = iter([http_error(404), http_error(404), http_error(404), http_error(503)])
        self.esi.members[FLEET_ID] = lambda: next(outcomes)

        self.poll()
        self.poll()
        self.assertEqual(self.poll(), "auto-ended")

        self.assertEqual(self.operation.status, Status.CLOSED)
        self.assertEqual(self.operation.end_reason, EndReason.FLEET_GONE)

    def test_auto_end_disabled_keeps_counting_without_ending(self):
        f.settings(auto_end_enabled=False)
        self.hand_boss_over()

        for _ in range(4):
            self.assertEqual(self.poll(), "BOSS_HANDOVER")

        self.assertEqual(self.operation.status, Status.ACTIVE)
        self.assertEqual(self.operation.fleet_missing_count, 4)


class UnreadableWarningTests(HandoverTestBase):
    def detail(self):
        self.client.force_login(self.fc)
        return self.client.get(reverse("fleetops:operation_detail", args=[self.operation.uuid]))

    def test_detail_page_shows_the_handover_and_the_remaining_checks(self):
        self.hand_boss_over()
        self.poll()

        response = self.detail()

        self.assertContains(
            response,
            "Fleet boss handed over to New Boss — this fleet will end automatically after 2 more checks "
            "unless the boss is returned. Unreadable checks in a row: 1.",
        )

    def test_warning_counts_down(self):
        self.hand_boss_over()
        self.poll()
        self.poll()

        self.assertContains(self.detail(), "after 1 more check unless the boss is returned")

    def test_fleet_gone_warning(self):
        self.esi.character_fleets.clear()
        self.esi.members[FLEET_ID] = http_error(404)
        self.poll()

        self.assertContains(
            self.detail(),
            "The fleet could not be read: it no longer exists or the FC left it — this fleet will end "
            "automatically after 2 more checks unless it can be read again.",
        )

    def test_warning_without_automatic_end(self):
        settings = f.settings(auto_end_enabled=False)
        self.hand_boss_over()
        self.poll()

        self.assertEqual(
            unreadable_fleet_warning(self.operation, settings),
            "Fleet boss handed over to New Boss — automatic end is disabled, so this fleet stays open until "
            "it is ended manually. Unreadable checks in a row: 1.",
        )

    def test_no_warning_while_the_fleet_is_readable(self):
        response = self.detail()

        self.assertEqual(response.context["unreadable_warning"], "")
        self.assertNotContains(response, "Unreadable checks")

    def test_closed_fleet_shows_the_end_reason_instead_of_a_warning(self):
        self.hand_boss_over()
        self.poll()
        self.poll()
        self.poll()

        response = self.detail()

        self.assertContains(response, "Ended automatically: fleet boss handed over to New Boss")
        self.assertNotContains(response, "Unreadable checks")
        self.assertNotContains(response, "STALE DATA")
        self.assertNotContains(response, 'class="alert alert-warning"')
