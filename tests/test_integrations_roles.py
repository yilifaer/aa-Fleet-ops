"""Tests for optional integrations (SRP, doctrines, SDE routing), fleet controls and special roles."""

import uuid
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone
from types import SimpleNamespace
from unittest import mock

from allianceauth.eveonline.models import EveCharacter
from allianceauth.tests.auth_utils import AuthUtils
from django.apps import apps as django_apps
from django.contrib.auth.models import Permission, User
from django.db import models
from django.http import HttpResponse
from django.test import TestCase, override_settings
from django.urls import include, path, reverse
from django.utils import timezone
from esi.exceptions import HTTPClientError

from fleetops.constants import FLEET_READ_SCOPE
from fleetops.forms import OperationRoleAssignmentForm, StartFleetForm
from fleetops.models import (
    AuditLog,
    FleetMemberState,
    FleetOperation,
    OperationAction,
    OperationRoleAssignment,
)
from fleetops.providers import doctrines as doctrines_module
from fleetops.providers import esi as esi_provider
from fleetops.providers import srp as srp_module
from fleetops.providers.doctrines import get_doctrines
from fleetops.providers.esi import FleetDetectionResult, FleetESIError
from fleetops.providers.srp import (
    AllianceAuthBuiltinSRPProvider,
    SRPLinkResult,
    create_srp_link,
    get_registered_srp_providers,
    get_srp_provider,
    register_srp_provider,
)
from fleetops.services import routing as routing_module
from fleetops.services import sde as sde_module
from fleetops.services.fleet_controls import (
    KNOWN_CAPSULE_TYPE_IDS,
    is_capsule_member,
    kick_all_pods,
    pod_members,
)
from fleetops.services.operations import retry_srp, start_fleet
from fleetops.services.roles import add_role_assignment, delete_role_assignment
from fleetops.services.routing import proximity_rows, systems_within_jumps
from fleetops.services.statistics import fc_operations_queryset, fc_statistics

from . import factories as f

Status = FleetOperation.Status
ActionStatus = OperationAction.Status
Role = OperationRoleAssignment.Role

FLEET_ID = 1_045_777_000_001
CAPSULE = 670
GENOLUTION_CAPSULE = 33328
RIFTER = 587
GUARDIAN = 11987

# A fixed month inside the retention window, used by the statistics checks.
STATS_YEAR, STATS_MONTH = 2026, 9
STATS_START = datetime(2026, 9, 12, 19, 0, tzinfo=dt_timezone.utc)


# ---------------------------------------------------------------------------
# URL configuration mirroring Alliance Auth's built-in SRP URL names
# ---------------------------------------------------------------------------


def _blank_view(request, **kwargs):
    return HttpResponse("")


_aa_srp_patterns = (
    [
        path("", _blank_view, name="management"),
        path("<int:fleet_id>/view/", _blank_view, name="fleet"),
        path("<str:fleet_srp>/request/", _blank_view, name="request"),
    ],
    "srp",
)

urlpatterns = [path("srp/", include(_aa_srp_patterns))]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class FakeSRPProvider:
    """SRP provider double that records every create call."""

    def __init__(self, key="fake_srp", *, available=True, error=None, created=True):
        self.key = key
        self.is_available = available
        self.error = error
        self.created = created
        self.calls = []

    def available(self):
        if isinstance(self.is_available, BaseException):
            raise self.is_available
        return self.is_available

    def create_for_operation(self, operation):
        self.calls.append(operation.pk)
        if self.error is not None:
            raise self.error
        if not self.created:
            return SRPLinkResult(self.key, message="Provider declined to create an SRP fleet.")
        number = len(self.calls)
        return SRPLinkResult(
            provider=self.key,
            reference=f"SRP-{number}",
            url=f"/srp/{number}/",
            created=True,
            message="SRP fleet created.",
        )


