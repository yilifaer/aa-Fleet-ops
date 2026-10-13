"""Fleet composition: ship functions, ship types, classes and systems, and the bridgeable mass (F8)."""

from types import SimpleNamespace
from unittest import mock

from django.core.cache import cache
from django.test import SimpleTestCase, TestCase
from django.utils import translation

from fleetops.models import FleetOperation, FleetOpsSettings, ShipFunction
from fleetops.services import composition, sde
from fleetops.services.composition import UNCLASSIFIED, build_composition, classify, load_classifier
from fleetops.services.fleet_rules import DEFAULT_BRIDGE_EXCLUDED_GROUP_IDS
from fleetops.services.fleet_view import fleet_panel

from . import factories as f

# SDE groups (category 6 = ship) and types used below; masses in kg as in the SDE.
SHIP, CHARGE = 6, 8
GROUPS = {
    25: ("Frigate", SHIP),
    832: ("Logistics", SHIP),
    30: ("Titan", SHIP),
    941: ("Industrial Command Ship", SHIP),
    29: ("Capsule", SHIP),
    1534: ("Command Destroyer", SHIP),
    83: ("Projectile Ammo", CHARGE),
}
TYPES = {
    587: ("Rifter", "裂谷级", 25, 1_067_000.0),
    11987: ("Guardian", "守护者级", 832, 11_980_000.0),
    11567: ("Avatar", "神使级", 30, 2_278_125_000.0),
    28606: ("Orca", "逆戟鲸级", 941, 250_000_000.0),
    670: ("Capsule", "太空舱", 29, 32_000.0),
    37480: ("Bifrost", "彩虹桥级", 1534, None),
    178: ("Carbonized Lead S", "碳化铅弹S", 83, 1.0),
}
RIFTER, GUARDIAN, AVATAR, ORCA, CAPSULE, BIFROST, AMMO = 587, 11987, 11567, 28606, 670, 37480, 178
JITA, AMAMAKE = 30000142, 30002537


class FakeRows(list):
    def first(self):
        return self[0] if self else None


class FakeQuerySet:
    """Just enough of a queryset for fleetops.services.sde: filter, order_by, values_list, first."""

    def __init__(self, model, rows):
        self.model = model
        self.rows = list(rows)

    def _value(self, row, name):
        if name == "pk":
            return row["id"]
        if name == "group__category_id":
            group = self.model.sde.groups.get(row.get("group_id"))
            return group["category_id"] if group else None
        if name == "name" and "name_en" in row:
            # The translated column follows the active language, as django-modeltranslation's does.
            language = translation.get_language() or ""
            return row["name_zh"] if language.startswith("zh") else row["name_en"]
        return row.get(name)

    def filter(self, **lookups):
        rows = []
        for row in self.rows:
            for lookup, wanted in lookups.items():
                name, _sep, operator = lookup.partition("__")
                if lookup == "group__category_id":
                    name, operator = lookup, ""
                value = self._value(row, name)
                if operator == "in" and value not in set(wanted):
                    break
                if operator == "iexact" and str(value).casefold() != str(wanted).casefold():
                    break
                if operator == "" and value != wanted:
                    break
            else:
                rows.append(row)
        return FakeQuerySet(self.model, rows)

    def order_by(self, *fields):
        rows = list(self.rows)
        for field in reversed(fields):
            name = field.lstrip("-")
            rows.sort(key=lambda row: self._value(row, name) or 0, reverse=field.startswith("-"))
        return FakeQuerySet(self.model, rows)

    def values_list(self, *fields):
        self.model.sde.queries.append((self.model.name, fields))
        return FakeRows(tuple(self._value(row, field) for field in fields) for row in self.rows)


class FakeModel:
    def __init__(self, sde_fake, name, rows, columns):
        self.sde = sde_fake
        self.name = name
        self.rows = rows
        self._meta = SimpleNamespace(get_fields=lambda: [SimpleNamespace(name=column) for column in columns])
        self.objects = self

    def filter(self, **lookups):
        return FakeQuerySet(self, self.rows).filter(**lookups)


