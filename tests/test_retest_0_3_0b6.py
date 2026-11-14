# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Fixes from the re-test of 0.3.0b5: rejected claims and a closed link, the warning after an automatic rejection, the
ship order page that saves nothing, the button of a claim that cannot be submitted, the claims a ban rejected before
the marker existed, the payments page, the words of other plugins, small wordings and the audit text of a webhook."""

from datetime import datetime, timedelta
from datetime import timezone as dt_timezone
from decimal import Decimal
from unittest import mock

from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone, translation

from fleetops.models import FleetOperation, SrpBan, SrpClaim, SrpClaimEvent, SrpSettings
from fleetops.services import links as site_links
from fleetops.srp import bans, claims, display, review
from fleetops.srp.types import SrpError

from . import factories as f
from . import i18n_extract as extract
from .link_fakes import setUpModule, tearDownModule  # noqa: F401 - unittest runs them around the module (no ESI lookups)
from .test_fixes_0_3_0b5 import COLLIDING_WORDS, CONTEXT
from .test_srp_claims import ClaimBase
from .test_srp_payments_batches import PaymentsBase, route
from .test_srp_standing_intake import StandingBase

Status = SrpClaim.Status
Action = SrpClaimEvent.Action


# --- Q1: a rejected claim and a closed link ---------------------------------------------------------------


class RejectedClaimClosedLinkTests(StandingBase):
    def setUp(self):
        super().setUp()
        self.settings(member_resubmit_rejected=True)

    def rejected(self, link):
        claim = self.submit(self.killmail(), link=link)
        review.reject_claim(claim, actor=self.manager, reason="Wrong ship")
        claim.refresh_from_db()
        return claim

    def close(self, link):
        link.is_open = False
        link.save()

    def test_a_rejected_claim_of_a_fleet_link_cannot_be_submitted_again_once_the_link_is_closed(self):
        _operation, link = self.fleet()
        claim = self.rejected(link)
        self.close(link)

        with self.assertRaises(SrpError) as caught:
            claims.resubmit_claim(claim, actor=self.user)

        self.assertEqual((caught.exception.code, caught.exception.message), ("not_eligible", "This SRP link is closed."))
        claim.refresh_from_db()
        self.assertEqual(claim.status, Status.REJECTED)

    def test_a_rejected_claim_of_a_standing_link_cannot_be_submitted_again_once_the_link_is_closed(self):
        link = self.standing()
        claim = self.rejected(link)
        self.close(link)

        with self.assertRaises(SrpError) as caught:
            claims.resubmit_claim(claim, actor=self.user)

        self.assertEqual(caught.exception.code, "not_eligible")
        claim.refresh_from_db()
        self.assertEqual(claim.status, Status.REJECTED)

    def test_with_the_link_open_a_rejected_claim_still_goes_back_to_pending(self):
        for link in (self.fleet()[1], self.standing()):
            with self.subTest(kind=link.kind):
                claim = self.rejected(link)
                claims.resubmit_claim(claim, actor=self.user)
                claim.refresh_from_db()
                self.assertEqual(claim.status, Status.PENDING)
                SrpClaim.objects.filter(pk=claim.pk).delete()

    def test_a_withdrawn_claim_of_either_kind_of_link_is_not_stopped_by_a_closed_link(self):
        for link in (self.fleet()[1], self.standing()):
            with self.subTest(kind=link.kind):
                claim = self.submit(self.killmail(), link=link)
                claims.withdraw_claim(claim, actor=self.user)
                self.close(link)

                claims.resubmit_claim(claim, actor=self.user)

                claim.refresh_from_db()
                self.assertEqual((claim.status, claim.link), (Status.PENDING, link))
                SrpClaim.objects.filter(pk=claim.pk).delete()


# --- Q2: after an automatic rejection ---------------------------------------------------------------------


class AutomaticRejectionResubmittedTests(ClaimBase):
    REJECTED_AT = datetime(2026, 3, 9, 14, 5, 9, tzinfo=dt_timezone.utc)

    def auto_rejected(self):
        claim = self.submit(self.killmail())
        SrpClaim.objects.filter(pk=claim.pk).update(status=Status.REJECTED, auto_rejected=True)
        event = SrpClaimEvent.objects.create(
            claim=claim, actor=self.user, action=Action.AUTO_REJECT, to_status="rejected", reason="Over the limit"
        )
        SrpClaimEvent.objects.filter(pk=event.pk).update(created_at=self.REJECTED_AT)
        claim.refresh_from_db()
        self.settings(member_resubmit_rejected=True)
        return claim

    def test_the_warning_says_the_rules_rejected_it_with_their_reason_and_never_names_the_member(self):
        claim = self.auto_rejected()

        claims.resubmit_claim(claim, actor=self.user)

        claim.refresh_from_db()
        warning = next(item for item in claim.warnings if item["code"] == "resubmitted_after_rejection")
        self.assertEqual(warning["level"], "warning")
        self.assertTrue(warning["params"]["auto"])
        self.assertNotIn("reviewer", warning["params"])
        text = display.warning_text("resubmitted_after_rejection", warning["params"])
        day = self.REJECTED_AT.strftime("%Y-%m-%d")
        self.assertEqual(
            text, f"Submitted again after being rejected automatically by the rules on {day} 14:05 EVE: Over the limit"
        )
        self.assertNotIn(self.user.username, text)

    def test_batch_approval_skips_it(self):
        claim = self.auto_rejected()
        claims.resubmit_claim(claim, actor=self.user)
        claim.refresh_from_db()
        self.assertEqual(claim.status, Status.PENDING)

        result = review.approve_clean_claims([claim], actor=self.manager)

        self.assertEqual(result.done, [])
        self.assertEqual(len(result.skipped), 1)
        claim.refresh_from_db()
        self.assertEqual(claim.status, Status.PENDING)

    def test_without_a_reason_the_text_ends_at_the_time(self):
        text = display.warning_text(
            "resubmitted_after_rejection", {"auto": True, "reason": "", "rejected_at": "2026-11-14T09:03:00+00:00"}
        )
        self.assertEqual(text, "Submitted again after being rejected automatically by the rules on 2026-11-14 09:03 EVE.")

    def test_a_rejection_by_a_reviewer_still_names_the_reviewer(self):
        claim = self.submit(self.killmail())
        review.reject_claim(claim, actor=self.manager, reason="Wrong ship")
        self.settings(member_resubmit_rejected=True)

        claims.resubmit_claim(claim, actor=self.user)

        claim.refresh_from_db()
        warning = next(item for item in claim.warnings if item["code"] == "resubmitted_after_rejection")
        self.assertNotIn("auto", warning["params"])
        text = display.warning_text("resubmitted_after_rejection", warning["params"])
        self.assertRegex(text, r"^Submitted again after a rejection by .+ on \d{4}-\d\d-\d\d \d\d:\d\d EVE\. Reason: Wrong ship$")


# --- E7: the time in the warning --------------------------------------------------------------------------


class WithdrawnAfterRejectionTests(ClaimBase):
    def setUp(self):
        super().setUp()
        self.settings(member_resubmit_rejected=True)

    def go_through(self, claim):
        claims.resubmit_claim(claim, actor=self.user)
        claim.refresh_from_db()
        claims.withdraw_claim(claim, actor=self.user)
        claim.refresh_from_db()
        self.assertEqual(claim.status, Status.WITHDRAWN)
        claims.resubmit_claim(claim, actor=self.user)
        claim.refresh_from_db()
        self.assertEqual(claim.status, Status.PENDING)
        codes = [item["code"] for item in claim.warnings]
        self.assertEqual(codes.count("resubmitted_after_rejection"), 1)
        result = review.approve_clean_claims([claim], actor=self.manager)
        self.assertEqual(result.done, [])
        claim.refresh_from_db()
        self.assertEqual(claim.status, Status.PENDING)

    def test_the_warning_stays_after_reject_resubmit_withdraw_resubmit(self):
        claim = self.submit(self.killmail())
        review.reject_claim(claim, actor=self.manager, reason="Wrong ship")
        claim.refresh_from_db()
        self.go_through(claim)

    def test_the_warning_stays_after_an_automatic_rejection_too(self):
        claim = self.submit(self.killmail())
        SrpClaim.objects.filter(pk=claim.pk).update(status=Status.REJECTED, auto_rejected=True)
        SrpClaimEvent.objects.create(
            claim=claim, actor=self.user, action=Action.AUTO_REJECT, to_status="rejected", reason="Over the limit"
        )
        claim.refresh_from_db()
        self.go_through(claim)


class WarningTimeTests(SimpleTestCase):
    def test_the_time_is_shown_in_eve_time_to_the_minute(self):
        text = display.warning_text(
            "resubmitted_after_rejection",
            {"reviewer": "Reviewer", "reason": "", "rejected_at": "2026-11-14T10:20:59+00:00"},
        )
        self.assertEqual(text, "Submitted again after a rejection by Reviewer on 2026-11-14 10:20 EVE.")

    def test_a_time_with_another_offset_is_converted_to_eve_time(self):
        text = display.warning_text(
            "resubmitted_after_rejection",
            {"reviewer": "Reviewer", "rejected_at": "2026-11-14T10:20:59+08:00"},
        )
        self.assertIn("on 2026-11-14 02:20 EVE", text)

    def test_a_stored_warning_without_a_time_reads_on_a_question_mark(self):
        text = display.warning_text("resubmitted_after_rejection", {"reviewer": "Reviewer"})
        self.assertEqual(text, "Submitted again after a rejection by Reviewer on ?.")


# --- E2 and E4: the button, and the claims a ban rejected before the marker existed -----------------------


class ResubmitButtonTests(ClaimBase):
    def page(self, claim, user=None):
        self.client.force_login(user or self.user)
        return self.client.get(reverse("fleetops:srp_claim", args=[claim.uuid]))

    def rejected(self, **kwargs):
        claim = self.submit(self.killmail(**kwargs))
        review.reject_claim(claim, actor=self.manager, reason="Wrong ship")
        claim.refresh_from_db()
        return claim

    def withdrawn(self, **kwargs):
        claim = self.submit(self.killmail(**kwargs))
        claims.withdraw_claim(claim, actor=self.user)
        claim.refresh_from_db()
        return claim

    def assert_button(self, claim, shown):
        page = self.page(claim)
        url = reverse("fleetops:srp_claim_resubmit", args=[claim.uuid])
        self.assertEqual(page.context["can_resubmit"], shown)
        if shown:
            self.assertContains(page, url)
            self.assertNotContains(page, "srp-claim-resubmit-blocked")
        else:
            self.assertNotContains(page, url)
        return page

    def test_the_button_shows_when_the_claim_can_be_submitted_again(self):
        self.settings(member_resubmit_rejected=True)
        self.assert_button(self.withdrawn(), True)
        self.assert_button(self.rejected(), True)

    def test_no_button_for_a_banned_member_and_the_reason_is_shown(self):
        self.settings(member_resubmit_rejected=True)
        withdrawn, rejected = self.withdrawn(), self.rejected()
        bans.ban_user(self.user, actor=self.manager, days=5, reason="Abuse")

        for claim in (withdrawn, rejected):
            with self.subTest(status=claim.status):
                page = self.assert_button(claim, False)
                self.assertContains(page, "srp-claim-resubmit-blocked")
                self.assertContains(page, "This claim cannot be submitted again:")

    def test_no_button_while_srp_is_switched_off(self):
        self.settings(member_resubmit_rejected=True)
        withdrawn, rejected = self.withdrawn(), self.rejected()
        self.settings(enabled=False)

        self.assert_button(withdrawn, False)
        self.assert_button(rejected, False)

    def test_no_button_after_the_deadline(self):
        self.settings(member_resubmit_rejected=True)
        withdrawn = self.withdrawn(time=timezone.now() - timedelta(days=10))
        rejected = self.rejected(time=timezone.now() - timedelta(days=10))
        self.settings(claim_deadline_days=5)

        self.assert_button(withdrawn, False)
        self.assert_button(rejected, False)

    def test_a_rejected_claim_of_a_closed_link_has_no_button_but_a_withdrawn_one_has(self):
        self.settings(member_resubmit_rejected=True)
        _operation, link = self.fleet()
        rejected = self.submit(self.killmail(), link=link)
        review.reject_claim(rejected, actor=self.manager, reason="Wrong ship")
        withdrawn = self.submit(self.killmail(), link=link)
        claims.withdraw_claim(withdrawn, actor=self.user)
        link.is_open = False
        link.save()

        self.assert_button(rejected, False)
        self.assert_button(withdrawn, True)

    def test_no_button_for_a_claim_a_ban_rejected_and_the_page_shows_no_reason_card_text_for_it(self):
        self.settings(member_resubmit_rejected=True)
        pending = self.submit(self.killmail())
        bans.ban_user(self.user, actor=self.manager, days=1, reason="Abuse", reject_pending=True, reject_reason="Banned")
        SrpBan.objects.update(expires_at=timezone.now() - timedelta(hours=1))
        pending.refresh_from_db()

        self.assert_button(pending, False)


    def test_no_resubmit_or_withdraw_button_without_request_srp(self):
        self.settings(member_resubmit_rejected=True)
        pending = self.submit(self.killmail())
        withdrawn = self.withdrawn()
        rejected = self.rejected()
        self.user.user_permissions.remove(*self.user.user_permissions.filter(codename="request_srp"))
        self.user = type(self.user).objects.get(pk=self.user.pk)

        for claim in (withdrawn, rejected):
            self.assert_button(claim, False)
        page = self.page(pending)
        self.assertFalse(page.context["can_withdraw"])
        self.assertNotContains(page, reverse("fleetops:srp_claim_withdraw", args=[pending.uuid]))


class OldBanRejectionTests(ClaimBase):
    """Claims a ban rejected in 0.3.0b4 or earlier have no ``ban`` marker; the ban and the time find them."""

    def setUp(self):
        super().setUp()
        self.settings(member_resubmit_rejected=True)
        self.other_reviewer = f.create_user("b6_other_reviewer", perms=f.SRP_REVIEWER_PERMS)

    def banned_and_rejected(self, *, actor=None, minutes=0.0, lift=False):
        """A pending claim, a ban of its member, then a rejection ``minutes`` after the ban without the marker."""
        claim = self.submit(self.killmail())
        ban, _result = bans.ban_user(self.user, actor=self.manager, days=7, reason="Abuse")
        review.reject_claim(claim, actor=actor or self.manager, reason="Banned")
        event = claim.events.get(action=Action.REJECT)
        self.assertFalse((event.data or {}).get("ban"))
        SrpClaimEvent.objects.filter(pk=event.pk).update(created_at=ban.created_at + timedelta(minutes=minutes))
        if lift:
            bans.lift_ban(ban, actor=self.manager, reason="Over")
        claim.refresh_from_db()
        return claim

    def test_a_rejection_just_after_the_ban_by_the_same_reviewer_counts_as_a_ban_rejection(self):
        claim = self.banned_and_rejected(minutes=1)

        self.assertTrue(claims.rejected_by_ban(claim))
        self.assertFalse(claims.can_resubmit(claim, SrpSettings.get_solo()))

    def test_also_after_the_ban_was_lifted_and_the_member_cannot_resubmit(self):
        claim = self.banned_and_rejected(minutes=1, lift=True)

        with self.assertRaises(SrpError) as caught:
            claims.resubmit_claim(claim, actor=self.user)

        self.assertEqual(caught.exception.code, "rejected_by_ban")
        claim.refresh_from_db()
        self.assertEqual(claim.status, Status.REJECTED)
        # A reviewer can still reopen it.
        review.reopen_claim(claim, actor=self.manager, reason="Mistake")
        claim.refresh_from_db()
        self.assertEqual(claim.status, Status.PENDING)

    def test_inside_and_outside_the_window(self):
        window = claims.BAN_REJECTION_WINDOW.total_seconds() / 60
        self.assertGreaterEqual(window, 1)
        inside = self.banned_and_rejected(minutes=window - 0.5, lift=True)
        self.assertTrue(claims.rejected_by_ban(inside))

        outside = self.banned_and_rejected(minutes=window + 1, lift=True)

        self.assertFalse(claims.rejected_by_ban(outside))
        self.assertTrue(claims.can_resubmit(outside, SrpSettings.get_solo()))

    def test_a_rejection_before_the_ban_does_not_count(self):
        claim = self.banned_and_rejected(minutes=-2, lift=True)

        self.assertFalse(claims.rejected_by_ban(claim))

    def test_a_manual_rejection_by_another_reviewer_does_not_count(self):
        claim = self.banned_and_rejected(minutes=1, actor=self.other_reviewer, lift=True)

        self.assertFalse(claims.rejected_by_ban(claim))
        claims.resubmit_claim(claim, actor=self.user)
        claim.refresh_from_db()
        self.assertEqual(claim.status, Status.PENDING)

    def test_a_ban_of_another_member_does_not_count(self):
        claim = self.submit(self.killmail())
        ban, _result = bans.ban_user(self.other, actor=self.manager, days=7, reason="Abuse")
        review.reject_claim(claim, actor=self.manager, reason="Wrong ship")
        event = claim.events.get(action=Action.REJECT)
        SrpClaimEvent.objects.filter(pk=event.pk).update(created_at=ban.created_at + timedelta(minutes=1))
        claim.refresh_from_db()

        self.assertFalse(claims.rejected_by_ban(claim))

    def test_an_automatic_rejection_is_never_a_ban_rejection(self):
        claim = self.submit(self.killmail())
        bans.ban_user(self.user, actor=self.manager, days=7, reason="Abuse")
        SrpClaim.objects.filter(pk=claim.pk).update(status=Status.REJECTED, auto_rejected=True)
        SrpClaimEvent.objects.create(claim=claim, actor=self.manager, action=Action.AUTO_REJECT, reason="Over the limit")
        claim.refresh_from_db()

        self.assertFalse(claims.rejected_by_ban(claim))


# --- E3: the payments page --------------------------------------------------------------------------------


class PaymentsCountTests(PaymentsBase):
    def setUp(self):
        super().setUp()
        SrpSettings.objects.filter(pk=1).update(require_different_payer=True)
        self.client = self.client_for(self.payer2)

    def test_a_card_the_viewer_may_not_pay_shows_no_big_amount(self):
        f.srp_claim(self.member, status=Status.APPROVED, amount=10_000_000, decided_by=self.payer2)

        page = self.client.get(route("srp_payments"))

        row = page.context["pay_rows"][0]
        self.assertEqual(row.payable_total, 0)
        # Only the "To pay" summary at the top and the selection bar use the class; the card has none.
        self.assertNotContains(page, '<span class="fo-pay-total">')
        self.assertContains(page, "plus 10,000,000 ISK you may not pay")

    def test_a_card_the_viewer_may_pay_shows_the_amount(self):
        f.srp_claim(self.member, status=Status.APPROVED, amount=25_000_000, decided_by=self.manager)

        page = self.client.get(route("srp_payments"))

        self.assertContains(page, '<span class="fo-pay-total">25,000,000 ISK</span>')

    def test_the_top_line_counts_what_the_amount_is_made_of(self):
        f.srp_claim(self.member, status=Status.APPROVED, amount=10_000_000, decided_by=self.payer2)
        f.srp_claim(self.member, status=Status.APPROVED, amount=25_000_000, decided_by=self.manager)
        other = f.create_user("b6_other_member", perms=f.SRP_MEMBER_PERMS)
        f.main_of(other)
        f.srp_claim(other, status=Status.APPROVED, amount=5_000_000, decided_by=self.payer2)

        page = self.client.get(route("srp_payments"))

        self.assertEqual(page.context["to_pay_total"], 25_000_000)
        self.assertEqual(
            (page.context["to_pay_payable_count"], page.context["to_pay_payable_players"], page.context["to_pay_unpayable_count"]),
            (1, 1, 2),
        )
        self.assertContains(page, "1 claim for 1 player (2 claims you may not pay)")

    def test_without_claims_the_viewer_may_not_pay_the_line_has_no_addition(self):
        f.srp_claim(self.member, status=Status.APPROVED, amount=25_000_000, decided_by=self.manager)

        page = self.client.get(route("srp_payments"))

        self.assertContains(page, "1 claim for 1 player")
        self.assertNotContains(page, "claims you may not pay")
        self.assertNotContains(page, "claim you may not pay")


# --- E5: the words of other plugins -----------------------------------------------------------------------

# Words of the zh_Hans catalogs of other common Alliance Auth plugins (aasrp, afat, fleetpings, structures, the
# fleet finder, the intel tool and others) that read differently from FleetOps' own. Built from the lists of the
# re-tests of 0.3.0b4 and 0.3.0b5. A FleetOps text with one of these as its whole message and no context shows the
# other plugin's wording on a site where that plugin is installed first. Add a word when a site shows one.
FOREIGN_MSGIDS = frozenset(
    {
        # The re-test of 0.3.0b4.
        "Request SRP", "Registered", "SRP link", "SRP links", "SRP", "First", "Last", "Total", "Overview", "Created",
        "Created at", "Expires at", "Group name", "FC name", "Ping target", "Inactive", "Target", "Main characters",
        "Notes", "Owner", "Ship type", "Ship types", "Ship classes", "Eligible", "Error", "error", "Webhook",
        "Webhook URL", "Clear filters", "Create SRP link", "EVE time",
        # The re-test of 0.3.0b5 and the catalog of aasrp.
        "Previous", "Next", "Search", "Killmail", "Approve", "Actions", "Additional information", "FC",
        "%(counter)s member",
    }
)


class ForeignWordsTests(SimpleTestCase):
    def test_every_colliding_word_of_the_list_is_in_the_bundled_list(self):
        self.assertEqual(sorted(set(COLLIDING_WORDS) - FOREIGN_MSGIDS), [])

    def test_no_message_without_context_is_a_word_other_plugins_translate_differently(self):
        found = {}
        for path in extract.python_files():
            found.update(extract.python_messages(path))
        for path in extract.template_files():
            found.update(extract.template_messages(path))

        plain = sorted(word for word in FOREIGN_MSGIDS if word in found)

        self.assertEqual(plain, [], "give these messages the context 'FleetOps' (pgettext or context in the template)")

    def test_the_plural_member_text_has_the_context_in_the_catalog(self):
        entries = extract.po.read_po(extract.po.LOCALE_DIR / "zh_Hans" / "LC_MESSAGES" / "django.po")
        keys = {entry.key for entry in entries if not entry.is_header and not entry.obsolete}
        self.assertIn(f"{CONTEXT}{extract.po.CONTEXT_SEPARATOR}%(counter)s member", keys)
        self.assertNotIn("%(counter)s member", keys)

    def test_the_words_show_the_fleetops_translation_even_when_another_catalog_wins(self):
        with translation.override("zh-hans"):
            catalog = translation.trans_real.translation("zh-hans")._catalog
            fake = {word: f"OTHER {word}" for word in FOREIGN_MSGIDS}
            catalog._catalogs.insert(0, fake)
            try:
                shown = [
                    translation.pgettext(CONTEXT, word)
                    for word in sorted(set(COLLIDING_WORDS) - {"%(counter)s member"})
                ]
            finally:
                catalog._catalogs.remove(fake)

        self.assertFalse([text for text in shown if text.startswith("OTHER ")])
        self.assertTrue(all(shown))

    def test_the_stored_word_error_has_the_context_too(self):
        from fleetops import texts

        with translation.override("zh-hans"):
            self.assertEqual(texts.translate_stored(texts.FLEET_STATUS_WORDS["error"]()), "出错")
            catalog = translation.trans_real.translation("zh-hans")._catalog
            fake = {"error": "OTHER"}
            catalog._catalogs.insert(0, fake)
            try:
                self.assertEqual(texts.translate_stored(texts.FLEET_STATUS_WORDS["error"]()), "出错")
            finally:
                catalog._catalogs.remove(fake)
        with translation.override("en"):
            self.assertEqual(texts.translate_stored(texts.FLEET_STATUS_WORDS["error"]()), "error")


# --- E9: the button to the SRP fleet ----------------------------------------------------------------------


class OpenSrpFleetInternalUrlTests(TestCase):
    def setUp(self):
        self.fc = f.create_user("b6_fc_plain", perms=f.FC_PERMS)
        self.fc_srp = f.create_user("b6_fc_srp", perms=[*f.FC_PERMS, "fleetops.request_srp"])
        f.srp_settings(enabled=True)

    def page(self, user, url, provider=""):
        operation = f.create_operation(
            user,
            status=FleetOperation.Status.CLOSED,
            started_at=timezone.now() - timedelta(hours=3),
            srp_url=url,
            srp_reference="SRP-1",
            srp_provider=provider,
        )
        self.client.force_login(user)
        return self.client.get(reverse("fleetops:operation_detail", args=[operation.uuid]))

    @property
    def claim_path(self):
        return reverse("fleetops:srp_link", args=["abc123"])

    def test_an_internal_claim_url_without_a_provider_is_treated_like_a_fleetops_link(self):
        page = self.page(self.fc, self.claim_path)

        self.assertEqual(page.status_code, 200)
        self.assertNotContains(page, "Open SRP Fleet")

    def test_the_same_with_the_site_address_in_front(self):
        with self.settings(SITE_URL="https://auth.example.org"):
            page = self.page(self.fc, f"https://auth.example.org{self.claim_path}")

        self.assertNotContains(page, "Open SRP Fleet")

    def test_a_member_who_may_request_keeps_the_button(self):
        operation = f.create_operation(
            self.fc_srp, status=FleetOperation.Status.CLOSED, started_at=timezone.now() - timedelta(hours=3),
            srp_url=self.claim_path, srp_reference="SRP-1", srp_provider="",
        )
        f.fleet_link(operation)
        self.client.force_login(self.fc_srp)

        page = self.client.get(reverse("fleetops:operation_detail", args=[operation.uuid]))

        self.assertContains(page, "Open SRP Fleet")

    def test_external_addresses_are_not_affected(self):
        for url in ("https://srp.example.org/fleet/7/", "/ship-replacement/abc/", f"https://other.example.net{self.claim_path}"):
            with self.subTest(url=url):
                page = self.page(self.fc, url)
                self.assertContains(page, "Open SRP Fleet")

    def test_the_check_itself(self):
        path = self.claim_path
        self.assertTrue(site_links.is_internal_srp_link(path))
        self.assertFalse(site_links.is_internal_srp_link(""))
        self.assertFalse(site_links.is_internal_srp_link("/fleetops/"))
        self.assertFalse(site_links.is_internal_srp_link("javascript:alert(1)"))
        self.assertFalse(site_links.is_internal_srp_link(f"https://evil.example.com{path}"))


# --- The audit text of a replaced webhook -----------------------------------------------------------------


class WebhookReplacedTextTests(TestCase):
    def setUp(self):
        self.auditor = f.create_user("b6_auditor", perms=f.MEMBER_PERMS + ["fleetops.view_audit_log"])
        self.client.force_login(self.auditor)

    def test_the_page_says_the_address_was_replaced_without_the_key_or_a_secret(self):
        from fleetops.services.audit import audit

        audit(
            self.auditor, "configuration.update", f.fleet_type("B6 type"),
            {"webhook_url_replaced": False, "name": "A"}, {"webhook_url_replaced": True, "name": "B"},
        )

        page = self.client.get(reverse("fleetops:audit_log"))

        self.assertContains(page, "Webhook address replaced")
        self.assertNotContains(page, "webhook_url_replaced")

    def test_the_text_is_translated(self):
        from fleetops.services.audit import audit
        from fleetops.templatetags.fleetops_config import audit_changes

        row = audit(self.auditor, "x.test", f.fleet_type("B6 type 2"), {"webhook_url_replaced": False}, {"webhook_url_replaced": True})

        with translation.override("zh-hans"):
            self.assertEqual(audit_changes(row), [("Webhook 地址已替换", "", "")])
