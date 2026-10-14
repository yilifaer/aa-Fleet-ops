# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Deciding on SRP claims: every transition and the states it is refused from, the reason and confirmation
rules, the monthly limits at approval, self review, bulk actions, events, audit entries and notifications."""

import logging
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from allianceauth.notifications.models import Notification
from django.core import signing
from django.test import TestCase
from django.utils import timezone

from fleetops.constants import MAX_ISK
from fleetops.models import AuditLog, FleetMemberEvent, SrpClaim, SrpClaimEvent, SrpKillmail, SrpSettings
from fleetops.srp import claims, display, notify, quota, review
from fleetops.srp.types import (
    Calculation,
    SrpError,
    SrpLimitError,
    SrpStateError,
    warnings_from_json,
)

from . import factories as f

Status = SrpClaim.Status
Action = SrpClaimEvent.Action
IN_FLEET = SrpClaim.Scope.IN_FLEET
OUT_OF_FLEET = SrpClaim.Scope.OUT_OF_FLEET

MOA = 623


def stored_codes(claim):
    return [warning.code for warning in warnings_from_json(claim.warnings)]


def fresh(claim):
    return SrpClaim.objects.get(pk=claim.pk)


class ReviewBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.srp = f.srp_settings(percentage=Decimal("80"))
        cls.user = f.create_user("pilot", perms=f.SRP_MEMBER_PERMS)
        cls.other = f.create_user("other-pilot", perms=f.SRP_MEMBER_PERMS, corporation=f.OTHER_CORP)
        cls.manager = f.create_user("manager", perms=f.SRP_MANAGER_PERMS)
        cls.second_manager = f.create_user("second-manager", perms=f.SRP_MANAGER_PERMS)
        cls.fc = f.create_user("fc", perms=f.FC_PERMS)

    def settings(self, **values):
        return f.srp_settings(**values)

    def submit(self, user=None, *, link=None, fitted="100000000", time=None, **killmail):
        """A claim as members make them: through the signed preview token."""
        user = user or self.user
        km = f.srp_killmail(f.main_of(user), fitted=fitted, time=time, **killmail)
        token = signing.dumps(
            {"km": km.pk, "link": link.code if link else None, "u": user.pk}, salt=claims.PREVIEW_SALT
        )
        return claims.submit_claim(user, token)

    def approved(self, user=None, amount=80_000_000, **kwargs):
        claim = self.submit(user, **kwargs)
        approver = self.second_manager if user is None or user != self.second_manager else self.manager
        return review.approve_claim(claim, actor=approver, amount=amount, reason="Test", confirm_above_policy=True)

    def fleet(self, **extra):
        operation = f.create_operation(self.fc, **extra)
        return operation, f.fleet_link(operation)

    def used(self, user=None, claim=None):
        claim = claim or f.srp_claim(user or self.user)
        year, month = quota.claim_month(claim)
        return quota.month_usage(year=year, month=month, user_id=(user or self.user).pk)[0]

    def event(self, claim, action):
        return claim.events.get(action=action)

    def audit(self, claim, action):
        return AuditLog.objects.get(action=action, object_type="SrpClaim", object_id=str(claim.pk))


class ApproveTests(ReviewBase):
    def test_approve_at_the_suggested_amount(self):
        claim = self.submit()
        before = timezone.now()
        result = review.approve_claim(claim, actor=self.manager, amount=claim.suggested_amount)
        self.assertIs(result, claim)
        stored = fresh(claim)
        for obj in (claim, stored):
            self.assertEqual(obj.status, Status.APPROVED)
            self.assertEqual(obj.approved_amount, 80_000_000)
            self.assertEqual(obj.decided_by, self.manager)
            self.assertGreaterEqual(obj.decided_at, before)
            self.assertEqual(obj.decision_reason, "")
            self.assertFalse(obj.auto_rejected)
        self.assertIsNone(stored.paid_at)

    def test_approve_writes_event_audit_and_notification(self):
        claim = self.submit()
        with self.captureOnCommitCallbacks(execute=True):
            review.approve_claim(claim, actor=self.manager, amount=80_000_000)
        event = self.event(claim, Action.APPROVE)
        self.assertEqual(
            (event.actor, event.from_status, event.to_status, event.amount, event.internal),
            (self.manager, "pending", "approved", 80_000_000, False),
        )
        self.assertEqual(event.data["suggested_amount"], 80_000_000)
        self.assertEqual(event.data["over_limit_by"], 0)
        entry = self.audit(claim, "srp.claim_approve")
        self.assertEqual(entry.actor, self.manager)
        self.assertEqual(entry.old_value, {"status": "pending", "approved_amount": None})
        self.assertEqual(entry.new_value, {"status": "approved", "approved_amount": 80_000_000})
        notification = Notification.objects.get()
        self.assertEqual(notification.user, self.user)
        self.assertEqual(notification.level, "success")
        self.assertIn("SRP approved", notification.title)
        self.assertIn("80,000,000 ISK", notification.message)
        self.assertIn(claim.reference, notification.message)

    def test_a_different_amount_needs_a_reason(self):
        claim = self.submit()
        for amount in (50_000_000, 70_000_000):
            with self.subTest(amount=amount):
                with self.assertRaises(SrpError) as caught:
                    review.approve_claim(claim, actor=self.manager, amount=amount)
                self.assertEqual(caught.exception.code, "reason_required")
                self.assertEqual(fresh(claim).status, Status.PENDING)
        review.approve_claim(claim, actor=self.manager, amount=50_000_000, reason="  Partial loss  ")
        self.assertEqual(fresh(claim).decision_reason, "Partial loss")
        self.assertEqual(fresh(claim).approved_amount, 50_000_000)

    def test_a_blank_reason_does_not_count(self):
        claim = self.submit()
        with self.assertRaises(SrpError):
            review.approve_claim(claim, actor=self.manager, amount=50_000_000, reason="   ")

    def test_more_than_the_policy_needs_a_confirmation(self):
        claim = self.submit()
        with self.assertRaises(SrpLimitError) as caught:
            review.approve_claim(claim, actor=self.manager, amount=90_000_000, reason="Rare fit")
        self.assertEqual(caught.exception.code, "confirm_above_policy")
        self.assertIn("more than the policy amount of 80,000,000 ISK", caught.exception.message)
        self.assertEqual(fresh(claim).status, Status.PENDING)
        review.approve_claim(
            claim, actor=self.manager, amount=90_000_000, reason="Rare fit", confirm_above_policy=True
        )
        self.assertEqual(fresh(claim).approved_amount, 90_000_000)

    def test_more_than_the_policy_also_needs_a_reason(self):
        claim = self.submit()
        with self.assertRaises(SrpError) as caught:
            review.approve_claim(claim, actor=self.manager, amount=90_000_000, confirm_above_policy=True)
        self.assertEqual(caught.exception.code, "reason_required")

    def test_claim_without_a_suggested_amount_can_be_paid_with_a_reason(self):
        claim = self.submit(fitted=None, zkill_status="pending")
        self.assertEqual(claim.suggested_amount, 0)
        with self.assertRaises(SrpLimitError):
            review.approve_claim(claim, actor=self.manager, amount=20_000_000, reason="By hand")
        review.approve_claim(
            claim, actor=self.manager, amount=20_000_000, reason="By hand", confirm_above_policy=True
        )
        self.assertEqual(fresh(claim).status, Status.APPROVED)

    def test_amounts_are_checked(self):
        claim = self.submit()
        for amount in (0, -5, MAX_ISK + 1, True, 1.5, "100", None, Decimal("10.5")):
            with self.subTest(amount=amount):
                with self.assertRaises(SrpError) as caught:
                    review.approve_claim(claim, actor=self.manager, amount=amount, reason="x", confirm_above_policy=True)
                self.assertEqual(caught.exception.code, "invalid_amount")
        self.assertEqual(fresh(claim).status, Status.PENDING)

    def test_largest_and_smallest_amounts_are_accepted(self):
        for amount in (1, MAX_ISK):
            claim = self.submit()
            with self.subTest(amount=amount):
                review.approve_claim(claim, actor=self.manager, amount=amount, reason="x", confirm_above_policy=True)
                self.assertEqual(fresh(claim).approved_amount, amount)

    def test_whole_decimal_amount_is_accepted(self):
        claim = self.submit()
        review.approve_claim(claim, actor=self.manager, amount=Decimal("80000000"))
        self.assertEqual(fresh(claim).approved_amount, 80_000_000)

    def test_reason_has_a_limit(self):
        claim = self.submit()
        with self.assertRaises(SrpError) as caught:
            review.approve_claim(claim, actor=self.manager, amount=80_000_000, reason="x" * 1001)
        self.assertEqual(caught.exception.code, "reason_too_long")

    def test_approving_again(self):
        claim = self.submit()
        review.approve_claim(claim, actor=self.manager, amount=80_000_000)
        with self.assertRaises(SrpStateError) as caught:
            review.approve_claim(fresh(claim), actor=self.manager, amount=80_000_000)
        self.assertEqual(caught.exception.message, "This claim is already approved for 80,000,000 ISK.")
        self.assertEqual(claim.events.filter(action=Action.APPROVE).count(), 1)

    def test_a_stale_page_cannot_approve_a_claim_decided_meanwhile(self):
        claim = self.submit()
        stale = fresh(claim)
        review.reject_claim(claim, actor=self.second_manager, reason="Not covered")
        with self.assertRaises(SrpStateError) as caught:
            review.approve_claim(stale, actor=self.manager, amount=80_000_000)
        self.assertEqual(caught.exception.message, "Only pending claims can be approved.")
        self.assertEqual(fresh(claim).status, Status.REJECTED)

    def test_paid_claims_cannot_be_approved_again(self):
        claim = f.srp_claim(self.user, status=Status.PAID, amount=5_000_000)
        with self.assertRaises(SrpStateError) as caught:
            review.approve_claim(claim, actor=self.manager, amount=6_000_000, reason="x", confirm_above_policy=True)
        self.assertIn("Undo the payment", caught.exception.message)

    def test_rejected_and_withdrawn_claims_cannot_be_approved(self):
        for status in (Status.REJECTED, Status.WITHDRAWN):
            claim = f.srp_claim(self.user, status=status)
            with self.subTest(status=status):
                with self.assertRaises(SrpStateError):
                    review.approve_claim(claim, actor=self.manager, amount=1)
                self.assertEqual(fresh(claim).status, status)

    def test_a_deleted_claim(self):
        claim = self.submit()
        stale = fresh(claim)
        claim.delete()
        with self.assertRaises(SrpError) as caught:
            review.approve_claim(stale, actor=self.manager, amount=1, reason="x", confirm_above_policy=True)
        self.assertEqual(caught.exception.code, "missing")

    def test_rule_claim_approved_at_the_rule_amount(self):
        f.payout_rule(MOA, 60_000_000)
        claim = self.submit(ship_type_id=MOA)
        review.approve_claim(claim, actor=self.manager, amount=60_000_000)
        self.assertEqual(fresh(claim).approved_amount, 60_000_000)


class ChangeAmountTests(ReviewBase):
    def test_amount_of_an_approved_claim_can_be_changed(self):
        claim = self.approved()
        with self.captureOnCommitCallbacks(execute=True):
            review.approve_claim(claim, actor=self.second_manager, amount=60_000_000, reason="Cargo was not covered")
        stored = fresh(claim)
        self.assertEqual((stored.status, stored.approved_amount), (Status.APPROVED, 60_000_000))
        self.assertEqual((stored.decided_by, stored.decision_reason), (self.second_manager, "Cargo was not covered"))
        event = self.event(claim, Action.AMOUNT_CHANGE)
        self.assertEqual((event.from_status, event.to_status, event.amount), ("approved", "approved", 60_000_000))
        self.assertEqual(event.data["previous_amount"], 80_000_000)
        entry = self.audit(claim, "srp.claim_amount_change")
        self.assertEqual(entry.old_value["approved_amount"], 80_000_000)
        self.assertEqual(entry.new_value["approved_amount"], 60_000_000)
        self.assertEqual(claim.events.filter(action=Action.APPROVE).count(), 1)
        titles = sorted(Notification.objects.values_list("title", flat=True))
        self.assertTrue(any("SRP amount changed" in title for title in titles))

    def test_changing_the_amount_always_needs_a_reason(self):
        claim = self.approved()
        with self.assertRaises(SrpError) as caught:
            review.approve_claim(claim, actor=self.manager, amount=80_000_000 - 1)
        self.assertEqual(caught.exception.code, "reason_required")
        self.assertEqual(fresh(claim).approved_amount, 80_000_000)

    def test_changing_back_to_the_suggested_amount_still_needs_a_reason(self):
        claim = self.submit()
        review.approve_claim(claim, actor=self.manager, amount=90_000_000, reason="Rigs counted", confirm_above_policy=True)
        with self.assertRaises(SrpError) as caught:
            review.approve_claim(claim, actor=self.manager, amount=claim.suggested_amount)
        self.assertEqual(caught.exception.code, "reason_required")
        self.assertEqual(fresh(claim).approved_amount, 90_000_000)
        review.approve_claim(claim, actor=self.manager, amount=claim.suggested_amount, reason="Back to the policy amount")
        self.assertEqual(fresh(claim).approved_amount, 80_000_000)
        self.assertEqual(self.event(claim, Action.AMOUNT_CHANGE).reason, "Back to the policy amount")

    def test_changing_to_the_amount_it_already_has(self):
        claim = self.approved(amount=80_000_000)
        with self.assertRaises(SrpStateError):
            review.approve_claim(claim, actor=self.manager, amount=80_000_000, reason="Again")

    def test_the_new_amount_counts_toward_the_limits(self):
        self.settings(personal_monthly_cap=200_000_000)
        claim = self.approved()
        year, month = quota.claim_month(claim)
        self.assertEqual(quota.month_usage(year=year, month=month, user_id=self.user.pk)[0], 80_000_000)
        review.approve_claim(claim, actor=self.manager, amount=30_000_000, reason="Less")
        self.assertEqual(quota.month_usage(year=year, month=month, user_id=self.user.pk)[0], 30_000_000)

    def test_the_claim_does_not_count_against_itself(self):
        self.settings(personal_monthly_cap=100_000_000, over_limit_action="reject")
        claim = self.submit()
        review.approve_claim(claim, actor=self.manager, amount=80_000_000)
        review.approve_claim(claim, actor=self.manager, amount=100_000_000, reason="Up to the cap", confirm_above_policy=True)
        self.assertEqual(fresh(claim).approved_amount, 100_000_000)
        with self.assertRaises(SrpLimitError):
            review.approve_claim(claim, actor=self.manager, amount=100_000_001, reason="Over", confirm_above_policy=True)

    def test_paid_claim_amount_cannot_be_changed(self):
        claim = self.approved()
        review.mark_paid([claim], actor=self.second_manager)
        with self.assertRaises(SrpStateError):
            review.approve_claim(fresh(claim), actor=self.manager, amount=1_000, reason="x")


class MonthlyLimitApprovalTests(ReviewBase):
    """The remaining limit is read again when a claim is approved."""

    def claim_pair(self, **settings):
        self.settings(personal_monthly_cap=100_000_000, **settings)
        first = self.submit(time=timezone.now() - timedelta(hours=3))
        second = self.submit(time=timezone.now() - timedelta(hours=2))
        return first, second

    def test_second_claim_sees_what_the_first_one_approved(self):
        first, second = self.claim_pair(over_limit_action="review")
        self.assertFalse(second.over_limit, "Both fit while nothing is approved")
        review.approve_claim(first, actor=self.manager, amount=80_000_000)
        with self.assertRaises(SrpLimitError) as caught:
            review.approve_claim(second, actor=self.manager, amount=80_000_000)
        self.assertEqual(caught.exception.code, "confirm_over_limit")
        self.assertIn("60,000,000 ISK over the monthly personal limit (20,000,000 ISK left)", caught.exception.message)
        self.assertEqual(fresh(second).status, Status.PENDING)

    def test_review_mode_allows_going_over_with_confirmation_and_a_reason(self):
        first, second = self.claim_pair(over_limit_action="review")
        review.approve_claim(first, actor=self.manager, amount=80_000_000)
        with self.assertRaises(SrpError) as caught:
            review.approve_claim(second, actor=self.manager, amount=80_000_000, confirm_over_limit=True)
        self.assertEqual(caught.exception.code, "reason_required")
        review.approve_claim(
            second, actor=self.manager, amount=80_000_000, confirm_over_limit=True, reason="Fleet commander's call"
        )
        stored = fresh(second)
        self.assertEqual((stored.status, stored.approved_amount), (Status.APPROVED, 80_000_000))
        self.assertEqual(self.event(second, Action.APPROVE).data["over_limit_by"], 60_000_000)

    def test_review_mode_within_the_limit_needs_no_confirmation(self):
        first, second = self.claim_pair(over_limit_action="review")
        review.approve_claim(first, actor=self.manager, amount=80_000_000)
        review.approve_claim(second, actor=self.manager, amount=20_000_000, reason="Reduced to the remaining monthly limit.")
        self.assertEqual(fresh(second).approved_amount, 20_000_000)

    def test_reject_mode_refuses_what_does_not_fit(self):
        first, second = self.claim_pair(over_limit_action="reject")
        review.approve_claim(first, actor=self.manager, amount=80_000_000)
        for confirm in (False, True):
            with self.subTest(confirm=confirm):
                with self.assertRaises(SrpLimitError) as caught:
                    review.approve_claim(
                        second, actor=self.manager, amount=80_000_000, reason="x", confirm_over_limit=confirm
                    )
                self.assertEqual(caught.exception.code, "over_limit")
                self.assertEqual(caught.exception.message, "Only 20,000,000 ISK left in the monthly personal limit.")
        self.assertEqual(fresh(second).status, Status.PENDING)
        review.approve_claim(second, actor=self.manager, amount=20_000_000, reason="Within the limit")
        self.assertEqual(fresh(second).approved_amount, 20_000_000)

    def test_exactly_the_remaining_amount_fits(self):
        first, second = self.claim_pair(over_limit_action="reject")
        review.approve_claim(first, actor=self.manager, amount=80_000_000)
        review.approve_claim(second, actor=self.manager, amount=20_000_000, reason="Remaining")
        self.assertEqual(self.used(), 100_000_000)

    def test_corporation_limit_counts_all_members(self):
        self.settings(corporation_monthly_cap=100_000_000, over_limit_action="reject")
        mine = self.submit(self.user)
        colleague = f.create_user("colleague", perms=f.SRP_MEMBER_PERMS)
        theirs = self.submit(colleague)
        review.approve_claim(mine, actor=self.manager, amount=80_000_000)
        with self.assertRaises(SrpLimitError) as caught:
            review.approve_claim(theirs, actor=self.manager, amount=80_000_000)
        self.assertIn("monthly corporation limit", caught.exception.message)

    def test_the_tightest_limit_is_named(self):
        self.settings(personal_monthly_cap=1_000_000_000, corporation_monthly_cap=100_000_000, over_limit_action="reject")
        a = self.submit(self.user)
        b = self.submit(self.user)
        review.approve_claim(a, actor=self.manager, amount=80_000_000)
        with self.assertRaises(SrpLimitError) as caught:
            review.approve_claim(b, actor=self.manager, amount=80_000_000)
        self.assertIn("monthly corporation limit", caught.exception.message)

    def test_in_fleet_loss_not_counted_toward_the_personal_limit(self):
        self.settings(personal_monthly_cap=100_000_000, personal_cap_includes_in_fleet=False, over_limit_action="reject")
        _operation, link = self.fleet()
        in_fleet = self.submit(link=link)
        out_of_fleet = self.submit()
        review.approve_claim(in_fleet, actor=self.manager, amount=80_000_000)
        self.assertEqual(self.used(), 0, "Not counted toward the personal limit")
        review.approve_claim(out_of_fleet, actor=self.manager, amount=80_000_000)
        self.assertEqual(self.used(), 80_000_000)

    def test_in_fleet_approval_is_not_limited_by_a_limit_that_does_not_count_it(self):
        self.settings(personal_monthly_cap=10_000_000, personal_cap_includes_in_fleet=False, over_limit_action="reject")
        _operation, link = self.fleet()
        claim = self.submit(link=link)
        review.approve_claim(claim, actor=self.manager, amount=80_000_000)
        self.assertEqual(fresh(claim).status, Status.APPROVED)

    def test_limits_of_the_claims_snapshot_are_used(self):
        self.settings(personal_monthly_cap=100_000_000, over_limit_action="reject")
        a = self.submit()
        b = self.submit()
        review.approve_claim(a, actor=self.manager, amount=80_000_000)
        # The admin raises the cap afterwards; the claim keeps what it was calculated with until it is recalculated.
        self.settings(personal_monthly_cap=500_000_000)
        with self.assertRaises(SrpLimitError):
            review.approve_claim(b, actor=self.manager, amount=80_000_000)
        review.recalculate_claim(b, actor=self.manager)
        review.approve_claim(b, actor=self.manager, amount=80_000_000)
        self.assertEqual(fresh(b).approved_amount, 80_000_000)

    def test_loss_month_decides_which_usage_counts(self):
        self.settings(personal_monthly_cap=100_000_000, over_limit_action="reject")
        last_month = timezone.now() - timedelta(days=45)
        old = self.submit(time=last_month)
        current = self.submit()
        review.approve_claim(old, actor=self.manager, amount=80_000_000)
        review.approve_claim(current, actor=self.manager, amount=80_000_000)
        self.assertEqual(fresh(current).status, Status.APPROVED)

    def test_paid_claims_still_use_the_limit(self):
        first, second = self.claim_pair(over_limit_action="reject")
        review.approve_claim(first, actor=self.manager, amount=80_000_000)
        review.mark_paid([first], actor=self.second_manager)
        with self.assertRaises(SrpLimitError):
            review.approve_claim(second, actor=self.manager, amount=80_000_000)

    def test_revoking_frees_the_limit(self):
        first, second = self.claim_pair(over_limit_action="reject")
        review.approve_claim(first, actor=self.manager, amount=80_000_000)
        review.revoke_approval(first, actor=self.manager, reason="Wrong fit")
        review.approve_claim(second, actor=self.manager, amount=80_000_000)
        self.assertEqual(self.used(), 80_000_000)


class RejectTests(ReviewBase):
    def test_reject_with_a_reason(self):
        claim = self.submit()
        with self.captureOnCommitCallbacks(execute=True):
            result = review.reject_claim(claim, actor=self.manager, reason="  Not a doctrine ship ")
        self.assertIs(result, claim)
        stored = fresh(claim)
        self.assertEqual(stored.status, Status.REJECTED)
        self.assertEqual((stored.decided_by, stored.decision_reason), (self.manager, "Not a doctrine ship"))
        self.assertIsNotNone(stored.decided_at)
        self.assertIsNone(stored.approved_amount)
        self.assertFalse(stored.auto_rejected)
        event = self.event(claim, Action.REJECT)
        self.assertEqual((event.actor, event.from_status, event.to_status, event.reason), (self.manager, "pending", "rejected", "Not a doctrine ship"))
        entry = self.audit(claim, "srp.claim_reject")
        self.assertEqual(entry.reason, "Not a doctrine ship")
        notification = Notification.objects.get()
        self.assertEqual(notification.level, "danger")
        self.assertIn("SRP rejected", notification.title)
        self.assertIn("Not a doctrine ship", notification.message)

    def test_reason_is_required(self):
        claim = self.submit()
        for reason in ("", "   ", None):
            with self.subTest(reason=reason):
                with self.assertRaises(SrpError) as caught:
                    review.reject_claim(claim, actor=self.manager, reason=reason)
                self.assertEqual(caught.exception.message, "Give a reason for the rejection.")
        self.assertEqual(fresh(claim).status, Status.PENDING)

    def test_only_pending_claims_can_be_rejected(self):
        for status in (Status.APPROVED, Status.PAID, Status.WITHDRAWN):
            claim = f.srp_claim(self.user, status=status)
            with self.subTest(status=status):
                with self.assertRaises(SrpStateError) as caught:
                    review.reject_claim(claim, actor=self.manager, reason="x")
                self.assertEqual(caught.exception.message, "Only pending claims can be rejected.")

    def test_rejecting_again(self):
        claim = self.submit()
        review.reject_claim(claim, actor=self.manager, reason="No")
        with self.assertRaises(SrpStateError) as caught:
            review.reject_claim(fresh(claim), actor=self.manager, reason="No")
        self.assertEqual(caught.exception.message, "This claim is already rejected.")

    def test_rejected_claims_use_no_limit(self):
        claim = self.submit()
        review.reject_claim(claim, actor=self.manager, reason="No")
        self.assertEqual(self.used(claim=claim), 0)


class RevokeTests(ReviewBase):
    def test_revoke_returns_the_claim_to_review(self):
        claim = self.approved()
        with self.captureOnCommitCallbacks(execute=True):
            review.revoke_approval(claim, actor=self.second_manager, reason="Approved by mistake")
        stored = fresh(claim)
        self.assertEqual(stored.status, Status.PENDING)
        self.assertIsNone(stored.approved_amount)
        self.assertEqual((stored.decided_by, stored.decision_reason), (self.second_manager, "Approved by mistake"))
        event = self.event(claim, Action.REVOKE)
        self.assertEqual((event.from_status, event.to_status, event.amount), ("approved", "pending", 80_000_000))
        entry = self.audit(claim, "srp.claim_revoke")
        self.assertEqual(entry.old_value, {"status": "approved", "approved_amount": 80_000_000})
        self.assertEqual(entry.new_value, {"status": "pending", "approved_amount": None})
        notification = Notification.objects.filter(title__startswith="SRP approval revoked").get()
        self.assertEqual(notification.level, "warning")

    def test_revoke_releases_the_amount(self):
        claim = self.approved()
        self.assertEqual(self.used(claim=claim), 80_000_000)
        review.revoke_approval(claim, actor=self.manager, reason="Mistake")
        self.assertEqual(self.used(claim=claim), 0)

    def test_the_claim_can_be_approved_again_after_a_revoke(self):
        claim = self.approved()
        review.revoke_approval(claim, actor=self.manager, reason="Mistake")
        review.approve_claim(claim, actor=self.manager, amount=70_000_000, reason="Smaller amount")
        self.assertEqual(fresh(claim).approved_amount, 70_000_000)

    def test_reason_is_required(self):
        claim = self.approved()
        with self.assertRaises(SrpError):
            review.revoke_approval(claim, actor=self.manager, reason=" ")
        self.assertEqual(fresh(claim).status, Status.APPROVED)

    def test_paid_claims_cannot_be_revoked(self):
        claim = self.approved()
        review.mark_paid([claim], actor=self.second_manager)
        with self.assertRaises(SrpStateError) as caught:
            review.revoke_approval(fresh(claim), actor=self.manager, reason="x")
        self.assertIn("Undo the payment first", caught.exception.message)
        self.assertEqual(fresh(claim).status, Status.PAID)

    def test_revoke_after_undoing_the_payment(self):
        claim = self.approved()
        review.mark_paid([claim], actor=self.second_manager)
        review.undo_payment(fresh(claim), actor=self.second_manager, reason="Paid the wrong pilot")
        review.revoke_approval(fresh(claim), actor=self.second_manager, reason="Mistake")
        self.assertEqual(fresh(claim).status, Status.PENDING)

    def test_only_approved_claims_can_be_revoked(self):
        for status in (Status.PENDING, Status.REJECTED, Status.WITHDRAWN):
            claim = f.srp_claim(self.user, status=status)
            with self.subTest(status=status):
                with self.assertRaises(SrpStateError):
                    review.revoke_approval(claim, actor=self.manager, reason="x")

    def test_revoking_twice(self):
        claim = self.approved()
        review.revoke_approval(claim, actor=self.manager, reason="x")
        with self.assertRaises(SrpStateError) as caught:
            review.revoke_approval(fresh(claim), actor=self.manager, reason="x")
        self.assertEqual(caught.exception.message, "This claim is already pending.")


class PaymentTests(ReviewBase):
    def test_mark_paid(self):
        claim = self.approved()
        before = timezone.now()
        with self.captureOnCommitCallbacks(execute=True):
            result = review.mark_paid([claim], actor=self.second_manager, note="  Sent in game  ")
        self.assertEqual(result.skipped, [])
        self.assertEqual(result.done, [claim])
        self.assertEqual(claim.status, Status.PAID)
        stored = fresh(claim)
        self.assertEqual(stored.status, Status.PAID)
        self.assertEqual((stored.paid_by, stored.payment_note), (self.second_manager, "Sent in game"))
        self.assertGreaterEqual(stored.paid_at, before)
        self.assertEqual(stored.approved_amount, 80_000_000)
        event = self.event(claim, Action.PAID)
        self.assertEqual((event.actor, event.from_status, event.to_status, event.amount), (self.second_manager, "approved", "paid", 80_000_000))
        self.assertTrue(self.audit(claim, "srp.claim_paid"))
        notification = Notification.objects.filter(title__startswith="SRP paid").get()
        self.assertEqual(notification.level, "success")
        self.assertIn("80,000,000 ISK", notification.message)

    def test_bulk_payment_of_several_claims(self):
        claims_ = [self.approved(user, amount=10_000_000 * (index + 1)) for index, user in enumerate((self.user, self.other, self.user))]
        result = review.mark_paid(claims_, actor=self.second_manager, note="March payout")
        self.assertEqual(len(result.done), 3)
        self.assertEqual(result.skipped, [])
        self.assertEqual(SrpClaim.objects.filter(status=Status.PAID, payment_note="March payout").count(), 3)

    def test_a_single_claim_instead_of_a_list(self):
        claim = self.approved()
        self.assertEqual(review.mark_paid(claim, actor=self.second_manager).done, [claim])

    def test_a_queryset_is_accepted(self):
        self.approved()
        self.approved(self.other)
        result = review.mark_paid(SrpClaim.objects.filter(status=Status.APPROVED), actor=self.second_manager)
        self.assertEqual(len(result.done), 2)

    def test_claims_that_are_not_approved_are_skipped_with_the_reason(self):
        approved = self.approved()
        pending = self.submit()
        paid = f.srp_claim(self.user, status=Status.PAID)
        rejected = f.srp_claim(self.user, status=Status.REJECTED)
        result = review.mark_paid([approved, pending, paid, rejected], actor=self.second_manager)
        self.assertEqual(result.done, [approved])
        skipped = {claim.pk: message for claim, message in result.skipped}
        self.assertEqual(skipped[pending.pk], "Only approved claims can be marked paid.")
        self.assertEqual(skipped[paid.pk], "This claim is already paid.")
        self.assertEqual(skipped[rejected.pk], "Only approved claims can be marked paid.")
        self.assertEqual(fresh(pending).status, Status.PENDING)

    def test_a_managers_own_claims_are_skipped_not_refused(self):
        own = self.approved(self.manager)
        other = self.approved(self.user)
        result = review.mark_paid([own, other], actor=self.manager)
        self.assertEqual(result.done, [other])
        self.assertEqual(result.skipped, [(own, "Your own claims are decided by another SRP manager.")])
        self.assertEqual(fresh(own).status, Status.APPROVED)

    def test_own_claims_can_be_paid_when_the_settings_allow_it(self):
        self.settings(allow_self_review=True)
        own = self.approved(self.manager)
        self.assertEqual(review.mark_paid([own], actor=self.manager).done, [own])

    def test_note_is_optional_and_limited(self):
        claim = self.approved()
        review.mark_paid([claim], actor=self.second_manager)
        self.assertEqual(fresh(claim).payment_note, "")
        other = self.approved()
        with self.assertRaises(SrpError) as caught:
            review.mark_paid([other], actor=self.second_manager, note="x" * 256)
        self.assertEqual(caught.exception.code, "note_too_long")
        self.assertEqual(fresh(other).status, Status.APPROVED)

    def test_only_managers_pay(self):
        claim = self.approved()
        for actor in (self.user, self.fc, None):
            with self.subTest(actor=actor):
                with self.assertRaises(SrpError) as caught:
                    review.mark_paid([claim], actor=actor)
                self.assertEqual(caught.exception.code, "forbidden")
        self.assertEqual(fresh(claim).status, Status.APPROVED)

    def test_paying_twice_in_a_row(self):
        claim = self.approved()
        review.mark_paid([claim], actor=self.second_manager)
        result = review.mark_paid([fresh(claim)], actor=self.second_manager)
        self.assertEqual(result.done, [])
        self.assertEqual(result.skipped[0][1], "This claim is already paid.")
        self.assertEqual(claim.events.filter(action=Action.PAID).count(), 1)

    def test_undo_payment(self):
        claim = self.approved()
        review.mark_paid([claim], actor=self.second_manager, note="Sent")
        result = review.undo_payment(fresh(claim), actor=self.manager, reason="Paid the wrong pilot")
        stored = fresh(claim)
        self.assertEqual(stored.status, Status.APPROVED)
        self.assertEqual((stored.paid_by, stored.paid_at, stored.payment_note), (None, None, ""))
        self.assertEqual(stored.approved_amount, 80_000_000)
        self.assertEqual(result.status, Status.APPROVED)
        event = self.event(claim, Action.UNPAID)
        self.assertEqual((event.from_status, event.to_status, event.reason), ("paid", "approved", "Paid the wrong pilot"))
        self.assertTrue(self.audit(claim, "srp.claim_unpaid"))
        self.assertEqual(self.used(claim=claim), 80_000_000, "The amount stays used")

    def test_undo_needs_a_reason_and_a_paid_claim(self):
        claim = self.approved()
        with self.assertRaises(SrpStateError) as caught:
            review.undo_payment(claim, actor=self.manager, reason="x")
        self.assertEqual(caught.exception.message, "This claim is already approved.")
        review.mark_paid([claim], actor=self.second_manager)
        with self.assertRaises(SrpError):
            review.undo_payment(fresh(claim), actor=self.manager, reason="")
        pending = self.submit()
        with self.assertRaises(SrpStateError) as caught:
            review.undo_payment(pending, actor=self.manager, reason="x")
        self.assertEqual(caught.exception.message, "Only paid claims can be marked unpaid.")

    def test_paid_again_after_the_payment_was_undone(self):
        claim = self.approved()
        review.mark_paid([claim], actor=self.second_manager)
        review.undo_payment(fresh(claim), actor=self.manager, reason="Wrong pilot")
        self.assertEqual(len(review.mark_paid([fresh(claim)], actor=self.second_manager).done), 1)


class ReopenTests(ReviewBase):
    def test_reopen_a_rejected_claim(self):
        claim = self.submit()
        review.reject_claim(claim, actor=self.manager, reason="Wrong fit")
        with self.captureOnCommitCallbacks(execute=True):
            review.reopen_claim(claim, actor=self.second_manager, reason="Pilot sent the right fit")
        stored = fresh(claim)
        self.assertEqual(stored.status, Status.PENDING)
        self.assertFalse(stored.auto_rejected)
        self.assertEqual((stored.decided_by, stored.decision_reason), (self.second_manager, "Pilot sent the right fit"))
        event = self.event(claim, Action.REOPEN)
        self.assertEqual((event.from_status, event.to_status, event.reason), ("rejected", "pending", "Pilot sent the right fit"))
        self.assertIn("previous", event.data)
        self.assertTrue(self.audit(claim, "srp.claim_reopen"))
        notification = Notification.objects.filter(title__startswith="SRP claim reopened").get()
        self.assertEqual(notification.level, "info")

    def test_reopening_calculates_the_claim_again(self):
        claim = self.submit(fitted="100000000")
        review.reject_claim(claim, actor=self.manager, reason="No")
        self.settings(percentage=Decimal("50"))
        review.reopen_claim(claim, actor=self.manager, reason="Policy changed")
        stored = fresh(claim)
        self.assertEqual((stored.base_amount, stored.suggested_amount), (50_000_000, 50_000_000))
        self.assertEqual(stored.policy["percentage"], "50.00")
        self.assertEqual(stored.events.get(action=Action.REOPEN).data["previous"]["suggested_amount"], 80_000_000)

    def test_an_automatically_rejected_claim_can_be_reopened(self):
        self.settings(personal_monthly_cap=50_000_000, over_limit_action="reject")
        f.srp_claim(self.user, amount=50_000_000, status=Status.APPROVED)
        claim = self.submit()
        self.assertTrue(claim.auto_rejected)
        review.reopen_claim(claim, actor=self.manager, reason="Fleet loss, make an exception")
        stored = fresh(claim)
        self.assertEqual((stored.status, stored.auto_rejected), (Status.PENDING, False))
        self.assertEqual(stored.limited_amount, 0)
        self.assertEqual(stored_codes(stored), ["over_limit"])
        # The reviewer can still approve over the limit; reject mode keeps refusing it.
        with self.assertRaises(SrpLimitError):
            review.approve_claim(claim, actor=self.manager, amount=80_000_000, reason="x", confirm_over_limit=True)

    def test_reason_is_required(self):
        claim = f.srp_claim(self.user, status=Status.REJECTED)
        with self.assertRaises(SrpError):
            review.reopen_claim(claim, actor=self.manager, reason="")

    def test_only_rejected_claims_can_be_reopened(self):
        pending = self.submit()
        with self.assertRaises(SrpStateError) as caught:
            review.reopen_claim(pending, actor=self.manager, reason="x")
        self.assertEqual(caught.exception.message, "This claim is already pending.")
        for status in (Status.APPROVED, Status.PAID, Status.WITHDRAWN):
            claim = f.srp_claim(self.user, status=status)
            with self.subTest(status=status):
                with self.assertRaises(SrpStateError) as caught:
                    review.reopen_claim(claim, actor=self.manager, reason="x")
                self.assertEqual(caught.exception.message, "Only rejected claims can be reopened.")


class ReclassifyTests(ReviewBase):
    def test_out_of_fleet_to_in_fleet(self):
        operation, link = self.fleet(doctrine_name="Moa Fleet")
        f.payout_rule(0, 75_000_000, doctrine="Moa Fleet")
        f.member_state(operation, f.main_of(self.user), first_seen=operation.started_at)
        claim = self.submit(time=timezone.now() - timedelta(minutes=5))
        self.assertEqual(claim.payout_basis, "percentage")
        review.reclassify_claim(claim, actor=self.manager, scope=IN_FLEET, link=link, reason="Was in the fleet")
        stored = fresh(claim)
        self.assertEqual(stored.status, Status.PENDING)
        self.assertEqual((stored.scope, stored.link, stored.operation), (IN_FLEET, link, operation))
        self.assertEqual((stored.fc_user, stored.fc_main_character_name), (self.fc, link.fc_main_character_name))
        self.assertTrue(stored.fleet_label.startswith("StratOp — "))
        self.assertEqual((stored.payout_basis, stored.suggested_amount), ("rule", 75_000_000))
        self.assertEqual(stored.fleet_check, display.IN_FLEET)
        event = self.event(claim, Action.RECLASSIFY)
        self.assertEqual(event.reason, "Was in the fleet")
        self.assertEqual(
            (event.data["from_scope"], event.data["to_scope"], event.data["from_link"], event.data["to_link"]),
            (OUT_OF_FLEET, IN_FLEET, None, link.code),
        )
        self.assertEqual(event.data["previous"]["payout_basis"], "percentage")
        entry = self.audit(claim, "srp.claim_reclassify")
        self.assertEqual((entry.old_value["scope"], entry.new_value["scope"]), (OUT_OF_FLEET, IN_FLEET))

    def test_in_fleet_to_out_of_fleet(self):
        _operation, link = self.fleet(doctrine_name="Moa Fleet")
        f.payout_rule(0, 75_000_000, doctrine="Moa Fleet")
        claim = self.submit(link=link)
        self.assertEqual(claim.suggested_amount, 75_000_000)
        review.reclassify_claim(claim, actor=self.manager, scope=OUT_OF_FLEET, reason="Not in the fleet")
        stored = fresh(claim)
        self.assertEqual((stored.scope, stored.link, stored.operation, stored.fc_user, stored.fleet_label), (OUT_OF_FLEET, None, None, None, ""))
        self.assertEqual((stored.payout_basis, stored.suggested_amount), ("percentage", 80_000_000))
        self.assertEqual(stored.fleet_check, display.NOT_APPLICABLE)
        self.assertEqual(stored.events.get(action=Action.RECLASSIFY).data["from_link"], link.code)

    def test_flags_and_caps_follow_the_new_scope(self):
        self.settings(personal_cap_includes_in_fleet=False, in_fleet_loss_cap=30_000_000)
        _operation, link = self.fleet()
        claim = self.submit()
        self.assertTrue(claim.counts_personal)
        review.reclassify_claim(claim, actor=self.manager, scope=IN_FLEET, link=link, reason="In fleet")
        stored = fresh(claim)
        self.assertFalse(stored.counts_personal)
        self.assertEqual(stored.suggested_amount, 30_000_000)

    def test_presence_warnings_are_replaced(self):
        _operation, link = self.fleet()
        claim = self.submit()
        review.reclassify_claim(claim, actor=self.manager, scope=IN_FLEET, link=link, reason="Maybe in fleet")
        self.assertEqual(stored_codes(fresh(claim)), [display.NEVER_SEEN])
        review.reclassify_claim(claim, actor=self.manager, scope=OUT_OF_FLEET, reason="Back")
        self.assertEqual(stored_codes(fresh(claim)), [])

    def test_moving_to_another_fleet(self):
        _operation, link = self.fleet()
        claim = self.submit(link=link)
        _other_operation, other_link = self.fleet()
        review.reclassify_claim(claim, actor=self.manager, scope=IN_FLEET, link=other_link, reason="Other fleet")
        self.assertEqual(fresh(claim).link, other_link)

    def test_refusals(self):
        _operation, link = self.fleet()
        claim = self.submit()
        cases = [
            ({"scope": IN_FLEET, "link": None}, "link_required"),
            ({"scope": OUT_OF_FLEET}, "unchanged"),
            ({"scope": "elsewhere"}, "invalid_scope"),
        ]
        for kwargs, code in cases:
            with self.subTest(code=code):
                with self.assertRaises(SrpError) as caught:
                    review.reclassify_claim(claim, actor=self.manager, reason="x", **kwargs)
                self.assertEqual(caught.exception.code, code)
        link.is_open = False
        link.save()
        with self.assertRaises(SrpError) as caught:
            review.reclassify_claim(claim, actor=self.manager, scope=IN_FLEET, link=link, reason="x")
        self.assertEqual(caught.exception.code, "link_closed")
        self.assertEqual(fresh(claim).scope, OUT_OF_FLEET)

    def test_same_fleet_is_not_a_change(self):
        _operation, link = self.fleet()
        claim = self.submit(link=link)
        with self.assertRaises(SrpError) as caught:
            review.reclassify_claim(claim, actor=self.manager, scope=IN_FLEET, link=link, reason="x")
        self.assertEqual(caught.exception.code, "unchanged")

    def test_a_link_that_no_longer_exists(self):
        _operation, link = self.fleet()
        claim = self.submit()
        gone = SrpClaim._meta.get_field("link").related_model(pk=link.pk + 1000)
        with self.assertRaises(SrpError) as caught:
            review.reclassify_claim(claim, actor=self.manager, scope=IN_FLEET, link=gone, reason="x")
        self.assertEqual(caught.exception.code, "link_required")

    def test_out_of_fleet_target_needs_the_switch(self):
        _operation, link = self.fleet()
        claim = self.submit(link=link)
        self.settings(out_of_fleet_enabled=False)
        with self.assertRaises(SrpError) as caught:
            review.reclassify_claim(claim, actor=self.manager, scope=OUT_OF_FLEET, reason="x")
        self.assertEqual(caught.exception.code, "fleet_only")

    def test_reason_is_required(self):
        _operation, link = self.fleet()
        claim = self.submit()
        with self.assertRaises(SrpError) as caught:
            review.reclassify_claim(claim, actor=self.manager, scope=IN_FLEET, link=link, reason=" ")
        self.assertEqual(caught.exception.code, "reason_required")

    def test_only_pending_claims_can_be_moved(self):
        _operation, link = self.fleet()
        for status in (Status.APPROVED, Status.PAID, Status.REJECTED, Status.WITHDRAWN):
            claim = f.srp_claim(self.user, status=status)
            with self.subTest(status=status):
                with self.assertRaises(SrpStateError):
                    review.reclassify_claim(claim, actor=self.manager, scope=IN_FLEET, link=link, reason="x")
                self.assertEqual(fresh(claim).scope, OUT_OF_FLEET)


class RecalculateTests(ReviewBase):
    def test_recalculation_applies_the_current_policy_and_keeps_the_old_calculation(self):
        claim = self.submit(fitted="100000000")
        old_calculation = dict(claim.calculation)
        old_policy = dict(claim.policy)
        self.settings(percentage=Decimal("50"), out_of_fleet_loss_cap=40_000_000)
        result = review.recalculate_claim(claim, actor=self.manager, reason="Policy changed")
        self.assertIs(result, claim)
        stored = fresh(claim)
        self.assertEqual((stored.base_amount, stored.suggested_amount, stored.limited_amount), (50_000_000, 40_000_000, 40_000_000))
        self.assertEqual(stored.policy["percentage"], "50.00")
        self.assertEqual(Calculation.from_json(stored.calculation).cap, 40_000_000)
        event = self.event(claim, Action.RECALCULATE)
        self.assertEqual((event.actor, event.reason), (self.manager, "Policy changed"))
        self.assertEqual(event.data["previous"]["calculation"], old_calculation)
        self.assertEqual(event.data["previous"]["policy"], old_policy)
        self.assertEqual(event.data["previous"]["suggested_amount"], 80_000_000)
        entry = self.audit(claim, "srp.claim_recalculate")
        self.assertEqual((entry.old_value["suggested_amount"], entry.new_value["suggested_amount"]), (80_000_000, 40_000_000))

    def test_new_payout_rule_applies(self):
        claim = self.submit(ship_type_id=MOA)
        f.payout_rule(MOA, 33_000_000)
        review.recalculate_claim(claim, actor=self.manager)
        stored = fresh(claim)
        self.assertEqual((stored.payout_basis, stored.suggested_amount, stored.payout_rule.amount), ("rule", 33_000_000, 33_000_000))

    def test_current_limits_apply_and_the_claim_does_not_count_against_itself(self):
        self.settings(personal_monthly_cap=100_000_000)
        claim = self.submit()
        f.srp_claim(self.user, amount=40_000_000, status=Status.APPROVED)
        review.recalculate_claim(claim, actor=self.manager)
        stored = fresh(claim)
        self.assertEqual((stored.suggested_amount, stored.limited_amount, stored.over_limit), (80_000_000, 60_000_000, True))
        self.assertEqual(stored_codes(stored), ["over_limit"])
        review.recalculate_claim(claim, actor=self.manager)
        self.assertEqual(fresh(claim).limited_amount, 60_000_000)

    def test_warnings_that_are_not_calculated_stay(self):
        claim = self.submit()
        SrpClaim.objects.filter(pk=claim.pk).update(
            warnings=[{"code": "external_claim", "level": "warning", "params": {"claims": ["aa-srp: Pending"]}}]
        )
        review.recalculate_claim(claim, actor=self.manager)
        self.assertEqual(stored_codes(fresh(claim)), ["external_claim"])

    def test_the_system_can_recalculate_without_an_actor(self):
        claim = self.submit()
        self.settings(percentage=Decimal("10"))
        review.recalculate_claim(claim, reason="Nightly")
        event = self.event(claim, Action.RECALCULATE)
        self.assertIsNone(event.actor)
        self.assertEqual(fresh(claim).suggested_amount, 10_000_000)

    def test_decided_claims_are_not_recalculated(self):
        for status in (Status.APPROVED, Status.PAID, Status.REJECTED, Status.WITHDRAWN):
            claim = f.srp_claim(self.user, status=status, amount=7)
            with self.subTest(status=status):
                with self.assertRaises(SrpStateError) as caught:
                    review.recalculate_claim(claim, actor=self.manager)
                self.assertEqual(caught.exception.message, "Only pending claims can be recalculated.")
                self.assertEqual(fresh(claim).suggested_amount, 7)

    def test_recalculate_pending_handles_every_pending_claim(self):
        a, b = self.submit(), self.submit()
        approved = self.approved()
        withdrawn = self.submit()
        claims.withdraw_claim(withdrawn, actor=self.user)
        self.settings(percentage=Decimal("50"))
        self.assertEqual(review.recalculate_pending(actor=self.second_manager), 2)
        self.assertEqual({fresh(a).suggested_amount, fresh(b).suggested_amount}, {50_000_000})
        self.assertEqual(fresh(approved).approved_amount, 80_000_000)
        self.assertEqual(fresh(withdrawn).suggested_amount, 80_000_000)

    def test_recalculate_pending_leaves_the_managers_own_claims_alone(self):
        own = self.submit(self.manager)
        other = self.submit()
        self.settings(percentage=Decimal("50"))
        self.assertEqual(review.recalculate_pending(actor=self.manager), 1)
        self.assertEqual(fresh(own).suggested_amount, 80_000_000)
        self.assertEqual(fresh(other).suggested_amount, 50_000_000)

    def test_recalculate_pending_includes_own_claims_when_the_settings_allow_it(self):
        own = self.submit(self.manager)
        self.settings(allow_self_review=True, percentage=Decimal("50"))
        self.assertEqual(review.recalculate_pending(actor=self.manager), 1)
        self.assertEqual(fresh(own).suggested_amount, 50_000_000)

    def test_recalculate_pending_is_for_managers(self):
        with self.assertRaises(SrpError):
            review.recalculate_pending(actor=self.user)

    def test_recalculate_pending_skips_claims_that_changed_meanwhile(self):
        claim = self.submit()
        with mock.patch.object(claims, "recalculate_under_lock", side_effect=SrpStateError("already", "x")):
            self.assertEqual(review.recalculate_pending(actor=self.manager), 0)
        self.assertEqual(fresh(claim).status, Status.PENDING)


class BulkApproveTests(ReviewBase):
    def test_clean_claims_are_approved_at_the_suggested_amount(self):
        a, b = self.submit(), self.submit(self.other)
        result = review.approve_clean_claims([a, b], actor=self.manager)
        self.assertEqual(result.done, [a, b])
        self.assertEqual(result.skipped, [])
        self.assertEqual({fresh(a).approved_amount, fresh(b).approved_amount}, {80_000_000})
        self.assertEqual(self.event(a, Action.APPROVE).amount, 80_000_000)

    def test_claims_with_warnings_are_skipped(self):
        clean = self.submit()
        flagged = self.submit()
        SrpClaim.objects.filter(pk=flagged.pk).update(warnings=[{"code": "near", "level": "warning", "params": {}}])
        result = review.approve_clean_claims([clean, flagged], actor=self.manager)
        self.assertEqual(result.done, [clean])
        self.assertEqual(result.skipped, [(flagged, "This claim has warnings. Review it on its own.")])
        self.assertEqual(fresh(flagged).status, Status.PENDING)

    def test_over_limit_claims_are_skipped(self):
        self.settings(personal_monthly_cap=50_000_000)
        claim = self.submit()
        self.assertTrue(claim.over_limit)
        result = review.approve_clean_claims([claim], actor=self.manager)
        self.assertEqual(result.done, [])
        self.assertEqual(result.skipped[0][1], "This claim has warnings. Review it on its own.")

    def test_claims_without_a_suggested_amount_are_skipped(self):
        self.settings(percentage=Decimal("0"))
        claim = self.submit()
        result = review.approve_clean_claims([claim], actor=self.manager)
        self.assertEqual(result.skipped[0][1], "This claim has no suggested amount.")

    def test_claims_that_are_not_pending_are_skipped(self):
        approved = self.approved()
        result = review.approve_clean_claims([approved], actor=self.manager)
        self.assertEqual(result.skipped[0][1], "This claim is already approved.")
        rejected = f.srp_claim(self.user, status=Status.REJECTED)
        self.assertEqual(
            review.approve_clean_claims([rejected], actor=self.manager).skipped[0][1],
            "Only pending claims can be approved.",
        )

    def test_the_managers_own_claims_are_skipped(self):
        own, other = self.submit(self.manager), self.submit()
        result = review.approve_clean_claims([own, other], actor=self.manager)
        self.assertEqual(result.done, [other])
        self.assertEqual(result.skipped, [(own, "Your own claims are decided by another SRP manager.")])

    def test_claims_are_approved_oldest_loss_first_and_stop_at_the_limit(self):
        self.settings(personal_monthly_cap=100_000_000, over_limit_action="reject")
        newer = self.submit(time=timezone.now() - timedelta(hours=1))
        older = self.submit(time=timezone.now() - timedelta(hours=5))
        result = review.approve_clean_claims([newer, older], actor=self.manager)
        self.assertEqual(result.done, [older])
        self.assertEqual(fresh(older).status, Status.APPROVED)
        self.assertEqual(fresh(newer).status, Status.PENDING)
        self.assertEqual(result.skipped[0][0], newer)
        self.assertIn("left in the monthly personal limit", result.skipped[0][1])

    def test_in_review_mode_a_claim_that_no_longer_fits_is_skipped_too(self):
        self.settings(personal_monthly_cap=100_000_000, over_limit_action="review")
        older = self.submit(time=timezone.now() - timedelta(hours=5))
        newer = self.submit(time=timezone.now() - timedelta(hours=1))
        result = review.approve_clean_claims([newer, older], actor=self.manager)
        self.assertEqual(result.done, [older])
        self.assertEqual(result.skipped[0][0], newer)
        self.assertEqual(fresh(newer).status, Status.PENDING)

    def test_a_single_claim_or_a_queryset(self):
        claim = self.submit()
        self.assertEqual(review.approve_clean_claims(claim, actor=self.manager).done, [claim])
        self.submit()
        self.assertEqual(len(review.approve_clean_claims(SrpClaim.objects.filter(status=Status.PENDING), actor=self.manager).done), 1)

    def test_only_managers_approve_in_bulk(self):
        claim = self.submit()
        with self.assertRaises(SrpError) as caught:
            review.approve_clean_claims([claim], actor=self.user)
        self.assertEqual(caught.exception.code, "forbidden")

    def test_every_claim_gets_its_own_event_and_audit_entry(self):
        a, b = self.submit(), self.submit()
        review.approve_clean_claims([a, b], actor=self.manager)
        for claim in (a, b):
            self.assertEqual(claim.events.filter(action=Action.APPROVE).count(), 1)
            self.assertTrue(self.audit(claim, "srp.claim_approve"))


class SelfReviewTests(ReviewBase):
    """3-4: SRP managers cannot decide their own claims unless the settings allow it."""

    def own(self):
        return self.submit(self.manager)

    def decisions(self, claim):
        """Every decision of a manager on ``claim``, as calls that must all be refused or all be allowed."""
        return {
            "approve": lambda: review.approve_claim(claim, actor=self.manager, amount=claim.suggested_amount),
            "reject": lambda: review.reject_claim(claim, actor=self.manager, reason="No"),
            "reclassify": lambda: review.reclassify_claim(
                claim, actor=self.manager, scope=IN_FLEET, link=self.fleet()[1], reason="x"
            ),
            "recalculate": lambda: review.recalculate_claim(claim, actor=self.manager),
        }

    def test_a_manager_cannot_decide_on_their_own_claim(self):
        claim = self.own()
        for name, decide in self.decisions(claim).items():
            with self.subTest(name):
                with self.assertRaises(SrpError) as caught:
                    decide()
                self.assertEqual(caught.exception.code, "own_claim")
                self.assertEqual(caught.exception.message, "Another SRP manager has to decide on your own claim.")
        self.assertEqual(fresh(claim).status, Status.PENDING)
        self.assertFalse(claim.events.exclude(action=Action.SUBMIT).exists())

    def test_every_later_step_on_an_own_claim_is_refused_too(self):
        approved = f.srp_claim(self.manager, status=Status.APPROVED)
        paid = f.srp_claim(self.manager, status=Status.PAID)
        rejected = f.srp_claim(self.manager, status=Status.REJECTED)
        calls = [
            lambda: review.revoke_approval(approved, actor=self.manager, reason="x"),
            lambda: review.approve_claim(approved, actor=self.manager, amount=5, reason="x", confirm_above_policy=True),
            lambda: review.undo_payment(paid, actor=self.manager, reason="x"),
            lambda: review.reopen_claim(rejected, actor=self.manager, reason="x"),
        ]
        for call in calls:
            with self.assertRaises(SrpError) as caught:
                call()
            self.assertEqual(caught.exception.code, "own_claim")

    def test_another_manager_decides(self):
        claim = self.own()
        review.approve_claim(claim, actor=self.second_manager, amount=claim.suggested_amount)
        self.assertEqual(fresh(claim).status, Status.APPROVED)

    def test_the_setting_allows_it(self):
        self.settings(allow_self_review=True)
        claim = self.own()
        review.approve_claim(claim, actor=self.manager, amount=claim.suggested_amount)
        self.assertEqual(fresh(claim).decided_by, self.manager)
        review.revoke_approval(claim, actor=self.manager, reason="x")
        review.reject_claim(claim, actor=self.manager, reason="x")
        review.reopen_claim(claim, actor=self.manager, reason="x")
        self.assertEqual(fresh(claim).status, Status.PENDING)

    def test_the_setting_is_read_under_the_lock(self):
        claim = self.own()
        self.settings(allow_self_review=True)
        review.reject_claim(claim, actor=self.manager, reason="Mine, not covered")
        self.settings(allow_self_review=False)
        with self.assertRaises(SrpError):
            review.reopen_claim(claim, actor=self.manager, reason="x")

    def test_notes_and_rechecks_are_allowed_on_own_claims(self):
        claim = self.own()
        review.add_note(claim, actor=self.manager, text="Context for the second manager")
        review.recheck_presence(claim, actor=self.manager)
        self.assertEqual(claim.events.filter(action=Action.NOTE).count(), 1)
        self.assertEqual(claim.events.filter(action=Action.RECHECK).count(), 1)

    def test_a_claim_without_a_user_can_be_decided_by_any_manager(self):
        claim = self.submit()
        SrpClaim.objects.filter(pk=claim.pk).update(user=None)
        review.approve_claim(claim, actor=self.manager, amount=80_000_000)
        self.assertEqual(fresh(claim).status, Status.APPROVED)

    def test_members_and_other_roles_cannot_decide(self):
        claim = self.submit()
        for actor in (self.user, self.fc, f.create_user("viewer", perms=f.MEMBER_PERMS + ["fleetops.view_all_stats"]), None):
            with self.subTest(actor=actor):
                with self.assertRaises(SrpError) as caught:
                    review.approve_claim(claim, actor=actor, amount=80_000_000)
                self.assertEqual(caught.exception.code, "forbidden")
                with self.assertRaises(SrpError):
                    review.reject_claim(claim, actor=actor, reason="x")
                if actor is not None:
                    # Without an actor a recalculation is the system's (such as a value that arrived).
                    with self.assertRaises(SrpError):
                        review.recalculate_claim(claim, actor=actor)
        self.assertEqual(fresh(claim).status, Status.PENDING)

    def test_can_review_follows_the_access_rule(self):
        own, other = self.own(), self.submit()
        self.assertFalse(review.can_review(self.manager, own))
        self.assertTrue(review.can_review(self.second_manager, own))
        self.assertTrue(review.can_review(self.manager, other))
        self.assertFalse(review.can_review(self.user, other))
        self.settings(allow_self_review=True)
        self.assertTrue(review.can_review(self.manager, own))

    def test_bulk_actions_skip_own_claims_instead_of_failing(self):
        own = self.own()
        paid_own = f.srp_claim(self.manager, status=Status.APPROVED)
        approve_result = review.approve_clean_claims([own], actor=self.manager)
        pay_result = review.mark_paid([paid_own], actor=self.manager)
        for result, claim in ((approve_result, own), (pay_result, paid_own)):
            self.assertEqual(result.done, [])
            self.assertEqual(result.skipped, [(claim, "Your own claims are decided by another SRP manager.")])


class RecheckAndNoteTests(ReviewBase):
    def test_recheck_finds_tracking_data_that_came_in_later(self):
        operation, link = self.fleet()
        main = f.main_of(self.user)
        claim = self.submit(link=link, time=operation.started_at + timedelta(minutes=10))
        self.assertEqual(claim.fleet_check, display.NEVER_SEEN)
        self.assertEqual(stored_codes(claim), [display.NEVER_SEEN])
        f.member_state(operation, main, first_seen=operation.started_at)
        join = FleetMemberEvent.objects.create(
            operation=operation, character_id=main.character_id, character_name=main.character_name,
            event_type=FleetMemberEvent.EventType.JOIN,
        )
        FleetMemberEvent.objects.filter(pk=join.pk).update(created_at=operation.started_at)

        result = review.recheck_presence(claim, actor=self.manager)
        self.assertIs(result, claim)
        stored = fresh(claim)
        self.assertEqual(stored.fleet_check, display.IN_FLEET)
        self.assertEqual(stored.warnings, [])
        self.assertIsNotNone(stored.fleet_checked_at)
        event = self.event(claim, Action.RECHECK)
        self.assertEqual(event.data["previous"]["fleet_check"], display.NEVER_SEEN)
        self.assertEqual(event.data["fleet_check"], display.IN_FLEET)
        self.assertEqual((event.actor, event.from_status, event.to_status), (self.manager, "pending", "pending"))
        entry = self.audit(claim, "srp.claim_recheck")
        self.assertEqual((entry.old_value, entry.new_value), ({"fleet_check": display.NEVER_SEEN}, {"fleet_check": display.IN_FLEET}))

    def test_recheck_keeps_other_warnings_and_amounts(self):
        operation, link = self.fleet()
        claim = self.submit(link=link)
        SrpClaim.objects.filter(pk=claim.pk).update(
            warnings=[
                {"code": "never_seen", "level": "danger", "params": {}},
                {"code": "external_claim", "level": "warning", "params": {"claims": ["aa-srp: Pending"]}},
                {"code": "over_limit", "level": "danger", "params": {}},
            ]
        )
        review.recheck_presence(claim, actor=self.manager)
        stored = fresh(claim)
        self.assertEqual(sorted(stored_codes(stored)), ["external_claim", "never_seen", "over_limit"])
        self.assertEqual(stored.suggested_amount, 80_000_000)

    def test_recheck_works_on_decided_claims(self):
        _operation, link = self.fleet()
        claim = self.submit(link=link)
        review.approve_claim(claim, actor=self.manager, amount=80_000_000)
        review.recheck_presence(fresh(claim), actor=self.second_manager)
        self.assertEqual(fresh(claim).status, Status.APPROVED)

    def test_out_of_fleet_claims_are_not_applicable(self):
        claim = self.submit()
        review.recheck_presence(claim, actor=self.manager)
        self.assertEqual(fresh(claim).fleet_check, display.NOT_APPLICABLE)

    def test_the_fleet_may_be_gone(self):
        operation, link = self.fleet()
        claim = self.submit(link=link)
        operation.delete()
        with self.assertRaises(SrpError) as caught:
            review.recheck_presence(fresh(claim), actor=self.manager)
        self.assertEqual(caught.exception.code, "no_fleet")

    def test_recheck_is_for_managers(self):
        claim = self.submit()
        with self.assertRaises(SrpError) as caught:
            review.recheck_presence(claim, actor=self.user)
        self.assertEqual(caught.exception.code, "forbidden")

    def test_internal_note(self):
        claim = self.submit()
        event = review.add_note(claim, actor=self.manager, text="  Talked to the FC, was in fleet  ")
        self.assertEqual(event.action, Action.NOTE)
        self.assertTrue(event.internal)
        self.assertEqual((event.actor, event.reason, event.claim), (self.manager, "Talked to the FC, was in fleet", claim))
        self.assertEqual((event.from_status, event.to_status), ("pending", "pending"))
        self.assertEqual(fresh(claim).status, Status.PENDING)
        entry = self.audit(claim, "srp.claim_note")
        self.assertEqual(entry.actor, self.manager)
        self.assertIsNone(AuditLog.objects.filter(action="srp.claim_note").first().old_value)

    def test_the_text_of_a_note_stays_out_of_the_audit_log(self):
        claim = self.submit()
        review.add_note(claim, actor=self.manager, text="SECRET remark about the pilot")
        entry = self.audit(claim, "srp.claim_note")
        self.assertEqual(entry.reason, "")
        self.assertEqual(entry.new_value, {"note_length": len("SECRET remark about the pilot")})
        self.assertNotIn("SECRET", str(AuditLog.objects.filter(action="srp.claim_note").values()))
        # The claim's own timeline keeps the text for the managers.
        self.assertEqual(self.event(claim, Action.NOTE).reason, "SECRET remark about the pilot")

    def test_the_reasons_of_decisions_the_claimant_sees_stay_in_the_audit_log(self):
        claim = self.submit()
        review.reject_claim(claim, actor=self.manager, reason="Not covered")
        self.assertEqual(self.audit(claim, "srp.claim_reject").reason, "Not covered")

    def test_notes_work_on_claims_in_any_status(self):
        for status in Status.values:
            claim = f.srp_claim(self.user, status=status)
            with self.subTest(status=status):
                self.assertTrue(review.add_note(claim, actor=self.manager, text="x").internal)

    def test_notes_do_not_notify_the_claimant(self):
        claim = self.submit()
        with self.captureOnCommitCallbacks(execute=True):
            review.add_note(claim, actor=self.manager, text="private")
        self.assertFalse(Notification.objects.exists())

    def test_note_needs_text_within_limits_and_a_manager(self):
        claim = self.submit()
        for text in ("", "  ", None):
            with self.assertRaises(SrpError) as caught:
                review.add_note(claim, actor=self.manager, text=text)
            self.assertEqual(caught.exception.code, "note_required")
        with self.assertRaises(SrpError) as caught:
            review.add_note(claim, actor=self.manager, text="x" * 2001)
        self.assertEqual(caught.exception.code, "note_too_long")
        with self.assertRaises(SrpError) as caught:
            review.add_note(claim, actor=self.user, text="hello")
        self.assertEqual(caught.exception.code, "forbidden")
        self.assertFalse(claim.events.filter(action=Action.NOTE).exists())


class ShownStateTests(ReviewBase):
    """A decision is made on the claim the reviewer looked at, not on one that changed in the meantime."""

    def estimated(self):
        """A pending claim of 4,000,000 ISK whose value is still an estimate, and the killmail that gets its value."""
        claim = self.submit(fitted="5000000")
        self.assertEqual(claim.suggested_amount, 4_000_000)
        return claim

    def value_arrives(self, claim, fitted="200000000"):
        km = claim.killmail
        SrpKillmail.objects.filter(pk=km.pk).update(zkill_fitted_value=Decimal(fitted))
        claims.on_value_updated(SrpKillmail.objects.get(pk=km.pk))
        self.assertEqual(fresh(claim).suggested_amount, 160_000_000)

    def test_the_suggested_amount_that_changed_is_not_approved(self):
        claim = self.estimated()
        self.value_arrives(claim)

        with self.assertRaises(SrpStateError) as caught:
            review.approve_claim(claim, actor=self.manager, amount=4_000_000, expected_suggested=4_000_000)

        self.assertEqual(caught.exception.code, "amount_changed")
        self.assertIn("changed from 4,000,000 ISK to 160,000,000 ISK", caught.exception.message)
        stored = fresh(claim)
        self.assertEqual((stored.status, stored.approved_amount), (Status.PENDING, None))
        self.assertFalse(claim.events.filter(action=Action.APPROVE).exists())
        # Looking at the new amount, the reviewer approves it.
        review.approve_claim(claim, actor=self.manager, amount=160_000_000, expected_suggested=160_000_000)
        self.assertEqual(fresh(claim).approved_amount, 160_000_000)

    def test_the_amount_within_the_limits_that_changed_is_not_approved(self):
        self.settings(personal_monthly_cap=50_000_000)
        claim = self.submit()
        self.assertEqual((claim.suggested_amount, claim.limited_amount), (80_000_000, 50_000_000))

        with self.assertRaises(SrpStateError) as caught:
            review.approve_claim(claim, actor=self.manager, amount=40_000_000, reason="Limit", expected_limited=40_000_000)

        self.assertEqual(caught.exception.code, "amount_changed")
        self.assertEqual(fresh(claim).status, Status.PENDING)
        review.approve_claim(claim, actor=self.manager, amount=50_000_000, reason="Limit", expected_limited=50_000_000)
        self.assertEqual(fresh(claim).approved_amount, 50_000_000)

    def test_a_decision_without_shown_values_compares_nothing(self):
        claim = self.estimated()
        self.value_arrives(claim)

        review.approve_claim(claim, actor=self.manager, amount=160_000_000)

        self.assertEqual(fresh(claim).approved_amount, 160_000_000)

    def test_a_typed_amount_is_not_held_to_the_stored_amounts(self):
        claim = self.estimated()
        self.value_arrives(claim)

        review.approve_claim(claim, actor=self.manager, amount=3_000_000, reason="Cheap fit", expected_status="pending")

        self.assertEqual(fresh(claim).approved_amount, 3_000_000)

    def test_bulk_approval_skips_a_claim_whose_amount_changed(self):
        changed, unchanged, unlisted = self.estimated(), self.submit(self.other), self.submit(self.second_manager)
        self.value_arrives(changed)

        result = review.approve_clean_claims(
            [changed, unchanged, unlisted],
            actor=self.manager,
            expected_amounts={changed.pk: 4_000_000, unchanged.pk: 80_000_000},
        )

        self.assertEqual(set(result.done), {unchanged, unlisted})
        ((skipped, text),) = result.skipped
        self.assertEqual(skipped, changed)
        self.assertIn("changed from 4,000,000 ISK to 160,000,000 ISK", text)
        self.assertEqual(fresh(changed).status, Status.PENDING)

    def test_a_stale_change_of_the_amount_does_not_approve_a_revoked_claim(self):
        claim = self.approved(amount=10_000_000)
        review.revoke_approval(claim, actor=self.second_manager, reason="Pilot was not in the fleet")
        stale = SrpClaim.objects.get(pk=claim.pk)

        with self.assertRaises(SrpStateError) as caught:
            review.approve_claim(
                stale, actor=self.manager, amount=8_000_000, reason="partial loss", expected_status="approved"
            )

        self.assertEqual(caught.exception.code, "changed")
        self.assertIn("pending now", caught.exception.message)
        stored = fresh(claim)
        self.assertEqual((stored.status, stored.approved_amount), (Status.PENDING, None))
        self.assertEqual(stored.decision_reason, "Pilot was not in the fleet")
        self.assertEqual(list(claim.events.order_by("pk").values_list("action", flat=True)), ["submit", "approve", "revoke"])

    def test_approving_a_claim_another_manager_approved_is_not_an_amount_change(self):
        claim = self.submit()
        review.approve_claim(claim, actor=self.second_manager, amount=80_000_000)

        with self.assertRaises(SrpStateError) as caught:
            review.approve_claim(
                SrpClaim.objects.get(pk=claim.pk), actor=self.manager, amount=50_000_000, reason="Half",
                expected_status="pending",
            )

        self.assertEqual(caught.exception.code, "changed")
        self.assertEqual(fresh(claim).approved_amount, 80_000_000)

    def test_the_status_the_page_showed_lets_the_decision_through(self):
        pending = self.submit()
        review.approve_claim(pending, actor=self.manager, amount=80_000_000, expected_status="pending")
        review.approve_claim(pending, actor=self.manager, amount=70_000_000, reason="Less", expected_status="approved")
        self.assertEqual(fresh(pending).approved_amount, 70_000_000)

    def test_a_paid_amount_that_changed_is_not_booked(self):
        claim = self.approved(amount=10_000_000)
        review.approve_claim(claim, actor=self.second_manager, amount=4_000_000, reason="Cargo was not covered")

        result = review.mark_paid([claim], actor=self.manager, expected_amounts={claim.pk: 10_000_000})

        self.assertEqual(result.done, [])
        ((skipped, text),) = result.skipped
        self.assertEqual(skipped, claim)
        self.assertIn("changed from 10,000,000 ISK to 4,000,000 ISK", text)
        stored = fresh(claim)
        self.assertEqual((stored.status, stored.approved_amount, stored.paid_at), (Status.APPROVED, 4_000_000, None))
        self.assertFalse(claim.events.filter(action=Action.PAID).exists())

    def test_the_payment_of_the_shown_amount_is_booked(self):
        shown, unshown = self.approved(amount=10_000_000), self.approved(self.other, amount=20_000_000)

        result = review.mark_paid(
            [shown, unshown], actor=self.second_manager, expected_amounts={shown.pk: 10_000_000}
        )

        self.assertEqual(set(result.done), {shown, unshown})
        self.assertEqual(self.event(shown, Action.PAID).amount, 10_000_000)

    def test_one_changed_amount_does_not_stop_the_other_payments(self):
        changed, steady = self.approved(amount=10_000_000), self.approved(self.other, amount=20_000_000)
        review.approve_claim(changed, actor=self.second_manager, amount=5_000_000, reason="Smaller")

        result = review.mark_paid(
            [changed, steady], actor=self.manager, expected_amounts={changed.pk: 10_000_000, steady.pk: 20_000_000}
        )

        self.assertEqual(result.done, [steady])
        self.assertEqual([claim for claim, _text in result.skipped], [changed])


class TimelineAndAuditTests(ReviewBase):
    """A claim's whole life leaves one event and one audit entry per step."""

    def test_the_timeline_of_a_claim(self):
        claim = self.submit()
        review.approve_claim(claim, actor=self.manager, amount=70_000_000, reason="Smaller fit")
        review.approve_claim(claim, actor=self.manager, amount=75_000_000, reason="Rigs counted")
        review.mark_paid([claim], actor=self.second_manager, note="Sent")
        review.undo_payment(fresh(claim), actor=self.second_manager, reason="Wrong pilot")
        review.revoke_approval(fresh(claim), actor=self.second_manager, reason="Check again")
        review.reject_claim(fresh(claim), actor=self.second_manager, reason="Not covered")
        review.reopen_claim(fresh(claim), actor=self.second_manager, reason="Appeal")
        review.add_note(claim, actor=self.second_manager, text="Looks fine")
        actions = list(claim.events.order_by("created_at", "pk").values_list("action", flat=True))
        self.assertEqual(
            actions,
            ["submit", "approve", "amount_change", "paid", "unpaid", "revoke", "reject", "reopen", "note"],
        )
        audit_actions = set(AuditLog.objects.filter(object_type="SrpClaim", object_id=str(claim.pk)).values_list("action", flat=True))
        self.assertEqual(
            audit_actions,
            {f"srp.claim_{name}" for name in actions if name != "paid"} | {"srp.claim_paid"},
        )

    def test_a_refused_action_leaves_no_trace(self):
        claim = self.submit()
        events = claim.events.count()
        audits = AuditLog.objects.count()
        for call in (
            lambda: review.approve_claim(claim, actor=self.manager, amount=1),
            lambda: review.reject_claim(claim, actor=self.manager, reason=""),
            lambda: review.revoke_approval(claim, actor=self.manager, reason="x"),
            lambda: review.undo_payment(claim, actor=self.manager, reason="x"),
            lambda: review.reopen_claim(claim, actor=self.manager, reason="x"),
        ):
            with self.assertRaises(SrpError):
                call()
        self.assertEqual((claim.events.count(), AuditLog.objects.count()), (events, audits))

    def test_audit_entries_never_hold_secrets_or_the_whole_claim(self):
        claim = self.submit()
        review.approve_claim(claim, actor=self.manager, amount=80_000_000)
        entry = self.audit(claim, "srp.claim_approve")
        self.assertEqual(set(entry.new_value), {"status", "approved_amount"})


