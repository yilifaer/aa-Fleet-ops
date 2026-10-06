"""Tests for how FleetOps reads and writes ESI through django-esi operations.

django-esi caches responses and, by default, answers a read of an unchanged resource
with ``HTTPNotModified`` instead of data. Fleet tracking needs the data on every read.
"""

import hashlib
import json
import os
import shutil
import tempfile
import uuid
from contextlib import contextmanager
from datetime import timedelta
from types import SimpleNamespace
from unittest import mock

from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from esi import openapi_clients
from esi.exceptions import (
    ESIBucketLimitException,
    ESIErrorLimitException,
    HTTPClientError,
    HTTPNotModified,
    HTTPServerError,
)
from esi.openapi_clients import ESIClientProvider

from fleetops.constants import FLEET_READ_SCOPE, FLEET_WRITE_SCOPE
from fleetops.models import AttendanceRecord, FleetMemberEvent, FleetMemberState, FleetOperation, OperationAction
from fleetops.providers import esi as esi_provider
from fleetops.providers.esi import (
    FleetESIError,
    detect_character_fleet,
    get_fleet_info,
    get_fleet_members,
    kick_fleet_member,
    set_fleet_motd,
)
from fleetops.services.operations import end_fleet, retry_motd, start_fleet
from fleetops.tasks import POLL_SLACK_SECONDS, schedule_active_fleet_tracking, track_operation

from . import factories as f

FLEET_ID = 1_045_000_000_777

Status = FleetOperation.Status


def http_error(status):
    if status >= 500:
        return HTTPServerError(status_code=status, headers={}, data=None)
    return HTTPClientError(status_code=status, headers={}, data=None)


@contextmanager
def frozen_now(moment):
    with mock.patch("django.utils.timezone.now", return_value=moment):
        yield


# ---------------------------------------------------------------------------
# Fake django-esi operations
# ---------------------------------------------------------------------------


class FakeOperation:
    """Follows the contract of django-esi's ``EsiOperation.result()``.

    Parameters are only validated when the request runs. Responses are cached per
    operation and parameters (token and body excluded). With ``use_etag`` a cached
    response whose ETag is known, or a 304 answer from ESI, raises ``HTTPNotModified``.
    """

    def __init__(self, esi, name, kwargs):
        self.esi = esi
        self.name = name
        self.kwargs = kwargs

    def result(self, use_etag=True, return_response=False, force_refresh=False, use_cache=True, store_cache=True, **extra):
        method, accepted = FakeFleetsAPI.OPERATIONS[self.name]
        params = dict(self.kwargs) | extra
        params.pop("token", None)
        body = params.pop("body", None)
        unknown = set(params) - accepted
        if unknown:
            raise ValueError(f"Parameter {sorted(unknown)} unknown (accepted {sorted(accepted)})")
        key = (self.name, tuple(sorted(params.items())))

        if force_refresh:
            self.esi.response_cache.pop(key, None)
            self.esi.etags.pop(key, None)
        known_etag = self.esi.etags.get(key) if use_etag else None

        if use_cache and key in self.esi.response_cache:
            data, etag = self.esi.response_cache[key]
            if known_etag and known_etag == etag:
                raise HTTPNotModified(status_code=304, headers={"ETag": etag})
            return data

        self.esi.requests.append((self.name, params, body, known_etag))
        if self.esi.always_not_modified and not force_refresh:
            raise HTTPNotModified(status_code=304, headers={})
        outcome = self.esi.respond(self.name, params, body)
        if isinstance(outcome, BaseException):
            raise outcome
        etag = f'"{hashlib.md5(repr(outcome).encode()).hexdigest()}"' if method == "GET" else None
        if use_etag and etag:
            self.esi.etags[key] = etag
        if known_etag and known_etag == etag:
            raise HTTPNotModified(status_code=304, headers={"ETag": etag})
        if store_cache and self.esi.cache_responses:
            self.esi.response_cache[key] = (outcome, etag)
        return outcome


