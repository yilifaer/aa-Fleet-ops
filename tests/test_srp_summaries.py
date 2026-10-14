# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""SRP summaries: personal, corporation, FC and alliance by EVE month of the loss."""

from datetime import datetime, timezone as dt_timezone

from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from fleetops.models import SrpClaim, SrpCorporationLimit, SrpFleetLink, SrpSettings
from fleetops.srp import summaries
from fleetops.srp.types import LIMIT_CORPORATION, LIMIT_PERSONAL

from . import factories as f

UTC = dt_timezone.utc
M = 1_000_000
RIFTER, MUNINN, GUARDIAN = 587, 12015, 11987
Status = SrpClaim.Status


def at(day, hour=12, minute=0, *, month=3, year=2026):
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


class SummaryData(TestCase):
    """One March 2026 with every status, both scopes, two corporations and two fleets."""

    @classmethod
    def setUpTestData(cls):
        cls.settings_obj = f.srp_settings(
            enabled=True, personal_monthly_cap=150 * M, corporation_monthly_cap=500 * M
        )
        cls.alice = f.create_user("sum_alice", perms=f.SRP_MEMBER_PERMS, main_name="Alice Main")
        cls.bob = f.create_user("sum_bob", perms=f.SRP_MEMBER_PERMS, main_name="Bob Main")
        cls.cara = f.create_user("sum_cara", perms=f.SRP_MEMBER_PERMS, main_name="Cara Main", corporation=f.OTHER_CORP)
        cls.fc1 = f.create_user("sum_fc1", perms=[*f.FC_PERMS, "fleetops.request_srp"], main_name="First FC")
        cls.fc2 = f.create_user("sum_fc2", perms=[*f.FC_PERMS, "fleetops.request_srp"], main_name="Second FC")
        started = at(10, 18)
        cls.fleet1 = f.create_operation(cls.fc1, status="closed", started_at=started)
        cls.fleet2 = f.create_operation(cls.fc2, status="closed", started_at=started)
        cls.link1 = f.fleet_link(cls.fleet1)
        cls.link2 = f.fleet_link(cls.fleet2)
        SrpCorporationLimit.objects.create(
            corporation_id=f.OTHER_CORP[0], corporation_name=f.OTHER_CORP[1], monthly_cap=100 * M
        )

        cls.a1 = cls.claim(cls.alice, Status.PAID, 100 * M, 90 * M, link=cls.link1, ship=MUNINN, when=at(10, 19))
        cls.a2 = cls.claim(cls.alice, Status.APPROVED, 50 * M, 50 * M, ship=RIFTER, when=at(12))
        cls.a3 = cls.claim(cls.alice, Status.PENDING, 30 * M, None, limited_amount=20 * M, ship=RIFTER, when=at(13))
        cls.a4 = cls.claim(cls.alice, Status.REJECTED, 10 * M, None, ship=GUARDIAN, when=at(14))
        cls.a5 = cls.claim(cls.alice, Status.WITHDRAWN, 999 * M, None, ship=RIFTER, when=at(15))
        cls.b1 = cls.claim(cls.bob, Status.APPROVED, 40 * M, 40 * M, link=cls.link1, ship=MUNINN, when=at(10, 20))
        cls.b2 = cls.claim(cls.bob, Status.PAID, 25 * M, 25 * M, link=cls.link2, ship=RIFTER, when=at(11, 20))
        cls.c1 = cls.claim(cls.cara, Status.PAID, 60 * M, 60 * M, ship=GUARDIAN, when=at(16))
        cls.c2 = cls.claim(
            cls.cara, Status.PENDING, 5 * M, None, link=cls.link2, ship=RIFTER, when=at(11, 21), counts_corporation=False
        )

    @classmethod
    def claim(cls, user, status, amount, approved, *, when, ship=RIFTER, link=None, **extra):
        character = f.main_of(user)
        killmail = f.srp_killmail(character, ship_type_id=ship, ship_name=f"Ship {ship}", time=when)
        extra.setdefault("paid_at", at(20) if status == Status.PAID else None)
        return f.srp_claim(
            user, killmail=killmail, status=status, amount=amount, approved_amount=approved, link=link,
            decided_by=cls.fc1, **extra
        )

    def grow(self, times):
        for index in range(times):
            pilot = f.create_user(f"grow_{times}_{index}", perms=f.SRP_MEMBER_PERMS, corporation=f.THIRD_CORP)
            fc = f.create_user(f"grow_fc_{times}_{index}", perms=[*f.FC_PERMS, "fleetops.request_srp"])
            link = f.fleet_link(f.create_operation(fc, status="closed", started_at=at(5)))
            for status in (Status.PENDING, Status.APPROVED, Status.PAID, Status.REJECTED):
                self.claim(pilot, status, 10 * M, 10 * M if status in (Status.APPROVED, Status.PAID) else None, link=link, when=at(6 + index))


