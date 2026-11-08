# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Losses SRP does not cover (RUL-2 to RUL-5): the pure checks, the settings page with its "recommended list"
button, the claim check on its three paths (preview, submit, submit again) and the card members read.

ESI is never reached; the lookup of the location is patched for the whole run (see ``tests/__init__``).
"""

from decimal import Decimal
from types import SimpleNamespace
from unittest import mock

from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse

from fleetops.models import AuditLog, SrpClaim, SrpSettings
from fleetops.srp import blocks, claims, policy
from fleetops.srp.types import SrpError

from . import factories as f
from .test_srp_claims import ClaimBase
from .test_srp_views import SrpPagesTestCase

JITA = 30_000_142
AMAMAKE = 30_002_537
COVETOR = 17_476
MINING_BARGE = 463
VENTURE = 32_880
HIGH = Decimal("0.9459")
LOW = Decimal("0.4388")


def loss(**values):
    """A loss as the checks read it (a stored killmail or a discovered loss)."""
    fields = dict(
        ship_type_id=f.RIFTER,
        ship_type_name="Rifter",
        ship_group_id=25,
        solar_system_id=AMAMAKE,
        solar_system_name="Amamake",
        solar_system_security=LOW,
        region_id=10_000_001,
        war_id=None,
    )
    fields.update(values)
    return SimpleNamespace(**fields)


def srp(**values):
    fields = dict(
        blocked_ship_group_ids="",
        blocked_ship_type_ids="",
        blocked_security_bands="",
        payout_rule_lifts_ship_block=True,
        highsec_war_losses_covered=False,
    )
    fields.update(values)
    return SimpleNamespace(**fields)


class SecurityBandTests(TestCase):
    def test_the_band_follows_the_security_status_and_the_ids(self):
        # (security, system, region, band): the boundaries of RUL-1
        table = [
            (Decimal("0.4500"), AMAMAKE, 10_000_001, "highsec"),
            (Decimal("0.4490"), AMAMAKE, 10_000_001, "lowsec"),
            (Decimal("0.0500"), AMAMAKE, 10_000_001, "lowsec"),
            (Decimal("0.0000"), AMAMAKE, 10_000_001, "nullsec"),
            (Decimal("-0.1000"), AMAMAKE, 10_000_001, "nullsec"),
            (Decimal("-0.9900"), 31_000_005, 11_000_001, "wormhole"),
            (None, 31_000_005, None, "wormhole"),
            (Decimal("-1.0000"), 32_000_001, None, "abyssal"),
            (Decimal("-1.0000"), 30_001_000, 10_000_070, "pochven"),
            (None, JITA, 10_000_002, None),
        ]
        for security, system, region, band in table:
            with self.subTest(security=security, system=system):
                found = blocks.security_band(loss(solar_system_security=security, solar_system_id=system, region_id=region))
                self.assertEqual(found, band)

    def test_the_edges_of_the_id_ranges(self):
        for system, band in (
            (30_999_999, "lowsec"),
            (31_000_000, "wormhole"),
            (31_999_999, "wormhole"),
            (32_000_000, "abyssal"),
            (32_999_999, "abyssal"),
            (33_000_000, "lowsec"),
        ):
            with self.subTest(system=system):
                self.assertEqual(blocks.security_band(loss(solar_system_id=system, region_id=None)), band)

    def test_security_is_shown_the_way_eve_shows_it(self):
        for value, shown in (
            ("0.9459", "0.9"),
            ("0.4500", "0.5"),
            ("0.4490", "0.4"),
            ("0.0100", "0.1"),
            ("0.0400", "0.1"),
            ("0.0500", "0.1"),
            ("0.0000", "0.0"),
            ("-0.1000", "-0.1"),
            (None, ""),
        ):
            with self.subTest(value=value):
                self.assertEqual(blocks.format_security(Decimal(value) if value else None), shown)


class SubmissionBlockTests(TestCase):
    def test_nothing_is_blocked_by_default(self):
        self.assertEqual(blocks.submission_blocks(loss(), srp(), scope="out_of_fleet"), [])
        self.assertEqual(
            blocks.submission_blocks(loss(ship_group_id=MINING_BARGE, solar_system_security=HIGH), srp()), []
        )

    def test_a_blocked_ship_group_is_refused(self):
        settings_row = srp(blocked_ship_group_ids="463,543")
        barge = loss(ship_type_id=COVETOR, ship_group_id=MINING_BARGE, ship_type_name="Covetor")
        # The name comes from the caller's one lookup (the built-in names when the SDE is not installed).
        names = blocks.blocked_group_names([barge], settings_row)
        found = blocks.submission_blocks(barge, settings_row, group_names=names)
        self.assertEqual(found, ["Mining Barge ships are not covered."])

    def test_a_blocked_ship_is_refused_by_its_type(self):
        found = blocks.submission_blocks(
            loss(ship_type_id=VENTURE, ship_type_name="Venture", ship_group_id=25),
            srp(blocked_ship_type_ids="32880,605"),
        )
        self.assertEqual(found, ["Venture losses are not covered."])

    def test_a_ship_without_a_name_is_named_by_its_id(self):
        found = blocks.submission_blocks(loss(ship_type_id=VENTURE, ship_type_name=""), srp(blocked_ship_type_ids="32880"))
        self.assertEqual(found, ["Type 32880 losses are not covered."])

    def test_a_blocked_band_names_the_system_and_its_security(self):
        found = blocks.submission_blocks(
            loss(solar_system_id=JITA, solar_system_name="Jita", solar_system_security=HIGH, region_id=10_000_002),
            srp(blocked_security_bands="highsec"),
        )
        self.assertEqual(found, ["Losses in high security space are not covered (Jita 0.9)."])

    def test_low_security_is_not_refused_when_only_high_security_is_blocked(self):
        self.assertEqual(blocks.submission_blocks(loss(), srp(blocked_security_bands="highsec")), [])

    def test_the_other_bands(self):
        for system, security, region, band, text in (
            (31_000_005, None, None, "wormhole", "wormhole"),
            (32_000_005, None, None, "abyssal", "abyssal"),
            (30_001_000, Decimal("-1.0"), 10_000_070, "pochven", "Pochven"),
            (30_001_000, Decimal("-0.5"), 10_000_001, "nullsec", "null security"),
        ):
            with self.subTest(band=band):
                found = blocks.submission_blocks(
                    loss(solar_system_id=system, solar_system_security=security, region_id=region),
                    srp(blocked_security_bands=band),
                )
                self.assertEqual(len(found), 1)
                self.assertTrue(found[0].startswith(f"Losses in {text} space are not covered"), found)

    def test_a_payout_table_entry_for_the_ship_lifts_the_ship_blocks_only(self):
        rule = SimpleNamespace(ship_type_id=COVETOR)
        covetor = loss(ship_type_id=COVETOR, ship_group_id=MINING_BARGE, solar_system_security=HIGH, solar_system_id=JITA)
        settings = srp(blocked_ship_group_ids="463", blocked_ship_type_ids="17476", blocked_security_bands="highsec")

        found = blocks.submission_blocks(covetor, settings, rule=rule)

        self.assertEqual(len(found), 1)
        self.assertIn("high security", found[0])

    def test_a_rule_for_another_ship_lifts_nothing(self):
        covetor = loss(ship_type_id=COVETOR, ship_group_id=MINING_BARGE)
        found = blocks.submission_blocks(
            covetor, srp(blocked_ship_group_ids="463"), rule=SimpleNamespace(ship_type_id=0)
        )
        self.assertEqual(len(found), 1)

    def test_a_doctrine_rule_for_any_ship_lifts_nothing(self):
        # A rule without a ship (a doctrine rule has ship_type_id 0) does not name this ship.
        covetor = loss(ship_type_id=COVETOR, ship_group_id=MINING_BARGE)
        found = blocks.submission_blocks(covetor, srp(blocked_ship_group_ids="463"), rule=SimpleNamespace(ship_type_id=None))
        self.assertEqual(len(found), 1)

    def test_the_lifting_can_be_switched_off(self):
        covetor = loss(ship_type_id=COVETOR, ship_group_id=MINING_BARGE)
        found = blocks.submission_blocks(
            covetor,
            srp(blocked_ship_group_ids="463", payout_rule_lifts_ship_block=False),
            rule=SimpleNamespace(ship_type_id=COVETOR),
        )
        self.assertEqual(len(found), 1)

    def test_a_war_loss_in_high_security_is_refused_unless_the_switch_is_on(self):
        war = loss(solar_system_id=JITA, solar_system_security=HIGH, region_id=10_000_002, war_id=7)
        self.assertEqual(len(blocks.submission_blocks(war, srp(blocked_security_bands="highsec"))), 1)
        self.assertEqual(
            blocks.submission_blocks(war, srp(blocked_security_bands="highsec", highsec_war_losses_covered=True)), []
        )

    def test_the_war_switch_does_not_open_other_bands(self):
        war = loss(war_id=7, solar_system_security=Decimal("-0.5"), solar_system_id=30_001_000)
        settings = srp(blocked_security_bands="nullsec", highsec_war_losses_covered=True)
        self.assertEqual(len(blocks.submission_blocks(war, settings)), 1)

    def test_a_value_that_is_not_known_blocks_nothing(self):
        unknown = loss(ship_group_id=None, solar_system_security=None, region_id=None, solar_system_id=JITA)
        settings = srp(blocked_ship_group_ids="463", blocked_security_bands="highsec,lowsec,nullsec")
        self.assertEqual(blocks.submission_blocks(unknown, settings), [])

    def test_unreadable_lists_block_nothing(self):
        settings = srp(blocked_ship_group_ids="x,y", blocked_ship_type_ids="??", blocked_security_bands="nowhere")
        self.assertEqual(blocks.submission_blocks(loss(), settings), [])
        self.assertFalse(blocks.has_blocks(settings))

    def test_the_recommended_lists_match_the_requirements(self):
        self.assertEqual(blocks.RECOMMENDED_SHIP_GROUP_IDS, (463, 543, 28, 380, 1202, 513, 902, 941, 883, 1283, 4902, 1022))
        self.assertEqual(blocks.RECOMMENDED_SHIP_TYPE_IDS, (32880, 605, 607, 29248, 586))
        self.assertEqual(blocks.RECOMMENDED_SECURITY_BANDS, ("highsec",))


class BlockSettingsTests(TestCase):
    def test_nothing_is_blocked_on_a_new_site(self):
        row = SrpSettings.get_solo()
        self.assertEqual((row.blocked_ship_group_ids, row.blocked_ship_type_ids, row.blocked_security_bands), ("", "", ""))
        self.assertTrue(row.payout_rule_lifts_ship_block)
        self.assertFalse(row.highsec_war_losses_covered)
        self.assertEqual((row.ban_default_days, row.ban_max_days, row.auto_ban_over_monthly_cap), (7, 365, False))

    def test_an_unknown_band_does_not_validate(self):
        row = SrpSettings.get_solo()
        row.blocked_security_bands = "highsec,moon"
        from django.core.exceptions import ValidationError

        with self.assertRaises(ValidationError) as caught:
            row.full_clean()
        self.assertIn("blocked_security_bands", caught.exception.message_dict)

    def test_ship_ids_that_are_not_numbers_do_not_validate(self):
        from django.core.exceptions import ValidationError

        row = SrpSettings.get_solo()
        row.blocked_ship_type_ids = "12,abc"
        with self.assertRaises(ValidationError) as caught:
            row.full_clean()
        self.assertIn("blocked_ship_type_ids", caught.exception.message_dict)


class MemberNoticeTests(TestCase):
    def test_nothing_to_show_when_nothing_is_blocked_and_capsules_are_free(self):
        row = SrpSettings.get_solo()
        row.capsules_need_rule = False
        self.assertIsNone(blocks.member_notice(row))

    def test_the_capsule_rule_alone_is_shown(self):
        notice = blocks.member_notice(SrpSettings.get_solo())
        self.assertTrue(notice["capsules"])
        self.assertEqual((notice["groups"], notice["ships"], notice["bands"]), ([], [], []))

    def test_groups_ships_and_bands_are_named(self):
        row = SrpSettings.get_solo()
        row.blocked_ship_group_ids = "463,543"
        row.blocked_ship_type_ids = "32880,99999999"
        row.blocked_security_bands = "highsec,wormhole"
        notice = blocks.member_notice(row)
        self.assertEqual(notice["groups"], ["Mining Barge", "Exhumer"])
        self.assertEqual(notice["ships"], ["Venture", "Type 99999999"])
        self.assertEqual(notice["bands"], ["high security", "wormhole"])
        self.assertTrue(notice["lifted_by_rule"])


# --- The claim check: preview, submit, submit again -------------------------------------------------------------


class BlockedClaimTests(ClaimBase):
    """RUL-3 on the three paths, with the recommended lists saved."""

    def setUp(self):
        super().setUp()
        self.srp = self.settings(
            enabled=True,
            percentage=Decimal("80"),
            blocked_ship_group_ids=",".join(str(i) for i in blocks.RECOMMENDED_SHIP_GROUP_IDS),
            blocked_ship_type_ids=",".join(str(i) for i in blocks.RECOMMENDED_SHIP_TYPE_IDS),
            blocked_security_bands="highsec",
        )

    def jita_rifter(self, **extra):
        return self.killmail(
            solar_system_id=JITA,
            solar_system_name="Jita",
            solar_system_security=HIGH,
            region_id=10_000_002,
            ship_group_id=25,
            **extra,
        )

    def test_a_covetor_is_refused_with_its_group(self):
        km = self.killmail(ship_type_id=COVETOR, ship_name="Covetor", ship_group_id=MINING_BARGE, solar_system_security=LOW)
        preview = self.prepare(km)
        self.assertEqual(preview.blockers, ["Mining Barge ships are not covered."])
        self.assertFalse(preview.eligible)
        with self.assertRaises(SrpError) as caught:
            claims.submit_claim(self.user, self.token(km))
        self.assertEqual(caught.exception.code, "not_eligible")

    def test_a_rifter_in_high_security_is_refused_and_one_in_low_security_is_not(self):
        self.assertEqual(
            self.prepare(self.jita_rifter()).blockers,
            ["Losses in high security space are not covered (Jita 0.9)."],
        )
        amamake = self.killmail(solar_system_id=AMAMAKE, solar_system_name="Amamake", solar_system_security=LOW, ship_group_id=25)
        self.assertEqual(self.prepare(amamake).blockers, [])

    def test_a_payout_table_entry_for_the_covetor_lets_it_through(self):
        f.payout_rule(COVETOR, 30_000_000)
        km = self.killmail(ship_type_id=COVETOR, ship_name="Covetor", ship_group_id=MINING_BARGE, solar_system_security=LOW)
        claim = self.submit(km)
        self.assertEqual((claim.status, claim.payout_basis, claim.suggested_amount), ("pending", "rule", 30_000_000))

    def test_the_lifting_switched_off_keeps_the_covetor_out(self):
        f.payout_rule(COVETOR, 30_000_000)
        self.settings(payout_rule_lifts_ship_block=False)
        km = self.killmail(ship_type_id=COVETOR, ship_name="Covetor", ship_group_id=MINING_BARGE, solar_system_security=LOW)
        self.assertEqual(self.prepare(km).blockers, ["Mining Barge ships are not covered."])

    def test_a_rule_of_another_category_does_not_lift_the_block(self):
        f.payout_rule(COVETOR, 30_000_000, category=f.srp_category("Mining"))
        km = self.killmail(ship_type_id=COVETOR, ship_name="Covetor", ship_group_id=MINING_BARGE, solar_system_security=LOW)
        self.assertEqual(self.prepare(km).blockers, ["Mining Barge ships are not covered."])

    def test_a_war_loss_in_jita_is_refused_by_default_and_covered_when_the_switch_is_on(self):
        km = self.jita_rifter(war_id=5)
        self.assertEqual(len(self.prepare(km).blockers), 1)
        self.settings(highsec_war_losses_covered=True)
        self.assertEqual(self.prepare(km).blockers, [])

    def test_a_loss_without_location_values_is_not_blocked_and_has_no_warning(self):
        km = self.killmail(ship_group_id=None, solar_system_security=None, region_id=None)
        preview = self.prepare(km)
        self.assertEqual(preview.blockers, [])
        self.assertEqual([warning.code for warning in preview.warnings], [])

    def test_a_blocked_ship_type_in_the_general_frigate_group_is_refused(self):
        km = self.killmail(ship_type_id=VENTURE, ship_name="Venture", ship_group_id=25, solar_system_security=LOW)
        self.assertEqual(self.prepare(km).blockers, ["Venture losses are not covered."])

    def test_submitting_again_is_refused_too(self):
        km = self.killmail(ship_type_id=COVETOR, ship_name="Covetor", ship_group_id=MINING_BARGE, solar_system_security=LOW)
        f.payout_rule(COVETOR, 30_000_000)
        claim = self.submit(km)
        claims.withdraw_claim(claim, actor=self.user)
        SrpClaim.objects.filter(pk=claim.pk).update()  # the claim keeps its rule; now remove the rule
        from fleetops.models import SrpPayoutRule

        SrpPayoutRule.objects.all().delete()

        with self.assertRaises(SrpError) as caught:
            claims.resubmit_claim(claim, actor=self.user)
        self.assertEqual(caught.exception.code, "not_eligible")

    def test_an_old_killmail_without_location_values_can_be_submitted_again(self):
        # RUL-1: the new fields are NULL on a killmail stored before 0.3.0b1; "fail open" applies to it.
        km = self.killmail(ship_type_id=COVETOR, ship_name="Covetor", ship_group_id=None, solar_system_security=None)
        claim = self.submit(km)
        claims.withdraw_claim(claim, actor=self.user)
        self.assertEqual(claims.resubmit_claim(claim, actor=self.user).status, "pending")

    def test_a_reopened_claim_is_not_checked(self):
        # RUL-3: reopening is the reviewer's way to make an exception.
        from fleetops.srp import review

        f.payout_rule(COVETOR, 30_000_000)
        km = self.killmail(ship_type_id=COVETOR, ship_name="Covetor", ship_group_id=MINING_BARGE, solar_system_security=LOW)
        claim = self.submit(km)
        review.reject_claim(claim, actor=self.manager, reason="No")
        from fleetops.models import SrpPayoutRule

        SrpPayoutRule.objects.all().delete()

        reopened = review.reopen_claim(claim, actor=self.manager, reason="Appeal")
        self.assertEqual(reopened.status, "pending")

    def test_the_block_comes_after_the_capsule_rule(self):
        capsule = self.killmail(ship_type_id=f.CAPSULE, ship_name="Capsule", ship_group_id=29, solar_system_security=LOW)
        self.settings(blocked_ship_group_ids="29")
        found = self.prepare(capsule).blockers
        self.assertEqual(found, ["Capsule losses are not covered.", "Capsule ships are not covered."])

    def test_the_check_costs_no_extra_query_when_nothing_is_blocked(self):
        self.settings(blocked_ship_group_ids="", blocked_ship_type_ids="", blocked_security_bands="")
        km = self.killmail()
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        with CaptureQueriesContext(connection) as plain:
            policy.eligibility(km=km, user=self.user, scope="out_of_fleet", link=None, settings_obj=self.srp)
        self.settings(blocked_ship_group_ids="463", blocked_security_bands="highsec")
        with CaptureQueriesContext(connection) as blocked:
            policy.eligibility(km=km, user=self.user, scope="out_of_fleet", link=None, settings_obj=self.srp)
        # A ship that is not blocked does not look for a payout rule.
        self.assertEqual(len(plain), len(blocked))


# --- The settings page --------------------------------------------------------------------------------------------


class BlockSettingsPageTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = f.create_user("srp_config_admin", perms=[*f.MEMBER_PERMS, "fleetops.manage_configuration"])

    def setUp(self):
        super().setUp()
        self.client.force_login(self.admin)
        cache.clear()
        self.url = reverse("fleetops:configuration_list", args=["srp"])

    def base_post(self, **extra):
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

    def test_the_page_shows_the_not_covered_group_and_the_fill_button(self):
        response = self.client.get(self.url)
        self.assertContains(response, 'id="srp-not-covered"')
        self.assertContains(response, 'name="fill_recommended"')
        self.assertContains(response, "Fill in the recommended list")
        self.assertContains(response, "Mining Barge (463)")

    def test_the_fill_button_shows_the_recommended_values_and_saves_nothing(self):
        before = AuditLog.objects.count()
        response = self.client.post(self.url, self.base_post(fill_recommended="1"))

        self.assertEqual(response.status_code, 200)
        form = response.context["form"]
        self.assertEqual(sorted(form["blocked_groups"].value()), sorted(str(i) for i in blocks.RECOMMENDED_SHIP_GROUP_IDS))
        self.assertEqual(form["blocked_ship_type_ids"].value(), "32880, 605, 607, 29248, 586")
        self.assertEqual(form["blocked_bands"].value(), ["highsec"])
        self.assertContains(response, "The recommended lists were filled in. Check them and save.")
        row = SrpSettings.get_solo()
        self.assertEqual((row.blocked_ship_group_ids, row.blocked_ship_type_ids, row.blocked_security_bands), ("", "", ""))
        self.assertEqual(AuditLog.objects.count(), before)

    def test_the_fill_button_adds_to_what_is_entered_without_duplicates(self):
        data = self.base_post(fill_recommended="1", blocked_ship_type_ids="605, 12345", blocked_bands=["lowsec"])
        data["blocked_groups"] = ["463", "25"]
        response = self.client.post(self.url, data)
        form = response.context["form"]
        self.assertEqual(form["blocked_ship_type_ids"].value(), "605, 12345, 32880, 607, 29248, 586")
        self.assertEqual(form["blocked_bands"].value(), ["lowsec", "highsec"])
        groups = form["blocked_groups"].value()
        self.assertEqual(len(groups), len(set(groups)))
        self.assertIn("25", groups)
        self.assertEqual(len(groups), 13)

    def test_saving_after_the_fill_applies_the_lists_and_audits_them(self):
        filled = self.client.post(self.url, self.base_post(fill_recommended="1")).context["form"]
        data = self.base_post(
            blocked_ship_type_ids=filled["blocked_ship_type_ids"].value(), blocked_bands=filled["blocked_bands"].value()
        )
        data["blocked_groups"] = filled["blocked_groups"].value()

        response = self.client.post(self.url, data)

        self.assertEqual(response.status_code, 302)
        row = SrpSettings.get_solo()
        self.assertEqual(
            sorted(row.blocked_ship_group_ids.split(",")), sorted(str(i) for i in blocks.RECOMMENDED_SHIP_GROUP_IDS)
        )
        self.assertEqual(row.blocked_ship_type_ids, "32880,605,607,29248,586")
        self.assertEqual(row.blocked_security_bands, "highsec")
        entry = AuditLog.objects.filter(action="configuration.update", object_type="SrpSettings").latest("pk")
        for name in (
            "blocked_ship_group_ids",
            "blocked_ship_type_ids",
            "blocked_security_bands",
            "payout_rule_lifts_ship_block",
            "highsec_war_losses_covered",
            "ban_default_days",
            "ban_max_days",
            "auto_ban_over_monthly_cap",
        ):
            with self.subTest(field=name):
                self.assertIn(name, entry.old_value)
                self.assertIn(name, entry.new_value)
        self.assertEqual(entry.old_value["blocked_security_bands"], "")
        self.assertEqual(entry.new_value["blocked_security_bands"], "highsec")

    def test_other_group_ids_are_kept_and_unticking_everything_clears_the_lists(self):
        response = self.client.post(self.url, self.base_post(blocked_other_group_ids="99999", blocked_bands=["lowsec"]))
        self.assertEqual(response.status_code, 302)
        row = SrpSettings.get_solo()
        self.assertEqual((row.blocked_ship_group_ids, row.blocked_security_bands), ("99999", "lowsec"))
        # Without the SDE an ID the list does not offer is saved, with a notice.
        self.assertTrue(any("could not be checked" in str(m) for m in response.wsgi_request._messages))

        self.client.post(self.url, self.base_post())
        row.refresh_from_db()
        self.assertEqual((row.blocked_ship_group_ids, row.blocked_security_bands), ("", ""))

    def test_a_group_that_is_not_a_ship_group_is_refused_with_the_sde(self):
        with mock.patch("fleetops.forms_config.sde_service.ship_groups", return_value=[(463, "Mining Barge")]):
            response = self.client.post(self.url, self.base_post(blocked_other_group_ids="99999"))
        self.assertEqual(response.status_code, 200)
        self.assertIn("Not ship groups: 99999.", str(response.context["form"].errors["blocked_other_group_ids"]))
        self.assertEqual(SrpSettings.get_solo().blocked_ship_group_ids, "")

    def test_a_submission_without_the_block_fields_leaves_the_lists_alone(self):
        SrpSettings.objects.filter(pk=1).update(blocked_ship_group_ids="463", blocked_security_bands="highsec")
        data = self.base_post()
        del data["blocked_other_group_ids"], data["blocked_ship_type_ids"]
        response = self.client.post(self.url, data)
        self.assertEqual(response.status_code, 302)
        row = SrpSettings.get_solo()
        self.assertEqual((row.blocked_ship_group_ids, row.blocked_security_bands), ("463", "highsec"))

    def test_bad_ship_ids_and_ban_lengths_are_refused(self):
        response = self.client.post(self.url, self.base_post(blocked_ship_type_ids="12,x"))
        self.assertEqual(response.status_code, 200)
        self.assertIn("blocked_ship_type_ids", response.context["form"].errors)
        response = self.client.post(self.url, self.base_post(ban_default_days="30", ban_max_days="10"))
        self.assertIn("ban_default_days", response.context["form"].errors)

    def test_the_page_names_the_groups_and_ships_in_force(self):
        SrpSettings.objects.filter(pk=1).update(blocked_ship_group_ids="463", blocked_ship_type_ids="32880")
        response = self.client.get(self.url)
        self.assertContains(response, "Ship groups in force")
        self.assertContains(response, "Mining Barge (463)")
        self.assertContains(response, "Venture (32880)")

    def test_pressing_enter_would_save_not_fill(self):
        # The first submit button of the form is the one a browser sends on Enter: it must not be the fill button.
        html = self.client.get(self.url).content.decode()
        first_submit = html.index('type="submit"')
        self.assertLess(first_submit, html.index('name="fill_recommended"'))
        self.assertNotIn("fill_recommended", html[first_submit : first_submit + 200])



# --- The pages members read ---------------------------------------------------------------------------------------


class NotCoveredCardTests(SrpPagesTestCase):
    def test_the_card_lists_what_is_not_covered_on_my_srp_and_the_link_page(self):
        SrpSettings.objects.filter(pk=1).update(
            blocked_ship_group_ids="463", blocked_ship_type_ids="32880", blocked_security_bands="highsec"
        )
        self.patch_home_services()
        for response in (
            self.client.get(reverse("fleetops:srp_home")),
            self.client.get(reverse("fleetops:srp_link", args=[self.link.code])),
        ):
            self.assertContains(response, 'id="srp-not-covered"')
            self.assertContains(response, "Mining Barge")
            self.assertContains(response, "Venture")
            self.assertContains(response, "high security")

    def test_the_card_shows_the_capsule_rule_by_default(self):
        self.patch_home_services()
        response = self.client.get(reverse("fleetops:srp_home"))
        self.assertContains(response, 'id="srp-not-covered"')
        self.assertContains(response, "Capsules are only covered when the payout table lists them.")

    def test_no_card_without_blocks_and_without_the_capsule_rule(self):
        SrpSettings.objects.filter(pk=1).update(capsules_need_rule=False)
        self.patch_home_services()
        response = self.client.get(reverse("fleetops:srp_home"))
        self.assertNotContains(response, 'id="srp-not-covered"')


class PureBlocksTests(TestCase):
    """RUL-3: ``submission_blocks`` reads no database; names come from the caller."""

    def test_submission_blocks_runs_no_query_and_never_asks_for_names(self):
        settings_row = srp(blocked_ship_group_ids="463", blocked_security_bands="lowsec")
        with mock.patch("fleetops.services.composition_config.group_names", side_effect=AssertionError("lookup")):
            with self.assertNumQueries(0):
                found = blocks.submission_blocks(loss(ship_group_id=MINING_BARGE), settings_row)
        self.assertEqual(found[0], "Group 463 ships are not covered.")

    def test_names_given_by_the_caller_are_used(self):
        found = blocks.submission_blocks(
            loss(ship_group_id=MINING_BARGE), srp(blocked_ship_group_ids="463"), group_names={463: "Mining Barge"}
        )
        self.assertEqual(found, ["Mining Barge ships are not covered."])

    def test_the_names_of_many_blocked_losses_cost_one_lookup(self):
        settings_row = srp(blocked_ship_group_ids="463,543")
        losses = [loss(ship_group_id=463 if index % 2 else 543) for index in range(40)]
        losses.append(loss(ship_group_id=25))
        with mock.patch(
            "fleetops.services.composition_config.group_names", return_value={463: "Mining Barge", 543: "Exhumer"}
        ) as lookup:
            names = blocks.blocked_group_names(losses, settings_row)
        self.assertEqual(lookup.call_count, 1)
        self.assertEqual(lookup.call_args.args[0], [463, 543])
        with self.assertNumQueries(0):
            texts = [blocks.submission_blocks(item, settings_row, group_names=names) for item in losses]
        self.assertEqual(texts[0], ["Exhumer ships are not covered."])
        self.assertEqual(texts[-1], [])

    def test_no_lookup_when_no_loss_is_in_a_blocked_group(self):
        with mock.patch("fleetops.services.composition_config.group_names") as lookup:
            self.assertEqual(blocks.blocked_group_names([loss()], srp(blocked_ship_group_ids="463")), {})
        lookup.assert_not_called()


class SecurityDisplayTests(SrpPagesTestCase):
    """REV-9a: the claim page shows the security status and the band; unknown says so."""

    def setUp(self):
        super().setUp()
        # Changed in 0.3.0b1 (REV-9): the security status is shown to SRP staff only, so these pages are
        # read as a manager. The claimant keeps seeing the system name (test_srp_review_info checks both sides).
        self.client = self.client_for(self.manager)

    def claim_with(self, **values):
        km = f.srp_killmail(f.main_of(self.member), fitted="1000000", **values)
        return f.srp_claim(self.member, killmail=km)

    def test_the_claim_page_shows_one_decimal_and_the_band_pill(self):
        claim = self.claim_with(solar_system_security=Decimal("0.4388"), region_id=10_000_001)
        response = self.client.get(self.claim_url(claim))
        self.assertContains(response, "0.4")
        self.assertContains(response, "fo-pill-warn")
        self.assertContains(response, "low security")

    def test_a_wormhole_and_pochven_have_their_own_pills(self):
        wormhole = self.claim_with(solar_system_id=31_000_005, solar_system_security=Decimal("-0.9900"))
        self.assertContains(self.client.get(self.claim_url(wormhole)), "wormhole")
        pochven = self.claim_with(solar_system_security=Decimal("-0.4000"), region_id=blocks.POCHVEN_REGION_ID)
        self.assertContains(self.client.get(self.claim_url(pochven)), "Pochven")

    def test_an_unknown_security_status_says_so(self):
        claim = self.claim_with(solar_system_security=None, region_id=None)
        response = self.client.get(self.claim_url(claim))
        self.assertContains(response, "Security status unknown")
        self.assertNotContains(response, "low security")

    def test_the_filter_marks_null_and_high_security(self):
        from fleetops.templatetags.fleetops_srp import srp_security

        self.assertIn("fo-pill-ok", srp_security(loss(solar_system_security=HIGH)))
        self.assertIn("0.9", srp_security(loss(solar_system_security=HIGH)))
        self.assertIn("fo-pill-bad", srp_security(loss(solar_system_security=Decimal("-0.2"))))