class FakeFleetsAPI:
    """In-memory ``esi.client.Fleets`` whose operations behave like django-esi ones."""

    OPERATIONS = {
        "GetCharactersCharacterIdFleet": ("GET", {"character_id"}),
        "GetFleetsFleetId": ("GET", {"fleet_id"}),
        "GetFleetsFleetIdMembers": ("GET", {"fleet_id"}),
        "PutFleetsFleetId": ("PUT", {"fleet_id"}),
        "DeleteFleetsFleetIdMembersMemberId": ("DELETE", {"fleet_id", "member_id"}),
    }

    def __init__(self):
        self.character_fleets = {}
        self.fleet_info = {}
        self.members = {}
        self.put_outcome = None
        self.kick_outcomes = {}
        self.etags = {}
        self.response_cache = {}
        self.requests = []
        # ESI marks responses cacheable; expire_cache() stands in for the cache window passing.
        self.cache_responses = True
        # Answer 304 even to reads that send no ETag.
        self.always_not_modified = False

    def __getattr__(self, name):
        if name not in self.OPERATIONS:
            raise AttributeError(name)
        return lambda **kwargs: FakeOperation(self, name, kwargs)

    def expire_cache(self):
        self.response_cache.clear()

    def requests_of(self, name):
        return [request for request in self.requests if request[0] == name]

    def respond(self, name, params, body):
        if name == "GetCharactersCharacterIdFleet":
            return self.character_fleets.get(params["character_id"], http_error(404))
        if name == "GetFleetsFleetId":
            return self.fleet_info.get(params["fleet_id"], http_error(404))
        if name == "GetFleetsFleetIdMembers":
            return self.members.get(params["fleet_id"], http_error(404))
        if name == "PutFleetsFleetId":
            if self.put_outcome is None:
                self.fleet_info[params["fleet_id"]] = dict(body)
            return self.put_outcome
        return self.kick_outcomes.get(params["member_id"])


class FakeESIMixin:
    def setUp(self):
        super().setUp()
        cache.clear()
        self.esi = FakeFleetsAPI()
        patcher = mock.patch.object(esi_provider, "esi", SimpleNamespace(client=SimpleNamespace(Fleets=self.esi)))
        patcher.start()
        self.addCleanup(patcher.stop)
        post_patcher = mock.patch("fleetops.providers.pings.requests.post")
        post_patcher.start().return_value = mock.Mock(status_code=204, text="")
        self.addCleanup(post_patcher.stop)

        self.fc = f.create_user(perms=f.FC_PERMS, main_name="Boss Main")
        self.fc_char = f.main_of(self.fc)
        self.token = f.add_token(self.fc, self.fc_char)
        self.pilot = f.create_user(main_name="Line Pilot")
        self.pilot_char = f.main_of(self.pilot)
        self.esi.character_fleets[self.fc_char.character_id] = {
            "fleet_id": FLEET_ID,
            "role": "fleet_commander",
            "squad_id": -1,
            "wing_id": -1,
        }
        self.esi.fleet_info[FLEET_ID] = {"is_free_move": True, "is_registered": False, "is_voice_enabled": False, "motd": "old"}
        self.esi.members[FLEET_ID] = [
            f.esi_member(self.fc_char, role="fleet_commander", wing_id=-1, squad_id=-1),
            f.esi_member(self.pilot_char),
        ]

    def start(self):
        f.settings(srp_auto_create=False)
        return start_fleet(
            user=self.fc,
            cleaned_data={
                "operation_mode": "full",
                "fc_character_id": str(self.fc_char.character_id),
                "fleet_type": f.fleet_type(),
                "formup": "Jita",
            },
            request_id=uuid.uuid4(),
        )


