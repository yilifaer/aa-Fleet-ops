"""Monthly limit accounting: months in EVE time, usage from approved and paid claims, the quota lock."""

import threading
from datetime import datetime, timedelta, timezone as dt_timezone
from decimal import Decimal
from unittest import mock

from django.core import signing
from django.db import connection
from django.test import TestCase, TransactionTestCase, override_settings, skipUnlessDBFeature
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from fleetops.models import SrpClaim, SrpSettings
from fleetops.providers import zkillboard
from fleetops.srp import claims, quota, review
from fleetops.srp import policy as rules
from fleetops.srp import tasks as srp_tasks
from fleetops.srp.types import PolicySnapshot, SrpError, SrpLimitError, ZkillResult

from . import factories as f

Status = SrpClaim.Status
IN_FLEET = SrpClaim.Scope.IN_FLEET
OUT_OF_FLEET = SrpClaim.Scope.OUT_OF_FLEET


def utc(*args):
    return datetime(*args, tzinfo=dt_timezone.utc)


class ClaimMonthTests(TestCase):
    def test_month_is_the_eve_month_of_the_loss(self):
        self.assertEqual(quota.claim_month(utc(2026, 9, 14, 12, 0)), (2026, 9))

    def test_last_second_and_first_second_of_a_month(self):
        self.assertEqual(quota.claim_month(utc(2026, 9, 30, 23, 59, 59)), (2026, 9))
        self.assertEqual(quota.claim_month(utc(2026, 10, 1, 0, 0, 0)), (2026, 10))
        self.assertEqual(quota.claim_month(utc(2026, 12, 31, 23, 59, 59)), (2026, 12))
        self.assertEqual(quota.claim_month(utc(2027, 1, 1, 0, 0, 0)), (2027, 1))

    @override_settings(TIME_ZONE="Asia/Tokyo", USE_TZ=True)
    def test_site_time_zone_does_not_move_the_month(self):
        # 2026-09-30 23:30 UTC is already October 1st in Tokyo; the claim counts in September.
        self.assertEqual(quota.claim_month(utc(2026, 9, 30, 23, 30)), (2026, 9))
        self.assertEqual(quota.claim_month(utc(2026, 10, 1, 0, 0)), (2026, 10))

    def test_aware_time_in_another_zone_is_converted_to_utc(self):
        minus_five = dt_timezone(timedelta(hours=-5))
        self.assertEqual(quota.claim_month(datetime(2026, 9, 30, 20, 0, tzinfo=minus_five)), (2026, 10))
        plus_nine = dt_timezone(timedelta(hours=9))
        self.assertEqual(quota.claim_month(datetime(2026, 10, 1, 5, 0, tzinfo=plus_nine)), (2026, 9))

    def test_naive_time_is_taken_as_eve_time(self):
        self.assertEqual(quota.claim_month(datetime(2026, 9, 30, 23, 59, 59)), (2026, 9))

    def test_claim_and_killmail_objects_give_their_loss_month(self):
        user = f.create_user()
        claim = f.srp_claim(user, time=utc(2026, 9, 30, 23, 59, 59))
        self.assertEqual(quota.claim_month(claim), (2026, 9))
        self.assertEqual(quota.claim_month(claim.killmail), (2026, 9))

    def test_month_follows_the_loss_not_the_submission_or_decision(self):
        user = f.create_user()
        claim = f.srp_claim(
            user,
            status=Status.APPROVED,
            time=utc(2026, 8, 31, 23, 0),
            submitted_at=utc(2026, 9, 2, 10, 0),
            decided_at=utc(2026, 9, 3, 10, 0),
        )
        self.assertEqual(quota.claim_month(claim), (2026, 8))


class MonthUsageTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        f.srp_settings()
        cls.user = f.create_user("pilot", perms=f.SRP_MEMBER_PERMS)
        cls.other = f.create_user("other", perms=f.SRP_MEMBER_PERMS)
        cls.far = f.create_user("far", perms=f.SRP_MEMBER_PERMS, corporation=f.OTHER_CORP)
        cls.when = utc(2026, 9, 14, 12, 0)

    def usage(self, **kwargs):
        kwargs.setdefault("year", 2026)
        kwargs.setdefault("month", 9)
        return quota.month_usage(**kwargs)

    def claim(self, user=None, *, amount=100, status=Status.APPROVED, time=None, **extra):
        return f.srp_claim(user or self.user, amount=amount, status=status, time=time or self.when, **extra)

    def test_nothing_used_without_claims(self):
        self.assertEqual(self.usage(user_id=self.user.pk), (0, 0))
        self.assertEqual(self.usage(corporation_id=f.DEFAULT_CORP[0]), (0, 0))

    def test_approved_and_paid_amounts_are_used(self):
        self.claim(amount=100, status=Status.APPROVED)
        self.claim(amount=250, status=Status.PAID)
        self.assertEqual(self.usage(user_id=self.user.pk), (350, 0))

    def test_approved_amount_counts_not_the_suggestion(self):
        self.claim(amount=500, status=Status.APPROVED, approved_amount=320)
        self.assertEqual(self.usage(user_id=self.user.pk), (320, 0))

    def test_pending_claims_are_pending_not_used(self):
        self.claim(amount=100, status=Status.PENDING, limited_amount=70)
        self.assertEqual(self.usage(user_id=self.user.pk), (0, 70))

    def test_rejected_and_withdrawn_claims_use_nothing(self):
        self.claim(status=Status.REJECTED)
        self.claim(status=Status.WITHDRAWN)
        self.assertEqual(self.usage(user_id=self.user.pk), (0, 0))

    def test_revoked_approval_releases_the_amount(self):
        claim = self.claim(amount=400, status=Status.APPROVED)
        self.assertEqual(self.usage(user_id=self.user.pk), (400, 0))
        SrpClaim.objects.filter(pk=claim.pk).update(status=Status.PENDING, approved_amount=None)
        self.assertEqual(self.usage(user_id=self.user.pk), (0, 400))

    def test_the_loss_month_decides(self):
        self.claim(amount=100, time=utc(2026, 8, 31, 23, 59, 59))
        self.claim(amount=200, time=utc(2026, 9, 1, 0, 0, 0))
        self.claim(amount=300, time=utc(2026, 9, 30, 23, 59, 59))
        self.claim(amount=400, time=utc(2026, 10, 1, 0, 0, 0))
        self.assertEqual(self.usage(user_id=self.user.pk), (500, 0))
        self.assertEqual(self.usage(user_id=self.user.pk, month=8), (100, 0))
        self.assertEqual(self.usage(user_id=self.user.pk, month=10), (400, 0))
        self.assertEqual(self.usage(user_id=self.user.pk, year=2025), (0, 0))

    @override_settings(TIME_ZONE="America/New_York", USE_TZ=True)
    def test_month_bounds_are_utc_with_another_site_time_zone(self):
        self.claim(amount=100, time=utc(2026, 9, 1, 2, 0))
        self.claim(amount=200, time=utc(2026, 10, 1, 2, 0))
        self.assertEqual(self.usage(user_id=self.user.pk), (100, 0))
        self.assertEqual(self.usage(user_id=self.user.pk, month=10), (200, 0))

    def test_other_players_are_not_counted_in_the_personal_usage(self):
        self.claim(self.user, amount=100)
        self.claim(self.other, amount=900)
        self.assertEqual(self.usage(user_id=self.user.pk), (100, 0))
        self.assertEqual(self.usage(user_id=self.other.pk), (900, 0))

    def test_corporation_usage_sums_the_members_of_the_corporation(self):
        self.claim(self.user, amount=100)
        self.claim(self.other, amount=900)
        self.claim(self.far, amount=50)
        self.assertEqual(self.usage(corporation_id=f.DEFAULT_CORP[0]), (1000, 0))
        self.assertEqual(self.usage(corporation_id=f.OTHER_CORP[0]), (50, 0))

    def test_corporation_is_the_one_snapshotted_on_the_claim(self):
        self.claim(self.user, amount=100, corporation_id=f.THIRD_CORP[0])
        self.assertEqual(self.usage(corporation_id=f.DEFAULT_CORP[0]), (0, 0))
        self.assertEqual(self.usage(corporation_id=f.THIRD_CORP[0]), (100, 0))

    def test_personal_usage_honours_the_personal_flag(self):
        self.claim(amount=100, counts_personal=True)
        self.claim(amount=200, counts_personal=False)
        self.assertEqual(self.usage(user_id=self.user.pk), (100, 0))

    def test_corporation_usage_honours_the_corporation_flag(self):
        self.claim(amount=100, counts_corporation=True)
        self.claim(amount=200, counts_corporation=False)
        self.assertEqual(self.usage(corporation_id=f.DEFAULT_CORP[0]), (100, 0))

    def test_flags_are_independent(self):
        self.claim(amount=100, counts_personal=False, counts_corporation=True)
        self.assertEqual(self.usage(user_id=self.user.pk), (0, 0))
        self.assertEqual(self.usage(corporation_id=f.DEFAULT_CORP[0]), (100, 0))

    def test_flags_apply_to_pending_amounts_too(self):
        self.claim(status=Status.PENDING, amount=100, counts_personal=False)
        self.assertEqual(self.usage(user_id=self.user.pk), (0, 0))

    def test_claim_can_be_excluded(self):
        mine = self.claim(amount=100)
        self.claim(amount=200)
        self.assertEqual(self.usage(user_id=self.user.pk, exclude_claim_id=mine.pk), (200, 0))

    def test_locking_read_gives_the_same_numbers(self):
        self.claim(amount=100, status=Status.APPROVED)
        self.claim(amount=250, status=Status.PAID)
        self.claim(amount=50, status=Status.PENDING)
        self.claim(amount=999, status=Status.REJECTED)
        self.claim(amount=70, counts_personal=False)
        with quota.quota_lock():
            locked = self.usage(user_id=self.user.pk, locking=True)
            locked_corporation = self.usage(corporation_id=f.DEFAULT_CORP[0], locking=True)
        self.assertEqual(locked, self.usage(user_id=self.user.pk))
        self.assertEqual(locked, (350, 50))
        self.assertEqual(locked_corporation, self.usage(corporation_id=f.DEFAULT_CORP[0]))

    def test_needs_a_user_or_a_corporation(self):
        with self.assertRaises(ValueError):
            self.usage()

    def test_usage_is_a_plain_number(self):
        self.claim(amount=100)
        used, pending = self.usage(user_id=self.user.pk)
        self.assertIsInstance(used, int)
        self.assertIsInstance(pending, int)


class LimitStateTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        f.srp_settings()
        cls.user = f.create_user("pilot", perms=f.SRP_MEMBER_PERMS)
        cls.when = utc(2026, 9, 14, 12, 0)

    def states(self, scope=OUT_OF_FLEET, *, user_id="user", corporation_id=f.DEFAULT_CORP[0], **policy_values):
        return quota.limit_states(
            user_id=self.user.pk if user_id == "user" else user_id,
            corporation_id=corporation_id,
            scope=scope,
            killmail_time=self.when,
            policy=PolicySnapshot(**policy_values),
        )

    def claim(self, amount, **extra):
        return f.srp_claim(self.user, amount=amount, status=Status.APPROVED, time=self.when, **extra)

    def test_two_states_personal_then_corporation(self):
        states = self.states()
        self.assertEqual([state.kind for state in states], ["personal", "corporation"])

    def test_no_caps_means_no_remaining_amount(self):
        for state in self.states():
            self.assertIsNone(state.cap)
            self.assertIsNone(state.remaining)
            self.assertIsNone(state.effective_remaining)

    def test_remaining_is_the_cap_less_the_usage(self):
        self.claim(300_000_000)
        personal, corporation = self.states(personal_monthly_cap=1_000_000_000, corporation_monthly_cap=2_000_000_000)
        self.assertEqual((personal.cap, personal.used, personal.remaining), (1_000_000_000, 300_000_000, 700_000_000))
        self.assertEqual((corporation.cap, corporation.used, corporation.remaining), (2_000_000_000, 300_000_000, 1_700_000_000))

    def test_remaining_never_goes_below_zero(self):
        self.claim(900)
        personal, _corporation = self.states(personal_monthly_cap=500)
        self.assertEqual((personal.used, personal.remaining), (900, 0))

    def test_pending_amounts_are_reported_but_not_deducted(self):
        f.srp_claim(self.user, amount=200, status=Status.PENDING, time=self.when)
        personal, _ = self.states(personal_monthly_cap=1_000)
        self.assertEqual((personal.pending, personal.used, personal.remaining), (200, 0, 1_000))

    def test_out_of_fleet_claims_always_count_toward_both_limits(self):
        personal, corporation = self.states(
            OUT_OF_FLEET, personal_cap_includes_in_fleet=False, corporation_cap_includes_in_fleet=False
        )
        self.assertTrue(personal.applies)
        self.assertTrue(corporation.applies)

    def test_in_fleet_claims_follow_the_switches(self):
        personal, corporation = self.states(
            IN_FLEET, personal_cap_includes_in_fleet=False, corporation_cap_includes_in_fleet=True
        )
        self.assertFalse(personal.applies)
        self.assertTrue(corporation.applies)
        self.assertIsNone(personal.effective_remaining)

    def test_each_limit_follows_its_own_switch_for_in_fleet_claims(self):
        for personal_switch, corporation_switch in ((True, False), (False, True), (False, False), (True, True)):
            with self.subTest(personal=personal_switch, corporation=corporation_switch):
                personal, corporation = self.states(
                    IN_FLEET,
                    personal_cap_includes_in_fleet=personal_switch,
                    corporation_cap_includes_in_fleet=corporation_switch,
                )
                self.assertEqual((personal.applies, corporation.applies), (personal_switch, corporation_switch))

    def test_an_exhausted_corporation_cap_does_not_limit_an_in_fleet_claim_it_does_not_count(self):
        self.claim(100)
        _personal, corporation = self.states(
            IN_FLEET, corporation_monthly_cap=100, corporation_cap_includes_in_fleet=False
        )
        self.assertEqual((corporation.remaining, corporation.applies), (0, False))
        self.assertIsNone(corporation.effective_remaining)
        # The same cap limits an out-of-fleet claim, and an in-fleet one once the switch counts it.
        self.assertEqual(self.states(OUT_OF_FLEET, corporation_monthly_cap=100)[1].effective_remaining, 0)
        self.assertEqual(
            self.states(IN_FLEET, corporation_monthly_cap=100, corporation_cap_includes_in_fleet=True)[1].effective_remaining,
            0,
        )

    def test_states_without_a_user_or_corporation_have_no_usage(self):
        personal, corporation = self.states(user_id=None, corporation_id=None, personal_monthly_cap=500, corporation_monthly_cap=900)
        self.assertEqual((personal.used, personal.remaining), (0, 500))
        self.assertEqual((corporation.used, corporation.remaining), (0, 900))

    def test_excluding_the_claim_being_recalculated(self):
        mine = self.claim(400)
        self.claim(100)
        personal, _ = quota.limit_states(
            user_id=self.user.pk,
            corporation_id=None,
            scope=OUT_OF_FLEET,
            killmail_time=self.when,
            policy=PolicySnapshot(personal_monthly_cap=1_000),
            exclude_claim_id=mine.pk,
        )
        self.assertEqual((personal.used, personal.remaining), (100, 900))

    def test_month_comes_from_the_killmail_time(self):
        self.claim(400)
        personal, _ = quota.limit_states(
            user_id=self.user.pk,
            corporation_id=None,
            scope=OUT_OF_FLEET,
            killmail_time=utc(2026, 10, 2, 1, 0),
            policy=PolicySnapshot(personal_monthly_cap=1_000),
        )
        self.assertEqual(personal.used, 0)

    def test_corporation_override_cap_comes_from_the_policy(self):
        from fleetops.models import SrpCorporationLimit
        from fleetops.srp import policy as srp_policy

        SrpCorporationLimit.objects.create(corporation_id=f.DEFAULT_CORP[0], monthly_cap=250)
        snapshot = srp_policy.current_policy(
            corporation_id=f.DEFAULT_CORP[0], settings_obj=f.srp_settings(corporation_monthly_cap=10_000)
        )
        self.claim(100)
        _, corporation = quota.limit_states(
            user_id=self.user.pk,
            corporation_id=f.DEFAULT_CORP[0],
            scope=OUT_OF_FLEET,
            killmail_time=self.when,
            policy=snapshot,
        )
        self.assertEqual((corporation.cap, corporation.remaining), (250, 150))


