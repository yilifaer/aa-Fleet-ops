"""Manual PAP from a pasted roster and manual PAP by character name."""

from types import SimpleNamespace
from unittest import mock

from django.contrib.messages import get_messages
from django.core import signing
from django.test import TestCase
from django.urls import reverse

from fleetops.forms import HistoricalManualAttendanceForm, ManualAttendanceForm
from fleetops.models import AttendanceRecord, AuditLog, FleetOperation, ReviewStatus
from fleetops.providers import esi as esi_provider
from fleetops.providers.esi import FleetESIError
from fleetops.services import roster_import
from fleetops.services.roster_import import (
    build_import_preview,
    import_rows,
    load_preview,
    parse_roster,
    resolve_character_name,
    resolve_names,
    sign_preview,
)

from . import factories as f
from .test_roster_paste import CHINESE_FLEET, ENGLISH_FLEET

Status = FleetOperation.Status
IMPORTED = AttendanceRecord.Source.IMPORTED
MANUAL = AttendanceRecord.Source.MANUAL
ESI_DOWN = FleetESIError("IDS_ERROR", "Could not look up character names: ESI returned HTTP 503 (Service unavailable).", 503)
# The provider's name lookup; the tests replace it with FakeESINames.
UNIVERSE_IDS = esi_provider.universe_ids


def message_texts(response):
    return [str(message) for message in get_messages(response.wsgi_request)]


class FakeESINames:
    """``universe_ids`` answering from a fixed list of EVE characters, and recording its calls."""

    def __init__(self):
        self.characters = {}
        self.calls = []
        self.failure = None

    def add(self, name, character_id=None):
        character_id = character_id or f.next_id()
        self.characters[name.casefold()] = (character_id, name)
        return character_id

    def __call__(self, names):
        self.calls.append(list(names))
        if self.failure is not None:
            raise self.failure
        return {name.casefold(): self.characters[name.casefold()] for name in names if name.casefold() in self.characters}


class RosterTestCase(TestCase):
    def setUp(self):
        self.esi = FakeESINames()
        patcher = mock.patch.object(esi_provider, "universe_ids", self.esi)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.fc = f.create_user("fc", perms=f.FC_PERMS, main_name="yilifaerrrrrrr")
        self.lead = f.create_user("lead", perms=f.FC_LEAD_PERMS)
        self.sally = f.create_user("sally", perms=f.MEMBER_PERMS, main_name="Sally en Chalune")
        self.operation = f.create_operation(self.fc, status=Status.CLOSED)

    def preview(self, text, operation=None):
        lines, _warnings = parse_roster(text)
        return build_import_preview(operation or self.operation, lines, actor=self.fc)

    def by_name(self, rows):
        return {row.line.name: row for row in rows}


