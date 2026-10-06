"""Tests for the front-end configuration centre and the FleetOps audit log."""

import json
import unittest
import uuid
from datetime import timedelta
from decimal import Decimal
from unittest import mock

import requests
from django.contrib.messages import get_messages
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from fleetops.forms import FleetOpsSettingsForm, MessageTemplateForm, StartFleetForm
from fleetops.models import (
    AuditLog,
    ChannelPreset,
    CommsPreset,
    DiscordWebhook,
    FleetOperation,
    FleetOpsSettings,
    FleetType,
    MessageTemplate,
    OperationAction,
    PingTarget,
)
from fleetops.services.audit import audit
from fleetops.services.history import prune_history
from fleetops.services.messages import render_operation_messages
from fleetops.services.operations import retry_ping

from . import factories as f

WEBHOOK_SECRET = "Zq7-hunter2-webhook-token-0d9c"
WEBHOOK_URL = f"https://discord.com/api/webhooks/112233445566/{WEBHOOK_SECRET}"

SECTIONS = ["fleet-types", "comms", "channels", "webhooks", "ping-targets", "templates"]

CONFIG_PERMS = f.MEMBER_PERMS + ["fleetops.manage_configuration"]
NO_CONFIG_PERMS = [p for p in f.FC_LEAD_PERMS if p != "fleetops.manage_configuration"]

SECTION_MODELS = {
    "fleet-types": FleetType,
    "comms": CommsPreset,
    "channels": ChannelPreset,
    "webhooks": DiscordWebhook,
    "ping-targets": PingTarget,
    "templates": MessageTemplate,
}


def create_sample_objects():
    """One object per configuration section, keyed by section slug."""
    webhook = DiscordWebhook.objects.create(name="Main Pings", webhook_url=WEBHOOK_URL)
    return {
        "fleet-types": f.fleet_type("StratOp", "2.00"),
        "comms": CommsPreset.objects.create(name="Mumble", channel_name="Ops 1", voice_url="mumble://voice.example.com/ops1"),
        "channels": ChannelPreset.objects.create(name="Logi A", channel_type=ChannelPreset.ChannelType.LOGI, channel_value="LogiA"),
        "webhooks": webhook,
        "ping-targets": PingTarget.objects.create(name="Everyone", target_value="@everyone", webhook=webhook),
        "templates": MessageTemplate.objects.create(
            name="Short Ping", template_type=MessageTemplate.TemplateType.PING, content="{{ fc }} at {{ formup }}"
        ),
    }


def create_payload(section, *, webhook=None):
    """Valid POST data that creates a new object in the given section."""
    return {
        "fleet-types": {"name": "Roam", "short_name": "RM", "point_weight": "1.50", "is_active": "on", "sort_order": "3"},
        "comms": {
            "name": "Discord Voice",
            "channel_name": "Fleet Comms",
            "voice_url": "https://discord.gg/example",
            "description": "Primary voice",
            "is_active": "on",
        },
        "channels": {"name": "Boost A", "channel_type": "boost", "channel_value": "BoostA", "is_active": "on"},
        "webhooks": {"name": "Ops Webhook", "webhook_url": WEBHOOK_URL, "is_active": "on"},
        "ping-targets": {
            "name": "Ops Ping",
            "target_value": "@here",
            "webhook": str(webhook.pk) if webhook else "",
            "is_active": "on",
        },
        "templates": {
            "name": "Compact MOTD",
            "template_type": "motd",
            "content": "<b>{{ fleet_type }}</b> {{ fc }}",
            "is_active": "on",
        },
    }[section]


def edit_payload(section, obj):
    """POST data that edits the given object, changing exactly one visible value."""
    if section == "fleet-types":
        return {"name": obj.name, "short_name": obj.short_name, "point_weight": "3.25", "is_active": "on", "sort_order": "1"}
    if section == "comms":
        return {"name": obj.name, "channel_name": "Ops 2", "voice_url": obj.voice_url, "description": "", "is_active": "on"}
    if section == "channels":
        return {"name": obj.name, "channel_type": obj.channel_type, "channel_value": "LogiB", "is_active": "on"}
    if section == "webhooks":
        return {"name": "Renamed Webhook", "webhook_url": obj.webhook_url, "is_active": "on"}
    if section == "ping-targets":
        return {"name": obj.name, "target_value": "@here", "webhook": str(obj.webhook_id or ""), "is_active": "on"}
    if section == "templates":
        return {"name": obj.name, "template_type": obj.template_type, "content": "{{ fc }} / {{ doctrine }}", "is_active": "on"}
    raise AssertionError(section)


def settings_payload(**overrides):
    data = {
        "attendance_limit": "",
        "tracking_interval": "60",
        "stale_threshold": "180",
        "auto_end_enabled": "on",
        "auto_end_missing_count": "3",
        "incentive_minimum_fleets": "3",
        "data_retention_days": "365",
        "history_alliance_ids": "",
        "srp_auto_create": "on",
        "srp_provider": "auto",
    }
    data.update(overrides)
    return {key: value for key, value in data.items() if value is not None}


def audit_entries(action=None, obj=None):
    qs = AuditLog.objects.all()
    if action:
        qs = qs.filter(action=action)
    if obj is not None:
        qs = qs.filter(object_type=obj.__class__.__name__, object_id=str(obj.pk))
    return list(qs.order_by("pk"))


def message_texts(response):
    return [str(m) for m in get_messages(response.wsgi_request)]


