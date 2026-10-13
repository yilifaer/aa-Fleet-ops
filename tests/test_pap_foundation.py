"""Models, defaults and ESI lookups the PAP rules build on."""

from decimal import Decimal
from io import StringIO
from types import SimpleNamespace
from unittest import mock

from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db import IntegrityError, transaction
from django.test import TestCase

from fleetops.constants import CLICK_SCOPES
from fleetops.models import (
    MAX_MULTIPLIER,
    AttendanceRecord,
    FleetOperation,
    FleetOpsSettings,
    FleetStartPreset,
    MultiplierTier,
    OperationRoleAssignment,
    PapRegistration,
    SpecialRole,
)
from fleetops.providers import esi as esi_provider
from fleetops.providers.esi import FleetESIError, resolve_names_to_ids, universe_ids
from fleetops.services import tracking
from fleetops.services.attendance import create_manual_attendance

from . import factories as f
from .test_operations_tracking import http_error

Status = FleetOperation.Status


def run_seed(*args):
    call_command("fleetops_seed", *args, stdout=StringIO())


class SettingsDefaultsTests(TestCase):
    def test_new_settings_keep_the_earlier_behaviour(self):
        settings = FleetOpsSettings.get_solo()

        self.assertEqual(settings.multiplier_mode, FleetOpsSettings.MultiplierMode.FC)
        self.assertEqual(settings.pap_minimum_minutes, 0)
        self.assertEqual(settings.minimum_fleet_minutes, 0)
        self.assertEqual(settings.pap_link_minutes, 30)
        self.assertEqual(FleetOpsSettings._meta.get_field("attendance_limit").verbose_name, "PAP characters per player")

    def test_limits_are_validated(self):
        for field, value in (
            ("pap_minimum_minutes", 1441),
            ("minimum_fleet_minutes", 1441),
            ("pap_link_minutes", 0),
            ("pap_link_minutes", 1441),
            ("multiplier_mode", "sometimes"),
        ):
            with self.subTest(field=field, value=value):
                settings = FleetOpsSettings.get_solo()
                setattr(settings, field, value)
                with self.assertRaises(ValidationError) as ctx:
                    settings.full_clean()
                self.assertIn(field, ctx.exception.message_dict)


class MultiplierModelTests(TestCase):
    def test_tiers_are_ordered_by_threshold(self):
        f.multiplier_tiers((180, 3), (60, 2), (240, 4))

        self.assertEqual(list(MultiplierTier.objects.values_list("threshold_minutes", flat=True)), [60, 180, 240])
        self.assertEqual(str(MultiplierTier.objects.first()), "2x after 60 min")

    def test_tier_bounds(self):
        for threshold, multiplier, field in (
            (0, 2, "threshold_minutes"),
            (10081, 2, "threshold_minutes"),
            (60, 1, "multiplier"),
            (60, MAX_MULTIPLIER + 1, "multiplier"),
        ):
            with self.subTest(threshold=threshold, multiplier=multiplier):
                with self.assertRaises(ValidationError) as ctx:
                    MultiplierTier(threshold_minutes=threshold, multiplier=multiplier).full_clean()
                self.assertIn(field, ctx.exception.message_dict)
        MultiplierTier(threshold_minutes=10080, multiplier=MAX_MULTIPLIER).full_clean()

    def test_thresholds_are_unique(self):
        f.multiplier_tiers((90, 2))

        with self.assertRaises(IntegrityError), transaction.atomic():
            MultiplierTier.objects.create(threshold_minutes=90, multiplier=3)

    def test_fleet_multipliers_go_up_to_the_maximum(self):
        operation = f.create_operation(f.create_user(perms=f.FC_PERMS))
        for field in ("attendance_multiplier", "end_requested_multiplier"):
            with self.subTest(field=field):
                setattr(operation, field, MAX_MULTIPLIER)
                operation.full_clean()
                setattr(operation, field, MAX_MULTIPLIER + 1)
                with self.assertRaises(ValidationError) as ctx:
                    operation.full_clean()
                self.assertIn(field, ctx.exception.message_dict)
                setattr(operation, field, 1)

    def test_fc_points_are_the_weight_times_the_multiplier(self):
        fc = f.create_user(perms=f.FC_PERMS)
        operation = f.create_operation(fc, type_obj=f.fleet_type("CTA", "1.50"), attendance_multiplier=3)

        self.assertEqual(operation.fc_points, Decimal("4.50"))
        operation.attendance_multiplier = 1
        self.assertEqual(operation.fc_points, Decimal("1.50"))
        operation.fleet_point_weight_snapshot = None
        self.assertEqual(operation.fc_points, Decimal("0"))

    def test_new_fleets_are_approved_counted_and_use_time_in_fleet(self):
        operation = f.create_operation(f.create_user(perms=f.FC_PERMS))

        self.assertEqual(operation.pap_rules, FleetOperation.PapRules.TIME_IN_FLEET)
        self.assertEqual(operation.review_status, "approved")
        self.assertEqual(operation.stats_mode, FleetOperation.StatsMode.AUTO)
        self.assertTrue(operation.counts_for_stats)
        self.assertFalse(operation.multiplier_pending)
        self.assertEqual(operation.multiplier_source, "")
        self.assertIsNone(operation.pap_minimum_minutes)
        self.assertIsNone(operation.pap_character_limit)