class MonthTotalsTests(SummaryData):
    def test_totals_of_the_month(self):
        totals = summaries.month_totals(2026, 3)

        self.assertEqual(
            (totals.claims, totals.pending, totals.approved_count, totals.paid_count, totals.rejected),
            (8, 2, 5, 3, 1),
        )
        self.assertEqual(totals.requested, 320 * M)
        self.assertEqual(totals.approved, 265 * M)
        self.assertEqual(totals.paid, 175 * M)
        self.assertEqual(totals.outstanding, 90 * M)

    def test_withdrawn_claims_are_left_out(self):
        withdrawn_only = summaries.month_totals(2026, 3, base=SrpClaim.objects.filter(pk=self.a5.pk))

        self.assertEqual(withdrawn_only.claims, 0)
        self.assertEqual(withdrawn_only.requested, 0)

    def test_scope_split(self):
        in_fleet = summaries.month_totals(2026, 3, scope="in_fleet")
        out_of_fleet = summaries.month_totals(2026, 3, scope="out_of_fleet")

        self.assertEqual((in_fleet.claims, in_fleet.requested, in_fleet.approved), (4, 170 * M, 155 * M))
        self.assertEqual((out_of_fleet.claims, out_of_fleet.requested, out_of_fleet.approved), (4, 150 * M, 110 * M))

    def test_a_base_queryset_limits_the_claims(self):
        totals = summaries.month_totals(2026, 3, base=SrpClaim.objects.filter(user=self.bob))

        self.assertEqual((totals.claims, totals.requested, totals.approved, totals.paid), (2, 65 * M, 65 * M, 25 * M))

    def test_other_months_are_empty(self):
        for year, month in ((2026, 2), (2026, 4), (2025, 3)):
            with self.subTest(month=month, year=year):
                self.assertEqual(summaries.month_totals(year, month).claims, 0)

    def test_claims_totals_of_any_claims(self):
        totals = summaries.claims_totals(SrpClaim.objects.filter(link=self.link1))

        self.assertEqual((totals.claims, totals.approved, totals.paid, totals.outstanding), (2, 130 * M, 90 * M, 40 * M))

    def test_empty_totals_are_zero(self):
        totals = summaries.claims_totals(SrpClaim.objects.none())

        self.assertEqual((totals.claims, totals.requested, totals.approved, totals.paid, totals.outstanding), (0, 0, 0, 0, 0))


