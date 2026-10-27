# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Configuration of the Fleet tab: ship functions and rules, structure templates, settings and quick classify."""

from types import SimpleNamespace
from unittest import mock

from django.contrib.messages import get_messages
from django.core.cache import cache
from django.core.exceptions import FieldError
from django.db import IntegrityError
from django.template.loader import render_to_string
from django.test import TestCase
from django.urls import reverse

from fleetops.forms_fleet import NEEDS_SDE, SDE_UNREADABLE, FleetStructureTemplateForm, ShipFunctionRuleForm
from fleetops.models import AuditLog, FleetOpsSettings, FleetStructureTemplate, ShipFunction, ShipFunctionRule
from fleetops.services import composition_config
from fleetops.services import sde as sde_module
from fleetops.services.composition import Row
from fleetops.services.fleet_rules import DEFAULT_BRIDGE_EXCLUDED_GROUP_IDS, ShipFunctionMap

from . import factories as f
from .test_configuration_audit import settings_payload

SHIP = 6
GUARDIAN, RIFTER, RAVEN = 11987, 587, 638


class FakeRows(list):
    """Query results with the queryset methods FleetOps uses on eve_sde models."""

    def __init__(self, model, rows):
        super().__init__(rows)
        self.model = model

    def filter(self, **lookups):
        return self.model.select(self, lookups)

    def order_by(self, *fields):
        rows = list(self)
        for name in reversed(fields):
            rows.sort(key=lambda row: self.model.value(row, name.lstrip("-")) or 0, reverse=name.startswith("-"))
        return FakeRows(self.model, rows)

    def values_list(self, *fields, flat=False):
        values = [tuple(self.model.value(row, name) for name in fields) for row in self]
        return FakeRows(self.model, [value[0] for value in values] if flat else values)

    def first(self):
        return self[0] if self else None


class FakeModel:
    """An eve_sde model for the lookups FleetOps makes: columns, ``filter()`` and ``values_list()``."""

    def __init__(self, rows, fields=("name", "name_en"), relation="group"):
        self.rows = rows
        self.columns = {"id", relation, *fields}
        self.fields = {"pk", "category_id", "group", *fields}
        self.objects = self
        self._meta = SimpleNamespace(get_fields=lambda: [SimpleNamespace(name=name) for name in self.columns])

    def filter(self, **lookups):
        return self.select(self.rows, lookups)

    def select(self, rows, lookups):
        for key in lookups:
            if key.split("__")[0] not in self.fields:
                raise FieldError(f"Cannot resolve keyword {key!r}")
        return FakeRows(self, [row for row in rows if all(self._match(row, key, value) for key, value in lookups.items())])

    @staticmethod
    def value(row, name):
        if name in ("pk", "id"):
            return row.pk
        if name == "group_id":
            return getattr(getattr(row, "group", None), "pk", None)
        return getattr(row, name, None)

    @staticmethod
    def _match(row, key, value):
        if key == "pk__in":
            return row.pk in set(value)
        if key.endswith("__iexact"):
            return str(getattr(row, key[: -len("__iexact")], "") or "").lower() == str(value).lower()
        if key == "category_id":
            return row.category_id == value
        if key == "published":
            return getattr(row, "published", True) == value
        if key == "group__category_id":
            return row.group.category_id == value
        raise FieldError(key)


class FakeSDEApps:
    def __init__(self, error=None, **models):
        self.models = models
        self.error = error

    def is_installed(self, name):
        return name == "eve_sde"

    def get_model(self, app_label, model_name):
        if self.error is not None:
            raise self.error
        return self.models[model_name]


