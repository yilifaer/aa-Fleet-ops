# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Tests for SRP killmail intake: pasted references, zKillboard, ESI killmails and loss values.

No request leaves the test run: zKillboard is reached through a patched ``requests.get`` and
ESI through a fake django-esi client.
"""

import json
from datetime import datetime, timedelta, timezone as dt_timezone
from decimal import Decimal
from email.utils import format_datetime
from importlib import import_module
from types import SimpleNamespace
from unittest import mock

import pydantic
import requests
from django.core.cache import cache
from django.test import SimpleTestCase, override_settings
from esi.exceptions import (
    ESIBucketLimitException,
    ESIErrorLimitException,
    HTTPClientError,
    HTTPNotModified,
    HTTPServerError,
)
from requests.structures import CaseInsensitiveDict

import fleetops
from fleetops.models import MAX_DATABASE_ID
from fleetops.providers import esi as esi_provider, esi_killmails, zkillboard
from fleetops.providers.esi import FleetESIError
from fleetops.srp import killmails, values
from fleetops.srp.killmails import (
    MAX_INPUT_LENGTH,
    UNRECOGNISED,
    killmail_fields,
    parse_killmail_reference,
)
from fleetops.srp.types import KillmailRef, SrpError, ZkillResult

UTC = dt_timezone.utc
KILL_ID = 131_234_567
HASH = "0123456789abcdef0123456789abcdef01234567"
KILL_TIME = datetime(2026, 10, 1, 19, 30, tzinfo=UTC)
NOW = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)
RIFTER = 587
CAPSULE = 670


def esi_killmail_payload(**overrides):
    """A killmail shaped like ESI's GetKillmailsKillmailIdKillmailHash answer."""
    payload = {
        "killmail_id": KILL_ID,
        "killmail_time": "2026-10-01T19:30:00Z",
        "solar_system_id": 30_002_187,
        "moon_id": None,
        "war_id": None,
        "attackers": [
            {"character_id": 95_000_001, "corporation_id": 98_000_001, "ship_type_id": 17_738, "final_blow": True},
            {"character_id": None, "corporation_id": 1_000_125, "ship_type_id": None, "final_blow": False},
        ],
        "victim": {
            "character_id": 95_000_100,
            "corporation_id": 98_000_100,
            "alliance_id": 99_000_100,
            "ship_type_id": RIFTER,
            "damage_taken": 2_345,
            "position": {"x": 1.0, "y": 2.0, "z": 3.0},
            "items": [
                {"flag": 27, "item_type_id": 2_873, "quantity_destroyed": 1, "singleton": 0},
                {"flag": 5, "item_type_id": 34, "quantity_dropped": 1000, "singleton": 0},
            ],
        },
    }
    payload.update(overrides)
    return payload


def zkill_item(killmail_id=KILL_ID, **zkb):
    """One entry of zKillboard's /api/killID/<id>/ answer."""
    block = {
        "locationID": 40_139_385,
        "hash": HASH,
        "fittedValue": 10_234_567.89,
        "droppedValue": 120_000.5,
        "destroyedValue": 9_800_000,
        "totalValue": 12_500_000.25,
        "points": 1,
        "npc": False,
        "solo": True,
        "awox": False,
        "labels": ["cat:6", "solo", "pvp", "loc:lowsec"],
    }
    block.update(zkb)
    return dict(esi_killmail_payload(killmail_id=killmail_id), zkb=block)


def http_client_timeout():
    # django-esi 9 uses httpx, django-esi 10 httpx2.
    for name in ("httpx", "httpx2"):
        try:
            return import_module(name).ReadTimeout("timed out")
        except ImportError:
            continue
    return TimeoutError("timed out")


class FakeResponse:
    def __init__(self, status=200, body=None, *, text=None, headers=None):
        self.status_code = status
        self.headers = CaseInsensitiveDict(headers or {})
        self.text = text if text is not None else json.dumps(body)

    def json(self):
        return json.loads(self.text)


# ---------------------------------------------------------------------------
# Pasted killmail references
# ---------------------------------------------------------------------------


