"""The SRP rules engine: rule matching, the pure calculation, the settings snapshot and eligibility."""

from datetime import timedelta
from decimal import Decimal

from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from fleetops.constants import MAX_ISK
from fleetops.models import SrpCorporationLimit, SrpPayoutRule, SrpSettings
from fleetops.srp import policy
from fleetops.srp.types import (
    CORPORATION_CAP_DEFAULT,
    CORPORATION_CAP_OVERRIDE,
    LimitState,
    PolicySnapshot,
    RuleSnapshot,
)

from . import factories as f

IN_FLEET = SrpPayoutRule.Scope.IN_FLEET
OUT_OF_FLEET = SrpPayoutRule.Scope.OUT_OF_FLEET
ANY = SrpPayoutRule.Scope.ANY

MOA = 623
CARACAL = 621
FITTINGS = "fittings:fittings.doctrine"


def limit(kind, cap, used=0, *, applies=True):
    remaining = None if cap is None else max(cap - used, 0)
    return LimitState(kind=kind, cap=cap, used=used, remaining=remaining, applies=applies)


def calc(*, scope=OUT_OF_FLEET, ship=MOA, value=None, source="zkill_fitted", limits=(), **policy_values):
    snapshot = PolicySnapshot(**policy_values)
    return policy.calculate(
        policy=snapshot, scope=scope, ship_type_id=ship, loss_value=value, value_source=source, limits=tuple(limits)
    )


class DoctrineKeyTests(TestCase):
    def test_doctrine_key_delegates_to_the_model_helper(self):
        self.assertEqual(policy.doctrine_key("fittings:fittings.doctrine", "7", "Ignored"), f"{FITTINGS}:7")
        self.assertEqual(policy.doctrine_key("", "", "  Moa Fleet "), "name:moa fleet")
        self.assertEqual(policy.doctrine_key("", "", ""), "")

    def test_the_name_key_collapses_white_space_of_every_kind(self):
        for typed in ("Moa  Fleet", "Moa\u00a0Fleet", "Moa\tFleet", " moa \u2003 FLEET\n"):
            with self.subTest(typed=typed):
                self.assertEqual(policy.doctrine_key("", "", typed), "name:moa fleet")

    def test_fleet_with_a_doctrine_app_id_and_a_name_has_both_keys(self):
        fc = f.create_user()
        operation = f.create_operation(
            fc, doctrine_source=FITTINGS, doctrine_external_id="7", doctrine_name="Moa Fleet"
        )
        self.assertEqual(policy.operation_doctrine_keys(operation), {f"{FITTINGS}:7", "name:moa fleet"})

    def test_fleet_without_doctrine_has_no_keys(self):
        operation = f.create_operation(f.create_user())
        self.assertEqual(policy.operation_doctrine_keys(operation), set())
        self.assertEqual(policy.operation_doctrine_keys(None), set())

    def test_source_without_id_falls_back_to_the_name(self):
        operation = f.create_operation(f.create_user(), doctrine_source=FITTINGS, doctrine_name="Moa Fleet")
        self.assertEqual(policy.operation_doctrine_keys(operation), {"name:moa fleet"})


class MatchRuleTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.fc = f.create_user()
        cls.operation = f.create_operation(
            cls.fc, doctrine_source=FITTINGS, doctrine_external_id="7", doctrine_name="Moa Fleet"
        )

    def match(self, ship=MOA, scope=IN_FLEET, operation="fleet"):
        operation = self.operation if operation == "fleet" else operation
        return policy.match_rule(ship_type_id=ship, scope=scope, operation=operation)

    def test_no_rules_no_match(self):
        self.assertIsNone(self.match())

    def test_ship_rule_matches_the_ship_in_and_out_of_fleet(self):
        rule = f.payout_rule(MOA, 40_000_000)
        self.assertEqual(self.match(), rule)
        self.assertEqual(self.match(scope=OUT_OF_FLEET, operation=None), rule)
        self.assertIsNone(self.match(ship=CARACAL))

    def test_doctrine_and_ship_beats_doctrine_beats_ship(self):
        ship_only = f.payout_rule(MOA, 40_000_000)
        doctrine_any_ship = f.payout_rule(0, 55_000_000, doctrine=(FITTINGS, "7", "Moa Fleet"))
        doctrine_ship = f.payout_rule(MOA, 70_000_000, doctrine=(FITTINGS, "7", "Moa Fleet"))
        self.assertEqual(self.match(), doctrine_ship)
        doctrine_ship.delete()
        self.assertEqual(self.match(), doctrine_any_ship)
        doctrine_any_ship.delete()
        self.assertEqual(self.match(), ship_only)

    def test_doctrine_any_ship_rule_covers_other_ships_of_the_fleet(self):
        rule = f.payout_rule(0, 55_000_000, doctrine=(FITTINGS, "7", "Moa Fleet"))
        self.assertEqual(self.match(ship=CARACAL), rule)

    def test_capsule_entry_beats_a_doctrine_rule_for_any_ship_while_capsules_need_a_rule(self):
        doctrine_any_ship = f.payout_rule(0, 55_000_000, doctrine=(FITTINGS, "7", "Moa Fleet"))
        capsule = f.payout_rule(f.CAPSULE, 1_000_000)

        def match(**kwargs):
            return policy.match_rule(ship_type_id=f.CAPSULE, scope=IN_FLEET, operation=self.operation, **kwargs)

        self.assertEqual(match(capsules_need_rule=True), capsule)
        # The switch off makes a capsule an ordinary ship again: the more specific doctrine rule wins.
        self.assertEqual(match(capsules_need_rule=False), doctrine_any_ship)
        # Other ships keep the doctrine rule either way.
        self.assertEqual(
            policy.match_rule(ship_type_id=CARACAL, scope=IN_FLEET, operation=self.operation, capsules_need_rule=True),
            doctrine_any_ship,
        )
        capsule.delete()
        self.assertIsNone(match(capsules_need_rule=True))

    def test_scope_specific_rule_beats_any_within_a_level(self):
        f.payout_rule(MOA, 40_000_000, scope=ANY)
        in_fleet = f.payout_rule(MOA, 45_000_000, scope=IN_FLEET)
        out_of_fleet = f.payout_rule(MOA, 30_000_000, scope=OUT_OF_FLEET)
        self.assertEqual(self.match(scope=IN_FLEET), in_fleet)
        self.assertEqual(self.match(scope=OUT_OF_FLEET, operation=None), out_of_fleet)

    def test_a_rule_for_the_other_scope_does_not_match(self):
        f.payout_rule(MOA, 45_000_000, scope=IN_FLEET)
        self.assertIsNone(self.match(scope=OUT_OF_FLEET, operation=None))

    def test_the_level_beats_the_scope(self):
        f.payout_rule(MOA, 40_000_000, scope=IN_FLEET)
        doctrine = f.payout_rule(MOA, 60_000_000, doctrine=(FITTINGS, "7", "Moa Fleet"), scope=ANY)
        self.assertEqual(self.match(), doctrine)

    def test_inactive_rules_are_ignored(self):
        f.payout_rule(MOA, 40_000_000, is_active=False)
        self.assertIsNone(self.match())
        active = f.payout_rule(MOA, 20_000_000, scope=IN_FLEET)
        self.assertEqual(self.match(), active)

    def test_doctrine_rule_of_another_doctrine_does_not_match(self):
        f.payout_rule(MOA, 70_000_000, doctrine=(FITTINGS, "8", "Other"))
        self.assertIsNone(self.match())

    def test_custom_name_doctrine_matches_by_casefolded_name(self):
        rule = f.payout_rule(MOA, 66_000_000, doctrine="  MOA fleet ")
        self.assertEqual(self.match(), rule)

    def test_custom_name_matches_however_the_white_space_of_the_fleet_was_typed(self):
        rule = f.payout_rule(MOA, 75_000_000, doctrine="Moa Fleet")
        for typed in ("Moa  Fleet", "Moa\u00a0Fleet", "Moa\tFleet", "  moa fleet "):
            with self.subTest(typed=typed):
                operation = f.create_operation(self.fc, doctrine_name=typed)
                self.assertEqual(self.match(operation=operation), rule)

    def test_custom_name_matches_a_fleet_without_doctrine_app(self):
        operation = f.create_operation(self.fc, doctrine_name="Moa Fleet")
        rule = f.payout_rule(0, 12_000_000, doctrine="moa fleet")
        self.assertEqual(self.match(operation=operation), rule)

    def test_doctrine_app_id_beats_custom_name_at_the_same_level(self):
        f.payout_rule(MOA, 66_000_000, doctrine="Moa Fleet")
        by_id = f.payout_rule(MOA, 77_000_000, doctrine=(FITTINGS, "7", "Moa Fleet"))
        self.assertEqual(self.match(), by_id)

    def test_doctrine_rules_need_an_in_fleet_loss(self):
        f.payout_rule(MOA, 70_000_000, doctrine=(FITTINGS, "7", "Moa Fleet"))
        self.assertIsNone(self.match(scope=OUT_OF_FLEET))

    def test_doctrine_rules_need_a_fleet(self):
        f.payout_rule(MOA, 70_000_000, doctrine=(FITTINGS, "7", "Moa Fleet"))
        self.assertIsNone(self.match(operation=None))

    def test_fleet_without_doctrine_only_matches_ship_rules(self):
        plain = f.create_operation(self.fc)
        f.payout_rule(MOA, 70_000_000, doctrine=(FITTINGS, "7", "Moa Fleet"))
        ship_rule = f.payout_rule(MOA, 40_000_000)
        self.assertEqual(self.match(operation=plain), ship_rule)

    def test_match_labels_follow_the_level(self):
        self.assertEqual(policy.rule_match(f.payout_rule(MOA, 1)), "ship")
        self.assertEqual(policy.rule_match(f.payout_rule(0, 1, doctrine="A")), "doctrine")
        self.assertEqual(policy.rule_match(f.payout_rule(MOA, 1, doctrine="B")), "doctrine_ship")


