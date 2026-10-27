# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""The clickable FC character and form-up system of the MOTD, and the ESI reads behind them."""

import logging
from types import SimpleNamespace
from unittest import mock

from django.test import SimpleTestCase

from fleetops.providers import esi as esi_provider
from fleetops.services import eve_links, sde
from fleetops.services.eve_links import (
    BLOODLINE_CHARACTER_TYPES,
    DEFAULT_CHARACTER_TYPE,
    character_link,
    character_type_id,
    remember_character_type,
    remember_solar_system,
    solar_system,
    system_link,
)

from .link_fakes import FAKE, start_fakes, stop_fakes

# The real functions, taken before the fakes replace them.
REAL_CHARACTER_BLOODLINE = esi_provider.character_bloodline
REAL_UNIVERSE_SYSTEM_IDS = esi_provider.universe_system_ids
REAL_ESI = esi_provider.esi

EXAMPLE_FC = 90_000_001


class LinkTestCase(SimpleTestCase):
    """Fresh fake answers and a fresh private cache for every test."""

    def setUp(self):
        super().setUp()
        start_fakes()
        self.addCleanup(stop_fakes)


class CharacterTypeTests(LinkTestCase):
    def test_the_table_maps_bloodlines_to_character_types(self):
        for bloodline, expected in ((8, 1378), (4, 1380), (5, 1373), (14, 1386)):
            with self.subTest(bloodline=bloodline):
                FAKE.bloodlines[EXAMPLE_FC + bloodline] = bloodline

                self.assertEqual(character_type_id(EXAMPLE_FC + bloodline), expected)
        self.assertEqual(sorted(BLOODLINE_CHARACTER_TYPES.values()), list(range(1373, 1387)))

    def test_a_bloodline_outside_the_table_is_the_default_type(self):
        FAKE.bloodlines[EXAMPLE_FC] = 25

        self.assertEqual(character_type_id(EXAMPLE_FC), DEFAULT_CHARACTER_TYPE)

    def test_an_unknown_character_id_is_the_default_type(self):
        FAKE.calls.clear()

        for value in (None, "", "abc"):
            with self.subTest(value=value):
                self.assertEqual(character_type_id(value), DEFAULT_CHARACTER_TYPE)
        self.assertEqual(FAKE.calls, [])

    def test_a_known_character_is_asked_once(self):
        FAKE.bloodlines[EXAMPLE_FC] = 4

        self.assertEqual(character_type_id(EXAMPLE_FC), 1380)
        self.assertEqual(character_type_id(EXAMPLE_FC), 1380)
        self.assertEqual(FAKE.calls, [("character", EXAMPLE_FC)])

    def test_a_failed_lookup_gives_the_default_type_and_is_not_repeated_at_once(self):
        FAKE.fail = True

        self.assertEqual(character_type_id(EXAMPLE_FC), DEFAULT_CHARACTER_TYPE)
        FAKE.fail = False
        FAKE.bloodlines[EXAMPLE_FC] = 4
        # The failure is remembered for a few minutes: ESI is not asked again on the next preview.
        self.assertEqual(character_type_id(EXAMPLE_FC), DEFAULT_CHARACTER_TYPE)
        self.assertEqual(len(FAKE.calls), 1)

    def test_a_failed_lookup_is_remembered_for_ten_minutes_and_a_good_one_for_thirty_days(self):
        with mock.patch.object(eve_links.cache, "set") as store:
            FAKE.fail = True
            character_type_id(EXAMPLE_FC)
            FAKE.fail = False
            character_type_id(EXAMPLE_FC + 1)

        self.assertEqual(
            [call.args[1:] for call in store.call_args_list],
            [(0, 600), (BLOODLINE_CHARACTER_TYPES[8], 30 * 24 * 3600)],
        )

    def test_nothing_raises(self):
        with mock.patch.object(esi_provider, "character_bloodline", side_effect=RuntimeError("boom")):
            with self.assertLogs(eve_links.logger, "WARNING") as logs:
                self.assertEqual(character_type_id(EXAMPLE_FC), DEFAULT_CHARACTER_TYPE)

        self.assertNotIn("boom", " ".join(logs.output))

    def test_remembered_types_are_used_without_a_lookup(self):
        remember_character_type(EXAMPLE_FC, 1380)

        self.assertEqual(character_type_id(EXAMPLE_FC), 1380)
        self.assertEqual(FAKE.calls, [])