class UnchangedReadTests(FakeESIMixin, TestCase):
    def test_fake_follows_django_esi_defaults(self):
        self.esi.GetFleetsFleetIdMembers(fleet_id=FLEET_ID, token=self.token).result()

        with self.assertRaises(HTTPNotModified):
            self.esi.GetFleetsFleetIdMembers(fleet_id=FLEET_ID, token=self.token).result()
        self.esi.expire_cache()
        with self.assertRaises(HTTPNotModified):
            self.esi.GetFleetsFleetIdMembers(fleet_id=FLEET_ID, token=self.token).result()

    def test_detecting_the_same_fleet_twice_returns_it_both_times(self):
        first = detect_character_fleet(self.fc, self.fc_char.character_id)
        cached = detect_character_fleet(self.fc, self.fc_char.character_id)
        self.esi.expire_cache()
        refetched = detect_character_fleet(self.fc, self.fc_char.character_id)

        for result in (first, cached, refetched):
            self.assertEqual(result.fleet_id, FLEET_ID)
            self.assertEqual(result.role, "fleet_commander")

    def test_detection_preview_then_start_fleet(self):
        self.client.force_login(self.fc)
        url = reverse("fleetops:detect_fleet", args=[self.fc_char.character_id])
        for _ in range(2):
            preview = self.client.get(url).json()
            self.assertTrue(preview["ok"], preview)
            self.assertTrue(preview["is_fleet_boss"])

        operation = self.start()

        self.assertEqual(operation.status, Status.ACTIVE)
        self.assertEqual(operation.esi_fleet_id, FLEET_ID)
        actions = dict(OperationAction.objects.filter(operation=operation).values_list("action", "status"))
        self.assertEqual(actions["fleet_detection"], OperationAction.Status.SUCCESS)
        self.assertEqual(actions["motd_update"], OperationAction.Status.SUCCESS)
        self.assertEqual(actions["tracking_start"], OperationAction.Status.SUCCESS)
        self.assertEqual(operation.member_states.count(), 2)

    def test_unchanged_member_list_keeps_tracking(self):
        operation = f.create_operation(self.fc, esi_fleet_id=FLEET_ID)
        start = timezone.now()
        with frozen_now(start):
            self.assertEqual(track_operation(operation.pk), 2)

        later = start + timedelta(minutes=1)
        self.esi.expire_cache()
        with frozen_now(later):
            self.assertEqual(track_operation(operation.pk), 2)

        operation.refresh_from_db()
        self.assertEqual(operation.last_error, "")
        self.assertEqual(operation.last_esi_update, later)
        pilot_state = FleetMemberState.objects.get(operation=operation, character_id=self.pilot_char.character_id)
        self.assertTrue(pilot_state.is_active)
        self.assertEqual(pilot_state.last_seen, later)
        attendance = AttendanceRecord.objects.get(operation=operation, character_id=self.pilot_char.character_id)
        self.assertEqual(attendance.last_seen, later)
        self.assertFalse(FleetMemberEvent.objects.filter(operation=operation, event_type=FleetMemberEvent.EventType.LEAVE).exists())

    def test_cached_member_list_counts_as_successful_sync(self):
        operation = f.create_operation(self.fc, esi_fleet_id=FLEET_ID)
        track_operation(operation.pk)

        later = timezone.now() + timedelta(seconds=30)
        with frozen_now(later):
            self.assertEqual(track_operation(operation.pk), 2)

        operation.refresh_from_db()
        self.assertEqual(operation.last_esi_update, later)
        self.assertEqual(len(self.esi.requests_of("GetFleetsFleetIdMembers")), 1)

    def test_member_changes_are_seen_after_the_cache_window(self):
        operation = f.create_operation(self.fc, esi_fleet_id=FLEET_ID)
        track_operation(operation.pk)

        self.esi.members[FLEET_ID] = self.esi.members[FLEET_ID][:1]
        self.esi.expire_cache()
        self.assertEqual(track_operation(operation.pk), 1)

        pilot_state = FleetMemberState.objects.get(operation=operation, character_id=self.pilot_char.character_id)
        self.assertFalse(pilot_state.is_active)

    def test_reads_never_send_a_stored_etag(self):
        detect_character_fleet(self.fc, self.fc_char.character_id)
        get_fleet_info(self.fc, self.fc_char.character_id, FLEET_ID)
        get_fleet_members(self.fc, self.fc_char.character_id, FLEET_ID)
        self.esi.expire_cache()
        get_fleet_members(self.fc, self.fc_char.character_id, FLEET_ID)

        self.assertTrue(self.esi.requests)
        self.assertEqual([request[3] for request in self.esi.requests], [None] * len(self.esi.requests))

    def test_motd_update_rereads_unchanged_fleet_info(self):
        self.assertTrue(get_fleet_info(self.fc, self.fc_char.character_id, FLEET_ID)["is_free_move"])
        self.esi.expire_cache()

        set_fleet_motd(self.fc, self.fc_char.character_id, FLEET_ID, "<b>Form up</b>")

        puts = self.esi.requests_of("PutFleetsFleetId")
        self.assertEqual(len(puts), 1)
        self.assertEqual(puts[0][1], {"fleet_id": FLEET_ID})
        self.assertEqual(puts[0][2], {"motd": "<b>Form up</b>", "is_free_move": True})

    def test_retry_motd_after_start_writes_again(self):
        operation = self.start()

        action = retry_motd(operation)

        self.assertEqual(action.status, OperationAction.Status.SUCCESS)
        self.assertEqual(action.attempts, 2)
        self.assertEqual(len(self.esi.requests_of("PutFleetsFleetId")), 2)

    def test_writes_are_never_answered_from_the_cache(self):
        set_fleet_motd(self.fc, self.fc_char.character_id, FLEET_ID, "first")
        set_fleet_motd(self.fc, self.fc_char.character_id, FLEET_ID, "second")
        kick_fleet_member(self.fc, self.fc_char.character_id, FLEET_ID, self.pilot_char.character_id)
        kick_fleet_member(self.fc, self.fc_char.character_id, FLEET_ID, self.pilot_char.character_id)

        self.assertEqual([put[2]["motd"] for put in self.esi.requests_of("PutFleetsFleetId")], ["first", "second"])
        self.assertEqual(len(self.esi.requests_of("DeleteFleetsFleetIdMembersMemberId")), 2)

    def test_end_fleet_final_sync_reads_unchanged_members(self):
        operation = f.create_operation(self.fc, esi_fleet_id=FLEET_ID)
        track_operation(operation.pk)
        self.esi.expire_cache()

        later = timezone.now() + timedelta(minutes=2)
        with frozen_now(later):
            end_fleet(operation, actor=self.fc)

        operation.refresh_from_db()
        self.assertEqual(operation.status, Status.CLOSED)
        self.assertEqual(operation.last_esi_update, later)

    def test_not_modified_answer_to_a_plain_read_is_fetched_again(self):
        self.esi.always_not_modified = True

        rows = get_fleet_members(self.fc, self.fc_char.character_id, FLEET_ID)
        info = get_fleet_info(self.fc, self.fc_char.character_id, FLEET_ID)
        detection = detect_character_fleet(self.fc, self.fc_char.character_id)

        self.assertEqual(len(rows), 2)
        self.assertEqual(info["motd"], "old")
        self.assertEqual(detection.fleet_id, FLEET_ID)


