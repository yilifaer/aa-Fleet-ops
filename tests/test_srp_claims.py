# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Submitting SRP claims: the preview, the claim with its snapshots, the over-limit outcomes, withdrawing,
submitting again and the value that arrives later."""

import time
from datetime import datetime, timedelta, timezone as dt_timezone
from decimal import Decimal
from unittest import mock

from allianceauth.authentication.models import CharacterOwnership
from allianceauth.notifications.models import Notification
from django.core import signing
from django.core.cache import cache
from django.db import IntegrityError
from django.test import TestCase
from django.utils import timezone

from fleetops.models import (
    AuditLog,
    FleetMemberEvent,
    SrpClaim,
    SrpClaimEvent,
    SrpCorporationLimit,
    SrpKillmail,
    SrpPayoutRule,
)
from fleetops.srp import claims, display, quota, review
from fleetops.srp.types import (
    Calculation,
    PolicySnapshot,
    SrpDuplicateError,
    SrpError,
    SrpStateError,
    warnings_from_json,
)

from . import factories as f

Status = SrpClaim.Status
IN_FLEET = SrpClaim.Scope.IN_FLEET
OUT_OF_FLEET = SrpClaim.Scope.OUT_OF_FLEET
Zkill = SrpKillmail.ZkillStatus

MOA = 623


def utc(*args):
    return datetime(*args, tzinfo=dt_timezone.utc)


def codes(warnings):
    return [warning.code for warning in warnings]


def stored_codes(claim):
    return codes(warnings_from_json(claim.warnings))


class ClaimBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.srp = f.srp_settings(percentage=Decimal("80"))
        cls.user = f.create_user("pilot", perms=f.SRP_MEMBER_PERMS)
        cls.main = f.main_of(cls.user)
        cls.other = f.create_user("other-pilot", perms=f.SRP_MEMBER_PERMS)
        cls.manager = f.create_user("manager", perms=f.SRP_MANAGER_PERMS)
        cls.fc = f.create_user("fc", perms=f.FC_PERMS)

    def setUp(self):
        super().setUp()
        for user in (self.user, self.other, self.manager, self.fc):
            cache.delete(f"fleetops:srp:check:{user.pk}")
        self.addCleanup(self.clear_throttle)

    def clear_throttle(self):
        for user in (self.user, self.other, self.manager, self.fc):
            cache.delete(f"fleetops:srp:check:{user.pk}")

    def settings(self, **values):
        return f.srp_settings(**values)

    def killmail(self, character=None, **kwargs):
        kwargs.setdefault("fitted", "100000000")
        return f.srp_killmail(character or self.main, **kwargs)

    def text(self, km):
        return f"<url=killReport:{km.killmail_id}:{km.killmail_hash}>Loss</url>"

    def prepare(self, km, *, user=None, link=None):
        with mock.patch.object(claims.killmails, "fetch_killmail", return_value=km) as fetch:
            preview = claims.prepare_claim(user or self.user, self.text(km), link=link)
        self.fetch = fetch
        return preview

    def submit(self, km, *, user=None, link=None, comment="", preview=None):
        user = user or self.user
        preview = preview or self.prepare(km, user=user, link=link)
        self.assertTrue(preview.token, preview.blockers)
        return claims.submit_claim(user, preview.token, comment=comment)

    def token(self, km, *, user=None, link=None):
        return signing.dumps(
            {"km": km.pk, "link": link.code if link else None, "u": (user or self.user).pk}, salt=claims.PREVIEW_SALT
        )

    def fleet(self, *, member=True, **extra):
        operation = f.create_operation(self.fc, **extra)
        link = f.fleet_link(operation)
        if member:
            f.member_state(operation, self.main, first_seen=operation.started_at)
            event = FleetMemberEvent.objects.create(
                operation=operation,
                character_id=self.main.character_id,
                character_name=self.main.character_name,
                event_type=FleetMemberEvent.EventType.JOIN,
            )
            FleetMemberEvent.objects.filter(pk=event.pk).update(created_at=operation.started_at)
        return operation, link


class PrepareClaimTests(ClaimBase):
    def test_preview_of_an_out_of_fleet_loss(self):
        km = self.killmail(fitted="250000000", ship_name="Moa", ship_type_id=MOA)
        preview = self.prepare(km)
        self.assertEqual(preview.killmail, km)
        self.assertEqual(preview.scope, OUT_OF_FLEET)
        self.assertIsNone(preview.link)
        self.assertEqual(preview.user, self.user)
        self.assertTrue(preview.eligible)
        self.assertEqual(preview.blockers, [])
        self.assertIsNone(preview.existing_claim)
        self.assertEqual(preview.presence.code, display.NOT_APPLICABLE)
        self.assertEqual(preview.identity.character_id, self.main.character_id)
        self.assertEqual(preview.identity.main_character_name, self.main.character_name)
        self.assertEqual(preview.identity.corporation_id, f.DEFAULT_CORP[0])
        self.assertEqual(preview.calculation.basis, "percentage")
        self.assertEqual(preview.calculation.suggested, 200_000_000)
        self.assertEqual(preview.calculation.value_source, "zkill_fitted")
        self.assertEqual(preview.policy.percentage, Decimal("80"))
        self.assertEqual(preview.warnings, [])

    def test_the_pasted_reference_is_parsed_and_fetched(self):
        km = self.killmail()
        self.prepare(km)
        reference = self.fetch.call_args.args[0]
        self.assertEqual((reference.killmail_id, reference.killmail_hash), (km.killmail_id, km.killmail_hash))
        self.assertEqual(self.fetch.call_args.kwargs, {"zkill_ok": True})

    def test_zkillboard_switch_is_passed_on(self):
        self.settings(zkillboard_enabled=False, value_source="esi_estimate")
        self.prepare(self.killmail())
        self.assertEqual(self.fetch.call_args.kwargs, {"zkill_ok": False})

    def test_unreadable_text_is_an_error(self):
        with self.assertRaises(SrpError) as caught:
            claims.prepare_claim(self.user, "not a killmail")
        self.assertEqual(caught.exception.code, "unrecognised")

    def test_errors_of_the_fetch_pass_through(self):
        with mock.patch.object(claims.killmails, "fetch_killmail", side_effect=SrpError("not_found", "Killmail not found.")):
            with self.assertRaises(SrpError) as caught:
                claims.prepare_claim(self.user, self.text(self.killmail()))
        self.assertEqual(caught.exception.message, "Killmail not found.")

    def test_payout_rule_is_used_before_the_percentage(self):
        rule = f.payout_rule(MOA, 60_000_000)
        preview = self.prepare(self.killmail(ship_type_id=MOA, fitted="900000000"))
        self.assertEqual(preview.calculation.basis, "rule")
        self.assertEqual(preview.calculation.suggested, 60_000_000)
        self.assertEqual(preview.policy.rule.id, rule.pk)

    def test_in_fleet_preview_checks_the_fleet(self):
        operation, link = self.fleet(doctrine_name="Moa Fleet")
        f.payout_rule(0, 75_000_000, doctrine="Moa Fleet")
        km = self.killmail(time=timezone.now() - timedelta(minutes=2))
        preview = self.prepare(km, link=link)
        self.assertEqual(preview.scope, IN_FLEET)
        self.assertEqual(preview.link, link)
        self.assertEqual(preview.presence.code, display.IN_FLEET)
        self.assertEqual(preview.calculation.basis, "rule")
        self.assertEqual(preview.calculation.suggested, 75_000_000)
        self.assertEqual(preview.policy.rule.match, "doctrine")
        self.assertEqual(preview.warnings, [])
        self.assertTrue(preview.eligible)

    def test_in_fleet_loss_outside_the_tracked_time_warns(self):
        operation, link = self.fleet()
        km = self.killmail(time=operation.started_at - timedelta(hours=3))
        preview = self.prepare(km, link=link)
        self.assertEqual(preview.presence.code, display.NOT_IN_FLEET)
        self.assertEqual(codes(preview.warnings), [display.NOT_IN_FLEET])
        self.assertEqual(preview.warnings[0].level, display.LEVEL_DANGER)
        self.assertTrue(preview.eligible, "Presence only warns")
        self.assertTrue(preview.token)

    def test_presence_warning_for_a_pilot_never_seen(self):
        operation, link = self.fleet(member=False)
        preview = self.prepare(self.killmail(), link=link)
        self.assertEqual(preview.presence.code, display.NEVER_SEEN)
        self.assertEqual(codes(preview.warnings), [display.NEVER_SEEN])

    def test_ship_mismatch_warning(self):
        operation, link = self.fleet()
        event = FleetMemberEvent.objects.create(
            operation=operation,
            character_id=self.main.character_id,
            character_name=self.main.character_name,
            event_type=FleetMemberEvent.EventType.SHIP_CHANGE,
            old_value="Rifter",
            new_value="Caracal",
        )
        FleetMemberEvent.objects.filter(pk=event.pk).update(created_at=operation.started_at + timedelta(minutes=1))
        km = self.killmail(time=operation.started_at + timedelta(minutes=10), ship_type_id=MOA, ship_name="Moa")
        preview = self.prepare(km, link=link)
        self.assertEqual(codes(preview.warnings), ["ship_mismatch"])
        self.assertEqual(preview.warnings[0].params["ship"], "Caracal")
        self.assertEqual(preview.warnings[0].text, "Tracked ship before the loss: Caracal.")

    def test_corporation_is_the_main_characters_not_the_victims(self):
        alt = f.add_alt(self.user, "Alt In Another Corp", corporation=f.OTHER_CORP)
        preview = self.prepare(self.killmail(alt))
        self.assertEqual(preview.identity.character_id, alt.character_id)
        self.assertEqual(preview.identity.main_character_id, self.main.character_id)
        self.assertEqual(preview.identity.corporation_id, f.DEFAULT_CORP[0])
        self.assertEqual(preview.identity.corporation_name, f.DEFAULT_CORP[1])
        self.assertEqual(preview.identity.alliance_id, f.DEFAULT_ALLIANCE[0])

    def test_loss_of_someone_elses_character_is_blocked_without_a_calculation(self):
        stranger = f.main_of(self.other)
        preview = self.prepare(self.killmail(stranger))
        self.assertFalse(preview.eligible)
        self.assertEqual(preview.blockers, ["This character is not on your Alliance Auth account."])
        self.assertEqual(preview.token, "")
        self.assertIsNone(preview.calculation)
        self.assertIsNone(preview.policy)
        self.assertIsNone(preview.presence)
        self.assertEqual(preview.identity.character_id, stranger.character_id)
        self.assertIsNone(preview.identity.main_character_id)

    def test_loss_without_a_pilot(self):
        preview = self.prepare(f.srp_killmail(None, fitted="1000"))
        self.assertEqual(preview.blockers, ["This loss has no pilot."])
        self.assertIsNone(preview.identity)
        self.assertEqual(preview.token, "")

    def test_blockers_that_do_not_hide_the_calculation(self):
        self.settings(claim_deadline_days=1)
        km = self.killmail(time=timezone.now() - timedelta(days=3))
        preview = self.prepare(km)
        self.assertEqual(preview.blockers, ["Losses older than 1 days cannot be submitted."])
        self.assertFalse(preview.eligible)
        self.assertEqual(preview.token, "")
        self.assertIsNotNone(preview.calculation)

    def test_capsule_without_a_rule_is_blocked(self):
        preview = self.prepare(self.killmail(ship_type_id=f.CAPSULE))
        self.assertEqual(preview.blockers, ["Capsule losses are not covered."])
        self.assertEqual(preview.calculation.suggested, 0)

    def test_capsule_with_a_rule_is_paid_the_fixed_amount(self):
        f.payout_rule(f.CAPSULE, 1_500_000)
        preview = self.prepare(self.killmail(ship_type_id=f.CAPSULE, fitted="800000000"))
        self.assertTrue(preview.eligible)
        self.assertEqual(preview.calculation.suggested, 1_500_000)

    def test_closed_link_and_disabled_srp_block(self):
        _operation, link = self.fleet()
        link.is_open = False
        link.save()
        self.assertEqual(self.prepare(self.killmail(), link=link).blockers, ["This SRP link is closed."])
        self.settings(enabled=False)
        self.assertIn("FleetOps SRP is not enabled.", self.prepare(self.killmail()).blockers)

    def test_out_of_fleet_losses_can_be_switched_off(self):
        self.settings(out_of_fleet_enabled=False)
        self.assertEqual(self.prepare(self.killmail()).blockers, ["Only losses in FleetOps fleets are covered."])

    def test_existing_own_claim_is_reported_with_a_link_to_it(self):
        km = self.killmail()
        claim = f.srp_claim(self.user, killmail=km)
        preview = self.prepare(km)
        self.assertEqual(preview.existing_claim, claim)
        self.assertEqual(preview.blockers, ["You already submitted this loss."])
        self.assertFalse(preview.eligible)
        self.assertEqual(preview.token, "")
        self.assertIsNone(preview.calculation)

    def test_existing_claim_of_another_pilot_is_not_named_for_members(self):
        stranger = f.main_of(self.other)
        km = self.killmail(stranger)
        f.srp_claim(self.other, killmail=km)
        preview = self.prepare(km)
        self.assertEqual(preview.blockers, ["This loss was already claimed by another pilot."])
        self.assertNotIn(stranger.character_name, " ".join(preview.blockers))

    def test_managers_see_who_claimed_it(self):
        km = self.killmail(f.main_of(self.other))
        f.srp_claim(self.other, killmail=km)
        preview = self.prepare(km, user=self.manager)
        self.assertEqual(
            preview.blockers, [f"This loss was already claimed by {f.main_of(self.other).character_name}."]
        )

    def test_token_names_the_killmail_link_and_user(self):
        _operation, link = self.fleet()
        km = self.killmail()
        data = signing.loads(self.prepare(km, link=link).token, salt=claims.PREVIEW_SALT, max_age=60)
        self.assertEqual(data, {"km": km.pk, "link": link.code, "u": self.user.pk})
        data = signing.loads(self.prepare(self.killmail()).token, salt=claims.PREVIEW_SALT, max_age=60)
        self.assertIsNone(data["link"])

    def test_check_creates_no_claim(self):
        self.prepare(self.killmail())
        self.assertFalse(SrpClaim.objects.exists())


class PreviewWarningTests(ClaimBase):
    def test_pending_value_is_a_warning(self):
        km = self.killmail(fitted=None, zkill_status=Zkill.PENDING)
        preview = self.prepare(km)
        self.assertEqual(preview.calculation.basis, "none")
        self.assertEqual(codes(preview.warnings), ["value_pending"])
        self.assertTrue(preview.eligible)

    def test_value_that_will_not_come_is_a_warning(self):
        km = self.killmail(fitted=None, zkill_status=Zkill.UNAVAILABLE)
        self.assertEqual(codes(self.prepare(km).warnings), ["value_missing"])

    def test_rule_claims_do_not_wait_for_a_value(self):
        f.payout_rule(MOA, 30_000_000)
        km = self.killmail(fitted=None, ship_type_id=MOA, zkill_status=Zkill.PENDING)
        self.assertEqual(self.prepare(km).warnings, [])

    def test_estimated_value_is_a_warning(self):
        km = self.killmail(fitted=None, zkill_status=Zkill.UNAVAILABLE, esi_estimate=Decimal("50000000"))
        preview = self.prepare(km)
        self.assertEqual(preview.calculation.value_source, "esi_estimate")
        self.assertEqual(preview.calculation.suggested, 40_000_000)
        self.assertEqual(codes(preview.warnings), ["value_estimate"])

    def test_zero_percentage_is_not_a_value_warning(self):
        self.settings(percentage=Decimal("0"))
        km = self.killmail(fitted=None, zkill_status=Zkill.PENDING)
        self.assertEqual(self.prepare(km).warnings, [])

    def test_external_claim_warning(self):
        km = self.killmail()
        with mock.patch.object(claims.legacy, "external_claims", return_value=["aa-srp: Pending"]) as lookup:
            preview = self.prepare(km)
        lookup.assert_called_once_with(km.killmail_id)
        self.assertEqual(codes(preview.warnings), ["external_claim"])
        self.assertEqual(preview.warnings[0].params, {"claims": ["aa-srp: Pending"]})
        self.assertTrue(preview.eligible, "A claim elsewhere only warns")

    def test_unreadable_other_systems_are_ignored(self):
        with mock.patch.object(claims.legacy, "external_claims", side_effect=RuntimeError("boom")):
            self.assertEqual(self.prepare(self.killmail()).warnings, [])

    def test_over_the_monthly_limit_in_review_mode(self):
        self.settings(personal_monthly_cap=150_000_000, over_limit_action="review")
        f.srp_claim(self.user, amount=100_000_000, status=Status.APPROVED)
        preview = self.prepare(self.killmail(fitted="125000000"))
        self.assertTrue(preview.calculation.over_limit)
        self.assertEqual((preview.calculation.suggested, preview.calculation.limited), (100_000_000, 50_000_000))
        self.assertEqual(codes(preview.warnings), ["over_limit"])
        warning = preview.warnings[0]
        self.assertEqual(warning.level, display.LEVEL_DANGER)
        self.assertEqual(warning.params, {"kind": "personal", "requested": 100_000_000, "remaining": 50_000_000})
        self.assertEqual(
            warning.text, "Over the monthly personal limit: 100,000,000 ISK requested, 50,000,000 ISK left."
        )
        self.assertTrue(preview.eligible)

    def test_partly_fitting_claim_in_reject_mode_is_reduced_not_rejected(self):
        self.settings(personal_monthly_cap=150_000_000, over_limit_action="reject")
        f.srp_claim(self.user, amount=100_000_000, status=Status.APPROVED)
        preview = self.prepare(self.killmail(fitted="125000000"))
        self.assertEqual(codes(preview.warnings), ["reduced_by_limit"])
        self.assertEqual(preview.warnings[0].level, display.LEVEL_WARNING)

    def test_nothing_left_in_reject_mode_warns_as_over_limit(self):
        self.settings(personal_monthly_cap=100_000_000, over_limit_action="reject")
        f.srp_claim(self.user, amount=100_000_000, status=Status.APPROVED)
        preview = self.prepare(self.killmail(fitted="125000000"))
        self.assertEqual(codes(preview.warnings), ["over_limit"])

    def test_remaining_allowance_is_part_of_the_calculation(self):
        self.settings(personal_monthly_cap=400_000_000, corporation_monthly_cap=900_000_000)
        f.srp_claim(self.user, amount=100_000_000, status=Status.APPROVED)
        f.srp_claim(self.user, amount=30_000_000, status=Status.PENDING)
        calculation = self.prepare(self.killmail()).calculation
        personal, corporation = calculation.limit("personal"), calculation.limit("corporation")
        self.assertEqual((personal.used, personal.pending, personal.remaining), (100_000_000, 30_000_000, 300_000_000))
        self.assertEqual((corporation.cap, corporation.remaining), (900_000_000, 800_000_000))

    def test_out_of_fleet_preview_offers_the_fleet_the_pilot_was_in(self):
        operation, link = self.fleet(started_at=timezone.now() - timedelta(minutes=50))
        km = self.killmail(time=timezone.now() - timedelta(minutes=10))
        preview = self.prepare(km)
        self.assertEqual(preview.scope, OUT_OF_FLEET)
        self.assertEqual(preview.suggested_link, link)

    def test_no_suggestion_for_a_closed_link_or_another_time(self):
        operation, link = self.fleet(started_at=timezone.now() - timedelta(minutes=50))
        km = self.killmail(time=timezone.now() - timedelta(days=2))
        self.assertIsNone(self.prepare(km).suggested_link)
        link.is_open = False
        link.save()
        self.assertIsNone(self.prepare(self.killmail(time=timezone.now() - timedelta(minutes=10))).suggested_link)

    def test_in_fleet_preview_offers_no_other_fleet(self):
        _operation, link = self.fleet()
        self.assertIsNone(self.prepare(self.killmail(), link=link).suggested_link)


class PreviewValueRefreshTests(ClaimBase):
    def old_killmail(self, **kwargs):
        kwargs.setdefault("time", timezone.now() - timedelta(minutes=30))
        kwargs.setdefault("fitted", None)
        kwargs.setdefault("zkill_status", Zkill.PENDING)
        return self.killmail(**kwargs)

    def refresh(self, km, **kwargs):
        with mock.patch.object(claims.srp_values, "refresh_zkill", **kwargs) as refresh:
            preview = self.prepare(km)
        return preview, refresh

    def test_value_is_looked_up_once_for_an_old_killmail_without_a_rule(self):
        km = self.old_killmail()

        def fill(killmail):
            SrpKillmail.objects.filter(pk=killmail.pk).update(
                zkill_status=Zkill.OK, zkill_fitted_value=Decimal("200000000")
            )
            return True

        preview, refresh = self.refresh(km, side_effect=fill)
        refresh.assert_called_once()
        self.assertEqual(preview.calculation.basis, "percentage")
        self.assertEqual(preview.calculation.suggested, 160_000_000)
        self.assertEqual(preview.warnings, [])

    def test_unchanged_value_keeps_the_pending_warning(self):
        preview, refresh = self.refresh(self.old_killmail(), return_value=False)
        refresh.assert_called_once()
        self.assertEqual(codes(preview.warnings), ["value_pending"])

    def test_young_killmails_are_not_looked_up(self):
        km = self.old_killmail(time=timezone.now() - timedelta(minutes=3))
        _preview, refresh = self.refresh(km, return_value=True)
        refresh.assert_not_called()

    def test_killmails_waiting_for_their_next_attempt_are_not_looked_up(self):
        km = self.old_killmail(zkill_next_attempt_at=timezone.now() + timedelta(hours=1))
        _preview, refresh = self.refresh(km, return_value=True)
        refresh.assert_not_called()

    def test_rule_claims_do_not_ask_zkillboard(self):
        f.payout_rule(MOA, 30_000_000)
        _preview, refresh = self.refresh(self.old_killmail(ship_type_id=MOA), return_value=True)
        refresh.assert_not_called()

    def test_killmails_with_a_value_are_not_looked_up(self):
        km = self.old_killmail(fitted="100000000", zkill_status=Zkill.OK)
        _preview, refresh = self.refresh(km, return_value=True)
        refresh.assert_not_called()

    def test_zero_percentage_does_not_ask_zkillboard(self):
        self.settings(percentage=Decimal("0"))
        _preview, refresh = self.refresh(self.old_killmail(), return_value=True)
        refresh.assert_not_called()

    def test_zkillboard_switched_off_is_not_asked(self):
        self.settings(zkillboard_enabled=False, value_source="esi_estimate")
        _preview, refresh = self.refresh(self.old_killmail(), return_value=True)
        refresh.assert_not_called()

    def test_a_failing_lookup_leaves_the_value_pending(self):
        preview, refresh = self.refresh(self.old_killmail(), side_effect=RuntimeError("down"))
        refresh.assert_called_once()
        self.assertEqual(codes(preview.warnings), ["value_pending"])
        self.assertTrue(preview.eligible)


class ThrottleTests(ClaimBase):
    def test_twenty_checks_are_allowed_and_the_next_is_not(self):
        km = self.killmail()
        for _ in range(claims.CHECK_LIMIT):
            self.prepare(km)
        with self.assertRaises(SrpError) as caught:
            self.prepare(km)
        self.assertEqual(caught.exception.code, "throttled")
        self.assertIn("Wait a few minutes", caught.exception.message)

    def test_other_users_have_their_own_counter(self):
        km = self.killmail()
        for _ in range(claims.CHECK_LIMIT):
            self.prepare(km)
        self.assertTrue(self.prepare(self.killmail(f.main_of(self.other)), user=self.other).eligible)

    def test_the_counter_expires(self):
        km = self.killmail()
        for _ in range(claims.CHECK_LIMIT):
            self.prepare(km)
        cache.delete(f"fleetops:srp:check:{self.user.pk}")
        self.assertTrue(self.prepare(km).eligible)

    def test_submitting_is_not_throttled(self):
        km = self.killmail()
        for _ in range(claims.CHECK_LIMIT):
            self.prepare(km)
        self.assertEqual(claims.submit_claim(self.user, self.token(km)).status, Status.PENDING)

    def test_throttle_is_skipped_when_the_cache_is_down(self):
        with mock.patch.object(claims.cache, "add", side_effect=ConnectionError("down")):
            self.assertTrue(self.prepare(self.killmail()).eligible)


class SubmitClaimTests(ClaimBase):
    def test_submit_creates_a_pending_claim_with_the_snapshots(self):
        self.settings(
            percentage=Decimal("80"),
            out_of_fleet_loss_cap=500_000_000,
            personal_monthly_cap=2_000_000_000,
            corporation_monthly_cap=9_000_000_000,
        )
        km = self.killmail(ship_type_id=MOA, ship_name="Moa", fitted="250000000", solar_system_name="Amamake")
        claim = self.submit(km, comment="  Lost to a gate camp  ")

        self.assertEqual(claim.status, Status.PENDING)
        self.assertEqual(claim.killmail, km)
        self.assertEqual(claim.scope, OUT_OF_FLEET)
        self.assertEqual(claim.user, self.user)
        self.assertEqual((claim.character_id, claim.character_name), (self.main.character_id, self.main.character_name))
        self.assertEqual(claim.main_character_id, self.main.character_id)
        self.assertEqual(claim.corporation_id, f.DEFAULT_CORP[0])
        self.assertEqual(claim.corporation_name, f.DEFAULT_CORP[1])
        self.assertEqual(claim.alliance_id, f.DEFAULT_ALLIANCE[0])
        self.assertEqual(claim.killmail_time, km.killmail_time)
        self.assertEqual((claim.ship_type_id, claim.ship_type_name), (MOA, "Moa"))
        self.assertEqual(claim.solar_system_name, "Amamake")
        self.assertEqual(claim.member_comment, "Lost to a gate camp")
        self.assertEqual(claim.loss_value, Decimal("250000000.00"))
        self.assertEqual(claim.value_source, "zkill_fitted")
        self.assertEqual(claim.payout_basis, "percentage")
        self.assertIsNone(claim.payout_rule)
        self.assertEqual((claim.base_amount, claim.suggested_amount, claim.limited_amount), (200_000_000,) * 3)
        self.assertFalse(claim.over_limit)
        self.assertTrue(claim.counts_personal and claim.counts_corporation)
        self.assertEqual(claim.fleet_check, display.NOT_APPLICABLE)
        self.assertIsNotNone(claim.fleet_checked_at)
        self.assertEqual(claim.warnings, [])
        self.assertIsNone(claim.approved_amount)
        self.assertFalse(claim.auto_rejected)
        self.assertEqual(claim.reference, f"SRP-{claim.pk}")

        policy = PolicySnapshot.from_json(claim.policy)
        self.assertEqual(policy.percentage, Decimal("80"))
        self.assertEqual(policy.out_of_fleet_loss_cap, 500_000_000)
        self.assertEqual(policy.personal_monthly_cap, 2_000_000_000)
        calculation = Calculation.from_json(claim.calculation)
        self.assertEqual(calculation.suggested, 200_000_000)
        self.assertEqual(calculation.limit("personal").cap, 2_000_000_000)
        self.assertEqual(calculation.limit("corporation").cap, 9_000_000_000)

    def test_payout_rule_claim_keeps_the_rule(self):
        rule = f.payout_rule(MOA, 60_000_000, label="Moa")
        claim = self.submit(self.killmail(ship_type_id=MOA))
        self.assertEqual(claim.payout_basis, "rule")
        self.assertEqual(claim.payout_rule, rule)
        self.assertEqual(claim.suggested_amount, 60_000_000)
        self.assertEqual(claim.policy["rule"]["amount"], 60_000_000)

    def test_later_rule_changes_do_not_touch_the_stored_claim(self):
        rule = f.payout_rule(MOA, 60_000_000)
        claim = self.submit(self.killmail(ship_type_id=MOA))
        rule.amount = 1
        rule.save()
        claim.refresh_from_db()
        self.assertEqual(claim.suggested_amount, 60_000_000)
        self.assertEqual(claim.policy["rule"]["amount"], 60_000_000)

    def test_submit_writes_the_event_and_the_audit_entry(self):
        claim = self.submit(self.killmail(), comment="please")
        event = claim.events.get()
        self.assertEqual(event.action, SrpClaimEvent.Action.SUBMIT)
        self.assertEqual((event.actor, event.from_status, event.to_status), (self.user, "", "pending"))
        self.assertEqual(event.amount, claim.suggested_amount)
        self.assertFalse(event.internal)
        self.assertEqual(event.data["scope"], OUT_OF_FLEET)
        entry = AuditLog.objects.get(action="srp.claim_submit")
        self.assertEqual((entry.actor, entry.object_type, entry.object_id), (self.user, "SrpClaim", str(claim.pk)))
        self.assertEqual(entry.new_value["status"], "pending")
        self.assertEqual(entry.new_value["killmail_id"], claim.killmail_id)

    def test_corporation_snapshot_is_the_mains_not_the_victims(self):
        alt = f.add_alt(self.user, "Alt In Another Corp", corporation=f.OTHER_CORP)
        claim = self.submit(self.killmail(alt))
        self.assertEqual(claim.character_id, alt.character_id)
        self.assertEqual(claim.main_character_id, self.main.character_id)
        self.assertEqual(claim.corporation_id, f.DEFAULT_CORP[0])
        self.assertEqual(claim.corporation_ticker, self.main.corporation_ticker)

    def test_corporation_stays_when_the_main_moves_later(self):
        claim = self.submit(self.killmail())
        self.main.corporation_id, self.main.corporation_name = f.OTHER_CORP
        self.main.save()
        claim.refresh_from_db()
        self.assertEqual(claim.corporation_id, f.DEFAULT_CORP[0])
        # A recalculation keeps the corporation of the claim as well.
        review.recalculate_claim(claim, actor=self.manager)
        claim.refresh_from_db()
        self.assertEqual(claim.corporation_id, f.DEFAULT_CORP[0])

    def test_corporation_limit_of_the_snapshot_corporation_applies(self):
        self.settings(corporation_monthly_cap=900_000_000)
        SrpCorporationLimit.objects.create(corporation_id=f.DEFAULT_CORP[0], monthly_cap=50_000_000)
        claim = self.submit(self.killmail(fitted="200000000"))
        self.assertTrue(claim.over_limit)
        self.assertEqual(claim.limited_amount, 50_000_000)
        self.assertEqual(claim.policy["corporation_cap_source"], "override")
        self.assertEqual(claim.policy["corporation_monthly_cap"], 50_000_000)

    def test_in_fleet_claim_names_the_fleet_and_its_fc(self):
        operation, link = self.fleet(doctrine_name="Moa Fleet")
        km = self.killmail(time=timezone.now() - timedelta(minutes=2))
        claim = self.submit(km, link=link)
        self.assertEqual(claim.scope, IN_FLEET)
        self.assertEqual((claim.link, claim.operation), (link, operation))
        self.assertEqual(claim.fc_user, self.fc)
        self.assertEqual(claim.fc_main_character_name, f.main_of(self.fc).character_name)
        self.assertTrue(claim.fleet_label.startswith("StratOp — "))
        self.assertTrue(claim.fleet_label.endswith(" EVE"))
        self.assertIn(f.main_of(self.fc).character_name, claim.fleet_label)
        self.assertEqual(claim.fleet_check, display.IN_FLEET)
        self.assertEqual(claim.warnings, [])

    def test_out_of_fleet_claim_has_no_fleet_fields(self):
        claim = self.submit(self.killmail())
        self.assertEqual((claim.link, claim.operation, claim.fc_user, claim.fleet_label), (None, None, None, ""))

    def test_flags_follow_the_scope_and_the_switches(self):
        self.settings(personal_cap_includes_in_fleet=False, corporation_cap_includes_in_fleet=True)
        out_claim = self.submit(self.killmail())
        _operation, link = self.fleet()
        in_claim = self.submit(self.killmail(), link=link)
        self.assertEqual((out_claim.counts_personal, out_claim.counts_corporation), (True, True))
        self.assertEqual((in_claim.counts_personal, in_claim.counts_corporation), (False, True))
        self.assertFalse(in_claim.policy["personal_cap_includes_in_fleet"])

    def test_in_fleet_loss_not_counted_in_the_personal_limit_is_not_limited_by_it(self):
        self.settings(personal_monthly_cap=10_000_000, personal_cap_includes_in_fleet=False)
        _operation, link = self.fleet()
        in_claim = self.submit(self.killmail(), link=link)
        self.assertFalse(in_claim.over_limit)
        out_claim = self.submit(self.killmail())
        self.assertTrue(out_claim.over_limit)
        self.assertEqual(out_claim.limited_amount, 10_000_000)

    def test_in_fleet_loss_not_counted_in_the_corporation_limit_is_not_limited_by_it(self):
        self.settings(corporation_monthly_cap=100_000_000, corporation_cap_includes_in_fleet=False)
        f.srp_claim(self.other, amount=100_000_000, status=Status.APPROVED)
        _operation, link = self.fleet()
        in_claim = self.submit(self.killmail(), link=link)
        self.assertFalse(in_claim.over_limit)
        self.assertEqual((in_claim.limited_amount, in_claim.counts_corporation), (80_000_000, False))
        out_claim = self.submit(self.killmail())
        self.assertTrue(out_claim.over_limit)
        self.assertEqual(out_claim.limited_amount, 0)

    def test_in_fleet_loss_counted_in_the_corporation_limit_is_limited_by_it(self):
        self.settings(corporation_monthly_cap=100_000_000, corporation_cap_includes_in_fleet=True)
        f.srp_claim(self.other, amount=100_000_000, status=Status.APPROVED)
        _operation, link = self.fleet()
        in_claim = self.submit(self.killmail(), link=link)
        self.assertTrue(in_claim.over_limit)
        self.assertEqual((in_claim.limited_amount, in_claim.counts_corporation), (0, True))

    def test_reject_mode_does_not_reject_an_in_fleet_loss_the_corporation_limit_does_not_count(self):
        self.settings(
            corporation_monthly_cap=100_000_000, corporation_cap_includes_in_fleet=False, over_limit_action="reject"
        )
        f.srp_claim(self.other, amount=100_000_000, status=Status.APPROVED)
        _operation, link = self.fleet()
        self.assertEqual(self.submit(self.killmail(), link=link).status, Status.PENDING)
        self.assertEqual(self.submit(self.killmail()).status, Status.REJECTED)

    def test_presence_is_stored_with_its_warnings(self):
        _operation, link = self.fleet(member=False)
        claim = self.submit(self.killmail(), link=link)
        self.assertEqual(claim.fleet_check, display.NEVER_SEEN)
        self.assertEqual(stored_codes(claim), [display.NEVER_SEEN])
        self.assertEqual(claim.warnings[0]["level"], display.LEVEL_DANGER)

    def test_external_claims_are_stored_as_a_warning(self):
        km = self.killmail()
        with mock.patch.object(claims.legacy, "external_claims", return_value=["Alliance Auth SRP: Approved"]):
            claim = self.submit(km)
        self.assertEqual(stored_codes(claim), ["external_claim"])
        self.assertEqual(claim.warnings[0]["params"], {"claims": ["Alliance Auth SRP: Approved"]})

    def test_value_pending_is_stored_as_a_warning(self):
        claim = self.submit(self.killmail(fitted=None, zkill_status=Zkill.PENDING))
        self.assertEqual((claim.payout_basis, claim.suggested_amount), ("none", 0))
        self.assertEqual(stored_codes(claim), ["value_pending"])
        self.assertIsNone(claim.loss_value)

    def test_a_value_stored_between_the_preview_and_the_lock_is_used(self):
        # The value sweep stores the zKillboard value after the killmail was read and before the claim exists:
        # nothing recalculates a claim made from the old row afterwards.
        km = self.killmail(fitted=None, zkill_status=Zkill.PENDING)
        token = self.token(km)
        presence = claims.loss_presence

        def sweep_stores_the_value(*args, **kwargs):
            SrpKillmail.objects.filter(pk=km.pk).update(
                zkill_status=Zkill.OK, zkill_fitted_value=Decimal("100000000")
            )
            return presence(*args, **kwargs)

        with mock.patch.object(claims, "loss_presence", side_effect=sweep_stores_the_value):
            claim = claims.submit_claim(self.user, token)

        claim.refresh_from_db()
        self.assertEqual((claim.payout_basis, claim.suggested_amount), ("percentage", 80_000_000))
        self.assertEqual(claim.loss_value, Decimal("100000000.00"))
        self.assertEqual(stored_codes(claim), [])

    def test_comment_may_be_empty_and_has_a_limit(self):
        self.assertEqual(self.submit(self.killmail()).member_comment, "")
        km = self.killmail()
        with self.assertRaises(SrpError) as caught:
            claims.submit_claim(self.user, self.token(km), comment="x" * 1001)
        self.assertEqual(caught.exception.code, "comment_too_long")
        self.assertFalse(SrpClaim.objects.filter(killmail=km).exists())

    def test_no_notification_for_a_normal_submission(self):
        with self.captureOnCommitCallbacks(execute=True):
            self.submit(self.killmail())
        self.assertFalse(Notification.objects.exists())


class SubmitRefusalTests(ClaimBase):
    def test_tampered_token(self):
        km = self.killmail()
        token = self.token(km)
        for bad in (token + "x", token[:-3], "garbage", "", None):
            with self.subTest(token=bad):
                with self.assertRaises(SrpError) as caught:
                    claims.submit_claim(self.user, bad)
                self.assertEqual(caught.exception.code, "invalid_token")
        self.assertFalse(SrpClaim.objects.exists())

    def test_token_signed_with_another_salt(self):
        km = self.killmail()
        token = signing.dumps({"km": km.pk, "link": None, "u": self.user.pk}, salt="something.else")
        with self.assertRaises(SrpError):
            claims.submit_claim(self.user, token)

    def test_expired_token(self):
        km = self.killmail()
        token = self.token(km)
        later = time.time() + claims.PREVIEW_MAX_AGE + 5
        with mock.patch("django.core.signing.time.time", return_value=later):
            with self.assertRaises(SrpError) as caught:
                claims.submit_claim(self.user, token)
        self.assertEqual(caught.exception.code, "invalid_token")
        self.assertIn("expired", caught.exception.message)

    def test_token_still_works_just_before_it_expires(self):
        km = self.killmail()
        token = self.token(km)
        later = time.time() + claims.PREVIEW_MAX_AGE - 30
        with mock.patch("django.core.signing.time.time", return_value=later):
            self.assertEqual(claims.submit_claim(self.user, token).status, Status.PENDING)

    def test_token_of_another_user(self):
        km = self.killmail()
        token = self.token(km, user=self.other)
        with self.assertRaises(SrpError) as caught:
            claims.submit_claim(self.user, token)
        self.assertEqual(caught.exception.code, "invalid_token")
        self.assertFalse(SrpClaim.objects.exists())

    def test_token_with_unexpected_content(self):
        token = signing.dumps(["not", "a", "dict"], salt=claims.PREVIEW_SALT)
        with self.assertRaises(SrpError):
            claims.submit_claim(self.user, token)

    def test_killmail_that_is_gone(self):
        km = self.killmail()
        token = self.token(km)
        km.delete()
        with self.assertRaises(SrpError) as caught:
            claims.submit_claim(self.user, token)
        self.assertEqual(caught.exception.code, "expired")

    def test_link_that_is_gone(self):
        _operation, link = self.fleet()
        token = self.token(self.killmail(), link=link)
        link.delete()
        with self.assertRaises(SrpError) as caught:
            claims.submit_claim(self.user, token)
        self.assertEqual(caught.exception.code, "expired")

    def test_eligibility_is_checked_again(self):
        km = self.killmail()
        token = self.token(km)
        self.settings(enabled=False)
        with self.assertRaises(SrpError) as caught:
            claims.submit_claim(self.user, token)
        self.assertEqual(caught.exception.code, "not_eligible")
        self.assertEqual(caught.exception.message, "FleetOps SRP is not enabled.")

    def test_link_closed_after_the_preview(self):
        _operation, link = self.fleet()
        token = self.token(self.killmail(), link=link)
        link.is_open = False
        link.save()
        with self.assertRaises(SrpError) as caught:
            claims.submit_claim(self.user, token)
        self.assertEqual(caught.exception.message, "This SRP link is closed.")

    def test_character_moved_to_another_account_after_the_preview(self):
        km = self.killmail()
        token = self.token(km)
        CharacterOwnership.objects.filter(character=self.main).delete()
        with self.assertRaises(SrpError) as caught:
            claims.submit_claim(self.user, token)
        self.assertEqual(caught.exception.code, "not_eligible")

    def test_second_submission_of_the_same_killmail(self):
        km = self.killmail()
        first = self.submit(km)
        with self.assertRaises(SrpDuplicateError) as caught:
            claims.submit_claim(self.user, self.token(km))
        self.assertEqual(caught.exception.claim, first)
        self.assertEqual(caught.exception.code, "duplicate")
        self.assertEqual(caught.exception.message, "You already submitted this loss.")
        self.assertEqual(SrpClaim.objects.filter(killmail=km).count(), 1)

    def test_killmail_claimed_by_another_user_in_the_meantime(self):
        km = self.killmail()
        token = self.token(km)
        f.srp_claim(self.other, killmail=km, character=self.main)
        with self.assertRaises(SrpDuplicateError) as caught:
            claims.submit_claim(self.user, token)
        self.assertEqual(caught.exception.message, "This loss was already claimed by another pilot.")

    def test_database_refuses_a_second_claim_when_the_check_missed_it(self):
        km = self.killmail()
        first = self.submit(km)
        real = claims._duplicate
        answers = iter([None])

        def miss_once(user, killmail):
            try:
                return next(answers)
            except StopIteration:
                return real(user, killmail)

        with mock.patch.object(claims, "_duplicate", side_effect=miss_once):
            with self.assertRaises(SrpDuplicateError) as caught:
                claims.submit_claim(self.user, self.token(km))
        self.assertEqual(caught.exception.claim, first)
        self.assertEqual(SrpClaim.objects.filter(killmail=km).count(), 1)

    def test_other_database_errors_are_not_hidden(self):
        km = self.killmail()
        with mock.patch.object(SrpClaim, "save", side_effect=IntegrityError("something else")):
            with self.assertRaises(IntegrityError):
                claims.submit_claim(self.user, self.token(km))


class OverLimitSubmissionTests(ClaimBase):
    """The monthly limits at submission, and what the policy does with a claim that does not fit."""

    def used(self, amount, user=None, **extra):
        return f.srp_claim(user or self.user, amount=amount, status=Status.APPROVED, **extra)

    def test_review_mode_sends_an_over_limit_claim_to_review_with_a_warning(self):
        self.settings(personal_monthly_cap=150_000_000, over_limit_action="review")
        self.used(100_000_000)
        claim = self.submit(self.killmail(fitted="125000000"))
        self.assertEqual(claim.status, Status.PENDING)
        self.assertTrue(claim.over_limit)
        self.assertEqual((claim.suggested_amount, claim.limited_amount), (100_000_000, 50_000_000))
        self.assertEqual(stored_codes(claim), ["over_limit"])
        self.assertEqual(claim.warnings[0]["level"], "danger")
        self.assertFalse(claim.auto_rejected)

    def test_review_mode_with_nothing_left_is_still_reviewed(self):
        self.settings(personal_monthly_cap=100_000_000, over_limit_action="review")
        self.used(100_000_000)
        claim = self.submit(self.killmail(fitted="125000000"))
        self.assertEqual((claim.status, claim.limited_amount), (Status.PENDING, 0))
        self.assertEqual(stored_codes(claim), ["over_limit"])

    def test_reject_mode_cuts_a_partly_fitting_claim_and_sends_it_to_review(self):
        self.settings(personal_monthly_cap=150_000_000, over_limit_action="reject")
        self.used(100_000_000)
        claim = self.submit(self.killmail(fitted="125000000"))
        self.assertEqual(claim.status, Status.PENDING)
        self.assertFalse(claim.auto_rejected)
        self.assertTrue(claim.over_limit)
        self.assertEqual((claim.suggested_amount, claim.limited_amount), (100_000_000, 50_000_000))
        self.assertEqual(stored_codes(claim), ["reduced_by_limit"])
        self.assertEqual(claim.warnings[0]["level"], "warning")
        self.assertEqual(claim.warnings[0]["params"]["remaining"], 50_000_000)
        self.assertEqual(claim.events.get().action, SrpClaimEvent.Action.SUBMIT)

    def test_reject_mode_rejects_a_claim_when_nothing_is_left(self):
        self.settings(personal_monthly_cap=100_000_000, over_limit_action="reject")
        self.used(100_000_000)
        with self.captureOnCommitCallbacks(execute=True):
            claim = self.submit(self.killmail(fitted="125000000"))
        self.assertEqual(claim.status, Status.REJECTED)
        self.assertTrue(claim.auto_rejected)
        self.assertIsNone(claim.approved_amount)
        self.assertEqual(
            claim.decision_reason,
            "Rejected automatically: the monthly personal limit is used up (cap 100,000,000 ISK).",
        )
        self.assertIsNone(claim.decided_by)
        self.assertIsNotNone(claim.decided_at)
        event = claim.events.get()
        self.assertEqual(event.action, SrpClaimEvent.Action.AUTO_REJECT)
        self.assertEqual((event.from_status, event.to_status, event.reason), ("", "rejected", claim.decision_reason))
        self.assertEqual(event.data["auto_reject"], {"code": "limit_used_up", "kind": "personal", "cap": 100_000_000})
        self.assertTrue(AuditLog.objects.filter(action="srp.claim_auto_reject", object_id=str(claim.pk)).exists())
        notification = Notification.objects.get()
        self.assertEqual(notification.user, self.user)
        self.assertEqual(notification.level, "danger")
        self.assertIn("SRP rejected", notification.title)
        self.assertIn("used up", notification.message)

    def test_corporation_limit_is_named_in_the_automatic_rejection(self):
        self.settings(corporation_monthly_cap=100_000_000, over_limit_action="reject")
        self.used(100_000_000, user=self.other)
        claim = self.submit(self.killmail(fitted="125000000"))
        self.assertTrue(claim.auto_rejected)
        self.assertIn("monthly corporation limit", claim.decision_reason)

    def test_auto_rejection_does_not_use_up_the_limit(self):
        self.settings(personal_monthly_cap=100_000_000, over_limit_action="reject")
        self.used(100_000_000)
        claim = self.submit(self.killmail(fitted="125000000"))
        self.assertEqual(claim.status, Status.REJECTED)
        year, month = quota.claim_month(claim)
        self.assertEqual(quota.month_usage(year=year, month=month, user_id=self.user.pk)[0], 100_000_000)

    def test_notifications_can_be_switched_off(self):
        self.settings(personal_monthly_cap=100_000_000, over_limit_action="reject", notify_members=False)
        self.used(100_000_000)
        with self.captureOnCommitCallbacks(execute=True):
            self.submit(self.killmail(fitted="125000000"))
        self.assertFalse(Notification.objects.exists())

    def test_most_restrictive_limit_wins(self):
        self.settings(personal_monthly_cap=1_000_000_000, corporation_monthly_cap=300_000_000)
        self.used(250_000_000, user=self.other)
        claim = self.submit(self.killmail(fitted="125000000"))
        self.assertEqual(claim.limited_amount, 50_000_000)
        self.assertEqual(Calculation.from_json(claim.calculation).binding_limit, "corporation")
        self.assertEqual(claim.warnings[0]["params"]["kind"], "corporation")

    def test_loss_cap_and_monthly_limit_together(self):
        self.settings(out_of_fleet_loss_cap=90_000_000, personal_monthly_cap=120_000_000)
        self.used(60_000_000)
        claim = self.submit(self.killmail(fitted="125000000"))
        self.assertEqual(
            (claim.base_amount, claim.suggested_amount, claim.limited_amount), (100_000_000, 90_000_000, 60_000_000)
        )

    def test_usage_of_another_month_does_not_count(self):
        self.settings(personal_monthly_cap=120_000_000)
        self.used(120_000_000, time=timezone.now() - timedelta(days=75))
        claim = self.submit(self.killmail(fitted="125000000"))
        self.assertFalse(claim.over_limit)

    def test_limit_counts_by_the_month_of_the_loss(self):
        self.settings(personal_monthly_cap=120_000_000)
        old_loss = timezone.now() - timedelta(days=75)
        self.used(120_000_000, time=old_loss)
        claim = self.submit(self.killmail(fitted="125000000", time=old_loss + timedelta(hours=1)))
        self.assertTrue(claim.over_limit)
        self.assertEqual(claim.limited_amount, 0)

    def test_pending_claims_do_not_use_up_the_limit(self):
        self.settings(personal_monthly_cap=120_000_000)
        f.srp_claim(self.user, amount=100_000_000, status=Status.PENDING)
        claim = self.submit(self.killmail(fitted="125000000"))
        self.assertFalse(claim.over_limit)

    def test_revoked_approval_frees_the_limit_for_the_next_claim(self):
        self.settings(personal_monthly_cap=120_000_000)
        used = self.used(100_000_000)
        self.assertTrue(self.submit(self.killmail(fitted="125000000")).over_limit)
        SrpClaim.objects.filter(pk=used.pk).update(status=Status.PENDING, approved_amount=None)
        self.assertFalse(self.submit(self.killmail(fitted="125000000")).over_limit)


class WithdrawTests(ClaimBase):
    def setUp(self):
        super().setUp()
        self.claim = self.submit(self.killmail())

    def test_claimant_withdraws_a_pending_claim(self):
        result = claims.withdraw_claim(self.claim, actor=self.user, reason=" Wrong ship ")
        self.assertIs(result, self.claim)
        self.assertEqual(self.claim.status, Status.WITHDRAWN)
        stored = SrpClaim.objects.get(pk=self.claim.pk)
        self.assertEqual(stored.status, Status.WITHDRAWN)
        event = stored.events.get(action=SrpClaimEvent.Action.WITHDRAW)
        self.assertEqual(
            (event.actor, event.from_status, event.to_status, event.reason),
            (self.user, "pending", "withdrawn", "Wrong ship"),
        )
        self.assertTrue(AuditLog.objects.filter(action="srp.claim_withdraw", actor=self.user).exists())

    def test_only_the_claimant_can_withdraw(self):
        for actor in (self.other, self.manager, None):
            with self.subTest(actor=actor):
                with self.assertRaises(SrpError) as caught:
                    claims.withdraw_claim(self.claim, actor=actor)
                self.assertEqual(caught.exception.code, "forbidden")
        self.assertEqual(SrpClaim.objects.get(pk=self.claim.pk).status, Status.PENDING)

    def test_withdrawing_twice(self):
        claims.withdraw_claim(self.claim, actor=self.user)
        with self.assertRaises(SrpStateError) as caught:
            claims.withdraw_claim(SrpClaim.objects.get(pk=self.claim.pk), actor=self.user)
        self.assertEqual(caught.exception.message, "This claim is already withdrawn.")

    def test_decided_claims_cannot_be_withdrawn(self):
        for status in (Status.APPROVED, Status.PAID, Status.REJECTED):
            claim = f.srp_claim(self.user, status=status)
            with self.subTest(status=status):
                with self.assertRaises(SrpStateError) as caught:
                    claims.withdraw_claim(claim, actor=self.user)
                self.assertEqual(caught.exception.message, "Only pending claims can be withdrawn.")
                claim.refresh_from_db()
                self.assertEqual(claim.status, status)

    def test_no_notification_for_a_withdrawal(self):
        with self.captureOnCommitCallbacks(execute=True):
            claims.withdraw_claim(self.claim, actor=self.user)
        self.assertFalse(Notification.objects.exists())

    def test_reason_has_a_limit(self):
        with self.assertRaises(SrpError):
            claims.withdraw_claim(self.claim, actor=self.user, reason="x" * 1001)


class ResubmitTests(ClaimBase):
    def withdrawn(self, **kwargs):
        claim = self.submit(self.killmail(**kwargs))
        claims.withdraw_claim(claim, actor=self.user)
        return claim

    def test_resubmit_makes_the_claim_pending_again(self):
        claim = self.withdrawn()
        before = claim.submitted_at
        result = claims.resubmit_claim(claim, actor=self.user)
        self.assertIs(result, claim)
        stored = SrpClaim.objects.get(pk=claim.pk)
        self.assertEqual(stored.status, Status.PENDING)
        self.assertGreater(stored.submitted_at, before)
        event = stored.events.get(action=SrpClaimEvent.Action.RESUBMIT)
        self.assertEqual((event.actor, event.from_status, event.to_status), (self.user, "withdrawn", "pending"))
        self.assertTrue(AuditLog.objects.filter(action="srp.claim_resubmit").exists())

    def test_the_claim_is_calculated_again_with_the_current_policy(self):
        claim = self.withdrawn(fitted="100000000")
        self.assertEqual(claim.suggested_amount, 80_000_000)
        self.settings(percentage=Decimal("50"), out_of_fleet_loss_cap=30_000_000)
        claims.resubmit_claim(claim, actor=self.user)
        claim.refresh_from_db()
        self.assertEqual((claim.base_amount, claim.suggested_amount), (50_000_000, 30_000_000))
        self.assertEqual(claim.policy["percentage"], "50.00")
        self.assertEqual(claim.policy["out_of_fleet_loss_cap"], 30_000_000)

    def test_a_new_limit_applies_when_submitting_again(self):
        claim = self.withdrawn()
        self.settings(personal_monthly_cap=100_000_000)
        f.srp_claim(self.user, amount=70_000_000, status=Status.APPROVED)
        claims.resubmit_claim(claim, actor=self.user)
        claim.refresh_from_db()
        self.assertTrue(claim.over_limit)
        self.assertEqual(claim.limited_amount, 30_000_000)
        self.assertEqual(stored_codes(claim), ["over_limit"])

    def test_old_calculation_warnings_are_replaced_and_others_stay(self):
        claim = self.withdrawn()
        SrpClaim.objects.filter(pk=claim.pk).update(
            warnings=[
                {"code": "over_limit", "level": "danger", "params": {}},
                {"code": "external_claim", "level": "warning", "params": {"claims": ["aa-srp: Pending"]}},
            ]
        )
        claims.resubmit_claim(claim, actor=self.user)
        claim.refresh_from_db()
        self.assertEqual(stored_codes(claim), ["external_claim"])

    def test_reject_mode_can_reject_the_claim_again(self):
        claim = self.withdrawn()
        self.settings(personal_monthly_cap=70_000_000, over_limit_action="reject")
        f.srp_claim(self.user, amount=70_000_000, status=Status.APPROVED)
        with self.captureOnCommitCallbacks(execute=True):
            claims.resubmit_claim(claim, actor=self.user)
        claim.refresh_from_db()
        self.assertEqual((claim.status, claim.auto_rejected), (Status.REJECTED, True))
        self.assertTrue(claim.events.filter(action=SrpClaimEvent.Action.AUTO_REJECT).exists())
        self.assertEqual(Notification.objects.count(), 1)

    def test_only_the_claimant_can_resubmit(self):
        claim = self.withdrawn()
        for actor in (self.other, self.manager, None):
            with self.subTest(actor=actor):
                with self.assertRaises(SrpError) as caught:
                    claims.resubmit_claim(claim, actor=actor)
                self.assertEqual(caught.exception.code, "forbidden")

    def test_only_withdrawn_claims_can_be_submitted_again(self):
        pending = self.submit(self.killmail())
        with self.assertRaises(SrpStateError) as caught:
            claims.resubmit_claim(pending, actor=self.user)
        self.assertEqual(caught.exception.message, "This claim is already pending.")
        for status in (Status.APPROVED, Status.PAID, Status.REJECTED):
            claim = f.srp_claim(self.user, status=status)
            with self.subTest(status=status):
                with self.assertRaises(SrpStateError) as caught:
                    claims.resubmit_claim(claim, actor=self.user)
                self.assertEqual(caught.exception.message, "Only withdrawn claims can be submitted again.")

    def test_not_when_srp_is_switched_off(self):
        claim = self.withdrawn()
        self.settings(enabled=False)
        with self.assertRaises(SrpError) as caught:
            claims.resubmit_claim(claim, actor=self.user)
        self.assertEqual(caught.exception.code, "not_eligible")

    def test_not_after_the_deadline(self):
        claim = self.withdrawn(time=timezone.now() - timedelta(days=10))
        self.settings(claim_deadline_days=5)
        with self.assertRaises(SrpError) as caught:
            claims.resubmit_claim(claim, actor=self.user)
        self.assertIn("older than 5 days", caught.exception.message)

    def test_a_closed_link_does_not_stop_a_claim_that_exists(self):
        _operation, link = self.fleet()
        claim = self.submit(self.killmail(), link=link)
        claims.withdraw_claim(claim, actor=self.user)
        link.is_open = False
        link.save()
        claims.resubmit_claim(claim, actor=self.user)
        claim.refresh_from_db()
        self.assertEqual((claim.status, claim.scope, claim.link), (Status.PENDING, IN_FLEET, link))

    def test_a_doctrine_capsule_rule_still_covers_a_withdrawn_claim_after_its_link_closes(self):
        f.payout_rule(f.CAPSULE, 20_000_000, doctrine="Ferox Fleet")
        _operation, link = self.fleet(doctrine_name="Ferox Fleet")
        claim = self.submit(self.killmail(ship_type_id=f.CAPSULE), link=link)
        self.assertEqual((claim.payout_basis, claim.suggested_amount), ("rule", 20_000_000))
        claims.withdraw_claim(claim, actor=self.user)
        link.is_open = False
        link.save()

        claims.resubmit_claim(claim, actor=self.user)

        claim.refresh_from_db()
        self.assertEqual((claim.status, claim.payout_basis, claim.suggested_amount), (Status.PENDING, "rule", 20_000_000))

    def test_a_withdrawn_capsule_claim_without_a_rule_for_its_fleet_stays_blocked(self):
        f.payout_rule(f.CAPSULE, 20_000_000, doctrine="Ferox Fleet")
        _operation, link = self.fleet(doctrine_name="Ferox Fleet")
        claim = self.submit(self.killmail(ship_type_id=f.CAPSULE), link=link)
        claims.withdraw_claim(claim, actor=self.user)
        link.is_open = False
        link.save()
        SrpPayoutRule.objects.all().delete()

        with self.assertRaises(SrpError) as caught:
            claims.resubmit_claim(claim, actor=self.user)

        self.assertEqual(caught.exception.message, "Capsule losses are not covered.")

    def test_in_fleet_claim_is_calculated_with_its_fleets_doctrine(self):
        _operation, link = self.fleet(doctrine_name="Moa Fleet")
        claim = self.submit(self.killmail(), link=link)
        claims.withdraw_claim(claim, actor=self.user)
        f.payout_rule(0, 75_000_000, doctrine="Moa Fleet")
        claims.resubmit_claim(claim, actor=self.user)
        claim.refresh_from_db()
        self.assertEqual((claim.payout_basis, claim.suggested_amount), ("rule", 75_000_000))


class ValueUpdateTests(ClaimBase):
    def waiting(self):
        km = self.killmail(fitted=None, zkill_status=Zkill.PENDING)
        return km, self.submit(km)

    def value_arrives(self, km, fitted="200000000"):
        SrpKillmail.objects.filter(pk=km.pk).update(zkill_status=Zkill.OK, zkill_fitted_value=Decimal(fitted))
        return SrpKillmail.objects.get(pk=km.pk)

    def test_pending_claim_without_a_value_is_calculated_when_it_arrives(self):
        km, claim = self.waiting()
        self.assertEqual(claim.suggested_amount, 0)
        claims.on_value_updated(self.value_arrives(km))
        claim.refresh_from_db()
        self.assertEqual(claim.status, Status.PENDING)
        self.assertEqual(claim.payout_basis, "percentage")
        self.assertEqual((claim.suggested_amount, claim.limited_amount), (160_000_000, 160_000_000))
        self.assertEqual(claim.loss_value, Decimal("200000000.00"))
        self.assertEqual(claim.value_source, "zkill_fitted")
        self.assertEqual(claim.warnings, [])
        event = claim.events.get(action=SrpClaimEvent.Action.VALUE_UPDATE)
        self.assertIsNone(event.actor)
        self.assertEqual(event.reason, "Loss value from zKillboard")
        self.assertEqual(event.data["previous"]["suggested_amount"], 0)
        self.assertTrue(AuditLog.objects.filter(action="srp.claim_recalculate", object_id=str(claim.pk)).exists())

    def test_the_new_value_meets_the_limits(self):
        km, claim = self.waiting()
        self.settings(personal_monthly_cap=100_000_000)
        claims.on_value_updated(self.value_arrives(km))
        claim.refresh_from_db()
        self.assertTrue(claim.over_limit)
        self.assertEqual((claim.suggested_amount, claim.limited_amount), (160_000_000, 100_000_000))
        self.assertEqual(stored_codes(claim), ["over_limit"])

    def test_claim_paid_from_the_payout_table_only_shows_the_value(self):
        f.payout_rule(MOA, 30_000_000)
        km = self.killmail(fitted=None, ship_type_id=MOA, zkill_status=Zkill.PENDING)
        claim = self.submit(km)
        self.assertIsNone(claim.loss_value)
        claims.on_value_updated(self.value_arrives(km))
        claim.refresh_from_db()
        self.assertEqual((claim.payout_basis, claim.suggested_amount), ("rule", 30_000_000))
        self.assertEqual(claim.loss_value, Decimal("200000000.00"))
        self.assertEqual(claim.value_source, "zkill_fitted")
        self.assertEqual(claim.calculation["loss_value"], "200000000.00")
        self.assertEqual(claim.events.filter(action=SrpClaimEvent.Action.VALUE_UPDATE).count(), 1)

    def test_nothing_happens_when_the_shown_value_is_the_same(self):
        f.payout_rule(MOA, 30_000_000)
        km = self.killmail(fitted="200000000", ship_type_id=MOA)
        claim = self.submit(km)
        claims.on_value_updated(km)
        self.assertFalse(claim.events.filter(action=SrpClaimEvent.Action.VALUE_UPDATE).exists())

    def test_decided_claims_are_never_changed(self):
        for status in (Status.APPROVED, Status.PAID, Status.REJECTED, Status.WITHDRAWN):
            km = self.killmail(fitted="200000000")
            claim = f.srp_claim(self.user, killmail=km, status=status, amount=5)
            before = SrpClaim.objects.filter(pk=claim.pk).values().get()
            with self.subTest(status=status):
                claims.on_value_updated(km)
                self.assertEqual(SrpClaim.objects.filter(pk=claim.pk).values().get(), before)
                self.assertFalse(claim.events.exists())

    def test_killmail_without_a_claim(self):
        claims.on_value_updated(self.killmail())

    def test_claim_decided_while_the_value_was_looked_up(self):
        km, _claim = self.waiting()
        error = SrpStateError("already", "This claim is already approved.")
        with mock.patch.object(claims, "recalculate_under_lock", side_effect=error):
            claims.on_value_updated(self.value_arrives(km))

    def test_amount_changes_with_each_value(self):
        km, claim = self.waiting()
        claims.on_value_updated(self.value_arrives(km, "200000000"))
        claims.on_value_updated(self.value_arrives(km, "100000000"))
        claim.refresh_from_db()
        self.assertEqual(claim.suggested_amount, 80_000_000)
        self.assertEqual(claim.events.filter(action=SrpClaimEvent.Action.VALUE_UPDATE).count(), 2)
