"""FleetOps SRP models, their factories and how the SRP sweep is wired into the Celery tasks."""

from datetime import datetime, timedelta, timezone as dt_timezone
from decimal import Decimal
from io import StringIO
from unittest import mock

from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db import IntegrityError, connection, transaction
from django.db.models import ProtectedError
from django.test import TestCase

from fleetops import tasks
from fleetops.constants import CAPSULE_TYPE_IDS, MAX_ISK
from fleetops.models import (
    FleetOperation,
    FleetOpsSettings,
    SrpClaim,
    SrpClaimEvent,
    SrpCorporationLimit,
    SrpFleetLink,
    SrpKillmail,
    SrpPayoutRule,
    SrpSettings,
    srp_doctrine_key,
)
from fleetops.providers import zkillboard
from fleetops.srp import policy
from fleetops.srp.killmails import killmail_fields
from fleetops.srp.values import estimate_value

from . import factories as f

UTC = dt_timezone.utc
FITTINGS_SOURCE = "fittings:fittings.doctrine"


class SettingsModelTests(TestCase):
    def test_defaults(self):
        row = SrpSettings.get_solo()

        self.assertEqual(
            (
                row.enabled,
                row.out_of_fleet_enabled,
                row.zkillboard_enabled,
                row.value_source,
                row.percentage,
                row.over_limit_action,
                row.capsules_need_rule,
                row.claim_deadline_days,
                row.allow_self_review,
                row.notify_members,
                row.member_instructions,
            ),
            (False, True, True, "zkill_fitted", Decimal("0"), "review", True, 0, False, True, ""),
        )
        self.assertEqual(
            (
                row.in_fleet_loss_cap,
                row.out_of_fleet_loss_cap,
                row.personal_monthly_cap,
                row.corporation_monthly_cap,
            ),
            (None, None, None, None),
        )
        self.assertTrue(row.personal_cap_includes_in_fleet and row.corporation_cap_includes_in_fleet)

    def test_it_is_a_singleton_row(self):
        self.assertEqual(SrpSettings.get_solo().pk, 1)
        SrpSettings.get_solo()
        self.assertEqual(SrpSettings.objects.count(), 1)

    def test_without_zkillboard_only_the_estimate_is_a_value_source(self):
        row = SrpSettings.get_solo()
        row.zkillboard_enabled = False

        with self.assertRaises(ValidationError) as ctx:
            row.full_clean()
        self.assertIn("value_source", ctx.exception.message_dict)

        row.value_source = SrpSettings.ValueSource.ESI_ESTIMATE
        row.full_clean()

    def test_field_validation(self):
        invalid = {
            "percentage": [Decimal("-1"), Decimal("100.01")],
            "in_fleet_loss_cap": [0, MAX_ISK + 1],
            "out_of_fleet_loss_cap": [0],
            "personal_monthly_cap": [0],
            "corporation_monthly_cap": [MAX_ISK + 1],
            "claim_deadline_days": [3651],
            "member_instructions": ["x" * 2001],
            "value_source": ["bogus"],
            "over_limit_action": ["bogus"],
        }
        for field, values in invalid.items():
            for value in values:
                with self.subTest(field=field, value=str(value)[:12]):
                    row = SrpSettings.get_solo()
                    setattr(row, field, value)
                    with self.assertRaises(ValidationError) as ctx:
                        row.full_clean()
                    self.assertIn(field, ctx.exception.message_dict)

    def test_limits_of_the_valid_range(self):
        row = SrpSettings.get_solo()
        row.percentage = Decimal("100")
        row.in_fleet_loss_cap = MAX_ISK
        row.personal_monthly_cap = 1
        row.claim_deadline_days = 3650
        row.member_instructions = "x" * 2000
        row.full_clean()

    def test_the_provider_help_text_names_the_new_order(self):
        text = FleetOpsSettings._meta.get_field("srp_provider").help_text
        self.assertIn("FleetOps SRP", text)
        self.assertLess(text.index("FleetOps SRP"), text.index("aa-srp"))
        self.assertLess(text.index("aa-srp"), text.index("Alliance Auth SRP"))

    def test_permissions_are_declared_on_the_settings_model(self):
        declared = dict(FleetOpsSettings._meta.permissions)

        self.assertEqual(declared["request_srp"], "Can request SRP")
        self.assertEqual(declared["manage_srp"], "Can review and pay SRP claims")