class FakeSDE:
    """The eve_sde app as fleetops.services.sde sees it, with ItemType and ItemGroup.

    ``english_column=False`` imitates a schema without django-modeltranslation's name_en.
    ``queries`` records every values_list() call, so tests can count database reads.
    """

    def __init__(self, *, english_column=True, types=TYPES, groups=GROUPS, error=None):
        self.queries = []
        self.error = error
        self.groups = {
            group_id: {"id": group_id, "category_id": category_id, "published": True, **self._names(name, name, english_column)}
            for group_id, (name, category_id) in groups.items()
        }
        type_rows = [
            {
                "id": type_id,
                "group_id": group_id,
                "mass": mass,
                "published": True,
                **self._names(name, chinese, english_column),
            }
            for type_id, (name, chinese, group_id, mass) in types.items()
        ]
        name_columns = ["name", "name_en"] if english_column else ["name"]
        self.models = {
            "ItemType": FakeModel(self, "ItemType", type_rows, ["id", *name_columns, "group", "mass", "published"]),
            "ItemGroup": FakeModel(self, "ItemGroup", list(self.groups.values()), ["id", *name_columns, "category", "published"]),
        }

    @staticmethod
    def _names(english, chinese, english_column):
        if english_column:
            return {"name_en": english, "name_zh": chinese}
        # Without name_en the only column is translated; in English it reads English.
        return {"name": english}

    def is_installed(self, name):
        return name == "eve_sde"

    def get_model(self, app_label, model_name):
        if self.error is not None:
            raise self.error
        return self.models[model_name]

    def install(self, testcase):
        patcher = mock.patch.object(sde, "apps", self)
        patcher.start()
        testcase.addCleanup(patcher.stop)
        cache.delete_many([f"fleetops:sde:type:{type_id}" for type_id in (*TYPES, 999_999)])
        testcase.addCleanup(cache.delete_many, [f"fleetops:sde:type:{type_id}" for type_id in (*TYPES, 999_999)])
        return self


def info(type_id):
    name, _chinese, group_id, mass = TYPES[type_id]
    return sde.ShipTypeInfo(type_id, name, group_id, GROUPS[group_id][0], GROUPS[group_id][1], mass)


def member(type_id, *, role="squad_member", system=JITA, character_id=None, ship_name="", station_id=None, **extra):
    return {
        "character_id": character_id or f.next_id(),
        "role": role,
        "ship_type_id": type_id,
        "ship_type_name": ship_name,
        "solar_system_id": system,
        "solar_system_name": {JITA: "Jita", AMAMAKE: "Amamake"}.get(system, ""),
        "station_id": station_id,
        **extra,
    }


class ShipTypeDetailsTests(TestCase):
    def test_nothing_without_eve_sde(self):
        self.assertFalse(sde.sde_installed())
        self.assertEqual(sde.ship_type_details({RIFTER}), {})
        self.assertIsNone(sde.find_item_type("Rifter"))
        self.assertIsNone(sde.find_item_group("Frigate"))
        self.assertEqual(sde.item_group_names({25}), {})

    def test_details_come_in_english_in_any_language(self):
        for english_column in (True, False):
            with self.subTest(english_column=english_column):
                FakeSDE(english_column=english_column).install(self)
                with translation.override("zh-hans"):
                    details = sde.ship_type_details([GUARDIAN, AVATAR, "x", None])
                    language = translation.get_language()
                self.assertEqual(details, {GUARDIAN: info(GUARDIAN), AVATAR: info(AVATAR)})
                self.assertEqual(language, "zh-hans")

    def test_only_the_needed_columns_are_read_and_the_result_is_cached(self):
        fake = FakeSDE().install(self)

        sde.ship_type_details({GUARDIAN, RIFTER, 999_999})
        self.assertEqual(
            fake.queries,
            [("ItemType", ("pk", "name_en", "group_id", "mass")), ("ItemGroup", ("pk", "name_en", "category_id"))],
        )

        # Known and unknown types are both cached: a second call reads nothing.
        details = sde.ship_type_details({GUARDIAN, RIFTER, 999_999})
        self.assertEqual(len(fake.queries), 2)
        self.assertEqual(set(details), {GUARDIAN, RIFTER})
        self.assertEqual(details[GUARDIAN].mass, 11_980_000.0)

        sde.ship_type_details({GUARDIAN, AVATAR})
        self.assertEqual(fake.queries[2][0], "ItemType")
        self.assertEqual(len(fake.queries), 4)

    def test_unreadable_sde_gives_nothing(self):
        FakeSDE(error=LookupError("No installed app with label 'eve_sde'.")).install(self)

        self.assertEqual(sde.ship_type_details({GUARDIAN}), {})
        self.assertIsNone(sde.find_item_type("Guardian"))
        self.assertEqual(sde.item_group_names({832}), {})

    def test_find_by_exact_english_name(self):
        FakeSDE().install(self)
        with translation.override("zh-hans"):
            self.assertEqual(sde.find_item_type("  guardian "), (GUARDIAN, "Guardian"))
            self.assertEqual(sde.find_item_group("logistics"), (832, "Logistics"))
            self.assertEqual(sde.item_group_names({832, 25, 404}), {832: "Logistics", 25: "Frigate"})
        self.assertIsNone(sde.find_item_type("Guard"))
        self.assertIsNone(sde.find_item_type(""))
        # Ships only, unless asked otherwise.
        self.assertIsNone(sde.find_item_type("Carbonized Lead S"))
        self.assertEqual(sde.find_item_type("Carbonized Lead S", category_id=None), (AMMO, "Carbonized Lead S"))
        self.assertIsNone(sde.find_item_group("Projectile Ammo"))

    def test_find_without_english_column(self):
        FakeSDE(english_column=False).install(self)

        self.assertEqual(sde.find_item_type("RIFTER"), (RIFTER, "Rifter"))


