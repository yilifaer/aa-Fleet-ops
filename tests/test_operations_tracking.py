"""Tests for starting, tracking and ending fleet operations against a fake ESI."""

import unittest
import uuid
from contextlib import contextmanager
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest import mock

import requests
from django.core.cache import cache
from django.db import IntegrityError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from esi.exceptions import ESIErrorLimitException, HTTPClientError, HTTPNotModified, HTTPServerError
from esi.models import Scope

from fleetops.constants import FLEET_READ_SCOPE, FLEET_WRITE_SCOPE
from fleetops.forms import ManualFleetForm
from fleetops.models import (
    AttendanceRecord,
    AuditLog,
    ChannelPreset,
    CommsPreset,
    DiscordWebhook,
    FleetMemberEvent,
    FleetMemberState,
    FleetOperation,
    MessageTemplate,
    OperationAction,
    PingTarget,
)
from fleetops.providers import esi as esi_provider
from fleetops.providers import srp as srp_module
from fleetops.providers.esi import (
    FleetDetectionResult,
    FleetESIError,
    as_dict,
    detect_character_fleet,
    get_fleet_info,
    get_fleet_members,
    get_token,
    has_scope_token,
    kick_fleet_member,
    set_fleet_motd,
)
from fleetops.providers.pings import send_discord_webhook
from fleetops.providers.srp import SRPLinkResult
from fleetops.services.attendance import create_manual_attendance
from fleetops.services.fleet_controls import kick_all_pods
from fleetops.services.messages import render_operation_messages
from fleetops.services.operations import (
    create_manual_fleet,
    end_fleet,
    retry_motd,
    retry_ping,
    retry_srp,
    set_action,
    start_fleet,
)
from fleetops.services.tracking import sync_operation
from fleetops.tasks import schedule_active_fleet_tracking, track_operation

from . import factories as f

FLEET_ID = 1_045_000_000_001
WEBHOOK_URL = "https://discord.com/api/webhooks/123456/webhook-secret-token"

Status = FleetOperation.Status
ActionStatus = OperationAction.Status
Event = FleetMemberEvent.EventType


# ---------------------------------------------------------------------------
# Fake ESI client
# ---------------------------------------------------------------------------


def http_error(status: int):
    """Build the exception django-esi raises for an HTTP error response."""
    if status >= 500:
        return HTTPServerError(status_code=status, headers={}, data=None)
    return HTTPClientError(status_code=status, headers={}, data=None)


class FakeRequest:
    """Stand-in for a django-esi operation; ``result()`` returns or raises the configured outcome."""

    def __init__(self, outcome, etags=None, key=None):
        self.outcome = outcome
        self.etags = etags
        self.key = key

    def result(self, use_etag=True, force_refresh=False, **kwargs):
        outcome = self.outcome() if callable(self.outcome) else self.outcome
        if isinstance(outcome, BaseException):
            raise outcome
        if self.etags is not None:
            # Same contract as django-esi 9: re-reading an unchanged resource while
            # ETags are enabled raises HTTPNotModified instead of returning data.
            fingerprint = repr(outcome)
            if use_etag and not force_refresh and self.etags.get(self.key) == fingerprint:
                raise HTTPNotModified(status_code=304, headers={"ETag": f'"{hash(fingerprint)}"'})
            self.etags[self.key] = fingerprint
        return outcome


class FakeFleetsAPI:
    """In-memory replacement for ``esi.client.Fleets``."""

    def __init__(self):
        self.character_fleets = {}
        self.members = {}
        self.fleet_info = {}
        self.put_outcome = None
        self.kick_outcomes = {}
        self.calls = []
        self.put_calls = []
        self.kicks = []
        self.etags = None

    def _request(self, outcome, key):
        return FakeRequest(outcome, self.etags, key)

    def put_in_fleet(self, character, fleet_id=FLEET_ID, role="fleet_commander"):
        self.character_fleets[character.character_id] = {
            "fleet_id": fleet_id,
            "fleet_boss_id": character.character_id if role == "fleet_commander" else None,
            "role": role,
            "squad_id": -1,
            "wing_id": -1,
        }

    def calls_of(self, kind):
        return [call for call in self.calls if call[0] == kind]

    def GetCharactersCharacterIdFleet(self, *, character_id, token):
        self.calls.append(("detect", character_id, token))
        return self._request(self.character_fleets.get(character_id, http_error(404)), ("detect", character_id))

    def GetFleetsFleetId(self, *, fleet_id, token):
        self.calls.append(("info", fleet_id, token))
        return self._request(self.fleet_info.get(fleet_id, {"is_free_move": False, "motd": ""}), ("info", fleet_id))

    def GetFleetsFleetIdMembers(self, *, fleet_id, token):
        self.calls.append(("members", fleet_id, token))
        return self._request(self.members.get(fleet_id, http_error(404)), ("members", fleet_id))

    def PutFleetsFleetId(self, *, fleet_id, token, new_settings=None, body=None):
        self.put_calls.append({"fleet_id": fleet_id, "token": token, "body": new_settings if new_settings is not None else body})
        return FakeRequest(self.put_outcome)

    def DeleteFleetsFleetIdMembersMemberId(self, *, fleet_id, member_id, token):
        self.kicks.append(member_id)
        return FakeRequest(self.kick_outcomes.get(member_id))


class FakeESIMixin:
    """Routes all ESI and Discord traffic of a test case to in-memory fakes."""

    def setUp(self):
        super().setUp()
        cache.clear()
        self.esi = FakeFleetsAPI()
        client = SimpleNamespace(client=SimpleNamespace(Fleets=self.esi))
        esi_patcher = mock.patch.object(esi_provider, "esi", client)
        esi_patcher.start()
        self.addCleanup(esi_patcher.stop)
        post_patcher = mock.patch("fleetops.providers.pings.requests.post")
        self.discord_post = post_patcher.start()
        self.discord_post.return_value = mock.Mock(status_code=204, text="")
        self.addCleanup(post_patcher.stop)


def set_scopes(token, scopes):
    """Replace the scopes on an existing token without touching character ownership."""
    token.scopes.clear()
    for name in scopes:
        scope, _ = Scope.objects.get_or_create(name=name, defaults={"help_text": name})
        token.scopes.add(scope)


@contextmanager
def frozen_now(moment):
    with mock.patch("django.utils.timezone.now", return_value=moment):
        yield


def action_statuses(operation):
    return dict(OperationAction.objects.filter(operation=operation).values_list("action", "status"))


def action(operation, name):
    return OperationAction.objects.get(operation=operation, action=name)


def audit_actions(obj):
    return list(
        AuditLog.objects.filter(object_type=obj.__class__.__name__, object_id=str(obj.pk))
        .order_by("pk")
        .values_list("action", flat=True)
    )


def events(operation, character=None):
    qs = FleetMemberEvent.objects.filter(operation=operation).order_by("pk")
    if character is not None:
        qs = qs.filter(character_id=character.character_id)
    return list(qs.values_list("event_type", "old_value", "new_value"))


# ---------------------------------------------------------------------------
# Start fleet
# ---------------------------------------------------------------------------