class SpecialRoleModelTests(TestCase):
    def test_key_is_made_from_the_name_once(self):
        role = SpecialRole.objects.create(name="Fleet Scout")
        self.assertEqual(role.key, "fleet_scout")

        role.name = "Scout"
        role.save()
        role.refresh_from_db()
        self.assertEqual((role.key, role.name), ("fleet_scout", "Scout"))

    def test_taken_keys_get_a_number(self):
        first = SpecialRole.objects.create(name="Cyno Alt")
        second = SpecialRole.objects.create(name="Cyno-Alt")
        third = SpecialRole.objects.create(name="Cyno  Alt")

        self.assertEqual([first.key, second.key, third.key], ["cyno_alt", "cyno_alt_2", "cyno_alt_3"])

    def test_long_names_keep_keys_within_thirty_characters(self):
        name = "Very Long Special Role Name For Testing"
        first = SpecialRole.objects.create(name=name)
        second = SpecialRole.objects.create(name=f"{name}!")

        self.assertEqual(first.key, "very_long_special_role_name_fo")
        self.assertEqual(second.key, "very_long_special_role_name_2")
        self.assertLessEqual(len(second.key), 30)

    def test_names_without_latin_letters_get_a_generic_key(self):
        first = SpecialRole.objects.create(name="后排指挥")
        second = SpecialRole.objects.create(name="侦察")

        self.assertEqual((first.key, second.key), ("role", "role_2"))

    def test_seeded_key_is_kept(self):
        self.assertEqual(SpecialRole.objects.get(name="Back Seat FC").key, "backseat_fc")

    def test_assignment_label_follows_the_role(self):
        fc = f.create_user(perms=f.FC_PERMS)
        operation = f.create_operation(fc, status=Status.CLOSED)
        main = f.main_of(fc)
        role = f.special_role("Cloaky Eyes")
        linked = OperationRoleAssignment.objects.create(
            operation=operation, role=role.key, special_role=role, character_id=main.character_id, character_name="Eyes"
        )
        legacy = OperationRoleAssignment.objects.create(
            operation=operation, role="logi_anchor", character_id=main.character_id, character_name="Anchor"
        )
        unknown = OperationRoleAssignment.objects.create(
            operation=operation, role="mystery", character_id=main.character_id, character_name="Who"
        )

        self.assertEqual(str(linked), "Cloaky Eyes: Eyes")
        self.assertEqual(linked.get_role_display(), "Cloaky Eyes")
        # Assignments made by key alone are linked to the role with that key.
        self.assertEqual(legacy.special_role, SpecialRole.objects.get(key="logi_anchor"))
        self.assertEqual(legacy.role_label, "Logi Anchor")
        self.assertIsNone(unknown.special_role)
        self.assertEqual(unknown.role_label, "mystery")
        role.name = "Eyes On"
        role.save()
        linked.refresh_from_db()
        self.assertEqual(linked.role_label, "Eyes On")

    def test_roles_in_use_cannot_be_deleted(self):
        from django.db.models import ProtectedError

        fc = f.create_user(perms=f.FC_PERMS)
        role = f.special_role(key="backseat_fc", grants_fc_credit=True)
        OperationRoleAssignment.objects.create(
            operation=f.create_operation(fc),
            role=role.key,
            special_role=role,
            character_id=f.main_of(fc).character_id,
            character_name="Backseat",
        )

        with self.assertRaises(ProtectedError):
            role.delete()

    def test_factory_updates_a_seeded_role(self):
        role = f.special_role(key="logi_anchor", grants_fc_credit=True, sort_order=3)

        self.assertEqual((role.name, role.grants_fc_credit, role.sort_order), ("Logi Anchor", True, 3))
        self.assertEqual(SpecialRole.objects.filter(key="logi_anchor").count(), 1)