def fake_sde(fields=("name", "name_en")):
    logistics, frigate = SimpleNamespace(pk=832, category_id=SHIP), SimpleNamespace(pk=25, category_id=SHIP)
    battleship, structure = SimpleNamespace(pk=27, category_id=SHIP), SimpleNamespace(pk=1_404, category_id=65)

    def item(pk, name, group, name_en=None):
        row = SimpleNamespace(pk=pk, name=f"{name} (local)" if "name_en" in fields else name, group=group)
        if "name_en" in fields:
            row.name_en = name_en or name
        return row

    types = FakeModel(
        [item(GUARDIAN, "Guardian", logistics), item(RIFTER, "Rifter", frigate), item(RAVEN, "Raven", battleship),
         item(99_001, "Raven", structure)],
        fields,
    )
    groups = FakeModel(
        [SimpleNamespace(pk=900, name="Marauder", name_en="Marauder", category_id=SHIP),
         SimpleNamespace(pk=25, name="Frigate", name_en="Frigate", category_id=SHIP),
         SimpleNamespace(pk=1_404, name="Engineering Complex", name_en="Engineering Complex", category_id=65)],
        relation="category",
    )
    return FakeSDEApps(ItemType=types, ItemGroup=groups)


class ConfigTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.lead = f.create_user("config-lead", perms=f.FC_LEAD_PERMS)
        cls.fc = f.create_user("config-fc", perms=f.FC_PERMS)
        cls.logistics = ShipFunction.objects.get(name="Logistics")
        cls.dps = ShipFunction.objects.get(name="DPS")

    def setUp(self):
        super().setUp()
        self.client.force_login(self.lead)

    def with_sde(self, apps=None):
        patcher = mock.patch.object(sde_module, "apps", apps or fake_sde())
        patcher.start()
        self.addCleanup(patcher.stop)
        # Ship type details are cached; other tests must not see this SDE's.
        self.addCleanup(cache.clear)

    def rule_form(self, target, kind="group", label="", instance=None, function=None):
        data = {"function": str((function or self.logistics).pk), "kind": kind, "target": target, "label": label}
        return ShipFunctionRuleForm(data, instance=instance)

    def add(self, section, data):
        return self.client.post(reverse("fleetops:configuration_add", args=[section]), data)


class ShipFunctionTests(ConfigTestCase):
    def post(self, **values):
        data = {"name": "Cyno Alts", "color": "#ffc107", "sort_order": "95", "is_active": "on", **values}
        return self.add("ship-functions", data)

    def test_positions_are_stored_in_fleet_order(self):
        response = self.post(fleet_roles=["squad_commander", "fleet_commander"])

        self.assertRedirects(response, reverse("fleetops:configuration_list", args=["ship-functions"]))
        function = ShipFunction.objects.get(name="Cyno Alts")
        self.assertEqual(function.fleet_roles, "fleet_commander,squad_commander")
        entry = AuditLog.objects.get(action="configuration.create", object_type="ShipFunction")
        self.assertEqual(entry.new_value["fleet_roles"], "fleet_commander,squad_commander")

        edit = self.client.get(reverse("fleetops:configuration_edit", args=["ship-functions", function.pk]))
        self.assertContains(edit, 'value="fleet_commander" class="form-check-input" id="id_fleet_roles_0" checked')
        self.assertContains(edit, 'value="squad_commander" class="form-check-input" id="id_fleet_roles_2" checked')
        self.assertNotContains(edit, 'value="wing_commander" class="form-check-input" id="id_fleet_roles_1" checked')

    def test_a_new_default_replaces_the_old_one(self):
        response = self.post(is_default="on")

        self.assertEqual(
            [str(message) for message in get_messages(response.wsgi_request)],
            ["Ship Functions saved.", "“DPS” is no longer the default function."],
        )
        self.assertEqual(list(ShipFunction.objects.filter(is_default=True).values_list("name", flat=True)), ["Cyno Alts"])
        demoted = AuditLog.objects.get(action="configuration.update", object_type="ShipFunction", object_id=str(self.dps.pk))
        self.assertEqual((demoted.old_value["is_default"], demoted.new_value["is_default"]), (True, False))
        self.assertEqual(demoted.reason, "Default ship function replaced from FleetOps front-end administration")

    def test_the_default_must_be_active(self):
        response = self.post(is_default="on", is_active="")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "The default function must be active.")
        self.assertFalse(ShipFunction.objects.filter(name="Cyno Alts").exists())

    def test_unknown_positions_and_colours_are_refused(self):
        response = self.post(fleet_roles=["boss"], color="red")

        self.assertEqual(response.status_code, 200)
        form = response.context["form"]
        self.assertIn("fleet_roles", form.errors)
        self.assertIn("color", form.errors)

    def test_deleting_a_function_deletes_its_rules(self):
        rules = {str(rule.pk): (rule.kind, rule.target_id, rule.label) for rule in self.logistics.rules.all()}

        self.client.post(reverse("fleetops:configuration_delete", args=["ship-functions", self.logistics.pk]))

        self.assertFalse(ShipFunctionRule.objects.filter(pk__in=rules).exists())
        entry = AuditLog.objects.get(action="configuration.delete", object_type="ShipFunction")
        self.assertEqual(entry.old_value["name"], "Logistics")
        # Each rule deleted with it keeps its own entry: what was mapped can still be read and restored.
        entries = AuditLog.objects.filter(action="configuration.delete", object_type="ShipFunctionRule")
        self.assertEqual(
            {e.object_id: (e.old_value["kind"], e.old_value["target_id"], e.old_value["label"]) for e in entries}, rules
        )
        self.assertTrue(all(e.old_value["function"] == self.logistics.pk and e.actor == self.lead for e in entries))


