# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Loss reading authorization (INT-1): the card, the switch for all characters, the way back from the EVE login.

ESI and EVE login are never reached: the login redirect is replaced and tokens are made by the factories.
"""

from datetime import timedelta
from unittest import mock

from allianceauth.authentication.models import CharacterOwnership
from django.contrib.messages import get_messages
from django.core.cache import cache
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone
from esi.models import CallbackRedirect

from fleetops.constants import KILLMAIL_READ_SCOPE, LOSS_SCOPES
from fleetops.models import SrpClaim, SrpLossScan, SrpSettings
from fleetops.srp import claims, discovery
from fleetops.srp import policy as rules
from fleetops.srp.types import SrpError

from . import factories as f
from .test_srp_claims import ClaimBase


def route(name, *args):
    return reverse(f"fleetops:{name}", args=args)


class AuthBase(ClaimBase):
    """A member with a main and two alts, the main and one alt authorized."""

    def setUp(self):
        super().setUp()
        self.alt_a = f.add_alt(self.user, "Alt Alpha")
        self.alt_b = f.add_alt(self.user, "Alt Bravo")
        f.add_token(self.user, self.main, scopes=[KILLMAIL_READ_SCOPE])
        f.add_token(self.user, self.alt_a, scopes=[KILLMAIL_READ_SCOPE])
        self.settings(enabled=True, loss_discovery_enabled=True, require_all_alts_authorized=False)

    def login(self, user=None):
        client = Client(raise_request_exception=True)
        client.force_login(user or self.user)
        return client


class UnauthorizedCharactersTests(AuthBase):
    def names(self, user=None):
        return [name for _character_id, name in discovery.unauthorized_characters(user or self.user)]

    def test_nothing_is_missing_while_the_switch_is_off(self):
        self.assertEqual(self.names(), [])

    def test_the_character_without_a_token_is_listed_when_the_switch_is_on(self):
        self.settings(require_all_alts_authorized=True)

        self.assertEqual(discovery.unauthorized_characters(self.user), [(self.alt_b.character_id, "Alt Bravo")])

    def test_the_switch_needs_the_discovery_and_srp_to_be_on(self):
        self.settings(require_all_alts_authorized=True, loss_discovery_enabled=False)
        self.assertEqual(self.names(), [])
        self.settings(require_all_alts_authorized=True, loss_discovery_enabled=True, enabled=False)
        self.assertEqual(self.names(), [])

    def test_a_token_of_somebody_else_does_not_count(self):
        self.settings(require_all_alts_authorized=True)
        f.add_token(self.other, self.alt_b, scopes=[KILLMAIL_READ_SCOPE])
        # Alliance Auth hands a character to whoever holds a new token for it; the state under test is that the
        # character stays with this member while a token of somebody else exists.
        CharacterOwnership.objects.filter(character=self.alt_b).update(user=self.user)

        self.assertEqual(self.names(), ["Alt Bravo"])

    def test_a_token_without_the_scope_does_not_count(self):
        self.settings(require_all_alts_authorized=True)
        f.add_token(self.user, self.alt_b, scopes=["esi-location.read_online.v1"])

        self.assertEqual(self.names(), ["Alt Bravo"])

    def test_it_is_one_query_without_a_token_refresh(self):
        self.settings(require_all_alts_authorized=True)
        srp = SrpSettings.get_solo()
        with mock.patch("esi.managers.TokenQueryset.require_valid") as valid:
            with self.assertNumQueries(1):
                discovery.unauthorized_characters(self.user, srp)
        valid.assert_not_called()


class CardTests(AuthBase):
    def test_the_card_lists_every_character_with_its_state(self):
        SrpLossScan.objects.create(
            character_id=self.main.character_id, status="ok", last_checked_at=timezone.now(), user=self.user
        )

        cards = {card["name"]: card for card in discovery.character_cards(self.user)}

        self.assertEqual(len(cards), 3)
        self.assertTrue(cards[self.main.character_name]["is_main"])
        self.assertFalse(cards["Alt Alpha"]["is_main"])
        self.assertEqual(
            [cards[name]["authorized"] for name in (self.main.character_name, "Alt Alpha", "Alt Bravo")], [True, True, False]
        )
        self.assertEqual(cards[self.main.character_name]["status"], "ok")
        self.assertIsNotNone(cards[self.main.character_name]["last_checked_at"])

    def test_a_refusal_noted_before_the_character_was_authorized_again_is_not_shown(self):
        for character, status in ((self.main, "no_token"), (self.alt_a, "denied")):
            SrpLossScan.objects.create(character_id=character.character_id, status=status, user=self.user)

        cards = {card["name"]: card for card in discovery.character_cards(self.user)}

        for name in (self.main.character_name, "Alt Alpha"):
            self.assertTrue(cards[name]["authorized"])
            self.assertEqual((cards[name]["status"], cards[name]["status_label"]), ("", ""))

    def test_the_page_shows_three_characters_and_an_authorize_button_for_the_one_without_a_token(self):
        response = self.login().get(route("srp_home"))

        self.assertContains(response, 'id="srp-loss-reading"')
        for name in (self.main.character_name, "Alt Alpha", "Alt Bravo"):
            self.assertContains(response, name)
        self.assertContains(response, "Not authorized", count=1)
        self.assertContains(response, route("srp_losses_authorize"))

    def test_the_card_costs_a_fixed_number_of_queries_and_never_refreshes_a_token(self):
        for index in range(4):
            f.add_alt(self.user, f"Extra {index}")
        with mock.patch("esi.managers.TokenQueryset.require_valid") as valid:
            with mock.patch("esi.managers.TokenQueryset.bulk_refresh") as refresh:
                self.login().get(route("srp_home"))
        valid.assert_not_called()
        refresh.assert_not_called()

    def test_the_card_is_not_shown_while_the_discovery_is_off(self):
        self.settings(loss_discovery_enabled=False)

        response = self.login().get(route("srp_home"))

        self.assertNotContains(response, 'id="srp-loss-reading"')
        self.assertNotContains(response, 'id="srp-recent-losses"')


class RequireAllTests(AuthBase):
    """The switch for all characters holds for the three entrances: pasted link, standing link, found loss."""

    def setUp(self):
        super().setUp()
        self.settings(require_all_alts_authorized=True, percentage=80)
        self.km = self.killmail()

    def test_the_preview_is_blocked_and_names_the_character(self):
        preview = self.prepare(self.km)

        self.assertEqual(preview.token, "")
        self.assertEqual(preview.blockers, ["Authorize loss reading for all your characters first: Alt Bravo."])

    def test_the_ban_message_comes_first(self):
        from fleetops.srp import bans

        bans.ban_user(self.user, actor=self.manager, reason="No", days=3)

        blockers = self.prepare(self.km).blockers

        self.assertEqual(len(blockers), 2)
        self.assertTrue(blockers[1].startswith("Authorize loss reading"))

    def test_a_token_that_was_made_before_the_authorization_is_refused_at_the_submit(self):
        self.settings(require_all_alts_authorized=False)
        preview = self.prepare(self.km)
        self.assertTrue(preview.token)
        self.settings(require_all_alts_authorized=True)

        with self.assertRaises(SrpError) as caught:
            claims.submit_claim(self.user, preview.token)

        self.assertIn("Alt Bravo", str(caught.exception))
        self.assertFalse(SrpClaim.objects.exists())

    def test_the_standing_link_entrance_is_blocked_too(self):
        link = f.standing_link()

        preview = self.prepare(self.km, link=link)

        self.assertTrue(any("Authorize loss reading" in blocker for blocker in preview.blockers))

    def test_a_found_loss_is_blocked_too(self):
        from fleetops.srp.types import KillmailRef

        with mock.patch.object(claims.killmails, "fetch_killmail", return_value=self.km):
            preview = claims.prepare_claim_for_ref(
                self.user, KillmailRef(self.km.killmail_id, self.km.killmail_hash, "id_hash"), source="discovered"
            )

        self.assertTrue(any("Alt Bravo" in blocker for blocker in preview.blockers))

    def test_submitting_again_is_blocked_but_withdrawing_is_not(self):
        self.settings(require_all_alts_authorized=False)
        claim = self.submit(self.km)
        self.settings(require_all_alts_authorized=True)

        claims.withdraw_claim(claim, actor=self.user)
        with self.assertRaises(SrpError) as caught:
            claims.resubmit_claim(claim, actor=self.user)

        self.assertIn("Authorize loss reading", str(caught.exception))
        claim.refresh_from_db()
        self.assertEqual(claim.status, SrpClaim.Status.WITHDRAWN)

    def test_the_authorization_lifts_the_block(self):
        f.add_token(self.user, self.alt_b, scopes=[KILLMAIL_READ_SCOPE])

        claim = self.submit(self.km)

        self.assertEqual(claim.status, SrpClaim.Status.PENDING)

    def test_nothing_is_blocked_once_the_discovery_is_off(self):
        # The rollback drill: with the discovery off members paste their links again.
        self.settings(loss_discovery_enabled=False)

        claim = self.submit(self.km)

        self.assertEqual(claim.status, SrpClaim.Status.PENDING)

    def test_a_reviewers_decisions_are_not_checked(self):
        # Reopening and the other decisions do not pass the list: eligibility without it blocks nothing.
        self.settings(require_all_alts_authorized=False)
        claim = self.submit(self.km)
        self.settings(require_all_alts_authorized=True)

        blockers = rules.eligibility(
            km=claim.killmail, user=self.user, scope="out_of_fleet", link=None, settings_obj=self.settings()
        )

        self.assertFalse(any("Authorize loss reading" in blocker for blocker in blockers))

    def test_the_banner_lists_the_character_and_the_authorize_button(self):
        response = self.login().get(route("srp_home"))

        self.assertContains(response, 'id="srp-alts-banner"')
        self.assertContains(response, "Alt Bravo")
        self.assertNotContains(response, "Alt Alpha</li>")

    def test_the_banner_is_gone_when_everything_is_authorized(self):
        f.add_token(self.user, self.alt_b, scopes=[KILLMAIL_READ_SCOPE])

        self.assertNotContains(self.login().get(route("srp_home")), 'id="srp-alts-banner"')

    def test_the_preview_costs_one_more_query_than_without_the_switch(self):
        self.settings(require_all_alts_authorized=False)
        with mock.patch.object(claims.killmails, "fetch_killmail", return_value=self.km):
            claims.prepare_claim(self.user, self.text(self.km))
            cache.delete(f"fleetops:srp:check:{self.user.pk}")
            from django.db import connection
            from django.test.utils import CaptureQueriesContext

            with CaptureQueriesContext(connection) as without:
                claims.prepare_claim(self.user, self.text(self.km))
            self.settings(require_all_alts_authorized=True)
            cache.delete(f"fleetops:srp:check:{self.user.pk}")
            with CaptureQueriesContext(connection) as with_switch:
                claims.prepare_claim(self.user, self.text(self.km))

        self.assertLessEqual(len(with_switch) - len(without), 1)


class AuthorizeViewTests(AuthBase):
    """The way out to EVE login and back (the login itself is replaced)."""

    def back_from_login(self, client, token, url=None):
        """Leave a callback record for the browser as django-esi does when EVE sends the member back."""
        store = client.session
        store.save()
        CallbackRedirect.objects.filter(session_key=store.session_key).delete()
        CallbackRedirect.objects.create(
            session_key=store.session_key, state="state", url=url or route("srp_losses_authorize"), token=token
        )

    def messages(self, response):
        return [str(message) for message in get_messages(response.wsgi_request)]

    def test_the_entry_starts_the_login_with_the_loss_scope(self):
        client = self.login()
        with mock.patch("esi.views.sso_redirect") as redirect:
            from django.http import HttpResponseRedirect

            redirect.return_value = HttpResponseRedirect("https://login.example.invalid/")
            response = client.get(route("srp_losses_authorize"))

        self.assertEqual(response.status_code, 302)
        self.assertEqual(redirect.call_args.kwargs["scopes"], LOSS_SCOPES)

    def test_back_from_the_login_the_character_is_due_now(self):
        client = self.login()
        token = f.add_token(self.user, self.alt_b, scopes=[KILLMAIL_READ_SCOPE])
        SrpLossScan.objects.create(
            character_id=self.alt_b.character_id, user=self.user, next_check_at=timezone.now() + timedelta(hours=20)
        )
        self.back_from_login(client, token)

        response = client.get(route("srp_losses_authorize"))

        self.assertRedirects(response, route("srp_home"), fetch_redirect_response=False)
        scan = SrpLossScan.objects.get(character_id=self.alt_b.character_id)
        self.assertLessEqual(scan.next_check_at, timezone.now())
        self.assertIn("Loss reading is authorized. Your recent losses will appear shortly.", self.messages(response))

    def test_a_character_of_somebody_else_is_refused_and_not_read(self):
        # Alliance Auth moves a character to whoever logs in as it (decision 29), so this state cannot come from a
        # real login; it is built directly: a token of this member for a character that is owned by another member.
        client = self.login()
        foreign = f.main_of(self.other)
        token = f.add_token(self.user, foreign, scopes=[KILLMAIL_READ_SCOPE])
        CharacterOwnership.objects.filter(character=foreign).update(user=self.other)
        later = timezone.now() + timedelta(hours=20)
        SrpLossScan.objects.create(character_id=foreign.character_id, user=self.other, next_check_at=later)
        self.back_from_login(client, token)

        response = client.get(route("srp_losses_authorize"))

        self.assertIn("That character is not on your account. It will not be read.", self.messages(response))
        self.assertEqual(SrpLossScan.objects.get(character_id=foreign.character_id).next_check_at, later)
        self.assertNotIn(foreign.character_id, discovery.candidate_characters(self.user.pk))
        self.assertNotIn(foreign.character_id, discovery.candidate_characters())

    def test_the_entry_is_closed_while_the_discovery_is_off(self):
        self.settings(loss_discovery_enabled=False)

        response = self.login().get(route("srp_losses_authorize"))

        self.assertRedirects(response, route("srp_home"), fetch_redirect_response=False)
        self.assertIn("Finding losses automatically is not enabled.", self.messages(response))

    def test_a_site_without_the_scope_gets_a_hint_and_the_log_names_only_the_class(self):
        client = self.login()
        secret = "tok-SECRET-1234567890"
        with mock.patch("esi.views.sso_redirect", side_effect=ValueError(f"invalid_scope access_token={secret}")):
            with self.assertLogs(level="DEBUG") as logs:
                response = client.get(route("srp_losses_authorize"))

        self.assertRedirects(response, route("srp_home"), fetch_redirect_response=False)
        self.assertTrue(any("esi-killmails.read_killmails.v1" in text for text in self.messages(response)))
        text = "\n".join(logs.output)
        self.assertIn("ValueError", text)
        self.assertNotIn(secret, text)
        self.assertNotIn("access_token", text)

    def test_the_entry_needs_the_request_permission(self):
        member = f.create_user("noreq", perms=f.MEMBER_PERMS)

        self.assertEqual(self.login(member).get(route("srp_losses_authorize")).status_code, 403)

    def test_the_entry_needs_a_login(self):
        response = Client().get(route("srp_losses_authorize"))

        self.assertEqual(response.status_code, 302)
        self.assertIn("login", response["Location"])


class ScopedTokenTests(AuthBase):
    def test_a_token_of_the_user_for_the_character_is_returned(self):
        from fleetops.providers import esi

        token = esi.get_scoped_token(self.user, self.main.character_id, LOSS_SCOPES)

        self.assertEqual(token.character_id, self.main.character_id)

    def test_a_token_of_somebody_else_is_no_token(self):
        from fleetops.providers import esi
        from fleetops.providers.esi import FleetESIError

        with self.assertRaises(FleetESIError) as caught:
            esi.get_scoped_token(self.other, self.main.character_id, LOSS_SCOPES)

        self.assertEqual(caught.exception.code, "MISSING_SCOPE")

    def test_a_failed_refresh_logs_the_class_only(self):
        from fleetops.providers import esi
        from fleetops.providers.esi import FleetESIError

        secret = "refresh-SECRET-abcdef"
        with mock.patch("esi.managers.TokenQueryset.require_valid", side_effect=ConnectionError(f"token {secret}")):
            with self.assertLogs(level="DEBUG") as logs:
                with self.assertRaises(FleetESIError) as caught:
                    esi.get_scoped_token(self.user, self.main.character_id, LOSS_SCOPES)

        self.assertEqual(caught.exception.code, "TOKEN_REFRESH_FAILED")
        self.assertNotIn(secret, "\n".join(logs.output) + str(caught.exception))
        self.assertIn("ConnectionError", "\n".join(logs.output) + str(caught.exception))