class DoctrineKeyTests(TestCase):
    def test_keys(self):
        cases = [
            (("", "", ""), ""),
            ((FITTINGS_SOURCE, "5", "Moa Fleet"), f"{FITTINGS_SOURCE}:5"),
            (("", "", "Moa Fleet"), "name:moa fleet"),
            (("", "", "  MOA   Fleet "), "name:moa fleet"),
            (("", "", "Moa\u00a0Fleet"), "name:moa fleet"),
            ((FITTINGS_SOURCE, "", "Moa Fleet"), "name:moa fleet"),
            (("", "5", "Moa Fleet"), "name:moa fleet"),
            ((None, None, None), ""),
        ]
        for args, expected in cases:
            with self.subTest(args=args):
                self.assertEqual(srp_doctrine_key(*args), expected)

    def test_the_policy_module_uses_the_same_key(self):
        self.assertEqual(policy.doctrine_key(FITTINGS_SOURCE, "7", "x"), f"{FITTINGS_SOURCE}:7")
        self.assertEqual(policy.doctrine_key("", "", "Gnosis"), "name:gnosis")

    def test_capsule_types_are_shared(self):
        self.assertEqual(policy.CAPSULE_TYPE_IDS, CAPSULE_TYPE_IDS)
        self.assertEqual(CAPSULE_TYPE_IDS, frozenset({670, 33328}))


class PayoutRuleTests(TestCase):
    def test_saving_computes_the_doctrine_key(self):
        ship = f.payout_rule(f.RIFTER, 10_000_000)
        named = f.payout_rule(f.RIFTER, 20_000_000, doctrine="Frigate Fleet")
        external = f.payout_rule(0, 30_000_000, doctrine=(FITTINGS_SOURCE, "3", "Moa Fleet"))

        self.assertEqual(ship.doctrine_key, "")
        self.assertEqual(named.doctrine_key, "name:frigate fleet")
        self.assertEqual(external.doctrine_key, f"{FITTINGS_SOURCE}:3")

    def test_the_key_follows_a_changed_doctrine(self):
        rule = f.payout_rule(f.RIFTER, 10_000_000, doctrine="Old Name")

        rule.doctrine_name = "New Name"
        rule.save(update_fields=["doctrine_name"])

        rule.refresh_from_db()
        self.assertEqual(rule.doctrine_key, "name:new name")

    def test_a_rule_needs_a_ship_or_a_doctrine(self):
        rule = SrpPayoutRule(amount=1_000_000)

        with self.assertRaises(ValidationError) as ctx:
            rule.full_clean()
        self.assertIn("Choose a ship type, a doctrine, or both.", str(ctx.exception))

        SrpPayoutRule(ship_type_id=f.RIFTER, amount=1_000_000).full_clean()
        SrpPayoutRule(doctrine_name="Moa Fleet", amount=1_000_000).full_clean()

    def test_amount_range(self):
        for amount in (0, MAX_ISK + 1):
            with self.subTest(amount=amount):
                with self.assertRaises(ValidationError) as ctx:
                    SrpPayoutRule(ship_type_id=f.RIFTER, amount=amount).full_clean()
                self.assertIn("amount", ctx.exception.message_dict)
        SrpPayoutRule(ship_type_id=f.RIFTER, amount=MAX_ISK).full_clean()
        SrpPayoutRule(ship_type_id=f.RIFTER, amount=1).full_clean()

    def test_a_rule_is_unique_per_ship_doctrine_and_scope(self):
        f.payout_rule(f.RIFTER, 10_000_000)

        with self.assertRaises(IntegrityError), transaction.atomic():
            f.payout_rule(f.RIFTER, 20_000_000)

        # Another scope, another doctrine, or another ship are other rules.
        f.payout_rule(f.RIFTER, 20_000_000, scope=SrpPayoutRule.Scope.IN_FLEET)
        f.payout_rule(f.RIFTER, 20_000_000, doctrine="Frigate Fleet")
        f.payout_rule(f.CAPSULE, 20_000_000)
        self.assertEqual(SrpPayoutRule.objects.count(), 4)

    def test_validation_refuses_a_duplicate_with_a_message(self):
        f.payout_rule(f.RIFTER, 10_000_000, doctrine="Frigate Fleet")
        duplicate = SrpPayoutRule(ship_type_id=f.RIFTER, doctrine_name="frigate FLEET", amount=5_000_000)

        with self.assertRaises(ValidationError) as ctx:
            duplicate.full_clean()
        self.assertIn("A rule for this ship, doctrine and scope already exists.", str(ctx.exception))

    def test_validation_finds_the_duplicate_when_a_form_leaves_the_derived_fields_out(self):
        f.payout_rule(f.RIFTER, 10_000_000)
        duplicate = SrpPayoutRule(ship_type_id=f.RIFTER, amount=5_000_000)

        with self.assertRaises(ValidationError):
            duplicate.full_clean(exclude=["doctrine_key", "ship_type_id", "scope"])

    def test_editing_a_rule_is_not_a_duplicate_of_itself(self):
        rule = f.payout_rule(f.RIFTER, 10_000_000)

        rule.amount = 12_000_000
        rule.full_clean()
        rule.save()

    def test_text(self):
        self.assertEqual(str(f.payout_rule(f.RIFTER, 1, ship_name="Rifter")), "Rifter")
        self.assertEqual(str(f.payout_rule(f.RIFTER, 1, ship_name="Rifter", doctrine="Frigates")), "Rifter / Frigates")
        self.assertEqual(str(f.payout_rule(0, 1, doctrine="Anything Goes")), "Anything Goes")