class RuleFormTests(ConfigTestCase):
    def test_ids_without_the_sde(self):
        form = self.rule_form("898", label="ignored")
        self.assertTrue(form.is_valid(), form.errors)
        rule = form.save()
        self.assertEqual((rule.target_id, rule.label), (898, "Black Ops"))

        form = self.rule_form("12345", kind="type", label="My Hull")
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual((form.instance.target_id, form.cleaned_data["label"]), (12345, "My Hull"))

        form = self.rule_form("12346", kind="type")
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["label"], "Type 12346")

    def test_type_ids_take_the_name_tracked_members_flew(self):
        operation = f.create_operation(self.lead)
        f.member_state(operation, f.main_of(self.fc), ship_type_id=GUARDIAN, ship_type_name="Guardian")

        form = self.rule_form(str(GUARDIAN), kind="type")

        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["label"], "Guardian")

    def test_names_without_the_sde(self):
        form = self.rule_form("  marauder ")
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual((form.instance.target_id, form.cleaned_data["label"]), (900, "Marauder"))

        form = self.rule_form("Guardian", kind="type")
        self.assertFalse(form.is_valid())
        self.assertEqual(form.errors["target"], [NEEDS_SDE])

        form = self.rule_form("Space Yacht")
        self.assertEqual(
            form.errors["target"], ["No ship group is called “Space Yacht”. Enter the exact English name or the ID."]
        )

    def test_names_and_ids_with_the_sde(self):
        self.with_sde()

        guardian = self.rule_form("guardian", kind="type")
        self.assertTrue(guardian.is_valid(), guardian.errors)
        self.assertEqual((guardian.instance.target_id, guardian.cleaned_data["label"]), (GUARDIAN, "Guardian"))
        # Only ships count: the structure called Raven is not one.
        raven = self.rule_form("RAVEN", kind="type")
        self.assertTrue(raven.is_valid(), raven.errors)
        self.assertEqual(raven.instance.target_id, RAVEN)
        # The English name wins over the label typed in, and over the translated name.
        rifter = self.rule_form(str(RIFTER), kind="type", label="Frig")
        self.assertTrue(rifter.is_valid(), rifter.errors)
        self.assertEqual(rifter.cleaned_data["label"], "Rifter")
        frigate = self.rule_form("frigate")
        self.assertTrue(frigate.is_valid(), frigate.errors)
        self.assertEqual(frigate.instance.target_id, 25)
        self.assertFalse(self.rule_form("Engineering Complex").is_valid())

    def test_sde_without_translated_columns(self):
        self.with_sde(fake_sde(fields=("name",)))

        form = self.rule_form("Guardian", kind="type")

        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual((form.instance.target_id, form.cleaned_data["label"]), (GUARDIAN, "Guardian"))

    def test_unreadable_sde(self):
        self.with_sde(FakeSDEApps(error=LookupError("no such table")))

        form = self.rule_form("Guardian", kind="type")
        self.assertEqual(form.errors["target"], [SDE_UNREADABLE])
        # Ship groups still come from the built-in list.
        self.assertTrue(self.rule_form("Black Ops").is_valid())

    def test_duplicates_name_the_function_they_belong_to(self):
        for target in ("832", "Logistics"):
            with self.subTest(target=target):
                form = self.rule_form(target, function=self.dps)
                self.assertFalse(form.is_valid())
                self.assertEqual(form.errors["target"], ["Ship group 832 (Logistics) is already mapped to Logistics."])

    def test_editing_a_rule(self):
        rule = ShipFunctionRule.objects.get(kind="group", target_id=832)
        form = ShipFunctionRuleForm(instance=rule)
        self.assertEqual(form.initial["target"], "832")

        form = self.rule_form("832", instance=rule, function=self.dps)
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        rule.refresh_from_db()
        self.assertEqual((rule.function, rule.label), (self.dps, "Logistics"))

    def test_invalid_ids(self):
        for target in ("0", "9223372036854775808"):
            with self.subTest(target=target):
                self.assertEqual(self.rule_form(target).errors["target"], ["Enter a valid ID."])

    def test_rules_from_the_configuration_page(self):
        self.with_sde()

        response = self.add(
            "ship-function-rules",
            {"function": str(self.logistics.pk), "kind": "type", "target": "Guardian", "label": ""},
        )

        self.assertRedirects(response, reverse("fleetops:configuration_list", args=["ship-function-rules"]))
        rule = ShipFunctionRule.objects.get(kind="type", target_id=GUARDIAN)
        entry = AuditLog.objects.get(action="configuration.create", object_type="ShipFunctionRule")
        self.assertEqual(
            {key: entry.new_value[key] for key in ("function", "kind", "target_id", "label")},
            {"function": self.logistics.pk, "kind": "type", "target_id": GUARDIAN, "label": "Guardian"},
        )
        self.assertEqual(entry.object_id, str(rule.pk))
        listing = self.client.get(reverse("fleetops:configuration_list", args=["ship-function-rules"]))
        self.assertContains(listing, "Ship type")


