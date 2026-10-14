# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Models, forms, views and template tags show their text in the language of the viewer.

The tests compile a small catalog for ``zh-hans`` into a temporary directory and put it first in
``LOCALE_PATHS``, so they do not depend on the translations that ship with FleetOps.
"""

import struct
import tempfile
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

from django.conf import settings
from django.contrib.messages import get_messages
from django.contrib.messages.storage.fallback import FallbackStorage
from django.test import RequestFactory, SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import translation

from fleetops import forms_config, models, texts
from fleetops.forms import ManualFleetForm, format_duration
from fleetops.models import AttendanceRecord, FleetOperation, FleetOpsSettings, FleetStartPreset, end_reason_text
from fleetops.templatetags import fleetops_srp
from fleetops.views_config import CONFIG_SECTIONS
from fleetops.views_controls import report_kicks

from . import factories as f

CATALOG = {
    "FleetOps setting": "舰队设置",
    "Tracking interval": "追踪间隔",
    "PAP characters per player": "每名玩家的 PAP 角色数",
    "Fleet type": "舰队类型",
    "Fleet Types": "舰队类型设置",
    "PAP record": "PAP 记录",
    "PAP records": "PAP 记录",
    "Ended manually": "手动结束",
    "Ended automatically: fleet boss handed over to %(name)s": "因 Boss 移交给 %(name)s 而自动结束",
    "another character": "另一个角色",
    "%(minutes)s min": "%(minutes)s 分钟",
    "%(hours)s h %(minutes)02d min": "%(hours)s 小时 %(minutes)02d 分钟",
    "Preset %(name)s deleted.": "预设 %(name)s 已删除。",
    "Kicked %(count)s capsule member.": "已踢出 %(count)s 个蛋。",
    "Nobody was kicked: no members were selected.": "没有踢出任何人：未选择成员。",
    "Roles FCs assign to pilots in a fleet, such as Back Seat FC, and whether a role gives FC credit.": "FC 分配给飞行员的角色。",
    "Approved": "已通过",
    "Pending": "待处理",
    ("fleet status", "Active"): "进行中",
    ("claim history actor", "You"): "你",
    ("claim history actor", "System"): "系统",
}
PLURAL_CATALOG = {
    ("Kicked %(count)s capsule member.", "Kicked %(count)s capsule members."): ["已踢出 %(count)s 个蛋。"],
}


def build_mo(entries, plurals):
    """The bytes of a compiled catalog: ``entries`` maps msgid or (context, msgid) to msgstr."""
    header = "Content-Type: text/plain; charset=UTF-8\nPlural-Forms: nplurals=1; plural=0;\n"
    messages = {b"": header.encode()}
    for key, value in entries.items():
        context, msgid = key if isinstance(key, tuple) else (None, key)
        original = msgid.encode() if context is None else context.encode() + b"\x04" + msgid.encode()
        messages[original] = value.encode()
    for (singular, plural), values in plurals.items():
        messages[singular.encode() + b"\x00" + plural.encode()] = b"\x00".join(value.encode() for value in values)
    keys = sorted(messages)
    count = len(keys)
    originals, translations = b"", b""
    original_table, translation_table = [], []
    for key in keys:
        original_table.append((len(key), len(originals)))
        originals += key + b"\x00"
    for key in keys:
        translation_table.append((len(messages[key]), len(translations)))
        translations += messages[key] + b"\x00"
    start = 7 * 4
    original_offset = start + count * 16
    translation_offset = original_offset + len(originals)
    output = struct.pack("<7I", 0x950412DE, 0, count, start, start + count * 8, 0, 0)
    for length, offset in original_table:
        output += struct.pack("<2I", length, original_offset + offset)
    for length, offset in translation_table:
        output += struct.pack("<2I", length, translation_offset + offset)
    return output + originals + translations


class ChineseCatalogMixin:
    """Activate a small ``zh-hans`` catalog for the test."""

    def setUp(self):
        super().setUp()
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        messages_dir = Path(folder.name) / "zh_Hans" / "LC_MESSAGES"
        messages_dir.mkdir(parents=True)
        (messages_dir / "django.mo").write_bytes(build_mo(CATALOG, PLURAL_CATALOG))
        # Changing LOCALE_PATHS drops the loaded catalogs. Some tests patch the app registry with stand-ins that
        # have no locale folder, so the default catalog is loaded again right after the override ends.
        self.addCleanup(translation.gettext, "FleetOps")
        override = override_settings(LOCALE_PATHS=[folder.name, *settings.LOCALE_PATHS])
        override.enable()
        self.addCleanup(override.disable)
        texts.clear_cache()
        self.addCleanup(texts.clear_cache)


class ModelTextTests(ChineseCatalogMixin, SimpleTestCase):
    def test_names_and_help_texts_follow_the_active_language(self):
        field = FleetOpsSettings._meta.get_field("attendance_limit")

        self.assertEqual(str(FleetOpsSettings._meta.verbose_name), "FleetOps setting")
        with translation.override("zh-hans"):
            self.assertEqual(str(FleetOpsSettings._meta.verbose_name), "舰队设置")
            self.assertEqual(str(field.verbose_name), "每名玩家的 PAP 角色数")
            self.assertEqual(str(FleetOpsSettings._meta.get_field("tracking_interval").verbose_name), "追踪间隔")
        self.assertEqual(str(field.verbose_name), "PAP characters per player")

    def test_a_choice_with_a_context_is_translated(self):
        with translation.override("zh-hans"):
            self.assertEqual(str(FleetOperation.Status.ACTIVE.label), "进行中")
            self.assertEqual(str(FleetOperation.Status.DRAFT.label), "草稿")
        self.assertEqual(str(FleetOperation.Status.ACTIVE.label), "Active")

    def test_the_pap_record_is_named_pap(self):
        self.assertEqual(str(AttendanceRecord._meta.verbose_name), "PAP record")
        self.assertEqual(str(AttendanceRecord._meta.get_field("attendance_value").verbose_name), "PAP value")
        with translation.override("zh-hans"):
            self.assertEqual(str(AttendanceRecord._meta.verbose_name_plural), "PAP 记录")

    def test_every_end_reason_is_shown_in_the_language_of_the_viewer(self):
        reasons = [
            (FleetOperation.EndReason.MANUAL, "", "Ended manually", "手动结束"),
            (
                FleetOperation.EndReason.BOSS_HANDOVER,
                "Ana Lyst",
                "Ended automatically: fleet boss handed over to Ana Lyst",
                "因 Boss 移交给 Ana Lyst 而自动结束",
            ),
            (
                FleetOperation.EndReason.BOSS_HANDOVER,
                "",
                "Ended automatically: fleet boss handed over to another character",
                "因 Boss 移交给 另一个角色 而自动结束",
            ),
        ]
        for reason, detail, english, chinese in reasons:
            with self.subTest(reason=reason, detail=detail):
                self.assertEqual(end_reason_text(reason, detail), english)
                with translation.override("zh-hans"):
                    self.assertEqual(end_reason_text(reason, detail), chinese)

    def test_the_end_reason_of_an_unknown_value_is_empty(self):
        self.assertEqual(end_reason_text("", ""), "")

    def test_the_text_that_is_stored_stays_english(self):
        # The property reads the active language; the stored row never changes.
        operation = FleetOperation(end_reason=FleetOperation.EndReason.MANUAL)
        with translation.override("zh-hans"):
            self.assertEqual(operation.end_reason_text, "手动结束")
            self.assertEqual(operation.end_reason, "manual")

    def test_an_object_name_keeps_its_english_wording(self):
        tier = models.MultiplierTier(multiplier=2, threshold_minutes=90)

        self.assertEqual(str(tier), "2x after 90 min")


class FormTextTests(ChineseCatalogMixin, TestCase):
    def test_labels_of_declared_fields_are_translated(self):
        with translation.override("zh-hans"):
            self.assertEqual(str(ManualFleetForm().fields["fleet_type"].label), "舰队类型")
        self.assertEqual(str(ManualFleetForm().fields["fleet_type"].label), "Fleet type")

    def test_labels_of_model_fields_are_translated(self):
        with translation.override("zh-hans"):
            form = forms_config.FleetOpsSettingsForm()
            self.assertEqual(str(form.fields["tracking_interval"].label), "追踪间隔")
        self.assertEqual(str(forms_config.FleetOpsSettingsForm().fields["tracking_interval"].label), "Tracking interval")

    def test_a_duration_reads_in_the_language_of_the_viewer(self):
        self.assertEqual(format_duration(timedelta(minutes=37)), "37 min")
        self.assertEqual(format_duration(timedelta(minutes=65)), "1 h 05 min")
        with translation.override("zh-hans"):
            self.assertEqual(format_duration(timedelta(minutes=37)), "37 分钟")
            self.assertEqual(format_duration(timedelta(minutes=65)), "1 小时 05 分钟")

    def test_the_choices_of_the_review_filter_are_translated(self):
        from fleetops.forms_srp import ReviewFilterForm

        with translation.override("zh-hans"):
            statuses = dict(ReviewFilterForm.STATUSES)
            self.assertEqual(str(statuses["approved"]), "已通过")
            self.assertEqual(str(statuses["pending"]), "待审核")
        self.assertEqual(str(dict(ReviewFilterForm.STATUSES)["approved"]), "Approved")


class ConfigurationTextTests(ChineseCatalogMixin, SimpleTestCase):
    def test_section_titles_and_descriptions_are_translated(self):
        section = CONFIG_SECTIONS["fleet-types"]
        special = CONFIG_SECTIONS["special-roles"]

        self.assertEqual(str(section["title"]), "Fleet Types")
        with translation.override("zh-hans"):
            self.assertEqual(str(section["title"]), "舰队类型设置")
            self.assertEqual(str(special["description"]), "FC 分配给飞行员的角色。")


class FlashMessageTests(ChineseCatalogMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.fc = f.create_user("fc", perms=f.FC_PERMS)

    def messages_of(self, response):
        return [str(message) for message in get_messages(response.wsgi_request)]

    def delete_preset(self, language):
        preset = FleetStartPreset.objects.create(owner=self.fc, name="Roams")
        self.client.force_login(self.fc)
        if language:
            self.client.cookies.load({settings.LANGUAGE_COOKIE_NAME: language})
        return self.client.post(reverse("fleetops:preset_delete", args=[preset.pk]))

    def test_a_flash_message_is_in_the_language_of_the_user(self):
        self.assertEqual(self.messages_of(self.delete_preset("")), ["Preset Roams deleted."])

    def test_a_flash_message_is_translated_for_a_chinese_user(self):
        self.assertEqual(self.messages_of(self.delete_preset("zh-hans")), ["预设 Roams 已删除。"])


class KickReportTests(ChineseCatalogMixin, SimpleTestCase):
    def report(self, **result):
        request = RequestFactory().post("/")
        request.session = {}
        request._messages = FallbackStorage(request)
        base = {"queued": False, "target_count": 1, "kicked": [1], "failures": [], "skipped": []}
        report_kicks(request, {**base, **result})
        return [str(message) for message in request._messages]

    def test_the_count_chooses_the_form(self):
        self.assertEqual(self.report(), ["Kicked 1 capsule member."])
        self.assertEqual(self.report(kicked=[1, 2], target_count=2), ["Kicked 2 capsule members."])

    def test_kicks_outside_a_system_name_it(self):
        request = RequestFactory().post("/")
        request.session = {}
        request._messages = FallbackStorage(request)
        report_kicks(
            request,
            {"queued": False, "target_count": 2, "kicked": [1, 2], "failures": [3], "skipped": []},
            "Jita",
        )

        self.assertEqual([str(m) for m in request._messages], ["Kicked 2 members outside Jita; 1 failed."])

    def test_a_chinese_viewer_reads_chinese(self):
        with translation.override("zh-hans"):
            self.assertEqual(self.report(), ["已踢出 1 个蛋。"])


class TemplateTagTextTests(ChineseCatalogMixin, SimpleTestCase):
    def test_the_actor_of_a_history_entry_is_translated(self):
        entry = SimpleNamespace(actor_id=None, action="auto_reject")
        viewer = SimpleNamespace(pk=1)

        self.assertEqual(fleetops_srp.srp_actor(entry, viewer), "System")
        with translation.override("zh-hans"):
            self.assertEqual(fleetops_srp.srp_actor(entry, viewer), "系统")
            own = SimpleNamespace(actor_id=1, action="approve")
            self.assertEqual(fleetops_srp.srp_actor(own, viewer), "你")
