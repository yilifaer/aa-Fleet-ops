# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Fixes from the regression run of 0.3.0b6 (display, translation and small behaviour items)."""

import ast
import pathlib
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone
from types import SimpleNamespace
from unittest import mock

from django.conf import settings
from django.contrib.messages import get_messages
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone, translation
from django.utils.translation import gettext, pgettext

from fleetops.forms_operation import HistoricalManualAttendanceForm
from fleetops.forms_srp import FleetLinkForm, StandingLinkForm
from fleetops.models import AuditLog, SrpClaim, SrpClaimEvent, StandalonePing
from fleetops.services import discord_roles, fleet_controls, mentions, messages
from fleetops.services.audit import audit
from fleetops.services.messages import render_cancellation_notice, render_messages, render_standalone_ping
from fleetops.srp import claims, review
from fleetops.templatetags.fleetops_config import (
    AUDIT_ACTION_LABELS,
    audit_action_label,
    audit_changes,
)
from fleetops.texts import translate_stored

from . import factories as f
from .link_fakes import setUpModule, tearDownModule  # noqa: F401 - unittest runs them around the module (no ESI lookups)
from .test_owner_feedback_0_3_0b6 import MemberViewFixture
from .test_srp_claims import ClaimBase
from .test_srp_payments_batches import PaymentsBase, route

ZH = "zh-hans"
PACKAGE = pathlib.Path(__file__).resolve().parent.parent / "fleetops"


class LanguageGuard:
    """A request with the Chinese cookie leaves Chinese active in the thread; the next test must not inherit it."""

    def setUp(self):
        super().setUp()
        previous = translation.get_language()
        self.addCleanup(translation.activate, previous)


def messages_of(response):
    return [(message.level_tag, str(message)) for message in get_messages(response.wsgi_request)]


# --- First and Last as column headers ------------------------------------------------------------------------


class AttendanceHeaderTests(LanguageGuard, MemberViewFixture):
    def zh(self, user):
        client = self.client_class()
        client.force_login(user)
        client.cookies[settings.LANGUAGE_COOKIE_NAME] = ZH
        return client.get(f"/fleetops/operations/{self.operation.uuid}/")

    def test_the_words_have_their_own_translation_next_to_the_page_links(self):
        with translation.override(ZH):
            self.assertEqual(pgettext("first seen column", "First"), "首次")
            self.assertEqual(pgettext("last seen column", "Last"), "末次")
            # The links of the pagination keep their words.
            self.assertEqual(pgettext("FleetOps", "First"), "首页")
            self.assertEqual(pgettext("FleetOps", "Last"), "末页")

    def test_the_member_page_has_first_and_last_seen_as_headers(self):
        page = self.zh(self.member).content.decode()

        self.assertIn("<th>首次</th><th>末次</th>", page)
        self.assertNotIn("<th>首页</th>", page)

    def test_the_full_page_of_an_fc_has_them_too(self):
        page = self.zh(self.fc).content.decode()

        self.assertIn("<th>首次</th><th>末次</th>", page)
        self.assertNotIn("<th>首页</th>", page)

    def test_no_column_header_uses_the_page_link_words(self):
        # A header cell with the words of the pagination links would read "First page" in a table.
        for path in (PACKAGE / "templates").rglob("*.html"):
            text = path.read_text(encoding="utf-8")
            for word in ("First", "Last"):
                self.assertNotIn(f'<th>{{% translate "{word}" context "FleetOps" %}}</th>', text, path.name)


# --- The reasons a claim cannot be submitted again --------------------------------------------------------------