class StructureTemplateFormTests(ConfigTestCase):
    def form(self, layout):
        return FleetStructureTemplateForm({"name": "Test", "layout": layout, "sort_order": "0", "is_active": "on"})

    def test_layout_errors_name_the_line(self):
        cases = (
            ("Main: Alpha\nLogi", 'Line 2: wing "Logi" needs at least one squad, for example "Logi: Squad 1".'),
            ("# comment\n\nMain: Alpha, Logistics 1", 'Line 3: squad name "Logistics 1" is longer than 10 characters.'),
            ("\n".join(f"W{number}: S" for number in range(6)), "Line 6: EVE allows at most 5 wings."),
            ("Main: A, B, C, D, E, F", 'Line 1: wing "Main" has 6 squads; EVE allows at most 5 per wing.'),
            ("# only a comment", 'Add at least one wing, for example "Main: Alpha, Bravo".'),
        )
        for layout, message in cases:
            with self.subTest(layout=layout):
                self.assertEqual(self.form(layout).errors["layout"], [message])
        self.assertTrue(self.form("Main: Alpha, Bravo\n# later\nLogi: Logi").is_valid())

    def test_the_form_page_previews_the_layout(self):
        template = FleetStructureTemplate.objects.get(name="Standard")

        page = self.client.get(reverse("fleetops:configuration_edit", args=["structure-templates", template.pk]))

        self.assertContains(page, 'data-structure-layout="1"')
        self.assertContains(page, "structure-layout-preview")
        other = self.client.get(reverse("fleetops:configuration_add", args=["fleet-types"]))
        self.assertNotContains(other, "structure-layout-preview")

    def test_templates_from_the_configuration_page_are_audited(self):
        response = self.add(
            "structure-templates",
            {"name": "Small", "layout": "Main: Alpha", "description": "", "is_active": "on", "sort_order": "5"},
        )

        self.assertEqual(response.status_code, 302)
        entry = AuditLog.objects.get(action="configuration.create", object_type="FleetStructureTemplate")
        self.assertEqual(entry.new_value["layout"], "Main: Alpha")

        invalid = self.add("structure-templates", {"name": "Broken", "layout": "Main", "sort_order": "0"})
        self.assertEqual(invalid.status_code, 200)
        self.assertContains(invalid, "needs at least one squad")


