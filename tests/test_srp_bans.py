# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""SRP bans (REV-8): when a ban is in force, making and lifting one, rejecting the pending claims with it, the
automatic ban for a member who keeps submitting over the monthly limit, where the ban stops a claim and the pages.

ESI and zKillboard are never reached; killmails come from the factories.
"""

from datetime import datetime, timedelta, timezone as dt_timezone
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import connection
from django.db.models.query import QuerySet
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from fleetops.models import AuditLog, SrpBan, SrpClaim, SrpClaimEvent, SrpSettings
from fleetops.srp import bans, claims, notify, policy, review
from fleetops.srp.types import Calculation, LimitState, SrpError

from . import factories as f
from .test_srp_claims import ClaimBase
from .test_srp_views import SrpPagesTestCase, messages_of

Status = SrpClaim.Status
NOW = datetime(2026, 11, 4, 12, 0, tzinfo=dt_timezone.utc)


def make_ban(user, **values):
    values.setdefault("reason", "Abuse of SRP")
    values.setdefault("starts_at", NOW - timedelta(days=1))
    values.setdefault("expires_at", NOW + timedelta(days=6))
    return SrpBan.objects.create(user=user, main_character_name=f.main_of(user).character_name, **values)


class BanBase(ClaimBase):
    """ClaimBase has a member, another member and a manager (manage_srp); a reviewer and a payer are added."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.reviewer = f.create_user("ban_reviewer", perms=f.SRP_REVIEWER_PERMS)
        cls.payer = f.create_user("ban_payer", perms=f.SRP_PAYER_PERMS)

    def setUp(self):
        super().setUp()
        self.settings(enabled=True, percentage=Decimal("80"))


# --- When a ban is in force ----------------------------------------------------------------------------------------


class ActiveBanTests(BanBase):
    def test_a_member_without_a_ban_has_none_and_it_costs_one_query(self):
        with CaptureQueriesContext(connection) as queries:
            self.assertIsNone(bans.active_ban(self.user, NOW))
        self.assertEqual(len(queries), 1)

    def test_a_ban_is_in_force_from_its_start_until_just_before_its_end(self):
        ban = make_ban(self.user, starts_at=NOW, expires_at=NOW + timedelta(days=7))
        for moment, expected in (
            (NOW - timedelta(seconds=1), False),
            (NOW, True),
            (NOW + timedelta(days=7) - timedelta(seconds=1), True),
            (NOW + timedelta(days=7), False),
            (NOW + timedelta(days=8), False),
        ):
            with self.subTest(moment=moment):
                self.assertEqual(bans.active_ban(self.user, moment) == ban, expected)
                self.assertEqual(ban.in_force(moment), expected)

    def test_a_permanent_ban_has_no_end(self):
        ban = make_ban(self.user, expires_at=None)
        self.assertTrue(ban.is_permanent)
        self.assertEqual(bans.active_ban(self.user, NOW + timedelta(days=366)), ban)

    def test_a_lifted_ban_is_not_in_force(self):
        make_ban(self.user, lifted_at=NOW, lifted_by=self.manager, lift_reason="Sorry")
        self.assertIsNone(bans.active_ban(self.user, NOW))

    def test_a_ban_belongs_to_the_account_not_to_a_character(self):
        make_ban(self.user)
        f.add_alt(self.user)
        self.assertIsNotNone(bans.active_ban(self.user, NOW))
        self.assertIsNone(bans.active_ban(self.other, NOW))

    def test_nobody_and_anonymous_have_no_ban(self):
        from django.contrib.auth.models import AnonymousUser

        self.assertIsNone(bans.active_ban(None))
        self.assertIsNone(bans.active_ban(AnonymousUser()))


# --- Making and lifting a ban --------------------------------------------------------------------------------------


