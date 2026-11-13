# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Fixes found by the re-test of 0.3.0b4: the payments page counts only what the payer may pay, the saves that change
nothing on every configuration form, the words other plugins translate differently, the pre ping of an edited fleet
that was not sent, the changes on the audit log page and a few small wordings."""

import re
import uuid
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from unittest import mock

from allianceauth.eveonline.models import EveCorporationInfo
from django.contrib.auth.models import Group
from django.contrib.messages import get_messages
from django.forms import PasswordInput
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone, translation

from fleetops.forms_config import ShipLookup
from fleetops.models import (
    AuditLog,
    SrpFleetLink,
    ChannelPreset,
    CommsPreset,
    DiscordWebhook,
    FleetOperation,
    MessageTemplate,
    OperationAction,
    ShipFunctionRule,
    SrpClaim,
    SrpCorporationLimit,
    SrpSettings,
)
from fleetops.services import composition_config
from fleetops.services.messages import render_operation_messages
from fleetops.srp import payments

from . import factories as f
from .link_fakes import setUpModule, tearDownModule  # noqa: F401 - unittest runs them around the module (no ESI lookups)
from . import i18n_extract as extract
from . import i18n_utils as po
from .test_configuration_audit import WEBHOOK_SECRET, WEBHOOK_URL, settings_payload
from .test_srp_config import settings_post
from .test_srp_payments_batches import PaymentsBase, route

Status = SrpClaim.Status
CONFIG_PERMS = f.MEMBER_PERMS + ["fleetops.manage_configuration"]


def flashed(response):
    return [(message.level_tag, str(message)) for message in get_messages(response.wsgi_request)]


class PayableTotalTests(PaymentsBase):
    """D1: a player's card counts only the claims the viewer may pay."""

    def setUp(self):
        super().setUp()
        SrpSettings.objects.filter(pk=1).update(require_different_payer=True)
        # 10 million approved by the viewer (so the viewer may not pay it) and 25 million approved by someone else.
        self.mine = f.srp_claim(self.member, status=Status.APPROVED, amount=10_000_000, decided_by=self.payer2)
        self.fine = f.srp_claim(self.member, status=Status.APPROVED, amount=25_000_000, decided_by=self.manager)
        self.client = self.client_for(self.payer2)

    def test_the_row_adds_up_both_sums(self):
        claims = list(SrpClaim.objects.filter(pk__in=[self.mine.pk, self.fine.pk]))
        for claim in claims:
            claim.payable = claim.pk == self.fine.pk

        row = payments.pay_rows(claims)[0]

        self.assertEqual((row.total, row.payable_total, row.unpayable_total), (35_000_000, 25_000_000, 10_000_000))

    def test_the_export_holds_what_the_page_holds(self):
        from .test_export_datasets import csv_rows

        def download(**params):
            return self.client.get(reverse("fleetops:export", args=["srp-payable"]), {"format": "csv", **params})

        page = self.client.get(route("srp_payments"))

        by_player = csv_rows(download(group="player"))
        by_claim = csv_rows(download())

        self.assertEqual(int(by_player[-1][-1]), page.context["to_pay_total"])
        self.assertEqual(int(by_claim[-1][-1]), 25_000_000)
        self.assertEqual([row[2] for row in by_player[1:-1]], ["1"])
        self.assertEqual([row[-1] for row in by_player[1:-1]], ["25000000"])
        self.assertEqual(len(by_claim), 1 + 1 + 1)
        # The About sheet of the workbook says what was left out.
        import io
        import zipfile

        archive = zipfile.ZipFile(io.BytesIO(f.streamed_body(download(format="xlsx"))))
        text = "".join(archive.read(name).decode("utf-8") for name in archive.namelist() if name.endswith(".xml"))
        self.assertIn("1 claims (10000000 ISK) that you may not pay", text)
        self.assertNotIn("35000000", text)

    def test_a_row_without_the_mark_counts_every_claim(self):
        row = payments.pay_rows(list(SrpClaim.objects.filter(pk=self.mine.pk)))[0]

        self.assertEqual(row.payable_total, 10_000_000)

    def test_the_card_the_copy_buttons_and_the_top_total_use_the_payable_sum(self):
        page = self.client.get(route("srp_payments"))

        row = page.context["pay_rows"][0]
        self.assertEqual(row.payable_total, 25_000_000)
        self.assertEqual(page.context["to_pay_total"], 25_000_000)
        self.assertEqual(page.context["to_pay_unpayable_total"], 10_000_000)
        self.assertContains(page, 'data-srp-copy="25000000"')
        self.assertContains(page, "&#9;25000000")
        self.assertNotContains(page, 'data-srp-copy="35000000"')
        self.assertNotContains(page, "&#9;35000000")
        self.assertContains(page, "plus 10,000,000 ISK you may not pay", count=2)

    def test_a_card_that_the_viewer_may_not_pay_at_all_has_no_copy_amount(self):
        SrpClaim.objects.filter(pk=self.fine.pk).update(decided_by=self.payer2)

        page = self.client.get(route("srp_payments"))

        self.assertEqual(page.context["pay_rows"][0].payable_total, 0)
        self.assertEqual(page.context["to_pay_total"], 0)
        self.assertNotContains(page, "Copy amount")
        self.assertNotContains(page, "Copy name and amount")
        self.assertContains(page, "Copy name")

    def test_a_viewer_who_may_pay_everything_sees_no_extra_line(self):
        page = self.client_for(self.payer).get(route("srp_payments"))

        self.assertEqual(page.context["to_pay_total"], 35_000_000)
        self.assertNotContains(page, "you may not pay")
        self.assertContains(page, 'data-srp-copy="35000000"')


