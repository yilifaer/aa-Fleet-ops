"""The member pages of FleetOps SRP: My SRP, the fleet SRP link, the preview, submitting, and the claim page.

The services behind the pages (reading killmails, the quota and the handling of claims) have their own tests, so
the pages are tested with those services replaced by fakes that return what the real ones return.
"""

from datetime import datetime, timedelta, timezone as dt_timezone
from decimal import Decimal
from unittest import mock

from django.contrib.messages import get_messages
from django.test import Client, TestCase
from django.test.utils import CaptureQueriesContext
from django.db import connection
from django.urls import reverse
from django.utils import timezone

from fleetops.models import FleetOperation, SrpClaim, SrpSettings
from fleetops.providers.esi import FleetESIError
from fleetops.services.periods import current_year_month
from fleetops.srp.types import (
    Calculation,
    CalcNote,
    ClaimIdentity,
    ClaimPreview,
    ClaimWarning,
    LimitState,
    PersonalSrp,
    PolicySnapshot,
    PresenceResult,
    RuleSnapshot,
    SrpDuplicateError,
    SrpError,
    SrpStateError,
    SrpTotals,
)

from . import factories as f

UTC = dt_timezone.utc
CLOSED = FleetOperation.Status.CLOSED
PENDING = SrpClaim.Status.PENDING
INSTRUCTIONS = "Post a killmail within a week of the loss."


def route(name, *args):
    return reverse(f"fleetops:{name}", args=args)


def messages_of(response):
    return [str(message) for message in get_messages(response.wsgi_request)]


def make_preview(user, *, link=None, blockers=(), warnings=(), token="signed-token", scope=None, **extra):
    """A preview as ``claims.prepare_claim`` returns it, for the character of ``user``."""
    character = f.main_of(user)
    killmail = f.srp_killmail(character, ship_name="Rifter", fitted=Decimal("12345678.00"))
    identity = ClaimIdentity(
        character_id=character.character_id,
        character_name=character.character_name,
        main_character_id=character.character_id,
        main_character_name=character.character_name,
        corporation_id=character.corporation_id,
        corporation_name=character.corporation_name,
        corporation_ticker=character.corporation_ticker,
        alliance_id=character.alliance_id,
        alliance_name=character.alliance_name,
    )
    policy = PolicySnapshot(percentage=Decimal("80"), rule=None)
    calculation = Calculation(
        basis="percentage",
        loss_value=killmail.zkill_fitted_value,
        value_source="zkill_fitted",
        base=9_876_542,
        cap=None,
        suggested=9_876_542,
        limits=(
            LimitState("personal", cap=100_000_000, used=40_000_000, pending=5_000_000, remaining=60_000_000),
            LimitState("corporation", cap=None, used=0, pending=0, remaining=None),
        ),
        limited=9_876_542,
    )
    values = dict(
        killmail=killmail,
        scope=scope or (SrpClaim.Scope.IN_FLEET if link else SrpClaim.Scope.OUT_OF_FLEET),
        link=link,
        user=user,
        identity=identity,
        eligible=not blockers,
        blockers=list(blockers),
        warnings=list(warnings),
        presence=PresenceResult("in_fleet") if link else None,
        policy=policy,
        calculation=calculation,
        token=token,
    )
    values.update(extra)
    return ClaimPreview(**values)


class SrpPagesTestCase(TestCase):
    """People, a fleet with an SRP link and a few claims the pages are tested with."""

    @classmethod
    def setUpTestData(cls):
        cls.settings_row = f.srp_settings(enabled=True, member_instructions=INSTRUCTIONS)
        cls.member = f.create_user("pilot", perms=f.SRP_MEMBER_PERMS)
        cls.other = f.create_user("otherpilot", perms=f.SRP_MEMBER_PERMS)
        cls.manager = f.create_user("srpmanager", perms=f.SRP_MANAGER_PERMS)
        cls.manager2 = f.create_user("srpmanagertwo", perms=f.SRP_MANAGER_PERMS)
        cls.fc = f.create_user("srpfc", perms=[*f.FC_PERMS, "fleetops.request_srp"])
        cls.stats = f.create_user("srpstats", perms=[*f.MEMBER_PERMS, "fleetops.view_all_stats"])
        cls.no_srp = f.create_user("nosrp", perms=f.MEMBER_PERMS)
        now = timezone.now()
        cls.fleet = f.create_operation(
            cls.fc, status=CLOSED, started_at=now - timedelta(days=1), doctrine_name="Muninn Fleet"
        )
        cls.link = f.fleet_link(cls.fleet)
        cls.closed_fleet = f.create_operation(cls.fc, status=CLOSED, started_at=now - timedelta(days=2))
        cls.closed_link = f.fleet_link(cls.closed_fleet, is_open=False)
        cls.loss_time = now - timedelta(hours=20)

    def setUp(self):
        super().setUp()
        self.client = self.client_for(self.member)

    def client_for(self, user):
        client = Client(raise_request_exception=True)
        client.force_login(user)
        return client

    def patch(self, target, **kwargs):
        patcher = mock.patch(target, **kwargs)
        mocked = patcher.start()
        self.addCleanup(patcher.stop)
        return mocked

    def patch_home_services(self, user=None, **personal):
        """The summary and the fleet list that My SRP asks other modules for."""
        user = user or self.member
        values = dict(year=2026, month=10, totals=SrpTotals(), claims=[], limits=())
        values.update(personal)
        self.personal = self.patch("fleetops.srp.summaries.personal", return_value=PersonalSrp(**values))
        self.recent = self.patch("fleetops.srp.links.recent_links_for_user", return_value=[])

    def claim_url(self, claim, name="srp_claim", *args):
        return route(name, claim.uuid, *args)