class BanUserTests(BanBase):
    def ban(self, **values):
        values.setdefault("actor", self.reviewer)
        values.setdefault("days", 7)
        values.setdefault("reason", "Fake losses")
        values.setdefault("now", NOW)
        return bans.ban_user(self.user, **values)

    def test_a_reviewer_bans_a_member_for_seven_days(self):
        ban, result = self.ban(internal_note="Seen by two FCs")

        self.assertEqual((ban.user, ban.created_by, ban.kind), (self.user, self.reviewer, "manual"))
        self.assertEqual((ban.starts_at, ban.expires_at), (NOW, NOW + timedelta(days=7)))
        self.assertEqual((ban.reason, ban.internal_note), ("Fake losses", "Seen by two FCs"))
        self.assertEqual(ban.main_character_name, f.main_of(self.user).character_name)
        self.assertEqual((result.done, result.skipped), ([], []))

    def test_the_manager_may_ban_too(self):
        ban, _result = self.ban(actor=self.manager)
        self.assertEqual(ban.created_by, self.manager)

    def test_a_payer_and_a_member_may_not(self):
        for actor in (self.payer, self.other, None):
            with self.subTest(actor=actor):
                with self.assertRaises(SrpError) as caught:
                    self.ban(actor=actor)
                self.assertEqual(caught.exception.code, "forbidden")
        self.assertFalse(SrpBan.objects.exists())

    def test_nobody_bans_themselves(self):
        with self.assertRaises(SrpError) as caught:
            bans.ban_user(self.reviewer, actor=self.reviewer, days=3, reason="Oops", now=NOW)
        self.assertEqual(caught.exception.code, "self")

    def test_a_member_with_a_ban_in_force_cannot_be_banned_again(self):
        self.ban()
        with self.assertRaises(SrpError) as caught:
            self.ban()
        self.assertEqual(caught.exception.code, "exists")
        self.assertEqual(SrpBan.objects.count(), 1)

    def test_a_ban_that_ran_out_does_not_stop_a_new_one(self):
        make_ban(self.user, starts_at=NOW - timedelta(days=30), expires_at=NOW - timedelta(days=1))
        self.assertEqual(self.ban()[0].expires_at, NOW + timedelta(days=7))

    def test_the_length_is_between_one_day_and_the_longest_ban(self):
        SrpSettings.objects.filter(pk=1).update(ban_max_days=30)
        for days in (0, -1, 31, None, True, "7", 1.5):
            with self.subTest(days=days):
                with self.assertRaises(SrpError) as caught:
                    self.ban(days=days)
                self.assertEqual(caught.exception.code, "days")
        self.assertEqual(self.ban(days=30)[0].expires_at, NOW + timedelta(days=30))

    def test_a_permanent_ban_has_no_end_and_is_not_limited_by_the_longest_ban(self):
        SrpSettings.objects.filter(pk=1).update(ban_max_days=30)
        ban, _result = self.ban(days=None, permanent=True)
        self.assertIsNone(ban.expires_at)
        self.assertEqual(bans.active_ban(self.user, NOW + timedelta(days=400)), ban)

    def test_a_permanent_ban_with_a_length_is_refused(self):
        with self.assertRaises(SrpError) as caught:
            self.ban(days=7, permanent=True)
        self.assertEqual(caught.exception.code, "days")

    def test_the_reason_is_required_and_has_a_limit(self):
        for reason in ("", "   ", None, "x" * 1001):
            with self.subTest(reason=reason):
                with self.assertRaises(SrpError):
                    self.ban(reason=reason)
        with self.assertRaises(SrpError):
            self.ban(internal_note="x" * 1001)
        self.assertFalse(SrpBan.objects.exists())

    def test_the_audit_entry_names_the_ban_and_never_the_internal_note(self):
        ban, _result = self.ban(internal_note="TOP-SECRET-NOTE")

        entry = AuditLog.objects.get(action="srp.ban_create")
        self.assertEqual((entry.actor, entry.object_type, entry.object_id), (self.reviewer, "SrpBan", str(ban.pk)))
        self.assertEqual(entry.reason, "Fake losses")
        self.assertEqual(
            entry.new_value,
            {
                "user_id": self.user.pk,
                "expires_at": (NOW + timedelta(days=7)).isoformat(),
                "days": 7,
                "permanent": False,
                "kind": "manual",
                "rejected_count": 0,
            },
        )
        for value in (entry.new_value, entry.old_value, entry.reason):
            self.assertNotIn("TOP-SECRET-NOTE", str(value))

    def test_a_permanent_ban_is_audited_with_no_end_date(self):
        self.ban(days=None, permanent=True)
        entry = AuditLog.objects.get(action="srp.ban_create")
        self.assertEqual((entry.new_value["expires_at"], entry.new_value["permanent"], entry.new_value["days"]), (None, True, None))

    def test_the_member_is_notified_when_the_ban_is_committed(self):
        with mock.patch.object(notify, "notify_banned") as told, self.captureOnCommitCallbacks(execute=True):
            ban, _result = self.ban()
        told.assert_called_once_with(ban)

    def test_the_notification_names_the_reason_and_not_the_note(self):
        ban, _result = self.ban(internal_note="TOP-SECRET-NOTE")
        with mock.patch.object(notify.notify, "danger") as sent:
            notify.notify_banned(ban)
        user, title, message = sent.call_args.args
        self.assertEqual((user, title), (self.user, "SRP submissions blocked"))
        self.assertIn("Reason: Fake losses", message)
        self.assertIn("Until:", message)
        self.assertNotIn("TOP-SECRET-NOTE", message)

    def test_no_notification_when_members_are_not_notified(self):
        self.settings(notify_members=False)
        ban, _result = self.ban()
        with mock.patch.object(notify.notify, "danger") as sent:
            notify.notify_banned(ban)
        sent.assert_not_called()

    def test_lifting_a_ban_ends_it_at_once_and_is_audited(self):
        ban, _result = self.ban()

        bans.lift_ban(ban, actor=self.manager, reason="Cleared up", now=NOW + timedelta(hours=1))

        ban.refresh_from_db()
        self.assertEqual((ban.lifted_by, ban.lift_reason), (self.manager, "Cleared up"))
        self.assertIsNone(bans.active_ban(self.user, NOW + timedelta(hours=1)))
        entry = AuditLog.objects.get(action="srp.ban_lift")
        self.assertEqual((entry.actor, entry.reason, entry.object_id), (self.manager, "Cleared up", str(ban.pk)))

    def test_lifting_needs_a_reviewer_a_reason_and_a_ban_in_force(self):
        ban, _result = self.ban()
        with self.assertRaises(SrpError):
            bans.lift_ban(ban, actor=self.payer, reason="Nice")
        with self.assertRaises(SrpError):
            bans.lift_ban(ban, actor=self.reviewer, reason="  ")
        bans.lift_ban(ban, actor=self.reviewer, reason="Done", now=NOW)
        with self.assertRaises(SrpError) as caught:
            bans.lift_ban(ban, actor=self.reviewer, reason="Again", now=NOW)
        self.assertEqual(caught.exception.code, "not_in_force")

    def test_an_expired_ban_cannot_be_lifted(self):
        ban = make_ban(self.user, expires_at=NOW - timedelta(hours=1))
        with self.assertRaises(SrpError):
            bans.lift_ban(ban, actor=self.reviewer, reason="Late", now=NOW)