class ParseKillmailReferenceTests(SimpleTestCase):
    def assertRef(self, text, killmail_id, killmail_hash, source):
        self.assertEqual(parse_killmail_reference(text), KillmailRef(killmail_id, killmail_hash, source))

    def assertRejected(self, text, code="unrecognised"):
        with self.assertRaises(SrpError) as ctx:
            parse_killmail_reference(text)
        self.assertEqual(ctx.exception.code, code)
        return ctx.exception

    def test_in_game_link_as_pasted_from_chat(self):
        self.assertRef(f"<url=killReport:{KILL_ID}:{HASH}>Kill: Pilot (Rifter)</url>", KILL_ID, HASH, "ingame")

    def test_in_game_link_as_html_anchor(self):
        self.assertRef(f'<a href="killReport:{KILL_ID}:{HASH}">Kill: Pilot (Rifter)</a>', KILL_ID, HASH, "ingame")

    def test_in_game_link_inside_other_text(self):
        self.assertRef(f"please srp  killReport:{KILL_ID}:{HASH} thanks", KILL_ID, HASH, "ingame")

    def test_upper_case_hash_is_lower_cased(self):
        self.assertRef(f"<url=killReport:{KILL_ID}:{HASH.upper()}>x</url>", KILL_ID, HASH, "ingame")

    def test_in_game_link_wins_over_a_killboard_link(self):
        text = f"https://zkillboard.com/kill/999/ <url=killReport:{KILL_ID}:{HASH}>x</url>"
        self.assertRef(text, KILL_ID, HASH, "ingame")

    def test_esi_urls(self):
        for prefix in ("latest/", "v1/", "v2/", "dev/", ""):
            with self.subTest(prefix=prefix):
                url = f"https://esi.evetech.net/{prefix}killmails/{KILL_ID}/{HASH}/"
                self.assertRef(url, KILL_ID, HASH, "esi_url")

    def test_esi_url_query_string_and_missing_scheme_are_ignored(self):
        self.assertRef(
            f"esi.evetech.net/latest/killmails/{KILL_ID}/{HASH.upper()}/?datasource=tranquility",
            KILL_ID,
            HASH,
            "esi_url",
        )

    def test_id_and_hash(self):
        for separator in (" ", ":", "/", " : ", "\t", "  /  "):
            with self.subTest(separator=separator):
                self.assertRef(f"  {KILL_ID}{separator}{HASH}\n", KILL_ID, HASH, "id_hash")

    def test_id_and_hash_must_be_the_whole_text(self):
        self.assertRejected(f"kill {KILL_ID} {HASH}")
        self.assertRejected(f"{KILL_ID} {HASH} please")

    def test_zkillboard_urls(self):
        for url in (
            f"https://zkillboard.com/kill/{KILL_ID}/",
            f"https://zkillboard.com/kill/{KILL_ID}",
            f"http://www.zkillboard.com/kill/{KILL_ID}/",
            f"zkillboard.com/kill/{KILL_ID}/",
            f"https://zkillboard.com/kill/{KILL_ID}/#items",
            f"https://zkillboard.com/kill/{KILL_ID}/?utm=x",
            f"My loss: <https://zkillboard.com/kill/{KILL_ID}/> thanks",
        ):
            with self.subTest(url=url):
                self.assertRef(url, KILL_ID, None, "zkillboard")

    def test_other_killboards(self):
        for url in (
            f"https://eve-kill.com/kill/{KILL_ID}",
            f"https://www.eve-kill.com/kill/{KILL_ID}/",
            f"https://kb.evetools.org/kill/{KILL_ID}",
        ):
            with self.subTest(url=url):
                self.assertRef(url, KILL_ID, None, "killboard")

    def test_garbage_is_rejected_with_a_hint(self):
        for text in (
            "my rifter died",
            "<script>alert(1)</script>",
            "https://zkillboard.com/character/95000100/",
            f"https://zkillboard.com/kill/{KILL_ID}abc/",
            f"https://notzkillboard.com/kill/{KILL_ID}/",
            f"https://zkillboard.com.evil.example/kill/{KILL_ID}/",
            f"killReport:{KILL_ID}:{HASH}0",
            f"{KILL_ID} {HASH[:-1]}",
            str(KILL_ID),
            "https://zkillboard.com/kill/\u0661\u0662\u0663/",
            f"\u0661\u0662\u0663 {HASH}",
        ):
            with self.subTest(text=text):
                error = self.assertRejected(text)
                self.assertEqual(error.message, UNRECOGNISED)

    def test_empty_input(self):
        for text in ("", "   \n\t", None, 12345):
            with self.subTest(text=text):
                self.assertRejected(text)

    def test_overlong_input(self):
        link = f"https://zkillboard.com/kill/{KILL_ID}/"
        self.assertRef(" " * 10 + "x" * (MAX_INPUT_LENGTH - len(link) - 1) + " " + link, KILL_ID, None, "zkillboard")
        error = self.assertRejected("x" * MAX_INPUT_LENGTH + " " + link, code="too_long")
        self.assertIn("too long", error.message)

    def test_killmail_id_must_fit_the_database(self):
        self.assertRejected("https://zkillboard.com/kill/0/", code="invalid_id")
        self.assertRejected(f"https://zkillboard.com/kill/{MAX_DATABASE_ID + 1}/", code="invalid_id")
        self.assertRejected(f"https://esi.evetech.net/latest/killmails/{'9' * 30}/{HASH}/", code="invalid_id")
        self.assertRef(f"https://zkillboard.com/kill/{MAX_DATABASE_ID}/", MAX_DATABASE_ID, None, "zkillboard")

    def test_reference_is_immutable(self):
        ref = parse_killmail_reference(f"{KILL_ID} {HASH}")
        with self.assertRaises(AttributeError):
            ref.killmail_id = 1


# ---------------------------------------------------------------------------
# ESI killmail payloads
# ---------------------------------------------------------------------------


class KillmailFieldsTests(SimpleTestCase):
    def test_fields_from_an_esi_payload(self):
        fields = killmail_fields(esi_killmail_payload())
        self.assertEqual(
            {key: value for key, value in fields.items() if key != "items"},
            {
                "killmail_id": KILL_ID,
                "killmail_time": KILL_TIME,
                "solar_system_id": 30_002_187,
                "victim_character_id": 95_000_100,
                "victim_corporation_id": 98_000_100,
                "victim_alliance_id": 99_000_100,
                "ship_type_id": RIFTER,
                "attacker_count": 2,
                "war_id": None,
            },
        )
        self.assertEqual(fields["killmail_time"].tzinfo, UTC)
        self.assertEqual(
            fields["items"],
            [
                {"flag": 27, "item_type_id": 2_873, "quantity_destroyed": 1, "quantity_dropped": None, "singleton": 0},
                {"flag": 5, "item_type_id": 34, "quantity_destroyed": None, "quantity_dropped": 1000, "singleton": 0},
            ],
        )
        json.dumps(fields["items"])

    def test_killmail_time_from_datetimes(self):
        naive = datetime(2026, 10, 1, 19, 30)
        berlin = datetime(2026, 10, 1, 21, 30, tzinfo=dt_timezone(timedelta(hours=2)))
        for value in (KILL_TIME, naive, berlin):
            with self.subTest(value=value):
                self.assertEqual(killmail_fields(esi_killmail_payload(killmail_time=value))["killmail_time"], KILL_TIME)

    def test_structure_or_npc_victim_has_no_character(self):
        payload = esi_killmail_payload()
        payload["victim"] = {"corporation_id": 1_000_125, "ship_type_id": 35_832, "damage_taken": 1}
        fields = killmail_fields(payload)
        self.assertIsNone(fields["victim_character_id"])
        self.assertIsNone(fields["victim_alliance_id"])
        self.assertEqual(fields["items"], [])

    def test_items_are_reduced_and_checked(self):
        payload = esi_killmail_payload()
        payload["victim"]["items"] = [
            {
                "flag": 5,
                "item_type_id": 3_467,
                "quantity_dropped": 1,
                "singleton": 1,
                "note": "<b>dropped</b>",
                "items": [{"flag": 0, "item_type_id": 34, "quantity_destroyed": 50, "singleton": 0}],
            },
            {"flag": 27, "item_type_id": True, "quantity_destroyed": 1, "singleton": 0},
            {"flag": 27, "item_type_id": 2_873, "quantity_destroyed": -1, "singleton": 0},
            {"item_type_id": 2_873, "quantity_destroyed": 1, "singleton": 0},
            "junk",
        ]
        items = killmail_fields(payload)["items"]
        self.assertEqual(
            items,
            [
                {
                    "flag": 5,
                    "item_type_id": 3_467,
                    "quantity_destroyed": None,
                    "quantity_dropped": 1,
                    "singleton": 1,
                    "items": [
                        {
                            "flag": 0,
                            "item_type_id": 34,
                            "quantity_destroyed": 50,
                            "quantity_dropped": None,
                            "singleton": 0,
                        }
                    ],
                }
            ],
        )

    def test_nesting_is_cut_off(self):
        item = {"flag": 0, "item_type_id": 34, "quantity_destroyed": 1, "singleton": 0}
        for _ in range(10):
            item = {"flag": 5, "item_type_id": 3_467, "quantity_dropped": 1, "singleton": 1, "items": [item]}
        payload = esi_killmail_payload()
        payload["victim"]["items"] = [item]
        depth, current = 0, killmail_fields(payload)["items"]
        while current:
            depth += 1
            current = current[0].get("items")
        self.assertEqual(depth, 3)

    def test_item_count_is_limited(self):
        payload = esi_killmail_payload()
        item = {"flag": 0, "item_type_id": 34, "quantity_destroyed": 1, "singleton": 0}
        payload["victim"]["items"] = [dict(item, items=[item, item]), item, item]
        with mock.patch.object(killmails, "MAX_ITEMS", 3):
            items = killmail_fields(payload)["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(len(items[0]["items"]), 2)

    def test_incomplete_payloads_are_refused(self):
        broken = []
        for key in ("killmail_id", "killmail_time", "solar_system_id", "victim"):
            payload = esi_killmail_payload()
            del payload[key]
            broken.append(payload)
        no_ship = esi_killmail_payload()
        del no_ship["victim"]["ship_type_id"]
        broken += [
            no_ship,
            esi_killmail_payload(killmail_id=True),
            esi_killmail_payload(killmail_id=0),
            esi_killmail_payload(killmail_time="yesterday"),
            esi_killmail_payload(victim="nobody"),
            esi_killmail_payload(war_id="1"),
        ]
        for payload in broken:
            with self.subTest(payload=payload), self.assertRaises(SrpError) as ctx:
                killmail_fields(payload)
            self.assertEqual(ctx.exception.code, "invalid_killmail")


# ---------------------------------------------------------------------------
# zKillboard client
# ---------------------------------------------------------------------------


class ZkillboardTestCase(SimpleTestCase):
    def setUp(self):
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)
        patcher = mock.patch("fleetops.providers.zkillboard.requests.get")
        self.get = patcher.start()
        self.addCleanup(patcher.stop)
        self.get.return_value = FakeResponse(200, [zkill_item()])

    def fetch(self, killmail_id=KILL_ID, **kwargs):
        # Every lookup takes the site-wide two-second slot; free it so tests can call again.
        cache.delete(zkillboard.SLOT_KEY)
        kwargs.setdefault("now", NOW)
        return zkillboard.fetch_kill(killmail_id, **kwargs)


