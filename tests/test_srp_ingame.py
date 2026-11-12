# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""0.3.0b2: the SRP payments page by claim, copyable claim numbers and the Open in game button."""

import re
from datetime import timedelta
from unittest import mock

from django.core.cache import cache
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from esi.models import Token

from fleetops.constants import OPEN_WINDOW_SCOPE
from fleetops.models import AuditLog, SrpClaim, SrpClaimEvent, SrpPayerSetting, SrpPaymentBatch, SrpSettings
from fleetops.providers.esi import FleetESIError
from fleetops import views_srp_ingame
from fleetops.srp import ingame
from fleetops.texts import ESI_NO_SCOPED_TOKEN

from . import factories as f
from .test_srp_payments_batches import PaymentsBase
from .test_srp_views import messages_of, route

Status = SrpClaim.Status
OPEN = "fleetops.views_srp_ingame.esi_provider.open_character_window"


class IngameBase(PaymentsBase):
    def setUp(self):
        super().setUp()
        self.payer_client = self.client_for(self.payer)
        cache.clear()

    def page(self, client=None, query=""):
        response = (client or self.payer_client).get(route("srp_payments") + query)
        self.assertEqual(response.status_code, 200)
        return response

    def authorize(self, user=None, scopes=None):
        """Choose the main character of ``user`` (the payer) as the in-game character, with a token."""
        user = user or self.payer
        character = f.main_of(user)
        f.add_token(user, character, scopes=[OPEN_WINDOW_SCOPE] if scopes is None else scopes)
        ingame.set_game_character(user, character.character_id, character.character_name)
        return character