class ConfigurationAccessTests(TestCase):
    def setUp(self):
        self.objects = create_sample_objects()

    def _urls(self):
        urls = [
            ("get", reverse("fleetops:configuration_index")),
            ("get", reverse("fleetops:configuration_settings")),
            ("post", reverse("fleetops:configuration_settings")),
        ]
        for section, obj in self.objects.items():
            urls += [
                ("get", reverse("fleetops:configuration_list", args=[section])),
                ("get", reverse("fleetops:configuration_add", args=[section])),
                ("post", reverse("fleetops:configuration_add", args=[section])),
                ("get", reverse("fleetops:configuration_edit", args=[section, obj.pk])),
                ("post", reverse("fleetops:configuration_edit", args=[section, obj.pk])),
                ("post", reverse("fleetops:configuration_delete", args=[section, obj.pk])),
            ]
        return urls

    def test_every_configuration_url_is_forbidden_without_manage_configuration(self):
        user = f.create_user(perms=NO_CONFIG_PERMS)
        self.client.force_login(user)

        for method, url in self._urls():
            with self.subTest(method=method, url=url):
                response = getattr(self.client, method)(url)
                self.assertEqual(response.status_code, 403)

    def test_forbidden_requests_change_nothing(self):
        user = f.create_user(perms=f.MEMBER_PERMS)
        self.client.force_login(user)

        self.client.post(reverse("fleetops:configuration_add", args=["fleet-types"]), create_payload("fleet-types"))
        self.client.post(reverse("fleetops:configuration_settings"), settings_payload(tracking_interval="300"))
        for section, obj in self.objects.items():
            self.client.post(reverse("fleetops:configuration_delete", args=[section, obj.pk]))

        self.assertFalse(FleetType.objects.filter(name="Roam").exists())
        self.assertEqual(FleetOpsSettings.get_solo().tracking_interval, 60)
        for section, obj in self.objects.items():
            self.assertTrue(SECTION_MODELS[section].objects.filter(pk=obj.pk).exists(), section)
        self.assertFalse(AuditLog.objects.exists())

    def test_anonymous_user_is_redirected_to_login(self):
        for method, url in self._urls():
            with self.subTest(method=method, url=url):
                response = getattr(self.client, method)(url)
                self.assertEqual(response.status_code, 302)
                self.assertIn("login", response["Location"])

    def test_manage_configuration_is_enough_without_staff_status(self):
        user = f.create_user(perms=["fleetops.manage_configuration"])
        self.assertFalse(user.is_staff)
        self.client.force_login(user)

        self.assertEqual(self.client.get(reverse("fleetops:configuration_index")).status_code, 200)
        self.assertEqual(self.client.get(reverse("fleetops:configuration_settings")).status_code, 200)
        for section, obj in self.objects.items():
            with self.subTest(section=section):
                self.assertEqual(self.client.get(reverse("fleetops:configuration_list", args=[section])).status_code, 200)
                self.assertEqual(self.client.get(reverse("fleetops:configuration_add", args=[section])).status_code, 200)
                self.assertEqual(
                    self.client.get(reverse("fleetops:configuration_edit", args=[section, obj.pk])).status_code, 200
                )

    def test_index_lists_every_section_with_counts(self):
        user = f.create_user(perms=CONFIG_PERMS)
        self.client.force_login(user)

        response = self.client.get(reverse("fleetops:configuration_index"))

        self.assertEqual(response.status_code, 200)
        cards = {card["key"]: card["count"] for card in response.context["cards"]}
        self.assertEqual(set(cards), set(SECTIONS))
        for section in SECTIONS:
            self.assertEqual(cards[section], SECTION_MODELS[section].objects.count(), section)
            self.assertContains(response, reverse("fleetops:configuration_list", args=[section]))
        self.assertContains(response, reverse("fleetops:configuration_settings"))

    def test_unknown_section_returns_404(self):
        user = f.create_user(perms=CONFIG_PERMS)
        self.client.force_login(user)

        self.assertEqual(self.client.get(reverse("fleetops:configuration_list", args=["bogus"])).status_code, 404)
        self.assertEqual(self.client.get(reverse("fleetops:configuration_add", args=["bogus"])).status_code, 404)
        self.assertEqual(self.client.get(reverse("fleetops:configuration_edit", args=["bogus", 1])).status_code, 404)
        self.assertEqual(self.client.post(reverse("fleetops:configuration_delete", args=["bogus", 1])).status_code, 404)

    def test_missing_object_returns_404(self):
        user = f.create_user(perms=CONFIG_PERMS)
        self.client.force_login(user)

        for section in SECTIONS:
            with self.subTest(section=section):
                self.assertEqual(
                    self.client.get(reverse("fleetops:configuration_edit", args=[section, 999_999])).status_code, 404
                )
                self.assertEqual(
                    self.client.post(reverse("fleetops:configuration_delete", args=[section, 999_999])).status_code, 404
                )

    def test_delete_requires_post(self):
        user = f.create_user(perms=CONFIG_PERMS)
        self.client.force_login(user)
        fleet_type = self.objects["fleet-types"]

        response = self.client.get(reverse("fleetops:configuration_delete", args=["fleet-types", fleet_type.pk]))

        self.assertEqual(response.status_code, 405)
        self.assertTrue(FleetType.objects.filter(pk=fleet_type.pk).exists())


