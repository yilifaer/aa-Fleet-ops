# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Tests for the ping channel and ping role configuration, including the Discord role sync."""

import sys
from dataclasses import dataclass
from types import ModuleType, SimpleNamespace
from unittest import mock

import requests
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from fleetops.forms_config import DiscordWebhookForm, PingRoleForm
from fleetops.models import MAX_DATABASE_ID, AuditLog, DiscordWebhook, PingRole, PingTarget
from fleetops.services import discord_roles
from fleetops.services.discord_roles import (
    DISCORD_SERVICE_APP,
    NO_GUILD_MESSAGE,
    NOT_INSTALLED_MESSAGE,
    READY_MESSAGE,
    discord_service_status,
    sync_discord_roles,
)

from . import factories as f

CONFIG_PERMS = f.MEMBER_PERMS + ["fleetops.manage_configuration"]
NO_CONFIG_PERMS = [p for p in f.FC_LEAD_PERMS if p != "fleetops.manage_configuration"]
GUILD_ID = 410000000000000001
ROLE_URL = reverse("fleetops:configuration_list", args=["ping-roles"])
SYNC_URL = reverse("fleetops:sync_discord_roles")


@dataclass(frozen=True)
class DiscordRole:
    """Shaped like allianceauth.services.modules.discord.api.Role."""

    id: int
    name: str
    managed: bool = False


def discord_role(role_id, name):
    return DiscordRole(role_id, name)


def fake_discord_api(roles=(), *, guild_id=GUILD_ID, error=None):
    client = mock.Mock()
    if error is not None:
        client.guild_roles.side_effect = error
    else:
        client.guild_roles.return_value = set(roles)
    api = SimpleNamespace(
        create_bot_client=mock.Mock(return_value=client), discord_guild_id=mock.Mock(return_value=guild_id)
    )
    return api, client


class DiscordApiMixin:
    def use_discord(self, roles=(), **kwargs):
        api, client = fake_discord_api(roles, **kwargs)
        patcher = mock.patch.object(discord_roles, "_discord_api", return_value=api)
        patcher.start()
        self.addCleanup(patcher.stop)
        return client


class DiscordServiceStatusTests(DiscordApiMixin, TestCase):
    def test_service_not_installed(self):
        with mock.patch.object(discord_roles.apps, "is_installed", return_value=False) as is_installed:
            self.assertEqual(discord_service_status(), (False, NOT_INSTALLED_MESSAGE))
        is_installed.assert_called_once_with(DISCORD_SERVICE_APP)

    def test_service_api_is_imported_only_when_installed(self):
        api = ModuleType("allianceauth.services.modules.discord.api")
        api.discord_guild_id = lambda: GUILD_ID
        with mock.patch.dict(sys.modules, {"allianceauth.services.modules.discord.api": api}):
            with mock.patch.object(discord_roles.apps, "is_installed", return_value=True):
                self.assertIs(discord_roles._discord_api(), api)
            with mock.patch.object(discord_roles.apps, "is_installed", return_value=False):
                self.assertIsNone(discord_roles._discord_api())

    def test_guild_id_not_set(self):
        self.use_discord(guild_id=None)

        self.assertEqual(discord_service_status(), (False, NO_GUILD_MESSAGE))

    def test_ready(self):
        self.use_discord()

        self.assertEqual(discord_service_status(), (True, READY_MESSAGE))


