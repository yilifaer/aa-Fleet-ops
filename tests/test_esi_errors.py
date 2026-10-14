# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Short ESI error texts: status and reason only, never the raw django-esi exception text."""

import uuid
from types import SimpleNamespace

import requests
from aiopenapi3.errors import HTTPClientError as BaseHTTPClientError
from aiopenapi3.errors import RequestError
from django.test import TestCase
from django.urls import reverse
from esi.exceptions import ESIBucketLimitException, ESIErrorLimitException, HTTPClientError, HTTPServerError

from fleetops.models import FleetOperation, OperationAction
from fleetops.providers import esi as esi_provider
from fleetops.providers.esi import (
    HTTP_CLIENTS,
    FleetESIError,
    describe_esi_failure,
    detect_character_fleet,
    get_fleet_info,
    get_fleet_members,
    kick_fleet_member,
    set_fleet_motd,
)
from fleetops.services.operations import retry_motd
from fleetops.tasks import track_operation

from . import factories as f
from .test_operations_tracking import FLEET_ID, FakeESIMixin

SECRET_HEADER = "set-cookie-secret-value"
# The HTTP client of the installed django-esi: httpx for django-esi 9, httpx2 for django-esi 10.
http_client = HTTP_CLIENTS[0]


def esi_error(status, error=None, request_id="req-abc-123"):
    headers = {"X-ESI-Request-ID": request_id, "Set-Cookie": SECRET_HEADER, "Content-Type": "application/json"}
    data = {"error": error} if error else None
    if status >= 500:
        return HTTPServerError(status_code=status, headers=headers, data=data)
    return HTTPClientError(status_code=status, headers=headers, data=data)


def error_limit(reset=None, request_id="req-420"):
    """What django-esi raises for a 420: ESIErrorLimitException while handling aiopenapi3's HTTPClientError."""
    headers = {"X-ESI-Request-ID": request_id, "Set-Cookie": SECRET_HEADER}
    try:
        try:
            raise BaseHTTPClientError(status_code=420, headers=headers, data=None)
        except BaseHTTPClientError:
            raise ESIErrorLimitException(reset=reset)
    except ESIErrorLimitException as exc:
        return exc


def timeout_from_the_http_client():
    # aiopenapi3 wraps httpx transport errors in its own RequestError.
    try:
        try:
            raise http_client.ReadTimeout("read timed out")
        except http_client.ReadTimeout as exc:
            raise RequestError(None, None, None, None) from exc
    except RequestError as wrapped:
        return wrapped


class DescribeFailureTests(TestCase):
    def test_http_statuses_use_the_esi_error_field_or_the_reason_phrase(self):
        cases = [
            (esi_error(404), "ESI returned HTTP 404 (Not found)."),
            (esi_error(404, "Fleet not found"), "ESI returned HTTP 404 (Fleet not found)."),
            (esi_error(403), "ESI returned HTTP 403 (Forbidden)."),
            # Only the formatter: django-esi turns a real 420 into ESIErrorLimitException (see error_limit).
            (esi_error(420), "ESI returned HTTP 420 (Error limited)."),
            (esi_error(500), "ESI returned HTTP 500 (Internal server error)."),
            (esi_error(502), "ESI returned HTTP 502 (Bad gateway)."),
            (esi_error(503), "ESI returned HTTP 503 (Service unavailable)."),
            (esi_error(504, "Timeout contacting tranquility"), "ESI returned HTTP 504 (Timeout contacting tranquility)."),
            (esi_error(599), "ESI returned HTTP 599 (unknown status)."),
        ]
        for exc, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(describe_esi_failure(exc), expected)

    def test_raw_exception_text_would_leak_headers(self):
        exc = esi_error(503)

        self.assertIn(SECRET_HEADER, str(exc))
        self.assertNotIn(SECRET_HEADER, describe_esi_failure(exc))
        self.assertNotIn("req-abc-123", describe_esi_failure(exc))

    def test_non_http_failures(self):
        cases = [
            (ESIErrorLimitException(reset=30), "ESI error limit reached; try again in 30 seconds."),
            (error_limit(reset=30), "ESI error limit reached; try again in 30 seconds."),
            (ESIErrorLimitException(), "ESI error limit reached."),
            (ESIBucketLimitException(bucket="fleet", reset=10), "ESI rate limit reached; try again later."),
            (requests.ReadTimeout("slow"), "ESI did not respond in time."),
            (TimeoutError("timed out"), "ESI did not respond in time."),
            (timeout_from_the_http_client(), "ESI did not respond in time."),
            (requests.ConnectionError("reset"), "Could not connect to ESI."),
            (http_client.ConnectError("refused"), "Could not connect to ESI."),
            (ValueError(f"unexpected {SECRET_HEADER}"), "Unexpected ESI error (ValueError)."),
        ]
        for exc, expected in cases:
            with self.subTest(exc=repr(exc)):
                self.assertEqual(describe_esi_failure(exc), expected)

    def test_long_or_odd_error_fields_are_shortened_to_one_line(self):
        long_error = "word " * 60
        self.assertLessEqual(len(describe_esi_failure(esi_error(500, long_error))), 160)
        self.assertEqual(
            describe_esi_failure(esi_error(500, "line one\nline two.")), "ESI returned HTTP 500 (line one line two)."
        )
        raw = HTTPServerError(status_code=500, headers={}, data=b'{"error": "from bytes"}')
        self.assertEqual(describe_esi_failure(raw), "ESI returned HTTP 500 (from bytes).")
        raw = HTTPServerError(status_code=500, headers={}, data="<html>bad gateway</html>")
        self.assertEqual(describe_esi_failure(raw), "ESI returned HTTP 500 (Internal server error).")