class CurrentPolicyTests(TestCase):
    def test_snapshot_copies_the_settings(self):
        settings_obj = f.srp_settings(
            value_source="zkill_total",
            percentage=Decimal("62.50"),
            in_fleet_loss_cap=300_000_000,
            out_of_fleet_loss_cap=100_000_000,
            personal_monthly_cap=900_000_000,
            personal_cap_includes_in_fleet=False,
            corporation_monthly_cap=5_000_000_000,
            corporation_cap_includes_in_fleet=False,
            over_limit_action="reject",
            capsules_need_rule=False,
            claim_deadline_days=14,
        )
        snapshot = policy.current_policy(corporation_id=f.DEFAULT_CORP[0], settings_obj=settings_obj)
        self.assertEqual(snapshot.value_source, "zkill_total")
        self.assertEqual(snapshot.percentage, Decimal("62.50"))
        self.assertEqual(snapshot.in_fleet_loss_cap, 300_000_000)
        self.assertEqual(snapshot.out_of_fleet_loss_cap, 100_000_000)
        self.assertEqual(snapshot.personal_monthly_cap, 900_000_000)
        self.assertFalse(snapshot.personal_cap_includes_in_fleet)
        self.assertEqual(snapshot.corporation_monthly_cap, 5_000_000_000)
        self.assertEqual(snapshot.corporation_cap_source, CORPORATION_CAP_DEFAULT)
        self.assertFalse(snapshot.corporation_cap_includes_in_fleet)
        self.assertEqual(snapshot.over_limit_action, "reject")
        self.assertFalse(snapshot.capsules_need_rule)
        self.assertEqual(snapshot.claim_deadline_days, 14)
        self.assertIsNone(snapshot.rule)

    def test_loads_the_settings_when_none_are_given(self):
        f.srp_settings(percentage=Decimal("40"))
        self.assertEqual(policy.current_policy(corporation_id=None).percentage, Decimal("40"))

    def test_snapshot_survives_a_json_round_trip(self):
        rule = f.payout_rule(MOA, 40_000_000, label="Moa", ship_name="Moa")
        f.srp_settings(percentage="80")
        snapshot = policy.current_policy(corporation_id=None, rule=rule, settings_obj=SrpSettings.get_solo())
        stored = snapshot.as_json()
        self.assertEqual(PolicySnapshot.from_json(stored), snapshot)
        self.assertEqual(stored["rule"]["id"], rule.pk)
        self.assertEqual(stored["rule"]["amount"], 40_000_000)
        self.assertEqual(stored["rule"]["match"], "ship")
        self.assertEqual(stored["percentage"], "80.00")

    def test_rule_snapshot_has_the_rule_values(self):
        rule = f.payout_rule(MOA, 70_000_000, doctrine=(FITTINGS, "7", "Moa Fleet"), scope=IN_FLEET, label="Fleet Moa")
        snapshot = policy.current_policy(corporation_id=None, rule=rule, settings_obj=f.srp_settings())
        self.assertEqual(
            snapshot.rule,
            RuleSnapshot(
                id=rule.pk,
                label="Fleet Moa",
                ship_type_id=MOA,
                ship_type_name=rule.ship_type_name,
                doctrine_name="Moa Fleet",
                scope=IN_FLEET,
                amount=70_000_000,
                match="doctrine_ship",
            ),
        )

    def test_corporation_override_replaces_the_default_cap(self):
        settings_obj = f.srp_settings(corporation_monthly_cap=5_000_000_000)
        SrpCorporationLimit.objects.create(corporation_id=f.OTHER_CORP[0], monthly_cap=1_000_000_000)
        other = policy.current_policy(corporation_id=f.OTHER_CORP[0], settings_obj=settings_obj)
        self.assertEqual(other.corporation_monthly_cap, 1_000_000_000)
        self.assertEqual(other.corporation_cap_source, CORPORATION_CAP_OVERRIDE)
        default = policy.current_policy(corporation_id=f.DEFAULT_CORP[0], settings_obj=settings_obj)
        self.assertEqual(default.corporation_monthly_cap, 5_000_000_000)
        self.assertEqual(default.corporation_cap_source, CORPORATION_CAP_DEFAULT)

    def test_override_without_a_cap_means_no_limit_for_that_corporation(self):
        settings_obj = f.srp_settings(corporation_monthly_cap=5_000_000_000)
        SrpCorporationLimit.objects.create(corporation_id=f.OTHER_CORP[0], monthly_cap=None)
        snapshot = policy.current_policy(corporation_id=f.OTHER_CORP[0], settings_obj=settings_obj)
        self.assertIsNone(snapshot.corporation_monthly_cap)
        self.assertEqual(snapshot.corporation_cap_source, CORPORATION_CAP_OVERRIDE)

    def test_no_corporation_uses_the_default(self):
        settings_obj = f.srp_settings(corporation_monthly_cap=5_000_000_000)
        snapshot = policy.current_policy(corporation_id=None, settings_obj=settings_obj)
        self.assertEqual(snapshot.corporation_monthly_cap, 5_000_000_000)

    def test_scope_helpers(self):
        snapshot = PolicySnapshot(
            in_fleet_loss_cap=1,
            out_of_fleet_loss_cap=2,
            personal_cap_includes_in_fleet=False,
            corporation_cap_includes_in_fleet=True,
        )
        self.assertEqual(policy.loss_cap(snapshot, IN_FLEET), 1)
        self.assertEqual(policy.loss_cap(snapshot, OUT_OF_FLEET), 2)
        self.assertFalse(policy.counts_toward_personal(snapshot, IN_FLEET))
        self.assertTrue(policy.counts_toward_personal(snapshot, OUT_OF_FLEET))
        self.assertTrue(policy.counts_toward_corporation(snapshot, IN_FLEET))


