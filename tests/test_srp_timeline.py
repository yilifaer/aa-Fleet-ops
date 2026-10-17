# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""The look of the SRP pages: the steps of a claim, the history, the loss cards of My SRP, pills and plain text."""

import re
from datetime import timedelta

from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from fleetops.models import SrpClaim
from fleetops.templatetags.fleetops_srp import srp_steps
from fleetops.views_srp import CLAIMS_PER_PAGE

from . import factories as f
from .test_srp_views import SrpPagesTestCase, route

Status = SrpClaim.Status


def states(claim):
    return [(step["key"], step["state"]) for step in srp_steps(claim)]


class StepStateTests(TestCase):
    """Which steps a claim shows and what each one is: done, the next thing to happen, bad or still to come."""

    @classmethod
    def setUpTestData(cls):
        f.srp_settings()
        cls.pilot = f.create_user("steps-pilot", perms=f.SRP_MEMBER_PERMS)
        cls.manager = f.create_user("steps-manager", perms=f.SRP_MANAGER_PERMS)

    def test_a_pending_claim_waits_for_a_decision(self):
        claim = f.srp_claim(self.pilot)

        self.assertEqual(
            states(claim),
            [("submitted", "done"), ("checked", "done"), ("decided", "current"), ("paid", "todo")],
        )
        self.assertEqual([step["label"] for step in srp_steps(claim)][2], "Decision")

    def test_an_approved_claim_waits_for_its_payment(self):
        claim = f.srp_claim(self.pilot, status=Status.APPROVED, decided_by=self.manager)

        self.assertEqual(
            states(claim),
            [("submitted", "done"), ("checked", "done"), ("decided", "done"), ("paid", "current")],
        )
        self.assertEqual(srp_steps(claim)[2]["label"], "Approved")
        self.assertIsNotNone(srp_steps(claim)[2]["time"])

    def test_a_paid_claim_has_every_step_done(self):
        claim = f.srp_claim(self.pilot, status=Status.PAID, decided_by=self.manager)

        self.assertEqual(
            states(claim),
            [("submitted", "done"), ("checked", "done"), ("decided", "done"), ("paid", "done")],
        )
        self.assertEqual(srp_steps(claim)[3]["label"], "Paid")
        self.assertEqual(srp_steps(claim)[3]["time"], claim.paid_at)

    def test_a_rejected_claim_ends_in_a_red_step_without_a_payment_step(self):
        claim = f.srp_claim(self.pilot, status=Status.REJECTED, decided_by=self.manager)

        self.assertEqual(states(claim), [("submitted", "done"), ("checked", "done"), ("decided", "bad")])
        self.assertEqual(srp_steps(claim)[2]["label"], "Rejected")
        self.assertEqual(srp_steps(claim)[2]["time"], claim.decided_at)

    def test_a_withdrawn_claim_has_no_payment_step_and_keeps_the_time_of_the_withdrawal(self):
        claim = f.srp_claim(self.pilot, status=Status.WITHDRAWN)

        self.assertEqual(states(claim), [("submitted", "done"), ("checked", "done"), ("decided", "done")])
        self.assertEqual(srp_steps(claim)[2]["label"], "Withdrawn")
        self.assertEqual(srp_steps(claim)[2]["time"], claim.updated_at)

    def test_the_check_time_is_the_fleet_check_or_the_calculation(self):
        checked = timezone.now() - timedelta(hours=2)
        claim = f.srp_claim(self.pilot, fleet_checked_at=checked)

        self.assertEqual(srp_steps(claim)[1]["time"], checked)

    def test_a_claim_without_any_stored_time_still_shows_the_check_as_done(self):
        claim = f.srp_claim(self.pilot, calculation={}, fleet_checked_at=None)

        self.assertEqual(srp_steps(claim)[1]["state"], "done")
        self.assertIsNone(srp_steps(claim)[1]["time"])