class ResubmitReasonTranslationTests(LanguageGuard, ClaimBase):
    def page(self, claim):
        self.client.force_login(self.user)
        self.client.cookies[settings.LANGUAGE_COOKIE_NAME] = ZH
        return self.client.get(reverse("fleetops:srp_claim", args=[claim.uuid]))

    def rejected(self):
        claim = self.submit(self.killmail())
        review.reject_claim(claim, actor=self.manager, reason="Wrong ship")
        claim.refresh_from_db()
        self.settings(member_resubmit_rejected=True)
        return claim

    def test_the_reason_is_shown_in_the_language_of_the_page(self):
        claim = self.rejected()

        with mock.patch(
            "fleetops.views_srp.discovery.unauthorized_characters", return_value=[(1, "Example Alt"), (2, "Other Alt")]
        ):
            page = self.page(claim)

        self.assertContains(page, "srp-claim-resubmit-blocked")
        self.assertContains(page, "Example Alt, Other Alt")
        self.assertNotContains(page, "Authorize loss reading")
        self.assertContains(page, "请先为你所有的角色授权损失读取")

    def test_every_kind_of_reason_is_translated(self):
        claim = self.rejected()
        self.settings(claim_deadline_days=1)
        claim.killmail.killmail_time = timezone.now() - timedelta(days=5)
        claim.killmail.save()

        with mock.patch("fleetops.views_srp.discovery.unauthorized_characters", return_value=[]):
            page = self.page(claim)
        self.assertNotContains(page, "cannot be submitted.")
        self.assertContains(page, "srp-claim-resubmit-blocked")

        self.settings(enabled=False)
        page = self.page(claim)
        self.assertNotContains(page, "SRP is switched off")

    def test_the_stored_texts_of_every_blocker_have_a_translation(self):
        with translation.override(ZH):
            for text in (
                "Authorize loss reading for all your characters first: Example Alt.",
                "Authorize loss reading for all your characters first: Alpha, Beta.",
            ):
                self.assertNotEqual(translate_stored(text), text)


# --- The kick records ---------------------------------------------------------------------------------------


class KickRecordTests(SimpleTestCase):
    def member(self, corporation):
        return SimpleNamespace(character_name="Example Pilot", corporation_name=corporation)

    def test_a_member_without_a_corporation_is_the_bare_name(self):
        self.assertEqual(fleet_controls.member_label(self.member("")), "Example Pilot")
        self.assertEqual(fleet_controls.member_label(self.member(None)), "Example Pilot")
        self.assertEqual(fleet_controls.member_label(self.member("Example Corp")), "Example Pilot (Example Corp)")

    def test_no_english_words_are_written_for_an_empty_corporation(self):
        label = fleet_controls.member_label(self.member(""))

        self.assertNotIn("unknown", label)

    def test_old_records_with_the_english_words_are_shown_translated(self):
        old = {
            "Kicked Example Pilot (unknown corporation).": "已踢出 Example Pilot（未知军团）。",
            "Could not kick Example Pilot (unknown corporation): already left the fleet.": "无法踢出 Example Pilot（未知军团）：已经离开舰队。",
        }
        with translation.override(ZH):
            for stored, shown in old.items():
                self.assertEqual(translate_stored(stored), shown)
            failed = translate_stored("Could not kick Example Pilot (unknown corporation): Not found.")
            self.assertTrue(failed.startswith("无法踢出 Example Pilot（未知军团）："), failed)
            self.assertNotIn("unknown", failed)

    def test_new_records_with_a_corporation_are_translated_as_before(self):
        with translation.override(ZH):
            self.assertEqual(
                translate_stored("Kicked Example Pilot (Example Corp)."), "已踢出 Example Pilot (Example Corp)。"
            )


# --- The historical fleet menu -------------------------------------------------------------------------------


class HistoricalFleetLabelTests(TestCase):
    def test_the_menu_shows_type_fc_and_start_time_in_eve_time(self):
        manager = f.create_user("b7_manager", perms=f.MEMBER_PERMS + ["fleetops.manage_fleets"], main_name="Example Manager")
        started = datetime(2026, 11, 3, 19, 45, 30, tzinfo=dt_timezone.utc)
        operation = f.create_operation(
            manager, status="closed", started_at=started, ended_at=started + timedelta(hours=1)
        )

        form = HistoricalManualAttendanceForm(user=manager)
        label = form.fields["operation"].label_from_instance(operation)

        self.assertEqual(label, f"StratOp · {operation.fc_character_name} · 2026-11-03 19:45 EVE")
        self.assertNotIn(str(operation.uuid), label)
        self.assertIn(label, form.as_p())

    def test_a_fleet_without_a_start_time_has_no_dangling_separator(self):
        manager = f.create_user("b7_manager2", perms=f.MEMBER_PERMS + ["fleetops.manage_fleets"])
        operation = f.create_operation(manager, status="closed")
        operation.started_at = None

        label = HistoricalManualAttendanceForm(user=manager).fields["operation"].label_from_instance(operation)

        self.assertFalse(label.endswith("·"))
        self.assertNotIn("None", label)