class SettingsTests(ConfigTestCase):
    def post(self, **values):
        return self.client.post(reverse("fleetops:configuration_settings"), settings_payload(**values))

    def test_live_refresh_is_off_or_at_least_ten_seconds(self):
        for value, ok in (("0", True), ("10", True), ("300", True), ("5", False), ("301", False)):
            with self.subTest(value=value):
                response = self.post(fleet_live_refresh_seconds=value)
                self.assertEqual(response.status_code, 302 if ok else 200)
                if ok:
                    self.assertEqual(FleetOpsSettings.get_solo().fleet_live_refresh_seconds, int(value))
        response = self.post(fleet_live_refresh_seconds="5")
        self.assertContains(response, "Use 0 to turn the live refresh off, or at least 10 seconds.")

    def test_bridge_exclusions_are_ship_group_ids(self):
        response = self.post(bridge_groups=["30"], bridge_other_ids="abc")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "&quot;abc&quot; is not a valid ID. Separate IDs with commas.")

        self.post(bridge_groups=["30"], bridge_other_ids="485; 4594")
        settings = FleetOpsSettings.get_solo()
        self.assertEqual(settings.bridge_excluded_groups, {30, 485, 4594})
        entry = AuditLog.objects.filter(action="configuration.update", object_type="FleetOpsSettings").latest("pk")
        self.assertEqual(entry.new_value["bridge_excluded_group_ids"], "30,485,4594")

    def test_bridge_exclusions_by_name(self):
        # Q4: the titan bridge rule leaves out capitals, supercapitals, Rorquals, freighters, jump freighters and
        # industrial command ships.
        self.assertEqual(
            composition_config.describe_group_ids(DEFAULT_BRIDGE_EXCLUDED_GROUP_IDS),
            "Titan (30), Dreadnought (485), Freighter (513), Carrier (547), Supercarrier (659), Capital Industrial "
            "Ship (883), Jump Freighter (902), Industrial Command Ship (941), Force Auxiliary (1538), Lancer "
            "Dreadnought (4594), Command Carrier (5120)",
        )
        self.assertEqual(composition_config.describe_group_ids("30, 99999"), "Titan (30), Group 99999 (99999)")
        self.assertEqual(composition_config.describe_group_ids("abc"), "")
        self.with_sde()
        self.assertEqual(composition_config.describe_group_ids("900 1404"), "Marauder (900), Engineering Complex (1404)")