class ZkillboardUserAgentTests(SimpleTestCase):
    def setUp(self):
        # The contact tests read the header without the project address.
        patcher = mock.patch.object(zkillboard, "__url__", "")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_contact_email(self):
        self.assertEqual(
            zkillboard.user_agent(),
            f"AA-FleetOps/{zkillboard.__version__} (fleetops-tests@example.com) requests/{requests.__version__}",
        )

    @override_settings(ESI_USER_CONTACT_EMAIL=None, SITE_URL="https://auth.example.org")
    def test_site_url_without_contact_email(self):
        self.assertIn("(https://auth.example.org)", zkillboard.user_agent())

    def test_the_package_url_is_the_project_url(self):
        with mock.patch.object(zkillboard, "__url__", fleetops.__url__):
            self.assertIn(f"(+{fleetops.__url__}; fleetops-tests@example.com)", zkillboard.user_agent())

    def test_project_url(self):
        with mock.patch.object(zkillboard, "__url__", "https://github.com/example/aa-fleetops"):
            self.assertIn(
                "(+https://github.com/example/aa-fleetops; fleetops-tests@example.com)", zkillboard.user_agent()
            )

    @override_settings(ESI_USER_CONTACT_EMAIL="", SITE_URL="")
    def test_no_contact(self):
        self.assertEqual(
            zkillboard.user_agent(), f"AA-FleetOps/{zkillboard.__version__} requests/{requests.__version__}"
        )

    @override_settings(ESI_USER_CONTACT_EMAIL="ops@example.com\r\nX-Evil: 1 (x);")
    def test_contact_is_made_header_safe(self):
        header = zkillboard.user_agent()
        self.assertNotIn("\r", header)
        self.assertNotIn("\n", header)
        self.assertIn("(ops@example.comX-Evil:1x)", header)
        self.assertEqual(header.count("("), 1)


class ZkillboardRequestTests(ZkillboardTestCase):
    def test_request_follows_the_api_rules(self):
        self.fetch()
        self.get.assert_called_once()
        args, kwargs = self.get.call_args
        url = args[0] if args else kwargs["url"]
        self.assertEqual(url, f"https://zkillboard.com/api/killID/{KILL_ID}/")
        self.assertNotIn("?", url)
        self.assertNotIn("params", kwargs)
        self.assertEqual(kwargs["headers"]["User-Agent"], zkillboard.user_agent())
        self.assertEqual(kwargs["headers"]["Accept-Encoding"], "gzip")
        self.assertEqual(kwargs["headers"]["Accept"], "application/json")
        self.assertEqual(kwargs["timeout"], (5, 10))
        self.assertIs(kwargs["allow_redirects"], False)

    def test_kill_url(self):
        self.assertEqual(zkillboard.kill_url(KILL_ID), f"https://zkillboard.com/kill/{KILL_ID}/")

    def test_invalid_id(self):
        for killmail_id in (0, -5):
            with self.subTest(killmail_id=killmail_id), self.assertRaises(ValueError):
                self.fetch(killmail_id)
        self.get.assert_not_called()