class DiscordRoleSyncTests(DiscordApiMixin, TestCase):
    def setUp(self):
        self.admin = f.create_user(perms=CONFIG_PERMS)

    def test_new_roles_are_added_inactive(self):
        client = self.use_discord([discord_role(GUILD_ID, "@everyone"), discord_role(11, "Sig A"), discord_role(12, "Supers")])

        result = sync_discord_roles(actor=self.admin)

        client.guild_roles.assert_called_once_with(GUILD_ID, use_cache=False)
        self.assertTrue(result.ok)
        self.assertEqual((result.created, result.updated, result.missing), (2, 0, 0))
        self.assertEqual(result.message, "Roles synced from Discord: 2 new (inactive until enabled), 0 updated.")
        roles = {role.role_id: role for role in PingRole.objects.all()}
        self.assertEqual(set(roles), {11, 12})
        for role in roles.values():
            self.assertFalse(role.is_active)
            self.assertEqual(role.source, PingRole.Source.DISCORD)
            self.assertFalse(role.missing_from_discord)
            self.assertIsNotNone(role.last_synced_at)
        self.assertEqual(roles[11].name, "Sig A")

    def test_everyone_role_is_skipped(self):
        self.use_discord([discord_role(GUILD_ID, "@everyone")])

        result = sync_discord_roles(actor=self.admin)

        self.assertEqual(result.created, 0)
        self.assertFalse(PingRole.objects.exists())

    def test_known_roles_are_renamed_and_keep_their_switch(self):
        manual = f.ping_role("Old Name", role_id=11, is_active=True)
        synced = f.ping_role("Supers", role_id=12, is_active=False, source=PingRole.Source.DISCORD)
        self.use_discord([discord_role(11, "Sig A"), discord_role(12, "Supers")])

        result = sync_discord_roles(actor=self.admin)

        self.assertEqual((result.created, result.updated, result.missing), (0, 1, 0))
        manual.refresh_from_db()
        synced.refresh_from_db()
        self.assertEqual(manual.name, "Sig A")
        self.assertEqual(manual.source, PingRole.Source.DISCORD)
        self.assertTrue(manual.is_active)
        self.assertIsNotNone(manual.last_synced_at)
        self.assertFalse(synced.is_active)
        self.assertIsNotNone(synced.last_synced_at)

    def test_vanished_synced_roles_are_marked_missing_and_disabled(self):
        gone = f.ping_role("Gone", role_id=21, is_active=True, source=PingRole.Source.DISCORD)
        manual = f.ping_role("Other Server", role_id=22, is_active=True)
        self.use_discord([discord_role(11, "Sig A")])

        result = sync_discord_roles(actor=self.admin)

        self.assertEqual((result.created, result.updated, result.missing), (1, 0, 1))
        self.assertIn("1 no longer on Discord (disabled)", result.message)
        gone.refresh_from_db()
        manual.refresh_from_db()
        self.assertTrue(gone.missing_from_discord)
        self.assertFalse(gone.is_active)
        # Hand-added roles may belong to another Discord server, so they are left alone.
        self.assertFalse(manual.missing_from_discord)
        self.assertTrue(manual.is_active)

    def test_missing_roles_are_counted_once(self):
        f.ping_role("Gone", role_id=21, is_active=False, source=PingRole.Source.DISCORD, missing_from_discord=True)
        self.use_discord([discord_role(11, "Sig A")])

        self.assertEqual(sync_discord_roles(actor=self.admin).missing, 0)

    def test_returning_role_is_no_longer_missing_but_stays_disabled(self):
        back = f.ping_role("Back", role_id=21, is_active=False, source=PingRole.Source.DISCORD, missing_from_discord=True)
        self.use_discord([discord_role(21, "Back")])

        result = sync_discord_roles(actor=self.admin)

        self.assertEqual(result.updated, 1)
        back.refresh_from_db()
        self.assertFalse(back.missing_from_discord)
        self.assertFalse(back.is_active)

    def test_unusable_ids_and_long_names(self):
        self.use_discord([discord_role(0, "Zero"), discord_role(MAX_DATABASE_ID + 1, "Huge"), discord_role(13, "x" * 150)])

        sync_discord_roles(actor=self.admin)

        self.assertEqual(list(PingRole.objects.values_list("role_id", flat=True)), [13])
        self.assertEqual(PingRole.objects.get().name, "x" * 100)

    def test_sync_is_audited_with_counts(self):
        f.ping_role("Gone", role_id=21, source=PingRole.Source.DISCORD)
        self.use_discord([discord_role(11, "Sig A")])

        sync_discord_roles(actor=self.admin)

        entry = AuditLog.objects.get(action="configuration.discord_role_sync")
        self.assertEqual(entry.actor, self.admin)
        self.assertEqual(entry.object_type, "PingRole")
        self.assertEqual(entry.new_value, {"created": 1, "updated": 0, "missing": 1, "roles": 1})

    def test_discord_errors_show_only_the_exception_class(self):
        secret = "Bot MTIzNDU2Nzg5.secret-bot-token"
        error = requests.HTTPError(f"401 Client Error for url: https://discord.com/api/v10/guilds/{GUILD_ID}/roles {secret}")
        existing = f.ping_role("Sig A", role_id=11, is_active=True, source=PingRole.Source.DISCORD)
        self.use_discord(error=error)

        with self.assertLogs("extensions", level="WARNING") as logs:
            result = sync_discord_roles(actor=self.admin)

        self.assertFalse(result.ok)
        self.assertEqual(
            result.message,
            "Could not read the roles from Discord (HTTPError). Check the Discord service, or add role IDs manually.",
        )
        self.assertNotIn(secret, " ".join(logs.output))
        self.assertNotIn("discord.com", " ".join(logs.output))
        existing.refresh_from_db()
        self.assertTrue(existing.is_active)
        self.assertFalse(existing.missing_from_discord)
        self.assertFalse(AuditLog.objects.exists())

    def test_service_not_installed_changes_nothing(self):
        role = f.ping_role("Sig A", role_id=11, source=PingRole.Source.DISCORD)
        with mock.patch.object(discord_roles.apps, "is_installed", return_value=False):
            result = sync_discord_roles(actor=self.admin)

        self.assertFalse(result.ok)
        self.assertEqual(result.message, NOT_INSTALLED_MESSAGE)
        role.refresh_from_db()
        self.assertFalse(role.missing_from_discord)
        self.assertFalse(AuditLog.objects.exists())

    def test_guild_id_not_set_changes_nothing(self):
        client = self.use_discord([discord_role(11, "Sig A")], guild_id=None)

        result = sync_discord_roles(actor=self.admin)

        self.assertEqual(result.message, NO_GUILD_MESSAGE)
        client.guild_roles.assert_not_called()
        self.assertFalse(PingRole.objects.exists())


