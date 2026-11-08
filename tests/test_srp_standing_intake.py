# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Standing links as an entry point (INT-10), the daily upkeep of the links (INT-11) and the claim deadline (INT-12).

ESI and zKillboard are never reached; killmails come from the factories and the clock is passed in.
"""

import contextlib
from datetime import datetime, timedelta, timezone as dt_timezone
from decimal import Decimal
from unittest import mock

from django.core.cache import cache
from django.db import IntegrityError, connection
from django.test import TestCase, TransactionTestCase, tag
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone, translation

from fleetops import tasks as fleet_tasks
from fleetops import texts
from fleetops.models import AuditLog, SrpClaim, SrpFleetLink, SrpSettings
from fleetops.srp import claims, display, links, review
from fleetops.srp import policy as rules
from fleetops.srp.types import SrpError

from . import factories as f
from .test_srp_claims import ClaimBase, stored_codes
from .test_srp_views import SrpPagesTestCase

Status = SrpClaim.Status


def utc(*args):
    return datetime(*args, tzinfo=dt_timezone.utc)


def today():
    return links.eve_today()


class StandingBase(ClaimBase):
    def setUp(self):
        super().setUp()
        self.settings(enabled=True, percentage=Decimal("100"))
        self.category = f.srp_category("Home defense")

    def standing(self, **values):
        values.setdefault("category", self.category)
        values.setdefault("link_date", today())
        return f.standing_link(**values)

    def killmail(self, character=None, **kwargs):
        # The default killmail time of the factories is an hour ago, which is yesterday (EVE time) in the first hour after
        # 00:00 UTC; the standing link is dated today, so the loss is never earlier than the start of today.
        if kwargs.get("time") is None:
            start = utc(*today().timetuple()[:3])
            kwargs["time"] = max(timezone.now() - timedelta(hours=1), start)
        return super().killmail(character, **kwargs)

    def loss_on(self, day, hour=12, **extra):
        return self.killmail(time=utc(day.year, day.month, day.day, hour), **extra)


# --- INT-10: the rules of a claim through a standing link --------------------------------------------------------


class EligibilityTests(StandingBase):
    def check(self, **values):
        km = values.pop("km", None) or self.killmail()
        values.setdefault("scope", "out_of_fleet")
        values.setdefault("link", None)
        return rules.eligibility(km=km, user=self.user, settings_obj=SrpSettings.get_solo(), **values)

    def test_a_loss_outside_a_fleet_is_refused_when_that_is_switched_off(self):
        self.settings(out_of_fleet_enabled=False)
        self.assertEqual(self.check(), ["Only losses in FleetOps fleets are covered."])

    def test_through_a_standing_link_the_switch_does_not_apply(self):
        self.settings(out_of_fleet_enabled=False)
        self.assertEqual(self.check(link=self.standing(), via_standing_link=True), [])

    def test_a_closed_link_is_refused_unless_the_link_is_not_checked(self):
        link = self.standing(is_open=False)
        self.assertEqual(self.check(link=link, via_standing_link=True), ["This SRP link is closed."])
        self.assertEqual(self.check(link=link, via_standing_link=True, check_link_open=False), [])

    def test_the_default_checks_the_link(self):
        self.assertEqual(self.check(link=self.standing(is_open=False)), ["This SRP link is closed."])


class StandingEntryTests(StandingBase):
    """The paths of the table in INT-10, one test each."""

    def test_preview_and_submit_through_the_link_make_an_out_of_fleet_claim_in_its_category(self):
        link = self.standing()
        preview = self.prepare(self.killmail(), link=link)
        self.assertEqual(preview.blockers, [])
        self.assertEqual((preview.scope, preview.category, preview.link), ("out_of_fleet", self.category, link))

        claim = claims.submit_claim(self.user, preview.token)

        self.assertEqual((claim.scope, claim.category, claim.link, claim.operation), ("out_of_fleet", self.category, link, None))
        self.assertEqual(claim.fleet_label, link.name)
        self.assertEqual(stored_codes(claim), [])

    def test_a_standing_link_works_when_losses_outside_a_fleet_are_switched_off(self):
        self.settings(out_of_fleet_enabled=False)
        claim = self.submit(self.killmail(), link=self.standing())
        self.assertEqual(claim.status, Status.PENDING)

    def test_a_closed_link_takes_no_new_claim(self):
        preview = self.prepare(self.killmail(), link=self.standing(is_open=False))
        self.assertEqual(preview.blockers, ["This SRP link is closed."])

    def test_a_claim_under_a_standing_link_can_be_submitted_again_after_the_link_closed(self):
        self.settings(out_of_fleet_enabled=False)
        link = self.standing()
        claim = self.submit(self.killmail(), link=link)
        claims.withdraw_claim(claim, actor=self.user)
        links.set_link_open(link, actor=self.manager, is_open=False)

        again = claims.resubmit_claim(claim, actor=self.user)

        self.assertEqual(again.status, Status.PENDING)

    def test_a_withdrawn_claim_without_a_link_is_still_held_back_by_the_switch(self):
        claim = self.submit(self.killmail())
        claims.withdraw_claim(claim, actor=self.user)
        self.settings(out_of_fleet_enabled=False)

        with self.assertRaises(SrpError) as caught:
            claims.resubmit_claim(claim, actor=self.user)

        self.assertIn("Only losses in FleetOps fleets are covered.", str(caught.exception))

    def test_a_fleet_claim_can_still_be_submitted_again_after_its_link_closed(self):
        # As in 0.2.0b3: the link is not checked when a claim that exists is submitted again.
        fleet = f.create_operation(self.fc)
        link = f.fleet_link(fleet)
        claim = self.submit(self.killmail(), link=link)
        claims.withdraw_claim(claim, actor=self.user)
        links.set_link_open(link, actor=self.manager, is_open=False)

        self.assertEqual(claims.resubmit_claim(claim, actor=self.user).status, Status.PENDING)

    def test_the_category_of_the_link_wins_over_a_chosen_one(self):
        other = f.srp_category("Solo")
        claim = self.submit(self.killmail(), link=self.standing())
        self.assertNotEqual(claim.category, other)
        preview = claims.prepare_claim(self.user, self.text(self.killmail()), link=self.standing(), category=other)
        self.assertEqual(preview.category, self.category)

    def test_no_pap_or_fc_statistics_follow_from_a_standing_claim(self):
        claim = self.submit(self.killmail(), link=self.standing())
        self.assertIsNone(claim.fc_user)
        self.assertIsNone(claim.operation)


class DayLinkTests(StandingBase):
    """A category chosen without a link: the open standing link of the day of the loss."""

    def test_resolve_finds_the_open_link_of_the_category_and_day(self):
        link = self.standing()
        self.assertEqual(links.resolve_standing_link(self.category, today()), link)

    def test_resolve_ignores_other_days_other_categories_and_closed_links(self):
        self.standing(link_date=today() - timedelta(days=1))
        self.standing(is_open=False)
        self.standing(category=f.srp_category("Solo"))
        self.assertIsNone(links.resolve_standing_link(self.category, today()))
        self.assertIsNone(links.resolve_standing_link(None, today()))

    def test_resolve_takes_the_link_made_last(self):
        self.standing()
        newest = self.standing(name="Second")
        self.assertEqual(links.resolve_standing_link(self.category, today()), newest)

    def test_a_category_claim_goes_through_the_link_of_the_day(self):
        link = self.standing()
        km = self.loss_on(today())
        preview = claims.prepare_claim(self.user, self.text(km), category=self.category)
        self.assertEqual((preview.link, preview.blockers), (link, []))

        claim = claims.submit_claim(self.user, preview.token)

        self.assertEqual((claim.link, claim.category, claim.operation), (link, self.category, None))
        self.assertEqual(stored_codes(claim), [])

    def test_a_category_claim_through_the_link_works_without_losses_outside_a_fleet(self):
        self.settings(out_of_fleet_enabled=False)
        link = self.standing()
        claim = self.submit_category(self.loss_on(today()))
        self.assertEqual(claim.link, link)

    def submit_category(self, km):
        preview = claims.prepare_claim(self.user, self.text(km), category=self.category)
        self.assertTrue(preview.token, preview.blockers)
        return claims.submit_claim(self.user, preview.token)

    def test_without_a_link_the_claim_only_records_the_category(self):
        claim = self.submit_category(self.loss_on(today()))
        self.assertEqual((claim.link, claim.category), (None, self.category))

    def test_a_link_of_another_day_does_not_take_the_claim(self):
        self.standing(link_date=today() - timedelta(days=1))
        claim = self.submit_category(self.loss_on(today()))
        self.assertIsNone(claim.link)

    def test_the_day_of_the_loss_decides_not_the_day_of_the_claim(self):
        yesterday = today() - timedelta(days=1)
        old_link = self.standing(link_date=yesterday)
        claim = self.submit_category(self.loss_on(yesterday))
        self.assertEqual(claim.link, old_link)

    def test_the_setting_can_require_a_link(self):
        self.settings(category_claims_without_link=False)
        preview = claims.prepare_claim(self.user, self.text(self.loss_on(today())), category=self.category)
        self.assertEqual(preview.blockers, ["This category has no open SRP link for the day of the loss."])
        self.assertFalse(preview.eligible)
        self.standing()
        self.assertEqual(claims.prepare_claim(self.user, self.text(self.loss_on(today())), category=self.category).blockers, [])

    def test_the_setting_is_checked_again_when_the_claim_is_submitted(self):
        km = self.loss_on(today())
        preview = claims.prepare_claim(self.user, self.text(km), category=self.category)
        self.settings(category_claims_without_link=False)

        with self.assertRaises(SrpError) as caught:
            claims.submit_claim(self.user, preview.token)

        self.assertEqual(caught.exception.code, "not_eligible")

    def test_a_link_closed_after_the_check_stops_the_submission(self):
        link = self.standing()
        km = self.loss_on(today())
        preview = claims.prepare_claim(self.user, self.text(km), category=self.category)
        links.set_link_open(link, actor=self.manager, is_open=False)

        with self.assertRaises(SrpError) as caught:
            claims.submit_claim(self.user, preview.token)

        self.assertEqual(str(caught.exception), "This SRP link is closed.")
        # Checking the loss again finds no open link and records the category only.
        again = claims.prepare_claim(self.user, self.text(km), category=self.category)
        self.assertEqual((again.link, again.blockers), (None, []))

    def test_a_loss_without_a_category_is_not_looked_up(self):
        self.standing()
        claim = self.submit(self.killmail())
        self.assertIsNone(claim.link)

    def test_a_fleet_claim_never_looks_for_a_standing_link(self):
        self.standing()
        link = f.fleet_link(f.create_operation(self.fc))
        claim = self.submit(self.killmail(), link=link)
        self.assertEqual(claim.link, link)


class LinkDateWarningTests(StandingBase):
    """A loss of another day than the link's date carries ``outside_link_date`` as long as it is under that link."""

    def setUp(self):
        super().setUp()
        self.link = self.standing()
        self.yesterday = today() - timedelta(days=1)

    def test_a_loss_of_the_day_has_no_warning_and_can_be_approved_in_bulk(self):
        claim = self.submit(self.loss_on(today()), link=self.link)
        self.assertEqual(stored_codes(claim), [])
        result = review.approve_clean_claims([claim], actor=self.manager)
        self.assertEqual(len(result.done), 1)

    def test_a_loss_of_the_day_before_has_the_warning_and_bulk_approval_skips_it(self):
        km = self.loss_on(self.yesterday)
        preview = self.prepare(km, link=self.link)
        self.assertIn("outside_link_date", [warning.code for warning in preview.warnings])
        claim = claims.submit_claim(self.user, preview.token)

        self.assertEqual(stored_codes(claim), ["outside_link_date"])
        warning = claim.warnings[0]
        self.assertEqual(warning["level"], "warning")
        self.assertEqual(warning["params"], {"loss_date": self.yesterday.isoformat(), "link_date": today().isoformat()})
        result = review.approve_clean_claims([claim], actor=self.manager)
        self.assertEqual((len(result.done), len(result.skipped)), (0, 1))

    def test_the_warning_is_not_one_of_the_presence_codes(self):
        self.assertNotIn("outside_link_date", claims.PRESENCE_WARNING_CODES)
        self.assertEqual(claims.LINK_WARNING_CODES, frozenset({"outside_link_date"}))

    def test_the_warning_survives_checking_the_presence_again(self):
        claim = self.submit(self.loss_on(self.yesterday), link=self.link)

        review.recheck_presence(claim, actor=self.manager)

        claim.refresh_from_db()
        self.assertIn("outside_link_date", stored_codes(claim))

    def test_the_warning_goes_when_the_claim_leaves_the_link(self):
        claim = self.submit(self.loss_on(self.yesterday), link=self.link)
        fleet = f.create_operation(self.fc, started_at=timezone.now() - timedelta(days=1))
        target = f.fleet_link(fleet)

        review.reclassify_claim(claim, actor=self.manager, scope="in_fleet", link=target, reason="It was a fleet")

        claim.refresh_from_db()
        self.assertNotIn("outside_link_date", stored_codes(claim))

    def test_recategorizing_keeps_the_warning_and_other_warnings(self):
        claim = self.submit(self.loss_on(self.yesterday), link=self.link)
        review.recategorize_claim(claim, actor=self.manager, category=f.srp_category("Solo"), reason="Solo")
        claim.refresh_from_db()
        self.assertIn("outside_link_date", stored_codes(claim))

    def test_the_text_is_shown(self):
        self.assertEqual(
            display.warning_text("outside_link_date", {"loss_date": "2026-11-03", "link_date": "2026-11-04"}),
            "The loss happened on 2026-11-03 EVE; the link is for 2026-11-04.",
        )

    def test_the_utc_day_of_the_loss_counts_not_a_local_day(self):
        # 23:59 UTC the day before is the day before, 00:00 UTC is the day.
        late = self.killmail(time=utc(today().year, today().month, today().day) - timedelta(minutes=1))
        early = self.killmail(time=utc(today().year, today().month, today().day))
        self.assertTrue(claims.outside_link_date(self.link, late.killmail_time))
        self.assertFalse(claims.outside_link_date(self.link, early.killmail_time))

    def test_a_fleet_link_never_has_the_warning(self):
        fleet = f.create_operation(self.fc)
        claim = self.submit(self.killmail(), link=f.fleet_link(fleet))
        self.assertNotIn("outside_link_date", stored_codes(claim))