class TransientESIErrorTests(FakeESIMixin, TestCase):
    def transient_failures(self):
        return [
            ESIErrorLimitException(reset=30),
            ESIBucketLimitException("fleet", 10),
            http_error(420),
            http_error(500),
            http_error(502),
            http_error(503),
            http_error(504),
        ]

    def test_reads_report_transient_failures_as_esi_errors(self):
        for failure in self.transient_failures():
            with self.subTest(failure=repr(failure)):
                self.esi.character_fleets[self.fc_char.character_id] = failure
                self.esi.fleet_info[FLEET_ID] = failure
                self.esi.members[FLEET_ID] = failure
                calls = (
                    lambda: detect_character_fleet(self.fc, self.fc_char.character_id),
                    lambda: get_fleet_info(self.fc, self.fc_char.character_id, FLEET_ID),
                    lambda: get_fleet_members(self.fc, self.fc_char.character_id, FLEET_ID),
                )
                for call in calls:
                    with self.assertRaises(FleetESIError) as ctx:
                        call()
                    self.assertEqual(ctx.exception.code, "ESI_ERROR")

    def test_transient_failures_never_count_as_a_missing_fleet(self):
        f.settings(auto_end_enabled=True, auto_end_missing_count=1)
        operation = f.create_operation(self.fc, esi_fleet_id=FLEET_ID)

        for failure in self.transient_failures():
            with self.subTest(failure=repr(failure)):
                self.esi.members[FLEET_ID] = failure

                self.assertEqual(track_operation(operation.pk), "ESI_ERROR")

                operation.refresh_from_db()
                self.assertEqual(operation.status, Status.ACTIVE)
                self.assertEqual(operation.fleet_missing_count, 0)
                self.assertTrue(operation.last_error.startswith("Could not read fleet members"))


# ---------------------------------------------------------------------------
# Scheduling after failed polls
# ---------------------------------------------------------------------------


