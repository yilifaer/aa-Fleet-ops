# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Tests for the statistics population and the membership filter."""

from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone as dt_timezone
from unittest import mock

from allianceauth.authentication.models import UserProfile
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from fleetops.models import AttendanceRecord, FleetOperation, FleetOpsSettings
from fleetops.services import population
from fleetops.services.history import (
    apply_current_membership_filter,
    current_member_profiles,
    current_member_user_ids,
    prune_history,
)
from fleetops.services.identity import corporation_main_count as identity_corporation_main_count

from . import factories as f

UTC = dt_timezone.utc
Status = FleetOperation.Status


def set_alliances(*alliances):
    f.settings(history_alliance_ids=",".join(str(alliance[0]) for alliance in alliances))


def user_ids(identities):
    return [identity.user_id for identity in identities]


class MemberProfilesTests(TestCase):
    def setUp(self):
        self.alice = f.member("alice")
        self.bob = f.member("bob")
        self.gone = f.member("disabled", active=False)
        self.biomassed = f.member("biomassed", corporation=f.DOOMHEIM_CORP, alliance=None)
        self.no_main = f.member("nomain", with_main=False)
        self.other_corp = f.member("carol", corporation=f.OTHER_CORP)
        self.foreign = f.member("dave", corporation=f.THIRD_CORP, alliance=f.FOREIGN_ALLIANCE)
        self.no_alliance = f.member("erin", corporation=f.THIRD_CORP, alliance=None)

    def pks(self, **kwargs):
        return set(population.member_profiles(**kwargs).values_list("user_id", flat=True))

    def test_without_alliance_filter_every_active_main_counts(self):
        self.assertEqual(
            self.pks(),
            {self.alice.pk, self.bob.pk, self.other_corp.pk, self.foreign.pk, self.no_alliance.pk},
        )

    def test_disabled_accounts_are_not_members(self):
        self.assertNotIn(self.gone.pk, self.pks())

    def test_biomassed_mains_are_not_members(self):
        self.assertNotIn(self.biomassed.pk, self.pks())

    def test_users_without_a_main_are_not_members(self):
        self.assertNotIn(self.no_main.pk, self.pks())

    def test_alliance_filter_keeps_only_mains_in_the_configured_alliances(self):
        set_alliances(f.DEFAULT_ALLIANCE)
        self.assertEqual(self.pks(), {self.alice.pk, self.bob.pk, self.other_corp.pk})

    def test_several_alliances(self):
        set_alliances(f.DEFAULT_ALLIANCE, f.FOREIGN_ALLIANCE)
        self.assertEqual(self.pks(), {self.alice.pk, self.bob.pk, self.other_corp.pk, self.foreign.pk})

    def test_users_without_alliance_are_outside_a_configured_filter(self):
        set_alliances(f.DEFAULT_ALLIANCE)
        self.assertNotIn(self.no_alliance.pk, self.pks())

    def test_corporation_filter(self):
        self.assertEqual(self.pks(corporation_id=f.DEFAULT_CORP[0]), {self.alice.pk, self.bob.pk})
        self.assertEqual(self.pks(corporation_id=f.OTHER_CORP[0]), {self.other_corp.pk})
        self.assertEqual(self.pks(corporation_id=9_999_999), set())

    def test_corporation_and_alliance_filter_combine(self):
        set_alliances(f.DEFAULT_ALLIANCE)
        self.assertEqual(self.pks(corporation_id=f.THIRD_CORP[0]), set())
        set_alliances(f.FOREIGN_ALLIANCE)
        self.assertEqual(self.pks(corporation_id=f.THIRD_CORP[0]), {self.foreign.pk})

    def test_the_doomheim_corporation_cannot_be_asked_for(self):
        self.assertEqual(self.pks(corporation_id=f.DOOMHEIM_CORP[0]), set())

    def test_explicit_settings_object(self):
        configured = FleetOpsSettings(history_alliance_ids=str(f.FOREIGN_ALLIANCE[0]))
        self.assertEqual(self.pks(settings_obj=configured), {self.foreign.pk})

    def test_an_alt_in_the_alliance_does_not_make_a_member(self):
        outsider = f.member("frank", corporation=f.THIRD_CORP, alliance=f.FOREIGN_ALLIANCE)
        f.add_alt(outsider, "Frank Alt")
        set_alliances(f.DEFAULT_ALLIANCE)
        self.assertNotIn(outsider.pk, self.pks())

    def test_doomheim_constant(self):
        self.assertEqual(population.DOOMHEIM_CORPORATION_ID, 1_000_001)
        self.assertEqual(population.DOOMHEIM_CORPORATION_ID, f.DOOMHEIM_CORP[0])