class RejectPendingTests(BanBase):
    def claim(self, status=Status.PENDING, user=None, amount=10_000_000):
        return f.srp_claim(user or self.user, status=status, amount=amount, decided_by=self.manager)

    def ban(self, **values):
        values.setdefault("actor", self.reviewer)
        values.setdefault("days", 7)
        values.setdefault("reason", "Fake losses")
        return bans.ban_user(self.user, **values)

    def test_the_pending_claims_are_rejected_with_the_given_reason_and_the_approved_one_is_not(self):
        pending = [self.claim() for _ in range(3)]
        approved = self.claim(Status.APPROVED)
        other = self.claim(user=self.other)

        ban, result = self.ban(reject_pending=True, reject_reason="Not a real loss")

        self.assertEqual(len(result.done), 3)
        self.assertEqual(result.skipped, [])
        for claim in pending:
            claim.refresh_from_db()
            self.assertEqual((claim.status, claim.decision_reason, claim.decided_by), (Status.REJECTED, "Not a real loss", self.reviewer))
            event = claim.events.get(action=SrpClaimEvent.Action.REJECT)
            self.assertEqual((event.actor, event.reason), (self.reviewer, "Not a real loss"))
        approved.refresh_from_db()
        other.refresh_from_db()
        self.assertEqual((approved.status, other.status), (Status.APPROVED, Status.PENDING))
        self.assertEqual(AuditLog.objects.filter(action="srp.claim_reject").count(), 3)
        self.assertEqual(AuditLog.objects.get(action="srp.ban_create").new_value["rejected_count"], 3)

    def test_without_the_option_the_pending_claims_stay_pending(self):
        pending = self.claim()

        self.ban()

        pending.refresh_from_db()
        self.assertEqual(pending.status, Status.PENDING)

    def test_the_rejection_reason_is_required_and_no_ban_is_made_without_it(self):
        self.claim()
        for reason in ("", "  "):
            with self.assertRaises(SrpError) as caught:
                self.ban(reject_pending=True, reject_reason=reason)
            self.assertEqual(caught.exception.code, "reject_reason_required")
        self.assertFalse(SrpBan.objects.exists())

    def test_a_skipped_claim_is_reported_with_its_reason(self):
        first = self.claim()
        with mock.patch.object(review, "reject_claim", side_effect=SrpError("wrong_state", "This claim is no longer pending.")):
            ban, result = self.ban(reject_pending=True, reject_reason="Not real")
        self.assertEqual(result.done, [])
        self.assertEqual([(claim.pk, text) for claim, text in result.skipped], [(first.pk, "This claim is no longer pending.")])
        self.assertTrue(SrpBan.objects.filter(pk=ban.pk).exists())

    def test_every_claim_is_rejected_under_the_usual_locks(self):
        self.claim()
        order = []
        real_lock = claims.lock_claim

        def lock(claim):
            order.append("claim")
            return real_lock(claim)

        with mock.patch.object(claims, "lock_claim", side_effect=lock):
            self.ban(reject_pending=True, reject_reason="Not real")
        self.assertEqual(order, ["claim"])


