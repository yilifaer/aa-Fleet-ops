# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Administration in 0.2.0b2: the ship groups without bridge as a list (FB01), the fleet positions of ship
functions (FB15.3), the object names of the audit log (FB15.4) and the look of the configuration lists and forms."""

from types import SimpleNamespace
from unittest import mock

from django.contrib.messages import get_messages
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from django.utils import translation

from fleetops import forms_config
from fleetops.fleet_defaults import KNOWN_SHIP_GROUPS
from fleetops.forms_config import FleetOpsSettingsForm
from fleetops.models import AuditLog, CommsPreset, FleetOpsSettings, GroupAccess, ShipFunction
from fleetops.services import composition_config
from fleetops.services import sde as sde_module
from fleetops.services.audit import audit, audit_event
from fleetops.services.fleet_rules import DEFAULT_BRIDGE_EXCLUDED_GROUP_IDS
from fleetops.templatetags.fleetops_config import audit_object_label

from . import factories as f
from .test_configuration_audit import settings_payload
from .test_fleet_config import FakeModel, FakeSDEApps, fake_sde

CONFIG_PERMS = f.MEMBER_PERMS + ["fleetops.manage_configuration"]
SHIP = 6
DEFAULT_IDS = [int(group_id) for group_id in DEFAULT_BRIDGE_EXCLUDED_GROUP_IDS.split(",")]


def sde_with_ship_groups():
    """An SDE whose item groups include published and unpublished ship groups and a group of another category."""

    def group(pk, name, category=SHIP, published=True):
        return SimpleNamespace(pk=pk, name=f"{name} (local)", name_en=name, category_id=category, published=published)

    groups = FakeModel(
        [
            group(900, "Marauder"),
            group(25, "Frigate"),
            group(27, "Battleship"),
            group(9_999, "Retired Hull", published=False),
            group(1_404, "Engineering Complex", category=65),
        ],
        fields=("name", "name_en", "published"),
        relation="category",
    )
    return FakeSDEApps(ItemType=fake_sde().models["ItemType"], ItemGroup=groups)


class BridgeSetUp(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = f.create_user("bridge-admin", perms=CONFIG_PERMS)

    def setUp(self):
        self.client.force_login(self.admin)
        self.url = reverse("fleetops:configuration_settings")
        self.addCleanup(cache.clear)

    def with_sde(self):
        patcher = mock.patch.object(sde_module, "apps", sde_with_ship_groups())
        patcher.start()
        self.addCleanup(patcher.stop)
        cache.clear()

    def post(self, **values):
        return self.client.post(self.url, settings_payload(**values))

    def stored(self):
        return FleetOpsSettings.get_solo().bridge_excluded_group_ids

    def messages(self, response):
        return [str(message) for message in get_messages(response.wsgi_request)]


class BridgeChoiceTests(BridgeSetUp):
    def test_with_the_sde_only_published_ship_groups_are_offered_sorted_by_name(self):
        self.with_sde()

        choices = composition_config.ship_group_choices()

        self.assertEqual(choices, [(27, "Battleship (27)"), (25, "Frigate (25)"), (900, "Marauder (900)")])

    def test_the_form_offers_the_same_choices_with_the_label_name_and_id(self):
        self.with_sde()

        form = FleetOpsSettingsForm(instance=f.settings())

        self.assertEqual(
            list(form.fields["bridge_groups"].choices),
            [(27, "Battleship (27)"), (25, "Frigate (25)"), (900, "Marauder (900)")],
        )

    def test_without_the_sde_the_built_in_list_is_offered_sorted_by_name(self):
        choices = composition_config.ship_group_choices()

        self.assertEqual(len(choices), len(KNOWN_SHIP_GROUPS))
        self.assertEqual(choices[0], (324, "Assault Frigate (324)"))
        self.assertEqual([label for _id, label in choices], sorted((label for _id, label in choices), key=str.casefold))
        self.assertLessEqual(set(DEFAULT_IDS), {group_id for group_id, _label in choices})

    def test_an_sde_without_ship_groups_falls_back_to_the_built_in_list(self):
        empty = FakeSDEApps(ItemType=fake_sde().models["ItemType"], ItemGroup=FakeModel([], fields=("name", "name_en"), relation="category"))
        with mock.patch.object(sde_module, "apps", empty):
            choices = composition_config.ship_group_choices()

        self.assertEqual(len(choices), len(KNOWN_SHIP_GROUPS))

    def test_an_sde_that_cannot_be_read_falls_back_to_the_built_in_list(self):
        with mock.patch.object(sde_module, "apps", FakeSDEApps(error=LookupError("no such model"))):
            self.assertIsNone(sde_module.ship_groups())
            self.assertEqual(len(composition_config.ship_group_choices()), len(KNOWN_SHIP_GROUPS))

    def test_the_ship_groups_are_cached(self):
        self.with_sde()
        sde_module.ship_groups()

        with mock.patch.object(sde_module.apps, "get_model", side_effect=AssertionError("read again")):
            self.assertEqual(len(sde_module.ship_groups()), 3)

    def test_the_default_list_is_ticked_on_a_fresh_installation(self):
        form = FleetOpsSettingsForm(instance=f.settings())

        self.assertEqual(form.initial["bridge_groups"], DEFAULT_IDS)
        self.assertEqual(form.initial["bridge_other_ids"], "")


class BridgeSaveTests(BridgeSetUp):
    def test_ticked_groups_are_stored_as_a_comma_list(self):
        response = self.post(bridge_groups=["27", "25"])

        self.assertRedirects(response, reverse("fleetops:configuration_index"), fetch_redirect_response=False)
        self.assertEqual(self.stored(), "27,25")
        entry = AuditLog.objects.filter(action="configuration.update", object_type="FleetOpsSettings").latest("pk")
        self.assertEqual(entry.old_value["bridge_excluded_group_ids"], DEFAULT_BRIDGE_EXCLUDED_GROUP_IDS)
        self.assertEqual(entry.new_value["bridge_excluded_group_ids"], "27,25")

    def test_nothing_ticked_stores_an_empty_list(self):
        self.post(bridge_groups=[])

        self.assertEqual(self.stored(), "")
        self.assertEqual(FleetOpsSettings.get_solo().bridge_excluded_groups, set())

    def test_other_ids_are_added_after_the_ticked_groups_without_duplicates(self):
        self.post(bridge_groups=["30", "485"], bridge_other_ids="485, 99999; 88888")

        self.assertEqual(self.stored(), "30,485,99999,88888")

    def test_the_default_list_button_replaces_the_selection(self):
        self.post(bridge_groups=["25"], bridge_other_ids="99999", bridge_restore_default="on")

        self.assertEqual(self.stored(), DEFAULT_BRIDGE_EXCLUDED_GROUP_IDS)

    def test_a_script_that_posts_none_of_the_three_fields_leaves_the_list_alone(self):
        FleetOpsSettings.objects.filter(pk=1).update(bridge_excluded_group_ids="30,485")
        payload = settings_payload()
        for name in ("bridge_groups", "bridge_other_ids"):
            payload.pop(name)

        response = self.client.post(self.url, payload)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.stored(), "30,485")

    def test_ids_that_are_no_numbers_are_refused(self):
        response = self.post(bridge_other_ids="abc")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "&quot;abc&quot; is not a valid ID. Separate IDs with commas.")
        self.assertEqual(self.stored(), DEFAULT_BRIDGE_EXCLUDED_GROUP_IDS)

    def test_with_the_sde_ids_that_are_no_ship_groups_are_refused_by_name(self):
        self.with_sde()

        response = self.post(bridge_groups=["27"], bridge_other_ids="1404, 9999")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Not ship groups: 1404, 9999.")
        self.assertEqual(self.stored(), DEFAULT_BRIDGE_EXCLUDED_GROUP_IDS)

    def test_with_the_sde_an_offered_group_typed_by_hand_is_fine(self):
        self.with_sde()

        response = self.post(bridge_groups=[], bridge_other_ids="900")

        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.stored(), "900")

    def test_without_the_sde_unknown_ids_are_saved_with_a_warning(self):
        response = self.post(bridge_groups=["30"], bridge_other_ids="99999, 88888")

        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.stored(), "30,99999,88888")
        warnings = [text for text in self.messages(response) if "could not be checked" in text]
        self.assertEqual(
            warnings,
            ["The ship group IDs 99999, 88888 were saved, but could not be checked without django-eveonline-sde."],
        )

    def test_known_ids_typed_by_hand_need_no_warning_without_the_sde(self):
        response = self.post(bridge_groups=[], bridge_other_ids="30")

        self.assertFalse([text for text in self.messages(response) if "could not be checked" in text])
        self.assertEqual(self.stored(), "30")

    def test_a_list_longer_than_the_column_is_refused(self):
        # 489 characters in the field itself, and the ticked default groups come on top of them.
        many = ",".join(str(100_000 + number) for number in range(70))

        response = self.post(bridge_other_ids=many)

        self.assertEqual(response.status_code, 200)
        self.assertIn("bridge_groups", response.context["form"].errors)
        self.assertEqual(self.stored(), DEFAULT_BRIDGE_EXCLUDED_GROUP_IDS)

    def test_a_group_that_is_not_in_the_list_cannot_be_ticked(self):
        response = self.post(bridge_groups=["99999"])

        self.assertEqual(response.status_code, 200)
        self.assertIn("bridge_groups", response.context["form"].errors)

    def test_a_stored_id_outside_the_list_comes_back_in_the_other_ids_field(self):
        FleetOpsSettings.objects.filter(pk=1).update(bridge_excluded_group_ids="30,99999,485")

        form = FleetOpsSettingsForm(instance=FleetOpsSettings.get_solo())

        self.assertEqual(form.initial["bridge_groups"], [30, 485])
        self.assertEqual(form.initial["bridge_other_ids"], "99999")
        # Saving the page as it comes does not lose the ID.
        response = self.client.post(
            self.url, settings_payload(bridge_groups=["30", "485"], bridge_other_ids="99999")
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.stored(), "30,485,99999")

    def test_a_stored_list_nobody_can_read_does_not_break_the_page(self):
        FleetOpsSettings.objects.filter(pk=1).update(bridge_excluded_group_ids="30,x")

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["form"].initial["bridge_groups"], [])


class BridgePageTests(BridgeSetUp):
    def get(self):
        return self.client.get(self.url).content.decode()

    def test_the_page_has_the_filter_the_checklist_and_the_other_fields(self):
        html = self.get()

        self.assertIn('id="bridge-groups-filter"', html)
        self.assertIn('data-fo-filter="bridge-groups-list"', html)
        self.assertIn('class="fo-checklist" id="bridge-groups-list"', html)
        self.assertEqual(html.count("data-fo-filter-item="), len(KNOWN_SHIP_GROUPS))
        self.assertIn('data-fo-filter-item="Titan (30)"', html)
        self.assertIn('name="bridge_other_ids"', html)
        self.assertIn('name="bridge_restore_default"', html)
        self.assertIn("Use the default list (11 groups)", html)
        self.assertNotIn('name="bridge_excluded_group_ids"', html)

    def test_the_default_groups_are_ticked(self):
        html = self.get()

        for group_id in DEFAULT_IDS:
            self.assertRegex(html, rf'name="bridge_groups" value="{group_id}"[^>]*checked')
        self.assertNotRegex(html, r'name="bridge_groups" value="25"[^>]*checked')

    def test_every_group_in_force_links_to_its_page_on_eve_ref(self):
        html = self.get()
        now = html.split('id="bridge-groups-now"', 1)[1].split("</div>", 1)[0]

        self.assertIn('<a href="https://everef.net/groups/30" target="_blank" rel="noopener">Titan (30)</a>', now)
        self.assertEqual(now.count("https://everef.net/groups/"), len(DEFAULT_IDS))

    def test_all_ship_groups_link_to_the_category_on_eve_ref(self):
        html = self.get()

        self.assertIn('<a href="https://everef.net/categories/6" target="_blank" rel="noopener">All ship groups on EVE Ref</a>', html)

    def test_an_empty_list_shows_a_dash_and_the_category_link(self):
        FleetOpsSettings.objects.filter(pk=1).update(bridge_excluded_group_ids="")

        now = self.get().split('id="bridge-groups-now"', 1)[1].split("</div>", 1)[0]

        self.assertNotIn("everef.net/groups/", now)
        self.assertIn("everef.net/categories/6", now)

    def test_with_the_sde_the_unpublished_group_is_not_listed(self):
        self.with_sde()

        html = self.get()

        self.assertIn('data-fo-filter-item="Marauder (900)"', html)
        self.assertNotIn("Retired Hull", html)
        self.assertNotIn("Engineering Complex", html)

    def test_the_old_help_text_with_the_current_groups_is_gone_from_the_field(self):
        form = FleetOpsSettingsForm(instance=f.settings())

        self.assertEqual(form.bridge_now[0], (30, "Titan"))
        self.assertNotIn("Now:", form.fields["bridge_groups"].help_text)


class FleetPositionColumnTests(TestCase):
    """FB15.3: the fleet positions of a ship function are listed with their names."""

    @classmethod
    def setUpTestData(cls):
        cls.admin = f.create_user("positions-admin", perms=CONFIG_PERMS)

    def setUp(self):
        self.client.force_login(self.admin)

    def test_the_list_shows_names_and_not_codes(self):
        ShipFunction.objects.filter(name="DPS").update(fleet_roles="fleet_commander,squad_commander")

        response = self.client.get(reverse("fleetops:configuration_list", args=["ship-functions"]))

        self.assertContains(response, "Fleet Commander, Squad Commander")
        self.assertNotContains(response, "fleet_commander")

    def test_the_list_is_in_chinese_on_a_chinese_page(self):
        ShipFunction.objects.filter(name="DPS").update(fleet_roles="fleet_commander,squad_commander")
        self.client.cookies["django_language"] = "zh-hans"

        response = self.client.get(reverse("fleetops:configuration_list", args=["ship-functions"]))

        self.assertContains(response, "舰队指挥官")
        self.assertContains(response, "中队指挥官")
        self.assertNotContains(response, "fleet_commander")

    def test_a_function_without_positions_shows_a_dash(self):
        response = self.client.get(reverse("fleetops:configuration_list", args=["ship-functions"]))
        row = next(row for row in response.context["rows"] if row["object"].name == "DPS")

        self.assertEqual(dict(row["values"])["Fleet positions"], "")

    def test_an_unreadable_stored_value_is_shown_as_it_is(self):
        ShipFunction.objects.filter(name="DPS").update(fleet_roles="boss")

        response = self.client.get(reverse("fleetops:configuration_list", args=["ship-functions"]))
        row = next(row for row in response.context["rows"] if row["object"].name == "DPS")

        self.assertEqual(dict(row["values"])["Fleet positions"], "boss")


class AuditObjectLabelTests(TestCase):
    """FB15.4 and R24: the Object column names what was changed, in the language of the viewer."""

    @classmethod
    def setUpTestData(cls):
        cls.auditor = f.create_user("object-auditor", perms=f.MEMBER_PERMS + ["fleetops.view_audit_log"])
        cls.fc = f.create_user("object-fc", perms=f.FC_PERMS)

    def setUp(self):
        # Earlier tests may leave a language active after a request; the filter calls below run outside one.
        english = translation.override("en")
        english.__enter__()
        self.addCleanup(english.__exit__, None, None, None)

    def label(self, object_type, object_id):
        return audit_object_label(AuditLog(object_type=object_type, object_id=str(object_id)))

    def test_a_model_is_named_by_its_verbose_name(self):
        self.assertEqual(self.label("FleetOperation", 77), "Fleet operation #77")
        self.assertEqual(self.label("FleetType", 3), "Fleet type #3")
        self.assertEqual(self.label("SrpClaim", 12), "SRP claim #12")
        self.assertEqual(self.label("DiscordWebhook", 4), "Ping channel #4")

    def test_the_three_group_objects_have_names(self):
        self.assertEqual(self.label("StandalonePing", 5), "Ping without fleet #5")
        self.assertEqual(self.label("GroupAccess", 2), "Group access #2")
        self.assertEqual(self.label("Group", 9), "Group #9")

    def test_exports_have_names(self):
        self.assertEqual(self.label("Statistics", "member-fleets"), "Statistics #member-fleets")
        self.assertEqual(self.label("SrpSummary", "claims"), "SRP Summary #claims")

    def test_something_unknown_is_shown_as_stored(self):
        self.assertEqual(self.label("SomethingElse", 1), "SomethingElse #1")
        self.assertEqual(self.label("Odd", ""), "Odd")
        self.assertEqual(self.label("", 3), " #3")

    def test_the_label_follows_the_viewer_language(self):
        with translation.override("zh-hans"):
            self.assertEqual(self.label("FleetOperation", 77), "舰队记录 #77")
            self.assertEqual(self.label("GroupAccess", 2), "小组权限 #2")
            self.assertEqual(self.label("StandalonePing", 5), "无舰队 Ping #5")
            self.assertEqual(self.label("Group", 9), "小组 #9")
            self.assertEqual(self.label("Statistics", "x"), "统计 #x")

    def test_the_page_shows_the_names_in_english_and_in_chinese(self):
        operation = f.create_operation(self.fc)
        audit(self.fc, "fleet.end", operation)
        audit_event(self.fc, "statistics.export", "Statistics", "member-fleets")
        AuditLog.objects.create(actor=None, action="group.deleted", object_type="Group", object_id="9")
        self.client.force_login(self.auditor)
        url = reverse("fleetops:audit_log")

        english = self.client.get(url)
        self.assertContains(english, f"Fleet operation #{operation.pk}")
        self.assertContains(english, "Statistics #member-fleets")
        self.assertContains(english, "Group #9")
        self.assertNotContains(english, "FleetOperation #")

        self.client.cookies["django_language"] = "zh-hans"
        chinese = self.client.get(url)
        self.assertContains(chinese, f"舰队记录 #{operation.pk}")
        self.assertContains(chinese, "统计 #member-fleets")
        self.assertContains(chinese, "小组 #9")

    def test_the_page_costs_one_query_for_the_rows_whatever_the_number_of_entries(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        self.client.force_login(self.auditor)
        url = reverse("fleetops:audit_log")
        operation = f.create_operation(self.fc)

        def cost():
            with CaptureQueriesContext(connection) as queries:
                self.client.get(url)
            return len(queries)

        audit(self.fc, "fleet.end", operation)
        cost()
        small = cost()
        for _ in range(30):
            audit(self.fc, "fleet.end", operation)

        self.assertEqual(cost(), small)


class ListAndFormLookTests(TestCase):
    """The configuration lists and forms: pills, the shared confirmation, no script of their own, empty states."""

    @classmethod
    def setUpTestData(cls):
        cls.admin = f.create_user("look-admin", perms=CONFIG_PERMS)

    def setUp(self):
        self.client.force_login(self.admin)

    def list_html(self, section):
        return self.client.get(reverse("fleetops:configuration_list", args=[section])).content.decode()

    def test_yes_and_no_are_pills(self):
        f.fleet_type("Roam", "1.00")
        FleetOpsSettings.get_solo()

        html = self.list_html("fleet-types")

        self.assertIn('<span class="fo-pill fo-pill-ok">Yes</span>', html)
        self.assertNotIn('class="badge', html)

    def test_a_switched_off_row_is_a_neutral_no(self):
        fleet_type = f.fleet_type("Roam", "1.00")
        fleet_type.is_active = False
        fleet_type.save()

        self.assertIn('<span class="fo-pill fo-pill-neutral">No</span>', self.list_html("fleet-types"))

    def test_delete_asks_in_the_shared_dialog_and_not_with_an_inline_handler(self):
        f.fleet_type("Roam", "1.00")

        html = self.list_html("fleet-types")

        self.assertIn('data-fo-confirm="Delete this item? Existing fleet history may prevent deletion."', html)
        self.assertIn('data-fo-confirm-label="Delete"', html)
        self.assertNotIn("onsubmit", html)
        self.assertNotIn("confirm(", html)

    def test_copy_fields_use_the_shared_copy_button_and_the_page_has_no_script_of_its_own(self):
        CommsPreset.objects.create(name="Ops", voice_url="https://discord.com/channels/1/2", is_active=True)

        html = self.list_html("comms")

        self.assertNotIn("data-fleetops-copy", html)
        self.assertNotIn("navigator.clipboard", html)
        if "fo-copy-field" in html:
            self.assertRegex(html, r'data-fo-copy="#fo-copy-comms-\d+"')

    def test_figures_are_right_aligned_and_names_are_not(self):
        f.fleet_type("Roam", "2.50")

        html = self.list_html("fleet-types")

        self.assertIn('<th scope="col" class="text-end">Point weight</th>', html)
        self.assertIn('<th scope="col">Name</th>', html)
        self.assertIn('<td class="text-end">2.50</td>', html)

    def test_every_list_has_an_empty_state_naming_its_section(self):
        for section in ("fleet-types", "comms", "ping-channels", "groups", "structure-templates"):
            with self.subTest(section=section):
                if section == "structure-templates":
                    from fleetops.models import FleetStructureTemplate

                    FleetStructureTemplate.objects.all().delete()
                response = self.client.get(reverse("fleetops:configuration_list", args=[section]))
                title = response.context["config"]["title"]
                if response.context["rows"]:
                    continue
                self.assertContains(response, f"No {title} yet.")
                self.assertContains(response, 'class="fo-empty"')
                self.assertContains(response, reverse("fleetops:configuration_add", args=[section]))

    def test_a_list_with_rows_has_no_empty_state(self):
        f.fleet_type("Roam", "1.00")

        self.assertNotIn("fo-empty", self.list_html("fleet-types"))

    def test_the_main_action_of_a_list_is_the_one_solid_button(self):
        f.fleet_type("Roam", "1.00")

        html = self.list_html("fleet-types").split("<h2", 1)[1]

        self.assertEqual(html.count("btn btn-primary"), 1)
        self.assertNotIn("btn-success", html)
        self.assertNotIn("btn-warning", html)

    def test_the_form_keeps_one_solid_button_and_limits_its_width(self):
        html = self.client.get(reverse("fleetops:configuration_add", args=["fleet-types"])).content.decode()

        self.assertEqual(html.split("<h2", 1)[1].count("btn btn-primary"), 1)
        self.assertIn('class="card fo-form-card"', html)

    def test_a_form_with_a_side_card_is_not_narrowed(self):
        group = f.aa_group("Example Group")
        access = f.group_access(group, GroupAccess.Mode.PING)

        html = self.client.get(reverse("fleetops:configuration_edit", args=["groups", access.pk])).content.decode()

        self.assertNotIn("fo-form-card", html)

    def test_the_ping_channel_list_keeps_its_group_columns(self):
        group = f.aa_group("Example Group")
        f.group_channel("Group Pings", groups=[group])

        response = self.client.get(reverse("fleetops:configuration_list", args=["ping-channels"]))

        labels = [label for label, _value in response.context["rows"][0]["values"]]
        self.assertIn("Groups", labels)
        self.assertIn("FCs", labels)
        self.assertContains(response, "Example Group")

    def test_the_ping_channel_form_keeps_the_group_fields(self):
        f.group_access(f.aa_group("Example Group"), GroupAccess.Mode.PING)
        response = self.client.get(reverse("fleetops:configuration_add", args=["ping-channels"]))

        self.assertContains(response, 'name="allowed_groups"')
        self.assertContains(response, 'name="fcs_allowed"')

    def test_the_page_level_error_of_a_form_is_one_readable_callout(self):
        f.payout_rule(587, 1_000_000)
        with mock.patch.object(forms_config.sde_service, "item_type_names", return_value={587: "Rifter"}):
            response = self.client.post(
                reverse("fleetops:configuration_add", args=["srp-rules"]),
                {"label": "", "ship": "587", "doctrine_choice": "", "doctrine_custom": "", "scope": "any",
                 "amount": "1m", "is_active": "on", "notes": ""},
            )

        self.assertContains(response, '<div class="alert alert-danger" role="alert">')
        self.assertNotContains(response, 'class="alert alert-danger"><ul class="errorlist')

    def test_the_audit_page_has_an_empty_state_and_plain_text_paging(self):
        auditor = f.create_user("empty-audit", perms=f.MEMBER_PERMS + ["fleetops.view_audit_log"])
        self.client.force_login(auditor)

        html = self.client.get(reverse("fleetops:audit_log")).content.decode()

        self.assertIn("No audit entries.", html)
        self.assertIn('class="fo-empty"', html)
        self.assertNotIn("<table", html.split("<h2", 1)[1])