class ResolveNamesTests(RosterTestCase):
    def test_alliance_auth_first_case_insensitive(self):
        result = resolve_names(["sally EN chalune", "  Sally en   Chalune "])

        self.assertEqual(list(result), ["sally en chalune"])
        found = result["sally en chalune"]
        main = f.main_of(self.sally)
        self.assertEqual(
            (found.status, found.character_id, found.name, found.user_id, found.main_character_name),
            ("auth", main.character_id, "Sally en Chalune", self.sally.pk, "Sally en Chalune"),
        )
        self.assertEqual(self.esi.calls, [])

    def test_known_character_without_owner_needs_no_esi(self):
        neutral = f.create_character("Neutral Pilot")

        result = resolve_names(["neutral pilot"])

        self.assertEqual((result["neutral pilot"].status, result["neutral pilot"].character_id), ("not_in_auth", neutral.character_id))
        self.assertEqual(self.esi.calls, [])

    def test_owned_copy_of_a_name_wins(self):
        f.create_character("Twin Name")
        owned = f.add_alt(self.sally, "Twin Name")

        self.assertEqual(resolve_names(["Twin Name"])["twin name"].character_id, owned.character_id)

    def test_esi_for_the_rest(self):
        stranger_id = self.esi.add("Stream Sarum")
        # A character Alliance Auth knows under its old name is found by its ID.
        renamed = f.add_alt(self.sally, "Old Name")
        self.esi.add("New Name", renamed.character_id)

        result = resolve_names(["Sally en Chalune", "stream sarum", "New Name", "Nobody Here"])

        self.assertEqual(self.esi.calls, [["stream sarum", "New Name", "Nobody Here"]])
        self.assertEqual((result["stream sarum"].status, result["stream sarum"].character_id), ("not_in_auth", stranger_id))
        self.assertEqual(result["stream sarum"].name, "Stream Sarum")
        self.assertEqual((result["new name"].status, result["new name"].user_id), ("auth", self.sally.pk))
        self.assertEqual(result["nobody here"].status, "not_found")

    def test_esi_failure_keeps_the_alliance_auth_matches(self):
        self.esi.failure = ESI_DOWN

        result = resolve_names(["Sally en Chalune", "Stream Sarum"])

        self.assertEqual(result["sally en chalune"].status, "auth")
        self.assertEqual(
            (result["stream sarum"].status, result["stream sarum"].error),
            ("error", "ESI returned HTTP 503 (Service unavailable)."),
        )

    def test_names_that_cannot_exist_are_not_sent(self):
        result = resolve_names(["x" * 38, "<script>", "", "Real Name"])

        self.assertEqual(self.esi.calls, [["Real Name"]])
        self.assertEqual(list(result), ["real name"])
        self.assertEqual(resolve_names([]), {})

    def test_large_pastes_are_looked_up_in_chunks_of_five_hundred(self):
        calls = []

        class Operation:
            def __init__(self, body):
                self.body = body

            def result(self, **kwargs):
                calls.append(len(self.body))
                return {"characters": [{"id": 2_119_000_001, "name": "Pilot 1199"}] if "Pilot 1199" in self.body else []}

        universe = SimpleNamespace(PostUniverseIds=lambda body: Operation(body))
        names = [f"Pilot {number}" for number in range(1200)]
        with mock.patch.object(esi_provider, "universe_ids", UNIVERSE_IDS), \
                mock.patch.object(esi_provider, "esi", SimpleNamespace(client=SimpleNamespace(Universe=universe))):
            result = resolve_names(names)

        self.assertEqual(calls, [500, 500, 200])
        self.assertEqual(result["pilot 1199"].character_id, 2_119_000_001)
        self.assertEqual(sum(1 for found in result.values() if found.status == "not_found"), 1199)

    def test_single_name(self):
        self.assertEqual(resolve_character_name(" sally en chalune ").status, "auth")
        self.assertEqual(resolve_character_name("Nobody").status, "not_found")


