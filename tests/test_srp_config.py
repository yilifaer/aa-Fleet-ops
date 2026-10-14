# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""The FleetOps SRP configuration: the settings page, the payout table and the corporation limits."""

from decimal import Decimal
from types import SimpleNamespace
from unittest import mock

from allianceauth.eveonline.models import EveCorporationInfo
from django.contrib.messages import get_messages
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

from fleetops import forms_config
from fleetops.forms_config import (
    SrpCorporationLimitForm,
    SrpPayoutRuleForm,
    SrpSettingsForm,
    lookup_ship,
    srp_provider_choices,
)
from fleetops.models import AuditLog, SrpClaim, SrpCorporationLimit, SrpPayoutRule, SrpSettings
from fleetops.providers.doctrines import FITTINGS_DOCTRINE_SOURCE, DoctrineChoice
from fleetops.providers.esi import FleetESIError

from . import factories as f

CONFIG_PERMS = f.MEMBER_PERMS + ["fleetops.manage_configuration"]
RIFTER = 587
MUNINN = 12015


def messages_of(response):
    return [str(message) for message in get_messages(response.wsgi_request)]


def settings_post(**overrides):
    """A valid POST of the SRP settings page: the defaults with the checkboxes the model has on."""
    data = {
        "enabled": "on",
        "out_of_fleet_enabled": "on",
        "zkillboard_enabled": "on",
        "value_source": "zkill_fitted",
        "percentage": "80",
        "in_fleet_loss_cap": "",
        "out_of_fleet_loss_cap": "",
        "personal_monthly_cap": "",
        "personal_cap_includes_in_fleet": "on",
        "corporation_monthly_cap": "",
        "corporation_cap_includes_in_fleet": "on",
        "over_limit_action": "review",
        "capsules_need_rule": "on",
        "claim_deadline_days": "0",
        "notify_members": "on",
        "member_instructions": "",
    }
    data.update(overrides)
    return {key: value for key, value in data.items() if value is not None}