class IsolatedSRPRegistryMixin:
    """Run each test against an empty SRP provider registry."""

    def setUp(self):
        super().setUp()
        patcher = mock.patch.dict(srp_module._PROVIDERS, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def register(self, provider):
        register_srp_provider(provider)
        return provider


class PatchedStartFleetMixin:
    """Stubs ESI detection, MOTD, tracking and Discord for start_fleet()."""

    def setUp(self):
        super().setUp()
        self.fc = f.create_user("start_fc", perms=f.FC_PERMS)
        self.fc_char = f.main_of(self.fc)
        f.add_token(self.fc, self.fc_char)
        self.type_obj = f.fleet_type("Roam", "2.00")
        targets = {
            "detect_character_fleet": mock.Mock(
                return_value=FleetDetectionResult(fleet_id=FLEET_ID, role="fleet_commander")
            ),
            "set_fleet_motd": mock.Mock(),
            "sync_operation": mock.Mock(),
            "send_discord_webhook": mock.Mock(),
        }
        self.stubs = {}
        for name, stub in targets.items():
            patcher = mock.patch(f"fleetops.services.operations.{name}", stub)
            self.stubs[name] = patcher.start()
            self.addCleanup(patcher.stop)

    def start(self, mode="full", **extra):
        data = {
            "fc_character_id": self.fc_char.character_id,
            "fleet_type": self.type_obj,
            "formup": "Jita",
            "operation_mode": mode,
        }
        data.update(extra)
        return start_fleet(user=self.fc, cleaned_data=data, request_id=uuid.uuid4())


def action_of(operation, name):
    return OperationAction.objects.get(operation=operation, action=name)


def track(
    operation,
    character,
    *,
    active=True,
    system_id=None,
    system_name="",
    ship_type_id=None,
    ship_type_name="",
    fleet_role="squad_member",
):
    now = timezone.now()
    return FleetMemberState.objects.create(
        operation=operation,
        character_id=character.character_id,
        character_name=character.character_name,
        corporation_id=character.corporation_id,
        corporation_name=character.corporation_name,
        ship_type_id=ship_type_id,
        ship_type_name=ship_type_name,
        solar_system_id=system_id,
        solar_system_name=system_name,
        fleet_role=fleet_role,
        first_seen=now - timedelta(minutes=20),
        last_seen=now,
        left_at=None if active else now,
        is_active=active,
    )


def model_field(name, field):
    field.set_attributes_from_name(name)
    return field


class FakeSrpFleetManager:
    def __init__(self):
        self.created = []

    def create(self, **values):
        obj = SimpleNamespace(pk=len(self.created) + 1, **values)
        self.created.append(obj)
        return obj


def fake_aa_srp_model(extra_fields=()):
    """A stand-in for allianceauth.srp.models.SrpFleetMain with the same field definitions."""
    fields = [
        model_field("id", models.AutoField(primary_key=True)),
        model_field("fleet_name", models.CharField(max_length=254, default="")),
        model_field("fleet_doctrine", models.CharField(max_length=254, default="")),
        model_field("fleet_time", models.DateTimeField()),
        model_field("fleet_srp_code", models.CharField(max_length=254, default="")),
        model_field("fleet_srp_status", models.CharField(max_length=254, default="")),
        model_field("fleet_commander", models.ForeignKey(EveCharacter, null=True, on_delete=models.SET_NULL)),
        model_field("fleet_srp_aar_link", models.CharField(max_length=254, default="")),
        *extra_fields,
    ]
    return SimpleNamespace(_meta=SimpleNamespace(concrete_fields=fields), objects=FakeSrpFleetManager())


def fake_model(class_name, label_lower, field_names, rows=(), error=None):
    """Minimal model-like class exposing what the doctrine/SDE integrations introspect."""

    def all_rows():
        if error is not None:
            raise error
        return [SimpleNamespace(pk=pk, **values) for pk, values in rows]

    meta = SimpleNamespace(
        label_lower=label_lower,
        get_fields=lambda: [SimpleNamespace(name=name) for name in field_names],
    )
    return type(class_name, (), {"_meta": meta, "objects": SimpleNamespace(all=all_rows)})


def fake_app_config(name, label, models_list):
    return SimpleNamespace(name=name, label=label, get_models=lambda: list(models_list))


def patch_app_configs(*extra_configs):
    real = list(django_apps.get_app_configs())
    return mock.patch.object(doctrines_module.apps, "get_app_configs", return_value=real + list(extra_configs))


# ---------------------------------------------------------------------------
# SRP provider registry
# ---------------------------------------------------------------------------


class SRPRegistryTests(IsolatedSRPRegistryMixin, TestCase):
    def test_register_adds_provider_by_key(self):
        provider = self.register(FakeSRPProvider("community_srp"))

        self.assertIs(get_registered_srp_providers()["community_srp"], provider)

    def test_register_replaces_existing_provider_with_same_key(self):
        self.register(FakeSRPProvider("community_srp"))
        replacement = self.register(FakeSRPProvider("community_srp"))

        registered = get_registered_srp_providers()
        self.assertEqual(list(registered), ["community_srp"])
        self.assertIs(registered["community_srp"], replacement)

    def test_registered_providers_listing_is_a_copy(self):
        self.register(FakeSRPProvider("community_srp"))

        listing = get_registered_srp_providers()
        listing.pop("community_srp")

        self.assertIn("community_srp", get_registered_srp_providers())

    def test_auto_prefers_better_srp_then_aa_srp_then_builtin(self):
        other = self.register(FakeSRPProvider("zeta_srp"))
        builtin = self.register(FakeSRPProvider("allianceauth_builtin"))
        aa_srp = self.register(FakeSRPProvider("aa_srp"))
        better = self.register(FakeSRPProvider("better_srp"))

        self.assertIs(get_srp_provider("auto"), better)
        better.is_available = False
        self.assertIs(get_srp_provider("auto"), aa_srp)
        aa_srp.is_available = False
        self.assertIs(get_srp_provider("auto"), builtin)
        builtin.is_available = False
        self.assertIs(get_srp_provider("auto"), other)

    def test_auto_falls_back_to_first_available_registered_provider(self):
        self.register(FakeSRPProvider("first_srp", available=False))
        second = self.register(FakeSRPProvider("second_srp"))
        self.register(FakeSRPProvider("third_srp"))

        self.assertIs(get_srp_provider("auto"), second)

    def test_auto_returns_none_when_every_provider_is_unavailable(self):
        self.register(FakeSRPProvider("better_srp", available=False))
        self.register(FakeSRPProvider("zeta_srp", available=False))

        self.assertIsNone(get_srp_provider("auto"))

    def test_blank_or_missing_key_means_auto(self):
        provider = self.register(FakeSRPProvider("zeta_srp"))

        self.assertIs(get_srp_provider(""), provider)
        self.assertIs(get_srp_provider(None), provider)
        self.assertIs(get_srp_provider("  auto  "), provider)

    def test_explicit_key_selects_that_provider_only(self):
        self.register(FakeSRPProvider("better_srp"))
        chosen = self.register(FakeSRPProvider("zeta_srp"))

        self.assertIs(get_srp_provider("zeta_srp"), chosen)
        self.assertIs(get_srp_provider(" zeta_srp "), chosen)

    def test_explicit_unavailable_or_unknown_key_does_not_fall_back(self):
        self.register(FakeSRPProvider("better_srp"))
        self.register(FakeSRPProvider("zeta_srp", available=False))

        self.assertIsNone(get_srp_provider("zeta_srp"))
        self.assertIsNone(get_srp_provider("missing_srp"))

    def test_create_srp_link_without_provider_reports_message(self):
        fc = f.create_user(perms=f.FC_PERMS)
        operation = f.create_operation(fc)

        result = create_srp_link(operation, "missing_srp")

        self.assertFalse(result.created)
        self.assertEqual(result.provider, "missing_srp")
        self.assertEqual(result.reference, "")
        self.assertEqual(result.url, "")
        self.assertIn("No supported SRP provider", result.message)

    def test_create_srp_link_delegates_to_selected_provider(self):
        provider = self.register(FakeSRPProvider("zeta_srp"))
        fc = f.create_user(perms=f.FC_PERMS)
        operation = f.create_operation(fc)

        result = create_srp_link(operation, "auto")

        self.assertEqual(provider.calls, [operation.pk])
        self.assertTrue(result.created)
        self.assertEqual(result.provider, "zeta_srp")
        self.assertEqual(result.reference, "SRP-1")


# ---------------------------------------------------------------------------
# Alliance Auth built-in SRP provider
# ---------------------------------------------------------------------------


class BuiltinSRPProviderTests(TestCase):
    def setUp(self):
        self.fc = f.create_user("builtin_fc", perms=f.FC_PERMS)
        self.operation = f.create_operation(self.fc, doctrine_name="Shield Ferox")
        self.provider = AllianceAuthBuiltinSRPProvider()

    def installed_with(self, model):
        available = mock.patch.object(AllianceAuthBuiltinSRPProvider, "available", return_value=True)
        model_patch = mock.patch.object(AllianceAuthBuiltinSRPProvider, "_model", return_value=model)
        available.start()
        model_patch.start()
        self.addCleanup(available.stop)
        self.addCleanup(model_patch.stop)

    def test_builtin_provider_is_registered_by_default(self):
        provider = get_registered_srp_providers().get("allianceauth_builtin")

        self.assertIsInstance(provider, AllianceAuthBuiltinSRPProvider)

    def test_reports_unavailable_when_srp_app_is_not_installed(self):
        self.assertFalse(django_apps.is_installed("allianceauth.srp"))
        self.assertFalse(self.provider.available())

        result = self.provider.create_for_operation(self.operation)

        self.assertFalse(result.created)
        self.assertEqual(result.provider, "allianceauth_builtin")
        self.assertIn("not installed", result.message)

    def test_explicit_builtin_key_without_srp_app_returns_no_provider(self):
        with mock.patch.dict(srp_module._PROVIDERS, {"allianceauth_builtin": self.provider}, clear=True):
            self.assertIsNone(get_srp_provider("allianceauth_builtin"))
            self.assertIsNone(get_srp_provider("auto"))
            result = create_srp_link(self.operation, "allianceauth_builtin")

        self.assertFalse(result.created)
        self.assertIn("No supported SRP provider", result.message)

    def test_creates_srp_fleet_from_operation_details(self):
        model = fake_aa_srp_model()
        self.installed_with(model)

        result = self.provider.create_for_operation(self.operation)

        self.assertTrue(result.created)
        self.assertEqual(result.provider, "allianceauth_builtin")
        self.assertEqual(result.reference, "1")
        self.assertEqual(result.url, "/srp/")
        created = model.objects.created[0]
        self.assertEqual(created.fleet_time, self.operation.started_at)
        self.assertEqual(created.fleet_doctrine, "Shield Ferox")
        self.assertEqual(created.fleet_commander, f.main_of(self.fc))
        self.assertIn(self.operation.fc_character_name, created.fleet_name)

    def test_doctrine_falls_back_to_fleet_type_name(self):
        model = fake_aa_srp_model()
        self.installed_with(model)
        self.operation.doctrine_name = ""

        self.provider.create_for_operation(self.operation)

        self.assertEqual(model.objects.created[0].fleet_doctrine, str(self.operation.fleet_type))

    def test_unknown_fc_character_does_not_create_fleet(self):
        model = fake_aa_srp_model()
        self.installed_with(model)
        stranger = SimpleNamespace(character_id=f.next_id(), character_name="Unknown Boss")
        operation = f.create_operation(self.fc, fc_character=stranger)

        result = self.provider.create_for_operation(operation)

        self.assertFalse(result.created)
        self.assertIn("not present", result.message)
        self.assertEqual(model.objects.created, [])

    def test_unsupported_schema_with_required_field_is_reported(self):
        model = fake_aa_srp_model([model_field("fleet_budget", models.BigIntegerField())])
        self.installed_with(model)

        result = self.provider.create_for_operation(self.operation)

        self.assertFalse(result.created)
        self.assertIn("fleet_budget", result.message)
        self.assertEqual(model.objects.created, [])

    def test_created_builtin_srp_fleet_accepts_requests(self):
        model = fake_aa_srp_model()
        self.installed_with(model)

        self.provider.create_for_operation(self.operation)

        self.assertTrue(getattr(model.objects.created[0], "fleet_srp_code", ""))

    @override_settings(ROOT_URLCONF=__name__)
    def test_link_points_to_the_created_srp_fleet(self):
        model = fake_aa_srp_model()
        self.installed_with(model)

        result = self.provider.create_for_operation(self.operation)

        self.assertRegex(result.url, r"^/srp/[^/]+/(view|request)/$")


# ---------------------------------------------------------------------------
# SRP during fleet start and retry
# ---------------------------------------------------------------------------


class StartFleetSRPTests(IsolatedSRPRegistryMixin, PatchedStartFleetMixin, TestCase):
    def test_provider_exception_is_captured_and_fleet_still_starts(self):
        provider = self.register(FakeSRPProvider("zeta_srp", error=RuntimeError("SRP database is locked")))

        operation = self.start()

        operation.refresh_from_db()
        self.assertEqual(provider.calls, [operation.pk])
        self.assertEqual(operation.status, Status.ACTIVE)
        self.assertIn("SRP database is locked", operation.srp_error)
        self.assertEqual(operation.srp_reference, "")
        srp_action = action_of(operation, "srp_link")
        self.assertEqual(srp_action.status, ActionStatus.FAILED)
        self.assertIn("SRP database is locked", srp_action.error_message)
        self.assertEqual(action_of(operation, "tracking_start").status, ActionStatus.SUCCESS)

    def test_provider_availability_check_exception_does_not_block_start(self):
        self.register(FakeSRPProvider("zeta_srp", available=RuntimeError("provider import failed")))

        operation = self.start()

        operation.refresh_from_db()
        self.assertEqual(operation.status, Status.ACTIVE)
        self.assertIn("provider import failed", operation.srp_error)
        self.assertEqual(action_of(operation, "srp_link").status, ActionStatus.FAILED)

    def test_created_link_is_stored_on_operation(self):
        self.register(FakeSRPProvider("zeta_srp"))

        operation = self.start()

        operation.refresh_from_db()
        self.assertEqual(operation.srp_provider, "zeta_srp")
        self.assertEqual(operation.srp_reference, "SRP-1")
        self.assertEqual(operation.srp_url, "/srp/1/")
        self.assertEqual(operation.srp_error, "")
        self.assertEqual(action_of(operation, "srp_link").status, ActionStatus.SUCCESS)

    def test_configured_provider_key_is_used(self):
        preferred = self.register(FakeSRPProvider("better_srp"))
        configured = self.register(FakeSRPProvider("zeta_srp"))
        f.settings(srp_provider="zeta_srp")

        operation = self.start()

        operation.refresh_from_db()
        self.assertEqual(preferred.calls, [])
        self.assertEqual(configured.calls, [operation.pk])
        self.assertEqual(operation.srp_provider, "zeta_srp")

    def test_declined_link_is_recorded_as_skipped_with_message(self):
        self.register(FakeSRPProvider("zeta_srp", created=False))

        operation = self.start()

        operation.refresh_from_db()
        self.assertEqual(operation.status, Status.ACTIVE)
        self.assertEqual(operation.srp_error, "Provider declined to create an SRP fleet.")
        self.assertEqual(action_of(operation, "srp_link").status, ActionStatus.SKIPPED)

    def test_no_available_provider_is_recorded_as_skipped(self):
        self.register(FakeSRPProvider("zeta_srp", available=False))

        operation = self.start()

        operation.refresh_from_db()
        self.assertEqual(operation.status, Status.ACTIVE)
        self.assertEqual(operation.srp_provider, "auto")
        self.assertIn("No supported SRP provider", operation.srp_error)
        self.assertEqual(action_of(operation, "srp_link").status, ActionStatus.SKIPPED)

    def test_builtin_provider_without_srp_app_does_not_block_start(self):
        register_srp_provider(AllianceAuthBuiltinSRPProvider())

        operation = self.start()

        operation.refresh_from_db()
        self.assertEqual(operation.status, Status.ACTIVE)
        self.assertEqual(operation.srp_url, "")
        self.assertEqual(action_of(operation, "srp_link").status, ActionStatus.SKIPPED)

    def test_attendance_only_mode_never_calls_provider(self):
        provider = self.register(FakeSRPProvider("zeta_srp"))

        operation = self.start(mode="attendance_only")

        operation.refresh_from_db()
        self.assertEqual(provider.calls, [])
        self.assertEqual(operation.status, Status.ACTIVE)
        self.assertEqual(operation.srp_reference, "")
        self.assertEqual(action_of(operation, "srp_link").status, ActionStatus.SKIPPED)

    def test_disabled_auto_create_never_calls_provider(self):
        provider = self.register(FakeSRPProvider("zeta_srp"))
        f.settings(srp_auto_create=False)

        operation = self.start()

        operation.refresh_from_db()
        self.assertEqual(provider.calls, [])
        self.assertEqual(action_of(operation, "srp_link").status, ActionStatus.SKIPPED)


class RetrySRPTests(IsolatedSRPRegistryMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.fc = f.create_user("retry_fc", perms=f.FC_PERMS)
        f.settings(srp_provider="zeta_srp")

    def test_retry_creates_link_when_operation_has_none(self):
        provider = self.register(FakeSRPProvider("zeta_srp"))
        operation = f.create_operation(self.fc, srp_error="Provider was offline.")

        action = retry_srp(operation)

        operation.refresh_from_db()
        self.assertEqual(provider.calls, [operation.pk])
        self.assertEqual(action.status, ActionStatus.SUCCESS)
        self.assertEqual(operation.srp_reference, "SRP-1")
        self.assertEqual(operation.srp_url, "/srp/1/")
        self.assertEqual(operation.srp_error, "")

    def test_retry_counts_attempts_on_the_same_action(self):
        self.register(FakeSRPProvider("zeta_srp", error=RuntimeError("offline")))
        operation = f.create_operation(self.fc)

        retry_srp(operation)
        action = retry_srp(operation)

        self.assertEqual(action.attempts, 2)
        self.assertEqual(OperationAction.objects.filter(operation=operation, action="srp_link").count(), 1)

    def test_retry_exception_is_captured(self):
        self.register(FakeSRPProvider("zeta_srp", error=RuntimeError("SRP backend timeout")))
        operation = f.create_operation(self.fc)

        action = retry_srp(operation)

        operation.refresh_from_db()
        self.assertEqual(action.status, ActionStatus.FAILED)
        self.assertIn("SRP backend timeout", operation.srp_error)

    def test_retry_exception_keeps_existing_link(self):
        self.register(FakeSRPProvider("zeta_srp", error=RuntimeError("SRP backend timeout")))
        operation = f.create_operation(
            self.fc, srp_provider="zeta_srp", srp_reference="SRP-OLD", srp_url="/srp/old/"
        )

        retry_srp(operation)

        operation.refresh_from_db()
        self.assertEqual(operation.srp_reference, "SRP-OLD")
        self.assertEqual(operation.srp_url, "/srp/old/")

    def test_retry_does_not_create_duplicate_srp_fleet(self):
        provider = self.register(FakeSRPProvider("zeta_srp"))
        operation = f.create_operation(
            self.fc, srp_provider="zeta_srp", srp_reference="SRP-OLD", srp_url="/srp/old/"
        )

        retry_srp(operation)

        operation.refresh_from_db()
        self.assertEqual(provider.calls, [])
        self.assertEqual(operation.srp_reference, "SRP-OLD")

    def test_retry_without_provider_keeps_existing_link(self):
        self.register(FakeSRPProvider("zeta_srp", available=False))
        operation = f.create_operation(
            self.fc, srp_provider="zeta_srp", srp_reference="SRP-OLD", srp_url="/srp/old/"
        )

        retry_srp(operation)

        operation.refresh_from_db()
        self.assertEqual(operation.srp_reference, "SRP-OLD")
        self.assertEqual(operation.srp_url, "/srp/old/")


# ---------------------------------------------------------------------------
# Doctrines (fittings integration)
# ---------------------------------------------------------------------------


class DoctrineIntegrationTests(PatchedStartFleetMixin, TestCase):
    def form_data(self, **overrides):
        data = {
            "request_id": str(uuid.uuid4()),
            "operation_mode": "full",
            "fc_character_id": str(self.fc_char.character_id),
            "fleet_type": str(self.type_obj.pk),
            "formup": "Jita",
        }
        data.update(overrides)
        return data

    def fittings_config(self, *models_list, name="fittings", label="fittings"):
        return fake_app_config(name, label, models_list)

    def test_no_doctrines_without_fittings_app(self):
        self.assertFalse(django_apps.is_installed("fittings"))
        self.assertEqual(doctrines_module._fittings_configs(), [])
        self.assertEqual(get_doctrines(self.fc), [])

    def test_start_form_offers_only_none_custom_without_fittings(self):
        form = StartFleetForm(user=self.fc)

        self.assertEqual(list(form.fields["doctrine_choice"].choices), [("", "None / Custom")])

    def test_start_form_without_doctrine_is_none_and_custom_text_is_custom(self):
        form = StartFleetForm(self.form_data(), user=self.fc)
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["doctrine_source"], "none")
        self.assertEqual(form.cleaned_data["doctrine_name"], "")

        form = StartFleetForm(self.form_data(custom_doctrine="  Kitchen Sink  "), user=self.fc)
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["doctrine_source"], "custom")
        self.assertEqual(form.cleaned_data["doctrine_name"], "Kitchen Sink")
        self.assertEqual(form.cleaned_data["doctrine_external_id"], "")

    def test_fake_fittings_doctrine_model_is_discovered(self):
        doctrine = fake_model(
            "Doctrine",
            "fittings.doctrine",
            ["id", "name", "description"],
            rows=[(7, {"name": "shield ferox"}), (3, {"name": "Armor Zealots"}), (9, {"name": "Bombers"})],
        )

        with patch_app_configs(self.fittings_config(doctrine)):
            choices = get_doctrines(self.fc)

        self.assertEqual([c.name for c in choices], ["Armor Zealots", "Bombers", "shield ferox"])
        self.assertEqual([c.external_id for c in choices], ["3", "9", "7"])
        self.assertEqual({c.source for c in choices}, {"fittings:fittings.doctrine"})

    def test_alternative_labels_and_name_fields_are_supported(self):
        doctrine = fake_model("DoctrineModel", "doctrine.doctrinemodel", ["id", "title"], rows=[(1, {"title": "Nightmares"})])
        group = fake_model("DoctrineGroup", "fitting.doctrinegroup", ["id", "doctrine_name"], rows=[(2, {"doctrine_name": "Fleet Logi"})])

        with patch_app_configs(
            self.fittings_config(doctrine, name="fittings.doctrine", label="doctrine"),
            self.fittings_config(group, name="fittings_extra", label="fitting"),
        ):
            choices = get_doctrines()

        self.assertEqual(
            [(c.name, c.source) for c in choices],
            [("Fleet Logi", "fittings:fitting.doctrinegroup"), ("Nightmares", "fittings:doctrine.doctrinemodel")],
        )

    def test_unrecognised_layouts_fall_back_to_no_doctrines(self):
        fitting = fake_model("Fitting", "fittings.fitting", ["id", "name"], rows=[(1, {"name": "Ferox"})])
        nameless = fake_model("Doctrine", "fittings.doctrine", ["id", "icon_url"], rows=[(1, {"icon_url": "x"})])

        with patch_app_configs(self.fittings_config(fitting, nameless)):
            self.assertEqual(get_doctrines(), [])
            form = StartFleetForm(user=self.fc)

        self.assertEqual(list(form.fields["doctrine_choice"].choices), [("", "None / Custom")])

    def test_failing_doctrine_query_is_skipped(self):
        broken = fake_model("Doctrine", "fittings.doctrine", ["id", "name"], error=RuntimeError("no such table"))
        working = fake_model("DoctrineFit", "fittings.doctrinefit", ["id", "name"], rows=[(4, {"name": "Muninns"})])

        with patch_app_configs(self.fittings_config(broken, working)):
            choices = get_doctrines()

        self.assertEqual([c.name for c in choices], ["Muninns"])

    def test_doctrine_models_from_other_apps_are_ignored(self):
        doctrine = fake_model("Doctrine", "otherapp.doctrine", ["id", "name"], rows=[(1, {"name": "Other"})])

        with patch_app_configs(fake_app_config("otherapp", "otherapp", [doctrine])):
            self.assertEqual(get_doctrines(), [])

    def test_duplicate_rows_are_listed_once(self):
        doctrine = fake_model("Doctrine", "fittings.doctrine", ["id", "name"], rows=[(1, {"name": "Ferox"})])
        config = self.fittings_config(doctrine)

        with patch_app_configs(config, config):
            choices = get_doctrines()

        self.assertEqual(len(choices), 1)

    def test_selected_doctrine_is_parsed_and_snapshotted_on_operation(self):
        doctrine = fake_model("Doctrine", "fittings.doctrine", ["id", "name"], rows=[(12, {"name": "Armor | HACs"})])

        with patch_app_configs(self.fittings_config(doctrine)):
            form = StartFleetForm(
                self.form_data(doctrine_choice="fittings:fittings.doctrine|12|Armor | HACs"), user=self.fc
            )
            self.assertTrue(form.is_valid(), form.errors)

        self.assertEqual(form.cleaned_data["doctrine_source"], "fittings:fittings.doctrine")
        self.assertEqual(form.cleaned_data["doctrine_external_id"], "12")
        self.assertEqual(form.cleaned_data["doctrine_name"], "Armor | HACs")

        with mock.patch.dict(srp_module._PROVIDERS, clear=True):
            operation = start_fleet(user=self.fc, cleaned_data=form.cleaned_data, request_id=form.cleaned_data["request_id"])

        operation.refresh_from_db()
        self.assertEqual(operation.doctrine_name, "Armor | HACs")
        self.assertEqual(operation.doctrine_external_id, "12")
        self.assertEqual(operation.doctrine_source, "fittings:fittings.doctrine")

    def test_unknown_doctrine_choice_is_rejected(self):
        form = StartFleetForm(self.form_data(doctrine_choice="fittings:fittings.doctrine|99|Forged"), user=self.fc)

        self.assertFalse(form.is_valid())
        self.assertIn("doctrine_choice", form.errors)