class ViewSwitchTests(IngameBase):
    def test_a_payer_who_never_chose_sees_the_cards_by_player_and_nothing_is_stored(self):
        self.approved(self.member)

        html = self.page().content.decode()

        self.assertIn("data-srp-group", html)
        self.assertNotIn("data-srp-claim-row", html)
        self.assertFalse(SrpPayerSetting.objects.exists())

    def test_by_claim_shows_one_row_per_claim(self):
        first, second = self.approved(self.member), self.approved(self.member, 20_000_000)
        third = self.approved(self.other, 5_000_000)

        html = self.page(query="?view=claim").content.decode()

        self.assertEqual(html.count("data-srp-claim-row"), 3)
        for claim in (first, second, third):
            self.assertIn(f'data-srp-copy="SRP-{claim.pk}"', html)

    def test_a_get_never_writes_the_choice(self):
        self.approved(self.member)

        html = self.page(query="?view=claim").content.decode()

        # The parameter only draws that view for this request.
        self.assertIn("data-srp-claim-row", html)
        self.assertFalse(SrpPayerSetting.objects.exists())
        self.assertNotIn("data-srp-claim-row", self.page().content.decode())
        self.assertFalse(SrpPayerSetting.objects.exists())

    def test_the_choice_is_saved_by_a_post_and_remembered(self):
        self.approved(self.member)
        url = route("srp_payment_view")

        response = self.payer_client.post(url, {"view": "claim", "next": route("srp_payments") + "?category=1"})

        self.assertRedirects(response, route("srp_payments") + "?category=1", fetch_redirect_response=False)
        self.assertEqual(SrpPayerSetting.objects.get(user=self.payer).payment_view, "claim")
        self.assertIn("data-srp-claim-row", self.page().content.decode())
        self.payer_client.post(url, {"view": "player"})
        self.assertEqual(SrpPayerSetting.objects.get(user=self.payer).payment_view, "player")
        self.assertNotIn("data-srp-claim-row", self.page().content.decode())

    def test_the_switch_is_a_post_form_and_the_post_needs_csrf_and_a_known_view(self):
        self.approved(self.member)
        html = self.page().content.decode()
        self.assertIn(f'action="{route("srp_payment_view")}"', html)
        self.assertIn('name="view" value="claim"', html)
        self.assertEqual(self.payer_client.get(route("srp_payment_view")).status_code, 405)
        self.assertEqual(self.payer_client.post(route("srp_payment_view"), {"view": "sideways"}).status_code, 404)
        strict = Client(enforce_csrf_checks=True)
        strict.force_login(self.payer)
        self.assertEqual(strict.post(route("srp_payment_view"), {"view": "claim"}).status_code, 403)
        self.assertFalse(SrpPayerSetting.objects.exists())

    def test_the_choice_belongs_to_the_user_and_an_unknown_view_changes_nothing(self):
        self.approved(self.member)
        self.payer_client.post(route("srp_payment_view"), {"view": "claim"})

        self.assertNotIn("data-srp-claim-row", self.page(self.client_for(self.payer2)).content.decode())
        self.assertIn("data-srp-claim-row", self.page(query="?view=sideways").content.decode())
        self.assertEqual(SrpPayerSetting.objects.get(user=self.payer).payment_view, "claim")

    def test_a_row_names_the_player_the_pilot_the_claim_the_ship_and_the_amounts(self):
        pilot = f.create_character("Lost Alt")
        f.add_ownership(self.member, pilot)
        claim = self.approved(self.member, 12_345_678, character=pilot, loss_value=900_000_000)

        html = self.page(query="?view=claim").content.decode()

        row = html[html.index("data-srp-claim-row") :]
        self.assertIn(f.main_of(self.member).character_name, row)
        self.assertIn('data-srp-copy="Lost Alt"', row)
        self.assertIn(f'href="{route("srp_claim", claim.uuid)}">SRP-{claim.pk}</a>', row)
        self.assertIn("900,000,000 ISK", row)
        self.assertIn("12,345,678 ISK", row)
        self.assertIn('data-srp-copy="12345678"', row)
        self.assertIn(f'name="only" value="{claim.uuid}"', row)

    def test_the_claims_are_one_list_oldest_claim_first_not_grouped_by_player(self):
        now = timezone.now()
        big_early = self.approved(self.member, 500_000_000, submitted_at=now - timedelta(days=5))
        small_mid = self.approved(self.other, 30_000_000, submitted_at=now - timedelta(days=3))
        big_late = self.approved(self.member, 10_000_000, submitted_at=now - timedelta(days=1))

        html = self.page(query="?view=claim").content.decode()

        order = [html.index(f">SRP-{claim.pk}</a>") for claim in (big_early, small_mid, big_late)]
        self.assertEqual(order, sorted(order))

    def test_an_own_claim_has_no_checkbox_and_no_paid_button_by_claim(self):
        own = self.approved(self.payer)
        other = self.approved(self.member)

        html = self.page(query="?view=claim").content.decode()

        self.assertNotIn(f'name="only" value="{own.uuid}"', html)
        self.assertNotIn(f'value="{own.uuid}" data-srp-select', html)
        self.assertIn(f'name="only" value="{other.uuid}"', html)
        self.assertIn(f'value="{other.uuid}" data-srp-select', html)

    def test_filters_totals_and_exports_work_in_both_views(self):
        category = f.srp_category("Logistics")
        with_category = self.approved(self.member, category=category)
        without = self.approved(self.other)
        for view in ("player", "claim"):
            with self.subTest(view=view):
                html = self.page(query=f"?view={view}&category={category.pk}").content.decode()
                self.assertIn(f"SRP-{with_category.pk}", html)
                self.assertNotIn(f">SRP-{without.pk}<", html)
                self.assertIn("srp-payable-export", html)
                self.assertIn("1 claim for 1 player", html)

    def test_the_view_buttons_come_back_to_the_page_with_its_filters_but_without_the_view(self):
        self.approved(self.member)

        html = self.page(query="?link=ABC&view=claim").content.decode()

        self.assertIn(f'name="next" value="{route("srp_payments")}?link=ABC"', html)


