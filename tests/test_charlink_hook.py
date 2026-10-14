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

from fleetops.auth_hooks import register_charlink_hook
from fleetops.constants import CLICK_SCOPES, FLEET_READ_SCOPE, FLEET_SCOPES

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
    def setUp(self):
        super().setUp()
        modules = {name: module for name, module in sys.modules.items() if name != HOOK_MODULE}
        modules.update(charlink_stand_in())
        patcher = mock.patch.dict(sys.modules, modules, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.app_import = importlib.import_module(HOOK_MODULE).app_import
        self.fleet = self.app_import.get("fcfleet")
        self.click = self.app_import.get("clickpap")

        self.fc = f.create_user("fc", perms=f.FC_PERMS)
        self.member = f.create_user("member", perms=f.MEMBER_PERMS)
        self.nobody = f.create_user("nobody")

    def test_charlink_accepts_the_imports(self):
        self.app_import.validate_import()

        self.assertEqual(self.app_import.app_label, "fleetops")
        self.assertEqual([i.unique_id for i in self.app_import.imports], ["fcfleet", "clickpap"])
        self.assertEqual([i.get_query_id() for i in self.app_import.imports], ["fleetops_fcfleet", "fleetops_clickpap"])
        self.assertIn("fleetops", settings.INSTALLED_APPS)

    def test_scopes_are_the_fleet_and_the_afat_click_tracking_ones(self):
        self.assertEqual(self.fleet.scopes, FLEET_SCOPES)
        # The same three scopes as AFAT's click tracking, so the tokens pilots already have are used.
        self.assertEqual(
            sorted(self.click.scopes),
            ["esi-location.read_location.v1", "esi-location.read_online.v1", "esi-location.read_ship_type.v1"],
        )
        self.assertEqual(self.click.scopes, CLICK_SCOPES)

    def test_fleet_control_is_offered_to_fcs_and_pap_links_to_every_pilot(self):
        self.assertFalse(self.fleet.default_initial_selection)
        self.assertTrue(self.click.default_initial_selection)
        self.assertEqual(
            [self.fleet.check_permissions(user) for user in (self.fc, self.member, self.nobody)], [True, False, False]
        )
        self.assertEqual(
            [self.click.check_permissions(user) for user in (self.fc, self.member, self.nobody)], [True, True, False]
        )
        self.assertTrue(self.app_import.has_any_perms(self.member))
        self.assertFalse(self.app_import.has_any_perms(self.nobody))

    def test_characters_count_as_added_with_a_token_holding_every_scope(self):
        full = f.main_of(self.member)
        partial = f.add_alt(self.member, "Partial Alt")
        none = f.add_alt(self.member, "No Token Alt")
        f.add_token(self.member, full, scopes=CLICK_SCOPES)
        f.add_token(self.member, partial, scopes=CLICK_SCOPES[:2])
        fc_main = f.main_of(self.fc)
        f.add_token(self.fc, fc_main)

        self.assertEqual([self.click.is_character_added(c) for c in (full, partial, none)], [True, False, False])
        self.assertTrue(self.fleet.is_character_added(fc_main))
        self.assertFalse(self.fleet.is_character_added(full))
        added = EveCharacter.objects.annotate(added=self.click.is_character_added_annotation).filter(added=True)
        self.assertEqual(list(added.values_list("character_name", flat=True)), [full.character_name])
        fleet_added = EveCharacter.objects.annotate(added=self.fleet.is_character_added_annotation).filter(added=True)
        self.assertEqual(list(fleet_added), [fc_main])

    def test_scopes_seen_after_the_import_still_count(self):
        # The module was imported before any token held these scopes; the annotation still finds them.
        f.add_token(self.fc, f.main_of(self.fc), scopes=[FLEET_READ_SCOPE, *CLICK_SCOPES])

        added = EveCharacter.objects.annotate(added=self.click.is_character_added_annotation).filter(added=True)

        self.assertEqual(list(added), [f.main_of(self.fc)])

    def test_users_with_the_permission_by_any_route(self):
        permission = Permission.objects.get(content_type__app_label="fleetops", codename="start_fleet")
        by_group = f.create_user("by_group")
        group = Group.objects.create(name="FCs")
        group.permissions.add(permission)
        by_group.groups.add(group)
        by_state = f.create_user("by_state")
        state = State.objects.create(name="FC State", priority=200)
        # State permissions point to Alliance Auth's proxy of Permission, so the id is passed.
        state.permissions.add(permission.pk)
        state.member_characters.add(f.main_of(by_state))
        by_state.profile.assign_state()
        admin = User.objects.create_superuser("admin", "admin@example.com", "pw")

        users = set(self.fleet.get_users_with_perms())

        self.assertEqual(users, {self.fc, by_group, by_state, admin})
        self.assertTrue({self.fc, self.member, admin} <= set(self.click.get_users_with_perms()))
        self.assertNotIn(self.nobody, set(self.click.get_users_with_perms()))

    def test_adding_a_character_needs_nothing_more(self):
        token = f.add_token(self.member, f.main_of(self.member), scopes=CLICK_SCOPES)

        self.assertIsNone(self.click.add_character(mock.Mock(), token))
        self.assertIsNone(self.fleet.add_character(mock.Mock(), token))


@unittest.skipUnless(importlib.util.find_spec("charlink"), "aa-charlink is not installed")
class RealCharLinkTests(TestCase):
    def test_the_real_package_accepts_the_imports(self):
        from charlink.app_imports.utils import AppImport

        module = importlib.import_module(HOOK_MODULE)

        self.assertIsInstance(module.app_import, AppImport)
        module.app_import.validate_import()