class ProviderErrorTextTests(FakeESIMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.user = f.create_user(perms=f.FC_PERMS)
        self.char = f.main_of(self.user)
        self.token = f.add_token(self.user, self.char)

    def assert_short_error(self, call, expected, code):
        with self.assertLogs(esi_provider.logger, "WARNING") as logs, self.assertRaises(FleetESIError) as ctx:
            call()
        self.assertEqual(str(ctx.exception), expected)
        self.assertEqual(ctx.exception.code, code)
        log = "\n".join(logs.output)
        for text in (str(ctx.exception), log):
            self.assertNotIn(SECRET_HEADER, text)
            self.assertNotIn(self.token.access_token, text)
            self.assertNotIn(self.token.refresh_token, text)
        return log

    def test_member_reads(self):
        cases = [
            (esi_error(404), "Could not read fleet members: ESI returned HTTP 404 (Not found).", "FLEET_NOT_FOUND"),
            (esi_error(403), "Could not read fleet members: ESI returned HTTP 403 (Forbidden).", "ESI_ERROR"),
            (error_limit(reset=30), "Could not read fleet members: ESI error limit reached; try again in 30 seconds.", "ESI_ERROR"),
            (esi_error(503), "Could not read fleet members: ESI returned HTTP 503 (Service unavailable).", "ESI_ERROR"),
            (requests.ReadTimeout("slow"), "Could not read fleet members: ESI did not respond in time.", "ESI_ERROR"),
            (KeyError("payload"), "Could not read fleet members: Unexpected ESI error (KeyError).", "ESI_ERROR"),
        ]
        for outcome, expected, code in cases:
            with self.subTest(expected=expected):
                self.esi.members[FLEET_ID] = outcome
                self.assert_short_error(
                    lambda: get_fleet_members(self.user, self.char.character_id, FLEET_ID), expected, code
                )

    def test_status_and_request_id_are_logged(self):
        self.esi.members[FLEET_ID] = esi_error(503, request_id="req-503-xyz")

        log = self.assert_short_error(
            lambda: get_fleet_members(self.user, self.char.character_id, FLEET_ID),
            "Could not read fleet members: ESI returned HTTP 503 (Service unavailable).",
            "ESI_ERROR",
        )

        self.assertIn("WARNING", log)
        self.assertIn("Could not read fleet members (HTTPServerError, HTTP status 503, x-esi-request-id req-503-xyz)", log)

    def test_error_limit_logs_the_status_and_request_id_of_the_420_answer(self):
        self.esi.members[FLEET_ID] = error_limit(reset=30, request_id="req-420-xyz")

        log = self.assert_short_error(
            lambda: get_fleet_members(self.user, self.char.character_id, FLEET_ID),
            "Could not read fleet members: ESI error limit reached; try again in 30 seconds.",
            "ESI_ERROR",
        )

        self.assertIn(
            "Could not read fleet members (ESIErrorLimitException, HTTP status 420, x-esi-request-id req-420-xyz)", log
        )

    def test_request_id_is_taken_from_the_response_when_the_error_has_no_headers(self):
        response = SimpleNamespace(status_code=502, headers={"x-esi-request-id": "req-from-response"})
        failure = ValueError("unexpected content type text/html")
        failure.response = response
        self.esi.members[FLEET_ID] = failure

        log = self.assert_short_error(
            lambda: get_fleet_members(self.user, self.char.character_id, FLEET_ID),
            "Could not read fleet members: ESI returned HTTP 502 (Bad gateway).",
            "ESI_ERROR",
        )

        self.assertIn("(ValueError, HTTP status 502, x-esi-request-id req-from-response)", log)

    def test_other_calls_name_their_step(self):
        self.esi.character_fleets[self.char.character_id] = esi_error(502)
        self.assert_short_error(
            lambda: detect_character_fleet(self.user, self.char.character_id),
            "Could not detect fleet: ESI returned HTTP 502 (Bad gateway).",
            "ESI_ERROR",
        )
        self.esi.character_fleets[self.char.character_id] = esi_error(403)
        self.assert_short_error(
            lambda: detect_character_fleet(self.user, self.char.character_id),
            "ESI denied access to this character's fleet: ESI returned HTTP 403 (Forbidden).",
            "ESI_FORBIDDEN",
        )
        self.esi.fleet_info[FLEET_ID] = esi_error(504)
        self.assert_short_error(
            lambda: get_fleet_info(self.user, self.char.character_id, FLEET_ID),
            "Could not read fleet information: ESI returned HTTP 504 (Gateway timeout).",
            "ESI_ERROR",
        )
        self.esi.fleet_info[FLEET_ID] = {"is_free_move": False, "motd": ""}
        self.esi.put_outcome = esi_error(403)
        self.assert_short_error(
            lambda: set_fleet_motd(self.user, self.char.character_id, FLEET_ID, "motd"),
            "ESI denied permission to update the fleet MOTD: ESI returned HTTP 403 (Forbidden).",
            "MOTD_FORBIDDEN",
        )
        self.esi.kick_outcomes = {5: esi_error(404), 6: esi_error(500), 7: esi_error(403), 8: error_limit(reset=60)}
        self.assert_short_error(
            lambda: kick_fleet_member(self.user, self.char.character_id, FLEET_ID, 5),
            "Fleet member 5 was not found: ESI returned HTTP 404 (Not found).",
            "MEMBER_NOT_FOUND",
        )
        self.assert_short_error(
            lambda: kick_fleet_member(self.user, self.char.character_id, FLEET_ID, 6),
            "Could not kick fleet member 6: ESI returned HTTP 500 (Internal server error).",
            "FLEET_WRITE_ERROR",
        )
        # A kick batch stops on these like every fleet job (fleet_jobs.STOP_CODES).
        self.assert_short_error(
            lambda: kick_fleet_member(self.user, self.char.character_id, FLEET_ID, 7),
            "ESI denied permission to kick this fleet member: ESI returned HTTP 403 (Forbidden).",
            "FLEET_WRITE_FORBIDDEN",
        )
        self.assert_short_error(
            lambda: kick_fleet_member(self.user, self.char.character_id, FLEET_ID, 8),
            "Could not kick fleet member 8: ESI error limit reached; try again in 60 seconds.",
            "FLEET_WRITE_LIMITED",
        )

    def test_not_being_in_a_fleet_is_not_a_warning(self):
        self.esi.character_fleets.clear()

        with self.assertLogs(esi_provider.logger, "INFO") as logs, self.assertRaises(FleetESIError) as ctx:
            detect_character_fleet(self.user, self.char.character_id)

        self.assertEqual(str(ctx.exception), "Character is currently not in a fleet.")
        self.assertTrue(all(line.startswith("INFO") for line in logs.output))


class StoredErrorTextTests(FakeESIMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.fc = f.create_user(perms=f.FC_PERMS, main_name="Boss Main")
        self.fc_char = f.main_of(self.fc)
        f.add_token(self.fc, self.fc_char)

    def test_tracking_error_on_the_fleet_page_is_short(self):
        operation = f.create_operation(self.fc, esi_fleet_id=FLEET_ID)
        self.esi.members[FLEET_ID] = esi_error(503)

        self.assertEqual(track_operation(operation.pk), "ESI_ERROR")

        operation.refresh_from_db()
        self.assertEqual(operation.last_error, "Could not read fleet members: ESI returned HTTP 503 (Service unavailable).")
        self.client.force_login(self.fc)
        page = self.client.get(reverse("fleetops:operation_detail", args=[operation.uuid]))
        self.assertContains(page, "Could not read fleet members: ESI returned HTTP 503 (Service unavailable).")
        self.assertNotContains(page, SECRET_HEADER)
        self.assertNotContains(page, "req-abc-123")

    def test_motd_step_error_is_short(self):
        operation = f.create_operation(self.fc, esi_fleet_id=FLEET_ID, motd_text="Form up")
        self.esi.fleet_info[FLEET_ID] = {"is_free_move": False, "motd": ""}
        self.esi.put_outcome = esi_error(500, "Internal error occurred")

        step = retry_motd(operation)

        self.assertEqual(step.status, OperationAction.Status.FAILED)
        self.assertEqual(
            step.error_message, "Could not update fleet MOTD: ESI returned HTTP 500 (Internal error occurred)."
        )

    def test_start_form_shows_the_short_detection_error(self):
        self.esi.character_fleets[self.fc_char.character_id] = esi_error(502)
        self.client.force_login(self.fc)

        response = self.client.post(
            reverse("fleetops:start_fleet"),
            {
                "request_id": str(uuid.uuid4()),
                "operation_mode": "full",
                "fc_character_id": str(self.fc_char.character_id),
                "fleet_type": str(f.fleet_type().pk),
                "formup": "Jita",
            },
        )

        self.assertContains(response, "Could not detect fleet: ESI returned HTTP 502 (Bad gateway).")
        self.assertNotContains(response, SECRET_HEADER)
        self.assertFalse(FleetOperation.objects.exists())