class ZkillboardAnswerTests(ZkillboardTestCase):
    def test_full_answer(self):
        result = self.fetch()
        self.assertEqual(
            result,
            ZkillResult(
                status="ok",
                killmail_hash=HASH,
                fitted_value=Decimal("10234567.89"),
                total_value=Decimal("12500000.25"),
                destroyed_value=Decimal("9800000.00"),
                dropped_value=Decimal("120000.50"),
                npc=False,
                solo=True,
                awox=False,
                labels=["cat:6", "solo", "pvp", "loc:lowsec"],
            ),
        )
        self.assertTrue(result.ok)
        self.assertTrue(result.has_value)
        self.assertTrue(result.requested)

    def test_documented_minimal_answer(self):
        item = {"killmail_id": KILL_ID, "zkb": {"hash": HASH.upper(), "totalValue": 1000, "points": 1}}
        self.get.return_value = FakeResponse(200, [item])
        result = self.fetch()
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.killmail_hash, HASH)
        self.assertIsNone(result.fitted_value)
        self.assertEqual(result.total_value, Decimal("1000.00"))
        self.assertIsNone(result.npc)
        self.assertEqual(result.labels, [])
        self.assertTrue(result.has_value)

    def test_answer_with_hash_only_has_no_value(self):
        self.get.return_value = FakeResponse(200, [{"killmail_id": KILL_ID, "zkb": {"hash": HASH}}])
        result = self.fetch()
        self.assertTrue(result.ok)
        self.assertFalse(result.has_value)

    def test_values_are_validated(self):
        for raw in (-1, 10**15, 1e16, True, "1000", None, [1], float("nan"), float("inf")):
            with self.subTest(raw=raw):
                self.get.return_value = FakeResponse(200, [zkill_item(fittedValue=raw)])
                self.assertIsNone(self.fetch().fitted_value)
        self.get.return_value = FakeResponse(200, [zkill_item(fittedValue=999_999_999_999_999)])
        self.assertEqual(self.fetch().fitted_value, Decimal("999999999999999.00"))
        self.get.return_value = FakeResponse(200, [zkill_item(fittedValue=1234.565)])
        self.assertEqual(self.fetch().fitted_value, Decimal("1234.57"))
        self.get.return_value = FakeResponse(200, [zkill_item(fittedValue=0)])
        self.assertEqual(self.fetch().fitted_value, Decimal("0.00"))

    def test_flags_and_labels_are_validated(self):
        labels = ["ok", "", "x" * 51, 7, None] + [f"l{i}" for i in range(60)]
        self.get.return_value = FakeResponse(200, [zkill_item(npc=1, solo="yes", awox=None, labels=labels)])
        result = self.fetch()
        self.assertIsNone(result.npc)
        self.assertIsNone(result.solo)
        self.assertIsNone(result.awox)
        self.assertEqual(len(result.labels), 50)
        self.assertEqual(result.labels[:2], ["ok", "l0"])
        self.get.return_value = FakeResponse(200, [zkill_item(labels="solo")])
        self.assertEqual(self.fetch().labels, [])

    def test_unknown_kill(self):
        self.get.return_value = FakeResponse(200, [])
        result = self.fetch()
        self.assertEqual(result.status, "missing")
        self.assertTrue(result.requested)
        self.assertIn("about 5 minutes", result.message)
        self.assertFalse(result.has_value)

    def test_answer_for_another_kill_counts_as_missing(self):
        self.get.return_value = FakeResponse(200, [zkill_item(killmail_id=KILL_ID + 1), "junk", {"killmail_id": True}])
        self.assertEqual(self.fetch().status, "missing")

    def test_invalid_hash(self):
        for bad in (None, "", "xyz", HASH[:-1], HASH + "0", 12345):
            with self.subTest(hash=bad):
                self.get.return_value = FakeResponse(200, [zkill_item(hash=bad)])
                self.assertEqual(self.fetch().status, "unavailable")
                cache.delete(zkillboard.FAILURES_KEY)
        self.get.return_value = FakeResponse(200, [dict(esi_killmail_payload(), zkb="broken")])
        self.assertEqual(self.fetch().status, "unavailable")

    def test_error_and_unexpected_bodies(self):
        bodies = [
            FakeResponse(200, {"error": "Invalid killID SECRET-BODY"}),
            FakeResponse(200, text="<html>SECRET-BODY</html>"),
            FakeResponse(200, {"killmail_id": KILL_ID}),
            FakeResponse(200, "SECRET-BODY"),
            FakeResponse(500, text="SECRET-BODY"),
            FakeResponse(404, text="SECRET-BODY"),
        ]
        for response in bodies:
            with self.subTest(status=response.status_code, text=response.text):
                cache.delete(zkillboard.FAILURES_KEY)
                self.get.return_value = response
                with self.assertLogs("extensions.fleetops.providers.zkillboard", "WARNING") as logs:
                    result = self.fetch()
                self.assertEqual(result.status, "unavailable")
                self.assertEqual(result.message, zkillboard.MESSAGES["unavailable"])
                output = "\n".join(logs.output)
                self.assertIn(str(KILL_ID), output)
                self.assertNotIn("SECRET-BODY", output)

    def test_network_errors_log_the_class_only(self):
        self.get.side_effect = requests.ConnectionError("https://zkillboard.com/api/killID/1/ SECRET-DETAIL")
        with self.assertLogs("extensions.fleetops.providers.zkillboard", "WARNING") as logs:
            result = self.fetch()
        self.assertEqual(result.status, "unavailable")
        self.assertTrue(result.requested)
        self.assertIn("ConnectionError", logs.output[0])
        self.assertNotIn("SECRET-DETAIL", logs.output[0])


class ZkillboardThrottleTests(ZkillboardTestCase):
    def test_one_request_per_two_seconds(self):
        first = zkillboard.fetch_kill(KILL_ID, now=NOW)
        second = zkillboard.fetch_kill(KILL_ID, now=NOW)
        self.assertEqual(first.status, "ok")
        self.assertEqual(second.status, "throttled")
        self.assertFalse(second.requested)
        self.assertEqual(second.retry_after, 2)
        self.assertEqual(second.message, "zKillboard is busy, try again in a few seconds.")
        self.assertEqual(self.get.call_count, 1)

    def test_slot_expires(self):
        zkillboard.fetch_kill(KILL_ID, now=NOW)
        cache.delete(zkillboard.SLOT_KEY)
        self.assertEqual(zkillboard.fetch_kill(KILL_ID, now=NOW).status, "ok")
        self.assertEqual(self.get.call_count, 2)

    def test_young_kills_are_never_requested(self):
        for age in (timedelta(0), timedelta(minutes=5), timedelta(minutes=5, seconds=59)):
            with self.subTest(age=age):
                result = self.fetch(killmail_time=NOW - age)
                self.assertEqual(result.status, "missing")
                self.assertFalse(result.requested)
                self.assertEqual(result.retry_after, (zkillboard.MIN_KILL_AGE - age).total_seconds())
        future = self.fetch(killmail_time=NOW + timedelta(minutes=1))
        self.assertEqual(future.retry_after, 7 * 60)
        self.get.assert_not_called()

    def test_kill_at_the_minimum_age_is_requested(self):
        self.assertEqual(self.fetch(killmail_time=NOW - zkillboard.MIN_KILL_AGE).status, "ok")
        self.get.assert_called_once()