class MySrpCardTests(SrpPagesTestCase):
    def setUp(self):
        super().setUp()
        self.patch_home_services()
        self.category = f.srp_category("Home defense")

    def test_the_card_lists_the_open_standing_links_newest_first_then_by_category(self):
        solo = f.srp_category("Solo", sort_order=5)
        older = f.standing_link(category=self.category, link_date=timezone.now().date() - timedelta(days=1), name="Older link")
        first = f.standing_link(category=self.category, name="Today home")
        second = f.standing_link(category=solo, name="Today solo")
        f.standing_link(category=self.category, name="Closed one", is_open=False)

        response = self.client.get(reverse("fleetops:srp_home"))

        self.assertContains(response, 'id="srp-standing-links"')
        self.assertEqual([link.pk for link in response.context["standing_links"]], [first.pk, second.pk, older.pk])
        self.assertNotContains(response, "Closed one")
        self.assertContains(response, reverse("fleetops:srp_link", args=[first.code]))

    def test_the_card_is_not_there_without_a_link(self):
        self.assertNotContains(self.client.get(reverse("fleetops:srp_home")), 'id="srp-standing-links"')

    def test_at_most_thirty_links_are_listed(self):
        for number in range(35):
            f.standing_link(category=self.category, name=f"Link {number}", link_date=timezone.now().date() - timedelta(days=number))
        response = self.client.get(reverse("fleetops:srp_home"))
        self.assertEqual(len(response.context["standing_links"]), 30)

    def test_the_card_costs_a_single_query(self):
        for _ in range(3):
            f.standing_link(category=f.srp_category())
        with CaptureQueriesContext(connection) as queries:
            links.open_standing_links()
        self.assertEqual(len(queries), 1)

    def test_the_link_page_shows_the_standing_link_and_checks_a_loss_without_losses_outside_a_fleet(self):
        SrpSettings.objects.filter(pk=1).update(out_of_fleet_enabled=False)
        link = f.standing_link(category=self.category, name="Home defense today", doctrine_text="Muninns")
        page = self.client.get(reverse("fleetops:srp_link", args=[link.code]))
        self.assertContains(page, "Home defense today")
        self.assertContains(page, "doctrine Muninns")
        self.assertContains(page, "Claim a loss through this link")


