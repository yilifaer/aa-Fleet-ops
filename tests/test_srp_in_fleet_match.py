# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""A loss inside a tracked fleet that is entered outside a fleet (setting ``in_fleet_match_action``): the switch to
the fleet link, the block, the plain hint, the closed link, the token and the reason of "outside the fleet anyway"."""

from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.core import signing
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from fleetops.models import AuditLog, FleetMemberEvent, SrpClaim, SrpClaimEvent, SrpSettings
from fleetops.srp import claims, display
from fleetops.srp.types import SrpError

from . import factories as f
from .test_srp_claims import ClaimBase

IN_FLEET = SrpClaim.Scope.IN_FLEET
OUT_OF_FLEET = SrpClaim.Scope.OUT_OF_FLEET
Action = SrpSettings.InFleetMatchAction


def codes(warnings):
    return [warning.code for warning in warnings]


class MatchBase(ClaimBase):
    """A pilot who was in a running fleet 10 minutes ago; losses outside a fleet are capped at 50m, in a fleet not."""

    mode = Action.AUTO

    def settings(self, **values):
        # The factory keeps the older tests on "suggest"; these tests keep the mode they chose in each change.
        if "in_fleet_match_action" in values:
            self.mode = values["in_fleet_match_action"]
        values.setdefault("in_fleet_match_action", self.mode)
        return super().settings(**values)

    def setUp(self):
        super().setUp()
        self.settings(in_fleet_match_action=Action.AUTO, out_of_fleet_loss_cap=50_000_000)
        self.operation, self.link = self.fleet(started_at=timezone.now() - timedelta(minutes=50))
        self.km = self.killmail(time=timezone.now() - timedelta(minutes=10))

    def event(self, claim):
        return claim.events.get(action=SrpClaimEvent.Action.SUBMIT)


class DefaultTests(TestCase):
    def test_a_new_site_switches_automatically(self):
        self.assertEqual(SrpSettings.get_solo().in_fleet_match_action, "auto")
        self.assertEqual(SrpSettings._meta.get_field("in_fleet_match_action").default, Action.AUTO)


class AutoSwitchTests(MatchBase):
    def test_the_preview_is_calculated_with_the_fleet_link(self):
        preview = self.prepare(self.km)

        self.assertEqual((preview.scope, preview.link), (IN_FLEET, self.link))
        self.assertTrue(preview.switched)
        self.assertEqual(preview.match_code, display.IN_FLEET)
        self.assertEqual(preview.presence.code, display.IN_FLEET)
        # The in-fleet rules: no 50m cap for a loss outside a fleet.
        self.assertEqual(preview.calculation.suggested, 80_000_000)
        self.assertIsNone(preview.suggested_link)
        self.assertTrue(preview.eligible)
        self.assertTrue(preview.token)

    def test_the_claim_is_in_the_fleet_and_the_event_says_it_was_switched(self):
        claim = self.submit(self.km)

        self.assertEqual((claim.scope, claim.link, claim.operation), (IN_FLEET, self.link, self.operation))
        self.assertEqual(claim.suggested_amount, 80_000_000)
        data = self.event(claim).data
        self.assertTrue(data["auto_in_fleet"])
        self.assertEqual(data["match"], display.IN_FLEET)
        self.assertEqual(data["scope"], IN_FLEET)
        self.assertEqual(data["link"], self.link.code)
        self.assertNotIn("forced_out_of_fleet", data)

    def test_a_pilot_who_was_only_near_the_fleet_is_switched_too(self):
        leave = timezone.now() - timedelta(minutes=20)
        FleetMemberEvent.objects.create(
            operation=self.operation,
            character_id=self.main.character_id,
            character_name=self.main.character_name,
            event_type=FleetMemberEvent.EventType.LEAVE,
        )
        FleetMemberEvent.objects.filter(operation=self.operation, event_type=FleetMemberEvent.EventType.LEAVE).update(
            created_at=leave
        )
        km = self.killmail(time=leave + timedelta(seconds=30))

        preview = self.prepare(km)

        self.assertEqual(preview.match_code, display.NEAR)
        self.assertEqual((preview.scope, preview.link), (IN_FLEET, self.link))

    def test_a_closed_fleet_link_does_not_switch_and_the_page_says_so(self):
        self.link.is_open = False
        self.link.save()

        preview = self.prepare(self.km)

        self.assertEqual((preview.scope, preview.link), (OUT_OF_FLEET, None))
        self.assertFalse(preview.switched)
        self.assertEqual(preview.closed_link, self.link)
        self.assertIsNone(preview.suggested_link)
        self.assertEqual(self.submit(self.km, preview=preview).scope, OUT_OF_FLEET)

    def test_a_registration_alone_does_not_switch(self):
        self.link.is_open = False
        self.link.save()
        pap = f.create_operation(
            self.fc, link_only=True, is_manual=True, tracking_enabled=False,
            started_at=timezone.now() - timedelta(minutes=50),
        )
        f.fleet_link(pap)
        f.registration(pap, self.main)

        preview = self.prepare(self.km)

        self.assertEqual((preview.scope, preview.link), (OUT_OF_FLEET, None))
        self.assertIsNone(preview.suggested_link)
        self.assertFalse(preview.switched)

    def test_a_loss_from_another_time_is_not_switched(self):
        km = self.killmail(time=timezone.now() - timedelta(days=2))

        self.assertEqual(self.prepare(km).scope, OUT_OF_FLEET)

    def test_a_loss_of_somebody_else_shows_nothing_of_the_fleet(self):
        preview = self.prepare(self.km, user=self.other)

        self.assertIn("not on your alliance auth account", " ".join(preview.blockers).lower())
        self.assertFalse(preview.switched)
        self.assertIsNone(preview.suggested_link)
        self.assertIsNone(preview.closed_link)
        self.assertEqual(preview.scope, OUT_OF_FLEET)

    def test_a_fleet_the_pilot_cannot_claim_through_changes_nothing(self):
        # The fleet link must pass the checks of a fleet loss: here a ship that is blocked for everybody is refused
        # in both ways, so the preview is the plain one with its own blockers.
        self.settings(blocked_ship_type_ids=str(self.km.ship_type_id))

        preview = self.prepare(self.km)

        self.assertFalse(preview.switched)
        self.assertEqual(preview.scope, OUT_OF_FLEET)
        self.assertTrue(preview.blockers)

    def test_a_standing_link_chosen_by_the_member_is_never_switched(self):
        standing = f.standing_link(category=f.srp_category("Home defense"))

        preview = self.prepare(self.km, link=standing)

        self.assertFalse(preview.switched)
        self.assertEqual(preview.scope, OUT_OF_FLEET)
        # Only the hint of 0.3.0b1.
        self.assertEqual(preview.suggested_link, self.link)

    def test_a_loss_entered_through_a_link_is_not_touched(self):
        other_op, other_link = self.fleet(started_at=timezone.now() - timedelta(hours=3))

        preview = self.prepare(self.km, link=other_link)

        self.assertEqual(preview.link, other_link)
        self.assertFalse(preview.switched)

    def test_the_token_holds_the_final_link_and_cannot_be_edited(self):
        preview = self.prepare(self.km)
        data = signing.loads(preview.token, salt=claims.PREVIEW_SALT)
        self.assertEqual((data["link"], data["auto"]), (self.link.code, display.IN_FLEET))

        forged = signing.dumps({**data, "link": "no-such-link"}, salt="some.other.salt")
        with self.assertRaises(SrpError) as caught:
            claims.submit_claim(self.user, forged)
        self.assertEqual(caught.exception.code, "invalid_token")
        with self.assertRaises(SrpError):
            claims.submit_claim(self.user, preview.token[:-3] + "xyz")
        # A token of another member is refused too.
        with self.assertRaises(SrpError):
            claims.submit_claim(self.other, preview.token)

    def test_a_fleet_link_in_a_token_that_is_not_for_the_member_is_refused(self):
        # Hand-made token fields cannot give a claim through a link the member may not use: the submit step checks
        # the eligibility for the link again (here the member has no such character).
        km = self.killmail(character=f.main_of(self.other), time=timezone.now() - timedelta(minutes=10))
        token = signing.dumps(
            {"km": km.pk, "link": self.link.code, "u": self.user.pk, "auto": display.IN_FLEET}, salt=claims.PREVIEW_SALT
        )

        with self.assertRaises(SrpError):
            claims.submit_claim(self.user, token)

        self.assertFalse(SrpClaim.objects.filter(killmail=km).exists())


class ClosedBetweenPreviewAndSubmitTests(MatchBase):
    def test_a_link_closed_after_the_preview_gives_an_out_of_fleet_claim_with_a_warning(self):
        preview = self.prepare(self.km)
        self.assertEqual(preview.scope, IN_FLEET)
        self.link.is_open = False
        self.link.save()

        claim = claims.submit_claim(self.user, preview.token)

        self.assertEqual((claim.scope, claim.link, claim.operation), (OUT_OF_FLEET, None, None))
        self.assertIn("fleet_link_closed", [warning["code"] for warning in claim.warnings])
        # Calculated as a loss outside a fleet: the 50m cap.
        self.assertEqual(claim.suggested_amount, 50_000_000)
        data = self.event(claim).data
        self.assertNotIn("auto_in_fleet", data)
        self.assertEqual(data["scope"], OUT_OF_FLEET)
        self.assertIn("was closed before this claim was submitted", display.warning_text("fleet_link_closed"))

    def test_a_plain_fleet_token_with_a_closed_link_is_still_an_error(self):
        # Only a switched loss falls back; a loss entered through the link of a closed fleet is refused as before.
        preview = self.prepare(self.km, link=self.link)
        self.link.is_open = False
        self.link.save()

        with self.assertRaises(SrpError):
            claims.submit_claim(self.user, preview.token)


class ClosedLinkFallbackSettingsTests(MatchBase):
    def close(self):
        self.link.is_open = False
        self.link.save()

    def test_with_out_of_fleet_claims_off_the_fallback_is_refused_as_such(self):
        # Documented behaviour: the fallback follows the rules of a loss outside a fleet, so it is refused there.
        self.settings(out_of_fleet_enabled=False)
        preview = self.prepare(self.km)
        self.assertEqual(preview.scope, IN_FLEET)
        self.close()

        with self.assertRaises(SrpError) as caught:
            claims.submit_claim(self.user, preview.token)

        self.assertEqual(caught.exception.code, "not_eligible")
        self.assertFalse(SrpClaim.objects.filter(killmail=self.km).exists())

    def test_a_required_category_that_was_not_chosen_does_not_stop_the_fallback(self):
        self.settings(out_of_fleet_category_required=True, default_category=None)
        preview = self.prepare(self.km)
        self.assertEqual(preview.scope, IN_FLEET)
        self.assertTrue(preview.eligible)
        self.close()

        claim = claims.submit_claim(self.user, preview.token)

        self.assertEqual((claim.scope, claim.link), (OUT_OF_FLEET, None))
        self.assertIn("fleet_link_closed", [warning["code"] for warning in claim.warnings])


class OutOfFleetWayOutChecksTests(MatchBase):
    def test_a_required_category_withdraws_the_way_out_and_says_why(self):
        self.settings(out_of_fleet_category_required=True, default_category=None)

        preview = self.prepare(self.km)

        self.assertTrue(preview.eligible)
        self.assertEqual(preview.forced_token, "")
        self.assertTrue(preview.forced_blocker)

    def test_a_blocker_of_the_scope_outside_a_fleet_withdraws_the_way_out(self):
        real = claims._checks

        def fake(user, km, srp, now, *, link, chosen, missing_alts):
            result = real(user, km, srp, now, link=link, chosen=chosen, missing_alts=missing_alts)
            if link is None:
                result.blockers.append("Out of fleet blocker")
            return result

        with mock.patch.object(claims, "_checks", side_effect=fake):
            preview = self.prepare(self.km)

        self.assertTrue(preview.eligible)
        self.assertEqual(preview.forced_token, "")
        self.assertEqual(preview.forced_blocker, "Out of fleet blocker")
        self.assertTrue(preview.token)

    def test_the_page_shows_the_reason_instead_of_the_form(self):
        self.settings(out_of_fleet_category_required=True, default_category=None)
        self.client.force_login(self.user)
        with mock.patch.object(claims.killmails, "fetch_killmail", return_value=self.km):
            response = self.client.post(reverse("fleetops:srp_check"), {"killmail": self.text(self.km)})

        self.assertContains(response, 'id="srp-out-of-fleet-refused"')
        self.assertNotContains(response, 'name="out_of_fleet_reason"')

    def test_with_a_chosen_category_the_way_out_is_offered_and_works(self):
        self.settings(out_of_fleet_category_required=True, default_category=None)
        category = f.srp_category()
        with mock.patch.object(claims.killmails, "fetch_killmail", return_value=self.km):
            preview = claims.prepare_claim(self.user, self.text(self.km), category=category)
        self.assertTrue(preview.forced_token)

        claim = claims.submit_claim(self.user, preview.forced_token, out_of_fleet_reason="why")

        self.assertEqual((claim.scope, claim.category), (OUT_OF_FLEET, category))


class StaleTokenAgainstBlockTests(MatchBase):
    def test_a_token_from_suggest_mode_is_refused_after_the_switch_to_block(self):
        self.settings(in_fleet_match_action=Action.SUGGEST)
        preview = self.prepare(self.km)
        self.assertEqual(preview.scope, OUT_OF_FLEET)
        self.settings(in_fleet_match_action=Action.BLOCK)

        with self.assertRaises(SrpError) as caught:
            claims.submit_claim(self.user, preview.token)

        self.assertEqual(caught.exception.code, "not_eligible")
        self.assertIn("SRP link of that fleet", caught.exception.message)
        self.assertFalse(SrpClaim.objects.filter(killmail=self.km).exists())

    def test_a_forced_token_is_refused_after_the_switch_to_block(self):
        preview = self.prepare(self.km)
        self.assertTrue(preview.forced_token)
        self.settings(in_fleet_match_action=Action.BLOCK)

        with self.assertRaises(SrpError):
            claims.submit_claim(self.user, preview.forced_token, out_of_fleet_reason="why")

        self.assertFalse(SrpClaim.objects.filter(killmail=self.km).exists())

    def test_a_loss_outside_every_fleet_is_not_affected_by_block(self):
        self.link.is_open = False
        self.link.save()
        self.settings(in_fleet_match_action=Action.SUGGEST)
        preview = self.prepare(self.km)
        self.settings(in_fleet_match_action=Action.BLOCK)

        claim = claims.submit_claim(self.user, preview.token)

        self.assertEqual(claim.scope, OUT_OF_FLEET)


class OutOfFleetAnywayTests(MatchBase):
    def test_the_preview_offers_a_second_token_that_needs_a_reason(self):
        preview = self.prepare(self.km)

        self.assertTrue(preview.forced_token)
        with self.assertRaises(SrpError) as caught:
            claims.submit_claim(self.user, preview.forced_token)
        self.assertEqual(caught.exception.code, "reason_required")
        with self.assertRaises(SrpError):
            claims.submit_claim(self.user, preview.forced_token, out_of_fleet_reason="   ")
        self.assertFalse(SrpClaim.objects.filter(killmail=self.km).exists())

    def test_with_a_reason_the_claim_is_out_of_fleet_and_the_event_keeps_the_reason(self):
        preview = self.prepare(self.km)

        claim = claims.submit_claim(self.user, preview.forced_token, out_of_fleet_reason="I was in a scout alt fleet")

        self.assertEqual((claim.scope, claim.link), (OUT_OF_FLEET, None))
        self.assertEqual(claim.suggested_amount, 50_000_000)
        data = self.event(claim).data
        self.assertTrue(data["forced_out_of_fleet"])
        self.assertEqual(data["forced_reason"], "I was in a scout alt fleet")
        self.assertNotIn("auto_in_fleet", data)

    def test_a_reason_that_is_too_long_is_refused(self):
        preview = self.prepare(self.km)

        with self.assertRaises(SrpError) as caught:
            claims.submit_claim(self.user, preview.forced_token, out_of_fleet_reason="x" * 1001)
        self.assertEqual(caught.exception.code, "reason_too_long")

    def test_the_normal_token_ignores_a_reason(self):
        preview = self.prepare(self.km)

        claim = claims.submit_claim(self.user, preview.token, out_of_fleet_reason="not asked")

        self.assertEqual(claim.scope, IN_FLEET)
        self.assertNotIn("forced_out_of_fleet", self.event(claim).data)

    def test_without_out_of_fleet_claims_there_is_no_way_out(self):
        self.settings(out_of_fleet_enabled=False)

        preview = self.prepare(self.km)

        self.assertEqual(preview.scope, IN_FLEET)
        self.assertEqual(preview.forced_token, "")


class BlockTests(MatchBase):
    def setUp(self):
        super().setUp()
        self.settings(in_fleet_match_action=Action.BLOCK)

    def test_the_loss_cannot_be_submitted_outside_the_fleet(self):
        preview = self.prepare(self.km)

        self.assertFalse(preview.eligible)
        self.assertEqual(preview.token, "")
        self.assertEqual(preview.forced_token, "")
        self.assertEqual(preview.scope, OUT_OF_FLEET)
        self.assertEqual(preview.suggested_link, self.link)
        self.assertEqual(preview.match_action, "block")
        [blocker] = [text for text in preview.blockers if "SRP link of that fleet" in text]
        self.assertIn(self.operation.fleet_type.name, blocker)

    def test_the_link_of_the_fleet_works_as_usual(self):
        claim = self.submit(self.km, link=self.link)

        self.assertEqual((claim.scope, claim.link), (IN_FLEET, self.link))

    def test_a_closed_link_blocks_nothing(self):
        self.link.is_open = False
        self.link.save()

        preview = self.prepare(self.km)

        self.assertTrue(preview.eligible)
        self.assertEqual(preview.scope, OUT_OF_FLEET)
        self.assertEqual(preview.closed_link, self.link)


class BlockWithRequiredCategoryTests(MatchBase):
    def test_the_fleet_link_instruction_comes_before_the_category_message(self):
        self.settings(in_fleet_match_action=Action.BLOCK, out_of_fleet_category_required=True, default_category=None)

        preview = self.prepare(self.km)

        self.assertFalse(preview.eligible)
        self.assertEqual(preview.suggested_link, self.link)
        self.assertTrue([text for text in preview.blockers if "SRP link of that fleet" in text])


class SuggestTests(MatchBase):
    def setUp(self):
        super().setUp()
        self.settings(in_fleet_match_action=Action.SUGGEST)

    def test_only_a_hint_as_in_the_earlier_version(self):
        preview = self.prepare(self.km)

        self.assertEqual((preview.scope, preview.link, preview.suggested_link), (OUT_OF_FLEET, None, self.link))
        self.assertTrue(preview.eligible)
        self.assertFalse(preview.switched)
        self.assertEqual(preview.forced_token, "")
        self.assertEqual(preview.calculation.suggested, 50_000_000)
        claim = self.submit(self.km, preview=preview)
        self.assertEqual(claim.scope, OUT_OF_FLEET)
        data = self.event(claim).data
        self.assertNotIn("auto_in_fleet", data)
        self.assertNotIn("forced_out_of_fleet", data)

    def test_a_closed_link_is_not_looked_for(self):
        self.link.is_open = False
        self.link.save()

        preview = self.prepare(self.km)

        self.assertIsNone(preview.closed_link)
        self.assertIsNone(preview.suggested_link)


class QueryCountTests(MatchBase):
    def count(self, km):
        with CaptureQueriesContext(connection) as queries:
            self.prepare(km)
        return len(queries)

    def test_more_fleets_around_the_loss_do_not_add_queries(self):
        first = self.count(self.killmail(time=timezone.now() - timedelta(minutes=10)))
        for _index in range(3):
            self.fleet(started_at=timezone.now() - timedelta(minutes=55))
        second = self.count(self.killmail(time=timezone.now() - timedelta(minutes=10)))

        # Only the three latest fleets are looked at; the candidate fleets are loaded in one query.
        self.assertLessEqual(second - first, 6)
        for _index in range(3):
            self.fleet(started_at=timezone.now() - timedelta(minutes=56))
        third = self.count(self.killmail(time=timezone.now() - timedelta(minutes=10)))
        self.assertEqual(third, second)

    def test_the_switch_costs_a_few_queries_more_than_the_hint(self):
        auto = self.count(self.killmail(time=timezone.now() - timedelta(minutes=10)))
        self.settings(in_fleet_match_action=Action.SUGGEST)
        suggest = self.count(self.killmail(time=timezone.now() - timedelta(minutes=10)))

        self.assertLess(auto - suggest, 25)


class PageTests(MatchBase):
    def check(self, km=None, user=None):
        self.client.force_login(user or self.user)
        km = km or self.km
        with mock.patch.object(claims.killmails, "fetch_killmail", return_value=km):
            return self.client.post(reverse("fleetops:srp_check"), {"killmail": self.text(km)})

    def test_auto_states_the_fleet_and_offers_the_way_out(self):
        response = self.check()

        self.assertContains(response, 'id="srp-switched-note"')
        self.assertContains(response, "so it is calculated with the SRP link of that fleet")
        self.assertContains(response, self.operation.fleet_type.name)
        self.assertContains(response, "Submit as a loss outside a fleet anyway")
        self.assertContains(response, "Submit claim")
        # The reason field is required in the page.
        self.assertContains(response, 'name="out_of_fleet_reason"')
        self.assertContains(response, "required")

    def test_block_has_no_submit_button_and_the_link_button_instead(self):
        self.settings(in_fleet_match_action=Action.BLOCK)

        response = self.check()

        self.assertContains(response, "This loss cannot be submitted.")
        self.assertContains(response, "Submit with the link of")
        self.assertNotContains(response, "Submit claim")
        self.assertNotContains(response, 'name="out_of_fleet_reason"')

    def test_suggest_shows_the_old_question(self):
        self.settings(in_fleet_match_action=Action.SUGGEST)

        response = self.check()

        self.assertContains(response, "Submit it as a fleet loss?")
        self.assertNotContains(response, 'id="srp-switched-note"')

    def test_a_closed_link_is_explained(self):
        self.link.is_open = False
        self.link.save()

        response = self.check()

        self.assertContains(response, 'id="srp-closed-fleet-note"')
        self.assertContains(response, "the SRP link of that fleet is closed")
        self.assertContains(response, "Submit claim")

    def test_the_submit_view_passes_the_reason_and_the_review_page_shows_it(self):
        preview = self.prepare(self.km)
        self.client.force_login(self.user)

        response = self.client.post(
            reverse("fleetops:srp_submit"),
            {"token": preview.forced_token, "comment": "", "out_of_fleet_reason": "Scouting for a different fleet"},
        )

        claim = SrpClaim.objects.get(killmail=self.km)
        self.assertRedirects(response, reverse("fleetops:srp_claim", args=[claim.uuid]), fetch_redirect_response=False)
        for viewer in (self.user, self.manager):
            self.client.force_login(viewer)
            page = self.client.get(reverse("fleetops:srp_claim", args=[claim.uuid]))
            self.assertContains(page, "Submitted as a loss outside a fleet although it happened in a tracked fleet")
            self.assertContains(page, "Scouting for a different fleet")

    def test_a_missing_reason_goes_back_with_a_message(self):
        preview = self.prepare(self.km)
        self.client.force_login(self.user)

        response = self.client.post(
            reverse("fleetops:srp_submit"), {"token": preview.forced_token, "comment": ""}, follow=True
        )

        self.assertContains(response, "Give a reason.")
        self.assertFalse(SrpClaim.objects.filter(killmail=self.km).exists())

    def test_the_claim_page_says_a_switched_claim_was_entered_outside_a_fleet(self):
        claim = self.submit(self.km)
        self.client.force_login(self.manager)

        page = self.client.get(reverse("fleetops:srp_claim", args=[claim.uuid]))

        self.assertContains(page, "Entered outside a fleet; submitted with the SRP link of the fleet")


class SettingFormTests(TestCase):
    def setUp(self):
        self.admin = f.create_user("match_admin", perms=f.MEMBER_PERMS + ["fleetops.manage_configuration"])
        self.client.force_login(self.admin)
        self.url = reverse("fleetops:configuration_list", args=["srp"])

    def post(self, **extra):
        from .test_srp_config import settings_post

        return self.client.post(self.url, settings_post(**extra))

    def test_the_page_offers_the_three_choices_and_the_default(self):
        response = self.client.get(self.url)

        form = response.context["form"]
        self.assertEqual(form["in_fleet_match_action"].value(), "auto")
        for label in (
            "Switch to the fleet link automatically",
            "Block it and ask for the fleet link",
            "Only suggest the fleet link",
            "Losses inside a tracked fleet",
        ):
            self.assertContains(response, label)

    def test_each_choice_is_saved_and_audited(self):
        previous = "auto"
        for value in ("block", "suggest", "auto"):
            with self.subTest(value=value):
                response = self.post(in_fleet_match_action=value)
                self.assertEqual(response.status_code, 302)
                self.assertEqual(SrpSettings.get_solo().in_fleet_match_action, value)
                entry = AuditLog.objects.filter(action="configuration.update", object_type="SrpSettings").latest("pk")
                self.assertEqual(entry.old_value["in_fleet_match_action"], previous)
                self.assertEqual(entry.new_value["in_fleet_match_action"], value)
                previous = value

    def test_an_unknown_value_is_refused(self):
        response = self.post(in_fleet_match_action="always")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["form"].errors["in_fleet_match_action"])
        self.assertEqual(SrpSettings.get_solo().in_fleet_match_action, "auto")

    def test_a_post_without_the_field_keeps_the_stored_choice(self):
        f.srp_settings(in_fleet_match_action="block")
        from .test_srp_config import settings_post

        data = settings_post()
        data.pop("in_fleet_match_action", None)
        response = self.client.post(self.url, data)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(SrpSettings.get_solo().in_fleet_match_action, "block")