class MonthBoundaryTests(TestCase):
    def setUp(self):
        self.user = f.create_user("boundary_user", perms=f.SRP_MEMBER_PERMS)

    def claim(self, when, **extra):
        return f.srp_claim(self.user, time=when, **extra)

    def test_the_loss_time_decides_at_the_second(self):
        february = self.claim(datetime(2026, 2, 28, 23, 59, 59, tzinfo=UTC))
        march_first = self.claim(datetime(2026, 3, 1, 0, 0, 0, tzinfo=UTC))
        march_last = self.claim(datetime(2026, 3, 31, 23, 59, 59, tzinfo=UTC))
        april = self.claim(datetime(2026, 4, 1, 0, 0, 0, tzinfo=UTC))

        for (year, month), expected in (((2026, 2), [february]), ((2026, 3), [march_last, march_first]), ((2026, 4), [april])):
            with self.subTest(month=month):
                self.assertEqual(summaries.personal(self.user, year, month).claims, expected)
                self.assertEqual(summaries.month_totals(year, month).claims, len(expected))

    def test_submission_decision_and_payment_dates_do_not_move_a_claim(self):
        lost = datetime(2026, 3, 31, 23, 0, tzinfo=UTC)
        claim = self.claim(
            lost,
            status=Status.PAID,
            approved_amount=10 * M,
            submitted_at=datetime(2026, 4, 2, tzinfo=UTC),
            decided_at=datetime(2026, 4, 3, tzinfo=UTC),
            paid_at=datetime(2026, 5, 4, tzinfo=UTC),
        )

        self.assertEqual(summaries.month_totals(2026, 3).paid, 10 * M)
        for year, month in ((2026, 4), (2026, 5)):
            self.assertEqual(summaries.month_totals(year, month).claims, 0)
        self.assertEqual(summaries.personal(self.user, 2026, 3).claims, [claim])

    def test_the_site_time_zone_does_not_matter(self):
        # 23:30 UTC on March 31st is already April 1st in Auckland.
        claim = self.claim(datetime(2026, 3, 31, 23, 30, tzinfo=UTC))

        with override_settings(TIME_ZONE="Pacific/Auckland"), timezone.override("Pacific/Auckland"):
            self.assertEqual(summaries.personal(self.user, 2026, 3).claims, [claim])
            self.assertEqual(summaries.personal(self.user, 2026, 4).claims, [])
            self.assertEqual(summaries.month_totals(2026, 3).claims, 1)


class PersonalTests(SummaryData):
    def test_claims_totals_and_order(self):
        personal = summaries.personal(self.alice, 2026, 3)

        self.assertEqual((personal.year, personal.month), (2026, 3))
        # The latest loss first; the withdrawn claim stays in the list, so it can be submitted again.
        self.assertEqual(personal.claims, [self.a5, self.a4, self.a3, self.a2, self.a1])
        self.assertEqual(personal.totals.claims, 4)
        self.assertEqual((personal.totals.requested, personal.totals.approved, personal.totals.paid), (190 * M, 140 * M, 90 * M))

    def test_only_the_claims_of_the_player(self):
        self.assertEqual(summaries.personal(self.bob, 2026, 3).claims, [self.b2, self.b1])
        self.assertEqual(summaries.personal(self.fc1, 2026, 3).claims, [])

    def test_personal_and_corporation_limits(self):
        personal, corporation = summaries.personal(self.alice, 2026, 3).limits

        self.assertEqual((personal.kind, personal.cap, personal.used, personal.pending), (LIMIT_PERSONAL, 150 * M, 140 * M, 20 * M))
        self.assertEqual(personal.remaining, 10 * M)
        self.assertTrue(personal.applies)
        self.assertEqual((corporation.kind, corporation.cap, corporation.used, corporation.pending), (LIMIT_CORPORATION, 500 * M, 205 * M, 20 * M))
        self.assertEqual(corporation.remaining, 295 * M)

    def test_the_corporation_override_replaces_the_default(self):
        _personal, corporation = summaries.personal(self.cara, 2026, 3).limits

        self.assertEqual((corporation.cap, corporation.used, corporation.remaining), (100 * M, 60 * M, 40 * M))
        # Cara's pending claim does not count toward the corporation limit.
        self.assertEqual(corporation.pending, 0)

    def test_an_override_without_a_cap_means_no_limit(self):
        SrpCorporationLimit.objects.filter(corporation_id=f.OTHER_CORP[0]).update(monthly_cap=None)

        _personal, corporation = summaries.personal(self.cara, 2026, 3).limits

        self.assertIsNone(corporation.cap)
        self.assertIsNone(corporation.remaining)
        self.assertEqual(corporation.used, 60 * M)

    def test_no_caps_means_no_limits(self):
        SrpSettings.objects.filter(pk=1).update(personal_monthly_cap=None, corporation_monthly_cap=None)

        personal, corporation = summaries.personal(self.alice, 2026, 3).limits

        self.assertEqual((personal.cap, personal.remaining), (None, None))
        self.assertEqual((corporation.cap, corporation.remaining), (None, None))
        self.assertEqual(personal.used, 140 * M)

    def test_the_remaining_never_goes_below_zero(self):
        SrpSettings.objects.filter(pk=1).update(personal_monthly_cap=100 * M)

        personal, _corporation = summaries.personal(self.alice, 2026, 3).limits

        self.assertEqual((personal.used, personal.remaining), (140 * M, 0))

    def test_in_fleet_losses_that_do_not_count_are_not_used(self):
        SrpClaim.objects.filter(pk=self.a1.pk).update(counts_personal=False, counts_corporation=False)

        personal, corporation = summaries.personal(self.alice, 2026, 3).limits

        self.assertEqual(personal.used, 50 * M)
        self.assertEqual(corporation.used, 115 * M)

    def test_the_limits_of_a_month_without_claims(self):
        personal, corporation = summaries.personal(self.alice, 2026, 5).limits

        self.assertEqual((personal.used, personal.remaining), (0, 150 * M))
        self.assertEqual((corporation.used, corporation.remaining), (0, 500 * M))

    def test_a_player_without_a_main_has_no_corporation_limit(self):
        nobody = f.create_user("sum_nomain", perms=f.SRP_MEMBER_PERMS, with_main=False)

        personal, corporation = summaries.personal(nobody, 2026, 3).limits

        self.assertEqual(personal.cap, 150 * M)
        self.assertEqual((corporation.cap, corporation.used), (None, 0))

    def test_withdrawn_claims_use_no_limit(self):
        personal, _corporation = summaries.personal(self.alice, 2026, 3).limits

        self.assertEqual(personal.pending, 20 * M)


