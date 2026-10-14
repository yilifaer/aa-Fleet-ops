"""The reviewer pages of FleetOps SRP: the review queue, the decisions on a claim and the payments list.

The services that decide on claims (approving, paying, recalculating) have their own tests; the pages are tested with those
services replaced by mocks, so the tests check what the pages hand to them and what they show of the answer.
"""

from datetime import date, datetime, timedelta, timezone as dt_timezone

from allianceauth.authentication.models import CharacterOwnership, UserProfile
from django.db import connection
from django.test import SimpleTestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from fleetops.models import SrpClaim, SrpSettings
from fleetops.srp import review
from fleetops.srp.types import BulkResult, Calculation, ClaimWarning, LimitState, SrpError, SrpLimitError, SrpStateError
from fleetops.views_srp_review import day_bounds

from . import factories as f
from .test_srp_views import SrpPagesTestCase, messages_of, route

UTC = dt_timezone.utc
PENDING = SrpClaim.Status.PENDING
APPROVED = SrpClaim.Status.APPROVED
PAID = SrpClaim.Status.PAID
REJECTED = SrpClaim.Status.REJECTED
WITHDRAWN = SrpClaim.Status.WITHDRAWN


def refs(response):
    """The claims of the queue in the order shown."""
    return [claim.reference for claim in response.context["claims"]]


class ManagerPagesTestCase(SrpPagesTestCase):
    def setUp(self):
        super().setUp()
        self.client = self.client_for(self.manager)

    def pending(self, user=None, **extra):
        return f.srp_claim(user or self.member, time=extra.pop("time", self.loss_time), **extra)