class StartPresetModelTests(TestCase):
    def test_one_default_per_owner(self):
        fc = f.create_user(perms=f.FC_PERMS)
        other = f.create_user(perms=f.FC_PERMS)
        first = FleetStartPreset.objects.create(owner=fc, name="Roam", is_default=True)
        others_default = FleetStartPreset.objects.create(owner=other, name="Roam", is_default=True)
        second = FleetStartPreset.objects.create(owner=fc, name="Home defence", is_default=True)

        first.refresh_from_db()
        others_default.refresh_from_db()
        self.assertFalse(first.is_default)
        self.assertTrue(second.is_default)
        self.assertTrue(others_default.is_default)

        first.is_default = True
        first.save()
        second.refresh_from_db()
        self.assertFalse(second.is_default)

    def test_names_are_unique_per_owner(self):
        fc = f.create_user(perms=f.FC_PERMS)
        last_used = FleetStartPreset.objects.create(owner=fc, is_last_used=True, values={"fields": {"formup": "Jita"}})

        self.assertEqual(str(last_used), "Last used settings")
        with self.assertRaises(IntegrityError), transaction.atomic():
            FleetStartPreset.objects.create(owner=fc, name="")


class PapLinkModelTests(TestCase):
    def test_a_character_registers_once_per_fleet(self):
        fc = f.create_user(perms=f.FC_PERMS)
        operation = f.create_operation(fc)
        link = f.pap_link(operation)
        pilot = f.create_user()
        registration = f.registration(operation, f.main_of(pilot), link=link)

        self.assertEqual(registration.auth_user, pilot)
        self.assertTrue(link.require_online)
        self.assertGreater(link.expires_at, link.created_at)
        self.assertEqual(list(link.registrations.all()), [registration])
        with self.assertRaises(IntegrityError), transaction.atomic():
            f.registration(operation, f.main_of(pilot), link=f.pap_link(operation))
        f.registration(f.create_operation(fc), f.main_of(pilot))
        self.assertEqual(PapRegistration.objects.count(), 2)

    def test_click_scopes_match_afat_click_tracking(self):
        self.assertEqual(
            sorted(CLICK_SCOPES),
            ["esi-location.read_location.v1", "esi-location.read_online.v1", "esi-location.read_ship_type.v1"],
        )

    def test_member_state_factory_uses_the_characters_own_corporation(self):
        # Member states show where a character is; statistics stay on the main's corporation.
        fc = f.create_user(perms=f.FC_PERMS)
        pilot = f.create_user(corporation=f.DEFAULT_CORP)
        alt = f.add_alt(pilot, "Foreign Alt", corporation=f.OTHER_CORP, alliance=f.FOREIGN_ALLIANCE)
        operation = f.create_operation(fc)

        state = f.member_state(operation, alt, accumulated_seconds=600)
        outsider = f.member_state(operation, 91_555_001, active=False)

        self.assertEqual((state.auth_user, state.main_character_id), (pilot, f.main_of(pilot).character_id))
        self.assertEqual((state.corporation_id, state.alliance_id), (f.OTHER_CORP[0], f.FOREIGN_ALLIANCE[0]))
        self.assertEqual(state.accumulated_seconds, 600)
        self.assertIsNone(outsider.auth_user)
        self.assertFalse(outsider.is_active)
        self.assertIsNotNone(outsider.left_at)
        # The alt's PAP row is credited to the main and the main's corporation.
        row = f.add_attendance(operation, pilot, alt)
        self.assertEqual((row.character_id, row.main_character_id), (alt.character_id, f.main_of(pilot).character_id))
        self.assertEqual(row.corporation_id, f.DEFAULT_CORP[0])