class QuotaLockTests(TestCase):
    def test_first_statement_locks_the_settings_row(self):
        f.srp_settings()
        with CaptureQueriesContext(connection) as queries:
            with quota.quota_lock():
                SrpClaim.objects.count()
        statements = [q["sql"] for q in queries.captured_queries if "SAVEPOINT" not in q["sql"].upper()]
        first = statements[0]
        self.assertIn("fleetops_srpsettings", first)
        self.assertTrue(first.lstrip().upper().startswith("SELECT"))
        if connection.features.has_select_for_update:
            self.assertIn("FOR UPDATE", first.upper())

    def test_yields_the_locked_settings(self):
        f.srp_settings(personal_monthly_cap=123)
        with quota.quota_lock() as srp:
            self.assertIsInstance(srp, SrpSettings)
            self.assertEqual(srp.personal_monthly_cap, 123)

    def test_missing_settings_row_is_created(self):
        SrpSettings.objects.all().delete()
        with quota.quota_lock() as srp:
            self.assertEqual(srp.pk, 1)
        self.assertTrue(SrpSettings.objects.filter(pk=1).exists())

    def test_runs_in_a_transaction(self):
        f.srp_settings()
        with quota.quota_lock():
            self.assertTrue(connection.in_atomic_block)

    def test_a_failure_inside_rolls_the_work_back(self):
        f.srp_settings()
        user = f.create_user()
        with self.assertRaises(RuntimeError):
            with quota.quota_lock():
                f.srp_claim(user)
                raise RuntimeError("stop")
        self.assertFalse(SrpClaim.objects.exists())

    def test_nesting_is_allowed(self):
        f.srp_settings()
        with quota.quota_lock():
            with quota.quota_lock() as inner:
                self.assertEqual(inner.pk, 1)