class DiscordRoleSyncViewTests(DiscordApiMixin, TestCase):
    def setUp(self):
        self.admin = f.create_user(perms=CONFIG_PERMS)
        self.client.force_login(self.admin)

    def test_requires_manage_configuration(self):
        self.use_discord([discord_role(11, "Sig A")])
        self.client.force_login(f.create_user(perms=NO_CONFIG_PERMS))

        self.assertEqual(self.client.post(SYNC_URL).status_code, 403)
        self.assertFalse(PingRole.objects.exists())

    def test_anonymous_users_are_sent_to_login(self):
        self.client.logout()

        response = self.client.post(SYNC_URL)

        self.assertEqual(response.status_code, 302)
        self.assertIn("login", response["Location"])

    def test_requires_post(self):
        self.use_discord([discord_role(11, "Sig A")])

        self.assertEqual(self.client.get(SYNC_URL).status_code, 405)
        self.assertFalse(PingRole.objects.exists())

    def test_sync_reports_the_result(self):
        self.use_discord([discord_role(11, "Sig A")])

        response = self.client.post(SYNC_URL, follow=True)

        self.assertRedirects(response, ROLE_URL)
        self.assertContains(response, "Roles synced from Discord: 1 new (inactive until enabled), 0 updated.")
        self.assertContains(response, "<td>Sig A</td>", html=True)
        self.assertContains(response, "<td>Synced</td>", html=True)

    def test_failure_is_reported(self):
        self.use_discord(error=RuntimeError("Unexpected response when fetching roles from API: secret"))

        response = self.client.post(SYNC_URL, follow=True)

        self.assertContains(response, "Could not read the roles from Discord (RuntimeError).")
        self.assertNotContains(response, "secret")

    def test_role_list_offers_the_sync_when_available(self):
        self.use_discord()

        response = self.client.get(ROLE_URL)

        self.assertContains(response, READY_MESSAGE)
        self.assertContains(response, f'<form method="post" action="{SYNC_URL}">')
        self.assertContains(response, "<button class=\"btn btn-outline-primary\">Sync roles from Discord</button>", html=True)

    def test_role_list_explains_why_the_sync_is_unavailable(self):
        with mock.patch.object(discord_roles.apps, "is_installed", return_value=False):
            response = self.client.get(ROLE_URL)

        self.assertContains(response, NOT_INSTALLED_MESSAGE)
        self.assertContains(
            response, '<button class="btn btn-outline-primary" disabled>Sync roles from Discord</button>', html=True
        )

    def test_other_sections_have_no_sync_button(self):
        response = self.client.get(reverse("fleetops:configuration_list", args=["ping-channels"]))

        self.assertNotContains(response, "Sync roles from Discord")

    def test_role_list_shows_where_each_role_came_from(self):
        f.ping_role("Manual", role_id=11)
        f.ping_role("Synced", role_id=12, source=PingRole.Source.DISCORD, last_synced_at=timezone.now())
        f.ping_role("Gone", role_id=13, source=PingRole.Source.DISCORD, missing_from_discord=True, is_active=False)

        response = self.client.get(ROLE_URL)

        states = {row["object"].name: dict(row["values"])["Discord"] for row in response.context["rows"]}
        self.assertEqual(states, {"Manual": "Added manually", "Synced": "Synced", "Gone": "Not on Discord"})