class CopyTests(IngameBase):
    def test_a_player_card_copies_the_numbers_of_its_payable_claims_separated_by_spaces(self):
        first, second = self.approved(self.member), self.approved(self.member, 4_000_000)
        other = self.approved(self.other)

        html = self.page().content.decode()

        self.assertIn(f'data-srp-copy="SRP-{first.pk} SRP-{second.pk}"', html)
        self.assertIn(f'data-srp-copy="SRP-{other.pk}"', html)
        self.assertIn("Copy claim numbers", html)

    def test_a_card_leaves_out_the_claims_the_payer_may_not_pay(self):
        # The payer's own claims share a card with nobody else, so the claims of the card come from one player.
        SrpSettings.objects.filter(pk=1).update(require_different_payer=True)
        mine = f.srp_claim(self.member, status=Status.APPROVED, decided_by=self.payer)
        fine = self.approved(self.member, 3_000_000)

        html = self.page().content.decode()

        self.assertIn(f'data-srp-copy="SRP-{fine.pk}"', html)
        self.assertNotIn(f"SRP-{mine.pk} SRP-{fine.pk}", html)
        self.assertNotIn(f'data-srp-copy="SRP-{mine.pk} ', html)

    def test_a_card_without_payable_claims_has_no_copy_button_for_numbers(self):
        self.approved(self.payer)

        self.assertNotIn("Copy claim numbers", self.page().content.decode())

    def test_the_numbers_are_the_reference_shown_on_the_claim_pages(self):
        claim = self.approved(self.member)

        page = self.payer_client.get(route("srp_claim", claim.uuid)).content.decode()

        self.assertIn(f"SRP-{claim.pk}", page)


class PaidButtonTests(IngameBase):
    def pay_one(self, claim, extra=None, client=None):
        data = {"only": str(claim.uuid), f"shown-{claim.uuid}": str(claim.approved_amount), "next": route("srp_payments")}
        data.update(extra or {})
        return (client or self.payer_client).post(route("srp_payments"), data, follow=True)

    def test_paid_marks_just_that_claim_in_a_batch_of_one_with_audit_and_notification(self):
        claim, ticked = self.approved(self.member), self.approved(self.other, 7_000_000)

        with mock.patch("fleetops.srp.review.claims.notify_after_commit") as notify:
            response = self.pay_one(claim, {"claim": str(ticked.uuid), f"shown-{ticked.uuid}": str(ticked.approved_amount)})

        claim.refresh_from_db()
        ticked.refresh_from_db()
        self.assertEqual((claim.status, ticked.status), (Status.PAID, Status.APPROVED))
        batch = SrpPaymentBatch.objects.get()
        self.assertEqual((batch.claim_count, batch.payee_count, batch.total), (1, 1, claim.approved_amount))
        self.assertEqual(claim.payment_batch, batch)
        self.assertEqual(AuditLog.objects.filter(action="srp.payment_batch").count(), 1)
        self.assertTrue(SrpClaimEvent.objects.filter(claim=claim, action=SrpClaimEvent.Action.PAID).exists())
        notify.assert_called_once()
        self.assertEqual(notify.call_args.args[1], "paid")
        self.assertIn(f"Payment batch {batch.reference}.", messages_of(response))

    def test_the_note_of_the_page_is_kept_with_the_payment(self):
        claim = self.approved(self.member)

        self.pay_one(claim, {"note": "Round 3"})

        self.assertEqual(SrpPaymentBatch.objects.get().note, "Round 3")

    def test_the_payer_cannot_pay_their_own_claim_this_way(self):
        own = self.approved(self.payer)

        response = self.pay_one(own)

        own.refresh_from_db()
        self.assertEqual(own.status, Status.APPROVED)
        self.assertFalse(SrpPaymentBatch.objects.exists())
        self.assertTrue(any("your own claim" in text for text in messages_of(response)))

    def test_the_four_eyes_rule_refuses_the_approver_even_for_one_claim(self):
        SrpSettings.objects.filter(pk=1).update(require_different_payer=True)
        claim = f.srp_claim(self.member, status=Status.APPROVED, decided_by=self.payer)

        response = self.pay_one(claim)

        claim.refresh_from_db()
        self.assertEqual(claim.status, Status.APPROVED)
        self.assertTrue(any("you approved it" in text for text in messages_of(response)))

    def test_another_amount_than_the_one_shown_is_refused(self):
        claim = self.approved(self.member, 10_000_000)

        response = self.pay_one(claim, {f"shown-{claim.uuid}": "1"})

        claim.refresh_from_db()
        self.assertEqual(claim.status, Status.APPROVED)
        self.assertTrue(messages_of(response))

    def test_without_the_payer_role_nothing_is_paid(self):
        claim = self.approved(self.member)

        response = self.client_for(self.reviewer).post(
            route("srp_payments"), {"only": str(claim.uuid), f"shown-{claim.uuid}": str(claim.approved_amount)}
        )

        self.assertEqual(response.status_code, 403)
        claim.refresh_from_db()
        self.assertEqual(claim.status, Status.APPROVED)

    def test_the_bulk_button_still_pays_the_ticked_claims_in_the_by_claim_view(self):
        first, second = self.approved(self.member), self.approved(self.other)
        left = self.approved(self.third)

        self.page(query="?view=claim")
        self.payer_client.post(
            route("srp_payments"),
            {
                "claim": [str(first.uuid), str(second.uuid)],
                f"shown-{first.uuid}": str(first.approved_amount),
                f"shown-{second.uuid}": str(second.approved_amount),
            },
        )

        self.assertEqual(SrpPaymentBatch.objects.get().claim_count, 2)
        left.refresh_from_db()
        self.assertEqual(left.status, Status.APPROVED)

    def test_enter_in_the_form_answers_with_the_bulk_button_first(self):
        # The first button of the form takes the Enter key: it must be the (disabled until ticked) bulk button.
        self.approved(self.member)
        for view in ("claim", "player"):
            html = self.page(query=f"?view={view}").content.decode()
            form = html[html.index('id="fleetops-srp-pay-form"') :]
            first_button = re.search(r"<button[^>]*>", form).group(0)
            self.assertIn("data-srp-bulk-button", first_button)
            self.assertIn("disabled", first_button)