# ---------------------------------------------------------------------------
# SDE lookups and routing
# ---------------------------------------------------------------------------


class FakeSDEApps:
    """Replacement for the app registry as seen by fleetops.services.sde."""

    def __init__(self, models_by_name=None, error=None):
        self.models_by_name = models_by_name or {}
        self.error = error

    def is_installed(self, name):
        return name == "eve_sde"

    def get_model(self, app_label, model_name):
        if self.error is not None:
            raise self.error
        return self.models_by_name[model_name]


def fake_named_model(rows=(), error=None):
    def filter_rows(pk__in):
        if error is not None:
            raise error
        wanted = set(pk__in)
        return [SimpleNamespace(pk=pk, **values) for pk, values in rows if pk in wanted]

    return SimpleNamespace(objects=SimpleNamespace(filter=filter_rows))


class FakeValuesList(list):
    def iterator(self):
        return iter(self)


class SDEFallbackTests(TestCase):
    def test_lookups_are_empty_when_eve_sde_is_not_installed(self):
        self.assertFalse(django_apps.is_installed("eve_sde"))

        self.assertEqual(sde_module.item_type_names({CAPSULE, RIFTER}), {})
        self.assertEqual(sde_module.solar_system_names({30000142}), {})
        self.assertEqual(sde_module.stargate_edges(), {})

    def test_missing_sde_models_degrade_to_empty_results(self):
        with mock.patch.object(sde_module, "apps", FakeSDEApps(error=LookupError("No model"))):
            self.assertEqual(sde_module.item_type_names({CAPSULE}), {})
            self.assertEqual(sde_module.solar_system_names({30000142}), {})
            self.assertEqual(sde_module.stargate_edges(), {})

    def test_failing_name_queries_degrade_to_empty_results(self):
        broken = fake_named_model(error=RuntimeError("relation does not exist"))
        with mock.patch.object(sde_module, "apps", FakeSDEApps({"ItemType": broken, "SolarSystem": broken})):
            self.assertEqual(sde_module.item_type_names({CAPSULE}), {})
            self.assertEqual(sde_module.solar_system_names({30000142}), {})

    def test_names_are_read_from_sde_models(self):
        item_types = fake_named_model([(CAPSULE, {"name": "Capsule"}), (RIFTER, {"name_en": "Rifter"})])
        systems = fake_named_model([(30000142, {"name": "Jita"}), (30000144, {"name": "Perimeter"})])

        with mock.patch.object(sde_module, "apps", FakeSDEApps({"ItemType": item_types, "SolarSystem": systems})):
            self.assertEqual(sde_module.item_type_names([CAPSULE, RIFTER]), {CAPSULE: "Capsule", RIFTER: "Rifter"})
            self.assertEqual(sde_module.solar_system_names({30000142}), {30000142: "Jita"})

    def test_stargate_graph_from_current_schema_is_undirected(self):
        rows = FakeValuesList([(1, 2), (2, 3), (3, 2), (4, None)])
        stargate = SimpleNamespace(
            _meta=SimpleNamespace(get_fields=lambda: [SimpleNamespace(name=n) for n in ("id", "solar_system", "destination")]),
            objects=SimpleNamespace(values_list=lambda *names: rows),
        )

        with mock.patch.object(sde_module, "apps", FakeSDEApps({"Stargate": stargate})):
            graph = sde_module.stargate_edges()

        self.assertEqual(graph, {1: {2}, 2: {1, 3}, 3: {2}})

    def test_stargate_graph_from_legacy_schema(self):
        gates = [
            SimpleNamespace(solar_system_id=10, destination_solar_system_id=11),
            SimpleNamespace(solar_system_id=11, destination_solar_system_id=12),
            SimpleNamespace(solar_system_id=12, destination_solar_system_id=None),
        ]
        stargate = SimpleNamespace(
            _meta=SimpleNamespace(
                get_fields=lambda: [SimpleNamespace(name=n) for n in ("id", "solar_system", "destination_solar_system")]
            ),
            objects=SimpleNamespace(all=lambda: gates),
        )

        with mock.patch.object(sde_module, "apps", FakeSDEApps({"Stargate": stargate})):
            graph = sde_module.stargate_edges()

        self.assertEqual(graph, {10: {11}, 11: {10, 12}, 12: {11}})

    def test_unknown_stargate_schema_yields_no_graph(self):
        stargate = SimpleNamespace(
            _meta=SimpleNamespace(get_fields=lambda: [SimpleNamespace(name=n) for n in ("id", "position")]),
            objects=SimpleNamespace(all=lambda: []),
        )

        with mock.patch.object(sde_module, "apps", FakeSDEApps({"Stargate": stargate})):
            self.assertEqual(sde_module.stargate_edges(), {})

    def test_systems_within_jumps_without_graph_returns_origin_only(self):
        self.assertEqual(systems_within_jumps(30000142, 3), {30000142: 0})
        self.assertEqual(systems_within_jumps(None, 3), {})
        self.assertEqual(systems_within_jumps(0, 3), {})