class SeedCommandTests(TestCase):
    def test_missing_defaults_are_created(self):
        MultiplierTier.objects.all().delete()
        SpecialRole.objects.all().delete()

        run_seed()

        self.assertEqual(list(MultiplierTier.objects.values_list("threshold_minutes", "multiplier")), [(90, 2), (180, 3)])
        self.assertEqual(
            list(SpecialRole.objects.values_list("key", "name", "grants_fc_credit")),
            [
                ("backseat_fc", "Back Seat FC", True),
                ("logi_anchor", "Logi Anchor", False),
                ("snowflake", "Snowflake Member", False),
            ],
        )

    def test_configured_tiers_and_roles_are_kept(self):
        f.multiplier_tiers((120, 2))
        SpecialRole.objects.filter(key="backseat_fc").update(name="Backseat", grants_fc_credit=False)
        SpecialRole.objects.filter(key="snowflake").delete()
        SpecialRole.objects.create(key="custom_snowflake", name="Snowflake Member")

        run_seed()
        run_seed("--demo")

        self.assertEqual(list(MultiplierTier.objects.values_list("threshold_minutes", "multiplier")), [(120, 2)])
        self.assertEqual(
            dict(SpecialRole.objects.values_list("key", "name")),
            {"backseat_fc": "Backseat", "logi_anchor": "Logi Anchor", "custom_snowflake": "Snowflake Member"},
        )
        self.assertFalse(SpecialRole.objects.get(key="backseat_fc").grants_fc_credit)


class ManualAttendanceSourceTests(TestCase):
    def setUp(self):
        self.fc = f.create_user(perms=f.FC_PERMS)
        self.pilot = f.create_user()
        self.operation = f.create_operation(self.fc, status=Status.CLOSED)

    def test_imported_rows_keep_their_source(self):
        row = create_manual_attendance(
            self.operation,
            actor=self.fc,
            character_id=f.main_of(self.pilot).character_id,
            source=AttendanceRecord.Source.IMPORTED,
        )

        self.assertEqual(row.source, AttendanceRecord.Source.IMPORTED)

    def test_manual_is_the_default_source(self):
        row = create_manual_attendance(self.operation, actor=self.fc, character_id=f.main_of(self.pilot).character_id)

        self.assertEqual(row.source, AttendanceRecord.Source.MANUAL)

    def test_alt_of_another_corporation_is_credited_to_the_main(self):
        main = f.main_of(self.pilot)
        alt = f.add_alt(self.pilot, "Foreign Corp Alt", corporation=f.OTHER_CORP, alliance=f.FOREIGN_ALLIANCE)

        row = create_manual_attendance(self.operation, actor=self.fc, character_id=alt.character_id)

        self.assertEqual((row.character_id, row.auth_user), (alt.character_id, self.pilot))
        self.assertEqual((row.main_character_id, row.main_character_name), (main.character_id, main.character_name))
        self.assertEqual((row.corporation_id, row.corporation_name), (main.corporation_id, main.corporation_name))

    def test_tracked_sources_are_refused(self):
        for source in (AttendanceRecord.Source.AUTOMATIC, AttendanceRecord.Source.CLICK):
            with self.subTest(source=source), self.assertRaises(ValueError):
                create_manual_attendance(
                    self.operation, actor=self.fc, character_id=f.main_of(self.pilot).character_id, source=source
                )
        self.assertFalse(AttendanceRecord.objects.exists())


# ---------------------------------------------------------------------------
# ESI
# ---------------------------------------------------------------------------


class FakeIdsOperation:
    def __init__(self, api, body):
        self.api = api
        self.body = list(body)

    def result(self, **kwargs):
        self.api.calls.append((self.body, kwargs))
        if self.api.failure is not None:
            raise self.api.failure
        characters = [
            {"id": self.api.characters[name.casefold()][0], "name": self.api.characters[name.casefold()][1]}
            for name in self.body
            if name.casefold() in self.api.characters
        ]
        answer = {"characters": characters, "corporations": [{"id": 98_000_001, "name": "Jita Traders"}]}
        return self.api.wrap(answer)


class FakeUniverseAPI:
    def __init__(self):
        self.characters = {}
        self.calls = []
        self.failure = None
        self.wrap = lambda answer: answer

    def PostUniverseIds(self, *, body):
        return FakeIdsOperation(self, body)

    def add(self, character_id, name):
        self.characters[name.casefold()] = (character_id, name)


