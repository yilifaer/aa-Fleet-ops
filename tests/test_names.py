# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Names, corporations and alliances of fleet members without an Alliance Auth account.

ESI's POST /universe/names/ and /characters/affiliation/ are replaced by an in-memory fake that
fails a whole request with a 404 when one ID is invalid, as ESI does.
"""

from datetime import timedelta
from types import SimpleNamespace
from unittest import mock

from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from kombu.exceptions import OperationalError as QueueError

from fleetops.models import (
    CharacterAffiliation,
    EveEntityName,
    FleetMemberEvent,
    FleetMemberState,
    FleetOperation,
    OperationRoleAssignment,
)
from fleetops.providers import esi as esi_provider
from fleetops.providers.esi import FleetESIError, LookupBudget, character_affiliations, resolve_names
from fleetops.services import names
from fleetops.services.identity import character_label, identity_for_character_id, resolve_many
from fleetops.services.names import (
    apply_names_to_operation,
    cached_identities,
    entity_names,
    queue_name_resolution,
    resolve_and_store,
    stale_character_ids,
)
from fleetops.services.operations import end_fleet
from fleetops.services.tracking import observe_unreadable_fleet
from fleetops.tasks import resolve_character_names, track_operation

from . import factories as f
from .test_operations_tracking import FLEET_ID, FakeESIMixin, http_error

EndReason = FleetOperation.EndReason

PILOT = 2_119_100_001
PILOT_TWO = 2_119_100_002
CORP = 98_700_101
ALLIANCE = 99_000_101
NPC_CORP = 1_000_180
BOSS = 2_119_100_009


# ---------------------------------------------------------------------------
# Fake ESI lookups
# ---------------------------------------------------------------------------


class FakeLookup:
    def __init__(self, api, kind, body):
        self.api = api
        self.kind = kind
        self.body = list(body)

    def result(self, **kwargs):
        self.api.calls.append((self.kind, self.body, kwargs))
        if self.api.failure is not None:
            raise self.api.failure
        known = self.api.names if self.kind == "names" else self.api.affiliations
        if any(entity_id not in known for entity_id in self.body):
            raise http_error(404)
        if self.kind == "names":
            return [{"category": known[i][0], "id": i, "name": known[i][1]} for i in self.body]
        return [{"character_id": i, **known[i]} for i in self.body]


class FakeLookupAPI:
    """``esi.client.Universe`` and ``esi.client.Character`` for the two bulk lookups."""

    def __init__(self):
        self.names = {}
        self.affiliations = {}
        self.calls = []
        self.failure = None

    def PostUniverseNames(self, *, body):
        return FakeLookup(self, "names", body)

    def PostCharactersAffiliation(self, *, body):
        return FakeLookup(self, "affiliation", body)

    def bodies(self, kind):
        return [body for call_kind, body, _ in self.calls if call_kind == kind]

    def add_pilot(self, character_id, name, corporation=(CORP, "Outside Corp"), alliance=(ALLIANCE, "Outside Alliance")):
        self.names[character_id] = ("character", name)
        self.names[corporation[0]] = ("corporation", corporation[1])
        affiliation = {"corporation_id": corporation[0]}
        if alliance:
            self.names[alliance[0]] = ("alliance", alliance[1])
            affiliation["alliance_id"] = alliance[0]
        self.affiliations[character_id] = affiliation


class LookupMixin:
    def setUp(self):
        super().setUp()
        cache.clear()
        self.lookups = FakeLookupAPI()
        # Keeps the fake fleet API of FakeESIMixin when a test uses both.
        fleets = getattr(self, "esi", None)
        client = SimpleNamespace(client=SimpleNamespace(Fleets=fleets, Universe=self.lookups, Character=self.lookups))
        patcher = mock.patch.object(esi_provider, "esi", client)
        patcher.start()
        self.addCleanup(patcher.stop)


def cache_name(entity_id, name, category="character", age=timedelta(0)):
    return EveEntityName.objects.update_or_create(
        id=entity_id, defaults={"category": category, "name": name, "updated_at": timezone.now() - age}
    )[0]


def cache_affiliation(character_id, corporation_id=CORP, alliance_id=ALLIANCE, age=timedelta(0)):
    return CharacterAffiliation.objects.update_or_create(
        character_id=character_id,
        defaults={"corporation_id": corporation_id, "alliance_id": alliance_id, "updated_at": timezone.now() - age},
    )[0]


# ---------------------------------------------------------------------------
# Provider: bulk lookups
# ---------------------------------------------------------------------------


class BulkLookupTests(LookupMixin, TestCase):
    def test_every_post_skips_the_django_esi_cache(self):
        self.lookups.add_pilot(PILOT, "Outside Pilot")

        resolve_names([PILOT])
        character_affiliations([PILOT])

        self.assertEqual(
            [kwargs for _, _, kwargs in self.lookups.calls],
            [{"use_etag": False, "use_cache": False, "store_cache": False}] * 2,
        )

    def test_ids_are_deduplicated_and_sent_in_chunks_of_1000(self):
        ids = list(range(1, 2501))
        self.lookups.names = {entity_id: ("character", f"Pilot {entity_id}") for entity_id in ids}

        result = resolve_names(ids + ids[:10] + [None, 0])

        self.assertEqual(len(result), 2500)
        self.assertEqual([len(body) for body in self.lookups.bodies("names")], [1000, 1000, 500])
        self.assertEqual(sum(self.lookups.bodies("names"), []), ids)

    def test_invalid_ids_are_isolated_and_marked_unresolved(self):
        self.lookups.names = {entity_id: ("character", f"Pilot {entity_id}") for entity_id in (11, 12, 14)}

        result = resolve_names([11, 12, 13, 14])

        self.assertEqual(result[13], ("unresolved", ""))
        self.assertEqual(result[14], ("character", "Pilot 14"))
        self.assertEqual(self.lookups.bodies("names"), [[11, 12, 13, 14], [11, 12], [13, 14], [13], [14]])

    def test_a_run_makes_at_most_25_calls(self):
        # Every ID is invalid, so every chunk splits until the budget runs out.
        result = resolve_names(range(1, 101))

        self.assertEqual(len(self.lookups.calls), 25)
        self.assertTrue(all(value == ("unresolved", "") for value in result.values()))
        self.assertLess(len(result), 100)

    def test_the_budget_is_shared_by_the_calls_of_one_run(self):
        self.lookups.add_pilot(PILOT, "Outside Pilot")
        budget = LookupBudget(1)

        self.assertEqual(set(character_affiliations([PILOT], budget=budget)), {PILOT})
        self.assertEqual(resolve_names([PILOT], budget=budget), {})
        self.assertEqual(len(self.lookups.calls), 1)

    def test_unknown_characters_have_no_affiliation(self):
        self.lookups.add_pilot(PILOT, "Outside Pilot", alliance=None)

        result = character_affiliations([PILOT, 77])

        self.assertEqual(result, {PILOT: {"corporation_id": CORP, "alliance_id": None, "faction_id": None}, 77: None})

    def test_other_errors_raise_a_short_fleet_esi_error(self):
        self.lookups.failure = http_error(503)

        with self.assertLogs(esi_provider.logger, "WARNING"), self.assertRaises(FleetESIError) as ctx:
            resolve_names([PILOT])

        self.assertEqual(ctx.exception.code, "NAMES_ERROR")
        self.assertEqual(str(ctx.exception), "Could not look up names: ESI returned HTTP 503 (Service unavailable).")
        self.assertEqual(len(self.lookups.calls), 1)


# ---------------------------------------------------------------------------
# Name cache
# ---------------------------------------------------------------------------


class EntityNamesTests(LookupMixin, TestCase):
    def test_cached_names_are_used_without_esi(self):
        cache_name(CORP, "Cached Corp", "corporation", age=timedelta(days=6))

        self.assertEqual(entity_names([CORP]), {CORP: "Cached Corp"})
        self.assertEqual(self.lookups.calls, [])

    def test_missing_and_old_names_are_fetched_and_stored(self):
        cache_name(CORP, "Old Corp Name", "corporation", age=timedelta(days=8))
        self.lookups.names = {CORP: ("corporation", "New Corp Name"), ALLIANCE: ("alliance", "Some Alliance")}

        result = entity_names([CORP, ALLIANCE])

        self.assertEqual(result, {CORP: "New Corp Name", ALLIANCE: "Some Alliance"})
        self.assertEqual(self.lookups.bodies("names"), [[CORP, ALLIANCE]])
        self.assertEqual(EveEntityName.objects.get(id=ALLIANCE).category, "alliance")
        self.assertEqual(EveEntityName.objects.get(id=CORP).name, "New Corp Name")

    def test_ids_nobody_knows_are_left_out_and_asked_again_after_a_day(self):
        self.assertEqual(entity_names([5]), {})
        self.assertEqual(EveEntityName.objects.get(id=5).category, EveEntityName.UNRESOLVED)

        entity_names([5])
        self.assertEqual(len(self.lookups.calls), 1)

        EveEntityName.objects.filter(id=5).update(updated_at=timezone.now() - timedelta(days=1, minutes=1))
        entity_names([5])
        self.assertEqual(len(self.lookups.calls), 2)

    def test_old_names_are_returned_when_esi_fails(self):
        cache_name(CORP, "Old Corp Name", "corporation", age=timedelta(days=30))
        self.lookups.failure = http_error(502)

        with self.assertLogs(esi_provider.logger, "WARNING"):
            self.assertEqual(entity_names([CORP, ALLIANCE]), {CORP: "Old Corp Name"})

    def test_a_name_esi_no_longer_knows_is_kept(self):
        cache_name(PILOT, "Known Pilot", age=timedelta(days=8))

        self.assertEqual(entity_names([PILOT]), {PILOT: "Known Pilot"})

        row = EveEntityName.objects.get(id=PILOT)
        self.assertEqual(row.category, EveEntityName.UNRESOLVED)
        self.assertEqual(row.name, "Known Pilot")


class StaleCharacterTests(TestCase):
    def test_time_to_live_of_names_affiliations_and_unknown_ids(self):
        fresh, old_name, old_affiliation, no_affiliation, unknown, unknown_old, missing = range(1, 8)
        for character_id in (fresh, old_affiliation, no_affiliation):
            cache_name(character_id, f"Pilot {character_id}", age=timedelta(days=6))
        cache_name(old_name, "Old Name", age=timedelta(days=7, minutes=1))
        for character_id in (fresh, old_name):
            cache_affiliation(character_id, age=timedelta(minutes=59))
        cache_affiliation(old_affiliation, age=timedelta(minutes=61))
        cache_name(unknown, "", EveEntityName.UNRESOLVED, age=timedelta(hours=23))
        cache_name(unknown_old, "", EveEntityName.UNRESOLVED, age=timedelta(hours=25))

        stale = stale_character_ids(range(1, 8))

        self.assertEqual(stale, [old_name, old_affiliation, no_affiliation, unknown_old, missing])


class CachedIdentityTests(TestCase):
    def test_name_corporation_and_alliance_come_from_the_caches(self):
        cache_name(PILOT, "Outside Pilot")
        cache_name(CORP, "Outside Corp", "corporation")
        cache_name(ALLIANCE, "Outside Alliance", "alliance")
        cache_affiliation(PILOT)
        cache_affiliation(PILOT_TWO, corporation_id=NPC_CORP, alliance_id=None)

        identities = cached_identities([PILOT, PILOT_TWO, 3])

        self.assertEqual(set(identities), {PILOT, PILOT_TWO})
        pilot = identities[PILOT]
        self.assertEqual(
            (pilot.character_name, pilot.corporation_id, pilot.corporation_name, pilot.alliance_id, pilot.alliance_name),
            ("Outside Pilot", CORP, "Outside Corp", ALLIANCE, "Outside Alliance"),
        )
        second = identities[PILOT_TWO]
        self.assertEqual(
            (second.character_name, second.corporation_id, second.corporation_name, second.alliance_name),
            (f"Character {PILOT_TWO}", NPC_CORP, "", ""),
        )

    def test_identity_of_a_character_alliance_auth_does_not_know(self):
        cache_name(PILOT, "Outside Pilot")
        cache_name(CORP, "Outside Corp", "corporation")
        cache_affiliation(PILOT, alliance_id=None)

        identity = identity_for_character_id(PILOT)
        unknown = identity_for_character_id(PILOT_TWO)

        self.assertTrue(identity.external)
        self.assertIsNone(identity.user)
        self.assertEqual(identity.character_name, "Outside Pilot")
        self.assertEqual((identity.corporation_id, identity.corporation_name), (CORP, "Outside Corp"))
        self.assertIsNone(identity.alliance_id)
        self.assertTrue(unknown.external)
        self.assertEqual(unknown.character_name, f"Character {PILOT_TWO}")

    def test_alliance_auth_data_wins_over_the_cache(self):
        member = f.create_user(main_name="Auth Member")
        main = f.main_of(member)
        stranger = f.create_character("Unowned Stranger")
        for character in (main, stranger):
            cache_name(character.character_id, "Cached Name")
            cache_affiliation(character.character_id, corporation_id=NPC_CORP)

        identities = resolve_many([main.character_id, stranger.character_id])

        self.assertEqual(identities[main.character_id].character_name, "Auth Member")
        self.assertEqual(identities[main.character_id].corporation_id, f.DEFAULT_CORP[0])
        self.assertFalse(identities[main.character_id].external)
        self.assertEqual(identities[stranger.character_id].character_name, "Unowned Stranger")
        self.assertFalse(identities[stranger.character_id].external)

    def test_character_label_uses_the_name_cache(self):
        cache_name(PILOT, "Outside Pilot")
        cache_name(PILOT_TWO, "", EveEntityName.UNRESOLVED)

        self.assertEqual(character_label(PILOT), "Outside Pilot")
        self.assertEqual(character_label(PILOT_TWO), f"character {PILOT_TWO}")
        self.assertEqual(character_label(f.main_of(f.create_user(main_name="Auth Pilot")).character_id), "Auth Pilot")


# ---------------------------------------------------------------------------
# Lookup and storage
# ---------------------------------------------------------------------------


class ResolveAndStoreTests(LookupMixin, TestCase):
    def test_one_affiliation_call_then_one_name_call(self):
        self.lookups.add_pilot(PILOT, "Outside Pilot")
        self.lookups.add_pilot(PILOT_TWO, "Second Pilot", corporation=(NPC_CORP, "NPC Corp"), alliance=None)

        stored = resolve_and_store([PILOT, PILOT_TWO, PILOT])

        self.assertEqual(self.lookups.bodies("affiliation"), [[PILOT, PILOT_TWO]])
        self.assertEqual(self.lookups.bodies("names"), [[PILOT, PILOT_TWO, NPC_CORP, CORP, ALLIANCE]])
        self.assertEqual(stored, 7)
        identities = cached_identities([PILOT, PILOT_TWO])
        self.assertEqual(identities[PILOT].alliance_name, "Outside Alliance")
        self.assertEqual(identities[PILOT_TWO].corporation_name, "NPC Corp")

    def test_fresh_data_is_not_fetched_again(self):
        self.lookups.add_pilot(PILOT, "Outside Pilot")
        resolve_and_store([PILOT])

        self.assertEqual(resolve_and_store([PILOT]), 0)
        self.assertEqual(len(self.lookups.calls), 2)

        # An hour later only the affiliation is due; the names are still fresh.
        CharacterAffiliation.objects.update(updated_at=timezone.now() - timedelta(hours=2))
        resolve_and_store([PILOT])
        self.assertEqual(self.lookups.bodies("affiliation"), [[PILOT], [PILOT]])
        self.assertEqual(len(self.lookups.bodies("names")), 1)

    def test_a_character_esi_does_not_know_is_not_asked_for_twice(self):
        self.lookups.add_pilot(PILOT, "Outside Pilot")

        resolve_and_store([PILOT, 77])

        self.assertEqual(self.lookups.bodies("names"), [[PILOT, CORP, ALLIANCE]])
        self.assertEqual(EveEntityName.objects.get(id=77).category, EveEntityName.UNRESOLVED)
        self.assertFalse(CharacterAffiliation.objects.filter(character_id=77).exists())
        self.assertNotIn(77, stale_character_ids([77]))

    def test_changed_names_and_affiliations_replace_the_old_rows(self):
        cache_name(PILOT, "Old Name", age=timedelta(days=8))
        cache_affiliation(PILOT, corporation_id=NPC_CORP, alliance_id=None, age=timedelta(hours=2))
        self.lookups.add_pilot(PILOT, "New Name")

        resolve_and_store([PILOT])

        self.assertEqual(EveEntityName.objects.get(id=PILOT).name, "New Name")
        affiliation = CharacterAffiliation.objects.get(character_id=PILOT)
        self.assertEqual((affiliation.corporation_id, affiliation.alliance_id), (CORP, ALLIANCE))

    def test_affiliations_are_kept_when_the_name_lookup_fails(self):
        self.lookups.add_pilot(PILOT, "Outside Pilot")
        lookups = self.lookups

        def fail_names(*, body):
            lookups.failure = http_error(502)
            return FakeLookup(lookups, "names", body)

        with mock.patch.object(lookups, "PostUniverseNames", side_effect=fail_names):
            with self.assertLogs(esi_provider.logger, "WARNING"), self.assertRaises(FleetESIError):
                resolve_and_store([PILOT])

        self.assertTrue(CharacterAffiliation.objects.filter(character_id=PILOT, corporation_id=CORP).exists())


# ---------------------------------------------------------------------------
# Fleet tracking
# ---------------------------------------------------------------------------


class TrackingNamesTests(LookupMixin, FakeESIMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.fc = f.create_user(perms=f.FC_PERMS, main_name="Name Boss")
        self.fc_char = f.main_of(self.fc)
        f.add_token(self.fc, self.fc_char)
        self.operation = f.create_operation(self.fc, esi_fleet_id=FLEET_ID)
        self.outsider = SimpleNamespace(character_id=PILOT)
        self.esi.members[FLEET_ID] = [f.esi_member(self.fc_char), f.esi_member(self.outsider)]
        self.lookups.add_pilot(PILOT, "Outside Pilot")

    def poll(self):
        return track_operation(self.operation.pk)

    def state(self):
        return FleetMemberState.objects.get(operation=self.operation, character_id=PILOT)

    def test_unknown_member_is_named_after_the_poll_commits(self):
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(self.poll(), 2)

        state = self.state()
        self.assertIsNone(state.auth_user)
        self.assertEqual(state.character_name, "Outside Pilot")
        self.assertEqual((state.corporation_id, state.corporation_name), (CORP, "Outside Corp"))
        self.assertEqual((state.alliance_id, state.alliance_name), (ALLIANCE, "Outside Alliance"))
        join = FleetMemberEvent.objects.get(operation=self.operation, character_id=PILOT)
        self.assertEqual(join.character_name, "Outside Pilot")
        # Only the outsider is looked up; Alliance Auth knows the FC.
        self.assertEqual(self.lookups.bodies("affiliation"), [[PILOT]])

    def test_nothing_is_looked_up_until_the_transaction_commits(self):
        with self.captureOnCommitCallbacks() as callbacks:
            self.poll()

        self.assertEqual(len(callbacks), 1)
        self.assertEqual(self.lookups.calls, [])
        self.assertEqual(self.state().character_name, f"Character {PILOT}")

    def test_lookups_are_queued_once_per_fleet_in_two_minutes(self):
        with mock.patch.object(resolve_character_names, "delay") as delay:
            with self.captureOnCommitCallbacks(execute=True):
                self.poll()
            with self.captureOnCommitCallbacks(execute=True):
                queue_name_resolution(self.operation.pk, [PILOT])
            other = f.create_operation(self.fc)
            with self.captureOnCommitCallbacks(execute=True):
                queue_name_resolution(other.pk, [PILOT])

        self.assertEqual(
            [call.args for call in delay.call_args_list], [(self.operation.pk, [PILOT]), (other.pk, [PILOT])]
        )
        self.assertLessEqual(cache.ttl(f"fleetops:names:{self.operation.pk}"), names.RESOLVE_DEBOUNCE_SECONDS)

    def test_an_unreachable_queue_does_not_break_tracking(self):
        with mock.patch.object(resolve_character_names, "delay", side_effect=QueueError("broker down")):
            with self.assertLogs("extensions", "ERROR") as logs, self.captureOnCommitCallbacks(execute=True):
                self.assertEqual(self.poll(), 2)

        self.operation.refresh_from_db()
        self.assertEqual(self.operation.last_error, "")
        log = "\n".join(logs.output)
        self.assertIn("Queueing the name lookup failed (OperationalError)", log)
        self.assertNotIn("broker down", log)

    def test_known_members_are_not_queued_again(self):
        with self.captureOnCommitCallbacks(execute=True):
            self.poll()
        cache.clear()

        with self.captureOnCommitCallbacks() as callbacks:
            self.poll()

        self.assertEqual(callbacks, [])
        self.assertEqual(self.state().character_name, "Outside Pilot")

    def test_a_placeholder_never_replaces_a_real_name(self):
        with self.captureOnCommitCallbacks(execute=True):
            self.poll()
        # The cache rows are gone (for example pruned by an admin) and ESI is down.
        EveEntityName.objects.all().delete()
        CharacterAffiliation.objects.all().delete()
        self.lookups.failure = http_error(503)
        cache.clear()

        with self.assertLogs(esi_provider.logger, "WARNING"), self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(self.poll(), 2)

        state = self.state()
        self.assertEqual(state.character_name, "Outside Pilot")
        self.assertEqual(state.corporation_name, "Outside Corp")

    def poll_members(self, *characters):
        self.esi.members[FLEET_ID] = [f.esi_member(self.fc_char), *(f.esi_member(c) for c in characters)]
        cache.delete(f"fleetops:track:attempt:{self.operation.pk}")
        with self.captureOnCommitCallbacks(execute=True):
            return self.poll()

    def names_on_the_fleet(self):
        return dict(
            FleetMemberState.objects.filter(operation=self.operation).values_list("character_id", "character_name")
        )

    def test_member_who_left_during_the_debounce_is_named_by_a_later_poll(self):
        second = SimpleNamespace(character_id=PILOT_TWO)
        self.lookups.add_pilot(PILOT_TWO, "Second Pilot")

        self.poll_members(self.outsider)
        # The second outsider joins while the first lookup still holds the fleet's slot, then leaves.
        self.poll_members(self.outsider, second)
        cache.delete(f"fleetops:names:{self.operation.pk}")
        self.poll_members(self.outsider)

        self.assertEqual(self.names_on_the_fleet()[PILOT_TWO], "Second Pilot")
        state = FleetMemberState.objects.get(operation=self.operation, character_id=PILOT_TWO)
        self.assertEqual((state.is_active, state.corporation_name), (False, "Outside Corp"))
        self.assertEqual(self.lookups.bodies("affiliation"), [[PILOT], [PILOT_TWO]])

    def test_fleet_end_names_members_seen_just_before_it(self):
        second = SimpleNamespace(character_id=PILOT_TWO)
        self.lookups.add_pilot(PILOT_TWO, "Second Pilot")
        self.poll_members(self.outsider)
        self.poll_members(self.outsider, second)
        self.assertEqual(self.names_on_the_fleet()[PILOT_TWO], f"Character {PILOT_TWO}")

        with self.captureOnCommitCallbacks(execute=True):
            end_fleet(self.operation, automatic=True)

        self.assertEqual(self.names_on_the_fleet()[PILOT_TWO], "Second Pilot")
        self.assertEqual(
            FleetMemberEvent.objects.filter(operation=self.operation, character_id=PILOT_TWO)
            .values_list("character_name", flat=True)
            .distinct()
            .get(),
            "Second Pilot",
        )

    def test_members_esi_cannot_name_are_not_queued_on_every_poll(self):
        self.lookups.names.pop(PILOT)
        self.lookups.affiliations.pop(PILOT)
        self.poll_members(self.outsider)
        cache.delete(f"fleetops:names:{self.operation.pk}")

        with mock.patch.object(resolve_character_names, "delay") as delay:
            self.poll_members(self.outsider)

        delay.assert_not_called()

    def test_the_task_never_raises(self):
        self.poll()
        self.lookups.failure = http_error(502)

        with self.assertLogs(esi_provider.logger, "WARNING"):
            self.assertEqual(resolve_character_names(self.operation.pk, [PILOT]), 0)

        with mock.patch("fleetops.tasks.resolve_and_store", side_effect=RuntimeError("boom")), self.assertLogs(
            "extensions", "ERROR"
        ) as logs:
            resolve_character_names(self.operation.pk, [PILOT])
        self.assertIn("Name lookup failed (RuntimeError)", "\n".join(logs.output))
        self.assertNotIn("boom", "\n".join(logs.output))


class ApplyNamesTests(TestCase):
    def setUp(self):
        self.fc = f.create_user(perms=f.FC_PERMS)
        self.operation = f.create_operation(self.fc, status=FleetOperation.Status.CLOSED)
        now = timezone.now()
        self.state = FleetMemberState.objects.create(
            operation=self.operation,
            character_id=PILOT,
            character_name=f"Character {PILOT}",
            first_seen=now,
            last_seen=now,
            is_active=False,
        )
        self.event = FleetMemberEvent.objects.create(
            operation=self.operation,
            character_id=PILOT,
            character_name=f"Character {PILOT}",
            event_type=FleetMemberEvent.EventType.JOIN,
        )
        self.role = OperationRoleAssignment.objects.create(
            operation=self.operation,
            role=OperationRoleAssignment.Role.LOGI_ANCHOR,
            character_id=PILOT,
            character_name=f"Character {PILOT}",
        )
        cache_name(PILOT, "Outside Pilot")
        cache_name(CORP, "Outside Corp", "corporation")
        cache_name(ALLIANCE, "Outside Alliance", "alliance")
        cache_affiliation(PILOT)

    def test_states_events_and_roles_of_a_closed_fleet_are_named(self):
        changed = apply_names_to_operation(self.operation.pk, [PILOT])

        self.assertGreaterEqual(changed, 3)
        self.state.refresh_from_db()
        self.event.refresh_from_db()
        self.role.refresh_from_db()
        self.assertEqual(self.state.character_name, "Outside Pilot")
        self.assertEqual(self.state.alliance_name, "Outside Alliance")
        self.assertEqual(self.event.character_name, "Outside Pilot")
        self.assertEqual(self.role.character_name, "Outside Pilot")
        self.assertEqual(self.role.corporation_name, "Outside Corp")

    def test_rows_with_a_real_name_or_an_auth_user_are_left_alone(self):
        self.event.character_name = "Renamed Earlier"
        self.event.save()
        member = f.create_user()
        FleetMemberState.objects.filter(pk=self.state.pk).update(auth_user=member, character_name="Auth Name")

        apply_names_to_operation(self.operation.pk, [PILOT])

        self.state.refresh_from_db()
        self.event.refresh_from_db()
        self.assertEqual(self.state.character_name, "Auth Name")
        self.assertEqual(self.state.corporation_name, "")
        self.assertEqual(self.event.character_name, "Renamed Earlier")

    def test_nothing_changes_without_cached_data(self):
        self.assertEqual(apply_names_to_operation(self.operation.pk, [PILOT_TWO]), 0)


# ---------------------------------------------------------------------------
# Boss handover to a character Alliance Auth does not know
# ---------------------------------------------------------------------------


class HandoverBossIdTests(FakeESIMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.fc = f.create_user(perms=f.FC_PERMS, main_name="Old Boss")
        self.fc_char = f.main_of(self.fc)
        f.add_token(self.fc, self.fc_char)
        self.operation = f.create_operation(self.fc, esi_fleet_id=FLEET_ID)
        f.settings(auto_end_enabled=True, auto_end_missing_count=2)
        cache_name(BOSS, "Outside Boss")
        self.readable()
        self.poll()

    def poll(self):
        result = track_operation(self.operation.pk)
        self.operation.refresh_from_db()
        return result

    def readable(self):
        self.esi.put_in_fleet(self.fc_char, role="fleet_commander")
        self.esi.members[FLEET_ID] = [f.esi_member(self.fc_char)]

    def hand_over(self):
        self.esi.put_in_fleet(self.fc_char, role="fleet_commander", boss=BOSS)
        self.esi.members[FLEET_ID] = http_error(404)

    def test_observation_names_the_new_boss_and_keeps_its_id(self):
        self.hand_over()

        observation = observe_unreadable_fleet(self.operation)

        self.assertEqual(observation, (EndReason.BOSS_HANDOVER, "Outside Boss"))
        self.assertEqual(observation.boss_id, BOSS)
        self.esi.character_fleets.clear()
        self.assertIsNone(observe_unreadable_fleet(self.operation).boss_id)

    def test_handover_keeps_the_new_boss_id_and_its_cached_name(self):
        self.hand_over()

        self.assertEqual(self.poll(), "BOSS_HANDOVER")

        self.assertEqual(self.operation.missing_detail, "Outside Boss")
        self.assertEqual(self.operation.fleet_boss_character_id, BOSS)

    def test_handing_the_boss_back_restores_the_fc_as_boss(self):
        self.hand_over()
        self.poll()
        self.readable()

        self.assertEqual(self.poll(), 1)

        self.assertEqual(self.operation.fleet_boss_character_id, self.fc_char.character_id)

    def test_a_fleet_ended_by_a_handover_keeps_the_new_boss(self):
        self.hand_over()
        self.poll()

        self.assertEqual(self.poll(), "auto-ended")

        self.assertEqual(self.operation.status, FleetOperation.Status.CLOSED)
        self.assertEqual(self.operation.end_reason, EndReason.BOSS_HANDOVER)
        self.assertEqual(self.operation.end_detail, "Outside Boss")
        self.assertEqual(self.operation.fleet_boss_character_id, BOSS)


# ---------------------------------------------------------------------------
# Members on the fleet page
# ---------------------------------------------------------------------------


class MemberListPageTests(TestCase):
    def setUp(self):
        self.fc = f.create_user(perms=f.FC_PERMS, main_name="Page Boss")
        self.member = f.create_user(perms=f.MEMBER_PERMS, main_name="Line Member")
        self.member_alt = f.add_alt(self.member, "Member Alt")
        self.operation = f.create_operation(self.fc)
        now = timezone.now()
        common = dict(operation=self.operation, first_seen=now, last_seen=now, is_active=True)
        FleetMemberState.objects.create(
            character_id=PILOT,
            character_name="Outside Pilot",
            corporation_id=CORP,
            corporation_name="Outside Corp",
            alliance_id=ALLIANCE,
            alliance_name="Outside Alliance",
            ship_type_name="Secret Doctrine Hull",
            **common,
        )
        member_main = f.main_of(self.member)
        FleetMemberState.objects.create(
            character_id=member_main.character_id,
            character_name=member_main.character_name,
            auth_user=self.member,
            ship_type_name="Guardian",
            **common,
        )
        f.add_attendance(self.operation, self.member)
        f.add_attendance(self.operation, self.member, self.member_alt)
        f.add_attendance(self.operation, self.fc)
        OperationRoleAssignment.objects.create(
            operation=self.operation,
            role=OperationRoleAssignment.Role.LOGI_ANCHOR,
            character_id=PILOT,
            character_name="Outside Pilot",
        )

    def page(self, user):
        self.client.force_login(user)
        response = self.client.get(reverse("fleetops:operation_detail", args=[self.operation.uuid]))
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def test_member_without_auth_account_is_marked_and_linked(self):
        content = self.page(self.fc)

        badge = '<span class="badge bg-secondary">Not in Alliance Auth</span>'
        self.assertIn(f"Outside Pilot {badge}", content)
        # Members with an account are not marked; the PAP preview lists the pilot once more without PAP.
        self.assertEqual(content.count(badge), 1)
        self.assertIn(f'href="https://evewho.com/character/{PILOT}" target="_blank" rel="noopener noreferrer"', content)
        self.assertIn(f'href="https://zkillboard.com/character/{PILOT}/" target="_blank" rel="noopener noreferrer"', content)
        self.assertIn('Outside Corp<div class="small text-muted">Outside Alliance</div>', content)

    def test_members_of_an_open_fleet_are_hidden_from_other_viewers(self):
        content = self.page(self.member)

        self.assertIn('id="composition-hidden"', content)
        for hidden in ("Secret Doctrine Hull", "Outside Pilot", "evewho.com"):
            self.assertNotIn(hidden, content)
        # Their own attendance stays visible.
        self.assertIn("Member Alt", content)
        self.assertIn("Line Member", content)

    def test_people_who_see_the_members_of_an_open_fleet(self):
        lead = f.create_user(perms=f.FC_LEAD_PERMS)
        viewer = f.create_user(perms=f.MEMBER_PERMS + ["fleetops.view_all_fleets"])
        co_fc = f.create_user(perms=f.MEMBER_PERMS + ["fleetops.manage_own_fleet"])
        OperationRoleAssignment.objects.create(
            operation=self.operation,
            role=OperationRoleAssignment.Role.LOGI_ANCHOR,
            character_id=f.main_of(co_fc).character_id,
            character_name="Co Fc",
            auth_user=co_fc,
            grants_fc_credit=True,
        )
        for user in (self.fc, lead, viewer, co_fc):
            with self.subTest(user=user.username):
                content = self.page(user)
                self.assertIn("Secret Doctrine Hull", content)
                self.assertNotIn('id="composition-hidden"', content)

    def test_everyone_sees_the_members_once_the_fleet_is_closed(self):
        for status in (FleetOperation.Status.ENDING, FleetOperation.Status.CLOSED):
            with self.subTest(status=status):
                FleetOperation.objects.filter(pk=self.operation.pk).update(status=status, ended_at=timezone.now())
                content = self.page(self.member)
                self.assertEqual("Secret Doctrine Hull" in content, status == FleetOperation.Status.CLOSED)
                self.assertEqual("Outside Pilot" in content, status == FleetOperation.Status.CLOSED)