class ZkillboardBreakerTests(ZkillboardTestCase):
    def test_block_redirect_pauses_for_an_hour_and_warns_once(self):
        self.get.return_value = FakeResponse(302, text="", headers={"Location": "/api/blocked.json"})
        with self.assertLogs("extensions.fleetops.providers.zkillboard", "WARNING") as logs:
            result = self.fetch()
        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.retry_after, 3600)
        self.assertEqual(zkillboard.paused_seconds(NOW), 3600)
        self.assertEqual(len(logs.output), 1)
        self.assertIn("60 minutes", logs.output[0])

        paused = self.fetch(now=NOW + timedelta(minutes=10))
        self.assertEqual(paused.status, "unavailable")
        self.assertFalse(paused.requested)
        self.assertEqual(paused.retry_after, 3000)
        self.assertEqual(self.get.call_count, 1)

        # A second block while the first warning is still recent is not logged again.
        cache.delete(zkillboard.PAUSE_KEY)
        with self.assertNoLogs("fleetops.providers.zkillboard", "WARNING"):
            self.assertEqual(self.fetch().status, "blocked")

    def test_forbidden_is_a_block(self):
        self.get.return_value = FakeResponse(403, text="403 Forbidden - Do not include a query string")
        self.assertEqual(self.fetch().status, "blocked")
        self.assertEqual(zkillboard.paused_seconds(NOW), 3600)

    def test_other_redirects_are_failures(self):
        self.get.return_value = FakeResponse(302, text="", headers={"Location": "https://zkillboard.com/"})
        result = self.fetch()
        self.assertEqual(result.status, "unavailable")
        self.assertEqual(zkillboard.paused_seconds(NOW), 0)
        self.assertEqual(cache.get(zkillboard.FAILURES_KEY), 1)

    def test_slow_down_answers_pause_for_retry_after(self):
        cases = [
            (429, {"Retry-After": "120"}, 120),
            (503, {"Retry-After": "120"}, 120),
            (503, {}, 900),
            (429, {"Retry-After": "soon"}, 900),
            (429, {"Retry-After": "0"}, 60),
            (429, {"Retry-After": "9999999"}, 86400),
            (429, {"Retry-After": format_datetime(NOW + timedelta(minutes=30), usegmt=True)}, 1800),
            (429, {"Retry-After": "Fri, 02 Oct 2026 12:10:00 -0000"}, 600),
        ]
        for status, headers, seconds in cases:
            with self.subTest(status=status, headers=headers):
                cache.delete(zkillboard.PAUSE_KEY)
                self.get.return_value = FakeResponse(status, text="503 Reinforced mode", headers=headers)
                result = self.fetch()
                self.assertEqual(result.status, "unavailable")
                self.assertTrue(result.requested)
                self.assertEqual(result.retry_after, seconds)
                self.assertEqual(zkillboard.paused_seconds(NOW), seconds)

    def test_shorter_pause_does_not_shorten_a_longer_one(self):
        self.get.return_value = FakeResponse(302, text="", headers={"Location": "/api/blocked.json"})
        self.fetch()
        zkillboard._pause(60, NOW)
        self.assertEqual(zkillboard.paused_seconds(NOW), 3600)

    def test_three_failures_in_a_row_pause_for_fifteen_minutes(self):
        self.get.side_effect = requests.Timeout()
        with self.assertLogs("extensions.fleetops.providers.zkillboard", "WARNING") as logs:
            for _ in range(3):
                self.assertEqual(self.fetch().status, "unavailable")
        self.assertEqual(self.get.call_count, 3)
        self.assertEqual(zkillboard.paused_seconds(NOW), 900)
        self.assertTrue(any("15 minutes" in line for line in logs.output))
        self.assertIsNone(cache.get(zkillboard.FAILURES_KEY))

        result = self.fetch()
        self.assertFalse(result.requested)
        self.assertEqual(self.get.call_count, 3)

    def test_success_resets_the_failure_count(self):
        responses = [requests.Timeout(), requests.Timeout(), FakeResponse(200, []), requests.Timeout()]
        self.get.side_effect = responses
        for _ in responses:
            self.fetch()
        self.assertEqual(zkillboard.paused_seconds(NOW), 0)
        self.assertEqual(cache.get(zkillboard.FAILURES_KEY), 1)

    def test_failure_count_survives_an_expired_counter(self):
        self.get.side_effect = requests.Timeout()
        with mock.patch.object(zkillboard.cache, "incr", side_effect=ValueError("expired")):
            self.fetch()
        self.assertEqual(cache.get(zkillboard.FAILURES_KEY), 1)

    def test_paused_requests_do_not_take_the_slot(self):
        zkillboard._pause(60, NOW)
        zkillboard.fetch_kill(KILL_ID, now=NOW)
        self.assertIsNone(cache.get(zkillboard.SLOT_KEY))


# ---------------------------------------------------------------------------
# ESI killmails and market prices
# ---------------------------------------------------------------------------


class EsiVictimItem(pydantic.BaseModel):
    flag: int
    item_type_id: int
    quantity_destroyed: int | None = None
    quantity_dropped: int | None = None
    singleton: int
    items: list["EsiVictimItem"] | None = None


class EsiVictim(pydantic.BaseModel):
    character_id: int | None = None
    corporation_id: int | None = None
    alliance_id: int | None = None
    ship_type_id: int
    damage_taken: int
    items: list[EsiVictimItem] | None = None


class EsiAttacker(pydantic.BaseModel):
    character_id: int | None = None
    ship_type_id: int | None = None
    final_blow: bool


class EsiKillmail(pydantic.BaseModel):
    killmail_id: int
    killmail_time: datetime
    solar_system_id: int
    war_id: int | None = None
    attackers: list[EsiAttacker]
    victim: EsiVictim


class EsiPrice(pydantic.BaseModel):
    type_id: int
    average_price: float | None = None
    adjusted_price: float | None = None


class FakeOperation:
    def __init__(self, outcome, calls, name, kwargs):
        self.outcome = outcome
        self.calls = calls
        self.name = name
        self.kwargs = kwargs

    def result(self, **kwargs):
        self.calls.append((self.name, self.kwargs, kwargs))
        outcome = self.outcome(kwargs) if callable(self.outcome) else self.outcome
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class FakeEsiTestCase(SimpleTestCase):
    def setUp(self):
        super().setUp()
        self.calls = []
        self.killmail = EsiKillmail.model_validate(esi_killmail_payload())
        self.prices = [
            EsiPrice(type_id=RIFTER, average_price=450_000.5, adjusted_price=400_000.0),
            EsiPrice(type_id=2_873, average_price=None, adjusted_price=12_000.0),
        ]
        client = SimpleNamespace(
            Killmails=SimpleNamespace(
                GetKillmailsKillmailIdKillmailHash=lambda **kw: FakeOperation(
                    self.killmail, self.calls, "GetKillmailsKillmailIdKillmailHash", kw
                )
            ),
            Market=SimpleNamespace(
                GetMarketsPrices=lambda **kw: FakeOperation(self.prices, self.calls, "GetMarketsPrices", kw)
            ),
        )
        patcher = mock.patch.object(esi_provider, "esi", SimpleNamespace(client=client))
        patcher.start()
        self.addCleanup(patcher.stop)