class MembersTests(TestCase):
    def setUp(self):
        self.zed = f.member("zed", main_name="Zed Pilot", joined=datetime(2025, 1, 2, 3, 4, tzinfo=UTC))
        self.amy = f.member("amy", main_name="Amy Pilot", corporation=f.OTHER_CORP)
        f.add_alt(self.amy, "Amy Alt")
        f.member("disabled", active=False)

    def test_identity_fields(self):
        identities = {identity.user_id: identity for identity in population.members()}
        zed = identities[self.zed.pk]
        main = f.main_of(self.zed)
        self.assertEqual(zed.main_character_id, main.character_id)
        self.assertEqual(zed.main_character_name, "Zed Pilot")
        self.assertEqual(zed.corporation_id, f.DEFAULT_CORP[0])
        self.assertEqual(zed.corporation_name, f.DEFAULT_CORP[1])
        self.assertEqual(zed.corporation_ticker, main.corporation_ticker)
        self.assertEqual(zed.alliance_id, f.DEFAULT_ALLIANCE[0])
        self.assertEqual(zed.alliance_name, f.DEFAULT_ALLIANCE[1])
        self.assertEqual(zed.registered_at, datetime(2025, 1, 2, 3, 4, tzinfo=UTC))

    def test_alts_are_not_listed(self):
        names = [identity.main_character_name for identity in population.members()]
        self.assertEqual(names, ["Amy Pilot", "Zed Pilot"])

    def test_sorted_by_main_name(self):
        f.member("bea", main_name="Bea Pilot")
        names = [identity.main_character_name for identity in population.members()]
        self.assertEqual(names, sorted(names))

    def test_corporation_scope(self):
        identities = population.members(corporation_id=f.OTHER_CORP[0])
        self.assertEqual(user_ids(identities), [self.amy.pk])

    def test_no_alliance(self):
        solo = f.member("solo", alliance=None)
        identity = next(item for item in population.members() if item.user_id == solo.pk)
        self.assertIsNone(identity.alliance_id)
        self.assertEqual(identity.alliance_name, "")

    def test_one_query(self):
        f.member("bea")
        f.member("cal")
        settings_obj = FleetOpsSettings.get_solo()
        with CaptureQueriesContext(connection) as queries:
            result = population.members(settings_obj=settings_obj)
        self.assertEqual(len(result), 4)
        self.assertEqual(len(queries), 1)

    def test_identities_are_immutable(self):
        identity = population.members()[0]
        # A real field: a frozen dataclass with slots answers an unknown name differently on Python 3.10 to 3.12.
        with self.assertRaises(FrozenInstanceError):
            identity.user_id = 0