# Small stargate graph: a chain 1-2-3-4-5 with a side branch 2-6-3.
GRAPH = {1: {2}, 2: {1, 3, 6}, 3: {2, 4, 6}, 4: {3, 5}, 5: {4}, 6: {2, 3}}
SYSTEM_NAMES = {1: "Jita", 2: "Perimeter", 3: "Urlen", 4: "Sirppala", 5: "Inaro", 6: "Maurasi"}


class ProximityTests(TestCase):
    def setUp(self):
        self.fc = f.create_user("prox_fc", perms=f.FC_PERMS)
        self.fc_char = f.main_of(self.fc)
        self.operation = f.create_operation(self.fc)
        self.pilots = {name: f.create_character(name) for name in ("Alpha", "Bravo", "Charlie", "Delta", "Echo", "Foxtrot")}

    def patch_graph(self, graph=GRAPH, names=SYSTEM_NAMES):
        edges = mock.patch.object(routing_module, "stargate_edges", return_value=graph)
        system_names = mock.patch.object(
            routing_module, "solar_system_names", side_effect=lambda ids: {i: names[i] for i in ids if i in names}
        )
        edges.start()
        system_names.start()
        self.addCleanup(edges.stop)
        self.addCleanup(system_names.stop)

    def test_systems_within_jumps_uses_breadth_first_distances(self):
        self.patch_graph()

        self.assertEqual(systems_within_jumps(1, 3), {1: 0, 2: 1, 3: 2, 6: 2, 4: 3})
        self.assertEqual(systems_within_jumps(1, 1), {1: 0, 2: 1})
        self.assertEqual(systems_within_jumps(1, 0), {1: 0})

    def test_origin_outside_graph_returns_origin_only(self):
        self.patch_graph()

        self.assertEqual(systems_within_jumps(31000005, 3), {31000005: 0})

    def test_rows_group_members_within_three_jumps(self):
        self.patch_graph()
        track(self.operation, self.fc_char, system_id=1)
        track(self.operation, self.pilots["Alpha"], system_id=2)
        track(self.operation, self.pilots["Bravo"], system_id=2)
        track(self.operation, self.pilots["Charlie"], system_id=3)
        track(self.operation, self.pilots["Delta"], system_id=4)
        track(self.operation, self.pilots["Echo"], system_id=5)

        rows = proximity_rows(self.operation, 3)

        self.assertEqual([(r["system_id"], r["jump_distance"]) for r in rows], [(1, 0), (2, 1), (3, 2), (4, 3)])
        self.assertEqual([r["system_name"] for r in rows], ["Jita", "Perimeter", "Urlen", "Sirppala"])
        by_system = {r["system_id"]: sorted(m.character_name for m in r["members"]) for r in rows}
        self.assertEqual(by_system[2], ["Alpha", "Bravo"])
        self.assertNotIn(5, by_system)

    def test_inactive_members_and_other_operations_are_excluded(self):
        self.patch_graph()
        track(self.operation, self.fc_char, system_id=1)
        track(self.operation, self.pilots["Alpha"], system_id=2, active=False)
        other = f.create_operation(f.create_user(perms=f.FC_PERMS))
        track(other, self.pilots["Bravo"], system_id=2)

        rows = proximity_rows(self.operation, 3)

        self.assertEqual([r["system_id"] for r in rows], [1])

    def test_rows_at_same_distance_sort_by_system_name(self):
        self.patch_graph()
        track(self.operation, self.fc_char, system_id=2)
        track(self.operation, self.pilots["Alpha"], system_id=3)
        track(self.operation, self.pilots["Bravo"], system_id=6)
        track(self.operation, self.pilots["Charlie"], system_id=1)

        rows = proximity_rows(self.operation, 3)

        self.assertEqual([r["system_name"] for r in rows], ["Perimeter", "Jita", "Maurasi", "Urlen"])

    def test_stored_system_name_wins_and_unknown_systems_use_id(self):
        self.patch_graph(names={})
        track(self.operation, self.fc_char, system_id=1, system_name="Jita")
        track(self.operation, self.pilots["Alpha"], system_id=2)

        rows = proximity_rows(self.operation, 3)

        self.assertEqual([r["system_name"] for r in rows], ["Jita", "2"])

    def test_no_rows_without_active_fc_or_fc_system(self):
        self.patch_graph()
        track(self.operation, self.pilots["Alpha"], system_id=1)
        self.assertEqual(proximity_rows(self.operation, 3), [])

        fc_state = track(self.operation, self.fc_char, system_id=None)
        self.assertEqual(proximity_rows(self.operation, 3), [])

        fc_state.solar_system_id = 1
        fc_state.is_active = False
        fc_state.save()
        self.assertEqual(proximity_rows(self.operation, 3), [])

    def test_without_sde_only_the_fc_system_is_listed(self):
        track(self.operation, self.fc_char, system_id=30000142, system_name="Jita")
        track(self.operation, self.pilots["Alpha"], system_id=30000142, system_name="Jita")
        track(self.operation, self.pilots["Bravo"], system_id=30000144, system_name="Perimeter")

        rows = proximity_rows(self.operation, 3)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["system_id"], 30000142)
        self.assertEqual(rows[0]["jump_distance"], 0)
        self.assertEqual(len(rows[0]["members"]), 2)

    def test_operation_detail_renders_without_sde(self):
        track(self.operation, self.fc_char, system_id=30000142, ship_type_id=GUARDIAN)
        self.client.force_login(self.fc)

        response = self.client.get(reverse("fleetops:operation_detail", args=[self.operation.uuid]))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context["map_rows"]), 1)