class EsiKillmailTests(FakeEsiTestCase):
    def test_killmail_as_plain_dict(self):
        payload = esi_killmails.get_killmail(KILL_ID, HASH)
        self.assertIsInstance(payload, dict)
        self.assertIsInstance(payload["victim"], dict)
        self.assertIsInstance(payload["victim"]["items"][0], dict)
        self.assertEqual(payload["killmail_time"], KILL_TIME)
        self.assertEqual(
            self.calls,
            [
                (
                    "GetKillmailsKillmailIdKillmailHash",
                    {"killmail_id": KILL_ID, "killmail_hash": HASH},
                    {"use_etag": False},
                )
            ],
        )
        self.assertEqual(killmail_fields(payload)["ship_type_id"], RIFTER)

    def test_dict_answers_are_kept(self):
        self.killmail = esi_killmail_payload()
        self.assertEqual(esi_killmails.get_killmail(KILL_ID, HASH)["victim"]["ship_type_id"], RIFTER)

    def test_not_modified_reads_the_cached_answer(self):
        killmail = self.killmail
        self.killmail = lambda kwargs: (
            killmail if kwargs.get("force_refresh") else HTTPNotModified(status_code=304, headers={})
        )
        self.assertEqual(esi_killmails.get_killmail(KILL_ID, HASH)["killmail_id"], KILL_ID)

    def test_unknown_killmail_or_wrong_hash(self):
        for status in (404, 422):
            with self.subTest(status=status):
                self.killmail = HTTPClientError(status_code=status, headers={}, data=None)
                with self.assertRaises(FleetESIError) as ctx:
                    esi_killmails.get_killmail(KILL_ID, HASH)
                self.assertEqual(ctx.exception.code, "KILLMAIL_NOT_FOUND")
                self.assertEqual(str(ctx.exception), "Killmail not found. Check the link or the hash.")
                self.assertEqual(ctx.exception.status_code, status)

    def test_other_failures_have_short_texts(self):
        cases = [
            (
                ESIErrorLimitException(reset=30),
                "Could not read the killmail: ESI error limit reached; try again in a minute.",
            ),
            (
                HTTPClientError(status_code=420, headers={"X-Secret": "header"}, data=None),
                "Could not read the killmail: ESI error limit reached; try again in a minute.",
            ),
            (
                HTTPServerError(status_code=502, headers={"X-Secret": "header"}, data=None),
                "Could not read the killmail: ESI returned HTTP 502.",
            ),
            (
                ESIBucketLimitException(SimpleNamespace(window=60), reset=15),
                "Could not read the killmail: ESI rate limit reached; try again later.",
            ),
            (http_client_timeout(), "Could not read the killmail: ESI did not respond in time."),
            (requests.Timeout(), "Could not read the killmail: ESI did not respond in time."),
            (RuntimeError("X-Secret: header"), "Could not read the killmail: Unexpected ESI error (RuntimeError)."),
        ]
        for error, text in cases:
            with self.subTest(error=type(error).__name__):
                self.killmail = error
                with self.assertLogs("extensions.fleetops.providers.esi_killmails", "WARNING") as logs:
                    with self.assertRaises(FleetESIError) as ctx:
                        esi_killmails.get_killmail(KILL_ID, HASH)
                self.assertEqual(ctx.exception.code, "ESI_ERROR")
                self.assertEqual(str(ctx.exception), text)
                self.assertNotIn("X-Secret", logs.output[0])

    def test_older_response_models(self):
        class LegacyModel:
            def dict(self):
                return {"killmail_id": KILL_ID, "victim": {"ship_type_id": RIFTER}}

        self.killmail = LegacyModel()
        self.assertEqual(esi_killmails.get_killmail(KILL_ID, HASH)["victim"], {"ship_type_id": RIFTER})

    def test_empty_answer(self):
        self.killmail = None
        with self.assertRaises(FleetESIError):
            esi_killmails.get_killmail(KILL_ID, HASH)

    def test_market_prices(self):
        self.prices += [
            {"type_id": 34, "average_price": 5.12, "adjusted_price": 4.0},
            {"type_id": 35, "average_price": None, "adjusted_price": None},
            {"type_id": 36, "average_price": -1, "adjusted_price": 3},
            {"type_id": True, "average_price": 1.0},
            {"average_price": 1.0},
            "junk",
        ]
        self.assertEqual(
            esi_killmails.get_market_prices(),
            {
                RIFTER: Decimal("450000.5"),
                2_873: Decimal("12000.0"),
                34: Decimal("5.12"),
                36: Decimal("3"),
            },
        )
        self.assertEqual(self.calls, [("GetMarketsPrices", {}, {"use_etag": False})])

    def test_market_price_failure(self):
        self.prices = HTTPServerError(status_code=503, headers={}, data=None)
        with self.assertLogs("extensions.fleetops.providers.esi_killmails", "WARNING"), self.assertRaises(FleetESIError) as ctx:
            esi_killmails.get_market_prices()
        self.assertEqual(str(ctx.exception), "Could not read market prices: ESI returned HTTP 503.")

    def test_operations_are_named(self):
        self.assertEqual(esi_killmails.KILLMAIL_OPERATIONS, ("GetKillmailsKillmailIdKillmailHash", "GetMarketsPrices"))


# ---------------------------------------------------------------------------
# Loss values
# ---------------------------------------------------------------------------


def stored(fitted=None, total=None, estimate=None):
    return SimpleNamespace(zkill_fitted_value=fitted, zkill_total_value=total, esi_estimate=estimate)