# --- Where the ban stops a claim -----------------------------------------------------------------------------------


class BannedClaimTests(BanBase):
    def test_the_preview_is_stopped_with_the_reason_and_the_end(self):
        make_ban(self.user, reason="Fake losses", starts_at=timezone.now() - timedelta(days=1), expires_at=timezone.now() + timedelta(days=3))
        preview = self.prepare(self.killmail())
        self.assertEqual(len(preview.blockers), 1)
        self.assertTrue(preview.blockers[0].startswith("You are banned from submitting SRP claims until "), preview.blockers)
        self.assertIn("Fake losses", preview.blockers[0])
        self.assertFalse(preview.eligible)

    def test_a_permanent_ban_says_so(self):
        make_ban(self.user, expires_at=None, reason="Enough")
        self.assertEqual(self.prepare(self.killmail()).blockers, ["You are banned from submitting SRP claims: Enough"])

    def test_a_token_from_before_the_ban_cannot_be_submitted(self):
        km = self.killmail()
        token = self.prepare(km).token
        make_ban(self.user, starts_at=timezone.now() - timedelta(minutes=1), expires_at=timezone.now() + timedelta(days=1))

        with self.assertRaises(SrpError) as caught:
            claims.submit_claim(self.user, token)

        self.assertEqual(caught.exception.code, "not_eligible")
        self.assertIn("banned", str(caught.exception))
        self.assertFalse(SrpClaim.objects.exists())

    def test_the_ban_ends_by_itself_after_its_last_day(self):
        make_ban(self.user, starts_at=NOW - timedelta(days=7), expires_at=NOW)
        km = self.killmail()
        self.assertTrue(policy.eligibility(km=km, user=self.user, scope="out_of_fleet", link=None, settings_obj=self.srp, now=NOW - timedelta(seconds=1)))
        self.assertEqual(
            policy.eligibility(km=km, user=self.user, scope="out_of_fleet", link=None, settings_obj=self.srp, now=NOW + timedelta(days=1)),
            [],
        )

    def test_lifting_makes_a_claim_possible_at_once(self):
        ban = make_ban(self.user, starts_at=timezone.now() - timedelta(days=1), expires_at=timezone.now() + timedelta(days=3))
        km = self.killmail()
        self.assertTrue(self.prepare(km).blockers)
        bans.lift_ban(ban, actor=self.reviewer, reason="Mistake")
        self.assertEqual(self.prepare(km).blockers, [])

    def test_a_pending_claim_can_still_be_withdrawn_but_not_submitted_again(self):
        claim = self.submit(self.killmail())
        make_ban(self.user, starts_at=timezone.now() - timedelta(days=1), expires_at=timezone.now() + timedelta(days=3))

        claims.withdraw_claim(claim, actor=self.user)
        with self.assertRaises(SrpError) as caught:
            claims.resubmit_claim(claim, actor=self.user)

        self.assertEqual(caught.exception.code, "not_eligible")
        claim.refresh_from_db()
        self.assertEqual(claim.status, Status.WITHDRAWN)

    def test_a_ban_does_not_touch_other_members(self):
        make_ban(self.user, starts_at=timezone.now() - timedelta(days=1), expires_at=timezone.now() + timedelta(days=3))
        km = self.killmail(f.main_of(self.other))
        self.assertEqual(self.prepare(km, user=self.other).blockers, [])

    def test_the_ban_comes_first_in_the_list_of_reasons(self):
        self.settings(enabled=False)
        make_ban(self.user, starts_at=timezone.now() - timedelta(days=1), expires_at=timezone.now() + timedelta(days=3))
        blockers = self.prepare(self.killmail()).blockers
        self.assertTrue(blockers[0].startswith("You are banned"))
        self.assertIn("FleetOps SRP is not enabled.", blockers)

    def test_the_check_costs_one_query_for_the_ban(self):
        km = self.killmail()
        with CaptureQueriesContext(connection) as queries:
            bans.active_ban(self.user)
        self.assertEqual(len(queries), 1)
        with CaptureQueriesContext(connection) as plain:
            policy.eligibility(km=km, user=self.user, scope="out_of_fleet", link=None, settings_obj=self.srp)
        self.assertLessEqual(len(plain), 6)


# --- The automatic ban over the monthly limit ----------------------------------------------------------------------