class OpenInGameTests(IngameBase):
    def open_url(self, claim, kind="lost"):
        return route("srp_open_in_game", claim.uuid, kind)

    def post(self, claim, kind="lost", client=None, **extra):
        return (client or self.payer_client).post(self.open_url(claim, kind), **extra)

    def test_the_lost_character_is_opened_from_the_in_game_character_without_audit(self):
        pilot = f.create_character("Lost Alt")
        f.add_ownership(self.member, pilot)
        claim = self.approved(self.member, character=pilot)
        character = self.authorize()

        with mock.patch(OPEN) as opened:
            response = self.post(claim, HTTP_X_REQUESTED_WITH="XMLHttpRequest")

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["ok"])
        self.assertIn(character.character_name, data["message"])
        self.assertIn("must be running and logged in", data["message"])
        opened.assert_called_once_with(self.payer, character.character_id, pilot.character_id)
        self.assertEqual(AuditLog.objects.count(), 0)
        self.assertEqual(SrpClaim.objects.get(pk=claim.pk).status, Status.APPROVED)

    def test_the_main_character_of_the_player_is_opened_for_main(self):
        pilot = f.create_character("Lost Alt")
        f.add_ownership(self.member, pilot)
        claim = self.approved(self.member, character=pilot)
        self.authorize()

        with mock.patch(OPEN) as opened:
            self.post(claim, "main", HTTP_X_REQUESTED_WITH="XMLHttpRequest")

        self.assertEqual(opened.call_args.args[2], f.main_of(self.member).character_id)

    def test_without_javascript_the_page_comes_back_with_a_message(self):
        claim = self.approved(self.member)
        self.authorize()

        with mock.patch(OPEN):
            response = self.post(claim, follow=True, data={"next": route("srp_payments")})

        self.assertRedirects(response, route("srp_payments"))
        self.assertTrue(any("Sent to the game client" in text for text in messages_of(response)))

    def test_only_a_post_with_a_csrf_token_is_accepted(self):
        claim = self.approved(self.member)
        self.authorize()
        strict = Client(enforce_csrf_checks=True)
        strict.force_login(self.payer)

        with mock.patch(OPEN) as opened:
            self.assertEqual(strict.post(self.open_url(claim)).status_code, 403)
            self.assertEqual(self.payer_client.get(self.open_url(claim)).status_code, 405)

        opened.assert_not_called()

    def test_the_pay_permission_is_needed(self):
        claim = self.approved(self.member)
        self.authorize()

        with mock.patch(OPEN) as opened:
            for user in (self.member, self.reviewer):
                with self.subTest(user=user.username):
                    self.assertEqual(self.post(claim, client=self.client_for(user)).status_code, 403)

        opened.assert_not_called()

    def test_a_manager_may_use_it(self):
        claim = self.approved(self.member)
        self.authorize(self.manager)

        with mock.patch(OPEN) as opened:
            response = self.post(claim, client=self.client_for(self.manager), HTTP_X_REQUESTED_WITH="XMLHttpRequest")

        self.assertEqual(response.status_code, 200)
        opened.assert_called_once()

    def test_only_claims_of_the_payments_page_can_be_opened(self):
        self.authorize()
        pending = f.srp_claim(self.member)
        old = f.srp_claim(
            self.member, status=Status.PAID, decided_by=self.manager, paid_by=self.payer, paid_at=timezone.now() - timedelta(days=40)
        )
        recent = f.srp_claim(
            self.member, status=Status.PAID, decided_by=self.manager, paid_by=self.payer, paid_at=timezone.now() - timedelta(days=2)
        )
        rejected = f.srp_claim(self.member, status=Status.REJECTED)

        with mock.patch(OPEN) as opened:
            codes = {name: self.post(claim, HTTP_X_REQUESTED_WITH="XMLHttpRequest").status_code for name, claim in (
                ("pending", pending), ("old", old), ("recent", recent), ("rejected", rejected))}

        self.assertEqual(codes, {"pending": 404, "old": 404, "recent": 200, "rejected": 404})
        self.assertEqual(opened.call_count, 1)

    def test_a_character_id_is_never_taken_from_the_request(self):
        claim = self.approved(self.member)
        self.authorize()

        with mock.patch(OPEN) as opened:
            self.post(claim, data={"target_id": "1", "character_id": "2", "next": "/x/"}, HTTP_X_REQUESTED_WITH="XMLHttpRequest")
            self.assertEqual(self.post(claim, "123456").status_code, 404)

        self.assertEqual(opened.call_args.args[2], claim.character_id)
        self.assertEqual(opened.call_count, 1)

    def test_without_an_in_game_character_the_answer_says_what_to_do(self):
        claim = self.approved(self.member)

        with mock.patch(OPEN) as opened:
            response = self.post(claim, HTTP_X_REQUESTED_WITH="XMLHttpRequest")

        self.assertEqual(response.status_code, 400)
        self.assertIn("Choose your in-game character first", response.json()["message"])
        opened.assert_not_called()

    def test_a_missing_or_expired_token_asks_to_add_the_character_again(self):
        claim = self.approved(self.member)
        character = self.authorize()

        error = FleetESIError("MISSING_SCOPE", ESI_NO_SCOPED_TOKEN())
        with mock.patch(OPEN, side_effect=error):
            response = self.post(claim, HTTP_X_REQUESTED_WITH="XMLHttpRequest")

        self.assertEqual(response.status_code, 409)
        self.assertIn(f"token of {character.character_name} is missing or has expired", response.json()["message"])

    def test_an_esi_error_is_a_short_message_without_details_of_the_request(self):
        claim = self.approved(self.member)
        self.authorize()
        cases = (("OPEN_WINDOW_ERROR", 502), ("OPEN_WINDOW_LIMITED", 429), ("OPEN_WINDOW_FORBIDDEN", 409))
        for code, status in cases:
            with self.subTest(code=code):
                error = FleetESIError(code, "Could not open the window in the game: ESI returned HTTP 500 (boom).", 500)
                with mock.patch(OPEN, side_effect=error):
                    response = self.post(claim, HTTP_X_REQUESTED_WITH="XMLHttpRequest")
                self.assertEqual(response.status_code, status)
                self.assertNotIn("Bearer", response.json()["message"])

    def test_a_click_is_logged_at_debug_level_without_a_token(self):
        claim = self.approved(self.member)
        self.authorize()
        token = f.main_of(self.payer).character_id

        with mock.patch(OPEN), self.assertLogs(views_srp_ingame.logger, "DEBUG") as logs:
            self.post(claim, HTTP_X_REQUESTED_WITH="XMLHttpRequest")

        text = "\n".join(logs.output)
        self.assertIn(f"claim {claim.pk}", text)
        self.assertNotIn("access-", text)
        self.assertNotIn("refresh-", text)
        self.assertIn(str(token), text)


