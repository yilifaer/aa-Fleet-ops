# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""The aa-srp provider: SRP links created in aa-srp (app label ``aasrp``) at fleet start.

aa-srp is not installed in the test project. The fakes below carry the field definitions of
``aasrp.SrpLink`` and ``aasrp.FleetType`` from aa-srp 5.2.1 (unchanged in 5.3.0).
"""

from datetime import datetime, timedelta
from datetime import timezone as dt_timezone
from types import SimpleNamespace
from unittest import mock

from allianceauth.eveonline.models import EveCharacter
from django.apps import apps as django_apps
from django.contrib import admin
from django.contrib.auth.models import User
from django.db import models
from django.http import HttpResponse
from django.test import RequestFactory, TestCase, override_settings
from django.urls import include, path
from django.utils import timezone
from django.utils.crypto import get_random_string

from fleetops.models import FleetOperation, FleetOpsSettings, OperationAction
from fleetops.providers import srp as srp_module
from fleetops.providers.srp import (
    AaSrpProvider,
    AllianceAuthBuiltinSRPProvider,
    create_srp_link,
    get_registered_srp_providers,
    get_srp_provider,
)
from fleetops.services.operations import retry_srp

from . import factories as f
from .link_fakes import setUpModule, tearDownModule  # noqa: F401 - unittest runs them around the module
from .test_integrations_roles import PatchedStartFleetMixin, model_field

FLEET_TIME = datetime(2026, 10, 12, 19, 5, tzinfo=dt_timezone.utc)


def _blank_view(request, **kwargs):
    return HttpResponse("")


_aasrp_patterns = (
    [
        path("", _blank_view, name="srp_links"),
        path("srp-link/<str:srp_code>/request-srp/", _blank_view, name="request_srp"),
    ],
    "aasrp",
)

# The FleetOps pages plus aa-srp's URL names, mounted where aa-srp mounts them.
urlpatterns = [
    path("fleetops/", include("fleetops.urls")),
    path("ship-replacement/", include(_aasrp_patterns)),
]


class FakeQuerySet(list):
    def order_by(self, *fields):
        return FakeQuerySet(sorted(self, key=lambda row: row.pk))

    def first(self):
        return self[0] if self else None


class FakeSrpLinkManager:
    def __init__(self):
        self.rows = []

    def filter(self, **lookups):
        return FakeQuerySet(
            row for row in self.rows if all(getattr(row, key) == value for key, value in lookups.items())
        )

    def create(self, **values):
        row = SimpleNamespace(pk=len(self.rows) + 1, **({"srp_code": "", "srp_status": "Active"} | values))
        # aa-srp's SrpLink.save() fills an empty code with a random 16-character one.
        if not row.srp_code:
            row.srp_code = get_random_string(length=16)
        self.rows.append(row)
        return row


def fake_srp_link_model(extra_fields=()):
    """A stand-in for aasrp.SrpLink with the aa-srp 5.2.1 field definitions."""
    fields = [
        model_field("id", models.AutoField(primary_key=True)),
        model_field("srp_name", models.CharField(max_length=254, default="")),
        model_field("srp_status", models.CharField(max_length=9, default="Active")),
        model_field("srp_code", models.CharField(max_length=16, default="")),
        model_field(
            "fleet_commander",
            models.ForeignKey(EveCharacter, null=True, blank=True, default=None, on_delete=models.SET_NULL),
        ),
        model_field("fleet_doctrine", models.CharField(max_length=254, default="")),
        model_field(
            "fleet_type",
            models.ForeignKey("auth.Group", null=True, blank=True, default=None, on_delete=models.SET_NULL),
        ),
        model_field("fleet_time", models.DateTimeField()),
        model_field("aar_link", models.CharField(max_length=254, blank=True, default="")),
        model_field("creator", models.ForeignKey(User, null=True, blank=True, default=None, on_delete=models.SET_NULL)),
        *extra_fields,
    ]
    return SimpleNamespace(_meta=SimpleNamespace(concrete_fields=fields), objects=FakeSrpLinkManager())


class FakeFleetTypeManager:
    def __init__(self, rows):
        self.rows = rows

    def filter(self, **lookups):
        return FakeQuerySet(
            row for row in self.rows if all(getattr(row, key) == value for key, value in lookups.items())
        )


def fake_fleet_type_model(*types):
    rows = [SimpleNamespace(pk=pk, name=name, is_enabled=enabled) for pk, (name, enabled) in enumerate(types, 1)]
    return SimpleNamespace(objects=FakeFleetTypeManager(rows))


class AaSrpInstalledMixin:
    """Pretend aa-srp is installed, with fake SrpLink and FleetType models."""

    def install_aasrp(self, link_model=None, fleet_type_model=None):
        self.link_model = link_model or fake_srp_link_model()
        self.fleet_type_model = fleet_type_model or fake_fleet_type_model()
        real_is_installed = django_apps.is_installed
        real_get_model = django_apps.get_model
        fakes = {("aasrp", "SrpLink"): self.link_model, ("aasrp", "FleetType"): self.fleet_type_model}

        def is_installed(name):
            return name == "aasrp" or real_is_installed(name)

        def get_model(app_label, model_name=None, **kwargs):
            if (app_label, model_name) in fakes:
                return fakes[(app_label, model_name)]
            return real_get_model(app_label, model_name, **kwargs)

        for name, replacement in (("is_installed", is_installed), ("get_model", get_model)):
            patcher = mock.patch.object(srp_module.apps, name, side_effect=replacement)
            patcher.start()
            self.addCleanup(patcher.stop)
        return self.link_model


@override_settings(ROOT_URLCONF=__name__, SITE_URL="https://auth.example.com")
class AaSrpProviderTests(AaSrpInstalledMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.fc = f.create_user("aasrp_fc", perms=f.FC_PERMS, main_name="Srp Main")
        self.fc_alt = f.add_alt(self.fc, "Srp Alt")
        self.type_obj = f.fleet_type("StratOp")
        self.operation = f.create_operation(
            self.fc, fc_character=self.fc_alt, type_obj=self.type_obj, started_at=FLEET_TIME, doctrine_name="Shield Ferox"
        )
        self.provider = AaSrpProvider()

    def created(self):
        return self.link_model.objects.rows

    def page_url(self, operation=None):
        return f"https://auth.example.com/fleetops/operations/{(operation or self.operation).uuid}/"

    def test_provider_is_registered_by_default(self):
        self.assertIsInstance(get_registered_srp_providers().get("aasrp"), AaSrpProvider)

    def test_creates_a_link_from_the_fleet_details(self):
        self.install_aasrp()

        result = self.provider.create_for_operation(self.operation)

        [link] = self.created()
        self.assertTrue(result.created)
        self.assertEqual(result.provider, "aasrp")
        self.assertEqual(result.message, "aa-srp link created.")
        self.assertEqual(len(link.srp_code), 16)
        self.assertEqual(result.reference, link.srp_code)
        self.assertEqual(result.url, f"/ship-replacement/srp-link/{link.srp_code}/request-srp/")
        self.assertEqual(link.srp_name, "StratOp — Srp Alt — 2026-10-12 19:05 EVE")
        self.assertEqual(link.fleet_time, FLEET_TIME)
        self.assertEqual(link.fleet_doctrine, "Shield Ferox")
        # The main character is the fleet commander of record, the FC's Auth user the creator.
        self.assertEqual(link.fleet_commander, f.main_of(self.fc))
        self.assertEqual(link.creator, self.fc)
        self.assertEqual(link.aar_link, self.page_url())
        self.assertIsNone(link.fleet_type)

    def test_srp_name_uses_eve_time_whatever_the_site_time_zone(self):
        self.install_aasrp()

        with override_settings(TIME_ZONE="Australia/Adelaide"), timezone.override("Australia/Adelaide"):
            self.provider.create_for_operation(self.operation)

        self.assertTrue(self.created()[0].srp_name.endswith("2026-10-12 19:05 EVE"))

    def test_doctrine_falls_back_to_the_fleet_type(self):
        self.install_aasrp()
        self.operation.doctrine_name = ""

        self.provider.create_for_operation(self.operation)

        self.assertEqual(self.created()[0].fleet_doctrine, "StratOp")

    def test_long_values_are_cut_to_the_aasrp_field_length(self):
        self.install_aasrp()
        self.operation.doctrine_name = "D" * 255
        self.operation.fc_character_name = "N" * 255

        self.provider.create_for_operation(self.operation)

        link = self.created()[0]
        self.assertEqual(len(link.srp_name), 254)
        self.assertEqual(len(link.fleet_doctrine), 254)

    def test_fleet_type_is_matched_by_name_or_short_name_ignoring_case(self):
        self.type_obj.short_name = "Strat"
        self.type_obj.save()
        cases = (
            ((("stratop", True), ("Roam", True)), "stratop"),
            ((("Roam", True), ("STRAT", True)), "STRAT"),
            ((("StratOp", False), ("Roam", True)), None),
            ((("Home Defense", True),), None),
        )
        for types, expected in cases:
            with self.subTest(types=types):
                self.install_aasrp(fleet_type_model=fake_fleet_type_model(*types))

                self.provider.create_for_operation(self.operation)

                fleet_type = self.created()[-1].fleet_type
                self.assertEqual(getattr(fleet_type, "name", None), expected)

    def test_fc_character_is_the_commander_when_the_main_is_unknown(self):
        self.install_aasrp()
        self.operation.fc_main_character_id = None

        self.provider.create_for_operation(self.operation)

        self.assertEqual(self.created()[0].fleet_commander, self.fc_alt)

    def test_link_is_created_without_a_known_commander(self):
        self.install_aasrp()
        self.operation.fc_main_character_id = f.next_id()
        self.operation.fc_character_id = f.next_id()

        result = self.provider.create_for_operation(self.operation)

        self.assertTrue(result.created)
        self.assertIsNone(self.created()[0].fleet_commander)

    def test_fleet_time_falls_back_to_the_scheduled_time_then_now(self):
        self.install_aasrp()
        scheduled = FLEET_TIME + timedelta(hours=2)
        self.operation.started_at = None
        self.operation.scheduled_at = scheduled

        self.provider.create_for_operation(self.operation)
        self.assertEqual(self.created()[-1].fleet_time, scheduled)

        other = f.create_operation(self.fc, started_at=FLEET_TIME)
        other.started_at = None
        before = timezone.now()
        self.provider.create_for_operation(other)
        self.assertGreaterEqual(self.created()[-1].fleet_time, before)

    def test_link_pointing_to_the_fleet_page_is_reused(self):
        model = self.install_aasrp()
        model.objects.create(srp_name="Earlier", srp_code="EXISTINGCODE0001", aar_link=self.page_url())

        result = self.provider.create_for_operation(self.operation)

        self.assertEqual(len(self.created()), 1)
        self.assertTrue(result.created)
        self.assertEqual(result.reference, "EXISTINGCODE0001")
        self.assertEqual(result.url, "/ship-replacement/srp-link/EXISTINGCODE0001/request-srp/")
        self.assertEqual(result.message, "Existing aa-srp link of this fleet reused.")

    def test_links_of_other_fleets_are_not_reused(self):
        model = self.install_aasrp()
        other = f.create_operation(self.fc)
        model.objects.create(srp_name="Other", srp_code="OTHERFLEETCODE01", aar_link=self.page_url(other))

        result = self.provider.create_for_operation(self.operation)

        self.assertEqual(len(self.created()), 2)
        self.assertNotEqual(result.reference, "OTHERFLEETCODE01")

    def test_unknown_required_field_is_reported_without_creating_a_link(self):
        self.install_aasrp(fake_srp_link_model([model_field("fleet_budget", models.BigIntegerField())]))

        result = self.provider.create_for_operation(self.operation)

        self.assertFalse(result.created)
        self.assertEqual(result.message, "Unsupported aasrp schema; required fields: fleet_budget")
        self.assertEqual(self.created(), [])

    @override_settings(ROOT_URLCONF="testauth.urls")
    def test_without_aasrp_url_names_the_link_points_to_the_aasrp_app(self):
        self.install_aasrp()

        result = self.provider.create_for_operation(self.operation)

        self.assertTrue(result.created)
        self.assertEqual(result.url, "/ship-replacement/")

    def test_auto_mode_prefers_aasrp_over_the_builtin_provider(self):
        self.install_aasrp()
        with mock.patch.object(AllianceAuthBuiltinSRPProvider, "available", return_value=True):
            self.assertIsInstance(get_srp_provider("auto"), AaSrpProvider)
            self.assertIsInstance(get_srp_provider("aa_srp"), AaSrpProvider)

            result = create_srp_link(self.operation, "auto")

        self.assertEqual(result.provider, "aasrp")
        self.assertEqual(len(self.created()), 1)


class AaSrpNotInstalledTests(TestCase):
    def setUp(self):
        self.fc = f.create_user(perms=f.FC_PERMS)
        self.operation = f.create_operation(self.fc)

    def test_provider_reports_that_aasrp_is_not_installed(self):
        provider = AaSrpProvider()

        self.assertFalse(django_apps.is_installed("aasrp"))
        self.assertFalse(provider.available())
        result = provider.create_for_operation(self.operation)
        self.assertFalse(result.created)
        self.assertEqual(result.message, "aa-srp is not installed.")

    def test_auto_mode_falls_back_to_the_builtin_provider(self):
        with mock.patch.object(AllianceAuthBuiltinSRPProvider, "available", return_value=True):
            self.assertIsInstance(get_srp_provider("auto"), AllianceAuthBuiltinSRPProvider)

    def test_django_admin_describes_the_automatic_order(self):
        settings_admin = admin.site._registry[FleetOpsSettings]
        request = RequestFactory().get("/")
        request.user = User.objects.create_superuser("srp_admin", "srp_admin@example.com", "x")

        form = settings_admin.get_form(request)()

        self.assertEqual(srp_module.AUTO_PROVIDER_ORDER, ("fleetops", "aasrp", "allianceauth_builtin"))
        self.assertEqual(
            form.fields["srp_provider"].help_text,
            "SRP provider. 'auto' uses FleetOps SRP when it is enabled, then aa-srp, then Alliance Auth SRP.",
        )

    def test_explicit_key_reports_the_missing_app(self):
        for key in ("aasrp", "aa_srp"):
            with self.subTest(key=key):
                result = create_srp_link(self.operation, key)

                self.assertFalse(result.created)
                self.assertEqual(result.message, f"SRP provider '{key}' is not installed.")


@override_settings(ROOT_URLCONF=__name__)
class AaSrpFleetStartTests(AaSrpInstalledMixin, PatchedStartFleetMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.install_aasrp()

    def test_fleet_start_creates_the_link_automatically(self):
        operation = self.start()

        operation.refresh_from_db()
        [link] = self.link_model.objects.rows
        self.assertEqual(operation.status, FleetOperation.Status.ACTIVE)
        self.assertEqual(operation.srp_provider, "aasrp")
        self.assertEqual(operation.srp_reference, link.srp_code)
        self.assertEqual(operation.srp_url, f"/ship-replacement/srp-link/{link.srp_code}/request-srp/")
        self.assertEqual(operation.srp_error, "")
        self.assertEqual(link.creator, self.fc)
        action = OperationAction.objects.get(operation=operation, action="srp_link")
        self.assertEqual(action.status, OperationAction.Status.SUCCESS)
        self.assertEqual(action.error_message, "aa-srp link created.")

    def test_retry_after_the_start_does_not_create_a_second_link(self):
        operation = self.start()

        action = retry_srp(operation)

        self.assertEqual(len(self.link_model.objects.rows), 1)
        self.assertEqual(action.status, OperationAction.Status.SUCCESS)
        self.assertIn("already linked", action.error_message)

    def test_retry_after_an_interrupted_save_reuses_the_link(self):
        operation = self.start()
        code = self.link_model.objects.rows[0].srp_code
        # The link exists in aa-srp, but the fleet never stored it (for example a worker restart).
        FleetOperation.objects.filter(pk=operation.pk).update(srp_provider="", srp_reference="", srp_url="")
        operation.refresh_from_db()

        action = retry_srp(operation)

        operation.refresh_from_db()
        self.assertEqual(len(self.link_model.objects.rows), 1)
        self.assertEqual(action.status, OperationAction.Status.SUCCESS)
        self.assertEqual(operation.srp_reference, code)
        self.assertEqual(operation.srp_provider, "aasrp")

    def test_attendance_only_fleet_creates_no_link(self):
        self.start(mode="attendance_only")

        self.assertEqual(self.link_model.objects.rows, [])