class CharacterLinkTests(LinkTestCase):
    def test_the_link_opens_the_character(self):
        FAKE.bloodlines[EXAMPLE_FC] = 8

        self.assertEqual(
            character_link(EXAMPLE_FC, "Example FC"), f'<a href="showinfo:1378//{EXAMPLE_FC}">Example FC</a>'
        )

    def test_without_an_id_only_the_name_is_printed(self):
        for value in (None, "", 0):
            with self.subTest(value=value):
                self.assertEqual(character_link(value, "Example <FC> & Co"), "Example &lt;FC&gt; &amp; Co")
        self.assertEqual(FAKE.calls, [])

    def test_an_apostrophe_stays_in_the_link_text(self):
        self.assertTrue(character_link(EXAMPLE_FC, "O'Brien").endswith(">O'Brien</a>"))


class SolarSystemTests(LinkTestCase):
    def test_a_system_name_gets_the_location_link(self):
        self.assertEqual(system_link("Jita"), '<loc><a href="showinfo:5//30000142">Jita</a></loc>')

    def test_the_link_text_is_the_name_the_game_spells(self):
        self.assertEqual(solar_system("  jita  "), (30000142, "Jita"))
        self.assertEqual(system_link("jita"), '<loc><a href="showinfo:5//30000142">Jita</a></loc>')

    def test_text_that_is_no_system_stays_as_typed_and_is_never_looked_up(self):
        for text in ("Jita (staging)", "Jita IV - Moon 4 - Caldari Navy Assembly Plant", "x" * 41, "", "   ", "a/b"):
            with self.subTest(text=text):
                self.assertIsNone(solar_system(text))
        self.assertEqual(system_link("Jita (staging) & Co"), "Jita (staging) &amp; Co")
        self.assertEqual(FAKE.calls, [])

    def test_a_name_that_looks_like_a_system_but_is_not_known_stays_as_typed(self):
        self.assertEqual(system_link("Staging"), "Staging")
        self.assertEqual(FAKE.calls, [("systems", ("Staging",))])

    def test_an_answer_is_remembered_found_or_not(self):
        solar_system("Jita")
        solar_system("jita")
        solar_system("Staging")
        solar_system("staging")

        self.assertEqual(FAKE.calls, [("systems", ("Jita",)), ("systems", ("Staging",))])

    def test_the_cache_holds_a_found_system_for_a_month_a_missing_one_for_a_day_and_an_error_for_a_minute(self):
        with mock.patch.object(eve_links.cache, "set") as store:
            solar_system("Jita")
            solar_system("Staging")
            FAKE.fail = True
            solar_system("Nowhere")

        self.assertEqual(
            [(call.args[1], call.args[2]) for call in store.call_args_list],
            [((30000142, "Jita"), 30 * 24 * 3600), ((), 24 * 3600), ((), 60)],
        )

    def test_an_error_gives_the_text_as_typed(self):
        FAKE.fail = True

        with self.assertLogs(eve_links.logger, "WARNING"):
            self.assertEqual(system_link("Jita"), "Jita")

    def test_the_sde_answers_when_it_is_installed(self):
        with (
            mock.patch.object(sde, "sde_installed", return_value=True),
            mock.patch.object(sde, "find_solar_system", return_value=(30000999, "R-Example")) as find,
        ):
            self.assertEqual(system_link("r-example"), '<loc><a href="showinfo:5//30000999">R-Example</a></loc>')

        find.assert_called_once_with("r-example")
        # ESI is not asked when the SDE is there.
        self.assertEqual(FAKE.calls, [])

    def test_a_system_the_sde_does_not_know_is_not_asked_of_esi(self):
        with (
            mock.patch.object(sde, "sde_installed", return_value=True),
            mock.patch.object(sde, "find_solar_system", return_value=None),
        ):
            self.assertEqual(system_link("Jita"), "Jita")

        self.assertEqual(FAKE.calls, [])

    def test_remembered_systems_are_used_without_a_lookup(self):
        remember_solar_system("Example Gate", 30000999, "Example Gate")

        self.assertEqual(system_link("example  gate"), '<loc><a href="showinfo:5//30000999">Example Gate</a></loc>')
        self.assertEqual(FAKE.calls, [])