class ButtonVisibilityTests(IngameBase):
    def test_a_payer_with_a_valid_token_sees_the_button_in_both_views(self):
        claim = self.approved(self.member)
        self.authorize()

        by_claim = self.page(query="?view=claim").content.decode()
        by_player = self.page(query="?view=player").content.decode()

        self.assertIn(f'formaction="{route("srp_open_in_game", claim.uuid, "lost")}"', by_claim)
        self.assertIn(f'formaction="{route("srp_open_in_game", claim.uuid, "main")}"', by_player)
        self.assertNotIn("Set up opening characters in game", by_claim)

    def test_a_payer_who_never_chose_a_character_sees_the_setup_link(self):
        self.approved(self.member)
        for view in ("claim", "player"):
            html = self.page(query=f"?view={view}").content.decode()
            self.assertNotIn("data-srp-open", html)
            self.assertIn('href="#srp-game-character">Set up opening characters in game', html)
            self.assertNotIn("Add the opening character again", html)

    def test_a_chosen_character_whose_token_lost_the_scope_must_be_added_again(self):
        # Changed in 0.3.0b3: this case used to show the generic setup link; the payer did choose a character, so the
        # rows say that it has to be added again (the link still goes to the box).
        self.approved(self.member)
        for prepare in (lambda: self.authorize(scopes=[]), lambda: self.authorize(scopes=["esi-location.read_location.v1"])):
            prepare()
            for view in ("claim", "player"):
                with self.subTest(view=view):
                    html = self.page(query=f"?view={view}").content.decode()
                    self.assertNotIn("data-srp-open", html)
                    self.assertIn('href="#srp-game-character">Add the opening character again', html)
                    self.assertNotIn("Set up opening characters in game", html)

    def test_the_setting_area_offers_authorized_characters_and_the_authorization_link(self):
        self.approved(self.member)
        self.authorize()

        html = self.page().content.decode()

        self.assertIn('id="srp-game-character"', html)
        self.assertIn(route("srp_game_character_authorize"), html)
        self.assertIn(f"{f.main_of(self.payer).character_name}</strong>", html)