class SrpSettingsPageTests(TestCase):
    def setUp(self):
        self.admin = f.create_user("srp_admin", perms=CONFIG_PERMS)
        self.url = reverse("fleetops:configuration_list", args=["srp"])
        self.client.force_login(self.admin)

    def test_the_address_is_under_the_configuration_pages(self):
        self.assertEqual(self.url, "/fleetops/configuration/srp/")

    def test_page_shows_the_groups_and_the_stored_values(self):
        SrpSettings.objects.update_or_create(
            pk=1, defaults={"personal_monthly_cap": 2_500_000_000, "percentage": Decimal("75.50")}
        )

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "fleetops/configuration/srp_settings.html")
        for title in ("General", "Value", "Payouts and caps", "Monthly limits", "Members and review"):
            self.assertContains(response, title)
        form = response.context["form"]
        self.assertEqual(form["personal_monthly_cap"].value(), "2,500,000,000")
        self.assertContains(response, 'value="2,500,000,000"')
        self.assertContains(response, 'value="75.50"')

    def test_page_links_to_the_lists_and_the_general_settings(self):
        response = self.client.get(self.url)

        for target in (
            reverse("fleetops:configuration_list", args=["srp-rules"]),
            reverse("fleetops:configuration_list", args=["srp-corporation-limits"]),
            reverse("fleetops:configuration_settings"),
        ):
            self.assertContains(response, target)

    def test_settings_are_saved_audited_and_the_index_is_shown_again(self):
        response = self.client.post(
            self.url,
            settings_post(
                enabled="on",
                value_source="zkill_total",
                percentage="85",
                in_fleet_loss_cap="1.5b",
                out_of_fleet_loss_cap="500m",
                personal_monthly_cap="2,000,000,000",
                corporation_monthly_cap="10b",
                over_limit_action="reject",
                claim_deadline_days="30",
                member_instructions="Use the in-game link.",
                allow_self_review="on",
            ),
        )

        self.assertRedirects(response, reverse("fleetops:configuration_index"), fetch_redirect_response=False)
        self.assertIn("SRP settings updated.", messages_of(response))
        obj = SrpSettings.get_solo()
        self.assertTrue(obj.enabled)
        self.assertEqual(obj.value_source, "zkill_total")
        self.assertEqual(obj.percentage, Decimal("85.00"))
        self.assertEqual(
            (obj.in_fleet_loss_cap, obj.out_of_fleet_loss_cap, obj.personal_monthly_cap, obj.corporation_monthly_cap),
            (1_500_000_000, 500_000_000, 2_000_000_000, 10_000_000_000),
        )
        self.assertEqual((obj.over_limit_action, obj.claim_deadline_days), ("reject", 30))
        self.assertTrue(obj.allow_self_review)
        self.assertEqual(obj.member_instructions, "Use the in-game link.")
        entry = AuditLog.objects.get(action="configuration.update", object_type="SrpSettings")
        self.assertEqual(entry.actor, self.admin)
        self.assertEqual(entry.old_value["personal_monthly_cap"], None)
        self.assertEqual(entry.new_value["personal_monthly_cap"], 2_000_000_000)
        self.assertEqual((entry.old_value["over_limit_action"], entry.new_value["over_limit_action"]), ("review", "reject"))
        self.assertEqual(
            (Decimal(entry.old_value["percentage"]), Decimal(entry.new_value["percentage"])), (Decimal("0"), Decimal("85"))
        )
        self.assertEqual((entry.old_value["allow_self_review"], entry.new_value["allow_self_review"]), (False, True))

    def test_unticked_switches_are_saved_off(self):
        response = self.client.post(
            self.url,
            settings_post(
                out_of_fleet_enabled=None,
                personal_cap_includes_in_fleet=None,
                corporation_cap_includes_in_fleet=None,
                capsules_need_rule=None,
                notify_members=None,
            ),
        )

        self.assertEqual(response.status_code, 302)
        obj = SrpSettings.get_solo()
        self.assertFalse(obj.out_of_fleet_enabled)
        self.assertFalse(obj.personal_cap_includes_in_fleet)
        self.assertFalse(obj.corporation_cap_includes_in_fleet)
        self.assertFalse(obj.capsules_need_rule)
        self.assertFalse(obj.notify_members)

    def test_emptying_a_cap_removes_it(self):
        SrpSettings.objects.update_or_create(pk=1, defaults={"in_fleet_loss_cap": 1_000_000_000})

        self.client.post(self.url, settings_post(in_fleet_loss_cap=""))

        self.assertIsNone(SrpSettings.get_solo().in_fleet_loss_cap)

    def test_caps_are_typed_like_isk(self):
        for text, expected in (("250m", 250_000_000), ("1.2b", 1_200_000_000), ("750k", 750_000), ("250,000,000", 250_000_000)):
            with self.subTest(text=text):
                form = SrpSettingsForm(settings_post(personal_monthly_cap=text), instance=SrpSettings.get_solo())
                self.assertTrue(form.is_valid(), form.errors)
                self.assertEqual(form.cleaned_data["personal_monthly_cap"], expected)

    def test_bad_caps_are_refused(self):
        for text in ("1,5m", "abc", "0", "-5m", "1000000000000000001", "12.5"):
            with self.subTest(text=text):
                form = SrpSettingsForm(settings_post(in_fleet_loss_cap=text), instance=SrpSettings.get_solo())
                self.assertFalse(form.is_valid())
                self.assertIn("in_fleet_loss_cap", form.errors)

    def test_percentage_is_between_zero_and_one_hundred(self):
        for text, valid in (("0", True), ("100", True), ("33.33", True), ("100.01", False), ("-1", False), ("abc", False)):
            with self.subTest(text=text):
                form = SrpSettingsForm(settings_post(percentage=text), instance=SrpSettings.get_solo())
                self.assertEqual(form.is_valid(), valid, form.errors)
                if not valid:
                    self.assertIn("percentage", form.errors)

    def test_zkillboard_off_needs_the_esi_estimate(self):
        form = SrpSettingsForm(
            settings_post(zkillboard_enabled=None, value_source="zkill_fitted"), instance=SrpSettings.get_solo()
        )
        self.assertFalse(form.is_valid())
        self.assertEqual(
            form.errors["value_source"], ["Choose the ESI market estimate when zKillboard is turned off."]
        )

        form = SrpSettingsForm(
            settings_post(zkillboard_enabled=None, value_source="esi_estimate"), instance=SrpSettings.get_solo()
        )
        self.assertTrue(form.is_valid(), form.errors)

    def test_deadline_and_instructions_have_limits(self):
        for field, value in (
            ("claim_deadline_days", "3651"),
            ("claim_deadline_days", "-1"),
            ("member_instructions", "x" * 2001),
        ):
            with self.subTest(field=field, value=value[:10]):
                form = SrpSettingsForm(settings_post(**{field: value}), instance=SrpSettings.get_solo())
                self.assertFalse(form.is_valid())
                self.assertIn(field, form.errors)
        form = SrpSettingsForm(
            settings_post(claim_deadline_days="3650", member_instructions="x" * 2000), instance=SrpSettings.get_solo()
        )
        self.assertTrue(form.is_valid(), form.errors)

    def test_a_refused_form_changes_and_audits_nothing(self):
        response = self.client.post(self.url, settings_post(zkillboard_enabled=None, percentage="55"))

        self.assertEqual(response.status_code, 200)
        self.assertIn("value_source", response.context["form"].errors)
        self.assertEqual(SrpSettings.get_solo().percentage, Decimal("0"))
        self.assertFalse(AuditLog.objects.filter(object_type="SrpSettings").exists())

    def test_the_form_lists_every_setting_exactly_once(self):
        form = SrpSettingsForm(instance=SrpSettings.get_solo())
        listed = [field.name for _title, _text, fields in form.field_groups() for field in fields]

        self.assertEqual(sorted(listed), sorted(form.fields))
        self.assertEqual(len(listed), len(set(listed)))

    def test_page_warns_when_fleets_do_not_get_a_fleetops_link(self):
        f.settings(srp_provider="aasrp")
        self.assertContains(self.client.get(self.url), 'id="srp-provider-notice"')
        f.settings(srp_provider="auto", srp_auto_create=False)
        response = self.client.get(self.url)
        self.assertContains(response, 'id="srp-auto-create-notice"')
        self.assertNotContains(response, 'id="srp-provider-notice"')
        f.settings(srp_provider="fleetops", srp_auto_create=True)
        response = self.client.get(self.url)
        self.assertNotContains(response, 'id="srp-provider-notice"')
        self.assertNotContains(response, 'id="srp-auto-create-notice"')

    def test_only_configuration_managers_get_in(self):
        for perms in (f.MEMBER_PERMS, f.SRP_MANAGER_PERMS):
            with self.subTest(perms=perms):
                self.client.force_login(f.create_user(perms=perms))
                self.assertEqual(self.client.get(self.url).status_code, 403)
                self.assertEqual(self.client.post(self.url, settings_post()).status_code, 403)
        self.client.logout()
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 302)
        self.assertIn("login", response["Location"])

    def test_index_has_a_card_for_srp(self):
        response = self.client.get(reverse("fleetops:configuration_index"))

        self.assertContains(response, "FleetOps SRP")
        self.assertContains(response, self.url)
        self.assertContains(response, "Disabled")
        f.srp_settings(enabled=True)
        self.assertContains(self.client.get(reverse("fleetops:configuration_index")), "Enabled")

    def test_srp_is_not_a_list_section(self):
        for name in ("configuration_add",):
            self.assertEqual(self.client.get(reverse(f"fleetops:{name}", args=["srp"])).status_code, 404)
        self.assertEqual(self.client.get(reverse("fleetops:configuration_edit", args=["srp", 1])).status_code, 404)