class IdentityOfTests(TestCase):
    def test_member(self):
        user = f.member("alice", main_name="Alice Pilot")
        identity = population.identity_of(user)
        self.assertEqual(identity.user_id, user.pk)
        self.assertEqual(identity.main_character_name, "Alice Pilot")
        self.assertEqual(identity.corporation_id, f.DEFAULT_CORP[0])

    def test_user_id_is_accepted(self):
        user = f.member("alice")
        self.assertEqual(population.identity_of(user.pk).user_id, user.pk)

    def test_not_in_scope(self):
        self.assertIsNone(population.identity_of(f.member("gone", active=False)))
        self.assertIsNone(population.identity_of(f.member("nomain", with_main=False)))
        self.assertIsNone(population.identity_of(f.member("biomassed", corporation=f.DOOMHEIM_CORP)))
        self.assertIsNone(population.identity_of(999_999))

    def test_outside_the_member_alliances(self):
        foreign = f.member("dave", alliance=f.FOREIGN_ALLIANCE)
        self.assertIsNotNone(population.identity_of(foreign))
        set_alliances(f.DEFAULT_ALLIANCE)
        self.assertIsNone(population.identity_of(foreign))

    def test_the_main_decides_not_the_alts(self):
        user = f.member("alice", corporation=f.OTHER_CORP)
        f.add_alt(user, corporation=f.DEFAULT_CORP)
        self.assertEqual(population.identity_of(user).corporation_id, f.OTHER_CORP[0])


class SubqueryTests(TestCase):
    def test_member_user_ids_subquery(self):
        alice = f.member("alice")
        bob = f.member("bob", corporation=f.OTHER_CORP)
        f.member("gone", active=False)
        subquery = population.member_user_ids_subquery()
        self.assertNotIsInstance(subquery, (list, set))
        self.assertEqual(set(subquery.values_list("user_id", flat=True)), {alice.pk, bob.pk})
        self.assertEqual(
            set(population.member_user_ids_subquery(corporation_id=f.OTHER_CORP[0]).values_list("user_id", flat=True)),
            {bob.pk},
        )

    def test_used_inside_a_filter(self):
        operation = f.closed_operation(f.create_user(perms=f.FC_PERMS))
        alice = f.member("alice")
        gone = f.member("gone", active=False)
        kept = f.add_attendance(operation, alice)
        f.add_attendance(operation, gone)
        rows = AttendanceRecord.objects.filter(auth_user_id__in=population.member_user_ids_subquery())
        self.assertEqual(list(rows), [kept])


class ScopeAlliancesTests(TestCase):
    def test_not_configured(self):
        f.member("alice")
        self.assertEqual(population.scope_alliances(), (False, []))

    def test_names_come_from_the_members(self):
        f.member("alice")
        f.member("dave", alliance=f.FOREIGN_ALLIANCE)
        set_alliances(f.FOREIGN_ALLIANCE, f.DEFAULT_ALLIANCE)
        configured, alliances = population.scope_alliances()
        self.assertTrue(configured)
        self.assertEqual(alliances, [(f.FOREIGN_ALLIANCE[0], "Foreign Alliance"), (f.DEFAULT_ALLIANCE[0], "Test Alliance")])

    def test_an_alliance_without_members_is_named_by_its_id(self):
        f.member("alice")
        f.settings(history_alliance_ids=f"{f.DEFAULT_ALLIANCE[0]}, 3999999")
        configured, alliances = population.scope_alliances()
        self.assertTrue(configured)
        self.assertEqual(alliances, [(3999999, "Alliance 3999999"), (f.DEFAULT_ALLIANCE[0], "Test Alliance")])


class CorporationMainCountTests(TestCase):
    def test_counts_mains_of_the_corporation(self):
        user = f.member("alice")
        f.add_alt(user)
        f.add_alt(user)
        f.member("bob")
        other = f.member("carol", corporation=f.OTHER_CORP)
        f.add_alt(other, corporation=f.DEFAULT_CORP)
        self.assertEqual(population.corporation_main_count(f.DEFAULT_CORP[0]), 2)
        self.assertEqual(population.corporation_main_count(f.OTHER_CORP[0]), 1)
        self.assertEqual(population.corporation_main_count(9_999_999), 0)

    def test_disabled_accounts_and_biomassed_mains_are_not_counted(self):
        f.member("alice")
        f.member("gone", active=False)
        f.member("biomassed", corporation=f.DOOMHEIM_CORP)
        self.assertEqual(population.corporation_main_count(f.DEFAULT_CORP[0]), 1)
        self.assertEqual(population.corporation_main_count(f.DOOMHEIM_CORP[0]), 0)

    def test_the_alliance_filter_applies(self):
        f.member("alice")
        f.member("dave", alliance=f.FOREIGN_ALLIANCE)
        self.assertEqual(population.corporation_main_count(f.DEFAULT_CORP[0]), 2)
        set_alliances(f.DEFAULT_ALLIANCE)
        self.assertEqual(population.corporation_main_count(f.DEFAULT_CORP[0]), 1)

    def test_identity_wrapper(self):
        f.member("alice")
        f.member("gone", active=False)
        self.assertEqual(identity_corporation_main_count(f.DEFAULT_CORP[0]), 1)