class AutoBanTests(BanBase):
    """A member whose personal limit is used up and who still submits is banned to the end of the month."""

    def setUp(self):
        super().setUp()
        self.srp = self.settings(
            auto_ban_over_monthly_cap=True, personal_monthly_cap=1_000_000_000, percentage=Decimal("100")
        )

    def approved(self, amount, **extra):
        return f.srp_claim(
            self.user,
            status=Status.APPROVED,
            amount=amount,
            decided_by=self.manager,
            counts_personal=True,
            **extra,
        )

    def new(self, fitted="250000000"):
        return self.submit(self.killmail(fitted=fitted))

    def test_the_switch_is_off_by_default(self):
        self.assertFalse(SrpSettings._meta.get_field("auto_ban_over_monthly_cap").default)

    def test_with_the_switch_off_nothing_happens(self):
        self.settings(auto_ban_over_monthly_cap=False)
        self.approved(1_000_000_000)

        claim = self.new()

        self.assertEqual(claim.status, Status.PENDING)
        self.assertFalse(SrpBan.objects.exists())

    def test_a_used_up_limit_bans_to_the_end_of_the_month_and_the_claim_is_still_made(self):
        self.approved(1_000_000_000)

        claim = self.new()

        self.assertEqual((claim.status, claim.over_limit, claim.limited_amount), (Status.PENDING, True, 0))
        ban = SrpBan.objects.get()
        now = timezone.now().astimezone(dt_timezone.utc)
        first_of_next = datetime(now.year + (now.month == 12), now.month % 12 + 1, 1, tzinfo=dt_timezone.utc)
        self.assertEqual((ban.user, ban.kind, ban.created_by, ban.expires_at), (self.user, "auto_monthly_cap", None, first_of_next))
        self.assertEqual(ban.reason, "The monthly personal limit is used up and another claim was submitted. Submitting is not possible until the end of the month.")
        entry = AuditLog.objects.get(action="srp.ban_create")
        self.assertIsNone(entry.actor)
        self.assertEqual((entry.new_value["kind"], entry.new_value["trigger_claim"]), ("auto_monthly_cap", claim.reference))
        event = claim.events.get(action=SrpClaimEvent.Action.SUBMIT)
        self.assertTrue(event.data["auto_banned"])

    def test_the_member_is_stopped_afterwards(self):
        self.approved(1_000_000_000)
        self.new()
        self.assertTrue(self.prepare(self.killmail()).blockers)

    def test_a_claim_that_only_runs_over_the_limit_does_not_ban(self):
        self.approved(900_000_000)

        claim = self.new()  # 250m with 100m left: over the limit, but the limit was not used up before

        self.assertTrue(claim.over_limit)
        self.assertFalse(SrpBan.objects.exists())
        self.assertNotIn("auto_banned", claim.events.get(action=SrpClaimEvent.Action.SUBMIT).data)

    def test_a_used_up_corporation_limit_does_not_ban(self):
        self.settings(personal_monthly_cap=None, corporation_monthly_cap=1_000_000_000)
        f.srp_claim(
            self.other, status=Status.APPROVED, amount=1_000_000_000, decided_by=self.manager, counts_corporation=True
        )

        claim = self.new()

        self.assertEqual(claim.limited_amount, 0)
        self.assertFalse(SrpBan.objects.exists())

    def test_a_used_up_category_limit_of_the_member_bans(self):
        category = f.srp_category("Roaming", personal_monthly_cap=500_000_000)
        self.settings(personal_monthly_cap=None, default_category=category)
        f.srp_claim(self.user, status=Status.APPROVED, amount=500_000_000, decided_by=self.manager, category=category)

        self.new()

        self.assertEqual(SrpBan.objects.get().kind, "auto_monthly_cap")

    def test_in_fleet_claims_that_do_not_count_toward_the_limit_do_not_ban(self):
        self.settings(personal_cap_includes_in_fleet=False)
        self.approved(1_000_000_000)
        operation = f.create_operation(self.fc)
        link = f.fleet_link(operation)

        claim = self.submit(self.killmail(), link=link)

        self.assertEqual(claim.scope, "in_fleet")
        self.assertFalse(SrpBan.objects.exists())

    def test_the_preview_does_not_ban(self):
        self.approved(1_000_000_000)
        self.prepare(self.killmail())
        self.assertFalse(SrpBan.objects.exists())

    def test_a_member_who_is_banned_already_cannot_submit_and_is_not_banned_twice(self):
        self.approved(1_000_000_000)
        km = self.killmail()
        token = self.prepare(km).token
        make_ban(self.user, starts_at=timezone.now() - timedelta(days=1), expires_at=timezone.now() + timedelta(days=3))

        with self.assertRaises(SrpError):
            claims.submit_claim(self.user, token)

        self.assertEqual(SrpBan.objects.count(), 1)
        self.assertEqual(SrpBan.objects.get().kind, "manual")

    def test_the_helper_does_not_ban_a_member_who_is_banned_already(self):
        self.approved(1_000_000_000)
        make_ban(self.user, starts_at=timezone.now() - timedelta(days=1), expires_at=timezone.now() + timedelta(days=3))
        claim = f.srp_claim(self.user)
        calculation = Calculation(limits=(LimitState("personal", cap=1, remaining=0),))

        self.assertIsNone(bans.auto_ban_for_cap(self.user, claim, calculation, self.srp, now=timezone.now()))
        self.assertEqual(SrpBan.objects.count(), 1)

    def test_a_manual_ban_written_while_the_helper_waits_for_the_member_is_seen(self):
        # L-2: the helper takes the member's row lock (as ban_user does) before the last check. The lock is
        # simulated: when it is taken, the manual ban of the other request has just been committed.
        self.approved(1_000_000_000)
        claim = f.srp_claim(self.user)
        calculation = Calculation(limits=(LimitState("personal", cap=1, remaining=0),))
        real = QuerySet.select_for_update
        locked = []

        def lock_then_ban(queryset, *args, **kwargs):
            if queryset.model is get_user_model() and not locked:
                locked.append(True)
                make_ban(self.user, starts_at=timezone.now() - timedelta(minutes=1), expires_at=timezone.now() + timedelta(days=3))
            return real(queryset, *args, **kwargs)

        with mock.patch.object(QuerySet, "select_for_update", lock_then_ban):
            result = bans.auto_ban_for_cap(self.user, claim, calculation, self.srp, now=timezone.now())

        self.assertTrue(locked)
        self.assertIsNone(result)
        self.assertEqual(SrpBan.objects.filter(user=self.user).count(), 1)

    def test_the_member_is_locked_before_the_claim_is_written(self):
        # A claim insert takes a shared lock on the member's row; the exclusive lock must come first (InnoDB deadlock).
        self.approved(1_000_000_000)
        km = self.killmail()
        token = self.prepare(km).token
        order = []
        real_lock = QuerySet.select_for_update
        real_save = SrpClaim.save

        def lock(queryset, *args, **kwargs):
            if queryset.model is get_user_model():
                order.append("member")
            return real_lock(queryset, *args, **kwargs)

        def save(claim, *args, **kwargs):
            order.append("claim")
            return real_save(claim, *args, **kwargs)

        with (
            mock.patch.object(QuerySet, "select_for_update", lock),
            mock.patch.object(SrpClaim, "save", save),
        ):
            claims.submit_claim(self.user, token)

        self.assertEqual(order[0], "member")
        self.assertLess(order.index("member"), order.index("claim"))

    def test_with_the_switch_off_the_member_is_not_locked(self):
        self.settings(auto_ban_over_monthly_cap=False)
        locked = []
        real_lock = QuerySet.select_for_update

        def lock(queryset, *args, **kwargs):
            if queryset.model is get_user_model():
                locked.append(True)
            return real_lock(queryset, *args, **kwargs)

        with mock.patch.object(QuerySet, "select_for_update", lock):
            self.new()

        self.assertFalse(locked)

    def test_submitting_a_withdrawn_claim_again_bans_too(self):
        claim = self.new()
        claims.withdraw_claim(claim, actor=self.user)
        self.assertFalse(SrpBan.objects.exists())
        self.approved(1_000_000_000)

        claims.resubmit_claim(claim, actor=self.user)

        ban = SrpBan.objects.get()
        self.assertEqual(ban.kind, "auto_monthly_cap")
        event = claim.events.get(action=SrpClaimEvent.Action.RESUBMIT)
        self.assertTrue(event.data["auto_banned"])

    def test_the_automatic_ban_can_be_lifted_like_any_other(self):
        self.approved(1_000_000_000)
        self.new()
        bans.lift_ban(SrpBan.objects.get(), actor=self.reviewer, reason="One-off")
        self.assertIsNone(bans.active_ban(self.user))

    def test_the_end_of_the_ban_is_the_end_of_the_current_month_not_of_the_loss_month(self):
        self.assertEqual(bans.next_month_start(datetime(2026, 11, 30, 23, 59, tzinfo=dt_timezone.utc)), datetime(2026, 12, 1, tzinfo=dt_timezone.utc))
        self.assertEqual(bans.next_month_start(datetime(2026, 12, 31, 23, 59, tzinfo=dt_timezone.utc)), datetime(2027, 1, 1, tzinfo=dt_timezone.utc))
        self.assertEqual(bans.next_month_start(datetime(2026, 2, 1, tzinfo=dt_timezone.utc)), datetime(2026, 3, 1, tzinfo=dt_timezone.utc))

    def test_member_edit_and_review_actions_do_not_ban(self):
        claim = self.new()
        self.assertFalse(SrpBan.objects.exists())
        self.approved(1_000_000_000)
        review.recalculate_claim(claim, actor=self.manager)
        review.reject_claim(claim, actor=self.manager, reason="No")
        self.assertFalse(SrpBan.objects.exists())

    def test_the_automatic_ban_is_notified_after_the_commit(self):
        self.approved(1_000_000_000)
        with mock.patch.object(notify, "notify_banned") as told, self.captureOnCommitCallbacks(execute=True):
            self.new()
        told.assert_called_once()