class PreviewTests(RosterTestCase):
    def test_real_chinese_fleet_window(self):
        # The FC row ("yilifaerrrrrrr", fleet commander) ends with an empty position column.
        f.add_attendance(self.operation, self.fc)
        self.esi.add("Stream Sarum")

        rows = self.preview(CHINESE_FLEET)

        self.assertEqual([row.line.name for row in rows], ["Sally en Chalune", "yilifaerrrrrrr", "Stream Sarum"])
        sally, boss, stranger = rows
        self.assertEqual((sally.status, sally.checked, sally.user_id), ("add", True, self.sally.pk))
        self.assertEqual((sally.line.ship_type, sally.line.location, sally.line.role), ("Ragnarok", "HY-RWO", "中队队员"))
        self.assertEqual((boss.status, boss.checked, boss.reason), ("has_pap", False, "Already has PAP in this fleet"))
        self.assertEqual(boss.line.role, "舰队指挥官（统帅）")
        self.assertEqual((stranger.status, stranger.checked, stranger.reason), ("not_in_auth", False, "Not in Alliance Auth"))
        self.assertTrue(sally.importable and boss.importable)
        self.assertFalse(stranger.importable)

    def test_english_fleet_window(self):
        alpha = f.add_alt(self.sally, "Alpha Pilot")
        f.create_character("Bravo Pilot")

        rows = self.by_name(self.preview(ENGLISH_FLEET))

        self.assertEqual(list(rows), ["Alpha Pilot", "Bravo Pilot", "Charlie Pilot"])
        self.assertEqual((rows["Alpha Pilot"].status, rows["Alpha Pilot"].character_id), ("add", alpha.character_id))
        self.assertEqual(rows["Alpha Pilot"].main_character_name, "Sally en Chalune")
        self.assertEqual(rows["Alpha Pilot"].line.role, "Fleet Commander (Boss)")
        self.assertEqual(rows["Alpha Pilot"].line.location, "Jita IV - Moon 4 - Caldari Navy Assembly Plant")
        self.assertEqual(rows["Bravo Pilot"].status, "not_in_auth")
        self.assertEqual((rows["Charlie Pilot"].status, rows["Charlie Pilot"].reason), ("not_found", "No EVE character with this name"))
        self.assertEqual(rows["Charlie Pilot"].line.position, "Wing 1 / Squad 2")

    def test_chat_member_list_and_chat_lines(self):
        f.add_alt(self.sally, "Stream Sarum")

        names = self.preview("Sally en Chalune\nStream Sarum\nsally en chalune\n")
        chat = self.preview("[12:18:36] Sally en Chalune > o7\n[12:18:40] Stream Sarum > x up\n")

        for rows in (names, chat):
            self.assertEqual([(row.line.name, row.status) for row in rows], [("Sally en Chalune", "add"), ("Stream Sarum", "add")])

    def test_invalid_names_and_esi_errors(self):
        self.esi.failure = ESI_DOWN

        rows = self.by_name(self.preview(f"Sally en Chalune\n{'x' * 38}\nStream Sarum\n"))

        self.assertEqual(rows["Sally en Chalune"].status, "add")
        self.assertEqual(rows["x" * 38].status, "invalid")
        self.assertEqual(rows["x" * 38].reason, "Character names are at most 37 characters long.")
        self.assertEqual((rows["Stream Sarum"].status, rows["Stream Sarum"].reason), ("error", "Could not be checked (ESI error)"))

    def test_existing_pap(self):
        pilots = {name: f.create_user(perms=f.MEMBER_PERMS, main_name=name) for name in ("Granted", "Waiting", "Rejected", "Not Granted")}
        f.add_attendance(self.operation, pilots["Granted"])
        f.add_attendance(self.operation, pilots["Waiting"], source=MANUAL, granted=False, review_status=ReviewStatus.PENDING)
        f.add_attendance(self.operation, pilots["Rejected"], source=MANUAL, granted=False, review_status=ReviewStatus.REJECTED)
        f.add_attendance(self.operation, pilots["Not Granted"], granted=False, below_minimum=True)

        rows = self.by_name(self.preview("Granted\nWaiting\nRejected\nNot Granted\n"))

        self.assertEqual(
            {name: row.status for name, row in rows.items()},
            {"Granted": "has_pap", "Waiting": "has_pap", "Rejected": "add", "Not Granted": "add"},
        )

    def test_tracked_members_and_link_registrations_of_open_fleets_have_pap(self):
        active = f.create_operation(self.fc)
        tracked = f.add_alt(self.sally, "Tracked Alt")
        linked = f.add_alt(self.sally, "Linked Alt")
        f.member_state(active, tracked, accumulated_seconds=60)
        f.registration(active, linked)

        rows = self.by_name(self.preview("Tracked Alt\nLinked Alt\nSally en Chalune\n", operation=active))

        self.assertEqual([row.status for row in rows.values()], ["has_pap", "has_pap", "add"])
        # A closed fleet's PAP rows decide; a tracked member without granted PAP can be added.
        f.member_state(self.operation, tracked)
        self.assertEqual(self.by_name(self.preview("Tracked Alt\n"))["Tracked Alt"].status, "add")


