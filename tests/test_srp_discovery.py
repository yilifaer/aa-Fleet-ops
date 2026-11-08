# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Automatic loss discovery: the data model (INT-2), the scan task (INT-3), the fleet match (INT-5) and the cleanup.

ESI is never reached: the list of recent killmails and the killmails are served by ``FakeEsi``, the universe lookups
and the names are replaced, and the clock and the random jitter are passed in or patched.
"""

from datetime import timedelta
from decimal import Decimal
from unittest import mock

from allianceauth.authentication.models import CharacterOwnership
from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone

from fleetops import tasks as fleet_tasks
from fleetops.constants import KILLMAIL_READ_SCOPE
from fleetops.models import SrpLoss, SrpLossScan, SrpSettings
from fleetops.providers.esi import FleetESIError
from fleetops.providers.esi_killmails import RecentKillmails
from fleetops.services.universe import SystemInfo
from fleetops.srp import discovery, display, presence

from . import factories as f
from .test_srp_presence import PresenceBase, at, tracked
from .test_srp_presence import Event as TrackEvent

Kind = SrpLoss.Kind
Status = SrpLossScan.Status
NEWEST = 200_000_000
# The real name lookup, kept before ScanBase replaces it, for the tests of the lookup itself.
REAL_NAMES_OF = discovery.killmails.names_of


class FakeEsi:
    """The list of recent killmails per character and the killmails, with a record of every call."""

    def __init__(self, now):
        self.now = now
        self.pages = {}
        self.payloads = {}
        self.list_calls = []
        self.detail_calls = []
        self.list_error = None
        self.detail_error = None

    def killmail(self, victim, *, minutes_ago, ship_type_id=f.RIFTER, **extra):
        """A killmail of ``victim`` (a character or None); newer killmails have larger IDs. Returns its ID."""
        killmail_id = NEWEST - int(minutes_ago)
        self.payloads[killmail_id] = f.esi_killmail_payload(
            killmail_id,
            victim=victim,
            ship_type_id=ship_type_id,
            time=self.now - timedelta(minutes=minutes_ago),
            **extra,
        )
        return killmail_id

    def listed(self, character, *killmail_ids, pages=None):
        """What ESI lists for the character: one page, or ``pages`` (a list of ID lists)."""
        rows = pages if pages is not None else [list(killmail_ids)]
        self.pages[character.character_id if hasattr(character, "character_id") else character] = [
            sorted(((i, f"{i:040x}") for i in page), reverse=True) for page in rows
        ]

    def list_page(self, token, character_id, page=1):
        self.list_calls.append((character_id, page))
        if self.list_error is not None:
            raise self.list_error
        pages = self.pages.get(character_id, [])
        rows = RecentKillmails(pages[page - 1] if page <= len(pages) else [])
        rows.pages = max(len(pages), 1)
        return rows

    def detail(self, killmail_id, killmail_hash):
        self.detail_calls.append(killmail_id)
        if self.detail_error is not None:
            raise self.detail_error
        return self.payloads[killmail_id]


class ScanBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = f.create_user("scanned", perms=f.SRP_MEMBER_PERMS)
        cls.main = f.main_of(cls.user)
        cls.alt_a = f.add_alt(cls.user, "Scan Alt A")
        cls.alt_b = f.add_alt(cls.user, "Scan Alt B")
        for character in (cls.main, cls.alt_a, cls.alt_b):
            f.add_token(cls.user, character, scopes=[KILLMAIL_READ_SCOPE])
        cls.srp = f.srp_settings(enabled=True, loss_discovery_enabled=True, loss_discovery_interval_minutes=60)

    def setUp(self):
        super().setUp()
        self.now = timezone.now()
        self.esi = FakeEsi(self.now)
        for name, target in (
            ("get_recent_killmails", self.esi.list_page),
            ("get_killmail", self.esi.detail),
        ):
            patcher = mock.patch.object(discovery, name, side_effect=target)
            patcher.start()
            self.addCleanup(patcher.stop)
        # L-5: the scan names all ships and systems of a character in one call (names_of), not one call per loss.
        names = mock.patch.object(
            discovery.killmails,
            "names_of",
            side_effect=lambda ships, systems, budget=None: (dict.fromkeys(ships, "Rifter"), dict.fromkeys(systems, "Amamake")),
        )
        names.start()
        self.addCleanup(names.stop)
        # No jitter unless a test asks for it.
        jitter = mock.patch.object(discovery.random, "uniform", return_value=0)
        jitter.start()
        self.addCleanup(jitter.stop)
        self.clear_cache()
        self.addCleanup(self.clear_cache)

    def clear_cache(self):
        for key in (discovery.SCAN_LOCK_KEY, discovery.QUEUED_KEY, discovery.BACKOFF_KEY):
            cache.delete(key)

    def scan(self, now=None, **kwargs):
        return discovery.run_scan(now or self.now, **kwargs)

    def rows(self, **filters):
        return SrpLoss.objects.filter(**filters)


# --- INT-2: the data model ----------------------------------------------------------------------------------------


class ModelTests(TestCase):
    def test_a_killmail_is_stored_once_per_character(self):
        SrpLoss.objects.create(character_id=1, killmail_id=2, kind=Kind.LOSS)
        with self.assertRaises(IntegrityError), transaction.atomic():
            SrpLoss.objects.create(character_id=1, killmail_id=2, kind=Kind.KILL)
        SrpLoss.objects.create(character_id=3, killmail_id=2, kind=Kind.KILL)

    def test_a_concurrent_write_of_the_same_row_leaves_one_row(self):
        SrpLoss.objects.create(character_id=1, killmail_id=2, kind=Kind.LOSS)

        SrpLoss.objects.bulk_create([SrpLoss(character_id=1, killmail_id=2, kind=Kind.LOSS)], ignore_conflicts=True)

        self.assertEqual(SrpLoss.objects.count(), 1)

    def test_a_scan_row_is_unique_per_character(self):
        SrpLossScan.objects.create(character_id=5)
        with self.assertRaises(IntegrityError), transaction.atomic():
            SrpLossScan.objects.create(character_id=5)

    def test_the_tables_have_no_default_permissions(self):
        self.assertEqual(SrpLoss._meta.default_permissions, ())
        self.assertEqual(SrpLossScan._meta.default_permissions, ())

    def test_the_settings_start_with_the_feature_off(self):
        row = SrpSettings.get_solo()
        self.assertEqual(
            (row.loss_discovery_enabled, row.loss_discovery_interval_minutes, row.require_all_alts_authorized),
            (False, 60, False),
        )

    def test_the_interval_is_limited_to_a_quarter_of_an_hour_to_a_day(self):
        from django.core.exceptions import ValidationError

        row = SrpSettings.get_solo()
        for minutes in (14, 1441):
            row.loss_discovery_interval_minutes = minutes
            with self.assertRaises(ValidationError):
                row.full_clean()
        for minutes in (15, 1440):
            row.loss_discovery_interval_minutes = minutes
            row.full_clean()

    def test_the_two_tables_are_read_only_in_the_admin(self):
        from django.contrib import admin

        for model in (SrpLoss, SrpLossScan):
            model_admin = admin.site._registry[model]
            self.assertFalse(model_admin.has_add_permission(mock.Mock()))
            self.assertFalse(model_admin.has_delete_permission(mock.Mock()))


# --- INT-3: the scan -----------------------------------------------------------------------------------------------


class ScanTests(ScanBase):
    def test_a_loss_is_stored_with_its_ship_place_and_time(self):
        loss_id = self.esi.killmail(self.main, minutes_ago=30)
        self.esi.listed(self.main, loss_id)

        result = self.scan()

        row = self.rows().get()
        self.assertEqual((result.scanned, result.ended), (3, "done"))
        self.assertEqual((row.killmail_id, row.character_id, row.kind), (loss_id, self.main.character_id, Kind.LOSS))
        self.assertEqual((row.ship_type_id, row.ship_type_name, row.solar_system_name), (f.RIFTER, "Rifter", "Amamake"))
        self.assertEqual(row.solar_system_id, f.SRP_SYSTEM[0])
        self.assertEqual(row.killmail_hash, f"{loss_id:040x}")
        self.assertAlmostEqual(row.killmail_time, self.now - timedelta(minutes=30), delta=timedelta(seconds=2))

    def test_scanning_twice_keeps_the_rows_and_reads_each_killmail_once(self):
        loss_id = self.esi.killmail(self.main, minutes_ago=30)
        self.esi.listed(self.main, loss_id)
        self.scan()

        self.scan(self.now + timedelta(hours=2))

        self.assertEqual(self.rows().count(), 1)
        self.assertEqual(self.esi.detail_calls, [loss_id])

    def test_the_main_and_both_alts_are_scanned_and_an_alt_as_attacker_is_a_kill(self):
        main_loss = self.esi.killmail(self.main, minutes_ago=50)
        alt_loss = self.esi.killmail(self.alt_a, minutes_ago=40)
        awox = self.esi.killmail(self.alt_b, minutes_ago=20)  # the victim is the alt itself
        # A killmail the alt only took part in: another pilot died, the character is not the victim.
        kill = self.esi.killmail(f.main_of(f.create_user("victim")), minutes_ago=10)
        self.esi.listed(self.main, main_loss)
        self.esi.listed(self.alt_a, alt_loss)
        self.esi.listed(self.alt_b, awox, kill)

        self.scan()

        self.assertEqual(
            {(row.character_id, row.killmail_id, row.kind) for row in self.rows()},
            {
                (self.main.character_id, main_loss, Kind.LOSS),
                (self.alt_a.character_id, alt_loss, Kind.LOSS),
                (self.alt_b.character_id, awox, Kind.LOSS),
                (self.alt_b.character_id, kill, Kind.KILL),
            },
        )
        self.assertEqual(SrpLossScan.objects.filter(status=Status.OK).count(), 3)

    def test_a_kill_keeps_no_ship_or_place(self):
        kill = self.esi.killmail(f.main_of(f.create_user("victim2")), minutes_ago=10)
        self.esi.listed(self.main, kill)

        self.scan()

        row = self.rows().get()
        self.assertEqual((row.kind, row.ship_type_id, row.ship_type_name, row.solar_system_name), (Kind.KILL, None, "", ""))

    def test_only_the_newest_killmails_in_the_window_are_read_in_full(self):
        self.srp = f.srp_settings(claim_deadline_days=7)
        ids = [self.esi.killmail(self.main, minutes_ago=minutes) for minutes in (60, 120, 180)]
        old = [self.esi.killmail(self.main, minutes_ago=60 * 24 * 8 + number) for number in range(97)]
        self.esi.listed(self.main, *ids, *old)

        self.scan()

        self.assertEqual(len(self.esi.detail_calls), 4)
        self.assertEqual(self.rows(kind=Kind.LOSS).count(), 3)
        self.assertEqual(self.rows(kind=Kind.OLD).count(), 97)
        self.assertEqual(self.rows().count(), 100)

    def test_old_rows_are_not_read_again(self):
        self.srp = f.srp_settings(claim_deadline_days=7)
        old = [self.esi.killmail(self.main, minutes_ago=60 * 24 * 8 + number) for number in range(5)]
        self.esi.listed(self.main, *old)
        self.scan()
        calls = len(self.esi.detail_calls)

        self.scan(self.now + timedelta(hours=2))

        self.assertEqual(len(self.esi.detail_calls), calls)

    def test_without_a_deadline_the_window_is_ninety_days(self):
        recent = self.esi.killmail(self.main, minutes_ago=60 * 24 * 80)
        old = self.esi.killmail(self.main, minutes_ago=60 * 24 * 100)
        self.esi.listed(self.main, recent, old)

        self.scan()

        self.assertEqual(self.rows().get(killmail_id=recent).kind, Kind.LOSS)
        self.assertEqual(self.rows().get(killmail_id=old).kind, Kind.OLD)

    def test_a_list_of_more_than_one_page_is_read_to_the_end(self):
        pages = [[self.esi.killmail(self.main, minutes_ago=page * 100 + number) for number in range(3)] for page in range(3)]
        self.esi.listed(self.main, pages=pages)

        self.scan()

        self.assertEqual(self.rows().count(), 9)
        self.assertEqual([call for call in self.esi.list_calls if call[0] == self.main.character_id], [(self.main.character_id, 1), (self.main.character_id, 2), (self.main.character_id, 3)])

    def test_at_most_five_pages_are_read(self):
        pages = [[self.esi.killmail(self.main, minutes_ago=page * 100 + number) for number in range(2)] for page in range(8)]
        self.esi.listed(self.main, pages=pages)

        self.scan()

        self.assertEqual(self.rows().count(), 10)
        self.assertEqual(max(page for _character, page in self.esi.list_calls), discovery.MAX_RECENT_PAGES)

    def test_a_known_killmail_stops_the_paging(self):
        first = [self.esi.killmail(self.main, minutes_ago=number) for number in range(3)]
        second = [self.esi.killmail(self.main, minutes_ago=100 + number) for number in range(3)]
        self.esi.listed(self.main, pages=[first, second])
        # The oldest ID of the page is known: everything older was stored by an earlier run.
        SrpLoss.objects.create(character_id=self.main.character_id, killmail_id=first[2], kind=Kind.LOSS)

        self.scan()

        self.assertEqual({call[1] for call in self.esi.list_calls if call[0] == self.main.character_id}, {1})
        self.assertNotIn(first[2], self.esi.detail_calls)

    def test_a_backlog_behind_the_per_character_limit_is_read_on_the_next_pages(self):
        cap = discovery.MAX_FETCH_PER_CHARACTER
        first = [self.esi.killmail(self.main, minutes_ago=number) for number in range(cap + 5)]
        second = [self.esi.killmail(self.main, minutes_ago=1000 + number) for number in range(10)]
        self.esi.listed(self.main, pages=[first, second])

        self.scan()
        self.assertEqual(self.rows().count(), cap)
        self.scan(self.now + timedelta(minutes=5))

        self.assertEqual(self.rows().count(), cap + 15)
        self.assertEqual(set(second), set(self.rows().filter(killmail_id__in=second).values_list("killmail_id", flat=True)))
        self.assertEqual(SrpLossScan.objects.get(character_id=self.main.character_id).status, Status.OK)

    def test_a_killmail_missing_for_the_hash_is_recorded_as_old(self):
        loss_id = self.esi.killmail(self.main, minutes_ago=30)
        self.esi.listed(self.main, loss_id)
        self.esi.detail_error = FleetESIError("KILLMAIL_NOT_FOUND", "gone", 404)

        self.scan()

        self.assertEqual(self.rows().get().kind, Kind.OLD)

    def test_the_per_character_limit_leaves_the_character_due(self):
        ids = [self.esi.killmail(self.main, minutes_ago=number) for number in range(discovery.MAX_FETCH_PER_CHARACTER + 5)]
        self.esi.listed(self.main, *ids)

        self.scan()

        self.assertEqual(self.rows().count(), discovery.MAX_FETCH_PER_CHARACTER)
        self.assertIsNone(SrpLossScan.objects.get(character_id=self.main.character_id).last_checked_at)
        self.scan(self.now + timedelta(minutes=5))
        self.assertEqual(self.rows().count(), discovery.MAX_FETCH_PER_CHARACTER + 5)
        self.assertEqual(len(self.esi.detail_calls), discovery.MAX_FETCH_PER_CHARACTER + 5)

    def test_the_next_read_follows_the_interval_with_up_to_ten_percent_more(self):
        with mock.patch.object(discovery.random, "uniform", return_value=0.1) as uniform:
            self.scan()

        scan = SrpLossScan.objects.get(character_id=self.main.character_id)
        uniform.assert_called_with(0, 0.1)
        self.assertEqual(scan.next_check_at, self.now + timedelta(minutes=66))
        self.assertEqual((scan.status, scan.failures, scan.last_checked_at), (Status.OK, 0, self.now))

    def test_a_character_is_not_due_before_its_time(self):
        self.scan()
        self.esi.list_calls.clear()

        result = self.scan(self.now + timedelta(minutes=30))

        self.assertEqual((result.scanned, self.esi.list_calls), (0, []))

    def test_only_users_who_may_request_srp_with_their_own_token_are_scanned(self):
        nobody = f.create_user("nobody-scan", perms=f.MEMBER_PERMS)
        f.add_token(nobody, f.main_of(nobody), scopes=[KILLMAIL_READ_SCOPE])
        other = f.create_user("other-scan", perms=f.SRP_MEMBER_PERMS)
        # A character without a token of its owner, and a token without the scope.
        f.add_alt(other, "No Token Alt")
        f.add_token(other, f.main_of(other), scopes=["esi-location.read_online.v1"])

        candidates = discovery.candidate_characters()

        self.assertEqual(
            set(candidates), {self.main.character_id, self.alt_a.character_id, self.alt_b.character_id}
        )
        self.assertEqual(set(candidates.values()), {self.user.pk})

    def test_an_inactive_user_is_not_scanned(self):
        self.user.is_active = False
        self.user.save(update_fields=["is_active"])

        self.assertEqual(discovery.candidate_characters(), {})

    def test_a_character_that_changed_hands_is_scanned_for_the_new_owner(self):
        self.scan()
        newcomer = f.create_user("newcomer", perms=f.SRP_MEMBER_PERMS)
        f.add_token(newcomer, self.alt_b, scopes=[KILLMAIL_READ_SCOPE])
        CharacterOwnership.objects.filter(character=self.alt_b).update(user=newcomer)

        self.scan(self.now + timedelta(hours=2))

        self.assertEqual(SrpLossScan.objects.get(character_id=self.alt_b.character_id).user_id, newcomer.pk)
        # The earlier owner's token for the character is no longer used.
        self.assertNotIn(self.alt_b.character_id, discovery.candidate_characters(self.user.pk))

    def test_the_scan_for_one_user_leaves_the_others_alone(self):
        other = f.create_user("other-user-scan", perms=f.SRP_MEMBER_PERMS)
        f.add_token(other, f.main_of(other), scopes=[KILLMAIL_READ_SCOPE])

        result = self.scan(user_id=other.pk)

        self.assertEqual(result.scanned, 1)
        self.assertEqual({call[0] for call in self.esi.list_calls}, {f.main_of(other).character_id})


class LocationTests(ScanBase):
    def test_a_loss_gets_its_security_region_and_ship_group(self):
        loss_id = self.esi.killmail(self.main, minutes_ago=30, solar_system_id=30000142)
        self.esi.listed(self.main, loss_id)
        info = SystemInfo(30000142, Decimal("0.9459"), 20000020, 10000002)

        with mock.patch.object(discovery.universe, "system_infos", return_value={30000142: info}) as systems:
            with mock.patch.object(discovery.universe, "type_groups", return_value={f.RIFTER: 25}) as groups:
                self.scan()

        row = self.rows().get()
        self.assertEqual((row.solar_system_security, row.region_id, row.ship_group_id), (Decimal("0.9459"), 10000002, 25))
        systems.assert_called_once_with({30000142})
        groups.assert_called_once_with({f.RIFTER})

    def test_the_lookup_is_made_once_for_all_losses_of_a_character(self):
        ids = [self.esi.killmail(self.main, minutes_ago=number + 1) for number in range(5)]
        self.esi.listed(self.main, *ids)

        with mock.patch.object(discovery.universe, "system_infos", return_value={}) as systems:
            with mock.patch.object(discovery.universe, "type_groups", return_value={}) as groups:
                self.scan()

        self.assertEqual((systems.call_count, groups.call_count), (1, 1))

    def test_a_lookup_that_fails_leaves_the_values_empty(self):
        loss_id = self.esi.killmail(self.main, minutes_ago=30)
        self.esi.listed(self.main, loss_id)

        with mock.patch.object(discovery.universe, "system_infos", side_effect=RuntimeError("down")):
            self.scan()

        row = self.rows().get()
        self.assertEqual((row.kind, row.solar_system_security, row.region_id, row.ship_group_id), (Kind.LOSS, None, None, None))

    def test_the_war_of_the_killmail_is_kept(self):
        loss_id = self.esi.killmail(self.main, minutes_ago=30, war_id=777)
        self.esi.listed(self.main, loss_id)

        self.scan()

        self.assertEqual(self.rows().get().war_id, 777)

    def test_the_universe_calls_count_against_the_detail_budget(self):
        loss_id = self.esi.killmail(self.main, minutes_ago=30)
        self.esi.listed(self.main, loss_id)
        budget = discovery.ScanBudget()
        calls = iter([0, 3])

        with mock.patch.object(discovery.esi_universe, "calls_made", side_effect=lambda: next(calls)):
            scan = SrpLossScan.objects.create(character_id=self.main.character_id, user=self.user)
            discovery.scan_character(scan, SrpSettings.get_solo(), self.now, budget)

        self.assertEqual(budget.details.remaining, discovery.LOSS_DETAIL_BUDGET - 1 - 3)


class NameLookupTests(ScanBase):
    """L-5: without the SDE the names of all losses of a character are looked up once, within the detail budget."""

    def test_one_lookup_names_every_loss_of_the_character(self):
        ids = [self.esi.killmail(self.main, minutes_ago=number + 1, solar_system_id=30000142 + number) for number in range(4)]
        self.esi.listed(self.main, *ids)
        looked_up = []

        def entity_names(wanted, **kwargs):
            looked_up.append(sorted(wanted))
            return {pk: f"Name {pk}" for pk in wanted}

        discovery.killmails.names_of.side_effect = REAL_NAMES_OF
        with (
            mock.patch.object(discovery.killmails.sde, "item_type_names", return_value={}),
            mock.patch.object(discovery.killmails.sde, "solar_system_names", return_value={}),
            mock.patch.object(discovery.killmails.names, "entity_names", side_effect=entity_names),
            mock.patch.object(discovery.universe, "system_infos", return_value={}),
            mock.patch.object(discovery.universe, "type_groups", return_value={}),
        ):
            self.scan()

        self.assertEqual(len(looked_up), 1)
        self.assertEqual(len(looked_up[0]), 5)  # the ship type and four systems
        self.assertEqual(set(self.rows().values_list("ship_type_name", flat=True)), {f"Name {f.RIFTER}"})
        self.assertFalse(self.rows().filter(solar_system_name="").exists())

    def test_the_name_lookup_gets_the_detail_budget(self):
        loss_id = self.esi.killmail(self.main, minutes_ago=30)
        self.esi.listed(self.main, loss_id)
        budget = discovery.ScanBudget()
        seen = []

        def lookup(ships, systems, budget=None):
            seen.append(budget)
            budget.take()
            return {}, {}

        discovery.killmails.names_of.side_effect = lookup
        scan = SrpLossScan.objects.create(character_id=self.main.character_id, user=self.user)
        with (
            mock.patch.object(discovery.universe, "system_infos", return_value={}),
            mock.patch.object(discovery.universe, "type_groups", return_value={}),
        ):
            discovery.scan_character(scan, SrpSettings.get_solo(), self.now, budget)

        self.assertIs(seen[0], budget.details)
        self.assertEqual(budget.details.remaining, discovery.LOSS_DETAIL_BUDGET - 1 - 1)

    def test_the_budget_reaches_the_name_request(self):
        from fleetops.providers.esi import LookupBudget

        budget = LookupBudget(3)
        with (
            mock.patch.object(discovery.killmails.sde, "item_type_names", return_value={}),
            mock.patch.object(discovery.killmails.sde, "solar_system_names", return_value={}),
            mock.patch.object(discovery.killmails.names, "entity_names", return_value={}) as lookup,
        ):
            REAL_NAMES_OF([f.RIFTER], [30000142], budget=budget)

        lookup.assert_called_once_with([f.RIFTER, 30000142], budget=budget)


class LimitedScanTests(ScanBase):
    """L-8: after ESI asked for a pause the scan makes no lookups for what it already read."""

    def three_killmails(self):
        """Read newest first: a kill, then the loss, then another kill."""
        other = f.main_of(f.create_user("victim_of_scan"))
        first = self.esi.killmail(other, minutes_ago=10)
        loss = self.esi.killmail(self.main, minutes_ago=20)
        last = self.esi.killmail(other, minutes_ago=30)
        self.esi.listed(self.main, first, loss, last)
        return first, loss, last

    def fail_on_the_third_read(self, code, status):
        real = self.esi.detail
        calls = []

        def detail(killmail_id, killmail_hash):
            calls.append(killmail_id)
            if len(calls) == 3:
                raise FleetESIError(code, "Could not read", status)
            return real(killmail_id, killmail_hash)

        return detail

    def test_no_universe_request_is_made_after_a_limit_and_the_loss_is_read_again_later(self):
        first, loss, _last = self.three_killmails()

        with (
            mock.patch.object(discovery, "get_killmail", side_effect=self.fail_on_the_third_read("ESI_LIMITED", 420)),
            mock.patch.object(discovery.universe, "system_infos", return_value={}) as systems,
            mock.patch.object(discovery.universe, "type_groups", return_value={}) as groups,
        ):
            result = self.scan()

        self.assertEqual(result.ended, "limited")
        systems.assert_not_called()
        groups.assert_not_called()
        discovery.killmails.names_of.assert_not_called()
        # The kill needs no lookup and is stored; the loss would need one and is left for the next run.
        self.assertEqual(set(self.rows().values_list("killmail_id", "kind")), {(first, Kind.KILL)})

    def test_a_run_that_is_out_of_requests_makes_no_lookup_either(self):
        self.three_killmails()
        scan = SrpLossScan.objects.create(character_id=self.main.character_id, user=self.user)
        budget = discovery.ScanBudget()
        budget.details.remaining = 2

        with (
            mock.patch.object(discovery.universe, "system_infos", return_value={}) as systems,
            mock.patch.object(discovery.universe, "type_groups", return_value={}) as groups,
        ):
            with self.assertRaises(discovery.OutOfBudget):
                discovery.scan_character(scan, SrpSettings.get_solo(), self.now, budget)

        systems.assert_not_called()
        groups.assert_not_called()
        self.assertFalse(self.rows().filter(kind=Kind.LOSS).exists())

    def test_a_loss_read_with_the_last_detail_call_is_stored_only_with_its_names(self):
        # The list ends normally and nothing raises, but no call is left for the names: the loss is read again later.
        loss_id = self.esi.killmail(self.main, minutes_ago=30)
        self.esi.listed(self.main, loss_id)
        scan = SrpLossScan.objects.create(character_id=self.main.character_id, user=self.user)
        budget = discovery.ScanBudget()
        budget.details.remaining = 1

        with (
            mock.patch.object(discovery.universe, "system_infos", return_value={}) as systems,
            mock.patch.object(discovery.universe, "type_groups", return_value={}),
            mock.patch.object(discovery.killmails, "names_of", return_value=({}, {})),
        ):
            discovery.scan_character(scan, SrpSettings.get_solo(), self.now, budget)

        systems.assert_not_called()
        self.assertFalse(self.rows().filter(kind=Kind.LOSS).exists())

    def test_an_ordinary_error_still_stores_what_was_read_with_its_lookups(self):
        self.three_killmails()

        with (
            mock.patch.object(discovery, "get_killmail", side_effect=self.fail_on_the_third_read("ESI_ERROR", 500)),
            mock.patch.object(discovery.universe, "system_infos", return_value={}) as systems,
            mock.patch.object(discovery.universe, "type_groups", return_value={}),
        ):
            self.scan()

        systems.assert_called_once()
        self.assertEqual(set(self.rows().values_list("kind", flat=True)), {Kind.KILL, Kind.LOSS})


class StoredKillmailTests(ScanBase):
    def test_a_stored_killmail_with_a_checked_location_is_used_without_a_call(self):
        km = f.srp_killmail(
            self.main,
            time=self.now - timedelta(minutes=20),
            ship_group_id=25,
            solar_system_security=Decimal("0.4388"),
            region_id=10000030,
            location_checked_at=self.now,
            war_id=5,
        )
        self.esi.listed(self.main, km.killmail_id)

        self.scan()

        row = self.rows().get()
        self.assertEqual(self.esi.detail_calls, [])
        self.assertEqual((row.kind, row.ship_group_id, row.solar_system_security, row.region_id, row.war_id), (Kind.LOSS, 25, Decimal("0.4388"), 10000030, 5))
        self.assertEqual((row.ship_type_name, row.solar_system_name), (km.ship_type_name, km.solar_system_name))

    def test_a_stored_killmail_without_a_checked_location_is_read_again(self):
        km = f.srp_killmail(self.main, time=self.now - timedelta(minutes=20))
        self.esi.payloads[km.killmail_id] = f.esi_killmail_payload(km.killmail_id, victim=self.main, time=km.killmail_time)
        self.esi.listed(self.main, km.killmail_id)

        self.scan()

        self.assertEqual(self.esi.detail_calls, [km.killmail_id])

    def test_an_imported_placeholder_is_neither_read_nor_copied(self):
        placeholder = f.srp_killmail(
            self.main,
            time=self.now - timedelta(days=3),
            origin="aasrp",
            solar_system_id=0,
            killmail_hash="",
            solar_system_name="",
        )
        awox = f.srp_killmail(f.main_of(f.create_user("awoxed")), time=self.now - timedelta(days=2), origin="aasrp", solar_system_id=0, killmail_hash="")
        self.esi.listed(self.main, placeholder.killmail_id, awox.killmail_id)

        self.scan()

        self.assertEqual(self.esi.detail_calls, [])
        mine = self.rows().get(killmail_id=placeholder.killmail_id)
        self.assertEqual((mine.kind, mine.solar_system_id, mine.ship_type_id, mine.ship_group_id), (Kind.LOSS, None, None, None))
        self.assertEqual(mine.killmail_time, placeholder.killmail_time)
        self.assertEqual(self.rows().get(killmail_id=awox.killmail_id).kind, Kind.KILL)

    def test_an_imported_placeholder_older_than_the_window_is_old(self):
        self.srp = f.srp_settings(claim_deadline_days=7)
        placeholder = f.srp_killmail(self.main, time=self.now - timedelta(days=30), origin="aasrp", solar_system_id=0, killmail_hash="")
        self.esi.listed(self.main, placeholder.killmail_id)

        self.scan()

        self.assertEqual(self.rows().get().kind, Kind.OLD)

    def test_an_old_estimate_of_a_placeholder_does_not_make_the_smaller_ids_old(self):
        # L-7: the time of a placeholder is only an estimate; "smaller ID = older" holds for real times only.
        f.srp_settings(claim_deadline_days=7)
        placeholder = f.srp_killmail(
            self.main, killmail_id=NEWEST + 5, time=self.now - timedelta(days=30), origin="aasrp", solar_system_id=0, killmail_hash=""
        )
        real = self.esi.killmail(self.main, minutes_ago=60 * 24)
        self.esi.listed(self.main, placeholder.killmail_id, real)

        self.scan()

        self.assertEqual(self.rows().get(killmail_id=placeholder.killmail_id).kind, Kind.OLD)
        self.assertEqual(self.rows().get(killmail_id=real).kind, Kind.LOSS)
        self.assertEqual(self.esi.detail_calls, [real])

    def test_the_scan_never_writes_a_killmail(self):
        from fleetops.models import SrpKillmail

        loss_id = self.esi.killmail(self.main, minutes_ago=30)
        self.esi.listed(self.main, loss_id)

        self.scan()

        self.assertFalse(SrpKillmail.objects.exists())


class ErrorTests(ScanBase):
    def scan_row(self):
        return SrpLossScan.objects.get(character_id=self.main.character_id)

    def test_a_character_without_a_token_is_tried_again_in_a_day(self):
        scan = SrpLossScan.objects.create(character_id=self.main.character_id, user=self.user)
        with mock.patch.object(
            discovery.esi_provider, "get_scoped_token", side_effect=FleetESIError("MISSING_SCOPE", "none")
        ):
            discovery.scan_character(scan, SrpSettings.get_solo(), self.now, discovery.ScanBudget())

        scan.refresh_from_db()
        self.assertEqual((scan.status, scan.next_check_at), (Status.NO_TOKEN, self.now + timedelta(hours=24)))

    def test_a_refusal_by_esi_is_tried_again_in_a_day(self):
        self.esi.list_error = FleetESIError("LOSS_DENIED", "denied", 403)

        self.scan()

        scan = self.scan_row()
        self.assertEqual((scan.status, scan.next_check_at), (Status.DENIED, self.now + timedelta(hours=24)))

    def test_a_failure_backs_off_with_a_doubling_delay_up_to_a_day(self):
        self.esi.list_error = FleetESIError("ESI_ERROR", "Could not read", 500)

        self.scan()
        first = self.scan_row()
        self.assertEqual((first.status, first.failures, first.next_check_at), (Status.ERROR, 1, self.now + timedelta(minutes=30)))
        self.scan(first.next_check_at)
        second = self.scan_row()
        self.assertEqual((second.failures, second.next_check_at), (2, first.next_check_at + timedelta(minutes=60)))
        for _ in range(12):
            self.scan(self.scan_row().next_check_at)
        self.assertEqual(self.scan_row().next_check_at - self.scan_row().last_checked_at, timedelta(hours=24))

    def test_a_success_clears_the_failures(self):
        self.esi.list_error = FleetESIError("ESI_ERROR", "Could not read", 500)
        self.scan()
        self.esi.list_error = None

        self.scan(self.scan_row().next_check_at)

        self.assertEqual((self.scan_row().status, self.scan_row().failures), (Status.OK, 0))

    def test_the_error_limit_ends_the_run_and_starts_the_backoff(self):
        self.esi.list_error = FleetESIError("ESI_LIMITED", "limit", 420)

        result = self.scan()

        self.assertEqual((result.ended, result.scanned), ("limited", 0))
        self.assertEqual(len(self.esi.list_calls), 1)
        self.assertTrue(cache.get(discovery.BACKOFF_KEY))
        scans = SrpLossScan.objects.filter(status=Status.THROTTLED)
        self.assertEqual(scans.count(), 1)
        self.assertIsNone(scans.get().last_checked_at)

    def test_a_limit_inside_a_killmail_read_ends_the_run_too(self):
        loss_id = self.esi.killmail(self.main, minutes_ago=30)
        self.esi.listed(self.main, loss_id)
        error = FleetESIError("ESI_ERROR", "Could not read", 420)
        self.esi.detail_error = error

        result = self.scan()

        self.assertEqual(result.ended, "limited")

    def test_nothing_is_queued_while_the_backoff_lasts(self):
        cache.set(discovery.BACKOFF_KEY, 1, 900)

        with mock.patch.object(fleet_tasks.scan_srp_losses, "delay") as delay:
            self.assertFalse(discovery.queue_loss_scan(self.now))

        delay.assert_not_called()
        self.assertEqual(fleet_tasks.scan_srp_losses(), "backoff")

    def test_an_unexpected_error_marks_the_character_failed_and_the_run_goes_on(self):
        with mock.patch.object(discovery, "_read_character", side_effect=[RuntimeError("boom secret"), True, True]):
            with self.assertLogs(level="ERROR") as logs:
                result = self.scan()

        self.assertEqual(result.scanned, 3)
        self.assertEqual(SrpLossScan.objects.filter(status=Status.ERROR).count(), 1)
        self.assertEqual(SrpLossScan.objects.filter(status=Status.OK).count(), 2)
        self.assertNotIn("boom secret", "\n".join(logs.output))
        self.assertIn("RuntimeError", "\n".join(logs.output))

    def test_a_token_that_cannot_be_refreshed_is_a_failure_not_a_missing_token(self):
        with mock.patch.object(
            discovery.esi_provider, "get_scoped_token", side_effect=FleetESIError("TOKEN_REFRESH_FAILED", "later")
        ):
            self.scan()

        self.assertEqual(SrpLossScan.objects.filter(status=Status.ERROR, failures=1).count(), 3)

    def test_the_rows_found_before_a_failure_are_kept(self):
        first = self.esi.killmail(self.main, minutes_ago=10)
        second = self.esi.killmail(self.main, minutes_ago=20)
        self.esi.listed(self.main, first, second)
        original = self.esi.detail

        def flaky(killmail_id, killmail_hash):
            if killmail_id == second:
                raise FleetESIError("ESI_ERROR", "Could not read", 500)
            return original(killmail_id, killmail_hash)

        with mock.patch.object(discovery, "get_killmail", side_effect=flaky):
            self.scan()

        self.assertEqual(list(self.rows().values_list("killmail_id", flat=True)), [first])
        self.assertEqual(self.scan_row().status, Status.ERROR)


class BudgetTests(ScanBase):
    def test_a_run_stops_when_its_time_is_used_up(self):
        clock = iter([0, 1, 10_000, 10_001, 10_002, 10_003])
        with mock.patch.object(discovery.time, "monotonic", side_effect=lambda: next(clock)):
            result = self.scan()

        self.assertEqual((result.ended, result.scanned), ("budget", 1))

    def test_a_run_stops_when_the_list_reads_are_used_up(self):
        with mock.patch.object(discovery, "LOSS_LIST_BUDGET", 2):
            result = self.scan()

        self.assertEqual((result.ended, result.scanned), ("budget", 2))
        self.assertEqual(len(self.esi.list_calls), 2)

    def test_a_run_stops_when_the_killmail_reads_are_used_up(self):
        ids = [self.esi.killmail(self.main, minutes_ago=number + 1) for number in range(10)]
        self.esi.listed(self.main, *ids)

        with mock.patch.object(discovery, "LOSS_DETAIL_BUDGET", 4):
            result = self.scan()

        self.assertEqual(result.ended, "budget")
        self.assertEqual(len(self.esi.detail_calls), 4)
        # Changed for L-8: the run is out of requests, so the four losses it read are not stored without their names and
        # place (no lookup is made); the next run reads them again.
        self.assertEqual(self.rows().count(), 0)
        # The character stays due: the next run goes on with the rest.
        self.assertIsNone(SrpLossScan.objects.get(character_id=self.main.character_id).last_checked_at)

    def test_one_run_reads_at_most_three_hundred_characters(self):
        self.assertEqual((discovery.LOSS_SCAN_MAX_CHARACTERS, discovery.LOSS_SCAN_TIME_BUDGET), (300, 240))
        self.assertEqual((discovery.LOSS_LIST_BUDGET, discovery.LOSS_DETAIL_BUDGET), (400, 600))

    def test_nine_hundred_characters_are_all_read_within_an_hour(self):
        # 900 characters of one account, an interval of 60 minutes, a run every five minutes (12 runs an hour).
        characters = {1_000_000 + number: self.user.pk for number in range(900)}
        reads = {}

        def list_page(token, character_id, page=1):
            reads.setdefault(character_id, []).append(self.clock)
            return RecentKillmails()

        with (
            mock.patch.object(discovery, "candidate_characters", return_value=characters),
            mock.patch.object(discovery, "get_recent_killmails", side_effect=list_page),
            mock.patch.object(discovery.esi_provider, "get_scoped_token", return_value=object()),
        ):
            for run in range(12):
                self.clock = self.now + timedelta(minutes=5 * run)
                discovery.run_scan(self.clock)

        self.assertEqual(len(reads), 900)
        self.assertEqual(set(map(len, reads.values())), {1})
        self.assertEqual(SrpLossScan.objects.filter(status=Status.OK).count(), 900)
        # Nothing is read again before its interval is over.
        self.assertLess(max(reads.values())[0], self.now + timedelta(minutes=60))


class TaskTests(ScanBase):
    def setUp(self):
        super().setUp()
        # The minute beat also queues the daily insurance read while SRP is on; it is not under test here and would
        # reach ESI.
        patcher = mock.patch.object(fleet_tasks, "_queue_srp_insurance")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_task_returns_disabled_while_the_feature_is_off(self):
        f.srp_settings(loss_discovery_enabled=False)
        self.assertEqual(fleet_tasks.scan_srp_losses(), "disabled")
        f.srp_settings(loss_discovery_enabled=True, enabled=False)
        self.assertEqual(fleet_tasks.scan_srp_losses(), "disabled")
        self.assertEqual(fleet_tasks.scan_srp_losses_for_user(self.user.pk), "disabled")
        self.assertEqual(self.esi.list_calls, [])

    def test_the_task_scans_and_releases_its_lock(self):
        result = fleet_tasks.scan_srp_losses()

        self.assertEqual(result, "done: 3 characters")
        self.assertIsNone(cache.get(discovery.SCAN_LOCK_KEY))

    def test_a_second_task_does_not_run_while_the_lock_is_held(self):
        cache.set(discovery.SCAN_LOCK_KEY, "1", 600)

        self.assertEqual(fleet_tasks.scan_srp_losses(), "already-running")
        self.assertEqual(self.esi.list_calls, [])
        cache.delete(discovery.SCAN_LOCK_KEY)

    def test_the_task_for_a_user_tries_again_while_the_general_scan_holds_the_lock(self):
        # L-9: the refresh of a member is not dropped when the general scan is running.
        from celery.exceptions import Retry

        cache.set(discovery.SCAN_LOCK_KEY, "1", 600)

        with mock.patch.object(fleet_tasks.scan_srp_losses_for_user, "retry", side_effect=Retry()) as retry:
            with self.assertRaises(Retry):
                fleet_tasks.scan_srp_losses_for_user(self.user.pk)

        retry.assert_called_once_with(countdown=fleet_tasks.LOSS_REFRESH_RETRY_SECONDS)
        self.assertEqual(self.esi.list_calls, [])
        cache.delete(discovery.SCAN_LOCK_KEY)

    def test_the_task_for_a_user_gives_up_after_the_last_retry(self):
        from celery.exceptions import MaxRetriesExceededError

        cache.set(discovery.SCAN_LOCK_KEY, "1", 600)

        with mock.patch.object(fleet_tasks.scan_srp_losses_for_user, "retry", side_effect=MaxRetriesExceededError()):
            self.assertEqual(fleet_tasks.scan_srp_losses_for_user(self.user.pk), "already-running")
        cache.delete(discovery.SCAN_LOCK_KEY)

    def test_the_general_scan_is_not_retried(self):
        cache.set(discovery.SCAN_LOCK_KEY, "1", 600)
        self.assertEqual(fleet_tasks.scan_srp_losses(), "already-running")
        cache.delete(discovery.SCAN_LOCK_KEY)

    def test_a_refreshed_character_is_read_before_the_overdue_ones(self):
        overdue = SrpLossScan.objects.create(
            character_id=self.alt_a.character_id, user=self.user, next_check_at=self.now - timedelta(days=2)
        )
        refreshed = SrpLossScan.objects.create(
            character_id=self.alt_b.character_id, user=self.user, next_check_at=self.now - timedelta(minutes=3)
        )

        discovery.mark_due([self.alt_b.character_id], self.now)

        order = list(
            SrpLossScan.objects.filter(pk__in=[overdue.pk, refreshed.pk])
            .filter(next_check_at__isnull=True)
            .values_list("pk", flat=True)
        )
        self.assertEqual(order, [refreshed.pk])
        self.scan()
        self.assertEqual(self.esi.list_calls[0][0], self.alt_b.character_id)

    def test_the_task_for_a_user_reads_that_users_characters(self):
        self.assertEqual(fleet_tasks.scan_srp_losses_for_user(self.user.pk), "done: 3 characters")

    def test_a_crash_is_reported_without_its_message(self):
        with mock.patch.object(discovery, "run_scan", side_effect=RuntimeError("secret detail")):
            with self.assertLogs(level="ERROR") as logs:
                self.assertEqual(fleet_tasks.scan_srp_losses(), "error")

        self.assertNotIn("secret detail", "\n".join(logs.output))
        self.assertIsNone(cache.get(discovery.SCAN_LOCK_KEY))

    def test_the_minute_beat_queues_the_scan_every_five_minutes_at_most(self):
        with mock.patch.object(fleet_tasks.scan_srp_losses, "delay") as delay:
            fleet_tasks.schedule_active_fleet_tracking()
            fleet_tasks.schedule_active_fleet_tracking()

        delay.assert_called_once_with()

    def test_the_beat_queues_nothing_while_the_feature_is_off(self):
        f.srp_settings(loss_discovery_enabled=False)

        with mock.patch.object(fleet_tasks.scan_srp_losses, "delay") as delay:
            fleet_tasks.schedule_active_fleet_tracking()

        delay.assert_not_called()

    def test_a_broker_error_does_not_stop_the_beat_and_frees_the_slot(self):
        with mock.patch.object(fleet_tasks.scan_srp_losses, "delay", side_effect=ConnectionError("broker down")):
            fleet_tasks.schedule_active_fleet_tracking()

        self.assertIsNone(cache.get(discovery.QUEUED_KEY))

    def test_no_beat_entry_is_created_by_the_code(self):
        import inspect

        for module in (discovery, fleet_tasks):
            self.assertNotIn("PeriodicTask", inspect.getsource(module))


# --- INT-5: the fleet a loss belongs to ----------------------------------------------------------------------------


class FindLossMatchTests(PresenceBase):
    def link_for(self, **fleet):
        operation = self.fleet(**fleet)
        return operation, f.fleet_link(operation)

    def test_a_pilot_in_the_fleet_matches_it(self):
        operation, link = self.link_for(ended=at(60))
        tracked(operation, self.character, TrackEvent.JOIN, at(-20))
        f.member_state(operation, self.character, active=False, first_seen=at(-20), left_at=at(40))

        self.assertEqual(presence.find_loss_match(self.character.character_id, at(0)), (link, display.IN_FLEET))

    def test_a_pilot_just_outside_matches_as_near(self):
        operation, link = self.link_for(ended=at(60))
        tracked(operation, self.character, TrackEvent.JOIN, at(-20))
        f.member_state(operation, self.character, active=True, first_seen=at(-20))

        self.assertEqual(presence.find_loss_match(self.character.character_id, at(60, 60)), (link, display.NEAR))

    def test_in_fleet_beats_near(self):
        near_op, near_link = self.link_for(started=at(-10), ended=at(10))
        tracked(near_op, self.character, TrackEvent.JOIN, at(-10))
        tracked(near_op, self.character, TrackEvent.LEAVE, at(-1))
        f.member_state(near_op, self.character, active=False, first_seen=at(-10), left_at=at(-1))
        in_op, in_link = self.link_for(started=at(-60), ended=at(30))
        tracked(in_op, self.character, TrackEvent.JOIN, at(-60))
        f.member_state(in_op, self.character, active=True, first_seen=at(-60))

        self.assertEqual(presence.find_loss_match(self.character.character_id, at(0)), (in_link, display.IN_FLEET))

    def test_a_registered_pilot_of_a_manual_fleet_matches_as_registered(self):
        operation, link = self.link_for(ended=at(60), is_manual=True, esi_fleet_id=None)
        f.registration(operation, self.character)

        self.assertEqual(presence.find_loss_match(self.character.character_id, at(0)), (link, display.REGISTERED))

    def test_granted_attendance_of_a_link_only_fleet_matches_too(self):
        operation, link = self.link_for(ended=at(60), link_only=True, esi_fleet_id=None)
        f.add_attendance(operation, self.pilot, self.character)

        self.assertEqual(presence.find_loss_match(self.character.character_id, at(0)), (link, display.REGISTERED))

    def test_the_fleet_of_the_loss_is_found_among_more_than_three_open_registered_fleets(self):
        # L-6: the time window is part of the query, so newer fleets that do not contain the loss do not push the
        # right one out of the three candidates.
        oldest, oldest_link = self.link_for(started=at(-120), ended=at(60), is_manual=True, esi_fleet_id=None)
        f.registration(oldest, self.character)
        for number in range(4):
            later, _link = self.link_for(started=at(600 + number * 10), ended=at(900), is_manual=True, esi_fleet_id=None)
            f.registration(later, self.character)

        self.assertEqual(presence.find_loss_match(self.character.character_id, at(0)), (oldest_link, display.REGISTERED))

    def test_a_registration_outside_the_time_of_the_fleet_does_not_match(self):
        operation, _link = self.link_for(ended=at(60), is_manual=True, esi_fleet_id=None)
        f.registration(operation, self.character)

        self.assertEqual(presence.find_loss_match(self.character.character_id, at(300)), (None, ""))

    def test_a_closed_link_or_another_pilot_does_not_match(self):
        operation, link = self.link_for(ended=at(60), is_manual=True, esi_fleet_id=None)
        f.registration(operation, self.character)
        link.is_open = False
        link.save()
        self.assertEqual(presence.find_loss_match(self.character.character_id, at(0)), (None, ""))
        link.is_open = True
        link.save()
        other = f.main_of(f.create_user("someone-else"))
        self.assertEqual(presence.find_loss_match(other.character_id, at(0)), (None, ""))

    def test_a_standing_link_is_never_a_match(self):
        f.standing_link()
        self.assertEqual(presence.find_loss_match(self.character.character_id, at(0)), (None, ""))

    def test_the_old_function_still_returns_a_link_only_for_a_tracked_fleet(self):
        tracked_op, tracked_link = self.link_for(ended=at(60))
        tracked(tracked_op, self.character, TrackEvent.JOIN, at(-20))
        f.member_state(tracked_op, self.character, active=False, first_seen=at(-20), left_at=at(40))
        self.assertEqual(presence.find_fleet_for_loss(self.character.character_id, at(0)), tracked_link)

    def test_the_old_function_does_not_search_the_registrations(self):
        with mock.patch.object(presence, "_find_registered") as registered:
            self.assertIsNone(presence.find_fleet_for_loss(self.character.character_id, at(0)))
        registered.assert_not_called()
        with mock.patch.object(presence, "_find_registered", return_value=(None, "")) as registered:
            presence.find_loss_match(self.character.character_id, at(0))
        registered.assert_called_once()

    def test_a_registered_fleet_is_not_returned_by_the_old_function(self):
        operation, _link = self.link_for(ended=at(60), is_manual=True, esi_fleet_id=None)
        f.registration(operation, self.character)

        self.assertIsNone(presence.find_fleet_for_loss(self.character.character_id, at(0)))


class ScanMatchTests(ScanBase):
    def test_a_loss_in_a_tracked_fleet_is_matched_by_the_scan(self):
        fc = f.create_user("scan-fc", perms=f.FC_PERMS)
        operation = f.create_operation(fc, started_at=self.now - timedelta(hours=2))
        link = f.fleet_link(operation)
        f.member_state(operation, self.main, first_seen=operation.started_at)
        from fleetops.models import FleetMemberEvent

        event = FleetMemberEvent.objects.create(
            operation=operation,
            character_id=self.main.character_id,
            character_name=self.main.character_name,
            event_type=FleetMemberEvent.EventType.JOIN,
        )
        FleetMemberEvent.objects.filter(pk=event.pk).update(created_at=operation.started_at)
        loss_id = self.esi.killmail(self.main, minutes_ago=30)
        self.esi.listed(self.main, loss_id)

        self.scan()

        row = self.rows().get()
        self.assertEqual((row.matched_link, row.match_code), (link, "in_fleet"))

    def test_a_loss_without_a_fleet_has_no_match(self):
        loss_id = self.esi.killmail(self.main, minutes_ago=30)
        self.esi.listed(self.main, loss_id)

        self.scan()

        row = self.rows().get()
        self.assertEqual((row.matched_link, row.match_code), (None, ""))

    def test_a_link_that_closed_counts_as_no_match(self):
        fc = f.create_user("scan-fc2", perms=f.FC_PERMS)
        operation = f.create_operation(fc)
        link = f.fleet_link(operation)
        row = SrpLoss.objects.create(
            character_id=self.main.character_id, killmail_id=5, kind=Kind.LOSS, matched_link=link, match_code="in_fleet"
        )
        self.assertEqual(discovery.matched_open_link(row), link)

        link.is_open = False
        link.save()
        row = SrpLoss.objects.select_related("matched_link").get(pk=row.pk)

        self.assertIsNone(discovery.matched_open_link(row))


# --- The cleanup ---------------------------------------------------------------------------------------------------


class PruneTests(ScanBase):
    def row(self, killmail_id, kind, *, days_ago=None, discovered_days_ago=0):
        row = SrpLoss.objects.create(
            character_id=self.main.character_id,
            killmail_id=killmail_id,
            kind=kind,
            killmail_time=None if days_ago is None else timezone.now() - timedelta(days=days_ago),
        )
        SrpLoss.objects.filter(pk=row.pk).update(discovered_at=timezone.now() - timedelta(days=discovered_days_ago))
        return row

    def test_kills_and_old_rows_go_a_day_after_the_window(self):
        f.srp_settings(claim_deadline_days=7)
        keep = [self.row(1, Kind.KILL, days_ago=7), self.row(2, Kind.OLD, days_ago=7.5), self.row(3, Kind.OLD, discovered_days_ago=7)]
        gone = [self.row(4, Kind.KILL, days_ago=9), self.row(5, Kind.OLD, days_ago=20), self.row(6, Kind.OLD, discovered_days_ago=9)]

        discovery.prune_losses()

        self.assertEqual(set(SrpLoss.objects.values_list("pk", flat=True)), {row.pk for row in keep})
        self.assertFalse(SrpLoss.objects.filter(pk__in=[row.pk for row in gone]).exists())

    def test_losses_stay_thirty_days_longer(self):
        f.srp_settings(claim_deadline_days=7)
        recent = self.row(1, Kind.LOSS, days_ago=30)
        stale = self.row(2, Kind.LOSS, days_ago=38)

        discovery.prune_losses()

        self.assertTrue(SrpLoss.objects.filter(pk=recent.pk).exists())
        self.assertFalse(SrpLoss.objects.filter(pk=stale.pk).exists())

    def test_scan_rows_of_characters_on_no_account_go(self):
        SrpLossScan.objects.create(character_id=self.main.character_id, user=self.user)
        SrpLossScan.objects.create(character_id=42, user=None)

        discovery.prune_losses()

        self.assertEqual(list(SrpLossScan.objects.values_list("character_id", flat=True)), [self.main.character_id])

    def test_the_daily_cleanup_task_calls_it(self):
        with mock.patch.object(discovery, "prune_losses") as prune:
            fleet_tasks.prune_attendance_history()

        prune.assert_called_once_with()

    def test_a_failure_of_the_cleanup_does_not_stop_the_task(self):
        with mock.patch.object(discovery, "prune_losses", side_effect=RuntimeError("x")):
            with self.assertLogs(level="ERROR"):
                fleet_tasks.prune_attendance_history()