class FailedPollScheduleTests(FakeESIMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.start = timezone.now()
        self.operation = f.create_operation(
            self.fc, esi_fleet_id=FLEET_ID, last_esi_update=self.start - timedelta(minutes=10)
        )

    def queued_at(self, moment):
        with mock.patch.object(track_operation, "delay") as delay, frozen_now(moment):
            schedule_active_fleet_tracking()
        return {call.args[0] for call in delay.call_args_list}

    def test_failed_poll_waits_for_the_interval(self):
        f.settings(tracking_interval=300)
        for failure in (http_error(404), http_error(502), ESIErrorLimitException(reset=30)):
            with self.subTest(failure=repr(failure)):
                cache.clear()
                self.esi.members[FLEET_ID] = failure
                with frozen_now(self.start):
                    track_operation(self.operation.pk)

                self.assertNotIn(self.operation.pk, self.queued_at(self.start + timedelta(seconds=60)))
                early = timedelta(seconds=300 - POLL_SLACK_SECONDS - 1)
                self.assertNotIn(self.operation.pk, self.queued_at(self.start + early))
                self.assertIn(self.operation.pk, self.queued_at(self.start + timedelta(seconds=300)))

    def test_auto_end_waits_for_the_configured_number_of_intervals(self):
        f.settings(tracking_interval=300, auto_end_enabled=True, auto_end_missing_count=3)
        self.esi.members[FLEET_ID] = http_error(404)
        polls = []

        def run_now(operation_id):
            polls.append(timezone.now())
            return track_operation(operation_id)

        # One scheduler beat per minute, as in the recommended beat schedule.
        with mock.patch.object(track_operation, "delay", side_effect=run_now):
            for minute in range(10):
                with frozen_now(self.start + timedelta(minutes=minute)):
                    schedule_active_fleet_tracking()
            self.operation.refresh_from_db()
            self.assertEqual(self.operation.status, Status.ACTIVE)
            self.assertEqual(self.operation.fleet_missing_count, 2)

            with frozen_now(self.start + timedelta(minutes=10)):
                schedule_active_fleet_tracking()

        self.operation.refresh_from_db()
        self.assertEqual(self.operation.status, Status.CLOSED)
        self.assertEqual(polls, [self.start + timedelta(minutes=minute) for minute in (0, 5, 10)])

    def test_successful_poll_still_uses_last_sync(self):
        f.settings(tracking_interval=120)
        with frozen_now(self.start):
            self.assertEqual(track_operation(self.operation.pk), 2)

        early = timedelta(seconds=120 - POLL_SLACK_SECONDS - 1)
        self.assertNotIn(self.operation.pk, self.queued_at(self.start + early))
        self.assertIn(self.operation.pk, self.queued_at(self.start + timedelta(seconds=120)))

    def run_beats(self, minutes, first=0, latency=timedelta(seconds=1)):
        """Run one scheduler beat per minute, each a little after the minute, and return the poll times.

        A queued poll runs ``latency`` after its beat, like a Celery worker picking it up.
        """
        polls = []

        def run_later(operation_id):
            moment = timezone.now() + latency
            polls.append(moment)
            with frozen_now(moment):
                track_operation(operation_id)

        with mock.patch.object(track_operation, "delay", side_effect=run_later):
            for minute in range(first, first + minutes):
                with frozen_now(self.start + timedelta(minutes=minute, seconds=0.2)):
                    schedule_active_fleet_tracking()
        return polls

    def polls_at(self, *seconds):
        return [self.start + timedelta(seconds=value) for value in seconds]

    def test_late_worker_still_polls_every_interval(self):
        for interval, minutes, expected in (
            (60, 5, (1.2, 61.2, 121.2, 181.2, 241.2)),
            (300, 11, (1.2, 301.2, 601.2)),
        ):
            with self.subTest(interval=interval):
                cache.clear()
                FleetOperation.objects.filter(pk=self.operation.pk).update(
                    last_esi_update=self.start - timedelta(minutes=10)
                )
                f.settings(tracking_interval=interval)

                self.assertEqual(self.run_beats(minutes), self.polls_at(*expected))

    def test_late_worker_paces_failed_polls_the_same_way(self):
        f.settings(tracking_interval=60, auto_end_enabled=False)
        self.esi.members[FLEET_ID] = http_error(502)

        self.assertEqual(self.run_beats(4), self.polls_at(1.2, 61.2, 121.2, 181.2))

    def test_auto_end_with_a_late_worker_takes_the_configured_number_of_intervals(self):
        f.settings(tracking_interval=300, auto_end_enabled=True, auto_end_missing_count=3)
        self.esi.members[FLEET_ID] = http_error(404)

        self.assertEqual(self.run_beats(10), self.polls_at(1.2, 301.2))
        self.operation.refresh_from_db()
        self.assertEqual(self.operation.status, Status.ACTIVE)

        self.assertEqual(self.run_beats(1, first=10), self.polls_at(601.2))
        self.operation.refresh_from_db()
        self.assertEqual(self.operation.status, Status.CLOSED)

    def test_empty_cache_falls_back_to_last_successful_sync(self):
        f.settings(tracking_interval=300)
        self.esi.members[FLEET_ID] = http_error(404)
        with frozen_now(self.start):
            track_operation(self.operation.pk)

        cache.clear()

        self.assertIn(self.operation.pk, self.queued_at(self.start + timedelta(seconds=60)))

    def test_skipped_runs_do_not_count_as_polls(self):
        f.settings(tracking_interval=300)
        cache.add(f"fleetops:track:{self.operation.pk}", "1", timeout=60)
        with frozen_now(self.start):
            self.assertEqual(track_operation(self.operation.pk), "already-running")
        cache.delete(f"fleetops:track:{self.operation.pk}")

        self.assertIn(self.operation.pk, self.queued_at(self.start + timedelta(seconds=60)))
        self.assertEqual(self.esi.requests, [])

    def test_deleted_operation_is_reported_as_missing(self):
        operation_id = self.operation.pk
        self.operation.delete()

        self.assertEqual(track_operation(operation_id), "missing")
        self.assertEqual(track_operation(987_654_321), "missing")

        self.assertIsNone(cache.get(f"fleetops:track:{operation_id}"))
        self.assertEqual(self.esi.requests, [])


# ---------------------------------------------------------------------------
# django-esi's own operations against a recorded HTTP transport
# ---------------------------------------------------------------------------


def _fleet_spec():
    """A minimal ESI OpenAPI document with the fleet operations FleetOps uses."""
    error = {"description": "Error", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Error"}}}}

    def operation(operation_id, scope, path_params, ok_status, schema=None, request_schema=None):
        spec = {
            "operationId": operation_id,
            "tags": ["Fleets"],
            "parameters": [
                {"name": name, "in": "path", "required": True, "schema": {"type": "integer", "format": "int64"}}
                for name in path_params
            ],
            "security": [{"OAuth2": [scope]}],
            "responses": {
                ok_status: {"description": "OK"},
                "default": error,
            },
            "x-rate-limit": {"group": "fleet", "max-tokens": 1800, "window-size": "15m"},
        }
        if schema:
            spec["responses"][ok_status]["content"] = {"application/json": {"schema": {"$ref": f"#/components/schemas/{schema}"}}}
        if request_schema:
            spec["requestBody"] = {
                "required": True,
                "content": {"application/json": {"schema": {"$ref": f"#/components/schemas/{request_schema}"}}},
            }
        return spec

    integer = {"type": "integer", "format": "int64"}
    return {
        "openapi": "3.1.0",
        "info": {"title": "EVE Stable Infrastructure (ESI) - tranquility", "version": "2025-11-06"},
        "servers": [{"url": "https://esi.evetech.net"}],
        "tags": [{"name": "Fleets"}],
        "paths": {
            "/characters/{character_id}/fleet": {
                "get": operation("GetCharactersCharacterIdFleet", FLEET_READ_SCOPE, ["character_id"], "200", "CharacterFleet"),
            },
            "/fleets/{fleet_id}": {
                "get": operation("GetFleetsFleetId", FLEET_READ_SCOPE, ["fleet_id"], "200", "Fleet"),
                "put": operation("PutFleetsFleetId", FLEET_WRITE_SCOPE, ["fleet_id"], "204", request_schema="FleetUpdate"),
            },
            "/fleets/{fleet_id}/members": {
                "get": operation("GetFleetsFleetIdMembers", FLEET_READ_SCOPE, ["fleet_id"], "200", "FleetMembers"),
            },
            "/fleets/{fleet_id}/members/{member_id}": {
                "delete": operation("DeleteFleetsFleetIdMembersMemberId", FLEET_WRITE_SCOPE, ["fleet_id", "member_id"], "204"),
            },
        },
        "components": {
            "schemas": {
                "Error": {"type": "object", "properties": {"error": {"type": "string"}}},
                "CharacterFleet": {
                    "type": "object",
                    "properties": {"fleet_id": integer, "fleet_boss_id": integer, "role": {"type": "string"}, "squad_id": integer, "wing_id": integer},
                },
                "Fleet": {
                    "type": "object",
                    "properties": {
                        "is_free_move": {"type": "boolean"},
                        "is_registered": {"type": "boolean"},
                        "is_voice_enabled": {"type": "boolean"},
                        "motd": {"type": "string"},
                    },
                },
                "FleetUpdate": {"type": "object", "properties": {"is_free_move": {"type": "boolean"}, "motd": {"type": "string"}}},
                "FleetMembers": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "character_id": integer,
                            "role": {"type": "string"},
                            "role_name": {"type": "string"},
                            "ship_type_id": integer,
                            "solar_system_id": integer,
                            "squad_id": integer,
                            "takes_fleet_warp": {"type": "boolean"},
                            "wing_id": integer,
                        },
                    },
                },
            },
            "securitySchemes": {
                "OAuth2": {
                    "type": "oauth2",
                    "flows": {
                        "authorizationCode": {
                            "authorizationUrl": "https://login.eveonline.com/v2/oauth/authorize",
                            "tokenUrl": "https://login.eveonline.com/v2/oauth/token",
                            "scopes": {FLEET_READ_SCOPE: FLEET_READ_SCOPE, FLEET_WRITE_SCOPE: FLEET_WRITE_SCOPE},
                        }
                    },
                }
            },
        },
    }