class LookupShipTests(TestCase):
    def test_empty_means_no_ship(self):
        self.assertEqual(lookup_ship("  "), forms_config.ShipLookup(0, ""))
        self.assertEqual(lookup_ship(None), forms_config.ShipLookup(0, ""))

    def test_an_id_is_named_by_the_sde_when_it_knows_it(self):
        with mock.patch.object(forms_config.sde_service, "item_type_names", return_value={RIFTER: "Rifter"}):
            self.assertEqual(lookup_ship("587"), forms_config.ShipLookup(RIFTER, "Rifter"))

    def test_an_id_is_named_by_esi_without_the_sde(self):
        resolve = mock.Mock(return_value={RIFTER: ("inventory_type", "Rifter")})
        with mock.patch.object(forms_config.sde_service, "item_type_names", return_value={}), mock.patch.object(
            forms_config.esi_provider, "resolve_names", resolve
        ):
            self.assertEqual(lookup_ship(" 587 "), forms_config.ShipLookup(RIFTER, "Rifter"))
        resolve.assert_called_once_with([RIFTER])

    def test_an_id_of_something_else_is_refused(self):
        for answer in ({RIFTER: ("corporation", "Some Corp")}, {RIFTER: ("unresolved", "")}, {}):
            with self.subTest(answer=answer):
                with mock.patch.object(forms_config.sde_service, "item_type_names", return_value={}), mock.patch.object(
                    forms_config.esi_provider, "resolve_names", return_value=answer
                ):
                    with self.assertRaises(ValidationError) as caught:
                        lookup_ship("587")
                self.assertEqual(caught.exception.messages, ["This ID is not an item type."])

    def test_an_id_is_kept_with_a_warning_when_esi_cannot_be_asked(self):
        with mock.patch.object(forms_config.sde_service, "item_type_names", return_value={}), mock.patch.object(
            forms_config.esi_provider, "resolve_names", side_effect=FleetESIError("NAMES_ERROR", "down")
        ):
            found = lookup_ship("587")

        self.assertEqual((found.type_id, found.name), (RIFTER, ""))
        self.assertEqual(found.warning, forms_config.SHIP_NAME_MISSING)

    def test_ids_out_of_range_are_refused(self):
        for text in ("0", str(2**63), "99999999999999999999"):
            with self.subTest(text=text):
                with self.assertRaises(ValidationError):
                    lookup_ship(text)

    def test_a_name_is_looked_up_in_the_sde_when_it_is_installed(self):
        with mock.patch.object(forms_config.sde_service, "sde_installed", return_value=True), mock.patch.object(
            forms_config.sde_service, "find_item_type", return_value=(MUNINN, "Muninn")
        ) as find:
            self.assertEqual(lookup_ship("  muninn "), forms_config.ShipLookup(MUNINN, "Muninn"))
        find.assert_called_once_with("muninn")

    def test_an_unknown_name_is_refused_with_the_sde(self):
        with mock.patch.object(forms_config.sde_service, "sde_installed", return_value=True), mock.patch.object(
            forms_config.sde_service, "find_item_type", return_value=None
        ):
            with self.assertRaises(ValidationError) as caught:
                lookup_ship("Banana")

        self.assertEqual(
            caught.exception.messages, ["No ship called “Banana” was found. Use its exact English name or its type ID."]
        )

    def esi_client(self, answer=None, error=None):
        request = mock.Mock()
        request.result.side_effect = error
        request.result.return_value = answer
        universe = SimpleNamespace(PostUniverseIds=mock.Mock(return_value=request))
        return SimpleNamespace(client=SimpleNamespace(Universe=universe)), universe, request

    def test_a_name_is_looked_up_at_esi_without_the_sde(self):
        client, universe, request = self.esi_client({"inventory_types": [{"id": MUNINN, "name": "Muninn"}]})
        with mock.patch.object(forms_config.sde_service, "sde_installed", return_value=False), mock.patch.object(
            forms_config.esi_provider, "esi", client
        ):
            found = lookup_ship("muninn")

        self.assertEqual(found, forms_config.ShipLookup(MUNINN, "Muninn"))
        universe.PostUniverseIds.assert_called_once_with(body=["muninn"])
        # A POST answer must not come from or go into the response cache.
        request.result.assert_called_once_with(use_etag=False, use_cache=False, store_cache=False)

    def test_esi_answers_in_objects_too(self):
        row = SimpleNamespace(id=MUNINN, name="Muninn")
        client, *_ = self.esi_client(SimpleNamespace(inventory_types=[row]))
        with mock.patch.object(forms_config.sde_service, "sde_installed", return_value=False), mock.patch.object(
            forms_config.esi_provider, "esi", client
        ):
            self.assertEqual(lookup_ship("Muninn"), forms_config.ShipLookup(MUNINN, "Muninn"))

    def test_a_name_esi_does_not_know_is_refused(self):
        for answer in ({}, {"inventory_types": []}, {"inventory_types": [{"id": 1, "name": "Something Else"}]}):
            with self.subTest(answer=answer):
                client, *_ = self.esi_client(answer)
                with mock.patch.object(forms_config.sde_service, "sde_installed", return_value=False), mock.patch.object(
                    forms_config.esi_provider, "esi", client
                ):
                    with self.assertRaises(ValidationError):
                        lookup_ship("Muninn")

    def test_an_esi_failure_asks_for_the_id(self):
        client, *_ = self.esi_client(error=RuntimeError("secret detail"))
        with mock.patch.object(forms_config.sde_service, "sde_installed", return_value=False), mock.patch.object(
            forms_config.esi_provider, "esi", client
        ):
            with self.assertLogs(forms_config.logger, "WARNING") as logs:
                with self.assertRaises(ValidationError) as caught:
                    lookup_ship("Muninn")

        self.assertEqual(caught.exception.messages, [forms_config.SHIP_LOOKUP_FAILED])
        self.assertNotIn("secret detail", "\n".join(logs.output))