class PingRoleFormTests(TestCase):
    def form(self, role_id, name="Sig A", **extra):
        return PingRoleForm({"name": name, "role_id": role_id, "is_active": "on", "sort_order": "0", **extra})

    def test_role_ids_and_role_mentions_are_accepted(self):
        for value, expected in (
            ("123456789012345678", 123456789012345678),
            ("  123456789012345678 ", 123456789012345678),
            ("<@&123456789012345678>", 123456789012345678),
            (str(MAX_DATABASE_ID), MAX_DATABASE_ID),
        ):
            with self.subTest(value=value):
                form = self.form(value)
                self.assertTrue(form.is_valid(), form.errors)
                self.assertEqual(form.cleaned_data["role_id"], expected)

    def test_anything_else_is_rejected(self):
        for value in ("", "abc", "12 34", "-1", "0", "1.5", str(MAX_DATABASE_ID + 1), "１２３", "<@123>", "<@&>", "@Sig A"):
            with self.subTest(value=value):
                form = self.form(value)
                self.assertFalse(form.is_valid())
                self.assertIn("role_id", form.errors)

    def test_leading_at_sign_is_dropped_from_the_name(self):
        form = self.form("123", name="  @Sig A ")

        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["name"], "Sig A")

    def test_name_is_required(self):
        for name in ("", "@", " @ "):
            with self.subTest(name=name):
                self.assertIn("name", self.form("123", name=name).errors)

    def test_role_ids_are_unique(self):
        f.ping_role("Sig A", role_id=123)

        form = self.form("<@&123>", name="Sig B")

        self.assertFalse(form.is_valid())
        self.assertIn("role_id", form.errors)

    def test_manual_roles_are_added_from_the_page(self):
        self.client.force_login(f.create_user(perms=CONFIG_PERMS))

        response = self.client.post(
            reverse("fleetops:configuration_add", args=["ping-roles"]),
            {"name": "@Sig B", "role_id": "<@&876543210987654321>", "is_active": "on", "sort_order": "2"},
        )

        self.assertRedirects(response, ROLE_URL)
        role = PingRole.objects.get()
        self.assertEqual((role.name, role.role_id, role.source, role.is_active, role.sort_order), ("Sig B", 876543210987654321, "manual", True, 2))
        entry = AuditLog.objects.get(action="configuration.create")
        self.assertEqual(entry.new_value["role_id"], 876543210987654321)