class ClassificationTests(TestCase):
    """First hit wins: fleet position, ship type rule, ship group rule, default function (ships only)."""

    def setUp(self):
        f.clear_ship_functions()
        self.command = f.ship_function("Command", rules=(), fleet_roles="fleet_commander,wing_commander", sort_order=1)
        self.logi = f.ship_function("Logi", rules=(("group", 832),), sort_order=2)
        self.special = f.ship_function("Special", rules=(("type", GUARDIAN),), sort_order=3)
        self.dps = f.ship_function("DPS", rules=(), is_default=True, sort_order=4)
        self.classifier = load_classifier()

    def function_of(self, type_id, role="squad_member", with_info=True):
        found = classify(member(type_id, role=role), info(type_id) if with_info else None, self.classifier)
        return found.name if found else None

    def test_precedence(self):
        self.assertEqual(self.function_of(GUARDIAN, "wing_commander"), "Command")
        self.assertEqual(self.function_of(GUARDIAN), "Special")
        self.assertEqual(self.function_of(GUARDIAN, with_info=False), "Special")
        self.assertEqual(self.function_of(RIFTER), "DPS")
        # Without SDE data the default cannot tell a ship from anything else.
        self.assertIsNone(self.function_of(RIFTER, with_info=False))
        self.assertIsNone(self.function_of(AMMO))
        self.assertEqual(self.function_of(RIFTER, "squad_commander"), "DPS")

    def test_group_rule(self):
        self.special.rules.all().delete()
        self.classifier = load_classifier()
        self.assertEqual(self.function_of(GUARDIAN, with_info=True), "Logi")
        self.assertIsNone(load_classifier().resolve(ship_type_id=GUARDIAN))

    def test_inactive_functions_are_ignored(self):
        ShipFunction.objects.filter(pk__in=[self.command.pk, self.special.pk]).update(is_active=False)
        classifier = load_classifier()

        found = classify(member(GUARDIAN, role="fleet_commander"), info(GUARDIAN), classifier)

        self.assertEqual(found.name, "Logi")