class UniverseIdsTests(TestCase):
    def setUp(self):
        super().setUp()
        self.api = FakeUniverseAPI()
        patcher = mock.patch.object(esi_provider, "esi", SimpleNamespace(client=SimpleNamespace(Universe=self.api)))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.api.add(2_119_000_001, "Ana Khan")
        self.api.add(2_119_000_002, "Jita Trader")

    def test_names_resolve_case_insensitively_to_characters_only(self):
        result = universe_ids(["ana khan", "  Jita   Trader ", "Jita Traders", "Nobody Here", "ANA KHAN", ""])

        self.assertEqual(
            result,
            {"ana khan": (2_119_000_001, "Ana Khan"), "jita trader": (2_119_000_002, "Jita Trader")},
        )
        # One request, duplicates and blanks removed; the response cache is never used for a POST.
        [(body, kwargs)] = self.api.calls
        self.assertEqual(body, ["ana khan", "Jita Trader", "Jita Traders", "Nobody Here"])
        self.assertEqual(kwargs, {"use_etag": False, "use_cache": False, "store_cache": False})

    def test_object_answers_are_read_too(self):
        self.api.wrap = lambda answer: SimpleNamespace(
            characters=[SimpleNamespace(id=row["id"], name=row["name"]) for row in answer["characters"]]
        )

        self.assertEqual(universe_ids(["Ana Khan"]), {"ana khan": (2_119_000_001, "Ana Khan")})

    def test_empty_answers(self):
        self.api.wrap = lambda answer: {}
        self.assertEqual(universe_ids(["Ana Khan"]), {})
        self.api.wrap = lambda answer: None
        self.assertEqual(universe_ids(["Ana Khan"]), {})
        self.assertEqual(universe_ids([]), {})
        self.assertEqual(len(self.api.calls), 2)

    def test_names_are_sent_in_chunks_of_five_hundred(self):
        names = [f"Pilot {number}" for number in range(1001)]
        self.api.add(2_119_000_999, "Pilot 1000")

        result = universe_ids(names)

        self.assertEqual([len(body) for body, _kwargs in self.api.calls], [500, 500, 1])
        self.assertEqual(result, {"pilot 1000": (2_119_000_999, "Pilot 1000")})

    def test_names_esi_cannot_take_are_not_sent(self):
        universe_ids(["x" * 101, "y" * 100])

        [(body, _kwargs)] = self.api.calls
        self.assertEqual(body, ["y" * 100])

    def test_failures_are_short(self):
        self.api.failure = http_error(503)

        with self.assertLogs(esi_provider.logger, "WARNING"), self.assertRaises(FleetESIError) as ctx:
            universe_ids(["Ana Khan"])

        self.assertEqual(ctx.exception.code, "IDS_ERROR")
        self.assertEqual(
            str(ctx.exception), "Could not look up character names: ESI returned HTTP 503 (Service unavailable)."
        )

    def test_alias(self):
        self.assertIs(resolve_names_to_ids, universe_ids)


class OperationListTests(TestCase):
    def test_new_operations_are_loaded_with_the_others(self):
        # One client with one operation list, so the ESI spec is only loaded once.
        operations = esi_provider.esi._operations
        for name in (
            "GetCharactersCharacterIdOnline",
            "GetCharactersCharacterIdLocation",
            "GetCharactersCharacterIdShip",
            "PostUniverseIds",
            "GetFleetsFleetIdMembers",
        ):
            self.assertIn(name, operations)


class FreshMemberReadTests(TestCase):
    def setUp(self):
        self.fc = f.create_user(perms=f.FC_PERMS)
        self.fc_char = f.main_of(self.fc)
        f.add_token(self.fc, self.fc_char)

    def test_fresh_read_reaches_the_operation(self):
        fleets = SimpleNamespace(GetFleetsFleetIdMembers=lambda **kwargs: "members-operation")
        with mock.patch.object(esi_provider, "esi", SimpleNamespace(client=SimpleNamespace(Fleets=fleets))), mock.patch.object(
            esi_provider, "_read", return_value=[]
        ) as read:
            esi_provider.get_fleet_members(self.fc, self.fc_char.character_id, 42)
            esi_provider.get_fleet_members(self.fc, self.fc_char.character_id, 42, fresh=True)

        self.assertEqual(
            read.call_args_list, [mock.call("members-operation", fresh=False), mock.call("members-operation", fresh=True)]
        )

    def test_sync_passes_fresh_on(self):
        operation = f.create_operation(self.fc)
        with mock.patch.object(tracking, "get_fleet_members", return_value=[]) as members, mock.patch.object(
            tracking, "queue_name_resolution"
        ):
            tracking.sync_operation(operation)
            tracking.sync_operation(operation, fresh=True)

        self.assertEqual([call.kwargs["fresh"] for call in members.call_args_list], [False, True])