# ---------------------------------------------------------------------------
# Fleet controls: capsule kicking
# ---------------------------------------------------------------------------


class CapsuleDetectionTests(TestCase):
    def member(self, ship_type_id=None, ship_type_name=""):
        return FleetMemberState(ship_type_id=ship_type_id, ship_type_name=ship_type_name)

    def test_known_capsule_type_ids(self):
        self.assertEqual(KNOWN_CAPSULE_TYPE_IDS, {CAPSULE, GENOLUTION_CAPSULE})
        self.assertTrue(is_capsule_member(self.member(CAPSULE)))
        self.assertTrue(is_capsule_member(self.member(GENOLUTION_CAPSULE)))

    def test_capsule_detected_by_name(self):
        self.assertTrue(is_capsule_member(self.member(999_999, "Capsule")))
        self.assertTrue(is_capsule_member(self.member(None, "  capsule ")))
        self.assertTrue(is_capsule_member(self.member(999_999, "Golden Capsule")))

    def test_ships_are_not_capsules(self):
        self.assertFalse(is_capsule_member(self.member(RIFTER, "Rifter")))
        self.assertFalse(is_capsule_member(self.member(GUARDIAN, "")))
        self.assertFalse(is_capsule_member(self.member(None, "")))
        self.assertFalse(is_capsule_member(self.member(999_999, "Capsule Hauler")))

    def test_pod_members_lists_only_active_capsules(self):
        fc = f.create_user(perms=f.FC_PERMS)
        operation = f.create_operation(fc)
        pod = track(operation, f.create_character("Podded"), ship_type_id=CAPSULE)
        track(operation, f.create_character("Left Pod"), ship_type_id=CAPSULE, active=False)
        track(operation, f.create_character("Flying"), ship_type_id=GUARDIAN)

        self.assertEqual([m.pk for m in pod_members(operation)], [pod.pk])