# --- The pages -----------------------------------------------------------------------------------------------------


class BanPageTests(SrpPagesTestCase):
    def setUp(self):
        super().setUp()
        cache.clear()
        self.reviewer = f.create_user("banpage_reviewer", perms=f.SRP_REVIEWER_PERMS)
        self.payer = f.create_user("banpage_payer", perms=f.SRP_PAYER_PERMS)
        self.url = reverse("fleetops:srp_ban_new")

    def member_claim(self, **extra):
        return f.srp_claim(self.member, **extra)

    def test_a_reviewer_bans_a_member_from_the_form(self):
        claim = self.member_claim()
        client = self.client_for(self.reviewer)

        form = client.get(self.url, {"user": self.member.pk, "claim": str(claim.uuid)})
        self.assertContains(form, "New SRP ban")
        self.assertContains(form, f.main_of(self.member).character_name)
        self.assertContains(form, "Reject the 1 pending claim of the member")

        response = client.post(
            self.url,
            {"user": self.member.pk, "days": "7", "reason": "Fake losses", "internal_note": "Seen twice",
             "reject_pending": "on", "reject_reason": "Not a real loss"},
        )

        self.assertRedirects(response, reverse("fleetops:srp_bans"), fetch_redirect_response=False)
        ban = SrpBan.objects.get()
        self.assertEqual((ban.user, ban.created_by, ban.internal_note), (self.member, self.reviewer, "Seen twice"))
        claim.refresh_from_db()
        self.assertEqual((claim.status, claim.decision_reason), (Status.REJECTED, "Not a real loss"))

    def test_the_member_can_be_found_by_the_name_of_the_main_character(self):
        name = f.main_of(self.member).character_name
        response = self.client_for(self.reviewer).post(
            self.url, {"member": name.upper(), "permanent": "on", "reason": "Enough"}
        )
        self.assertEqual(response.status_code, 302)
        self.assertIsNone(SrpBan.objects.get().expires_at)

    def test_an_unknown_name_is_an_error_and_no_ban_is_made(self):
        response = self.client_for(self.reviewer).post(self.url, {"member": "Nobody Atall", "days": "3", "reason": "x"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("member", response.context["form"].errors)
        self.assertFalse(SrpBan.objects.exists())

    def test_the_rejection_needs_its_reason_and_then_no_ban_is_made(self):
        self.member_claim()
        response = self.client_for(self.reviewer).post(
            self.url, {"user": self.member.pk, "days": "7", "reason": "x", "reject_pending": "on", "reject_reason": ""}
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("reject_reason", response.context["form"].errors)
        self.assertFalse(SrpBan.objects.exists())

    def test_a_length_or_permanent_is_needed(self):
        response = self.client_for(self.reviewer).post(self.url, {"user": self.member.pk, "reason": "x"})
        self.assertIn("days", response.context["form"].errors)

    def test_too_long_a_ban_is_refused_with_a_message(self):
        response = self.client_for(self.reviewer).post(self.url, {"user": self.member.pk, "days": "4000", "reason": "x"})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(SrpBan.objects.exists())
        response = self.client_for(self.reviewer).post(self.url, {"user": self.member.pk, "days": "366", "reason": "x"})
        self.assertTrue(any("between 1 and 365" in text for text in messages_of(response)))

    def test_payers_members_and_visitors_are_refused(self):
        for user in (self.payer, self.member, self.no_srp):
            client = self.client_for(user)
            with self.subTest(user=user.username):
                for response in (
                    client.get(reverse("fleetops:srp_bans")),
                    client.get(self.url),
                    client.post(self.url, {"user": self.other.pk, "days": "3", "reason": "x"}),
                    client.post(reverse("fleetops:srp_ban_lift", args=[1]), {"reason": "x"}),
                ):
                    self.assertEqual(response.status_code, 403)
        self.assertFalse(SrpBan.objects.exists())

    def test_the_list_shows_the_bans_in_force_and_filters(self):
        ban = make_ban(self.member, starts_at=timezone.now() - timedelta(days=1), expires_at=timezone.now() + timedelta(days=3), internal_note="Staff only")
        make_ban(self.other, starts_at=timezone.now() - timedelta(days=9), expires_at=timezone.now() - timedelta(days=2), reason="Old one")
        client = self.client_for(self.reviewer)

        shown = client.get(reverse("fleetops:srp_bans"))
        self.assertContains(shown, ban.reason)
        self.assertContains(shown, "Staff only")
        self.assertNotContains(shown, "Old one")
        self.assertContains(client.get(reverse("fleetops:srp_bans"), {"state": "expired"}), "Old one")
        self.assertContains(client.get(reverse("fleetops:srp_bans"), {"state": "all"}), "Old one")
        self.assertNotContains(client.get(reverse("fleetops:srp_bans"), {"kind": "auto_monthly_cap"}), ban.reason)

    def test_lifting_from_the_list(self):
        ban = make_ban(self.member, starts_at=timezone.now() - timedelta(days=1), expires_at=timezone.now() + timedelta(days=3))
        client = self.client_for(self.reviewer)

        response = client.post(reverse("fleetops:srp_ban_lift", args=[ban.pk]), {"reason": "Cleared"})

        self.assertRedirects(response, reverse("fleetops:srp_bans"), fetch_redirect_response=False)
        ban.refresh_from_db()
        self.assertEqual(ban.lift_reason, "Cleared")

    def test_lifting_needs_a_reason(self):
        ban = make_ban(self.member, starts_at=timezone.now() - timedelta(days=1), expires_at=timezone.now() + timedelta(days=3))
        response = self.client_for(self.reviewer).post(reverse("fleetops:srp_ban_lift", args=[ban.pk]), {"reason": ""})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(messages_of(response), ["Give a reason for lifting the ban."])
        ban.refresh_from_db()
        self.assertIsNone(ban.lifted_at)

    def test_lifting_needs_a_post(self):
        ban = make_ban(self.member)
        self.assertEqual(self.client_for(self.reviewer).get(reverse("fleetops:srp_ban_lift", args=[ban.pk])).status_code, 405)

    def test_the_member_sees_the_banner_with_the_reason_and_no_internal_note(self):
        make_ban(self.member, reason="Fake losses", internal_note="Staff only", starts_at=timezone.now() - timedelta(days=1), expires_at=timezone.now() + timedelta(days=3))
        self.patch_home_services()

        response = self.client.get(reverse("fleetops:srp_home"))

        self.assertContains(response, 'id="srp-ban-banner"')
        self.assertContains(response, "You cannot submit SRP claims until")
        self.assertContains(response, "Fake losses")
        self.assertNotContains(response, "Staff only")

    def test_a_permanent_ban_says_permanently(self):
        make_ban(self.member, expires_at=None, starts_at=timezone.now() - timedelta(days=1))
        self.patch_home_services()
        self.assertContains(self.client.get(reverse("fleetops:srp_home")), "permanently banned")

    def test_no_banner_without_a_ban(self):
        self.patch_home_services()
        self.assertNotContains(self.client.get(reverse("fleetops:srp_home")), 'id="srp-ban-banner"')

    def test_the_claim_page_offers_the_ban_to_reviewers_only(self):
        claim = self.member_claim()
        url = reverse("fleetops:srp_claim", args=[claim.uuid])

        reviewer_page = self.client_for(self.reviewer).get(url)
        self.assertContains(reviewer_page, 'id="srp-ban-panel"')
        self.assertContains(reviewer_page, "Ban this member from submitting")
        for user in (self.member, self.payer):
            self.assertNotContains(self.client_for(user).get(url), 'id="srp-ban-panel"')

    def test_the_claim_page_shows_the_ban_with_its_internal_note_and_a_lift_form(self):
        claim = self.member_claim()
        ban = make_ban(self.member, internal_note="Staff only", starts_at=timezone.now() - timedelta(days=1), expires_at=timezone.now() + timedelta(days=3))
        page = self.client_for(self.reviewer).get(reverse("fleetops:srp_claim", args=[claim.uuid]))
        self.assertContains(page, "Staff only")
        self.assertContains(page, reverse("fleetops:srp_ban_lift", args=[ban.pk]))
        # The member reading the claim never sees the note.
        self.assertNotContains(self.client_for(self.member).get(reverse("fleetops:srp_claim", args=[claim.uuid])), "Staff only")

    def test_a_reviewer_does_not_get_a_ban_panel_on_their_own_claim(self):
        claim = f.srp_claim(self.reviewer)
        self.assertNotContains(self.client_for(self.reviewer).get(reverse("fleetops:srp_claim", args=[claim.uuid])), 'id="srp-ban-panel"')

    def test_the_menu_lists_the_ban_page_for_reviewers(self):
        self.patch_home_services(user=self.reviewer)
        self.assertContains(self.client_for(self.reviewer).get(reverse("fleetops:srp_bans")), reverse("fleetops:srp_bans"))
        self.assertNotContains(self.client_for(self.payer).get(reverse("fleetops:srp_payments")), reverse("fleetops:srp_bans"))