class ClaimPageStepsTests(SrpPagesTestCase):
    def page(self, claim, user=None):
        return (self.client_for(user) if user else self.client).get(route("srp_claim", claim.uuid))

    def test_the_steps_sit_under_the_header_and_above_the_history(self):
        claim = f.srp_claim(self.member, status=Status.APPROVED, decided_by=self.manager, time=self.loss_time)

        html = self.page(claim).content.decode()

        self.assertLess(html.index("</h2>"), html.index('id="srp-claim-steps"'))
        self.assertLess(html.index('id="srp-claim-steps"'), html.index("<strong>History</strong>"))
        steps = html.split('id="srp-claim-steps"', 1)[1].split("</ol>", 1)[0]
        self.assertEqual(re.findall(r'data-step="(\w+)"', steps), ["submitted", "checked", "decided", "paid"])
        self.assertEqual(steps.count("is-done"), 3)
        # The next thing that has to happen is marked for assistive technology.
        self.assertEqual(steps.count('aria-current="step"'), 1)
        self.assertRegex(steps, r'class="fo-step is-current" data-step="paid" aria-current="step"')

    def test_a_rejected_claim_shows_its_red_step_and_no_payment(self):
        claim = f.srp_claim(self.member, status=Status.REJECTED, decided_by=self.manager, time=self.loss_time)

        steps = self.page(claim).content.decode().split('id="srp-claim-steps"', 1)[1].split("</ol>", 1)[0]

        self.assertIn("is-bad", steps)
        self.assertNotIn('data-step="paid"', steps)
        self.assertNotIn("aria-current", steps)

    def test_the_times_use_the_eve_format(self):
        claim = f.srp_claim(self.member, time=self.loss_time)

        steps = self.page(claim).content.decode().split('id="srp-claim-steps"', 1)[1].split("</ol>", 1)[0]

        self.assertRegex(steps, r'<time class="fo-time" datetime="[\d-]+T[\d:]+Z">\d{4}-\d{2}-\d{2} \d{2}:\d{2} EVE</time>')

    def test_every_viewer_who_may_open_the_claim_sees_the_steps(self):
        claim = f.srp_claim(self.member, status=Status.PAID, decided_by=self.manager, time=self.loss_time)
        for user in (self.member, self.manager, self.stats):
            with self.subTest(user=user.username):
                self.assertContains(self.page(claim, user), 'id="srp-claim-steps"')

    def test_the_history_is_a_timeline_with_the_same_entries(self):
        claim = f.srp_claim(self.member, time=self.loss_time)
        f.claim_event(claim, "submit", actor=self.member)
        f.claim_event(claim, "approve", actor=self.manager, reason="Looks right", to_status="approved", amount=5_000_000)

        html = self.page(claim, self.manager).content.decode()
        history = html.split("<strong>History</strong>", 1)[1]

        self.assertIn('<ul class="fo-timeline fleetops-srp-timeline mb-0">', history)
        self.assertNotIn("list-group", history)
        self.assertEqual(history.count("<li>"), 2)
        self.assertIn("Looks right", history)
        self.assertIn("5,000,000 ISK", history)

    def test_an_internal_note_is_a_neutral_pill_for_managers_only(self):
        claim = f.srp_claim(self.member, time=self.loss_time)
        f.claim_event(claim, "note", actor=self.manager, reason="Check the alt", internal=True)

        manager = self.page(claim, self.manager).content.decode()
        owner = self.page(claim).content.decode()

        self.assertIn('<span class="fo-pill fo-pill-neutral">Internal note</span>', manager)
        self.assertNotIn("Check the alt", owner)

    def test_an_empty_history_says_so(self):
        claim = f.srp_claim(self.member, time=self.loss_time)

        self.assertContains(self.page(claim), "Nothing has happened to this claim yet.")

    def test_the_ship_render_has_the_frame_class_and_no_old_class(self):
        claim = f.srp_claim(self.member, time=self.loss_time)

        html = self.page(claim).content.decode()

        self.assertRegex(html, r'<img class="fo-render" src="https://images\.evetech\.net/types/\d+/render\?size=64"')
        self.assertNotIn("fleetops-srp-ship", html)

    def test_an_automatic_rejection_is_marked_with_a_neutral_pill(self):
        claim = f.srp_claim(self.member, status=Status.REJECTED, auto_rejected=True, time=self.loss_time)

        self.assertContains(self.page(claim), '<span class="fo-pill fo-pill-neutral">Automatic</span>')


