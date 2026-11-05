# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""INT-13: the claimant changes a pending claim (comment, category or link, the loss), the revision number the review
checks, and the edit pages."""

from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.test import Client
from django.urls import reverse
from django.utils import timezone

from fleetops.models import AuditLog, SrpBan, SrpClaim, SrpClaimEvent, SrpKillmail, SrpLoss
from fleetops.srp import claims, review
from fleetops.srp.types import KillmailRef, SrpDuplicateError, SrpError, SrpStateError

from . import factories as f
from .test_srp_claims import ClaimBase

Status = SrpClaim.Status
Action = SrpClaimEvent.Action


class EditBase(ClaimBase):
    def setUp(self):
        super().setUp()
        self.settings(percentage=Decimal("80"), default_category=None, out_of_fleet_category_required=False)

    def pending(self, **kwargs):
        return self.submit(self.killmail(**kwargs))

    def edit(self, claim, **kwargs):
        return claims.edit_claim(claim, actor=self.user, **kwargs)

    def swapped(self, km, claim, **kwargs):
        """Replace the loss of ``claim`` with ``km`` (the read of ESI is replaced by the stored killmail)."""
        ref = KillmailRef(km.killmail_id, km.killmail_hash, "ingame")
        with mock.patch.object(claims.killmails, "fetch_killmail", return_value=km) as fetch:
            result = self.edit(claim, new_ref=ref, **kwargs)
        self.fetch = fetch
        return result


class CommentEditTests(EditBase):
    def test_changing_only_the_comment_keeps_the_amounts_and_counts_a_revision(self):
        claim = self.pending()
        before = (claim.suggested_amount, claim.limited_amount, claim.calculation)

        result = self.edit(claim, comment="  Lost to a gate camp  ")

        claim.refresh_from_db()
        self.assertEqual(result.changed, ["comment"])
        self.assertEqual(claim.member_comment, "Lost to a gate camp")
        self.assertEqual((claim.member_revision, claim.status), (1, Status.PENDING))
        self.assertIsNotNone(claim.member_edited_at)
        self.assertEqual((claim.suggested_amount, claim.limited_amount, claim.calculation), before)
        self.assertIsNone(result.evaluation)
        event = claim.events.get(action=Action.MEMBER_EDIT)
        self.assertEqual((event.actor, event.reason, event.data["changed"]), (self.user, "Lost to a gate camp", ["comment"]))
        self.assertEqual(event.data["from"], {"comment": ""})

    def test_the_audit_entry_has_no_comment_text(self):
        claim = self.pending()

        self.edit(claim, comment="Private words here")

        entry = AuditLog.objects.get(action="srp.claim_member_edit")
        self.assertEqual(entry.reason, "")
        self.assertNotIn("Private words", str(entry.new_value) + str(entry.old_value))
        self.assertEqual(entry.new_value["comment_length"], len("Private words here"))
        self.assertEqual(entry.new_value["revision"], 1)

    def test_nothing_to_change_is_refused_and_counts_nothing(self):
        claim = self.pending()
        with self.assertRaises(SrpError) as caught:
            self.edit(claim, comment="")
        self.assertEqual(caught.exception.code, "nothing_changed")
        claim.refresh_from_db()
        self.assertEqual(claim.member_revision, 0)
        self.assertFalse(claim.events.filter(action=Action.MEMBER_EDIT).exists())

    def test_a_long_comment_is_refused(self):
        claim = self.pending()
        with self.assertRaises(SrpError) as caught:
            self.edit(claim, comment="x" * 1001)
        self.assertEqual(caught.exception.code, "comment_too_long")


class RefusalTests(EditBase):
    def test_only_the_claimant_can_edit(self):
        claim = self.pending()
        for actor in (self.other, self.manager, None):
            with self.subTest(actor=actor):
                with self.assertRaises(SrpError) as caught:
                    claims.edit_claim(claim, actor=actor, comment="Hello")
                self.assertEqual(caught.exception.code, "forbidden")

    def test_only_a_pending_claim_can_be_edited(self):
        for status in (Status.APPROVED, Status.REJECTED, Status.WITHDRAWN, Status.PAID):
            with self.subTest(status=status):
                claim = f.srp_claim(self.user, status=status, decided_by=self.manager)
                with self.assertRaises(SrpError) as caught:
                    self.edit(claim, comment="Hello")
                self.assertIn(caught.exception.code, {"wrong_state", "already"})
                claim.refresh_from_db()
                self.assertEqual(claim.member_revision, 0)

    def test_the_switch_turns_the_edit_off(self):
        claim = self.pending()
        self.settings(member_edit_enabled=False)
        with self.assertRaises(SrpError) as caught:
            self.edit(claim, comment="Hello")
        self.assertEqual(caught.exception.code, "edit_off")

    def test_a_banned_member_cannot_edit_but_can_withdraw(self):
        claim = self.pending()
        SrpBan.objects.create(user=self.user, reason="Abuse", created_by=self.manager)

        with self.assertRaises(SrpError) as caught:
            self.edit(claim, comment="Hello")

        self.assertEqual(caught.exception.code, "banned")
        claims.withdraw_claim(claim, actor=self.user)
        claim.refresh_from_db()
        self.assertEqual(claim.status, Status.WITHDRAWN)

    def test_a_revision_that_moved_on_is_refused(self):
        claim = self.pending()
        self.edit(claim, comment="First")
        with self.assertRaises(SrpStateError) as caught:
            self.edit(claim, comment="Second", expected_revision=0)
        self.assertEqual(caught.exception.code, "changed")


class CategoryEditTests(EditBase):
    def categories(self):
        solo = f.srp_category("Solo PVP", percentage=Decimal("80"))
        home = f.srp_category("Home defense", percentage=Decimal("50"))
        return solo, home

    def test_a_category_alone_puts_the_claim_under_the_standing_link_of_the_day(self):
        solo, home = self.categories()
        km = self.killmail(fitted="100000000")
        claim = f.srp_claim(self.user, killmail=km, category=solo, suggested_amount=80_000_000, limited_amount=80_000_000)
        link = f.standing_link(category=home, link_date=km.killmail_time.date())

        result = self.edit(claim, category=home)

        claim.refresh_from_db()
        self.assertEqual(set(result.changed), {"category", "link"})
        self.assertEqual((claim.category, claim.link, claim.scope), (home, link, SrpClaim.Scope.OUT_OF_FLEET))
        self.assertEqual(claim.suggested_amount, 50_000_000)
        self.assertEqual(claim.member_revision, 1)
        event = claim.events.get(action=Action.MEMBER_EDIT)
        self.assertEqual(event.data["from"]["category"], solo.key)
        self.assertEqual(event.data["to"], {"category": home.key, "link": link.code})
        self.assertEqual(event.data["previous"]["suggested_amount"], 80_000_000)

    def test_a_category_without_a_link_for_the_day_only_records_the_category(self):
        solo, home = self.categories()
        claim = f.srp_claim(self.user, killmail=self.killmail(), category=solo)

        self.edit(claim, category=home)

        claim.refresh_from_db()
        self.assertEqual((claim.category, claim.link), (home, None))

    def test_the_day_must_have_a_link_when_the_settings_ask_for_one(self):
        solo, home = self.categories()
        self.settings(category_claims_without_link=False)
        claim = f.srp_claim(self.user, killmail=self.killmail(), category=solo)

        with self.assertRaises(SrpError) as caught:
            self.edit(claim, category=home)

        self.assertEqual(caught.exception.code, "not_eligible")
        claim.refresh_from_db()
        self.assertEqual((claim.category, claim.member_revision), (solo, 0))

    def test_a_loss_of_another_day_gets_the_link_date_warning(self):
        solo, home = self.categories()
        km = self.killmail()
        claim = f.srp_claim(self.user, killmail=km, category=solo)
        link = f.standing_link(category=home, link_date=km.killmail_time.date())
        # The loss is of the day before the link's.
        SrpKillmail.objects.filter(pk=km.pk).update(killmail_time=km.killmail_time - timedelta(days=1))
        SrpClaim.objects.filter(pk=claim.pk).update(killmail_time=km.killmail_time - timedelta(days=1))
        claim.refresh_from_db()

        self.edit(claim, target_link=link)

        claim.refresh_from_db()
        self.assertIn("outside_link_date", [w["code"] for w in claim.warnings])

    def test_a_closed_link_is_not_a_target(self):
        solo, home = self.categories()
        claim = f.srp_claim(self.user, killmail=self.killmail(), category=solo)
        link = f.standing_link(category=home, is_open=False)
        with self.assertRaises(SrpError) as caught:
            self.edit(claim, target_link=link)
        self.assertEqual(caught.exception.code, "not_eligible")

    def test_a_fleet_link_makes_the_claim_in_fleet(self):
        operation, link = self.fleet()
        km = self.killmail(time=timezone.now() - timedelta(minutes=2))
        claim = f.srp_claim(self.user, killmail=km)

        self.edit(claim, target_link=link)

        claim.refresh_from_db()
        self.assertEqual((claim.scope, claim.link, claim.operation), (SrpClaim.Scope.IN_FLEET, link, operation))
        self.assertEqual(claim.category, f.fleet_category())
        self.assertEqual(claim.fleet_check, "in_fleet")

    def test_a_claim_a_reviewer_moved_keeps_only_the_comment_and_the_loss(self):
        solo, home = self.categories()
        claim = f.srp_claim(self.user, killmail=self.killmail(), category=solo)
        review.recategorize_claim(claim, actor=self.manager, category=home, reason="It was a defense")
        claim.refresh_from_db()

        with self.assertRaises(SrpError) as caught:
            self.edit(claim, category=solo)
        self.assertEqual(caught.exception.code, "after_review")
        self.edit(claim, comment="Thanks")
        claim.refresh_from_db()
        self.assertEqual((claim.category, claim.member_comment), (home, "Thanks"))

    def test_the_same_place_is_not_a_change(self):
        solo, _home = self.categories()
        claim = f.srp_claim(self.user, killmail=self.killmail(), category=solo)
        with self.assertRaises(SrpError) as caught:
            self.edit(claim, category=solo)
        self.assertEqual(caught.exception.code, "nothing_changed")


class SwapLossTests(EditBase):
    def test_another_loss_of_the_claimant_replaces_the_killmail_and_keeps_the_number(self):
        claim = self.pending(fitted="100000000")
        alt = f.add_alt(self.user)
        new = self.killmail(character=alt, fitted="200000000", ship_name="Moa", ship_type_id=623)
        old_pk, number = claim.killmail_id, claim.pk

        result = self.swapped(new, claim)

        claim.refresh_from_db()
        self.assertEqual(claim.pk, number)
        self.assertEqual(claim.killmail_id, new.pk)
        self.assertEqual((claim.character_id, claim.ship_type_id, claim.ship_type_name), (alt.character_id, 623, "Moa"))
        self.assertEqual(claim.killmail_time, new.killmail_time)
        self.assertEqual(claim.suggested_amount, 160_000_000)
        self.assertEqual((claim.user, claim.main_character_id), (self.user, self.main.character_id))
        self.assertEqual(result.old_killmail_id, old_pk)
        # The old killmail is free again and is cleaned up with the other unclaimed ones.
        self.assertFalse(SrpClaim.objects.filter(killmail_id=old_pk).exists())
        SrpKillmail.objects.filter(pk=old_pk).update(fetched_at=timezone.now() - timedelta(days=30))
        from fleetops.srp.killmails import prune_unclaimed

        prune_unclaimed(7)
        self.assertFalse(SrpKillmail.objects.filter(pk=old_pk).exists())
        self.assertTrue(SrpKillmail.objects.filter(pk=new.pk).exists())
        event = claim.events.get(action=Action.MEMBER_EDIT)
        self.assertEqual((event.data["changed"], event.data["old_killmail_id"]), (["loss"], old_pk))

    def test_the_read_of_the_killmail_happens_before_the_lock(self):
        claim = self.pending()
        new = self.killmail()
        order = []
        real_lock = claims.quota.quota_lock

        def fetch(*args, **kwargs):
            order.append("fetch")
            return new

        def lock():
            order.append("lock")
            return real_lock()

        with mock.patch.object(claims.killmails, "fetch_killmail", side_effect=fetch), mock.patch.object(
            claims.quota, "quota_lock", side_effect=lock
        ):
            self.edit(claim, new_ref=KillmailRef(new.killmail_id, new.killmail_hash, "ingame"))

        self.assertEqual(order[:2], ["fetch", "lock"])

    def test_a_loss_somebody_claimed_already_is_refused_and_the_claim_stays(self):
        claim = self.pending()
        taken = self.killmail(character=self.other and f.main_of(self.other))
        f.srp_claim(self.other, killmail=taken)

        with self.assertRaises(SrpDuplicateError):
            self.swapped(taken, claim)

        claim.refresh_from_db()
        self.assertEqual((claim.member_revision, claim.killmail_id == taken.pk), (0, False))

    def test_a_loss_of_somebody_else_is_refused(self):
        claim = self.pending()
        foreign = self.killmail(character=f.main_of(self.other))
        with self.assertRaises(SrpError) as caught:
            self.swapped(foreign, claim)
        self.assertEqual(caught.exception.code, "not_eligible")

    def test_a_loss_beyond_the_deadline_is_refused(self):
        self.settings(claim_deadline_days=7)
        claim = self.pending()
        old = self.killmail(time=timezone.now() - timedelta(days=9))
        with self.assertRaises(SrpError) as caught:
            self.swapped(old, claim)
        self.assertEqual(caught.exception.code, "not_eligible")
        claim.refresh_from_db()
        self.assertEqual(claim.member_revision, 0)

    def test_the_same_loss_is_not_a_change(self):
        claim = self.pending()
        with self.assertRaises(SrpError) as caught:
            self.swapped(claim.killmail, claim)
        self.assertEqual(caught.exception.code, "same_loss")

    def test_swapping_can_be_switched_off(self):
        claim = self.pending()
        self.settings(member_swap_loss_enabled=False)
        with self.assertRaises(SrpError) as caught:
            self.swapped(self.killmail(), claim)
        self.assertEqual(caught.exception.code, "swap_off")
        self.edit(claim, comment="Still works")

    def test_unauthorized_alts_stop_the_swap_like_a_new_claim(self):
        claim = self.pending()
        self.settings(loss_discovery_enabled=True, require_all_alts_authorized=True)
        f.add_alt(self.user, "Silent Alt")
        with self.assertRaises(SrpError) as caught:
            self.swapped(self.killmail(), claim)
        self.assertEqual(caught.exception.code, "not_eligible")
        self.assertIn("Silent Alt", caught.exception.message)

    def test_a_loss_of_another_month_is_reported_and_uses_the_limit_of_that_month(self):
        claim = self.pending()
        later = self.killmail(time=timezone.now() - timedelta(days=45))
        self.settings(personal_monthly_cap=100_000_000)
        f.srp_claim(
            self.user,
            amount=90_000_000,
            status=Status.APPROVED,
            killmail=self.killmail(time=later.killmail_time),
            decided_by=self.manager,
        )

        result = self.swapped(later, claim)

        self.assertTrue(result.month_changed)
        claim.refresh_from_db()
        self.assertTrue(claim.over_limit)
        self.assertEqual(claim.limited_amount, 10_000_000)

    def test_an_imported_placeholder_is_not_sent_to_zkillboard(self):
        claim = self.pending()
        placeholder = self.killmail(origin=SrpKillmail.Origin.AASRP, zkill_status=SrpKillmail.ZkillStatus.PENDING)
        SrpKillmail.objects.filter(pk=placeholder.pk).update(zkill_next_attempt_at=None)
        with mock.patch.object(claims.killmails, "schedule_zkill") as schedule:
            self.swapped(placeholder, claim)
        schedule.assert_not_called()

    def test_a_loss_read_for_the_swap_is_sent_to_zkillboard_once(self):
        claim = self.pending()
        new = self.killmail(zkill_status=SrpKillmail.ZkillStatus.PENDING, fitted=None)
        SrpKillmail.objects.filter(pk=new.pk).update(zkill_next_attempt_at=None)
        new.refresh_from_db()
        with mock.patch.object(claims.killmails, "schedule_zkill") as schedule:
            self.swapped(new, claim)
        schedule.assert_called_once()

    def test_a_concurrent_claim_for_the_same_loss_is_reported_as_a_duplicate(self):
        claim = self.pending()
        new = self.killmail()
        other = f.create_user("edit_race_other", perms=f.SRP_MEMBER_PERMS)
        f.srp_claim(other, killmail=new)
        real_save, real_duplicate = SrpClaim.save, claims._duplicate
        calls = []

        def duplicate(user, km):
            # The check before the write does not see the other claim yet (it was made at the same moment).
            calls.append(km.pk)
            return None if len(calls) == 1 else real_duplicate(user, km)

        def save(instance, *args, **kwargs):
            from django.db import IntegrityError

            raise IntegrityError("unique")

        with mock.patch.object(claims, "_duplicate", duplicate), mock.patch.object(SrpClaim, "save", save):
            with self.assertRaises(SrpDuplicateError) as caught:
                self.swapped(new, claim)
        self.assertEqual(caught.exception.claim.killmail_id, new.pk)
        self.assertEqual(len(calls), 2)
        claim.refresh_from_db()
        self.assertEqual(claim.member_revision, 0)
        self.assertIs(SrpClaim.save, real_save)


class AutoRejectEditTests(EditBase):
    def test_a_costlier_loss_is_refused_when_the_limit_is_used_up_in_reject_mode(self):
        self.settings(personal_monthly_cap=100_000_000, over_limit_action="reject")
        claim = self.pending(fitted="10000000")
        f.srp_claim(self.user, amount=100_000_000, status=Status.APPROVED, decided_by=self.manager)
        dearer = self.killmail(fitted="500000000")

        with self.assertRaises(SrpError) as caught:
            self.swapped(dearer, claim)

        self.assertEqual(caught.exception.code, "edit_would_reject")
        claim.refresh_from_db()
        self.assertEqual((claim.status, claim.member_revision, claim.killmail_id == dearer.pk), (Status.PENDING, 0, False))
        self.assertFalse(claim.events.filter(action=Action.MEMBER_EDIT).exists())


class PreviewTests(EditBase):
    def test_a_dry_run_changes_nothing_and_shows_the_claim_as_it_would_be(self):
        claim = self.pending(fitted="100000000")
        new = self.killmail(fitted="50000000")

        with mock.patch.object(claims.killmails, "fetch_killmail", return_value=new):
            result, token = claims.preview_edit(
                self.user, claim, comment="New words", ref=KillmailRef(new.killmail_id, new.killmail_hash, "ingame")
            )

        self.assertEqual(result.claim.suggested_amount, 40_000_000)
        self.assertEqual(result.claim.member_comment, "New words")
        claim.refresh_from_db()
        self.assertEqual((claim.suggested_amount, claim.member_comment, claim.member_revision), (80_000_000, "", 0))
        self.assertEqual(AuditLog.objects.filter(action="srp.claim_member_edit").count(), 0)
        self.assertTrue(token)

    def test_the_token_applies_the_checked_change(self):
        claim = self.pending()
        _result, token = claims.preview_edit(self.user, claim, comment="Checked")

        claims.submit_edit(self.user, claim, token)

        claim.refresh_from_db()
        self.assertEqual((claim.member_comment, claim.member_revision), ("Checked", 1))

    def test_a_claim_that_changed_since_the_preview_refuses_the_token(self):
        claim = self.pending()
        _result, token = claims.preview_edit(self.user, claim, comment="Checked")
        self.edit(claim, comment="Changed meanwhile")

        with self.assertRaises(SrpStateError) as caught:
            claims.submit_edit(self.user, claim, token)

        self.assertEqual(caught.exception.code, "changed")

    def test_a_token_of_somebody_else_or_a_forged_one_is_refused(self):
        claim = self.pending()
        _result, token = claims.preview_edit(self.user, claim, comment="Checked")
        for who, text in ((self.other, token), (self.user, token + "x"), (self.user, "")):
            with self.subTest(who=who.username, text=text[-3:]):
                with self.assertRaises(SrpError) as caught:
                    claims.submit_edit(who, claim, text)
                self.assertEqual(caught.exception.code, "invalid_token")


class ReviewRevisionTests(EditBase):
    """The review actions carry the revision the page showed (INT-13 item 4)."""

    def test_approve_is_refused_after_the_claimant_changed_the_claim(self):
        claim = self.pending()
        self.edit(claim, comment="Changed after you looked")

        with self.assertRaises(SrpStateError) as caught:
            review.approve_claim(claim, actor=self.manager, amount=claim.suggested_amount, expected_revision=0)

        self.assertEqual(caught.exception.code, "changed")
        self.assertIn("claimant changed this claim", caught.exception.message)
        claim.refresh_from_db()
        self.assertEqual(claim.status, Status.PENDING)

    def test_withdraw_and_resubmit_move_the_revision(self):
        claim = self.pending()
        claims.withdraw_claim(claim, actor=self.user)
        claims.resubmit_claim(claim, actor=self.user)
        claim.refresh_from_db()
        self.assertEqual((claim.status, claim.member_revision), (Status.PENDING, 1))
        self.assertIsNotNone(claim.member_edited_at)

        with self.assertRaises(SrpStateError) as caught:
            review.reject_claim(claim, actor=self.manager, reason="No", expected_revision=0)

        self.assertEqual(caught.exception.code, "changed")
        claim.refresh_from_db()
        self.assertEqual(claim.status, Status.PENDING)

    def test_the_current_revision_goes_through(self):
        claim = self.pending()
        self.edit(claim, comment="Changed")
        review.approve_claim(claim, actor=self.manager, amount=claim.suggested_amount, expected_revision=1)
        claim.refresh_from_db()
        self.assertEqual(claim.status, Status.APPROVED)

    def test_reject_reclassify_and_recategorize_check_the_revision_too(self):
        category = f.srp_category("Other")
        for name, call in (
            ("reject", lambda c: review.reject_claim(c, actor=self.manager, reason="No", expected_revision=0)),
            (
                "recategorize",
                lambda c: review.recategorize_claim(c, actor=self.manager, category=category, reason="Yes", expected_revision=0),
            ),
            (
                "reclassify",
                lambda c: review.reclassify_claim(
                    c, actor=self.manager, scope=SrpClaim.Scope.IN_FLEET, link=self.fleet()[1], reason="Yes", expected_revision=0
                ),
            ),
        ):
            with self.subTest(action=name):
                claim = self.pending()
                self.edit(claim, comment="Changed")
                with self.assertRaises(SrpStateError) as caught:
                    call(claim)
                self.assertEqual(caught.exception.code, "changed")

    def test_the_bulk_approval_skips_a_claim_that_changed(self):
        changed = self.pending()
        self.edit(changed, comment="Changed")
        untouched = self.pending()

        result = review.approve_clean_claims(
            [changed, untouched],
            actor=self.manager,
            expected_revisions={changed.pk: 0, untouched.pk: 0},
        )

        self.assertEqual([claim.pk for claim in result.done], [untouched.pk])
        self.assertEqual([claim.pk for claim, _text in result.skipped], [changed.pk])
        self.assertIn("claimant changed this claim", result.skipped[0][1])


class EditPageTests(EditBase):
    def setUp(self):
        super().setUp()
        self.client = Client(raise_request_exception=True)
        self.client.force_login(self.user)

    def url(self, claim):
        return reverse("fleetops:srp_claim_edit", kwargs={"claim_uuid": claim.uuid})

    def test_the_claim_page_offers_the_button_to_the_claimant_only_while_pending(self):
        claim = self.pending()
        page = reverse("fleetops:srp_claim", kwargs={"claim_uuid": claim.uuid})
        self.assertContains(self.client.get(page), self.url(claim))
        staff = Client()
        staff.force_login(self.manager)
        self.assertNotContains(staff.get(page), self.url(claim))
        SrpClaim.objects.filter(pk=claim.pk).update(status=Status.APPROVED)
        self.assertNotContains(self.client.get(page), self.url(claim))

    def test_the_button_is_gone_when_the_switch_is_off_or_the_member_is_banned(self):
        claim = self.pending()
        page = reverse("fleetops:srp_claim", kwargs={"claim_uuid": claim.uuid})
        self.settings(member_edit_enabled=False)
        self.assertNotContains(self.client.get(page), self.url(claim))
        self.settings(member_edit_enabled=True)
        SrpBan.objects.create(user=self.user, reason="Abuse", created_by=self.manager)
        response = self.client.get(page)
        self.assertNotContains(response, self.url(claim))
        self.assertContains(response, "Withdraw claim")

    def test_other_people_get_a_404_including_managers(self):
        claim = self.pending()
        for user in (self.other, self.manager):
            with self.subTest(user=user.username):
                client = Client()
                client.force_login(user)
                self.assertEqual(client.get(self.url(claim)).status_code, 404)
                self.assertEqual(client.post(self.url(claim), {"comment": "x"}).status_code, 404)

    def test_staff_without_request_srp_get_a_404_too(self):
        claim = self.pending()
        for perms in (f.SRP_REVIEWER_PERMS, f.SRP_PAYER_PERMS, f.MEMBER_PERMS + ["fleetops.manage_srp"]):
            user = f.create_user(perms=perms)
            with self.subTest(perms=perms):
                client = Client()
                client.force_login(user)
                self.assertEqual(client.get(self.url(claim)).status_code, 404)
                self.assertEqual(client.post(self.url(claim), {"comment": "x"}).status_code, 404)

    def test_the_claimant_without_request_srp_gets_a_403(self):
        user = f.create_user(perms=f.MEMBER_PERMS)
        claim = f.srp_claim(user)
        client = Client()
        client.force_login(user)
        self.assertEqual(client.get(self.url(claim)).status_code, 403)

    def test_the_form_then_the_preview_then_the_confirmation(self):
        claim = self.pending()
        self.assertEqual(self.client.get(self.url(claim)).status_code, 200)

        preview = self.client.post(self.url(claim), {"comment": "From the page"})

        self.assertContains(preview, "Check the change")
        token = preview.context["token"]
        self.assertEqual(claim.__class__.objects.get(pk=claim.pk).member_comment, "")

        done = self.client.post(self.url(claim), {"token": token}, follow=True)

        self.assertEqual(done.status_code, 200)
        claim.refresh_from_db()
        self.assertEqual((claim.member_comment, claim.member_revision), ("From the page", 1))

    def test_a_refused_change_shows_the_form_with_the_reason(self):
        claim = self.pending()
        response = self.client.post(self.url(claim), {"comment": ""})
        self.assertContains(response, "Nothing was changed.")

    def test_the_switch_off_refuses_the_page_and_the_post(self):
        claim = self.pending()
        self.settings(member_edit_enabled=False)
        for method in (self.client.get, lambda url: self.client.post(url, {"comment": "x"})):
            response = method(self.url(claim))
            self.assertEqual(response.status_code, 302)
        claim.refresh_from_db()
        self.assertEqual(claim.member_revision, 0)

    def test_a_banned_member_is_sent_back_with_the_ban_text(self):
        claim = self.pending()
        SrpBan.objects.create(user=self.user, reason="Abuse of the table", created_by=self.manager)
        response = self.client.post(self.url(claim), {"comment": "x"}, follow=True)
        self.assertContains(response, "Abuse of the table")
        claim.refresh_from_db()
        self.assertEqual(claim.member_revision, 0)

    def test_a_non_pending_claim_cannot_be_edited_by_post(self):
        claim = self.pending()
        SrpClaim.objects.filter(pk=claim.pk).update(status=Status.APPROVED)
        response = self.client.post(self.url(claim), {"comment": "x"}, follow=True)
        self.assertContains(response, "Only pending claims can be changed.")
        claim.refresh_from_db()
        self.assertEqual(claim.member_revision, 0)

    def test_pasted_and_chosen_loss_together_are_refused(self):
        self.settings(loss_discovery_enabled=True)
        claim = self.pending()
        new = self.killmail()
        SrpLoss.objects.create(
            character_id=self.main.character_id,
            killmail_id=new.killmail_id,
            killmail_hash=new.killmail_hash,
            kind=SrpLoss.Kind.LOSS,
            killmail_time=new.killmail_time,
            ship_type_id=new.ship_type_id,
        )
        response = self.client.post(
            self.url(claim), {"comment": "", "killmail": self.text(new), "loss": str(new.killmail_id)}
        )
        self.assertContains(response, "Choose one loss")

    def test_the_review_page_says_when_the_claimant_changed_the_claim(self):
        claim = self.pending()
        self.edit(claim, comment="Edited")
        staff = Client()
        staff.force_login(self.manager)
        page = staff.get(reverse("fleetops:srp_claim", kwargs={"claim_uuid": claim.uuid}))
        self.assertContains(page, "srp-member-edited")
        self.assertContains(page, 'name="shown_revision" value="1"')
        member = self.client.get(reverse("fleetops:srp_claim", kwargs={"claim_uuid": claim.uuid}))
        self.assertNotContains(member, "srp-member-edited")

    def test_a_stale_approve_form_is_refused_by_the_page(self):
        claim = self.pending()
        staff = Client()
        staff.force_login(self.manager)
        self.edit(claim, comment="Edited")
        response = staff.post(
            reverse("fleetops:srp_claim_action", kwargs={"claim_uuid": claim.uuid, "action": "approve"}),
            {"mode": "suggested", "shown_revision": "0", "shown_status": "pending", "shown_suggested": str(claim.suggested_amount)},
            follow=True,
        )
        self.assertContains(response, "claimant changed this claim")
        claim.refresh_from_db()
        self.assertEqual(claim.status, Status.PENDING)