# --- INT-11: the daily upkeep -----------------------------------------------------------------------------------------


class MaintainTests(TestCase):
    def setUp(self):
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)
        self.srp = f.srp_settings()
        self.now = utc(2026, 11, 4, 0, 5)

    def category(self, name="Home defense", **values):
        values.setdefault("auto_create_daily", True)
        return f.srp_category(name, **values)

    def standing(self, category=None):
        return SrpFleetLink.objects.filter(kind="standing", category=category) if category else SrpFleetLink.objects.filter(kind="standing")

    def test_nothing_is_made_by_default(self):
        f.srp_category("Home defense")
        self.assertEqual(links.maintain_standing_links(self.now), (0, 0))
        self.assertFalse(self.standing().exists())

    def test_a_link_is_made_for_today_with_the_name_and_doctrine_of_the_category(self):
        category = self.category(default_doctrine_text="Muninn", daily_link_name="{category} on {date}")

        created, closed = links.maintain_standing_links(self.now)

        self.assertEqual((created, closed), (1, 0))
        link = self.standing(category).get()
        self.assertEqual((link.link_date, link.name, link.doctrine_text), (utc(2026, 11, 4).date(), "Home defense on 2026-11-04", "Muninn"))
        self.assertEqual((link.auto_created, link.created_by, link.daily_key, link.is_open), (True, None, f"{category.pk}:2026-11-04", True))
        self.assertIsNone(link.fc_user)
        entry = AuditLog.objects.get(action="srp.link_create", object_id=str(link.pk))
        self.assertIsNone(entry.actor)
        self.assertTrue(entry.new_value["auto_created"])

    def test_running_twice_makes_one_link_per_category(self):
        category = self.category()
        other = self.category("Solo")
        links.maintain_standing_links(self.now)
        self.assertEqual(links.maintain_standing_links(self.now + timedelta(hours=1)), (0, 0))
        self.assertEqual((self.standing(category).count(), self.standing(other).count()), (1, 1))

    def test_a_category_that_is_switched_off_or_does_not_ask_for_it_gets_none(self):
        self.category("Off", is_active=False)
        self.category("Manual", auto_create_daily=False)
        links.maintain_standing_links(self.now)
        self.assertFalse(self.standing().exists())

    def test_a_link_of_the_day_made_by_hand_is_enough(self):
        category = self.category()
        f.standing_link(category=category, link_date=utc(2026, 11, 4).date(), name="By hand")
        self.assertEqual(links.maintain_standing_links(self.now), (0, 0))
        self.assertEqual(self.standing(category).count(), 1)

    def test_a_link_is_not_made_while_srp_is_off(self):
        self.category()
        self.srp.enabled = False
        self.srp.save()
        self.assertEqual(links.maintain_standing_links(self.now), (0, 0))

    def test_a_category_switched_off_stops_the_links_the_next_day(self):
        category = self.category()
        links.maintain_standing_links(self.now)
        category.is_active = False
        category.save()

        links.maintain_standing_links(self.now + timedelta(days=1))

        self.assertEqual(self.standing(category).count(), 1)

    def test_a_beat_that_was_down_makes_today_only(self):
        category = self.category()
        # The beat stopped at 00:00 and is back at 02:10 of the next day: yesterday's link is not made afterwards.
        links.maintain_standing_links(utc(2026, 11, 5, 2, 10))
        link = self.standing(category).get()
        self.assertEqual(link.link_date, utc(2026, 11, 5).date())

    def test_no_link_is_made_after_its_closing_time_has_passed(self):
        # L-13: with a 12 hour closing time the first upkeep of the day at 14:30 used to make the link and close it
        # in the same run; now it makes none (and a run before the closing time still makes one).
        category = self.category(auto_close_after_hours=12)

        self.assertEqual(links.maintain_standing_links(utc(2026, 11, 4, 14, 30)), (0, 0))
        self.assertFalse(self.standing(category).exists())
        self.assertFalse(AuditLog.objects.filter(action="srp.link_close").exists())

        # The skip repeats with every upkeep run of the day, so it is not logged at info level.
        with self.assertNoLogs(links.logger, level="INFO"):
            links.maintain_standing_links(utc(2026, 11, 4, 14, 40))

        self.assertEqual(links.maintain_standing_links(utc(2026, 11, 4, 11, 59)), (1, 0))
        self.assertTrue(self.standing(category).get().is_open)

    def test_the_eve_date_is_the_utc_date(self):
        category = self.category()
        links.maintain_standing_links(utc(2026, 11, 4, 23, 59))
        links.maintain_standing_links(utc(2026, 11, 5, 0, 0))
        self.assertEqual(
            [str(date) for date in self.standing(category).order_by("link_date").values_list("link_date", flat=True)],
            ["2026-11-04", "2026-11-05"],
        )

    # --- Closing

    def test_a_link_stays_open_when_the_category_never_closes_links(self):
        category = self.category()
        links.maintain_standing_links(self.now)
        link = self.standing(category).get()
        self.assertIsNone(link.auto_close_at)

        links.maintain_standing_links(self.now + timedelta(days=30))

        link.refresh_from_db()
        self.assertTrue(link.is_open)

    def test_forty_eight_hours_close_the_link_at_the_end_of_the_next_day(self):
        category = self.category(auto_close_after_hours=48)
        links.maintain_standing_links(self.now)
        link = self.standing(category).get()
        self.assertEqual(link.auto_close_at, utc(2026, 11, 6, 0, 0))

        self.assertEqual(links.maintain_standing_links(utc(2026, 11, 5, 23, 59))[1], 0)
        link.refresh_from_db()
        self.assertTrue(link.is_open)
        # The upkeep at 2026-11-06 00:00 closes it (and, as it runs, makes the link of that day).
        self.assertEqual(links.maintain_standing_links(utc(2026, 11, 6, 0, 0))[1], 1)

        link.refresh_from_db()
        self.assertFalse(link.is_open)
        self.assertIsNotNone(link.closed_at)
        self.assertIsNone(link.closed_by)
        entry = AuditLog.objects.get(action="srp.link_close", object_id=str(link.pk))
        self.assertIsNone(entry.actor)
        self.assertEqual(entry.reason, "Closed automatically at the time set for the link.")

    def test_a_link_made_by_hand_closes_at_its_own_time_too(self):
        category = self.category()
        link = f.standing_link(category=category, auto_close_at=self.now - timedelta(minutes=1))
        other = f.standing_link(category=category, auto_close_at=self.now + timedelta(hours=1), name="Later")
        kept = f.standing_link(category=category, name="Never")

        self.assertEqual(links.maintain_standing_links(self.now)[1], 1)

        for item, is_open in ((link, False), (other, True), (kept, True)):
            item.refresh_from_db()
            self.assertEqual(item.is_open, is_open)

    def test_a_closed_link_is_not_closed_again(self):
        category = self.category()
        link = f.standing_link(category=category, is_open=False, auto_close_at=self.now - timedelta(hours=1))
        self.assertEqual(links.maintain_standing_links(self.now)[1], 0)
        self.assertFalse(AuditLog.objects.filter(action="srp.link_close", object_id=str(link.pk)).exists())

    def test_closing_leaves_the_claims_alone(self):
        category = self.category()
        user = f.create_user("closing-pilot", perms=f.SRP_MEMBER_PERMS)
        link = f.standing_link(category=category, auto_close_at=self.now - timedelta(hours=1))
        claim = f.srp_claim(user, link=link)

        links.maintain_standing_links(self.now)

        claim.refresh_from_db()
        self.assertEqual((claim.status, claim.link), (Status.PENDING, link))

    def test_closing_works_while_srp_is_off(self):
        category = self.category()
        link = f.standing_link(category=category, auto_close_at=self.now - timedelta(hours=1))
        self.srp.enabled = False
        self.srp.save()

        links.maintain_standing_links(self.now)

        link.refresh_from_db()
        self.assertFalse(link.is_open)

    def test_the_auto_close_time_helper(self):
        category = self.category(auto_close_after_hours=24)
        self.assertEqual(links.auto_close_time(category, utc(2026, 11, 4).date()), utc(2026, 11, 5))
        self.assertIsNone(links.auto_close_time(self.category("None"), utc(2026, 11, 4).date()))

    @tag("catalog")
    def test_the_reason_is_shown_in_the_language_of_the_reader(self):
        category = self.category()
        link = f.standing_link(category=category, auto_close_at=self.now - timedelta(hours=1))
        links.maintain_standing_links(self.now)
        reason = AuditLog.objects.get(action="srp.link_close", object_id=str(link.pk)).reason
        with translation.override("zh-hans"):
            self.assertNotEqual(texts.translate_stored(reason), reason)
        with translation.override("en"):
            self.assertEqual(texts.translate_stored(reason), reason)