class PayoutTableTests(TestCase):
    def setUp(self):
        self.admin = f.create_user("rules_admin", perms=CONFIG_PERMS)
        self.client.force_login(self.admin)
        self.add_url = reverse("fleetops:configuration_add", args=["srp-rules"])
        self.list_url = reverse("fleetops:configuration_list", args=["srp-rules"])
        for patcher in (
            mock.patch.object(forms_config.sde_service, "item_type_names", side_effect=lambda ids: self.names(ids)),
            mock.patch.object(forms_config.sde_service, "sde_installed", return_value=True),
            mock.patch.object(forms_config.sde_service, "find_item_type", side_effect=lambda name: self.find(name)),
            mock.patch.object(forms_config, "get_doctrines", side_effect=lambda user=None: list(self.doctrines)),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.ships = {RIFTER: "Rifter", MUNINN: "Muninn", 670: "Capsule"}
        self.doctrines = [
            DoctrineChoice("12", "Muninn Fleet", FITTINGS_DOCTRINE_SOURCE),
            DoctrineChoice("13", "Logistics", FITTINGS_DOCTRINE_SOURCE),
        ]

    def names(self, ids):
        return {i: self.ships[i] for i in ids if i in self.ships}

    def find(self, name):
        for type_id, ship in self.ships.items():
            if ship.casefold() == name.casefold():
                return type_id, ship
        return None

    def post(self, **fields):
        data = {"ship": "", "doctrine_choice": "", "doctrine_custom": "", "scope": "any", "amount": "50m", "is_active": "on"}
        data.update(fields)
        return self.client.post(self.add_url, data)

    def test_add_page_has_the_fields(self):
        response = self.client.get(self.add_url)

        self.assertEqual(response.status_code, 200)
        form = response.context["form"]
        self.assertEqual(list(form.fields), ["label", "ship", "doctrine_choice", "doctrine_custom", "scope", "amount", "is_active", "notes"])
        self.assertEqual(
            [value for value, _label in form.fields["doctrine_choice"].choices],
            ["", "custom", f"{FITTINGS_DOCTRINE_SOURCE}|12|Muninn Fleet", f"{FITTINGS_DOCTRINE_SOURCE}|13|Logistics"],
        )
        self.assertContains(response, "Doctrine rules only apply to losses in a fleet")

    def test_a_ship_rule_by_name(self):
        response = self.post(ship="rifter", amount="12.5m", label="Frigates")

        self.assertRedirects(response, self.list_url, fetch_redirect_response=False)
        rule = SrpPayoutRule.objects.get()
        self.assertEqual((rule.ship_type_id, rule.ship_type_name), (RIFTER, "Rifter"))
        self.assertEqual((rule.doctrine_key, rule.doctrine_name, rule.scope), ("", "", "any"))
        self.assertEqual((rule.amount, rule.label, rule.is_active), (12_500_000, "Frigates", True))

    def test_a_ship_rule_by_id(self):
        self.post(ship="12015", amount="300m", scope="in_fleet")

        rule = SrpPayoutRule.objects.get()
        self.assertEqual((rule.ship_type_id, rule.ship_type_name, rule.scope), (MUNINN, "Muninn", "in_fleet"))

    def test_a_doctrine_rule_of_a_doctrine_app(self):
        self.post(doctrine_choice=f"{FITTINGS_DOCTRINE_SOURCE}|12|Muninn Fleet", amount="100m")

        rule = SrpPayoutRule.objects.get()
        self.assertEqual(rule.ship_type_id, 0)
        self.assertEqual(
            (rule.doctrine_source, rule.doctrine_external_id, rule.doctrine_name),
            (FITTINGS_DOCTRINE_SOURCE, "12", "Muninn Fleet"),
        )
        self.assertEqual(rule.doctrine_key, f"{FITTINGS_DOCTRINE_SOURCE}:12")

    def test_a_ship_of_a_doctrine(self):
        self.post(ship="Muninn", doctrine_choice=f"{FITTINGS_DOCTRINE_SOURCE}|12|Muninn Fleet", amount="250m")

        rule = SrpPayoutRule.objects.get()
        self.assertEqual((rule.ship_type_id, rule.doctrine_key), (MUNINN, f"{FITTINGS_DOCTRINE_SOURCE}:12"))

    def test_a_doctrine_by_custom_name(self):
        self.post(doctrine_choice="custom", doctrine_custom="  Ferox   Fleet ", amount="90m")

        rule = SrpPayoutRule.objects.get()
        self.assertEqual((rule.doctrine_source, rule.doctrine_external_id, rule.doctrine_name), ("", "", "Ferox Fleet"))
        self.assertEqual(rule.doctrine_key, "name:ferox fleet")

    def test_the_custom_name_is_needed_with_custom(self):
        response = self.post(doctrine_choice="custom", doctrine_custom=" ")

        self.assertEqual(response.status_code, 200)
        self.assertIn("doctrine_custom", response.context["form"].errors)
        self.assertFalse(SrpPayoutRule.objects.exists())

    def test_a_rule_needs_a_ship_or_a_doctrine(self):
        response = self.post()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["form"].non_field_errors(), ["Choose a ship type, a doctrine, or both."])
        self.assertFalse(SrpPayoutRule.objects.exists())

    def test_a_doctrine_rule_cannot_be_for_losses_out_of_fleet(self):
        response = self.post(doctrine_choice="custom", doctrine_custom="Ferox", scope="out_of_fleet")

        self.assertEqual(response.status_code, 200)
        self.assertIn("scope", response.context["form"].errors)
        self.assertFalse(SrpPayoutRule.objects.exists())

    def test_a_ship_rule_can_be_for_losses_out_of_fleet(self):
        self.post(ship="Rifter", scope="out_of_fleet")

        self.assertEqual(SrpPayoutRule.objects.get().scope, "out_of_fleet")

    def test_unknown_ship_is_refused(self):
        response = self.post(ship="Banana")

        self.assertIn("ship", response.context["form"].errors)
        self.assertFalse(SrpPayoutRule.objects.exists())

    def test_the_amount_is_needed_and_positive(self):
        for amount in ("", "0", "-1m", "abc", "1,5m"):
            with self.subTest(amount=amount):
                response = self.post(ship="Rifter", amount=amount)
                self.assertEqual(response.status_code, 200)
                self.assertIn("amount", response.context["form"].errors)
        self.assertFalse(SrpPayoutRule.objects.exists())

    def test_the_same_rule_twice_is_refused(self):
        self.post(ship="Rifter", scope="any")

        response = self.post(ship="587", scope="any", amount="1m")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.context["form"].non_field_errors(), ["A rule for this ship, doctrine and scope already exists."]
        )
        self.assertEqual(SrpPayoutRule.objects.count(), 1)

    def test_a_custom_doctrine_name_is_compared_ignoring_case(self):
        self.post(doctrine_choice="custom", doctrine_custom="Ferox Fleet")

        response = self.post(doctrine_choice="custom", doctrine_custom="ferox fleet ")

        self.assertEqual(
            response.context["form"].non_field_errors(), ["A rule for this ship, doctrine and scope already exists."]
        )

    def test_other_scopes_ships_and_doctrines_are_no_duplicates(self):
        self.post(ship="Rifter", scope="any")
        for fields in (
            {"ship": "Rifter", "scope": "in_fleet"},
            {"ship": "Muninn", "scope": "any"},
            {"ship": "Rifter", "scope": "any", "doctrine_choice": f"{FITTINGS_DOCTRINE_SOURCE}|12|Muninn Fleet"},
        ):
            with self.subTest(fields=fields):
                self.assertEqual(self.post(**fields).status_code, 302)
        self.assertEqual(SrpPayoutRule.objects.count(), 4)

    def test_the_capsule_can_have_a_rule(self):
        self.post(ship="Capsule", amount="1m")

        rule = SrpPayoutRule.objects.get()
        self.assertEqual(rule.ship_type_id, 670)

    def test_creation_is_audited(self):
        self.post(ship="Rifter", amount="12.5m")

        rule = SrpPayoutRule.objects.get()
        entry = AuditLog.objects.get(action="configuration.create", object_type="SrpPayoutRule")
        self.assertEqual((entry.actor, entry.object_id), (self.admin, str(rule.pk)))
        self.assertEqual(entry.new_value["amount"], 12_500_000)
        self.assertEqual(entry.new_value["ship_type_id"], RIFTER)
        self.assertIsNone(entry.old_value)

    def test_the_list_shows_ship_doctrine_scope_and_amount(self):
        f.payout_rule(RIFTER, 12_500_000, ship_name="Rifter", scope="in_fleet")
        f.payout_rule(0, 100_000_000, doctrine=(FITTINGS_DOCTRINE_SOURCE, "12", "Muninn Fleet"))
        f.payout_rule(MUNINN, 5_000_000, ship_name="", is_active=False)

        response = self.client.get(self.list_url)

        self.assertContains(response, "SRP Payout Table")
        self.assertContains(response, "Rifter")
        self.assertContains(response, "In-fleet losses only")
        self.assertContains(response, "12,500,000 ISK")
        self.assertContains(response, "Any ship")
        self.assertContains(response, "Muninn Fleet")
        self.assertContains(response, "Any doctrine")
        self.assertContains(response, "Ship 12015")
        self.assertContains(response, "In fleet and out of fleet")

    def test_edit_shows_the_stored_rule(self):
        rule = f.payout_rule(
            MUNINN, 250_000_000, ship_name="Muninn", doctrine=(FITTINGS_DOCTRINE_SOURCE, "12", "Muninn Fleet"),
            scope="in_fleet",
        )

        response = self.client.get(reverse("fleetops:configuration_edit", args=["srp-rules", rule.pk]))

        form = response.context["form"]
        self.assertEqual(form["ship"].value(), "Muninn")
        self.assertEqual(form["doctrine_choice"].value(), f"{FITTINGS_DOCTRINE_SOURCE}|12|Muninn Fleet")
        self.assertEqual(form["amount"].value(), "250,000,000")
        self.assertContains(response, 'value="250,000,000"')

    def edit(self, rule, **fields):
        data = {
            "ship": rule.ship_type_name or str(rule.ship_type_id),
            "doctrine_choice": "",
            "doctrine_custom": "",
            "scope": rule.scope,
            "amount": str(rule.amount),
            "is_active": "on",
        }
        data.update(fields)
        return self.client.post(reverse("fleetops:configuration_edit", args=["srp-rules", rule.pk]), data)

    def test_editing_the_amount_keeps_the_rest_and_is_audited(self):
        rule = f.payout_rule(RIFTER, 10_000_000, ship_name="Rifter")

        response = self.edit(rule, amount="15m", notes="Raised")

        self.assertRedirects(response, self.list_url, fetch_redirect_response=False)
        rule.refresh_from_db()
        self.assertEqual((rule.amount, rule.ship_type_id, rule.notes), (15_000_000, RIFTER, "Raised"))
        entry = AuditLog.objects.get(action="configuration.update", object_type="SrpPayoutRule")
        self.assertEqual((entry.old_value["amount"], entry.new_value["amount"]), (10_000_000, 15_000_000))

    def test_a_rule_does_not_clash_with_itself(self):
        rule = f.payout_rule(0, 10_000_000, doctrine="Ferox Fleet")

        response = self.edit(rule, ship="", doctrine_choice="custom", doctrine_custom="Ferox Fleet", amount="20m")

        self.assertEqual(response.status_code, 302)
        rule.refresh_from_db()
        self.assertEqual(rule.amount, 20_000_000)

    def test_editing_into_another_rule_is_refused(self):
        f.payout_rule(RIFTER, 10_000_000, ship_name="Rifter", scope="in_fleet")
        rule = f.payout_rule(RIFTER, 10_000_000, ship_name="Rifter", scope="out_of_fleet")

        response = self.edit(rule, scope="in_fleet")

        self.assertEqual(
            response.context["form"].non_field_errors(), ["A rule for this ship, doctrine and scope already exists."]
        )
        rule.refresh_from_db()
        self.assertEqual(rule.scope, "out_of_fleet")

    def test_a_doctrine_that_left_the_doctrine_app_stays_selectable(self):
        rule = f.payout_rule(0, 10_000_000, doctrine=(FITTINGS_DOCTRINE_SOURCE, "99", "Old Doctrine"))

        response = self.client.get(reverse("fleetops:configuration_edit", args=["srp-rules", rule.pk]))
        choices = dict(response.context["form"].fields["doctrine_choice"].choices)
        stored = f"{FITTINGS_DOCTRINE_SOURCE}|99|Old Doctrine"

        self.assertEqual(choices[stored], "Old Doctrine (no longer listed)")
        saved = self.edit(rule, ship="", doctrine_choice=stored, amount="11m")
        self.assertEqual(saved.status_code, 302)

    def test_a_custom_doctrine_is_shown_as_custom(self):
        rule = f.payout_rule(0, 10_000_000, doctrine="Ferox Fleet")

        form = SrpPayoutRuleForm(instance=rule)

        self.assertEqual(form["doctrine_choice"].value(), "custom")
        self.assertEqual(form["doctrine_custom"].value(), "Ferox Fleet")

    def test_deleting_a_rule_keeps_the_claims_and_is_audited(self):
        rule = f.payout_rule(RIFTER, 10_000_000, ship_name="Rifter")
        user = f.create_user("rule_claimant", perms=f.SRP_MEMBER_PERMS)
        claim = f.srp_claim(user, payout_rule=rule, payout_basis="rule")

        response = self.client.post(reverse("fleetops:configuration_delete", args=["srp-rules", rule.pk]))

        self.assertRedirects(response, self.list_url, fetch_redirect_response=False)
        self.assertFalse(SrpPayoutRule.objects.exists())
        claim = SrpClaim.objects.get(pk=claim.pk)
        self.assertIsNone(claim.payout_rule)
        self.assertEqual(claim.payout_basis, "rule")
        entry = AuditLog.objects.get(action="configuration.delete", object_type="SrpPayoutRule")
        self.assertEqual(entry.old_value["ship_type_id"], RIFTER)

    def test_a_name_is_resolved_at_esi_when_the_sde_is_not_installed(self):
        client = SimpleNamespace(
            client=SimpleNamespace(
                Universe=SimpleNamespace(
                    PostUniverseIds=mock.Mock(
                        return_value=mock.Mock(result=mock.Mock(return_value={"inventory_types": [{"id": RIFTER, "name": "Rifter"}]}))
                    )
                )
            )
        )
        with mock.patch.object(forms_config.sde_service, "sde_installed", return_value=False), mock.patch.object(
            forms_config.esi_provider, "esi", client
        ):
            response = self.post(ship="rifter")

        self.assertEqual(response.status_code, 302)
        self.assertEqual(SrpPayoutRule.objects.get().ship_type_name, "Rifter")

    def test_an_id_that_cannot_be_named_is_saved_with_a_warning(self):
        with mock.patch.object(forms_config.sde_service, "item_type_names", return_value={}), mock.patch.object(
            forms_config.esi_provider, "resolve_names", side_effect=FleetESIError("NAMES_ERROR", "down")
        ):
            response = self.post(ship="587")

        self.assertRedirects(response, self.list_url, fetch_redirect_response=False)
        self.assertEqual(SrpPayoutRule.objects.get().ship_type_name, "")
        self.assertIn(forms_config.SHIP_NAME_MISSING, messages_of(response))

    def test_only_configuration_managers_get_in(self):
        user = f.create_user(perms=f.SRP_MANAGER_PERMS)
        self.client.force_login(user)

        self.assertEqual(self.client.get(self.list_url).status_code, 403)
        self.assertEqual(self.client.get(self.add_url).status_code, 403)
        self.assertEqual(self.post(ship="Rifter").status_code, 403)
        self.assertFalse(SrpPayoutRule.objects.exists())