class AllConfigurationFormsTests(TestCase):
    """D3: an unchanged save writes no audit entry on any configuration form, and one changed field writes one."""

    def setUp(self):
        self.admin = f.create_user("b5_config_admin", perms=CONFIG_PERMS)
        self.client.force_login(self.admin)

    def updates(self):
        return AuditLog.objects.filter(action="configuration.update").count()

    @staticmethod
    def posted(form):
        """What a browser posts for the form: every field with the value it shows (a password field stays empty)."""
        data = {}
        for name, field in form.fields.items():
            if isinstance(field.widget, PasswordInput):
                data[name] = ""
                continue
            value = form[name].value()
            if isinstance(value, bool):
                if value:
                    data[name] = "on"
            elif isinstance(value, (list, tuple, set)):
                data[name] = [str(item.pk if hasattr(item, "pk") else item) for item in value]
            elif value is not None:
                data[name] = field.widget.format_value(value) or ""
        return data

    def objects(self):
        """One stored row per list section of the configuration centre, and the field a test changes."""
        group = Group.objects.create(name="B5 group")
        corp = EveCorporationInfo.objects.create(
            corporation_id=98_000_001, corporation_name="B5 Corp", corporation_ticker="B5C", member_count=1
        )
        f.clear_ship_functions()
        function = f.ship_function("B5 function", rules=())
        # The form stores the name of the target as the label, so a stored rule has it already.
        label = composition_config.target_label("group", 832) or composition_config.default_label("group", 832)
        rule = ShipFunctionRule.objects.create(function=function, kind="group", target_id=832, label=label)
        return {
            "fleet-types": (f.fleet_type("B5 type"), "name"),
            "multiplier-tiers": (f.multiplier_tiers((90, 2))[0], "threshold_minutes"),
            "statistics-targets": (f.statistics_target(2026, 1, "2.5", note="b5"), "note"),
            "special-roles": (f.special_role("B5 role"), "name"),
            "comms": (CommsPreset.objects.create(name="B5 comms", channel_name="Comms 1"), "name"),
            "channels": (
                ChannelPreset.objects.create(name="B5 channel", channel_type="logi", channel_value="Logi 1"),
                "name",
            ),
            "ping-channels": (f.ping_channel("B5 channel"), "name"),
            "ping-roles": (f.ping_role("B5 role", role_id=123456789012345), "name"),
            "templates": (
                MessageTemplate.objects.create(
                    name="B5 MOTD", template_type=MessageTemplate.TemplateType.MOTD, content="{{ fc }}", is_active=True
                ),
                "name",
            ),
            "ship-functions": (function, "name"),
            "ship-function-rules": (rule, "target"),
            "structure-templates": (f.structure_template("B5 structure"), "name"),
            "srp-categories": (f.srp_category("B5 category"), "name"),
            "srp-rules": (f.payout_rule(587, 50_000_000), "amount"),
            "groups": (f.group_access(group), "note"),
            "srp-corporation-limits": (
                SrpCorporationLimit.objects.create(corporation_id=corp.corporation_id, corporation_name="B5 Corp"),
                "note",
            ),
        }

    @staticmethod
    def changed(value):
        plain = str(value).replace(",", "")
        return str(int(plain) + 1) if plain.isdigit() else f"{value} X"

    def test_every_list_section_is_covered(self):
        from fleetops.views_config import CONFIG_SECTIONS

        # A section added later needs its row in objects() above (the whole point of this test).
        self.assertEqual(set(self.objects()), set(CONFIG_SECTIONS))

    def test_an_unchanged_save_writes_nothing_and_one_changed_field_writes_one_entry(self):
        from fleetops.views_config import CONFIG_SECTIONS

        for section, (obj, field) in self.objects().items():
            with self.subTest(section=section), mock.patch("fleetops.forms_config.lookup_ship", return_value=ShipLookup(587, "Ship 587")):
                url = reverse("fleetops:configuration_edit", args=[section, obj.pk])
                form = CONFIG_SECTIONS[section]["form"](instance=type(obj).objects.get(pk=obj.pk))
                data = self.posted(form)
                before = self.updates()

                response = self.client.post(url, data)

                self.assertEqual(response.status_code, 302, getattr(getattr(response, "context", None), "get", lambda *_: None)("form") and response.context["form"].errors)
                self.assertEqual(self.updates(), before, "an unchanged save wrote an audit entry")
                self.assertIn(("info", "Nothing was changed."), flashed(response))
                self.assertNotIn("success", [level for level, _text in flashed(response)])
                self.client.get(response["Location"])  # shows (and so clears) the messages

                data[field] = self.changed(data[field])
                response = self.client.post(url, data)

                self.assertEqual(response.status_code, 302, getattr(getattr(response, "context", None), "get", lambda *_: None)("form") and response.context["form"].errors)
                self.assertEqual(self.updates(), before + 1, "one changed field must write exactly one entry")
                self.client.get(response["Location"])

    def test_the_two_pages_that_are_not_list_sections(self):
        settings_url = reverse("fleetops:configuration_settings")
        self.client.post(settings_url, settings_payload())
        before = self.updates()
        response = self.client.post(settings_url, settings_payload())
        self.assertEqual(self.updates(), before)
        self.assertIn(("info", "Nothing was changed."), flashed(response))
        self.client.post(settings_url, settings_payload(tracking_interval="120"))
        self.assertEqual(self.updates(), before + 1)

        srp_url = reverse("fleetops:configuration_list", args=["srp"])
        self.client.post(srp_url, settings_post(claim_deadline_days="30"))
        before = self.updates()
        response = self.client.post(srp_url, settings_post(claim_deadline_days="30"))
        self.assertEqual(self.updates(), before)
        self.assertIn(("info", "Nothing was changed."), flashed(response))
        self.client.post(srp_url, settings_post(claim_deadline_days="31"))
        self.assertEqual(self.updates(), before + 1)

    def test_a_newly_typed_webhook_is_audited_and_stays_redacted(self):
        channel = f.ping_channel("B5 hook")
        url = reverse("fleetops:configuration_edit", args=["ping-channels", channel.pk])
        from fleetops.views_config import CONFIG_SECTIONS

        data = self.posted(CONFIG_SECTIONS["ping-channels"]["form"](instance=channel))
        data["webhook_url"] = WEBHOOK_URL
        before = self.updates()

        response = self.client.post(url, data)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.updates(), before + 1)
        entry = AuditLog.objects.filter(action="configuration.update").latest("pk")
        self.assertTrue(entry.new_value["webhook_url_replaced"])
        self.assertNotIn(WEBHOOK_SECRET, str(entry.old_value) + str(entry.new_value))
        self.assertEqual(DiscordWebhook.objects.get(pk=channel.pk).webhook_url, WEBHOOK_URL)

    def test_the_ping_role_with_the_role_id_shown_as_text(self):
        role = f.ping_role("B5 sig", role_id=555000111222333)
        url = reverse("fleetops:configuration_edit", args=["ping-roles", role.pk])
        before = self.updates()

        from fleetops.views_config import CONFIG_SECTIONS

        data = self.posted(CONFIG_SECTIONS["ping-roles"]["form"](instance=role))
        # A browser posts the number as text; the form shows it as a number.
        self.assertEqual(data["role_id"], "555000111222333")
        response = self.client.post(url, data)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.updates(), before)
        self.assertIn(("info", "Nothing was changed."), flashed(response))