class CharacterLimitPreviewTests(RosterTestCase):
    def setUp(self):
        super().setUp()
        self.first_alt = f.add_alt(self.sally, "Sally Alt One")
        self.second_alt = f.add_alt(self.sally, "Sally Alt Two")

    def statuses(self, text, operation=None):
        return {row.line.name: row.status for row in self.preview(text, operation)}

    def test_no_limit(self):
        self.assertEqual(
            set(self.statuses("Sally Alt One\nSally en Chalune\nSally Alt Two\n").values()), {"add"}
        )

    def test_main_comes_before_paste_order(self):
        FleetOperation.objects.filter(pk=self.operation.pk).update(pap_character_limit=2)
        self.operation.refresh_from_db()

        rows = self.by_name(self.preview("Sally Alt One\nSally Alt Two\nSally en Chalune\n"))

        self.assertEqual(
            {name: (row.status, row.checked) for name, row in rows.items()},
            {
                "Sally Alt One": ("add", True),
                "Sally Alt Two": ("capped", False),
                "Sally en Chalune": ("add", True),
            },
        )
        self.assertEqual(
            rows["Sally Alt Two"].reason,
            "Over the limit of 2 PAP characters per player (Sally en Chalune, Sally Alt One first)",
        )
        self.assertTrue(rows["Sally Alt Two"].importable)

    def test_fc_character_comes_first(self):
        fc_alt = f.add_alt(self.fc, "FC Boss Alt")
        operation = f.create_operation(self.fc, status=Status.CLOSED, fc_character=fc_alt, pap_character_limit=1)

        statuses = self.statuses("yilifaerrrrrrr\nFC Boss Alt\n", operation)

        self.assertEqual(statuses, {"yilifaerrrrrrr": "capped", "FC Boss Alt": "add"})

    def test_characters_with_pap_keep_their_places(self):
        operation = f.create_operation(self.fc, status=Status.CLOSED, pap_character_limit=1)
        f.add_attendance(operation, self.sally, self.second_alt)

        rows = self.by_name(self.preview("Sally en Chalune\nSally Alt One\n", operation))

        self.assertEqual([row.status for row in rows.values()], ["capped", "capped"])
        self.assertEqual(rows["Sally en Chalune"].reason, "Over the limit of 1 PAP character per player (Sally Alt Two first)")

    def test_limit_counts_characters_per_player(self):
        f.create_user(perms=f.MEMBER_PERMS, main_name="Other Main")
        operation = f.create_operation(self.fc, status=Status.CLOSED, pap_character_limit=1)

        statuses = self.statuses("Sally Alt One\nOther Main\nSally en Chalune\n", operation)

        self.assertEqual(statuses, {"Sally Alt One": "capped", "Other Main": "add", "Sally en Chalune": "add"})