class CorporationLimitTests(TestCase):
    CORPORATION = 98_000_123

    def setUp(self):
        self.admin = f.create_user("limits_admin", perms=CONFIG_PERMS)
        self.client.force_login(self.admin)
        self.add_url = reverse("fleetops:configuration_add", args=["srp-corporation-limits"])
        self.list_url = reverse("fleetops:configuration_list", args=["srp-corporation-limits"])
        patcher = mock.patch.object(forms_config, "entity_names", return_value={})
        self.entity_names = patcher.start()
        self.addCleanup(patcher.stop)

    def post(self, **fields):
        data = {"corporation_id": str(self.CORPORATION), "corporation_name": "", "monthly_cap": "5b", "note": ""}
        data.update(fields)
        return self.client.post(self.add_url, data)

    def test_a_limit_with_the_name_from_alliance_auth(self):
        EveCorporationInfo.objects.create(
            corporation_id=self.CORPORATION, corporation_name="Known Corp", corporation_ticker="KNOWN", member_count=3
        )

        response = self.post()

        self.assertRedirects(response, self.list_url, fetch_redirect_response=False)
        limit = SrpCorporationLimit.objects.get()
        self.assertEqual((limit.corporation_id, limit.corporation_name, limit.monthly_cap), (self.CORPORATION, "Known Corp", 5_000_000_000))
        self.entity_names.assert_not_called()

    def test_the_name_comes_from_esi_names_otherwise(self):
        self.entity_names.return_value = {self.CORPORATION: "Far Corp"}

        self.post()

        self.entity_names.assert_called_once_with([self.CORPORATION])
        self.assertEqual(SrpCorporationLimit.objects.get().corporation_name, "Far Corp")

    def test_an_unknown_corporation_is_saved_with_a_warning(self):
        response = self.post()

        self.assertRedirects(response, self.list_url, fetch_redirect_response=False)
        self.assertEqual(SrpCorporationLimit.objects.get().corporation_name, "")
        self.assertTrue(any("could not be looked up" in message for message in messages_of(response)))

    def test_a_name_that_was_typed_is_not_looked_up(self):
        self.post(corporation_name="My Own Name")

        self.assertEqual(SrpCorporationLimit.objects.get().corporation_name, "My Own Name")
        self.entity_names.assert_not_called()

    def test_no_cap_means_no_limit_for_the_corporation(self):
        self.post(monthly_cap="", note="Allies")

        limit = SrpCorporationLimit.objects.get()
        self.assertIsNone(limit.monthly_cap)
        response = self.client.get(self.list_url)
        self.assertContains(response, "No limit")
        self.assertContains(response, "Allies")

    def test_the_list_shows_name_and_cap(self):
        SrpCorporationLimit.objects.create(corporation_id=1, corporation_name="Alpha Corp", monthly_cap=2_000_000_000)
        SrpCorporationLimit.objects.create(corporation_id=2, corporation_name="", monthly_cap=None)

        response = self.client.get(self.list_url)

        self.assertContains(response, "SRP Corporation Limits")
        self.assertContains(response, "Alpha Corp")
        self.assertContains(response, "2,000,000,000 ISK")
        self.assertContains(response, ">2<")

    def test_a_corporation_has_one_limit(self):
        self.post()

        response = self.post(monthly_cap="1b")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["form"].errors["corporation_id"], ["This corporation already has a limit."])
        self.assertEqual(SrpCorporationLimit.objects.get().monthly_cap, 5_000_000_000)

    def test_bad_input_is_refused(self):
        for fields in (
            {"corporation_id": "0"},
            {"corporation_id": "-4"},
            {"corporation_id": "abc"},
            {"corporation_id": ""},
            {"monthly_cap": "0"},
            {"monthly_cap": "1,5b"},
            {"monthly_cap": "x"},
        ):
            with self.subTest(fields=fields):
                self.assertEqual(self.post(**fields).status_code, 200)
        self.assertFalse(SrpCorporationLimit.objects.exists())

    def test_editing_and_deleting_are_audited(self):
        limit = SrpCorporationLimit.objects.create(corporation_id=self.CORPORATION, corporation_name="Known", monthly_cap=1)
        edit_url = reverse("fleetops:configuration_edit", args=["srp-corporation-limits", limit.pk])

        response = self.client.post(
            edit_url,
            {"corporation_id": str(self.CORPORATION), "corporation_name": "Known", "monthly_cap": "7b", "note": "Raised"},
        )

        self.assertRedirects(response, self.list_url, fetch_redirect_response=False)
        entry = AuditLog.objects.get(action="configuration.update", object_type="SrpCorporationLimit")
        self.assertEqual((entry.old_value["monthly_cap"], entry.new_value["monthly_cap"]), (1, 7_000_000_000))
        self.client.post(reverse("fleetops:configuration_delete", args=["srp-corporation-limits", limit.pk]))
        self.assertFalse(SrpCorporationLimit.objects.exists())
        self.assertTrue(AuditLog.objects.filter(action="configuration.delete", object_type="SrpCorporationLimit").exists())

    def test_edit_page_shows_the_cap_with_separators(self):
        limit = SrpCorporationLimit.objects.create(corporation_id=self.CORPORATION, monthly_cap=2_500_000_000)

        response = self.client.get(reverse("fleetops:configuration_edit", args=["srp-corporation-limits", limit.pk]))

        self.assertContains(response, 'value="2,500,000,000"')

    def test_the_form_class_is_the_one_of_the_section(self):
        from fleetops.views_config import CONFIG_SECTIONS

        self.assertIs(CONFIG_SECTIONS["srp-corporation-limits"]["form"], SrpCorporationLimitForm)
        self.assertIs(CONFIG_SECTIONS["srp-rules"]["form"], SrpPayoutRuleForm)


