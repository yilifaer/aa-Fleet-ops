# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""My recent losses (INT-4), claiming a found loss (INT-5), the refresh and the paste entrance (INT-6).

ESI is never reached: killmails are stored by the factories, found losses are rows made here, the universe is replaced.
"""

from datetime import timedelta
from decimal import Decimal
from unittest import mock

from allianceauth.authentication.models import CharacterOwnership
from django.contrib.messages import get_messages
from django.core import signing
from django.core.cache import cache
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from fleetops import tasks as fleet_tasks
from fleetops.constants import KILLMAIL_READ_SCOPE
from fleetops.models import AuditLog, FleetMemberEvent, SrpClaim, SrpClaimEvent, SrpLoss
from fleetops.srp import claims, discovery

from . import factories as f
from .test_srp_claims import ClaimBase

Kind = SrpLoss.Kind
MINING_BARGE_GROUP = 463
COVETOR = 17476


def route(name, *args):
    return reverse(f"fleetops:{name}", args=args)


class ViewBase(ClaimBase):
    def setUp(self):
        super().setUp()
        self.alt = f.add_alt(self.user, "Views Alt")
        for character in (self.main, self.alt):
            f.add_token(self.user, character, scopes=[KILLMAIL_READ_SCOPE])
        self.settings(
            enabled=True,
            loss_discovery_enabled=True,
            require_all_alts_authorized=False,
            out_of_fleet_category_required=False,
            percentage=Decimal("80"),
        )
        self.client = self.client_for(self.user)

    def client_for(self, user):
        client = Client(raise_request_exception=True)
        client.force_login(user)
        return client

    def messages(self, response):
        return [str(message) for message in get_messages(response.wsgi_request)]

    def loss_of(self, character, *, minutes_ago=30, **km_extra):
        """A stored killmail and the found loss that points to it (as the scan would store both... the scan stores
        only the loss; the killmail is there so that the check finds it without ESI)."""
        km_extra.setdefault("fitted", "100000000")
        km = f.srp_killmail(character, time=timezone.now() - timedelta(minutes=minutes_ago), **km_extra)
        loss = SrpLoss.objects.create(
            character_id=character.character_id,
            killmail_id=km.killmail_id,
            killmail_hash=km.killmail_hash,
            kind=Kind.LOSS,
            killmail_time=km.killmail_time,
            ship_type_id=km.ship_type_id,
            ship_type_name=km.ship_type_name,
            solar_system_id=km.solar_system_id,
            solar_system_name=km.solar_system_name,
        )
        return km, loss

    def home(self, client=None, **params):
        return (client or self.client).get(route("srp_home"), params)


class RecentLossesTests(ViewBase):
    def test_the_list_shows_the_ship_the_character_and_the_main_marker(self):
        _km, _loss = self.loss_of(self.main, ship_name="Rifter")
        self.loss_of(self.alt, ship_name="Moa", ship_type_id=623)

        response = self.home()

        self.assertContains(response, 'id="srp-recent-losses"')
        self.assertContains(response, "Rifter")
        self.assertContains(response, "Moa")
        self.assertContains(response, "Views Alt")
        self.assertContains(response, "main", count=None)

    def test_somebody_elses_losses_are_not_listed_and_their_ids_are_a_404(self):
        _km, theirs = self.loss_of(f.main_of(self.other), ship_name="Hidden Hull")

        self.assertNotContains(self.home(), "Hidden Hull")
        for name in ("srp_loss_ignore", "srp_loss_unignore", "srp_loss_check"):
            with self.subTest(name=name):
                self.assertEqual(self.client.post(route(name, theirs.killmail_id), {"target": ""}).status_code, 404)
        theirs.refresh_from_db()
        self.assertIsNone(theirs.ignored_by)

    def test_kills_and_old_rows_are_not_listed(self):
        SrpLoss.objects.create(
            character_id=self.main.character_id, killmail_id=900, kind=Kind.KILL, killmail_time=timezone.now(), ship_type_name="Kill Hull"
        )
        SrpLoss.objects.create(
            character_id=self.main.character_id, killmail_id=901, kind=Kind.OLD, killmail_time=timezone.now(), ship_type_name="Old Hull"
        )

        response = self.home()

        self.assertNotContains(response, "Kill Hull")
        self.assertNotContains(response, "Old Hull")

    def test_a_claimed_loss_is_hidden_by_default_and_shows_its_number_with_the_toggle(self):
        km, _loss = self.loss_of(self.main, ship_name="Claimed Hull")
        claim = f.srp_claim(self.user, killmail=km)

        self.assertEqual(discovery.recent_losses(self.user, self.settings()), [])
        self.assertNotContains(self.home(), 'id="srp-recent-losses-table"><thead>Claimed')
        shown = self.home(claimed="1")
        self.assertEqual(len(shown.context["recent_losses"]), 1)
        self.assertContains(shown, f'href="{route("srp_claim", claim.uuid)}">SRP-{claim.pk}</a>')

    def test_a_loss_claimed_by_somebody_else_says_only_that_it_is_claimed(self):
        km, _loss = self.loss_of(self.main, ship_name="Taken Hull")
        stolen = f.srp_claim(self.other, killmail=km)

        self.assertEqual(discovery.recent_losses(self.user, self.settings()), [])
        response = self.home(claimed="1")

        self.assertContains(response, "Already claimed")
        self.assertNotContains(response, str(stolen.uuid))
        self.assertNotContains(response, stolen.reference)
        self.assertNotContains(response, f.main_of(self.other).character_name)

    def test_an_imported_claim_marks_the_loss_as_claimed_too(self):
        km, _loss = self.loss_of(self.main, ship_name="Imported Hull")
        f.srp_claim(self.other, killmail=km, source="aasrp")

        self.assertEqual(discovery.recent_losses(self.user, self.settings()), [])
        self.assertContains(self.home(claimed="1"), "Already claimed")

    def test_an_imported_placeholder_shows_a_dash_for_the_ship(self):
        km = f.srp_killmail(self.main, time=timezone.now() - timedelta(days=1), origin="aasrp", solar_system_id=0, killmail_hash="")
        SrpLoss.objects.create(
            character_id=self.main.character_id, killmail_id=km.killmail_id, kind=Kind.LOSS, killmail_time=km.killmail_time
        )
        f.srp_claim(self.other, killmail=km, source="aasrp")

        response = self.home(claimed="1")

        self.assertContains(response, "Already claimed")
        self.assertNotContains(response, ">None<")

    def test_ignoring_hides_a_loss_and_restoring_shows_it_again_with_an_audit_entry_each_time(self):
        _km, loss = self.loss_of(self.main, ship_name="Ignored Hull")

        response = self.client.post(route("srp_loss_ignore", loss.killmail_id))

        self.assertRedirects(response, route("srp_home"), fetch_redirect_response=False)
        loss.refresh_from_db()
        self.assertEqual(loss.ignored_by, self.user)
        self.assertNotContains(self.home(), "Ignored Hull")
        self.assertContains(self.home(ignored="1"), "Ignored Hull")
        self.client.post(route("srp_loss_unignore", loss.killmail_id))
        loss.refresh_from_db()
        self.assertIsNone(loss.ignored_by)
        self.assertContains(self.home(), "Ignored Hull")
        self.assertEqual(
            list(AuditLog.objects.filter(action__startswith="srp.loss_").order_by("pk").values_list("action", "actor")),
            [("srp.loss_ignore", self.user.pk), ("srp.loss_unignore", self.user.pk)],
        )

    def test_ignoring_does_not_stop_a_pasted_link(self):
        km, loss = self.loss_of(self.main)
        self.client.post(route("srp_loss_ignore", loss.killmail_id))

        claim = self.submit(km)

        self.assertEqual((claim.status, claim.source), (SrpClaim.Status.PENDING, "paste"))

    def test_the_ignore_of_somebody_else_does_not_hide_the_loss_from_the_owner(self):
        _km, loss = self.loss_of(self.main, ship_name="Mine Hull")
        loss.ignored_by = self.other
        loss.save()

        self.assertContains(self.home(), "Mine Hull")

    def test_the_actions_need_a_post(self):
        _km, loss = self.loss_of(self.main)

        for name in ("srp_loss_ignore", "srp_loss_unignore", "srp_loss_check", "srp_losses_refresh"):
            args = () if name == "srp_losses_refresh" else (loss.killmail_id,)
            with self.subTest(name=name):
                self.assertEqual(self.client.get(route(name, *args)).status_code, 405)

    def test_a_killmail_id_beyond_the_database_range_is_a_404(self):
        self.assertEqual(self.client.post(route("srp_loss_ignore", 10**30)).status_code, 404)

    def test_with_a_deadline_the_eighth_day_is_not_listed_and_the_days_left_are(self):
        self.settings(claim_deadline_days=7)
        self.loss_of(self.main, ship_name="Fresh Hull", minutes_ago=60 * 24 * 2)
        self.loss_of(self.main, ship_name="Late Hull", minutes_ago=60 * 24 * 8)

        response = self.home()

        self.assertContains(response, "Fresh Hull")
        self.assertNotContains(response, "Late Hull")
        self.assertContains(response, "left to claim")

    def test_without_a_deadline_no_days_left_are_shown(self):
        self.loss_of(self.main, ship_name="Any Hull")

        self.assertNotContains(self.home(), "left to claim")

    def test_a_character_that_changed_hands_is_gone_for_the_old_owner(self):
        self.loss_of(self.alt, ship_name="Sold Hull")
        newcomer = f.create_user("buyer", perms=f.SRP_MEMBER_PERMS)
        CharacterOwnership.objects.filter(character=self.alt).update(user=newcomer)

        self.assertNotContains(self.home(), "Sold Hull")
        self.assertContains(self.home(client=self.client_for(newcomer)), "Sold Hull")
        self.assertEqual(self.client.post(route("srp_loss_ignore", SrpLoss.objects.get().killmail_id)).status_code, 404)

    def test_the_list_is_cut_at_fifty_rows_newest_first(self):
        for number in range(55):
            SrpLoss.objects.create(
                character_id=self.main.character_id,
                killmail_id=5000 + number,
                kind=Kind.LOSS,
                killmail_time=timezone.now() - timedelta(minutes=number + 1),
                ship_type_id=f.RIFTER,
                ship_type_name=f"Hull {number:02d}",
            )

        rows = discovery.recent_losses(self.user, self.settings())

        self.assertEqual(len(rows), 50)
        self.assertEqual(rows[0].ship_type_name, "Hull 00")

    def test_the_list_costs_the_same_with_more_rows(self):
        def count():
            self.client.get(route("srp_home"))
            with CaptureQueriesContext(connection) as queries:
                self.client.get(route("srp_home"))
            return len(queries)

        self.loss_of(self.main)
        small = count()
        for _ in range(8):
            self.loss_of(self.main)

        self.assertEqual(count(), small)

    def test_the_page_makes_no_esi_call(self):
        self.loss_of(self.main)
        boom = AssertionError("ESI was called while the page was drawn")
        with (
            mock.patch.object(discovery, "get_killmail", side_effect=boom),
            mock.patch.object(discovery, "get_recent_killmails", side_effect=boom),
            mock.patch("fleetops.providers.esi_universe._read_plain", side_effect=boom),
            mock.patch("fleetops.services.names.entity_names", side_effect=boom),
        ):
            self.assertEqual(self.home().status_code, 200)

    def test_the_list_is_not_shown_when_the_feature_is_off(self):
        self.settings(loss_discovery_enabled=False)
        self.loss_of(self.main, ship_name="Off Hull")

        self.assertNotContains(self.home(), "Off Hull")


class BlockedLossTests(ViewBase):
    def setUp(self):
        super().setUp()
        self.settings(blocked_ship_group_ids=str(MINING_BARGE_GROUP), payout_rule_lifts_ship_block=True)

    def covetor(self, **extra):
        return self.loss_of(
            self.main, ship_name="Covetor", ship_type_id=COVETOR, **extra
        )[1]

    def test_a_blocked_ship_shows_the_reason_and_no_claim_button(self):
        loss = self.covetor()
        SrpLoss.objects.filter(pk=loss.pk).update(ship_group_id=MINING_BARGE_GROUP)

        response = self.home()

        self.assertContains(response, "Mining Barge ships are not covered.")
        self.assertNotContains(response, f'action="{route("srp_loss_check", loss.killmail_id)}"')

    def test_a_payout_table_entry_for_the_ship_lifts_the_block(self):
        loss = self.covetor()
        SrpLoss.objects.filter(pk=loss.pk).update(ship_group_id=MINING_BARGE_GROUP)
        f.payout_rule(COVETOR, 30_000_000, ship_name="Covetor")

        response = self.home()

        self.assertNotContains(response, "ships are not covered")
        self.assertContains(response, f'action="{route("srp_loss_check", loss.killmail_id)}"')

    def test_the_rule_check_costs_one_query_whatever_the_number_of_ships(self):
        self.settings(blocked_security_bands="highsec")

        def count():
            self.client.get(route("srp_home"))
            with CaptureQueriesContext(connection) as queries:
                self.client.get(route("srp_home"))
            return len(queries)

        self.covetor()
        small = count()
        for number in range(6):
            self.loss_of(self.main, ship_type_id=COVETOR + number + 1, ship_name=f"Ship {number}")

        self.assertEqual(count(), small)

    def test_a_loss_in_blocked_space_shows_the_system(self):
        self.settings(blocked_security_bands="highsec", blocked_ship_group_ids="")
        _km, loss = self.loss_of(self.main)
        SrpLoss.objects.filter(pk=loss.pk).update(solar_system_security=Decimal("0.9459"))

        self.assertContains(self.home(), "Losses in high security space are not covered")

    def test_a_loss_without_stored_values_shows_the_claim_button(self):
        # The lookups failed when the loss was found: nothing is refused for it, the submit checks again.
        _km, loss = self.loss_of(self.main, ship_name="Unknown Place", ship_type_id=COVETOR)

        response = self.home()

        self.assertNotContains(response, "are not covered")
        self.assertContains(response, f'action="{route("srp_loss_check", loss.killmail_id)}"')


class AltsBlockingTests(ViewBase):
    def test_the_buttons_are_disabled_and_the_character_is_listed(self):
        self.settings(require_all_alts_authorized=True)
        third = f.add_alt(self.user, "Missing Alt")
        _km, loss = self.loss_of(self.main)

        response = self.home()

        self.assertContains(response, "Authorize loss reading for all your characters before you submit a claim.")
        self.assertContains(response, third.character_name)
        self.assertContains(response, 'disabled aria-disabled="true"')

    def test_the_buttons_work_when_everything_is_authorized(self):
        self.settings(require_all_alts_authorized=True)
        self.loss_of(self.main)

        self.assertNotContains(self.home(), 'disabled aria-disabled="true"')


class FleetMatchDisplayTests(ViewBase):
    def tracked_fleet(self):
        operation = f.create_operation(self.fc, started_at=timezone.now() - timedelta(hours=2))
        link = f.fleet_link(operation)
        f.member_state(operation, self.main, first_seen=operation.started_at)
        event = FleetMemberEvent.objects.create(
            operation=operation,
            character_id=self.main.character_id,
            character_name=self.main.character_name,
            event_type=FleetMemberEvent.EventType.JOIN,
        )
        FleetMemberEvent.objects.filter(pk=event.pk).update(created_at=operation.started_at)
        return operation, link

    def test_a_matched_open_fleet_is_shown_and_preselected(self):
        _operation, link = self.tracked_fleet()
        _km, loss = self.loss_of(self.main)
        SrpLoss.objects.filter(pk=loss.pk).update(matched_link=link, match_code="in_fleet")

        response = self.home()

        self.assertContains(response, "In fleet")
        self.assertContains(response, f'<option value="fleet:{link.code}" selected>')

    def test_a_closed_link_is_no_match(self):
        _operation, link = self.tracked_fleet()
        _km, loss = self.loss_of(self.main)
        SrpLoss.objects.filter(pk=loss.pk).update(matched_link=link, match_code="in_fleet")
        link.is_open = False
        link.save()

        self.assertNotContains(self.home(), f"fleet:{link.code}")

    def test_the_choices_offer_the_standing_links_and_the_categories(self):
        category = f.srp_category("Solo PVP")
        standing = f.standing_link(category=category)
        self.loss_of(self.main)

        response = self.home()

        self.assertContains(response, f'<option value="link:{standing.code}">')
        self.assertContains(response, f'<option value="category:{category.pk}">')


class CheckFlowTests(ViewBase):
    def check(self, loss, target="", client=None):
        return (client or self.client).post(route("srp_loss_check", loss.killmail_id), {"target": target})

    def submit_from(self, response, client=None):
        token = response.context["submit_form"].initial["token"]
        return (client or self.client).post(route("srp_submit"), {"token": token, "comment": ""})

    def test_a_tracked_fleet_is_checked_and_submitted_as_a_fleet_loss_from_a_discovery(self):
        operation = f.create_operation(self.fc, started_at=timezone.now() - timedelta(hours=2))
        link = f.fleet_link(operation)
        f.member_state(operation, self.main, first_seen=operation.started_at)
        km, loss = self.loss_of(self.main)

        response = self.check(loss, f"fleet:{link.code}")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["preview"].blockers, [])
        self.assertContains(response, "Found in your recent losses")
        submitted = self.submit_from(response)
        claim = SrpClaim.objects.get(killmail=km)
        self.assertRedirects(submitted, route("srp_claim", claim.uuid), fetch_redirect_response=False)
        self.assertEqual((claim.scope, claim.source, claim.link, claim.operation), ("in_fleet", "discovered", link, operation))
        event = SrpClaimEvent.objects.get(claim=claim, action=SrpClaimEvent.Action.SUBMIT)
        self.assertEqual(event.data["source"], "discovered")

    def test_an_empty_choice_takes_the_matched_fleet(self):
        operation = f.create_operation(self.fc, started_at=timezone.now() - timedelta(hours=2))
        link = f.fleet_link(operation)
        f.member_state(operation, self.main, first_seen=operation.started_at)
        _km, loss = self.loss_of(self.main)
        SrpLoss.objects.filter(pk=loss.pk).update(matched_link=link, match_code="in_fleet")
        loss.refresh_from_db()

        response = self.check(loss, "")

        self.assertEqual(response.context["preview"].link, link)
        self.assertEqual(response.context["preview"].scope, "in_fleet")

    def test_a_registered_pilot_of_a_manual_fleet_is_checked_through_that_fleet(self):
        operation = f.create_operation(
            self.fc, status="closed", started_at=timezone.now() - timedelta(hours=3), is_manual=True, esi_fleet_id=None
        )
        link = f.fleet_link(operation, is_open=True)
        f.registration(operation, self.main)
        _km, loss = self.loss_of(self.main, minutes_ago=150)

        response = self.check(loss, f"fleet:{link.code}")

        self.assertEqual(response.context["preview"].presence.code, "registered")

    def test_no_match_and_no_category_is_refused_when_a_category_is_required(self):
        self.settings(out_of_fleet_category_required=True)
        _km, loss = self.loss_of(self.main)

        response = self.check(loss, "")

        self.assertRedirects(response, route("srp_home"), fetch_redirect_response=False)
        # With no category that members can choose the reason is that, not "Choose a category".
        self.assertEqual(
            self.messages(response),
            ["No category can be chosen for a loss outside a fleet right now. Ask an SRP manager to change the SRP settings."],
        )
        self.assertFalse(SrpClaim.objects.exists())

    def test_a_category_with_a_standing_link_of_the_day_is_out_of_fleet_with_that_link(self):
        category = f.srp_category("Solo PVP")
        standing = f.standing_link(category=category)
        km, loss = self.loss_of(self.main, minutes_ago=5)
        self.settings(out_of_fleet_category_required=True)

        response = self.check(loss, f"category:{category.pk}")
        self.submit_from(response)

        claim = SrpClaim.objects.get(killmail=km)
        self.assertEqual(
            (claim.scope, claim.source, claim.category, claim.link, claim.fleet_check),
            ("out_of_fleet", "discovered", category, standing, "not_applicable"),
        )
        self.assertIsNone(claim.operation)

    def test_the_standing_link_can_be_chosen_directly(self):
        standing = f.standing_link()
        km, loss = self.loss_of(self.main, minutes_ago=5)

        response = self.check(loss, f"link:{standing.code}")
        self.submit_from(response)

        claim = SrpClaim.objects.get(killmail=km)
        self.assertEqual((claim.link, claim.scope, claim.fleet_check), (standing, "out_of_fleet", "not_applicable"))

    def test_a_standing_link_never_counts_as_in_fleet(self):
        # INT-5, the risk: a claim under a standing link must stay out of the FC statistics.
        standing = f.standing_link()
        _km, loss = self.loss_of(self.main, minutes_ago=5)

        preview = self.check(loss, f"link:{standing.code}").context["preview"]

        self.assertEqual(preview.scope, "out_of_fleet")

    def test_a_fleet_link_cannot_be_named_as_a_standing_link_or_the_other_way_round(self):
        operation = f.create_operation(self.fc)
        link = f.fleet_link(operation)
        standing = f.standing_link()
        _km, loss = self.loss_of(self.main, minutes_ago=5)

        for target in (f"link:{link.code}", f"fleet:{standing.code}", "fleet:nosuchcode", "category:999999", "category:abc", "nonsense"):
            with self.subTest(target=target):
                response = self.check(loss, target)
                self.assertRedirects(response, route("srp_home"), fetch_redirect_response=False)
                self.assertIn(
                    self.messages(response)[-1],
                    {
                        "That link or category is not available. Check the loss again.",
                        "This category is not available. Check the loss again.",
                    },
                )

    def test_a_closed_standing_link_is_refused_by_the_check(self):
        standing = f.standing_link(is_open=False)
        _km, loss = self.loss_of(self.main, minutes_ago=5)

        response = self.check(loss, f"link:{standing.code}")

        self.assertIn("This SRP link is closed.", response.context["preview"].blockers)

    def test_two_alts_can_each_claim_their_own_loss(self):
        km_a, loss_a = self.loss_of(self.main, minutes_ago=5)
        km_b, loss_b = self.loss_of(self.alt, minutes_ago=6)

        for loss in (loss_a, loss_b):
            self.submit_from(self.check(loss, ""))

        self.assertEqual(
            set(SrpClaim.objects.values_list("character_id", "source")),
            {(self.main.character_id, "discovered"), (self.alt.character_id, "discovered")},
        )

    def test_a_loss_that_is_claimed_already_is_a_blocker(self):
        km, loss = self.loss_of(self.main, minutes_ago=5)
        f.srp_claim(self.other, killmail=km)

        preview = self.check(loss, "").context["preview"]

        self.assertTrue(preview.blockers)
        self.assertEqual(preview.token, "")

    def test_the_throttle_of_the_checks_applies(self):
        _km, loss = self.loss_of(self.main, minutes_ago=5)
        with mock.patch.object(claims, "CHECK_LIMIT", 2):
            self.check(loss, "")
            self.check(loss, "")
            response = self.check(loss, "")

        self.assertRedirects(response, route("srp_home"), fetch_redirect_response=False)
        self.assertTrue(any("too many" in text.lower() or "wait" in text.lower() for text in self.messages(response)))

    def test_an_old_token_without_the_new_fields_still_submits_as_a_pasted_claim(self):
        km, _loss = self.loss_of(self.main, minutes_ago=5)
        token = signing.dumps({"km": km.pk, "link": None, "u": self.user.pk}, salt=claims.PREVIEW_SALT)

        claim = claims.submit_claim(self.user, token)

        self.assertEqual(claim.source, "paste")

    def test_a_submission_reads_the_settings_only_under_the_lock_while_discovery_is_off(self):
        self.settings(loss_discovery_enabled=False)
        km, _loss = self.loss_of(self.main, minutes_ago=5)
        token = signing.dumps({"km": km.pk, "link": None, "u": self.user.pk, "src": "paste", "cat": None}, salt=claims.PREVIEW_SALT)

        with CaptureQueriesContext(connection) as queries:
            claims.submit_claim(self.user, token)

        table = connection.ops.quote_name("fleetops_srpsettings")  # "..." on SQLite, `...` on MariaDB
        reads = [q for q in queries.captured_queries if f"FROM {table}" in q["sql"] and q["sql"].startswith("SELECT")]
        self.assertEqual(len(reads), 1)  # the lock's own read

    def test_a_token_names_a_source_that_the_server_signed_only(self):
        km, _loss = self.loss_of(self.main, minutes_ago=5)
        token = signing.dumps({"km": km.pk, "link": None, "u": self.user.pk, "src": "aasrp", "cat": None}, salt=claims.PREVIEW_SALT)

        self.assertEqual(claims.submit_claim(self.user, token).source, "paste")

    def test_the_preview_of_a_discovery_offers_the_fleet_suggestion_through_the_loss(self):
        operation = f.create_operation(self.fc, started_at=timezone.now() - timedelta(hours=2))
        link = f.fleet_link(operation)
        f.member_state(operation, self.main, first_seen=operation.started_at)
        FleetMemberEvent.objects.filter(operation=operation).delete()
        event = FleetMemberEvent.objects.create(
            operation=operation,
            character_id=self.main.character_id,
            character_name=self.main.character_name,
            event_type=FleetMemberEvent.EventType.JOIN,
        )
        FleetMemberEvent.objects.filter(pk=event.pk).update(created_at=operation.started_at)
        _km, loss = self.loss_of(self.main, minutes_ago=5)

        response = self.check(loss, "")

        self.assertContains(response, f'action="{route("srp_loss_check", loss.killmail_id)}"')
        self.assertContains(response, f'name="target" value="fleet:{link.code}"')
        self.assertNotContains(response, f'action="{route("srp_link", link.code)}"')

    def test_the_check_needs_srp_to_be_on(self):
        _km, loss = self.loss_of(self.main)
        self.settings(enabled=False)

        response = self.check(loss, "")

        self.assertRedirects(response, route("srp_home"), fetch_redirect_response=False)
        self.assertEqual(self.messages(response), ["FleetOps SRP is not enabled."])

    def test_a_member_without_the_request_permission_is_refused(self):
        member = f.create_user("no-request", perms=f.MEMBER_PERMS)
        _km, loss = self.loss_of(self.main)

        self.assertEqual(self.check(loss, "", client=self.client_for(member)).status_code, 403)


class RefreshTests(ViewBase):
    def setUp(self):
        super().setUp()
        cache.delete(f"fleetops:srp:loss-refresh:{self.user.pk}")
        self.addCleanup(cache.delete, f"fleetops:srp:loss-refresh:{self.user.pk}")

    def test_the_refresh_makes_the_characters_due_and_queues_a_task_for_the_user(self):
        from fleetops.models import SrpLossScan

        later = timezone.now() + timedelta(hours=1)
        SrpLossScan.objects.create(character_id=self.main.character_id, user=self.user, next_check_at=later)
        with mock.patch.object(fleet_tasks.scan_srp_losses_for_user, "delay") as delay:
            response = self.client.post(route("srp_losses_refresh"))

        self.assertRedirects(response, route("srp_home"), fetch_redirect_response=False)
        delay.assert_called_once_with(self.user.pk)
        # Changed for L-9: a refreshed character gets no next check, which sorts before every overdue character.
        self.assertIsNone(SrpLossScan.objects.get(character_id=self.main.character_id).next_check_at)

    def test_the_refresh_makes_no_esi_call(self):
        boom = AssertionError("ESI was called in the request")
        with (
            mock.patch.object(fleet_tasks.scan_srp_losses_for_user, "delay"),
            mock.patch.object(discovery, "get_recent_killmails", side_effect=boom),
            mock.patch.object(discovery, "get_killmail", side_effect=boom),
        ):
            self.assertEqual(self.client.post(route("srp_losses_refresh")).status_code, 302)

    def test_a_second_refresh_within_five_minutes_is_refused(self):
        with mock.patch.object(fleet_tasks.scan_srp_losses_for_user, "delay") as delay:
            self.client.post(route("srp_losses_refresh"))
            response = self.client.post(route("srp_losses_refresh"))

        delay.assert_called_once()
        self.assertEqual(self.messages(response)[-1], "Your losses were refreshed a moment ago. Try again in a few minutes.")

    def test_the_cooldown_is_per_user(self):
        with mock.patch.object(fleet_tasks.scan_srp_losses_for_user, "delay") as delay:
            self.client.post(route("srp_losses_refresh"))
            self.client_for(self.manager).post(route("srp_losses_refresh"))
            cache.delete(f"fleetops:srp:loss-refresh:{self.manager.pk}")

        self.assertEqual(delay.call_count, 2)

    def test_the_refresh_is_closed_while_the_feature_is_off(self):
        self.settings(loss_discovery_enabled=False)
        with mock.patch.object(fleet_tasks.scan_srp_losses_for_user, "delay") as delay:
            response = self.client.post(route("srp_losses_refresh"))

        delay.assert_not_called()
        self.assertEqual(self.messages(response), ["Finding losses automatically is not enabled."])

    def test_a_broker_error_is_reported_without_a_server_error(self):
        with mock.patch.object(fleet_tasks.scan_srp_losses_for_user, "delay", side_effect=ConnectionError("down")):
            response = self.client.post(route("srp_losses_refresh"))

        self.assertEqual(response.status_code, 302)
        self.assertTrue(any("could not be refreshed" in text for text in self.messages(response)))


class PasteEntranceTests(ViewBase):
    def test_the_card_is_called_claim_with_a_link_and_points_to_the_list_while_the_feature_is_on(self):
        response = self.home()

        self.assertContains(response, "Claim with a link")
        self.assertContains(response, "Use “My recent losses” first.")

    def test_without_the_feature_the_card_has_no_hint(self):
        self.settings(loss_discovery_enabled=False)

        response = self.home()

        self.assertContains(response, "Claim with a link")
        self.assertNotContains(response, "Use “My recent losses” first.")

    def test_a_pasted_loss_with_a_category_keeps_the_category_and_the_paste_source(self):
        category = f.srp_category("Solo PVP")
        km = self.killmail()
        with mock.patch.object(claims.killmails, "fetch_killmail", return_value=km):
            preview = claims.prepare_claim(self.user, self.text(km), category=category)

        claim = claims.submit_claim(self.user, preview.token)

        self.assertEqual((claim.category, claim.source), (category, "paste"))


class SettingsPageTests(ViewBase):
    """INT-2: the group "Loss discovery" of the SRP settings page; every change is audited, nothing is added to the
    policy snapshot."""

    def setUp(self):
        super().setUp()
        self.admin = f.create_user("loss_config_admin", perms=[*f.MEMBER_PERMS, "fleetops.manage_configuration"])
        self.url = reverse("fleetops:configuration_list", args=["srp"])
        self.admin_client = self.client_for(self.admin)

    def post_data(self, **extra):
        data = {
            "value_source": "zkill_total",
            "percentage": "0",
            "over_limit_action": "review",
            "claim_deadline_days": "0",
            "capsules_need_rule": "on",
            "payout_rule_lifts_ship_block": "on",
            "category_claims_without_link": "on",
            "ban_default_days": "7",
            "ban_max_days": "365",
            "zkillboard_enabled": "on",
            "out_of_fleet_enabled": "on",
            "notify_members": "on",
            "personal_cap_includes_in_fleet": "on",
            "corporation_cap_includes_in_fleet": "on",
            "blocked_other_group_ids": "",
            "blocked_ship_type_ids": "",
        }
        data.update(extra)
        return data

    def test_the_page_has_the_group_with_its_three_settings(self):
        response = self.admin_client.get(self.url)

        self.assertContains(response, "Loss discovery")
        for name in ("loss_discovery_enabled", "loss_discovery_interval_minutes", "require_all_alts_authorized"):
            self.assertContains(response, f'name="{name}"')

    def test_saving_changes_the_settings_and_writes_the_audit_entry_with_the_old_and_new_values(self):
        self.settings(loss_discovery_enabled=False)

        response = self.admin_client.post(
            self.url,
            self.post_data(loss_discovery_enabled="on", loss_discovery_interval_minutes="90", require_all_alts_authorized="on"),
        )

        self.assertEqual(response.status_code, 302)
        from fleetops.models import SrpSettings

        row = SrpSettings.get_solo()
        self.assertEqual((row.loss_discovery_enabled, row.loss_discovery_interval_minutes, row.require_all_alts_authorized), (True, 90, True))
        entry = AuditLog.objects.filter(action="configuration.update", object_type="SrpSettings").latest("pk")
        self.assertEqual(entry.old_value["loss_discovery_interval_minutes"], 60)
        self.assertEqual(entry.new_value["loss_discovery_interval_minutes"], 90)
        self.assertTrue(entry.new_value["loss_discovery_enabled"])

    def test_an_interval_outside_fifteen_to_fourteen_hundred_forty_is_refused(self):
        for minutes in ("14", "1441"):
            with self.subTest(minutes=minutes):
                response = self.admin_client.post(self.url, self.post_data(loss_discovery_interval_minutes=minutes))
                self.assertEqual(response.status_code, 200)
                from fleetops.models import SrpSettings

                self.assertEqual(SrpSettings.get_solo().loss_discovery_interval_minutes, 60)

    def test_a_submission_without_the_interval_keeps_it(self):
        from fleetops.models import SrpSettings

        SrpSettings.objects.filter(pk=1).update(loss_discovery_interval_minutes=120)

        self.admin_client.post(self.url, self.post_data())

        self.assertEqual(SrpSettings.get_solo().loss_discovery_interval_minutes, 120)

    def test_the_settings_are_not_part_of_the_claim_snapshot(self):
        from fleetops.srp import policy
        from fleetops.srp.types import PolicySnapshot

        self.assertFalse({"loss_discovery_enabled", "require_all_alts_authorized"} & set(policy._POLICY_FIELDS))
        self.assertFalse(
            {"loss_discovery_enabled", "loss_discovery_interval_minutes", "require_all_alts_authorized"}
            & set(PolicySnapshot().as_json())
        )