# --- D4: words other plugins translate differently ----------------------------------------------------

# FleetOps messages that are common words of the other apps of an Alliance Auth site (the SRP app aa-srp above all).
# A message without a context is one entry of the shared catalog, and the app that is loaded last wins: "Request SRP"
# showed as "request ship compensation", "Registered" as "registered (account)". With the context "FleetOps" the
# message is FleetOps' own. The catalog of aa-srp is not installed here, so the words are listed; add a word when a
# site shows another plugin's translation for a FleetOps text.
COLLIDING_WORDS = [
    "Request SRP",
    "Registered",
    "SRP link",
    "SRP links",
    "SRP",
    "Total",
    "First",
    "Last",
    "Previous",
    "Next",
    "Create SRP link",
    "Created at",
    "Expires at",
    "Overview",
    "Search",
    "Killmail",
    # Words of the aasrp 5.5.0 catalog that it translates differently from FleetOps (compared with its zh_Hans file).
    "Approve",
    "Actions",
    "Additional information",
    "FC",
]
CONTEXT = "FleetOps"


class SharedCatalogTests(SimpleTestCase):
    @classmethod
    def code_messages(cls):
        found = {}
        for path in extract.python_files():
            found.update(extract.python_messages(path))
        for path in extract.template_files():
            found.update(extract.template_messages(path))
        return found

    def test_the_colliding_words_are_only_used_with_the_context(self):
        used = set(self.code_messages())

        plain = [word for word in COLLIDING_WORDS if word in used]

        self.assertEqual(plain, [], "use pgettext('FleetOps', ...) or {% translate ... context 'FleetOps' %}")

    def test_the_catalog_has_no_entry_without_context_for_them_and_translates_each_with_it(self):
        entries = po.read_po(po.LOCALE_DIR / "zh_Hans" / "LC_MESSAGES" / "django.po")
        by_key = {entry.key: entry for entry in entries if not entry.is_header and not entry.obsolete}
        for word in COLLIDING_WORDS:
            with self.subTest(word=word):
                self.assertNotIn(word, by_key)
                entry = by_key[f"{CONTEXT}{po.CONTEXT_SEPARATOR}{word}"]
                self.assertTrue(entry.msgstr.get(0))