class CalculateBasisTests(SimpleTestCase):
    def test_rule_amount_is_the_base_whatever_the_value(self):
        rule = RuleSnapshot(id=1, ship_type_id=MOA, amount=40_000_000)
        result = calc(rule=rule, percentage=Decimal("80"), value=Decimal("500_000_000"))
        self.assertEqual(result.basis, "rule")
        self.assertEqual((result.base, result.suggested, result.limited), (40_000_000,) * 3)
        self.assertFalse(result.over_limit)
        self.assertEqual(result.binding_limit, "")
        self.assertEqual(result.notes, ())

    def test_rule_claim_does_not_need_a_value(self):
        result = calc(rule=RuleSnapshot(id=1, amount=10_000_000), value=None, percentage=Decimal("0"))
        self.assertEqual(result.basis, "rule")
        self.assertEqual(result.suggested, 10_000_000)

    def test_percentage_of_the_value_rounds_down(self):
        result = calc(percentage=Decimal("80"), value=Decimal("12345678.99"))
        self.assertEqual(result.basis, "percentage")
        self.assertEqual(result.base, 9_876_543)
        self.assertEqual(result.loss_value, Decimal("12345678.99"))

    def test_percentage_uses_exact_decimal_arithmetic(self):
        self.assertEqual(calc(percentage=Decimal("33.33"), value=Decimal("100.00")).base, 33)
        self.assertEqual(calc(percentage=Decimal("100"), value=Decimal("999999999999.99")).base, 999_999_999_999)
        self.assertEqual(calc(percentage=Decimal("0.01"), value=Decimal("9999")).base, 0)
        self.assertEqual(calc(percentage=Decimal("62.5"), value=Decimal("1000000000")).base, 625_000_000)

    def test_percentage_accepts_other_number_types(self):
        self.assertEqual(calc(percentage=Decimal("50"), value="1000.50").base, 500)
        self.assertEqual(calc(percentage=Decimal("50"), value=2000).base, 1000)

    def test_percentage_zero_gives_no_amount_and_says_why(self):
        result = calc(percentage=Decimal("0"), value=Decimal("100000000"))
        self.assertEqual(result.basis, "none")
        self.assertEqual(result.suggested, 0)
        self.assertEqual([note.code for note in result.notes], ["no_rule", "percentage_zero"])

    def test_missing_value_gives_no_amount_and_says_why(self):
        result = calc(percentage=Decimal("80"), value=None)
        self.assertEqual(result.basis, "none")
        self.assertEqual(result.base, 0)
        self.assertEqual([note.code for note in result.notes], ["no_value"])

    def test_negative_value_counts_as_missing(self):
        result = calc(percentage=Decimal("80"), value=Decimal("-5"))
        self.assertEqual(result.basis, "none")
        self.assertIsNone(result.loss_value)

    def test_value_source_is_recorded(self):
        result = calc(percentage=Decimal("80"), value=Decimal("1000"), source="esi_estimate")
        self.assertEqual(result.value_source, "esi_estimate")

    def test_calculation_has_a_timestamp(self):
        now = timezone.now()
        result = policy.calculate(
            policy=PolicySnapshot(),
            scope=OUT_OF_FLEET,
            ship_type_id=MOA,
            loss_value=None,
            value_source="",
            limits=(),
            now=now,
        )
        self.assertEqual(result.computed_at, now)
        self.assertIsNotNone(calc().computed_at)