class MemberTests(SummaryData):
    def test_one_row_per_player_the_most_approved_first(self):
        rows = summaries.by_member(2026, 3)

        self.assertEqual([row.main_character_name for row in rows], ["Alice Main", "Bob Main", "Cara Main"])
        alice, bob, cara = rows
        self.assertEqual((alice.user_id, alice.corporation_id, alice.corporation_name), (self.alice.pk, f.DEFAULT_CORP[0], f.DEFAULT_CORP[1]))
        self.assertEqual((alice.totals.claims, alice.totals.requested, alice.totals.approved, alice.totals.paid), (4, 190 * M, 140 * M, 90 * M))
        self.assertEqual((bob.totals.claims, bob.totals.approved, bob.totals.outstanding), (2, 65 * M, 40 * M))
        self.assertEqual((cara.corporation_id, cara.totals.claims, cara.totals.pending), (f.OTHER_CORP[0], 2, 1))

    def test_one_corporation(self):
        rows = summaries.by_member(2026, 3, corporation_id=f.OTHER_CORP[0])

        self.assertEqual([row.main_character_name for row in rows], ["Cara Main"])

    def test_a_corporation_without_claims_has_no_rows(self):
        self.assertEqual(summaries.by_member(2026, 3, corporation_id=1_234_567), [])
        self.assertEqual(summaries.by_member(2026, 1), [])

    def test_claims_of_a_deleted_account_stay_in_the_list(self):
        self.cara.delete()

        rows = summaries.by_member(2026, 3)

        row = next(row for row in rows if row.main_character_name == "Cara Main")
        self.assertIsNone(row.user_id)
        self.assertEqual(row.totals.claims, 2)

    def test_the_snapshot_decides_the_corporation(self):
        # Alice moves to the other corporation after her claims: they stay where they were made.
        main = f.main_of(self.alice)
        main.corporation_id, main.corporation_name = f.OTHER_CORP
        main.save()

        rows = {row.main_character_name: row for row in summaries.by_member(2026, 3)}

        self.assertEqual(rows["Alice Main"].corporation_id, f.DEFAULT_CORP[0])


