# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""The pages follow the language of the user, with the shipped Simplified Chinese catalog.

A user who holds every FleetOps permission opens every page in Chinese, with fleets in each state, claims
in each status and the administration. Each page has to answer, to contain Chinese text, and not to show
an English interface text: a word of the page that is neither data (a name, a number), nor a name the
translation keeps (Ping, MOTD, Discord), nor part of a message of the catalog.
"""

import importlib
import re
import sys
import uuid
from datetime import datetime, timedelta, timezone as dt_timezone
from html.parser import HTMLParser
from unittest import mock

from django.conf import settings
from django.contrib.auth.models import Permission
from django.db import models
from django.test import TestCase, tag
from django.urls import reverse
from django.utils import timezone, translation

from fleetops import texts
from fleetops.models import (
    AttendanceRecord,
    AuditLog,
    ChannelPreset,
    CommsPreset,
    FleetMemberEvent,
    FleetOperation,
    FleetStartPreset,
    IncentivePeriod,
    OperationAction,
    OperationActionAttempt,
    OperationRoleAssignment,
    SrpClaim,
    SrpClaimEvent,
)
from fleetops.services import incentives
from fleetops.views_config import CONFIG_SECTIONS

from . import factories as f
from . import i18n_utils as po

LANGUAGE = "zh-hans"
MONTH = (2026, 3)
STARTED = datetime(2026, 3, 10, 12, 0, tzinfo=dt_timezone.utc)
CJK = re.compile(r"[\u4e00-\u9fff]")
URL = re.compile(r"https?://\S+")
WORD = re.compile(r"[A-Za-z][A-Za-z']{2,}")
SKIPPED_TAGS = {"script", "style", "noscript", "template", "textarea"}
TEXT_ATTRIBUTES = {"title", "placeholder", "aria-label", "alt", "data-confirm"}

# Names and abbreviations that stay in English whatever the language is.
KEPT_WORDS = {
    "FleetOps",
    "Alliance",
    "Auth",
    "Django",
    "Discord",
    "Mumble",
    "Webhook",
    "zKillboard",
    "EveWho",
    "ESI",
    "EVE",
    "ISK",
    "UTC",
    "PAP",
    "SRP",
    "MOTD",
    "CSV",
    "Excel",
    "HTTP",
    "HTTPS",
    "URL",
    "SDE",
    "KM",
    "FC",
}


# The pages show example wing and squad names in the layout box of a structure template.
EXAMPLE_NAMES = {"Alpha", "Bravo", "Charlie", "Delta"}

# Text that comes from ESI or from people stays as it was written.
ESI_TEXT = {"Service Unavailable", "lead", "pilot", "other"}


def all_fleetops_permissions():
    codenames = Permission.objects.filter(content_type__app_label="fleetops").values_list("codename", flat=True)
    return [f"fleetops.{codename}" for codename in codenames]


def catalog_words():
    """The English words that the Chinese messages keep (Ping, Boss, names of ships and apps)."""
    words = set()
    for path, _mo in po.catalog_pairs():
        if path.parents[1].name != "zh_Hans":
            continue
        for entry in po.read_po(path):
            for text in entry.msgstr.values():
                words.update(WORD.findall(text))
    return words


VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}


class VisibleText(HTMLParser):
    """The text a person reads in the FleetOps part of a page: text nodes, option labels and attribute texts.

    The FleetOps menu (``#nav-left``) and the content (``div.fleetops``) are read; the rest is the chrome of
    Alliance Auth, which has its own catalogs.
    """

    def __init__(self, parts=("nav", "content")):
        super().__init__(convert_charrefs=True)
        self.chunks = []
        self.parts = parts
        self._skip = 0
        self._stack = []
        self._scope_depth = None

    def _in_scope(self, attrs):
        attributes = dict(attrs)
        if "nav" in self.parts and attributes.get("id") == "nav-left":
            return True
        return "content" in self.parts and "fleetops" in (attributes.get("class") or "").split()

    def handle_starttag(self, tag, attrs):
        if tag in VOID_TAGS:
            if self._scope_depth is not None:
                self._collect_attributes(tag, attrs)
            return
        self._stack.append(tag)
        if self._scope_depth is None and self._in_scope(attrs):
            self._scope_depth = len(self._stack)
        if self._scope_depth is None:
            return
        if tag in SKIPPED_TAGS:
            self._skip += 1
        self._collect_attributes(tag, attrs)

    def _collect_attributes(self, tag, attrs):
        attributes = dict(attrs)
        for name, value in attrs:
            if not value:
                continue
            if name in TEXT_ATTRIBUTES or (name.startswith("data-label-")):
                self.chunks.append(value)
            elif name == "value" and tag == "input" and attributes.get("type") in {"submit", "button"}:
                self.chunks.append(value)

    def handle_endtag(self, tag):
        if tag in VOID_TAGS or not self._stack:
            return
        if tag in SKIPPED_TAGS and self._skip and self._scope_depth is not None:
            self._skip -= 1
        while self._stack and self._stack.pop() != tag:
            pass
        if self._scope_depth is not None and len(self._stack) < self._scope_depth:
            self._scope_depth = None

    def handle_data(self, data):
        if self._scope_depth is not None and not self._skip and data.strip():
            self.chunks.append(data)


def visible_text(html, parts=("nav", "content")):
    parser = VisibleText(parts)
    parser.feed(html)
    return "\n".join(parser.chunks)


def stored_strings(value, found):
    """Every string inside a JSON value."""
    if isinstance(value, uuid.UUID):
        found.add(str(value))
    elif isinstance(value, str):
        found.add(value)
    elif isinstance(value, dict):
        for key, item in value.items():
            stored_strings(key, found)
            stored_strings(item, found)
    elif isinstance(value, (list, tuple)):
        for item in value:
            stored_strings(item, found)


def database_strings():
    """The text values of the database: names and notes that people typed are not interface text."""
    from django.apps import apps

    found = set()
    for model in apps.get_models():
        if model._meta.app_label not in {"fleetops", "eveonline", "auth", "authentication", "esi"}:
            continue
        fields = [
            field
            for field in model._meta.concrete_fields
            if isinstance(field, (models.CharField, models.TextField, models.JSONField, models.UUIDField))
        ]
        if not fields:
            continue
        for row in model.objects.values_list(*[field.name for field in fields]):
            for value in row:
                stored_strings(value, found)
    # Codes such as "active" or "pending" are not names; the pages show them as translated labels.
    return {text for text in found if len(text) >= 3 and not re.fullmatch(r"[a-z_]+", text)}


def data_regex(values):
    """One pattern for all data values, matched as whole words."""
    ordered = sorted(values, key=len, reverse=True)
    return re.compile(r"(?<![A-Za-z0-9])(?:" + "|".join(re.escape(value) for value in ordered) + r")(?![A-Za-z0-9])")


class PageWorld:
    """Fleets, claims and settings for the pages."""

    @classmethod
    def build(cls):
        now = timezone.now()
        cls.lead = f.create_user("lead", perms=all_fleetops_permissions(), main_name="Lena Lead")
        cls.fc = f.create_user("fc", perms=[*f.FC_PERMS, "fleetops.request_srp"], main_name="Finn Fc")
        cls.pilot = f.member("pilot", perms=f.SRP_MEMBER_PERMS, main_name="Pia Pilot")
        cls.pilot_alt = f.add_alt(cls.pilot, "Pia Alt")
        cls.other = f.member("other", corporation=f.OTHER_CORP, perms=f.SRP_MEMBER_PERMS, main_name="Otto Other")
        lead_main = f.main_of(cls.lead)
        fc_main = f.main_of(cls.fc)
        pilot_main = f.main_of(cls.pilot)

        f.settings(incentive_enabled=True)
        f.multiplier_tiers((60, 2), (120, 3))
        f.statistics_target(*MONTH, "1.50", minimum=1)
        f.srp_settings(personal_monthly_cap=100_000_000, corporation_monthly_cap=500_000_000)
        f.payout_rule(f.RIFTER, 20_000_000)
        cls.channel = f.ping_channel("Main Pings", allow_everyone=True, allow_here=True)
        cls.role = f.ping_role("Sig Alpha")
        cls.comms = CommsPreset.objects.create(
            name="Alliance Mumble", channel_name="Ops", voice_url="mumble://voice.example.com/Ops"
        )
        cls.logi = ChannelPreset.objects.create(
            name="Logi Main", channel_type=ChannelPreset.ChannelType.LOGI, channel_value="Logi One"
        )
        cls.special = f.special_role("Backup FC", key="backup", grants_fc_credit=True)
        f.structure_template("Standard", "Main: Alpha, Bravo")

        wings = f.wings(("Main", ("Alpha", "Bravo")), ("Logi", ("Logi",)))
        wing_id, squad_id = wings[0]["id"], wings[0]["squads"][0]["id"]
        cls.active = f.create_operation(
            cls.fc,
            doctrine_name="Moa Fleet",
            comms=cls.comms,
            logi_channel=cls.logi,
            ping_channel=cls.channel,
            ping_mention_mode=FleetOperation.MentionMode.HERE,
            ping_text="Fleet up in Jita",
            motd_text="Join Logi One",
            fleet_wings=wings,
            fleet_wings_read_at=now,
            last_esi_update=now,
            started_at=now - timedelta(minutes=40),
            srp_url="https://example.com/srp/abc",
            fleet_missing_count=1,
            missing_reason=FleetOperation.EndReason.FLEET_GONE,
            last_error=texts.READ_MEMBERS_FAILED(detail=texts.ESI_HTTP(status=503, reason="Service Unavailable")),
        )
        f.member_state(
            cls.active, fc_main, role="fleet_commander", accumulated_seconds=1800, ship_type_id=11987,
            ship_type_name="Guardian", solar_system_id=30000142, solar_system_name="Jita",
        )
        f.member_state(
            cls.active, pilot_main, role="squad_member", wing_id=wing_id, squad_id=squad_id, accumulated_seconds=900,
            ship_type_name="Moa", solar_system_name="Jita",
        )
        f.member_state(cls.active, cls.pilot_alt, active=False, accumulated_seconds=300, ship_type_name="Osprey")
        f.member_state(cls.active, 99_000_123, name="Nobody Known", accumulated_seconds=60)
        for event_type, old, new in (
            (FleetMemberEvent.EventType.JOIN, "", "2026-03-10T11:00:00+00:00"),
            (FleetMemberEvent.EventType.SHIP_CHANGE, "Moa", "Osprey"),
            (FleetMemberEvent.EventType.ROLE_CHANGE, "squad_member", "squad_commander"),
            (FleetMemberEvent.EventType.WING_CHANGE, str(wing_id), "-1"),
            (FleetMemberEvent.EventType.LEAVE, "", ""),
        ):
            FleetMemberEvent.objects.create(
                operation=cls.active, character_id=pilot_main.character_id, character_name=pilot_main.character_name,
                event_type=event_type, old_value=old, new_value=new,
            )
        failed = OperationAction.objects.create(
            operation=cls.active, action="motd_update", status=OperationAction.Status.FAILED,
            error_message=texts.READ_MEMBERS_FAILED(detail=texts.ESI_HTTP(status=503, reason="Service Unavailable")),
        )
        OperationActionAttempt.objects.create(
            action=failed, status="failed", message=texts.ESI_HTTP(status=503, reason="Service Unavailable")
        )
        for code, status, message in (
            ("fleet_detection", "success", ""),
            ("discord_ping", "success", ""),
            ("tracking_start", "skipped", texts.STEP_SKIPPED(mode=texts.MODE_PAP_ONLY(), step="Discord ping")),
            ("srp_link", "success", texts.SRP_LINK_CREATED() if hasattr(texts, "SRP_LINK_CREATED") else ""),
        ):
            OperationAction.objects.create(operation=cls.active, action=code, status=status, error_message=message)

        cls.closed = f.closed_operation(
            cls.fc, started_at=STARTED, end_reason=FleetOperation.EndReason.BOSS_HANDOVER, end_detail="Bob Boss",
            attendance_multiplier=2, multiplier_source=FleetOperation.MultiplierSource.FC, doctrine_name="Tengu Fleet",
            comms=cls.comms, srp_url="https://example.com/srp/def",
        )
        f.add_attendance(cls.closed, cls.pilot, value=2)
        f.add_attendance(cls.closed, cls.lead, value=2, capped=True, granted=False)
        f.add_attendance(cls.closed, cls.other, value=0, below_minimum=True, granted=False)
        f.add_attendance(
            cls.closed, cls.fc, value=3, source=AttendanceRecord.Source.MANUAL,
            review_status=AttendanceRecord.review_status.field.default,
        )
        OperationRoleAssignment.objects.create(
            operation=cls.closed, role="backup", special_role=cls.special, character_id=pilot_main.character_id,
            character_name=pilot_main.character_name, auth_user=cls.pilot, grants_fc_credit=True,
            assigned_by=cls.fc,
        )
        cls.pending = f.create_operation(
            cls.fc, status=FleetOperation.Status.CLOSED, is_manual=True, esi_fleet_id=None, started_at=STARTED + timedelta(days=1),
            ended_at=STARTED + timedelta(days=1, hours=2), review_status="pending", tracking_enabled=False,
        )
        f.add_attendance(
            cls.pending, cls.pilot, value=1, source=AttendanceRecord.Source.MANUAL, review_status="pending"
        )
        cls.scheduled = f.create_scheduled_operation(
            cls.fc, ping_channel=cls.channel, ping_text="Pre ping text", motd_text="MOTD text", additional_message="Bring logi",
        )
        cls.link_only = f.create_operation(
            cls.fc, link_only=True, esi_fleet_id=None, tracking_enabled=False, started_at=now - timedelta(minutes=10),
            send_ping=False,
        )
        cls.pap_link = f.pap_link(cls.link_only)
        f.registration(cls.link_only, f.main_of(cls.pilot), link=cls.pap_link)
        cls.rejected_fleet = f.closed_operation(
            cls.fc, started_at=STARTED + timedelta(days=2), review_status="rejected", review_note="Wrong times",
            stats_mode=FleetOperation.StatsMode.EXCLUDE, stats_note="Test fleet",
            end_reason=FleetOperation.EndReason.MANUAL,
        )

        cls.period = IncentivePeriod.objects.create(year=MONTH[0], month=MONTH[1], budget=1_000_000_000, minimum_fleets=1)
        incentives.rebuild_period(cls.period)
        for action, reason in (
            ("configuration.update", texts.AUDIT_CONFIG_UPDATED()),
            ("operation.edit", texts.AUDIT_FLEET_TIMES_EDITED(detail="x")),
            ("pap.recalculate", texts.AUDIT_RECALCULATED()),
            ("fleet.end", ""),
        ):
            AuditLog.objects.create(actor=cls.lead, action=action, object_type="FleetOperation", object_id="1", reason=reason)

        cls.link = f.fleet_link(cls.active, code="abcde12345")
        cls.pending_claim = f.srp_claim(cls.pilot, link=cls.link, amount=20_000_000, member_comment="Lost it")
        cls.approved_claim = f.srp_claim(cls.pilot, status=SrpClaim.Status.APPROVED, decided_by=cls.lead)
        cls.paid_claim = f.srp_claim(cls.pilot, status=SrpClaim.Status.PAID, decided_by=cls.lead)
        cls.rejected_claim = f.srp_claim(cls.other, status=SrpClaim.Status.REJECTED, decided_by=cls.lead)
        cls.auto_rejected_claim = f.srp_claim(
            cls.other,
            status=SrpClaim.Status.REJECTED,
            auto_rejected=True,
            decision_reason=texts.SRP_REJECTED_LIMIT_USED_UP(limit=texts.SRP_LIMIT_PERSONAL(), cap="100m ISK"),
        )
        f.claim_event(cls.pending_claim, SrpClaimEvent.Action.SUBMIT, actor=cls.pilot, to_status="pending")
        f.claim_event(cls.pending_claim, SrpClaimEvent.Action.NOTE, actor=cls.lead, reason="Looks fine", internal=True)
        f.claim_event(
            cls.approved_claim, SrpClaimEvent.Action.APPROVE, actor=None, reason=texts.SRP_REASON_RECALCULATED(),
            from_status="pending", to_status="approved", amount=10_000_000,
        )
        cls.preset = FleetStartPreset.objects.create(owner=cls.fc, name="Roam")
        cls.data = database_strings()
        cls.data.update(ESI_TEXT)
        cls.data_pattern = data_regex(cls.data)
        cls.allowed = KEPT_WORDS | EXAMPLE_NAMES | catalog_words()




class ChinesePageMixin:
    def setUp(self):
        super().setUp()
        # A request activates the language of its user for the thread; later tests start from the default.
        self.addCleanup(translation.deactivate)

    def sign_in(self, user, language=LANGUAGE):
        self.client.force_login(user)
        self.client.cookies[settings.LANGUAGE_COOKIE_NAME] = language

    def get(self, user, name, *args, query="", language=LANGUAGE, **kwargs):
        self.sign_in(user, language)
        path = reverse(f"fleetops:{name}", args=args, kwargs=kwargs or None) + query
        response = self.client.get(path)
        self.assertEqual(response.status_code, 200, path)
        return response

    def post(self, user, name, *args, data=None, language=LANGUAGE):
        self.sign_in(user, language)
        path = reverse(f"fleetops:{name}", args=args)
        response = self.client.post(path, data or {}, follow=True)
        self.assertEqual(response.status_code, 200, path)
        return response

    def strays(self, html, parts=("nav", "content")):
        """The lines of the page with English words that are neither data nor kept names."""
        lines = []
        for line in visible_text(html, parts).splitlines():
            line = URL.sub(" ", line)
            line = self.data_pattern.sub(" ", line)
            if [word for word in WORD.findall(line) if word not in self.allowed]:
                lines.append(" ".join(line.split())[:200])
        return lines

    def flashed(self, response):
        """The messages the request left for the page (Alliance Auth shows them above the content)."""
        return [str(message) for message in response.context["messages"]]

    def assertChinese(self, response, *texts):
        html = response.content.decode()
        self.assertRegex(visible_text(html), CJK)
        for expected in texts:
            self.assertIn(expected, html)
        self.assertEqual(self.strays(html), [], "English text on the page")
        if response.context and "messages" in response.context:
            for message in self.flashed(response):
                self.assertEqual(self.strays(f'<div class="fleetops">{message}</div>'), [], message)


@tag("catalog")
class ChineseSiteTests(PageWorld, ChinesePageMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.build()

    def page_list(self):
        month = f"?year={MONTH[0]}&month={MONTH[1]}"
        lead, fc, pilot = self.lead, self.fc, self.pilot
        pages = [
            ("dashboard", lead, (), ""),
            ("dashboard", pilot, (), ""),
            ("start_fleet", lead, (), ""),
            ("start_fleet", fc, (), f"?scheduled={self.scheduled.uuid}"),
            ("manual_fleet", lead, (), ""),
            ("fleet_operations", lead, (), ""),
            ("fleet_operations", lead, (), month),
            ("operation_detail", lead, (self.active.uuid,), ""),
            ("operation_detail", lead, (self.closed.uuid,), ""),
            ("operation_detail", lead, (self.pending.uuid,), ""),
            ("operation_detail", lead, (self.scheduled.uuid,), ""),
            ("operation_detail", lead, (self.link_only.uuid,), ""),
            ("operation_detail", lead, (self.rejected_fleet.uuid,), ""),
            ("operation_detail", pilot, (self.closed.uuid,), ""),
            ("edit_operation", lead, (self.closed.uuid,), ""),
            ("edit_scheduled_fleet", fc, (self.scheduled.uuid,), ""),
            ("roster_import", lead, (self.closed.uuid,), ""),
            ("my_statistics", pilot, (), month),
            ("my_statistics", pilot, (), ""),
            ("corporation_statistics", lead, (), month),
            ("corporation_statistics_detail", lead, (f.OTHER_CORP[0],), month),
            ("all_corporation_statistics", lead, (), month),
            ("all_corporation_statistics", lead, (), ""),
            ("all_fc_statistics", lead, (), month),
            ("fc_statistics_detail", lead, (self.fc.pk,), month),
            ("alliance_members", lead, (), month),
            ("corporation_members", lead, (), month),
            ("member_detail", lead, (self.pilot.pk,), month),
            ("leaderboard", lead, (), month),
            ("manual_attendance", lead, (), ""),
            ("attendance_history_me", pilot, (), month),
            ("attendance_history_corporation", lead, (), month),
            ("attendance_history_alliance", lead, (), month),
            ("approval_queue", lead, (), ""),
            ("audit_log", lead, (), ""),
            ("incentive_review", lead, (), month),
            ("preset_list", fc, (), ""),
            ("start_link_only_fleet", fc, (), ""),
            ("pap_link_register", pilot, (self.pap_link.code,), ""),
            ("srp_home", pilot, (), ""),
            ("srp_link", pilot, (self.link.code,), ""),
            ("srp_claim", pilot, (self.pending_claim.uuid,), ""),
            ("srp_claim", lead, (self.pending_claim.uuid,), ""),
            ("srp_claim", lead, (self.approved_claim.uuid,), ""),
            ("srp_claim", lead, (self.paid_claim.uuid,), ""),
            ("srp_claim", lead, (self.rejected_claim.uuid,), ""),
            ("srp_claim", self.other, (self.auto_rejected_claim.uuid,), ""),
            ("srp_claim", lead, (self.auto_rejected_claim.uuid,), ""),
            ("srp_review", lead, (), ""),
            ("srp_payments", lead, (), ""),
            ("srp_summary", lead, (), ""),
            ("srp_summary_own_corporation", lead, (), ""),
            ("srp_summary_own_fc", lead, (), ""),
            ("configuration_index", lead, (), ""),
            ("configuration_settings", lead, (), ""),
        ]
        for section, config in CONFIG_SECTIONS.items():
            pages.append(("configuration_list", lead, (section,), ""))
            if section == "srp":
                continue
            pages.append(("configuration_add", lead, (section,), ""))
            row = config["model"].objects.first()
            if row is not None:
                pages.append(("configuration_edit", lead, (section, row.pk), ""))
        return pages

    def test_every_page_is_in_chinese(self):
        pages = self.page_list()

        self.assertGreater(len(pages), 70)
        for name, user, args, query in pages:
            with self.subTest(page=name, args=[str(arg) for arg in args], user=user.username):
                self.assertChinese(self.get(user, name, *args, query=query))

    def test_the_same_pages_stay_in_english(self):
        for name, args in (("dashboard", ()), ("start_fleet", ()), ("operation_detail", (self.active.uuid,))):
            with self.subTest(page=name):
                html = self.get(self.lead, name, *args, language="en").content.decode()

                self.assertNotRegex(visible_text(html), CJK)
                self.assertIn("Dashboard", visible_text(html))

    def test_the_check_finds_English_text(self):
        html = self.get(self.lead, "dashboard", language="en").content.decode()

        self.assertIn("Start Fleet", " ".join(self.strays(html)))

    def test_the_menu_follows_the_language(self):
        english = ("Dashboard", "Start Fleet", "Fleets", "My Statistics", "Leaderboard", "Administration", "Audit")
        chinese = ("总览", "开舰队", "舰队", "我的统计", "排行榜", "后台设置", "操作日志")

        zh_menu = visible_text(self.get(self.lead, "dashboard").content.decode(), ("nav",))
        en_menu = visible_text(self.get(self.lead, "dashboard", language="en").content.decode(), ("nav",))

        for zh, en in zip(chinese, english):
            self.assertIn(zh, zh_menu)
            self.assertNotIn(en, zh_menu)
            self.assertIn(en, en_menu)
            self.assertNotIn(zh, en_menu)

    def test_a_member_sees_only_the_menu_entries_of_the_permissions_and_all_of_them_in_chinese(self):
        for user in (self.pilot, self.fc, self.other):
            with self.subTest(user=user.username):
                response = self.get(user, "dashboard")

                self.assertEqual(self.strays(response.content.decode(), ("nav",)), [])

    def test_stored_messages_are_shown_in_the_language_of_the_viewer(self):
        zh = self.get(self.lead, "operation_detail", self.active.uuid).content.decode()
        en = self.get(self.lead, "operation_detail", self.active.uuid, language="en").content.decode()
        ended_zh = self.get(self.lead, "operation_detail", self.closed.uuid).content.decode()
        ended_en = self.get(self.lead, "operation_detail", self.closed.uuid, language="en").content.decode()

        self.assertIn("无法读取舰队成员：ESI 返回 HTTP 503（Service Unavailable）。", zh)
        self.assertIn("Could not read fleet members: ESI returned HTTP 503 (Service Unavailable).", en)
        self.assertIn("PAP 仅追踪模式：Discord Ping 已跳过。", zh)
        self.assertIn("自动结束：舰队 Boss 已移交给 Bob Boss", ended_zh)
        self.assertIn("Ended automatically: fleet boss handed over to Bob Boss", ended_en)
        self.assertNotIn("Ended automatically", ended_zh)

    def test_audit_reasons_and_claim_history_are_translated(self):
        audit = self.get(self.lead, "audit_log").content.decode()
        claim = self.get(self.lead, "srp_claim", self.approved_claim.uuid).content.decode()

        self.assertIn("在 FleetOps 后台设置中更新", audit)
        self.assertIn("手动重新计算", audit)
        self.assertIn("已按当前规则重新计算", claim)
        self.assertIn("系统", claim)

    def test_the_state_of_the_fleet_page_is_translated(self):
        response = self.get(self.lead, "fleet_state", self.active.uuid)
        state = response.json()

        self.assertFalse(state["write"]["allowed"])
        self.assertRegex(state["write"]["reason"], CJK)
        self.assertNotIn("The FC character", state["write"]["reason"])
        self.assertEqual(self.strays(state["status_html"]), [])

    def test_the_live_pap_fragment_is_translated(self):
        response = self.get(self.pilot, "live_pap")

        self.assertContains(response, "进行中舰队的 PAP")
        self.assertEqual(self.strays(f'<div class="fleetops">{response.content.decode()}</div>'), [])

    def test_the_public_comms_page_follows_the_browser_language(self):
        self.client.cookies.clear()
        self.client.force_login(self.pilot)
        path = reverse("fleetops:comms_join", args=[self.comms.join_token])

        zh = self.client.get(path, headers={"accept-language": "zh-CN,zh;q=0.9"})
        en = self.client.get(path, headers={"accept-language": "en"})

        self.assertContains(zh, "在语音客户端中打开")
        self.assertContains(en, "Open in voice client")

    def test_exports_have_chinese_column_titles(self):
        self.sign_in(self.lead)
        month = f"?year={MONTH[0]}&month={MONTH[1]}&format=csv"
        zh = self.client.get(reverse("fleetops:export", args=["corporations"]) + month)
        self.sign_in(self.lead, "en")
        en = self.client.get(reverse("fleetops:export", args=["corporations"]) + month)

        self.assertEqual(zh.status_code, 200)
        header = b"".join(zh.streaming_content).decode("utf-8-sig").splitlines()[0]
        self.assertTrue(header.startswith("军团,"), header)
        self.assertIn("PAP 总计", header)
        self.assertTrue(b"".join(en.streaming_content).decode("utf-8-sig").splitlines()[0].startswith("Corporation,"))


@tag("catalog")
class ChineseFlowTests(PageWorld, ChinesePageMixin, TestCase):
    """Messages that a request leaves for the next page, and form errors."""

    @classmethod
    def setUpTestData(cls):
        cls.build()

    def test_saving_the_settings_flashes_a_chinese_message(self):
        from fleetops.forms_config import FleetOpsSettingsForm
        from fleetops.models import FleetOpsSettings

        form = FleetOpsSettingsForm(instance=FleetOpsSettings.get_solo())
        data = {}
        for name, value in form.initial.items():
            if value is True:
                data[name] = "on"
            elif value is not None and value is not False:
                data[name] = value

        response = self.post(self.lead, "configuration_settings", data=data)

        self.assertChinese(response)
        self.assertIn("FleetOps 设置已更新。", self.flashed(response))

    def test_a_form_error_is_in_chinese(self):
        response = self.post(
            self.lead,
            "manual_fleet",
            data={
                "fleet_type": f.fleet_type().pk,
                "formup": "Jita",
                "started_at": "2026-03-10 12:00",
                "ended_at": "2026-03-10 11:00",
            },
        )

        self.assertChinese(response, "结束时间必须晚于开始时间。")

    def test_approving_a_fleet_flashes_a_chinese_message_with_a_count(self):
        response = self.post(
            self.lead, "review_operation", self.pending.uuid, data={"action": "approve", "include_attendance": "on"}
        )

        self.assertChinese(response)
        self.assertEqual(self.flashed(response), ["舰队已通过，含 1 行 PAP。"])

    def test_rejecting_without_a_reason_is_refused_in_chinese(self):
        response = self.post(self.lead, "review_operation", self.pending.uuid, data={"action": "reject"})

        self.assertChinese(response)
        self.assertEqual(self.flashed(response), ["舰队未能审核。请填写拒绝原因。"])

    def test_recalculating_the_claims_flashes_a_chinese_message(self):
        response = self.post(self.lead, "srp_review_recalculate")

        self.assertChinese(response)
        self.assertRegex(self.flashed(response)[0], r"^已按当前规则重新计算 \d+ 条待审核申请。$")

    def test_a_refused_claim_action_is_explained_in_chinese(self):
        # The refusal is a stored message of the service.
        response = self.post(self.pilot, "srp_claim_withdraw", self.approved_claim.uuid)

        self.assertChinese(response)
        self.assertEqual(self.flashed(response), ["只能撤回待审核的申请。"])

    def test_a_missing_field_is_reported_in_chinese(self):
        response = self.post(self.lead, "srp_claim_action", self.pending_claim.uuid, "reject", data={"reason": ""})

        self.assertChinese(response)
        self.assertEqual(self.flashed(response), ["这个字段是必填项。"])

    def test_recalculating_the_incentive_period_flashes_a_chinese_message(self):
        response = self.post(self.lead, "incentive_recalculate", self.period.pk)

        self.assertChinese(response)
        self.assertEqual(self.flashed(response), ["FC 奖金周期已重新计算并转入审核。"])

    def test_renaming_a_preset_to_nothing_is_refused_in_chinese(self):
        response = self.post(self.fc, "preset_rename", self.preset.pk, data={"name": ""})

        self.assertChinese(response)
        self.assertTrue(self.flashed(response)[0].startswith("预设未重命名："))

    def test_syncing_the_roles_explains_a_missing_discord_service_in_chinese(self):
        response = self.post(self.lead, "sync_discord_roles")

        self.assertChinese(response)
        self.assertRegex(self.flashed(response)[0], CJK)


class CharLinkLabelTests(TestCase):
    def setUp(self):
        super().setUp()
        self.addCleanup(translation.deactivate)

    def test_the_labels_follow_the_language(self):
        from .test_charlink_hook import HOOK_MODULE, charlink_stand_in

        modules = {name: module for name, module in sys.modules.items() if name != HOOK_MODULE}
        modules.update(charlink_stand_in())
        with mock.patch.dict(sys.modules, modules, clear=True):
            app_import = importlib.import_module(HOOK_MODULE).app_import
            labels = {item.unique_id: item.field_label for item in app_import.imports}

            with translation.override(LANGUAGE):
                self.assertEqual(str(labels["fcfleet"]), "FleetOps FC 舰队控制")
                self.assertEqual(str(labels["clickpap"]), "FleetOps PAP 登记链接")
            with translation.override("en"):
                self.assertEqual(str(labels["fcfleet"]), "FleetOps FC fleet control")