class PreviewPayloadTests(RosterTestCase):
    def rows(self):
        f.add_attendance(self.operation, self.fc)
        return self.preview(CHINESE_FLEET)

    def sign(self, rows, operation=None, actor=None):
        return sign_preview(
            operation or self.operation, rows, actor=actor or self.fc, attendance_value=2, duplicate_action="merge", notes="Ops"
        )

    def test_round_trip_keeps_importable_rows_and_settings(self):
        data = load_preview(self.operation, self.sign(self.rows()), actor=self.fc)

        main = f.main_of(self.sally)
        self.assertEqual(
            data["rows"],
            [
                {"character_id": main.character_id, "character_name": "Sally en Chalune", "status": "add"},
                {"character_id": f.main_of(self.fc).character_id, "character_name": "yilifaerrrrrrr", "status": "has_pap"},
            ],
        )
        self.assertEqual((data["attendance_value"], data["duplicate_action"], data["notes"]), (2, "merge", "Ops"))

    def test_changed_foreign_or_old_previews_are_refused(self):
        token = self.sign(self.rows())
        other_fleet = f.create_operation(self.fc, status=Status.CLOSED)
        tampered = token[:-3] + ("AAA" if token[-3:] != "AAA" else "BBB")
        forged = signing.dumps({"operation": str(self.operation.uuid), "actor": self.fc.pk, "rows": []}, salt="other")

        for operation, value, actor in (
            (self.operation, tampered, self.fc),
            (self.operation, forged, self.fc),
            (self.operation, "", self.fc),
            (other_fleet, token, self.fc),
            (self.operation, token, self.lead),
        ):
            with self.assertRaisesMessage(ValueError, "Paste the roster again."):
                load_preview(operation, value, actor=actor)
        with mock.patch.object(roster_import, "PREVIEW_MAX_AGE", -1):
            with self.assertRaisesMessage(ValueError, "older than 30 minutes"):
                load_preview(self.operation, token, actor=self.fc)
        self.assertEqual(roster_import.PREVIEW_MAX_AGE, 30 * 60)


class ImportRowsTests(RosterTestCase):
    def row(self, character, status="add"):
        return {"character_id": character.character_id, "character_name": character.character_name, "status": status}

    def run_import(self, rows, operation=None, actor=None, **settings):
        values = dict(attendance_value=1, duplicate_action="keep", notes="Imported from fleet roster paste")
        values.update(settings)
        return import_rows(operation or self.operation, rows, actor=actor or self.fc, **values)

    def test_creates_imported_rows_through_the_attendance_service(self):
        main = f.main_of(self.sally)
        with mock.patch(
            "fleetops.services.attendance.create_manual_attendance",
            wraps=roster_import.attendance.create_manual_attendance,
        ) as create, mock.patch(
            "fleetops.services.pap.refresh_after_change", wraps=roster_import.pap.refresh_after_change
        ) as refresh:
            result = self.run_import([self.row(main)], attendance_value=2, duplicate_action="merge", notes="Ops")

        # The rows are created without a recalculation each; the fleet is recalculated once.
        create.assert_called_once_with(
            self.operation,
            actor=self.fc,
            character_id=main.character_id,
            character_name="Sally en Chalune",
            attendance_value=2,
            duplicate_action="merge",
            notes="Ops",
            source=IMPORTED,
            refresh=False,
        )
        refresh.assert_called_once_with(self.operation, actor=self.fc, reason="roster import")
        self.assertEqual(len(result.created), 1)
        self.assertEqual(result.skipped, [])

    def test_one_audit_entry_with_created_and_skipped_rows(self):
        main = f.main_of(self.sally)
        stranger = f.create_character("Left Alliance Auth")

        result = self.run_import([self.row(main), self.row(main), self.row(stranger)])

        record = AttendanceRecord.objects.get(operation=self.operation, character_id=main.character_id)
        self.assertEqual((record.source, record.created_by, record.notes), (IMPORTED, self.fc, "Imported from fleet roster paste"))
        self.assertEqual(
            result.skipped,
            [
                {"character_id": main.character_id, "character_name": "Sally en Chalune", "reason": "Listed twice"},
                {"character_id": stranger.character_id, "character_name": "Left Alliance Auth", "reason": "Not in Alliance Auth"},
            ],
        )
        entry = AuditLog.objects.get(action="attendance.roster_import")
        self.assertEqual((entry.actor, entry.object_type, entry.object_id), (self.fc, "FleetOperation", str(self.operation.pk)))
        self.assertEqual(
            entry.new_value,
            {
                "count": 1,
                "created_ids": [record.pk],
                "skipped": result.skipped,
                "attendance_value": 1,
                "duplicate_action": "keep",
            },
        )

    def test_importing_the_same_preview_twice_credits_once(self):
        rows = [self.row(f.main_of(self.sally))]

        self.run_import(rows)
        second = self.run_import(rows)

        self.assertEqual(second.created, [])
        self.assertEqual(second.skipped[0]["reason"], "Already has PAP in this fleet")
        self.assertEqual(AttendanceRecord.objects.filter(source=IMPORTED).count(), 1)

    def test_rows_ticked_although_they_have_pap_are_imported(self):
        f.add_attendance(self.operation, self.fc)

        result = self.run_import([self.row(f.main_of(self.fc), status="has_pap")])

        self.assertEqual(len(result.created), 1)

    def test_only_open_and_closed_fleets(self):
        cancelled = f.create_operation(self.fc, status=Status.CANCELLED)

        with self.assertRaisesMessage(ValueError, "Manual attendance can only be added to Active or Closed fleets."):
            self.run_import([self.row(f.main_of(self.sally))], operation=cancelled)
        self.assertFalse(AuditLog.objects.exists())

    def test_alt_of_another_corporation_counts_for_the_main(self):
        alt = f.add_alt(self.sally, "Sally Foreign Alt", corporation=f.OTHER_CORP, alliance=f.FOREIGN_ALLIANCE)

        self.run_import([self.row(alt)])

        record = AttendanceRecord.objects.get(character_id=alt.character_id)
        main = f.main_of(self.sally)
        self.assertEqual(
            (record.auth_user, record.main_character_id, record.corporation_id),
            (self.sally, main.character_id, main.corporation_id),
        )