class MaintainRaceTests(TransactionTestCase):
    """Two workers: the daily key lets only one link in, and the second call gets the first one back."""

    # Without it the teardown flush loses the migration rows and breaks later serialized tests (any test order).
    serialized_rollback = True

    def test_the_class_keeps_the_migration_rows_for_later_tests(self):
        self.assertTrue(type(self).serialized_rollback)
        self.assertTrue(SrpSettings.objects.filter(pk=1).exists())

    def test_the_second_creation_returns_the_existing_link(self):
        f.srp_settings()
        category = f.srp_category("Home defense", auto_create_daily=True)
        day = utc(2026, 11, 4).date()

        first = links.create_standing_link(actor=None, category=category, link_date=day, daily=True, auto_created=True)
        second = links.create_standing_link(actor=None, category=category, link_date=day, daily=True, auto_created=True)

        self.assertEqual(first.pk, second.pk)
        self.assertEqual(SrpFleetLink.objects.filter(daily_key=f"{category.pk}:2026-11-04").count(), 1)
        self.assertEqual(AuditLog.objects.filter(action="srp.link_create").count(), 1)

    def test_a_worker_that_checked_too_early_still_makes_one_link(self):
        f.srp_settings()
        category = f.srp_category("Home defense", auto_create_daily=True)
        now = utc(2026, 11, 4, 0, 5)
        links.create_standing_link(actor=None, category=category, link_date=now.date(), daily=True, auto_created=True)
        # The existence check of the second worker ran before the first one saved: it finds nothing.
        with mock.patch.object(links, "_has_standing_link", return_value=False):
            links.maintain_standing_links(now)

        self.assertEqual(SrpFleetLink.objects.filter(kind="standing").count(), 1)
        self.assertEqual(AuditLog.objects.filter(action="srp.link_create").count(), 1)

    def test_the_database_refuses_a_second_link_with_the_same_key(self):
        category = f.srp_category("Home defense")
        key = f"{category.pk}:2026-11-04"
        f.standing_link(category=category, daily_key=key)
        with self.assertRaises(IntegrityError):
            f.standing_link(category=category, daily_key=key)