# --- Small wording fixes ---------------------------------------------------------------------------------------


class WordingTests(SimpleTestCase):
    def test_the_role_sync_summary_uses_the_separator_of_the_language(self):
        with translation.override("en"):
            self.assertEqual(discord_roles._summary(2, 0, 1).count(", "), 2)
        with translation.override(ZH):
            text = discord_roles._summary(2, 0, 1)
        self.assertNotIn(", ", text)

    def test_the_sentence_after_a_refused_end_has_no_space_in_chinese(self):
        with translation.override(ZH):
            text = gettext("%(problem)s The fleet was not ended.") % {"problem": "请选择一个 PAP 倍率（90、180 分钟）。"}
        self.assertEqual(text, "请选择一个 PAP 倍率（90、180 分钟）。舰队没有结束。")
        with translation.override("en"):
            self.assertEqual(
                gettext("%(problem)s The fleet was not ended.") % {"problem": "Choose a multiplier."},
                "Choose a multiplier. The fleet was not ended.",
            )

    def test_the_example_in_the_layout_field_is_translated(self):
        from fleetops.forms_fleet import FleetStructureTemplateForm

        with translation.override("en"):
            english = str(FleetStructureTemplateForm().fields["layout"].widget.attrs["placeholder"])
        with translation.override(ZH):
            chinese = str(FleetStructureTemplateForm().fields["layout"].widget.attrs["placeholder"])

        self.assertEqual(english, "Main: Alpha, Bravo, Charlie\nLogi: Logi")
        self.assertNotEqual(chinese, english)
        # The example must be a layout the field accepts.
        from fleetops.models import FleetStructureTemplate

        self.assertTrue(FleetStructureTemplate(name="x", layout=chinese).wings)


class ReportAddressTests(SimpleTestCase):
    def errors(self, value):
        form = FleetLinkForm({"doctrine_text": "", "aar_url": value})
        self.assertFalse(form.is_valid(), value)
        return form.errors["aar_url"]

    def test_a_refused_address_is_refused_once(self):
        for value in ("javascript:alert(1)", "data:text/html,x", "ftp://example.org/x", "not a url at all"):
            with self.subTest(value=value):
                self.assertEqual(len(self.errors(value)), 1, self.errors(value))

    def test_good_addresses_pass(self):
        for value in ("https://example.org/report", "http://example.org/r", "example.org/report", ""):
            with self.subTest(value=value):
                self.assertTrue(FleetLinkForm({"doctrine_text": "", "aar_url": value}).is_valid())