class CorporationLimitTests(TestCase):
    def test_one_limit_per_corporation(self):
        SrpCorporationLimit.objects.create(corporation_id=f.DEFAULT_CORP[0], monthly_cap=5)

        with self.assertRaises(IntegrityError), transaction.atomic():
            SrpCorporationLimit.objects.create(corporation_id=f.DEFAULT_CORP[0], monthly_cap=6)

    def test_no_cap_means_no_limit_for_the_corporation(self):
        limit = SrpCorporationLimit.objects.create(corporation_id=f.OTHER_CORP[0])

        limit.full_clean()
        self.assertIsNone(SrpCorporationLimit.objects.get(pk=limit.pk).monthly_cap)

    def test_cap_range(self):
        for cap in (0, MAX_ISK + 1):
            with self.subTest(cap=cap):
                with self.assertRaises(ValidationError) as ctx:
                    SrpCorporationLimit(corporation_id=1, monthly_cap=cap).full_clean()
                self.assertIn("monthly_cap", ctx.exception.message_dict)

    def test_text(self):
        self.assertEqual(str(SrpCorporationLimit(corporation_id=7, corporation_name="Test Corp")), "Test Corp")
        self.assertEqual(str(SrpCorporationLimit(corporation_id=7)), "7")


class FleetLinkTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.fc = f.create_user(perms=f.FC_PERMS)
        cls.operation = f.create_operation(cls.fc)

    def test_a_fleet_has_one_link(self):
        link = f.fleet_link(self.operation)

        with self.assertRaises(IntegrityError), transaction.atomic():
            f.fleet_link(self.operation)

        # The refused second link was assigned to the same object, so the fleet is read again.
        self.assertEqual(FleetOperation.objects.get(pk=self.operation.pk).srp_link, link)
        self.assertTrue(link.is_open)
        self.assertEqual(link.fc_user, self.fc)
        self.assertEqual(link.fc_main_character_name, f.main_of(self.fc).character_name)

    def test_codes_are_unique(self):
        f.fleet_link(self.operation, code="same-code")
        other = f.create_operation(self.fc)

        with self.assertRaises(IntegrityError), transaction.atomic():
            f.fleet_link(other, code="same-code")

    def test_the_link_goes_with_the_fleet_but_the_claims_stay(self):
        link = f.fleet_link(self.operation)
        pilot = f.create_user(perms=f.SRP_MEMBER_PERMS)
        claim = f.srp_claim(pilot, link=link)

        self.operation.delete()

        self.assertFalse(SrpFleetLink.objects.filter(pk=link.pk).exists())
        claim.refresh_from_db()
        self.assertIsNone(claim.link)
        self.assertIsNone(claim.operation)
        self.assertIn(self.fc.profile.main_character.character_name, claim.fleet_label)
        self.assertEqual(claim.fc_user, self.fc)

    def test_the_link_keeps_the_fc_snapshot_when_the_user_is_removed(self):
        lead = f.create_user(perms=f.FC_PERMS)
        link = f.fleet_link(
            self.operation,
            fc_user=lead,
            fc_main_character_id=f.main_of(lead).character_id,
            fc_main_character_name=f.main_of(lead).character_name,
        )

        lead.delete()

        link.refresh_from_db()
        self.assertIsNone(link.fc_user)
        self.assertTrue(link.fc_main_character_name)
        self.assertTrue(link.fc_main_character_id)


class KillmailAndClaimTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.pilot = f.create_user(perms=f.SRP_MEMBER_PERMS)

    def test_the_killmail_id_is_the_primary_key(self):
        km = f.srp_killmail(f.main_of(self.pilot), killmail_id=555)

        self.assertEqual(km.pk, 555)
        self.assertEqual(SrpKillmail.objects.get(pk=555).victim_character_id, f.main_of(self.pilot).character_id)

    def test_a_killmail_is_claimed_once(self):
        km = f.srp_killmail(f.main_of(self.pilot))
        f.srp_claim(self.pilot, killmail=km)
        other = f.create_user(perms=f.SRP_MEMBER_PERMS)

        with self.assertRaises(IntegrityError), transaction.atomic():
            f.srp_claim(other, killmail=km)

        self.assertEqual(SrpClaim.objects.count(), 1)

    def test_a_claimed_killmail_cannot_be_deleted(self):
        km = f.srp_killmail(f.main_of(self.pilot))
        f.srp_claim(self.pilot, killmail=km)

        with self.assertRaises(ProtectedError):
            km.delete()

    def test_an_unclaimed_killmail_can_be_deleted(self):
        km = f.srp_killmail(f.main_of(self.pilot))

        km.delete()

        self.assertFalse(SrpKillmail.objects.exists())

    def test_killmail_items_keep_their_nesting(self):
        items = [{"flag": 5, "item_type_id": 1, "quantity_dropped": 2, "singleton": 0, "items": [{"flag": 5, "item_type_id": 2}]}]
        km = f.srp_killmail(f.main_of(self.pilot), items=items)

        self.assertEqual(SrpKillmail.objects.get(pk=km.pk).items, items)

    def test_zkill_defaults(self):
        km = f.srp_killmail(f.main_of(self.pilot))
        valued = f.srp_killmail(f.main_of(self.pilot), fitted="1500000.50")

        self.assertEqual(km.zkill_status, "pending")
        self.assertEqual(valued.zkill_status, "ok")
        self.assertEqual(SrpKillmail.objects.get(pk=valued.pk).zkill_fitted_value, Decimal("1500000.50"))
        self.assertEqual(km.zkill_labels, [])

    def test_claim_defaults_and_reference(self):
        claim = f.srp_claim(self.pilot)

        self.assertEqual(claim.reference, f"SRP-{claim.pk}")
        self.assertEqual(claim.status, "pending")
        self.assertEqual(claim.scope, "out_of_fleet")
        self.assertFalse(claim.over_limit)
        self.assertTrue(claim.counts_personal and claim.counts_corporation)
        self.assertEqual(claim.warnings, [])
        self.assertEqual(claim.corporation_id, f.DEFAULT_CORP[0])
        self.assertEqual(claim.main_character_id, f.main_of(self.pilot).character_id)
        self.assertEqual(claim.killmail_time, claim.killmail.killmail_time)
        self.assertIsNone(claim.approved_amount)
        self.assertEqual(claim.policy["version"], 1)

    def test_claim_statuses(self):
        approved = f.srp_claim(self.pilot, status="approved", amount=5_000_000, approved_amount=4_000_000)
        paid = f.srp_claim(self.pilot, status="paid", amount=5_000_000)
        rejected = f.srp_claim(self.pilot, status="rejected")
        pending = f.srp_claim(self.pilot)

        self.assertEqual((approved.approved_amount, approved.paid_at), (4_000_000, None))
        self.assertEqual(paid.approved_amount, 5_000_000)
        self.assertIsNotNone(paid.paid_at)
        self.assertIsNone(rejected.approved_amount)
        self.assertIsNone(pending.approved_amount)
        self.assertEqual(SrpClaim.USAGE_STATUSES, (SrpClaim.Status.APPROVED, SrpClaim.Status.PAID))

    def test_the_claim_stays_when_the_user_is_deleted(self):
        user = f.create_user(perms=f.SRP_MEMBER_PERMS)
        claim = f.srp_claim(user)
        name = claim.main_character_name

        user.delete()

        claim.refresh_from_db()
        self.assertIsNone(claim.user)
        self.assertEqual(claim.main_character_name, name)

    def test_the_claim_keeps_its_numbers_when_the_rule_is_deleted(self):
        rule = f.payout_rule(f.RIFTER, 7_000_000)
        claim = f.srp_claim(self.pilot, payout_rule=rule, payout_basis="rule", amount=7_000_000)

        rule.delete()

        claim.refresh_from_db()
        self.assertIsNone(claim.payout_rule)
        self.assertEqual((claim.payout_basis, claim.suggested_amount), ("rule", 7_000_000))

    def test_claims_are_listed_newest_first(self):
        now = datetime(2026, 10, 1, 12, tzinfo=UTC)
        old = f.srp_claim(self.pilot, submitted_at=now - timedelta(days=1))
        new = f.srp_claim(self.pilot, submitted_at=now)

        self.assertEqual(list(SrpClaim.objects.all()), [new, old])

    def test_indexes_for_the_month_queries_exist(self):
        with connection.cursor() as cursor:
            constraints = connection.introspection.get_constraints(cursor, SrpClaim._meta.db_table)
        names = {name for name, info in constraints.items() if info["index"]}

        for name in (
            "fleetops_srp_status_time_idx",
            "fleetops_srp_user_time_idx",
            "fleetops_srp_corp_time_idx",
            "fleetops_srp_fc_time_idx",
        ):
            self.assertIn(name, names)