class NotifyTests(ReviewBase):
    def claim(self, **kwargs):
        return f.srp_claim(self.user, amount=12_000_000, status=Status.APPROVED, **kwargs)

    def test_titles_and_levels(self):
        cases = {
            "approve": ("SRP approved", "success"),
            "amount_change": ("SRP amount changed", "info"),
            "paid": ("SRP paid", "success"),
            "reject": ("SRP rejected", "danger"),
            "auto_reject": ("SRP rejected", "danger"),
            "revoke": ("SRP approval revoked", "warning"),
            "reopen": ("SRP claim reopened", "info"),
        }
        claim = self.claim()
        for action, (title, level) in cases.items():
            with self.subTest(action):
                Notification.objects.all().delete()
                notify.notify_claimant(claim, action)
                notification = Notification.objects.get()
                self.assertEqual(notification.user, self.user)
                self.assertEqual(notification.title, f"{title}: {claim.ship_type_name}")
                self.assertEqual(notification.level, level)

    def test_message_is_plain_text_with_the_loss_amount_and_reason(self):
        claim = self.claim()
        claim.decision_reason = "Matches the doctrine"
        notify.notify_claimant(claim, "approve")
        message = Notification.objects.get().message
        lines = message.split("\n")
        self.assertEqual(lines[0], f"Claim: {claim.reference}")
        self.assertIn(f"Ship: {claim.ship_type_name}", lines)
        self.assertTrue(any(line.startswith("Loss time: ") and line.endswith(" EVE") for line in lines))
        self.assertIn("Amount: 12,000,000 ISK", lines)
        self.assertIn("Reason: Matches the doctrine", lines)
        self.assertNotIn("<", message)

    def test_rejection_has_the_reason_and_no_amount(self):
        claim = f.srp_claim(self.user, status=Status.REJECTED, decision_reason="Not covered")
        notify.notify_claimant(claim, "reject")
        message = Notification.objects.get().message
        self.assertIn("Reason: Not covered", message)
        self.assertNotIn("Amount:", message)

    def test_payment_message_has_the_amount_but_not_the_reason(self):
        claim = self.claim(decision_reason="Matches the doctrine")
        notify.notify_claimant(claim, "paid")
        message = Notification.objects.get().message
        self.assertIn("Amount: 12,000,000 ISK", message)
        self.assertNotIn("Reason", message)

    def test_switched_off(self):
        self.settings(notify_members=False)
        notify.notify_claimant(self.claim(), "approve")
        self.assertFalse(Notification.objects.exists())

    def test_nothing_for_unknown_actions_or_claims_without_a_user(self):
        claim = self.claim()
        notify.notify_claimant(claim, "note")
        notify.notify_claimant(claim, "something")
        SrpClaim.objects.filter(pk=claim.pk).update(user=None)
        notify.notify_claimant(fresh(claim), "approve")
        self.assertFalse(Notification.objects.exists())

    def test_a_failure_is_logged_and_ignored(self):
        claim = self.claim()
        with mock.patch.object(notify.notify, "success", side_effect=RuntimeError("secret token abc")):
            with self.assertLogs("extensions.fleetops.srp.notify", level=logging.WARNING) as logged:
                notify.notify_claimant(claim, "approve")
        text = " ".join(logged.output)
        self.assertIn("RuntimeError", text)
        self.assertNotIn("secret token abc", text)

    def test_long_ship_names_fit_the_title(self):
        claim = self.claim(ship_type_name="Z" * 255)
        notify.notify_claimant(claim, "approve")
        self.assertEqual(len(Notification.objects.get().title), 254)

    def test_decisions_notify_after_the_commit_only(self):
        claim = self.submit()
        with self.captureOnCommitCallbacks(execute=False) as callbacks:
            review.approve_claim(claim, actor=self.manager, amount=80_000_000)
        self.assertEqual(len(callbacks), 1)
        self.assertFalse(Notification.objects.exists())
        callbacks[0]()
        self.assertEqual(Notification.objects.count(), 1)

    def test_a_failing_notification_does_not_undo_the_decision(self):
        claim = self.submit()
        with mock.patch.object(notify.notify, "success", side_effect=RuntimeError("down")):
            with self.captureOnCommitCallbacks(execute=True):
                review.approve_claim(claim, actor=self.manager, amount=80_000_000)
        self.assertEqual(fresh(claim).status, Status.APPROVED)

    def test_no_notification_when_the_decision_fails(self):
        claim = self.submit()
        with self.captureOnCommitCallbacks(execute=True):
            with self.assertRaises(SrpError):
                review.approve_claim(claim, actor=self.manager, amount=1)
        self.assertFalse(Notification.objects.exists())