class ConfigurationCrudTests(TestCase):
    def setUp(self):
        self.admin = f.create_user(perms=CONFIG_PERMS)
        self.client.force_login(self.admin)

    def test_create_each_section_saves_object_and_audits(self):
        webhook = DiscordWebhook.objects.create(name="Existing Hook", webhook_url=WEBHOOK_URL)
        for section in SECTIONS:
            with self.subTest(section=section):
                model = SECTION_MODELS[section]
                payload = create_payload(section, webhook=webhook)
                before = set(model.objects.values_list("pk", flat=True))

                response = self.client.post(reverse("fleetops:configuration_add", args=[section]), payload)

                self.assertRedirects(response, reverse("fleetops:configuration_list", args=[section]))
                created = model.objects.exclude(pk__in=before).get()
                self.assertEqual(created.name, payload["name"])
                entries = audit_entries("configuration.create", created)
                self.assertEqual(len(entries), 1)
                entry = entries[0]
                self.assertEqual(entry.actor, self.admin)
                self.assertEqual(entry.object_type, model.__name__)
                self.assertIsNone(entry.old_value)
                self.assertEqual(entry.new_value["name"], payload["name"])

    def test_created_values_are_stored(self):
        self.client.post(reverse("fleetops:configuration_add", args=["fleet-types"]), create_payload("fleet-types"))
        self.client.post(reverse("fleetops:configuration_add", args=["channels"]), create_payload("channels"))
        self.client.post(reverse("fleetops:configuration_add", args=["templates"]), create_payload("templates"))

        roam = FleetType.objects.get(name="Roam")
        self.assertEqual(roam.short_name, "RM")
        self.assertEqual(roam.point_weight, Decimal("1.50"))
        self.assertTrue(roam.is_active)
        self.assertEqual(roam.sort_order, 3)
        channel = ChannelPreset.objects.get(name="Boost A")
        self.assertEqual(channel.channel_type, ChannelPreset.ChannelType.BOOST)
        self.assertEqual(channel.channel_value, "BoostA")
        template = MessageTemplate.objects.get(name="Compact MOTD")
        self.assertEqual(template.template_type, MessageTemplate.TemplateType.MOTD)
        self.assertEqual(template.content, "<b>{{ fleet_type }}</b> {{ fc }}")
        self.assertFalse(template.is_default)

    def test_edit_each_section_updates_object_and_audits_old_and_new(self):
        objects = create_sample_objects()
        expectations = {
            "fleet-types": ("point_weight", "2.00", "3.25"),
            "comms": ("channel_name", "Ops 1", "Ops 2"),
            "channels": ("channel_value", "LogiA", "LogiB"),
            "webhooks": ("name", "Main Pings", "Renamed Webhook"),
            "ping-targets": ("target_value", "@everyone", "@here"),
            "templates": ("content", "{{ fc }} at {{ formup }}", "{{ fc }} / {{ doctrine }}"),
        }
        for section, obj in objects.items():
            with self.subTest(section=section):
                field, old, new = expectations[section]

                response = self.client.post(
                    reverse("fleetops:configuration_edit", args=[section, obj.pk]), edit_payload(section, obj)
                )

                self.assertRedirects(response, reverse("fleetops:configuration_list", args=[section]))
                obj.refresh_from_db()
                self.assertEqual(str(getattr(obj, field)), new)
                entries = audit_entries("configuration.update", obj)
                self.assertEqual(len(entries), 1)
                self.assertEqual(entries[0].actor, self.admin)
                self.assertEqual(entries[0].old_value[field], old)
                self.assertEqual(entries[0].new_value[field], new)

    def test_delete_each_section_removes_object_and_audits(self):
        objects = create_sample_objects()
        # Delete the ping target before its webhook so each object is removed independently.
        for section in ["fleet-types", "comms", "channels", "ping-targets", "webhooks", "templates"]:
            obj = objects[section]
            model = SECTION_MODELS[section]
            with self.subTest(section=section):
                pk = obj.pk
                name = obj.name

                response = self.client.post(reverse("fleetops:configuration_delete", args=[section, pk]))

                self.assertRedirects(response, reverse("fleetops:configuration_list", args=[section]))
                self.assertFalse(model.objects.filter(pk=pk).exists())
                entry = AuditLog.objects.get(action="configuration.delete", object_type=model.__name__, object_id=str(pk))
                self.assertEqual(entry.actor, self.admin)
                self.assertEqual(entry.old_value["name"], name)
                self.assertIsNone(entry.new_value)

    def test_list_pages_show_configured_items(self):
        objects = create_sample_objects()
        for section, obj in objects.items():
            with self.subTest(section=section):
                response = self.client.get(reverse("fleetops:configuration_list", args=[section]))
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, obj.name)
                self.assertContains(response, reverse("fleetops:configuration_edit", args=[section, obj.pk]))
                self.assertContains(response, reverse("fleetops:configuration_delete", args=[section, obj.pk]))

    def test_list_shows_human_readable_choice_labels(self):
        objects = create_sample_objects()

        channels = self.client.get(reverse("fleetops:configuration_list", args=["channels"]))
        templates = self.client.get(reverse("fleetops:configuration_list", args=["templates"]))

        def row_for(response, obj):
            return next(row for row in response.context["rows"] if row["object"].pk == obj.pk)

        self.assertIn(("Type", "Logi"), row_for(channels, objects["channels"])["values"])
        self.assertIn(("Type", "Ping"), row_for(templates, objects["templates"])["values"])

    def test_list_shows_point_weight_values(self):
        FleetType.objects.create(name="Heavy", point_weight=Decimal("2.50"), sort_order=5)

        response = self.client.get(reverse("fleetops:configuration_list", args=["fleet-types"]))

        self.assertContains(response, "<td>2.50</td>")
        self.assertContains(response, "<td>5</td>")

    # Known issue: numeric values equal to 1 or 0 are rendered as Yes/No badges on list pages
    @unittest.expectedFailure
    def test_list_does_not_render_numeric_one_and_zero_as_booleans(self):
        FleetType.objects.create(name="Standard", point_weight=Decimal("1.00"), sort_order=1)
        FleetType.objects.create(name="Training", point_weight=Decimal("0.00"), sort_order=0)

        response = self.client.get(reverse("fleetops:configuration_list", args=["fleet-types"]))

        self.assertContains(response, "<td>1.00</td>")
        self.assertContains(response, "<td>0.00</td>")

    def test_empty_list_renders(self):
        response = self.client.get(reverse("fleetops:configuration_list", args=["comms"]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "No items configured yet.")

    def test_edit_form_is_prefilled(self):
        objects = create_sample_objects()

        response = self.client.get(reverse("fleetops:configuration_edit", args=["comms", objects["comms"].pk]))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["object"], objects["comms"])
        self.assertContains(response, 'value="Ops 1"')

    def test_invalid_form_is_rerendered_without_saving_or_auditing(self):
        response = self.client.post(
            reverse("fleetops:configuration_add", args=["fleet-types"]),
            {"name": "", "point_weight": "not-a-number", "sort_order": "-1"},
        )

        self.assertEqual(response.status_code, 200)
        form = response.context["form"]
        self.assertIn("name", form.errors)
        self.assertIn("point_weight", form.errors)
        self.assertIn("sort_order", form.errors)
        self.assertFalse(FleetType.objects.exists())
        self.assertFalse(AuditLog.objects.exists())

    def test_invalid_edit_does_not_change_object(self):
        fleet_type = f.fleet_type("StratOp", "2.00")

        response = self.client.post(
            reverse("fleetops:configuration_edit", args=["fleet-types", fleet_type.pk]),
            {"name": "StratOp", "point_weight": "abc", "sort_order": "0"},
        )

        self.assertEqual(response.status_code, 200)
        fleet_type.refresh_from_db()
        self.assertEqual(fleet_type.point_weight, Decimal("2.00"))
        self.assertFalse(AuditLog.objects.exists())

    def test_duplicate_fleet_type_name_is_rejected(self):
        f.fleet_type("Roam")

        response = self.client.post(reverse("fleetops:configuration_add", args=["fleet-types"]), create_payload("fleet-types"))

        self.assertEqual(response.status_code, 200)
        self.assertIn("name", response.context["form"].errors)
        self.assertEqual(FleetType.objects.filter(name="Roam").count(), 1)

    def test_channel_names_are_unique_per_channel_type(self):
        ChannelPreset.objects.create(name="Alpha", channel_type=ChannelPreset.ChannelType.LOGI, channel_value="x")
        url = reverse("fleetops:configuration_add", args=["channels"])

        duplicate = self.client.post(url, {"name": "Alpha", "channel_type": "logi", "channel_value": "y", "is_active": "on"})
        other_type = self.client.post(url, {"name": "Alpha", "channel_type": "boost", "channel_value": "z", "is_active": "on"})

        self.assertEqual(duplicate.status_code, 200)
        self.assertTrue(duplicate.context["form"].errors)
        self.assertEqual(other_type.status_code, 302)
        self.assertEqual(ChannelPreset.objects.filter(name="Alpha").count(), 2)

    def test_invalid_channel_type_is_rejected(self):
        response = self.client.post(
            reverse("fleetops:configuration_add", args=["channels"]),
            {"name": "Odd", "channel_type": "cyno", "channel_value": "x"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("channel_type", response.context["form"].errors)
        self.assertFalse(ChannelPreset.objects.exists())

    def test_template_names_are_unique_per_template_type(self):
        MessageTemplate.objects.create(name="Default", template_type="ping", content="{{ fc }}")
        url = reverse("fleetops:configuration_add", args=["templates"])

        duplicate = self.client.post(url, {"name": "Default", "template_type": "ping", "content": "x", "is_active": "on"})
        other_type = self.client.post(url, {"name": "Default", "template_type": "motd", "content": "x", "is_active": "on"})

        self.assertEqual(duplicate.status_code, 200)
        self.assertTrue(duplicate.context["form"].errors)
        self.assertEqual(other_type.status_code, 302)

    def test_ping_target_rejects_unknown_webhook(self):
        response = self.client.post(
            reverse("fleetops:configuration_add", args=["ping-targets"]),
            {"name": "Ghost", "target_value": "@here", "webhook": "424242", "is_active": "on"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("webhook", response.context["form"].errors)
        self.assertFalse(PingTarget.objects.exists())

    def test_ping_target_without_webhook_is_allowed(self):
        response = self.client.post(reverse("fleetops:configuration_add", args=["ping-targets"]), create_payload("ping-targets"))

        self.assertEqual(response.status_code, 302)
        self.assertIsNone(PingTarget.objects.get(name="Ops Ping").webhook)

    def test_unchecking_active_disables_item(self):
        comms = CommsPreset.objects.create(name="Old Comms")

        self.client.post(
            reverse("fleetops:configuration_edit", args=["comms", comms.pk]),
            {"name": "Old Comms", "channel_name": "", "voice_url": "", "description": ""},
        )

        comms.refresh_from_db()
        self.assertFalse(comms.is_active)
        entry = audit_entries("configuration.update", comms)[0]
        self.assertTrue(entry.old_value["is_active"])
        self.assertFalse(entry.new_value["is_active"])

    def test_inactive_presets_are_hidden_from_start_fleet_form(self):
        active_type = f.fleet_type("Active Type")
        inactive_type = FleetType.objects.create(name="Retired Type", is_active=False)
        CommsPreset.objects.create(name="Live", is_active=True)
        CommsPreset.objects.create(name="Dead", is_active=False)
        fc = f.create_user(perms=f.FC_PERMS)

        form = StartFleetForm(user=fc)

        self.assertIn(active_type, form.fields["fleet_type"].queryset)
        self.assertNotIn(inactive_type, form.fields["fleet_type"].queryset)
        self.assertEqual(list(form.fields["comms"].queryset.values_list("name", flat=True)), ["Live"])


class ConfigurationDeleteReferencedTests(TestCase):
    def setUp(self):
        self.admin = f.create_user(perms=CONFIG_PERMS)
        self.client.force_login(self.admin)
        self.fc = f.create_user(perms=f.FC_PERMS)
        self.objects = create_sample_objects()
        self.motd = MessageTemplate.objects.create(
            name="Short MOTD", template_type=MessageTemplate.TemplateType.MOTD, content="{{ fc }}"
        )
        self.boost = ChannelPreset.objects.create(name="Boost A", channel_type=ChannelPreset.ChannelType.BOOST, channel_value="BoostA")
        self.operation = f.create_operation(
            self.fc,
            status=FleetOperation.Status.CLOSED,
            type_obj=self.objects["fleet-types"],
            comms=self.objects["comms"],
            logi_channel=self.objects["channels"],
            boost_channel=self.boost,
            ping_target=self.objects["ping-targets"],
            ping_template=self.objects["templates"],
            motd_template=self.motd,
            ping_text="Rendered ping",
            motd_text="Rendered motd",
        )

    def _delete(self, section, obj):
        return self.client.post(reverse("fleetops:configuration_delete", args=[section, obj.pk]))

    def test_deleting_fleet_type_used_by_operations_is_refused_with_message(self):
        fleet_type = self.objects["fleet-types"]

        response = self._delete("fleet-types", fleet_type)

        self.assertRedirects(response, reverse("fleetops:configuration_list", args=["fleet-types"]))
        self.assertTrue(FleetType.objects.filter(pk=fleet_type.pk).exists())
        self.assertTrue(any("cannot be deleted" in text for text in message_texts(response)))
        self.assertFalse(AuditLog.objects.filter(action="configuration.delete").exists())
        self.operation.refresh_from_db()
        self.assertEqual(self.operation.fleet_type, fleet_type)

    def test_refused_delete_message_is_shown_on_list_page(self):
        response = self.client.post(
            reverse("fleetops:configuration_delete", args=["fleet-types", self.objects["fleet-types"].pk]), follow=True
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "referenced by existing fleet data")

    def test_unused_fleet_type_can_be_deleted(self):
        spare = FleetType.objects.create(name="Spare")

        self._delete("fleet-types", spare)

        self.assertFalse(FleetType.objects.filter(pk=spare.pk).exists())

    def test_deleting_presets_used_by_operations_keeps_operation_history(self):
        for section, obj in [
            ("comms", self.objects["comms"]),
            ("channels", self.objects["channels"]),
            ("channels", self.boost),
            ("ping-targets", self.objects["ping-targets"]),
            ("templates", self.objects["templates"]),
            ("templates", self.motd),
        ]:
            with self.subTest(section=section, name=obj.name):
                response = self._delete(section, obj)
                self.assertEqual(response.status_code, 302)
                self.assertFalse(type(obj).objects.filter(pk=obj.pk).exists())

        self.operation.refresh_from_db()
        self.assertEqual(self.operation.status, FleetOperation.Status.CLOSED)
        self.assertIsNone(self.operation.comms)
        self.assertIsNone(self.operation.logi_channel)
        self.assertIsNone(self.operation.boost_channel)
        self.assertIsNone(self.operation.ping_target)
        self.assertIsNone(self.operation.ping_template)
        self.assertIsNone(self.operation.motd_template)
        self.assertEqual(self.operation.ping_text, "Rendered ping")
        self.assertEqual(self.operation.motd_text, "Rendered motd")

    def test_deleting_webhook_unlinks_ping_target(self):
        target = self.objects["ping-targets"]

        response = self._delete("webhooks", self.objects["webhooks"])

        self.assertEqual(response.status_code, 302)
        target.refresh_from_db()
        self.assertIsNone(target.webhook)

    def test_operation_detail_still_renders_after_presets_are_deleted(self):
        for section in ["comms", "channels", "ping-targets", "webhooks", "templates"]:
            self._delete(section, self.objects[section])
        self.client.force_login(self.fc)

        response = self.client.get(reverse("fleetops:operation_detail", args=[self.operation.uuid]))

        self.assertEqual(response.status_code, 200)


class SettingsFormTests(TestCase):
    def setUp(self):
        self.admin = f.create_user(perms=CONFIG_PERMS)
        self.client.force_login(self.admin)
        self.url = reverse("fleetops:configuration_settings")
        self.settings = FleetOpsSettings.get_solo()

    def _post(self, **overrides):
        return self.client.post(self.url, settings_payload(**overrides))

    def test_settings_page_renders_current_values(self):
        f.settings(tracking_interval=120)

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["form"].initial["tracking_interval"], 120)

    def test_valid_update_saves_and_redirects_to_index(self):
        response = self._post(
            attendance_limit="2",
            tracking_interval="90",
            stale_threshold="240",
            auto_end_missing_count="5",
            incentive_minimum_fleets="4",
            data_retention_days="400",
            history_alliance_ids="3000001, 3000002",
            srp_provider="allianceauth_builtin",
        )

        self.assertRedirects(response, reverse("fleetops:configuration_index"))
        obj = FleetOpsSettings.get_solo()
        self.assertEqual(obj.attendance_limit, 2)
        self.assertEqual(obj.tracking_interval, 90)
        self.assertEqual(obj.stale_threshold, 240)
        self.assertEqual(obj.auto_end_missing_count, 5)
        self.assertEqual(obj.incentive_minimum_fleets, 4)
        self.assertEqual(obj.data_retention_days, 400)
        self.assertEqual(obj.history_alliance_ids, "3000001, 3000002")
        self.assertEqual(obj.srp_provider, "allianceauth_builtin")
        self.assertEqual(FleetOpsSettings.objects.count(), 1)

    def test_update_is_audited_with_old_and_new_values(self):
        self._post(tracking_interval="90", attendance_limit="2")

        entry = AuditLog.objects.get(action="configuration.update", object_type="FleetOpsSettings")
        self.assertEqual(entry.actor, self.admin)
        self.assertEqual(entry.object_id, str(self.settings.pk))
        self.assertEqual(entry.old_value["tracking_interval"], 60)
        self.assertEqual(entry.new_value["tracking_interval"], 90)
        self.assertIsNone(entry.old_value["attendance_limit"])
        self.assertEqual(entry.new_value["attendance_limit"], 2)
        self.assertIsNotNone(entry.created_at)

    def test_minimum_allowed_values_are_accepted(self):
        response = self._post(
            tracking_interval="30",
            stale_threshold="60",
            auto_end_missing_count="2",
            data_retention_days="365",
            incentive_minimum_fleets="1",
            attendance_limit="1",
        )

        self.assertEqual(response.status_code, 302)
        obj = FleetOpsSettings.get_solo()
        self.assertEqual(
            (obj.tracking_interval, obj.stale_threshold, obj.auto_end_missing_count, obj.data_retention_days),
            (30, 60, 2, 365),
        )

    def test_values_below_minimum_are_rejected(self):
        cases = {
            "tracking_interval": "29",
            "stale_threshold": "59",
            "auto_end_missing_count": "1",
            "data_retention_days": "364",
            "incentive_minimum_fleets": "0",
            "attendance_limit": "0",
        }
        for field, value in cases.items():
            with self.subTest(field=field, value=value):
                response = self._post(**{field: value})

                self.assertEqual(response.status_code, 200)
                self.assertIn(field, response.context["form"].errors)
                self.assertEqual(getattr(FleetOpsSettings.get_solo(), field), getattr(self.settings, field))
        self.assertFalse(AuditLog.objects.exists())

    def test_retention_below_a_year_is_never_stored(self):
        for value in ["0", "30", "364"]:
            with self.subTest(value=value):
                self._post(data_retention_days=value)
                self.assertGreaterEqual(FleetOpsSettings.get_solo().data_retention_days, 365)

    def test_blank_attendance_limit_means_unlimited(self):
        f.settings(attendance_limit=3)

        response = self._post(attendance_limit="")

        self.assertEqual(response.status_code, 302)
        self.assertIsNone(FleetOpsSettings.get_solo().attendance_limit)

    def test_garbage_numbers_are_rejected_without_error(self):
        response = self._post(tracking_interval="abc", stale_threshold="-5", data_retention_days="1e9", attendance_limit="1.5")

        self.assertEqual(response.status_code, 200)
        errors = response.context["form"].errors
        for field in ("tracking_interval", "stale_threshold", "data_retention_days", "attendance_limit"):
            self.assertIn(field, errors)
        self.assertEqual(FleetOpsSettings.get_solo().tracking_interval, 60)

    def test_missing_required_fields_are_rejected(self):
        response = self.client.post(self.url, {})

        self.assertEqual(response.status_code, 200)
        self.assertIn("tracking_interval", response.context["form"].errors)
        self.assertFalse(AuditLog.objects.exists())

    def test_unchecked_boxes_turn_flags_off(self):
        f.settings(auto_end_enabled=True, srp_auto_create=True, incentive_enabled=True)

        self._post(auto_end_enabled=None, srp_auto_create=None)

        obj = FleetOpsSettings.get_solo()
        self.assertFalse(obj.auto_end_enabled)
        self.assertFalse(obj.srp_auto_create)
        self.assertFalse(obj.incentive_enabled)

    # Known issue: very large retention values are accepted and overflow the history cutoff date
    @unittest.expectedFailure
    def test_huge_retention_value_does_not_break_history(self):
        self._post(data_retention_days="1000000")
        member = f.create_user(perms=f.MEMBER_PERMS)
        client = Client(raise_request_exception=False)
        client.force_login(member)

        response = client.get(reverse("fleetops:attendance_history_me"))

        self.assertEqual(response.status_code, 200)
        prune_history(dry_run=True)

    def test_form_level_validation_matches_view(self):
        form = FleetOpsSettingsForm(settings_payload(data_retention_days="100"), instance=FleetOpsSettings.get_solo())
        self.assertFalse(form.is_valid())
        self.assertIn("data_retention_days", form.errors)


class FleetTypeWeightSnapshotTests(TestCase):
    def setUp(self):
        self.admin = f.create_user(perms=CONFIG_PERMS)
        self.fc = f.create_user(perms=f.FC_PERMS)
        self.fleet_type = f.fleet_type("StratOp", "2.00")

    def test_changing_weight_does_not_change_existing_snapshots(self):
        active = f.create_operation(self.fc, type_obj=self.fleet_type)
        closed = f.create_operation(self.fc, type_obj=self.fleet_type, status=FleetOperation.Status.CLOSED)
        self.client.force_login(self.admin)

        response = self.client.post(
            reverse("fleetops:configuration_edit", args=["fleet-types", self.fleet_type.pk]),
            {"name": "StratOp", "short_name": "", "point_weight": "5.00", "is_active": "on", "sort_order": "0"},
        )

        self.assertEqual(response.status_code, 302)
        self.fleet_type.refresh_from_db()
        self.assertEqual(self.fleet_type.point_weight, Decimal("5.00"))
        for operation in (active, closed):
            operation.refresh_from_db()
            self.assertEqual(operation.fleet_point_weight_snapshot, Decimal("2.00"))
            self.assertEqual(operation.fleet_type, self.fleet_type)

    def test_weight_change_is_audited(self):
        self.client.force_login(self.admin)

        self.client.post(
            reverse("fleetops:configuration_edit", args=["fleet-types", self.fleet_type.pk]),
            {"name": "StratOp", "short_name": "", "point_weight": "0.50", "is_active": "on", "sort_order": "0"},
        )

        entry = audit_entries("configuration.update", self.fleet_type)[0]
        self.assertEqual(entry.old_value["point_weight"], "2.00")
        self.assertEqual(entry.new_value["point_weight"], "0.50")

    def test_disabling_fleet_type_keeps_existing_operations(self):
        operation = f.create_operation(self.fc, type_obj=self.fleet_type, status=FleetOperation.Status.CLOSED)
        self.client.force_login(self.admin)

        self.client.post(
            reverse("fleetops:configuration_edit", args=["fleet-types", self.fleet_type.pk]),
            {"name": "StratOp", "short_name": "", "point_weight": "2.00", "sort_order": "0"},
        )

        operation.refresh_from_db()
        self.assertEqual(operation.fleet_type, self.fleet_type)
        self.assertEqual(operation.fleet_point_weight_snapshot, Decimal("2.00"))


class WebhookSecretTests(TestCase):
    def setUp(self):
        self.admin = f.create_user(perms=CONFIG_PERMS + ["fleetops.view_audit_log"])
        self.client.force_login(self.admin)
        self.client.post(reverse("fleetops:configuration_add", args=["webhooks"]), create_payload("webhooks"))
        self.webhook = DiscordWebhook.objects.get(name="Ops Webhook")
        self.client.post(
            reverse("fleetops:configuration_add", args=["ping-targets"]), create_payload("ping-targets", webhook=self.webhook)
        )
        self.target = PingTarget.objects.get(name="Ops Ping")
        rotated = f"{WEBHOOK_URL}-rotated"
        self.client.post(
            reverse("fleetops:configuration_edit", args=["webhooks", self.webhook.pk]),
            {"name": "Ops Webhook", "webhook_url": rotated, "is_active": "on"},
        )
        self.webhook.refresh_from_db()

    def assertNoSecret(self, response):
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(WEBHOOK_SECRET, response.content.decode())

    def test_setup_stored_the_webhook(self):
        self.assertEqual(self.webhook.webhook_url, f"{WEBHOOK_URL}-rotated")
        self.assertEqual(self.target.webhook, self.webhook)

    def test_audit_json_never_contains_webhook_url(self):
        spare = DiscordWebhook.objects.create(name="Spare", webhook_url=WEBHOOK_URL)
        self.client.post(reverse("fleetops:configuration_delete", args=["webhooks", spare.pk]))

        entries = AuditLog.objects.all()
        self.assertGreaterEqual(entries.count(), 4)
        for entry in entries:
            payload = json.dumps([entry.old_value, entry.new_value, entry.reason])
            self.assertNotIn(WEBHOOK_SECRET, payload)
            self.assertNotIn("discord.com/api/webhooks", payload)

        update = audit_entries("configuration.update", self.webhook)[0]
        self.assertEqual(update.old_value["webhook_url"], "***redacted***")
        self.assertEqual(update.new_value["webhook_url"], "***redacted***")
        deleted = AuditLog.objects.get(action="configuration.delete", object_type="DiscordWebhook")
        self.assertEqual(deleted.old_value["webhook_url"], "***redacted***")

    def test_audit_log_page_hides_webhook_url(self):
        self.assertNoSecret(self.client.get(reverse("fleetops:audit_log")))

    def test_webhook_list_hides_url(self):
        response = self.client.get(reverse("fleetops:configuration_list", args=["webhooks"]))
        self.assertNoSecret(response)
        self.assertContains(response, "Ops Webhook")

    def test_ping_target_pages_hide_url(self):
        self.assertNoSecret(self.client.get(reverse("fleetops:configuration_list", args=["ping-targets"])))
        self.assertNoSecret(self.client.get(reverse("fleetops:configuration_add", args=["ping-targets"])))
        self.assertNoSecret(self.client.get(reverse("fleetops:configuration_edit", args=["ping-targets", self.target.pk])))

    def test_configuration_index_and_settings_hide_url(self):
        self.assertNoSecret(self.client.get(reverse("fleetops:configuration_index")))
        self.assertNoSecret(self.client.get(reverse("fleetops:configuration_settings")))

    def test_webhook_edit_form_shows_url(self):
        response = self.client.get(reverse("fleetops:configuration_edit", args=["webhooks", self.webhook.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, WEBHOOK_SECRET)

    def test_save_message_does_not_echo_url(self):
        response = self.client.post(
            reverse("fleetops:configuration_edit", args=["webhooks", self.webhook.pk]),
            {"name": "Ops Webhook", "webhook_url": WEBHOOK_URL, "is_active": "on"},
            follow=True,
        )
        self.assertNoSecret(response)

    def test_start_fleet_page_hides_url(self):
        fc = f.create_user(perms=f.FC_PERMS)
        self.client.force_login(fc)

        response = self.client.get(reverse("fleetops:start_fleet"))

        self.assertNoSecret(response)
        self.assertContains(response, "Ops Ping")

    def test_operation_detail_hides_url(self):
        fc = f.create_user(perms=f.FC_PERMS)
        operation = f.create_operation(fc, status=FleetOperation.Status.CLOSED, ping_target=self.target)
        self.client.force_login(fc)

        self.assertNoSecret(self.client.get(reverse("fleetops:operation_detail", args=[operation.uuid])))

    # Known issue: a failed Discord ping stores the requests exception text, which contains the webhook URL
    @unittest.expectedFailure
    def test_failed_ping_does_not_expose_webhook_url(self):
        fc = f.create_user(perms=f.FC_PERMS)
        pilot = f.create_user(perms=f.MEMBER_PERMS)
        operation = f.create_operation(
            fc, status=FleetOperation.Status.CLOSED, ping_target=self.target, ping_text="Form up"
        )
        f.add_attendance(operation, pilot)
        path = self.webhook.webhook_url.split("discord.com", 1)[1]
        error = requests.ConnectionError(
            "HTTPSConnectionPool(host='discord.com', port=443): Max retries exceeded with url: "
            f"{path} (Caused by NewConnectionError('Failed to establish a new connection: [Errno 111] Connection refused'))"
        )

        with mock.patch("fleetops.providers.pings.requests.post", side_effect=error) as post:
            action = retry_ping(operation)

        post.assert_called_once()
        self.assertEqual(action.status, OperationAction.Status.FAILED)
        self.assertNotIn(WEBHOOK_SECRET, action.error_message)
        self.client.force_login(pilot)
        self.assertNoSecret(self.client.get(reverse("fleetops:operation_detail", args=[operation.uuid])))


class MessageTemplateValidationTests(TestCase):
    def setUp(self):
        self.admin = f.create_user(perms=CONFIG_PERMS)
        self.client.force_login(self.admin)
        self.url = reverse("fleetops:configuration_add", args=["templates"])

    def test_valid_template_with_variables_is_saved(self):
        response = self.client.post(
            self.url,
            {
                "name": "Rich Ping",
                "template_type": "ping",
                "content": "{{ ping_target }} {{ fleet_type }}{% if comms_link %} {{ comms_link }}{% endif %}",
                "is_default": "on",
                "is_active": "on",
            },
        )

        self.assertEqual(response.status_code, 302)
        template = MessageTemplate.objects.get(name="Rich Ping")
        self.assertTrue(template.is_default)

    def test_saved_template_is_used_when_selected(self):
        self.client.post(
            self.url,
            {"name": "Short", "template_type": "ping", "content": "Fleet by {{ fc }} at {{ formup }}", "is_active": "on"},
        )
        fc = f.create_user(perms=f.FC_PERMS)
        operation = f.create_operation(fc, ping_template=MessageTemplate.objects.get(name="Short"), formup="Amarr")

        ping, _ = render_operation_messages(operation)

        self.assertEqual(ping, f"Fleet by {operation.fc_character_name} at Amarr")

    # Known issue: marking a template as default keeps the previous default, which wins by name ordering
    @unittest.expectedFailure
    def test_newly_marked_default_template_is_used(self):
        self.assertTrue(MessageTemplate.objects.filter(name="Default Ping", is_default=True).exists())
        self.client.post(
            self.url,
            {
                "name": "Short Ping",
                "template_type": "ping",
                "content": "NEW DEFAULT {{ formup }}",
                "is_default": "on",
                "is_active": "on",
            },
        )
        fc = f.create_user(perms=f.FC_PERMS)
        operation = f.create_operation(fc, formup="Amarr")

        ping, _ = render_operation_messages(operation)

        self.assertEqual(ping, "NEW DEFAULT Amarr")

    def test_blank_content_is_rejected(self):
        response = self.client.post(self.url, {"name": "Empty", "template_type": "ping", "content": ""})

        self.assertEqual(response.status_code, 200)
        self.assertIn("content", response.context["form"].errors)

    # Known issue: message templates are saved without checking Django template syntax
    @unittest.expectedFailure
    def test_invalid_template_syntax_is_rejected(self):
        response = self.client.post(
            self.url, {"name": "Broken", "template_type": "ping", "content": "{% if %}Fleet up", "is_active": "on"}
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("content", response.context["form"].errors)
        self.assertFalse(MessageTemplate.objects.filter(name="Broken").exists())
        self.assertFalse(AuditLog.objects.exists())

    # Known issue: message templates are saved without checking Django template syntax
    @unittest.expectedFailure
    def test_form_rejects_common_template_mistakes(self):
        broken = [
            "{% if fc %}missing endif",
            "{% endif %}",
            "{% unknown_tag %}",
            "{{ fc|no_such_filter }}",
            "{% for x in %}{% endfor %}",
        ]
        accepted = []
        for content in broken:
            form = MessageTemplateForm({"name": "T", "template_type": "motd", "content": content, "is_active": "on"})
            if form.is_valid():
                accepted.append(content)
        self.assertEqual(accepted, [])

    # Known issue: a stored template with invalid syntax makes the start-fleet preview crash with HTTP 500
    @unittest.expectedFailure
    def test_preview_with_invalid_stored_template_does_not_crash(self):
        fc = f.create_user(perms=f.FC_PERMS)
        fleet_type = f.fleet_type()
        broken = MessageTemplate.objects.create(name="Broken", template_type="ping", content="{% if %}x")
        client = Client(raise_request_exception=False)
        client.force_login(fc)

        response = client.post(
            reverse("fleetops:preview_fleet"),
            {
                "request_id": str(uuid.uuid4()),
                "operation_mode": "full",
                "fc_character_id": str(f.main_of(fc).character_id),
                "fleet_type": str(fleet_type.pk),
                "formup": "Jita",
                "ping_template": str(broken.pk),
            },
        )

        self.assertNotEqual(response.status_code, 500)


class AuditLogPageTests(TestCase):
    def setUp(self):
        self.auditor = f.create_user(perms=f.MEMBER_PERMS + ["fleetops.view_audit_log"])
        self.url = reverse("fleetops:audit_log")

    def test_requires_view_audit_log(self):
        everything_else = [p for p in f.FC_LEAD_PERMS if p != "fleetops.view_audit_log"]
        for perms in (f.MEMBER_PERMS, CONFIG_PERMS, everything_else):
            with self.subTest(perms=perms):
                self.client.force_login(f.create_user(perms=perms))
                self.assertEqual(self.client.get(self.url).status_code, 403)

    def test_anonymous_is_redirected_to_login(self):
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 302)
        self.assertIn("login", response["Location"])

    def test_lists_actor_action_object_and_reason(self):
        fleet_type = f.fleet_type("Audited Type")
        audit(self.auditor, "configuration.update", fleet_type, {"a": 1}, {"a": 2}, reason="Raised weight")
        audit(None, "fleet.auto_end", fleet_type)
        self.client.force_login(self.auditor)

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "configuration.update")
        self.assertContains(response, self.auditor.username)
        self.assertContains(response, f"FleetType #{fleet_type.pk}")
        self.assertContains(response, "Raised weight")
        self.assertContains(response, "System")

    def test_newest_entries_first(self):
        fleet_type = f.fleet_type()
        old = audit(self.auditor, "first.action", fleet_type)
        AuditLog.objects.filter(pk=old.pk).update(created_at=timezone.now() - timedelta(days=1))
        audit(self.auditor, "second.action", fleet_type)
        self.client.force_login(self.auditor)

        content = self.client.get(self.url).content.decode()

        self.assertLess(content.index("second.action"), content.index("first.action"))

    def test_html_in_reason_is_escaped(self):
        audit(self.auditor, "configuration.update", f.fleet_type(), reason="<script>alert(1)</script>")
        self.client.force_login(self.auditor)

        response = self.client.get(self.url)

        self.assertNotContains(response, "<script>alert(1)</script>")
        self.assertContains(response, "&lt;script&gt;")

    def test_bad_query_parameters_do_not_error(self):
        audit(self.auditor, "configuration.update", f.fleet_type())
        self.client.force_login(self.auditor)
        params = [
            {"page": "abc"},
            {"page": "-1"},
            {"page": "0"},
            {"page": "999999"},
            {"page": "1.5"},
            {"action": "<script>"},
            {"actor": "not-a-number"},
            {"actor": "99999999"},
            {"object_type": "x" * 500},
            {"year": "abc", "month": "13"},
            {"date_from": "2026-99-99", "date_to": "yesterday"},
        ]
        for query in params:
            with self.subTest(query=query):
                self.assertEqual(self.client.get(self.url, query).status_code, 200)

    def test_configuration_changes_appear_on_audit_page(self):
        admin = f.create_user(perms=CONFIG_PERMS + ["fleetops.view_audit_log"])
        self.client.force_login(admin)
        self.client.post(reverse("fleetops:configuration_add", args=["comms"]), create_payload("comms"))
        comms = CommsPreset.objects.get(name="Discord Voice")

        response = self.client.get(self.url)

        self.assertContains(response, "configuration.create")
        self.assertContains(response, f"CommsPreset #{comms.pk}")

    # Known issue: the audit log page shows only the newest 500 entries and has no pagination
    @unittest.expectedFailure
    def test_old_entries_are_reachable_through_pagination(self):
        fleet_type = f.fleet_type()
        oldest = audit(self.auditor, "oldest.marker", fleet_type)
        AuditLog.objects.filter(pk=oldest.pk).update(created_at=timezone.now() - timedelta(days=30))
        AuditLog.objects.bulk_create(
            AuditLog(actor=self.auditor, action="bulk.entry", object_type="FleetType", object_id=str(fleet_type.pk))
            for _ in range(520)
        )
        self.client.force_login(self.auditor)

        found = False
        previous = None
        for page in range(1, 30):
            content = self.client.get(self.url, {"page": page}).content.decode()
            if "oldest.marker" in content:
                found = True
                break
            if content == previous:
                break
            previous = content
        self.assertTrue(found)


class AuditServiceTests(TestCase):
    def test_records_actor_action_object_and_values(self):
        user = f.create_user()
        fleet_type = f.fleet_type()

        entry = audit(user, "configuration.update", fleet_type, {"point_weight": "1.00"}, {"point_weight": "2.00"}, reason="r")

        entry.refresh_from_db()
        self.assertEqual(entry.actor, user)
        self.assertEqual(entry.action, "configuration.update")
        self.assertEqual(entry.object_type, "FleetType")
        self.assertEqual(entry.object_id, str(fleet_type.pk))
        self.assertEqual(entry.old_value, {"point_weight": "1.00"})
        self.assertEqual(entry.new_value, {"point_weight": "2.00"})
        self.assertEqual(entry.reason, "r")
        self.assertIsNotNone(entry.created_at)

    def test_actor_deletion_keeps_entry(self):
        user = f.create_user()
        entry = audit(user, "configuration.create", f.fleet_type())
        user.profile.main_character = None
        user.profile.save()

        user.delete()

        entry.refresh_from_db()
        self.assertIsNone(entry.actor)
        self.assertEqual(entry.action, "configuration.create")