class CorporationTests(SummaryData):
    def test_rows_with_caps_and_what_is_left(self):
        rows = {row.corporation_id: row for row in summaries.by_corporation(2026, 3)}

        corp1, corp2 = rows[f.DEFAULT_CORP[0]], rows[f.OTHER_CORP[0]]
        self.assertEqual(list(rows), [f.DEFAULT_CORP[0], f.OTHER_CORP[0]])
        self.assertEqual((corp1.corporation_name, corp1.corporation_ticker), (f.DEFAULT_CORP[1], f.DEFAULT_CORP[1][:5].upper()))
        self.assertEqual((corp1.totals.claims, corp1.totals.requested, corp1.totals.approved, corp1.totals.paid), (6, 255 * M, 205 * M, 115 * M))
        self.assertEqual((corp1.cap, corp1.used, corp1.remaining), (500 * M, 205 * M, 295 * M))
        self.assertEqual((corp2.totals.claims, corp2.totals.requested), (2, 65 * M))
        self.assertEqual((corp2.cap, corp2.used, corp2.remaining), (100 * M, 60 * M, 40 * M))

    def test_what_does_not_count_toward_the_corporation_limit_is_not_used(self):
        SrpClaim.objects.filter(pk=self.c1.pk).update(counts_corporation=False)

        row = summaries.corporation_row(f.OTHER_CORP[0], 2026, 3)

        self.assertEqual(row.used, 0)
        self.assertEqual(row.totals.approved, 60 * M)

    def test_no_cap_at_all(self):
        SrpSettings.objects.filter(pk=1).update(corporation_monthly_cap=None)

        rows = {row.corporation_id: row for row in summaries.by_corporation(2026, 3)}

        self.assertEqual((rows[f.DEFAULT_CORP[0]].cap, rows[f.DEFAULT_CORP[0]].remaining), (None, None))
        self.assertEqual(rows[f.OTHER_CORP[0]].cap, 100 * M)

    def test_an_override_without_a_cap_is_no_limit_even_with_a_default(self):
        SrpCorporationLimit.objects.filter(corporation_id=f.OTHER_CORP[0]).update(monthly_cap=None)

        row = summaries.corporation_row(f.OTHER_CORP[0], 2026, 3)

        self.assertEqual((row.cap, row.remaining), (None, None))

    def test_the_remaining_is_zero_when_the_cap_was_lowered_below_the_use(self):
        SrpCorporationLimit.objects.filter(corporation_id=f.OTHER_CORP[0]).update(monthly_cap=50 * M)

        row = summaries.corporation_row(f.OTHER_CORP[0], 2026, 3)

        self.assertEqual((row.cap, row.used, row.remaining), (50 * M, 60 * M, 0))

    def test_a_corporation_without_claims_still_has_its_row(self):
        row = summaries.corporation_row(f.OTHER_CORP[0], 2026, 6, name="Test Corp Two")

        self.assertEqual(row.corporation_name, "Test Corp Two")
        self.assertEqual((row.totals.claims, row.used, row.cap, row.remaining), (0, 0, 100 * M, 100 * M))

    def test_the_corporation_of_a_corporation_row_is_the_one_of_the_claims(self):
        main = f.main_of(self.cara)
        main.corporation_id, main.corporation_name = f.DEFAULT_CORP
        main.save()

        row = summaries.corporation_row(f.OTHER_CORP[0], 2026, 3)

        self.assertEqual(row.totals.claims, 2)
        self.assertEqual(summaries.corporation_row(f.DEFAULT_CORP[0], 2026, 3).totals.claims, 6)