class MyClaimsCardsTests(SrpPagesTestCase):
    """My SRP: a table from 768 px up, loss cards on a phone, and the empty state with its action."""

    def setUp(self):
        super().setUp()
        self.patch_home_services()

    def home(self, **query):
        return self.client.get(route("srp_home"), query)

    def test_losses_are_cards_on_a_phone_and_a_table_from_768_px(self):
        paid = f.srp_claim(
            self.member, status=Status.PAID, decided_by=self.manager, amount=7_000_000, time=self.loss_time
        )
        rejected = f.srp_claim(self.member, status=Status.REJECTED, decided_by=self.manager, time=self.loss_time)
        pending = f.srp_claim(self.member, amount=3_000_000, time=self.loss_time)

        html = self.home().content.decode()

        cards = html.split('id="srp-loss-cards"', 1)[1].split('<div class="table-responsive', 1)[0]
        self.assertEqual(cards.count('class="fo-loss-card"'), 3)
        for claim in (paid, rejected, pending):
            self.assertIn(route("srp_claim", claim.uuid), cards)
            self.assertIn(claim.reference, cards)
        # The card shows the figure that counts: what was approved, nothing for a rejected claim, else what fits.
        self.assertIn("7,000,000 ISK", cards)
        self.assertIn("3,000,000 ISK", cards)
        self.assertIn("&ndash;", cards)
        self.assertIn('<div class="fo-loss-grid d-md-none', html)
        self.assertIn('<div class="table-responsive d-none d-md-block">', html)

    def test_a_card_has_the_render_the_ship_the_status_the_time_and_the_fleet(self):
        claim = f.srp_claim(self.member, link=self.link, time=self.loss_time)

        card = self.home().content.decode().split('id="srp-loss-cards"', 1)[1].split("</div>\n</div>", 1)[0]

        self.assertIn('class="fo-render"', card)
        self.assertIn("images.evetech.net/types/", card)
        self.assertIn(claim.ship_type_name or str(claim.ship_type_id), card)
        self.assertIn('class="fo-pill fo-pill-warn">Pending</span>', card)
        self.assertIn(claim.fleet_label, card)
        self.assertIn("stretched-link", card)

    def test_the_claims_of_a_fleet_link_page_are_cards_too(self):
        f.srp_claim(self.member, link=self.link, time=self.loss_time)

        response = self.client.get(route("srp_link", self.link.code))

        self.assertContains(response, 'id="srp-loss-cards"')

    def test_no_claims_is_an_empty_state_with_the_way_to_the_form(self):
        html = self.home().content.decode()

        self.assertNotIn('id="srp-loss-cards"', html)
        self.assertNotIn("fleetops-srp-claims", html)
        self.assertIn("You have no SRP claims yet.", html)
        self.assertRegex(html, r'class="btn btn-outline-secondary" href="#srp-claim-form">Request SRP</a>')
        self.assertIn('id="srp-claim-form"', html)

    def test_the_empty_state_has_no_action_while_the_form_is_off(self):
        self.settings_row.out_of_fleet_enabled = False
        self.settings_row.save()

        html = self.home().content.decode()

        self.assertIn("You have no SRP claims yet.", html)
        self.assertNotIn('href="#srp-claim-form"', html)

    def test_the_page_count_is_plain_text_and_not_a_disabled_button(self):
        for index in range(CLAIMS_PER_PAGE + 1):
            f.srp_claim(self.member, time=self.loss_time - timedelta(minutes=index))

        html = self.home().content.decode()

        self.assertRegex(html, r'<span class="small text-muted">Page 1 of 2</span>')
        self.assertNotIn("btn btn-outline-secondary disabled", html)
        self.assertIn('aria-label="Pages"', html)

    def test_the_status_of_every_claim_is_a_pill_with_its_words(self):
        for status in (Status.PENDING, Status.REJECTED, Status.WITHDRAWN):
            f.srp_claim(self.member, status=status, decided_by=self.manager, time=self.loss_time)
        f.srp_claim(self.member, status=Status.APPROVED, decided_by=self.manager, time=self.loss_time)

        html = self.home().content.decode()

        for tone, word in (("warn", "Pending"), ("bad", "Rejected"), ("neutral", "Withdrawn"), ("info", "Approved")):
            self.assertIn(f'<span class="fo-pill fo-pill-{tone}">{word}</span>', html)
        # Alliance Auth's own navbar has a "text-bg-primary"; nothing in the FleetOps part is a Bootstrap badge.
        self.assertNotIn('class="badge', html)

    def test_the_ships_in_the_table_have_a_framed_render(self):
        f.srp_claim(self.member, time=self.loss_time)

        table = self.home().content.decode().split('<table class="table', 1)[1]

        self.assertIn('<span class="fleetops-srp-ship-cell"><img class="fo-render"', table)