class KickCapsulesTests(TestCase):
    def setUp(self):
        self.fc = f.create_user("kick_fc", perms=f.FC_PERMS)
        self.fc_char = f.main_of(self.fc)
        self.operation = f.create_operation(self.fc, esi_fleet_id=FLEET_ID)
        self.fc_state = track(self.operation, self.fc_char, ship_type_id=CAPSULE, fleet_role="fleet_commander")
        self.pod_a = track(self.operation, f.create_character("Pod Alpha"), ship_type_id=CAPSULE)
        self.pod_b = track(self.operation, f.create_character("Pod Bravo"), ship_type_id=GENOLUTION_CAPSULE)
        self.flyer = track(self.operation, f.create_character("Logi Pilot"), ship_type_id=GUARDIAN)

    def patch_kick(self, **kwargs):
        patcher = mock.patch("fleetops.services.fleet_controls.kick_fleet_member", **kwargs)
        kick = patcher.start()
        self.addCleanup(patcher.stop)
        return kick

    def patch_esi_client(self):
        fleets = mock.Mock()
        fleets.DeleteFleetsFleetIdMembersMemberId.return_value.result.return_value = None
        patcher = mock.patch.object(esi_provider, "esi", SimpleNamespace(client=SimpleNamespace(Fleets=fleets)))
        patcher.start()
        self.addCleanup(patcher.stop)
        return fleets

    def test_kicks_every_capsule_except_the_fc(self):
        kick = self.patch_kick()

        result = kick_all_pods(self.operation, actor=self.fc)

        kicked_ids = [c.args[3] for c in kick.call_args_list]
        self.assertCountEqual(kicked_ids, [self.pod_a.character_id, self.pod_b.character_id])
        self.assertNotIn(self.fc_char.character_id, kicked_ids)
        for call in kick.call_args_list:
            self.assertEqual(call.args[:3], (self.fc, self.fc_char.character_id, FLEET_ID))
        self.assertEqual(result["target_count"], 2)
        self.assertCountEqual(result["kicked"], ["Pod Alpha", "Pod Bravo"])
        self.assertEqual(result["failures"], [])

    def test_successful_kick_records_action_and_audit(self):
        self.patch_kick()

        kick_all_pods(self.operation, actor=self.fc)

        action = action_of(self.operation, "kick_capsules")
        self.assertEqual(action.status, ActionStatus.SUCCESS)
        self.assertIn("Kicked 2", action.error_message)
        entry = AuditLog.objects.get(action="fleet.kick_capsules")
        self.assertEqual(entry.actor, self.fc)
        self.assertEqual(entry.object_type, "FleetOperation")
        self.assertEqual(entry.object_id, str(self.operation.pk))
        self.assertCountEqual(entry.new_value["kicked"], ["Pod Alpha", "Pod Bravo"])
        self.assertEqual(entry.new_value["failures"], [])

    def test_per_member_failures_are_reported(self):
        failing_id = self.pod_b.character_id

        def kick(user, character_id, fleet_id, member_id):
            if member_id == failing_id:
                raise FleetESIError("MEMBER_NOT_FOUND", f"Fleet member {member_id} was not found.", 404)

        self.patch_kick(side_effect=kick)

        result = kick_all_pods(self.operation, actor=self.fc)

        self.assertEqual(result["kicked"], ["Pod Alpha"])
        self.assertEqual(result["failures"], [{"character": "Pod Bravo", "error": f"Fleet member {failing_id} was not found."}])
        action = action_of(self.operation, "kick_capsules")
        self.assertEqual(action.status, ActionStatus.FAILED)
        self.assertIn("1 failed", action.error_message)
        entry = AuditLog.objects.get(action="fleet.kick_capsules")
        self.assertEqual(entry.new_value["failures"][0]["character"], "Pod Bravo")

    def test_no_capsules_is_a_successful_no_op(self):
        FleetMemberState.objects.filter(pk__in=[self.pod_a.pk, self.pod_b.pk]).update(ship_type_id=GUARDIAN)
        kick = self.patch_kick()

        result = kick_all_pods(self.operation, actor=self.fc)

        kick.assert_not_called()
        self.assertEqual(result, {"kicked": [], "failures": [], "target_count": 0})
        self.assertEqual(action_of(self.operation, "kick_capsules").status, ActionStatus.SUCCESS)

    def test_repeated_kick_increments_attempts(self):
        self.patch_kick()

        kick_all_pods(self.operation, actor=self.fc)
        kick_all_pods(self.operation, actor=self.fc)

        self.assertEqual(action_of(self.operation, "kick_capsules").attempts, 2)
        self.assertEqual(AuditLog.objects.filter(action="fleet.kick_capsules").count(), 2)

    def test_operation_without_esi_fleet_id_raises(self):
        manual = f.create_operation(self.fc, status=Status.CLOSED, is_manual=True)
        FleetOperation.objects.filter(pk=manual.pk).update(esi_fleet_id=None)
        manual.refresh_from_db()
        kick = self.patch_kick()

        with self.assertRaises(ValueError):
            kick_all_pods(manual, actor=self.fc)

        kick.assert_not_called()
        self.assertFalse(OperationAction.objects.filter(operation=manual, action="kick_capsules").exists())
        self.assertFalse(AuditLog.objects.filter(action="fleet.kick_capsules").exists())

    def test_kick_uses_fleet_write_token(self):
        token = f.add_token(self.fc, self.fc_char)
        fleets = self.patch_esi_client()

        result = kick_all_pods(self.operation, actor=self.fc)

        self.assertEqual(result["failures"], [])
        calls = fleets.DeleteFleetsFleetIdMembersMemberId.call_args_list
        self.assertCountEqual(
            [c.kwargs["member_id"] for c in calls], [self.pod_a.character_id, self.pod_b.character_id]
        )
        for call in calls:
            self.assertEqual(call.kwargs["fleet_id"], FLEET_ID)
            self.assertEqual(call.kwargs["token"], token)

    def test_missing_write_scope_fails_each_kick_without_calling_esi(self):
        f.add_token(self.fc, self.fc_char, scopes=[FLEET_READ_SCOPE])
        fleets = self.patch_esi_client()

        result = kick_all_pods(self.operation, actor=self.fc)

        fleets.DeleteFleetsFleetIdMembersMemberId.assert_not_called()
        self.assertEqual(result["kicked"], [])
        self.assertEqual(len(result["failures"]), 2)
        self.assertIn("No valid ESI token", result["failures"][0]["error"])
        self.assertEqual(action_of(self.operation, "kick_capsules").status, ActionStatus.FAILED)

    def test_esi_forbidden_is_reported_per_member(self):
        f.add_token(self.fc, self.fc_char)
        fleets = self.patch_esi_client()
        fleets.DeleteFleetsFleetIdMembersMemberId.return_value.result.side_effect = HTTPClientError(
            status_code=403, headers={}, data=None
        )

        result = kick_all_pods(self.operation, actor=self.fc)

        self.assertEqual(len(result["failures"]), 2)
        self.assertIn("denied permission", result["failures"][0]["error"])

    def test_view_kicks_capsules_on_active_fleet(self):
        kick = self.patch_kick()
        self.client.force_login(self.fc)

        response = self.client.post(reverse("fleetops:kick_capsules", args=[self.operation.uuid]))

        self.assertRedirects(
            response, reverse("fleetops:operation_detail", args=[self.operation.uuid]), fetch_redirect_response=False
        )
        self.assertEqual(kick.call_count, 2)

    def test_view_refuses_closed_fleet(self):
        FleetOperation.objects.filter(pk=self.operation.pk).update(status=Status.CLOSED, ended_at=timezone.now())
        kick = self.patch_kick()
        self.client.force_login(self.fc)

        response = self.client.post(reverse("fleetops:kick_capsules", args=[self.operation.uuid]))

        self.assertEqual(response.status_code, 404)
        kick.assert_not_called()

    def test_view_requires_fleet_management_permission(self):
        kick = self.patch_kick()
        member = f.create_user(perms=f.MEMBER_PERMS)
        self.client.force_login(member)

        response = self.client.post(reverse("fleetops:kick_capsules", args=[self.operation.uuid]))

        self.assertEqual(response.status_code, 403)
        kick.assert_not_called()

    def test_view_hides_other_fcs_fleet(self):
        kick = self.patch_kick()
        other_fc = f.create_user(perms=f.FC_PERMS)
        self.client.force_login(other_fc)

        response = self.client.post(reverse("fleetops:kick_capsules", args=[self.operation.uuid]))

        self.assertEqual(response.status_code, 404)
        kick.assert_not_called()