class StandingLinkCloseTimeTests(TestCase):
    def setUp(self):
        self.category = f.srp_category("B7 category")

    def data(self, close):
        category = self.category
        return {
            "name": "",
            "category": category.pk,
            "link_date": timezone.now().date().isoformat(),
            "doctrine_text": "",
            "aar_url": "",
            "auto_close_at": close,
        }

    def stamp(self, delta):
        return (timezone.now() + delta).strftime("%Y-%m-%dT%H:%M")

    def test_a_closing_time_in_the_past_is_refused_with_a_clear_message(self):
        with translation.override("en"):
            form = StandingLinkForm(self.data(self.stamp(timedelta(hours=-3))))
            self.assertFalse(form.is_valid())
            errors = [str(error) for error in form.errors["auto_close_at"]]

        self.assertEqual(
            errors, ["The closing time has already passed. Choose a time in the future or leave the field empty."]
        )

    def test_a_future_time_and_an_empty_field_are_fine(self):
        self.assertTrue(StandingLinkForm(self.data(self.stamp(timedelta(hours=3)))).is_valid())
        self.assertTrue(StandingLinkForm(self.data("")).is_valid())

    def test_the_unchanged_time_of_a_link_that_closed_itself_can_stay(self):
        past = timezone.now().replace(second=0, microsecond=0) - timedelta(hours=5)
        link = f.standing_link(auto_close_at=past, is_open=False)

        same = StandingLinkForm(self.data(past.strftime("%Y-%m-%dT%H:%M")), link=link)
        other = StandingLinkForm(self.data((past - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M")), link=link)

        self.assertTrue(same.is_valid(), same.errors)
        self.assertFalse(other.is_valid())

    def test_the_message_is_translated(self):
        with translation.override(ZH):
            form = StandingLinkForm(self.data(self.stamp(timedelta(hours=-3))))
            form.is_valid()
            errors = [str(error) for error in form.errors["auto_close_at"]]
        self.assertEqual(errors, ["关闭时间已经过去。请选择一个将来的时间，或者留空。"])


# --- The audit log --------------------------------------------------------------------------------------------


class AuditLogDisplayTests(LanguageGuard, TestCase):
    def setUp(self):
        self.auditor = f.create_user("b7_auditor", perms=f.MEMBER_PERMS + ["fleetops.view_audit_log"])
        self.client.force_login(self.auditor)

    def page(self, language="en"):
        self.client.cookies[settings.LANGUAGE_COOKIE_NAME] = language
        return self.client.get(reverse("fleetops:audit_log")).content.decode()

    def test_the_action_column_shows_a_name_not_the_code(self):
        audit(self.auditor, "fleet.kick_member", f.fleet_type("B7 type"), None, {"kicked": True})

        english = self.page()
        chinese = self.page(ZH)

        self.assertIn('title="fleet.kick_member">Member kicked<', english)
        self.assertIn('title="fleet.kick_member">已踢出成员<', chinese)
        self.assertNotIn(">fleet.kick_member<", english)

    def test_the_object_column_is_translated(self):
        audit(self.auditor, "srp.link_close", f.fleet_type("B7 type"), None, None)

        self.assertIn("舰队类型 #", self.page(ZH))

    def test_an_unknown_action_is_shown_as_stored(self):
        self.assertEqual(audit_action_label("something.new"), "something.new")
        self.assertEqual(audit_action_label(""), "")
        self.assertEqual(audit_action_label(None), "")

    def test_claim_actions_use_the_names_of_the_claim_events(self):
        with translation.override("en"):
            self.assertEqual(str(audit_action_label("srp.claim_approve")), "Approved")
            self.assertEqual(str(audit_action_label("srp.claim_paid")), "Marked paid")
        with translation.override(ZH):
            self.assertEqual(str(audit_action_label("srp.claim_paid")), "标记为已付款")
        self.assertEqual(audit_action_label("srp.claim_nonsense"), "srp.claim_nonsense")

    def test_every_action_the_code_writes_has_a_name(self):
        """The audit calls name their action as a string; each of those strings has a label."""
        used = set()
        for path in PACKAGE.rglob("*.py"):
            if "migrations" in path.parts:
                continue
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else ""
                if name not in ("audit", "audit_event", "_failed", "_audit_retry") or len(node.args) < 2:
                    continue
                for argument in node.args[:3]:
                    value = getattr(argument, "value", None)
                    if isinstance(value, str) and "." in value and " " not in value and value.split(".")[0] in {
                        "approval", "attendance", "configuration", "fleet", "incentive", "operation", "pap",
                        "pap_link", "ping", "srp", "statistics", "group",
                    }:
                        used.add(value)
        missing = sorted(action for action in used if action not in AUDIT_ACTION_LABELS)
        self.assertEqual(missing, [])

    def test_an_old_automatic_replacement_has_a_name(self):
        self.assertIn("attendance.replace_auto", AUDIT_ACTION_LABELS)

    def test_every_label_has_a_chinese_translation(self):
        with translation.override(ZH):
            for action, label in AUDIT_ACTION_LABELS.items():
                self.assertTrue(any("一" <= char <= "鿿" for char in str(label)), (action, str(label)))

    def test_status_words_and_empty_snapshots_in_the_changes_column(self):
        row = audit(
            self.auditor, "approval.attendance_approve", f.fleet_type("B7 type 3"),
            {"status": "pending", "review_status": "pending"}, {"status": "approved", "review_status": "approved"},
        )
        with translation.override("en"):
            english = audit_changes(row)
        with translation.override(ZH):
            chinese = audit_changes(row)

        self.assertEqual([(name, old, new) for name, old, new in english if name == "status"], [("status", "Pending approval", "Approved")])
        shown = {name: (old, new) for name, old, new in chinese}
        self.assertEqual(shown["status"], ("待审批", "已通过"))
        self.assertNotIn("pending", " ".join(part for pair in shown.values() for part in pair))

    def test_a_stored_error_text_is_translated_in_the_changes_column(self):
        row = audit(
            self.auditor, "fleet.kick_member", f.fleet_type("B7 type 4"), None,
            {"error": "This character is not in the fleet.", "kicked": False, "note": "Free text stays"},
        )
        with translation.override(ZH):
            shown = {name: new for name, _old, new in audit_changes(row)}
            expected = translate_stored("This character is not in the fleet.")

        self.assertEqual(shown["error"], expected)
        self.assertNotEqual(shown["error"], "This character is not in the fleet.")
        self.assertEqual(shown["note"], "Free text stays")

    def test_an_empty_object_is_not_shown_as_braces(self):
        row = audit(self.auditor, "configuration.update", f.fleet_type("B7 type 5"), {}, {"name": "A"})

        with translation.override("en"):
            changes = audit_changes(row)

        self.assertEqual(changes, [("name", "—", "A")])
        row = audit(self.auditor, "configuration.update", f.fleet_type("B7 type 6"), {"name": "A", "extra": {}}, {"name": "B", "extra": []})
        self.assertNotIn("{}", str(audit_changes(row)))
        self.assertNotIn("[]", str(audit_changes(row)))

    def test_secrets_stay_hidden(self):
        row = audit(self.auditor, "configuration.update", f.fleet_type("B7 type 7"), {"webhook_url": "https://example.org/x"}, {"webhook_url": "y"})

        self.assertIn("***", str(audit_changes(row)))


# --- Discord ping text ----------------------------------------------------------------------------------------


class PingMarkdownTests(TestCase):
    def setUp(self):
        self.fc = f.create_user("b7_fc", perms=f.FC_PERMS, main_name="Example FC")

    def ping(self, **extra):
        operation = f.create_operation(self.fc, **extra)
        with mock.patch("fleetops.services.messages.eve_links.character_link", return_value="x"), mock.patch(
            "fleetops.services.messages.eve_links.system_link", return_value="x"
        ):
            return render_messages(operation).ping

    def test_the_escape_function(self):
        escape = mentions.escape_discord_markdown
        self.assertEqual(escape(">>> quote"), "\\>>> quote")
        self.assertEqual(escape("> quote"), "\\> quote")
        self.assertEqual(escape("# Heading"), "\\# Heading")
        self.assertEqual(escape("-# small"), "\\-# small")
        self.assertEqual(escape("- item"), "\\- item")
        self.assertEqual(escape("+ item"), "\\+ item")
        self.assertEqual(escape("1. first"), "1\\. first")
        self.assertEqual(escape("**bold** _it_ ~~gone~~ `code` ||spoiler||"), "\\*\\*bold\\*\\* \\_it\\_ \\~\\~gone\\~\\~ \\`code\\` \\|\\|spoiler\\|\\|")
        self.assertEqual(escape("[a](b) \\ <@123>"), "\\[a\\](b) \\\\ \\<@123>")
        self.assertEqual(escape("line one\n>>> two\nthree"), "line one\n\\>>> two\nthree")
        self.assertEqual(escape("  > indented"), "  \\> indented")
        self.assertEqual(escape(None), "")
        self.assertEqual(escape("Plain text, with a - dash and a > sign."), "Plain text, with a - dash and a > sign.")

    def test_a_typed_address_is_not_changed(self):
        escape = mentions.escape_discord_markdown
        url = "https://docs.example.org/spreadsheets/d/1a_B9x/edit?x=a~b&y=*|z"
        self.assertEqual(escape(f"Fits: {url} and _it_"), f"Fits: {url} and \\_it\\_")
        # The address runs to the next space, so the closing bracket after it is part of it; the opening bracket is
        # escaped, which is enough to stop a masked link.
        self.assertEqual(escape(f"[{url}](x)"), f"\\[{url}](x)")
        text = self.ping(additional_message=f"Fits: {url}\nsecond_line")
        self.assertIn(f"Fits: {url}\nsecond\\_line", text)

    def test_role_and_type_names_of_the_ping_are_escaped(self):
        names = messages._discord_names(
            {"ping_target_label": "*Staff* __x__", "ping_target_name": "_a_", "ping_channel": "~c~", "fleet_type": "Strat_Op"}
        )

        self.assertEqual(names["ping_target_label"], "\\*Staff\\* \\_\\_x\\_\\_")
        self.assertEqual(names["ping_target_name"], "\\_a\\_")
        self.assertEqual(names["ping_channel"], "\\~c\\~")
        self.assertEqual(names["fleet_type"], "Strat\\_Op")

    def test_an_additional_message_cannot_turn_the_ping_into_a_quote(self):
        text = self.ping(additional_message=">>> Bring cap boosters\n# Big heading\n- one\n- two")

        self.assertIn("\\>>> Bring cap boosters\n\\# Big heading\n\\- one\n\\- two", text)
        self.assertNotRegex(text, r"(?m)^>")
        self.assertNotRegex(text, r"(?m)^# ")
        # The markdown of the template itself is not touched.
        self.assertIn("**FC Name:** Example FC", text)
        self.assertIn("**PAP Type:** StratOp", text)

    def test_other_free_text_is_shown_as_typed_too(self):
        text = self.ping(formup="Jita *4-4*", doctrine_name="[ACTIVE] Hull_Fleet")

        self.assertIn("**Formup Location:** Jita \\*4-4\\*", text)
        self.assertIn("\\[ACTIVE\\] Hull\\_Fleet", text)

    def test_the_standalone_ping_escapes_and_has_no_extra_blank_line(self):
        ping = StandalonePing(
            additional_message="> not a quote",
            formup="Jita",
            sender_name="Boss",
            channel_name="STR",
            group_name="Sig A group",
        )

        text = render_standalone_ping(ping, actor=self.fc).ping

        self.assertIn("\\> not a quote", text)
        self.assertNotIn("\n\n-# ", text)
        self.assertRegex(text, r"\n-# FleetOps ping from ")

    def test_the_cancel_notice_escapes_the_place(self):
        operation = f.create_operation(self.fc, status="scheduled", formup="Jita _gate_")

        text = render_cancellation_notice(operation)

        self.assertIn("(Jita \\_gate\\_)", text)

    def test_the_motd_is_not_touched(self):
        operation = f.create_operation(self.fc, additional_message=">>> keep")
        with mock.patch("fleetops.services.messages.eve_links.character_link", return_value="x"), mock.patch(
            "fleetops.services.messages.eve_links.system_link", return_value="x"
        ):
            motd = render_messages(operation).motd

        self.assertIn("&gt;&gt;&gt; keep", motd)
        self.assertNotIn("\\", motd)


# --- The dashboard card of the FC month --------------------------------------------------------------------------


class FcMonthCardTests(TestCase):
    def page(self, user):
        self.client.force_login(user)
        return self.client.get(reverse("fleetops:dashboard"))

    def test_the_card_is_for_the_people_who_may_start_fleets(self):
        fc = f.create_user("b7_card_fc", perms=f.FC_PERMS)
        member = f.create_user("b7_card_member", perms=f.MEMBER_PERMS)

        shown = self.page(fc)
        hidden = self.page(member)

        self.assertContains(shown, "My FC Month")
        self.assertIsNotNone(shown.context["fc_stats"])
        self.assertNotContains(hidden, "My FC Month")
        self.assertNotContains(hidden, "FC credited fleets")
        self.assertIsNone(hidden.context["fc_stats"])
        self.assertContains(hidden, "Active Fleets")

    def test_a_group_leader_who_starts_group_fleets_sees_it(self):
        from fleetops.services import group_access

        member = f.create_user("b7_card_leader", perms=f.MEMBER_PERMS)
        with mock.patch.object(
            group_access, "leader_access", return_value=mock.MagicMock(fleet_group_ids={1}, group_ids={1}, grants=set())
        ):
            self.assertContains(self.page(member), "My FC Month")


# --- Recalculating an empty queue ----------------------------------------------------------------------------------


class RecalculateEmptyQueueTests(PaymentsBase):
    def test_an_empty_queue_says_so_in_blue_not_in_green(self):
        client = self.client_for(self.manager)

        response = client.post(route("srp_review_recalculate"), follow=True)

        self.assertIn(("info", "There are no pending claims to recalculate."), messages_of(response))
        self.assertFalse([tag for tag, _text in messages_of(response) if tag == "success"])

    def test_a_queue_with_claims_still_reports_success(self):
        f.srp_claim(self.member)
        client = self.client_for(self.manager)

        response = client.post(route("srp_review_recalculate"), follow=True)

        self.assertTrue([tag for tag, _text in messages_of(response) if tag == "success"])


# --- A payment without the shown amount ---------------------------------------------------------------------------


class ShownAmountRequiredTests(PaymentsBase):
    def test_a_row_without_its_shown_amount_is_not_paid(self):
        claim = self.approved()
        client = self.client_for(self.payer)

        response = client.post(route("srp_payments"), {"claim": [str(claim.uuid)]}, follow=True)

        claim.refresh_from_db()
        self.assertEqual(claim.status, SrpClaim.Status.APPROVED)
        self.assertTrue(any("did not say which amount" in text for _tag, text in messages_of(response)))

    def test_a_shown_amount_that_is_not_a_number_does_not_pay_either(self):
        claim = self.approved()
        client = self.client_for(self.payer)

        for bad in ("", "ten", "-5", "1e9", "١٢٣"):
            with self.subTest(bad=bad):
                client.post(route("srp_payments"), {"claim": [str(claim.uuid)], f"shown-{claim.uuid}": bad})
                claim.refresh_from_db()
                self.assertEqual(claim.status, SrpClaim.Status.APPROVED)

    def test_the_other_rows_of_the_same_request_are_paid(self):
        paid = self.approved()
        left_out = self.approved(self.other)
        client = self.client_for(self.payer)

        client.post(
            route("srp_payments"),
            {"claim": [str(paid.uuid), str(left_out.uuid)], f"shown-{paid.uuid}": str(paid.approved_amount)},
        )

        paid.refresh_from_db()
        left_out.refresh_from_db()
        self.assertEqual(paid.status, SrpClaim.Status.PAID)
        self.assertEqual(left_out.status, SrpClaim.Status.APPROVED)

    def test_the_right_amount_pays(self):
        claim = self.approved()
        client = self.client_for(self.payer)

        client.post(route("srp_payments"), {"claim": [str(claim.uuid)], f"shown-{claim.uuid}": str(claim.approved_amount)})

        claim.refresh_from_db()
        self.assertEqual(claim.status, SrpClaim.Status.PAID)

    def test_a_changed_amount_is_still_refused(self):
        claim = self.approved()
        client = self.client_for(self.payer)

        client.post(route("srp_payments"), {"claim": [str(claim.uuid)], f"shown-{claim.uuid}": "1"})

        claim.refresh_from_db()
        self.assertEqual(claim.status, SrpClaim.Status.APPROVED)

    def test_the_claim_page_button_needs_the_shown_amount(self):
        # The claim page always sends its amount field; a post without it is refused like a changed amount.
        claim = self.approved()
        client = self.client_for(self.payer)

        client.post(route("srp_claim_action", claim.uuid, "paid"), {})
        claim.refresh_from_db()
        self.assertEqual(claim.status, SrpClaim.Status.APPROVED)

        client.post(route("srp_claim_action", claim.uuid, "paid"), {"shown_amount": 1})
        claim.refresh_from_db()
        self.assertEqual(claim.status, SrpClaim.Status.APPROVED)

        client.post(route("srp_claim_action", claim.uuid, "paid"), {"shown_amount": claim.approved_amount})
        claim.refresh_from_db()
        self.assertEqual(claim.status, SrpClaim.Status.PAID)

    def test_the_service_without_the_requirement_is_unchanged(self):
        claim = self.approved()

        result = review.mark_paid([claim], actor=self.payer)

        self.assertEqual(len(result.done), 1)


# --- Export: the permission before the period ------------------------------------------------------------------


class ExportOrderTests(TestCase):
    def test_somebody_who_may_not_download_gets_403_whatever_the_period_says(self):
        member = f.create_user("b7_export_member", perms=f.MEMBER_PERMS)
        self.client.force_login(member)

        for dataset in ("srp-claims", "srp-members", "srp-corporations", "srp-fcs", "srp-trend", "srp-payable"):
            for query in ("from=2026-13-45&to=nonsense", "preset=nonsense", "from=2026-05-02&to=2026-04-01"):
                with self.subTest(dataset=dataset, query=query):
                    response = self.client.get(f"/fleetops/statistics/export/{dataset}/?format=csv&{query}")
                    self.assertIn(response.status_code, (403, 404), (dataset, query, response.status_code))

    def test_somebody_who_may_still_gets_400_for_a_bad_period(self):
        manager = f.create_user("b7_export_manager", perms=f.SRP_MANAGER_PERMS)
        self.client.force_login(manager)

        response = self.client.get("/fleetops/statistics/export/srp-claims/?format=csv&from=2026-13-45&to=nonsense")

        self.assertEqual(response.status_code, 400)