class ReviewAndPaymentsLookTests(SrpPagesTestCase):
    def setUp(self):
        super().setUp()
        self.client = self.client_for(self.manager)

    def test_the_review_queue_has_a_sticky_bulk_bar_with_one_solid_button(self):
        f.srp_claim(self.member, time=self.loss_time)

        html = self.client.get(route("srp_review")).content.decode()

        bar = html.split('id="srp-bulk-bar"', 1)[1].split("</div>", 1)[0]
        self.assertIn('class="fo-actionbar"', html)
        self.assertEqual(bar.count("btn-primary"), 1)
        self.assertNotIn("btn-success", html)
        self.assertNotIn("card-footer", html.split('id="srp-bulk-bar"', 1)[0].rsplit("<form", 1)[-1])

    def test_the_row_selectors_are_the_touch_target_of_their_cell(self):
        f.srp_claim(self.member, time=self.loss_time)

        html = self.client.get(route("srp_review")).content.decode()

        self.assertIn('<label class="fo-check-cell"><input type="checkbox" class="form-check-input" data-srp-select-all', html)
        self.assertIn('<label class="fo-check-cell"><input type="checkbox" class="form-check-input" name="claim"', html)

    def test_an_empty_queue_says_nobody_waits(self):
        html = self.client.get(route("srp_review")).content.decode()

        self.assertIn("No claims wait for review.", html)
        self.assertIn('class="fo-empty"', html)

    def test_an_empty_filtered_queue_offers_to_reset_the_filters(self):
        f.srp_claim(self.member, time=self.loss_time)

        html = self.client.get(route("srp_review"), {"q": "nobody-has-this-name"}).content.decode()

        self.assertIn("No claims match these filters.", html)
        self.assertRegex(html, r'href="/fleetops/srp/review/">Reset</a>')

    def test_nothing_to_pay_is_an_empty_state(self):
        html = self.client.get(route("srp_payments")).content.decode()

        self.assertIn("Nothing waits for payment.", html)
        self.assertIn('class="fo-empty"', html)
        self.assertNotIn('class="alert alert-secondary"', html.split("Nothing waits for payment.", 1)[0][-300:])

    def test_the_pay_bar_has_one_solid_button_and_the_groups_a_touch_checkbox(self):
        f.srp_claim(self.member, status=Status.APPROVED, decided_by=self.manager2, time=self.loss_time)

        html = self.client.get(route("srp_payments")).content.decode()

        bar = html.split("fleetops-srp-paybar", 1)[1].split("</form>", 1)[0]
        self.assertEqual(bar.count("btn-primary"), 1)
        self.assertNotIn("btn-success", html)
        self.assertIn('<label class="fo-check-cell"><input type="checkbox" class="form-check-input mt-0" data-srp-select-group', html)

    def test_the_summary_tables_use_pills_for_the_link_state(self):
        f.srp_claim(self.member, link=self.link, time=self.loss_time)

        html = self.client.get(route("srp_summary_fc", self.fc.pk)).content.decode()

        self.assertIn('<span class="fo-pill fo-pill-ok">Open</span>', html)
        self.assertNotIn('class="badge', html)

    def test_the_decision_buttons_follow_the_hierarchy(self):
        claim = f.srp_claim(self.member, amount=30_000_000, suggested_amount=30_000_000, limited_amount=10_000_000,
                            over_limit=True, time=self.loss_time)

        html = self.client.get(route("srp_claim", claim.uuid)).content.decode()

        self.assertRegex(html, r'value="suggested" class="btn btn-primary">')
        self.assertRegex(html, r'value="within_limit" class="btn btn-outline-primary">')
        self.assertRegex(html, r'value="amount" class="btn btn-outline-primary">Approve this amount')
        self.assertNotIn("btn-warning", html)
        self.assertNotIn("btn-success", html)
        self.assertNotIn("btn-danger", html)


class StaticScriptTests(SimpleTestCase):
    """srp.js is a plain script: the loading state is built with the DOM API and uses the catalog."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from pathlib import Path

        import fleetops

        cls.script = (Path(fleetops.__file__).parent / "static" / "fleetops" / "srp.js").read_text(encoding="utf-8")

    def test_the_loss_check_shows_a_busy_button_and_a_placeholder_card(self):
        # The words are written by the page in the viewer's language, so the script needs no catalog entry for them.
        self.assertIn("button.dataset.srpChecking", self.script)
        self.assertNotIn("gettext('Checking", self.script)
        self.assertIn("fo-loss-card", self.script)
        self.assertIn("fo-skeleton", self.script)
        self.assertIn("[data-srp-check-button]", self.script)
        self.assertIn("[data-srp-result]", self.script)

    def test_nothing_is_written_with_inner_html(self):
        self.assertNotIn("innerHTML", self.script)
        self.assertNotIn("insertAdjacentHTML", self.script)

    def test_questions_are_asked_by_the_shared_dialog_and_not_by_the_browser(self):
        self.assertNotIn("window.confirm", self.script)
        self.assertNotIn("srpConfirm", self.script)

    def test_the_empty_state_action_focuses_the_claim_form(self):
        self.assertIn('a[href="#srp-claim-form"]', self.script)
        self.assertIn("field.focus()", self.script)

    def test_copying_goes_through_the_shared_clipboard_helper(self):
        self.assertIn("window.FleetOps.copyText", self.script)
