# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Webhook URLs of ping channels: never shown again, replaced on purpose, kept out of logs and audit entries."""

import json

from django.contrib import admin
from django.test import TestCase
from django.urls import reverse

from fleetops.admin import DiscordWebhookAdmin
from fleetops.forms_config import DiscordWebhookForm
from fleetops.models import (
    AuditLog,
    DiscordWebhook,
)

from . import factories as f

WEBHOOK_TOKEN = "Kq3-webhook-token-never-shown"
WEBHOOK_URL = f"https://discord.com/api/webhooks/445566/{WEBHOOK_TOKEN}"


class WebhookUrlTests(TestCase):
    def setUp(self):
        self.lead = f.create_user(perms=f.FC_LEAD_PERMS)
        self.client.force_login(self.lead)
        self.channel = f.ping_channel("Ops Pings", url=WEBHOOK_URL)

    def edit_url(self):
        return reverse("fleetops:configuration_edit", args=["ping-channels", self.channel.pk])

    def payload(self, **extra):
        data = {"name": "Ops Pings", "webhook_url": "", "is_active": "on", "sort_order": "0"}
        data.update(extra)
        return data

    def assertNoSecret(self, response):
        self.assertNotIn(WEBHOOK_TOKEN, response.content.decode())

    def test_edit_page_never_contains_the_token(self):
        response = self.client.get(self.edit_url())

        self.assertEqual(response.status_code, 200)
        self.assertNoSecret(response)
        self.assertContains(response, 'autocomplete="new-password"')
        self.assertContains(response, "Leave empty to keep the stored URL.")

    def test_blank_keeps_the_stored_url(self):
        response = self.client.post(self.edit_url(), self.payload(name="Ops Pings Renamed"))

        self.assertEqual(response.status_code, 302)
        self.channel.refresh_from_db()
        self.assertEqual(self.channel.name, "Ops Pings Renamed")
        self.assertEqual(self.channel.webhook_url, WEBHOOK_URL)
        entry = AuditLog.objects.get(action="configuration.update", object_type="DiscordWebhook")
        self.assertNotIn("webhook_url_replaced", entry.new_value)

    def test_a_new_url_replaces_the_stored_one(self):
        rotated = "https://discord.com/api/webhooks/445566/rotated-token"

        self.client.post(self.edit_url(), self.payload(webhook_url=rotated))

        self.channel.refresh_from_db()
        self.assertEqual(self.channel.webhook_url, rotated)
        entry = AuditLog.objects.get(action="configuration.update", object_type="DiscordWebhook")
        self.assertTrue(entry.new_value["webhook_url_replaced"])
        self.assertNotIn("rotated-token", json.dumps([entry.old_value, entry.new_value]))

    def test_invalid_url_is_neither_echoed_nor_rendered_back(self):
        typo = "https://discord.example/api/webhooks/1/typo-secret-token"

        response = self.client.post(self.edit_url(), self.payload(webhook_url=typo))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Enter a Discord webhook URL in the form")
        self.assertNotContains(response, "typo-secret-token")
        self.assertNoSecret(response)
        self.channel.refresh_from_db()
        self.assertEqual(self.channel.webhook_url, WEBHOOK_URL)

    def test_a_new_channel_needs_a_url(self):
        response = self.client.post(
            reverse("fleetops:configuration_add", args=["ping-channels"]), self.payload(name="New Channel")
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(DiscordWebhook.objects.filter(name="New Channel").exists())
        self.assertFalse(response.context["form"].fields["webhook_url"].help_text)

    def test_admin_uses_the_same_form(self):
        self.assertIs(DiscordWebhookAdmin.form, DiscordWebhookForm)
        self.assertIsInstance(admin.site._registry[DiscordWebhook], DiscordWebhookAdmin)

    def test_admin_change_page_hides_the_url_and_blank_keeps_it(self):
        superuser = f.create_user()
        superuser.is_staff = superuser.is_superuser = True
        superuser.save()
        self.client.force_login(superuser)
        url = reverse("admin:fleetops_discordwebhook_change", args=[self.channel.pk])

        page = self.client.get(url)
        self.assertEqual(page.status_code, 200)
        self.assertNoSecret(page)

        response = self.client.post(url, self.payload(name="Admin Renamed"))
        self.assertEqual(response.status_code, 302)
        self.channel.refresh_from_db()
        self.assertEqual((self.channel.name, self.channel.webhook_url), ("Admin Renamed", WEBHOOK_URL))

    def staff(self, *perms):
        user = f.create_user(perms=list(perms))
        user.is_staff = True
        user.save()
        self.client.force_login(user)
        return user

    def test_view_only_admins_never_see_the_url(self):
        # Without the change permission Django shows every field as text.
        self.staff("fleetops.view_discordwebhook")

        page = self.client.get(reverse("admin:fleetops_discordwebhook_change", args=[self.channel.pk]))

        self.assertEqual(page.status_code, 200)
        self.assertNoSecret(page)
        self.assertNotContains(page, "discord.com/api/webhooks")
        self.assertContains(page, "Stored (hidden)")

    def test_admins_who_may_add_still_enter_the_url(self):
        self.staff("fleetops.add_discordwebhook", "fleetops.view_discordwebhook")

        page = self.client.get(reverse("admin:fleetops_discordwebhook_add"))

        self.assertEqual(page.status_code, 200)
        self.assertContains(page, 'name="webhook_url"')
