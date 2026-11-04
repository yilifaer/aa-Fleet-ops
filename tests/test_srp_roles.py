# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""REV-1 and REV-2: the review and the payment permission, and the three self-processing settings.

Access functions and menu (all combinations of manage_srp, review_srp, pay_srp and request_srp), the services
(who may review, who may pay, own claims, the approver rule, bulk skip reasons), the pages (queue, payments page,
claim page buttons, actions by URL) and the settings page with its audit entry.
"""

import itertools
from datetime import timedelta

from django.contrib.auth.models import AnonymousUser
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from fleetops.models import AuditLog, SrpClaim, SrpSettings
from fleetops.srp import access, links, review
from fleetops.srp.types import SrpError
from fleetops.texts import translate_stored

from . import factories as f
from .test_srp_views import SrpPagesTestCase, messages_of, route

Status = SrpClaim.Status
PERMISSIONS = {
    "manage": ["fleetops.manage_srp"],
    "review": ["fleetops.review_srp"],
    "pay": ["fleetops.pay_srp"],
    "both": ["fleetops.review_srp", "fleetops.pay_srp"],
    "none": [],
}
# (is_reviewer, is_payer, is_srp_staff) of each combination.
EXPECTED = {
    "manage": (True, True, True),
    "review": (True, False, True),
    "pay": (False, True, True),
    "both": (True, True, True),
    "none": (False, False, False),
}


def make_users(prefix=""):
    """One user per combination of the SRP permissions, with and without request_srp."""
    users = {}
    for name, perms in PERMISSIONS.items():
        users[name] = f.create_user(f"{prefix}{name}", perms=f.MEMBER_PERMS + perms)
        users[f"{name}+request"] = f.create_user(f"{prefix}{name}-request", perms=f.SRP_MEMBER_PERMS + perms)
    return users


class RoleFunctionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        f.srp_settings(enabled=True)
        cls.users = make_users()
        cls.claimant = f.create_user("claimant", perms=f.SRP_MEMBER_PERMS)
        cls.claim = f.srp_claim(cls.claimant)

    def test_roles_of_every_combination_with_and_without_request_srp(self):
        for key, user in self.users.items():
            reviewer, payer, staff = EXPECTED[key.split("+")[0]]
            with self.subTest(user=key):
                self.assertEqual(access.is_reviewer(user), reviewer)
                self.assertEqual(access.is_payer(user), payer)
                self.assertEqual(access.is_srp_staff(user), staff)
                # is_manager is the old name of is_srp_staff.
                self.assertEqual(access.is_manager(user), staff)
                self.assertEqual(access.sees_everything(user), staff)
                self.assertEqual(access.can_view_internal_notes(user), staff)
                self.assertEqual(access.can_view_claim(user, self.claim), staff)
                self.assertEqual(access.can_review(user, self.claim), reviewer)
                self.assertEqual(access.can_pay(user, self.claim), payer)
                self.assertEqual(access.summary_access(user)["manager"], reviewer)
                self.assertEqual(access.summary_access(user)["alliance"], staff)
                self.assertEqual(access.can_request(user), key.endswith("+request"))

    def test_staff_open_every_claim_and_the_alliance_summary(self):
        other = f.srp_claim(f.create_user("far", perms=f.SRP_MEMBER_PERMS, corporation=f.OTHER_CORP))
        for key in ("review", "pay", "both", "manage"):
            user = self.users[key]
            with self.subTest(user=key):
                self.assertTrue(access.can_view_claim(user, other))
                self.assertTrue(access.can_view_alliance_summary(user))
                self.assertTrue(access.can_view_corporation_summary(user, f.OTHER_CORP[0]))
                self.assertTrue(access.can_view_fc_summary(user, 12345))
                self.assertEqual(set(access.viewable_claims(user)), set(SrpClaim.objects.all()))
        for key in ("none", "none+request"):
            user = self.users[key]
            with self.subTest(user=key):
                self.assertFalse(access.can_view_claim(user, other))
                self.assertFalse(access.can_view_alliance_summary(user))
                self.assertEqual(set(access.viewable_claims(user)), set())

    def test_anonymous_has_no_role(self):
        user = AnonymousUser()

        self.assertEqual((access.is_reviewer(user), access.is_payer(user), access.is_srp_staff(user)), (False,) * 3)
        self.assertFalse(access.can_pay(user, self.claim))

    def test_an_inactive_user_has_no_role(self):
        user = f.create_user("gone", perms=f.MEMBER_PERMS + PERMISSIONS["both"])
        user.is_active = False
        user.save()

        self.assertEqual((access.is_reviewer(user), access.is_payer(user), access.is_srp_staff(user)), (False,) * 3)

    def test_the_other_stats_permissions_are_no_srp_role(self):
        user = f.create_user("stats", perms=f.MEMBER_PERMS + ["fleetops.view_all_stats", "fleetops.manage_configuration"])

        self.assertEqual((access.is_reviewer(user), access.is_payer(user), access.is_srp_staff(user)), (False,) * 3)
        self.assertTrue(access.sees_everything(user))
        self.assertFalse(access.can_view_internal_notes(user))


class SelfProcessingMatrixTests(TestCase):
    """Three switches, every combination: allow_self_review, allow_self_payment, require_different_payer."""

    @classmethod
    def setUpTestData(cls):
        f.srp_settings(enabled=True)
        cls.both = f.create_user("both", perms=f.SRP_MEMBER_PERMS + PERMISSIONS["both"])
        cls.other = f.create_user("other", perms=f.SRP_MEMBER_PERMS + PERMISSIONS["both"])
        cls.reviewer = f.create_user("reviewer", perms=f.MEMBER_PERMS + PERMISSIONS["review"])
        cls.payer = f.create_user("payer", perms=f.MEMBER_PERMS + PERMISSIONS["pay"])

    def settings(self, review_own, pay_own, different):
        return f.srp_settings(
            allow_self_review=review_own, allow_self_payment=pay_own, require_different_payer=different
        )

    def test_every_combination(self):
        for review_own, pay_own, different in itertools.product((False, True), repeat=3):
            own = f.srp_claim(self.both, status=Status.APPROVED, decided_by=self.other)
            approved_by_me = f.srp_claim(self.other, status=Status.APPROVED, decided_by=self.both)
            approved_by_other = f.srp_claim(self.other, status=Status.APPROVED, decided_by=self.reviewer)
            own_pending = f.srp_claim(self.both)
            settings_obj = self.settings(review_own, pay_own, different)
            with self.subTest(review_own=review_own, pay_own=pay_own, different=different):
                self.assertEqual(
                    access.can_review(self.both, own_pending, settings_obj=settings_obj), review_own
                )
                self.assertEqual(access.can_pay(self.both, own, settings_obj=settings_obj), pay_own)
                self.assertEqual(
                    access.can_pay(self.both, approved_by_me, settings_obj=settings_obj), not different
                )
                self.assertTrue(access.can_pay(self.both, approved_by_other, settings_obj=settings_obj))
                # allow_self_review does not open payments, allow_self_payment does not open decisions.
                self.assertEqual(access.can_review(self.both, own, settings_obj=settings_obj), review_own)

    def test_the_reason_of_a_refusal(self):
        settings_obj = self.settings(False, False, True)
        own = f.srp_claim(self.both, status=Status.APPROVED, decided_by=self.both)
        approved_by_me = f.srp_claim(self.other, status=Status.APPROVED, decided_by=self.both)

        self.assertEqual(access.payment_block(self.both, own, settings_obj=settings_obj), access.OWN_CLAIM_PAY)
        self.assertEqual(
            access.payment_block(self.both, approved_by_me, settings_obj=settings_obj), access.OWN_APPROVAL_PAY
        )
        self.assertEqual(access.payment_block(self.reviewer, own, settings_obj=settings_obj), access.NOT_A_PAYER)
        self.assertEqual(
            access.payment_block(self.both, approved_by_me, settings_obj=settings_obj, approver_rule=False), ""
        )
        self.assertEqual(access.payment_block(self.payer, approved_by_me, settings_obj=settings_obj), "")

    def test_a_claim_without_an_approver_or_a_user_has_no_personal_block(self):
        settings_obj = self.settings(False, False, True)
        claim = f.srp_claim(self.other, status=Status.APPROVED, decided_by=None)
        SrpClaim.objects.filter(pk=claim.pk).update(user=None)
        claim.refresh_from_db()

        self.assertTrue(access.can_pay(self.both, claim, settings_obj=settings_obj))

    def test_a_settings_object_costs_no_query(self):
        settings_obj = SrpSettings.get_solo()
        claim = f.srp_claim(self.other, status=Status.APPROVED, decided_by=self.reviewer)
        self.both.has_perm("fleetops.pay_srp")

        with self.assertNumQueries(0):
            access.can_pay(self.both, claim, settings_obj=settings_obj)


class ServiceBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        f.srp_settings(enabled=True)
        cls.pilot = f.create_user("pilot", perms=f.SRP_MEMBER_PERMS)
        cls.reviewer = f.create_user("reviewer", perms=f.MEMBER_PERMS + PERMISSIONS["review"])
        cls.payer = f.create_user("payer", perms=f.MEMBER_PERMS + PERMISSIONS["pay"])
        cls.payer2 = f.create_user("payer2", perms=f.MEMBER_PERMS + PERMISSIONS["pay"])
        cls.manager = f.create_user("manager", perms=f.SRP_MANAGER_PERMS)
        cls.manager2 = f.create_user("manager2", perms=f.SRP_MANAGER_PERMS)
        cls.reviewer_payer = f.create_user("reviewer-payer", perms=f.SRP_MEMBER_PERMS + PERMISSIONS["both"])

    def pending(self, user=None):
        return f.srp_claim(user or self.pilot)

    def approved(self, user=None, by=None):
        return f.srp_claim(user or self.pilot, status=Status.APPROVED, decided_by=by or self.reviewer)

    def paid(self, user=None):
        return f.srp_claim(user or self.pilot, status=Status.PAID, decided_by=self.reviewer, paid_by=self.payer)

    def code_of(self, call):
        with self.assertRaises(SrpError) as caught:
            call()
        return caught.exception.code


class ReviewerRoleServiceTests(ServiceBase):
    def test_a_reviewer_does_every_review_step(self):
        claim = self.pending()
        review.approve_claim(claim, actor=self.reviewer, amount=claim.suggested_amount)
        review.revoke_approval(claim, actor=self.reviewer, reason="Wrong")
        review.reject_claim(claim, actor=self.reviewer, reason="No")
        review.reopen_claim(claim, actor=self.reviewer, reason="Appeal")
        review.recalculate_claim(claim, actor=self.reviewer)
        review.recheck_presence(claim, actor=self.reviewer)
        review.add_note(claim, actor=self.reviewer, text="Looked at it")
        self.assertEqual(review.recalculate_pending(actor=self.reviewer), 1)
        self.assertEqual(SrpClaim.objects.get(pk=claim.pk).status, Status.PENDING)

    def test_a_reviewer_recategorizes_and_reclassifies(self):
        claim = self.pending()
        category = f.srp_category("Solo")

        review.recategorize_claim(claim, actor=self.reviewer, category=category, reason="Solo loss")

        self.assertEqual(SrpClaim.objects.get(pk=claim.pk).category, category)

    def test_a_reviewer_approves_in_bulk(self):
        result = review.approve_clean_claims([self.pending()], actor=self.reviewer)

        self.assertEqual(len(result.done), 1)

    def test_a_reviewer_cannot_pay_or_undo_a_payment(self):
        approved, paid = self.approved(), self.paid()

        self.assertEqual(self.code_of(lambda: review.mark_paid([approved], actor=self.reviewer)), "forbidden")
        self.assertEqual(
            self.code_of(lambda: review.undo_payment(paid, actor=self.reviewer, reason="No")), "forbidden"
        )
        self.assertEqual(SrpClaim.objects.get(pk=approved.pk).status, Status.APPROVED)
        self.assertEqual(SrpClaim.objects.get(pk=paid.pk).status, Status.PAID)

    def test_the_refusal_names_the_payer_role(self):
        with self.assertRaises(SrpError) as caught:
            review.mark_paid([self.approved()], actor=self.reviewer)

        self.assertEqual(translate_stored(caught.exception.message), "You are not allowed to pay SRP claims.")


class PayerRoleServiceTests(ServiceBase):
    def test_a_payer_pays_and_undoes_a_payment(self):
        claim = self.approved()

        result = review.mark_paid([claim], actor=self.payer, note="Sent")
        self.assertEqual(result.done, [claim])
        review.undo_payment(claim, actor=self.payer, reason="Not received")

        self.assertEqual(SrpClaim.objects.get(pk=claim.pk).status, Status.APPROVED)

    def test_a_payer_cannot_review(self):
        pending, approved, rejected = self.pending(), self.approved(), f.srp_claim(self.pilot, status=Status.REJECTED)
        calls = {
            "approve": lambda: review.approve_claim(pending, actor=self.payer, amount=pending.suggested_amount),
            "reject": lambda: review.reject_claim(pending, actor=self.payer, reason="No"),
            "revoke": lambda: review.revoke_approval(approved, actor=self.payer, reason="No"),
            "reopen": lambda: review.reopen_claim(rejected, actor=self.payer, reason="No"),
            "reclassify": lambda: review.reclassify_claim(pending, actor=self.payer, scope="out_of_fleet", reason="x"),
            "recategorize": lambda: review.recategorize_claim(pending, actor=self.payer, category=None, reason="x"),
            "recalculate": lambda: review.recalculate_claim(pending, actor=self.payer),
            "recheck": lambda: review.recheck_presence(pending, actor=self.payer),
            "approve_clean": lambda: review.approve_clean_claims([pending], actor=self.payer),
            "recalculate_pending": lambda: review.recalculate_pending(actor=self.payer),
        }
        for name, call in calls.items():
            with self.subTest(action=name):
                self.assertEqual(self.code_of(call), "forbidden")
        self.assertEqual(SrpClaim.objects.get(pk=pending.pk).status, Status.PENDING)
        self.assertEqual(SrpClaim.objects.get(pk=approved.pk).status, Status.APPROVED)

    def test_the_refusal_names_the_reviewer_role(self):
        with self.assertRaises(SrpError) as caught:
            review.reject_claim(self.pending(), actor=self.payer, reason="No")

        self.assertEqual(translate_stored(caught.exception.message), "You are not allowed to review SRP claims.")

    def test_a_payer_adds_a_note(self):
        review.add_note(self.approved(), actor=self.payer, text="Paid in two parts")

    def test_nobody_else_adds_a_note(self):
        claim = self.pending()
        for actor in (self.pilot, None):
            with self.subTest(actor=actor):
                self.assertEqual(self.code_of(lambda: review.add_note(claim, actor=actor, text="x")), "forbidden")


class ManagerKeepsBothTests(ServiceBase):
    def test_manage_srp_reviews_and_pays(self):
        claim = self.pending()
        review.approve_claim(claim, actor=self.manager, amount=claim.suggested_amount)
        review.mark_paid([claim], actor=self.manager2)
        review.undo_payment(claim, actor=self.manager2, reason="Not received")
        review.recheck_presence(claim, actor=self.manager)
        review.add_note(claim, actor=self.manager, text="x")

        self.assertEqual(SrpClaim.objects.get(pk=claim.pk).status, Status.APPROVED)

    def test_without_the_new_settings_the_approver_may_still_pay(self):
        claim = self.pending()
        review.approve_claim(claim, actor=self.manager, amount=claim.suggested_amount)

        self.assertEqual(review.mark_paid([claim], actor=self.manager).done, [claim])


class SelfProcessingServiceTests(ServiceBase):
    def test_new_installs_forbid_own_review_and_own_payment(self):
        row = SrpSettings.get_solo()

        self.assertEqual((row.allow_self_review, row.allow_self_payment, row.require_different_payer), (False,) * 3)
        own = self.approved(self.reviewer_payer)
        result = review.mark_paid([own], actor=self.reviewer_payer)
        self.assertEqual(result.done, [])
        self.assertEqual(result.skipped, [(own, "Your own claims are paid by another payer.")])
        pending = self.pending(self.reviewer_payer)
        self.assertEqual(
            self.code_of(lambda: review.approve_claim(pending, actor=self.reviewer_payer, amount=1)), "own_claim"
        )

    def test_review_own_and_pay_own_are_separate(self):
        f.srp_settings(allow_self_review=True)
        pending = self.pending(self.reviewer_payer)
        approved = self.approved(self.reviewer_payer)

        review.approve_claim(pending, actor=self.reviewer_payer, amount=pending.suggested_amount)
        self.assertEqual(review.mark_paid([approved], actor=self.reviewer_payer).done, [])

        f.srp_settings(allow_self_review=False, allow_self_payment=True)
        self.assertEqual(review.mark_paid([approved], actor=self.reviewer_payer).done, [approved])
        another = self.pending(self.reviewer_payer)
        self.assertEqual(
            self.code_of(lambda: review.reject_claim(another, actor=self.reviewer_payer, reason="x")), "own_claim"
        )

    def test_a_single_refusal_of_a_payment_has_its_own_code_and_text(self):
        own = self.approved(self.reviewer_payer)

        with self.assertRaises(SrpError) as caught:
            review._mark_paid(own, self.reviewer_payer, "")

        self.assertEqual(caught.exception.code, "own_claim_pay")
        self.assertEqual(translate_stored(caught.exception.message), "Another payer has to pay your own claim.")
        self.assertEqual(SrpClaim.objects.get(pk=own.pk).status, Status.APPROVED)

    def test_the_four_eyes_rule(self):
        f.srp_settings(require_different_payer=True)
        claim = self.pending()
        review.approve_claim(claim, actor=self.reviewer_payer, amount=claim.suggested_amount)

        # A approves, A cannot pay.
        result = review.mark_paid([claim], actor=self.reviewer_payer)
        self.assertEqual(result.done, [])
        self.assertEqual(result.skipped, [(claim, "You approved this claim; a different payer has to pay it.")])
        with self.assertRaises(SrpError) as caught:
            review._mark_paid(claim, self.reviewer_payer, "")
        self.assertEqual(caught.exception.code, "own_approval_pay")
        self.assertEqual(translate_stored(caught.exception.message), "A different payer has to pay a claim you approved.")
        self.assertEqual(SrpClaim.objects.get(pk=claim.pk).status, Status.APPROVED)
        # B pays.
        self.assertEqual(review.mark_paid([claim], actor=self.payer).done, [claim])

    def test_the_four_eyes_rule_does_not_touch_the_undo(self):
        f.srp_settings(require_different_payer=True)
        claim = f.srp_claim(self.pilot, status=Status.PAID, decided_by=self.reviewer_payer, paid_by=self.payer)

        review.undo_payment(claim, actor=self.reviewer_payer, reason="Not received")

        self.assertEqual(SrpClaim.objects.get(pk=claim.pk).status, Status.APPROVED)

    def test_undoing_the_payment_of_an_own_claim_needs_the_payment_setting(self):
        own = f.srp_claim(self.reviewer_payer, status=Status.PAID, decided_by=self.reviewer, paid_by=self.payer)
        self.assertEqual(
            self.code_of(lambda: review.undo_payment(own, actor=self.reviewer_payer, reason="x")), "own_claim_pay"
        )

        f.srp_settings(allow_self_payment=True)
        review.undo_payment(own, actor=self.reviewer_payer, reason="x")

        self.assertEqual(SrpClaim.objects.get(pk=own.pk).status, Status.APPROVED)

    def test_the_bulk_skip_reasons_are_named_per_claim(self):
        f.srp_settings(require_different_payer=True)
        own = self.approved(self.reviewer_payer)
        mine_to_approve = f.srp_claim(self.pilot, status=Status.APPROVED, decided_by=self.reviewer_payer)
        free = self.approved(self.pilot)

        result = review.mark_paid([own, mine_to_approve, free], actor=self.reviewer_payer)

        self.assertEqual(result.done, [free])
        self.assertEqual(
            sorted((claim.pk, text) for claim, text in result.skipped),
            sorted(
                [
                    (own.pk, "Your own claims are paid by another payer."),
                    (mine_to_approve.pk, "You approved this claim; a different payer has to pay it."),
                ]
            ),
        )

    def test_the_settings_are_read_under_the_lock(self):
        claim = self.approved(self.reviewer_payer)
        f.srp_settings(allow_self_payment=True)
        review.mark_paid([claim], actor=self.reviewer_payer)
        claim2 = self.approved(self.reviewer_payer)
        f.srp_settings(allow_self_payment=False)

        self.assertEqual(review.mark_paid([claim2], actor=self.reviewer_payer).done, [])

    def test_a_note_and_a_recheck_are_not_bound_by_the_settings(self):
        f.srp_settings(require_different_payer=True)
        own = self.pending(self.reviewer_payer)

        review.add_note(own, actor=self.reviewer_payer, text="Context")
        review.recheck_presence(own, actor=self.reviewer_payer)

    def test_the_recalculation_of_all_pending_claims_skips_the_own_ones(self):
        own = self.pending(self.reviewer_payer)
        self.pending()

        self.assertEqual(review.recalculate_pending(actor=self.reviewer_payer), 1)
        f.srp_settings(allow_self_review=True)
        self.assertEqual(review.recalculate_pending(actor=self.reviewer_payer), 2)
        self.assertEqual(SrpClaim.objects.get(pk=own.pk).status, Status.PENDING)


class RolePagesBase(SrpPagesTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.reviewer = f.create_user("srpreviewer", perms=f.SRP_REVIEWER_PERMS)
        cls.payer = f.create_user("srppayer", perms=f.SRP_PAYER_PERMS)
        cls.payer2 = f.create_user("srppayertwo", perms=f.SRP_PAYER_PERMS)
        cls.both = f.create_user("srpboth", perms=f.SRP_MEMBER_PERMS + PERMISSIONS["both"])

    def action_url(self, claim, action):
        return route("srp_claim_action", claim.uuid, action)

    def post(self, user, claim, action, data=None):
        return self.client_for(user).post(self.action_url(claim, action), data or {})


class ActionUrlTests(RolePagesBase):
    """Every action URL with a single permission."""

    REVIEW = {
        "approve": {"mode": "suggested"},
        "reject": {"reason": "Not covered"},
        "revoke": {"reason": "Wrong amount"},
        "reopen": {"reason": "Appeal"},
        "reclassify": {"scope": "out_of_fleet", "reason": "Lost on the way home"},
        "recategorize": {"category": "", "reason": "No category"},
        "recalculate": {},
        "recheck": {},
    }
    PAY = {"paid": {"note": "Sent"}, "unpaid": {"reason": "Not received"}}

    def test_a_reviewer_reaches_the_review_actions_and_not_the_payment_actions(self):
        claim = f.srp_claim(self.member, time=self.loss_time)
        for action, data in self.REVIEW.items():
            with self.subTest(action=action):
                # A form that does not fit this claim shows the page again (200); the point is that it is not a 403.
                self.assertIn(self.post(self.reviewer, claim, action, data).status_code, (200, 302))
        for action, data in self.PAY.items():
            with self.subTest(action=action):
                self.assertEqual(self.post(self.reviewer, claim, action, data).status_code, 403)
        self.assertEqual(self.post(self.reviewer, claim, "note", {"text": "Seen"}).status_code, 302)

    def test_a_payer_reaches_the_payment_actions_and_not_the_review_actions(self):
        claim = f.srp_claim(self.member, time=self.loss_time)
        for action, data in self.REVIEW.items():
            with self.subTest(action=action):
                self.assertEqual(self.post(self.payer, claim, action, data).status_code, 403)
        for action, data in self.PAY.items():
            with self.subTest(action=action):
                self.assertEqual(self.post(self.payer, claim, action, data).status_code, 302)
        self.assertEqual(self.post(self.payer, claim, "note", {"text": "Seen"}).status_code, 302)
        claim.refresh_from_db()
        self.assertEqual(claim.status, Status.PENDING)

    def test_a_denied_action_changes_nothing(self):
        approved = f.srp_claim(self.member, status=Status.APPROVED, decided_by=self.manager, time=self.loss_time)

        self.assertEqual(self.post(self.reviewer, approved, "paid", {"note": "x"}).status_code, 403)
        pending = f.srp_claim(self.other, time=self.loss_time)
        self.assertEqual(self.post(self.payer, pending, "approve", {"mode": "suggested"}).status_code, 403)

        approved.refresh_from_db(), pending.refresh_from_db()
        self.assertEqual((approved.status, pending.status), (Status.APPROVED, Status.PENDING))
        self.assertFalse(pending.events.filter(action="approve").exists())

    def test_a_payer_really_pays_and_undoes(self):
        claim = f.srp_claim(self.member, status=Status.APPROVED, decided_by=self.manager, time=self.loss_time)

        self.assertEqual(self.post(self.payer, claim, "paid", {"note": "Sent"}).status_code, 302)
        claim.refresh_from_db()
        self.assertEqual((claim.status, claim.paid_by), (Status.PAID, self.payer))
        self.assertEqual(self.post(self.payer, claim, "unpaid", {"reason": "Not received"}).status_code, 302)
        claim.refresh_from_db()
        self.assertEqual(claim.status, Status.APPROVED)

    def test_members_and_view_all_stats_reach_no_action(self):
        claim = f.srp_claim(self.member, time=self.loss_time)
        for user in (self.member, self.stats, self.no_srp):
            for action in (*self.REVIEW, *self.PAY, "note"):
                with self.subTest(user=user.username, action=action):
                    self.assertEqual(self.post(user, claim, action, {"text": "x"}).status_code, 403)

    def test_the_four_eyes_rule_on_the_action_url(self):
        SrpSettings.objects.filter(pk=1).update(require_different_payer=True)
        claim = f.srp_claim(self.member, status=Status.APPROVED, decided_by=self.both, time=self.loss_time)

        self.assertEqual(self.post(self.both, claim, "paid", {"note": "x"}).status_code, 403)
        self.assertEqual(self.post(self.payer, claim, "paid", {"note": "x"}).status_code, 302)
        claim.refresh_from_db()
        self.assertEqual(claim.status, Status.PAID)
        # Undoing is not bound by the rule.
        self.assertEqual(self.post(self.both, claim, "unpaid", {"reason": "x"}).status_code, 302)

    def test_paying_an_own_claim_needs_allow_self_payment_not_allow_self_review(self):
        own = f.srp_claim(self.both, status=Status.APPROVED, decided_by=self.manager, time=self.loss_time)
        SrpSettings.objects.filter(pk=1).update(allow_self_review=True)
        self.assertEqual(self.post(self.both, own, "paid", {"note": "x"}).status_code, 403)

        SrpSettings.objects.filter(pk=1).update(allow_self_payment=True)

        self.assertEqual(self.post(self.both, own, "paid", {"note": "x"}).status_code, 302)


class ReviewPagesTests(RolePagesBase):
    def test_review_queue_and_bulk_routes_need_the_review_permission(self):
        self.assertEqual(self.client_for(self.reviewer).get(route("srp_review")).status_code, 200)
        self.assertEqual(self.client_for(self.payer).get(route("srp_review")).status_code, 403)
        for user, expected in ((self.reviewer, 302), (self.payer, 403)):
            with self.subTest(user=user.username):
                client = self.client_for(user)
                self.assertEqual(client.post(route("srp_review_bulk_approve"), {"claim": []}).status_code, expected)
                self.assertEqual(client.post(route("srp_review_recalculate")).status_code, expected)

    def test_the_403_page_of_the_queue_names_the_reason(self):
        response = self.client_for(self.payer).get(route("srp_review"))

        self.assertContains(response, "Only SRP managers can open this page.", status_code=403)

    def test_a_reviewer_has_no_payments_page(self):
        self.assertEqual(self.client_for(self.reviewer).get(route("srp_payments")).status_code, 403)
        self.assertEqual(self.client_for(self.reviewer).post(route("srp_payments"), {"claim": []}).status_code, 403)

    def test_the_queue_has_the_reviewer_texts(self):
        f.srp_claim(self.reviewer, time=self.loss_time)
        f.srp_claim(self.member, time=self.loss_time)

        response = self.client_for(self.reviewer).get(route("srp_review"))

        self.assertContains(response, "Your own claim: another reviewer has to decide on it.")


class PaymentPagesTests(RolePagesBase):
    def approved(self, user=None, by=None, **extra):
        return f.srp_claim(user or self.member, status=Status.APPROVED, decided_by=by or self.manager, time=self.loss_time, **extra)

    def test_a_payer_opens_the_payments_page_and_pays_in_bulk(self):
        first, second = self.approved(), self.approved(self.other)
        client = self.client_for(self.payer)

        page = client.get(route("srp_payments"))
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, f'name="claim" value="{first.uuid}"')
        response = client.post(
            route("srp_payments"),
            {"claim": [str(first.uuid), str(second.uuid)], "note": "March", f"shown-{first.uuid}": first.approved_amount},
        )

        self.assertEqual(response.status_code, 302)
        first.refresh_from_db(), second.refresh_from_db()
        self.assertEqual((first.status, second.status), (Status.PAID, Status.PAID))
        self.assertEqual(first.paid_by, self.payer)

    def test_the_tick_box_follows_the_payment_rules(self):
        SrpSettings.objects.filter(pk=1).update(require_different_payer=True, allow_self_review=True)
        own = self.approved(self.both, by=self.payer)
        mine = self.approved(self.member, by=self.both)
        free = self.approved(self.other, by=self.payer)

        response = self.client_for(self.both).get(route("srp_payments"))

        self.assertNotContains(response, f'name="claim" value="{own.uuid}"')
        self.assertNotContains(response, f'name="claim" value="{mine.uuid}"')
        self.assertContains(response, f'name="claim" value="{free.uuid}"')
        self.assertContains(response, "Your own claim: another payer has to pay it.")
        self.assertContains(response, "You approved this claim: a different payer has to pay it.")

    def test_a_bulk_payment_names_the_reason_it_skipped_a_claim(self):
        SrpSettings.objects.filter(pk=1).update(require_different_payer=True)
        own = self.approved(self.both, by=self.payer)
        mine = self.approved(self.member, by=self.both)
        free = self.approved(self.other, by=self.payer)
        client = self.client_for(self.both)

        response = client.post(
            route("srp_payments"), {"claim": [str(own.uuid), str(mine.uuid), str(free.uuid)], "note": ""}
        )

        text = " ".join(messages_of(response))
        self.assertIn("1 claim marked paid", text)
        self.assertIn("your own claim; another payer has to pay.", text)
        self.assertIn("you approved it; a different payer has to pay.", text)
        for claim, status in ((own, Status.APPROVED), (mine, Status.APPROVED), (free, Status.PAID)):
            claim.refresh_from_db()
            self.assertEqual(claim.status, status)

    def test_a_forged_post_for_a_claim_without_the_right_is_skipped(self):
        SrpSettings.objects.filter(pk=1).update(require_different_payer=True)
        mine = self.approved(self.member, by=self.both)

        self.client_for(self.both).post(route("srp_payments"), {"claim": [str(mine.uuid)], "note": ""})

        mine.refresh_from_db()
        self.assertEqual(mine.status, Status.APPROVED)

    def test_the_paid_claims_list_is_part_of_the_payments_page(self):
        paid = f.srp_claim(
            self.member, status=Status.PAID, decided_by=self.manager, paid_by=self.payer2, paid_at=timezone.now()
        )

        response = self.client_for(self.payer).get(route("srp_payments"))

        self.assertContains(response, paid.reference)


class ClaimPageTests(RolePagesBase):
    def page(self, user, claim):
        return self.client_for(user).get(route("srp_claim", claim.uuid))

    def has(self, response, claim, action):
        return f'action="{self.action_url(claim, action)}"' in response.content.decode()

    def test_a_reviewer_sees_review_buttons_and_no_payment_button(self):
        approved = f.srp_claim(self.member, status=Status.APPROVED, decided_by=self.manager, time=self.loss_time)
        pending = f.srp_claim(self.other, time=self.loss_time)

        page = self.page(self.reviewer, approved)
        self.assertTrue(self.has(page, approved, "revoke"))
        self.assertTrue(self.has(page, approved, "approve"))
        self.assertFalse(self.has(page, approved, "paid"))
        self.assertTrue(self.has(page, approved, "note"))
        self.assertTrue(self.has(page, approved, "recheck"))
        self.assertTrue(self.has(self.page(self.reviewer, pending), pending, "reject"))

    def test_a_payer_sees_payment_buttons_and_no_review_button(self):
        approved = f.srp_claim(self.member, status=Status.APPROVED, decided_by=self.manager, time=self.loss_time)
        paid = f.srp_claim(self.other, status=Status.PAID, decided_by=self.manager, time=self.loss_time)
        pending = f.srp_claim(self.other, time=self.loss_time)

        page = self.page(self.payer, approved)
        self.assertTrue(self.has(page, approved, "paid"))
        for action in ("approve", "revoke", "reject", "reclassify", "recheck"):
            self.assertFalse(self.has(page, approved, action), action)
        self.assertTrue(self.has(page, approved, "note"))
        self.assertTrue(self.has(self.page(self.payer, paid), paid, "unpaid"))
        pending_page = self.page(self.payer, pending)
        for action in ("approve", "reject", "recalculate", "recategorize", "recheck"):
            self.assertFalse(self.has(pending_page, pending, action), action)
        self.assertEqual(pending_page.status_code, 200)

    def test_a_payer_without_request_srp_opens_any_claim_and_sees_internal_notes(self):
        claim = f.srp_claim(self.member, time=self.loss_time)
        f.claim_event(claim, "note", actor=self.manager, internal=True, reason="Looks like an alt")

        page = self.page(self.payer, claim)

        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "Looks like an alt")
        self.assertFalse(page.context["can_decide"])
        self.assertTrue(page.context["is_staff"])

    def test_the_manager_page_is_as_before(self):
        approved = f.srp_claim(self.member, status=Status.APPROVED, decided_by=self.manager2, time=self.loss_time)

        page = self.page(self.manager, approved)

        for action in ("approve", "revoke", "paid", "note", "recheck"):
            self.assertTrue(self.has(page, approved, action), action)

    def test_the_self_review_and_self_payment_notices_are_separate(self):
        own_pending = f.srp_claim(self.both, time=self.loss_time)
        own_approved = f.srp_claim(self.both, status=Status.APPROVED, decided_by=self.manager, time=self.loss_time)

        pending_page = self.page(self.both, own_pending)
        approved_page = self.page(self.both, own_approved)

        self.assertContains(pending_page, "Another reviewer has to decide on it.")
        self.assertNotContains(pending_page, "Another payer has to mark it paid")
        self.assertContains(approved_page, "Another payer has to mark it paid or undo the payment.")
        self.assertFalse(self.has(approved_page, own_approved, "paid"))

    def test_the_approver_notice(self):
        SrpSettings.objects.filter(pk=1).update(require_different_payer=True)
        claim = f.srp_claim(self.member, status=Status.APPROVED, decided_by=self.both, time=self.loss_time)

        page = self.page(self.both, claim)

        self.assertContains(page, "You approved this claim. A different payer has to mark it paid.")
        self.assertFalse(self.has(page, claim, "paid"))
        self.assertTrue(self.has(page, claim, "revoke"))

    def test_the_undo_form_stays_for_the_approver(self):
        SrpSettings.objects.filter(pk=1).update(require_different_payer=True)
        paid = f.srp_claim(self.member, status=Status.PAID, decided_by=self.both, time=self.loss_time)

        self.assertTrue(self.has(self.page(self.both, paid), paid, "unpaid"))

    def test_the_back_link_follows_the_role(self):
        claim = f.srp_claim(self.member, time=self.loss_time)

        self.assertContains(self.page(self.reviewer, claim), f'href="{route("srp_review")}"')
        payer_page = self.page(self.payer, claim)
        self.assertContains(payer_page, f'href="{route("srp_payments")}"')
        self.assertContains(payer_page, "Back to payments")
        self.assertNotContains(payer_page, "Back to the review queue")


class MenuTests(RolePagesBase):
    def menu(self, user):
        return self.client_for(user).get(reverse("fleetops:dashboard")).content.decode()

    def test_a_reviewer_without_request_srp_sees_review_and_the_summary(self):
        html = self.menu(self.reviewer)

        self.assertIn(f'href="{route("srp_review")}"', html)
        self.assertNotIn(f'href="{route("srp_payments")}"', html)
        self.assertNotIn(f'href="{route("srp_home")}"', html)
        self.assertIn(f'href="{route("srp_summary")}"', html)

    def test_a_payer_without_request_srp_sees_payments_and_the_summary(self):
        html = self.menu(self.payer)

        self.assertIn(f'href="{route("srp_payments")}"', html)
        self.assertNotIn(f'href="{route("srp_review")}"', html)
        self.assertNotIn(f'href="{route("srp_home")}"', html)
        self.assertIn(f'href="{route("srp_summary")}"', html)

    def test_the_manager_sees_both(self):
        html = self.menu(self.manager)

        self.assertIn(f'href="{route("srp_review")}"', html)
        self.assertIn(f'href="{route("srp_payments")}"', html)

    def test_counts(self):
        SrpSettings.objects.filter(pk=1).update(require_different_payer=True)
        f.srp_claim(self.member, time=self.loss_time)
        f.srp_claim(self.other, time=self.loss_time)
        f.srp_claim(self.member, status=Status.APPROVED, decided_by=self.payer2, time=self.loss_time)
        f.srp_claim(self.other, status=Status.APPROVED, decided_by=self.both, time=self.loss_time)
        f.srp_claim(self.both, status=Status.APPROVED, decided_by=self.payer2, time=self.loss_time)

        self.assertEqual(access.srp_nav(self.reviewer)["pending_count"], 2)
        self.assertEqual(access.srp_nav(self.reviewer)["to_pay_count"], 0)
        self.assertEqual(access.srp_nav(self.payer)["to_pay_count"], 3)
        self.assertEqual(access.srp_nav(self.payer)["pending_count"], 0)
        # The approver rule and the own-claim rule take claims out of the count of the person.
        self.assertEqual(access.srp_nav(self.both)["to_pay_count"], 1)
        html = self.menu(self.payer)
        self.assertIn('Payments <span class="fo-pill fo-pill-warn">3</span>', html)
        self.assertNotIn("Review <span", html)

    def test_nav_keys_of_each_role(self):
        for user, expected in (
            (self.reviewer, dict(review=True, pay=False, summary=True, summary_all=True, request=False, visible=True)),
            (self.payer, dict(review=False, pay=True, summary=True, summary_all=True, request=False, visible=True)),
            (self.manager, dict(review=True, pay=True, summary=True, summary_all=True, request=True, visible=True)),
            (self.member, dict(review=False, pay=False, summary=False, summary_all=False, request=True, visible=True)),
            (self.no_srp, dict(review=False, pay=False, summary=False, summary_all=False, request=False, visible=False)),
        ):
            user.has_perm("fleetops.basic_access")
            nav = access.srp_nav(user)
            with self.subTest(user=user.username):
                self.assertEqual({key: nav[key] for key in expected}, expected)


class NavQueryTests(RolePagesBase):
    def test_a_payer_costs_one_count_more_than_a_member_with_the_menu(self):
        for user, queries in ((self.reviewer, 3), (self.payer, 3), (self.manager, 4)):
            user.has_perm("fleetops.basic_access")
            with self.subTest(user=user.username):
                with self.assertNumQueries(queries):
                    access.srp_nav(user)


class SettingsPageTests(TestCase):
    def setUp(self):
        self.admin = f.create_user("roles_admin", perms=f.MEMBER_PERMS + ["fleetops.manage_configuration"])
        self.url = reverse("fleetops:configuration_list", args=["srp"])
        self.client.force_login(self.admin)

    def post(self, **overrides):
        from .test_srp_config import settings_post

        return self.client.post(self.url, settings_post(**overrides))

    def test_the_page_shows_the_three_switches_with_their_labels(self):
        response = self.client.get(self.url)

        for label in (
            "SRP reviewers may decide on their own claims",
            "SRP payers may mark their own claims paid",
            "The approver of a claim may not pay it",
        ):
            self.assertContains(response, label)
        self.assertNotContains(response, "SRP managers may decide on their own claims")

    def test_the_fields_are_in_the_members_and_review_group(self):
        form = self.client.get(self.url).context["form"]
        groups = {str(group[0]): [field.name for field in group[2]] for group in form.field_groups()}

        self.assertEqual(
            groups["Members and review"],
            # LINK (INT-9): "Complete SRP links automatically" joined the group.
            [
                "notify_members",
                "allow_self_review",
                "allow_self_payment",
                "require_different_payer",
                "auto_complete_links",
                "member_instructions",
            ],
        )

    def test_new_installs_have_all_three_off(self):
        form = self.client.get(self.url).context["form"]

        for name in ("allow_self_review", "allow_self_payment", "require_different_payer"):
            self.assertFalse(form[name].value(), name)

    def test_the_switches_are_saved_and_audited(self):
        response = self.post(allow_self_payment="on", require_different_payer="on")

        self.assertEqual(response.status_code, 302)
        row = SrpSettings.get_solo()
        self.assertEqual((row.allow_self_review, row.allow_self_payment, row.require_different_payer), (False, True, True))
        entry = AuditLog.objects.get(action="configuration.update", object_type="SrpSettings")
        self.assertEqual(entry.actor, self.admin)
        self.assertEqual((entry.old_value["allow_self_payment"], entry.new_value["allow_self_payment"]), (False, True))
        self.assertEqual(
            (entry.old_value["require_different_payer"], entry.new_value["require_different_payer"]), (False, True)
        )
        self.assertEqual((entry.old_value["allow_self_review"], entry.new_value["allow_self_review"]), (False, False))

    def test_an_unticked_switch_is_saved_off(self):
        SrpSettings.objects.filter(pk=1).update(allow_self_payment=True, require_different_payer=True)

        self.post()

        row = SrpSettings.get_solo()
        self.assertEqual((row.allow_self_payment, row.require_different_payer), (False, False))
        entry = AuditLog.objects.filter(action="configuration.update", object_type="SrpSettings").latest("pk")
        self.assertEqual((entry.old_value["allow_self_payment"], entry.new_value["allow_self_payment"]), (True, False))

    def test_the_settings_page_needs_manage_configuration_and_no_srp_role(self):
        both = f.create_user("srp_only", perms=f.SRP_MANAGER_PERMS + PERMISSIONS["both"])
        self.client.force_login(both)

        self.assertEqual(self.client.get(self.url).status_code, 403)


class RecalculateNoticeTests(TestCase):
    def test_the_button_on_the_configuration_pages_is_for_reviewers(self):
        from fleetops import views_config

        reviewer = f.create_user("noticer", perms=f.MEMBER_PERMS + PERMISSIONS["review"])
        payer = f.create_user("noticep", perms=f.MEMBER_PERMS + PERMISSIONS["pay"])

        class Request:
            def __init__(self, user):
                self.user = user

            def get_full_path(self):
                return "/x/"

        self.assertTrue(views_config._outdated_claims_context(Request(reviewer))["can_recalculate_claims"])
        self.assertFalse(views_config._outdated_claims_context(Request(payer))["can_recalculate_claims"])


class LinkToggleTests(RolePagesBase):
    def test_every_srp_role_closes_a_link_and_reviewers_open_it(self):
        # LINK (INT-9, decision 22): closing is open to every SRP role; opening a link again lets members submit
        # through it, so a payer may not. Before this phase every role could open it as well.
        for user in (self.reviewer, self.payer, self.manager):
            with self.subTest(user=user.username):
                response = self.client_for(user).post(route("srp_link_close", self.link.code))
                self.assertEqual(response.status_code, 302)
                opened = self.client_for(user).post(route("srp_link_open", self.link.code))
                self.assertEqual(opened.status_code, 403 if user == self.payer else 302)
                self.link.refresh_from_db()
                self.assertEqual(self.link.is_open, user != self.payer)
                links.set_link_open(self.link, actor=self.manager, is_open=True)
        self.assertEqual(self.client_for(self.member).post(route("srp_link_close", self.link.code)).status_code, 403)

    def test_the_fleet_card_offers_the_toggle_to_every_srp_role(self):

        for user in (self.reviewer, self.payer, self.manager):
            with self.subTest(user=user.username):
                card = links.operation_srp_context(self.fleet, user)["srp_card"]
                self.assertTrue(card["can_toggle"])
        card = links.operation_srp_context(self.fleet, self.member).get("srp_card", {})
        self.assertFalse(card.get("can_toggle", False))