class LossValueTests(SimpleTestCase):
    def test_requested_source_first(self):
        km = stored(Decimal("100"), Decimal("150"), Decimal("90"))
        self.assertEqual(values.loss_value(km, "zkill_fitted"), (Decimal("100"), "zkill_fitted"))
        self.assertEqual(values.loss_value(km, "zkill_total"), (Decimal("150"), "zkill_total"))
        self.assertEqual(values.loss_value(km, "esi_estimate"), (Decimal("90"), "esi_estimate"))

    def test_fallback_order(self):
        self.assertEqual(
            values.loss_value(stored(total=Decimal("150")), "zkill_fitted"), (Decimal("150"), "zkill_total")
        )
        self.assertEqual(
            values.loss_value(stored(fitted=Decimal("100")), "zkill_total"), (Decimal("100"), "zkill_fitted")
        )
        self.assertEqual(
            values.loss_value(stored(estimate=Decimal("90")), "zkill_fitted"), (Decimal("90"), "esi_estimate")
        )
        self.assertEqual(
            values.loss_value(stored(fitted=Decimal("100"), total=Decimal("150")), "esi_estimate"),
            (Decimal("100"), "zkill_fitted"),
        )

    def test_zero_is_a_value(self):
        km = stored(fitted=Decimal("0"), total=Decimal("5"))
        self.assertEqual(values.loss_value(km, "zkill_fitted"), (Decimal("0"), "zkill_fitted"))

    def test_no_value(self):
        self.assertEqual(values.loss_value(stored(), "zkill_fitted"), (None, ""))

    def test_unknown_source(self):
        with self.assertRaises(ValueError):
            values.loss_value(stored(), "jita_sell")


class ZkillScheduleTests(SimpleTestCase):
    old_kill = NOW - timedelta(days=1)

    def lookup(self, result, *, attempts=0, status="pending", killmail_time=None, now=NOW):
        return values.after_zkill_lookup(
            result,
            attempts=attempts,
            status=status,
            killmail_time=killmail_time or self.old_kill,
            now=now,
        )

    def test_first_check_waits_six_minutes_after_the_kill(self):
        self.assertEqual(values.first_zkill_check(NOW - timedelta(minutes=1), now=NOW), NOW + timedelta(minutes=5))
        self.assertEqual(values.first_zkill_check(NOW - timedelta(minutes=6), now=NOW), NOW)
        self.assertEqual(values.first_zkill_check(self.old_kill, now=NOW), NOW)

    def test_value_found(self):
        result = ZkillResult("ok", killmail_hash=HASH, fitted_value=Decimal("1"))
        self.assertEqual(self.lookup(result, attempts=2, status="missing"), values.ZkillSchedule("ok", 3, None))

    def test_retry_ladder_then_unavailable(self):
        missing = ZkillResult("missing")
        status, attempts, now = "pending", 0, NOW
        seen = []
        while True:
            schedule = self.lookup(missing, attempts=attempts, status=status, now=now)
            seen.append(schedule)
            if schedule.next_attempt_at is None:
                break
            status, attempts, now = schedule.status, schedule.attempts, schedule.next_attempt_at
        self.assertEqual(
            [(s.status, s.attempts) for s in seen],
            [("missing", 1), ("missing", 2), ("missing", 3), ("missing", 4), ("unavailable", 5)],
        )
        delays = [later.next_attempt_at - earlier.next_attempt_at for earlier, later in zip(seen, seen[1:4])]
        self.assertEqual(seen[0].next_attempt_at, NOW + timedelta(minutes=65))
        self.assertEqual(delays, [timedelta(hours=3), timedelta(hours=12), timedelta(hours=24)])

    def test_answer_without_value_counts_as_missing(self):
        schedule = self.lookup(ZkillResult("ok", killmail_hash=HASH))
        self.assertEqual(schedule, values.ZkillSchedule("missing", 1, NOW + timedelta(minutes=65)))

    def test_errors_count_but_keep_the_status(self):
        schedule = self.lookup(ZkillResult("unavailable"), attempts=1, status="pending")
        self.assertEqual(schedule, values.ZkillSchedule("pending", 2, NOW + timedelta(hours=3)))

    def test_longer_retry_after_wins(self):
        schedule = self.lookup(ZkillResult("blocked", retry_after=4 * 3600))
        self.assertEqual(schedule.next_attempt_at, NOW + timedelta(hours=4))

    def test_answers_without_request_are_not_counted(self):
        for result in (
            ZkillResult("throttled", retry_after=2, requested=False),
            ZkillResult("unavailable", retry_after=600, requested=False),
        ):
            with self.subTest(status=result.status):
                schedule = self.lookup(result, attempts=2, status="missing")
                expected = values.ZkillSchedule("missing", 2, NOW + timedelta(seconds=result.retry_after))
                self.assertEqual(schedule, expected)

    def test_never_before_the_minimum_age(self):
        kill = NOW - timedelta(minutes=2)
        with mock.patch("fleetops.providers.zkillboard.requests.get") as get:
            result = zkillboard.fetch_kill(KILL_ID, killmail_time=kill, now=NOW)
        get.assert_not_called()
        schedule = self.lookup(result, killmail_time=kill)
        self.assertEqual(schedule, values.ZkillSchedule("pending", 0, kill + timedelta(minutes=6)))


FITTED_ITEMS = [
    {"flag": 27, "item_type_id": 2_873, "quantity_destroyed": 1, "singleton": 0},  # high slot
    {"flag": 27, "item_type_id": 21_898, "quantity_dropped": 100, "singleton": 0},  # its ammunition
    {"flag": 19, "item_type_id": 5_973, "quantity_dropped": 1, "singleton": 0},  # mid slot
    {"flag": 11, "item_type_id": 1_183, "quantity_destroyed": 1, "singleton": 0},  # low slot
    {"flag": 92, "item_type_id": 31_788, "quantity_destroyed": 1, "singleton": 0},  # rig
    {"flag": 87, "item_type_id": 2_486, "quantity_destroyed": 2, "quantity_dropped": 3, "singleton": 0},  # drones
    {"flag": 89, "item_type_id": 13_219, "quantity_destroyed": 1, "singleton": 0},  # implant
    {"flag": 125, "item_type_id": 45_586, "quantity_destroyed": 1, "singleton": 0},  # subsystem
    {"flag": 159, "item_type_id": 40_556, "quantity_destroyed": 9, "singleton": 0},  # fighter tube
]
PRICES = {
    RIFTER: Decimal("400000"),
    2_873: Decimal("12000"),
    21_898: Decimal("10.5"),
    5_973: Decimal("8000"),
    1_183: Decimal("15000"),
    31_788: Decimal("100000"),
    2_486: Decimal("2000"),
    13_219: Decimal("50000"),
    45_586: Decimal("1000"),
    40_556: Decimal("1"),
    34: Decimal("5"),
    3_467: Decimal("90000"),
    44_992: Decimal("2000000"),
}
FITTED_TOTAL = (
    Decimal("400000") + 12000 + Decimal("10.5") * 100 + 8000 + 15000 + 100000 + 2000 * 5 + 50000 + 1000 + 9
)


