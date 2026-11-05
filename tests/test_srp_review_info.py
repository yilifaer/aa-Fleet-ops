# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""REV-3 and REV-9: the review queue filters and what the claim page tells SRP staff (attackers, the limits as they are
now, the history of rejections), and the backfill command."""

import io
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.core.management import call_command
from django.db import connection
from django.test import SimpleTestCase, TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from fleetops.models import EveEntityName, SrpClaim, SrpKillmail
from fleetops.providers.esi import FleetESIError
from fleetops.services import names
from fleetops.srp import attackers, killmails, review, review_info
from fleetops.srp.types import PolicySnapshot, SrpLimitError

from . import factories as f
from .test_srp_review_views import ManagerPagesTestCase, refs
from .test_srp_views import SrpPagesTestCase, route

Status = SrpClaim.Status


def attacker(character_id=None, damage=0, *, final=False, corporation=None, alliance=None, ship=None, weapon=None, **extra):
    return {
        "character_id": character_id,
        "corporation_id": corporation,
        "alliance_id": alliance,
        "ship_type_id": ship,
        "weapon_type_id": weapon,
        "damage_done": damage,
        "final_blow": final,
        **extra,
    }


class ParseAttackersTests(SimpleTestCase):
    def test_the_stored_fields_are_ids_damage_and_the_final_blow(self):
        rows = killmails.clean_attackers(
            [
                {
                    "character_id": 9001,
                    "corporation_id": 8001,
                    "alliance_id": 7001,
                    "faction_id": 500001,
                    "ship_type_id": 587,
                    "weapon_type_id": 2873,
                    "damage_done": 1200,
                    "final_blow": True,
                    "security_status": -3.1,
                }
            ]
        )
        self.assertEqual(
            rows,
            [
                {
                    "character_id": 9001,
                    "corporation_id": 8001,
                    "alliance_id": 7001,
                    "faction_id": 500001,
                    "ship_type_id": 587,
                    "weapon_type_id": 2873,
                    "damage_done": 1200,
                    "final_blow": True,
                }
            ],
        )

    def test_missing_fields_and_wrong_types_never_fail_the_killmail(self):
        rows = killmails.clean_attackers(
            [
                "not an object",
                None,
                {"character_id": "9001", "corporation_id": True, "damage_done": "lots", "final_blow": "yes"},
                {"damage_done": -5},
                {},
            ]
        )
        self.assertEqual(len(rows), 3)
        for row in rows:
            self.assertEqual(set(row), set(killmails.ATTACKER_ID_FIELDS) | {"damage_done", "final_blow"})
            self.assertEqual((row["character_id"], row["corporation_id"], row["damage_done"], row["final_blow"]), (None, None, 0, False))

    def test_something_that_is_not_a_list_gives_nothing(self):
        for value in (None, {}, "x", 5):
            self.assertEqual(killmails.clean_attackers(value), [])

    def test_the_most_damage_comes_first_and_at_most_fifty_are_kept(self):
        rows = killmails.clean_attackers([attacker(index + 1, damage=index) for index in range(80)])
        self.assertEqual(len(rows), killmails.MAX_ATTACKERS_STORED)
        self.assertEqual([row["damage_done"] for row in rows][:3], [79, 78, 77])

    def test_the_final_blow_stays_even_when_it_did_the_least_damage(self):
        many = [attacker(index + 1, damage=100 + index) for index in range(60)]
        many.append(attacker(9999, damage=1, final=True))
        rows = killmails.clean_attackers(many)
        self.assertEqual(len(rows), killmails.MAX_ATTACKERS_STORED)
        self.assertIn(9999, [row["character_id"] for row in rows])
        self.assertEqual(sum(row["final_blow"] for row in rows), 1)

    def test_a_final_blow_that_is_in_the_top_is_not_doubled(self):
        many = [attacker(index + 1, damage=100 + index, final=index == 59) for index in range(60)]
        rows = killmails.clean_attackers(many)
        self.assertEqual(len(rows), 50)
        self.assertEqual(len({row["character_id"] for row in rows}), 50)

    def test_the_killmail_fields_carry_them_and_the_damage_taken(self):
        payload = f.esi_killmail_payload(victim=95_000_100)
        fields = killmails.killmail_fields(payload)
        self.assertEqual(fields["damage_taken"], 2345)
        self.assertEqual(len(fields["attackers"]), 2)
        self.assertEqual(fields["attacker_count"], 2)

    def test_a_damage_taken_that_is_not_a_number_is_none(self):
        payload = f.esi_killmail_payload(victim=95_000_100)
        payload["victim"]["damage_taken"] = "plenty"
        self.assertIsNone(killmails.killmail_fields(payload)["damage_taken"])
        payload["victim"]["damage_taken"] = True
        self.assertIsNone(killmails.killmail_fields(payload)["damage_taken"])


class AttackerNameTests(TestCase):
    def test_cached_names_never_call_esi(self):
        EveEntityName.objects.create(id=111, category="character", name="Alpha", updated_at=timezone.now())
        EveEntityName.objects.create(id=222, category="corporation", name="", updated_at=timezone.now())
        with mock.patch.object(names.esi, "resolve_names", side_effect=AssertionError("ESI")) as lookup:
            found = names.cached_entity_names([111, 222, 333, 111, 0])
        self.assertEqual(found, {111: "Alpha"})
        lookup.assert_not_called()

    def test_the_summary_reads_the_cache_only(self):
        km = f.srp_killmail(attackers=[attacker(1, 10, final=True, corporation=2)], damage_taken=10, attacker_count=1)
        with mock.patch.object(names.esi, "resolve_names", side_effect=AssertionError("ESI")):
            with mock.patch.object(names, "entity_names", side_effect=AssertionError("ESI")):
                summary = attackers.summary(km)
        self.assertEqual(summary["final_blow"]["character"], "1")

    def test_a_stored_killmail_queues_the_name_lookup_after_the_commit(self):
        km = f.srp_killmail(attackers=[attacker(1, 10)], attacker_count=1)
        with mock.patch("fleetops.tasks.warm_srp_killmail_names.delay") as delay:
            with self.captureOnCommitCallbacks(execute=True):
                attackers.queue_name_warming(km)
        delay.assert_called_once_with(km.pk)

    def test_a_killmail_without_attackers_queues_nothing(self):
        km = f.srp_killmail()
        with mock.patch("fleetops.tasks.warm_srp_killmail_names.delay") as delay:
            with self.captureOnCommitCallbacks(execute=True):
                attackers.queue_name_warming(km)
        delay.assert_not_called()

    def test_a_broker_problem_is_logged_and_ignored(self):
        km = f.srp_killmail(attackers=[attacker(1, 10)], attacker_count=1)
        with mock.patch("fleetops.tasks.warm_srp_killmail_names.delay", side_effect=OSError("broker")):
            with self.captureOnCommitCallbacks(execute=True):
                attackers.queue_name_warming(km)

    def test_the_worker_warms_every_id_once(self):
        km = f.srp_killmail(
            attackers=[attacker(1, 10, corporation=2, alliance=3, ship=4, weapon=5), attacker(None, 5, corporation=2)],
            attacker_count=2,
        )
        with mock.patch.object(names, "entity_names", return_value={}) as lookup:
            count = attackers.warm_names(km.pk)
        self.assertEqual(count, 5)
        self.assertEqual(sorted(lookup.call_args.args[0]), [1, 2, 3, 4, 5])

    def test_a_failed_lookup_does_not_stop_the_task(self):
        from fleetops import tasks

        km = f.srp_killmail(attackers=[attacker(1, 10)], attacker_count=1)
        with mock.patch.object(names, "entity_names", side_effect=FleetESIError("ESI_ERROR", "down")):
            self.assertEqual(tasks.warm_srp_killmail_names(km.pk), 1)
        self.assertEqual(tasks.warm_srp_killmail_names(0), 0)

    def test_storing_a_killmail_keeps_the_attackers(self):
        payload = f.esi_killmail_payload(555_001, victim=f.create_character())
        with mock.patch.object(killmails.universe, "system_infos", return_value={}), mock.patch.object(
            killmails.universe, "type_groups", return_value={}
        ), mock.patch.object(killmails.sde, "item_type_names", return_value={}), mock.patch.object(
            killmails.sde, "solar_system_names", return_value={}
        ), mock.patch.object(killmails.names, "entity_names", return_value={}):
            km = killmails.store_killmail(payload, "a" * 40)
        km.refresh_from_db()
        self.assertEqual((len(km.attackers), km.damage_taken), (2, 2345))


class SummaryTests(TestCase):
    def test_counts_final_blow_top_damage_and_the_biggest_groups(self):
        EveEntityName.objects.bulk_create(
            [
                EveEntityName(id=1, category="character", name="Alpha", updated_at=timezone.now()),
                EveEntityName(id=2, category="character", name="Bravo", updated_at=timezone.now()),
                EveEntityName(id=10, category="corporation", name="Corp Ten", updated_at=timezone.now()),
                EveEntityName(id=20, category="alliance", name="Alliance Twenty", updated_at=timezone.now()),
                EveEntityName(id=30, category="inventory_type", name="Punisher", updated_at=timezone.now()),
            ]
        )
        km = f.srp_killmail(
            attacker_count=5,
            damage_taken=1000,
            zkill_solo=False,
            zkill_npc=False,
            zkill_awox=True,
            attackers=[
                attacker(1, 600, corporation=10, alliance=20, ship=30),
                attacker(2, 300, final=True, corporation=10, alliance=20),
                attacker(3, 50, corporation=11),
                attacker(4, 40, corporation=11),
                attacker(None, 10, corporation=1000125),
            ],
        )

        summary = attackers.summary(km)

        self.assertEqual((summary["total"], summary["players"], summary["npcs"], summary["truncated"]), (5, 4, 1, False))
        self.assertEqual(summary["final_blow"]["character"], "Bravo")
        self.assertEqual(summary["top_damage"]["character"], "Alpha")
        self.assertEqual((summary["top_damage"]["damage"], summary["top_damage"]["percent"]), (600, 60))
        self.assertEqual(summary["top_damage"]["ship"], "Punisher")
        self.assertEqual(summary["groups"][0], {"kind": "alliance", "name": "Alliance Twenty", "count": 2})
        self.assertEqual(summary["groups"][1]["count"], 2)
        self.assertTrue(summary["awox"])

    def test_an_id_without_a_cached_name_is_shown_as_the_id(self):
        km = f.srp_killmail(attackers=[attacker(777, 5, final=True, corporation=888)], attacker_count=1)
        summary = attackers.summary(km)
        self.assertEqual((summary["final_blow"]["character"], summary["final_blow"]["corporation"]), ("777", "888"))

    def test_more_attackers_than_stored_make_the_counts_a_lower_bound(self):
        km = f.srp_killmail(attackers=[attacker(1, 10)], attacker_count=300)
        self.assertTrue(attackers.summary(km)["truncated"])

    def test_an_old_killmail_has_only_the_count(self):
        km = f.srp_killmail(attacker_count=12)
        summary = attackers.summary(km)
        self.assertEqual((summary["stored"], summary["total"], summary["final_blow"]), (False, 12, None))


class ClaimPageTests(SrpPagesTestCase):
    def setUp(self):
        super().setUp()
        self.staff = self.client_for(self.manager)
        self.owner = self.client_for(self.member)

    def claim(self, **extra):
        km = f.srp_killmail(
            f.main_of(self.member),
            fitted="100000000",
            attackers=[attacker(1, 600, final=True, corporation=10)],
            attacker_count=1,
            damage_taken=1000,
            solar_system_security=Decimal("0.4388"),
            region_id=10_000_001,
        )
        return f.srp_claim(self.member, killmail=km, **extra)

    def test_staff_sees_attackers_limits_and_history_the_claimant_does_not(self):
        claim = self.claim()
        url = route("srp_claim", claim.uuid)

        staff = self.staff.get(url)
        member = self.owner.get(url)

        for marker in ("srp-review-info", "srp-attackers", "srp-limits-now", "srp-rejection-history"):
            self.assertContains(staff, marker)
            self.assertNotContains(member, marker)

    def test_the_security_status_is_for_staff_only(self):
        claim = self.claim()
        url = route("srp_claim", claim.uuid)
        staff = self.staff.get(url)
        member = self.owner.get(url)
        self.assertContains(staff, "low security")
        self.assertContains(staff, "0.4")
        self.assertNotContains(member, "low security")
        self.assertNotContains(member, "Security status unknown")
        # The system name the member always saw stays.
        self.assertContains(member, claim.solar_system_name)

    def test_a_viewer_of_all_statistics_is_not_staff(self):
        claim = self.claim()
        page = self.client_for(self.stats).get(route("srp_claim", claim.uuid))
        self.assertEqual(page.status_code, 200)
        self.assertNotContains(page, "srp-review-info")

    def test_payers_and_reviewers_are_staff(self):
        claim = self.claim()
        for perms in (f.SRP_PAYER_PERMS, f.SRP_REVIEWER_PERMS):
            user = f.create_user(perms=perms)
            self.assertContains(self.client_for(user).get(route("srp_claim", claim.uuid)), "srp-review-info")

    def test_the_page_costs_a_fixed_number_of_extra_queries_for_staff(self):
        claim = self.claim()
        url = route("srp_claim", claim.uuid)

        def cost(client):
            with CaptureQueriesContext(connection) as queries:
                self.assertEqual(client.get(url).status_code, 200)
            return len(queries)

        cost(self.owner)
        member = cost(self.owner)
        cost(self.staff)
        staff = cost(self.staff)
        # The member page costs 2 reads that staff do not repeat; b3 already gave staff 2 more (forms, categories). The
        # reads REV-9 added (at most 4 beyond b3) are counted by pattern in test_query_budgets.
        self.assertLessEqual(staff - member, 8)
        self.assertEqual(cost(self.staff), staff)


class LimitsNowTests(SrpPagesTestCase):
    def policy(self, cap):
        return PolicySnapshot(percentage=Decimal("100"), personal_monthly_cap=cap).as_json()

    def test_the_table_matches_the_check_at_approval(self):
        f.srp_claim(self.member, status=Status.APPROVED, amount=150_000_000, decided_by=self.manager2, time=self.loss_time)
        claim = f.srp_claim(
            self.member,
            amount=150_000_000,
            time=self.loss_time,
            policy=self.policy(250_000_000),
        )

        info = review_info.limits_now(claim)

        personal = next(state for state in info["states"] if state.kind == "personal")
        self.assertEqual((personal.cap, personal.used, personal.remaining), (250_000_000, 150_000_000, 100_000_000))
        self.assertEqual((info["remaining"], info["over_by"]), (100_000_000, 50_000_000))
        with self.assertRaises(SrpLimitError) as caught:
            review.approve_claim(claim, actor=self.manager, amount=150_000_000)
        self.assertEqual(caught.exception.code, "confirm_over_limit")
        self.assertIn("50,000,000", caught.exception.message)

    def test_the_claim_itself_is_not_counted(self):
        claim = f.srp_claim(self.member, amount=100_000_000, time=self.loss_time, policy=self.policy(250_000_000))
        info = review_info.limits_now(claim)
        personal = next(state for state in info["states"] if state.kind == "personal")
        self.assertEqual((personal.used, personal.pending), (0, 0))
        self.assertEqual(info["over_by"], 0)

    def test_an_exhausted_limit_is_marked_on_the_page(self):
        f.srp_claim(self.member, status=Status.APPROVED, amount=250_000_000, decided_by=self.manager2, time=self.loss_time)
        claim = f.srp_claim(self.member, amount=10_000_000, time=self.loss_time, policy=self.policy(250_000_000))
        page = self.client_for(self.manager).get(route("srp_claim", claim.uuid))
        self.assertContains(page, "fleetops-srp-over-limit")
        self.assertContains(page, "Approving it needs the box")

    def test_no_limit_means_no_warning(self):
        claim = f.srp_claim(self.member, amount=10_000_000, time=self.loss_time)
        info = review_info.limits_now(claim)
        self.assertEqual((info["remaining"], info["over_by"]), (None, 0))

    def test_an_approved_claim_gets_no_over_by(self):
        claim = f.srp_claim(
            self.member, status=Status.APPROVED, amount=300_000_000, decided_by=self.manager2, time=self.loss_time,
            policy=self.policy(250_000_000),
        )
        self.assertEqual(review_info.limits_now(claim)["over_by"], 0)


class RejectionHistoryTests(SrpPagesTestCase):
    def setUp(self):
        super().setUp()
        self.srp = self.settings_row

    def history(self, claim):
        return review_info.rejection_history(claim, SrpClaim and self.settings_row.__class__.objects.get(pk=1))

    def test_numbers_and_latest_rejections(self):
        now = timezone.now()
        for index in range(2):
            f.srp_claim(self.member, status=Status.REJECTED, decided_by=self.manager, decided_at=now - timedelta(days=index))
        f.srp_claim(self.member, status=Status.REJECTED, auto_rejected=True, decided_at=now - timedelta(days=200))
        f.srp_claim(self.member, status=Status.WITHDRAWN)
        f.srp_claim(self.other, status=Status.REJECTED, decided_by=self.manager)
        current = f.srp_claim(self.member)

        info = self.history(current)

        self.assertEqual((info["total"], info["manual"], info["auto"], info["withdrawn"]), (5, 2, 1, 1))
        self.assertEqual(info["recent"], 2)
        self.assertEqual(len(info["latest"]), 3)
        self.assertEqual(info["latest"][0].decided_at.date(), now.date())

    def test_zero_days_means_all_time(self):
        f.srp_claim(self.member, status=Status.REJECTED, decided_by=self.manager, decided_at=timezone.now() - timedelta(days=900))
        current = f.srp_claim(self.member)
        self.settings_row.__class__.objects.filter(pk=1).update(review_history_days=0)
        self.assertEqual(self.history(current)["recent"], 1)
        self.settings_row.__class__.objects.filter(pk=1).update(review_history_days=90)
        self.assertEqual(self.history(current)["recent"], 0)

    def test_the_page_shows_two_plus_one_and_the_link_to_the_member(self):
        now = timezone.now()
        for _ in range(2):
            f.srp_claim(self.member, status=Status.REJECTED, decided_by=self.manager, decided_at=now)
        f.srp_claim(self.member, status=Status.REJECTED, auto_rejected=True, decided_at=now)
        current = f.srp_claim(self.member)

        page = self.client_for(self.manager).get(route("srp_claim", current.uuid))

        self.assertContains(page, "rejected 2 + 1")
        self.assertContains(page, f"user={self.member.pk}")
        self.assertContains(page, "status=all")

    def test_a_claim_without_an_account_has_no_history(self):
        claim = f.srp_claim(self.member)
        SrpClaim.objects.filter(pk=claim.pk).update(user=None)
        claim.refresh_from_db()
        self.assertIsNone(self.history(claim))
        page = self.client_for(self.manager).get(route("srp_claim", claim.uuid))
        self.assertContains(page, "This claim has no account.")

    def test_imported_claims_count_like_the_others(self):
        f.srp_claim(self.member, status=Status.REJECTED, decided_by=self.manager, source=SrpClaim.Source.AASRP, decided_at=timezone.now())
        self.assertEqual(self.history(f.srp_claim(self.member))["manual"], 1)


class QueueFilterTests(ManagerPagesTestCase):
    def setUp(self):
        super().setUp()
        self.solo = f.srp_category("Solo PVP")
        self.home = f.srp_category("Home defense")
        self.standing = f.standing_link(category=self.home)

    def queue(self, **params):
        return self.client.get(route("srp_review"), {"status": "all", **params})

    def test_category_filter_including_no_category(self):
        a = self.pending(category=self.solo)
        b = self.pending(self.other, category=self.home)
        c = self.pending(self.other)
        self.assertEqual(refs(self.queue(category=self.solo.pk)), [a.reference])
        self.assertEqual(refs(self.queue(category=self.home.pk)), [b.reference])
        self.assertEqual(refs(self.queue(category="none")), [c.reference])

    def test_the_category_choices_name_every_category_and_the_none_choice(self):
        self.home.is_active = False
        self.home.save()
        choices = dict(self.queue().context["filter_form"].fields["category"].choices)
        self.assertEqual(choices[str(self.solo.pk)], "Solo PVP")
        self.assertIn("(off)", str(choices[str(self.home.pk)]))
        self.assertIn("none", choices)

    def test_link_filter_and_the_datalist(self):
        a = self.pending(link=self.standing, category=self.home)
        self.pending(self.other)
        page = self.queue(link=self.standing.code)
        self.assertEqual(refs(page), [a.reference])
        self.assertContains(page, f'<option value="{self.standing.code}">')

    def test_submitted_dates_use_eve_days(self):
        early = self.pending(submitted_at=timezone.now().replace(year=2026, month=3, day=1, hour=23, minute=59))
        late = self.pending(self.other, submitted_at=timezone.now().replace(year=2026, month=3, day=2, hour=0, minute=1))
        self.assertEqual(refs(self.queue(submitted_to="2026-03-01")), [early.reference])
        self.assertEqual(refs(self.queue(submitted_from="2026-03-02")), [late.reference])
        self.assertEqual(
            sorted(refs(self.queue(submitted_from="2026-03-01", submitted_to="2026-03-02"))),
            sorted([early.reference, late.reference]),
        )

    def test_member_filter_from_the_url(self):
        mine = self.pending()
        self.pending(self.other)
        self.assertEqual(refs(self.queue(user=self.member.pk)), [mine.reference])

    def test_filters_combine_with_each_other_and_with_the_month(self):
        loss = self.loss_time
        a = self.pending(category=self.home, time=loss)
        self.pending(category=self.solo, time=loss)
        self.pending(self.other, category=self.home, time=loss)
        result = self.queue(category=self.home.pk, user=self.member.pk, year=loss.year, month=loss.month)
        self.assertEqual(refs(result), [a.reference])
        other_month = self.queue(category=self.home.pk, year=2020, month=1)
        self.assertEqual(refs(other_month), [])

    def test_invalid_parameters_are_ignored_without_an_error(self):
        claim = self.pending()
        for params in (
            {"category": "abc"},
            {"category": "99999999999999999999"},
            {"user": "x"},
            {"user": "-1"},
            {"submitted_from": "yesterday"},
            {"submitted_to": "2026-13-45"},
            {"submitted_to": "9999-12-31"},
            {"submitted_from": "9999-12-31", "submitted_to": "9999-12-31"},
            {"link": "x" * 200},
        ):
            with self.subTest(params=params):
                response = self.queue(**params)
                self.assertEqual(response.status_code, 200)
        self.assertIn(claim.reference, refs(self.queue(category="abc", user="x")))

    def test_the_filters_survive_the_month_links_and_the_bulk_form(self):
        self.pending(category=self.home)
        page = self.queue(category=self.home.pk, link="abc")
        self.assertContains(page, f"category={self.home.pk}")
        self.assertContains(page, 'name="link"')
        self.assertTrue(page.context["filters_active"])

    def test_the_table_shows_category_link_and_a_badge_for_banned_members(self):
        from fleetops.models import SrpBan

        self.pending(link=self.standing, category=self.home)
        SrpBan.objects.create(user=self.member, reason="Abuse", created_by=self.manager)
        page = self.queue()
        self.assertContains(page, "Category / link")
        self.assertContains(page, "Home defense")
        self.assertContains(page, self.standing.name)
        self.assertContains(page, "Banned")

    def test_the_query_count_does_not_grow_with_the_claims(self):
        def cost():
            with CaptureQueriesContext(connection) as queries:
                self.assertEqual(self.queue(category=self.home.pk).status_code, 200)
            return len(queries)

        for index in range(3):
            self.pending(self.member if index % 2 else self.other, category=self.home, link=self.standing)
        cost()
        small = cost()
        for index in range(25):
            self.pending(self.member if index % 2 else self.other, category=self.home, link=self.standing)
        self.assertEqual(cost(), small)

    def test_the_bulk_form_carries_the_revision_of_each_claim(self):
        claim = self.pending()
        SrpClaim.objects.filter(pk=claim.pk).update(member_revision=3)
        page = self.client.get(route("srp_review"))
        self.assertContains(page, f'name="rev-{claim.uuid}" value="3"')


class BackfillCommandTests(TestCase):
    def run_command(self, *args):
        out, err = io.StringIO(), io.StringIO()
        call_command("fleetops_srp_backfill_killmails", *args, stdout=out, stderr=err)
        return out.getvalue(), err.getvalue()

    def setUp(self):
        self.user = f.create_user("pilot", perms=f.SRP_MEMBER_PERMS)
        self.old = f.srp_killmail(f.main_of(self.user))
        f.srp_claim(self.user, killmail=self.old)
        # A killmail that has been read with the new version, and one without a claim.
        self.current = f.srp_killmail(f.main_of(self.user), damage_taken=5, solar_system_security=Decimal("0.5"))
        f.srp_claim(self.user, killmail=self.current)
        f.srp_killmail(f.main_of(self.user))
        # Stop only this patch: stopping every patch would also stop the run-wide lookups patched in tests/__init__.py.
        patcher = mock.patch("fleetops.management.commands.fleetops_srp_backfill_killmails.time.sleep")
        self.sleep = patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_dry_run_only_counts(self):
        with mock.patch("fleetops.management.commands.fleetops_srp_backfill_killmails.get_killmail") as read:
            out, _err = self.run_command("--dry-run")
        self.assertIn("Would read 1 killmails.", out)
        read.assert_not_called()

    def test_attackers_are_read_again_with_the_stored_hash(self):
        payload = f.esi_killmail_payload(self.old.killmail_id, victim=f.main_of(self.user))
        with mock.patch("fleetops.management.commands.fleetops_srp_backfill_killmails.get_killmail", return_value=payload) as read:
            out, _err = self.run_command("--attackers")
        read.assert_called_once_with(self.old.killmail_id, self.old.killmail_hash)
        self.old.refresh_from_db()
        self.assertEqual((len(self.old.attackers), self.old.damage_taken), (2, 2345))
        self.assertIn("Filled in 1 killmails", out)

    def test_the_security_status_is_looked_up_without_reading_the_killmail(self):
        fields = {
            "ship_group_id": 25,
            "solar_system_security": Decimal("0.4"),
            "constellation_id": 2,
            "region_id": 3,
            "location_checked_at": timezone.now(),
        }
        with mock.patch.object(killmails, "location_fields", return_value=fields), mock.patch(
            "fleetops.management.commands.fleetops_srp_backfill_killmails.get_killmail"
        ) as read:
            self.run_command("--security")
        read.assert_not_called()
        self.old.refresh_from_db()
        self.assertEqual((self.old.solar_system_security, self.old.region_id), (Decimal("0.4000"), 3))

    def test_the_limit_stops_early_and_a_failure_is_reported_without_its_text(self):
        extra = f.srp_killmail(f.main_of(self.user))
        f.srp_claim(self.user, killmail=extra)
        error = FleetESIError("ESI_ERROR", "secret details of the answer")
        with mock.patch("fleetops.management.commands.fleetops_srp_backfill_killmails.get_killmail", side_effect=error) as read:
            out, err = self.run_command("--attackers", "--limit", "1")
        self.assertEqual(read.call_count, 1)
        self.assertNotIn("secret details", err + out)
        self.assertIn("ESI_ERROR", err)
        self.assertIn("1 could not be read", out)

    def test_a_rate_limit_ends_the_run(self):
        extra = f.srp_killmail(f.main_of(self.user))
        f.srp_claim(self.user, killmail=extra)
        with mock.patch(
            "fleetops.management.commands.fleetops_srp_backfill_killmails.get_killmail",
            side_effect=FleetESIError("ESI_LIMITED", "slow"),
        ) as read:
            _out, err = self.run_command("--attackers")
        self.assertEqual(read.call_count, 1)
        self.assertIn("run the command again later", err)

    def test_imported_placeholders_are_left_alone(self):
        placeholder = f.srp_killmail(f.main_of(self.user), origin=SrpKillmail.Origin.AASRP)
        f.srp_claim(self.user, killmail=placeholder)
        out, _err = self.run_command("--dry-run")
        self.assertIn("Would read 1 killmails.", out)
