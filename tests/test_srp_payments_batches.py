# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""REV-4 to REV-6: the payments page by category and link, payment batches and the export of the claims to pay."""

from datetime import timedelta
from unittest import mock

from django.urls import reverse
from django.utils import timezone

from fleetops.models import AuditLog, SrpClaim, SrpClaimEvent, SrpPaymentBatch, SrpSettings
from fleetops.srp import payments, review
from fleetops.srp.types import SrpError
from fleetops.tasks import prune_attendance_history

from . import factories as f
from .test_export_datasets import csv_rows
from .test_srp_views import SrpPagesTestCase, messages_of, route

Status = SrpClaim.Status
Action = SrpClaimEvent.Action


class PaymentsBase(SrpPagesTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.payer = f.create_user("srppayer", perms=f.SRP_PAYER_PERMS)
        cls.payer2 = f.create_user("srppayertwo", perms=f.SRP_PAYER_PERMS)
        cls.reviewer = f.create_user("srpreviewer", perms=f.SRP_REVIEWER_PERMS)
        cls.third = f.create_user("thirdpilot", perms=f.SRP_MEMBER_PERMS)

    def approved(self, user=None, amount=10_000_000, **extra):
        return f.srp_claim(user or self.member, status=Status.APPROVED, amount=amount, decided_by=self.manager, **extra)

    def pay(self, claims, actor=None, **kwargs):
        return review.mark_paid(claims, actor=actor or self.payer, **kwargs)


class BatchTests(PaymentsBase):
    def test_a_payment_of_five_claims_for_three_players_makes_one_batch(self):
        claims = [
            self.approved(self.member, 10_000_000),
            self.approved(self.member, 20_000_000),
            self.approved(self.other, 30_000_000),
            self.approved(self.other, 5_000_000),
            self.approved(self.third, 7_000_000),
        ]

        result = self.pay(claims, note="Round one")

        self.assertEqual(len(result.done), 5)
        batch = SrpPaymentBatch.objects.get()
        self.assertEqual(result.batch, batch)
        self.assertEqual((batch.claim_count, batch.payee_count, batch.total), (5, 3, 72_000_000))
        self.assertEqual((batch.created_by, batch.note), (self.payer, "Round one"))
        self.assertEqual(batch.reference, f"PAY-{batch.pk}")
        self.assertEqual(set(SrpClaim.objects.filter(payment_batch=batch).values_list("pk", flat=True)), {c.pk for c in claims})
        events = SrpClaimEvent.objects.filter(action=Action.PAID)
        self.assertEqual({event.data["batch"] for event in events}, {batch.pk})
        self.assertEqual(events.count(), 5)
        entries = AuditLog.objects.filter(action="srp.payment_batch")
        self.assertEqual(entries.count(), 1)
        entry = entries.get()
        self.assertEqual(entry.new_value, {"count": 5, "payees": 3, "total": 72_000_000, "note_length": 9})
        self.assertEqual((entry.reason, entry.actor), ("Round one", self.payer))

    def test_a_payment_where_everything_is_skipped_leaves_no_batch(self):
        own = self.approved(self.payer)
        pending = f.srp_claim(self.member)

        result = self.pay([own, pending])

        self.assertEqual(result.done, [])
        self.assertEqual(len(result.skipped), 2)
        self.assertIsNone(result.batch)
        self.assertEqual(SrpPaymentBatch.objects.count(), 0)
        self.assertFalse(AuditLog.objects.filter(action="srp.payment_batch").exists())

    def test_a_single_payment_that_is_refused_leaves_no_batch(self):
        claim = self.approved(self.payer)
        client = self.client_for(self.payer)
        response = client.post(route("srp_claim_action", claim.uuid, "paid"), {"shown_amount": claim.approved_amount})
        self.assertIn(response.status_code, (302, 403))
        self.assertEqual(SrpPaymentBatch.objects.count(), 0)

    def test_a_single_payment_makes_a_batch_of_one(self):
        claim = self.approved()
        client = self.client_for(self.payer)

        response = client.post(
            route("srp_claim_action", claim.uuid, "paid"), {"shown_amount": claim.approved_amount}, follow=True
        )

        batch = SrpPaymentBatch.objects.get()
        self.assertEqual((batch.claim_count, batch.payee_count, batch.total), (1, 1, claim.approved_amount))
        claim.refresh_from_db()
        self.assertEqual(claim.payment_batch, batch)
        self.assertIn(f"Payment batch {batch.reference}.", messages_of(response))

    def test_the_rules_of_the_payer_apply_to_every_claim_of_the_batch(self):
        mine = self.approved(self.payer)
        approved_by_me = f.srp_claim(self.member, status=Status.APPROVED, decided_by=self.payer2)
        fine = self.approved(self.other)
        SrpSettings.objects.filter(pk=1).update(require_different_payer=True)

        result = self.pay([mine, approved_by_me, fine], actor=self.payer2)

        self.assertEqual([claim.pk for claim in result.done], [mine.pk, fine.pk])
        self.assertEqual([claim.pk for claim, _text in result.skipped], [approved_by_me.pk])
        batch = SrpPaymentBatch.objects.get()
        self.assertEqual(batch.claim_count, 2)

    def test_undoing_a_payment_keeps_the_numbers_of_the_batch_and_names_it_in_the_event(self):
        first, second = self.approved(), self.approved(self.other)
        self.pay([first, second])
        batch = SrpPaymentBatch.objects.get()

        review.undo_payment(first, actor=self.payer, reason="Not sent")

        first.refresh_from_db()
        batch.refresh_from_db()
        self.assertIsNone(first.payment_batch)
        self.assertEqual((batch.claim_count, batch.total), (2, 20_000_000))
        self.assertEqual(first.events.get(action=Action.UNPAID).data, {"batch": batch.pk})

    def test_a_payment_that_stops_halfway_keeps_the_batch_of_the_claims_paid(self):
        first, second = self.approved(), self.approved(self.other)
        real = review._mark_paid
        calls = []

        def broken(claim, *args, **kwargs):
            calls.append(claim.pk)
            if len(calls) == 2:
                raise RuntimeError("database went away")
            return real(claim, *args, **kwargs)

        with mock.patch.object(review, "_mark_paid", broken):
            with self.assertRaises(RuntimeError):
                self.pay([first, second])

        batch = SrpPaymentBatch.objects.get()
        self.assertEqual((batch.claim_count, batch.total), (1, 10_000_000))

    def test_a_payment_that_stops_before_any_claim_leaves_no_batch(self):
        claim = self.approved()
        with mock.patch.object(review, "_mark_paid", side_effect=RuntimeError("gone")):
            with self.assertRaises(RuntimeError):
                self.pay([claim])
        self.assertEqual(SrpPaymentBatch.objects.count(), 0)

    def test_the_note_is_limited_before_a_batch_is_made(self):
        claim = self.approved()
        with self.assertRaises(SrpError):
            self.pay([claim], note="x" * 300)
        self.assertEqual(SrpPaymentBatch.objects.count(), 0)

    def test_the_filters_are_kept_with_the_batch(self):
        category = f.srp_category("Roams")
        claim = self.approved(category=category)
        self.pay([claim], filters={"category": str(category.pk), "link": "abc", "extra": "ignored"})
        self.assertEqual(SrpPaymentBatch.objects.get().filters, {"category": str(category.pk), "link": "abc"})


class BatchCleanupAndListTests(PaymentsBase):
    def test_old_empty_batches_are_deleted_and_the_others_stay(self):
        old_empty = SrpPaymentBatch.objects.create(created_by=self.payer)
        recent_empty = SrpPaymentBatch.objects.create(created_by=self.payer)
        paid = SrpPaymentBatch.objects.create(created_by=self.payer, claim_count=1, total=1)
        has_claims = SrpPaymentBatch.objects.create(created_by=self.payer)
        self.approved(payment_batch=has_claims)
        for batch in (old_empty, paid, has_claims):
            SrpPaymentBatch.objects.filter(pk=batch.pk).update(created_at=timezone.now() - timedelta(hours=2))

        deleted = payments.prune_empty_batches()

        self.assertEqual(deleted, 1)
        self.assertEqual(
            set(SrpPaymentBatch.objects.values_list("pk", flat=True)), {recent_empty.pk, paid.pk, has_claims.pk}
        )

    def test_a_batch_cut_off_after_it_paid_claims_is_completed_by_the_cleanup(self):
        batch = SrpPaymentBatch.objects.create(created_by=self.payer)
        first = self.approved(self.member, 10_000_000)
        second = self.approved(self.other, 5_000_000)
        for claim in (first, second):
            SrpClaim.objects.filter(pk=claim.pk).update(status=Status.PAID, payment_batch=batch)
        young = SrpPaymentBatch.objects.create(created_by=self.payer)
        SrpClaim.objects.filter(pk=self.approved().pk).update(payment_batch=young)
        SrpPaymentBatch.objects.filter(pk=batch.pk).update(created_at=timezone.now() - timedelta(hours=2))
        self.assertNotIn(batch, payments.recent_batches())

        payments.prune_empty_batches()

        batch.refresh_from_db()
        self.assertEqual((batch.claim_count, batch.payee_count, batch.total), (2, 2, 15_000_000))
        self.assertIn(batch, payments.recent_batches())
        young.refresh_from_db()
        self.assertEqual(young.claim_count, 0)  # a payment that may still be running is left alone

    def test_the_paid_date_filter_survives_the_last_day_of_the_calendar(self):
        page = self.client_for(self.payer).get(route("srp_payments"), {"paid_from": "9999-12-31", "paid_to": "9999-12-31"})
        self.assertEqual(page.status_code, 200)

    def test_the_daily_cleanup_task_prunes_them(self):
        old_empty = SrpPaymentBatch.objects.create(created_by=self.payer)
        SrpPaymentBatch.objects.filter(pk=old_empty.pk).update(created_at=timezone.now() - timedelta(hours=3))
        prune_attendance_history()
        self.assertFalse(SrpPaymentBatch.objects.filter(pk=old_empty.pk).exists())

    def test_the_recent_list_has_the_last_30_days_batches_with_claims_only_and_at_most_50(self):
        now = timezone.now()
        old = SrpPaymentBatch.objects.create(created_by=self.payer, claim_count=1)
        SrpPaymentBatch.objects.filter(pk=old.pk).update(created_at=now - timedelta(days=31))
        SrpPaymentBatch.objects.create(created_by=self.payer, claim_count=0)
        SrpPaymentBatch.objects.bulk_create(SrpPaymentBatch(created_by=self.payer, claim_count=1) for _ in range(55))

        shown = payments.recent_batches()

        self.assertEqual(len(shown), 50)
        self.assertNotIn(old, shown)
        self.assertTrue(all(batch.claim_count for batch in shown))


class PaymentsPageTests(PaymentsBase):
    def setUp(self):
        super().setUp()
        self.client = self.client_for(self.payer)
        self.solo = f.srp_category("Solo PVP")
        self.home = f.srp_category("Home defense")
        self.standing = f.standing_link(category=self.home)

    def rows(self, response):
        return [claim.reference for row in response.context["pay_rows"] for claim in row.claims]

    def test_the_filters_narrow_the_list_and_the_group_totals(self):
        a = self.approved(self.member, 10_000_000, category=self.solo)
        b = self.approved(self.member, 20_000_000, category=self.home, link=self.standing)
        c = self.approved(self.other, 40_000_000, category=self.home)
        outsider = f.create_user("outsider", perms=f.SRP_MEMBER_PERMS, corporation=f.OTHER_CORP)
        d = self.approved(outsider, 80_000_000)

        page = self.client.get(route("srp_payments"))
        self.assertEqual(set(self.rows(page)), {a.reference, b.reference, c.reference, d.reference})

        by_category = self.client.get(route("srp_payments"), {"category": self.home.pk})
        self.assertEqual(set(self.rows(by_category)), {b.reference, c.reference})
        self.assertEqual(by_category.context["to_pay_total"], 60_000_000)
        member_row = next(row for row in by_category.context["pay_rows"] if row.user_id == self.member.pk)
        self.assertEqual(member_row.total, 20_000_000)

        by_link = self.client.get(route("srp_payments"), {"link": self.standing.code})
        self.assertEqual(self.rows(by_link), [b.reference])
        by_corporation = self.client.get(route("srp_payments"), {"corporation": f.OTHER_CORP[0]})
        self.assertEqual(self.rows(by_corporation), [d.reference])
        none = self.client.get(route("srp_payments"), {"category": "none"})
        self.assertEqual(set(self.rows(none)), {d.reference})

    def test_invalid_filters_are_ignored(self):
        self.approved()
        for params in ({"category": "abc"}, {"corporation": "x"}, {"link": "x" * 100}, {"category": "999999"}):
            with self.subTest(params=params):
                response = self.client.get(route("srp_payments"), params)
                self.assertEqual(response.status_code, 200)

    def test_the_group_header_copies_name_and_amount_separated_by_a_tab(self):
        self.approved(self.member, 12_345_678)
        page = self.client.get(route("srp_payments"))
        name = f.main_of(self.member).character_name
        self.assertContains(page, f'data-srp-copy="{name}&#9;12345678"')

    def test_the_filtered_totals_are_named_as_such(self):
        self.approved(category=self.home)
        plain = self.client.get(route("srp_payments"))
        filtered = self.client.get(route("srp_payments"), {"category": self.home.pk})
        self.assertNotContains(plain, "by the current filters")
        self.assertContains(filtered, "by the current filters")

    def test_the_pay_form_carries_the_filters_and_the_batch_remembers_them(self):
        claim = self.approved(category=self.home)
        self.approved(self.other, category=self.solo)
        page = self.client.get(route("srp_payments"), {"category": self.home.pk})
        self.assertContains(page, f'<input type="hidden" name="category" value="{self.home.pk}">')

        response = self.client.post(
            route("srp_payments"),
            {
                "claim": [str(claim.uuid)],
                f"shown-{claim.uuid}": str(claim.approved_amount),
                "category": str(self.home.pk),
                "next": route("srp_payments") + f"?category={self.home.pk}",
            },
            follow=True,
        )

        batch = SrpPaymentBatch.objects.get()
        self.assertEqual(batch.filters, {"category": str(self.home.pk)})
        self.assertIn(f"Payment batch {batch.reference}.", messages_of(response))
        self.assertEqual(response.redirect_chain[-1][0].split("?")[-1], f"category={self.home.pk}")

    def test_the_paid_list_shows_the_batch_and_a_dash_for_older_payments(self):
        old = f.srp_claim(self.member, status=Status.PAID, decided_by=self.manager)
        new = self.approved(self.other)
        self.pay([new])
        page = self.client.get(route("srp_payments"))
        batch = SrpPaymentBatch.objects.get()
        self.assertContains(page, f'href="{route("srp_payment_batch", batch.uuid)}"')
        self.assertContains(page, "(before batches)")
        self.assertIn(old.reference, [claim.reference for claim in page.context["paid_claims"]])

    def test_the_recent_batches_card_lists_the_batch(self):
        self.pay([self.approved()])
        page = self.client.get(route("srp_payments"))
        batch = SrpPaymentBatch.objects.get()
        self.assertEqual(page.context["recent_batches"], [batch])
        self.assertContains(page, batch.reference)

    def test_the_page_costs_the_same_queries_for_few_and_many_claims(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        def cost():
            with CaptureQueriesContext(connection) as queries:
                self.assertEqual(self.client.get(route("srp_payments")).status_code, 200)
            return len(queries)

        for index in range(3):
            self.approved(self.member if index % 2 else self.other, category=self.solo, link=self.standing)
        cost()
        small = cost()
        for index in range(25):
            self.approved(self.member if index % 2 else self.third, category=self.home, link=self.standing)
        self.assertEqual(cost(), small)


class BatchPageTests(PaymentsBase):
    def setUp(self):
        super().setUp()
        self.first, self.second = self.approved(self.member, 10_000_000), self.approved(self.other, 30_000_000)
        self.pay([self.first, self.second], note="Friday")
        self.batch = SrpPaymentBatch.objects.get()
        self.url = route("srp_payment_batch", self.batch.uuid)

    def test_a_payer_sees_the_batch(self):
        page = self.client_for(self.payer).get(self.url)
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, self.batch.reference)
        self.assertContains(page, self.first.reference)
        self.assertContains(page, "Friday")
        self.assertContains(page, "#undo-payment")

    def test_a_reviewer_gets_a_403_and_a_member_too(self):
        for user in (self.reviewer, self.member, self.stats):
            with self.subTest(user=user.username):
                self.assertEqual(self.client_for(user).get(self.url).status_code, 403)

    def test_a_manager_sees_it_and_an_unknown_batch_is_a_404(self):
        self.assertEqual(self.client_for(self.manager).get(self.url).status_code, 200)
        missing = route("srp_payment_batch", "00000000-0000-0000-0000-000000000000")
        self.assertEqual(self.client_for(self.payer).get(missing).status_code, 404)

    def test_the_page_counts_the_payments_undone_since(self):
        review.undo_payment(self.first, actor=self.payer, reason="Not sent")
        page = self.client_for(self.payer).get(self.url)
        self.assertEqual(page.context["undone"], 1)
        self.assertContains(page, "was paid back to approved")
        self.assertNotContains(page, self.first.reference)
        self.batch.refresh_from_db()
        self.assertEqual(self.batch.claim_count, 2)


class PayableExportTests(PaymentsBase):
    def download(self, user, fmt="csv", **params):
        client = self.client_for(user)
        return client.get(reverse("fleetops:export", args=["srp-payable"]), {"format": fmt, **params})

    def test_the_export_total_is_the_amount_on_the_page(self):
        self.approved(self.member, 10_000_000)
        self.approved(self.member, 15_000_000)
        self.approved(self.other, 30_000_000)
        page = self.client_for(self.payer).get(route("srp_payments"))

        rows = csv_rows(self.download(self.payer))

        self.assertEqual(rows[0][0], "Pay to")
        self.assertEqual(rows[-1][0], "Total")
        self.assertEqual(int(rows[-1][-1]), page.context["to_pay_total"])
        self.assertEqual(len(rows), 1 + 3 + 1)

    def test_the_players_view_has_one_row_per_player(self):
        self.approved(self.member, 10_000_000)
        self.approved(self.member, 15_000_000)
        self.approved(self.other, 30_000_000)

        rows = csv_rows(self.download(self.payer, group="player"))

        self.assertEqual(rows[0], ["Pay to", "Character ID", "Claims", "Approved (ISK)"])
        by_name = {row[0]: row for row in rows[1:-1]}
        self.assertEqual(by_name[f.main_of(self.member).character_name][2:], ["2", "25000000"])
        self.assertEqual(rows[-1][-1], "55000000")

    def test_the_filters_of_the_page_narrow_the_export(self):
        home = f.srp_category("Home defense")
        self.approved(self.member, 10_000_000, category=home)
        self.approved(self.other, 30_000_000)

        rows = csv_rows(self.download(self.payer, category=str(home.pk)))

        self.assertEqual(int(rows[-1][-1]), 10_000_000)
        self.assertEqual(len(rows), 3)

    def test_a_picked_month_narrows_the_export_like_the_page(self):
        may = timezone.now().replace(year=2026, month=5, day=10)
        june = may.replace(month=6)
        self.approved(self.member, 10_000_000, time=may)
        self.approved(self.member, 15_000_000, time=may)
        self.approved(self.other, 30_000_000, time=june)
        page = self.client_for(self.payer).get(route("srp_payments"), {"year": 2026, "month": 5})
        self.assertEqual(page.context["to_pay_total"], 25_000_000)

        link = page.context["export_query"]
        self.assertIn("year=2026", link)
        rows = csv_rows(self.download(self.payer, year="2026", month="5"))
        players = csv_rows(self.download(self.payer, group="player", year="2026", month="5"))

        self.assertEqual(int(rows[-1][-1]), page.context["to_pay_total"])
        self.assertEqual(int(players[-1][-1]), page.context["to_pay_total"])
        self.assertEqual(len(rows), 1 + 2 + 1)
        # No month picked: every month, as the page.
        self.assertEqual(int(csv_rows(self.download(self.payer))[-1][-1]), 55_000_000)

    def test_a_list_cut_at_the_limit_says_so(self):
        for _ in range(3):
            self.approved(self.member, 1_000_000)
        for params in ({}, {"group": "player"}):
            with self.subTest(params=params):
                with mock.patch("fleetops.services.exports.MAX_EXPORT_ROWS", 2):
                    rows = csv_rows(self.download(self.payer, **params))
                notes = [row[0] for row in rows if row[0].startswith("Export truncated")]
                self.assertEqual(notes, ["Export truncated at 2 rows. Choose a shorter period."])
        # Exactly at the limit is not a cut.
        with mock.patch("fleetops.services.exports.MAX_EXPORT_ROWS", 3):
            rows = csv_rows(self.download(self.payer))
        self.assertFalse([row for row in rows if row[0].startswith("Export truncated")])
        self.assertEqual(int(rows[-1][-1]), 3_000_000)

    def test_only_approved_claims_are_listed(self):
        f.srp_claim(self.member)
        f.srp_claim(self.member, status=Status.PAID, decided_by=self.manager)
        self.approved(self.other, 1_000_000)
        rows = csv_rows(self.download(self.payer))
        self.assertEqual(int(rows[-1][-1]), 1_000_000)

    def test_a_reviewer_and_a_viewer_of_all_statistics_get_a_403(self):
        self.approved()
        for user in (self.reviewer, self.stats, self.member):
            with self.subTest(user=user.username):
                self.assertEqual(self.download(user).status_code, 403)

    def test_a_payer_and_a_manager_can_download_and_each_download_is_audited(self):
        self.approved()
        for user in (self.payer, self.manager):
            self.assertEqual(self.download(user).status_code, 200)
        self.assertEqual(AuditLog.objects.filter(action="srp.export", object_id__startswith="srp-payable").count(), 2)
        self.assertEqual(self.download(self.payer, "xlsx").status_code, 200)

    def test_the_claims_export_names_the_batch_for_staff_only(self):
        claim = self.approved()
        self.pay([claim])
        batch = SrpPaymentBatch.objects.get()
        year, month = claim.killmail_time.year, claim.killmail_time.month

        staff = csv_rows(self.client_for(self.manager).get(
            reverse("fleetops:export", args=["srp-claims"]), {"format": "csv", "year": year, "month": month}
        ))
        viewer = csv_rows(self.client_for(self.stats).get(
            reverse("fleetops:export", args=["srp-claims"]), {"format": "csv", "year": year, "month": month}
        ))

        self.assertEqual(staff[0][-1], "Payment batch")
        self.assertEqual(staff[1][-1], batch.reference)
        self.assertEqual(viewer[1][-1], "")