class ClaimEventTests(TestCase):
    def test_events_are_newest_first_and_go_with_the_claim(self):
        pilot = f.create_user(perms=f.SRP_MEMBER_PERMS)
        claim = f.srp_claim(pilot)
        now = datetime(2026, 10, 1, 12, tzinfo=UTC)
        first = f.claim_event(claim, "submit", actor=pilot, created_at=now)
        second = f.claim_event(claim, "note", internal=True, reason="Looks fine", created_at=now + timedelta(minutes=1))

        self.assertEqual(list(claim.events.all()), [second, first])
        self.assertTrue(second.internal)
        self.assertFalse(first.internal)

        claim.delete()
        self.assertFalse(SrpClaimEvent.objects.exists())

    def test_every_audited_action_has_an_event_action(self):
        actions = {value for value, _label in SrpClaimEvent.Action.choices}

        self.assertTrue(
            {
                "submit",
                "auto_reject",
                "withdraw",
                "resubmit",
                "approve",
                "amount_change",
                "reject",
                "revoke",
                "paid",
                "unpaid",
                "reopen",
                "reclassify",
                "recalculate",
                "recheck",
                "value_update",
                "note",
            }
            <= actions
        )


class FactoryTests(TestCase):
    def test_esi_killmail_payload_is_readable_by_the_killmail_parser(self):
        pilot = f.create_character()
        payload = f.esi_killmail_payload(victim=pilot, ship_type_id=f.CAPSULE, killmail_id=42)

        fields = killmail_fields(payload)

        self.assertEqual(fields["killmail_id"], 42)
        self.assertEqual(fields["victim_character_id"], pilot.character_id)
        self.assertEqual(fields["victim_corporation_id"], pilot.corporation_id)
        self.assertEqual(fields["ship_type_id"], f.CAPSULE)
        self.assertEqual(fields["attacker_count"], 2)
        self.assertEqual(fields["killmail_time"].tzinfo, UTC)

    def test_a_victim_without_a_character_is_a_structure(self):
        fields = killmail_fields(f.esi_killmail_payload(victim=None))

        self.assertIsNone(fields["victim_character_id"])

    def test_nested_items_reach_the_estimate(self):
        items = [
            {"flag": 5, "item_type_id": 99, "quantity_dropped": 10},
            {"flag": 27, "item_type_id": 1, "quantity_destroyed": 1, "items": [{"flag": 5, "item_type_id": 2, "quantity_destroyed": 3}]},
        ]
        fields = killmail_fields(f.esi_killmail_payload(items=items))

        value = estimate_value(f.RIFTER, fields["items"], {f.RIFTER: Decimal("100"), 1: Decimal("10"), 2: Decimal("1"), 99: Decimal("5")})

        # The ship, the fitted module and the three items inside it; the cargo is left out.
        self.assertEqual(value, Decimal("113.00"))

    def test_zkill_payload_is_the_list_zkillboard_answers_with(self):
        pilot = f.create_character()
        body = f.zkill_payload(7_000_001, victim=pilot, fitted=1_000_000, total=3_000_000, npc=True)

        self.assertEqual(len(body), 1)
        item = body[0]
        self.assertEqual(item["killmail_id"], 7_000_001)
        self.assertEqual(item["victim"]["character_id"], pilot.character_id)
        self.assertEqual(item["zkb"]["fittedValue"], 1_000_000)
        self.assertEqual(item["zkb"]["totalValue"], 3_000_000)
        self.assertTrue(item["zkb"]["npc"])
        self.assertRegex(item["zkb"]["hash"], r"^[0-9a-f]{40}$")
        self.assertNotIn("fittedValue", f.zkill_payload(1, fitted=None)[0]["zkb"])

    def test_zkill_payload_is_accepted_by_the_zkillboard_client(self):
        keys = (zkillboard.SLOT_KEY, zkillboard.PAUSE_KEY, zkillboard.FAILURES_KEY)
        cache.delete_many(keys)
        self.addCleanup(cache.delete_many, keys)
        body = f.zkill_payload(7_000_002, fitted=2_500_000.5, total=None)
        response = mock.Mock(status_code=200, headers={})
        response.json.return_value = body

        with mock.patch.object(zkillboard.requests, "get", return_value=response):
            result = zkillboard.fetch_kill(7_000_002)

        self.assertTrue(result.ok)
        self.assertEqual(result.fitted_value, Decimal("2500000.50"))
        self.assertEqual(result.killmail_hash, body[0]["zkb"]["hash"])

    def test_in_fleet_claims_come_from_the_link(self):
        fc = f.create_user(perms=f.FC_PERMS)
        link = f.fleet_link(f.create_operation(fc))
        pilot = f.create_user(perms=f.SRP_MEMBER_PERMS)

        claim = f.srp_claim(pilot, link=link)

        self.assertEqual(
            (claim.scope, claim.link, claim.operation, claim.fc_user), ("in_fleet", link, link.operation, fc)
        )

    def test_srp_settings_are_enabled_by_default_in_tests(self):
        self.assertFalse(SrpSettings.get_solo().enabled)

        row = f.srp_settings(percentage=Decimal("80"))

        self.assertTrue(row.enabled)
        self.assertEqual(SrpSettings.get_solo().percentage, Decimal("80"))
        self.assertFalse(f.srp_settings(enabled=False).enabled)

    def test_permission_bundles(self):
        self.assertNotIn("fleetops.request_srp", f.MEMBER_PERMS)
        self.assertIn("fleetops.request_srp", f.SRP_MEMBER_PERMS)
        self.assertIn("fleetops.manage_srp", f.SRP_MANAGER_PERMS)
        self.assertIn("fleetops.request_srp", f.SRP_MANAGER_PERMS)
        self.assertTrue({"fleetops.request_srp", "fleetops.manage_srp"} <= set(f.FC_LEAD_PERMS))
        manager = f.create_user(perms=f.SRP_MANAGER_PERMS)
        self.assertTrue(manager.has_perm("fleetops.manage_srp"))


