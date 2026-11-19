# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""The rest of the fixes from the regression run of 0.3.0b6: the payout import command, the reject note of the
approvals page, the member export of an unknown corporation, a second manual PAP, the old ban rejections and the
continuous numbers of payment batches."""

import csv
import os
import re
import tempfile
from datetime import timedelta
from io import StringIO
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.core.management import CommandError, call_command
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from fleetops.forms_config import ShipLookup
from fleetops.models import (
    MAX_DATABASE_ID,
    AttendanceRecord,
    AuditLog,
    FleetOperation,
    ReviewStatus,
    SrpBan,
    SrpClaim,
    SrpClaimEvent,
    SrpPaymentBatch,
    SrpPayoutRule,
    SrpSettings,
)
from fleetops.srp import bans, claims, payments, review
from fleetops.templatetags.fleetops_config import AUDIT_ACTION_LABELS

from . import factories as f
from .test_export_datasets import csv_rows
from .test_srp_claims import ClaimBase
from .test_srp_payments_batches import PaymentsBase

Status = SrpClaim.Status
Action = SrpClaimEvent.Action
COLUMN = "Loose stack"


# --- B6-5, B6-6, L-11: the payout import command ---------------------------------------------------------------------


class PayoutImportTests(TestCase):
    def setUp(self):
        self.category = f.srp_category("Loose stack", key="loose-stack")
        patcher = mock.patch(
            "fleetops.management.commands.fleetops_srp_import_payouts.lookup_ship",
            side_effect=lambda text: ShipLookup(int(text), f"Looked up {text}"),
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def write(self, rows, header=("type_id", "name", COLUMN), raw=None):
        handle = tempfile.NamedTemporaryFile("wb", suffix=".csv", delete=False)
        self.addCleanup(os.unlink, handle.name)
        if raw is not None:
            handle.write(raw)
        else:
            text = StringIO()
            writer = csv.writer(text)
            writer.writerow(header)
            writer.writerows(rows)
            handle.write(text.getvalue().encode("utf-8"))
        handle.close()
        return handle.name

    def run_command(self, path, *args, expect_error=False):
        out, err = StringIO(), StringIO()
        arguments = [path, "--category", "loose-stack", "--column", COLUMN, *args]
        if expect_error:
            with self.assertRaises(CommandError) as caught:
                call_command("fleetops_srp_import_payouts", *arguments, stdout=out, stderr=err)
            return str(caught.exception)
        call_command("fleetops_srp_import_payouts", *arguments, stdout=out, stderr=err)
        return out.getvalue(), err.getvalue()

    def test_a_type_id_above_the_database_range_is_reported_in_the_dry_run(self):
        too_big = str(MAX_DATABASE_ID + 1)
        path = self.write([("1001", "Fine", "5000000"), (too_big, "Huge", "5000000"), ("0", "Zero", "5000000")])

        out, err = self.run_command(path)

        self.assertIn("1 rules would be created", out)
        self.assertIn("2 rows with a problem", out)
        self.assertIn("Line 3: skipped (type_id is not a ship type ID).", err)
        self.assertIn("Line 4: skipped (type_id is not a ship type ID).", err)

    def test_apply_does_not_crash_on_a_type_id_above_the_range_and_writes_the_other_rows(self):
        path = self.write([("1001", "Fine", "5000000"), (str(MAX_DATABASE_ID + 1), "Huge", "5000000")])

        out, err = self.run_command(path, "--apply")

        self.assertIn("1 rules created", out)
        self.assertEqual(list(SrpPayoutRule.objects.values_list("ship_type_id", flat=True)), [1001])

    def test_the_largest_allowed_type_id_is_accepted(self):
        path = self.write([(str(MAX_DATABASE_ID), "Edge", "5000000")])

        out, _err = self.run_command(path)

        self.assertIn("1 rules would be created, 0 would be updated, 0 unchanged, 0 without an amount, 0 rows with a problem.", out)

    def test_a_failure_half_way_writes_nothing(self):
        rows = [(str(1000 + index), f"Ship {index}", "1000000") for index in range(5)]
        path = self.write(rows)
        real = AuditLog.objects.create
        calls = []

        def failing(*args, **kwargs):
            calls.append(1)
            if len(calls) == 3:
                raise RuntimeError("stopped")
            return real(*args, **kwargs)

        with mock.patch("fleetops.services.audit.AuditLog.objects.create", side_effect=failing):
            with self.assertRaises(RuntimeError):
                self.run_command(path, "--apply")

        self.assertEqual((SrpPayoutRule.objects.count(), AuditLog.objects.count()), (0, 0))

    def test_a_successful_apply_still_writes_the_rules_and_their_audit_entries(self):
        path = self.write([(str(1000 + index), f"Ship {index}", "1000000") for index in range(3)])

        self.run_command(path, "--apply")

        self.assertEqual(SrpPayoutRule.objects.count(), 3)
        self.assertEqual(AuditLog.objects.filter(action="configuration.create").count(), 3)

    def test_a_file_that_is_not_utf8_is_refused_with_a_clear_message(self):
        # Latin-1 text late in the file, after many good rows: nothing is written on --apply either.
        good = "".join(f"{1000 + index},Ship {index},1000000\n" for index in range(3000))
        raw = ("type_id,name,%s\n" % COLUMN).encode() + good.encode() + "9999,Caf\xe9,1000000\n".encode("latin-1")
        path = self.write(None, raw=raw)

        for arguments in ((), ("--apply",)):
            with self.subTest(arguments=arguments):
                message = self.run_command(path, *arguments, expect_error=True)
                self.assertIn("is not a UTF-8 file", message)
                self.assertIn(os.path.basename(path), message)
                self.assertNotIn(os.path.dirname(path) + os.sep, message)
        self.assertEqual(SrpPayoutRule.objects.count(), 0)

    def test_a_file_with_a_byte_order_mark_still_reads(self):
        path = self.write(None, raw=b"\xef\xbb\xbftype_id,name," + COLUMN.encode() + b"\n1001,Fine,5000000\n")

        out, _err = self.run_command(path)

        self.assertIn("1 rules would be created", out)


# --- L-1: the approvals page asks for a reason before it asks to confirm ---------------------------------------------


class RejectNoteTests(TestCase):
    def setUp(self):
        self.lead = f.create_user("l1_lead", perms=f.FC_LEAD_PERMS)
        self.fc = f.create_user("l1_fc", perms=f.FC_PERMS)
        self.pilot = f.create_user("l1_pilot", perms=f.MEMBER_PERMS)
        start = timezone.now() - timedelta(days=1)
        self.operation = f.create_operation(
            self.fc,
            status=FleetOperation.Status.CLOSED,
            is_manual=True,
            esi_fleet_id=None,
            review_status=ReviewStatus.PENDING,
            started_at=start,
            ended_at=start + timedelta(hours=1),
        )
        f.add_attendance(
            self.operation, self.pilot, source=AttendanceRecord.Source.MANUAL, review_status=ReviewStatus.PENDING,
            created_by=self.fc, granted=False,
        )
        self.client.force_login(self.lead)

    def buttons(self):
        html = self.client.get(reverse("fleetops:approval_queue")).content.decode()
        found = {}
        for attributes, text in re.findall(r"<button([^>]*name=\"action\"[^>]*)>(.*?)</button>", html, flags=re.S):
            found.setdefault(text.strip(), []).append(attributes)
        return html, found

    def test_only_the_reject_buttons_need_the_note(self):
        _html, found = self.buttons()

        for label in ("Reject", "Reject selected"):
            with self.subTest(button=label):
                [attributes] = found[label]
                self.assertIn('data-fo-need="note"', attributes)
                self.assertIn('data-fo-need-message="Give a reason for the rejection."', attributes)
        for label in ("Approve", "Approve selected"):
            with self.subTest(button=label):
                for attributes in found[label]:
                    self.assertNotIn("data-fo-need", attributes)

    def test_the_shared_note_field_is_not_required_so_approving_needs_no_note(self):
        html, _found = self.buttons()

        fields = re.findall(r"<input[^>]*name=\"note\"[^>]*>", html)
        self.assertTrue(fields)
        for field in fields:
            # The placeholder says "required to reject"; the attribute would make Approve ask for a note too.
            self.assertIsNone(re.search(r"\srequired(\s|=|>|/)", field), field)

    def test_the_script_checks_the_note_before_it_asks_to_confirm(self):
        source = (Path(__file__).resolve().parent.parent / "fleetops/static/fleetops/fleetops.js").read_text(encoding="utf-8")

        need = source.index("foNeed")
        ask = source.index("confirmDialog(question")
        self.assertLess(need, ask)
        self.assertIn("event.preventDefault()", source[need - 400 : ask])
        self.assertIn("trim()", source[need - 400 : ask])

    def test_reject_with_an_empty_note_does_not_block_approve_with_an_empty_note(self):
        """Run in a real browser (Playwright with Chromium); skipped where Node, Playwright or Chromium are missing."""
        import json
        import shutil
        import subprocess

        node = shutil.which("node")
        if not node:
            self.skipTest("Node is not installed")
        root = Path(__file__).resolve().parent
        env = dict(os.environ)
        if not env.get("PLAYWRIGHT_MODULE"):
            found = subprocess.run([shutil.which("npm") or "npm", "root", "-g"], capture_output=True, text=True)
            env["PLAYWRIGHT_MODULE"] = str(Path(found.stdout.strip()) / "playwright") if found.returncode == 0 else "playwright"
        if "PLAYWRIGHT_BROWSERS_PATH" not in env and Path("/opt/pw-browsers").is_dir():
            env["PLAYWRIGHT_BROWSERS_PATH"] = "/opt/pw-browsers"
        done = subprocess.run(
            [node, str(root / "js/reject_note_check.js"), str(root.parent / "fleetops/static/fleetops/fleetops.js")],
            capture_output=True, text=True, env=env, timeout=120,
        )
        if done.returncode != 0:
            self.skipTest("No browser available: " + done.stderr.strip()[:200])
        result = json.loads(done.stdout.strip().splitlines()[-1])

        self.assertEqual(result["afterReject"]["sent"], [])
        self.assertFalse(result["afterReject"]["dialog"])
        self.assertEqual(result["afterReject"]["validity"], "")
        self.assertEqual(result["afterApprove"]["sent"], ["approve"])

    def test_the_server_still_refuses_a_rejection_without_a_note(self):
        response = self.client.post(
            reverse("fleetops:review_operation", args=[self.operation.uuid]), {"action": "reject", "note": " "}
        )

        self.operation.refresh_from_db()
        self.assertEqual(self.operation.review_status, ReviewStatus.PENDING)
        self.assertEqual(response.status_code, 302)

    def test_the_message_is_translated(self):
        self.client.cookies[settings.LANGUAGE_COOKIE_NAME] = "zh-hans"
        html = self.client.get(reverse("fleetops:approval_queue")).content.decode()
        self.assertIn('data-fo-need-message="请填写拒绝原因。"', html)


# --- L-2: the member export of an unknown corporation ----------------------------------------------------------------


class MemberExportCorporationTests(TestCase):
    def setUp(self):
        self.lead = f.member("l2_lead", perms=f.FC_LEAD_PERMS, main_name="Lead")
        self.client.force_login(self.lead)

    def get(self, **params):
        return self.client.get(reverse("fleetops:export", args=["members"]), {"format": "csv", **params})

    def test_an_unknown_corporation_is_404_like_its_page(self):
        page = self.client.get(reverse("fleetops:corporation_statistics_detail", args=[9_999_999]))
        file = self.get(corporation=9_999_999)

        self.assertEqual(page.status_code, 404)
        self.assertEqual(file.status_code, 404)

    def test_a_corporation_with_members_is_exported(self):
        response = self.get(corporation=f.DEFAULT_CORP[0])

        self.assertEqual(response.status_code, 200)

    def test_the_alliance_wide_file_does_not_need_a_corporation(self):
        self.assertEqual(self.get().status_code, 200)

    def test_a_corporation_manager_keeps_the_answers_they_had(self):
        manager = f.member("l2_manager", perms=f.CORP_MANAGEMENT_PERMS, main_name="Manager")
        self.client.force_login(manager)

        self.assertEqual(self.get(corporation=f.DEFAULT_CORP[0]).status_code, 200)
        self.assertEqual(self.get(corporation=9_999_999).status_code, 404)


# --- L-8: a second manual PAP of the same player ---------------------------------------------------------------------


class SecondManualPapTests(TestCase):
    def setUp(self):
        self.fc = f.create_user("l8_fc", perms=f.FC_PERMS)
        self.pilot = f.create_user("l8_pilot")
        self.other = f.create_user("l8_other")
        self.operation = f.create_operation(self.fc, status=FleetOperation.Status.CLOSED)
        self.client.force_login(self.fc)
        self.url = reverse("fleetops:add_manual_attendance", args=[self.operation.uuid])

    def payload(self, user=None, **extra):
        data = {
            "character_id": f.main_of(user or self.pilot).character_id,
            "character_name": "",
            "attendance_value": 1,
            "duplicate_action": "keep",
            "notes": "",
        }
        data.update(extra)
        return data

    def rows(self):
        return AttendanceRecord.objects.filter(operation=self.operation, source=AttendanceRecord.Source.MANUAL)

    def test_the_first_entry_saves_without_a_warning(self):
        self.client.post(self.url, self.payload())

        self.assertEqual(self.rows().count(), 1)

    def test_a_second_entry_is_held_back_with_a_warning_until_it_is_sent_again(self):
        self.client.post(self.url, self.payload())

        response = self.client.post(self.url, self.payload(attendance_value=2), follow=True)

        self.assertEqual(self.rows().count(), 1)
        texts = [str(message) for message in response.context["messages"]]
        self.assertTrue(any("already has manual PAP on this fleet" in text for text in texts), texts)
        form = response.context["manual_attendance_form"]
        self.assertEqual(form.initial["confirm_second"], str(f.main_of(self.pilot).character_id))
        self.assertEqual(form.initial["attendance_value"], "2")
        self.assertContains(response, 'name="confirm_second"')
        self.assertContains(response, "alert-warning")

        again = self.client.post(self.url, self.payload(attendance_value=2, confirm_second=form.initial["confirm_second"]))

        self.assertEqual(again.status_code, 302)
        self.assertEqual(sorted(self.rows().values_list("attendance_value", flat=True)), [1, 2])

    def test_the_acknowledgement_is_for_that_character_only(self):
        self.client.post(self.url, self.payload())
        self.assertEqual(self.rows().count(), 1)

        # The acknowledgement of another character does not count.
        self.client.post(self.url, self.payload(confirm_second="123"))

        self.assertEqual(self.rows().count(), 1)

    def test_another_player_is_not_warned(self):
        self.client.post(self.url, self.payload())

        self.client.post(self.url, self.payload(self.other))

        self.assertEqual(self.rows().count(), 2)

    def test_a_rejected_row_does_not_count(self):
        self.client.post(self.url, self.payload())
        self.rows().update(review_status=ReviewStatus.REJECTED)

        self.client.post(self.url, self.payload())

        self.assertEqual(self.rows().count(), 2)

    def test_the_historical_page_warns_and_then_saves(self):
        url = reverse("fleetops:manual_attendance")
        self.client.post(self.url, self.payload())
        data = self.payload(operation=self.operation.pk)

        response = self.client.post(url, data)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.rows().count(), 1)
        self.assertIn("already has manual PAP", " ".join(response.context["form"].non_field_errors()))
        self.assertContains(response, f'name="confirm_second" value="{data["character_id"]}"')

        saved = self.client.post(url, {**data, "confirm_second": str(data["character_id"])})

        self.assertEqual(saved.status_code, 302)
        self.assertEqual(self.rows().count(), 2)

    def test_the_warning_is_translated(self):
        self.client.post(self.url, self.payload())
        self.client.cookies[settings.LANGUAGE_COOKIE_NAME] = "zh-hans"

        response = self.client.post(self.url, self.payload(), follow=True)

        texts = " ".join(str(message) for message in response.context["messages"])
        self.assertNotIn("already has", texts)
        self.assertIn("再保存一次", texts)


# --- L-9: the window for ban rejections without a marker ---------------------------------------------------------------


class BanWindowTests(ClaimBase):
    def setUp(self):
        super().setUp()
        self.settings(member_resubmit_rejected=True)
        claims._marker_upgrade.clear()
        self.addCleanup(claims._marker_upgrade.clear)

    def unmarked_rejection(self, *, minutes_after_ban=1):
        claim = self.submit(self.killmail())
        ban, _result = bans.ban_user(self.user, actor=self.manager, days=7, reason="Abuse")
        review.reject_claim(claim, actor=self.manager, reason="Banned")
        event = claim.events.get(action=Action.REJECT)
        SrpClaimEvent.objects.filter(pk=event.pk).update(created_at=ban.created_at + timedelta(minutes=minutes_after_ban))
        claim.refresh_from_db()
        return claim

    def upgraded_at(self, when):
        patcher = mock.patch.object(claims, "marker_upgrade_time", return_value=when)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_rejection_from_before_the_upgrade_is_found_by_the_window(self):
        claim = self.unmarked_rejection()
        self.upgraded_at(timezone.now() + timedelta(hours=1))

        self.assertTrue(claims.rejected_by_ban(claim))

    def test_an_unmarked_rejection_after_the_upgrade_is_an_ordinary_rejection(self):
        # The reviewer banned the member and rejected a claim within five minutes, after the upgrade: the ban dialog
        # would have set the marker, so this was a separate rejection and the member may submit again.
        claim = self.unmarked_rejection()
        self.upgraded_at(timezone.now() - timedelta(days=1))

        self.assertFalse(claims.rejected_by_ban(claim))
        self.assertTrue(claims.can_resubmit(claim, SrpSettings.get_solo()))

    def test_without_a_readable_upgrade_time_the_window_does_not_apply(self):
        claim = self.unmarked_rejection()
        self.upgraded_at(None)

        self.assertFalse(claims.rejected_by_ban(claim))

    def test_a_marked_rejection_counts_whatever_the_upgrade_time_is(self):
        pending = self.submit(self.killmail())
        bans.ban_user(self.user, actor=self.manager, days=1, reason="Abuse", reject_pending=True, reject_reason="Banned")
        pending.refresh_from_db()
        self.upgraded_at(None)

        self.assertTrue(claims.rejected_by_ban(pending))

    def test_the_upgrade_time_is_read_from_the_applied_migrations(self):
        from django.db.migrations.recorder import MigrationRecorder

        applied = (
            MigrationRecorder.Migration.objects.filter(app="fleetops", name=claims.MARKER_MIGRATION)
            .values_list("applied", flat=True)
            .get()
        )

        self.assertEqual(claims.marker_upgrade_time(), applied)
        # Read once, then kept.
        with mock.patch("django.db.migrations.recorder.MigrationRecorder.Migration.objects") as objects:
            self.assertEqual(claims.marker_upgrade_time(), applied)
            objects.filter.assert_not_called()

    def test_an_unreadable_migration_table_gives_none(self):
        with mock.patch("django.db.migrations.recorder.MigrationRecorder.Migration.objects") as objects:
            objects.filter.side_effect = RuntimeError("no table")
            self.assertIsNone(claims.marker_upgrade_time())


# --- L-7: continuous payment batch numbers ---------------------------------------------------------------------------


class BatchNumberTests(PaymentsBase):
    def test_batches_are_numbered_one_after_the_other(self):
        self.pay([self.approved()])
        self.pay([self.approved()])

        self.assertEqual(
            list(SrpPaymentBatch.objects.order_by("number").values_list("number", flat=True)), [1, 2]
        )
        self.assertEqual(SrpPaymentBatch.objects.get(number=2).reference, "PAY-2")

    def test_a_payment_where_everything_is_skipped_takes_no_number(self):
        self.pay([self.approved()])
        own = self.approved(self.payer)
        self.pay([own])
        self.pay([self.approved()])

        self.assertEqual(
            list(SrpPaymentBatch.objects.order_by("number").values_list("number", flat=True)), [1, 2]
        )

    def test_the_next_number_is_the_highest_plus_one(self):
        old = SrpPaymentBatch.objects.create(claim_count=1, number=41)
        self.assertEqual(old.reference, "PAY-41")

        self.pay([self.approved()])

        self.assertEqual(SrpPaymentBatch.objects.exclude(pk=old.pk).get().number, 42)

    def test_a_batch_is_numbered_once(self):
        claim = self.approved()
        result = self.pay([claim])
        batch = result.batch

        self.assertEqual(payments.assign_number(batch), 1)
        batch.refresh_from_db()
        self.assertEqual(payments.assign_number(batch), batch.number)
        self.assertEqual(SrpPaymentBatch.objects.count(), 1)

    def test_a_clash_on_the_number_tries_again_with_the_new_maximum(self):
        first = SrpPaymentBatch.objects.create(claim_count=1)
        second = SrpPaymentBatch.objects.create(claim_count=1)
        real = SrpPaymentBatch.objects.aggregate
        reads = []

        def stale_read(*args, **kwargs):
            result = real(*args, **kwargs)
            reads.append(result)
            if len(reads) == 1:
                # Another payment takes the number 1 right after this one read the maximum (nothing yet).
                SrpPaymentBatch.objects.filter(pk=first.pk).update(number=1)
            return result

        with mock.patch.object(SrpPaymentBatch.objects, "aggregate", side_effect=stale_read):
            number = payments.assign_number(second)

        self.assertEqual(len(reads), 2)
        self.assertEqual(number, 2)
        self.assertEqual(sorted(SrpPaymentBatch.objects.values_list("number", flat=True)), [1, 2])

    def test_the_number_is_unique(self):
        SrpPaymentBatch.objects.create(number=5)

        with self.assertRaises(IntegrityError), transaction.atomic():
            SrpPaymentBatch.objects.create(number=5)

    def test_the_reference_uses_the_number_not_the_key(self):
        batch = SrpPaymentBatch.objects.create(number=7)

        self.assertEqual(batch.reference, "PAY-7")
        self.assertEqual(SrpPaymentBatch().reference, "PAY")
        self.assertEqual(SrpPaymentBatch.objects.create().reference, "PAY")

    def test_the_audit_entry_the_events_and_the_notice_name_the_number(self):
        SrpPaymentBatch.objects.create(claim_count=1, number=30)
        claim = self.approved()

        result = self.pay([claim])

        batch = result.batch
        self.assertEqual(batch.number, 31)
        entry = AuditLog.objects.get(action="srp.payment_batch")
        self.assertEqual((entry.object_type, entry.object_id), ("SrpPaymentBatch", "31"))
        self.assertEqual(SrpClaimEvent.objects.get(claim=claim, action=Action.PAID).data, {"batch": 31})
        review.undo_payment(claim, actor=self.payer, reason="Not sent")
        self.assertEqual(claim.events.get(action=Action.UNPAID).data, {"batch": 31})

    def test_pages_and_exports_show_the_number(self):
        # A number far from any primary key a test run can reach, so the number never equals the key.
        SrpPaymentBatch.objects.create(claim_count=1, number=10_000_030)
        claim = self.approved()
        batch = self.pay([claim]).batch
        self.client.force_login(self.payer)

        page = self.client.get(reverse("fleetops:srp_payment_batch", args=[batch.uuid]))
        payments_page = self.client.get(reverse("fleetops:srp_payments"))

        self.assertEqual(batch.number, 10_000_031)
        self.assertContains(page, "Payment batch PAY-10000031")
        self.assertContains(payments_page, ">PAY-10000031<")
        self.assertNotContains(payments_page, f">PAY-{batch.pk}<")

    def test_the_claim_export_names_the_batch_by_number(self):
        SrpPaymentBatch.objects.create(claim_count=1, number=30)
        claim = self.approved()
        self.pay([claim])
        manager = f.create_user("b7_numbers_manager", perms=f.SRP_MANAGER_PERMS)
        self.client.force_login(manager)

        response = self.client.get(reverse("fleetops:export", args=["srp-claims"]), {"format": "csv", "preset": "all"})

        self.assertEqual(response.status_code, 200)
        self.assertIn("PAY-31", [cell for row in csv_rows(response) for cell in row])

    def test_the_admin_finds_a_batch_by_the_reference_or_the_number_exactly(self):
        from django.contrib import admin as django_admin

        from fleetops.admin import SrpPaymentBatchAdmin

        wanted = SrpPaymentBatch.objects.create(claim_count=1, number=117, created_by_name="Payer")
        SrpPaymentBatch.objects.create(claim_count=1, number=1117)
        SrpPaymentBatch.objects.create(claim_count=1, number=2117)
        model_admin = SrpPaymentBatchAdmin(SrpPaymentBatch, django_admin.site)
        queryset = SrpPaymentBatch.objects.all()

        for term in ("PAY-117", "pay-117", "117", " 117 "):
            with self.subTest(term=term):
                found, _distinct = model_admin.get_search_results(None, queryset, term)
                self.assertEqual(list(found), [wanted])
        by_name, _distinct = model_admin.get_search_results(None, queryset, "Payer")
        self.assertEqual(list(by_name), [wanted])

    def cut_batch(self, claims_paid, *, counted=False):
        """A payment cut off after its claims were paid: the claims point to the batch, which has no number."""
        batch = payments.create_batch(self.payer, "")
        for claim in claims_paid:
            review._mark_paid(claim, self.payer, "", batch=batch)
        if counted:
            SrpPaymentBatch.objects.filter(pk=batch.pk).update(claim_count=len(claims_paid))
        SrpPaymentBatch.objects.filter(pk=batch.pk).update(created_at=timezone.now() - timedelta(hours=2))
        return SrpPaymentBatch.objects.get(pk=batch.pk)

    def test_a_cut_batch_is_numbered_by_the_repair_and_its_events_are_named(self):
        claim = self.approved()
        batch = self.cut_batch([claim])
        self.assertEqual(batch.reference, "PAY")
        self.assertEqual(SrpClaimEvent.objects.get(claim=claim, action=Action.PAID).data, {"batch_uuid": str(batch.uuid)})

        self.assertEqual(payments.repair_cut_batches(), 1)

        batch.refresh_from_db()
        self.assertEqual((batch.number, batch.claim_count), (1, 1))
        self.assertEqual(SrpClaimEvent.objects.get(claim=claim, action=Action.PAID).data, {"batch": 1})

    def test_a_batch_with_counts_but_no_number_is_repaired_too(self):
        claim = self.approved()
        batch = self.cut_batch([claim], counted=True)
        self.assertEqual((batch.claim_count, batch.number), (1, None))

        self.assertEqual(payments.repair_cut_batches(), 1)

        batch.refresh_from_db()
        self.assertEqual(batch.number, 1)
        self.assertEqual(SrpClaimEvent.objects.get(claim=claim, action=Action.PAID).data, {"batch": 1})

    def test_finishing_writes_the_counts_and_the_number_in_one_statement(self):
        claim = self.approved()
        batch = payments.create_batch(self.payer, "")
        review._mark_paid(claim, self.payer, "", batch=batch)

        with mock.patch.object(SrpPaymentBatch, "save", side_effect=AssertionError("save must not be used")):
            payments.finish_batch(batch, [SrpClaim.objects.get(pk=claim.pk)], "")

        stored = SrpPaymentBatch.objects.get(pk=batch.pk)
        self.assertEqual((stored.number, stored.claim_count, stored.payee_count), (1, 1, 1))

    def test_undoing_a_payment_of_an_unnumbered_batch_is_named_when_the_batch_is_repaired(self):
        kept, undone = self.approved(), self.approved()
        batch = self.cut_batch([kept, undone])

        review.undo_payment(undone, actor=self.payer, reason="Not sent")
        self.assertEqual(
            undone.events.get(action=Action.UNPAID).data, {"batch_uuid": str(batch.uuid)}
        )
        payments.repair_cut_batches()

        batch.refresh_from_db()
        self.assertEqual((batch.number, batch.claim_count), (1, 1))
        self.assertEqual(undone.events.get(action=Action.PAID).data, {"batch": 1})
        self.assertEqual(undone.events.get(action=Action.UNPAID).data, {"batch": 1})
        self.assertEqual(SrpClaimEvent.objects.get(claim=kept, action=Action.PAID).data, {"batch": 1})

    def test_the_events_of_a_batch_numbered_but_not_named_are_named_by_the_repair(self):
        claim = self.approved()
        batch = self.cut_batch([claim])
        payments.assign_number(batch)

        payments.repair_cut_batches()

        self.assertEqual(SrpClaimEvent.objects.get(claim=claim, action=Action.PAID).data, {"batch": 1})

    def test_the_audit_action_has_a_label(self):
        self.assertIn("srp.payment_batch", AUDIT_ACTION_LABELS)