class CalculateLargestAmountTests(SimpleTestCase):
    """The base amount stays inside what a reviewer can approve (1 to MAX_ISK), whatever the value or the rule."""

    def test_a_value_above_the_largest_amount_is_cut(self):
        result = calc(percentage=Decimal("100"), value=Decimal("5e16"))
        self.assertEqual((result.base, result.suggested, result.limited), (MAX_ISK,) * 3)

    def test_a_rule_above_the_largest_amount_is_cut(self):
        result = calc(rule=RuleSnapshot(id=1, amount=MAX_ISK + 1))
        self.assertEqual((result.basis, result.base, result.suggested), ("rule", MAX_ISK, MAX_ISK))

    def test_the_largest_amount_itself_is_kept(self):
        self.assertEqual(calc(rule=RuleSnapshot(id=1, amount=MAX_ISK)).base, MAX_ISK)

    def test_a_cut_amount_is_still_cut_further_by_the_caps(self):
        result = calc(percentage=Decimal("100"), value=Decimal("5e16"), out_of_fleet_loss_cap=250_000_000)
        self.assertEqual((result.base, result.suggested), (MAX_ISK, 250_000_000))


class CalculateCapTests(SimpleTestCase):
    def test_in_fleet_cap_applies_to_in_fleet_claims_only(self):
        rule = RuleSnapshot(id=1, amount=200_000_000)
        values = {"rule": rule, "in_fleet_loss_cap": 150_000_000, "out_of_fleet_loss_cap": None}
        in_fleet = calc(scope=IN_FLEET, **values)
        out_of_fleet = calc(scope=OUT_OF_FLEET, **values)
        self.assertEqual((in_fleet.base, in_fleet.cap, in_fleet.suggested), (200_000_000, 150_000_000, 150_000_000))
        self.assertEqual((out_of_fleet.cap, out_of_fleet.suggested), (None, 200_000_000))

    def test_out_of_fleet_cap_applies_to_out_of_fleet_claims_only(self):
        rule = RuleSnapshot(id=1, amount=200_000_000)
        values = {"rule": rule, "in_fleet_loss_cap": None, "out_of_fleet_loss_cap": 50_000_000}
        self.assertEqual(calc(scope=OUT_OF_FLEET, **values).suggested, 50_000_000)
        self.assertEqual(calc(scope=IN_FLEET, **values).suggested, 200_000_000)

    def test_cap_above_the_base_changes_nothing(self):
        result = calc(rule=RuleSnapshot(id=1, amount=10), out_of_fleet_loss_cap=1_000)
        self.assertEqual(result.suggested, 10)
        self.assertEqual(result.cap, 1_000)
        self.assertEqual(result.binding_limit, "")
        self.assertEqual(result.notes, ())

    def test_cap_applies_to_percentage_claims(self):
        result = calc(percentage=Decimal("100"), value=Decimal("900_000_000"), out_of_fleet_loss_cap=250_000_000)
        self.assertEqual((result.base, result.suggested), (900_000_000, 250_000_000))

    def test_cap_that_cuts_says_so_and_binds(self):
        result = calc(rule=RuleSnapshot(id=1, amount=200), out_of_fleet_loss_cap=150)
        self.assertEqual(result.binding_limit, "loss_cap")
        self.assertEqual([(n.code, n.params) for n in result.notes], [("loss_cap", {"cap": 150})])
        self.assertFalse(result.over_limit)
        self.assertEqual(result.limited, 150)