class FcTests(SummaryData):
    def test_in_fleet_claims_by_the_fc_of_the_link(self):
        rows = summaries.by_fc(2026, 3)

        self.assertEqual([row.fc_name for row in rows], ["First FC", "Second FC"])
        first, second = rows
        self.assertEqual((first.fc_user_id, first.fleets), (self.fc1.pk, 1))
        self.assertEqual((first.totals.claims, first.totals.requested, first.totals.approved, first.totals.paid), (2, 140 * M, 130 * M, 90 * M))
        self.assertEqual((second.fleets, second.totals.claims, second.totals.requested, second.totals.approved), (1, 2, 30 * M, 25 * M))

    def test_out_of_fleet_claims_have_no_fc(self):
        rows = summaries.by_fc(2026, 3)

        self.assertEqual(sum(row.totals.claims for row in rows), 4)

    def test_the_fc_is_the_one_of_the_link_not_the_pilot(self):
        # The link's FC changes hands later (or the account goes away): the claims keep their snapshot.
        SrpFleetLink.objects.filter(pk=self.link1.pk).update(fc_user=self.fc2)

        rows = {row.fc_name: row for row in summaries.by_fc(2026, 3)}

        self.assertEqual(rows["First FC"].totals.claims, 2)

    def test_the_name_falls_back_to_the_main_of_the_account(self):
        SrpClaim.objects.filter(fc_user=self.fc2).update(fc_main_character_name="")

        names = {row.fc_user_id: row.fc_name for row in summaries.by_fc(2026, 3)}

        self.assertEqual(names[self.fc2.pk], "Second FC")

    def test_several_fleets_of_one_fc(self):
        extra = f.fleet_link(f.create_operation(self.fc1, status="closed", started_at=at(20)))
        self.claim(self.alice, Status.APPROVED, 10 * M, 10 * M, link=extra, when=at(20, 13))

        first = summaries.by_fc(2026, 3)[0]

        self.assertEqual((first.fleets, first.totals.claims), (2, 3))

    def test_fleets_of_one_fc(self):
        rows = summaries.fc_fleets(self.fc1, 2026, 3)

        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual((row.link_code, row.operation_uuid, row.is_open), (self.link1.code, self.fleet1.uuid, True))
        self.assertEqual(row.fleet_label, self.a1.fleet_label)
        self.assertEqual((row.totals.claims, row.totals.requested, row.totals.approved), (2, 140 * M, 130 * M))

    def test_other_fcs_fleets_are_not_listed(self):
        self.assertEqual([row.link_code for row in summaries.fc_fleets(self.fc2, 2026, 3)], [self.link2.code])
        self.assertEqual(summaries.fc_fleets(self.alice, 2026, 3), [])

    def test_a_closed_link_is_shown_closed(self):
        SrpFleetLink.objects.filter(pk=self.link1.pk).update(is_open=False)

        self.assertFalse(summaries.fc_fleets(self.fc1, 2026, 3)[0].is_open)

    def test_the_latest_fleet_comes_first(self):
        later = f.fleet_link(f.create_operation(self.fc1, status="closed", started_at=at(25)))
        self.claim(self.alice, Status.APPROVED, 10 * M, 10 * M, link=later, when=at(25, 13))

        self.assertEqual([row.link_code for row in summaries.fc_fleets(self.fc1, 2026, 3)], [later.code, self.link1.code])

    def test_claims_of_a_deleted_fleet_keep_their_label(self):
        self.fleet1.delete()

        rows = summaries.fc_fleets(self.fc1, 2026, 3)

        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0].link_code, rows[0].operation_uuid, rows[0].is_open), ("", None, False))
        self.assertEqual(rows[0].fleet_label, self.a1.fleet_label)
        self.assertEqual(rows[0].totals.claims, 2)


class AllianceTests(SummaryData):
    def test_totals_split_and_status(self):
        summary = summaries.alliance(2026, 3)

        self.assertEqual((summary.year, summary.month), (2026, 3))
        self.assertEqual(summary.totals.claims, 8)
        self.assertEqual((summary.in_fleet.claims, summary.in_fleet.approved), (4, 155 * M))
        self.assertEqual((summary.out_of_fleet.claims, summary.out_of_fleet.approved), (4, 110 * M))
        self.assertEqual(summary.totals.approved, summary.in_fleet.approved + summary.out_of_fleet.approved)
        self.assertEqual(
            summary.by_status,
            {Status.PENDING: 2, Status.APPROVED: 2, Status.PAID: 3, Status.REJECTED: 1},
        )
        self.assertEqual(summary.by_status["pending"], 2)

    def test_the_ships_lost_most(self):
        ships = summaries.alliance(2026, 3).top_ships

        # Rifter 4 losses (a2, a3, b2, c2), Muninn 2, Guardian 2 (a4 rejected still lost).
        self.assertEqual([(ship.ship_type_id, ship.losses) for ship in ships], [(RIFTER, 4), (MUNINN, 2), (GUARDIAN, 2)])
        self.assertEqual(ships[0].ship_type_name, f"Ship {RIFTER}")
        self.assertEqual(ships[0].approved, 75 * M)
        self.assertEqual(ships[1].approved, 130 * M)

    def test_only_the_top_ten_ships(self):
        for index in range(12):
            self.claim(self.alice, Status.APPROVED, M, M, ship=20_000 + index, when=at(17, 1 + index))

        ships = summaries.alliance(2026, 3).top_ships

        self.assertEqual(len(ships), summaries.TOP_SHIPS)
        self.assertEqual(ships[0].ship_type_id, RIFTER)

    def test_an_empty_month(self):
        summary = summaries.alliance(2026, 1)

        self.assertEqual(summary.totals.claims, 0)
        self.assertEqual(summary.top_ships, [])
        self.assertEqual(summary.by_status, {Status.PENDING: 0, Status.APPROVED: 0, Status.PAID: 0, Status.REJECTED: 0})