class CompetingCatalogTests(TestCase):
    def test_another_plugins_catalog_cannot_override_what_fleetops_shows(self):
        user = f.create_user("b5_catalog_member", perms=f.MEMBER_PERMS + ["fleetops.request_srp"])
        f.srp_settings(enabled=True)
        self.client.force_login(user)
        self.client.cookies["django_language"] = "zh-hans"
        with translation.override("zh-hans"):
            catalog = translation.trans_real.translation("zh-hans")._catalog
            before = str(SrpFleetLink._meta.verbose_name)
            fake = {word: f"OTHER {word}" for word in COLLIDING_WORDS}
            catalog._catalogs.insert(0, fake)
            try:
                page = self.client.get(reverse("fleetops:srp_home"))
                after = str(SrpFleetLink._meta.verbose_name)
            finally:
                catalog._catalogs.remove(fake)

        self.assertEqual(page.status_code, 200)
        self.assertNotContains(page, "OTHER ")
        self.assertEqual(after, before)
        self.assertNotIn("OTHER", after)


# --- D2: the updated pre ping during the cooldown -----------------------------------------------------

from .test_scheduling import EVERYONE, UTC, SchedulingTestBase  # noqa: E402


class EditDuringCooldownTests(SchedulingTestBase):
    def edit(self, operation, **extra):
        when = operation.scheduled_at.astimezone(UTC)
        data = {
            "fleet_type": str(self.fleet_type.pk),
            "formup": operation.formup,
            "fleet_date": when.strftime("%Y-%m-%d"),
            "fleet_clock": when.strftime("%H:%M"),
            "ping_channel": str(self.channel.pk),
            "ping_mentions": [EVERYONE],
        }
        data.update(extra)
        self.client.force_login(self.fc)
        return self.client.post(reverse("fleetops:edit_scheduled_fleet", args=[operation.uuid]), data)

    def test_a_pre_ping_that_the_cooldown_holds_back_is_not_reported_as_posted(self):
        operation = self.scheduled()
        first = self.edit(operation, formup="Perimeter", send_update="on", edit_token=str(uuid.uuid4()))
        self.client.get(first["Location"])
        self.assertEqual(self.discord_post.call_count, 1)

        second = self.edit(operation, formup="Jita", send_update="on", edit_token=str(uuid.uuid4()))

        self.assertEqual(self.discord_post.call_count, 1, "nothing is sent during the cooldown")
        levels = flashed(second)
        self.assertEqual([level for level, _text in levels], ["warning"])
        text = levels[0][1]
        self.assertIn("the updated pre ping was not posted", text)
        self.assertIn("received a ping", text)
        self.assertIn("retry it later", text)
        self.assertNotIn("was posted", text.replace("was not posted", ""))
        operation.refresh_from_db()
        self.assertEqual(operation.formup, "Jita")
        self.assertEqual(operation.actions.get(action="pre_ping").status, OperationAction.Status.FAILED)

    def test_a_posted_update_still_says_so(self):
        operation = self.scheduled()

        response = self.edit(operation, formup="Perimeter", send_update="on", edit_token=str(uuid.uuid4()))

        self.assertEqual(
            flashed(response), [("success", "Scheduled fleet updated and the updated pre ping was posted.")]
        )

    def test_an_edit_without_the_update_is_not_a_warning(self):
        operation = self.scheduled()

        response = self.edit(operation, formup="Perimeter")

        self.assertEqual([level for level, _text in flashed(response)], ["success"])