class ReviewQueueTests(ManagerPagesTestCase):
    def test_only_managers_open_the_queue(self):
        for user, expected in ((self.member, 403), (self.stats, 403), (self.fc, 403), (self.manager, 200)):
            with self.subTest(user=user.username):
                self.assertEqual(self.client_for(user).get(route("srp_review")).status_code, expected)

    def test_default_list_is_the_pending_claims_oldest_first(self):
        now = timezone.now()
        newer = self.pending(submitted_at=now - timedelta(hours=1))
        older = self.pending(self.other, submitted_at=now - timedelta(hours=5))
        f.srp_claim(self.member, status=APPROVED, time=self.loss_time)
        f.srp_claim(self.member, status=REJECTED, time=self.loss_time)

        response = self.client.get(route("srp_review"))

        self.assertEqual(refs(response), [older.reference, newer.reference])
        self.assertEqual(response.context["filter_form"]["status"].value(), "pending")

    def test_any_status_lists_the_history_newest_first(self):
        now = timezone.now()
        first = self.pending(submitted_at=now - timedelta(hours=5))
        second = f.srp_claim(self.other, status=APPROVED, time=self.loss_time, submitted_at=now - timedelta(hours=1))

        response = self.client.get(route("srp_review"), {"status": "all"})

        self.assertEqual(refs(response), [second.reference, first.reference])

    def test_status_filter(self):
        pending = self.pending()
        approved = f.srp_claim(self.other, status=APPROVED, time=self.loss_time)
        paid = f.srp_claim(self.member, status=PAID, decided_by=self.manager2, time=self.loss_time)

        for status, expected in (("approved", [approved]), ("paid", [paid]), ("pending", [pending])):
            with self.subTest(status=status):
                response = self.client.get(route("srp_review"), {"status": status})
                self.assertEqual(refs(response), [claim.reference for claim in expected])

    def test_scope_filter(self):
        in_fleet = self.pending(link=self.link)
        out = self.pending(self.other)

        self.assertEqual(refs(self.client.get(route("srp_review"), {"scope": "in_fleet"})), [in_fleet.reference])
        self.assertEqual(refs(self.client.get(route("srp_review"), {"scope": "out_of_fleet"})), [out.reference])

    def test_flag_filters(self):
        plain = self.pending()
        over = self.pending(over_limit=True)
        far = self.pending(link=self.link, fleet_check="not_in_fleet")
        fine = self.pending(link=self.link, fleet_check="in_fleet")
        estimate = self.pending(value_source="esi_estimate")
        estimate_warning = self.pending(warnings=[ClaimWarning("value_estimate").as_json()])
        elsewhere = self.pending(warnings=[ClaimWarning("external_claim", params={"claims": ["aa-srp: Pending"]}).as_json()])

        def listed(flag):
            return set(refs(self.client.get(route("srp_review"), {"flag": flag})))

        self.assertEqual(listed("over_limit"), {over.reference})
        self.assertEqual(listed("fleet_check"), {far.reference})
        self.assertEqual(listed("estimate"), {estimate.reference, estimate_warning.reference})
        self.assertEqual(listed("external"), {elsewhere.reference})
        self.assertNotIn(plain.reference, listed("over_limit"))
        self.assertNotIn(fine.reference, listed("fleet_check"))

    def test_corporation_and_fc_filters(self):
        outsider = f.create_user("outsider", perms=f.SRP_MEMBER_PERMS, corporation=f.OTHER_CORP)
        mine = self.pending(link=self.link)
        theirs = self.pending(outsider)

        by_corporation = self.client.get(route("srp_review"), {"corporation": f.OTHER_CORP[0]})
        by_fc = self.client.get(route("srp_review"), {"fc": self.fc.pk})

        self.assertEqual(refs(by_corporation), [theirs.reference])
        self.assertEqual(refs(by_fc), [mine.reference])

    def test_filter_choices_are_the_corporations_and_fcs_that_have_claims(self):
        self.pending(link=self.link)

        form = self.client.get(route("srp_review")).context["filter_form"]

        self.assertIn((str(f.DEFAULT_CORP[0]), f.DEFAULT_CORP[1]), form.fields["corporation"].choices)
        self.assertNotIn(str(f.OTHER_CORP[0]), [value for value, _label in form.fields["corporation"].choices])
        self.assertIn((str(self.fc.pk), self.fc.profile.main_character.character_name), form.fields["fc"].choices)

    def test_search_by_pilot_ship_and_reference(self):
        rifter = self.pending(self.member)
        other = self.pending(self.fc, killmail=f.srp_killmail(f.main_of(self.fc), ship_name="Muninn", ship_type_id=12015))

        def found(text):
            return set(refs(self.client.get(route("srp_review"), {"q": text})))

        self.assertEqual(found(f.main_of(self.member).character_name), {rifter.reference})
        self.assertEqual(found("SRPFC"), {other.reference})
        self.assertEqual(found("munin"), {other.reference})
        self.assertEqual(found(other.reference), {other.reference})
        self.assertEqual(found(str(rifter.pk)), {rifter.reference})
        self.assertEqual(found(str(other.killmail_id)), {other.reference})
        self.assertEqual(found("nothing like it"), set())

    def test_month_filter_applies_once_a_month_is_picked(self):
        now = timezone.now()
        current = self.pending(time=now - timedelta(hours=1))
        march = self.pending(time=datetime(2026, 3, 14, 12, 0, tzinfo=UTC))

        everything = self.client.get(route("srp_review"))
        in_march = self.client.get(route("srp_review"), {"year": "2026", "month": "3"})

        self.assertEqual(set(refs(everything)), {current.reference, march.reference})
        self.assertFalse(everything.context["month_filter_active"])
        self.assertContains(everything, "Showing the losses of all months.")
        self.assertEqual(refs(in_march), [march.reference])
        self.assertTrue(in_march.context["month_filter_active"])
        self.assertContains(in_march, "Show all months")

    def test_month_selector_keeps_the_filters(self):
        response = self.client.get(route("srp_review"), {"status": "all", "scope": "in_fleet", "q": "pilot"})

        self.assertContains(response, '<input type="hidden" name="status" value="all">')
        self.assertContains(response, '<input type="hidden" name="scope" value="in_fleet">')
        self.assertContains(response, '<input type="hidden" name="q" value="pilot">')

    def test_invalid_filters_are_ignored(self):
        # An unreadable year or month is the current month: the claim is of this month whatever the date is.
        claim = self.pending(time=timezone.now())

        response = self.client.get(
            route("srp_review"),
            {"status": "zzz", "scope": "zzz", "flag": "zzz", "corporation": "x", "fc": "-1", "year": "99999", "month": "13", "page": "abc"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(refs(response), [claim.reference])

    def test_red_and_amber_rows(self):
        red = self.pending(over_limit=True, limited_amount=1_000_000)
        amber = self.pending(link=self.link, fleet_check="never_seen")
        warned = self.pending(warnings=[ClaimWarning("value_pending").as_json()])
        clean = self.pending()

        classes = {claim.reference: classes_of(self.client.get(route("srp_review")), claim) for claim in (red, amber, warned, clean)}

        self.assertEqual(classes[red.reference], "fleetops-srp-over-limit")
        self.assertEqual(classes[amber.reference], "fleetops-srp-warning")
        self.assertEqual(classes[warned.reference], "fleetops-srp-warning")
        self.assertEqual(classes[clean.reference], "")

    def test_flags_are_written_out(self):
        self.pending(link=self.link, fleet_check="not_in_fleet",
                     warnings=[ClaimWarning("external_claim", params={"claims": ["aa-srp: Pending"]}).as_json()])

        response = self.client.get(route("srp_review"))

        self.assertContains(response, "Not in the fleet at the time of the loss.")
        self.assertContains(response, "Also claimed in aa-srp: Pending.")

    def test_a_fleet_check_problem_is_listed_once(self):
        self.pending(
            link=self.link, fleet_check="never_seen",
            warnings=[ClaimWarning("never_seen", "danger").as_json()],
        )

        html = self.client.get(route("srp_review")).content.decode()

        self.assertEqual(html.count("This character was never seen in the fleet."), 1)

    def test_every_tick_box_posts_the_suggested_amount_it_shows(self):
        claim = self.pending(amount=12_345_678)
        own = self.pending(self.manager)

        html = self.client.get(route("srp_review")).content.decode()

        self.assertIn(f'name="claim" value="{claim.uuid}"', html)
        self.assertIn(f'<input type="hidden" name="shown-{claim.uuid}" value="12345678">', html)
        self.assertNotIn(f'shown-{own.uuid}', html)

    def test_tick_boxes_are_for_pending_claims_the_manager_may_decide(self):
        theirs = self.pending()
        own = self.pending(self.manager)
        f.srp_claim(self.member, status=APPROVED, time=self.loss_time)

        response = self.client.get(route("srp_review"), {"status": "all"})

        self.assertContains(response, f'name="claim" value="{theirs.uuid}"')
        self.assertNotContains(response, f'name="claim" value="{own.uuid}"')
        self.assertContains(response, "Your own claim: another SRP manager has to decide on it.")
        self.assertEqual(response.content.decode().count('name="claim"'), 1)
        self.assertContains(response, f'action="{route("srp_review_bulk_approve")}"')
        self.assertContains(response, "Approve selected at the suggested amount")

    def test_own_claims_only_give_no_bulk_form(self):
        self.pending(self.manager)

        response = self.client.get(route("srp_review"))

        self.assertNotContains(response, route("srp_review_bulk_approve"))

    def test_self_review_setting_adds_tick_boxes_for_own_claims(self):
        own = self.pending(self.manager)
        SrpSettings.objects.filter(pk=1).update(allow_self_review=True)

        self.assertContains(self.client.get(route("srp_review")), f'name="claim" value="{own.uuid}"')

    def test_queue_is_paged_by_50(self):
        now = timezone.now()
        for index in range(52):
            self.pending(submitted_at=now - timedelta(minutes=index))

        first = self.client.get(route("srp_review"))
        second = self.client.get(route("srp_review"), {"page": "2"})

        self.assertEqual((len(first.context["claims"]), len(second.context["claims"])), (50, 2))
        self.assertContains(first, "Claims 1&ndash;50 of 52")

    def test_recalculate_button_asks_for_confirmation(self):
        response = self.client.get(route("srp_review"))

        self.assertContains(response, f'action="{route("srp_review_recalculate")}"')
        self.assertContains(response, "data-srp-confirm=")

    def test_queue_costs_the_same_number_of_queries_for_5_and_50_claims(self):
        now = timezone.now()

        def add(count):
            for index in range(count):
                user = self.member if index % 2 else self.other
                self.pending(user, link=self.link if index % 3 else None, submitted_at=now - timedelta(minutes=index),
                             fleet_check="near" if index % 4 == 0 else "", over_limit=index % 5 == 0)

        def cost():
            with CaptureQueriesContext(connection) as queries:
                self.assertEqual(self.client.get(route("srp_review"), {"status": "all"}).status_code, 200)
            return len(queries)

        add(5)
        cost()
        small = cost()
        add(45)

        self.assertEqual(cost(), small)
        # The framework needs about 21 of them (session, user, permissions, menu, saving the session).
        self.assertLessEqual(small, 30)

    def test_queue_page_loads_the_script(self):
        self.assertContains(self.client.get(route("srp_review")), "fleetops/srp.js?v=")


def classes_of(response, claim):
    """The class attribute of the table row of a claim."""
    html = response.content.decode()
    marker = f'href="{route("srp_claim", claim.uuid)}"'
    row_start = html.rfind("<tr", 0, html.index(marker))
    tag = html[row_start : html.index(">", row_start)]
    if 'class="' not in tag:
        return ""
    return tag.split('class="', 1)[1].split('"', 1)[0].strip()


class BulkApproveTests(ManagerPagesTestCase):
    def setUp(self):
        super().setUp()
        self.approve = self.patch("fleetops.srp.review.approve_clean_claims")

    def post(self, claims, **extra):
        return self.client.post(route("srp_review_bulk_approve"), {"claim": [str(claim.uuid) for claim in claims], **extra})

    def test_ticked_claims_go_to_the_service_and_the_result_is_reported(self):
        clean = self.pending()
        flagged = self.pending(self.other)
        self.approve.return_value = BulkResult(done=[clean], skipped=[(flagged, "it has warnings")])

        response = self.post([clean, flagged])

        self.assertRedirects(response, route("srp_review"), fetch_redirect_response=False)
        (claims_arg,), kwargs = self.approve.call_args
        self.assertEqual({claim.pk for claim in claims_arg}, {clean.pk, flagged.pk})
        self.assertEqual(kwargs, {"actor": self.manager, "expected_amounts": {}})
        text = messages_of(response)
        self.assertIn("1 claim approved at the suggested amount.", text)
        self.assertTrue(any(flagged.reference in line and "it has warnings" in line for line in text))

    def test_the_amounts_the_list_showed_go_to_the_service(self):
        shown, unshown, unticked = self.pending(), self.pending(self.other), self.pending(self.fc)
        self.approve.return_value = BulkResult(done=[shown, unshown])

        self.post(
            [shown, unshown],
            **{f"shown-{shown.uuid}": "500000", f"shown-{unticked.uuid}": "7", f"shown-{unshown.uuid}": "lots"},
        )

        # Only ticked claims with a readable amount; the others are approved as before.
        self.assertEqual(self.approve.call_args.kwargs["expected_amounts"], {shown.pk: 500_000})

    def test_own_claims_are_skipped_with_a_message(self):
        mine = self.pending(self.manager)
        theirs = self.pending()
        self.approve.return_value = BulkResult(done=[theirs])

        response = self.post([mine, theirs])

        (claims_arg,), _kwargs = self.approve.call_args
        self.assertEqual([claim.pk for claim in claims_arg], [theirs.pk])
        self.assertTrue(any(mine.reference in line and "your own claim" in line for line in messages_of(response)))

    def test_only_own_claims_never_reach_the_service(self):
        mine = self.pending(self.manager)

        response = self.post([mine])

        self.approve.assert_not_called()
        self.assertTrue(any("your own claim" in line for line in messages_of(response)))

    def test_self_review_setting_lets_own_claims_through(self):
        mine = self.pending(self.manager)
        SrpSettings.objects.filter(pk=1).update(allow_self_review=True)
        self.approve.return_value = BulkResult(done=[mine])

        self.post([mine])

        self.assertEqual([claim.pk for claim in self.approve.call_args.args[0]], [mine.pk])

    def test_nothing_ticked_is_an_error(self):
        response = self.client.post(route("srp_review_bulk_approve"), {})

        self.assertRedirects(response, route("srp_review"), fetch_redirect_response=False)
        self.assertIn("Select at least one claim.", messages_of(response))
        self.approve.assert_not_called()

    def test_ids_of_no_claim_are_an_error(self):
        import uuid

        response = self.client.post(route("srp_review_bulk_approve"), {"claim": [str(uuid.uuid4())]})

        self.assertIn("None of the selected claims exist any more.", messages_of(response))
        self.approve.assert_not_called()

    def test_long_lists_of_skipped_claims_are_cut_short(self):
        claims = [self.pending(self.other) for _ in range(8)]
        self.approve.return_value = BulkResult(skipped=[(claim, "it has warnings") for claim in claims])

        response = self.post(claims)

        skipped = [line for line in messages_of(response) if line.startswith("Skipped:")]
        self.assertEqual(len(skipped), 1)
        self.assertIn("and 3 more.", skipped[0])

    def test_return_to_the_page_the_form_came_from(self):
        claim = self.pending()
        self.approve.return_value = BulkResult(done=[claim])

        response = self.post([claim], next=route("srp_review") + "?status=all&scope=in_fleet")

        self.assertRedirects(response, route("srp_review") + "?status=all&scope=in_fleet", fetch_redirect_response=False)

    def test_a_next_address_outside_the_site_is_ignored(self):
        claim = self.pending()
        self.approve.return_value = BulkResult(done=[claim])

        response = self.post([claim], next="https://evil.example/steal")

        self.assertRedirects(response, route("srp_review"), fetch_redirect_response=False)

    def test_only_managers_and_posts(self):
        self.assertEqual(self.client.get(route("srp_review_bulk_approve")).status_code, 405)
        self.assertEqual(self.client_for(self.member).post(route("srp_review_bulk_approve")).status_code, 403)
        self.assertEqual(self.client_for(self.stats).post(route("srp_review_bulk_approve")).status_code, 403)


class RecalculateTests(ManagerPagesTestCase):
    def test_recalculates_every_pending_claim_as_the_manager(self):
        recalc = self.patch("fleetops.srp.review.recalculate_pending", return_value=3)

        response = self.client.post(route("srp_review_recalculate"))

        self.assertRedirects(response, route("srp_review"), fetch_redirect_response=False)
        recalc.assert_called_once_with(actor=self.manager)
        self.assertIn("3 pending claims recalculated with the current policy.", messages_of(response))

    def test_one_claim_is_not_plural(self):
        self.patch("fleetops.srp.review.recalculate_pending", return_value=1)

        response = self.client.post(route("srp_review_recalculate"))

        self.assertIn("1 pending claim recalculated with the current policy.", messages_of(response))

    def test_members_cannot(self):
        recalc = self.patch("fleetops.srp.review.recalculate_pending")

        self.assertEqual(self.client_for(self.member).post(route("srp_review_recalculate")).status_code, 403)
        recalc.assert_not_called()


class ClaimActionTests(ManagerPagesTestCase):
    """The buttons of the claim page: each hands the right values to its service and tells what happened."""

    def setUp(self):
        super().setUp()
        self.claim = self.pending(
            amount=80_000_000, suggested_amount=80_000_000, limited_amount=50_000_000, over_limit=True,
            calculation=Calculation(
                basis="rule", base=80_000_000, suggested=80_000_000, limited=50_000_000, over_limit=True,
                binding_limit="personal", limits=(LimitState("personal", cap=100_000_000, used=50_000_000, remaining=50_000_000),),
            ).as_json(),
        )

    def service(self, name, **kwargs):
        return self.patch(f"fleetops.srp.review.{name}", **kwargs)

    def act(self, action, data=None, claim=None, client=None):
        claim = claim or self.claim
        return (client or self.client).post(route("srp_claim_action", claim.uuid, action), data or {})

    def page(self, claim=None):
        return route("srp_claim", (claim or self.claim).uuid)

    # --- approve

    def test_approve_suggested(self):
        approve = self.service("approve_claim")

        response = self.act("approve", {"mode": "suggested"})

        self.assertRedirects(response, self.page(), fetch_redirect_response=False)
        args, kwargs = approve.call_args
        self.assertEqual(args[0].pk, self.claim.pk)
        self.assertEqual(
            kwargs,
            {
                "actor": self.manager, "amount": 80_000_000, "reason": "", "confirm_above_policy": False,
                "confirm_over_limit": False, "expected_status": None, "expected_suggested": None, "expected_limited": None,
            },
        )
        self.assertIn(f"{self.claim.reference} approved for 80,000,000 ISK.", messages_of(response))

    def test_approve_suggested_approves_the_amount_the_button_showed(self):
        approve = self.service("approve_claim")

        self.act("approve", {"mode": "suggested", "shown_status": "pending", "shown_suggested": "500000", "shown_limited": "400000"})

        kwargs = approve.call_args.kwargs
        # The button's own amount, and the service is told to refuse the claim if it is another one by now.
        self.assertEqual(kwargs["amount"], 500_000)
        self.assertEqual(
            (kwargs["expected_status"], kwargs["expected_suggested"], kwargs["expected_limited"]), ("pending", 500_000, None)
        )

    def test_approve_within_limit_approves_the_amount_the_button_showed(self):
        approve = self.service("approve_claim")

        self.act("approve", {"mode": "within_limit", "shown_status": "pending", "shown_suggested": "500000", "shown_limited": "400000"})

        kwargs = approve.call_args.kwargs
        self.assertEqual(kwargs["amount"], 400_000)
        self.assertEqual(
            (kwargs["expected_status"], kwargs["expected_suggested"], kwargs["expected_limited"]), ("pending", None, 400_000)
        )

    def test_a_typed_amount_is_approved_as_typed_with_the_status_shown(self):
        approve = self.service("approve_claim")

        self.act("approve", {"mode": "amount", "amount": "9m", "reason": "Cheaper fit", "shown_status": "approved", "shown_suggested": "1"})

        kwargs = approve.call_args.kwargs
        self.assertEqual(kwargs["amount"], 9_000_000)
        self.assertEqual(
            (kwargs["expected_status"], kwargs["expected_suggested"], kwargs["expected_limited"]), ("approved", None, None)
        )

    def test_unreadable_shown_values_are_refused(self):
        approve = self.service("approve_claim")

        for bad in ({"shown_status": "paid"}, {"shown_suggested": "many"}, {"shown_limited": "-5"}):
            with self.subTest(bad=bad):
                response = self.act("approve", {"mode": "suggested", **bad})
                self.assertEqual(response.status_code, 200)
        approve.assert_not_called()

    def test_a_claim_that_changed_meanwhile_is_answered_with_a_message_and_its_page(self):
        self.service("approve_claim", side_effect=SrpStateError("amount_changed", "The suggested amount changed."))

        response = self.act("approve", {"mode": "suggested", "shown_suggested": "1"})

        self.assertRedirects(response, self.page(), fetch_redirect_response=False)
        self.assertEqual(messages_of(response), ["The suggested amount changed."])

    def test_approve_within_limit_gives_the_reason_when_none_was_written(self):
        approve = self.service("approve_claim")

        self.act("approve", {"mode": "within_limit"})

        self.assertEqual(approve.call_args.kwargs["amount"], 50_000_000)
        self.assertEqual(approve.call_args.kwargs["reason"], "Reduced to the remaining monthly personal limit.")

    def test_approve_within_limit_keeps_the_reason_that_was_written(self):
        approve = self.service("approve_claim")

        self.act("approve", {"mode": "within_limit", "reason": "Half now, half next month"})

        self.assertEqual(approve.call_args.kwargs["reason"], "Half now, half next month")

    def test_approve_typed_amount_with_confirmations(self):
        approve = self.service("approve_claim")

        self.act(
            "approve",
            {"mode": "amount", "amount": "1.2b", "reason": "Capital loss", "confirm_above_policy": "on", "confirm_over_limit": "on"},
        )

        kwargs = approve.call_args.kwargs
        self.assertEqual((kwargs["amount"], kwargs["reason"]), (1_200_000_000, "Capital loss"))
        self.assertTrue(kwargs["confirm_above_policy"] and kwargs["confirm_over_limit"])

    def test_approve_amount_mode_needs_an_amount(self):
        approve = self.service("approve_claim")

        response = self.act("approve", {"mode": "amount", "amount": ""})

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "fleetops/srp/claim.html")
        self.assertContains(response, "Enter the amount to approve.")
        approve.assert_not_called()

    def test_a_refused_amount_shows_the_page_again_with_the_form_as_it_was_sent(self):
        self.service("approve_claim", side_effect=SrpLimitError("over_limit", "Only 50,000,000 ISK is left in the monthly personal limit."))

        response = self.act("approve", {"mode": "amount", "amount": "250m", "reason": "A reason", "confirm_above_policy": "on"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Only 50,000,000 ISK is left in the monthly personal limit.")
        self.assertContains(response, 'value="250m"')
        self.assertContains(response, "A reason</textarea>")
        self.assertContains(response, 'name="confirm_above_policy" class="form-check-input" id="id_approve_confirm_above_policy" checked')
        self.assertEqual(messages_of(response)[-1], "Only 50,000,000 ISK is left in the monthly personal limit.")

    def test_a_claim_in_another_status_gets_a_message_and_its_page(self):
        self.service("approve_claim", side_effect=SrpStateError("state", "This claim is already approved."))

        response = self.act("approve", {"mode": "suggested"})

        self.assertRedirects(response, self.page(), fetch_redirect_response=False)
        self.assertEqual(messages_of(response), ["This claim is already approved."])

    def test_changing_the_amount_of_an_approved_claim_is_reported_as_a_change(self):
        claim = f.srp_claim(self.member, status=APPROVED, time=self.loss_time, decided_by=self.manager2)
        self.service("approve_claim")

        response = self.act("approve", {"mode": "amount", "amount": "9m", "reason": "Fit was cheaper"}, claim=claim)

        self.assertIn(f"{claim.reference}: amount changed to 9,000,000 ISK.", messages_of(response))

    # --- decisions with a reason

    def test_reject_revoke_unpaid_and_reopen_pass_their_reason(self):
        cases = {
            "reject": ("reject_claim", PENDING, "rejected."),
            "revoke": ("revoke_approval", APPROVED, "Approval of"),
            "unpaid": ("undo_payment", PAID, "Payment of"),
            "reopen": ("reopen_claim", REJECTED, "reopened"),
        }
        for action, (function, status, fragment) in cases.items():
            with self.subTest(action=action):
                claim = f.srp_claim(self.member, status=status, time=self.loss_time, decided_by=self.manager2)
                mocked = self.service(function)

                response = self.act(action, {"reason": "Because"}, claim=claim)

                self.assertRedirects(response, self.page(claim), fetch_redirect_response=False)
                args, kwargs = mocked.call_args
                self.assertEqual(args[0].pk, claim.pk)
                self.assertEqual(kwargs, {"actor": self.manager, "reason": "Because"})
                self.assertTrue(any(fragment in line for line in messages_of(response)), messages_of(response))

    def test_a_decision_without_its_reason_is_not_made(self):
        for action, function in (("reject", "reject_claim"), ("revoke", "revoke_approval"), ("unpaid", "undo_payment"), ("reopen", "reopen_claim")):
            with self.subTest(action=action):
                mocked = self.service(function)

                response = self.act(action, {"reason": "  "})

                self.assertEqual(response.status_code, 200)
                self.assertContains(response, "This field is required.")
                mocked.assert_not_called()

    # --- paid

    def test_mark_paid_hands_over_the_claim_and_the_note(self):
        claim = f.srp_claim(self.member, status=APPROVED, time=self.loss_time)
        paid = self.service("mark_paid", return_value=BulkResult(done=[claim]))

        response = self.act("paid", {"note": "Sent 12:05"}, claim=claim)

        args, kwargs = paid.call_args
        self.assertEqual([item.pk for item in args[0]], [claim.pk])
        self.assertEqual(kwargs, {"actor": self.manager, "note": "Sent 12:05", "expected_amounts": None})
        self.assertIn(f"{claim.reference} marked paid.", messages_of(response))

    def test_mark_paid_hands_over_the_amount_the_payer_was_shown(self):
        claim = f.srp_claim(self.member, status=APPROVED, time=self.loss_time)
        paid = self.service("mark_paid", return_value=BulkResult(done=[claim]))

        self.act("paid", {"note": "", "shown_amount": "10000000"}, claim=claim)

        self.assertEqual(paid.call_args.kwargs["expected_amounts"], {claim.pk: 10_000_000})

    def test_mark_paid_with_a_changed_amount_is_reported_as_an_error(self):
        claim = f.srp_claim(self.member, status=APPROVED, time=self.loss_time)
        self.service(
            "mark_paid",
            return_value=BulkResult(skipped=[(claim, "The approved amount changed from 10,000,000 ISK to 4,000,000 ISK.")]),
        )

        response = self.act("paid", {"shown_amount": "10000000"}, claim=claim)

        self.assertRedirects(response, self.page(claim), fetch_redirect_response=False)
        self.assertEqual(messages_of(response), ["The approved amount changed from 10,000,000 ISK to 4,000,000 ISK."])

    def test_mark_paid_skip_is_reported_as_an_error(self):
        claim = f.srp_claim(self.member, status=PAID, time=self.loss_time, decided_by=self.manager2)
        self.service("mark_paid", return_value=BulkResult(skipped=[(claim, "This claim is already paid.")]))

        response = self.act("paid", claim=claim)

        self.assertRedirects(response, self.page(claim), fetch_redirect_response=False)
        self.assertEqual(messages_of(response), ["This claim is already paid."])

    # --- reclassify

    def test_move_out_of_fleet(self):
        reclassify = self.service("reclassify_claim")
        claim = self.pending(link=self.link)

        response = self.act("reclassify", {"scope": "out_of_fleet", "reason": "Lost on the way home"}, claim=claim)

        args, kwargs = reclassify.call_args
        self.assertEqual(args[0].pk, claim.pk)
        self.assertEqual(kwargs, {"actor": self.manager, "scope": "out_of_fleet", "link": None, "reason": "Lost on the way home"})
        self.assertIn(f"{claim.reference} moved to out of fleet and recalculated.", messages_of(response))

    def test_move_into_a_fleet_by_its_link_code(self):
        reclassify = self.service("reclassify_claim")

        self.act("reclassify", {"scope": "in_fleet", "link": self.link.code, "reason": "It was in the fleet"})

        self.assertEqual(reclassify.call_args.kwargs["link"], self.link)
        self.assertEqual(reclassify.call_args.kwargs["scope"], "in_fleet")

    def test_move_into_a_fleet_needs_a_known_link(self):
        reclassify = self.service("reclassify_claim")

        response = self.act("reclassify", {"scope": "in_fleet", "link": "nosuchcode", "reason": "It was in the fleet"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "There is no fleet with this SRP link.")
        reclassify.assert_not_called()

    def test_move_into_a_fleet_without_a_link_is_refused(self):
        reclassify = self.service("reclassify_claim")

        response = self.act("reclassify", {"scope": "in_fleet", "reason": "It was in the fleet"})

        self.assertContains(response, "Choose the fleet&#x27;s SRP link for an in-fleet loss.")
        reclassify.assert_not_called()

    def test_a_refused_move_shows_the_service_text_and_keeps_the_form(self):
        self.service("reclassify_claim", side_effect=SrpError("closed", "This fleet's SRP link is closed."))

        response = self.act("reclassify", {"scope": "in_fleet", "link": self.link.code, "reason": "It was in the fleet"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "This fleet&#x27;s SRP link is closed.")
        self.assertContains(response, "It was in the fleet</textarea>")

    # --- recalculate, re-check and notes

    def test_recalculate_and_recheck(self):
        recalculate = self.service("recalculate_claim")
        recheck = self.service("recheck_presence")

        first = self.act("recalculate")
        second = self.act("recheck")

        self.assertRedirects(first, self.page(), fetch_redirect_response=False)
        self.assertEqual(recalculate.call_args.kwargs, {"actor": self.manager})
        self.assertEqual(recheck.call_args.kwargs, {"actor": self.manager})
        self.assertIn(f"{self.claim.reference} recalculated with the current policy.", messages_of(first))
        self.assertIn(f"Fleet presence of {self.claim.reference} checked again.", messages_of(second))

    def test_a_recalculate_that_the_service_refuses_is_a_message(self):
        self.service("recalculate_claim", side_effect=SrpStateError("state", "Only pending claims can be recalculated."))

        response = self.act("recalculate")

        self.assertRedirects(response, self.page(), fetch_redirect_response=False)
        self.assertEqual(messages_of(response), ["Only pending claims can be recalculated."])

    def test_internal_note(self):
        note = self.service("add_note")

        response = self.act("note", {"text": "Asked the pilot"})

        self.assertEqual(note.call_args.kwargs, {"actor": self.manager, "text": "Asked the pilot"})
        self.assertRedirects(response, self.page(), fetch_redirect_response=False)
        self.assertIn("Note added. Only SRP managers can see it.", messages_of(response))

    def test_an_empty_note_is_refused(self):
        note = self.service("add_note")

        response = self.act("note", {"text": " "})

        self.assertEqual(response.status_code, 200)
        note.assert_not_called()

    # --- who may do what

    def test_unknown_action_and_unknown_claim_are_404(self):
        import uuid

        self.assertEqual(self.act("explode").status_code, 404)
        self.assertEqual(self.client.post(route("srp_claim_action", uuid.uuid4(), "approve"), {"mode": "suggested"}).status_code, 404)

    def test_only_managers_decide(self):
        approve = self.service("approve_claim")
        for user in (self.member, self.stats, self.fc, self.no_srp):
            with self.subTest(user=user.username):
                self.assertEqual(self.act("approve", {"mode": "suggested"}, client=self.client_for(user)).status_code, 403)
        approve.assert_not_called()

    def test_a_manager_does_not_decide_on_their_own_claim(self):
        mine = self.pending(self.manager)
        calls = {name: self.service(name) for name in ("approve_claim", "reject_claim", "revoke_approval", "mark_paid", "undo_payment", "reopen_claim", "reclassify_claim", "recalculate_claim")}
        data = {
            "approve": {"mode": "suggested"}, "reject": {"reason": "x"}, "revoke": {"reason": "x"}, "paid": {},
            "unpaid": {"reason": "x"}, "reopen": {"reason": "x"}, "reclassify": {"scope": "out_of_fleet", "reason": "x"},
            "recalculate": {},
        }
        for action, payload in data.items():
            with self.subTest(action=action):
                self.assertEqual(self.act(action, payload, claim=mine).status_code, 403)
        for mocked in calls.values():
            mocked.assert_not_called()

    def test_a_manager_may_check_presence_and_write_notes_on_their_own_claim(self):
        mine = self.pending(self.manager)
        recheck = self.service("recheck_presence")
        note = self.service("add_note")

        self.assertEqual(self.act("recheck", claim=mine).status_code, 302)
        self.assertEqual(self.act("note", {"text": "Mine"}, claim=mine).status_code, 302)
        recheck.assert_called_once()
        note.assert_called_once()

    def test_the_self_review_setting_lets_a_manager_decide_on_their_own_claim(self):
        mine = self.pending(self.manager)
        approve = self.service("approve_claim")
        SrpSettings.objects.filter(pk=1).update(allow_self_review=True)

        response = self.act("approve", {"mode": "suggested"}, claim=mine)

        self.assertEqual(response.status_code, 302)
        approve.assert_called_once()

    def test_another_manager_can_decide_on_it(self):
        mine = self.pending(self.manager)
        approve = self.service("approve_claim")

        response = self.act("approve", {"mode": "suggested"}, claim=mine, client=self.client_for(self.manager2))

        self.assertEqual(response.status_code, 302)
        self.assertEqual(approve.call_args.kwargs["actor"], self.manager2)

    def test_get_is_refused(self):
        self.assertEqual(self.client.get(route("srp_claim_action", self.claim.uuid, "approve")).status_code, 405)

    def test_return_address_must_stay_on_the_site(self):
        self.service("add_note")

        local = self.act("note", {"text": "x", "next": route("srp_review")})
        remote = self.act("note", {"text": "x", "next": "//evil.example/"})

        self.assertRedirects(local, route("srp_review"), fetch_redirect_response=False)
        self.assertRedirects(remote, self.page(), fetch_redirect_response=False)


class DecisionFormsTests(ManagerPagesTestCase):
    """What the claim page offers a manager in each status."""

    def get(self, claim, user=None):
        return (self.client_for(user) if user else self.client).get(route("srp_claim", claim.uuid))

    def action_url(self, claim, action):
        return route("srp_claim_action", claim.uuid, action)

    def test_pending_claim_offers_approve_reject_move_and_recalculate(self):
        claim = self.pending(amount=80_000_000)

        response = self.get(claim)

        self.assertContains(response, "Approve suggested (80m ISK)")
        self.assertContains(response, 'name="mode" value="suggested"')
        self.assertContains(response, 'name="mode" value="amount"')
        self.assertContains(response, "Pay more than the policy amount")
        self.assertContains(response, "Approve over the monthly limit")
        for action in ("approve", "reject", "reclassify", "recalculate", "recheck", "note"):
            self.assertContains(response, f'action="{self.action_url(claim, action)}"')
        for action in ("revoke", "paid", "unpaid", "reopen"):
            self.assertNotContains(response, f'action="{self.action_url(claim, action)}"')

    def test_amount_field_starts_with_the_suggested_amount(self):
        claim = self.pending(amount=80_000_000)

        self.assertContains(self.get(claim), 'value="80,000,000"')

    def approve_form(self, claim):
        html = self.get(claim).content.decode()
        start = html.index('class="fleetops-srp-form fleetops-srp-approve')
        return html[start : html.index("</form>", start)]

    def test_enter_in_the_amount_field_approves_the_typed_amount(self):
        # Enter presses the first submit button of the form: that one must be the button for the typed amount.
        for claim in (self.pending(amount=80_000_000), f.srp_claim(self.member, status=APPROVED, time=self.loss_time)):
            with self.subTest(status=claim.status):
                form = self.approve_form(claim)
                first = form[form.index("<button") : form.index("</button>")]
                self.assertIn('type="submit" name="mode" value="amount"', first)
                self.assertIn("visually-hidden", first)
                self.assertNotIn("Approve suggested", first)

    def test_the_form_carries_what_the_page_shows(self):
        claim = self.pending(amount=80_000_000, limited_amount=50_000_000, over_limit=True)

        form = self.approve_form(claim)

        self.assertIn('<input type="hidden" name="shown_status" value="pending">', form)
        self.assertIn('<input type="hidden" name="shown_suggested" value="80000000">', form)
        self.assertIn('<input type="hidden" name="shown_limited" value="50000000">', form)

    def test_a_form_that_is_shown_again_carries_the_claim_as_it_is_now(self):
        claim = self.pending(amount=80_000_000)
        self.service = self.patch(
            "fleetops.srp.review.approve_claim", side_effect=SrpError("invalid_amount", "Enter an amount.")
        )

        response = self.client.post(
            route("srp_claim_action", claim.uuid, "approve"),
            {"mode": "amount", "amount": "9m", "shown_status": "pending", "shown_suggested": "1", "shown_limited": "1"},
        )

        self.assertContains(response, '<input type="hidden" name="shown_suggested" value="80000000">')
        self.assertNotContains(response, 'name="shown_suggested" value="1"')

    def test_the_reason_hint_names_the_rule_of_the_status(self):
        pending = self.approve_form(self.pending())
        approved = self.approve_form(f.srp_claim(self.member, status=APPROVED, time=self.loss_time, amount=80_000_000, approved_amount=60_000_000))

        self.assertIn("Required when the amount differs from the suggested amount or goes over a monthly limit.", pending)
        self.assertNotIn("data-srp-approved", pending)
        self.assertIn("Required: say why the amount changes.", approved)
        self.assertIn('data-srp-approved="60000000"', approved)

    def test_the_paid_form_carries_the_amount_to_send(self):
        claim = f.srp_claim(self.member, status=APPROVED, time=self.loss_time, amount=80_000_000, approved_amount=60_000_000)

        self.assertContains(self.get(claim), '<input type="hidden" name="shown_amount" value="60000000">')

    def test_over_limit_claim_offers_approving_within_the_limit(self):
        claim = self.pending(amount=80_000_000, limited_amount=50_000_000, over_limit=True)

        response = self.get(claim)

        self.assertContains(response, "Approve within limit (50m ISK)")
        self.assertContains(response, 'name="mode" value="within_limit"')
        self.assertContains(response, 'data-srp-limited="50000000"')

    def test_claim_within_its_limit_does_not_offer_it(self):
        self.assertNotContains(self.get(self.pending()), "Approve within limit")

    def test_claim_without_an_automatic_amount_asks_for_one(self):
        claim = self.pending(amount=0, suggested_amount=0, limited_amount=0)

        response = self.get(claim)

        self.assertContains(response, "There is no automatic amount for this claim. Enter the amount to pay.")
        self.assertNotContains(response, "Approve suggested")

    def test_approved_claim_offers_a_new_amount_revoking_and_paying(self):
        claim = f.srp_claim(self.member, status=APPROVED, time=self.loss_time, amount=80_000_000, approved_amount=60_000_000)

        response = self.get(claim)

        self.assertContains(response, "Change the amount")
        self.assertContains(response, 'value="60,000,000"')
        self.assertContains(response, f'action="{self.action_url(claim, "revoke")}"')
        self.assertContains(response, f'action="{self.action_url(claim, "paid")}"')
        self.assertContains(response, 'data-srp-copy="60000000"')
        self.assertNotContains(response, "Approve suggested")
        self.assertNotContains(response, f'action="{self.action_url(claim, "reject")}"')

    def test_paid_claim_offers_undoing_the_payment(self):
        claim = f.srp_claim(self.member, status=PAID, time=self.loss_time, decided_by=self.manager2)

        response = self.get(claim)

        self.assertContains(response, f'action="{self.action_url(claim, "unpaid")}"')
        self.assertNotContains(response, f'action="{self.action_url(claim, "revoke")}"')

    def test_rejected_claim_offers_reopening(self):
        claim = f.srp_claim(self.member, status=REJECTED, time=self.loss_time)

        self.assertContains(self.get(claim), f'action="{self.action_url(claim, "reopen")}"')

    def test_withdrawn_claim_offers_only_notes_and_the_presence_check(self):
        claim = f.srp_claim(self.member, status=WITHDRAWN, time=self.loss_time)

        response = self.get(claim)

        self.assertContains(response, f'action="{self.action_url(claim, "note")}"')
        for action in ("approve", "reject", "revoke", "paid", "reopen", "reclassify"):
            self.assertNotContains(response, f'action="{self.action_url(claim, action)}"')

    def test_own_claim_gets_a_notice_instead_of_decisions(self):
        mine = self.pending(self.manager)

        response = self.get(mine)

        self.assertContains(response, "This is your own claim. Another SRP manager has to decide on it.")
        self.assertNotContains(response, f'action="{self.action_url(mine, "approve")}"')
        self.assertContains(response, f'action="{self.action_url(mine, "note")}"')
        self.assertContains(response, f'action="{self.action_url(mine, "recheck")}"')
        self.assertFalse(response.context["can_decide"])

    def test_self_review_setting_restores_the_decisions(self):
        mine = self.pending(self.manager)
        SrpSettings.objects.filter(pk=1).update(allow_self_review=True)

        response = self.get(mine)

        self.assertNotContains(response, "Another SRP manager has to decide on it.")
        self.assertContains(response, f'action="{self.action_url(mine, "approve")}"')

    def test_nobody_but_managers_sees_any_review_form(self):
        claim = self.pending(link=self.link)
        for user in (self.member, self.stats, self.fc):
            with self.subTest(user=user.username):
                response = self.get(claim, user)
                self.assertEqual(response.status_code, 200)
                self.assertNotContains(response, route("srp_claim_action", claim.uuid, "note"))
                self.assertNotContains(response, "Internal note</h3>")

    def test_reclassify_form_lists_open_fleets_around_the_loss(self):
        claim = self.pending()

        response = self.get(claim)

        self.assertContains(response, f'<option value="{self.link.code}"')
        self.assertNotContains(response, f'<option value="{self.closed_link.code}"')

    def test_buttons_are_guarded_against_double_clicks(self):
        response = self.get(self.pending())

        self.assertContains(response, "data-srp-once")
        self.assertContains(response, "fleetops/srp.js?v=")


class InternalNoteVisibilityTests(ManagerPagesTestCase):
    NOTE = "SECRET remark about the pilot"

    def test_the_audit_page_does_not_show_an_internal_note(self):
        claim = self.pending()
        review.add_note(claim, actor=self.manager, text=self.NOTE)
        auditor = f.create_user("srpauditor", perms=[*f.MEMBER_PERMS, "fleetops.view_audit_log"])

        page = self.client_for(auditor).get(reverse("fleetops:audit_log"))

        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "srp.claim_note")
        self.assertNotContains(page, self.NOTE)
        # The managers still read the note on the claim.
        self.assertContains(self.client.get(route("srp_claim", claim.uuid)), self.NOTE)


class PaymentsPageTests(ManagerPagesTestCase):
    def approved(self, user=None, amount=10_000_000, **extra):
        return f.srp_claim(user or self.member, status=APPROVED, time=extra.pop("time", self.loss_time),
                           amount=amount, decided_by=self.manager2, **extra)

    def paid(self, paid_at, user=None, **extra):
        return f.srp_claim(user or self.member, status=PAID, time=self.loss_time, decided_by=self.manager2,
                           paid_at=paid_at, paid_by=self.manager2, **extra)

    def test_only_managers_open_the_page(self):
        for user, expected in ((self.member, 403), (self.stats, 403), (self.manager, 200)):
            with self.subTest(user=user.username):
                self.assertEqual(self.client_for(user).get(route("srp_payments")).status_code, expected)

    def test_approved_claims_are_grouped_by_main_with_totals_and_copy_buttons(self):
        first = self.approved(self.member, amount=60_000_000)
        second = self.approved(self.member, amount=40_000_000)
        third = self.approved(self.other, amount=250_000_000)
        pending = self.pending()
        self.paid(timezone.now(), user=self.other)

        response = self.client.get(route("srp_payments"))

        rows = response.context["pay_rows"]
        self.assertEqual([row.main_character_name for row in rows], [f.main_of(self.other).character_name, f.main_of(self.member).character_name])
        self.assertEqual([row.total for row in rows], [250_000_000, 100_000_000])
        self.assertEqual({claim.pk for claim in rows[1].claims}, {first.pk, second.pk})
        self.assertEqual(response.context["to_pay_total"], 350_000_000)
        self.assertEqual(response.context["to_pay_count"], 3)
        self.assertContains(response, 'data-srp-copy="250000000"')
        self.assertContains(response, 'data-srp-copy="100000000"')
        self.assertContains(response, 'data-srp-copy="60000000"')
        self.assertContains(response, f'data-srp-copy="{f.main_of(self.member).character_name}"')
        self.assertContains(response, third.reference)
        self.assertNotContains(response, f'name="claim" value="{pending.uuid}"')
        self.assertContains(response, "Copy name")
        self.assertContains(response, "Copy amount")

    def test_a_row_is_named_after_the_current_main_of_the_player(self):
        pilot = f.create_user("switcher", perms=f.SRP_MEMBER_PERMS, main_name="Old Main")
        first = self.approved(pilot, amount=10_000_000)
        newer = f.create_character("New Main")
        f.add_ownership(pilot, newer)
        pilot.profile.main_character = newer
        pilot.profile.save()
        second = self.approved(pilot, amount=20_000_000)
        self.assertEqual((first.main_character_name, second.main_character_name), ("Old Main", "New Main"))

        response = self.client.get(route("srp_payments"))

        (row,) = response.context["pay_rows"]
        self.assertEqual((row.main_character_name, row.main_character_id, row.total), ("New Main", newer.character_id, 30_000_000))
        self.assertContains(response, 'data-srp-copy="New Main"')
        self.assertContains(response, "Select all claims of New Main")
        self.assertNotContains(response, "Old Main")
        self.assertEqual({claim.pk for claim in row.claims}, {first.pk, second.pk})

    def test_a_main_that_left_the_account_is_not_paid(self):
        pilot = f.create_user("seller", perms=f.SRP_MEMBER_PERMS, main_name="Sold Main")
        old = f.main_of(pilot)
        self.approved(pilot, amount=10_000_000)
        newer = f.create_character("Replacement Main")
        f.add_ownership(pilot, newer)
        # Alliance Auth clears the main of the profile when its character changes owner.
        CharacterOwnership.objects.filter(character=old).delete()
        pilot.profile.refresh_from_db()
        self.assertIsNone(pilot.profile.main_character)
        pilot.profile.main_character = newer
        pilot.profile.save()

        (row,) = self.client.get(route("srp_payments")).context["pay_rows"]

        self.assertEqual(row.main_character_name, "Replacement Main")

    def test_without_a_main_the_main_recorded_with_the_claim_is_paid(self):
        pilot = f.create_user("nomain", perms=f.SRP_MEMBER_PERMS, main_name="Recorded Main")
        self.approved(pilot, amount=10_000_000)
        UserProfile.objects.filter(user=pilot).update(main_character=None)

        (row,) = self.client.get(route("srp_payments")).context["pay_rows"]

        self.assertEqual(row.main_character_name, "Recorded Main")

    def test_the_claims_of_a_deleted_account_are_paid_to_the_recorded_main(self):
        pilot = f.create_user("gone", perms=f.SRP_MEMBER_PERMS, main_name="Gone Main")
        claim = self.approved(pilot, amount=15_000_000)
        pilot.delete()
        claim.refresh_from_db()
        self.assertIsNone(claim.user_id)

        (row,) = self.client.get(route("srp_payments")).context["pay_rows"]

        self.assertEqual((row.user_id, row.main_character_name, row.total), (None, "Gone Main", 15_000_000))

    def test_only_approved_claims_are_to_pay(self):
        approved = self.approved()
        self.pending()
        f.srp_claim(self.member, status=REJECTED, time=self.loss_time, decided_by=self.manager2)
        f.srp_claim(self.member, status=WITHDRAWN, time=self.loss_time)

        response = self.client.get(route("srp_payments"))

        self.assertEqual([claim.pk for row in response.context["pay_rows"] for claim in row.claims], [approved.pk])

    def test_the_claim_page_names_the_current_main_to_send_the_isk_to(self):
        pilot = f.create_user("claimpage", perms=f.SRP_MEMBER_PERMS, main_name="Old Main")
        claim = self.approved(pilot, amount=12_000_000)
        newer = f.create_character("New Main")
        f.add_ownership(pilot, newer)
        pilot.profile.main_character = newer
        pilot.profile.save()

        response = self.client.get(route("srp_claim", claim.uuid))

        self.assertContains(response, "Send 12,000,000 ISK to New Main in game first.")
        self.assertNotContains(response, "to Old Main")

    def test_copy_buttons_are_hidden_until_the_script_runs(self):
        self.approved()

        html = self.client.get(route("srp_payments")).content.decode()

        self.assertIn('data-srp-copy="10000000" hidden>', html)
        self.assertIn("fleetops/srp.js?v=", html)

    def test_every_claim_has_a_tick_box_with_its_amount_for_the_running_total(self):
        claim = self.approved(amount=12_345_678)

        response = self.client.get(route("srp_payments"))

        self.assertContains(response, f'name="claim" value="{claim.uuid}" data-srp-select data-srp-amount="12345678"')
        self.assertContains(response, "Mark selected paid")
        self.assertContains(response, f'action="{route("srp_payments")}"')

    def test_every_tick_box_posts_the_amount_to_send(self):
        claim = self.approved(amount=12_345_678)

        html = self.client.get(route("srp_payments")).content.decode()

        self.assertIn(f'<input type="hidden" name="shown-{claim.uuid}" value="12345678">', html)

    def test_own_claims_have_no_tick_box_unless_self_review_is_allowed(self):
        own = self.approved(self.manager)

        response = self.client.get(route("srp_payments"))

        self.assertNotContains(response, f'name="claim" value="{own.uuid}"')
        self.assertContains(response, "Your own claim: another SRP manager has to pay it.")
        SrpSettings.objects.filter(pk=1).update(allow_self_review=True)
        self.assertContains(self.client.get(route("srp_payments")), f'name="claim" value="{own.uuid}"')

    def test_nothing_to_pay(self):
        response = self.client.get(route("srp_payments"))

        self.assertContains(response, "Nothing waits for payment")
        self.assertNotContains(response, "Mark selected paid")

    def test_month_filter_uses_the_month_of_the_loss(self):
        current = self.approved(time=timezone.now() - timedelta(hours=1))
        march = self.approved(time=datetime(2026, 3, 14, 12, 0, tzinfo=UTC))

        everything = self.client.get(route("srp_payments"))
        in_march = self.client.get(route("srp_payments"), {"year": "2026", "month": "3"})

        self.assertEqual(everything.context["to_pay_count"], 2)
        self.assertEqual(in_march.context["to_pay_count"], 1)
        self.assertContains(in_march, march.reference)
        self.assertNotContains(in_march, f'name="claim" value="{current.uuid}"')
        self.assertContains(in_march, "Show all months")

    def test_paid_claims_default_to_the_last_30_days(self):
        now = timezone.now()
        recent = self.paid(now - timedelta(days=3))
        old = self.paid(now - timedelta(days=45))

        response = self.client.get(route("srp_payments"))

        self.assertEqual([claim.pk for claim in response.context["paid_claims"]], [recent.pk])
        self.assertNotIn(old.pk, [claim.pk for claim in response.context["paid_claims"]])
        self.assertEqual(response.context["paid_count"], 1)

    def test_paid_claims_can_be_filtered_by_payment_date(self):
        paid_at = datetime(2026, 4, 10, 18, 30, tzinfo=UTC)
        inside = self.paid(paid_at)
        before = self.paid(datetime(2026, 4, 9, 23, 59, tzinfo=UTC))
        after = self.paid(datetime(2026, 4, 11, 0, 0, tzinfo=UTC))

        response = self.client.get(route("srp_payments"), {"paid_from": "2026-04-10", "paid_to": "2026-04-10"})

        self.assertEqual([claim.pk for claim in response.context["paid_claims"]], [inside.pk])
        self.assertNotIn(before.pk, [claim.pk for claim in response.context["paid_claims"]])
        self.assertNotIn(after.pk, [claim.pk for claim in response.context["paid_claims"]])

    def test_paid_claims_without_a_start_date_reach_back_to_the_beginning(self):
        old = self.paid(datetime(2025, 1, 1, tzinfo=UTC))

        response = self.client.get(route("srp_payments"), {"paid_from": "", "paid_to": "2026-12-31"})

        self.assertEqual([claim.pk for claim in response.context["paid_claims"]], [old.pk])

    def test_paid_total_and_note_are_shown(self):
        self.paid(timezone.now(), amount=70_000_000, payment_note="Sent at 19:40")

        response = self.client.get(route("srp_payments"))

        self.assertEqual(response.context["paid_total"], 70_000_000)
        self.assertContains(response, "Sent at 19:40")

    def test_invalid_dates_are_ignored(self):
        self.approved(time=timezone.now())

        response = self.client.get(route("srp_payments"), {"paid_from": "yesterday", "paid_to": "2026-13-45", "year": "x"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["to_pay_count"], 1)

    def test_page_costs_the_same_number_of_queries_for_few_and_many_claims(self):
        def cost():
            with CaptureQueriesContext(connection) as queries:
                self.assertEqual(self.client.get(route("srp_payments")).status_code, 200)
            return len(queries)

        for _ in range(2):
            self.approved(self.member)
        cost()
        small = cost()
        for index in range(12):
            self.approved(self.member if index % 2 else self.other)
            self.paid(timezone.now(), user=self.other)

        self.assertEqual(cost(), small)


class DayBoundsTests(SimpleTestCase):
    def test_a_date_range_covers_whole_days_in_eve_time(self):
        lower, upper = day_bounds(date(2026, 3, 21), date(2026, 3, 22))

        self.assertEqual(lower, datetime(2026, 3, 21, 0, 0, tzinfo=UTC))
        self.assertEqual(upper, datetime(2026, 3, 23, 0, 0, tzinfo=UTC))

    def test_one_end_may_be_open(self):
        self.assertEqual(day_bounds(date(2026, 3, 21), None), (datetime(2026, 3, 21, tzinfo=UTC), None))
        self.assertEqual(day_bounds(None, date(2026, 3, 21)), (None, datetime(2026, 3, 22, tzinfo=UTC)))
        self.assertEqual(day_bounds(None, None), (None, None))


class MarkPaidTests(ManagerPagesTestCase):
    def setUp(self):
        super().setUp()
        self.mark = self.patch("fleetops.srp.review.mark_paid")

    def approved(self, user=None, amount=10_000_000):
        return f.srp_claim(user or self.member, status=APPROVED, time=self.loss_time, amount=amount, decided_by=self.manager2)

    def post(self, claims, **extra):
        return self.client.post(route("srp_payments"), {"claim": [str(claim.uuid) for claim in claims], **extra})

    def test_ticked_claims_are_marked_paid_with_the_note(self):
        first, second = self.approved(amount=60_000_000), self.approved(self.other, amount=40_000_000)
        self.mark.return_value = BulkResult(done=[first, second])

        response = self.post([first, second], note="Sent 20:15")

        self.assertRedirects(response, route("srp_payments"), fetch_redirect_response=False)
        (claims_arg,), kwargs = self.mark.call_args
        self.assertEqual({claim.pk for claim in claims_arg}, {first.pk, second.pk})
        self.assertEqual(kwargs, {"actor": self.manager, "note": "Sent 20:15", "expected_amounts": {}})
        self.assertIn("2 claims marked paid (100,000,000 ISK).", messages_of(response))

    def test_the_amounts_the_payer_was_shown_go_to_the_service(self):
        first, second = self.approved(amount=60_000_000), self.approved(self.other, amount=40_000_000)
        self.mark.return_value = BulkResult(done=[first, second])

        self.post([first, second], **{f"shown-{first.uuid}": "60000000", f"shown-{second.uuid}": "x"})

        self.assertEqual(self.mark.call_args.kwargs["expected_amounts"], {first.pk: 60_000_000})

    def test_skipped_claims_are_listed(self):
        claim = self.approved()
        self.mark.return_value = BulkResult(skipped=[(claim, "This claim is already paid.")])

        response = self.post([claim])

        self.assertTrue(any(claim.reference in line and "already paid" in line for line in messages_of(response)))

    def test_own_claims_are_skipped_with_a_message_and_not_a_403(self):
        mine = self.approved(self.manager)
        theirs = self.approved()
        self.mark.return_value = BulkResult(done=[theirs])

        response = self.post([mine, theirs])

        self.assertEqual(response.status_code, 302)
        (claims_arg,), _kwargs = self.mark.call_args
        self.assertEqual([claim.pk for claim in claims_arg], [theirs.pk])
        self.assertTrue(any(mine.reference in line and "your own claim" in line for line in messages_of(response)))

    def test_only_own_claims_never_reach_the_service(self):
        mine = self.approved(self.manager)

        response = self.post([mine])

        self.assertEqual(response.status_code, 302)
        self.mark.assert_not_called()

    def test_nothing_ticked_is_an_error(self):
        response = self.client.post(route("srp_payments"), {})

        self.assertRedirects(response, route("srp_payments"), fetch_redirect_response=False)
        self.assertIn("Select at least one claim.", messages_of(response))
        self.mark.assert_not_called()

    def test_a_payment_note_that_is_too_long_is_refused(self):
        claim = self.approved()

        response = self.post([claim], note="x" * 300)

        self.mark.assert_not_called()
        self.assertTrue(messages_of(response))

    def test_return_to_the_filtered_page(self):
        claim = self.approved()
        self.mark.return_value = BulkResult(done=[claim])
        target = route("srp_payments") + "?year=2026&month=3"

        response = self.post([claim], next=target)

        self.assertRedirects(response, target, fetch_redirect_response=False)

    def test_members_cannot_mark_anything_paid(self):
        claim = self.approved()

        response = self.client_for(self.member).post(route("srp_payments"), {"claim": [str(claim.uuid)]})

        self.assertEqual(response.status_code, 403)
        self.mark.assert_not_called()
