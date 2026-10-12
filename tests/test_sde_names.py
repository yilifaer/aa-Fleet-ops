"""Tests for English ship and solar system names and the fleetops_refresh_names command."""

from io import StringIO
from types import SimpleNamespace
from unittest import mock

from django.core.management import call_command
from django.test import SimpleTestCase, TestCase
from django.utils import timezone, translation

from fleetops.management.commands import fleetops_refresh_names
from fleetops.models import FleetMemberEvent, FleetMemberState
from fleetops.providers import doctrines
from fleetops.providers.doctrines import FITTINGS_DOCTRINE_SOURCE, doctrine_fits_snapshot
from fleetops.services import sde

from . import factories as f

REDEEMER, RIFTER, CAPSULE = 22428, 587, 670
JITA, AMARR = 30000142, 30002187

ENGLISH = {REDEEMER: "Redeemer", RIFTER: "Rifter", CAPSULE: "Capsule", JITA: "Jita", AMARR: "Amarr"}
CHINESE = {REDEEMER: "救世级", RIFTER: "裂谷级", CAPSULE: "太空舱", JITA: "吉他", AMARR: "艾玛"}


class TranslatedRow:
    """An SDE row as django-modeltranslation exposes it: ``name`` follows the active language."""

    def __init__(self, pk, *, with_english_column=True):
        self.pk = pk
        if with_english_column:
            self.name_en = ENGLISH[pk]

    @property
    def name(self):
        return CHINESE[self.pk] if (translation.get_language() or "").startswith("zh") else ENGLISH[self.pk]


class FakeManager:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def filter(self, pk__in):
        self.calls.append(sorted(pk__in))
        # Like a queryset, the rows' ``name`` is read later, in whatever language is active then.
        return [row for row in self.rows if row.pk in set(pk__in)]


class FakeSDEApps:
    def __init__(self, **models):
        self.models = models

    def is_installed(self, name):
        return name == "eve_sde"

    def get_model(self, app_label, model_name):
        return self.models[model_name]


def fake_sde(*, with_english_column=True):
    def model(*ids):
        return SimpleNamespace(
            objects=FakeManager([TranslatedRow(pk, with_english_column=with_english_column) for pk in ids])
        )

    return FakeSDEApps(ItemType=model(REDEEMER, RIFTER, CAPSULE), SolarSystem=model(JITA, AMARR))


class LabelTests(SimpleTestCase):
    def test_english_column_wins_in_any_language(self):
        with translation.override("zh-hans"):
            self.assertEqual(sde._label(TranslatedRow(REDEEMER)), "Redeemer")
            self.assertEqual(sde._label(SimpleNamespace(name_en="", name="Rifter")), "Rifter")
            self.assertEqual(sde._label(SimpleNamespace(type_name="Capsule")), "Capsule")
            self.assertEqual(sde._label(SimpleNamespace(solar_system_name="Jita")), "Jita")
            self.assertEqual(sde._label(SimpleNamespace()), "")


class EnglishLookupTests(SimpleTestCase):
    def test_names_are_english_during_a_chinese_request(self):
        fake = fake_sde()
        with mock.patch.object(sde, "apps", fake), translation.override("zh-hans"):
            ships = sde.item_type_names({REDEEMER, RIFTER})
            systems = sde.solar_system_names([JITA])
            language = translation.get_language()

        self.assertEqual(ships, {REDEEMER: "Redeemer", RIFTER: "Rifter"})
        self.assertEqual(systems, {JITA: "Jita"})
        self.assertEqual(language, "zh-hans")

    def test_models_without_an_english_column_are_read_in_english(self):
        with mock.patch.object(sde, "apps", fake_sde(with_english_column=False)), translation.override("zh-hans"):
            self.assertEqual(sde.item_type_names([REDEEMER]), {REDEEMER: "Redeemer"})
            self.assertEqual(sde.solar_system_names([AMARR]), {AMARR: "Amarr"})

    def test_not_installed(self):
        self.assertFalse(sde.sde_installed())
        self.assertEqual(sde.item_type_names([REDEEMER]), {})


class ChainedQuerySet:
    """Doctrine-fit through rows; like a queryset, nothing is read before iteration."""

    def __init__(self, rows):
        self.rows = rows

    def filter(self, **kwargs):
        return self

    def select_related(self, *names):
        return self

    def prefetch_related(self, *names):
        return self

    def order_by(self, *names):
        return self

    def __iter__(self):
        return iter(self.rows)