class CalculateMonthlyLimitTests(SimpleTestCase):
    RULE = RuleSnapshot(id=1, amount=200_000_000)

    def test_no_limits_no_change(self):
        result = calc(rule=self.RULE, limits=(limit("personal", None), limit("corporation", None)))
        self.assertEqual(result.limited, 200_000_000)
        self.assertFalse(result.over_limit)

    def test_claim_that_fits_is_not_over_limit(self):
        result = calc(rule=self.RULE, limits=(limit("personal", 500_000_000, used=300_000_000),))
        self.assertEqual(result.limited, 200_000_000)
        self.assertFalse(result.over_limit)
        self.assertEqual(result.binding_limit, "")

    def test_claim_that_only_partly_fits_is_cut_to_the_remaining_amount(self):
        result = calc(rule=self.RULE, limits=(limit("personal", 500_000_000, used=350_000_000),))
        self.assertEqual((result.suggested, result.limited), (200_000_000, 150_000_000))
        self.assertTrue(result.over_limit)
        self.assertEqual(result.binding_limit, "personal")
        self.assertEqual(
            [(n.code, n.params) for n in result.notes], [("monthly_limit", {"kind": "personal", "remaining": 150_000_000})]
        )

    def test_used_up_limit_leaves_nothing(self):
        result = calc(rule=self.RULE, limits=(limit("corporation", 500_000_000, used=600_000_000),))
        self.assertEqual(result.limited, 0)
        self.assertTrue(result.over_limit)
        self.assertEqual(result.binding_limit, "corporation")

    def test_most_restrictive_limit_wins(self):
        personal = limit("personal", 500_000_000, used=380_000_000)
        corporation = limit("corporation", 500_000_000, used=450_000_000)
        result = calc(rule=self.RULE, limits=(personal, corporation))
        self.assertEqual(result.limited, 50_000_000)
        self.assertEqual(result.binding_limit, "corporation")
        result = calc(rule=self.RULE, limits=(corporation, personal))
        self.assertEqual(result.binding_limit, "corporation")
        personal = limit("personal", 500_000_000, used=480_000_000)
        result = calc(rule=self.RULE, limits=(personal, limit("corporation", 500_000_000, used=450_000_000)))
        self.assertEqual((result.limited, result.binding_limit), (20_000_000, "personal"))

    def test_monthly_limit_beats_a_per_loss_cap_that_leaves_more(self):
        result = calc(
            rule=self.RULE,
            out_of_fleet_loss_cap=150_000_000,
            limits=(limit("personal", 500_000_000, used=470_000_000),),
        )
        self.assertEqual((result.suggested, result.limited), (150_000_000, 30_000_000))
        self.assertTrue(result.over_limit)
        self.assertEqual(result.binding_limit, "personal")

    def test_per_loss_cap_beats_a_monthly_limit_that_leaves_more(self):
        result = calc(
            rule=self.RULE,
            out_of_fleet_loss_cap=100_000_000,
            limits=(limit("personal", 500_000_000, used=300_000_000),),
        )
        self.assertEqual((result.suggested, result.limited), (100_000_000, 100_000_000))
        self.assertFalse(result.over_limit)
        self.assertEqual(result.binding_limit, "loss_cap")

    def test_equal_results_prefer_the_monthly_limit_and_personal_before_corporation(self):
        both = (limit("personal", 100, used=50), limit("corporation", 100, used=50))
        self.assertEqual(calc(rule=RuleSnapshot(id=1, amount=500), limits=both).binding_limit, "personal")
        tie_with_cap = calc(
            rule=RuleSnapshot(id=1, amount=500), out_of_fleet_loss_cap=50, limits=(limit("corporation", 100, used=50),)
        )
        self.assertEqual(tie_with_cap.binding_limit, "corporation")

    def test_limit_that_does_not_apply_is_ignored(self):
        in_fleet_excluded = limit("personal", 500_000_000, used=499_000_000, applies=False)
        result = calc(scope=IN_FLEET, rule=self.RULE, limits=(in_fleet_excluded,))
        self.assertEqual(result.limited, 200_000_000)
        self.assertFalse(result.over_limit)
        self.assertEqual(result.binding_limit, "")

    def test_only_the_limit_that_applies_counts(self):
        excluded = limit("personal", 500_000_000, used=499_000_000, applies=False)
        corporation = limit("corporation", 500_000_000, used=400_000_000)
        result = calc(scope=IN_FLEET, rule=self.RULE, limits=(excluded, corporation))
        self.assertEqual((result.limited, result.binding_limit), (100_000_000, "corporation"))

    def test_zero_suggested_amount_is_never_over_limit(self):
        result = calc(percentage=Decimal("0"), limits=(limit("personal", 100, used=100),))
        self.assertEqual((result.suggested, result.limited), (0, 0))
        self.assertFalse(result.over_limit)

    def test_limits_are_kept_in_the_calculation_and_the_json(self):
        states = (limit("personal", 500, used=100), limit("corporation", None))
        result = calc(rule=RuleSnapshot(id=1, amount=50), limits=states)
        self.assertEqual(result.limits, states)
        self.assertEqual(result.limit("personal").remaining, 400)
        stored = result.as_json()
        self.assertEqual(stored["limits"][0], {"kind": "personal", "cap": 500, "used": 100, "pending": 0, "remaining": 400, "applies": True})
        from fleetops.srp.types import Calculation

        self.assertEqual(Calculation.from_json(stored).limits, states)
        self.assertEqual(Calculation.from_json(stored).suggested, 50)