class EstimateTests(SimpleTestCase):
    def test_ship_and_fitted_items(self):
        self.assertEqual(values.estimate_value(RIFTER, FITTED_ITEMS, PRICES), FITTED_TOTAL)

    def test_every_fitting_location_counts(self):
        expected = set(range(11, 35)) | {87, 89} | set(range(92, 100)) | set(range(125, 133)) | set(range(158, 164))
        self.assertEqual(values.FITTED_FLAGS, expected)
        self.assertNotIn(5, values.FITTED_FLAGS)  # cargo
        self.assertNotIn(90, values.FITTED_FLAGS)  # ship hangar

    def test_cargo_and_its_contents_are_left_out(self):
        cargo = [
            {"flag": 5, "item_type_id": 44_992, "quantity_dropped": 1, "singleton": 0},  # PLEX in cargo
            {
                "flag": 5,
                "item_type_id": 3_467,
                "quantity_dropped": 1,
                "singleton": 1,
                "items": [{"flag": 27, "item_type_id": 2_873, "quantity_dropped": 10, "singleton": 0}],
            },
        ]
        self.assertEqual(values.estimate_value(RIFTER, cargo, PRICES), Decimal("400000.00"))

    def test_contents_of_fitted_items_count(self):
        items = [
            {
                "flag": 27,
                "item_type_id": 3_467,
                "quantity_destroyed": 1,
                "singleton": 1,
                "items": [{"flag": 0, "item_type_id": 34, "quantity_destroyed": 10, "singleton": 0}],
            }
        ]
        self.assertEqual(values.estimate_value(RIFTER, items, PRICES), Decimal("490050.00"))

    def test_blueprint_copies_are_worth_nothing(self):
        items = [{"flag": 27, "item_type_id": 2_873, "quantity_destroyed": 1, "singleton": 2}]
        self.assertEqual(values.estimate_value(RIFTER, items, PRICES), Decimal("400000.00"))

    def test_quantities(self):
        drones = {"flag": 87, "item_type_id": 2_486, "singleton": 0}
        for quantities, expected in (
            ({}, 2000),
            ({"quantity_destroyed": 0}, 0),
            ({"quantity_destroyed": 4, "quantity_dropped": None}, 8000),
            ({"quantity_destroyed": -4, "quantity_dropped": 1}, 2000),
        ):
            with self.subTest(quantities=quantities):
                item = dict(drones, **quantities)
                self.assertEqual(values.estimate_value(RIFTER, [item], PRICES), Decimal(400000 + expected))

    def test_unpriced_items_count_as_zero(self):
        items = [{"flag": 27, "item_type_id": 999_999, "quantity_destroyed": 1, "singleton": 0}]
        self.assertEqual(values.estimate_value(RIFTER, items, PRICES), Decimal("400000.00"))

    def test_unpriced_ship_has_no_estimate(self):
        self.assertIsNone(values.estimate_value(CAPSULE, FITTED_ITEMS, PRICES))
        self.assertIsNone(values.estimate_value(RIFTER, FITTED_ITEMS, {}))

    def test_malformed_items_are_skipped(self):
        items = [
            "junk",
            None,
            {"flag": [27], "item_type_id": 2_873},
            {"flag": True, "item_type_id": 2_873},
            {"flag": 27, "item_type_id": [2_873]},
            {"flag": 27, "item_type_id": 2_873, "quantity_destroyed": "1", "items": "nested"},
        ]
        self.assertEqual(values.estimate_value(RIFTER, items, PRICES), Decimal("400000.00"))
        self.assertEqual(values.estimate_value(RIFTER, None, PRICES), Decimal("400000.00"))

    def test_deep_nesting_is_walked_without_recursion(self):
        item = {"flag": 0, "item_type_id": 34, "quantity_destroyed": 1, "singleton": 0}
        for _ in range(5000):
            item = {"flag": 0, "item_type_id": 34, "quantity_destroyed": 1, "singleton": 0, "items": [item]}
        item["flag"] = 27
        self.assertEqual(values.estimate_value(RIFTER, [item], PRICES), Decimal("400000") + 5 * 5001)

    def test_result_is_rounded_to_cents(self):
        prices = {**PRICES, RIFTER: Decimal("0.005")}
        self.assertEqual(values.estimate_value(RIFTER, [], prices), Decimal("0.01"))
        self.assertEqual(values.estimate_value(RIFTER, [], prices).as_tuple().exponent, -2)

    def test_market_estimate_uses_esi_prices(self):
        with mock.patch.object(values, "get_market_prices", return_value=PRICES) as prices:
            self.assertEqual(values.market_estimate(RIFTER, FITTED_ITEMS), FITTED_TOTAL)
        prices.assert_called_once_with()

    def test_market_estimate_with_given_prices(self):
        with mock.patch.object(values, "get_market_prices") as prices:
            self.assertEqual(values.market_estimate(RIFTER, [], prices={RIFTER: Decimal("5")}), Decimal("5.00"))
        prices.assert_not_called()

    def test_market_estimate_without_prices(self):
        error = FleetESIError("ESI_ERROR", "Could not read market prices: ESI returned HTTP 503.")
        with mock.patch.object(values, "get_market_prices", side_effect=error):
            self.assertIsNone(values.market_estimate(RIFTER, FITTED_ITEMS))


# ---------------------------------------------------------------------------
# The whole intake path
# ---------------------------------------------------------------------------


class IntakeFlowTests(ZkillboardTestCase, FakeEsiTestCase):
    def test_zkillboard_link_to_estimate(self):
        ref = parse_killmail_reference(f"https://zkillboard.com/kill/{KILL_ID}/")
        self.assertIsNone(ref.killmail_hash)
        answer = self.fetch(ref.killmail_id)
        payload = esi_killmails.get_killmail(ref.killmail_id, answer.killmail_hash)
        fields = killmail_fields(payload)
        self.assertEqual(fields["killmail_id"], ref.killmail_id)
        km = stored(answer.fitted_value, answer.total_value)
        self.assertEqual(values.loss_value(km, "zkill_fitted"), (Decimal("10234567.89"), "zkill_fitted"))
        estimate = values.market_estimate(fields["ship_type_id"], fields["items"])
        self.assertEqual(estimate, Decimal("462000.50"))

    def test_in_game_link_needs_no_zkillboard(self):
        ref = parse_killmail_reference(f"<url=killReport:{KILL_ID}:{HASH}>Kill</url>")
        fields = killmail_fields(esi_killmails.get_killmail(ref.killmail_id, ref.killmail_hash))
        self.assertEqual(fields["victim_character_id"], 95_000_100)
        self.get.assert_not_called()