class FakeCharacterOperation:
    def __init__(self, api, character_id):
        self.api = api
        self.character_id = character_id

    def result(self, **kwargs):
        self.api.calls.append((self.character_id, kwargs))
        if self.api.failure is not None:
            raise self.api.failure
        return self.api.wrap({"bloodline_id": 8, "name": "Example FC"})


class FakeCharacterAPI:
    def __init__(self):
        self.calls = []
        self.failure = None
        self.wrap = lambda answer: answer

    def GetCharactersCharacterId(self, *, character_id):
        return FakeCharacterOperation(self, character_id)


class FakeUniverseOperation:
    def __init__(self, api, body):
        self.api = api
        self.body = body

    def result(self, **kwargs):
        self.api.calls.append((self.body, kwargs))
        if self.api.failure is not None:
            raise self.api.failure
        return {"systems": [{"id": 30000142, "name": "Jita"}], "stations": [{"id": 60003760, "name": "Jita IV"}]}


class FakeUniverseAPI:
    def __init__(self):
        self.calls = []
        self.failure = None

    def PostUniverseIds(self, *, body):
        return FakeUniverseOperation(self, body)


class ProviderTests(SimpleTestCase):
    def setUp(self):
        super().setUp()
        self.characters = FakeCharacterAPI()
        self.universe = FakeUniverseAPI()
        client = SimpleNamespace(Character=self.characters, Universe=self.universe)
        patcher = mock.patch.object(esi_provider, "esi", SimpleNamespace(client=client))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_operation_is_loaded_with_the_others(self):
        # One client with one operation list, so the ESI spec is only loaded once.
        self.assertIn("GetCharactersCharacterId", REAL_ESI._operations)

    def test_the_bloodline_of_a_character(self):
        self.assertEqual(REAL_CHARACTER_BLOODLINE(EXAMPLE_FC), 8)
        [(character_id, kwargs)] = self.characters.calls
        self.assertEqual(character_id, EXAMPLE_FC)
        # A public read: the response cache is allowed.
        self.assertNotIn("use_cache", kwargs)

    def test_a_model_answer_is_read_too(self):
        self.characters.wrap = lambda answer: SimpleNamespace(bloodline_id=answer["bloodline_id"])

        self.assertEqual(REAL_CHARACTER_BLOODLINE(EXAMPLE_FC), 8)

    def test_a_failed_read_gives_none_and_logs_without_detail(self):
        self.characters.failure = RuntimeError("secret detail")

        with self.assertLogs(esi_provider.logger, logging.INFO) as logs:
            self.assertIsNone(REAL_CHARACTER_BLOODLINE(EXAMPLE_FC))

        self.assertNotIn("secret detail", " ".join(logs.output))

    def test_an_answer_without_a_bloodline_gives_none(self):
        self.characters.wrap = lambda answer: {}

        self.assertIsNone(REAL_CHARACTER_BLOODLINE(EXAMPLE_FC))

    def test_system_names_resolve_to_systems_only(self):
        self.assertEqual(REAL_UNIVERSE_SYSTEM_IDS(["jita", "Jita IV"]), {"jita": (30000142, "Jita")})
        [(body, kwargs)] = self.universe.calls
        self.assertEqual(body, ["jita", "Jita IV"])
        # A POST never uses the response cache.
        self.assertEqual(kwargs, {"use_etag": False, "use_cache": False, "store_cache": False})

    def test_a_failed_system_lookup_is_a_short_error(self):
        self.universe.failure = RuntimeError("boom")

        with self.assertLogs(esi_provider.logger, "WARNING"), self.assertRaises(esi_provider.FleetESIError) as ctx:
            REAL_UNIVERSE_SYSTEM_IDS(["Jita"])

        self.assertEqual(ctx.exception.code, "SYSTEM_IDS_ERROR")
        self.assertTrue(str(ctx.exception).startswith("Could not look up solar system names: "))