class HomePageTests(SrpPagesTestCase):
    def setUp(self):
        super().setUp()
        self.patch_home_services()

    def test_page_shows_the_form_instructions_and_the_month(self):
        response = self.client.get(route("srp_home"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'name="killmail"')
        self.assertContains(response, f'action="{route("srp_check")}"')
        self.assertContains(response, INSTRUCTIONS)
        self.assertContains(response, "(EVE time)")

    def test_allowance_and_totals_are_shown_from_the_personal_summary(self):
        self.patch_home_services(
            totals=SrpTotals(claims=4, pending=1, approved_count=3, paid_count=2, requested=400_000_000,
                             approved=300_000_000, paid=200_000_000, outstanding=100_000_000),
            limits=(
                LimitState("personal", cap=500_000_000, used=200_000_000, pending=50_000_000, remaining=300_000_000),
                LimitState("corporation", cap=None),
            ),
        )

        response = self.client.get(route("srp_home"), {"year": "2026", "month": "10"})

        self.assertContains(response, "Your monthly allowance")
        self.assertContains(response, "500,000,000 ISK")
        self.assertContains(response, "300,000,000 ISK")
        self.assertContains(response, "50,000,000 ISK")
        self.assertContains(response, "No limit")
        self.assertContains(response, "400m ISK")
        self.personal.assert_called_once_with(self.member, 2026, 10)

    def test_paid_claims_are_counted_once_in_the_approved_card(self):
        # approved_count already holds the paid claims: one paid claim is one approved claim.
        self.patch_home_services(
            totals=SrpTotals(claims=1, approved_count=1, paid_count=1, requested=5_000_000, approved=5_000_000,
                             paid=5_000_000),
        )

        html = self.client.get(route("srp_home")).content.decode()

        approved = html.split("<span>Approved</span>", 1)[1].split("</div>", 1)[0]
        paid = html.split("<span>Paid</span>", 1)[1].split("</div>", 1)[0]
        self.assertIn("1 claim<", approved)
        self.assertIn("1 claim<", paid)
        self.assertNotIn("2 claims", html)

    def test_a_limit_that_is_used_up_is_marked_red(self):
        self.patch_home_services(
            limits=(LimitState("personal", cap=100, used=100, pending=0, remaining=0),)
        )

        response = self.client.get(route("srp_home"))

        self.assertContains(response, 'class="fleetops-srp-over-limit"')

    def test_limits_that_do_not_count_in_fleet_losses_say_so(self):
        self.patch_home_services(
            limits=(LimitState("personal", cap=100, used=0, pending=0, remaining=100, applies=False),)
        )

        self.assertContains(self.client.get(route("srp_home")), "In-fleet losses do not count toward this limit.")

    def test_recent_fleets_offer_their_srp_page(self):
        self.recent.return_value = [self.link]

        response = self.client.get(route("srp_home"))

        self.assertContains(response, "Request SRP")
        self.assertContains(response, f'href="{route("srp_link", self.link.code)}"')
        self.recent.assert_called_once_with(self.member)

    def test_without_recent_fleets_the_page_says_where_to_look(self):
        self.assertContains(self.client.get(route("srp_home")), "No fleet of yours has an open SRP link")

    def test_claims_are_listed_for_the_month_of_the_loss_only(self):
        now = timezone.now()
        this_month = f.srp_claim(self.member, time=now)
        march = f.srp_claim(self.member, time=datetime(2026, 3, 14, 12, 0, tzinfo=UTC))
        theirs = f.srp_claim(self.other, time=now)

        response = self.client.get(route("srp_home"))
        in_march = self.client.get(route("srp_home"), {"year": "2026", "month": "3"})

        self.assertContains(response, this_month.reference)
        self.assertNotContains(response, march.reference)
        self.assertNotContains(response, theirs.reference)
        self.assertContains(in_march, march.reference)
        self.assertNotContains(in_march, this_month.reference)

    def test_withdrawn_claims_stay_in_the_list(self):
        claim = f.srp_claim(self.member, status=SrpClaim.Status.WITHDRAWN, time=timezone.now())

        self.assertContains(self.client.get(route("srp_home")), claim.reference)

    def test_claims_are_paged_by_25(self):
        now = timezone.now()
        for _ in range(27):
            f.srp_claim(self.member, time=now)

        first = self.client.get(route("srp_home"))
        second = self.client.get(route("srp_home"), {"page": "2"})
        junk = self.client.get(route("srp_home"), {"page": "abc"})

        self.assertEqual(len(first.context["page"].object_list), 25)
        self.assertEqual(len(second.context["page"].object_list), 2)
        self.assertEqual(first.context["page"].number, junk.context["page"].number)
        self.assertContains(first, "Claims 1&ndash;25 of 27")

    def test_open_claims_show_the_requested_amount_and_decided_ones_the_approved_amount(self):
        now = timezone.now()
        f.srp_claim(self.member, time=now, amount=12_000_000, limited_amount=9_000_000)
        f.srp_claim(self.member, time=now, status=SrpClaim.Status.APPROVED, amount=30_000_000, approved_amount=25_000_000)
        f.srp_claim(self.member, time=now, status=SrpClaim.Status.REJECTED, amount=40_000_000)

        response = self.client.get(route("srp_home"))

        self.assertContains(response, "9,000,000 ISK")
        self.assertContains(response, "25,000,000 ISK")
        self.assertNotContains(response, "40,000,000 ISK")

    def test_out_of_fleet_form_is_hidden_while_those_claims_are_off(self):
        SrpSettings.objects.filter(pk=1).update(out_of_fleet_enabled=False)

        response = self.client.get(route("srp_home"))

        self.assertNotContains(response, 'name="killmail"')
        self.assertContains(response, "Only losses in FleetOps fleets are covered.")

    def test_services_are_not_asked_while_srp_is_off(self):
        SrpSettings.objects.filter(pk=1).update(enabled=False)

        response = self.client.get(route("srp_home"))

        self.assertContains(response, "FleetOps SRP is not enabled.")
        self.assertNotContains(response, 'name="killmail"')
        self.personal.assert_not_called()
        self.recent.assert_not_called()

    def test_invalid_month_falls_back_to_the_current_month(self):
        response = self.client.get(route("srp_home"), {"year": "99999", "month": "13"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual((response.context["year"], response.context["month"]), current_year_month())


class CheckLossTests(SrpPagesTestCase):
    def setUp(self):
        super().setUp()
        self.patch_home_services()
        self.prepare = self.patch("fleetops.srp.claims.prepare_claim")

    def check(self, text="https://zkillboard.com/kill/123456/", **extra):
        return self.client.post(route("srp_check"), {"killmail": text, **extra})

    def test_preview_shows_the_loss_the_calculation_the_allowance_and_a_submit_form(self):
        warning = ClaimWarning("value_estimate", "warning")
        self.prepare.return_value = make_preview(self.member, warnings=[warning])

        response = self.check()

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "fleetops/srp/preview.html")
        self.assertContains(response, "Rifter")
        self.assertContains(response, "12,345,678 ISK")
        self.assertContains(response, "zKillboard fitted value")
        self.assertContains(response, "80% of the loss value")
        self.assertContains(response, "9,876,542 ISK")
        self.assertContains(response, "Your monthly allowance")
        self.assertContains(response, "60,000,000 ISK")
        self.assertContains(response, "Value is an ESI market estimate.")
        self.assertContains(response, f'action="{route("srp_submit")}"')
        self.assertContains(response, 'name="token" value="signed-token"')
        self.assertContains(response, 'name="comment"')
        self.assertContains(response, "Submit claim")
        self.assertContains(response, "images.evetech.net/types/587/render?size=64")
        self.assertContains(response, 'rel="noopener noreferrer"')
        self.prepare.assert_called_once_with(self.member, "https://zkillboard.com/kill/123456/", link=None)

    def test_preview_of_a_payout_table_ship_names_the_rule(self):
        preview = make_preview(self.member)
        preview.policy = PolicySnapshot(
            rule=RuleSnapshot(id=3, label="Muninn", ship_type_id=12015, ship_type_name="Muninn", scope="any",
                              amount=150_000_000, match="ship")
        )
        preview.calculation = Calculation(basis="rule", base=150_000_000, suggested=150_000_000, limited=150_000_000)
        self.prepare.return_value = preview

        response = self.check()

        self.assertContains(response, "Payout table")
        self.assertContains(response, "Muninn")
        self.assertContains(response, "150,000,000 ISK")

    def test_calculation_notes_are_written_out(self):
        preview = make_preview(self.member)
        preview.calculation = Calculation(
            basis="none", notes=(CalcNote("no_rule"), CalcNote("loss_cap", {"cap": 50_000_000})), limited=0
        )
        self.prepare.return_value = preview

        response = self.check()

        self.assertContains(response, "No payout rule for this ship.")
        self.assertContains(response, "Capped at the per-loss cap of 50,000,000 ISK.")
        self.assertContains(response, "There is no automatic amount for this loss")

    def test_over_limit_preview_is_red_and_says_which_limit_holds_it_down(self):
        preview = make_preview(self.member, warnings=[ClaimWarning("over_limit", "danger", {"kind": "personal", "requested": 100_000_000, "remaining": 60_000_000})])
        preview.calculation = Calculation(
            basis="rule", base=100_000_000, suggested=100_000_000, limited=60_000_000, over_limit=True,
            binding_limit="personal",
            limits=(LimitState("personal", cap=100_000_000, used=40_000_000, remaining=60_000_000),),
        )
        self.prepare.return_value = preview

        response = self.check()

        self.assertContains(response, "Over the monthly personal limit: 100,000,000 ISK requested, 60,000,000 ISK left.")
        self.assertContains(response, "Held down by the personal monthly limit.")
        self.assertContains(response, "text-bg-danger")

    def test_blockers_are_listed_and_there_is_no_submit_button(self):
        self.prepare.return_value = make_preview(self.member, blockers=["Capsule losses are not covered."])

        response = self.check()

        self.assertContains(response, "This loss cannot be submitted.")
        self.assertContains(response, "Capsule losses are not covered.")
        self.assertNotContains(response, "Submit claim")
        self.assertNotContains(response, route("srp_submit"))

    def test_existing_claim_of_the_pilot_is_linked(self):
        claim = f.srp_claim(self.member, status=SrpClaim.Status.APPROVED)
        self.prepare.return_value = make_preview(
            self.member, blockers=["This loss is already claimed."], existing_claim=claim
        )

        response = self.check()

        self.assertContains(response, f'href="{route("srp_claim", claim.uuid)}"')
        self.assertContains(response, "Approved")

    def test_existing_claim_of_somebody_else_is_not_linked(self):
        claim = f.srp_claim(self.other, status=SrpClaim.Status.APPROVED)
        self.prepare.return_value = make_preview(
            self.member, blockers=["This loss was already claimed by another pilot."], existing_claim=claim
        )

        response = self.check()

        self.assertContains(response, "already claimed by another pilot")
        self.assertNotContains(response, route("srp_claim", claim.uuid))

    def test_a_loss_inside_a_fleet_is_offered_as_a_fleet_loss(self):
        self.prepare.return_value = make_preview(self.member, suggested_link=self.link)

        response = self.check("killReport:123456:" + "ab" * 20)

        self.assertContains(response, "Submit it as a fleet loss?")
        self.assertContains(response, f'action="{route("srp_link", self.link.code)}"')
        self.assertContains(response, 'name="killmail" value="killReport:123456:' + "ab" * 20 + '"')

    def test_a_service_error_goes_back_to_the_form_with_its_text(self):
        self.prepare.side_effect = SrpError("no_hash", "zKillboard does not have this killmail yet.")

        response = self.check()

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "fleetops/srp/home.html")
        self.assertContains(response, "zKillboard does not have this killmail yet.")
        self.assertContains(response, "https://zkillboard.com/kill/123456/")

    def test_an_esi_error_is_shown_as_text(self):
        self.prepare.side_effect = FleetESIError("KILLMAIL_NOT_FOUND", "Killmail not found. Check the link or the hash.")

        response = self.check()

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Killmail not found. Check the link or the hash.")

    def test_text_that_is_no_killmail_is_refused_before_any_lookup(self):
        response = self.check("hello there")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Paste an in-game killmail link, a zKillboard link, or the killmail ID and hash.")
        self.prepare.assert_not_called()

    def test_an_empty_form_asks_for_the_killmail(self):
        response = self.check("")

        self.assertContains(response, "This field is required.")
        self.prepare.assert_not_called()

    def test_nothing_is_looked_up_while_srp_is_off(self):
        SrpSettings.objects.filter(pk=1).update(enabled=False)

        response = self.check()

        self.assertContains(response, "FleetOps SRP is not enabled.")
        self.prepare.assert_not_called()

    def test_nothing_is_looked_up_while_out_of_fleet_claims_are_off(self):
        SrpSettings.objects.filter(pk=1).update(out_of_fleet_enabled=False)

        response = self.check()

        self.assertContains(response, "Only losses in FleetOps fleets are covered.")
        self.prepare.assert_not_called()

    def test_the_check_needs_a_post(self):
        self.assertEqual(self.client.get(route("srp_check")).status_code, 405)


class FleetLinkPageTests(SrpPagesTestCase):
    def setUp(self):
        super().setUp()
        self.prepare = self.patch("fleetops.srp.claims.prepare_claim")

    def test_open_link_shows_the_fleet_and_the_form(self):
        response = self.client.get(route("srp_link", self.link.code))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "SRP for")
        self.assertContains(response, f"FC {self.fc.profile.main_character.character_name}")
        self.assertContains(response, "doctrine Muninn Fleet")
        self.assertContains(response, 'name="killmail"')
        self.assertContains(response, f'action="{route("srp_link", self.link.code)}"')
        self.assertContains(response, INSTRUCTIONS)

    def test_closed_link_shows_a_notice_and_no_form(self):
        response = self.client.get(route("srp_link", self.closed_link.code))

        self.assertContains(response, "closed")
        self.assertNotContains(response, 'name="killmail"')

    def test_posting_to_a_closed_link_looks_nothing_up(self):
        response = self.client.post(route("srp_link", self.closed_link.code), {"killmail": "https://zkillboard.com/kill/1/"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "closed")
        self.prepare.assert_not_called()

    def test_disabled_srp_shows_a_notice(self):
        SrpSettings.objects.filter(pk=1).update(enabled=False)

        response = self.client.get(route("srp_link", self.link.code))

        self.assertContains(response, "FleetOps SRP is not enabled.")
        self.assertNotContains(response, 'name="killmail"')

    def test_posting_checks_the_loss_for_that_fleet(self):
        self.prepare.return_value = make_preview(self.member, link=self.link)

        response = self.client.post(route("srp_link", self.link.code), {"killmail": "https://zkillboard.com/kill/654321/"})

        self.assertTemplateUsed(response, "fleetops/srp/preview.html")
        self.assertContains(response, "Fleet presence:")
        self.assertContains(response, "In the fleet at the time of the loss.")
        self.assertContains(response, "FC ")
        self.prepare.assert_called_once()
        self.assertEqual(self.prepare.call_args.kwargs["link"], self.link)
        self.assertContains(response, f'href="{route("srp_link", self.link.code)}"')

    def test_fleet_presence_warnings_are_shown_with_their_numbers(self):
        preview = make_preview(self.member, link=self.link)
        preview.presence = PresenceResult("near", "warning", {"direction": "join", "seconds": 45, "interval": 60})
        self.prepare.return_value = preview

        response = self.client.post(route("srp_link", self.link.code), {"killmail": "https://zkillboard.com/kill/654321/"})

        self.assertContains(response, "within 45 s of joining")
        self.assertContains(response, "text-bg-warning")

    def test_a_presence_problem_is_listed_once_in_the_preview(self):
        preview = make_preview(
            self.member, link=self.link, warnings=[ClaimWarning("never_seen", "danger"), ClaimWarning("external_claim", params={"claims": ["aa-srp: Pending"]})]
        )
        preview.presence = PresenceResult("never_seen", "danger")
        self.prepare.return_value = preview

        response = self.client.post(route("srp_link", self.link.code), {"killmail": "https://zkillboard.com/kill/654321/"})

        self.assertContains(response, "This character was never seen in the fleet.", count=1)
        self.assertContains(response, "Also claimed in aa-srp: Pending.")

    def test_error_keeps_the_link_page(self):
        self.prepare.side_effect = SrpError("throttled", "Too many checks. Try again in a few minutes.")

        response = self.client.post(route("srp_link", self.link.code), {"killmail": "https://zkillboard.com/kill/654321/"})

        self.assertTemplateUsed(response, "fleetops/srp/link.html")
        self.assertContains(response, "Too many checks. Try again in a few minutes.")

    def test_own_claims_of_the_fleet_are_listed(self):
        mine = f.srp_claim(self.member, link=self.link, time=self.loss_time)
        theirs = f.srp_claim(self.other, link=self.link, time=self.loss_time)

        response = self.client.get(route("srp_link", self.link.code))

        self.assertContains(response, mine.reference)
        self.assertNotContains(response, theirs.reference)

    def test_unknown_code_is_a_404(self):
        self.assertEqual(self.client.get(route("srp_link", "nosuchcode")).status_code, 404)


class SubmitTests(SrpPagesTestCase):
    def setUp(self):
        super().setUp()
        self.submit = self.patch("fleetops.srp.claims.submit_claim")

    def post(self, **data):
        return self.client.post(route("srp_submit"), {"token": "signed-token", "comment": "", **data})

    def test_a_claim_opens_its_page_with_a_message(self):
        claim = f.srp_claim(self.member)
        self.submit.return_value = claim

        response = self.post(comment="Lost it to a gate camp")

        self.assertRedirects(response, route("srp_claim", claim.uuid), fetch_redirect_response=False)
        self.submit.assert_called_once_with(self.member, "signed-token", comment="Lost it to a gate camp")
        self.assertIn(f"{claim.reference} submitted. A reviewer will decide on it.", messages_of(response))

    def test_an_automatic_rejection_is_reported_with_its_reason(self):
        claim = f.srp_claim(
            self.member, status=SrpClaim.Status.REJECTED, auto_rejected=True,
            decision_reason="Rejected automatically: the monthly personal limit is used up (cap 100,000,000 ISK).",
        )
        self.submit.return_value = claim

        response = self.post()

        self.assertRedirects(response, route("srp_claim", claim.uuid), fetch_redirect_response=False)
        text = " ".join(messages_of(response))
        self.assertIn("rejected automatically", text)
        self.assertIn("monthly personal limit is used up", text)

    def test_a_token_that_does_not_check_out_goes_back_to_my_srp(self):
        self.submit.side_effect = SrpError("token", "This check has expired. Check the loss again.")

        response = self.post()

        self.assertRedirects(response, route("srp_home"), fetch_redirect_response=False)
        self.assertIn("This check has expired. Check the loss again.", messages_of(response))

    def test_a_loss_the_pilot_already_claimed_opens_that_claim(self):
        claim = f.srp_claim(self.member)
        self.submit.side_effect = SrpDuplicateError("You already claimed this loss.", claim=claim)

        response = self.post()

        self.assertRedirects(response, route("srp_claim", claim.uuid), fetch_redirect_response=False)
        self.assertIn("You already claimed this loss.", messages_of(response))

    def test_a_loss_claimed_by_somebody_else_does_not_open_their_claim(self):
        claim = f.srp_claim(self.other)
        self.submit.side_effect = SrpDuplicateError("This loss was already claimed by another pilot.", claim=claim)

        response = self.post()

        self.assertRedirects(response, route("srp_home"), fetch_redirect_response=False)

    def test_unreadable_form_is_refused_without_calling_the_service(self):
        response = self.client.post(route("srp_submit"), {"comment": "x"})

        self.assertRedirects(response, route("srp_home"), fetch_redirect_response=False)
        self.submit.assert_not_called()
        self.assertTrue(messages_of(response))

    def test_nothing_is_submitted_while_srp_is_off(self):
        SrpSettings.objects.filter(pk=1).update(enabled=False)

        response = self.post()

        self.assertRedirects(response, route("srp_home"), fetch_redirect_response=False)
        self.submit.assert_not_called()
        self.assertIn("FleetOps SRP is not enabled.", messages_of(response))

    def test_a_get_is_refused(self):
        self.assertEqual(self.client.get(route("srp_submit")).status_code, 405)


class ClaimPageTests(SrpPagesTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.claim = f.srp_claim(
            cls.member, link=cls.link, time=cls.loss_time, amount=80_000_000, member_comment="Lost to a bubble",
            fleet_check="near", fleet_check_params={"direction": "join", "seconds": 30, "interval": 60},
            fleet_checked_at=timezone.now(),
            warnings=[
                ClaimWarning(
                    "over_limit", "danger", {"kind": "corporation", "requested": 80_000_000, "remaining": 50_000_000}
                ).as_json()
            ],
            over_limit=True, suggested_amount=80_000_000, limited_amount=50_000_000,
            calculation=Calculation(
                basis="rule", base=80_000_000, suggested=80_000_000, limited=50_000_000, over_limit=True,
                binding_limit="corporation",
                limits=(LimitState("corporation", cap=200_000_000, used=150_000_000, pending=0, remaining=50_000_000),),
                notes=(CalcNote("monthly_limit", {"kind": "corporation", "remaining": 50_000_000}),),
            ).as_json(),
        )
        f.claim_event(cls.claim, "note", actor=cls.manager2, internal=True, reason="Looks like an alt of a spy")
        f.claim_event(cls.claim, "submit", actor=cls.member, to_status="pending", reason="Submitted by the pilot",
                      amount=50_000_000)
        f.claim_event(cls.claim, "approve", actor=cls.manager2, from_status="pending", to_status="approved",
                      reason="Fine", amount=50_000_000)

    def get(self, user=None, claim=None):
        client = self.client_for(user) if user else self.client
        return client.get(self.claim_url(claim or self.claim))

    def test_claimant_sees_the_loss_the_calculation_and_the_warnings(self):
        response = self.get()

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.claim.reference)
        self.assertContains(response, "Pending")
        self.assertContains(response, "Lost to a bubble")
        self.assertContains(response, "Over the monthly corporation limit: 80,000,000 ISK requested, 50,000,000 ISK left.")
        self.assertContains(response, "within 30 s of joining")
        self.assertContains(response, "Held down by the corporation monthly limit.")
        self.assertContains(response, "Limited to the remaining monthly corporation limit of 50,000,000 ISK.")
        self.assertContains(response, "200,000,000 ISK")
        self.assertContains(response, f"https://zkillboard.com/kill/{self.claim.killmail_id}/")
        self.assertContains(response, 'rel="noopener noreferrer"')
        self.assertContains(response, self.claim.fleet_label)

    def test_claimant_sees_the_history_without_internal_notes(self):
        response = self.get()

        self.assertContains(response, "Submitted by the pilot")
        self.assertContains(response, "You")
        self.assertContains(response, "Reviewer")
        self.assertNotContains(response, "Looks like an alt of a spy")
        self.assertNotContains(response, self.manager2.username)

    def test_other_viewers_see_the_claimants_own_steps_as_the_claimants(self):
        corp_viewer = f.create_user("srpcorpviewer", perms=[*f.MEMBER_PERMS, "fleetops.view_corp_stats"])

        def history(response):
            html = response.content.decode().split("<strong>History</strong>", 1)[1]
            return {
                row.split("</strong>", 1)[0].split("<strong>")[-1]: row
                for row in html.split('<li class="list-group-item">')[1:]
            }

        for user in (self.stats, self.fc, corp_viewer):
            with self.subTest(user=user.username):
                rows = history(self.get(user))
                self.assertEqual(sorted(rows), ["Approved", "Submitted"])
                self.assertIn("Claimant", rows["Submitted"])
                self.assertNotIn("Reviewer", rows["Submitted"])
                self.assertIn("Reviewer", rows["Approved"])
                self.assertNotIn(self.manager2.username, rows["Approved"])
        # The claimant reads their own step as "You", and the decision as the reviewer's.
        rows = history(self.get())
        self.assertIn("You", rows["Submitted"])
        self.assertIn("Reviewer", rows["Approved"])

    def test_managers_see_internal_notes_and_who_acted(self):
        response = self.get(self.manager)

        self.assertContains(response, "Looks like an alt of a spy")
        self.assertContains(response, "Internal note")
        self.assertContains(response, self.manager2.username)

    def test_other_viewers_do_not_see_internal_notes(self):
        for user in (self.stats, self.fc):
            with self.subTest(user=user.username):
                self.assertNotContains(self.get(user), "Looks like an alt of a spy")

    def test_a_fleet_check_problem_is_listed_once_on_the_claim_page(self):
        claim = f.srp_claim(
            self.member, link=self.link, time=self.loss_time, fleet_check="never_seen",
            warnings=[ClaimWarning("never_seen", "danger").as_json(), ClaimWarning("ship_mismatch", "info", {"ship": "Moa"}).as_json()],
        )
        for user in (self.member, self.manager):
            with self.subTest(user=user.username):
                response = self.get(user, claim)
                self.assertContains(response, "This character was never seen in the fleet.", count=1)
                self.assertContains(response, "Moa")

    def test_the_header_of_a_pending_claim_names_the_requested_amount_and_what_fits_the_limits(self):
        claim = f.srp_claim(
            self.member, time=self.loss_time, amount=30_000_000, suggested_amount=30_000_000, limited_amount=0,
            over_limit=True,
        )
        for user in (self.member, self.manager):
            with self.subTest(user=user.username):
                html = self.get(user, claim).content.decode()
                header = html.split("</h2>", 1)[1].split("Decision", 1)[0].split('<div class="row g-3', 1)[0]
                self.assertIn('Requested</div><div class="fs-4 fw-semibold">30,000,000 ISK</div>', header)
                self.assertIn("Within limits:", header)
                self.assertIn("0 ISK", header.split("Within limits:", 1)[1])

    def test_the_header_shows_no_second_figure_while_the_claim_fits_its_limits(self):
        claim = f.srp_claim(self.member, time=self.loss_time, amount=30_000_000, suggested_amount=30_000_000)

        html = self.get(claim=claim).content.decode()

        header = html.split("</h2>", 1)[1].split('<div class="row g-3', 1)[0]
        self.assertIn('Requested</div><div class="fs-4 fw-semibold">30,000,000 ISK</div>', header)
        self.assertNotIn("Within limits:", header)

    def test_the_monthly_usage_is_shown_to_the_claimant_and_the_stats_roles_but_not_to_the_fc(self):
        corp_viewer = f.create_user("srpcorpstats", perms=[*f.MEMBER_PERMS, "fleetops.view_corp_stats"])
        for user, shown in (
            (self.member, True),
            (self.manager, True),
            (self.stats, True),
            (corp_viewer, True),
            (self.fc, False),
        ):
            with self.subTest(user=user.username):
                response = self.get(user)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.context["show_limits"], shown)
                if shown:
                    self.assertContains(response, "Monthly limits")
                    self.assertContains(response, "Approved and paid")
                    self.assertTrue(response.context["limits"])
                else:
                    self.assertNotContains(response, "Monthly limits")
                    self.assertNotContains(response, "Approved and paid")
                    self.assertFalse(response.context["limits"])
                # The claim's own calculation stays for every viewer.
                self.assertContains(response, "Suggested amount")

    def test_nobody_else_opens_the_claim(self):
        for user in (self.other, self.no_srp):
            with self.subTest(user=user.username):
                self.assertEqual(self.get(user).status_code, 404)

    def test_claimant_can_withdraw_a_pending_claim_but_not_decide(self):
        response = self.get()

        self.assertContains(response, f'action="{route("srp_claim_withdraw", self.claim.uuid)}"')
        self.assertContains(response, "Withdraw claim")
        self.assertNotContains(response, "Approve suggested")
        self.assertNotContains(response, route("srp_claim_action", self.claim.uuid, "approve"))
        self.assertNotContains(response, "Internal note")

    def test_withdrawn_claim_can_be_submitted_again(self):
        claim = f.srp_claim(self.member, status=SrpClaim.Status.WITHDRAWN, time=self.loss_time)

        response = self.get(claim=claim)

        self.assertContains(response, f'action="{route("srp_claim_resubmit", claim.uuid)}"')
        self.assertNotContains(response, "Withdraw claim")

    def test_decided_claims_have_neither_button(self):
        claim = f.srp_claim(self.member, status=SrpClaim.Status.APPROVED, time=self.loss_time)

        response = self.get(claim=claim)

        self.assertNotContains(response, "Withdraw claim")
        self.assertNotContains(response, "Submit again")

    def test_a_rejection_shows_its_reason_to_the_claimant(self):
        claim = f.srp_claim(self.member, status=SrpClaim.Status.REJECTED, decision_reason="Not covered by the policy", time=self.loss_time)

        response = self.get(claim=claim)

        self.assertContains(response, "Not covered by the policy")
        self.assertContains(response, "alert-danger")

    def test_amounts_show_what_was_suggested_and_approved(self):
        claim = f.srp_claim(
            self.member, status=SrpClaim.Status.APPROVED, time=self.loss_time, amount=80_000_000, approved_amount=60_000_000
        )

        response = self.get(claim=claim)

        self.assertContains(response, "80,000,000 ISK")
        self.assertContains(response, "60,000,000 ISK")

    def test_a_paid_claim_shows_the_payment_note(self):
        claim = f.srp_claim(
            self.member, status=SrpClaim.Status.PAID, decided_by=self.manager, time=self.loss_time, payment_note="Sent in game"
        )

        self.assertContains(self.get(claim=claim), "Sent in game")

    def test_items_are_listed_by_slot_with_names_from_the_sde(self):
        killmail = f.srp_killmail(
            f.main_of(self.member),
            items=[
                {"flag": 27, "item_type_id": 2873, "quantity_destroyed": 1},
                {"flag": 19, "item_type_id": 5973, "quantity_dropped": 1},
                {"flag": 5, "item_type_id": 34, "quantity_dropped": 5000,
                 "items": [{"flag": 5, "item_type_id": 35, "quantity_destroyed": 3}]},
            ],
        )
        claim = f.srp_claim(self.member, killmail=killmail)
        self.patch("fleetops.services.sde.item_type_names", return_value={2873: "125mm Gatling AutoCannon I"})

        response = self.get(claim=claim)

        self.assertContains(response, "High slots")
        self.assertContains(response, "125mm Gatling AutoCannon I")
        self.assertContains(response, "Mid slots")
        self.assertContains(response, "Type 5973")
        self.assertContains(response, "Cargo and other")
        self.assertContains(response, "Type 35")
        self.assertContains(response, "&times;5000 dropped")

    def test_unreadable_items_do_not_break_the_page(self):
        killmail = f.srp_killmail(f.main_of(self.member), items=[None, "x", {"item_type_id": "a"}, {"flag": 5}])
        claim = f.srp_claim(self.member, killmail=killmail)

        self.assertEqual(self.get(claim=claim).status_code, 200)

    def test_claim_with_an_empty_calculation_still_renders(self):
        claim = f.srp_claim(self.member, calculation={}, policy={}, warnings=[{"code": "mystery", "params": []}, "junk"])

        response = self.get(claim=claim)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Mystery")