class PingChannelConfigurationTests(TestCase):
    def setUp(self):
        self.admin = f.create_user(perms=CONFIG_PERMS)
        self.client.force_login(self.admin)
        self.sig_a = f.ping_role("Sig A", role_id=111, sort_order=1)
        self.sig_b = f.ping_role("Sig B", role_id=222, sort_order=2, is_active=False, missing_from_discord=True)

    def payload(self, **extra):
        data = {
            "name": "STR Pings",
            "webhook_url": "https://discord.com/api/webhooks/1/token",
            "is_active": "on",
            "sort_order": "5",
        }
        data.update(extra)
        return data

    def test_form_offers_every_role_as_a_checkbox(self):
        form = DiscordWebhookForm()

        self.assertEqual(
            [label for _, label in form.fields["allowed_roles"].choices],
            ["@Sig A", "@Sig B (inactive, not on Discord)"],
        )
        response = self.client.get(reverse("fleetops:configuration_add", args=["ping-channels"]))
        self.assertContains(response, 'type="checkbox" name="allowed_roles"', count=2)
        self.assertContains(response, "Leave empty to allow every active role.")
        self.assertContains(response, "Allow @everyone")
        self.assertContains(response, "Allow @here")

    def test_channel_rules_are_saved(self):
        response = self.client.post(
            reverse("fleetops:configuration_add", args=["ping-channels"]),
            self.payload(allow_everyone="on", allowed_roles=[str(self.sig_b.pk), str(self.sig_a.pk)]),
        )

        self.assertRedirects(response, reverse("fleetops:configuration_list", args=["ping-channels"]))
        channel = DiscordWebhook.objects.get()
        self.assertTrue(channel.allow_everyone)
        self.assertFalse(channel.allow_here)
        self.assertEqual(channel.sort_order, 5)
        self.assertEqual(set(channel.allowed_roles.all()), {self.sig_a, self.sig_b})
        entry = AuditLog.objects.get(action="configuration.create")
        self.assertEqual(entry.new_value["allowed_roles"], sorted([self.sig_a.pk, self.sig_b.pk]))
        self.assertEqual(entry.new_value["webhook_url"], "***redacted***")

    def test_role_changes_are_audited_as_id_lists(self):
        channel = f.ping_channel("STR Pings", roles=[self.sig_a])

        self.client.post(
            reverse("fleetops:configuration_edit", args=["ping-channels", channel.pk]),
            self.payload(webhook_url=channel.webhook_url, allowed_roles=[str(self.sig_b.pk)]),
        )
        self.client.post(reverse("fleetops:configuration_delete", args=["ping-channels", channel.pk]))

        update = AuditLog.objects.get(action="configuration.update")
        self.assertEqual(update.old_value["allowed_roles"], [self.sig_a.pk])
        self.assertEqual(update.new_value["allowed_roles"], [self.sig_b.pk])
        deleted = AuditLog.objects.get(action="configuration.delete")
        self.assertEqual(deleted.old_value["allowed_roles"], [self.sig_b.pk])

    def test_clearing_the_roles_allows_every_active_role(self):
        channel = f.ping_channel("STR Pings", roles=[self.sig_a])

        self.client.post(
            reverse("fleetops:configuration_edit", args=["ping-channels", channel.pk]),
            self.payload(webhook_url=channel.webhook_url),
        )

        self.assertFalse(channel.allowed_roles.exists())
        listing = self.client.get(reverse("fleetops:configuration_list", args=["ping-channels"]))
        self.assertContains(listing, "<td>Every active role</td>", html=True)

    def test_sort_order_is_required(self):
        response = self.client.post(
            reverse("fleetops:configuration_add", args=["ping-channels"]), self.payload(sort_order="")
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("sort_order", response.context["form"].errors)

    def test_role_allowed_in_a_channel_cannot_be_deleted(self):
        f.ping_channel("STR Pings", roles=[self.sig_a])
        f.ping_channel("Capitals", roles=[self.sig_a, self.sig_b])

        response = self.client.post(reverse("fleetops:configuration_delete", args=["ping-roles", self.sig_a.pk]), follow=True)

        self.assertTrue(PingRole.objects.filter(pk=self.sig_a.pk).exists())
        self.assertContains(response, "This role is allowed in the ping channels Capitals, STR Pings.")
        self.assertFalse(AuditLog.objects.filter(action="configuration.delete").exists())

    def test_unused_role_can_be_deleted(self):
        response = self.client.post(reverse("fleetops:configuration_delete", args=["ping-roles", self.sig_a.pk]))

        self.assertRedirects(response, ROLE_URL)
        self.assertFalse(PingRole.objects.filter(pk=self.sig_a.pk).exists())
        self.assertTrue(AuditLog.objects.filter(action="configuration.delete", object_type="PingRole").exists())

    def test_legacy_ping_targets_are_not_offered(self):
        PingTarget.objects.create(name="Legacy Target", target_value="@everyone")

        index = self.client.get(reverse("fleetops:configuration_index"))

        self.assertContains(index, "Ping Channels")
        self.assertContains(index, "Ping Roles")
        self.assertNotContains(index, "Ping Targets")
        self.assertNotContains(index, "Legacy Target")
        self.assertNotContains(index, "/configuration/ping-targets/")
