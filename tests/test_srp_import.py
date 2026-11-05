# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""The aa-srp history import (IMP-1 to IMP-8): reading, mapping, the command, the killmails it leaves behind, the
claims it makes read only, undoing it and completing the stand-in killmails."""

import json
import tempfile
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone
from decimal import Decimal
from io import StringIO
from types import SimpleNamespace
from unittest import mock

from django.contrib.auth.models import User
from django.core.management import CommandError, call_command
from django.test import TestCase
from django.utils import timezone

from fleetops import texts
from fleetops.models import AuditLog, SrpClaim, SrpClaimEvent, SrpFleetLink, SrpKillmail, SrpSettings
from fleetops.providers.esi import FleetESIError
from fleetops.srp import claims, importer, killmails, legacy_import, quota
from fleetops.srp import review
from fleetops.srp import tasks as srp_tasks
from fleetops.srp.types import SrpDuplicateError, SrpError, SrpStateError

from . import factories as f
from .aasrp_fakes import FLEET_TIME, FakeAaSrp, WriteAttempted

Status = SrpClaim.Status


class ImportTestCase(TestCase):
    """aa-srp installed with fake models; ``self.srp`` holds its rows."""

    shape = "5.x"

    def setUp(self):
        super().setUp()
        self.srp = FakeAaSrp(self.shape).install(self)
        self.pilot = f.create_user("importer-pilot")
        self.pilot_char = f.main_of(self.pilot)
        self.reviewer = f.create_user("importer-reviewer")
        self.fc = f.create_user("importer-fc")
        self.link = self.srp.add_link("Home defense fleet", commander=f.main_of(self.fc), creator=self.fc)

    def run_import(self, **options):
        options.setdefault("batch_size", 500)
        return importer.run_import(importer.Options(**options))

    def command(self, *args, expect_error=False):
        out, err = StringIO(), StringIO()
        if expect_error:
            with self.assertRaises(CommandError) as caught:
                call_command("fleetops_import_aasrp", *args, stdout=out, stderr=err)
            return str(caught.exception)
        call_command("fleetops_import_aasrp", *args, stdout=out, stderr=err)
        return out.getvalue() + err.getvalue()

    def approved(self, **kwargs):
        kwargs.setdefault("creator", self.pilot)
        kwargs.setdefault("decided_by", self.reviewer)
        return self.srp.add_request(self.link, self.pilot_char, **kwargs)

    def only_claim(self):
        return SrpClaim.objects.get(source="aasrp")


# --- IMP-2: the reading layer ------------------------------------------------------------------------------------


class ReadingTests(ImportTestCase):
    def source(self):
        return legacy_import.AaSrpSource()

    def test_a_request_of_aasrp_5_is_read_with_its_decision(self):
        row = self.approved(
            loss=150_000_000,
            payout=120_000_000,
            info="lost it",
            decision_comment="ok",
            decided_at=FLEET_TIME + timedelta(hours=20),
        )
        self.srp.add_comment(row, self.fc, "pending note", FLEET_TIME + timedelta(hours=16), "Pending")

        [request] = list(self.source().requests())

        self.assertEqual(
            (request.pk, request.killmail_id, request.ship_type_id, request.status, request.loss_amount, request.payout_amount),
            (row.pk, row.killmail_id, 587, "approved", 150_000_000, 120_000_000),
        )
        self.assertEqual((request.decided_by_id, request.decided_at), (self.reviewer.pk, FLEET_TIME + timedelta(hours=20)))
        self.assertEqual(request.comment, "lost it")
        self.assertEqual(request.character_id, self.pilot_char.character_id)

    def test_the_reviewer_is_found_by_the_creator_of_the_status_comment(self):
        self.approved(decision_comment="ok")
        self.run_import()

        self.assertEqual(self.only_claim().decided_by, self.reviewer)

    def test_the_decision_is_the_last_comment_that_set_the_present_status(self):
        row = self.approved(decided_by=self.fc, decision_comment="first", decided_at=FLEET_TIME + timedelta(hours=1))
        self.srp.add_comment(row, self.reviewer, "second", FLEET_TIME + timedelta(hours=2), "Approved")
        self.srp.add_comment(row, self.fc, "chat only", FLEET_TIME + timedelta(hours=3), "")

        [request] = list(self.source().requests())

        self.assertEqual((request.decided_by_id, request.decision_text), (self.reviewer.pk, "second"))

    def test_the_killmail_comes_from_the_link_when_aasrp_has_no_killmail_id(self):
        srp = FakeAaSrp("no_killmail_id").install(self)
        link = srp.add_link("Old fleet")
        srp.add_request(link, self.pilot_char, killmail_id=123456)

        [request] = list(legacy_import.AaSrpSource().requests())

        self.assertEqual(request.killmail_id, 123456)
        self.assertIsNone(request.ship_type_id)
        self.assertEqual(request.reject_info, "")

    def test_a_link_with_a_query_or_a_slash_gives_the_same_killmail(self):
        srp = FakeAaSrp("no_killmail_id").install(self)
        link = srp.add_link("Old fleet")
        for text in ("https://zkillboard.com/kill/777/", "https://zkillboard.com/kill/778", "https://zkillboard.com/kill/779/?x=1"):
            row = srp.add_request(link, self.pilot_char)
            row.killboard_link = text

        found = [request.killmail_id for request in legacy_import.AaSrpSource().requests()]

        self.assertEqual(found, [777, 778, 779])

    def test_with_only_a_ship_name_the_type_is_found_in_the_sde(self):
        srp = FakeAaSrp("ship_name").install(self)
        link = srp.add_link("Old fleet")
        srp.add_request(link, self.pilot_char, ship_name="Rifter")
        srp.add_request(link, self.pilot_char, ship_name="Mystery hull")
        with mock.patch.object(
            legacy_import.sde, "find_item_type", side_effect=lambda name: (587, "Rifter") if name == "Rifter" else None
        ) as find:
            requests = list(legacy_import.AaSrpSource().requests())

        self.assertEqual([r.ship_type_id for r in requests], [587, None])
        self.assertEqual(find.call_count, 2)

    def test_the_dates_filter_by_the_posting_time(self):
        early = self.approved(post_time=FLEET_TIME)
        late = self.approved(post_time=FLEET_TIME + timedelta(days=10))

        found = list(self.source().requests(since=FLEET_TIME + timedelta(days=1), until=FLEET_TIME + timedelta(days=30)))
        self.assertEqual([r.pk for r in found], [late.pk])
        found = list(self.source().requests(until=FLEET_TIME + timedelta(days=1)))
        self.assertEqual([r.pk for r in found], [early.pk])

    def test_reading_never_writes_and_asks_for_the_comments_once_per_batch(self):
        for _ in range(3):
            self.approved(decision_comment="ok")
        before = dict(self.srp.comments.objects.counter)

        list(self.source().requests())
        list(self.source().links())

        # One pass over the comments for the batch of three requests; any create/update/delete would have raised.
        self.assertEqual(self.srp.comments.objects.counter["iterators"] - before["iterators"], 1)
        for model in (self.srp.requests, self.srp.links, self.srp.comments):
            with self.assertRaises(WriteAttempted):
                model.objects.create()

    def test_more_than_one_batch_is_read_in_pieces(self):
        for _ in range(5):
            self.approved(decision_comment="ok")
        with mock.patch.object(legacy_import, "BATCH_SIZE", 2):
            requests = list(self.source().requests())

        self.assertEqual(len(requests), 5)
        self.assertEqual(self.srp.comments.objects.counter["iterators"], 3)

    def test_the_value_source_of_aasrp_is_translated(self):
        self.assertEqual(self.source().value_source(), "zkill_total")
        self.srp.settings.objects.rows[0].loss_value_source = "fittedValue"
        self.assertEqual(self.source().value_source(), "zkill_fitted")
        self.srp.settings.objects.rows[0].loss_value_source = "something else"
        self.assertEqual(self.source().value_source(), "")

    def test_without_aasrp_the_source_cannot_be_made(self):
        with mock.patch.object(legacy_import, "available", return_value=False):
            with self.assertRaises(legacy_import.AaSrpMissing):
                legacy_import.AaSrpSource()


# --- IMP-3: the mapping ------------------------------------------------------------------------------------------


class MappingTests(ImportTestCase):
    def test_an_approved_request_becomes_a_paid_claim_with_the_recorded_amount(self):
        row = self.approved(
            loss=300_000_000, payout=250_000_000, info="x" * 1500, decided_at=FLEET_TIME + timedelta(hours=20)
        )
        self.run_import(actor=self.reviewer)

        claim = self.only_claim()
        self.assertEqual(claim.status, Status.PAID)
        self.assertEqual((claim.approved_amount, claim.suggested_amount, claim.limited_amount), (250_000_000,) * 3)
        self.assertEqual((claim.base_amount, claim.loss_value), (300_000_000, Decimal(300_000_000)))
        self.assertEqual(claim.paid_at, FLEET_TIME + timedelta(hours=20))
        self.assertIsNone(claim.paid_by)
        self.assertIsNone(claim.payment_batch)
        self.assertEqual(claim.payment_note, texts.SRP_IMPORTED_PAYMENT_NOTE())
        self.assertEqual(claim.payment_note, "Imported from aa-srp")
        self.assertEqual(claim.external_id, f"aasrp:{row.pk}")
        self.assertEqual(claim.member_comment, "x" * 1000)
        self.assertEqual(claim.submitted_at, row.post_time)
        self.assertEqual(claim.value_source, "zkill_total")

    def test_a_payout_of_zero_is_paid_as_the_loss(self):
        self.approved(loss=80_000_000, payout=0)
        self.run_import()

        claim = self.only_claim()
        self.assertEqual((claim.approved_amount, claim.suggested_amount, claim.limited_amount), (80_000_000,) * 3)

    def test_a_rejected_request_keeps_its_reason_and_does_not_raise_the_requested_amount(self):
        self.approved(status="Rejected", loss=500_000_000, payout=0, reject_info="Capsules are not covered")
        self.run_import()

        claim = self.only_claim()
        self.assertEqual(claim.status, Status.REJECTED)
        self.assertEqual((claim.suggested_amount, claim.limited_amount, claim.base_amount), (0, 0, 500_000_000))
        self.assertIsNone(claim.approved_amount)
        self.assertEqual(claim.decision_reason, "Capsules are not covered")
        self.assertFalse(claim.auto_rejected)
        self.assertIsNone(claim.paid_at)

    def test_without_reject_info_the_last_rejection_comment_is_the_reason(self):
        self.approved(status="Rejected", payout=0, decision_comment="Not a PvP ship", reject_info="")
        self.run_import()

        self.assertEqual(self.only_claim().decision_reason, "Not a PvP ship")

    def test_a_rejection_of_aasrp_5_keeps_the_text_of_its_reject_reason_comment(self):
        self.approved(status="Rejected", payout=0, reject_info="", decision_comment="", reject_reason="Capsules are not covered")
        self.run_import()

        claim = self.only_claim()
        self.assertEqual(claim.decision_reason, "Capsules are not covered")
        self.assertEqual(claim.decided_by, self.reviewer)

    def test_reject_info_wins_over_the_reject_reason_comment(self):
        self.approved(status="Rejected", payout=0, reject_info="From the field", reject_reason="From the comment")
        self.run_import()

        self.assertEqual(self.only_claim().decision_reason, "From the field")

    def test_a_pending_request_is_not_imported_and_counted(self):
        self.approved(status="Pending", loss=70_000_000)
        report = self.run_import()

        self.assertEqual(SrpClaim.objects.count(), 0)
        self.assertEqual((report.pending, report.pending_amount), (1, 70_000_000))

    def test_the_fixed_fields_of_an_imported_claim(self):
        self.approved()
        self.run_import()

        claim = self.only_claim()
        self.assertEqual(claim.payout_basis, "imported")
        self.assertEqual((claim.policy, claim.calculation, claim.warnings, claim.fleet_check), ({}, {}, [], ""))
        self.assertFalse(claim.over_limit)
        self.assertEqual(claim.source, "aasrp")
        self.assertTrue(claim.is_imported)
        km = claim.killmail
        self.assertEqual((km.killmail_hash, km.solar_system_id, km.origin), ("", 0, "aasrp"))
        self.assertEqual((km.zkill_status, km.zkill_next_attempt_at), ("disabled", None))
        self.assertEqual((km.victim_character_id, km.ship_type_id), (self.pilot_char.character_id, 587))

    def test_the_decider_and_the_user_come_from_aasrp_and_the_snapshot_from_the_main(self):
        self.approved(decided_at=FLEET_TIME + timedelta(hours=9))
        self.run_import()

        claim = self.only_claim()
        self.assertEqual((claim.user, claim.decided_by, claim.decided_at), (self.pilot, self.reviewer, FLEET_TIME + timedelta(hours=9)))
        self.assertEqual(claim.character_id, self.pilot_char.character_id)
        self.assertEqual(claim.main_character_id, f.main_of(self.pilot).character_id)
        self.assertEqual(claim.corporation_id, f.main_of(self.pilot).corporation_id)

    def test_a_deleted_user_falls_back_to_the_owner_of_the_character_then_to_nobody(self):
        sentinel = User.objects.create(username="deleted")
        self.approved(creator=sentinel)
        stranger = f.create_character("Unowned pilot")
        self.srp.add_request(self.link, stranger, creator=sentinel, decided_by=sentinel, decision_comment="ok")
        self.run_import()

        owned, unowned = SrpClaim.objects.filter(source="aasrp").order_by("external_id")
        self.assertEqual(owned.user, self.pilot)
        self.assertIsNone(unowned.user)
        self.assertIsNone(unowned.decided_by)
        # Without an account the claim keeps the corporation of the character itself.
        self.assertEqual(unowned.corporation_id, stranger.corporation_id)
        self.assertEqual(unowned.main_character_name, "")

    def test_the_counting_flags_come_from_the_policy_of_the_scope(self):
        SrpSettings.objects.update(personal_cap_includes_in_fleet=False, corporation_cap_includes_in_fleet=True)
        self.approved()
        self.run_import()

        claim = self.only_claim()
        self.assertEqual(claim.scope, "in_fleet")
        self.assertEqual((claim.counts_personal, claim.counts_corporation), (False, True))

    def test_a_plain_link_becomes_a_closed_fleet_link_without_a_fleet(self):
        self.approved(decided_at=FLEET_TIME + timedelta(hours=20))
        self.approved(decided_at=FLEET_TIME + timedelta(hours=22))
        self.run_import()

        link = SrpFleetLink.objects.get(origin="aasrp")
        self.assertEqual(
            (link.kind, link.operation_id, link.external_id, link.code, link.name, link.is_open),
            ("fleet", None, f"aasrp:{self.link.pk}", "aa" + self.link.srp_code, "Home defense fleet", False),
        )
        self.assertEqual(link.link_date, FLEET_TIME.date())
        self.assertEqual(link.fc_user, self.fc)
        self.assertEqual(link.fc_main_character_id, f.main_of(self.fc).character_id)
        self.assertEqual(link.completed_at, FLEET_TIME + timedelta(hours=22))
        self.assertEqual(link.state, "completed")
        for claim in SrpClaim.objects.filter(source="aasrp"):
            self.assertEqual((claim.link, claim.scope, claim.category), (link, "in_fleet", f.fleet_category()))
            self.assertEqual(claim.fc_user, self.fc)
            self.assertEqual(claim.fc_main_character_name, f.main_of(self.fc).character_name)
            self.assertEqual(claim.fleet_label, "Home defense fleet — 2026-09-12 19:00 EVE")
            self.assertIsNone(claim.operation)

    def test_a_link_with_a_rejection_only_is_closed_but_not_completed(self):
        self.approved(status="Rejected", payout=0, reject_info="no")
        self.run_import()

        link = SrpFleetLink.objects.get(origin="aasrp")
        self.assertEqual((link.is_open, link.completed_at, link.state), (False, None, "closed"))

    def test_an_active_link_is_closed(self):
        self.link.srp_status = "Active"
        self.approved()
        self.run_import()

        self.assertFalse(SrpFleetLink.objects.get(origin="aasrp").is_open)

    def test_the_fc_stays_empty_when_the_commander_has_no_owner(self):
        link = self.srp.add_link("Orphan fleet", commander=f.create_character("Nobody owns me"))
        self.srp.add_request(link, self.pilot_char, creator=self.pilot)
        self.run_import()

        row = SrpFleetLink.objects.get(external_id=f"aasrp:{link.pk}")
        self.assertEqual((row.fc_user, row.fc_main_character_id, row.fc_main_character_name), (None, None, ""))

    def write_map(self, entries):
        handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
        self.addCleanup(__import__("os").unlink, handle.name)
        json.dump(entries, handle)
        handle.close()
        return handle.name

    def test_a_link_matched_by_the_category_map_becomes_a_standing_link_of_the_category(self):
        category = f.srp_category("Solo PVP", key="solo-pvp")
        solo = self.srp.add_link("Solo PVP Fleet", commander=f.main_of(self.fc))
        self.srp.add_request(solo, self.pilot_char, creator=self.pilot)
        self.approved()
        path = self.write_map([{"pattern": "(?i)solo", "category": "solo-pvp"}, {"pattern": ".*", "category": "solo-pvp"}])

        self.run_import(category_map=importer.load_category_map(path)[:1])

        standing = SrpFleetLink.objects.get(external_id=f"aasrp:{solo.pk}")
        self.assertEqual((standing.kind, standing.category, standing.operation_id, standing.origin), ("standing", category, None, "aasrp"))
        self.assertIsNone(standing.fc_user)
        claim = SrpClaim.objects.get(link=standing)
        self.assertEqual((claim.scope, claim.category, claim.fc_user), ("out_of_fleet", category, None))
        self.assertEqual(claim.fleet_label, "Solo PVP Fleet")
        other = SrpClaim.objects.exclude(link=standing).get()
        self.assertEqual((other.scope, other.category), ("in_fleet", f.fleet_category()))

    def test_the_first_matching_rule_of_the_map_decides(self):
        first = f.srp_category("First", key="first")
        f.srp_category("Second", key="second")
        path = self.write_map([{"pattern": "Home", "category": "first"}, {"pattern": "Home", "category": "second"}])

        rules = importer.load_category_map(path)

        self.assertEqual(importer.matched_category("Home defense", rules), first)
        self.assertIsNone(importer.matched_category("Roaming", rules))

    def test_a_bad_category_map_is_refused_with_the_entry_that_is_wrong(self):
        f.srp_category("Real", key="real")
        for entries, fragment in (
            ({"pattern": "x"}, "JSON list"),
            ([{"pattern": "(", "category": "real"}], "entry 1 has an invalid pattern"),
            ([{"pattern": "x", "category": "nope"}], 'unknown category "nope"'),
            ([{"pattern": "x"}], "entry 1 needs"),
        ):
            with self.subTest(fragment=fragment):
                with self.assertRaises(importer.ImportConfigError) as caught:
                    importer.load_category_map(self.write_map(entries))
                self.assertIn(fragment, str(caught.exception))
        with self.assertRaises(importer.ImportConfigError):
            importer.load_category_map("/nonexistent/map.json")

    def test_the_time_of_the_loss_is_the_time_of_the_link_and_marked_as_an_estimate(self):
        self.approved()
        report = self.run_import(actor=self.reviewer)

        claim = self.only_claim()
        self.assertEqual((claim.killmail_time, claim.killmail.killmail_time), (FLEET_TIME, FLEET_TIME))
        self.assertEqual(report.estimated_times, 1)
        event = claim.events.get()
        self.assertTrue(event.data["killmail_time_estimated"])

    def test_a_stored_killmail_without_a_claim_is_reused_with_its_real_time(self):
        row = self.approved()
        real_time = FLEET_TIME + timedelta(hours=1, minutes=7)
        km = f.srp_killmail(self.pilot_char, killmail_id=row.killmail_id, time=real_time)
        self.run_import()

        claim = self.only_claim()
        self.assertEqual(claim.killmail, km)
        self.assertEqual(claim.killmail_time, real_time)
        self.assertEqual(SrpKillmail.objects.get(pk=km.pk).origin, "esi")
        self.assertFalse(claim.events.get().data["killmail_time_estimated"])

    def test_every_claim_has_an_import_event_and_an_audit_entry_without_secrets(self):
        row = self.approved(decided_at=FLEET_TIME + timedelta(hours=9))
        self.run_import(actor=self.reviewer)

        claim = self.only_claim()
        event = claim.events.get()
        self.assertEqual(event.action, "import")
        self.assertEqual(event.actor, self.reviewer)
        self.assertEqual(
            event.data,
            {
                "aasrp_request": row.pk,
                "request_code": row.request_code,
                "aasrp_status": "approved",
                "killmail_time_estimated": True,
            },
        )
        entry = AuditLog.objects.get(action="srp.claim_import")
        self.assertEqual((entry.object_id, entry.actor), (str(claim.pk), self.reviewer))
        total = AuditLog.objects.get(action="srp.import_aasrp")
        self.assertEqual(total.new_value["counts"]["created"], 1)
        self.assertEqual(total.object_type, "SrpImport")

    def test_the_ship_name_comes_from_aasrp_or_the_sde(self):
        self.approved(ship_name="Rifter")
        self.approved(ship_name="")
        with mock.patch.object(importer.sde, "item_type_names", return_value={587: "Rifter (SDE)"}):
            self.run_import()

        names = sorted(SrpClaim.objects.values_list("ship_type_name", flat=True))
        self.assertEqual(names, ["Rifter", "Rifter (SDE)"])


# --- IMP-4: the command ------------------------------------------------------------------------------------------


class CommandTests(ImportTestCase):
    def test_without_aasrp_the_command_says_so(self):
        with mock.patch.object(legacy_import, "available", return_value=False):
            message = self.command(expect_error=True)
        self.assertEqual(message, "aa-srp (aasrp) is not installed.")

    def test_a_dry_run_writes_nothing_and_reports(self):
        self.approved(loss=100_000_000)
        self.approved(status="Pending")
        self.approved(status="Rejected", payout=0, reject_info="no")
        before = (SrpClaim.objects.count(), SrpKillmail.objects.count(), SrpFleetLink.objects.count(), AuditLog.objects.count())

        output = self.command("--dry-run")

        self.assertEqual(
            (SrpClaim.objects.count(), SrpKillmail.objects.count(), SrpFleetLink.objects.count(), AuditLog.objects.count()),
            before,
        )
        self.assertIn("Would create 2 claims", output)
        self.assertIn("aa-srp links read: 1; requests read: 3", output)
        self.assertIn("2026-09:", output)
        self.assertIn("approved 1 / 100,000,000", output)
        self.assertIn("pending in aa-srp: 1", output)
        self.assertIn("WARNING: 1 requests are still pending", output)
        self.assertIn("Dry run: nothing was written.", output)
        self.assertIn("Time:", output)

    def test_the_real_run_warns_about_pending_requests_before_it_writes(self):
        self.approved(status="Pending")
        self.approved()
        output = self.command()

        self.assertIn("WARNING: 1 requests are still pending", output)
        self.assertEqual(SrpClaim.objects.count(), 1)

    def test_the_run_is_repeatable_and_a_second_run_changes_nothing(self):
        self.approved()
        self.approved(status="Rejected", payout=0, reject_info="no")
        self.run_import(actor=self.reviewer)
        snapshot = list(SrpClaim.objects.order_by("pk").values())
        events = SrpClaimEvent.objects.count()
        audits = AuditLog.objects.filter(action="srp.claim_import").count()

        report = self.run_import(actor=self.reviewer)

        self.assertEqual((report.create, report.update, report.unchanged), (0, 0, 2))
        self.assertEqual(list(SrpClaim.objects.order_by("pk").values()), snapshot)
        self.assertEqual(SrpClaimEvent.objects.count(), events)
        self.assertEqual(AuditLog.objects.filter(action="srp.claim_import").count(), audits)
        self.assertEqual(SrpFleetLink.objects.count(), 1)

    def test_a_change_in_aasrp_updates_the_imported_claim_and_leaves_a_trail(self):
        row = self.approved(loss=100_000_000, payout=100_000_000)
        self.run_import(actor=self.reviewer)
        row.payout_amount = 60_000_000

        report = self.run_import(actor=self.reviewer)

        claim = self.only_claim()
        self.assertEqual((report.update, claim.approved_amount), (1, 60_000_000))
        update = claim.events.order_by("pk").last()
        self.assertEqual(update.action, "import")
        self.assertEqual(update.data["update"]["approved_amount"], [100_000_000, 60_000_000])
        self.assertIn("suggested_amount", update.data["update"])

    def test_claims_of_fleetops_are_never_touched(self):
        km = f.srp_killmail(self.pilot_char, killmail_id=555_001)
        native = f.srp_claim(self.pilot, killmail=km, amount=1_000_000)
        before = SrpClaim.objects.filter(pk=native.pk).values().get()
        self.approved(killmail_id=555_001)
        self.approved()

        output = self.command()

        self.assertEqual(SrpClaim.objects.filter(pk=native.pk).values().get(), before)
        self.assertEqual(SrpClaim.objects.filter(source="aasrp").count(), 1)
        self.assertIn("already a FleetOps claim: 1", output)
        self.assertIn(native.reference, output)

    def test_the_same_killmail_in_several_requests_imports_the_best_one(self):
        killmail_id = 777_001
        self.approved(status="Rejected", payout=0, killmail_id=killmail_id, reject_info="x")
        approved = self.approved(killmail_id=killmail_id, post_time=FLEET_TIME + timedelta(hours=1))
        self.approved(killmail_id=killmail_id, post_time=FLEET_TIME + timedelta(hours=30))
        report = self.run_import()

        claim = self.only_claim()
        self.assertEqual(report.skipped["duplicate"], 2)
        # Approved before rejected, then the newest of the approved ones.
        self.assertEqual(claim.status, Status.PAID)
        self.assertNotEqual(claim.external_id, f"aasrp:{approved.pk}")
        self.assertEqual(claim.external_id, "aasrp:3")

    def test_requests_that_cannot_be_imported_are_counted_by_reason(self):
        self.approved()
        no_killmail = self.approved()
        no_killmail.killmail_id = None
        no_killmail.killboard_link = "nothing here"
        no_ship = self.approved()
        no_ship.ship_id = None
        no_ship.ship = SimpleNamespace(name="")
        no_character = self.approved()
        no_character.character = None

        report = self.run_import()

        self.assertEqual(report.skipped["no_killmail"], 1)
        self.assertEqual(report.skipped["no_ship"], 1)
        self.assertEqual(report.skipped["no_character"], 1)
        self.assertEqual(SrpClaim.objects.count(), 1)

    def test_since_and_until_limit_the_requests(self):
        self.approved(post_time=FLEET_TIME)
        self.approved(post_time=FLEET_TIME + timedelta(days=20))

        output = self.command("--since", "2026-09-15", "--until", "2026-10-30")

        self.assertEqual(SrpClaim.objects.count(), 1)
        self.assertIn("requests read: 1", output)

    def test_a_bad_date_is_an_error(self):
        self.assertIn("--since must be a date", self.command("--since", "yesterday", expect_error=True))

    def test_an_unknown_actor_is_an_error(self):
        self.assertIn("--actor", self.command("--actor", "nobody", expect_error=True))

    def test_every_batch_runs_under_the_quota_lock(self):
        for _ in range(5):
            self.approved()
        with mock.patch.object(quota, "quota_lock", wraps=quota.quota_lock) as lock:
            self.command("--batch-size", "2")

        self.assertEqual(lock.call_count, 3)
        self.assertEqual(SrpClaim.objects.count(), 5)

    def test_the_actor_is_named_in_the_audit(self):
        self.approved()
        self.command("--actor", self.reviewer.username)

        self.assertEqual(AuditLog.objects.get(action="srp.import_aasrp").actor, self.reviewer)

    def test_unmatched_link_names_are_listed_when_a_map_is_given(self):
        f.srp_category("Solo", key="solo")
        other = self.srp.add_link("Corp roam")
        self.srp.add_request(other, self.pilot_char, creator=self.pilot)
        self.approved()
        handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
        self.addCleanup(__import__("os").unlink, handle.name)
        json.dump([{"pattern": "(?i)solo", "category": "solo"}], handle)
        handle.close()

        output = self.command("--dry-run", "--category-map", handle.name)

        self.assertIn("Links no category rule matched", output)
        self.assertIn("Corp roam: 1", output)
        self.assertIn("Home defense fleet: 1", output)

    def test_unmatched_link_names_count_requests_not_links(self):
        f.srp_category("Solo", key="solo")
        other = self.srp.add_link("Corp roam")
        for number in range(3):
            self.srp.add_request(other, self.pilot_char, creator=self.pilot, killmail_id=95_000_000 + number)
        handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
        self.addCleanup(__import__("os").unlink, handle.name)
        json.dump([{"pattern": "(?i)solo", "category": "solo"}], handle)
        handle.close()

        output = self.command("--dry-run", "--category-map", handle.name)

        self.assertIn("Corp roam: 3", output)

    def test_undo_takes_no_filters(self):
        self.approved()
        self.run_import()
        for option in (("--since", "2026-09-01"), ("--until", "2026-12-01"), ("--category-map", "x.json"), ("--batch-size", "10")):
            message = self.command("--undo", *option, expect_error=True)
            self.assertIn("takes no filters", message)
        self.assertEqual(SrpClaim.objects.filter(source="aasrp").count(), 1)

    def test_a_windowed_run_does_not_keep_the_rejected_duplicate_for_good(self):
        km = 91_000_001
        self.approved(killmail_id=km)
        self.approved(status="Rejected", payout=0, killmail_id=km, reject_info="no", post_time=FLEET_TIME + timedelta(days=20))

        self.run_import(since=FLEET_TIME + timedelta(days=10))
        self.assertEqual([(c.external_id, c.status) for c in SrpClaim.objects.filter(source="aasrp")], [("aasrp:2", Status.REJECTED)])

        report = self.run_import()

        self.assertEqual([(c.external_id, c.status) for c in SrpClaim.objects.filter(source="aasrp")], [("aasrp:1", Status.PAID)])
        self.assertEqual(report.update, 1)
        self.assertEqual(self.run_import().unchanged, 1)

    def test_a_request_approved_later_replaces_the_rejected_one_of_the_same_killmail(self):
        km = 91_000_002
        self.approved(status="Rejected", payout=0, killmail_id=km, reject_info="no")
        second = self.approved(status="Pending", killmail_id=km, post_time=FLEET_TIME + timedelta(days=1))
        self.run_import()
        second.request_status = "Approved"

        self.run_import()

        [claim] = SrpClaim.objects.filter(source="aasrp")
        self.assertEqual((claim.external_id, claim.status), ("aasrp:2", Status.PAID))

    def test_the_output_has_no_token_or_webhook(self):
        self.approved()
        output = self.command()
        for word in ("token", "webhook", "refresh", "Bearer"):
            self.assertNotIn(word, output.casefold() if word.islower() else output)

    def test_claims_without_an_account_are_counted(self):
        self.srp.add_request(self.link, f.create_character("Ghost"), decided_by=self.reviewer, decision_comment="ok")
        report = self.run_import()

        self.assertEqual(report.without_user, 1)