class QuickClassifyTests(ConfigTestCase):
    def setUp(self):
        super().setUp()
        self.operation = f.create_operation(self.fc)
        f.member_state(self.operation, f.main_of(self.fc), ship_type_id=GUARDIAN, ship_type_name="Guardian")
        self.url = reverse("fleetops:fleet_classify", args=[self.operation.uuid])

    def classify(self, type_id=GUARDIAN, function=None):
        return composition_config.classify_type(type_id, (function or self.logistics).pk, actor=self.lead)

    def resolved(self, type_id=GUARDIAN):
        mapping = ShipFunctionMap.build(ShipFunction.objects.all(), ShipFunctionRule.objects.all())
        return mapping.resolve(ship_type_id=type_id, group_id=830, category_id=SHIP)

    def test_a_new_rule(self):
        result = self.classify()

        self.assertTrue(result.ok)
        self.assertEqual(result.message, "Guardian now counts as Logistics.")
        self.assertEqual(result.data, {"type_id": GUARDIAN, "function_id": self.logistics.pk})
        rule = ShipFunctionRule.objects.get(kind="type", target_id=GUARDIAN)
        self.assertEqual((rule.function, rule.label), (self.logistics, "Guardian"))
        entry = AuditLog.objects.get(action="configuration.create", object_type="ShipFunctionRule")
        self.assertEqual((entry.actor, entry.reason), (self.lead, "Classified from a fleet's composition"))
        self.assertIsNone(entry.old_value)
        # The type rule wins over the group rule (Covert Ops counts as Support).
        self.assertEqual(self.resolved(), self.logistics)

    def test_moving_a_type_to_another_function(self):
        self.classify()

        result = self.classify(function=self.dps)

        self.assertEqual(result.message, "Guardian now counts as DPS.")
        entry = AuditLog.objects.get(action="configuration.update", object_type="ShipFunctionRule")
        self.assertEqual((entry.old_value["function"], entry.new_value["function"]), (self.logistics.pk, self.dps.pk))
        self.assertEqual(self.resolved(), self.dps)
        self.assertEqual(ShipFunctionRule.objects.filter(kind="type", target_id=GUARDIAN).count(), 1)

    def test_classifying_again_changes_nothing(self):
        self.classify()

        result = self.classify()

        self.assertEqual((result.ok, result.level), (True, "info"))
        self.assertEqual(result.message, "Guardian already counts as Logistics.")
        self.assertEqual(AuditLog.objects.filter(object_type="ShipFunctionRule").count(), 1)

    def test_names_come_from_the_sde_first(self):
        self.with_sde()

        self.classify(RIFTER)

        self.assertEqual(ShipFunctionRule.objects.get(kind="type", target_id=RIFTER).label, "Rifter")
        unknown = composition_config.classify_type(123, self.logistics.pk, actor=self.lead)
        self.assertEqual(unknown.message, "Type 123 now counts as Logistics.")

    def test_refusals(self):
        self.logistics.is_active = False
        self.logistics.save()
        for type_id, function_id, status in ((GUARDIAN, self.logistics.pk, 404), (GUARDIAN, 999_999, 404),
                                             (0, self.dps.pk, 400)):
            with self.subTest(type_id=type_id, function_id=function_id):
                result = composition_config.classify_type(type_id, function_id, actor=self.lead)
                self.assertEqual((result.ok, result.status), (False, status))
        self.assertFalse(ShipFunctionRule.objects.filter(kind="type", target_id=GUARDIAN).exists())

    def test_a_rule_created_at_the_same_moment(self):
        real = composition_config._save_type_rule
        calls = []

        def racing(type_id, function, label):
            calls.append(type_id)
            if len(calls) == 1:
                ShipFunctionRule.objects.create(function=self.dps, kind="type", target_id=type_id, label=label)
                raise IntegrityError("duplicate key")
            return real(type_id, function, label)

        with mock.patch.object(composition_config, "_save_type_rule", side_effect=racing):
            result = self.classify()

        self.assertEqual(result.message, "Guardian now counts as Logistics.")
        self.assertEqual(ShipFunctionRule.objects.get(kind="type", target_id=GUARDIAN).function, self.logistics)

    def test_from_the_composition_panel(self):
        response = self.client.post(
            self.url, {"type_id": str(GUARDIAN), "function_id": str(self.logistics.pk)}, HTTP_ACCEPT="application/json"
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual((body["ok"], body["message"]), (True, "Guardian now counts as Logistics."))
        self.assertIn("composition_html", body["state"])

        self.client.force_login(self.fc)
        refused = self.client.post(self.url, {"type_id": str(GUARDIAN), "function_id": str(self.dps.pk)})
        self.assertEqual(refused.status_code, 403)

    def test_classify_form_is_for_configuration_managers(self):
        row = Row(key=GUARDIAN, label="Guardian", count=3, percent=50.0)
        context = {"row": row, "operation": self.operation, "ship_functions": [self.dps, self.logistics]}

        html = render_to_string("fleetops/operation/_fleet_classify.html", {**context, "can_classify": True})

        self.assertIn(f'action="{self.url}"', html)
        self.assertIn(f'name="type_id" value="{GUARDIAN}"', html)
        self.assertIn(f'<option value="{self.logistics.pk}">Logistics</option>', html)
        self.assertIn('class="d-flex gap-1 fleet-classify" data-fleet-action', html)
        self.assertEqual(render_to_string("fleetops/operation/_fleet_classify.html",
                                          {**context, "can_classify": False}).strip(), "")