# --- D5: the changes on the audit log page ------------------------------------------------------------

class AuditChangesColumnTests(TestCase):
    def setUp(self):
        self.auditor = f.create_user("b5_auditor", perms=f.MEMBER_PERMS + ["fleetops.view_audit_log"])
        self.client.force_login(self.auditor)
        self.fleet_type = f.fleet_type("B5 type")

    def page(self):
        response = self.client.get(reverse("fleetops:audit_log"))
        self.assertEqual(response.status_code, 200)
        return response

    def test_a_short_change_is_shown_in_the_row_and_a_long_one_folds_out(self):
        from fleetops.services.audit import audit

        audit(self.auditor, "configuration.update", self.fleet_type, {"name": "Old"}, {"name": "New"})
        audit(
            self.auditor,
            "configuration.update",
            self.fleet_type,
            {"a": 1, "b": "x", "c": [1, 2]},
            {"a": 2, "b": "y", "c": [1, 3]},
        )

        page = self.page()

        self.assertContains(page, "Changes</th>")
        self.assertContains(page, "<code>name</code> <span class=\"text-muted text-break\">Old</span> &rarr; <span class=\"text-break\">New</span>")
        self.assertContains(page, "<details><summary>3 changes</summary>")
        self.assertContains(page, "<code>c</code>")

    def test_only_what_differs_is_listed(self):
        from fleetops.services.audit import audit

        row = audit(self.auditor, "configuration.update", self.fleet_type, {"a": 1, "b": 2}, {"a": 1, "b": 3})

        from fleetops.templatetags.fleetops_config import audit_changes

        self.assertEqual(audit_changes(row), [("b", "2", "3")])

    def test_created_and_deleted_objects_list_their_values(self):
        from fleetops.services.audit import audit
        from fleetops.templatetags.fleetops_config import audit_changes

        created = audit(self.auditor, "configuration.create", self.fleet_type, None, {"name": "N"})
        deleted = audit(self.auditor, "configuration.delete", self.fleet_type, {"name": "N"}, None)

        self.assertEqual(audit_changes(created), [("name", "—", "N")])
        self.assertEqual(audit_changes(deleted), [("name", "N", "—")])

    def test_values_are_escaped_and_cut(self):
        from fleetops.services.audit import audit

        audit(self.auditor, "configuration.update", self.fleet_type, {"name": "<b>x</b>"}, {"name": "y" * 500})

        page = self.page()

        self.assertNotContains(page, "<b>x</b>")
        self.assertContains(page, "&lt;b&gt;x&lt;/b&gt;")
        self.assertNotContains(page, "y" * 200)

    def test_secrets_are_never_shown(self):
        from fleetops.services.audit import audit

        channel = f.ping_channel("B5 hook", url=WEBHOOK_URL)
        # A saved ping channel: the snapshot holds a marker instead of the address.
        self.client.force_login(f.create_user("b5_cfg", perms=CONFIG_PERMS))
        from fleetops.views_config import CONFIG_SECTIONS

        data = AllConfigurationFormsTests.posted(CONFIG_SECTIONS["ping-channels"]["form"](instance=channel))
        data["webhook_url"] = f"https://discord.com/api/webhooks/998877665544/{WEBHOOK_SECRET}-new"
        self.client.post(reverse("fleetops:configuration_edit", args=["ping-channels", channel.pk]), data)
        # Whatever else an entry holds: keys and values that look like secrets are hidden on the page.
        audit(
            self.auditor,
            "x.test",
            self.fleet_type,
            {"webhook_url": WEBHOOK_URL, "join_token": "abc-123-token", "note": f"see {WEBHOOK_URL}"},
            {"webhook_url": "other", "join_token": "def-456-token", "note": "none"},
        )
        self.client.force_login(self.auditor)

        page = self.page()

        content = page.content.decode()
        self.assertNotIn(WEBHOOK_SECRET, content)
        self.assertNotIn("abc-123-token", content)
        self.assertNotIn("def-456-token", content)
        self.assertNotIn("/api/webhooks/", content)
        self.assertIn("webhook_url_replaced", content)
        self.assertIn("***", content)
        # The stored snapshots of the real save hold no secret either.
        saved = AuditLog.objects.get(action="configuration.update", object_type="DiscordWebhook")
        self.assertNotIn(WEBHOOK_SECRET, f"{saved.old_value}{saved.new_value}")

    def test_login_data_in_old_voice_links_is_never_shown(self):
        from fleetops.services.audit import audit
        from fleetops.templatetags.fleetops_config import audit_changes

        # 0.1.x kept voice links as they were typed, and the audit entries of that time still hold them.
        old = audit(
            self.auditor,
            "configuration.update",
            self.fleet_type,
            {"voice_url": "mumble://fc:hunter2@voice.example.com/room"},
            {"voice_url": "mumble://voice.example.com/room"},
        )
        created = audit(self.auditor, "configuration.create", self.fleet_type, None, {"note": "join mumble://u:pw9876@host now"})

        self.assertEqual(
            audit_changes(old), [("voice_url", "mumble://***@voice.example.com/room", "mumble://voice.example.com/room")]
        )
        page = self.page()
        self.assertNotContains(page, "hunter2")
        self.assertNotContains(page, "pw9876")
        self.assertEqual(audit_changes(created)[0][2], "join mumble://***@host now")

    def test_versioned_webhook_addresses_are_hidden_too(self):
        from fleetops.services.audit import audit

        audit(
            self.auditor,
            "x.test",
            self.fleet_type,
            {"target_value": "https://discord.com/api/v10/webhooks/1/abcSECRET"},
            {"target_value": "see https://discord.com/api/webhooks/2/defSECRET"},
        )

        page = self.page()

        self.assertNotContains(page, "abcSECRET")
        self.assertNotContains(page, "defSECRET")

    def test_the_replaced_flag_of_a_webhook_is_shown_as_a_flag(self):
        from fleetops.services.audit import audit
        from fleetops.templatetags.fleetops_config import audit_changes

        row = audit(self.auditor, "x.test", self.fleet_type, {"webhook_url_replaced": False}, {"webhook_url_replaced": True})

        self.assertEqual(audit_changes(row), [("webhook_url_replaced", "false", "true")])

    def test_the_page_costs_the_same_queries_for_more_entries(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        from fleetops.services.audit import audit

        def queries():
            with CaptureQueriesContext(connection) as captured:
                self.page()
            return len(captured)

        audit(self.auditor, "configuration.update", self.fleet_type, {"a": 1}, {"a": 2})
        self.page()  # warms the caches of the first request (content types, permissions)
        one = queries()
        for index in range(15):
            audit(self.auditor, "configuration.update", self.fleet_type, {"a": index, "b": 1}, {"a": index + 1, "b": 2})

        self.assertEqual(queries(), one)


# --- D6: the reason of a rejection on the audit log ---------------------------------------------------

from .test_approvals import ApprovalTestCase  # noqa: E402
from fleetops.services.approvals import reject_attendance, reject_operation  # noqa: E402


class RejectionReasonAuditTests(ApprovalTestCase):
    def test_the_reason_of_a_rejected_fleet_is_in_the_reason_column(self):
        operation = self.manual_fleet()
        self.manual_row(operation, self.pilot)

        reject_operation(operation, actor=self.lead, note="Wrong date")

        self.assertEqual(AuditLog.objects.get(action="approval.operation_reject").reason, "Wrong date")

    def test_the_reason_of_rejected_pap_rows_is_in_the_reason_column(self):
        operation = self.manual_fleet()
        row = self.manual_row(operation, self.pilot)

        reject_attendance([row], actor=self.lead, note="Was not there")

        self.assertEqual(AuditLog.objects.get(action="approval.attendance_reject").reason, "Was not there")
        self.client.force_login(f.create_user("b5_audit_reader", perms=f.MEMBER_PERMS + ["fleetops.view_audit_log"]))
        self.assertContains(self.client.get(reverse("fleetops:audit_log")), "Was not there")


# --- D7: the button to the SRP fleet --------------------------------------------------------------------

class OpenSrpFleetButtonTests(TestCase):
    def setUp(self):
        self.fc = f.create_user("b5_fc_plain", perms=f.FC_PERMS)
        self.fc_srp = f.create_user("b5_fc_srp", perms=[*f.FC_PERMS, "fleetops.request_srp"])
        f.srp_settings(enabled=True)

    def page(self, user, url):
        operation = f.create_operation(
            user, status=FleetOperation.Status.CLOSED, started_at=timezone.now() - timedelta(hours=3), srp_url=url,
            srp_reference="SRP-1", srp_provider="x"
        )
        self.client.force_login(user)
        return self.client.get(reverse("fleetops:operation_detail", args=[operation.uuid])), operation

    def test_an_fc_without_the_request_right_does_not_get_the_button_to_the_page_of_the_link(self):
        operation = f.create_operation(
            self.fc, status=FleetOperation.Status.CLOSED, started_at=timezone.now() - timedelta(hours=3),
            srp_url="/fleetops/srp/l/abc123/", srp_reference="SRP-1", srp_provider="fleetops",
        )
        f.fleet_link(operation)
        self.client.force_login(self.fc)
        page = self.client.get(reverse("fleetops:operation_detail", args=[operation.uuid]))

        self.assertEqual(page.status_code, 200)
        self.assertNotContains(page, "Open SRP Fleet")

    def test_a_site_relative_address_of_another_srp_app_keeps_its_button(self):
        page, _operation = self.page(self.fc, "/ship-replacement/abc/")

        self.assertContains(page, "Open SRP Fleet")
        self.assertContains(page, "/ship-replacement/abc/")

    def test_a_member_who_may_request_keeps_the_button_while_the_link_is_closed(self):
        operation = f.create_operation(
            self.fc_srp, status=FleetOperation.Status.CLOSED, started_at=timezone.now() - timedelta(hours=3),
            srp_url="/fleetops/srp/l/abc123/", srp_reference="SRP-1", srp_provider="fleetops",
        )
        link = f.fleet_link(operation)
        SrpFleetLink.objects.filter(pk=link.pk).update(is_open=False)
        self.client.force_login(self.fc_srp)

        page = self.client.get(reverse("fleetops:operation_detail", args=[operation.uuid]))

        self.assertContains(page, "Open SRP Fleet")

    def test_an_external_srp_address_keeps_its_button(self):
        page, _operation = self.page(self.fc, "https://srp.example.org/fleet/7/")

        self.assertContains(page, "Open SRP Fleet")
        self.assertContains(page, "https://srp.example.org/fleet/7/")

    def test_a_member_who_may_request_gets_the_button_for_an_open_link(self):
        operation = f.create_operation(
            self.fc_srp, status=FleetOperation.Status.CLOSED, started_at=timezone.now() - timedelta(hours=3),
            srp_url="/fleetops/srp/l/abc123/", srp_reference="SRP-1", srp_provider="fleetops",
        )
        f.fleet_link(operation)
        self.client.force_login(self.fc_srp)

        page = self.client.get(reverse("fleetops:operation_detail", args=[operation.uuid]))

        self.assertContains(page, "Open SRP Fleet")


# --- D8: the category of a loss outside a fleet ---------------------------------------------------------

class CategoryRequiredMessageTests(TestCase):
    def test_the_missing_category_has_the_wording_of_the_service(self):
        from fleetops.forms_srp import ClaimInputForm

        f.srp_settings(out_of_fleet_category_required=True, default_category=None)
        f.srp_category("B5 choosable")

        form = ClaimInputForm({"killmail": "123456 abcdef", "category": ""}, with_category=True)

        self.assertFalse(form.is_valid())
        self.assertEqual(form.errors["category"], ["Choose a category for this loss."])

    def test_the_message_is_in_chinese_too(self):
        from fleetops.forms_srp import ClaimInputForm

        f.srp_settings(out_of_fleet_category_required=True, default_category=None)
        f.srp_category("B5 choosable")

        with translation.override("zh-hans"):
            form = ClaimInputForm({"killmail": "123456 abcdef", "category": ""}, with_category=True)
            form.is_valid()

            self.assertNotIn("必填", form.errors["category"][0])
            self.assertEqual(form.errors["category"][0], translation.gettext("Choose a category for this loss."))


# --- D9, D11, D12, D13, D15: wording and counting -----------------------------------------------------

class SmallWordingTests(ApprovalTestCase):
    def zh(self):
        from django.conf import settings

        self.client.cookies[settings.LANGUAGE_COOKIE_NAME] = "zh-hans"
        # The middleware activates the language of the request for the rest of the test run.
        self.addCleanup(translation.deactivate)

    def test_the_rank_sentence_takes_its_full_stop_from_the_catalog(self):
        user = f.create_user("b5_rank", perms=f.MEMBER_PERMS)
        self.client.force_login(user)
        card = {"rank": 2, "of": 9, "scope": "alliance"}
        with mock.patch("fleetops.views_statistics._rank_card", return_value=card):
            english = self.client.get(reverse("fleetops:my_statistics"))
            self.zh()
            chinese = self.client.get(reverse("fleetops:my_statistics"))

        self.assertContains(english, "Among the alliance members with PAP this month. <a")
        self.assertContains(chinese, "在本月有 PAP 的联盟成员中。 <a")
        self.assertNotContains(chinese, "中. <a")
        self.assertNotContains(chinese, "。.")

    def test_the_multiplier_error_lists_the_multipliers_in_the_language_of_the_viewer(self):
        from .test_fleet_end import EndTestBase  # noqa: F401 - the same set-up is wanted, written out below

        fc = f.create_user("b5_end_fc", perms=f.FC_PERMS)
        operation = f.create_operation(fc, started_at=timezone.now() - timedelta(minutes=200))
        f.multiplier_tiers((90, 2), (180, 3))
        self.client.force_login(fc)
        self.zh()

        response = self.client.post(reverse("fleetops:end_fleet", args=[operation.uuid]), {"attendance_multiplier": "9"})

        text = [str(message) for message in get_messages(response.wsgi_request)][-1]
        self.assertIn("1 倍、2 倍、3 倍", text)
        self.assertNotIn(",", text)

    def test_a_pending_manual_pap_row_says_pending_approval_like_a_pending_fleet(self):
        operation = self.manual_fleet()
        self.manual_row(operation, self.pilot)
        self.client.force_login(self.lead)
        english = self.client.get(reverse("fleetops:operation_detail", args=[operation.uuid]))
        self.zh()
        chinese = self.client.get(reverse("fleetops:operation_detail", args=[operation.uuid]))

        def section(page):
            html = page.content.decode()
            start = html.index('id="pap-review"')
            return html[start : html.index("</details>", start)]

        self.assertIn("Pending approval", section(english))
        self.assertIn("Manual PAP waiting for approval", section(english))
        self.assertIn("待审批", section(chinese))
        self.assertIn("等待审批的补录 PAP", section(chinese))
        self.assertNotIn("待审核", section(chinese))

    def test_the_manual_pap_page_says_pending_approval(self):
        operation = self.manual_fleet(review_status=ReviewStatusValues.APPROVED)
        self.manual_row(operation, self.pilot)
        user = f.create_user("b5_manual", perms=[*f.FC_PERMS, "fleetops.manage_attendance"])
        self.client.force_login(user)

        page = self.client.get(reverse("fleetops:manual_attendance"))

        self.assertEqual(page.status_code, 200)
        self.assertNotContains(page, ">Pending<")


from fleetops.models import ReviewStatus as ReviewStatusValues  # noqa: E402


class DoctrineSettingsNothingChangedTests(TestCase):
    def test_nothing_changed_is_an_info_message(self):
        admin = f.create_user("b5_doc_admin", perms=f.MEMBER_PERMS + ["fleetops.manage_configuration"])
        self.client.force_login(admin)

        response = self.client.post(reverse("fleetops:doctrine_orders"), {"custom_mode": "everybody"})

        self.assertEqual(response.status_code, 302)
        self.assertEqual(flashed(response), [("info", "Nothing was changed.")])


class MotdLengthTests(TestCase):
    def test_entities_count_as_one_character_and_the_sent_text_keeps_them(self):
        import html

        from fleetops.services.messages import render_messages

        f.settings(motd_warning_length=900)
        fc = f.create_user("b5_motd_fc", perms=f.FC_PERMS)
        # 200 characters that are written as &gt; in the MOTD: 800 characters with the entities, 200 without.
        operation = f.create_operation(fc, status=FleetOperation.Status.ACTIVE, additional_message=">" * 600)

        rendered = render_messages(operation)

        self.assertIn("&gt;", rendered.motd)
        self.assertEqual(rendered.motd_length, len(html.unescape(rendered.motd)))
        self.assertLess(rendered.motd_length, len(rendered.motd))
        self.assertFalse([warning for warning in rendered.warnings if "MOTD is" in warning])

    def test_a_motd_that_is_long_without_the_entities_still_warns(self):
        from fleetops.services.messages import render_messages

        f.settings(motd_warning_length=300)
        fc = f.create_user("b5_motd_fc2", perms=f.FC_PERMS)
        operation = f.create_operation(fc, status=FleetOperation.Status.ACTIVE, additional_message=">" * 600)

        rendered = render_messages(operation)

        self.assertTrue([warning for warning in rendered.warnings if "MOTD is" in warning])
