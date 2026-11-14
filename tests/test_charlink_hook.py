# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""aa-charlink integration: the hook, and the imports CharLink validates and offers to pilots.

aa-charlink is not installed here (it needs Python 3.11 or newer and Alliance Auth below 6), so its
``charlink.app_imports.utils`` is replaced by a stand-in that mirrors the fields and checks of
aa-charlink 1.14. A last test runs against the real package where it is installed.
"""

import importlib
import importlib.util
import re
import sys
import types
import unittest
from datetime import timedelta
from collections.abc import Callable
from dataclasses import dataclass
from unittest import mock

from allianceauth import hooks
from allianceauth.authentication.models import State
from allianceauth.eveonline.models import EveCharacter
from django.conf import settings
from django.contrib.auth.models import Group, Permission, User
from django.db.models import Exists
from django.test import TestCase
from django.utils import timezone

from fleetops.auth_hooks import register_charlink_hook
from fleetops.constants import (
    ALL_SCOPES,
    CLICK_SCOPES,
    FLEET_SCOPES,
    LOSS_SCOPES,
    OPEN_WINDOW_SCOPE,
    OPEN_WINDOW_SCOPES,
)
from fleetops.models import SrpLossScan

from . import factories as f

HOOK_MODULE = "fleetops.charlink_hook"


def charlink_stand_in():
    """``charlink``, ``charlink.app_imports`` and ``charlink.app_imports.utils`` as aa-charlink 1.14 has them."""

    @dataclass
    class LoginImport:
        app_label: str
        unique_id: str
        field_label: str
        add_character: Callable
        scopes: list
        check_permissions: Callable
        is_character_added: Callable
        is_character_added_annotation: Exists
        get_users_with_perms: Callable
        default_initial_selection: bool = True

        def get_query_id(self):
            return f"{self.app_label}_{self.unique_id}"

        def __hash__(self):
            return hash(self.get_query_id())

        def validate_import(self):
            assert isinstance(self.app_label, str)
            assert re.match(r"^[a-zA-Z0-9]+$", self.unique_id)
            assert isinstance(str(self.field_label), str)
            assert callable(self.add_character)
            assert isinstance(self.scopes, list) and all(isinstance(scope, str) for scope in self.scopes)
            assert callable(self.check_permissions)
            assert callable(self.is_character_added)
            assert isinstance(self.is_character_added_annotation, Exists)
            assert callable(self.get_users_with_perms)
            assert isinstance(self.default_initial_selection, bool)

    @dataclass
    class AppImport:
        app_label: str
        imports: list

        def get(self, unique_id):
            for login_import in self.imports:
                if login_import.unique_id == unique_id:
                    return login_import
            raise KeyError(unique_id)

        def has_any_perms(self, user):
            return any(login_import.check_permissions(user) for login_import in self.imports)

        def validate_import(self):
            assert isinstance(self.app_label, str)
            assert self.app_label in settings.INSTALLED_APPS
            assert isinstance(self.imports, list) and self.imports
            for login_import in self.imports:
                assert isinstance(login_import, LoginImport)
                assert login_import.app_label == self.app_label
                login_import.validate_import()
            unique_ids = [login_import.unique_id for login_import in self.imports]
            assert len(unique_ids) == len(set(unique_ids))

    charlink = types.ModuleType("charlink")
    app_imports = types.ModuleType("charlink.app_imports")
    utils = types.ModuleType("charlink.app_imports.utils")
    utils.LoginImport = LoginImport
    utils.AppImport = AppImport
    charlink.app_imports = app_imports
    app_imports.utils = utils
    return {"charlink": charlink, "charlink.app_imports": app_imports, "charlink.app_imports.utils": utils}


class HookTests(TestCase):
    def test_the_hook_names_the_module(self):
        self.assertIn(register_charlink_hook, hooks.get_hooks("charlink"))
        self.assertEqual(register_charlink_hook(), HOOK_MODULE)

    def test_the_module_is_only_loaded_by_charlink(self):
        modules = {name: module for name, module in sys.modules.items() if name != HOOK_MODULE}
        with mock.patch.dict(sys.modules, modules, clear=True):
            importlib.import_module("fleetops.auth_hooks")
            self.assertNotIn(HOOK_MODULE, sys.modules)
            if importlib.util.find_spec("charlink") is None:
                with self.assertRaises(ImportError):
                    importlib.import_module(HOOK_MODULE)


class CharLinkImportTests(TestCase):
    """0.3.0b6: FleetOps offers ONE CharLink item (unique_id "clickpap") that asks for all seven scopes."""

    def setUp(self):
        super().setUp()
        modules = {name: module for name, module in sys.modules.items() if name != HOOK_MODULE}
        modules.update(charlink_stand_in())
        patcher = mock.patch.dict(sys.modules, modules, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.app_import = importlib.import_module(HOOK_MODULE).app_import
        self.item = self.app_import.get("clickpap")

        self.fc = f.create_user("fc", perms=f.FC_PERMS)
        self.member = f.create_user("member", perms=f.MEMBER_PERMS)
        self.nobody = f.create_user("nobody")

    def test_charlink_accepts_the_imports(self):
        self.app_import.validate_import()

        self.assertEqual(self.app_import.app_label, "fleetops")
        self.assertIn("fleetops", settings.INSTALLED_APPS)

    def test_fleetops_has_exactly_one_item_and_keeps_the_stored_id(self):
        # The stored CharLink setting fleetops_clickpap keeps working; fcfleet and srplosses are gone.
        self.assertEqual([i.unique_id for i in self.app_import.imports], ["clickpap"])
        self.assertEqual([i.get_query_id() for i in self.app_import.imports], ["fleetops_clickpap"])
        for gone in ("fcfleet", "srplosses"):
            with self.assertRaises(KeyError):
                self.app_import.get(gone)

    def test_the_label_is_translated_with_the_fleetops_context(self):
        from django.utils import translation

        self.assertEqual(str(self.item.field_label), "FleetOps (PAP links, SRP, fleet control)")
        with translation.override("zh-hans"):
            self.assertNotEqual(str(self.item.field_label), "FleetOps (PAP links, SRP, fleet control)")

    def test_the_scopes_are_all_seven_in_order_without_repeats(self):
        self.assertEqual(
            ALL_SCOPES,
            [
                "esi-fleets.read_fleet.v1",
                "esi-fleets.write_fleet.v1",
                "esi-location.read_location.v1",
                "esi-location.read_ship_type.v1",
                "esi-location.read_online.v1",
                "esi-killmails.read_killmails.v1",
                "esi-ui.open_window.v1",
            ],
        )
        self.assertEqual(ALL_SCOPES, list(dict.fromkeys([*FLEET_SCOPES, *CLICK_SCOPES, *LOSS_SCOPES, *OPEN_WINDOW_SCOPES])))
        self.assertEqual(self.item.scopes, ALL_SCOPES)

    def test_the_item_is_ticked_by_default(self):
        self.assertTrue(self.item.default_initial_selection)

    def test_only_users_with_basic_access_see_it(self):
        self.assertEqual(
            [self.item.check_permissions(user) for user in (self.fc, self.member, self.nobody)], [True, True, False]
        )
        self.assertTrue(self.app_import.has_any_perms(self.member))
        self.assertFalse(self.app_import.has_any_perms(self.nobody))
        # Independent of the loss reading switch: the item is always there for members.
        f.srp_settings(enabled=False, loss_discovery_enabled=False)
        self.assertTrue(self.item.check_permissions(self.member))

    def test_a_character_counts_as_added_only_with_all_seven_scopes(self):
        full = f.main_of(self.member)
        missing_one = f.add_alt(self.member, "Missing One")
        old_click_only = f.add_alt(self.member, "Click Only")
        none = f.add_alt(self.member, "No Token Alt")
        f.add_token(self.member, full, scopes=ALL_SCOPES)
        f.add_token(self.member, missing_one, scopes=[s for s in ALL_SCOPES if s != OPEN_WINDOW_SCOPE])
        f.add_token(self.member, old_click_only, scopes=CLICK_SCOPES)

        self.assertEqual(
            [self.item.is_character_added(c) for c in (full, missing_one, old_click_only, none)],
            [True, False, False, False],
        )
        added = EveCharacter.objects.annotate(added=self.item.is_character_added_annotation).filter(added=True)
        self.assertEqual(list(added.values_list("character_name", flat=True)), [full.character_name])

    def test_users_with_basic_access_by_any_route_are_counted(self):
        permission = Permission.objects.get(content_type__app_label="fleetops", codename="basic_access")
        by_group = f.create_user("by_group")
        group = Group.objects.create(name="Members")
        group.permissions.add(permission)
        by_group.groups.add(group)
        by_state = f.create_user("by_state")
        state = State.objects.create(name="Member State", priority=200)
        # State permissions point to Alliance Auth's proxy of Permission, so the id is passed.
        state.permissions.add(permission.pk)
        state.member_characters.add(f.main_of(by_state))
        by_state.profile.assign_state()
        admin = User.objects.create_superuser("admin", "admin@example.com", "pw")

        users = set(self.item.get_users_with_perms())

        self.assertEqual({self.fc, self.member, by_group, by_state, admin} - users, set())
        self.assertNotIn(self.nobody, users)

    def test_adding_a_character_makes_it_due_only_with_srp_and_loss_reading_on(self):
        main = f.main_of(self.member)
        token = f.add_token(self.member, main, scopes=ALL_SCOPES)
        scan = SrpLossScan.objects.create(character_id=main.character_id, next_check_at=timezone.now() + timedelta(hours=20))

        for enabled, discovery in ((False, True), (True, False), (False, False)):
            f.srp_settings(enabled=enabled, loss_discovery_enabled=discovery)
            self.assertIsNone(self.item.add_character(mock.Mock(), token))
            scan.refresh_from_db()
            self.assertIsNotNone(scan.next_check_at, (enabled, discovery))

        f.srp_settings(enabled=True, loss_discovery_enabled=True)
        self.assertIsNone(self.item.add_character(mock.Mock(), token))

        scan.refresh_from_db()
        # A character made due has no next check, which sorts before every overdue character.
        self.assertIsNone(scan.next_check_at)

    def test_the_hook_no_longer_hides_the_item_with_the_discovery_switch(self):
        self.assertFalse(hasattr(importlib.import_module(HOOK_MODULE), "_add_loss_character"))


@unittest.skipUnless(importlib.util.find_spec("charlink"), "aa-charlink is not installed")
class RealCharLinkTests(TestCase):
    def test_the_real_package_accepts_the_imports(self):
        from charlink.app_imports.utils import AppImport

        module = importlib.import_module(HOOK_MODULE)

        self.assertIsInstance(module.app_import, AppImport)
        module.app_import.validate_import()