class BuildCompositionTests(TestCase):
    def setUp(self):
        f.clear_ship_functions()
        self.boosts = f.ship_function("Boosts", rules=(("group", 941),), color="#20c997", sort_order=20)
        self.logi = f.ship_function("Logi", rules=(("group", 832),), color="#198754", sort_order=10)
        self.capsules = f.ship_function("Capsules", rules=(("type", CAPSULE),), color="#6c757d", sort_order=90)
        self.dps = f.ship_function("DPS", rules=(), is_default=True, sort_order=30)
        self.settings = FleetOpsSettings.get_solo()

    def build(self, members, **kwargs):
        return build_composition(members, settings=self.settings, **kwargs)

    def fleet(self):
        return [
            member(GUARDIAN, ship_name="Guardian"),
            member(GUARDIAN, ship_name="Guardian", system=AMAMAKE, station_id=60000001),
            member(RIFTER),
            member(AVATAR, system=AMAMAKE),
            member(ORCA),
            member(CAPSULE, station_id=60000002),
            member(BIFROST),
            member(424242),
        ]

    def test_counts_by_function_type_class_and_system(self):
        FakeSDE().install(self)

        result = self.build(self.fleet(), fc_system_id=JITA)

        self.assertEqual(result.total, 8)
        self.assertTrue(result.sde_available)
        self.assertEqual(
            [(row.label, row.count, row.percent, row.color) for row in result.by_function],
            [
                ("Logi", 2, 25.0, "#198754"),
                ("Boosts", 1, 12.5, "#20c997"),
                ("DPS", 3, 37.5, "#0d6efd"),
                ("Capsules", 1, 12.5, "#6c757d"),
                (UNCLASSIFIED, 1, 12.5, ""),
            ],
        )
        self.assertEqual(result.by_function[0].key, self.logi.pk)
        self.assertIsNone(result.by_function[-1].key)
        self.assertEqual(result.by_type[0].label, "Guardian")
        self.assertEqual(result.by_type[0].count, 2)
        self.assertEqual(result.by_type[0].icon_url, f"https://images.evetech.net/types/{GUARDIAN}/icon?size=32")
        self.assertEqual(result.by_type[0].extra["function_name"], "Logi")
        labels = {row.key: row.label for row in result.by_type}
        # Names come from the tracked member first, then from the SDE, then the id.
        self.assertEqual((labels[RIFTER], labels[424242]), ("Rifter", "Type 424242"))
        self.assertEqual([(row.key, row.label, row.count) for row in result.unclassified], [(424242, "Type 424242", 1)])
        self.assertEqual(
            [(row.label, row.count) for row in result.by_group],
            [("Logistics", 2), ("Capsule", 1), ("Command Destroyer", 1), ("Frigate", 1), ("Industrial Command Ship", 1),
             ("Not in the SDE", 1), ("Titan", 1)],
        )
        systems = {row.label: row for row in result.by_system}
        self.assertEqual([row.label for row in result.by_system], ["Jita", "Amamake"])
        self.assertEqual((systems["Jita"].count, systems["Jita"].extra), (6, {"docked": 1, "fc_system": True}))
        self.assertEqual((systems["Amamake"].count, systems["Amamake"].extra), (2, {"docked": 1, "fc_system": False}))

    def test_bridgeable_mass_follows_the_titan_bridge_rule(self):
        """Q4: capitals, supercapitals, Rorqual, freighters, jump freighters and Orca/Porpoise are not bridged."""
        FakeSDE().install(self)

        bridge = self.build(self.fleet()).bridge

        # Guardian x2, Rifter, Capsule; Avatar (Titan) and Orca (Industrial Command Ship) are excluded,
        # the Bifrost has no mass and the unknown hull is not in the SDE.
        self.assertEqual(bridge.mass_kg, 2 * 11_980_000.0 + 1_067_000.0 + 32_000.0)
        self.assertEqual(bridge.ships, 4)
        self.assertEqual([(row.label, row.count) for row in bridge.excluded], [("Industrial Command Ship", 1), ("Titan", 1)])
        self.assertEqual(bridge.unknown_mass, 2)
        excluded = {int(value) for value in DEFAULT_BRIDGE_EXCLUDED_GROUP_IDS.split(",")}
        self.assertEqual(excluded, {30, 485, 513, 547, 659, 883, 902, 941, 1538, 4594, 5120})
        self.assertEqual(self.settings.bridge_excluded_groups, excluded)

    def test_groups_without_a_name_in_the_sde_get_the_english_name(self):
        FakeSDE(groups={**GROUPS, 30: ("", SHIP)}).install(self)

        result = self.build([member(AVATAR)])

        self.assertEqual([row.label for row in result.by_group], ["Titan"])
        self.assertEqual([row.label for row in result.bridge.excluded], ["Titan"])

    def test_excluded_groups_come_from_the_settings(self):
        FakeSDE().install(self)
        self.settings.bridge_excluded_group_ids = "832"

        bridge = self.build(self.fleet()).bridge

        self.assertEqual([(row.label, row.count) for row in bridge.excluded], [("Logistics", 2)])
        self.assertEqual(bridge.mass_kg, 1_067_000.0 + 2_278_125_000.0 + 250_000_000.0 + 32_000.0)

    def test_without_the_sde(self):
        result = self.build(self.fleet(), fc_system_id=AMAMAKE)

        self.assertFalse(result.sde_available)
        self.assertEqual(result.by_group, [])
        self.assertEqual(result.bridge.mass_kg, 0)
        self.assertEqual(result.bridge.unknown_mass, 8)
        # Only type rules and positions work: the capsule; everything else is unclassified.
        self.assertEqual(
            [(row.label, row.count) for row in result.by_function], [("Capsules", 1), (UNCLASSIFIED, 7)]
        )
        self.assertEqual(result.by_system[1].extra["fc_system"], True)

    def test_empty_fleet(self):
        result = self.build([])

        self.assertEqual((result.total, result.by_function, result.by_type), (0, [], []))

    def test_tracked_member_states_count_too(self):
        fc = f.create_user(perms=f.FC_PERMS)
        operation = f.create_operation(fc)
        f.member_state(operation, f.main_of(fc), role="fleet_commander", ship_type_id=GUARDIAN, ship_type_name="Guardian",
                       solar_system_id=JITA, solar_system_name="Jita", station_id=60003760)
        ShipFunction.objects.filter(pk=self.logi.pk).update(fleet_roles="fleet_commander")

        result = self.build(operation.member_states.all())

        self.assertEqual([(row.label, row.count) for row in result.by_function], [("Logi", 1)])
        self.assertEqual(result.by_system[0].extra["docked"], 1)

    def test_closed_fleets_count_everyone_tracked(self):
        fc = f.create_user(perms=f.FC_PERMS)
        operation = f.create_operation(fc, status=FleetOperation.Status.CLOSED)
        f.member_state(operation, f.main_of(fc), role="fleet_commander", ship_type_id=GUARDIAN, active=False)
        f.member_state(operation, f.create_character("Left Early"), ship_type_id=RIFTER, active=False)

        panel = fleet_panel(operation, fc)

        self.assertIsNone(panel["fleet_view"])
        self.assertEqual(panel["composition"].total, 2)
        self.assertEqual(panel["fleet_changes"], [])