class GameCharacterTests(IngameBase):
    def post(self, data, client=None):
        return (client or self.payer_client).post(route("srp_game_character"), data, follow=True)

    def test_a_payer_chooses_an_own_character_that_has_the_scope(self):
        character = f.main_of(self.payer)
        f.add_token(self.payer, character, scopes=[OPEN_WINDOW_SCOPE])

        response = self.post({"action": "set", "character_id": character.character_id, "next": route("srp_payments")})

        setting = SrpPayerSetting.objects.get(user=self.payer)
        self.assertEqual((setting.game_character_id, setting.game_character_name), (character.character_id, character.character_name))
        self.assertTrue(any("is your in-game character" in text for text in messages_of(response)))

    def test_a_character_without_the_scope_or_of_somebody_else_is_refused(self):
        mine = f.main_of(self.payer)
        f.add_token(self.payer, mine, scopes=["esi-fleets.read_fleet.v1"])
        theirs = f.main_of(self.member)
        f.add_token(self.member, theirs, scopes=[OPEN_WINDOW_SCOPE])

        for character_id in (mine.character_id, theirs.character_id, "abc", "", "99999999999999999999999"):
            with self.subTest(character_id=character_id):
                self.post({"action": "set", "character_id": character_id})
                self.assertFalse(SrpPayerSetting.objects.filter(user=self.payer).exclude(game_character_id=None).exists())

    def test_removing_the_choice_keeps_every_token(self):
        character = self.authorize()

        self.post({"action": "remove"})

        setting = SrpPayerSetting.objects.get(user=self.payer)
        self.assertEqual((setting.game_character_id, setting.game_character_name), (None, ""))
        self.assertTrue(Token.objects.filter(user=self.payer, character_id=character.character_id).exists())

    def test_removing_when_nothing_was_chosen_makes_no_row(self):
        self.post({"action": "remove"})

        self.assertFalse(SrpPayerSetting.objects.exists())

    def test_the_pay_permission_csrf_and_post_are_needed(self):
        character = f.main_of(self.member)
        f.add_token(self.member, character, scopes=[OPEN_WINDOW_SCOPE])
        strict = Client(enforce_csrf_checks=True)
        strict.force_login(self.payer)

        self.assertEqual(
            self.client_for(self.member).post(route("srp_game_character"), {"action": "set", "character_id": character.character_id}).status_code,
            403,
        )
        self.assertEqual(strict.post(route("srp_game_character"), {"action": "remove"}).status_code, 403)
        self.assertEqual(self.payer_client.get(route("srp_game_character")).status_code, 405)
        self.assertEqual(self.payer_client.post(route("srp_game_character"), {"action": "other"}).status_code, 404)

    def test_the_authorization_needs_the_pay_permission_and_starts_the_eve_login_for_the_scope(self):
        self.assertEqual(self.client_for(self.member).get(route("srp_game_character_authorize")).status_code, 403)

        response = self.payer_client.get(route("srp_game_character_authorize"))

        # The login of EVE is started (a redirect to the django-esi login), nothing is stored by looking.
        self.assertEqual(response.status_code, 302)
        self.assertIn("esi", response.url)
        self.assertFalse(SrpPayerSetting.objects.exists())

    def test_back_from_the_login_the_character_is_offered_but_not_chosen(self):
        character = f.main_of(self.payer)
        token = f.add_token(self.payer, character, scopes=[OPEN_WINDOW_SCOPE])
        request = mock.Mock(user=self.payer)

        with mock.patch("fleetops.views_srp_ingame.messages") as shown:
            response = views_srp_ingame.authorized(request, token)

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.url.endswith("#srp-game-character"))
        shown.success.assert_called_once()
        self.assertFalse(SrpPayerSetting.objects.exists())
        self.assertTrue(
            [row for row in ingame.authorized_characters(self.payer) if row["id"] == character.character_id][0]["authorized"]
        )

    def test_a_token_of_a_character_that_is_not_on_the_account_is_not_offered(self):
        stranger = f.main_of(self.member)
        token = f.add_token(self.member, stranger, scopes=[OPEN_WINDOW_SCOPE])

        with mock.patch("fleetops.views_srp_ingame.messages") as shown:
            views_srp_ingame.authorized(mock.Mock(user=self.payer), token)

        shown.error.assert_called_once()
        self.assertFalse(ingame.authorized_characters(self.payer)[0]["authorized"])

    def test_a_failed_login_is_a_message_and_the_error_is_logged_without_its_text(self):
        with mock.patch("fleetops.views_srp_ingame._authorized", side_effect=RuntimeError("secret-code-123")), \
                self.assertLogs(views_srp_ingame.logger, "ERROR") as logs:
            response = self.payer_client.get(route("srp_game_character_authorize"), follow=True)

        self.assertRedirects(response, route("srp_payments"))
        self.assertTrue(any("esi-ui.open_window.v1" in text for text in messages_of(response)))
        self.assertNotIn("secret-code-123", "\n".join(logs.output))

    def test_a_removed_user_takes_the_setting_along(self):
        self.authorize()

        self.payer.delete()

        self.assertFalse(SrpPayerSetting.objects.exists())

    def test_the_scope_is_never_part_of_the_login_or_the_other_scope_lists(self):
        from fleetops import constants

        others = constants.FLEET_SCOPES + constants.CLICK_SCOPES + constants.LOSS_SCOPES
        self.assertNotIn(OPEN_WINDOW_SCOPE, others)
        self.assertEqual(constants.OPEN_WINDOW_SCOPES, [OPEN_WINDOW_SCOPE])


class QueryBudgetTests(IngameBase):
    def count(self, query):
        self.page(query=query)
        cache.clear()
        with CaptureQueriesContext(connection) as queries:
            self.page(query=query)
        return len(queries)

    def test_the_by_claim_view_does_not_grow_with_the_claims(self):
        self.authorize()
        pilots = [f.create_user(f"ingame_pilot_{index}", perms=f.SRP_MEMBER_PERMS) for index in range(6)]
        for pilot in pilots[:2]:
            self.approved(pilot)
        small = self.count("?view=claim")
        for pilot in pilots[2:]:
            self.approved(pilot)
            self.approved(pilot, 3_000_000)

        self.assertEqual(self.count("?view=claim"), small)

    def test_the_by_claim_view_costs_about_the_same_as_the_cards(self):
        self.authorize()
        for index in range(4):
            self.approved(f.create_user(f"ingame_cost_{index}", perms=f.SRP_MEMBER_PERMS))

        self.assertLessEqual(self.count("?view=claim"), self.count("?view=player") + 1)