# ---------------------------------------------------------------------------
# Special roles and FC credit
# ---------------------------------------------------------------------------


def remove_perm(user, codename):
    user.user_permissions.remove(Permission.objects.get(codename=codename, content_type__app_label="fleetops"))
    return User.objects.get(pk=user.pk)


class RoleAssignmentServiceTests(TestCase):
    def setUp(self):
        self.fc = f.create_user("role_fc", perms=f.FC_PERMS)
        self.operation = f.create_operation(self.fc, status=Status.CLOSED, started_at=STATS_START)
        self.co_fc = f.create_user("co_fc", perms=f.MEMBER_PERMS + ["fleetops.start_fleet"])
        self.member = f.create_user("line_member", perms=f.MEMBER_PERMS)

    def test_assignee_with_start_fleet_gets_fc_credit_snapshot(self):
        main = f.main_of(self.co_fc)

        assignment = add_role_assignment(
            self.operation, actor=self.fc, role=Role.BACKSEAT_FC, character_id=main.character_id, notes="Backup"
        )

        self.assertTrue(assignment.grants_fc_credit)
        self.assertEqual(assignment.auth_user, self.co_fc)
        self.assertEqual(assignment.character_name, main.character_name)
        self.assertEqual(assignment.main_character_id, main.character_id)
        self.assertEqual(assignment.assigned_by, self.fc)
        self.assertEqual(assignment.notes, "Backup")

    def test_assignee_without_start_fleet_gets_no_credit(self):
        assignment = add_role_assignment(
            self.operation, actor=self.fc, role=Role.LOGI_ANCHOR, character_id=f.main_of(self.member).character_id
        )

        self.assertFalse(assignment.grants_fc_credit)
        self.assertEqual(assignment.auth_user, self.member)

    def test_alt_in_other_corporation_is_attributed_to_main(self):
        alt = f.add_alt(self.co_fc, "Co FC Alt", corporation=f.OTHER_CORP, alliance=f.FOREIGN_ALLIANCE)

        assignment = add_role_assignment(self.operation, actor=self.fc, role=Role.SNOWFLAKE, character_id=alt.character_id)

        main = f.main_of(self.co_fc)
        self.assertEqual(assignment.character_name, "Co FC Alt")
        self.assertEqual(assignment.main_character_id, main.character_id)
        self.assertEqual(assignment.main_character_name, main.character_name)
        self.assertEqual(assignment.corporation_id, f.DEFAULT_CORP[0])
        self.assertTrue(assignment.grants_fc_credit)

    def test_unowned_character_gets_no_user_and_no_credit(self):
        stranger = f.create_character("Neutral Pilot", corporation=f.OTHER_CORP)

        assignment = add_role_assignment(self.operation, actor=self.fc, role=Role.SNOWFLAKE, character_id=stranger.character_id)
        unknown = add_role_assignment(self.operation, actor=self.fc, role=Role.SNOWFLAKE, character_id=1_999_999_999)

        self.assertIsNone(assignment.auth_user)
        self.assertFalse(assignment.grants_fc_credit)
        self.assertEqual(assignment.character_name, "Neutral Pilot")
        self.assertEqual(assignment.corporation_id, f.OTHER_CORP[0])
        self.assertEqual(unknown.character_name, "Character 1999999999")
        self.assertIsNone(unknown.auth_user)

    def test_create_is_audited(self):
        main = f.main_of(self.co_fc)

        assignment = add_role_assignment(self.operation, actor=self.fc, role=Role.BACKSEAT_FC, character_id=main.character_id)

        entry = AuditLog.objects.get(action="fleet.role_create")
        self.assertEqual(entry.actor, self.fc)
        self.assertEqual(entry.object_type, "OperationRoleAssignment")
        self.assertEqual(entry.object_id, str(assignment.pk))
        self.assertIsNone(entry.old_value)
        self.assertEqual(
            entry.new_value, {"role": "backseat_fc", "character_id": main.character_id, "grants_fc_credit": True}
        )

    def test_reassigning_same_role_updates_existing_row(self):
        main = f.main_of(self.member)
        manager = f.create_user(perms=f.FC_LEAD_PERMS)
        first = add_role_assignment(self.operation, actor=self.fc, role=Role.LOGI_ANCHOR, character_id=main.character_id, notes="First")

        second = add_role_assignment(
            self.operation, actor=manager, role=Role.LOGI_ANCHOR, character_id=main.character_id, notes="Second"
        )

        self.assertEqual(first.pk, second.pk)
        self.assertEqual(OperationRoleAssignment.objects.filter(operation=self.operation).count(), 1)
        second.refresh_from_db()
        self.assertEqual(second.notes, "Second")
        self.assertEqual(second.assigned_by, manager)
        self.assertEqual(
            list(AuditLog.objects.order_by("pk").values_list("action", flat=True)),
            ["fleet.role_create", "fleet.role_update"],
        )

    def test_different_roles_for_same_character_are_separate(self):
        main = f.main_of(self.co_fc)

        add_role_assignment(self.operation, actor=self.fc, role=Role.BACKSEAT_FC, character_id=main.character_id)
        add_role_assignment(self.operation, actor=self.fc, role=Role.LOGI_ANCHOR, character_id=main.character_id)

        self.assertEqual(
            sorted(self.operation.role_assignments.values_list("role", flat=True)), ["backseat_fc", "logi_anchor"]
        )

    def test_credit_snapshot_survives_permission_removal(self):
        main = f.main_of(self.co_fc)
        assignment = add_role_assignment(self.operation, actor=self.fc, role=Role.BACKSEAT_FC, character_id=main.character_id)

        co_fc = remove_perm(self.co_fc, "start_fleet")

        assignment.refresh_from_db()
        self.assertFalse(co_fc.has_perm("fleetops.start_fleet"))
        self.assertTrue(assignment.grants_fc_credit)
        self.assertEqual(fc_statistics(co_fc, STATS_YEAR, STATS_MONTH)["fleet_count"], 1)

    def test_later_permission_grant_is_not_retroactive(self):
        main = f.main_of(self.member)
        assignment = add_role_assignment(self.operation, actor=self.fc, role=Role.LOGI_ANCHOR, character_id=main.character_id)

        AuthUtils.add_permissions_to_user_by_name(["fleetops.start_fleet"], self.member)

        assignment.refresh_from_db()
        self.assertFalse(assignment.grants_fc_credit)
        self.assertEqual(fc_statistics(self.member, STATS_YEAR, STATS_MONTH)["fleet_count"], 0)

    def test_delete_is_audited_and_removes_assignment(self):
        main = f.main_of(self.co_fc)
        assignment = add_role_assignment(self.operation, actor=self.fc, role=Role.BACKSEAT_FC, character_id=main.character_id)
        pk = assignment.pk

        delete_role_assignment(assignment, actor=self.fc)

        self.assertFalse(OperationRoleAssignment.objects.filter(pk=pk).exists())
        entry = AuditLog.objects.get(action="fleet.role_delete")
        self.assertEqual(entry.actor, self.fc)
        self.assertEqual(entry.object_id, str(pk))
        self.assertEqual(
            entry.old_value, {"role": "backseat_fc", "character_id": main.character_id, "grants_fc_credit": True}
        )
        self.assertIsNone(entry.new_value)

    def test_credit_removal_drops_fleet_from_fc_statistics(self):
        main = f.main_of(self.co_fc)
        assignment = add_role_assignment(self.operation, actor=self.fc, role=Role.BACKSEAT_FC, character_id=main.character_id)
        before = fc_statistics(self.co_fc, STATS_YEAR, STATS_MONTH)

        delete_role_assignment(assignment, actor=self.fc)

        after = fc_statistics(self.co_fc, STATS_YEAR, STATS_MONTH)
        self.assertEqual(before["fleet_count"], 1)
        self.assertEqual(before["total_points"], 1.0)
        self.assertEqual(after["fleet_count"], 0)
        self.assertEqual(after["total_points"], 0)
        self.assertEqual(fc_statistics(self.fc, STATS_YEAR, STATS_MONTH)["fleet_count"], 1)

    def test_user_counted_once_with_several_credit_sources(self):
        alt = f.add_alt(self.co_fc, "Co FC Logi Alt")
        add_role_assignment(self.operation, actor=self.fc, role=Role.BACKSEAT_FC, character_id=f.main_of(self.co_fc).character_id)
        add_role_assignment(self.operation, actor=self.fc, role=Role.LOGI_ANCHOR, character_id=alt.character_id)
        add_role_assignment(self.operation, actor=self.fc, role=Role.SNOWFLAKE, character_id=f.main_of(self.fc).character_id)

        self.assertEqual(fc_statistics(self.co_fc, STATS_YEAR, STATS_MONTH)["fleet_count"], 1)
        self.assertEqual(fc_operations_queryset(self.co_fc, STATS_YEAR, STATS_MONTH).count(), 1)
        self.assertEqual(fc_statistics(self.fc, STATS_YEAR, STATS_MONTH)["fleet_count"], 1)
        self.assertEqual(fc_statistics(self.fc, STATS_YEAR, STATS_MONTH)["total_points"], 1.0)

    def test_assignment_without_credit_does_not_count_for_fc_statistics(self):
        add_role_assignment(self.operation, actor=self.fc, role=Role.LOGI_ANCHOR, character_id=f.main_of(self.member).character_id)

        self.assertEqual(fc_statistics(self.member, STATS_YEAR, STATS_MONTH)["fleet_count"], 0)