class CompositionTemplateTests(TestCase):
    def render(self, result, **context):
        from django.template.loader import render_to_string

        operation = SimpleNamespace(status="active", uuid="00000000-0000-0000-0000-000000000000")
        return render_to_string(
            "fleetops/operation/_fleet_composition.html",
            {"composition": result, "operation": operation, "fleet_view": object(), **context},
        )

    def test_numbers_and_hints(self):
        FakeSDE().install(self)
        result = build_composition([member(GUARDIAN), member(AVATAR)], settings=FleetOpsSettings.get_solo())

        html = self.render(result)

        self.assertIn("11,980,000 kg", html)
        self.assertIn("Not bridgeable: Titan 1", html)
        self.assertIn("Hull mass from the SDE; fitted modules (plates, propulsion) not included.", html)
        self.assertIn('data-function-filter="', html)
        self.assertIn('role="button"', html)

    def test_without_the_sde(self):
        html = self.render(build_composition([member(RIFTER)], settings=FleetOpsSettings.get_solo()))

        self.assertIn("Ship classes need django-eveonline-sde.", html)
        self.assertIn("Bridgeable mass needs django-eveonline-sde.", html)
        self.assertIn('id="fleet-unclassified"', html)
        self.assertIn('data-function-filter="none"', html)

    def test_colours_that_are_not_hex_never_reach_a_style_attribute(self):
        result = composition.Composition(
            total=1, by_function=[composition.Row(key=1, label="Odd", count=1, percent=100, color="red;x:y")]
        )

        html = self.render(result)

        self.assertNotIn("red;x:y", html)


class KilogramFilterTests(SimpleTestCase):
    def test_formatting(self):
        from fleetops.templatetags.fleetops_fleet import bar_width, hex_color, kilograms

        self.assertEqual(kilograms(1_234_567_000.4), "1,234,567,000 kg")
        self.assertEqual(kilograms(0), "0 kg")
        self.assertEqual(kilograms(None), "")
        self.assertEqual(bar_width(37.5), "37.5%")
        self.assertEqual(bar_width(140), "100.0%")
        self.assertEqual(bar_width("x"), "0.0%")
        self.assertEqual(hex_color("#0d6efd"), "#0d6efd")
        self.assertEqual(hex_color("#0d6efd; background: url(x)"), "")