class MembershipFilterSubqueryTests(TestCase):
    def setUp(self):
        self.fc = f.create_user(perms=f.FC_PERMS)
        self.operation = f.closed_operation(self.fc)

    def legacy_filter(self, qs, alliance_ids):
        """The filter as it worked with a Python list of user IDs."""
        user_ids = set(
            UserProfile.objects.filter(main_character__alliance_id__in=alliance_ids)
            .exclude(main_character=None)
            .values_list("user_id", flat=True)
        )
        from django.db.models import Q

        return qs.filter(Q(auth_user_id__in=user_ids) | Q(auth_user__isnull=True))

    def test_same_rows_as_the_id_list_version(self):
        inside = f.member("inside")
        outside = f.member("outside", corporation=f.OTHER_CORP, alliance=f.FOREIGN_ALLIANCE)
        no_main = f.member("nomain", with_main=False)
        f.add_attendance(self.operation, inside)
        f.add_attendance(self.operation, outside)
        stranger = f.create_character()
        AttendanceRecord.objects.create(
            operation=self.operation,
            character_id=stranger.character_id,
            character_name=stranger.character_name,
            source=AttendanceRecord.Source.MANUAL,
        )
        AttendanceRecord.objects.create(
            operation=self.operation,
            character_id=f.next_id(),
            character_name="No main",
            auth_user=no_main,
            source=AttendanceRecord.Source.AUTOMATIC,
        )
        set_alliances(f.DEFAULT_ALLIANCE)
        expected = set(self.legacy_filter(AttendanceRecord.objects.all(), {f.DEFAULT_ALLIANCE[0]}).values_list("pk", flat=True))
        actual = set(apply_current_membership_filter(AttendanceRecord.objects.all()).values_list("pk", flat=True))
        self.assertEqual(actual, expected)
        self.assertEqual(len(actual), 2)

    def test_no_configuration_returns_the_queryset_unchanged(self):
        qs = AttendanceRecord.objects.all()
        self.assertIs(apply_current_membership_filter(qs), qs)
        self.assertIsNone(current_member_profiles())
        self.assertIsNone(current_member_user_ids())

    def test_the_filter_is_a_subquery(self):
        set_alliances(f.DEFAULT_ALLIANCE)
        qs = apply_current_membership_filter(AttendanceRecord.objects.all())
        sql, params = qs.query.sql_with_params()
        self.assertEqual(sql.lower().count("select"), 2)
        self.assertIn("userprofile", sql.lower())
        # Only the configured alliance is a parameter, never a list of users.
        self.assertEqual(list(params), [f.DEFAULT_ALLIANCE[0]])

    def test_parameter_count_does_not_grow_with_the_number_of_members(self):
        set_alliances(f.DEFAULT_ALLIANCE)
        for index in range(5):
            f.member(f"small{index}")
        small = len(apply_current_membership_filter(AttendanceRecord.objects.all()).query.sql_with_params()[1])
        for index in range(45):
            f.member(f"large{index}")
        large = len(apply_current_membership_filter(AttendanceRecord.objects.all()).query.sql_with_params()[1])
        self.assertEqual(small, large)
        self.assertGreaterEqual(
            UserProfile.objects.filter(main_character__alliance_id=f.DEFAULT_ALLIANCE[0]).count(), 50
        )

    def test_one_query_whatever_the_number_of_members(self):
        set_alliances(f.DEFAULT_ALLIANCE)
        for index in range(30):
            f.add_attendance(self.operation, f.member(f"pilot{index}"))
        with CaptureQueriesContext(connection) as queries:
            rows = list(apply_current_membership_filter(AttendanceRecord.objects.all()))
        self.assertEqual(len(rows), 30)
        self.assertEqual(len(queries), 2)  # the settings read and the rows

    def test_departed_members_are_hidden(self):
        member = f.member("member")
        leaver = f.member("leaver", corporation=f.OTHER_CORP, alliance=f.FOREIGN_ALLIANCE)
        kept = f.add_attendance(self.operation, member)
        f.add_attendance(self.operation, leaver)
        set_alliances(f.DEFAULT_ALLIANCE)
        self.assertEqual(list(apply_current_membership_filter(AttendanceRecord.objects.all())), [kept])


