# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""What the import leaves behind: the stand-in killmails that stop a second claim (IMP-5), the claims that cannot be
changed (IMP-6), undoing the import (IMP-7) and completing the stand-ins (IMP-8)."""

from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.test import Client
from django.urls import reverse
from django.utils import timezone

from fleetops import texts
from fleetops.models import AuditLog, SrpClaim, SrpClaimEvent, SrpFleetLink, SrpKillmail, SrpSettings
from fleetops.providers.esi import FleetESIError
from fleetops.providers.esi_killmails import KILLMAIL_NOT_FOUND
from fleetops.srp import claims, importer, killmails, review, summaries
from fleetops.srp import tasks as srp_tasks
from fleetops.srp import values as srp_values
from fleetops.srp.types import SrpError, SrpStateError, ZkillResult

from . import factories as f
from .aasrp_fakes import FLEET_TIME
from .test_srp_import import ImportTestCase

HASH = "ab" * 20
OTHER_HASH = "cd" * 20
REVIEW_AND_PAY = f.SRP_MANAGER_PERMS + ["fleetops.review_srp", "fleetops.pay_srp"]


class PlaceholderTestCase(ImportTestCase):
    def setUp(self):
        super().setUp()
        self.row = self.approved(loss=150_000_000)
        self.run_import(actor=self.reviewer)
        self.claim = self.only_claim()
        self.km = self.claim.killmail
        self.killmail_id = self.km.pk

    def payload(self, **overrides):
        return f.esi_killmail_payload(
            self.killmail_id,
            victim=self.pilot_char,
            time=FLEET_TIME + timedelta(hours=1, minutes=12),
            **overrides,
        )

    def ingame(self, killmail_hash=HASH):
        return f'<a href="killReport:{self.killmail_id}:{killmail_hash}">Rifter</a>'

    def zkill(self):
        return f"https://zkillboard.com/kill/{self.killmail_id}/"


class StandInTests(PlaceholderTestCase):
    def test_a_zkillboard_link_is_answered_as_claimed_without_any_lookup(self):
        with (
            mock.patch.object(killmails, "get_killmail", side_effect=AssertionError("ESI")),
            mock.patch.object(killmails.zkillboard, "fetch_kill", side_effect=AssertionError("zKillboard")),
        ):
            preview = claims.prepare_claim(self.pilot, self.zkill())

        self.assertEqual(preview.existing_claim, self.claim)
        self.assertEqual(preview.blockers, [claims.OWN_DUPLICATE_TEXT])
        self.assertFalse(preview.eligible)

    def test_somebody_elses_paste_is_answered_as_claimed_too(self):
        other = f.create_user("other-pilot")
        preview = claims.prepare_claim(other, self.zkill())

        self.assertEqual(preview.blockers, [claims.OTHER_DUPLICATE_TEXT])

    def test_a_link_without_the_hash_does_not_ask_zkillboard_even_when_it_is_on(self):
        f.srp_settings(zkillboard_enabled=True)
        with mock.patch.object(killmails.zkillboard, "fetch_kill", side_effect=AssertionError("zKillboard")):
            preview = claims.prepare_claim(self.pilot, self.zkill())

        self.assertIsNotNone(preview.existing_claim)

    def test_an_ingame_link_completes_the_stand_in_and_the_claim_follows(self):
        f.srp_settings(zkillboard_enabled=True)
        with (
            mock.patch.object(killmails, "get_killmail", return_value=self.payload()) as read,
            mock.patch.object(killmails, "_names", return_value=("Rifter", "Amamake")),
        ):
            preview = claims.prepare_claim(self.pilot, self.ingame())

        read.assert_called_once_with(self.killmail_id, HASH)
        self.assertEqual(preview.existing_claim, self.claim)
        km = SrpKillmail.objects.get(pk=self.killmail_id)
        self.assertEqual(km.killmail_hash, HASH)
        self.assertEqual(km.killmail_time, FLEET_TIME + timedelta(hours=1, minutes=12))
        self.assertEqual((km.solar_system_id, km.solar_system_name, km.origin), (f.SRP_SYSTEM[0], "Amamake", "aasrp"))
        self.assertTrue(km.attackers)
        claim = SrpClaim.objects.get(pk=self.claim.pk)
        self.assertEqual(claim.killmail_time, FLEET_TIME + timedelta(hours=1, minutes=12))
        self.assertEqual(claim.solar_system_name, "Amamake")
        # Still the amount aa-srp recorded, and no price lookup was queued.
        self.assertEqual((claim.status, claim.approved_amount), ("paid", 150_000_000))
        self.assertEqual((km.zkill_status, km.zkill_next_attempt_at), ("disabled", None))
        entry = AuditLog.objects.get(action="srp.import_enrich")
        self.assertEqual((entry.object_type, entry.object_id), ("SrpKillmail", str(self.killmail_id)))
        self.assertNotIn(HASH, str(entry.new_value))

    def test_a_wrong_hash_is_not_found(self):
        error = FleetESIError("KILLMAIL_NOT_FOUND", KILLMAIL_NOT_FOUND, 404)
        with mock.patch.object(killmails, "get_killmail", side_effect=error):
            with self.assertRaises(SrpError) as caught:
                claims.prepare_claim(self.pilot, self.ingame(OTHER_HASH))

        self.assertEqual((caught.exception.code, caught.exception.message), ("not_found", KILLMAIL_NOT_FOUND))
        self.assertEqual(SrpKillmail.objects.get(pk=self.killmail_id).killmail_hash, "")

    def test_a_killmail_with_another_id_is_not_found(self):
        wrong = f.esi_killmail_payload(self.killmail_id + 1, victim=self.pilot_char)
        with mock.patch.object(killmails, "get_killmail", return_value=wrong):
            with self.assertRaises(SrpError) as caught:
                claims.prepare_claim(self.pilot, self.ingame())

        self.assertEqual(caught.exception.code, "not_found")
        self.assertEqual(SrpKillmail.objects.get(pk=self.killmail_id).killmail_hash, "")

    def test_an_esi_failure_leaves_the_stand_in_as_it_was(self):
        with mock.patch.object(killmails, "get_killmail", side_effect=FleetESIError("ESI_ERROR", "ESI is down")):
            with self.assertRaises(SrpError) as caught:
                claims.prepare_claim(self.pilot, self.ingame())

        self.assertEqual(caught.exception.code, "esi_unavailable")
        self.assertEqual(SrpKillmail.objects.get(pk=self.killmail_id).killmail_hash, "")

    def test_after_the_completion_the_hash_has_to_match(self):
        with (
            mock.patch.object(killmails, "get_killmail", return_value=self.payload()),
            mock.patch.object(killmails, "_names", return_value=("Rifter", "Amamake")),
        ):
            claims.prepare_claim(self.pilot, self.ingame())
        with mock.patch.object(killmails, "get_killmail", side_effect=AssertionError("ESI")):
            same = claims.prepare_claim(self.pilot, self.ingame())
            with self.assertRaises(SrpError) as caught:
                claims.prepare_claim(self.pilot, self.ingame(OTHER_HASH))

        self.assertEqual(same.existing_claim, self.claim)
        self.assertEqual(caught.exception.code, "not_found")

    def test_the_stand_in_is_never_in_the_price_queue(self):
        past = timezone.now() - timedelta(days=1)
        for status in ("pending", "missing", "unavailable", "disabled"):
            with self.subTest(status=status):
                SrpKillmail.objects.filter(pk=self.killmail_id).update(zkill_status=status, zkill_next_attempt_at=past)
                self.assertNotIn(self.killmail_id, [km.pk for km in srp_tasks.due_killmails(timezone.now())])

    def test_the_value_sweep_leaves_the_stand_in_alone(self):
        f.srp_settings(zkillboard_enabled=True)
        SrpKillmail.objects.filter(pk=self.killmail_id).update(zkill_next_attempt_at=timezone.now() - timedelta(days=1))
        with mock.patch.object(killmails.zkillboard, "fetch_kill", side_effect=AssertionError("zKillboard")):
            handled = srp_tasks.refresh_srp_values()

        self.assertEqual(handled, 0)

    def test_schedule_zkill_does_not_wake_a_stand_in(self):
        killmails.schedule_zkill(SrpKillmail.objects.get(pk=self.killmail_id))

        km = SrpKillmail.objects.get(pk=self.killmail_id)
        self.assertEqual((km.zkill_status, km.zkill_next_attempt_at), ("disabled", None))

    def test_a_real_killmail_still_gets_scheduled(self):
        km = f.srp_killmail(self.pilot_char, zkill_status="disabled")
        SrpKillmail.objects.filter(pk=km.pk).update(zkill_next_attempt_at=None)
        km.refresh_from_db()

        killmails.schedule_zkill(km)

        km.refresh_from_db()
        self.assertEqual(km.zkill_status, "pending")
        self.assertIsNotNone(km.zkill_next_attempt_at)

    def test_pasting_completing_and_recalculating_keep_the_stand_in_disabled(self):
        f.srp_settings(zkillboard_enabled=True)
        with (
            mock.patch.object(killmails, "get_killmail", return_value=self.payload()),
            mock.patch.object(killmails, "_names", return_value=("Rifter", "Amamake")),
        ):
            claims.prepare_claim(self.pilot, self.zkill())
            claims.prepare_claim(self.pilot, self.ingame())
        # The reviewer's recalculation refuses an imported claim, and the value update path does not reach it.
        with self.assertRaises(SrpStateError):
            review.recalculate_claim(self.claim, actor=f.create_user("recalc", perms=REVIEW_AND_PAY))
        claims.on_value_updated(SrpKillmail.objects.get(pk=self.killmail_id))

        km = SrpKillmail.objects.get(pk=self.killmail_id)
        self.assertEqual((km.zkill_status, km.zkill_next_attempt_at), ("disabled", None))
        self.assertEqual(SrpClaim.objects.get(pk=self.claim.pk).events.count(), 1)

    def test_an_unclaimed_stand_in_is_not_pruned_while_a_claim_uses_it(self):
        SrpKillmail.objects.filter(pk=self.killmail_id).update(fetched_at=timezone.now() - timedelta(days=30))

        self.assertEqual(killmails.prune_unclaimed(), 0)
        self.assertTrue(SrpKillmail.objects.filter(pk=self.killmail_id).exists())

    def test_a_member_cannot_swap_a_claim_to_an_imported_loss(self):
        mine = f.srp_claim(self.pilot)
        f.srp_settings(member_edit_enabled=True, member_swap_loss_enabled=True)
        ref = killmails.parse_killmail_reference(self.zkill())
        with self.assertRaises(SrpError):
            claims.edit_claim(mine, actor=self.pilot, new_ref=ref)


# --- IMP-6: read only --------------------------------------------------------------------------------------------


def _actions(case):
    """(name, callable(claim), status the guard is tested in) for every action that must refuse an imported claim."""
    manager = case.manager
    claimant = case.pilot
    category = case.category
    return [
        ("approve", lambda c: review.approve_claim(c, actor=manager, amount=1_000_000), "pending"),
        ("approve_amount_change", lambda c: review.approve_claim(c, actor=manager, amount=2_000_000, reason="x"), "approved"),
        ("reject", lambda c: review.reject_claim(c, actor=manager, reason="no"), "pending"),
        ("revoke", lambda c: review.revoke_approval(c, actor=manager, reason="no"), "approved"),
        ("paid", lambda c: review._mark_paid(c, manager, ""), "approved"),
        ("unpaid", lambda c: review.undo_payment(c, actor=manager, reason="no"), "paid"),
        ("reopen", lambda c: review.reopen_claim(c, actor=manager, reason="again"), "rejected"),
        ("reclassify", lambda c: review.reclassify_claim(c, actor=manager, scope="out_of_fleet", reason="x"), "pending"),
        ("recategorize", lambda c: review.recategorize_claim(c, actor=manager, category=category, reason="x"), "pending"),
        ("recalculate", lambda c: review.recalculate_claim(c, actor=manager), "pending"),
        ("recheck", lambda c: review.recheck_presence(c, actor=manager), "pending"),
        ("withdraw", lambda c: claims.withdraw_claim(c, actor=claimant), "pending"),
        ("resubmit", lambda c: claims.resubmit_claim(c, actor=claimant), "withdrawn"),
        ("edit", lambda c: claims.edit_claim(c, actor=claimant, comment="new"), "pending"),
        ("recalculate_under_lock", lambda c: claims.recalculate_under_lock(c, actor=None, reason="x"), "pending"),
    ]


class ReadOnlyTests(PlaceholderTestCase):
    def setUp(self):
        super().setUp()
        self.manager = f.create_user("import-manager", perms=REVIEW_AND_PAY)
        self.category = f.srp_category("Roaming")
        f.srp_settings(member_edit_enabled=True, allow_self_review=True, allow_self_payment=True)

    def state(self):
        return (
            list(SrpClaim.objects.order_by("pk").values()),
            SrpClaimEvent.objects.count(),
            AuditLog.objects.count(),
        )

    def test_every_action_refuses_an_imported_claim_in_every_state_without_a_trace(self):
        for name, action, status in _actions(self):
            with self.subTest(action=name):
                SrpClaim.objects.filter(pk=self.claim.pk).update(status=status)
                claim = SrpClaim.objects.get(pk=self.claim.pk)
                before = self.state()

                with self.assertRaises(SrpStateError) as caught:
                    action(claim)

                self.assertEqual(caught.exception.code, "imported")
                self.assertEqual(caught.exception.message, texts.SRP_IMPORTED_READ_ONLY())
                self.assertEqual(self.state(), before)

    def test_the_bulk_entries_skip_imported_claims_with_the_reason_and_go_on(self):
        native = f.srp_claim(self.pilot, status="approved", amount=5_000_000)
        SrpClaim.objects.filter(pk=self.claim.pk).update(status="approved")
        imported = SrpClaim.objects.get(pk=self.claim.pk)

        paid = review.mark_paid([imported, native], actor=self.manager)
        self.assertEqual([c.pk for c in paid.done], [native.pk])
        self.assertEqual([(c.pk, why) for c, why in paid.skipped], [(imported.pk, texts.SRP_IMPORTED_READ_ONLY())])
        self.assertEqual(SrpClaim.objects.get(pk=self.claim.pk).status, "approved")

        SrpClaim.objects.filter(pk=self.claim.pk).update(status="pending")
        pending = f.srp_claim(self.pilot, amount=3_000_000)
        result = review.approve_clean_claims([SrpClaim.objects.get(pk=self.claim.pk), pending], actor=self.manager)
        self.assertEqual([c.pk for c in result.done], [pending.pk])
        self.assertEqual([c.pk for c, _why in result.skipped], [self.claim.pk])

    def test_recalculating_all_pending_claims_leaves_an_imported_one_alone(self):
        SrpClaim.objects.filter(pk=self.claim.pk).update(status="pending")
        before = list(SrpClaim.objects.filter(pk=self.claim.pk).values())

        done = review.recalculate_pending(actor=self.manager)

        self.assertEqual(done, 0)
        self.assertEqual(list(SrpClaim.objects.filter(pk=self.claim.pk).values()), before)

    def test_an_internal_note_can_still_be_added(self):
        event = review.add_note(self.claim, actor=self.manager, text="checked against the aa-srp page")

        self.assertEqual((event.action, event.internal), ("note", True))

    def test_an_imported_link_does_not_open_again(self):
        from fleetops.srp import links

        link = SrpFleetLink.objects.get(origin="aasrp")
        with self.assertRaises(SrpStateError) as caught:
            links.set_link_open(link, actor=self.manager, is_open=True)
        self.assertEqual(caught.exception.message, texts.SRP_IMPORTED_LINK_CLOSED())
        link.refresh_from_db()
        self.assertFalse(link.is_open)
        # Closing it again, or completing it, is not refused.
        links.set_link_open(link, actor=self.manager, is_open=False)


class ImportedPageTests(PlaceholderTestCase):
    def setUp(self):
        super().setUp()
        self.manager = f.create_user("page-manager", perms=REVIEW_AND_PAY)
        self.client = Client()

    def page(self, user):
        client = Client()
        client.force_login(user)
        return client.get(reverse("fleetops:srp_claim", args=[self.claim.uuid]))

    def test_the_page_shows_the_badge_the_request_and_no_buttons(self):
        response = self.page(self.manager)
        html = response.content.decode()

        self.assertEqual(response.status_code, 200)
        self.assertIn("aa-srp import", html)
        self.assertIn(f"aa-srp request {self.row.pk} (code {self.row.request_code})", html)
        self.assertIn("Amount as recorded in aa-srp", html)
        self.assertIn('id="srp-imported-amounts"', html)
        self.assertNotIn("Fleet presence:", html)
        for action in ("approve", "reject", "revoke", "paid", "unpaid", "reopen", "reclassify", "recategorize", "recalculate"):
            self.assertNotIn(reverse("fleetops:srp_claim_action", args=[self.claim.uuid, action]), html)
        self.assertNotIn("With insurance", html)

    def test_the_time_is_called_an_estimate_until_the_stand_in_is_completed(self):
        self.assertIn('id="srp-time-estimated"', self.page(self.manager).content.decode())
        SrpKillmail.objects.filter(pk=self.killmail_id).update(killmail_hash=HASH)

        self.assertNotIn('id="srp-time-estimated"', self.page(self.manager).content.decode())

    def test_a_reviewer_who_posts_an_action_gets_the_error_and_nothing_changes(self):
        client = Client()
        client.force_login(self.manager)
        before = SrpClaimEvent.objects.count()
        SrpClaim.objects.filter(pk=self.claim.pk).update(status="rejected")

        response = client.post(
            reverse("fleetops:srp_claim_action", args=[self.claim.uuid, "reopen"]), {"reason": "again"}, follow=True
        )

        self.assertContains(response, "imported from aa-srp")
        self.assertEqual(SrpClaimEvent.objects.count(), before)
        self.assertEqual(SrpClaim.objects.get(pk=self.claim.pk).status, "rejected")

    def test_the_claimant_sees_the_claim_in_my_srp_with_the_paid_amount(self):
        member = Client()
        member.force_login(self.pilot)
        f.srp_settings()
        from django.contrib.auth.models import Permission

        self.pilot.user_permissions.add(Permission.objects.get(codename="request_srp"), Permission.objects.get(codename="basic_access"))
        response = member.get(reverse("fleetops:srp_home"), {"year": "2026", "month": "9"})

        self.assertContains(response, self.claim.reference)
        self.assertContains(response, "150,000,000")


# --- IMP-7: undo -------------------------------------------------------------------------------------------------


class UndoTests(ImportTestCase):
    def snapshot(self):
        return {
            "claims": SrpClaim.objects.count(),
            "killmails": SrpKillmail.objects.count(),
            "links": SrpFleetLink.objects.count(),
            "events": SrpClaimEvent.objects.count(),
        }

    def test_undo_returns_every_table_to_its_state_before_and_keeps_native_claims(self):
        native = f.srp_claim(self.pilot, amount=2_000_000)
        native_link = f.standing_link()
        before = self.snapshot()
        self.approved()
        self.approved(status="Rejected", payout=0, reject_info="no")
        self.run_import(actor=self.reviewer)
        imported = self.snapshot()
        self.assertEqual(imported["claims"], before["claims"] + 2)

        counts = importer.undo(self.reviewer)

        self.assertEqual((counts["claims"], counts["killmails"], counts["links"]), (2, 2, 1))
        self.assertEqual(self.snapshot(), before)
        self.assertTrue(SrpClaim.objects.filter(pk=native.pk).exists())
        self.assertTrue(SrpFleetLink.objects.filter(pk=native_link.pk).exists())
        audit = AuditLog.objects.get(action="srp.import_aasrp_undo")
        self.assertEqual((audit.actor, audit.new_value["claims"]), (self.reviewer, 2))

    def test_importing_again_after_an_undo_gives_the_same_result(self):
        self.approved()
        self.approved(status="Rejected", payout=0, reject_info="no")
        self.run_import()
        first = sorted(
            SrpClaim.objects.values_list("external_id", "status", "approved_amount", "suggested_amount", "killmail_time")
        )
        importer.undo(None)

        report = self.run_import()

        second = sorted(
            SrpClaim.objects.values_list("external_id", "status", "approved_amount", "suggested_amount", "killmail_time")
        )
        self.assertEqual(first, second)
        self.assertEqual(report.create, 2)

    def test_a_dry_run_counts_and_deletes_nothing(self):
        self.approved()
        self.run_import()
        before = self.snapshot()

        output = self.command("--undo", "--dry-run")

        self.assertEqual(self.snapshot(), before)
        self.assertIn("Would delete 1 imported claims, 1 imported killmails and 1 imported links.", output)
        self.assertFalse(AuditLog.objects.filter(action="srp.import_aasrp_undo").exists())

    def test_the_command_undoes(self):
        self.approved()
        self.run_import()

        output = self.command("--undo")

        self.assertIn("Deleted 1 imported claims", output)
        self.assertEqual(SrpClaim.objects.count(), 0)

    def test_an_unclaimed_killmail_that_the_import_reused_stays(self):
        row = self.approved()
        km = f.srp_killmail(self.pilot_char, killmail_id=row.killmail_id)
        self.run_import()

        importer.undo(None)

        self.assertTrue(SrpKillmail.objects.filter(pk=km.pk).exists())
        self.assertFalse(SrpClaim.objects.exists())

    def test_undo_still_works_when_aasrp_has_been_removed(self):
        self.approved()
        self.run_import()

        with mock.patch.object(importer.legacy_import, "available", return_value=False):
            output = self.command("--undo")

        self.assertIn("Deleted 1 imported claims", output)
        self.assertEqual(SrpClaim.objects.count(), 0)

    def test_undo_and_enrich_cannot_be_combined(self):
        self.assertIn("cannot be combined", self.command("--undo", "--enrich", expect_error=True))


# --- IMP-8: enrich -----------------------------------------------------------------------------------------------


class EnrichTests(ImportTestCase):
    def setUp(self):
        super().setUp()
        for _ in range(3):
            self.approved()
        self.run_import()
        f.srp_settings(zkillboard_enabled=True)
        self.ids = sorted(SrpKillmail.objects.values_list("pk", flat=True))

    def zkill(self, killmail_id):
        return ZkillResult("ok", killmail_hash=HASH)

    def payload(self, killmail_id, **kwargs):
        return f.esi_killmail_payload(killmail_id, victim=self.pilot_char, time=FLEET_TIME + timedelta(hours=1))

    def patches(self, **extra):
        sleep = mock.Mock()
        patchers = (
            mock.patch.object(importer.zkillboard, "fetch_kill", side_effect=extra.get("fetch", self.zkill)),
            mock.patch.object(importer, "get_killmail", side_effect=extra.get("read", lambda i, h: self.payload(i))),
            mock.patch.object(killmails, "_names", return_value=("Rifter", "Amamake")),
        )
        return sleep, patchers

    def run_enrich(self, **kwargs):
        sleep, patchers = self.patches(**{k: kwargs.pop(k) for k in ("fetch", "read") if k in kwargs})
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)
        return importer.enrich(sleep=sleep, **kwargs), sleep

    def test_every_stand_in_is_completed_one_per_second(self):
        result, sleep = self.run_enrich(rate=1.0, actor=self.reviewer)

        self.assertEqual((result.done, result.failed, result.remaining), (3, 0, 0))
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [1.0, 1.0])
        for km in SrpKillmail.objects.all():
            self.assertEqual((km.killmail_hash, km.origin, km.zkill_status), (HASH, "aasrp", "disabled"))
            self.assertEqual(km.claim.killmail_time, km.killmail_time)
        self.assertEqual(AuditLog.objects.filter(action="srp.import_enrich", actor=self.reviewer).count(), 3)

    def test_the_rate_sets_the_pause(self):
        _result, sleep = self.run_enrich(rate=4)

        self.assertEqual([call.args[0] for call in sleep.call_args_list], [0.25, 0.25])

    def test_the_limit_stops_early_and_the_next_run_goes_on(self):
        result, _sleep = self.run_enrich(limit=2)
        self.assertEqual((result.done, result.remaining), (2, 1))

        result, _sleep = self.run_enrich()
        self.assertEqual((result.done, result.remaining), (1, 0))

    def test_a_killmail_that_cannot_be_read_waits_for_the_next_run(self):
        def read(killmail_id, killmail_hash):
            if killmail_id == self.ids[1]:
                raise FleetESIError("KILLMAIL_NOT_FOUND", KILLMAIL_NOT_FOUND, 404)
            return self.payload(killmail_id)

        result, _sleep = self.run_enrich(read=read)

        self.assertEqual((result.done, result.failed, result.remaining), (2, 1, 1))
        self.assertEqual(SrpKillmail.objects.get(pk=self.ids[1]).killmail_hash, "")

    def test_a_missing_hash_counts_as_failed_and_throttling_stops_the_run(self):
        result, _sleep = self.run_enrich(fetch=lambda i: ZkillResult("missing"))
        self.assertEqual((result.done, result.failed), (0, 3))

        result, _sleep = self.run_enrich(fetch=lambda i: ZkillResult("throttled"))
        self.assertEqual((result.done, result.failed), (0, 1))
        self.assertIn("slow down", result.stopped)

    def test_an_esi_rate_limit_stops_the_run(self):
        result, _sleep = self.run_enrich(read=lambda i, h: (_ for _ in ()).throw(FleetESIError("ESI_LIMITED", "slow down")))

        self.assertEqual(result.failed, 1)
        self.assertIn("ESI asked to slow down", result.stopped)

    def test_without_zkillboard_the_command_refuses(self):
        f.srp_settings(zkillboard_enabled=False)

        self.assertIn("zKillboard is switched off", self.command("--enrich", expect_error=True))

    def test_the_command_reports_and_a_dry_run_counts(self):
        self.assertIn("Would look up 3 killmails.", self.command("--enrich", "--dry-run"))
        sleep, patchers = self.patches()
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)
        with mock.patch.object(importer.time_module, "sleep"):
            output = self.command("--enrich", "--limit", "1", "--rate", "100")

        self.assertIn("Completed 1 killmails; 0 could not be read; 2 still wait.", output)
