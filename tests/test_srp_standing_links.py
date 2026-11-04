# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Standing SRP links and the link management page: the model (INT-7), what changes where a link has no fleet
(INT-8), and creating, changing, closing, completing and listing links (INT-9 and REV-7).

ESI and zKillboard are never reached; killmails come from the factories.
"""

from datetime import date, timedelta, timezone as dt_timezone
from unittest import mock

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from fleetops import admin as fleetops_admin
from fleetops import texts
from fleetops.models import AuditLog, SrpCategory, SrpClaim, SrpFleetLink, SrpSettings
from fleetops.srp import access, claims, display, links, presence, review, summaries
from fleetops.srp import policy as rules
from fleetops.srp.types import SrpError, SrpStateError
from fleetops.validators import render_link_name, validate_link_name_template

from . import factories as f
from .test_srp_claims import ClaimBase
from .test_srp_views import SrpPagesTestCase, messages_of, route

Status = SrpClaim.Status
State = SrpFleetLink.State
TODAY = timezone.now().astimezone(dt_timezone.utc).date()


def audits(action, link=None):
    entries = AuditLog.objects.filter(action=action)
    if link is not None:
        entries = entries.filter(object_type="SrpFleetLink", object_id=str(link.pk))
    return entries


class StandingLinkBase(TestCase):
    """Reviewer, payer, manager, FC and a member, one category and an SRP-enabled site."""

    @classmethod
    def setUpTestData(cls):
        cls.srp = f.srp_settings()
        cls.reviewer = f.create_user("link_reviewer", perms=f.SRP_REVIEWER_PERMS)
        cls.payer = f.create_user("link_payer", perms=f.SRP_PAYER_PERMS)
        cls.manager = f.create_user("link_manager", perms=f.SRP_MANAGER_PERMS)
        cls.fc = f.create_user("link_fc", perms=f.FC_PERMS)
        cls.member = f.create_user("link_member", perms=f.SRP_MEMBER_PERMS)
        cls.category = f.srp_category("Home defense")

    def standing(self, **values):
        values.setdefault("category", self.category)
        return f.standing_link(**values)

    def create(self, **values):
        values.setdefault("actor", self.reviewer)
        values.setdefault("category", self.category)
        values.setdefault("link_date", TODAY)
        return links.create_standing_link(**values)


# --- INT-7: the model -----------------------------------------------------------------------------------------


class LinkModelTests(StandingLinkBase):
    def test_a_fleet_link_is_a_fleet_link_by_default(self):
        link = f.fleet_link(f.create_operation(self.fc))

        self.assertEqual((link.kind, link.origin), ("fleet", "fleetops"))
        self.assertIsNone(link.daily_key)
        self.assertIsNone(link.external_id)
        self.assertFalse(link.auto_created)
        self.assertEqual(link.name, "")

    def test_the_state_follows_open_and_completed(self):
        link = self.standing()
        self.assertEqual(link.state, State.OPEN)

        link.is_open = False
        self.assertEqual(link.state, State.CLOSED)

        link.completed_at = timezone.now()
        self.assertEqual(link.state, State.COMPLETED)

        # Open is the only thing that matters for submitting: an open link is open, whatever else is set.
        link.is_open = True
        self.assertEqual(link.state, State.OPEN)

    def test_the_string_of_a_standing_link_is_its_name(self):
        self.assertEqual(str(self.standing(name="Home defense 2026-11-05")), "Home defense 2026-11-05")

    def test_the_string_of_a_fleet_link_and_of_one_without_a_fleet(self):
        operation = f.create_operation(self.fc)
        self.assertEqual(str(f.fleet_link(operation)), f"SRP link of fleet {operation.pk}")
        imported = SrpFleetLink(code="abc123", origin="aasrp")
        self.assertEqual(str(imported), "SRP link abc123")

    def test_clean_of_a_standing_link(self):
        link = SrpFleetLink(kind="standing", code="x", name="Solo 2026-11-05", link_date=TODAY, category=self.category)
        link.clean()

        for values, field in (
            ({"name": " "}, "name"),
            ({"link_date": None}, "link_date"),
            ({"operation": f.create_operation(self.fc)}, "operation"),
        ):
            with self.subTest(field=field):
                broken = SrpFleetLink(
                    kind="standing", code="x", **{"name": "Solo", "link_date": TODAY, **values}
                )
                with self.assertRaises(ValidationError) as caught:
                    broken.clean()
                self.assertIn(field, caught.exception.message_dict)

    def test_clean_of_a_fleet_link_needs_a_fleet_unless_it_was_imported(self):
        with self.assertRaises(ValidationError) as caught:
            SrpFleetLink(kind="fleet", code="x").clean()
        self.assertIn("operation", caught.exception.message_dict)

        SrpFleetLink(kind="fleet", code="x", origin="aasrp").clean()
        SrpFleetLink(kind="fleet", code="x", operation=f.create_operation(self.fc)).clean()

    def test_the_daily_key_is_unique(self):
        key = f"{self.category.pk}:{TODAY.isoformat()}"
        self.standing(daily_key=key)

        with self.assertRaises(IntegrityError), transaction.atomic():
            self.standing(daily_key=key)

    def test_links_without_a_daily_key_or_external_id_do_not_clash(self):
        # The unique columns hold NULL, not an empty string (MariaDB would treat two empty strings as equal).
        first = self.standing(daily_key="", external_id="")
        second = self.standing(daily_key="", external_id="")

        first.refresh_from_db(), second.refresh_from_db()
        self.assertIsNone(first.daily_key)
        self.assertIsNone(second.external_id)

    def test_the_external_id_is_unique(self):
        self.standing(external_id="aasrp-1")

        with self.assertRaises(IntegrityError), transaction.atomic():
            self.standing(external_id="aasrp-1")

    def test_the_report_address_takes_http_and_https_only(self):
        for address, valid in (
            ("https://example.org/aar", True),
            ("http://example.org/aar", True),
            ("javascript:alert(1)", False),
            ("data:text/html,x", False),
            ("ftp://example.org/aar", False),
        ):
            with self.subTest(address=address):
                link = SrpFleetLink(
                    kind="standing", code="x", name="n", link_date=TODAY, category=self.category, aar_url=address
                )
                if valid:
                    link.full_clean(exclude=["code"])
                    self.assertTrue(link.has_aar_link)
                else:
                    with self.assertRaises(ValidationError) as caught:
                        link.full_clean(exclude=["code"])
                    self.assertIn("aar_url", caught.exception.message_dict)

    def test_the_names_of_the_model_changed(self):
        self.assertEqual(str(SrpFleetLink._meta.verbose_name), "SRP link")

    def test_the_category_cannot_be_deleted_while_a_link_uses_it(self):
        self.standing()
        from django.db.models import ProtectedError

        with self.assertRaises(ProtectedError):
            self.category.delete()

    def test_deleting_a_link_keeps_its_claims(self):
        link = self.standing()
        claim = f.srp_claim(self.member, link=link)

        link.delete()

        claim.refresh_from_db()
        self.assertIsNone(claim.link_id)


class CategoryDailyFieldsTests(StandingLinkBase):
    def test_defaults(self):
        category = f.srp_category()

        self.assertFalse(category.auto_create_daily)
        self.assertEqual(category.daily_link_name, "{category} {date}")
        self.assertEqual(category.default_doctrine_text, "")
        self.assertIsNone(category.auto_close_after_hours)

    def test_the_settings_default_is_off(self):
        row = SrpSettings.get_solo()

        self.assertFalse(row.auto_complete_links)
        self.assertTrue(row.category_claims_without_link)

    def test_the_name_template_only_takes_category_and_date(self):
        for template in ("{category} {date}", "Defense {date}", "{{literal}} {category}", ""):
            with self.subTest(template=template):
                validate_link_name_template(template)

        for template in ("{other}", "{category.name}", "{category!r}", "{date:>9999999}", "{0}", "{category", "}"):
            with self.subTest(template=template), self.assertRaises(ValidationError):
                validate_link_name_template(template)

    def test_a_bad_template_is_refused_by_the_category(self):
        category = f.srp_category()
        category.daily_link_name = "{secret}"

        with self.assertRaises(ValidationError) as caught:
            category.clean()
        self.assertIn("daily_link_name", caught.exception.message_dict)

    def test_the_template_is_filled_in(self):
        self.assertEqual(
            render_link_name("{category} {date}", category="Home defense", date="2026-11-05"),
            "Home defense 2026-11-05",
        )
        # A template that cannot be used falls back to the default one.
        self.assertEqual(render_link_name("{bad}", category="HD", date="2026-11-05"), "HD 2026-11-05")


# --- INT-8: a link without a fleet ----------------------------------------------------------------------------


class NoFleetTests(ClaimBase):
    """One test per place that used ``link.operation``."""

    def setUp(self):
        super().setUp()
        self.category = f.srp_category("Home defense")
        self.link = f.standing_link(category=self.category, name="Home defense today")

    def test_fleet_fields_name_the_link_and_have_no_fc(self):
        values = claims.fleet_fields(self.link, self.category)

        self.assertEqual(values["link"], self.link)
        self.assertIsNone(values["operation"])
        self.assertEqual(values["fleet_label"], "Home defense today")
        self.assertIsNone(values["fc_user"])
        self.assertEqual(values["fc_main_character_name"], "")
        self.assertEqual(values["category"], self.category)

    def test_the_fleet_check_does_not_apply(self):
        km = self.killmail()

        result = claims.loss_presence(SrpClaim.Scope.IN_FLEET, self.link, km)

        self.assertEqual(result.code, display.NOT_APPLICABLE)

    def test_a_preview_through_the_link_is_out_of_fleet_in_its_category(self):
        preview = self.prepare(self.killmail(), link=self.link)

        self.assertEqual(preview.scope, SrpClaim.Scope.OUT_OF_FLEET)
        self.assertEqual(preview.category, self.category)
        self.assertEqual(preview.presence.code, display.NOT_APPLICABLE)
        self.assertEqual(preview.blockers, [])

    def test_a_claim_through_the_link_is_out_of_fleet_and_in_no_fc_statistics(self):
        claim = self.submit(self.killmail(), link=self.link)

        self.assertEqual(claim.scope, SrpClaim.Scope.OUT_OF_FLEET)
        self.assertEqual(claim.link, self.link)
        self.assertIsNone(claim.operation_id)
        self.assertEqual(claim.category, self.category)
        self.assertEqual(claim.fleet_label, "Home defense today")
        self.assertIsNone(claim.fc_user_id)
        self.assertEqual(claim.fleet_check, display.NOT_APPLICABLE)

    def test_the_payout_rule_check_works_without_a_fleet(self):
        # The rule match for a link without a fleet takes no doctrine (``match_rule(operation=None)``).
        f.payout_rule(f.RIFTER, 40_000_000)

        preview = self.prepare(self.killmail(fitted="900000000"), link=self.link)

        self.assertEqual(preview.calculation.suggested, 40_000_000)
        self.assertEqual(preview.calculation.basis, "rule")

    def test_a_closed_link_blocks_the_claim(self):
        self.link.is_open = False
        self.link.save()

        preview = self.prepare(self.killmail(), link=self.link)

        self.assertEqual(preview.blockers, [str(rules.BLOCK_LINK_CLOSED)])

    def test_a_link_closed_after_the_check_stops_the_submission(self):
        km = self.killmail()
        preview = self.prepare(km, link=self.link)
        SrpFleetLink.objects.filter(pk=self.link.pk).update(is_open=False)

        with self.assertRaises(SrpError) as caught:
            claims.submit_claim(self.user, preview.token)

        self.assertEqual(caught.exception.code, "not_eligible")

    def test_a_claim_cannot_be_moved_into_a_standing_link(self):
        claim = self.submit(self.killmail())
        fleet_link = f.fleet_link(f.create_operation(self.fc))

        for target in (self.link, SrpFleetLink(pk=0)):
            with self.subTest(target=target.pk), self.assertRaises(SrpError) as caught:
                review.reclassify_claim(
                    claim, actor=self.manager, scope=SrpClaim.Scope.IN_FLEET, link=target, reason="Wrong place"
                )
            self.assertEqual(caught.exception.code, "link_required")

        review.reclassify_claim(
            claim, actor=self.manager, scope=SrpClaim.Scope.IN_FLEET, link=fleet_link, reason="It was a fleet"
        )
        claim.refresh_from_db()
        self.assertEqual(claim.link, fleet_link)

    def test_a_claim_of_a_standing_link_can_move_out_and_keeps_working(self):
        claim = self.submit(self.killmail(), link=self.link)

        review.recategorize_claim(claim, actor=self.manager, category=None, reason="No category")

        claim.refresh_from_db()
        self.assertIsNone(claim.category)
        self.assertEqual(claim.link, self.link)

    def test_member_links_leave_out_standing_links(self):
        operation, fleet_link = self.fleet()

        listed = links.recent_links_for_user(self.user)

        self.assertEqual(listed, [fleet_link])
        self.assertNotIn(self.link, listed)

    def test_the_candidates_of_a_move_leave_out_standing_links(self):
        from fleetops.views_srp import link_candidates

        operation, fleet_link = self.fleet()
        claim = f.srp_claim(self.user, time=operation.started_at + timedelta(minutes=10))

        self.assertEqual(link_candidates(claim), [fleet_link])

    def test_the_fleet_finder_never_returns_a_standing_link(self):
        operation, fleet_link = self.fleet()

        found = presence.find_fleet_for_loss(self.main.character_id, operation.started_at + timedelta(minutes=5))

        self.assertEqual(found, fleet_link)
        self.assertFalse(found.is_standing)

    def test_claims_of_a_standing_link_stay_out_of_the_fc_statistics(self):
        self.submit(self.killmail(), link=self.link)
        year, month = TODAY.year, TODAY.month

        self.assertEqual(summaries.fc_fleets(self.fc, year, month), [])
        totals = summaries.month_totals(
            year, month, scope=SrpClaim.Scope.IN_FLEET, base=SrpClaim.objects.filter(fc_user=self.fc)
        )
        self.assertEqual(totals.claims, 0)
        # Nobody leads a standing link, so the person who made it is no FC.
        self.assertFalse(SrpFleetLink.objects.filter(fc_user__isnull=False, kind="standing").exists())


class NoFleetPageTests(SrpPagesTestCase):
    def setUp(self):
        super().setUp()
        self.category = f.srp_category("Home defense")
        self.standing = f.standing_link(
            category=self.category,
            name="Home defense 2026-11-05",
            link_date=date(2026, 11, 5),
            doctrine_text="Muninns",
            aar_url="https://example.org/aar/1",
        )

    def test_the_link_page_shows_name_category_date_doctrine_and_report(self):
        response = self.client.get(route("srp_link", self.standing.code))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Home defense 2026-11-05")
        self.assertContains(response, "Home defense")
        self.assertContains(response, "2026-11-05")
        self.assertContains(response, "Muninns")
        self.assertContains(response, 'href="https://example.org/aar/1"')
        self.assertContains(response, 'rel="noopener noreferrer"')
        self.assertContains(response, "Claim a loss through this link")
        self.assertNotContains(response, "FC ")

    def test_a_report_address_that_is_not_http_is_not_a_link(self):
        SrpFleetLink.objects.filter(pk=self.standing.pk).update(aar_url="javascript:alert(1)")

        response = self.client.get(route("srp_link", self.standing.code))

        self.assertNotContains(response, "javascript:")

    def test_a_closed_standing_link_says_so(self):
        SrpFleetLink.objects.filter(pk=self.standing.pk).update(is_open=False)

        response = self.client.get(route("srp_link", self.standing.code))

        self.assertContains(response, "This SRP link is closed.")
        self.assertNotContains(response, "Check loss")

    def test_the_preview_through_a_standing_link_names_it_and_goes_back_to_it(self):
        km = f.srp_killmail(f.main_of(self.member), fitted="100000000")
        with mock.patch.object(claims.killmails, "fetch_killmail", return_value=km):
            response = self.client.post(
                route("srp_link", self.standing.code),
                {"killmail": f"<url=killReport:{km.killmail_id}:{km.killmail_hash}>Loss</url>"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Loss through the SRP link Home defense 2026-11-05")
        self.assertContains(response, f'href="{route("srp_link", self.standing.code)}"')
        self.assertNotContains(response, "Fleet presence:")

    def test_the_claim_page_names_the_link(self):
        claim = f.srp_claim(self.member, link=self.standing)

        response = self.client.get(route("srp_claim", claim.uuid))

        self.assertContains(response, "Outside a FleetOps fleet")
        self.assertContains(response, "Home defense 2026-11-05")

    def test_closing_a_standing_link_goes_back_to_the_link_list(self):
        response = self.client_for(self.manager).post(route("srp_link_close", self.standing.code))

        self.assertRedirects(response, route("srp_links"), fetch_redirect_response=False)
        self.standing.refresh_from_db()
        self.assertFalse(self.standing.is_open)

    def test_the_admin_lists_the_new_columns(self):
        model_admin = fleetops_admin.SrpFleetLinkAdmin
        for name in ("kind", "name", "link_date", "category"):
            self.assertIn(name, model_admin.list_display)
        self.assertIn("kind", model_admin.list_filter)
        self.assertIn("category", model_admin.list_filter)


# --- INT-9 and REV-7: services --------------------------------------------------------------------------------


class CreateStandingLinkTests(StandingLinkBase):
    def test_a_link_is_made_without_a_fleet_and_audited(self):
        link = self.create(name="Home defense 2026-11-05", doctrine_text="Muninns", aar_url="https://example.org/a")

        self.assertEqual(link.kind, "standing")
        self.assertIsNone(link.operation_id)
        self.assertEqual((link.category, link.link_date), (self.category, TODAY))
        self.assertTrue(link.is_open)
        self.assertEqual(link.created_by, self.reviewer)
        # Nobody is the FC of a standing link.
        self.assertIsNone(link.fc_user_id)
        self.assertIsNone(link.fc_main_character_id)
        self.assertEqual(link.fc_main_character_name, "")
        entry = audits("srp.link_create", link).get()
        self.assertEqual(entry.actor, self.reviewer)
        self.assertEqual(entry.new_value["category_id"], self.category.pk)
        self.assertEqual(entry.new_value["kind"], "standing")
        self.assertEqual(entry.new_value["link_date"], TODAY.isoformat())

    def test_an_empty_name_comes_from_the_category_template(self):
        category = f.srp_category("Solo PVP", daily_link_name="{category} on {date}")

        link = self.create(category=category, name="", link_date=date(2026, 11, 5))

        self.assertEqual(link.name, "Solo PVP on 2026-11-05")

    def test_the_default_template_is_category_and_date(self):
        link = self.create(name="  ", link_date=date(2026, 11, 5))

        self.assertEqual(link.name, "Home defense 2026-11-05")

    def test_the_date_is_today_in_eve_time_by_default(self):
        link = links.create_standing_link(actor=self.reviewer, name="x", category=self.category)

        self.assertEqual(link.link_date, links.eve_today())

    def test_only_reviewers_make_links(self):
        for user in (self.payer, self.fc, self.member):
            with self.subTest(user=user.username), self.assertRaises(SrpError) as caught:
                self.create(actor=user)
            self.assertEqual(caught.exception.code, "forbidden")
        self.assertFalse(SrpFleetLink.objects.exists())

        self.create(actor=self.manager)
        self.create(actor=None)
        self.assertEqual(SrpFleetLink.objects.count(), 2)

    def test_a_category_is_needed(self):
        with self.assertRaises(SrpError) as caught:
            self.create(category=None)
        self.assertEqual(caught.exception.code, "category_required")

    def test_a_report_address_that_is_not_http_is_refused(self):
        for address in ("javascript:alert(1)", "ftp://example.org/x", "data:text/plain,hi"):
            with self.subTest(address=address), self.assertRaises(SrpError) as caught:
                self.create(aar_url=address)
            self.assertEqual(caught.exception.code, "invalid")
        self.assertFalse(SrpFleetLink.objects.exists())

    def test_a_daily_link_is_made_once_per_category_and_day(self):
        first = self.create(daily=True)
        again = self.create(daily=True)
        other_day = self.create(daily=True, link_date=TODAY - timedelta(days=1))
        other_category = self.create(daily=True, category=f.srp_category("Solo PVP"))

        self.assertEqual(again.pk, first.pk)
        self.assertEqual(first.daily_key, f"{self.category.pk}:{TODAY.isoformat()}")
        self.assertEqual(len({first.pk, other_day.pk, other_category.pk}), 3)
        self.assertEqual(audits("srp.link_create").count(), 3)

    def test_a_daily_link_that_lost_the_race_returns_the_winner(self):
        winner = self.create(daily=True, auto_created=True, actor=None)
        # The same key arrives again after the check of another worker: the insert fails and the first link is returned.
        loser = self.create(daily=True, name="Other name")

        self.assertEqual(loser.pk, winner.pk)
        self.assertEqual(SrpFleetLink.objects.count(), 1)

    def test_links_without_the_daily_flag_can_share_a_day(self):
        self.create()
        self.create()

        self.assertEqual(SrpFleetLink.objects.filter(daily_key__isnull=True).count(), 2)

    def test_the_code_collision_is_retried(self):
        taken = self.create()
        codes = iter([taken.code, "fresh-code-1"])
        with mock.patch.object(links.secrets, "token_urlsafe", side_effect=lambda n: next(codes)):
            link = self.create()

        self.assertEqual(link.code, "fresh-code-1")


class UpdateLinkTests(StandingLinkBase):
    def test_a_standing_link_changes_and_the_change_is_audited(self):
        link = self.create(name="Old")
        other = f.srp_category("Solo PVP")

        links.update_link(
            link,
            actor=self.reviewer,
            name="New",
            category=other,
            doctrine_text="Rifters",
            aar_url="https://example.org/aar",
            auto_close_at=timezone.now() + timedelta(hours=5),
            link_date=TODAY - timedelta(days=1),
        )

        link.refresh_from_db()
        self.assertEqual((link.name, link.category, link.doctrine_text), ("New", other, "Rifters"))
        entry = audits("srp.link_update", link).get()
        self.assertEqual(entry.old_value["name"], "Old")
        self.assertEqual(entry.new_value["name"], "New")
        self.assertEqual(entry.new_value["category"], other.pk)

    def test_nothing_changes_and_nothing_is_audited_without_a_difference(self):
        link = self.create(name="Same")

        links.update_link(link, actor=self.reviewer, name="Same", doctrine_text="")

        self.assertFalse(audits("srp.link_update").exists())

    def test_a_category_change_leaves_the_claims_where_they_are(self):
        link = self.create()
        claim = f.srp_claim(self.member, link=link)
        other = f.srp_category("Solo PVP")

        links.update_link(link, actor=self.reviewer, category=other)

        claim.refresh_from_db()
        self.assertEqual(claim.category, self.category)
        self.assertEqual(claim.link, link)

    def test_changing_category_or_date_ends_the_role_of_the_day_link(self):
        link = self.create(daily=True)
        links.update_link(link, actor=self.reviewer, doctrine_text="Only text")
        link.refresh_from_db()
        self.assertIsNotNone(link.daily_key)

        links.update_link(link, actor=self.reviewer, link_date=TODAY - timedelta(days=2))

        link.refresh_from_db()
        self.assertIsNone(link.daily_key)
        entry = audits("srp.link_update", link).latest("pk")
        self.assertIsNone(entry.new_value["daily_key"])
        # The day's link can be made again.
        self.assertNotEqual(self.create(daily=True).pk, link.pk)

    def test_changing_the_category_ends_the_role_of_the_day_link(self):
        link = self.create(daily=True)
        other = f.srp_category("Other")

        links.update_link(link, actor=self.reviewer, category=other)

        link.refresh_from_db()
        self.assertIsNone(link.daily_key)
        self.assertEqual(link.category, other)
        entry = audits("srp.link_update", link).latest("pk")
        self.assertIsNone(entry.new_value["daily_key"])
        # The day's link of the original category can be made again.
        self.assertNotEqual(self.create(daily=True).pk, link.pk)

    def test_a_fleet_link_only_changes_doctrine_text_and_report(self):
        link = f.fleet_link(f.create_operation(self.fc))

        links.update_link(link, actor=self.reviewer, doctrine_text="Muninns", aar_url="https://example.org/a")
        link.refresh_from_db()
        self.assertEqual((link.doctrine_text, link.aar_url), ("Muninns", "https://example.org/a"))

        for field, value in (("name", "Renamed"), ("category", self.category), ("link_date", TODAY)):
            with self.subTest(field=field), self.assertRaises(SrpError):
                links.update_link(link, actor=self.reviewer, **{field: value})

    def test_a_bad_value_is_refused_and_nothing_is_saved(self):
        link = self.create(name="Fine")

        with self.assertRaises(SrpError):
            links.update_link(link, actor=self.reviewer, name="", aar_url="javascript:x")

        link.refresh_from_db()
        self.assertEqual(link.name, "Fine")
        self.assertFalse(audits("srp.link_update").exists())

    def test_only_reviewers_change_links(self):
        link = self.create()
        for user in (self.payer, self.fc, self.member):
            with self.subTest(user=user.username), self.assertRaises(SrpError):
                links.update_link(link, actor=user, name="Taken over")


class CompleteLinkTests(StandingLinkBase):
    def closed_link(self):
        link = self.create()
        SrpFleetLink.objects.filter(pk=link.pk).update(is_open=False, closed_at=timezone.now())
        link.refresh_from_db()
        return link

    def test_a_closed_link_with_settled_claims_is_completed(self):
        link = self.closed_link()
        f.srp_claim(self.member, link=link, status=Status.PAID, decided_by=self.manager)
        f.srp_claim(self.member, link=link, status=Status.REJECTED, decided_by=self.manager)
        f.srp_claim(self.member, link=link, status=Status.WITHDRAWN)

        links.complete_link(link, actor=self.payer)

        link.refresh_from_db()
        self.assertEqual(link.state, State.COMPLETED)
        self.assertEqual(link.completed_by, self.payer)
        entry = audits("srp.link_complete", link).get()
        self.assertEqual(entry.actor, self.payer)
        self.assertFalse(audits("srp.link_close", link).exists())

    def test_an_open_link_is_closed_first_and_both_steps_are_audited(self):
        link = self.create()

        links.complete_link(link, actor=self.reviewer)

        link.refresh_from_db()
        self.assertEqual((link.is_open, link.state), (False, State.COMPLETED))
        self.assertEqual(audits("srp.link_close", link).count(), 1)
        self.assertEqual(audits("srp.link_complete", link).count(), 1)

    def test_a_pending_or_approved_claim_stops_it_and_the_link_stays_as_it_is(self):
        for status in (Status.PENDING, Status.APPROVED):
            with self.subTest(status=status):
                link = self.create()
                f.srp_claim(self.member, link=link, status=status, decided_by=self.manager)

                with self.assertRaises(SrpStateError) as caught:
                    links.complete_link(link, actor=self.manager)

                self.assertEqual(caught.exception.code, "link_has_open_claims")
                link.refresh_from_db()
                # Not completed, and the open link was not closed on the way.
                self.assertEqual(link.state, State.OPEN)
                self.assertFalse(audits("srp.link_complete", link).exists())
                self.assertFalse(audits("srp.link_close", link).exists())

    def test_the_refusal_counts_what_is_open(self):
        link = self.create()
        f.srp_claim(self.member, link=link)
        f.srp_claim(self.member, link=link)
        f.srp_claim(self.member, link=link, status=Status.APPROVED, decided_by=self.manager)

        with self.assertRaises(SrpStateError) as caught:
            links.complete_link(link, actor=self.manager)

        self.assertEqual(caught.exception.message, texts.SRP_LINK_HAS_OPEN_CLAIMS(pending=2, approved=1))

    def test_there_is_no_parameter_that_forces_it(self):
        import inspect

        self.assertEqual(list(inspect.signature(links.complete_link).parameters), ["link", "actor"])
        link = self.create()
        f.srp_claim(self.member, link=link)
        with self.assertRaises(TypeError):
            links.complete_link(link, actor=self.manager, force=True)

    def test_closing_is_not_limited_by_open_claims(self):
        link = self.create()
        f.srp_claim(self.member, link=link)

        links.set_link_open(link, actor=self.payer, is_open=False)

        link.refresh_from_db()
        self.assertEqual(link.state, State.CLOSED)

    def test_completing_twice_says_so(self):
        link = self.create()
        links.complete_link(link, actor=self.manager)

        with self.assertRaises(SrpStateError) as caught:
            links.complete_link(link, actor=self.manager)
        self.assertEqual(caught.exception.code, "already_completed")
        self.assertEqual(audits("srp.link_complete", link).count(), 1)

    def test_the_claims_of_a_completed_link_do_not_change(self):
        link = self.create()
        claim = f.srp_claim(self.member, link=link, status=Status.PAID, decided_by=self.manager)

        links.complete_link(link, actor=self.manager)

        claim.refresh_from_db()
        self.assertEqual((claim.status, claim.link), (Status.PAID, link))

    def test_only_staff_complete(self):
        link = self.create()
        for user in (self.fc, self.member):
            with self.subTest(user=user.username), self.assertRaises(SrpError):
                links.complete_link(link, actor=user)

    def test_a_completed_link_opened_again_takes_claims(self):
        link = self.create()
        links.complete_link(link, actor=self.manager)

        links.set_link_open(link, actor=self.reviewer, is_open=True)

        link.refresh_from_db()
        self.assertEqual(link.state, State.OPEN)
        self.assertIsNone(link.completed_at)
        self.assertIsNone(link.completed_by)
        entry = audits("srp.link_open", link).get()
        self.assertIsNotNone(entry.old_value["completed_at"])
        self.assertIsNone(entry.new_value["completed_at"])

    def test_the_check_holds_the_lock_of_the_submissions(self):
        link = self.create()
        seen = []
        real = links.quota.quota_lock

        def locked():
            seen.append(True)
            return real()

        with mock.patch.object(links.quota, "quota_lock", side_effect=locked):
            links.complete_link(link, actor=self.manager)

        self.assertEqual(seen, [True])


class BulkLinkTests(StandingLinkBase):
    def test_six_settled_links_are_completed_with_six_audit_entries(self):
        batch = []
        for _index in range(6):
            link = self.create()
            f.srp_claim(self.member, link=link, status=Status.PAID, decided_by=self.manager)
            SrpFleetLink.objects.filter(pk=link.pk).update(is_open=False)
            batch.append(link)

        result = links.bulk_link_action([link.code for link in batch], actor=self.payer, action="complete")

        self.assertEqual(len(result.done), 6)
        self.assertEqual(result.skipped, [])
        self.assertEqual(audits("srp.link_complete").count(), 6)
        self.assertEqual(SrpClaim.objects.filter(status=Status.PAID).count(), 6)

    def test_links_with_open_claims_are_skipped_with_the_reason_and_not_closed(self):
        settled = self.create()
        pending = self.create()
        f.srp_claim(self.member, link=pending)
        to_pay = self.create()
        f.srp_claim(self.member, link=to_pay, status=Status.APPROVED, decided_by=self.manager)

        result = links.bulk_link_action(
            [settled.code, pending.code, to_pay.code, "nosuchcode"], actor=self.manager, action="complete"
        )

        self.assertEqual(result.done, [settled])
        skipped = {(item if isinstance(item, str) else item.code): reason for item, reason in result.skipped}
        self.assertEqual(skipped[pending.code], texts.SRP_LINK_HAS_OPEN_CLAIMS(pending=1, approved=0))
        self.assertEqual(skipped[to_pay.code], texts.SRP_LINK_HAS_OPEN_CLAIMS(pending=0, approved=1))
        self.assertEqual(skipped["nosuchcode"], texts.SRP_LINK_NOT_FOUND())
        for link in (pending, to_pay):
            link.refresh_from_db()
            self.assertEqual(link.state, State.OPEN)

    def test_close_and_open(self):
        first, second = self.create(), self.create()
        links.set_link_open(second, actor=self.manager, is_open=False)

        closed = links.bulk_link_action([first.code, second.code], actor=self.payer, action="close")
        self.assertEqual(closed.done, [first])
        self.assertEqual([reason for _item, reason in closed.skipped], [texts.SRP_LINK_ALREADY_CLOSED()])

        opened = links.bulk_link_action([first.code, second.code], actor=self.reviewer, action="open")
        self.assertEqual(len(opened.done), 2)
        self.assertEqual(audits("srp.link_close").count(), 2)
        self.assertEqual(audits("srp.link_open").count(), 2)

        again = links.bulk_link_action([first.code], actor=self.reviewer, action="open")
        self.assertEqual([reason for _item, reason in again.skipped], [texts.SRP_LINK_ALREADY_OPEN()])

    def test_a_payer_cannot_open_links(self):
        link = self.create()
        links.set_link_open(link, actor=self.manager, is_open=False)

        with self.assertRaises(SrpError) as caught:
            links.bulk_link_action([link.code], actor=self.payer, action="open")

        self.assertEqual(caught.exception.code, "forbidden")
        link.refresh_from_db()
        self.assertFalse(link.is_open)

    def test_the_limits(self):
        with self.assertRaises(SrpError) as caught:
            links.bulk_link_action([f"code{n}" for n in range(101)], actor=self.manager, action="close")
        self.assertEqual(caught.exception.code, "too_many")

        with self.assertRaises(SrpError):
            links.bulk_link_action(["a"], actor=self.manager, action="explode")
        with self.assertRaises(SrpError):
            links.bulk_link_action(["a"], actor=self.member, action="close")

        # Duplicates and empty codes count once.
        result = links.bulk_link_action(["a", "a", ""], actor=self.manager, action="close")
        self.assertEqual(len(result.skipped), 1)

    def test_each_link_is_done_on_its_own(self):
        good, bad = self.create(), self.create()
        f.srp_claim(self.member, link=bad)

        result = links.bulk_link_action([bad.code, good.code], actor=self.manager, action="complete")

        self.assertEqual(result.done, [good])
        good.refresh_from_db()
        self.assertEqual(good.state, State.COMPLETED)


class AutoCompleteTests(StandingLinkBase):
    """``auto_complete_links`` and the way back to closed."""

    def closed_link(self):
        link = self.create()
        links.set_link_open(link, actor=self.manager, is_open=False)
        return link

    def test_the_last_payment_completes_the_link_without_an_actor(self):
        f.srp_settings(auto_complete_links=True)
        link = self.closed_link()
        claim = f.srp_claim(self.member, link=link, status=Status.APPROVED, decided_by=self.manager)

        with self.captureOnCommitCallbacks(execute=True):
            review.mark_paid([claim], actor=self.payer)

        link.refresh_from_db()
        self.assertEqual(link.state, State.COMPLETED)
        self.assertIsNone(link.completed_by)
        entry = audits("srp.link_complete", link).get()
        self.assertIsNone(entry.actor)
        self.assertEqual(entry.reason, texts.SRP_LINK_AUTO_COMPLETED())

    def test_nothing_happens_while_a_claim_is_open_or_the_link_is_open(self):
        f.srp_settings(auto_complete_links=True)
        closed = self.closed_link()
        paid = f.srp_claim(self.member, link=closed, status=Status.APPROVED, decided_by=self.manager)
        f.srp_claim(self.member, link=closed)
        still_open = self.create()
        last = f.srp_claim(self.member, link=still_open, status=Status.APPROVED, decided_by=self.manager)

        with self.captureOnCommitCallbacks(execute=True):
            review.mark_paid([paid, last], actor=self.payer)

        for link in (closed, still_open):
            link.refresh_from_db()
            self.assertNotEqual(link.state, State.COMPLETED)

    def test_the_switch_off_leaves_the_link_closed(self):
        link = self.closed_link()
        claim = f.srp_claim(self.member, link=link, status=Status.APPROVED, decided_by=self.manager)

        with self.captureOnCommitCallbacks(execute=True):
            review.mark_paid([claim], actor=self.payer)

        link.refresh_from_db()
        self.assertEqual(link.state, State.CLOSED)

    def test_a_rejection_and_a_withdrawal_complete_as_well(self):
        f.srp_settings(auto_complete_links=True)
        rejected_link, withdrawn_link = self.closed_link(), self.closed_link()
        rejected = f.srp_claim(self.member, link=rejected_link)
        withdrawn = f.srp_claim(self.member, link=withdrawn_link)

        with self.captureOnCommitCallbacks(execute=True):
            review.reject_claim(rejected, actor=self.reviewer, reason="Not covered")
            claims.withdraw_claim(withdrawn, actor=self.member)

        for link in (rejected_link, withdrawn_link):
            link.refresh_from_db()
            self.assertEqual(link.state, State.COMPLETED)

    def test_closing_a_link_whose_claims_are_settled_completes_it(self):
        f.srp_settings(auto_complete_links=True)
        link = self.create()
        f.srp_claim(self.member, link=link, status=Status.PAID, decided_by=self.manager)

        with self.captureOnCommitCallbacks(execute=True):
            links.set_link_open(link, actor=self.manager, is_open=False)

        link.refresh_from_db()
        self.assertEqual(link.state, State.COMPLETED)
        self.assertEqual(audits("srp.link_close", link).count(), 1)

    def test_undoing_a_payment_takes_a_completed_link_back_to_closed(self):
        link = self.create()
        claim = f.srp_claim(self.member, link=link, status=Status.PAID, decided_by=self.manager)
        links.complete_link(link, actor=self.manager)

        with self.captureOnCommitCallbacks(execute=True):
            review.undo_payment(claim, actor=self.manager, reason="Paid by mistake")

        link.refresh_from_db()
        self.assertEqual(link.state, State.CLOSED)
        self.assertIsNone(link.completed_by)
        entry = audits("srp.link_uncomplete", link).get()
        self.assertIsNone(entry.actor)
        self.assertEqual(entry.reason, texts.SRP_LINK_AUTO_UNCOMPLETED())

    def test_the_way_back_does_not_depend_on_the_switch(self):
        self.assertFalse(SrpSettings.get_solo().auto_complete_links)
        link = self.create()
        claim = f.srp_claim(self.member, link=link, status=Status.REJECTED, decided_by=self.manager)
        links.complete_link(link, actor=self.manager)

        with self.captureOnCommitCallbacks(execute=True):
            review.reopen_claim(claim, actor=self.reviewer, reason="Second look")

        link.refresh_from_db()
        self.assertEqual(link.state, State.CLOSED)

    def test_a_withdrawn_claim_submitted_again_takes_it_back_too(self):
        link = self.create()
        claim = f.srp_claim(self.member, link=link, status=Status.WITHDRAWN)
        links.complete_link(link, actor=self.manager)

        with mock.patch.object(claims.rules, "eligibility", return_value=[]), self.captureOnCommitCallbacks(
            execute=True
        ):
            claims.resubmit_claim(claim, actor=self.member)

        link.refresh_from_db()
        self.assertEqual(link.state, State.CLOSED)

    def test_moving_the_last_claim_out_of_a_link_can_complete_it(self):
        f.srp_settings(auto_complete_links=True)
        link = self.closed_link()
        claim = f.srp_claim(self.member, link=link)
        fleet_link = f.fleet_link(f.create_operation(self.fc))
        # The claim moves into a fleet; the standing link has nothing waiting any more.
        with self.captureOnCommitCallbacks(execute=True), mock.patch.object(review.claims, "evaluate") as evaluate:
            evaluate.side_effect = RuntimeError("stop after the move")
            with self.assertRaises(RuntimeError):
                review.reclassify_claim(
                    claim, actor=self.manager, scope="in_fleet", link=fleet_link, reason="It was a fleet"
                )
        link.refresh_from_db()
        self.assertEqual(link.state, State.CLOSED)

    def test_a_failure_of_the_check_is_logged_with_the_class_only(self):
        link = self.closed_link()
        with mock.patch.object(links, "open_claim_counts", side_effect=ValueError("secret detail")), self.assertLogs(
            links.logger, level="WARNING"
        ) as logged:
            f.srp_settings(auto_complete_links=True)
            result = links.maybe_complete(link.pk)

        self.assertEqual(result, "")
        self.assertIn("ValueError", logged.output[0])
        self.assertNotIn("secret detail", logged.output[0])

    def test_an_unknown_link_is_ignored(self):
        self.assertEqual(links.maybe_complete(0), "")


# --- INT-9: the pages -----------------------------------------------------------------------------------------


class LinkPageBase(SrpPagesTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.reviewer = f.create_user("pagereviewer", perms=f.SRP_REVIEWER_PERMS)
        cls.payer = f.create_user("pagepayer", perms=f.SRP_PAYER_PERMS)
        cls.category = f.srp_category("Home defense", default_doctrine_text="Muninns", auto_close_after_hours=48)

    def standing(self, **values):
        values.setdefault("category", self.category)
        return f.standing_link(**values)

    def post_form(self, user, url, **values):
        return self.client_for(user).post(url, values)


class LinkListTests(LinkPageBase):
    def test_staff_open_the_list_and_others_get_a_403(self):
        for user in (self.reviewer, self.payer, self.manager):
            with self.subTest(user=user.username):
                self.assertEqual(self.client_for(user).get(route("srp_links")).status_code, 200)
        for user in (self.member, self.fc, self.stats, self.no_srp):
            with self.subTest(user=user.username):
                self.assertEqual(self.client_for(user).get(route("srp_links")).status_code, 403)

    def test_the_rows_count_what_is_open_on_each_link(self):
        link = self.standing(name="Counted link")
        f.srp_claim(self.member, link=link)
        f.srp_claim(self.member, link=link)
        f.srp_claim(self.other, link=link, status=Status.APPROVED, decided_by=self.manager)
        f.srp_claim(self.other, link=link, status=Status.PAID, decided_by=self.manager, amount=25_000_000)

        response = self.client_for(self.manager).get(route("srp_links"))

        row = next(row for row in response.context["links"] if row.pk == link.pk)
        self.assertEqual((row.pending_total, row.approved_total, row.paid_amount), (2, 1, 25_000_000))
        self.assertContains(response, "Counted link")

    def test_fleet_links_are_listed_with_their_fleet(self):
        response = self.client_for(self.manager).get(route("srp_links"))

        labels = [row.label for row in response.context["links"]]
        self.assertTrue(any("—" in label for label in labels))

    def test_filters(self):
        today = self.standing(name="Open today", link_date=TODAY)
        old = self.standing(name="Old closed", link_date=TODAY - timedelta(days=10))
        SrpFleetLink.objects.filter(pk=old.pk).update(is_open=False)
        done = self.standing(name="Done", link_date=TODAY - timedelta(days=5), category=f.srp_category("Solo PVP"))
        SrpFleetLink.objects.filter(pk=done.pk).update(is_open=False, completed_at=timezone.now())
        client = self.client_for(self.manager)

        def names(**params):
            rows = client.get(route("srp_links"), params).context["links"]
            return {row.name for row in rows if row.name}

        self.assertEqual(names(state="open") & {"Open today", "Old closed", "Done"}, {"Open today"})
        self.assertEqual(names(state="closed") & {"Open today", "Old closed", "Done"}, {"Old closed"})
        self.assertEqual(names(state="completed") & {"Open today", "Old closed", "Done"}, {"Done"})
        self.assertEqual(names(kind="standing"), {"Open today", "Old closed", "Done"})
        self.assertEqual(names(category=done.category_id), {"Done"})
        self.assertEqual(
            names(kind="standing", date_from=(TODAY - timedelta(days=6)).isoformat()), {"Open today", "Done"}
        )
        self.assertEqual(
            names(kind="standing", date_to=(TODAY - timedelta(days=6)).isoformat()), {"Old closed"}
        )
        # A date inside the range of the fleet links' start dates finds those too.
        fleet_rows = client.get(route("srp_links"), {"kind": "fleet"}).context["links"]
        self.assertTrue(all(row.kind == "fleet" for row in fleet_rows))
        # A value that does not check out is ignored.
        self.assertEqual(client.get(route("srp_links"), {"state": "nonsense", "date_from": "x"}).status_code, 200)

    def test_date_filter_covers_the_start_of_a_fleet(self):
        client = self.client_for(self.manager)
        start = self.fleet.started_at.astimezone(dt_timezone.utc).date()

        inside = client.get(route("srp_links"), {"date_from": start.isoformat(), "date_to": start.isoformat()})
        outside = client.get(route("srp_links"), {"date_from": (start + timedelta(days=30)).isoformat()})

        self.assertIn(self.link.pk, {row.pk for row in inside.context["links"]})
        self.assertNotIn(self.link.pk, {row.pk for row in outside.context["links"]})

    def test_codes_preselect_and_narrow_the_list(self):
        chosen = self.standing(name="Chosen")
        self.standing(name="Not chosen")

        response = self.client_for(self.manager).get(route("srp_links"), {"codes": f"{chosen.code},,{'x' * 40}"})

        self.assertEqual([row.pk for row in response.context["links"]], [chosen.pk])
        self.assertContains(response, f'value="{chosen.code}" data-srp-select checked')
        self.assertNotContains(response, "Not chosen")
        self.assertContains(response, "Show all links")

    def test_pages_of_fifty(self):
        category = self.category
        SrpFleetLink.objects.bulk_create(
            [
                SrpFleetLink(
                    kind="standing", code=f"bulk-{n:03d}", name=f"Bulk {n}", category=category, link_date=TODAY
                )
                for n in range(60)
            ]
        )
        client = self.client_for(self.manager)

        first = client.get(route("srp_links"), {"kind": "standing"})
        second = client.get(route("srp_links"), {"kind": "standing", "page": 2})

        self.assertEqual(len(first.context["links"]), 50)
        self.assertEqual(len(second.context["links"]), 10)

    def test_buttons_follow_the_role(self):
        self.standing()

        reviewer_page = self.client_for(self.reviewer).get(route("srp_links"))
        payer_page = self.client_for(self.payer).get(route("srp_links"))

        self.assertContains(reviewer_page, "New standing link")
        self.assertContains(reviewer_page, 'name="action" value="open"')
        self.assertContains(reviewer_page, "Edit")
        self.assertNotContains(payer_page, "New standing link")
        self.assertNotContains(payer_page, 'name="action" value="open"')
        self.assertContains(payer_page, 'name="action" value="complete"')
        self.assertContains(payer_page, 'name="action" value="close"')

    def test_the_menu_has_the_entry_for_staff_only(self):
        for user, shown in ((self.reviewer, True), (self.payer, True), (self.member, False), (self.fc, False)):
            with self.subTest(user=user.username):
                self.assertEqual(access.srp_nav(user)["links"], shown)
        self.assertContains(self.client_for(self.payer).get(route("srp_payments")), route("srp_links"))
        self.assertNotContains(self.client_for(self.member).get(route("srp_home")), route("srp_links"))

    def test_an_empty_list_says_so(self):
        SrpFleetLink.objects.all().delete()

        response = self.client_for(self.manager).get(route("srp_links"))

        self.assertContains(response, "There are no SRP links yet.")


class LinkFormPageTests(LinkPageBase):
    def form_values(self, **values):
        base = {
            "name": "",
            "category": self.category.pk,
            "link_date": TODAY.isoformat(),
            "doctrine_text": "Muninns",
            "aar_url": "",
            "auto_close_at": "",
        }
        base.update(values)
        return base

    def test_a_reviewer_makes_a_link_without_a_fleet(self):
        response = self.client_for(self.reviewer).post(route("srp_link_new"), self.form_values(name="Home defense 2026-11-05"))

        self.assertRedirects(response, route("srp_links"), fetch_redirect_response=False)
        link = SrpFleetLink.objects.get(name="Home defense 2026-11-05")
        self.assertEqual((link.kind, link.category, link.created_by), ("standing", self.category, self.reviewer))
        self.assertIsNone(link.operation_id)
        self.assertEqual(audits("srp.link_create", link).count(), 1)
        # The message tells the members where to find the link.
        self.assertIn(link.code, " ".join(messages_of(response)))

    def test_an_empty_name_uses_the_template_and_the_daily_flag_takes_the_day(self):
        client = self.client_for(self.reviewer)
        client.post(route("srp_link_new"), self.form_values(daily="on"))
        link = SrpFleetLink.objects.get(kind="standing")
        self.assertEqual(link.name, f"Home defense {TODAY.isoformat()}")
        self.assertEqual(link.daily_key, f"{self.category.pk}:{TODAY.isoformat()}")

        again = client.post(route("srp_link_new"), self.form_values(daily="on"))

        self.assertEqual(again.status_code, 200)
        self.assertContains(again, "already")
        self.assertEqual(SrpFleetLink.objects.filter(kind="standing").count(), 1)

    def test_the_close_time_is_read_as_eve_time(self):
        self.client_for(self.reviewer).post(
            route("srp_link_new"), self.form_values(name="Timed", auto_close_at="2026-11-07T00:00")
        )

        link = SrpFleetLink.objects.get(name="Timed")
        self.assertEqual(link.auto_close_at.isoformat(), "2026-11-07T00:00:00+00:00")

    def test_the_form_asks_for_the_defaults_of_the_category(self):
        response = self.client_for(self.reviewer).get(route("srp_link_new"), {"category": self.category.pk})

        self.assertContains(response, "Muninns")
        data = response.context["category_data"]
        self.assertEqual(data[str(self.category.pk)], {"doctrine": "Muninns", "hours": 48})
        self.assertContains(response, 'id="srp-link-category-data"')

    def test_a_report_address_that_is_not_http_is_refused(self):
        for address in ("javascript:alert(1)", "ftp://example.org/x"):
            with self.subTest(address=address):
                response = self.client_for(self.reviewer).post(route("srp_link_new"), self.form_values(aar_url=address))
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.context["form"].errors.get("aar_url"))
        self.assertFalse(SrpFleetLink.objects.filter(kind="standing").exists())

    def test_a_missing_category_or_date_is_refused(self):
        response = self.client_for(self.reviewer).post(route("srp_link_new"), self.form_values(category="", link_date=""))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(set(response.context["form"].errors), {"category", "link_date"})

    def test_a_switched_off_category_cannot_be_chosen(self):
        off = f.srp_category("Off", is_active=False)

        response = self.client_for(self.reviewer).post(route("srp_link_new"), self.form_values(category=off.pk))

        self.assertTrue(response.context["form"].errors.get("category"))

    def test_only_reviewers_open_the_pages_to_make_and_change_links(self):
        link = self.standing()
        for user in (self.payer, self.fc, self.member, self.no_srp):
            with self.subTest(user=user.username):
                client = self.client_for(user)
                self.assertEqual(client.get(route("srp_link_new")).status_code, 403)
                self.assertEqual(client.post(route("srp_link_new"), self.form_values()).status_code, 403)
                self.assertEqual(client.get(route("srp_link_edit", link.code)).status_code, 403)
                self.assertEqual(client.post(route("srp_link_edit", link.code), self.form_values()).status_code, 403)
        self.assertEqual(SrpFleetLink.objects.filter(kind="standing").count(), 1)

    def test_the_fc_of_a_fleet_cannot_edit_the_report_of_its_own_fleet_link(self):
        link = f.fleet_link(f.create_operation(self.fc))
        client = self.client_for(self.fc)

        self.assertEqual(client.get(route("srp_link_edit", link.code)).status_code, 403)
        response = client.post(route("srp_link_edit", link.code), {"doctrine_text": "Muninns", "aar_url": "https://example.org/a"})

        self.assertEqual(response.status_code, 403)
        link.refresh_from_db()
        self.assertEqual((link.doctrine_text, link.aar_url), ("", ""))

    def test_a_reviewer_edits_and_the_page_counts_the_claims_that_keep_their_category(self):
        link = self.standing(name="Before")
        f.srp_claim(self.member, link=link)
        other = f.srp_category("Solo PVP")
        client = self.client_for(self.reviewer)

        page = client.get(route("srp_link_edit", link.code))
        self.assertContains(page, "1 claim has been made through this link")

        response = client.post(
            route("srp_link_edit", link.code),
            self.form_values(name="After", category=other.pk, doctrine_text="", aar_url="https://example.org/aar"),
        )

        self.assertRedirects(response, route("srp_links"), fetch_redirect_response=False)
        link.refresh_from_db()
        self.assertEqual((link.name, link.category, link.aar_url), ("After", other, "https://example.org/aar"))
        self.assertEqual(SrpClaim.objects.get(link=link).category, self.category)
        self.assertEqual(audits("srp.link_update", link).count(), 1)

    def test_the_link_of_a_fleet_only_has_doctrine_text_and_report(self):
        client = self.client_for(self.reviewer)

        page = client.get(route("srp_link_edit", self.link.code))
        self.assertEqual(set(page.context["form"].fields), {"doctrine_text", "aar_url"})

        client.post(route("srp_link_edit", self.link.code), {"doctrine_text": "Muninns", "aar_url": "example.org/aar"})

        self.link.refresh_from_db()
        self.assertEqual(self.link.doctrine_text, "Muninns")
        # An address without a scheme gets https.
        self.assertEqual(self.link.aar_url, "https://example.org/aar")

    def test_an_unknown_code_is_not_found(self):
        self.assertEqual(self.client_for(self.reviewer).get(route("srp_link_edit", "nosuchcode")).status_code, 404)


class LinkActionPageTests(LinkPageBase):
    def test_complete_one_link(self):
        link = self.standing()

        response = self.client_for(self.payer).post(route("srp_link_complete", link.code))

        self.assertRedirects(response, route("srp_links"), fetch_redirect_response=False)
        link.refresh_from_db()
        self.assertEqual(link.state, State.COMPLETED)

    def test_a_link_with_a_claim_to_pay_cannot_be_completed_by_any_request(self):
        link = self.standing()
        f.srp_claim(self.member, link=link, status=Status.APPROVED, decided_by=self.manager)
        client = self.client_for(self.manager)

        for params in ({}, {"force": "1"}, {"force": "true", "next": "/x/"}, {"confirm": "yes"}):
            with self.subTest(params=params):
                response = client.post(route("srp_link_complete", link.code), params)
                self.assertEqual(response.status_code, 302)
                self.assertIn("not settled", " ".join(messages_of(response)))
                link.refresh_from_db()
                self.assertEqual(link.state, State.OPEN)
        response = client.post(route("srp_links_bulk"), {"action": "complete", "codes": [link.code], "force": "1"})
        self.assertEqual(response.status_code, 302)
        link.refresh_from_db()
        self.assertEqual(link.state, State.OPEN)

        # Closing is not limited.
        client.post(route("srp_link_close", link.code))
        link.refresh_from_db()
        self.assertEqual(link.state, State.CLOSED)

    def test_completing_from_the_fleet_page_goes_back_to_the_fleet(self):
        SrpFleetLink.objects.filter(pk=self.link.pk).update(is_open=False)

        response = self.client_for(self.manager).post(route("srp_link_complete", self.link.code))

        self.assertRedirects(
            response, route("operation_detail", self.fleet.uuid), fetch_redirect_response=False
        )

    def test_the_get_of_a_post_route_is_refused(self):
        link = self.standing()
        client = self.client_for(self.manager)

        self.assertEqual(client.get(route("srp_link_complete", link.code)).status_code, 405)
        self.assertEqual(client.get(route("srp_links_bulk")).status_code, 405)

    def test_requests_need_a_csrf_token(self):
        from django.test import Client

        link = self.standing()
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.manager)

        self.assertEqual(client.post(route("srp_link_complete", link.code)).status_code, 403)
        self.assertEqual(client.post(route("srp_links_bulk"), {"action": "close", "codes": [link.code]}).status_code, 403)
        link.refresh_from_db()
        self.assertTrue(link.is_open)

    def test_bulk_messages_name_the_skipped_links_and_why(self):
        good = self.standing(name="Good link")
        bad = self.standing(name="Bad link")
        f.srp_claim(self.member, link=bad)
        client = self.client_for(self.manager)

        response = client.post(
            route("srp_links_bulk"), {"action": "complete", "codes": [good.code, bad.code], "next": route("srp_links")}
        )
        shown = " ".join(messages_of(response))

        self.assertRedirects(response, route("srp_links"), fetch_redirect_response=False)
        self.assertIn("1 link completed.", shown)
        self.assertIn("Bad link", shown)
        self.assertIn("waiting for review: 1", shown)

    def test_bulk_with_nothing_chosen(self):
        client = self.client_for(self.manager)

        response = client.post(route("srp_links_bulk"), {"action": "close"})

        self.assertIn("No link was chosen.", " ".join(messages_of(response)))

    def test_a_payer_cannot_reopen_through_the_list(self):
        link = self.standing()
        SrpFleetLink.objects.filter(pk=link.pk).update(is_open=False)

        response = self.client_for(self.payer).post(route("srp_links_bulk"), {"action": "open", "codes": [link.code]})

        self.assertEqual(response.status_code, 403)
        link.refresh_from_db()
        self.assertFalse(link.is_open)

    def test_a_reviewer_reopens_through_the_list(self):
        link = self.standing()
        SrpFleetLink.objects.filter(pk=link.pk).update(is_open=False)

        self.client_for(self.reviewer).post(route("srp_links_bulk"), {"action": "open", "codes": [link.code]})

        link.refresh_from_db()
        self.assertTrue(link.is_open)

    def test_the_next_address_must_stay_on_the_site(self):
        link = self.standing()

        response = self.client_for(self.manager).post(
            route("srp_links_bulk"), {"action": "close", "codes": [link.code], "next": "https://evil.example/x"}
        )

        self.assertRedirects(response, route("srp_links"), fetch_redirect_response=False)


class FleetCardTests(LinkPageBase):
    """The card on the fleet page; the people here also may view every fleet."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        view = ["fleetops.view_all_fleets"]
        cls.reviewer = f.create_user("cardreviewer", perms=[*f.SRP_REVIEWER_PERMS, *view])
        cls.payer = f.create_user("cardpayer", perms=[*f.SRP_PAYER_PERMS, *view])
        cls.manager = f.create_user("cardmanager", perms=[*f.SRP_MANAGER_PERMS, *view])

    def card(self, user, link=None):
        link = link or self.link
        return self.client_for(user).get(route("operation_detail", link.operation.uuid))

    def test_the_card_has_the_completed_state_and_the_button(self):
        manager_page = self.card(self.manager)
        self.assertContains(manager_page, "Mark SRP link complete")
        self.assertContains(manager_page, route("srp_link_complete", self.link.code))

        links.complete_link(self.link, actor=self.manager)
        completed = self.card(self.manager)

        self.assertContains(completed, "Link completed")
        self.assertNotContains(completed, "Mark SRP link complete")
        self.assertContains(completed, "Completed ")

    def test_only_reviewers_see_the_button_that_opens_a_link_again(self):
        SrpFleetLink.objects.filter(pk=self.link.pk).update(is_open=False)

        self.assertContains(self.card(self.reviewer), route("srp_link_open", self.link.code))
        self.assertNotContains(self.card(self.payer), route("srp_link_open", self.link.code))
        self.assertContains(self.card(self.payer), "Mark SRP link complete")

    def test_the_report_shows_on_the_card(self):
        SrpFleetLink.objects.filter(pk=self.link.pk).update(aar_url="https://example.org/aar")

        page = self.card(self.manager)

        self.assertContains(page, 'href="https://example.org/aar"')
        self.assertContains(page, "noopener noreferrer")

    def test_the_card_context_flags(self):
        context = links.operation_srp_context(self.fleet, self.payer)["srp_card"]

        self.assertTrue(context["can_toggle"])
        self.assertFalse(context["can_reopen"])
        self.assertTrue(context["can_complete"])
        self.assertEqual(context["state"], State.OPEN)
        reviewer_context = links.operation_srp_context(self.fleet, self.reviewer)["srp_card"]
        self.assertTrue(reviewer_context["can_reopen"])


class SubmitRaceTests(ClaimBase):
    def test_a_link_completed_after_the_check_takes_no_claim(self):
        category = f.srp_category("Home defense")
        link = f.standing_link(category=category)
        preview = self.prepare(self.killmail(), link=link)
        links.complete_link(link, actor=self.manager)

        with self.assertRaises(SrpError) as caught:
            claims.submit_claim(self.user, preview.token)

        self.assertEqual(caught.exception.code, "not_eligible")
        self.assertFalse(SrpClaim.objects.filter(link=link).exists())


class LinkFormErrorTests(LinkPageBase):
    def test_a_refusal_of_the_service_is_shown_as_a_message_and_the_form_stays(self):
        client = self.client_for(self.reviewer)
        with mock.patch.object(links, "create_standing_link", side_effect=SrpError("invalid", "Not saved for a reason.")):
            response = client.post(
                route("srp_link_new"),
                {"name": "x", "category": self.category.pk, "link_date": TODAY.isoformat()},
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn("Not saved for a reason.", " ".join(messages_of(response)))
        self.assertContains(response, 'name="link_date"')