class SettingsEdgeTests(ReviewBase):
    def test_approval_uses_the_locked_settings_row(self):
        claim = self.submit()
        with mock.patch.object(quota, "quota_lock", wraps=quota.quota_lock) as lock:
            review.approve_claim(claim, actor=self.manager, amount=80_000_000)
        lock.assert_called_once_with()

    def test_every_status_change_runs_under_the_quota_lock(self):
        claim = self.submit()
        calls = []
        real = quota.quota_lock

        def spy():
            calls.append(1)
            return real()

        with mock.patch.object(quota, "quota_lock", side_effect=spy):
            review.reject_claim(claim, actor=self.manager, reason="x")
            review.reopen_claim(claim, actor=self.manager, reason="x")
            review.approve_claim(claim, actor=self.manager, amount=80_000_000)
            review.mark_paid([claim], actor=self.second_manager)
            review.undo_payment(fresh(claim), actor=self.manager, reason="x")
            review.revoke_approval(fresh(claim), actor=self.manager, reason="x")
            review.recalculate_claim(fresh(claim), actor=self.manager)
        self.assertEqual(len(calls), 7)

    def test_the_settings_row_exists_after_the_first_decision_even_if_it_was_missing(self):
        claim = self.submit()
        SrpSettings.objects.filter(pk=1).delete()
        review.reject_claim(claim, actor=self.manager, reason="x")
        self.assertTrue(SrpSettings.objects.filter(pk=1).exists())

    def test_claim_instance_of_the_caller_is_updated(self):
        claim = self.submit()
        review.approve_claim(claim, actor=self.manager, amount=70_000_000, reason="x")
        self.assertEqual((claim.status, claim.approved_amount, claim.decided_by), (Status.APPROVED, 70_000_000, self.manager))
        review.mark_paid([claim], actor=self.second_manager)
        self.assertEqual(claim.status, Status.PAID)
        self.assertEqual(claim.paid_by, self.second_manager)