class CalculateCapsuleTests(SimpleTestCase):
    def test_capsule_without_a_rule_is_not_paid_by_percentage(self):
        result = calc(ship=f.CAPSULE, percentage=Decimal("80"), value=Decimal("900_000_000"), capsules_need_rule=True)
        self.assertEqual((result.basis, result.suggested), ("none", 0))
        self.assertEqual([note.code for note in result.notes], ["no_rule"])

    def test_genolution_capsule_is_a_capsule_too(self):
        result = calc(ship=33328, percentage=Decimal("80"), value=Decimal("900_000_000"), capsules_need_rule=True)
        self.assertEqual(result.suggested, 0)

    def test_capsule_with_its_own_rule_is_paid_the_fixed_amount(self):
        rule = RuleSnapshot(id=1, ship_type_id=f.CAPSULE, amount=1_000_000)
        result = calc(ship=f.CAPSULE, rule=rule, percentage=Decimal("80"), value=Decimal("900_000_000"))
        self.assertEqual((result.basis, result.suggested), ("rule", 1_000_000))

    def test_doctrine_rule_for_any_ship_does_not_cover_capsules(self):
        rule = RuleSnapshot(id=1, ship_type_id=0, doctrine_name="Moa Fleet", amount=70_000_000, match="doctrine")
        result = calc(ship=f.CAPSULE, rule=rule, scope=IN_FLEET, capsules_need_rule=True, percentage=Decimal("80"), value=Decimal("1000"))
        self.assertEqual((result.basis, result.suggested), ("none", 0))

    def test_capsule_is_an_ordinary_ship_when_the_switch_is_off(self):
        by_percentage = calc(ship=f.CAPSULE, percentage=Decimal("80"), value=Decimal("1000"), capsules_need_rule=False)
        self.assertEqual((by_percentage.basis, by_percentage.suggested), ("percentage", 800))
        rule = RuleSnapshot(id=1, ship_type_id=0, doctrine_name="Moa Fleet", amount=70, match="doctrine")
        by_rule = calc(ship=f.CAPSULE, rule=rule, capsules_need_rule=False)
        self.assertEqual((by_rule.basis, by_rule.suggested), ("rule", 70))

    def test_other_ships_are_not_affected_by_the_switch(self):
        result = calc(ship=MOA, percentage=Decimal("80"), value=Decimal("1000"), capsules_need_rule=True)
        self.assertEqual(result.suggested, 800)

    def test_percentage_applies_helper(self):
        snapshot = PolicySnapshot(percentage=Decimal("80"), capsules_need_rule=True)
        self.assertTrue(policy.percentage_applies(snapshot, MOA))
        self.assertFalse(policy.percentage_applies(snapshot, f.CAPSULE))
        self.assertFalse(policy.percentage_applies(PolicySnapshot(percentage=Decimal("0")), MOA))
        self.assertTrue(policy.percentage_applies(PolicySnapshot(percentage=Decimal("5"), capsules_need_rule=False), f.CAPSULE))


class EligibilityTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = f.create_user("pilot", perms=f.SRP_MEMBER_PERMS)
        cls.main = f.main_of(cls.user)
        cls.srp = f.srp_settings()
        cls.fc = f.create_user("fc", perms=f.FC_PERMS)

    def check(self, km=None, *, user=None, scope=OUT_OF_FLEET, link=None, now=None, **settings_values):
        km = km or f.srp_killmail(self.main)
        srp = self.srp
        for key, value in settings_values.items():
            setattr(srp, key, value)
        return policy.eligibility(km=km, user=user or self.user, scope=scope, link=link, settings_obj=srp, now=now)

    def tearDown(self):
        SrpSettings.objects.update(enabled=True)

    def test_a_normal_loss_has_no_blockers(self):
        self.assertEqual(self.check(), [])

    def test_loss_without_pilot(self):
        self.assertEqual(self.check(f.srp_killmail(None)), [policy.BLOCK_NO_PILOT])

    def test_character_of_another_account(self):
        stranger = f.main_of(f.create_user())
        self.assertEqual(self.check(f.srp_killmail(stranger)), [policy.BLOCK_NOT_YOUR_CHARACTER])

    def test_character_alliance_auth_does_not_know(self):
        self.assertEqual(self.check(f.srp_killmail(95_123_456)), [policy.BLOCK_NOT_YOUR_CHARACTER])

    def test_alt_of_the_user_is_fine(self):
        alt = f.add_alt(self.user)
        self.assertEqual(self.check(f.srp_killmail(alt)), [])

    def test_user_without_a_main_character(self):
        user = f.create_user(with_main=False)
        alt = f.add_alt(user)
        self.assertEqual(self.check(f.srp_killmail(alt), user=user), [policy.BLOCK_NO_MAIN])

    def test_out_of_fleet_losses_can_be_switched_off(self):
        self.assertEqual(self.check(out_of_fleet_enabled=False), [policy.BLOCK_FLEET_ONLY])

    def test_in_fleet_losses_do_not_need_the_out_of_fleet_switch(self):
        link = f.fleet_link(f.create_operation(self.fc))
        self.assertEqual(self.check(scope=IN_FLEET, link=link, out_of_fleet_enabled=False), [])

    def test_closed_link(self):
        link = f.fleet_link(f.create_operation(self.fc), is_open=False)
        self.assertEqual(self.check(scope=IN_FLEET, link=link), [policy.BLOCK_LINK_CLOSED])

    def test_srp_switched_off(self):
        self.assertEqual(self.check(enabled=False), [policy.BLOCK_SRP_DISABLED])

    def test_deadline_boundary(self):
        now = timezone.now()
        just_inside = f.srp_killmail(self.main, time=now - timedelta(days=7) + timedelta(minutes=1))
        just_outside = f.srp_killmail(self.main, time=now - timedelta(days=7) - timedelta(minutes=1))
        self.assertEqual(self.check(just_inside, now=now, claim_deadline_days=7), [])
        self.assertEqual(
            self.check(just_outside, now=now, claim_deadline_days=7),
            ["Losses older than 7 days cannot be submitted."],
        )

    def test_no_deadline_by_default(self):
        old = f.srp_killmail(self.main, time=timezone.now() - timedelta(days=900))
        self.assertEqual(self.check(old, claim_deadline_days=0), [])

    def test_capsule_needs_a_capsule_rule(self):
        capsule = f.srp_killmail(self.main, ship_type_id=f.CAPSULE)
        self.assertEqual(self.check(capsule, capsules_need_rule=True), [policy.BLOCK_CAPSULE])
        f.payout_rule(f.CAPSULE, 500_000)
        self.assertEqual(self.check(capsule, capsules_need_rule=True), [])

    def test_a_rule_for_another_ship_does_not_cover_capsules(self):
        f.payout_rule(MOA, 40_000_000)
        capsule = f.srp_killmail(self.main, ship_type_id=f.CAPSULE)
        self.assertEqual(self.check(capsule, capsules_need_rule=True), [policy.BLOCK_CAPSULE])

    def test_doctrine_rule_for_any_ship_does_not_cover_capsules(self):
        operation = f.create_operation(self.fc, doctrine_name="Moa Fleet")
        link = f.fleet_link(operation)
        f.payout_rule(0, 40_000_000, doctrine="Moa Fleet")
        capsule = f.srp_killmail(self.main, ship_type_id=f.CAPSULE)
        self.assertEqual(self.check(capsule, scope=IN_FLEET, link=link, capsules_need_rule=True), [policy.BLOCK_CAPSULE])

    def test_doctrine_rule_naming_the_capsule_covers_it_in_that_fleet(self):
        operation = f.create_operation(self.fc, doctrine_name="Moa Fleet")
        link = f.fleet_link(operation)
        f.payout_rule(f.CAPSULE, 900_000, doctrine="Moa Fleet")
        capsule = f.srp_killmail(self.main, ship_type_id=f.CAPSULE)
        self.assertEqual(self.check(capsule, scope=IN_FLEET, link=link, capsules_need_rule=True), [])

    def test_capsule_is_allowed_when_the_switch_is_off(self):
        capsule = f.srp_killmail(self.main, ship_type_id=33328)
        self.assertEqual(self.check(capsule, capsules_need_rule=False), [])

    def test_several_blockers_are_all_listed(self):
        capsule = f.srp_killmail(f.main_of(f.create_user()), ship_type_id=f.CAPSULE)
        blockers = self.check(capsule, out_of_fleet_enabled=False, capsules_need_rule=True)
        self.assertEqual(blockers, [policy.BLOCK_NOT_YOUR_CHARACTER, policy.BLOCK_FLEET_ONLY, policy.BLOCK_CAPSULE])

    def test_settings_are_loaded_when_none_are_given(self):
        km = f.srp_killmail(self.main)
        self.assertEqual(policy.eligibility(km=km, user=self.user, scope=OUT_OF_FLEET, link=None, settings_obj=None), [])