class DoctrineShipNameTests(SimpleTestCase):
    def test_snapshot_ship_names_are_english(self):
        ship = TranslatedRow(REDEEMER, with_english_column=False)
        fitting = SimpleNamespace(pk=5, name="Bridge Redeemer", ship_type_type_id=REDEEMER, ship_type=ship)
        queryset = ChainedQuerySet([SimpleNamespace(pk=1, fitting=fitting)])
        model = SimpleNamespace(fittings=SimpleNamespace(through=SimpleNamespace(objects=queryset)))

        with mock.patch.object(doctrines, "apps", mock.Mock(get_model=mock.Mock(return_value=model))):
            with translation.override("zh-hans"):
                fits = doctrine_fits_snapshot(FITTINGS_DOCTRINE_SOURCE, "7")

        self.assertEqual(fits, [{"fit_id": 5, "fit": "Bridge Redeemer", "ship_type_id": REDEEMER, "ship": "Redeemer"}])


def run_command(*args):
    out = StringIO()
    call_command("fleetops_refresh_names", *args, stdout=out, stderr=StringIO())
    return out.getvalue()


class RefreshNamesCommandTests(TestCase):
    def setUp(self):
        self.fc = f.create_user(perms=f.FC_PERMS)
        self.operation = f.create_operation(self.fc)
        now = timezone.now()
        self.states = [
            FleetMemberState.objects.create(
                operation=self.operation,
                character_id=f.next_id(),
                character_name=name,
                ship_type_id=ship,
                ship_type_name=ship_name,
                solar_system_id=system,
                solar_system_name=system_name,
                first_seen=now,
                last_seen=now,
            )
            for name, ship, ship_name, system, system_name in (
                ("A", REDEEMER, "救世级", JITA, "吉他"),
                ("B", REDEEMER, "Redeemer", JITA, "Jita"),
                ("C", RIFTER, "裂谷级", AMARR, "Amarr"),
                ("D", None, "", None, ""),
                ("E", 999999, "Unknown ship", 31000000, "Unknown system"),
            )
        ]
        self.event = FleetMemberEvent.objects.create(
            operation=self.operation,
            character_id=self.states[0].character_id,
            character_name="A",
            event_type=FleetMemberEvent.EventType.SHIP_CHANGE,
            old_value="裂谷级",
            new_value="救世级",
        )

    def names(self):
        return list(
            FleetMemberState.objects.order_by("character_name").values_list("ship_type_name", "solar_system_name")
        )

    def test_dry_run_changes_nothing(self):
        before = self.names()

        with mock.patch.object(sde, "apps", fake_sde()), translation.override("zh-hans"):
            output = run_command("--dry-run")

        self.assertIn("Would update: 2 ship names and 1 solar system names on fleet member records.", output)
        self.assertEqual(self.names(), before)

    def test_names_are_stored_in_english(self):
        with mock.patch.object(sde, "apps", fake_sde()), translation.override("zh-hans"):
            output = run_command()

        self.assertIn("Updated: 2 ship names and 1 solar system names on fleet member records.", output)
        self.assertEqual(
            self.names(),
            [
                ("Redeemer", "Jita"),
                ("Redeemer", "Jita"),
                ("Rifter", "Amarr"),
                ("", ""),
                ("Unknown ship", "Unknown system"),
            ],
        )
        # Events keep the labels they were recorded with.
        self.event.refresh_from_db()
        self.assertEqual((self.event.old_value, self.event.new_value), ("裂谷级", "救世级"))

        with mock.patch.object(sde, "apps", fake_sde()):
            self.assertIn("Updated: 0 ship names and 0 solar system names", run_command())

    def test_ids_are_looked_up_in_batches(self):
        fake = fake_sde()

        with mock.patch.object(sde, "apps", fake), mock.patch.object(fleetops_refresh_names, "BATCH_SIZE", 2):
            run_command()

        self.assertEqual(fake.models["ItemType"].objects.calls, [[RIFTER, REDEEMER], [999999]])
        self.assertEqual(fake.models["SolarSystem"].objects.calls, [[JITA, AMARR], [31000000]])

    def test_without_the_sde_app(self):
        before = self.names()

        output = run_command()

        self.assertIn("The eve_sde app is not installed; there are no names to read.", output)
        self.assertEqual(self.names(), before)