@skipUnlessDBFeature("has_select_for_update")
class QuotaLockOnMariaDbTests(TestCase):
    def test_locking_statement_uses_for_update(self):
        f.srp_settings()
        with CaptureQueriesContext(connection) as queries:
            with quota.quota_lock():
                quota.month_usage(year=2026, month=9, user_id=1, locking=True)
        sql = [query["sql"].upper() for query in queries.captured_queries if "SAVEPOINT" not in query["sql"].upper()]
        self.assertIn("FOR UPDATE", sql[0])
        self.assertIn("SRPSETTINGS", sql[0])
        self.assertTrue(any("SRPCLAIM" in text and "FOR UPDATE" in text for text in sql[1:]))


def run_together(*targets):
    """Run each target in its own thread with its own database connection; returns the errors."""
    errors = []

    def run(target):
        try:
            target()
        except Exception as exc:
            errors.append(exc)
        finally:
            connection.close()

    threads = [threading.Thread(target=run, args=(target,)) for target in targets]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(60)
    return errors


@skipUnlessDBFeature("has_select_for_update")
class ConcurrentApprovalTests(TransactionTestCase):
    """Two managers approve claims of the same player near a cap at the same moment."""

    # Brings back the migrated rows (permissions, default templates) a flush removes.
    serialized_rollback = True
    CAP = 100_000_000

    def setUp(self):
        super().setUp()
        self.user = f.create_user("pilot", perms=f.SRP_MEMBER_PERMS)
        self.manager_a = f.create_user("manager-a", perms=f.SRP_MANAGER_PERMS)
        self.manager_b = f.create_user("manager-b", perms=f.SRP_MANAGER_PERMS)
        when = timezone.now() - timedelta(hours=2)
        self.first = f.srp_claim(self.user, amount=80_000_000, time=when)
        self.second = f.srp_claim(self.user, amount=80_000_000, time=when - timedelta(minutes=5))

    def approve_both(self, action_a, action_b):
        """Both decisions start together; the first one to take the lock holds it for a moment."""
        together = threading.Barrier(2, timeout=10)
        original = quota.month_usage

        def pause_in_the_first_usage_read(*args, **kwargs):
            result = original(*args, **kwargs)
            if kwargs.get("locking") and not getattr(pause_in_the_first_usage_read, "paused", False):
                pause_in_the_first_usage_read.paused = True
                # The other decision is waiting for the settings row by now.
                threading.Event().wait(0.7)
            return result

        def start(action):
            def run():
                together.wait()
                action()

            return run

        with mock.patch.object(quota, "month_usage", side_effect=pause_in_the_first_usage_read):
            return run_together(start(action_a), start(action_b))

    def approve(self, claim, actor, **kwargs):
        def run():
            review.approve_claim(SrpClaim.objects.get(pk=claim.pk), actor=actor, amount=80_000_000, **kwargs)

        return run

    def policy(self, **values):
        f.srp_settings(personal_monthly_cap=self.CAP, **values)
        for claim in (self.first, self.second):
            policy = PolicySnapshot(personal_monthly_cap=self.CAP, **values).as_json()
            SrpClaim.objects.filter(pk=claim.pk).update(policy=policy)

    def test_reject_mode_lets_exactly_one_claim_through(self):
        self.policy(over_limit_action="reject")
        errors = self.approve_both(
            self.approve(self.first, self.manager_a, confirm_above_policy=True),
            self.approve(self.second, self.manager_b, confirm_above_policy=True),
        )
        self.assertEqual(len(errors), 1, errors)
        self.assertIsInstance(errors[0], SrpLimitError)
        self.assertEqual(errors[0].code, "over_limit")
        approved = SrpClaim.objects.filter(status=Status.APPROVED)
        self.assertEqual(approved.count(), 1)
        self.assertEqual(SrpClaim.objects.filter(status=Status.PENDING).count(), 1)
        self.assertEqual(quota.month_usage(year=approved[0].killmail_time.year, month=approved[0].killmail_time.month, user_id=self.user.pk)[0], 80_000_000)

    def test_review_mode_flags_the_second_decision_as_over_the_limit(self):
        self.policy(over_limit_action="review")
        errors = self.approve_both(self.approve(self.first, self.manager_a), self.approve(self.second, self.manager_b))
        self.assertEqual(len(errors), 1, errors)
        self.assertIsInstance(errors[0], SrpLimitError)
        self.assertEqual(errors[0].code, "confirm_over_limit")
        self.assertEqual(SrpClaim.objects.filter(status=Status.APPROVED).count(), 1)

    def test_review_mode_second_decision_can_go_on_with_confirmation(self):
        self.policy(over_limit_action="review")
        errors = self.approve_both(
            self.approve(self.first, self.manager_a, reason="Fleet loss", confirm_over_limit=True),
            self.approve(self.second, self.manager_b, reason="Fleet loss", confirm_over_limit=True),
        )
        self.assertEqual(errors, [])
        self.assertEqual(SrpClaim.objects.filter(status=Status.APPROVED).count(), 2)

    def test_a_value_that_arrives_while_the_claim_is_made_reaches_the_claim(self):
        # The sweep stores the value while the claim is being made. Its update of the killmail waits for the
        # claim (the killmail row is locked with the quota lock), and then finds the claim and calculates it.
        f.srp_settings(enabled=True, percentage=Decimal("80"))
        when = timezone.now() - timedelta(hours=2)
        km = f.srp_killmail(
            f.main_of(self.user), time=when, zkill_status="pending",
            zkill_next_attempt_at=timezone.now() - timedelta(minutes=1),
        )
        token = signing.dumps({"km": km.pk, "link": None, "u": self.user.pk}, salt=claims.PREVIEW_SALT)
        inside = threading.Event()
        eligibility = rules.eligibility

        def eligibility_then_wait(*args, **kwargs):
            result = eligibility(*args, **kwargs)
            inside.set()
            threading.Event().wait(1.0)
            return result

        answer = ZkillResult(
            status="ok", killmail_hash="a" * 40, fitted_value=Decimal("100000000"), total_value=Decimal("110000000")
        )

        def submit():
            claims.submit_claim(self.user, token)

        def sweep():
            inside.wait(10)
            srp_tasks.refresh_srp_values()

        with mock.patch.object(rules, "eligibility", side_effect=eligibility_then_wait), mock.patch.object(
            zkillboard, "fetch_kill", return_value=answer
        ):
            errors = run_together(submit, sweep)

        self.assertEqual(errors, [])
        claim = SrpClaim.objects.get(killmail=km)
        self.assertEqual((claim.payout_basis, claim.suggested_amount), ("percentage", 80_000_000))
        self.assertEqual(claim.loss_value, Decimal("100000000.00"))
        self.assertNotIn("value_pending", [warning["code"] for warning in claim.warnings])

    def test_two_submissions_of_one_killmail_make_one_claim(self):
        f.srp_settings(enabled=True)
        km = f.srp_killmail(f.main_of(self.user))
        tokens = [signing.dumps({"km": km.pk, "link": None, "u": self.user.pk}, salt=claims.PREVIEW_SALT)] * 2
        together = threading.Barrier(2, timeout=10)
        outcomes = []

        def submit():
            together.wait()
            try:
                outcomes.append(claims.submit_claim(self.user, tokens[0]))
            except SrpError as exc:
                outcomes.append(exc)

        errors = run_together(submit, submit)
        self.assertEqual(errors, [])
        self.assertEqual(SrpClaim.objects.filter(killmail=km).count(), 1)
        self.assertEqual(sum(isinstance(outcome, SrpClaim) for outcome in outcomes), 1)
        self.assertEqual(sum(getattr(outcome, "code", "") == "duplicate" for outcome in outcomes), 1)