class StartFleetTestBase(FakeESIMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.fc = f.create_user(perms=f.FC_PERMS, main_name="Boss Main")
        self.fc_char = f.main_of(self.fc)
        self.token = f.add_token(self.fc, self.fc_char)
        self.pilot = f.create_user(main_name="Line Pilot")
        self.pilot_char = f.main_of(self.pilot)
        self.fleet_type = f.fleet_type("StratOp", "2.50")
        self.comms = CommsPreset.objects.create(name="Main Comms", channel_name="Fleet 1", voice_url="mumble://voice.example/fleet1")
        self.logi = ChannelPreset.objects.create(name="Logi", channel_type=ChannelPreset.ChannelType.LOGI, channel_value="logi-chan")
        self.boost = ChannelPreset.objects.create(name="Boost", channel_type=ChannelPreset.ChannelType.BOOST, channel_value="boost-chan")
        self.webhook = DiscordWebhook.objects.create(name="Pings", webhook_url=WEBHOOK_URL)
        self.target = PingTarget.objects.create(name="Everyone", target_value="@everyone", webhook=self.webhook)

        self.esi.put_in_fleet(self.fc_char)
        self.esi.fleet_info[FLEET_ID] = {"is_free_move": True, "motd": "old"}
        self.esi.members[FLEET_ID] = [
            f.esi_member(self.fc_char, role="fleet_commander", wing_id=-1, squad_id=-1),
            f.esi_member(self.pilot_char),
        ]

        srp_patcher = mock.patch(
            "fleetops.services.operations.create_srp_link",
            return_value=SRPLinkResult("test_srp", reference="77", url="/srp/77/", created=True, message="SRP fleet created."),
        )
        self.create_srp_link = srp_patcher.start()
        self.addCleanup(srp_patcher.stop)

    def cleaned_data(self, **overrides):
        data = {
            "operation_mode": "full",
            "fc_character_id": str(self.fc_char.character_id),
            "fleet_type": self.fleet_type,
            "doctrine_name": "Ferox Fleet",
            "doctrine_external_id": "42",
            "doctrine_source": "fittings",
            "formup": "Jita IV-4",
            "comms": self.comms,
            "logi_channel": self.logi,
            "boost_channel": self.boost,
            "ping_target": self.target,
            "ping_template": None,
            "motd_template": None,
            "scheduled_at": None,
            "additional_message": "Bring cap boosters",
        }
        data.update(overrides)
        return data

    def start(self, user=None, request_id=None, **overrides):
        return start_fleet(
            user=user or self.fc,
            cleaned_data=self.cleaned_data(**overrides),
            request_id=request_id or uuid.uuid4(),
        )


class StartFleetFullModeTests(StartFleetTestBase):
    def test_creates_active_operation_with_fc_and_fleet_data(self):
        request_id = uuid.uuid4()
        operation = self.start(request_id=request_id)

        operation.refresh_from_db()
        self.assertEqual(operation.status, Status.ACTIVE)
        self.assertEqual(operation.start_request_id, request_id)
        self.assertEqual(operation.esi_fleet_id, FLEET_ID)
        self.assertEqual(operation.created_by, self.fc)
        self.assertEqual(operation.fc_user, self.fc)
        self.assertEqual(operation.fc_character_id, self.fc_char.character_id)
        self.assertEqual(operation.fc_character_name, "Boss Main")
        self.assertEqual(operation.fc_main_character_id, self.fc_char.character_id)
        self.assertEqual(operation.fc_main_character_name, "Boss Main")
        self.assertEqual(operation.fleet_boss_character_id, self.fc_char.character_id)
        self.assertEqual(operation.formup, "Jita IV-4")
        self.assertEqual(operation.comms, self.comms)
        self.assertEqual(operation.logi_channel, self.logi)
        self.assertEqual(operation.boost_channel, self.boost)
        self.assertEqual(operation.ping_target, self.target)
        self.assertEqual(operation.additional_message, "Bring cap boosters")
        self.assertTrue(operation.tracking_enabled)
        self.assertTrue(operation.send_ping)
        self.assertFalse(operation.is_manual)
        self.assertEqual(operation.attendance_multiplier, 1)
        self.assertIsNotNone(operation.started_at)
        self.assertIsNone(operation.ended_at)
        self.assertEqual(operation.last_error, "")

    def test_snapshots_fleet_type_weight_and_doctrine(self):
        operation = self.start()
        self.fleet_type.point_weight = Decimal("9.00")
        self.fleet_type.save()

        operation.refresh_from_db()
        self.assertEqual(operation.fleet_point_weight_snapshot, Decimal("2.50"))
        self.assertEqual(operation.doctrine_name, "Ferox Fleet")
        self.assertEqual(operation.doctrine_external_id, "42")
        self.assertEqual(operation.doctrine_source, "fittings")

    def test_custom_doctrine_source_defaults_when_missing(self):
        data = self.cleaned_data()
        for key in ("doctrine_name", "doctrine_external_id", "doctrine_source"):
            data.pop(key)
        operation = start_fleet(user=self.fc, cleaned_data=data, request_id=uuid.uuid4())
        self.assertEqual(operation.doctrine_name, "")
        self.assertEqual(operation.doctrine_source, "custom")

    def test_renders_ping_and_motd_from_operation_data(self):
        operation = self.start()

        for expected in ("@everyone", "StratOp", "Boss Main", "Jita IV-4", "Ferox Fleet", "Main Comms",
                         "mumble://voice.example/fleet1", "logi-chan", "boost-chan", "Bring cap boosters"):
            self.assertIn(expected, operation.ping_text)
        self.assertTrue(operation.motd_text.startswith("<b>StratOp</b>"))
        for expected in ("FC: Boss Main", "Form Up: Jita IV-4", "Doctrine: Ferox Fleet", "Logi: logi-chan", "Boost: boost-chan"):
            self.assertIn(expected, operation.motd_text)

    def test_sends_discord_ping_to_configured_webhook(self):
        operation = self.start()

        self.discord_post.assert_called_once()
        args, kwargs = self.discord_post.call_args
        self.assertEqual(args[0], WEBHOOK_URL)
        self.assertEqual(kwargs["json"], {"content": operation.ping_text})
        self.assertIn("timeout", kwargs)
        self.assertEqual(action(operation, "discord_ping").status, ActionStatus.SUCCESS)

    def test_discord_error_response_is_recorded_and_fleet_still_starts(self):
        self.discord_post.return_value = mock.Mock(status_code=500, text="Discord is having a bad day")

        operation = self.start()

        ping = action(operation, "discord_ping")
        self.assertEqual(ping.status, ActionStatus.FAILED)
        self.assertIn("Discord is having a bad day", ping.error_message)
        self.assertEqual(operation.status, Status.ACTIVE)
        self.assertEqual(action(operation, "tracking_start").status, ActionStatus.SUCCESS)

    def test_discord_network_error_is_recorded_and_fleet_still_starts(self):
        self.discord_post.side_effect = requests.Timeout("read timed out")

        operation = self.start()

        self.assertEqual(action(operation, "discord_ping").status, ActionStatus.FAILED)
        self.assertEqual(operation.status, Status.ACTIVE)

    def test_without_webhook_ping_is_not_sent_but_kept_for_manual_copy(self):
        self.target.webhook = None
        self.target.save()

        operation = self.start()

        self.discord_post.assert_not_called()
        self.assertNotEqual(action(operation, "discord_ping").status, ActionStatus.SUCCESS)
        self.assertIn("@everyone", operation.ping_text)
        self.assertEqual(operation.status, Status.ACTIVE)

    def test_inactive_webhook_is_never_called(self):
        self.webhook.is_active = False
        self.webhook.save()

        operation = self.start()

        self.discord_post.assert_not_called()
        self.assertNotEqual(action(operation, "discord_ping").status, ActionStatus.SUCCESS)

    def test_writes_motd_through_esi_preserving_free_move(self):
        operation = self.start()

        self.assertEqual(len(self.esi.put_calls), 1)
        put = self.esi.put_calls[0]
        self.assertEqual(put["fleet_id"], FLEET_ID)
        self.assertEqual(put["token"], self.token)
        self.assertEqual(put["body"], {"motd": operation.motd_text, "is_free_move": True})
        self.assertEqual(action(operation, "motd_update").status, ActionStatus.SUCCESS)

    def test_motd_failure_is_recorded_and_fleet_still_starts(self):
        self.esi.put_outcome = http_error(403)

        operation = self.start()

        motd = action(operation, "motd_update")
        self.assertEqual(motd.status, ActionStatus.FAILED)
        self.assertIn("MOTD", motd.error_message)
        self.assertEqual(operation.status, Status.ACTIVE)
        self.assertEqual(action(operation, "tracking_start").status, ActionStatus.SUCCESS)

    def test_read_only_token_tracks_but_motd_fails_for_missing_write_scope(self):
        set_scopes(self.token, [FLEET_READ_SCOPE])

        operation = self.start()

        self.assertEqual(self.esi.put_calls, [])
        self.assertEqual(action(operation, "motd_update").status, ActionStatus.FAILED)
        self.assertEqual(action(operation, "tracking_start").status, ActionStatus.SUCCESS)
        self.assertEqual(operation.status, Status.ACTIVE)

    def test_starts_tracking_immediately(self):
        operation = self.start()

        states = {s.character_id: s for s in operation.member_states.all()}
        self.assertEqual(set(states), {self.fc_char.character_id, self.pilot_char.character_id})
        self.assertEqual(states[self.fc_char.character_id].fleet_role, "fleet_commander")
        self.assertEqual(
            FleetMemberEvent.objects.filter(operation=operation, event_type=Event.JOIN).count(), 2
        )
        self.assertTrue(
            AttendanceRecord.objects.filter(operation=operation, character_id=self.pilot_char.character_id).exists()
        )
        operation.refresh_from_db()
        self.assertIsNotNone(operation.last_esi_update)
        self.assertEqual(action(operation, "tracking_start").status, ActionStatus.SUCCESS)

    def test_tracking_failure_at_start_is_recorded_but_fleet_is_active(self):
        self.esi.members[FLEET_ID] = http_error(502)

        operation = self.start()

        operation.refresh_from_db()
        self.assertEqual(operation.status, Status.ACTIVE)
        self.assertTrue(operation.tracking_enabled)
        self.assertEqual(action(operation, "tracking_start").status, ActionStatus.FAILED)
        self.assertIn("Could not read fleet members", operation.last_error)

    def test_successful_srp_link_is_stored(self):
        operation = self.start()

        self.create_srp_link.assert_called_once()
        operation.refresh_from_db()
        self.assertEqual(operation.srp_provider, "test_srp")
        self.assertEqual(operation.srp_reference, "77")
        self.assertEqual(operation.srp_url, "/srp/77/")
        self.assertEqual(operation.srp_error, "")
        self.assertEqual(action(operation, "srp_link").status, ActionStatus.SUCCESS)

    def test_srp_provider_crash_does_not_block_fleet_start(self):
        self.create_srp_link.side_effect = RuntimeError("SRP backend down")

        operation = self.start()

        operation.refresh_from_db()
        self.assertEqual(operation.status, Status.ACTIVE)
        self.assertEqual(operation.srp_error, "SRP backend down")
        self.assertEqual(action(operation, "srp_link").status, ActionStatus.FAILED)

    def test_srp_without_available_provider_is_skipped(self):
        with mock.patch.dict(srp_module._PROVIDERS, {}, clear=True), mock.patch(
            "fleetops.services.operations.create_srp_link", srp_module.create_srp_link
        ):
            operation = self.start()

        operation.refresh_from_db()
        self.assertEqual(operation.status, Status.ACTIVE)
        self.assertEqual(operation.srp_reference, "")
        self.assertIn("No supported SRP provider", operation.srp_error)
        self.assertEqual(action(operation, "srp_link").status, ActionStatus.SKIPPED)

    def test_srp_auto_create_disabled_skips_srp(self):
        f.settings(srp_auto_create=False)

        operation = self.start()

        self.create_srp_link.assert_not_called()
        self.assertEqual(action(operation, "srp_link").status, ActionStatus.SKIPPED)

    def test_records_every_step_as_operation_action(self):
        operation = self.start()

        self.assertEqual(
            action_statuses(operation),
            {
                "fleet_detection": ActionStatus.SUCCESS,
                "discord_ping": ActionStatus.SUCCESS,
                "motd_update": ActionStatus.SUCCESS,
                "tracking_start": ActionStatus.SUCCESS,
                "srp_link": ActionStatus.SUCCESS,
            },
        )

    def test_fc_may_start_with_an_owned_alt_in_another_corporation(self):
        alt = f.add_alt(self.fc, "Boss Alt", corporation=f.OTHER_CORP, alliance=f.FOREIGN_ALLIANCE)
        f.add_token(self.fc, alt)
        self.esi.put_in_fleet(alt)
        self.esi.members[FLEET_ID] = [f.esi_member(alt, role="fleet_commander")]

        operation = self.start(fc_character_id=str(alt.character_id))

        self.assertEqual(operation.fc_character_id, alt.character_id)
        self.assertEqual(operation.fc_character_name, "Boss Alt")
        self.assertEqual(operation.fleet_boss_character_id, alt.character_id)
        self.assertEqual(operation.fc_main_character_id, self.fc_char.character_id)
        attendance = AttendanceRecord.objects.get(operation=operation, character_id=alt.character_id)
        self.assertEqual(attendance.main_character_id, self.fc_char.character_id)
        self.assertEqual(attendance.corporation_id, f.DEFAULT_CORP[0])


class StartFleetAttendanceOnlyTests(StartFleetTestBase):
    def test_skips_ping_motd_and_srp_but_tracks(self):
        operation = self.start(operation_mode="attendance_only")

        self.discord_post.assert_not_called()
        self.assertEqual(self.esi.put_calls, [])
        self.create_srp_link.assert_not_called()
        self.assertEqual(
            action_statuses(operation),
            {
                "fleet_detection": ActionStatus.SUCCESS,
                "discord_ping": ActionStatus.SKIPPED,
                "motd_update": ActionStatus.SKIPPED,
                "tracking_start": ActionStatus.SUCCESS,
                "srp_link": ActionStatus.SKIPPED,
            },
        )
        operation.refresh_from_db()
        self.assertEqual(operation.status, Status.ACTIVE)
        self.assertFalse(operation.send_ping)
        self.assertTrue(operation.tracking_enabled)
        self.assertEqual(operation.member_states.count(), 2)
        self.assertEqual(operation.srp_reference, "")

    def test_ping_and_motd_previews_remain_available(self):
        operation = self.start(operation_mode="attendance_only")

        self.assertIn("@everyone", operation.ping_text)
        self.assertIn("Jita IV-4", operation.motd_text)

    def test_still_requires_fleet_boss(self):
        self.esi.put_in_fleet(self.fc_char, role="squad_member")

        with self.assertRaises(FleetESIError) as ctx:
            self.start(operation_mode="attendance_only")

        self.assertEqual(ctx.exception.code, "NOT_FLEET_BOSS")
        self.assertFalse(FleetOperation.objects.exists())

    def test_works_with_read_only_token(self):
        set_scopes(self.token, [FLEET_READ_SCOPE])

        operation = self.start(operation_mode="attendance_only")

        self.assertEqual(operation.status, Status.ACTIVE)
        self.assertEqual(action(operation, "tracking_start").status, ActionStatus.SUCCESS)


class StartFleetValidationTests(StartFleetTestBase):
    def assert_start_rejected(self, code, **overrides):
        with self.assertRaises(FleetESIError) as ctx:
            self.start(**overrides)
        self.assertEqual(ctx.exception.code, code)
        self.assertFalse(FleetOperation.objects.exists())
        self.discord_post.assert_not_called()
        return ctx.exception

    def test_character_of_another_user_is_rejected(self):
        other = f.create_user(perms=f.FC_PERMS)
        other_char = f.main_of(other)
        f.add_token(other, other_char)
        self.esi.put_in_fleet(other_char)

        with self.assertRaises(PermissionError):
            self.start(fc_character_id=str(other_char.character_id))

        self.assertEqual(self.esi.calls, [])
        self.assertFalse(FleetOperation.objects.exists())

    def test_unknown_character_is_rejected(self):
        with self.assertRaises(PermissionError):
            self.start(fc_character_id="99999999")

    def test_missing_token_is_reported(self):
        set_scopes(self.token, [])

        error = self.assert_start_rejected("MISSING_READ_SCOPE")

        self.assertIn("token", str(error))
        self.assertEqual(self.esi.calls, [])

    def test_token_without_fleet_scope_is_reported(self):
        set_scopes(self.token, ["esi-skills.read_skills.v1"])

        self.assert_start_rejected("MISSING_READ_SCOPE")

    def test_character_not_in_fleet(self):
        self.esi.character_fleets.clear()

        error = self.assert_start_rejected("NOT_IN_FLEET")

        self.assertIn("not in a fleet", str(error))

    def test_character_that_is_not_fleet_boss(self):
        self.esi.put_in_fleet(self.fc_char, role="wing_commander")

        error = self.assert_start_rejected("NOT_FLEET_BOSS")

        self.assertIn("wing_commander", str(error))

    def test_esi_server_error_during_detection(self):
        self.esi.character_fleets[self.fc_char.character_id] = http_error(502)

        error = self.assert_start_rejected("ESI_ERROR")

        self.assertEqual(error.status_code, 502)

    def test_esi_forbidden_during_detection(self):
        self.esi.character_fleets[self.fc_char.character_id] = http_error(403)

        self.assert_start_rejected("ESI_FORBIDDEN")


class StartFleetIdempotencyTests(StartFleetTestBase):
    def test_same_request_id_returns_existing_operation(self):
        request_id = uuid.uuid4()

        first = self.start(request_id=request_id)
        second = self.start(request_id=request_id)

        self.assertEqual(first.pk, second.pk)
        self.assertEqual(FleetOperation.objects.count(), 1)
        self.assertEqual(self.discord_post.call_count, 1)
        self.assertEqual(len(self.esi.put_calls), 1)
        self.assertEqual(OperationAction.objects.get(operation=first, action="discord_ping").attempts, 1)

    def test_different_request_ids_create_separate_operations(self):
        self.start()
        self.start()

        self.assertEqual(FleetOperation.objects.count(), 2)

    def test_double_submit_through_view_creates_one_operation(self):
        self.client.force_login(self.fc)
        payload = {
            "request_id": str(uuid.uuid4()),
            "operation_mode": "full",
            "fc_character_id": str(self.fc_char.character_id),
            "fleet_type": str(self.fleet_type.pk),
            "doctrine_choice": "",
            "custom_doctrine": "Ferox Fleet",
            "formup": "Jita IV-4",
            "ping_target": str(self.target.pk),
        }

        first = self.client.post(reverse("fleetops:start_fleet"), payload)
        second = self.client.post(reverse("fleetops:start_fleet"), payload)

        self.assertEqual(FleetOperation.objects.count(), 1)
        operation = FleetOperation.objects.get()
        expected = reverse("fleetops:operation_detail", kwargs={"operation_uuid": operation.uuid})
        self.assertRedirects(first, expected, fetch_redirect_response=False)
        self.assertRedirects(second, expected, fetch_redirect_response=False)
        self.assertEqual(operation.doctrine_name, "Ferox Fleet")
        self.assertEqual(operation.doctrine_source, "custom")
        self.assertEqual(self.discord_post.call_count, 1)

    # Known issue: a concurrent duplicate submit fails with an IntegrityError instead of returning the started operation
    @unittest.expectedFailure
    def test_concurrent_duplicate_submit_returns_the_operation_started_first(self):
        request_id = uuid.uuid4()
        competing = {}

        def detect_while_other_request_commits(user, character_id):
            # The other request finishes while this one waits on ESI fleet detection.
            competing["operation"] = f.create_operation(self.fc, start_request_id=request_id, esi_fleet_id=FLEET_ID)
            return FleetDetectionResult(fleet_id=FLEET_ID, role="fleet_commander")

        with mock.patch("fleetops.services.operations.detect_character_fleet", side_effect=detect_while_other_request_commits):
            try:
                operation = self.start(request_id=request_id)
            except IntegrityError as exc:
                self.fail(f"Duplicate submit surfaced a database error: {exc}")

        self.assertEqual(operation.pk, competing["operation"].pk)
        self.assertEqual(FleetOperation.objects.count(), 1)


class StartFleetViewErrorTests(StartFleetTestBase):
    def test_non_boss_error_is_shown_on_the_form(self):
        self.esi.put_in_fleet(self.fc_char, role="squad_member")
        self.client.force_login(self.fc)

        response = self.client.post(
            reverse("fleetops:start_fleet"),
            {
                "request_id": str(uuid.uuid4()),
                "operation_mode": "full",
                "fc_character_id": str(self.fc_char.character_id),
                "fleet_type": str(self.fleet_type.pk),
                "formup": "Jita",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "not fleet_commander")
        self.assertFalse(FleetOperation.objects.exists())


# ---------------------------------------------------------------------------
# Message rendering
# ---------------------------------------------------------------------------


class RenderOperationMessagesTests(TestCase):
    def setUp(self):
        self.fc = f.create_user(perms=f.FC_PERMS, main_name="Boss Main")
        self.operation = f.create_operation(self.fc, doctrine_name="", additional_message="")

    def test_default_templates_without_optional_data(self):
        ping, motd = render_operation_messages(self.operation)

        self.assertIn("**StratOp**", ping)
        self.assertIn("FC: Boss Main", ping)
        self.assertIn("Form Up: Jita", ping)
        self.assertIn("Doctrine: None / Custom", ping)
        self.assertIn("Time: Now", ping)
        self.assertNotIn("Logi:", ping)
        self.assertIn("<b>StratOp</b>", motd)

    def test_scheduled_time_is_rendered(self):
        self.operation.scheduled_at = timezone.now().replace(year=2026, month=10, day=6, hour=19, minute=30, second=0, microsecond=0)

        ping, _ = render_operation_messages(self.operation)

        self.assertIn("Time: 2026-10-06T19:30", ping)

    def test_operation_template_overrides_default_template(self):
        MessageTemplate.objects.create(
            name="Default", template_type=MessageTemplate.TemplateType.PING, content="DEFAULT {{ fc }}", is_default=True
        )
        chosen = MessageTemplate.objects.create(
            name="Chosen", template_type=MessageTemplate.TemplateType.PING, content="CHOSEN {{ formup }}"
        )

        ping, _ = render_operation_messages(self.operation)
        self.assertEqual(ping, "DEFAULT Boss Main")

        self.operation.ping_template = chosen
        ping, _ = render_operation_messages(self.operation)
        self.assertEqual(ping, "CHOSEN Jita")

    def test_inactive_default_template_is_ignored(self):
        MessageTemplate.objects.create(
            name="Old", template_type=MessageTemplate.TemplateType.MOTD, content="OLD", is_default=True, is_active=False
        )

        _, motd = render_operation_messages(self.operation)

        self.assertIn("<b>StratOp</b>", motd)

    def test_script_tags_are_neutralised_in_motd(self):
        self.operation.additional_message = "<script>alert(1)</script>"

        _, motd = render_operation_messages(self.operation)

        self.assertNotIn("<script", motd)
        self.assertNotIn("</script", motd)


# ---------------------------------------------------------------------------
# Tracking sync
# ---------------------------------------------------------------------------


class TrackingTestBase(FakeESIMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.fc = f.create_user(perms=f.FC_PERMS, main_name="Boss Main")
        self.fc_char = f.main_of(self.fc)
        self.token = f.add_token(self.fc, self.fc_char)
        self.operation = f.create_operation(self.fc, esi_fleet_id=FLEET_ID)
        self.pilot = f.create_user(main_name="Line Pilot")
        self.pilot_char = f.main_of(self.pilot)

    def set_members(self, *rows):
        self.esi.members[FLEET_ID] = list(rows)

    def state(self, character):
        return FleetMemberState.objects.get(operation=self.operation, character_id=character.character_id)


class SyncOperationTests(TrackingTestBase):
    def test_first_sync_creates_member_state_join_event_and_attendance(self):
        self.set_members(
            f.esi_member(self.pilot_char, role="squad_member", ship_type_id=17738, solar_system_id=30002187, wing_id=11, squad_id=22)
        )
        moment = timezone.now()

        with frozen_now(moment):
            count = sync_operation(self.operation)

        self.assertEqual(count, 1)
        state = self.state(self.pilot_char)
        self.assertEqual(state.character_name, "Line Pilot")
        self.assertEqual(state.main_character_id, self.pilot_char.character_id)
        self.assertEqual(state.main_character_name, "Line Pilot")
        self.assertEqual(state.auth_user, self.pilot)
        self.assertEqual(state.corporation_id, f.DEFAULT_CORP[0])
        self.assertEqual(state.alliance_id, f.DEFAULT_ALLIANCE[0])
        self.assertEqual(state.ship_type_id, 17738)
        self.assertEqual(state.solar_system_id, 30002187)
        self.assertEqual(state.fleet_role, "squad_member")
        self.assertEqual(state.wing_id, 11)
        self.assertEqual(state.squad_id, 22)
        self.assertEqual(state.first_seen, moment)
        self.assertEqual(state.last_seen, moment)
        self.assertTrue(state.is_active)
        self.assertIsNone(state.left_at)
        self.assertEqual(events(self.operation), [(Event.JOIN, "", "")])

        record = AttendanceRecord.objects.get(operation=self.operation, character_id=self.pilot_char.character_id)
        self.assertEqual(record.source, AttendanceRecord.Source.AUTOMATIC)
        self.assertEqual(record.auth_user, self.pilot)
        self.assertTrue(record.granted)
        self.assertEqual(record.attendance_value, 1)

        self.operation.refresh_from_db()
        self.assertEqual(self.operation.last_esi_update, moment)

    def test_unowned_character_is_tracked_without_attendance(self):
        stranger = f.create_character("Neutral Pilot")
        unknown_row = {**f.esi_member(stranger), "character_id": 95_555_555}
        self.set_members(f.esi_member(stranger), unknown_row)

        sync_operation(self.operation)

        self.assertEqual(self.operation.member_states.count(), 2)
        self.assertIsNone(self.state(stranger).auth_user)
        unknown = FleetMemberState.objects.get(operation=self.operation, character_id=95_555_555)
        self.assertEqual(unknown.character_name, "Character 95555555")
        self.assertFalse(AttendanceRecord.objects.filter(operation=self.operation).exists())

    def test_alt_attendance_is_credited_to_main_and_main_corporation(self):
        alt = f.add_alt(self.pilot, "Pilot Alt", corporation=f.OTHER_CORP, alliance=f.FOREIGN_ALLIANCE)
        self.set_members(f.esi_member(alt))

        sync_operation(self.operation)

        record = AttendanceRecord.objects.get(operation=self.operation, character_id=alt.character_id)
        self.assertEqual(record.character_name, "Pilot Alt")
        self.assertEqual(record.main_character_id, self.pilot_char.character_id)
        self.assertEqual(record.auth_user, self.pilot)
        self.assertEqual(record.corporation_id, f.DEFAULT_CORP[0])
        self.assertEqual(self.state(alt).main_character_id, self.pilot_char.character_id)

    def test_unchanged_member_creates_no_events_and_keeps_first_seen(self):
        row = f.esi_member(self.pilot_char)
        self.set_members(row)
        start = timezone.now()
        with frozen_now(start):
            sync_operation(self.operation)

        later = start + timedelta(minutes=1)
        with frozen_now(later):
            sync_operation(self.operation)
            sync_operation(self.operation)

        self.assertEqual(events(self.operation), [(Event.JOIN, "", "")])
        state = self.state(self.pilot_char)
        self.assertEqual(state.first_seen, start)
        self.assertEqual(state.last_seen, later)
        record = AttendanceRecord.objects.get(operation=self.operation, character_id=self.pilot_char.character_id)
        self.assertEqual(record.first_seen, start)
        self.assertEqual(record.last_seen, later)
        self.assertEqual(AttendanceRecord.objects.filter(operation=self.operation).count(), 1)

    def test_leave_and_rejoin(self):
        row = f.esi_member(self.pilot_char)
        start = timezone.now()
        self.set_members(row)
        with frozen_now(start):
            sync_operation(self.operation)

        left = start + timedelta(minutes=1)
        self.set_members()
        with frozen_now(left):
            sync_operation(self.operation)

        state = self.state(self.pilot_char)
        self.assertFalse(state.is_active)
        self.assertEqual(state.left_at, left)
        self.assertEqual(state.last_seen, left)

        # Staying away does not repeat the leave event.
        sync_operation(self.operation)

        back = start + timedelta(minutes=3)
        self.set_members(row)
        with frozen_now(back):
            sync_operation(self.operation)

        state = self.state(self.pilot_char)
        self.assertTrue(state.is_active)
        self.assertIsNone(state.left_at)
        self.assertEqual(state.first_seen, start)
        self.assertEqual(state.last_seen, back)
        self.assertEqual(
            [e[0] for e in events(self.operation)],
            [Event.JOIN, Event.LEAVE, Event.REJOIN],
        )
        self.assertEqual(AttendanceRecord.objects.filter(operation=self.operation).count(), 1)

    def test_each_meaningful_change_creates_one_event(self):
        base = f.esi_member(self.pilot_char, ship_type_id=11987, solar_system_id=30000142, role="squad_member", wing_id=1, squad_id=1)
        self.set_members(base)
        sync_operation(self.operation)

        changes = [
            ({"ship_type_id": 17738}, (Event.SHIP_CHANGE, "11987", "17738")),
            ({"solar_system_id": 30002187}, (Event.SYSTEM_CHANGE, "30000142", "30002187")),
            ({"role": "squad_commander"}, (Event.ROLE_CHANGE, "squad_member", "squad_commander")),
            ({"wing_id": 2}, (Event.WING_CHANGE, "1", "2")),
            ({"squad_id": 3}, (Event.SQUAD_CHANGE, "1", "3")),
        ]
        current = dict(base)
        for overrides, expected in changes:
            with self.subTest(change=expected[0]):
                FleetMemberEvent.objects.filter(operation=self.operation).delete()
                current.update(overrides)
                self.set_members(dict(current))

                sync_operation(self.operation)

                self.assertEqual(events(self.operation), [expected])

        state = self.state(self.pilot_char)
        self.assertEqual(
            (state.ship_type_id, state.solar_system_id, state.fleet_role, state.wing_id, state.squad_id),
            (17738, 30002187, "squad_commander", 2, 3),
        )

    def test_changes_while_away_are_reported_with_rejoin(self):
        self.set_members(f.esi_member(self.pilot_char, ship_type_id=11987))
        sync_operation(self.operation)
        self.set_members()
        sync_operation(self.operation)
        FleetMemberEvent.objects.filter(operation=self.operation).delete()

        self.set_members(f.esi_member(self.pilot_char, ship_type_id=670))
        sync_operation(self.operation)

        self.assertEqual(
            events(self.operation),
            [(Event.REJOIN, "", ""), (Event.SHIP_CHANGE, "11987", "670")],
        )

    def test_attendance_limit_caps_extra_alts(self):
        f.settings(attendance_limit=1)
        alt = f.add_alt(self.pilot, "Pilot Alt")
        self.set_members(f.esi_member(self.pilot_char), f.esi_member(alt))

        sync_operation(self.operation)

        records = AttendanceRecord.objects.filter(operation=self.operation, auth_user=self.pilot)
        self.assertEqual(records.count(), 2)
        self.assertEqual(records.filter(granted=True).count(), 1)
        self.assertEqual(records.filter(capped=True, granted=False).count(), 1)

    def test_successful_sync_resets_missing_counter_and_error(self):
        self.operation.fleet_missing_count = 2
        self.operation.last_error = "Fleet no longer exists."
        self.operation.save()
        self.set_members(f.esi_member(self.pilot_char))

        sync_operation(self.operation)

        self.operation.refresh_from_db()
        self.assertEqual(self.operation.fleet_missing_count, 0)
        self.assertEqual(self.operation.last_error, "")

    def test_reads_members_with_the_fc_token(self):
        self.set_members(f.esi_member(self.pilot_char))

        sync_operation(self.operation)

        self.assertEqual(self.esi.calls_of("members"), [("members", FLEET_ID, self.token)])

    def test_operation_without_fleet_id_cannot_sync(self):
        operation = f.create_operation(self.fc, esi_fleet_id=0)

        with self.assertRaises(FleetESIError) as ctx:
            sync_operation(operation)

        self.assertEqual(ctx.exception.code, "NO_FLEET_ID")
        self.assertEqual(self.esi.calls, [])

    def test_esi_error_leaves_tracked_state_untouched(self):
        self.set_members(f.esi_member(self.pilot_char))
        sync_operation(self.operation)
        self.esi.members[FLEET_ID] = http_error(502)

        with self.assertRaises(FleetESIError):
            sync_operation(self.operation)

        self.assertTrue(self.state(self.pilot_char).is_active)
        self.assertEqual([e[0] for e in events(self.operation)], [Event.JOIN])


# ---------------------------------------------------------------------------
# Celery tasks
# ---------------------------------------------------------------------------


class ScheduleTrackingTests(FakeESIMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.fc = f.create_user(perms=f.FC_PERMS)
        delay_patcher = mock.patch.object(track_operation, "delay")
        self.delay = delay_patcher.start()
        self.addCleanup(delay_patcher.stop)

    def queued_ids(self):
        return {call.args[0] for call in self.delay.call_args_list}

    def test_queues_only_active_tracked_fleets_that_are_due(self):
        f.settings(tracking_interval=120)
        now = timezone.now()
        never_synced = f.create_operation(self.fc)
        overdue = f.create_operation(self.fc, last_esi_update=now - timedelta(seconds=121))
        recent = f.create_operation(self.fc, last_esi_update=now - timedelta(seconds=30))
        f.create_operation(self.fc, status=Status.CLOSED)
        f.create_operation(self.fc, tracking_enabled=False)
        f.create_operation(self.fc, status=Status.STARTING, tracking_enabled=True)

        with frozen_now(now):
            queued = schedule_active_fleet_tracking()

        self.assertEqual(queued, 2)
        self.assertEqual(self.queued_ids(), {never_synced.pk, overdue.pk})
        self.assertNotIn(recent.pk, self.queued_ids())

    def test_shorter_interval_makes_recent_fleets_due(self):
        f.settings(tracking_interval=30)
        now = timezone.now()
        recent = f.create_operation(self.fc, last_esi_update=now - timedelta(seconds=45))

        with frozen_now(now):
            schedule_active_fleet_tracking()

        self.assertEqual(self.queued_ids(), {recent.pk})

    # Known issue: a failed poll does not count towards the interval, so failing fleets are polled on every beat
    @unittest.expectedFailure
    def test_failed_poll_also_waits_for_the_interval(self):
        f.settings(tracking_interval=300, auto_end_enabled=True, auto_end_missing_count=3)
        fc_char = f.main_of(self.fc)
        f.add_token(self.fc, fc_char)
        start = timezone.now()
        operation = f.create_operation(self.fc, esi_fleet_id=FLEET_ID, last_esi_update=start - timedelta(minutes=10))
        self.esi.members[FLEET_ID] = http_error(404)
        with frozen_now(start):
            track_operation(operation.pk)

        with frozen_now(start + timedelta(seconds=60)):
            schedule_active_fleet_tracking()

        self.assertNotIn(operation.pk, self.queued_ids())

    def test_nothing_due_queues_nothing(self):
        now = timezone.now()
        f.create_operation(self.fc, last_esi_update=now - timedelta(seconds=10))

        with frozen_now(now):
            self.assertEqual(schedule_active_fleet_tracking(), 0)

        self.delay.assert_not_called()


class TrackOperationTaskTests(TrackingTestBase):
    def lock_key(self):
        return f"fleetops:track:{self.operation.pk}"

    def test_successful_run_returns_member_count_and_releases_lock(self):
        self.set_members(f.esi_member(self.fc_char), f.esi_member(self.pilot_char))

        self.assertEqual(track_operation(self.operation.pk), 2)

        self.assertIsNone(cache.get(self.lock_key()))
        self.assertEqual(self.operation.member_states.count(), 2)

    def test_running_lock_skips_second_worker(self):
        self.set_members(f.esi_member(self.pilot_char))
        cache.add(self.lock_key(), "1", timeout=60)

        self.assertEqual(track_operation(self.operation.pk), "already-running")

        self.assertEqual(self.esi.calls, [])
        self.assertIsNotNone(cache.get(self.lock_key()))

    def test_lock_is_released_after_failure(self):
        self.esi.members[FLEET_ID] = http_error(502)

        track_operation(self.operation.pk)

        self.assertIsNone(cache.get(self.lock_key()))

    def test_inactive_operations_are_not_tracked(self):
        for status, tracking in ((Status.CLOSED, False), (Status.ACTIVE, False), (Status.ENDING, True)):
            with self.subTest(status=status, tracking=tracking):
                FleetOperation.objects.filter(pk=self.operation.pk).update(status=status, tracking_enabled=tracking)
                self.assertEqual(track_operation(self.operation.pk), "inactive")
        self.assertEqual(self.esi.calls, [])

    def test_fleet_not_found_increments_counter_and_auto_ends_after_threshold(self):
        f.settings(auto_end_enabled=True, auto_end_missing_count=3)
        self.esi.members[FLEET_ID] = http_error(404)

        self.assertEqual(track_operation(self.operation.pk), "FLEET_NOT_FOUND")
        self.operation.refresh_from_db()
        self.assertEqual(self.operation.fleet_missing_count, 1)
        self.assertEqual(self.operation.status, Status.ACTIVE)
        self.assertIn("no longer exists", self.operation.last_error)

        self.assertEqual(track_operation(self.operation.pk), "FLEET_NOT_FOUND")
        self.operation.refresh_from_db()
        self.assertEqual(self.operation.fleet_missing_count, 2)
        self.assertEqual(self.operation.status, Status.ACTIVE)

        self.assertEqual(track_operation(self.operation.pk), "auto-ended")
        self.operation.refresh_from_db()
        self.assertEqual(self.operation.status, Status.CLOSED)
        self.assertFalse(self.operation.tracking_enabled)
        self.assertIsNotNone(self.operation.ended_at)
        end_action = action(self.operation, "fleet_end")
        self.assertEqual(end_action.status, ActionStatus.SUCCESS)
        self.assertEqual(end_action.error_message, "Automatic")
        entry = AuditLog.objects.get(action="fleet.auto_end")
        self.assertIsNone(entry.actor)
        self.assertEqual(entry.object_id, str(self.operation.pk))

    def test_successful_sync_between_misses_restarts_the_count(self):
        f.settings(auto_end_enabled=True, auto_end_missing_count=3)
        self.esi.members[FLEET_ID] = http_error(404)
        track_operation(self.operation.pk)
        track_operation(self.operation.pk)

        self.set_members(f.esi_member(self.fc_char))
        track_operation(self.operation.pk)
        self.operation.refresh_from_db()
        self.assertEqual(self.operation.fleet_missing_count, 0)

        self.esi.members[FLEET_ID] = http_error(404)
        track_operation(self.operation.pk)
        track_operation(self.operation.pk)
        self.operation.refresh_from_db()
        self.assertEqual(self.operation.status, Status.ACTIVE)
        self.assertEqual(self.operation.fleet_missing_count, 2)

    def test_transient_esi_errors_never_end_the_fleet(self):
        f.settings(auto_end_enabled=True, auto_end_missing_count=2)
        FleetOperation.objects.filter(pk=self.operation.pk).update(fleet_missing_count=1)
        failures = [
            http_error(500),
            http_error(502),
            http_error(503),
            http_error(504),
            http_error(401),
            http_error(403),
            ESIErrorLimitException(reset=30),
            requests.ConnectionError("connection reset by peer"),
            TimeoutError("timed out"),
        ]

        for failure in failures:
            with self.subTest(failure=repr(failure)):
                self.esi.members[FLEET_ID] = failure

                self.assertEqual(track_operation(self.operation.pk), "ESI_ERROR")

                self.operation.refresh_from_db()
                self.assertEqual(self.operation.status, Status.ACTIVE)
                self.assertTrue(self.operation.tracking_enabled)
                self.assertEqual(self.operation.fleet_missing_count, 1)
                self.assertIn("Could not read fleet members", self.operation.last_error)
        self.assertFalse(AuditLog.objects.filter(action="fleet.auto_end").exists())

    def test_auto_end_disabled_keeps_fleet_open(self):
        f.settings(auto_end_enabled=False, auto_end_missing_count=2)
        self.esi.members[FLEET_ID] = http_error(404)

        for _ in range(5):
            self.assertEqual(track_operation(self.operation.pk), "FLEET_NOT_FOUND")

        self.operation.refresh_from_db()
        self.assertEqual(self.operation.status, Status.ACTIVE)
        self.assertTrue(self.operation.tracking_enabled)
        self.assertEqual(self.operation.fleet_missing_count, 5)

    def test_missing_token_is_reported_but_not_counted_as_missing_fleet(self):
        f.settings(auto_end_enabled=True, auto_end_missing_count=2)
        self.token.delete()

        for _ in range(3):
            self.assertEqual(track_operation(self.operation.pk), "MISSING_READ_SCOPE")

        self.operation.refresh_from_db()
        self.assertEqual(self.operation.status, Status.ACTIVE)
        self.assertEqual(self.operation.fleet_missing_count, 0)

    def test_unexpected_error_is_recorded_without_ending_the_fleet(self):
        with mock.patch("fleetops.tasks.sync_operation", side_effect=ValueError("bad payload")):
            self.assertEqual(track_operation(self.operation.pk), "error")

        self.operation.refresh_from_db()
        self.assertEqual(self.operation.status, Status.ACTIVE)
        self.assertEqual(self.operation.last_error, "bad payload")
        self.assertIsNone(cache.get(self.lock_key()))

    def test_read_only_token_from_another_app_is_enough_for_tracking(self):
        set_scopes(self.token, [FLEET_READ_SCOPE, "esi-location.read_location.v1"])
        self.set_members(f.esi_member(self.pilot_char))

        self.assertEqual(track_operation(self.operation.pk), 1)

        self.assertEqual(self.esi.calls_of("members")[0][2], self.token)

    def test_boss_handover_currently_looks_like_a_disbanded_fleet(self):
        # After the FC passes boss to another pilot, ESI answers 404 for the FC's
        # token. That is indistinguishable from a closed fleet today.
        f.settings(auto_end_enabled=True, auto_end_missing_count=2)
        self.set_members(f.esi_member(self.fc_char, role="fleet_commander"), f.esi_member(self.pilot_char))
        track_operation(self.operation.pk)

        self.esi.members[FLEET_ID] = http_error(404)
        self.assertEqual(track_operation(self.operation.pk), "FLEET_NOT_FOUND")
        self.assertEqual(track_operation(self.operation.pk), "auto-ended")

        self.operation.refresh_from_db()
        self.assertEqual(self.operation.status, Status.CLOSED)


# ---------------------------------------------------------------------------
# End fleet
# ---------------------------------------------------------------------------


class EndFleetTests(TrackingTestBase):
    def setUp(self):
        super().setUp()
        self.set_members(f.esi_member(self.fc_char), f.esi_member(self.pilot_char))
        sync_operation(self.operation)

    def test_end_sequence_with_final_sync_and_multiplier(self):
        late = f.create_user(main_name="Late Pilot")
        late_char = f.main_of(late)
        seen_status = []

        def members_at_end():
            seen_status.append(FleetOperation.objects.get(pk=self.operation.pk).status)
            return [f.esi_member(self.fc_char), f.esi_member(self.pilot_char), f.esi_member(late_char)]

        self.esi.members[FLEET_ID] = members_at_end

        end_fleet(self.operation, actor=self.fc, attendance_multiplier=2)

        self.assertEqual(seen_status, [Status.ENDING])
        self.operation.refresh_from_db()
        self.assertEqual(self.operation.status, Status.CLOSED)
        self.assertFalse(self.operation.tracking_enabled)
        self.assertIsNotNone(self.operation.ended_at)
        self.assertEqual(self.operation.attendance_multiplier, 2)
        values = dict(
            AttendanceRecord.objects.filter(operation=self.operation).values_list("character_id", "attendance_value")
        )
        self.assertEqual(
            values,
            {self.fc_char.character_id: 2, self.pilot_char.character_id: 2, late_char.character_id: 2},
        )
        end_action = action(self.operation, "fleet_end")
        self.assertEqual(end_action.status, ActionStatus.SUCCESS)
        self.assertEqual(end_action.error_message, "Manual")
        self.assertEqual(audit_actions(self.operation), ["attendance.multiplier", "fleet.end"])
        entry = AuditLog.objects.get(action="fleet.end")
        self.assertEqual(entry.actor, self.fc)
        self.assertEqual(entry.new_value, {"status": Status.CLOSED, "tracking_enabled": False})

    def test_multiplier_skips_manual_and_capped_rows(self):
        manual = f.add_attendance(self.operation, self.pilot, source=AttendanceRecord.Source.MANUAL, value=1)
        alt = f.add_alt(self.pilot, "Capped Alt")
        capped = f.add_attendance(self.operation, self.pilot, alt, granted=False, capped=True)

        end_fleet(self.operation, actor=self.fc, attendance_multiplier=3)

        manual.refresh_from_db()
        capped.refresh_from_db()
        self.assertEqual(manual.attendance_value, 1)
        self.assertEqual(capped.attendance_value, 1)
        self.assertEqual(
            AttendanceRecord.objects.get(
                operation=self.operation, character_id=self.pilot_char.character_id, source=AttendanceRecord.Source.AUTOMATIC
            ).attendance_value,
            3,
        )

    def test_default_multiplier_keeps_current_operation_value(self):
        end_fleet(self.operation, actor=self.fc)

        self.operation.refresh_from_db()
        self.assertEqual(self.operation.attendance_multiplier, 1)
        self.assertEqual(audit_actions(self.operation), ["fleet.end"])

    def test_final_sync_failure_still_closes_the_fleet(self):
        self.esi.members[FLEET_ID] = http_error(502)

        end_fleet(self.operation, actor=self.fc, attendance_multiplier=2)

        self.operation.refresh_from_db()
        self.assertEqual(self.operation.status, Status.CLOSED)
        self.assertFalse(self.operation.tracking_enabled)
        self.assertEqual(
            AttendanceRecord.objects.get(operation=self.operation, character_id=self.pilot_char.character_id).attendance_value,
            2,
        )

    def test_final_sync_records_members_who_left(self):
        self.set_members(f.esi_member(self.fc_char))

        end_fleet(self.operation, actor=self.fc)

        self.assertFalse(self.state(self.pilot_char).is_active)

    def test_ending_a_closed_fleet_is_a_no_op(self):
        end_fleet(self.operation, actor=self.fc, attendance_multiplier=2)
        self.operation.refresh_from_db()
        ended_at = self.operation.ended_at
        audit_count = AuditLog.objects.count()
        calls = len(self.esi.calls)

        result = end_fleet(self.operation, actor=self.fc, attendance_multiplier=3)

        self.assertEqual(result.pk, self.operation.pk)
        self.operation.refresh_from_db()
        self.assertEqual(self.operation.ended_at, ended_at)
        self.assertEqual(self.operation.attendance_multiplier, 2)
        self.assertEqual(AuditLog.objects.count(), audit_count)
        self.assertEqual(len(self.esi.calls), calls)
        self.assertEqual(action(self.operation, "fleet_end").attempts, 1)

    def test_cancelled_fleet_is_left_alone(self):
        FleetOperation.objects.filter(pk=self.operation.pk).update(status=Status.CANCELLED)
        self.operation.refresh_from_db()

        end_fleet(self.operation, actor=self.fc)

        self.operation.refresh_from_db()
        self.assertEqual(self.operation.status, Status.CANCELLED)
        self.assertIsNone(self.operation.ended_at)

    def test_end_view_applies_selected_multiplier(self):
        self.client.force_login(self.fc)

        response = self.client.post(
            reverse("fleetops:end_fleet", kwargs={"operation_uuid": self.operation.uuid}),
            {"attendance_multiplier": "3"},
        )

        self.assertEqual(response.status_code, 302)
        self.operation.refresh_from_db()
        self.assertEqual(self.operation.status, Status.CLOSED)
        self.assertEqual(self.operation.attendance_multiplier, 3)

    # Known issue: the fleet.end audit entry always records "active" as the previous status
    @unittest.expectedFailure
    def test_audit_records_the_real_previous_status(self):
        FleetOperation.objects.filter(pk=self.operation.pk).update(status=Status.ERROR)
        self.operation.refresh_from_db()

        end_fleet(self.operation, actor=self.fc)

        entry = AuditLog.objects.get(action="fleet.end")
        self.assertEqual(entry.old_value["status"], Status.ERROR)


# ---------------------------------------------------------------------------
# Manual fleets
# ---------------------------------------------------------------------------


class CreateManualFleetTests(FakeESIMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.fc = f.create_user(perms=f.FC_PERMS, main_name="Boss Main")
        self.fleet_type = f.fleet_type("Roam", "1.50")
        self.started = timezone.now() - timedelta(hours=3)
        self.ended = self.started + timedelta(hours=2)

    def create(self, user=None, **overrides):
        data = {
            "fleet_type": self.fleet_type,
            "doctrine_name": "Kitey Frigates",
            "formup": "Amamake",
            "started_at": self.started,
            "ended_at": self.ended,
            "attendance_multiplier": 2,
            "notes": "Logged from Discord screenshots",
        }
        data.update(overrides)
        return create_manual_fleet(user=user or self.fc, cleaned_data=data)

    def test_creates_closed_manual_record_without_esi_or_discord(self):
        operation = self.create()

        operation.refresh_from_db()
        self.assertEqual(operation.status, Status.CLOSED)
        self.assertTrue(operation.is_manual)
        self.assertIsNone(operation.esi_fleet_id)
        self.assertFalse(operation.tracking_enabled)
        self.assertFalse(operation.send_ping)
        self.assertEqual(operation.ping_text, "")
        self.assertEqual(operation.motd_text, "")
        self.assertEqual(operation.fc_user, self.fc)
        self.assertEqual(operation.fc_character_id, f.main_of(self.fc).character_id)
        self.assertEqual(operation.fc_main_character_name, "Boss Main")
        self.assertEqual(operation.fleet_point_weight_snapshot, Decimal("1.50"))
        self.assertEqual(operation.doctrine_name, "Kitey Frigates")
        self.assertEqual(operation.doctrine_source, "manual")
        self.assertEqual(operation.additional_message, "Logged from Discord screenshots")
        self.assertEqual(operation.started_at, self.started)
        self.assertEqual(operation.ended_at, self.ended)
        self.assertEqual(operation.attendance_multiplier, 2)
        self.assertEqual(self.esi.calls, [])
        self.discord_post.assert_not_called()
        self.assertFalse(operation.member_states.exists())

    def test_records_skipped_automation_steps_and_audit(self):
        operation = self.create()

        self.assertEqual(
            action_statuses(operation),
            {
                "manual_fleet": ActionStatus.SUCCESS,
                "discord_ping": ActionStatus.SKIPPED,
                "motd_update": ActionStatus.SKIPPED,
                "tracking_start": ActionStatus.SKIPPED,
                "srp_link": ActionStatus.SKIPPED,
            },
        )
        entry = AuditLog.objects.get(action="fleet.manual_create")
        self.assertEqual(entry.actor, self.fc)
        self.assertEqual(entry.object_id, str(operation.pk))
        self.assertEqual(entry.new_value["attendance_multiplier"], 2)

    def test_requires_a_main_character(self):
        no_main = f.create_user(perms=f.FC_PERMS, with_main=False)

        with self.assertRaises(ValueError):
            self.create(user=no_main)

        self.assertFalse(FleetOperation.objects.exists())

    def test_missing_end_time_defaults_to_now(self):
        moment = timezone.now()
        with frozen_now(moment):
            operation = self.create(ended_at=None)

        self.assertEqual(operation.ended_at, moment)

    def test_is_never_picked_up_by_tracking(self):
        operation = self.create()

        with mock.patch.object(track_operation, "delay") as delay:
            schedule_active_fleet_tracking()
        delay.assert_not_called()
        self.assertEqual(track_operation(operation.pk), "inactive")
        end_fleet(operation, actor=self.fc)
        self.assertEqual(self.esi.calls, [])

    def test_can_receive_manual_attendance(self):
        operation = self.create()
        pilot = f.create_user()

        record = create_manual_attendance(
            operation, actor=self.fc, character_id=f.main_of(pilot).character_id, attendance_value=1
        )

        self.assertTrue(record.granted)
        self.assertEqual(record.auth_user, pilot)
        self.assertEqual(record.source, AttendanceRecord.Source.MANUAL)

    def test_form_rejects_end_before_start(self):
        form = ManualFleetForm(
            data={
                "fleet_type": self.fleet_type.pk,
                "formup": "Amamake",
                "started_at": "2026-10-01 20:00",
                "ended_at": "2026-10-01 19:00",
                "attendance_multiplier": "1",
            }
        )

        self.assertFalse(form.is_valid())

    def test_form_data_creates_manual_fleet(self):
        form = ManualFleetForm(
            data={
                "fleet_type": self.fleet_type.pk,
                "formup": "Amamake",
                "started_at": "2026-10-01 19:00",
                "ended_at": "2026-10-01 21:00",
                "attendance_multiplier": "3",
            }
        )
        self.assertTrue(form.is_valid(), form.errors)

        operation = create_manual_fleet(user=self.fc, cleaned_data=form.cleaned_data)

        self.assertEqual(operation.attendance_multiplier, 3)
        self.assertEqual(operation.ended_at - operation.started_at, timedelta(hours=2))
        self.assertTrue(timezone.is_aware(operation.started_at))


# ---------------------------------------------------------------------------
# ESI provider
# ---------------------------------------------------------------------------


class ESITokenTests(FakeESIMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.user = f.create_user(perms=f.FC_PERMS)
        self.char = f.main_of(self.user)

    def test_read_only_token_is_used_for_reads(self):
        token = f.add_token(self.user, self.char, scopes=[FLEET_READ_SCOPE, "esi-skills.read_skills.v1"])

        self.assertEqual(get_token(self.user, self.char.character_id), token)
        self.assertTrue(has_scope_token(self.user, self.char.character_id))
        self.assertFalse(has_scope_token(self.user, self.char.character_id, write=True))

    def test_write_requires_write_scope(self):
        f.add_token(self.user, self.char, scopes=[FLEET_READ_SCOPE])

        with self.assertRaises(FleetESIError) as ctx:
            get_token(self.user, self.char.character_id, write=True)

        self.assertEqual(ctx.exception.code, "MISSING_WRITE_SCOPE")

    def test_full_token_serves_reads_and_writes(self):
        token = f.add_token(self.user, self.char, scopes=[FLEET_READ_SCOPE, FLEET_WRITE_SCOPE])

        self.assertEqual(get_token(self.user, self.char.character_id), token)
        self.assertEqual(get_token(self.user, self.char.character_id, write=True), token)

    def test_write_token_is_picked_when_user_also_has_a_read_only_token(self):
        f.add_token(self.user, self.char, scopes=[FLEET_READ_SCOPE])
        full = f.add_token(self.user, self.char, scopes=[FLEET_READ_SCOPE, FLEET_WRITE_SCOPE])

        self.assertEqual(get_token(self.user, self.char.character_id, write=True), full)

    def test_tokens_of_other_users_or_characters_are_ignored(self):
        other = f.create_user()
        f.add_token(other, self.char)
        f.add_token(self.user, f.add_alt(self.user))

        with self.assertRaises(FleetESIError) as ctx:
            get_token(self.user, self.char.character_id)

        self.assertEqual(ctx.exception.code, "MISSING_READ_SCOPE")

    def test_write_calls_fail_fast_without_write_scope(self):
        f.add_token(self.user, self.char, scopes=[FLEET_READ_SCOPE])

        with self.assertRaises(FleetESIError) as motd_ctx:
            set_fleet_motd(self.user, self.char.character_id, FLEET_ID, "hello")
        with self.assertRaises(FleetESIError) as kick_ctx:
            kick_fleet_member(self.user, self.char.character_id, FLEET_ID, 12345)

        self.assertEqual(motd_ctx.exception.code, "MISSING_WRITE_SCOPE")
        self.assertEqual(kick_ctx.exception.code, "MISSING_WRITE_SCOPE")
        self.assertEqual(self.esi.put_calls, [])
        self.assertEqual(self.esi.kicks, [])


class ESIFleetCallTests(FakeESIMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.user = f.create_user(perms=f.FC_PERMS)
        self.char = f.main_of(self.user)
        self.token = f.add_token(self.user, self.char)

    def test_detect_returns_fleet_and_role(self):
        self.esi.character_fleets[self.char.character_id] = {"fleet_id": FLEET_ID, "role": "fleet_commander", "squad_id": -1, "wing_id": -1}

        result = detect_character_fleet(self.user, self.char.character_id)

        self.assertEqual(result.fleet_id, FLEET_ID)
        self.assertEqual(result.role, "fleet_commander")
        self.assertEqual(self.esi.calls_of("detect")[0][2], self.token)

    def test_detect_reports_non_boss_role_unchanged(self):
        self.esi.put_in_fleet(self.char, role="squad_member")

        result = detect_character_fleet(self.user, self.char.character_id)

        self.assertEqual(result.role, "squad_member")

    def test_detect_error_mapping(self):
        cases = [
            (http_error(404), "NOT_IN_FLEET"),
            (http_error(401), "ESI_FORBIDDEN"),
            (http_error(403), "ESI_FORBIDDEN"),
            (http_error(502), "ESI_ERROR"),
            (requests.ConnectionError("boom"), "ESI_ERROR"),
            ({"role": "fleet_commander"}, "NOT_IN_FLEET"),
        ]
        for outcome, code in cases:
            with self.subTest(code=code, outcome=repr(outcome)):
                self.esi.character_fleets[self.char.character_id] = outcome
                with self.assertRaises(FleetESIError) as ctx:
                    detect_character_fleet(self.user, self.char.character_id)
                self.assertEqual(ctx.exception.code, code)

    def test_members_are_returned_as_dicts(self):
        model = mock.Mock(spec=["model_dump"])
        model.model_dump.return_value = {"character_id": 1, "role": "squad_member"}
        self.esi.members[FLEET_ID] = [model, {"character_id": 2}]

        rows = get_fleet_members(self.user, self.char.character_id, FLEET_ID)

        self.assertEqual(rows, [{"character_id": 1, "role": "squad_member"}, {"character_id": 2}])

    def test_members_404_means_fleet_not_found(self):
        self.esi.members[FLEET_ID] = http_error(404)

        with self.assertRaises(FleetESIError) as ctx:
            get_fleet_members(self.user, self.char.character_id, FLEET_ID)

        self.assertEqual(ctx.exception.code, "FLEET_NOT_FOUND")
        self.assertEqual(ctx.exception.status_code, 404)

    def test_other_member_errors_are_generic_esi_errors(self):
        for outcome, status in (
            (http_error(500), 500),
            (http_error(502), 502),
            (http_error(403), 403),
            (http_error(420), 420),
            (ESIErrorLimitException(reset=10), None),
            (requests.ReadTimeout("slow"), None),
        ):
            with self.subTest(outcome=repr(outcome)):
                self.esi.members[FLEET_ID] = outcome
                with self.assertRaises(FleetESIError) as ctx:
                    get_fleet_members(self.user, self.char.character_id, FLEET_ID)
                self.assertEqual(ctx.exception.code, "ESI_ERROR")
                self.assertEqual(ctx.exception.status_code, status)

    def test_fleet_info_error_mapping(self):
        self.esi.fleet_info[FLEET_ID] = http_error(404)
        with self.assertRaises(FleetESIError) as ctx:
            get_fleet_info(self.user, self.char.character_id, FLEET_ID)
        self.assertEqual(ctx.exception.code, "FLEET_NOT_FOUND")

        self.esi.fleet_info[FLEET_ID] = http_error(503)
        with self.assertRaises(FleetESIError) as ctx:
            get_fleet_info(self.user, self.char.character_id, FLEET_ID)
        self.assertEqual(ctx.exception.code, "ESI_ERROR")

    def test_set_motd_sends_motd_and_keeps_free_move(self):
        self.esi.fleet_info[FLEET_ID] = {"is_free_move": True, "motd": "old"}

        set_fleet_motd(self.user, self.char.character_id, FLEET_ID, "<b>new</b>")

        self.assertEqual(self.esi.put_calls, [{"fleet_id": FLEET_ID, "token": self.token, "body": {"motd": "<b>new</b>", "is_free_move": True}}])

    def test_set_motd_error_mapping(self):
        for outcome, code in ((http_error(403), "MOTD_FORBIDDEN"), (http_error(500), "MOTD_ERROR")):
            with self.subTest(code=code):
                self.esi.put_outcome = outcome
                with self.assertRaises(FleetESIError) as ctx:
                    set_fleet_motd(self.user, self.char.character_id, FLEET_ID, "motd")
                self.assertEqual(ctx.exception.code, code)

    def test_kick_error_mapping(self):
        self.esi.kick_outcomes = {1: http_error(404), 2: http_error(403), 3: http_error(500)}
        for member_id, code in ((1, "MEMBER_NOT_FOUND"), (2, "KICK_FORBIDDEN"), (3, "KICK_ERROR")):
            with self.subTest(code=code):
                with self.assertRaises(FleetESIError) as ctx:
                    kick_fleet_member(self.user, self.char.character_id, FLEET_ID, member_id)
                self.assertEqual(ctx.exception.code, code)

    def test_as_dict_handles_plain_objects(self):
        obj = SimpleNamespace(fleet_id=5, role="fleet_commander")

        self.assertEqual(as_dict(obj), {"fleet_id": 5, "role": "fleet_commander"})
        self.assertEqual(as_dict(None), {})


class ESIUnchangedResponseTests(FakeESIMixin, TestCase):
    """django-esi 9 raises HTTPNotModified when an unchanged resource is read again with ETags on."""

    def setUp(self):
        super().setUp()
        self.esi.etags = {}
        self.fc = f.create_user(perms=f.FC_PERMS)
        self.fc_char = f.main_of(self.fc)
        f.add_token(self.fc, self.fc_char)
        self.pilot = f.create_user()
        self.esi.put_in_fleet(self.fc_char)
        self.esi.members[FLEET_ID] = [f.esi_member(self.fc_char, role="fleet_commander"), f.esi_member(f.main_of(self.pilot))]

    # Known issue: re-reading an unchanged fleet (ETag hit) raises ESI_ERROR, so start fails after the detection preview
    @unittest.expectedFailure
    def test_start_after_detection_preview_succeeds(self):
        # The start page asks the detect endpoint first; the form submit detects again.
        detect_character_fleet(self.fc, self.fc_char.character_id)

        try:
            operation = start_fleet(
                user=self.fc,
                cleaned_data={
                    "operation_mode": "attendance_only",
                    "fc_character_id": str(self.fc_char.character_id),
                    "fleet_type": f.fleet_type(),
                    "formup": "Jita",
                },
                request_id=uuid.uuid4(),
            )
        except FleetESIError as exc:
            self.fail(f"Start failed after a detection preview: {exc.code} {exc}")

        self.assertEqual(operation.status, Status.ACTIVE)

    # Known issue: an unchanged fleet member list (ETag hit) is treated as an ESI error during tracking
    @unittest.expectedFailure
    def test_unchanged_member_list_still_counts_as_successful_sync(self):
        operation = f.create_operation(self.fc, esi_fleet_id=FLEET_ID)
        self.assertEqual(track_operation(operation.pk), 2)

        later = timezone.now() + timedelta(minutes=1)
        with frozen_now(later):
            result = track_operation(operation.pk)

        operation.refresh_from_db()
        self.assertEqual(result, 2)
        self.assertEqual(operation.last_error, "")
        self.assertEqual(operation.last_esi_update, later)


# ---------------------------------------------------------------------------
# Discord pings, retries, SRP retries and capsule kicks
# ---------------------------------------------------------------------------


class DiscordWebhookTests(FakeESIMixin, TestCase):
    def test_success(self):
        result = send_discord_webhook(WEBHOOK_URL, "ping")

        self.assertTrue(result.success)
        self.assertEqual(result.status_code, 204)
        self.discord_post.assert_called_once_with(WEBHOOK_URL, json={"content": "ping"}, timeout=15)

    def test_error_response(self):
        self.discord_post.return_value = mock.Mock(status_code=429, text="rate limited")

        result = send_discord_webhook(WEBHOOK_URL, "ping")

        self.assertFalse(result.success)
        self.assertEqual(result.status_code, 429)
        self.assertEqual(result.message, "rate limited")

    def test_missing_url_is_not_posted(self):
        result = send_discord_webhook("", "ping")

        self.assertFalse(result.success)
        self.discord_post.assert_not_called()

    def test_network_error(self):
        self.discord_post.side_effect = requests.ConnectionError("refused")

        result = send_discord_webhook(WEBHOOK_URL, "ping")

        self.assertFalse(result.success)

    # Known issue: requests exception text containing the webhook URL is stored and shown on the operation page
    @unittest.expectedFailure
    def test_webhook_secret_never_reaches_action_error_message(self):
        fc = f.create_user(perms=f.FC_PERMS)
        # A webhook saved without the scheme; requests rejects it while preparing the request.
        webhook = DiscordWebhook.objects.create(name="Typo", webhook_url="discord.com/api/webhooks/123456/webhook-secret-token")
        target = PingTarget.objects.create(name="Pings", target_value="@here", webhook=webhook)
        operation = f.create_operation(fc, ping_target=target, ping_text="ping")
        self.discord_post.side_effect = lambda url, **kwargs: requests.Request("POST", url, json=kwargs.get("json")).prepare()

        ping = retry_ping(operation)

        self.assertEqual(ping.status, ActionStatus.FAILED)
        self.assertNotIn("webhook-secret-token", ping.error_message)


class WebhookSecretPageTests(FakeESIMixin, TestCase):
    # Known issue: a failed ping exposes the webhook URL, including its token, to members viewing the fleet
    @unittest.expectedFailure
    def test_webhook_secret_is_not_shown_to_fleet_members(self):
        fc = f.create_user(perms=f.FC_PERMS)
        member = f.create_user(perms=f.MEMBER_PERMS)
        webhook = DiscordWebhook.objects.create(name="Typo", webhook_url="discord.com/api/webhooks/123456/webhook-secret-token")
        target = PingTarget.objects.create(name="Pings", target_value="@here", webhook=webhook)
        operation = f.create_operation(fc, ping_target=target, ping_text="ping")
        f.add_attendance(operation, member)
        self.discord_post.side_effect = lambda url, **kwargs: requests.Request("POST", url, json=kwargs.get("json")).prepare()
        retry_ping(operation)
        self.client.force_login(member)

        response = self.client.get(reverse("fleetops:operation_detail", kwargs={"operation_uuid": operation.uuid}))

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "webhook-secret-token")


class RetryTests(FakeESIMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.fc = f.create_user(perms=f.FC_PERMS)
        self.fc_char = f.main_of(self.fc)
        f.add_token(self.fc, self.fc_char)
        webhook = DiscordWebhook.objects.create(name="Pings", webhook_url=WEBHOOK_URL)
        self.target = PingTarget.objects.create(name="Everyone", target_value="@everyone", webhook=webhook)
        self.operation = f.create_operation(
            self.fc, esi_fleet_id=FLEET_ID, ping_target=self.target, ping_text="ping", motd_text="motd"
        )

    def test_set_action_counts_attempts(self):
        set_action(self.operation, "discord_ping", False, "first")
        result = set_action(self.operation, "discord_ping", True)

        self.assertEqual(result.attempts, 2)
        self.assertEqual(result.status, ActionStatus.SUCCESS)
        self.assertEqual(result.error_message, "")

    def test_retry_ping_posts_stored_ping_text(self):
        result = retry_ping(self.operation)

        self.discord_post.assert_called_once_with(WEBHOOK_URL, json={"content": "ping"}, timeout=15)
        self.assertEqual(result.status, ActionStatus.SUCCESS)

    def test_retry_motd_uses_fc_character_token(self):
        result = retry_motd(self.operation)

        self.assertEqual(result.status, ActionStatus.SUCCESS)
        self.assertEqual(self.esi.put_calls[0]["body"]["motd"], "motd")

    def test_retry_motd_failure_is_recorded(self):
        self.esi.put_outcome = http_error(500)

        result = retry_motd(self.operation)

        self.assertEqual(result.status, ActionStatus.FAILED)
        self.assertIn("Could not update fleet MOTD", result.error_message)

    # Known issue K4: retrying SRP for an operation that already has an SRP reference creates another SRP fleet
    @unittest.expectedFailure
    def test_retry_srp_does_not_duplicate_existing_srp_fleet(self):
        provider = mock.Mock(key="counting_srp")
        provider.available.return_value = True
        provider.create_for_operation.return_value = SRPLinkResult(
            "counting_srp", reference="2", url="/srp/2/", created=True, message="SRP fleet created."
        )
        f.settings(srp_provider="counting_srp")
        FleetOperation.objects.filter(pk=self.operation.pk).update(
            srp_provider="counting_srp", srp_reference="1", srp_url="/srp/1/"
        )
        self.operation.refresh_from_db()

        with mock.patch.dict(srp_module._PROVIDERS, {"counting_srp": provider}):
            retry_srp(self.operation)

        provider.create_for_operation.assert_not_called()
        self.operation.refresh_from_db()
        self.assertEqual(self.operation.srp_reference, "1")


class KickCapsulesCallTests(TrackingTestBase):
    def test_kicks_capsules_with_fc_token_but_never_the_fc(self):
        podded = f.create_user()
        podded_char = f.main_of(podded)
        self.set_members(
            f.esi_member(self.fc_char, role="fleet_commander", ship_type_id=670),
            f.esi_member(self.pilot_char, ship_type_id=17738),
            f.esi_member(podded_char, ship_type_id=670),
        )
        sync_operation(self.operation)

        result = kick_all_pods(self.operation, actor=self.fc)

        self.assertEqual(self.esi.kicks, [podded_char.character_id])
        self.assertEqual(result["target_count"], 1)
        self.assertEqual(result["failures"], [])
        self.assertEqual(action(self.operation, "kick_capsules").status, ActionStatus.SUCCESS)
        self.assertEqual(audit_actions(self.operation), ["fleet.kick_capsules"])

    def test_read_only_token_reports_per_member_failure(self):
        set_scopes(self.token, [FLEET_READ_SCOPE])
        self.set_members(f.esi_member(self.pilot_char, ship_type_id=670))
        sync_operation(self.operation)

        result = kick_all_pods(self.operation, actor=self.fc)

        self.assertEqual(self.esi.kicks, [])
        self.assertEqual(len(result["failures"]), 1)
        self.assertEqual(action(self.operation, "kick_capsules").status, ActionStatus.FAILED)