class RosterImportViewTests(RosterTestCase):
    def url(self, operation=None):
        return reverse("fleetops:roster_import", args=[(operation or self.operation).uuid])

    def test_access(self):
        other_fc = f.create_user(perms=f.FC_PERMS)
        member = f.create_user(perms=f.MEMBER_PERMS)
        cancelled = f.create_operation(self.fc, status=Status.CANCELLED)

        self.client.force_login(member)
        self.assertEqual(self.client.get(self.url()).status_code, 403)
        self.client.force_login(other_fc)
        self.assertEqual(self.client.get(self.url()).status_code, 404)
        self.client.force_login(self.fc)
        page = self.client.get(self.url())
        self.assertContains(page, "Imported from fleet roster paste")
        self.assertContains(page, "Imported PAP counts once an FC lead approves it.")
        refused = self.client.get(self.url(cancelled))
        self.assertRedirects(refused, reverse("fleetops:operation_detail", args=[cancelled.uuid]), fetch_redirect_response=False)
        self.client.force_login(self.lead)
        self.assertNotContains(self.client.get(self.url()), "Imported PAP counts once")

    def test_paste_preview_and_import(self):
        f.add_attendance(self.operation, self.fc)
        self.esi.add("Stream Sarum")
        self.client.force_login(self.fc)

        preview = self.client.post(
            self.url(),
            {"step": "parse", "text": CHINESE_FLEET, "attendance_value": "2", "duplicate_action": "keep", "notes": "Ops"},
        )

        self.assertEqual(preview.status_code, 200)
        rows = preview.context["preview_rows"]
        self.assertEqual([(item["row"].status, item["index"]) for item in rows], [("add", 0), ("has_pap", 1), ("not_in_auth", None)])
        self.assertContains(preview, 'name="rows" value="0" checked')
        self.assertContains(preview, 'name="rows" value="1" aria-label')
        self.assertContains(preview, "Ragnarok")
        self.assertEqual(preview.context["counts"], {"add": 1, "has_pap": 1, "not_in_auth": 1})

        response = self.client.post(
            self.url(), {"step": "import", "payload": preview.context["payload"], "rows": ["0"]}
        )

        self.assertRedirects(
            response, reverse("fleetops:operation_detail", args=[self.operation.uuid]), fetch_redirect_response=False
        )
        record = AttendanceRecord.objects.get(source=IMPORTED)
        self.assertEqual((record.character_name, record.attendance_value, record.notes), ("Sally en Chalune", 2, "Ops"))
        self.assertIn("Imported PAP for 1 character.", message_texts(response)[0])

    def test_nothing_pasted_or_selected(self):
        self.client.force_login(self.fc)

        empty = self.client.post(self.url(), {"step": "parse", "text": "-----\n", "attendance_value": "1", "duplicate_action": "keep"})
        preview = self.client.post(
            self.url(), {"step": "parse", "text": "Sally en Chalune", "attendance_value": "1", "duplicate_action": "keep"}
        )
        none = self.client.post(self.url(), {"step": "import", "payload": preview.context["payload"]})

        self.assertContains(empty, "No character names were found in the pasted text.")
        self.assertRedirects(none, self.url(), fetch_redirect_response=False)
        self.assertIn("Nothing was imported: no character was selected.", message_texts(none))
        self.assertFalse(AttendanceRecord.objects.exists())

    def test_tampered_preview_imports_nothing(self):
        self.client.force_login(self.fc)
        forged = signing.dumps(
            {"operation": str(self.operation.uuid), "actor": self.fc.pk, "attendance_value": 100,
             "duplicate_action": "keep", "notes": "", "rows": [[f.main_of(self.sally).character_id, "x", "add"]]},
            salt="not-the-roster-salt",
        )

        response = self.client.post(self.url(), {"step": "import", "payload": forged, "rows": ["0"]})

        self.assertRedirects(response, self.url(), fetch_redirect_response=False)
        self.assertIn("The preview is older than 30 minutes or was changed. Paste the roster again.", message_texts(response))
        self.assertFalse(AttendanceRecord.objects.exists())

    def test_entry_points(self):
        self.client.force_login(self.fc)

        fleet_page = self.client.get(reverse("fleetops:operation_detail", args=[self.operation.uuid]))
        manual_page = self.client.get(reverse("fleetops:manual_attendance"))
        chosen = self.client.get(reverse("fleetops:manual_attendance"), {"roster_fleet": self.operation.pk})
        foreign = self.client.get(
            reverse("fleetops:manual_attendance"),
            {"roster_fleet": f.create_operation(f.create_user(perms=f.FC_PERMS), status=Status.CLOSED).pk},
        )

        self.assertContains(fleet_page, f'href="{self.url()}">Paste roster</a>')
        self.assertContains(manual_page, f'<option value="{self.operation.pk}">')
        self.assertRedirects(chosen, self.url(), fetch_redirect_response=False)
        self.assertRedirects(foreign, reverse("fleetops:manual_attendance"), fetch_redirect_response=False)


