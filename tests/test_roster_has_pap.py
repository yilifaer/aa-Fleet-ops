# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Pasted rosters and characters that have PAP already: only a correction of tracked PAP can be imported, the preview
note is shown in the language of the viewer, and the server enforces the rules also for a replayed preview."""

from unittest import mock

from django.test import TestCase, tag
from django.urls import reverse
from django.utils import translation

from fleetops.models import AttendanceRecord, FleetOperation
from fleetops.providers import esi as esi_provider
from fleetops.services.roster_import import build_import_preview, import_rows, parse_roster, sign_preview

from . import factories as f

Status = FleetOperation.Status
MANUAL = AttendanceRecord.Source.MANUAL
IMPORTED = AttendanceRecord.Source.IMPORTED
KEEP_MESSAGE = "Already has PAP in this fleet. Choose “Replace tracked PAP” or “Merge into tracked PAP” to correct it."
MANUAL_MESSAGE = "Already has a manual PAP row in this fleet."


class HasPapTestCase(TestCase):
    def setUp(self):
        # A page rendered in Chinese leaves that language active for the next test.
        self.addCleanup(translation.activate, "en")
        patcher = mock.patch.object(esi_provider, "universe_ids", lambda names: {})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.fc = f.create_user("fc", perms=f.FC_PERMS, main_name="Example FC")
        self.lead = f.create_user("lead", perms=f.FC_LEAD_PERMS, main_name="Example Lead")
        self.tracked_user = f.create_user("tracked", perms=f.MEMBER_PERMS, main_name="Pilot A")
        self.manual_user = f.create_user("manual", perms=f.MEMBER_PERMS, main_name="Pilot B")
        self.new_user = f.create_user("new", perms=f.MEMBER_PERMS, main_name="Pilot C")
        self.operation = f.create_operation(self.fc, status=Status.CLOSED)
        self.tracked_row = f.add_attendance(self.operation, self.tracked_user)
        f.member_state(self.operation, f.main_of(self.tracked_user), accumulated_seconds=3600, active=False)
        self.manual_row = f.add_attendance(self.operation, self.manual_user, source=MANUAL, created_by=self.fc)
        self.text = "Pilot A\nPilot B\nPilot C\n"

    def preview(self, action, text=None):
        lines, _warnings = parse_roster(text or self.text)
        rows = build_import_preview(self.operation, lines, actor=self.fc, duplicate_action=action)
        return {row.line.name: row for row in rows}

    def url(self):
        return reverse("fleetops:roster_import", args=[self.operation.uuid])

    def post_parse(self, action, user=None, notes=""):
        self.client.force_login(user or self.fc)
        return self.client.post(
            self.url(),
            {"step": "parse", "text": self.text, "attendance_value": "1", "duplicate_action": action, "notes": notes},
        )


class PreviewTests(HasPapTestCase):
    def test_keep_lists_the_rows_that_have_pap_without_a_way_to_import_them(self):
        rows = self.preview("keep")

        self.assertEqual((rows["Pilot A"].status, rows["Pilot A"].importable, rows["Pilot A"].checked), ("has_pap", False, False))
        self.assertEqual(rows["Pilot A"].reason, KEEP_MESSAGE)
        self.assertEqual((rows["Pilot B"].status, rows["Pilot B"].importable), ("has_pap", False))
        self.assertEqual(rows["Pilot B"].reason, MANUAL_MESSAGE)
        self.assertEqual((rows["Pilot C"].status, rows["Pilot C"].importable, rows["Pilot C"].checked), ("add", True, True))

    def test_replace_and_merge_make_only_tracked_pap_importable_and_never_tick_it(self):
        for action in ("replace", "merge"):
            with self.subTest(action=action):
                rows = self.preview(action)

                self.assertEqual((rows["Pilot A"].importable, rows["Pilot A"].checked), (True, False))
                self.assertEqual(rows["Pilot A"].reason, "Already has PAP in this fleet")
                # A manual row is not tracked PAP: there is nothing to replace or to merge into.
                self.assertEqual((rows["Pilot B"].importable, rows["Pilot B"].reason), (False, MANUAL_MESSAGE))

    def test_pap_of_a_running_fleet_counts_as_tracked(self):
        running = f.create_operation(self.fc, status=Status.ACTIVE)
        f.member_state(running, f.main_of(self.tracked_user))
        lines, _ = parse_roster("Pilot A\n")

        row = build_import_preview(running, lines, actor=self.fc, duplicate_action="replace")[0]

        self.assertEqual((row.status, row.importable), ("has_pap", True))

    def test_the_page_disables_the_checkbox_of_a_row_that_cannot_be_imported(self):
        page = self.post_parse("keep")

        self.assertContains(page, 'disabled aria-label="Cannot import Pilot A"')
        self.assertContains(page, KEEP_MESSAGE)
        self.assertContains(page, MANUAL_MESSAGE)
        self.assertNotContains(page, 'aria-label="Import Pilot A"')
        self.assertContains(page, 'aria-label="Import Pilot C"')

    def test_the_page_offers_a_correction_with_replace(self):
        page = self.post_parse("replace")

        self.assertContains(page, 'aria-label="Import Pilot A"')
        self.assertContains(page, 'aria-label="Cannot import Pilot B"')

    def test_the_pills_say_what_each_row_is(self):
        page = self.post_parse("keep")

        self.assertContains(page, '<span class="fo-pill fo-pill-neutral">Has PAP</span>')
        self.assertContains(page, '<span class="fo-pill fo-pill-ok">Add</span>')
        self.assertNotContains(page, "badge")


class ServerRuleTests(HasPapTestCase):
    def run_import(self, action, status="has_pap", user=None):
        row = lambda owner, name: {"character_id": f.main_of(owner).character_id, "character_name": name, "status": status}  # noqa: E731
        rows = [row(self.tracked_user, "Pilot A"), row(self.manual_user, "Pilot B")]
        return import_rows(
            self.operation, rows, actor=user or self.fc, attendance_value=1, duplicate_action=action, notes="Ops"
        )

    def test_keep_imports_neither_and_there_is_still_one_pap_each(self):
        result = self.run_import("keep")

        self.assertEqual(result.created, [])
        self.assertEqual([item["reason"] for item in result.skipped], [KEEP_MESSAGE, MANUAL_MESSAGE])
        self.assertEqual(AttendanceRecord.objects.filter(source=IMPORTED).count(), 0)
        self.assertEqual(AttendanceRecord.objects.filter(operation=self.operation).count(), 3 - 1)

    def test_replace_corrects_the_tracked_pap_and_skips_the_manual_row(self):
        # An FC lead's rows are approved at once, so the correction applies.
        result = self.run_import("replace", user=self.lead)

        self.assertEqual([record.character_id for record in result.created], [f.main_of(self.tracked_user).character_id])
        self.assertEqual(result.created[0].correction, "replace")
        self.assertEqual([item["reason"] for item in result.skipped], [MANUAL_MESSAGE])
        self.tracked_row.refresh_from_db()
        self.assertTrue(self.tracked_row.replaced)

    def test_a_replayed_preview_cannot_be_used_with_another_action(self):
        # Signed for "replace", imported as "keep": the import uses what the signed payload says, and an import called
        # with other settings checks the rules again.
        lines, _ = parse_roster(self.text)
        rows = build_import_preview(self.operation, lines, actor=self.fc, duplicate_action="replace")
        token = sign_preview(
            self.operation, rows, actor=self.fc, attendance_value=1, duplicate_action="replace", notes="Ops"
        )
        self.client.force_login(self.fc)

        response = self.client.post(self.url(), {"step": "import", "payload": token, "rows": ["0", "1", "2"]})

        self.assertEqual(response.status_code, 302)
        imported = AttendanceRecord.objects.filter(source=IMPORTED)
        self.assertEqual(
            sorted(imported.values_list("character_id", flat=True)),
            sorted([f.main_of(self.tracked_user).character_id, f.main_of(self.new_user).character_id]),
        )
        self.assertFalse(imported.filter(character_id=f.main_of(self.manual_user).character_id).exists())

    def test_a_character_that_got_pap_after_the_preview_is_not_credited_twice(self):
        row = {"character_id": f.main_of(self.new_user).character_id, "character_name": "Pilot C", "status": "add"}
        f.add_attendance(self.operation, self.new_user)

        result = import_rows(self.operation, [row], actor=self.fc, attendance_value=1, duplicate_action="keep", notes="")

        self.assertEqual(result.created, [])
        self.assertEqual(result.skipped[0]["reason"], KEEP_MESSAGE)


class NoteTests(HasPapTestCase):
    def test_the_note_is_shown_as_the_importer_typed_it(self):
        self.client.force_login(self.fc)

        page = self.client.post(
            self.url(),
            {"step": "parse", "text": self.text, "attendance_value": "1", "duplicate_action": "keep", "notes": "Ops night"},
        )

        self.assertContains(page, "Notes: Ops night")

    def test_the_offered_note_is_a_message_of_the_system(self):
        page = self.post_parse("keep", notes="Imported from fleet roster paste")

        self.assertContains(page, "Notes: Imported from fleet roster paste")

    def test_the_offered_note_is_translated_in_the_preview(self):
        self.client.cookies["django_language"] = "zh-hans"

        # The form offers the note in Chinese and sends it back as it was offered.
        page = self.post_parse("keep", notes="由粘贴的舰队名单导入")

        self.assertContains(page, "由粘贴的舰队名单导入")
        self.assertNotContains(page, "Imported from fleet roster paste")

    @tag("catalog")
    def test_the_reasons_are_translated(self):
        self.client.cookies["django_language"] = "zh-hans"

        page = self.post_parse("keep")

        self.assertNotContains(page, KEEP_MESSAGE)
        with translation.override("zh-hans"):
            from fleetops.texts import translate_stored

            self.assertNotEqual(translate_stored(KEEP_MESSAGE), KEEP_MESSAGE)