class WithdrawAndResubmitTests(SrpPagesTestCase):
    def setUp(self):
        super().setUp()
        self.claim = f.srp_claim(self.member, time=self.loss_time)
        self.withdraw = self.patch("fleetops.srp.claims.withdraw_claim")
        self.resubmit = self.patch("fleetops.srp.claims.resubmit_claim")

    def test_withdraw_passes_the_reason_and_comes_back_to_the_claim(self):
        response = self.client.post(route("srp_claim_withdraw", self.claim.uuid), {"reason": "Wrong ship"})

        self.assertRedirects(response, route("srp_claim", self.claim.uuid), fetch_redirect_response=False)
        self.withdraw.assert_called_once()
        args, kwargs = self.withdraw.call_args
        self.assertEqual(args[0].pk, self.claim.pk)
        self.assertEqual((kwargs["actor"], kwargs["reason"]), (self.member, "Wrong ship"))
        self.assertIn(f"{self.claim.reference} withdrawn.", " ".join(messages_of(response)))

    def test_withdraw_needs_no_reason(self):
        self.client.post(route("srp_claim_withdraw", self.claim.uuid))

        self.assertEqual(self.withdraw.call_args.kwargs["reason"], "")

    def test_a_claim_that_is_no_longer_pending_is_left_alone_with_a_message(self):
        self.withdraw.side_effect = SrpStateError("state", "This claim is already approved.")

        response = self.client.post(route("srp_claim_withdraw", self.claim.uuid))

        self.assertRedirects(response, route("srp_claim", self.claim.uuid), fetch_redirect_response=False)
        self.assertEqual(messages_of(response), ["This claim is already approved."])

    def test_resubmit_asks_the_service_and_comes_back_to_the_claim(self):
        response = self.client.post(route("srp_claim_resubmit", self.claim.uuid))

        self.assertRedirects(response, route("srp_claim", self.claim.uuid), fetch_redirect_response=False)
        self.assertEqual(self.resubmit.call_args.kwargs, {"actor": self.member})

    def test_resubmit_error_is_shown(self):
        self.resubmit.side_effect = SrpError("deadline", "Losses older than 14 days cannot be submitted.")

        response = self.client.post(route("srp_claim_resubmit", self.claim.uuid))

        self.assertEqual(messages_of(response), ["Losses older than 14 days cannot be submitted."])

    def test_only_the_claimant_may_withdraw_or_resubmit(self):
        for user in (self.other, self.manager):
            client = self.client_for(user)
            for name in ("srp_claim_withdraw", "srp_claim_resubmit"):
                with self.subTest(user=user.username, route=name):
                    self.assertEqual(client.post(route(name, self.claim.uuid)).status_code, 404)
        self.withdraw.assert_not_called()
        self.resubmit.assert_not_called()

    def test_a_get_is_refused(self):
        self.assertEqual(self.client.get(route("srp_claim_withdraw", self.claim.uuid)).status_code, 405)


class QueryBudgetTests(SrpPagesTestCase):
    def test_my_claims_cost_the_same_number_of_queries_whatever_the_number_of_claims(self):
        self.patch_home_services()
        now = timezone.now()
        f.srp_claim(self.member, time=now)

        def cost():
            with CaptureQueriesContext(connection) as queries:
                self.assertEqual(self.client.get(route("srp_home")).status_code, 200)
            return len(queries)

        cost()  # warm the caches of the first request
        small = cost()
        for index in range(20):
            f.srp_claim(self.member, time=now - timedelta(minutes=index + 1))

        self.assertEqual(cost(), small)

    def test_claim_page_does_not_query_per_event(self):
        claim = f.srp_claim(self.member, time=self.loss_time)
        self.patch("fleetops.services.sde.item_type_names", return_value={})

        def cost():
            with CaptureQueriesContext(connection) as queries:
                self.assertEqual(self.client.get(self.claim_url(claim)).status_code, 200)
            return len(queries)

        cost()
        small = cost()
        for index in range(15):
            f.claim_event(claim, "submit", actor=self.other, reason=f"Event {index}")

        self.assertEqual(cost(), small)