class SeedCommandTests(TestCase):
    def test_seed_creates_the_srp_settings_row(self):
        SrpSettings.objects.all().delete()

        call_command("fleetops_seed", stdout=StringIO())

        self.assertEqual(list(SrpSettings.objects.values_list("pk", "enabled")), [(1, False)])
        self.assertFalse(SrpPayoutRule.objects.exists())


class SweepWiringTests(TestCase):
    """The minute beat queues the SRP sweep, the task wraps it and the daily prune cleans up killmails."""

    def setUp(self):
        cache.delete(tasks.SRP_VALUES_LOCK_KEY)

    def test_the_beat_queues_the_sweep_with_its_clock(self):
        with mock.patch.object(tasks, "queue_srp_sweep") as queue:
            tasks.schedule_active_fleet_tracking()

        queue.assert_called_once()
        self.assertIsInstance(queue.call_args.args[0], datetime)

    def test_a_failing_sweep_never_stops_the_tracking(self):
        fc = f.create_user(perms=f.FC_PERMS)
        operation = f.create_operation(fc)
        # Poll marks of an earlier run with the same fleet ID would skip the poll.
        keys = [tasks._poll_attempt_key(operation.pk), tasks._poll_queued_key(operation.pk)]
        cache.delete_many(keys)
        self.addCleanup(cache.delete_many, keys)

        with (
            mock.patch.object(tasks, "queue_srp_sweep", side_effect=RuntimeError("secret detail")),
            mock.patch.object(tasks.track_operation, "delay") as delay,
            self.assertLogs("extensions", level="ERROR") as logs,
        ):
            queued = tasks.schedule_active_fleet_tracking()

        self.assertEqual(queued, 1)
        delay.assert_called_once()
        self.assertIn("SRP value sweep failed", logs.output[0])
        self.assertIn("RuntimeError", logs.output[0])
        self.assertNotIn("secret detail", logs.output[0])

    def test_the_task_runs_the_sweep_and_frees_its_lock(self):
        with mock.patch.object(tasks, "sweep_srp_values", return_value=3) as sweep:
            result = tasks.refresh_srp_values()

        self.assertEqual(result, 3)
        sweep.assert_called_once_with()
        self.assertIsNone(cache.get(tasks.SRP_VALUES_LOCK_KEY))

    def test_sweeps_do_not_overlap(self):
        cache.add(tasks.SRP_VALUES_LOCK_KEY, "1", timeout=60)

        with mock.patch.object(tasks, "sweep_srp_values") as sweep:
            result = tasks.refresh_srp_values()

        self.assertEqual(result, "already-running")
        sweep.assert_not_called()
        self.assertEqual(cache.get(tasks.SRP_VALUES_LOCK_KEY), "1")

    def test_a_failed_sweep_is_reported_and_frees_the_lock(self):
        with (
            mock.patch.object(tasks, "sweep_srp_values", side_effect=ValueError("token=abc")),
            self.assertLogs("extensions", level="ERROR") as logs,
        ):
            result = tasks.refresh_srp_values()

        self.assertEqual(result, "error")
        self.assertIsNone(cache.get(tasks.SRP_VALUES_LOCK_KEY))
        self.assertNotIn("token=abc", "\n".join(logs.output))

    def test_the_task_is_registered_under_its_name(self):
        self.assertEqual(tasks.refresh_srp_values.name, "fleetops.tasks.refresh_srp_values")

    def test_the_daily_prune_also_deletes_unclaimed_killmails(self):
        with (
            mock.patch.object(tasks, "prune_unclaimed", return_value=2) as prune,
            mock.patch("fleetops.services.history.prune_history", return_value={"old": 1}) as history,
        ):
            result = tasks.prune_attendance_history()

        history.assert_called_once_with(dry_run=False)
        prune.assert_called_once_with(7)
        self.assertEqual(result, {"old": 1})

    def test_a_failing_killmail_cleanup_keeps_the_attendance_result(self):
        with (
            mock.patch.object(tasks, "prune_unclaimed", side_effect=RuntimeError("boom")),
            mock.patch("fleetops.services.history.prune_history", return_value={"old": 4}),
            self.assertLogs("extensions", level="ERROR") as logs,
        ):
            result = tasks.prune_attendance_history()

        self.assertEqual(result, {"old": 4})
        self.assertIn("SRP killmail cleanup failed", logs.output[0])