class BeatHookTests(TestCase):
    def setUp(self):
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)

    def beat(self):
        # Only the standing-link upkeep is under test: the other SRP jobs of the beat (the insurance read queued once a
        # day while SRP is on, the loss scan) would otherwise run here and reach ESI.
        quiet = ("_queue_srp_sweep", "cancel_abandoned_starts", "_queue_srp_insurance", "_queue_srp_loss_scan")
        with contextlib.ExitStack() as stack:
            for name in quiet:
                stack.enter_context(mock.patch.object(fleet_tasks, name))
            return fleet_tasks.schedule_active_fleet_tracking()

    def test_the_beat_runs_the_upkeep_at_most_every_ten_minutes(self):
        with mock.patch.object(links, "maintain_standing_links") as upkeep:
            self.beat()
            self.beat()
        upkeep.assert_called_once()
        self.assertEqual(links.STANDING_LINKS_KEY, "fleetops:srp:standing-links")
        self.assertEqual(links.STANDING_LINKS_SECONDS, 600)

    def test_a_failing_upkeep_is_logged_and_does_not_stop_the_beat(self):
        with mock.patch.object(links, "maintain_standing_links", side_effect=RuntimeError("boom")), self.assertLogs(
            fleet_tasks.logger, "WARNING"
        ) as logs:
            self.assertEqual(self.beat(), 0)
        self.assertTrue(any("SRP standing links" in line for line in logs.output))
        # The next beat may try again at once.
        self.assertIsNone(cache.get(links.STANDING_LINKS_KEY))

    def test_the_beat_makes_the_link_of_the_day(self):
        f.srp_settings()
        category = f.srp_category("Home defense", auto_create_daily=True)
        self.beat()
        self.assertEqual(SrpFleetLink.objects.filter(kind="standing", category=category).count(), 1)

    def test_nothing_is_scheduled_by_the_app_itself(self):
        # The beat entry is the administrator's (docs/configuration.md); the app creates no periodic task.
        import inspect

        source = inspect.getsource(links) + inspect.getsource(fleet_tasks)
        for word in ("PeriodicTask", "CrontabSchedule", "IntervalSchedule"):
            self.assertNotIn(word, source)