class RoleAssignmentViewTests(TestCase):
    def setUp(self):
        self.fc = f.create_user("view_role_fc", perms=f.FC_PERMS)
        self.operation = f.create_operation(self.fc)
        self.co_fc = f.create_user("view_co_fc", perms=f.FC_PERMS)
        self.co_fc_main = f.main_of(self.co_fc)
        self.left_pilot = f.create_character("Left Early")
        track(self.operation, f.main_of(self.fc), system_id=30000142)
        track(self.operation, self.co_fc_main, system_id=30000142)
        track(self.operation, self.left_pilot, system_id=30000142, active=False)

    def add_url(self, operation=None):
        return reverse("fleetops:add_operation_role", args=[(operation or self.operation).uuid])

    def post_role(self, user, character_id, role=Role.BACKSEAT_FC, operation=None):
        self.client.force_login(user)
        return self.client.post(
            self.add_url(operation), {"role": role, "character_id": str(character_id), "notes": "Assigned in fleet"}
        )

    def test_form_offers_all_historically_tracked_members(self):
        form = OperationRoleAssignmentForm(operation=self.operation)

        offered = {int(value) for value, _ in form.fields["character_id"].choices}
        self.assertEqual(offered, {f.main_of(self.fc).character_id, self.co_fc_main.character_id, self.left_pilot.character_id})

    def test_fc_assigns_role_and_grants_credit(self):
        response = self.post_role(self.fc, self.co_fc_main.character_id)

        self.assertEqual(response.status_code, 302)
        assignment = OperationRoleAssignment.objects.get(operation=self.operation)
        self.assertEqual(assignment.auth_user, self.co_fc)
        self.assertTrue(assignment.grants_fc_credit)

    def test_member_who_left_can_still_be_assigned(self):
        self.post_role(self.fc, self.left_pilot.character_id, role=Role.SNOWFLAKE)

        assignment = OperationRoleAssignment.objects.get(operation=self.operation)
        self.assertEqual(assignment.character_id, self.left_pilot.character_id)
        self.assertFalse(assignment.grants_fc_credit)

    def test_untracked_character_is_rejected(self):
        outsider = f.create_character("Never In Fleet")

        response = self.post_role(self.fc, outsider.character_id)

        self.assertEqual(response.status_code, 302)
        self.assertFalse(OperationRoleAssignment.objects.exists())

    def test_roles_can_be_added_to_closed_fleets(self):
        FleetOperation.objects.filter(pk=self.operation.pk).update(
            status=Status.CLOSED, ended_at=timezone.now(), tracking_enabled=False
        )

        self.post_role(self.fc, self.co_fc_main.character_id)

        self.assertTrue(OperationRoleAssignment.objects.filter(operation=self.operation, grants_fc_credit=True).exists())

    def test_credited_co_fc_with_manage_own_fleet_can_manage(self):
        add_role_assignment(self.operation, actor=self.fc, role=Role.BACKSEAT_FC, character_id=self.co_fc_main.character_id)

        response = self.post_role(self.co_fc, self.left_pilot.character_id, role=Role.LOGI_ANCHOR)

        self.assertEqual(response.status_code, 302)
        self.assertTrue(
            OperationRoleAssignment.objects.filter(operation=self.operation, role=Role.LOGI_ANCHOR).exists()
        )

    def test_credited_user_without_manage_own_fleet_is_forbidden(self):
        limited = f.create_user("limited_co_fc", perms=f.MEMBER_PERMS + ["fleetops.start_fleet"])
        track(self.operation, f.main_of(limited), system_id=30000142)
        add_role_assignment(self.operation, actor=self.fc, role=Role.BACKSEAT_FC, character_id=f.main_of(limited).character_id)

        response = self.post_role(limited, self.left_pilot.character_id, role=Role.SNOWFLAKE)

        self.assertEqual(response.status_code, 403)
        self.assertFalse(OperationRoleAssignment.objects.filter(role=Role.SNOWFLAKE).exists())

    def test_uncredited_fc_cannot_manage_other_fleet(self):
        response = self.post_role(self.co_fc, self.left_pilot.character_id, role=Role.SNOWFLAKE)

        self.assertEqual(response.status_code, 404)
        self.assertFalse(OperationRoleAssignment.objects.exists())

    def test_delete_view_removes_assignment_and_audits(self):
        assignment = add_role_assignment(
            self.operation, actor=self.fc, role=Role.BACKSEAT_FC, character_id=self.co_fc_main.character_id
        )
        self.client.force_login(self.fc)

        response = self.client.post(reverse("fleetops:delete_operation_role", args=[assignment.pk]))

        self.assertEqual(response.status_code, 302)
        self.assertFalse(OperationRoleAssignment.objects.filter(pk=assignment.pk).exists())
        self.assertTrue(AuditLog.objects.filter(action="fleet.role_delete", actor=self.fc).exists())

    def test_delete_view_hides_assignments_of_unmanaged_fleets(self):
        assignment = add_role_assignment(
            self.operation, actor=self.fc, role=Role.SNOWFLAKE, character_id=self.left_pilot.character_id
        )
        self.client.force_login(self.co_fc)

        response = self.client.post(reverse("fleetops:delete_operation_role", args=[assignment.pk]))

        self.assertEqual(response.status_code, 404)
        self.assertTrue(OperationRoleAssignment.objects.filter(pk=assignment.pk).exists())