class RecordedESI:
    """Answers HTTP requests like ESI: ETags on reads and 304 for a matching If-None-Match."""

    def __init__(self):
        self.routes = {}
        self.requests = []
        self.cache_seconds = 0

    def send(self, request, *args, **kwargs):
        body = json.loads(request.content) if request.content else None
        self.requests.append((request.method, request.url.path, request.headers.get("If-None-Match"), body))
        status, payload = self.routes.get((request.method, request.url.path), (404, {"error": "Not found"}))
        headers = {}
        if self.cache_seconds:
            expires = timezone.now() + timedelta(seconds=self.cache_seconds)
            headers["Expires"] = expires.strftime("%a, %d %b %Y %H:%M:%S GMT")
        if request.method == "GET" and status == 200:
            etag = f'"{hashlib.md5(json.dumps(payload, sort_keys=True).encode()).hexdigest()}"'
            headers["ETag"] = etag
            if request.headers.get("If-None-Match") == etag:
                return openapi_clients.Response(304, headers=headers, request=request)
        if status == 204:
            return openapi_clients.Response(204, headers=headers, request=request)
        return openapi_clients.Response(status, json=payload, headers=headers, request=request)

    def requests_of(self, method):
        return [request for request in self.requests if request[0] == method]


class DjangoEsiOperationTests(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.spec_dir = tempfile.mkdtemp()
        spec_file = os.path.join(cls.spec_dir, "esi.json")
        with open(spec_file, "w") as fp:
            json.dump(_fleet_spec(), fp)
        cls.provider = ESIClientProvider(
            compatibility_date="2025-11-06",
            ua_appname="aa-fleetops-tests",
            ua_version="0.0.0",
            spec_file=spec_file,
            operations=list(FakeFleetsAPI.OPERATIONS),
        )

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.spec_dir, ignore_errors=True)
        super().tearDownClass()

    def setUp(self):
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)
        self.server = RecordedESI()
        # Every HTTP request django-esi makes goes to the recorded ESI instead of the network.
        send_patcher = mock.patch.object(
            openapi_clients.Client,
            "send",
            autospec=True,
            side_effect=lambda client, request, *args, **kwargs: self.server.send(request),
        )
        send_patcher.start()
        self.addCleanup(send_patcher.stop)
        provider_patcher = mock.patch.object(esi_provider, "esi", self.provider)
        provider_patcher.start()
        self.addCleanup(provider_patcher.stop)

        self.fc = f.create_user(perms=f.FC_PERMS)
        self.fc_char = f.main_of(self.fc)
        self.token = f.add_token(self.fc, self.fc_char)
        self.pilot_char = f.main_of(f.create_user())
        self.server.routes = {
            ("GET", f"/characters/{self.fc_char.character_id}/fleet"): (
                200,
                {"fleet_id": FLEET_ID, "fleet_boss_id": self.fc_char.character_id, "role": "fleet_commander", "squad_id": -1, "wing_id": -1},
            ),
            ("GET", f"/fleets/{FLEET_ID}"): (200, {"is_free_move": True, "is_registered": False, "is_voice_enabled": False, "motd": "old"}),
            ("PUT", f"/fleets/{FLEET_ID}"): (204, None),
            ("GET", f"/fleets/{FLEET_ID}/members"): (
                200,
                [
                    {"character_id": self.fc_char.character_id, "role": "fleet_commander", "ship_type_id": 11987, "solar_system_id": 30000142, "squad_id": -1, "wing_id": -1},
                    {"character_id": self.pilot_char.character_id, "role": "squad_member", "ship_type_id": 11987, "solar_system_id": 30000142, "squad_id": 1, "wing_id": 1},
                ],
            ),
            ("DELETE", f"/fleets/{FLEET_ID}/members/{self.pilot_char.character_id}"): (204, None),
        }

    def test_django_esi_defaults_raise_not_modified_for_an_unchanged_read(self):
        self.provider.client.Fleets.GetFleetsFleetIdMembers(fleet_id=FLEET_ID, token=self.token).result()

        with self.assertRaises(HTTPNotModified):
            self.provider.client.Fleets.GetFleetsFleetIdMembers(fleet_id=FLEET_ID, token=self.token).result()

    def test_unchanged_reads_return_data_every_time(self):
        for _ in range(2):
            self.assertEqual(detect_character_fleet(self.fc, self.fc_char.character_id).fleet_id, FLEET_ID)
            self.assertTrue(get_fleet_info(self.fc, self.fc_char.character_id, FLEET_ID)["is_free_move"])
            members = get_fleet_members(self.fc, self.fc_char.character_id, FLEET_ID)
            self.assertEqual({row["character_id"] for row in members}, {self.fc_char.character_id, self.pilot_char.character_id})

        self.assertEqual(len(self.server.requests), 6)
        self.assertEqual({request[2] for request in self.server.requests}, {None})

    def test_cached_reads_return_data(self):
        self.server.cache_seconds = 300

        for _ in range(2):
            self.assertEqual(len(get_fleet_members(self.fc, self.fc_char.character_id, FLEET_ID)), 2)
            self.assertEqual(detect_character_fleet(self.fc, self.fc_char.character_id).role, "fleet_commander")

        self.assertEqual(len(self.server.requests), 2)

    def test_motd_write_sends_the_request_body(self):
        get_fleet_info(self.fc, self.fc_char.character_id, FLEET_ID)

        set_fleet_motd(self.fc, self.fc_char.character_id, FLEET_ID, "Form up in Jita")

        self.assertEqual(
            self.server.requests_of("PUT"),
            [("PUT", f"/fleets/{FLEET_ID}", None, {"motd": "Form up in Jita", "is_free_move": True})],
        )

    def test_writes_are_sent_every_time_even_if_esi_marks_them_cacheable(self):
        self.server.cache_seconds = 300

        set_fleet_motd(self.fc, self.fc_char.character_id, FLEET_ID, "first")
        set_fleet_motd(self.fc, self.fc_char.character_id, FLEET_ID, "second")
        kick_fleet_member(self.fc, self.fc_char.character_id, FLEET_ID, self.pilot_char.character_id)
        kick_fleet_member(self.fc, self.fc_char.character_id, FLEET_ID, self.pilot_char.character_id)

        self.assertEqual([request[3]["motd"] for request in self.server.requests_of("PUT")], ["first", "second"])
        self.assertEqual(len(self.server.requests_of("DELETE")), 2)

    def test_error_statuses_are_mapped(self):
        members = ("GET", f"/fleets/{FLEET_ID}/members")
        for status, code in ((404, "FLEET_NOT_FOUND"), (500, "ESI_ERROR"), (420, "ESI_ERROR")):
            with self.subTest(status=status):
                cache.clear()
                self.server.routes[members] = (status, {"error": "failed"})
                with self.assertRaises(FleetESIError) as ctx:
                    get_fleet_members(self.fc, self.fc_char.character_id, FLEET_ID)
                self.assertEqual(ctx.exception.code, code)
                self.assertNotIn(self.token.access_token, str(ctx.exception))