class ManualEntryByNameTests(RosterTestCase):
    def form(self, **data):
        values = {"attendance_value": "1", "duplicate_action": "keep"}
        values.update(data)
        return ManualAttendanceForm(values)

    def test_name_found_in_alliance_auth(self):
        form = self.form(character_name="  sally EN chalune ")

        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["character_id"], f.main_of(self.sally).character_id)
        self.assertEqual(form.cleaned_data["character_name"], "Sally en Chalune")
        self.assertEqual(self.esi.calls, [])

    def test_name_found_in_eve_and_owned(self):
        renamed = f.add_alt(self.sally, "Before Rename")
        self.esi.add("After Rename", renamed.character_id)

        form = self.form(character_name="After Rename")

        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["character_id"], renamed.character_id)

    def test_name_errors(self):
        self.esi.add("Stream Sarum")
        cases = {
            "Stream Sarum": "Character 'Stream Sarum' exists in EVE but is not registered in Alliance Auth, so it cannot receive PAP.",
            "Nobody Here": "No EVE character named 'Nobody Here' was found.",
            "Bad<Name>": "'Bad<Name>' cannot be an EVE character name. Contains characters that cannot appear in a character name.",
        }
        for name, error in cases.items():
            with self.subTest(name=name):
                form = self.form(character_name=name)
                self.assertFalse(form.is_valid())
                self.assertEqual(form.errors["character_name"], [error])

    def test_esi_error(self):
        self.esi.failure = ESI_DOWN

        form = self.form(character_name="Stream Sarum")

        self.assertFalse(form.is_valid())
        self.assertEqual(
            form.errors["character_name"],
            ["Could not look the name up in ESI (ESI returned HTTP 503 (Service unavailable)). Enter the character ID instead."],
        )

    def test_name_or_id_needed(self):
        form = self.form()

        self.assertFalse(form.is_valid())
        self.assertEqual(form.non_field_errors(), ["Enter a character name or a character ID."])

    def test_id_wins_over_the_name(self):
        main = f.main_of(self.sally)

        form = self.form(character_id=str(main.character_id), character_name="Typed Label")

        self.assertTrue(form.is_valid())
        self.assertEqual((form.cleaned_data["character_id"], form.cleaned_data["character_name"]), (main.character_id, "Typed Label"))
        self.assertEqual(self.esi.calls, [])

    def test_invalid_entries_are_not_looked_up(self):
        form = self.form(character_name="Somebody", attendance_value="0")

        self.assertFalse(form.is_valid())
        self.assertEqual(self.esi.calls, [])

    def test_historical_page_by_name(self):
        self.client.force_login(self.fc)

        response = self.client.post(
            reverse("fleetops:manual_attendance"),
            {"operation": self.operation.pk, "character_name": "Sally en Chalune", "attendance_value": "1", "duplicate_action": "keep"},
        )
        missing = self.client.post(
            reverse("fleetops:manual_attendance"),
            {"operation": self.operation.pk, "character_name": "Nobody Here", "attendance_value": "1", "duplicate_action": "keep"},
        )

        self.assertRedirects(response, reverse("fleetops:manual_attendance"), fetch_redirect_response=False)
        self.assertTrue(AttendanceRecord.objects.filter(character_id=f.main_of(self.sally).character_id).exists())
        self.assertContains(missing, "No EVE character named &#x27;Nobody Here&#x27; was found.")
        self.assertIsInstance(missing.context["form"], HistoricalManualAttendanceForm)

    def test_fleet_page_keeps_an_entry_that_was_not_saved(self):
        self.client.force_login(self.fc)
        url = reverse("fleetops:add_manual_attendance", args=[self.operation.uuid])
        page_url = reverse("fleetops:operation_detail", args=[self.operation.uuid])

        response = self.client.post(
            url, {"character_name": "Nobody Here", "attendance_value": "3", "duplicate_action": "merge", "notes": "Late"}
        )

        self.assertRedirects(response, page_url, fetch_redirect_response=False)
        page = self.client.get(page_url)
        self.assertEqual(page.context["manual_attendance_errors"], ["No EVE character named 'Nobody Here' was found."])
        form = page.context["manual_attendance_form"]
        self.assertEqual(
            (form["character_name"].value(), form["attendance_value"].value(), form["duplicate_action"].value()),
            ("Nobody Here", "3", "merge"),
        )
        self.assertContains(page, 'id="manual-attendance-errors"')
        # Shown once.
        again = self.client.get(page_url)
        self.assertNotIn("manual_attendance_errors", again.context)

    def test_fleet_page_saves_by_name(self):
        self.client.force_login(self.fc)

        self.client.post(
            reverse("fleetops:add_manual_attendance", args=[self.operation.uuid]),
            {"character_name": "sally en chalune", "attendance_value": "1", "duplicate_action": "keep"},
        )

        record = AttendanceRecord.objects.get(operation=self.operation)
        self.assertEqual((record.character_id, record.character_name), (f.main_of(self.sally).character_id, "Sally en Chalune"))
