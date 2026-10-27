# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Fleet tokens on the start page: the R/W markers come from the database (no call to EVE SSO while the page
is drawn), and an EVE SSO that cannot be reached is an error message, never a server error."""

import logging
import uuid
from datetime import timedelta
from unittest import mock

import requests
from django.db import connection
from django.test import Client, TestCase, override_settings, tag
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone, translation
from esi.models import Token

from fleetops import texts
from fleetops.constants import FLEET_READ_SCOPE
from fleetops.forms import character_choices
from fleetops.models import FleetOperation, GroupAccess
from fleetops.providers import esi as esi_provider
from fleetops.providers.esi import FleetESIError, fleet_token_flags, get_token

from . import factories as f
from .test_operations_tracking import FakeESIMixin

# Older than django-esi's ESI_TOKEN_VALID_DURATION (about 20 minutes): the access token has expired.
EXPIRED = timedelta(hours=2)


def expire(*tokens):
    Token.objects.filter(pk__in=[token.pk for token in tokens]).update(created=timezone.now() - EXPIRED)


class TokenTestBase(FakeESIMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.fc = f.create_user("fc", perms=f.FC_PERMS, main_name="Example FC")
        self.main = f.main_of(self.fc)
        self.token = f.add_token(self.fc, self.main)
        expire(self.token)
        self.fleet_type = f.fleet_type("StratOp", "1.00")
        refresh = mock.patch.object(Token, "refresh", side_effect=requests.ConnectionError("EVE SSO unreachable"))
        self.refresh = refresh.start()
        self.addCleanup(refresh.stop)

    def client_for(self, user):
        client = Client()
        client.force_login(user)
        return client


class TokenMarkerTests(TokenTestBase):
    def test_the_markers_come_from_the_database(self):
        alt = f.add_alt(self.fc, "Pilot A")
        read_only = f.add_token(self.fc, alt, scopes=[FLEET_READ_SCOPE])
        dead = f.add_alt(self.fc, "Pilot B")
        dead_token = f.add_token(self.fc, dead)
        Token.objects.filter(pk=dead_token.pk).update(refresh_token=None)
        expire(dead_token)
        fresh_without_refresh = f.add_alt(self.fc, "Pilot C")
        Token.objects.filter(pk=f.add_token(self.fc, fresh_without_refresh).pk).update(refresh_token=None)

        flags = fleet_token_flags(self.fc, [self.main.character_id, alt.character_id, dead.character_id,
                                            fresh_without_refresh.character_id, 90000099])

        self.assertEqual(
            flags,
            {
                self.main.character_id: (True, True),
                alt.character_id: (True, False),
                dead.character_id: (False, False),
                fresh_without_refresh.character_id: (True, True),
                90000099: (False, False),
            },
        )
        labels = dict(character_choices(self.fc))
        self.assertTrue(labels[str(self.main.character_id)].endswith("R✓ W✓"))
        self.assertTrue(labels[str(alt.character_id)].endswith("R✓ W✗"))
        self.assertTrue(labels[str(dead.character_id)].endswith("R✗ W✗"))
        self.refresh.assert_not_called()
        # Nothing is deleted either: django-esi removes a token only when a real read refreshes it.
        self.assertEqual(Token.objects.filter(pk__in=[read_only.pk, dead_token.pk]).count(), 2)

    def test_the_token_queries_do_not_grow_with_the_characters(self):
        def queries():
            user = type(self.fc).objects.select_related("profile__main_character").get(pk=self.fc.pk)
            with CaptureQueriesContext(connection) as captured:
                character_choices(user)
            return len(captured)

        one = queries()
        for name in ("Pilot A", "Pilot B", "Pilot C"):
            f.add_token(self.fc, f.add_alt(self.fc, name))

        self.assertEqual(queries(), one)
        self.assertLessEqual(one, 2)


class StartPageTests(TokenTestBase):
    def test_the_start_page_of_an_fc_draws_without_eve_sso(self):
        response = self.client_for(self.fc).get(reverse("fleetops:start_fleet"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "R✓ W✓")
        self.refresh.assert_not_called()
        self.assertTrue(Token.objects.filter(pk=self.token.pk).exists())

    def test_the_start_page_of_a_group_leader_draws_without_eve_sso(self):
        leader = f.create_user("leader", perms=f.MEMBER_PERMS, main_name="Pilot D")
        expire(f.add_token(leader, f.main_of(leader)))
        group = f.aa_group("Example Fleet Group", leaders=[leader])
        f.group_access(group, GroupAccess.Mode.FLEETS, types=[f.fleet_type("Group Activity", "0.00")])

        response = self.client_for(leader).get(reverse("fleetops:start_fleet"))

        self.assertEqual(response.status_code, 200)
        self.refresh.assert_not_called()

    def test_a_start_while_eve_sso_is_unreachable_is_an_error_on_the_page(self):
        data = {
            "request_id": str(uuid.uuid4()),
            "operation_mode": "attendance_only",
            "fleet_timing": "now",
            "fc_character_id": str(self.main.character_id),
            "fleet_type": str(self.fleet_type.pk),
            "formup": "Jita",
        }

        with self.assertLogs(esi_provider.logger, logging.WARNING) as logs:
            response = self.client_for(self.fc).post(reverse("fleetops:start_fleet"), data)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, texts.ESI_TOKEN_REFRESH_FAILED(error_type="ConnectionError"))
        self.assertFalse(FleetOperation.objects.exists())
        self.assertEqual(self.esi.calls, [])
        self.refresh.assert_called_once()
        output = "\n".join(logs.output)
        self.assertIn("ConnectionError", output)
        for secret in (self.token.access_token, self.token.refresh_token, "EVE SSO unreachable"):
            self.assertNotIn(secret, output)

    def test_fleet_detection_answers_with_the_error(self):
        response = self.client_for(self.fc).get(reverse("fleetops:detect_fleet", args=[self.main.character_id]))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                "ok": False,
                "code": "TOKEN_REFRESH_FAILED",
                "message": texts.ESI_TOKEN_REFRESH_FAILED(error_type="ConnectionError"),
            },
        )


class GetTokenTests(TokenTestBase):
    def test_a_refresh_that_cannot_reach_eve_sso_is_a_fleet_esi_error(self):
        with self.assertRaises(FleetESIError) as raised, self.assertLogs(esi_provider.logger, logging.WARNING):
            get_token(self.fc, self.main.character_id)

        self.assertEqual(raised.exception.code, "TOKEN_REFRESH_FAILED")
        self.assertEqual(str(raised.exception), texts.ESI_TOKEN_REFRESH_FAILED(error_type="ConnectionError"))
        self.assertTrue(Token.objects.filter(pk=self.token.pk).exists())

    def test_a_token_that_is_still_valid_needs_no_refresh(self):
        Token.objects.filter(pk=self.token.pk).update(created=timezone.now())

        self.assertEqual(get_token(self.fc, self.main.character_id, write=True), self.token)
        self.refresh.assert_not_called()


@tag("catalog")
@override_settings(LANGUAGE_CODE="zh-hans")
class ChineseTokenErrorTests(TokenTestBase):
    """The refresh error is shown in the viewer's language (needs the compiled catalog)."""

    def test_fleet_detection_answers_in_chinese(self):
        client = Client(HTTP_ACCEPT_LANGUAGE="zh-hans")
        client.force_login(self.fc)

        with translation.override("zh-hans"):
            response = client.get(reverse("fleetops:detect_fleet", args=[self.main.character_id]))
            expected = texts.translate_stored(texts.ESI_TOKEN_REFRESH_FAILED(error_type="ConnectionError"))

        self.assertEqual(response.json()["code"], "TOKEN_REFRESH_FAILED")
        self.assertNotIn("could not be refreshed", expected)
        self.assertEqual(response.json()["message"], expected)