# --- INT-12: the deadline -----------------------------------------------------------------------------------------


class DeadlineTests(StandingBase):
    def test_a_new_site_has_no_deadline(self):
        self.assertEqual(SrpSettings._meta.get_field("claim_deadline_days").default, 0)

    def check(self, km, now):
        return rules.eligibility(km=km, user=self.user, scope="out_of_fleet", link=None, settings_obj=SrpSettings.get_solo(), now=now)

    def test_with_seven_days_day_six_is_possible_and_day_eight_is_not(self):
        self.settings(claim_deadline_days=7)
        now = utc(2026, 11, 10, 12)
        six = self.killmail(time=now - timedelta(days=6))
        eight = self.killmail(time=now - timedelta(days=8))
        self.assertEqual(self.check(six, now), [])
        self.assertEqual(self.check(eight, now), ["Losses older than 7 days cannot be submitted."])

    def test_the_exact_edge(self):
        self.settings(claim_deadline_days=7)
        now = utc(2026, 11, 10, 12)
        self.assertEqual(self.check(self.killmail(time=now - timedelta(days=7)), now), [])
        self.assertTrue(self.check(self.killmail(time=now - timedelta(days=7, seconds=1)), now))

    def test_the_deadline_applies_through_a_standing_link_too(self):
        self.settings(claim_deadline_days=7)
        old = self.killmail(time=timezone.now() - timedelta(days=9))
        preview = self.prepare(old, link=self.standing())
        self.assertEqual(preview.blockers, ["Losses older than 7 days cannot be submitted."])

    def test_a_snapshot_keeps_the_deadline_it_was_made_with(self):
        self.settings(claim_deadline_days=7)
        claim = self.submit(self.killmail())
        self.assertEqual(claim.policy["claim_deadline_days"], 7)

    def test_changing_the_deadline_does_not_make_pending_claims_outdated(self):
        self.submit(self.killmail())
        self.settings(claim_deadline_days=7)
        self.assertEqual(rules.outdated_pending_claim_count(), 0)

    def test_reopening_is_not_checked_against_the_deadline(self):
        self.settings(claim_deadline_days=7)
        claim = self.submit(self.killmail())
        review.reject_claim(claim, actor=self.manager, reason="No")
        SrpClaim.objects.filter(pk=claim.pk).update(killmail_time=timezone.now() - timedelta(days=30))
        claim.refresh_from_db()
        self.assertEqual(review.reopen_claim(claim, actor=self.manager, reason="Appeal").status, Status.PENDING)


class DeadlinePageTests(SrpPagesTestCase):
    def test_the_deadline_is_shown_on_my_srp_and_the_link_page_when_set(self):
        self.patch_home_services()
        SrpSettings.objects.filter(pk=1).update(claim_deadline_days=7)
        for url in (reverse("fleetops:srp_home"), reverse("fleetops:srp_link", args=[self.link.code])):
            response = self.client.get(url)
            self.assertContains(response, 'id="srp-deadline"')
            self.assertContains(response, "within 7 days after it happened")

    def test_one_day_is_in_the_singular(self):
        self.patch_home_services()
        SrpSettings.objects.filter(pk=1).update(claim_deadline_days=1)
        self.assertContains(self.client.get(reverse("fleetops:srp_home")), "within 1 day after it happened")

    def test_nothing_is_shown_without_a_deadline(self):
        self.patch_home_services()
        self.assertNotContains(self.client.get(reverse("fleetops:srp_home")), 'id="srp-deadline"')