class ListTests(SummaryData):
    def test_claims_of_a_corporation(self):
        claims = summaries.claims_in_month(2026, 3, corporation_id=f.OTHER_CORP[0])

        # The latest loss first.
        self.assertEqual(claims, [self.c1, self.c2])

    def test_claims_of_an_fc(self):
        claims = summaries.claims_in_month(2026, 3, fc_user_id=self.fc1.pk)

        self.assertEqual(set(claims), {self.a1, self.b1})

    def test_withdrawn_claims_are_not_listed(self):
        self.assertNotIn(self.a5, summaries.claims_in_month(2026, 3, corporation_id=f.DEFAULT_CORP[0]))

    def test_the_list_is_cut(self):
        self.assertEqual(len(summaries.claims_in_month(2026, 3, limit=3)), 3)


class QueryCountTests(SummaryData):
    """Every function costs the same number of queries whatever the number of claims."""

    def measure(self, call):
        with CaptureQueriesContext(connection) as queries:
            call()
        return len(queries)

    def calls(self):
        return {
            "month_totals": lambda: summaries.month_totals(2026, 3),
            "personal": lambda: summaries.personal(self.alice, 2026, 3),
            "by_member": lambda: summaries.by_member(2026, 3),
            "by_corporation": lambda: summaries.by_corporation(2026, 3),
            "by_fc": lambda: summaries.by_fc(2026, 3),
            "fc_fleets": lambda: summaries.fc_fleets(self.fc1, 2026, 3),
            "alliance": lambda: summaries.alliance(2026, 3),
            "claims_in_month": lambda: summaries.claims_in_month(2026, 3),
        }

    def test_the_number_of_queries_does_not_grow_with_the_data(self):
        small = {name: self.measure(call) for name, call in self.calls().items()}
        self.grow(3)
        medium = {name: self.measure(call) for name, call in self.calls().items()}
        self.grow(6)
        large = {name: self.measure(call) for name, call in self.calls().items()}

        self.assertEqual(small, medium)
        self.assertEqual(small, large)
        self.assertTrue(all(count <= 8 for count in small.values()), small)