class PruneHistorySubqueryTests(TestCase):
    def setUp(self):
        self.now = timezone.now()
        self.fc = f.create_user(perms=f.FC_PERMS)
        self.operation = f.closed_operation(self.fc, self.now - timedelta(days=10))
        self.member = f.member("member")
        self.leaver = f.member("leaver", corporation=f.OTHER_CORP, alliance=f.FOREIGN_ALLIANCE)
        self.kept = f.add_attendance(self.operation, self.member)
        self.left = f.add_attendance(self.operation, self.leaver)
        AttendanceRecord.objects.create(
            operation=self.operation,
            character_id=f.next_id(),
            character_name="Unmapped",
            source=AttendanceRecord.Source.MANUAL,
        )

    def test_nothing_is_removed_without_member_alliances(self):
        self.assertEqual(prune_history(), {"old_attendance": 0, "left_alliance": 0, "old_events": 0})
        self.assertEqual(AttendanceRecord.objects.count(), 3)

    def test_dry_run_counts_departed_members_only(self):
        set_alliances(f.DEFAULT_ALLIANCE)
        result = prune_history(dry_run=True)
        self.assertEqual(result, {"old_attendance": 0, "left_alliance": 1, "old_events": 0})
        self.assertEqual(AttendanceRecord.objects.count(), 3)

    def test_departed_members_history_is_deleted(self):
        set_alliances(f.DEFAULT_ALLIANCE)
        result = prune_history()
        self.assertEqual(result["left_alliance"], 1)
        self.assertEqual(AttendanceRecord.objects.filter(pk=self.left.pk).count(), 0)
        self.assertEqual(AttendanceRecord.objects.filter(pk=self.kept.pk).count(), 1)
        self.assertEqual(AttendanceRecord.objects.count(), 2)

    def test_expired_rows_are_not_counted_twice(self):
        old = f.closed_operation(self.fc, self.now - timedelta(days=500))
        f.add_attendance(old, self.leaver)
        f.add_attendance(old, self.member)
        set_alliances(f.DEFAULT_ALLIANCE)
        result = prune_history(dry_run=True)
        self.assertEqual(result, {"old_attendance": 2, "left_alliance": 1, "old_events": 0})

    def test_departed_members_are_removed_with_a_large_population(self):
        set_alliances(f.DEFAULT_ALLIANCE)
        for index in range(40):
            f.add_attendance(self.operation, f.member(f"pilot{index}"))
        for index in range(15):
            f.add_attendance(
                self.operation, f.member(f"gone{index}", corporation=f.OTHER_CORP, alliance=f.FOREIGN_ALLIANCE)
            )
        result = prune_history()
        self.assertEqual(result["left_alliance"], 16)
        self.assertEqual(AttendanceRecord.objects.count(), 42)

    def test_membership_is_read_in_the_database(self):
        set_alliances(f.DEFAULT_ALLIANCE)
        with mock.patch(
            "fleetops.services.history.current_member_user_ids", side_effect=AssertionError("id list built")
        ):
            self.assertEqual(prune_history(dry_run=True)["left_alliance"], 1)