class SrpConfigurationAccessTests(TestCase):
    """Every SRP configuration page needs manage_configuration; SRP management alone is not enough."""

    def setUp(self):
        self.rule = f.payout_rule(RIFTER, 10 * 1_000_000, ship_name="Rifter")
        self.limit = SrpCorporationLimit.objects.create(corporation_id=98_000_555, monthly_cap=1_000_000)

    def pages(self):
        r = reverse
        return [
            ("get", r("fleetops:configuration_list", args=["srp"])),
            ("get", r("fleetops:configuration_list", args=["srp-rules"])),
            ("get", r("fleetops:configuration_add", args=["srp-rules"])),
            ("get", r("fleetops:configuration_edit", args=["srp-rules", self.rule.pk])),
            ("post", r("fleetops:configuration_delete", args=["srp-rules", self.rule.pk])),
            ("get", r("fleetops:configuration_list", args=["srp-corporation-limits"])),
            ("get", r("fleetops:configuration_add", args=["srp-corporation-limits"])),
            ("get", r("fleetops:configuration_edit", args=["srp-corporation-limits", self.limit.pk])),
            ("post", r("fleetops:configuration_delete", args=["srp-corporation-limits", self.limit.pk])),
        ]

    def test_anonymous_users_are_sent_to_the_login(self):
        for method, url in self.pages():
            with self.subTest(url=url):
                response = getattr(self.client, method)(url)
                self.assertEqual(response.status_code, 302)
                self.assertIn("login", response["Location"])

    def test_members_and_srp_managers_are_refused(self):
        for perms in (f.MEMBER_PERMS, f.SRP_MEMBER_PERMS, f.SRP_MANAGER_PERMS):
            self.client.force_login(f.create_user(perms=perms))
            for method, url in self.pages():
                with self.subTest(url=url, perms=perms):
                    self.assertEqual(getattr(self.client, method)(url).status_code, 403)
        self.assertTrue(SrpPayoutRule.objects.exists())
        self.assertTrue(SrpCorporationLimit.objects.exists())

    def test_configuration_managers_get_in(self):
        self.client.force_login(f.create_user(perms=CONFIG_PERMS))

        for method, url in self.pages():
            if method == "get":
                with self.subTest(url=url):
                    self.assertEqual(self.client.get(url).status_code, 200)

    def test_deleting_needs_a_post(self):
        self.client.force_login(f.create_user(perms=CONFIG_PERMS))

        for url in (self.pages()[4][1], self.pages()[8][1]):
            self.assertEqual(self.client.get(url).status_code, 405)
        self.assertTrue(SrpPayoutRule.objects.exists())


class ProviderChoiceTests(TestCase):
    def test_fleetops_srp_is_listed_with_the_real_registry(self):
        f.srp_settings(enabled=True)

        choices = dict(srp_provider_choices())

        self.assertEqual(choices["auto"], "Automatic (FleetOps SRP when enabled, then aa-srp, then Alliance Auth SRP)")
        self.assertEqual(choices["fleetops"], "FleetOps SRP (enabled)")
        keys = [key for key, _label in srp_provider_choices()]
        self.assertLess(keys.index("fleetops"), keys.index("aasrp"))
        self.assertLess(keys.index("aasrp"), keys.index("allianceauth_builtin"))