class SummaryPageTests(SummaryData):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.manager = f.create_user("page_manager", perms=f.SRP_MANAGER_PERMS)
        cls.viewer = f.create_user("page_viewer", perms=[*f.MEMBER_PERMS, "fleetops.view_all_stats"])
        cls.corp_manager = f.create_user("page_corp", perms=[*f.MEMBER_PERMS, "fleetops.view_corp_stats"])
        cls.other_corp_manager = f.create_user(
            "page_other_corp", perms=[*f.MEMBER_PERMS, "fleetops.view_corp_stats"], corporation=f.OTHER_CORP
        )

    def get(self, user, name, *args, **params):
        self.client.force_login(user)
        return self.client.get(reverse(f"fleetops:{name}", args=args), {"year": 2026, "month": 3, **params})

    def test_the_alliance_page_shows_the_numbers(self):
        response = self.get(self.manager, "srp_summary")

        self.assertEqual(response.status_code, 200)
        summary = response.context["summary"]
        self.assertEqual(summary.totals.claims, 8)
        self.assertEqual([row.corporation_id for row in response.context["corporation_rows"]], [f.DEFAULT_CORP[0], f.OTHER_CORP[0]])
        self.assertEqual([row.fc_name for row in response.context["fc_rows"]], ["First FC", "Second FC"])
        self.assertContains(response, "265,000,000 ISK")
        self.assertContains(response, "Test Corp One")
        self.assertContains(response, "First FC")
        self.assertContains(response, reverse("fleetops:srp_summary_corporation", args=[f.OTHER_CORP[0]]))
        self.assertContains(response, reverse("fleetops:srp_summary_fc", args=[self.fc1.pk]))

    def test_view_all_stats_reads_the_alliance_page_and_exports(self):
        response = self.get(self.viewer, "srp_summary")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "read only")
        self.assertContains(response, reverse("fleetops:export", args=["srp-corporations"]))

    def test_the_month_comes_from_the_address(self):
        response = self.get(self.manager, "srp_summary", year=2026, month=4)

        self.assertEqual(response.context["summary"].totals.claims, 0)
        self.assertEqual((response.context["year"], response.context["month"]), (2026, 4))
        self.assertContains(response, "No claims this month.")

    def test_a_corporation_manager_sees_the_own_corporation_and_its_claims(self):
        response = self.get(self.corp_manager, "srp_summary_own_corporation")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["corporation_id"], f.DEFAULT_CORP[0])
        self.assertTrue(response.context["own_corporation"])
        self.assertEqual(response.context["corporation"].totals.claims, 6)
        self.assertEqual({claim.pk for claim in response.context["claims"]}, {c.pk for c in (self.a1, self.a2, self.a3, self.a4, self.b1, self.b2)})
        self.assertEqual([row.main_character_name for row in response.context["member_rows"]], ["Alice Main", "Bob Main"])
        self.assertContains(response, self.a1.reference)
        self.assertContains(response, reverse("fleetops:srp_claim", args=[self.a1.uuid]))
        self.assertNotContains(response, self.c1.reference)
        self.assertNotContains(response, "Cara Main")

    def test_a_corporation_manager_cannot_open_another_corporation(self):
        response = self.get(self.corp_manager, "srp_summary_corporation", f.OTHER_CORP[0])

        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.get(self.corp_manager, "srp_summary").status_code, 403)

    def test_the_other_corporation_sees_only_its_own_numbers(self):
        response = self.get(self.other_corp_manager, "srp_summary_own_corporation")

        self.assertEqual(response.context["corporation"].totals.claims, 2)
        self.assertEqual((response.context["corporation"].cap, response.context["corporation"].remaining), (100 * M, 40 * M))
        self.assertNotContains(response, "Alice Main")

    def test_managers_open_any_corporation(self):
        response = self.get(self.manager, "srp_summary_corporation", f.OTHER_CORP[0])

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context["own_corporation"])
        self.assertContains(response, "Cara Main")

    def test_an_fc_sees_the_fleets_they_led_and_their_claims(self):
        response = self.get(self.fc1, "srp_summary_own_fc")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["totals"].claims, 2)
        self.assertEqual([row.link_code for row in response.context["fleet_rows"]], [self.link1.code])
        self.assertEqual({claim.pk for claim in response.context["claims"]}, {self.a1.pk, self.b1.pk})
        self.assertContains(response, reverse("fleetops:operation_detail", args=[self.fleet1.uuid]))
        self.assertNotContains(response, self.b2.reference)
        # An FC reads the numbers; exports are for the people who see everything.
        self.assertNotContains(response, reverse("fleetops:export", args=["srp-claims"]))

    def test_an_fc_cannot_open_another_fc(self):
        self.assertEqual(self.get(self.fc1, "srp_summary_fc", self.fc2.pk).status_code, 403)
        self.assertEqual(self.get(self.manager, "srp_summary_fc", self.fc2.pk).status_code, 200)

    def test_internal_notes_are_nowhere_on_the_summaries(self):
        f.claim_event(self.a1, "note", actor=self.manager, internal=True, reason="Secret reviewer remark")

        for user, name, args in (
            (self.manager, "srp_summary", ()),
            (self.viewer, "srp_summary_corporation", (f.DEFAULT_CORP[0],)),
            (self.corp_manager, "srp_summary_own_corporation", ()),
            (self.fc1, "srp_summary_own_fc", ()),
        ):
            with self.subTest(page=name):
                self.assertNotContains(self.get(user, name, *args), "Secret reviewer remark")

    def test_withdrawn_claims_are_not_listed(self):
        response = self.get(self.corp_manager, "srp_summary_own_corporation")

        self.assertNotContains(response, self.a5.reference)

    def test_the_page_costs_a_constant_number_of_queries(self):
        def queries():
            self.client.force_login(self.manager)
            with CaptureQueriesContext(connection) as captured:
                self.client.get(reverse("fleetops:srp_summary"), {"year": 2026, "month": 3})
            return len(captured)

        queries()  # the first request fills the caches of the framework
        small = queries()
        self.grow(4)
        large = queries()

        self.assertEqual(small, large)
