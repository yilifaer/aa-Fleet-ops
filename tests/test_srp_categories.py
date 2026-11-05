# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""SRP categories (CAT-1 to CAT-6): the model, the category of a claim, the calculation per category, payout
rules per category, the monthly limits of a category and the detection of claims calculated with old settings."""

from datetime import datetime, timezone as dt_timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest import mock

from django.contrib.auth.models import User
from django.core import signing
from django.core.cache import cache
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.test import RequestFactory, TestCase, tag
from django.utils import translation

from fleetops import admin as fleetops_admin
from fleetops.models import AuditLog, SrpCategory, SrpClaim, SrpClaimEvent, SrpPayoutRule
from fleetops.providers import zkillboard
from fleetops.srp import categories, claims, display, quota, review
from fleetops.srp import policy as rules
from fleetops.srp.types import (
    BINDING_ORDER,
    CategorySnapshot,
    LimitState,
    PolicySnapshot,
    RuleSnapshot,
    SrpError,
    SrpLimitError,
    SrpStateError,
)

from . import factories as f

Status = SrpClaim.Status
Action = SrpClaimEvent.Action
IN_FLEET = SrpClaim.Scope.IN_FLEET
OUT_OF_FLEET = SrpClaim.Scope.OUT_OF_FLEET
Mode = SrpCategory.LossCapMode

MOA = 623
FLYCATCHER = 22_464
SEPTEMBER = datetime(2026, 9, 14, 12, 0, tzinfo=dt_timezone.utc)


def snapshot(category=None, **values):
    return PolicySnapshot(category=CategorySnapshot.from_category(category), **values)


def calc(*, scope=OUT_OF_FLEET, ship=MOA, value=None, limits=(), rule=None, category=None, **policy_values):
    return rules.calculate(
        policy=PolicySnapshot(rule=rules.rule_snapshot(rule), category=CategorySnapshot.from_category(category), **policy_values),
        scope=scope,
        ship_type_id=ship,
        loss_value=value,
        value_source="zkill_total",
        limits=tuple(limits),
    )


class CategoryBase(TestCase):
    """A pilot, a manager and the means to preview and submit a loss."""

    @classmethod
    def setUpTestData(cls):
        cls.srp = f.srp_settings(percentage=Decimal("100"), value_source="zkill_total")
        cls.user = f.create_user("pilot", perms=f.SRP_MEMBER_PERMS)
        cls.main = f.main_of(cls.user)
        cls.other = f.create_user("other-pilot", perms=f.SRP_MEMBER_PERMS)
        cls.manager = f.create_user("manager", perms=f.SRP_MANAGER_PERMS)
        cls.second_manager = f.create_user("second-manager", perms=f.SRP_MANAGER_PERMS)
        cls.fc = f.create_user("fc", perms=f.FC_PERMS)
        cls.fleet_category = f.fleet_category()

    def setUp(self):
        super().setUp()
        for user in (self.user, self.other):
            cache.delete(f"fleetops:srp:check:{user.pk}")
        patcher = mock.patch.object(
            zkillboard.requests, "get", side_effect=zkillboard.requests.ConnectionError("offline")
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def killmail(self, user=None, **kwargs):
        kwargs.setdefault("total", "300000000")
        kwargs.setdefault("time", SEPTEMBER)
        return f.srp_killmail(f.main_of(user or self.user), **kwargs)

    def prepare(self, km, *, user=None, link=None, category=None):
        user = user or self.user
        text = f"<url=killReport:{km.killmail_id}:{km.killmail_hash}>Loss</url>"
        with mock.patch.object(claims.killmails, "fetch_killmail", return_value=km):
            return claims.prepare_claim(user, text, link=link, category=category)

    def submit(self, km=None, *, user=None, link=None, category=None):
        user = user or self.user
        km = km or self.killmail(user)
        preview = self.prepare(km, user=user, link=link, category=category)
        self.assertTrue(preview.token, preview.blockers)
        return claims.submit_claim(user, preview.token)

    def token(self, km, *, data=None, user=None):
        payload = {"km": km.pk, "link": None, "u": (user or self.user).pk}
        payload.update(data or {})
        return signing.dumps(payload, salt=claims.PREVIEW_SALT)

    def category(self, name=None, **values):
        values.setdefault("percentage", None)
        return f.srp_category(name, **values)

    def fleet(self, **extra):
        operation = f.create_operation(self.fc, **extra)
        return operation, f.fleet_link(operation)


# --- CAT-1: the model ------------------------------------------------------------------------------------------


class CategoryModelTests(TestCase):
    def test_a_new_database_has_only_the_built_in_category(self):
        category = SrpCategory.objects.get()

        self.assertEqual((category.key, category.name), ("fleet", "Fleet"))
        self.assertTrue(category.is_system)
        self.assertFalse(category.member_selectable)
        self.assertTrue(category.is_active)
        self.assertIsNone(category.percentage)
        self.assertEqual(category.loss_cap_mode, Mode.DEFAULT)
        self.assertTrue(category.cap_applies_to_rules)
        self.assertEqual(
            (category.loss_cap, category.personal_monthly_cap, category.corporation_monthly_cap), (None, None, None)
        )

    def test_the_model_has_no_default_permissions(self):
        self.assertEqual(SrpCategory._meta.default_permissions, ())

    def test_the_built_in_category_cannot_be_switched_off(self):
        category = f.fleet_category()
        category.is_active = False

        with self.assertRaises(ValidationError) as caught:
            category.full_clean()
        self.assertIn("is_active", caught.exception.message_dict)

    def test_the_built_in_category_keeps_its_key(self):
        category = f.fleet_category()
        category.key = "other"

        with self.assertRaises(ValidationError) as caught:
            category.full_clean()
        self.assertIn("key", caught.exception.message_dict)

    def test_members_cannot_choose_the_built_in_category(self):
        category = f.fleet_category()
        category.member_selectable = True

        with self.assertRaises(ValidationError) as caught:
            category.full_clean()
        self.assertIn("member_selectable", caught.exception.message_dict)

    def test_the_built_in_category_can_be_renamed_and_given_caps(self):
        category = f.fleet_category()
        category.name = "Line fleets"
        category.loss_cap_mode = Mode.CUSTOM
        category.loss_cap = 500_000_000
        category.full_clean()

    def test_an_own_cap_needs_an_amount(self):
        category = SrpCategory(name="Solo", key="solo", loss_cap_mode=Mode.CUSTOM)

        with self.assertRaises(ValidationError) as caught:
            category.full_clean()
        self.assertIn("loss_cap", caught.exception.message_dict)

    def test_an_amount_without_the_own_cap_mode_is_dropped(self):
        for mode in (Mode.DEFAULT, Mode.NONE):
            with self.subTest(mode=mode):
                category = SrpCategory(name="Solo", key="solo", loss_cap_mode=mode, loss_cap=100)
                category.full_clean()
                self.assertIsNone(category.loss_cap)

    def test_percentage_is_between_0_and_100(self):
        for value in (Decimal("-1"), Decimal("100.01")):
            with self.subTest(value=value):
                with self.assertRaises(ValidationError) as caught:
                    SrpCategory(name="Solo", key="solo", percentage=value).full_clean()
                self.assertIn("percentage", caught.exception.message_dict)
        SrpCategory(name="Solo", key="solo", percentage=Decimal("0")).full_clean()

    def test_name_and_key_are_unique(self):
        f.srp_category("Solo PVP", key="solo")

        with self.assertRaises(ValidationError) as caught:
            SrpCategory(name="Solo PVP", key="other").full_clean()
        self.assertIn("name", caught.exception.message_dict)
        with self.assertRaises(ValidationError) as caught:
            SrpCategory(name="Other", key="solo").full_clean()
        self.assertIn("key", caught.exception.message_dict)

    def test_the_default_category_of_the_settings_must_be_usable(self):
        hidden = f.srp_category("Hidden", member_selectable=False)
        inactive = f.srp_category("Closed", is_active=False)
        usable = f.srp_category("Solo PVP")
        settings_obj = f.srp_settings()

        for category in (hidden, inactive):
            settings_obj.default_category = category
            with self.assertRaises(ValidationError) as caught:
                settings_obj.full_clean()
            self.assertIn("default_category", caught.exception.message_dict)
        settings_obj.default_category = usable
        settings_obj.full_clean()

    def test_deleting_a_category_with_claims_or_rules_is_refused_by_the_database(self):
        category = f.srp_category("Solo PVP")
        f.payout_rule(FLYCATCHER, category=category)

        from django.db.models.deletion import ProtectedError

        with self.assertRaises(ProtectedError):
            category.delete()

    @tag("catalog")
    def test_the_built_in_category_is_named_in_the_language_of_the_viewer(self):
        category = f.fleet_category()

        self.assertEqual(display.category_label(category), "Fleet")
        with translation.override("zh-hans"):
            self.assertEqual(display.category_label(category), "舰队")
            self.assertEqual(str(category), "舰队")
        category.name = "Line fleets"
        with translation.override("zh-hans"):
            self.assertEqual(display.category_label(category), "Line fleets")


class CategoryAdminTests(TestCase):
    def setUp(self):
        self.model_admin = fleetops_admin.SrpCategoryAdmin(SrpCategory, fleetops_admin.admin.site)
        self.request = RequestFactory().get("/")
        self.request.user = User.objects.create_superuser("root", "root@example.org", "not-a-real-password")

    def test_the_built_in_category_cannot_be_deleted_in_the_admin(self):
        category = f.fleet_category()

        self.assertFalse(self.model_admin.has_delete_permission(self.request, category))
        self.assertTrue(self.model_admin.has_delete_permission(self.request, f.srp_category("Solo")))

    def test_the_bulk_delete_leaves_the_built_in_category_alone(self):
        solo = f.srp_category("Solo")

        self.model_admin.delete_queryset(self.request, SrpCategory.objects.all())

        self.assertEqual(list(SrpCategory.objects.values_list("key", flat=True)), ["fleet"])
        self.assertFalse(SrpCategory.objects.filter(pk=solo.pk).exists())
        deleted = AuditLog.objects.filter(action="configuration.delete", object_type="SrpCategory")
        self.assertEqual([row.object_id for row in deleted], [str(solo.pk)])

    def test_deleting_the_built_in_category_directly_is_refused(self):
        with self.assertRaises(PermissionDenied):
            self.model_admin.delete_model(self.request, f.fleet_category())
        self.assertTrue(SrpCategory.objects.filter(key="fleet").exists())


# --- CAT-2: the category of a claim ------------------------------------------------------------------------------


class ResolveCategoryTests(CategoryBase):
    def resolve(self, *, scope=OUT_OF_FLEET, link=None, operation=None, chosen=None):
        return categories.resolve_category(scope=scope, link=link, operation=operation, chosen=chosen)

    def test_a_fleet_loss_gets_the_built_in_category(self):
        operation, link = self.fleet()

        self.assertEqual(self.resolve(scope=IN_FLEET, link=link, operation=operation), self.fleet_category)

    def test_a_fleet_loss_without_a_link_uses_the_operation(self):
        operation, _link = self.fleet()
        mapped = self.category("Strat ops")
        mapped.fleet_types.add(operation.fleet_type)

        self.assertEqual(self.resolve(scope=IN_FLEET, operation=operation), mapped)

    def test_the_fleet_type_can_be_mapped_to_a_category(self):
        operation, link = self.fleet()
        mapped = self.category("Strat ops")
        mapped.fleet_types.add(operation.fleet_type)

        self.assertEqual(self.resolve(scope=IN_FLEET, link=link), mapped)

    def test_an_inactive_category_is_not_used_for_the_mapping(self):
        operation, link = self.fleet()
        mapped = self.category("Strat ops", is_active=False)
        mapped.fleet_types.add(operation.fleet_type)

        self.assertEqual(self.resolve(scope=IN_FLEET, link=link), self.fleet_category)

    def test_a_standing_link_gives_its_own_category(self):
        standing = self.category("Standing doctrine")
        link = SimpleNamespace(kind="standing", category=standing, operation=None)

        self.assertEqual(self.resolve(scope=IN_FLEET, link=link), standing)

    def test_a_loss_outside_a_fleet_gets_the_chosen_category(self):
        solo = self.category("Solo PVP")

        self.assertEqual(self.resolve(chosen=solo), solo)

    def test_a_chosen_category_has_to_be_usable(self):
        for category in (self.category(is_active=False), self.category(member_selectable=False), self.fleet_category):
            with self.subTest(category=category.key):
                with self.assertRaises(SrpError) as caught:
                    self.resolve(chosen=category)
                self.assertEqual(caught.exception.code, "category_unavailable")

    def test_without_a_choice_the_default_category_is_used(self):
        solo = self.category("Solo PVP")
        f.srp_settings(default_category=solo)

        self.assertEqual(self.resolve(), solo)

    def test_a_default_category_that_cannot_be_used_any_more_is_ignored(self):
        solo = self.category("Solo PVP")
        f.srp_settings(default_category=solo)
        SrpCategory.objects.filter(pk=solo.pk).update(is_active=False)

        self.assertIsNone(self.resolve())

    def test_without_a_choice_and_a_default_there_is_no_category(self):
        self.assertIsNone(self.resolve())

    def test_a_required_category_has_to_be_chosen(self):
        f.srp_settings(out_of_fleet_category_required=True)

        with self.assertRaises(SrpError) as caught:
            self.resolve()
        self.assertEqual(caught.exception.code, "category_required")

    def test_a_required_category_is_met_by_the_default(self):
        solo = self.category("Solo PVP")
        f.srp_settings(out_of_fleet_category_required=True, default_category=solo)

        self.assertEqual(self.resolve(), solo)

    def test_required_does_not_matter_in_a_fleet(self):
        operation, link = self.fleet()
        f.srp_settings(out_of_fleet_category_required=True)

        self.assertEqual(self.resolve(scope=IN_FLEET, link=link, operation=operation), self.fleet_category)


class ClaimCategoryTests(CategoryBase):
    def test_a_claim_through_a_fleet_link_is_in_the_fleet_category(self):
        operation, link = self.fleet()
        f.member_state(operation, self.main, first_seen=operation.started_at)
        km = self.killmail(time=operation.started_at)

        claim = self.submit(km, link=link)

        self.assertEqual(claim.category, self.fleet_category)
        self.assertEqual(claim.policy["category"]["key"], "fleet")
        self.assertEqual(claim.events.get().data["category"], "fleet")

    def test_a_claim_outside_a_fleet_has_the_chosen_category(self):
        solo = self.category("Solo PVP")

        claim = self.submit(category=solo)

        self.assertEqual(claim.category, solo)
        self.assertEqual(claim.policy["category"]["id"], solo.pk)

    def test_a_claim_without_categories_has_none_and_the_old_amount(self):
        claim = self.submit(self.killmail(total="300000000"))

        self.assertIsNone(claim.category)
        self.assertIsNone(claim.policy["category"])
        self.assertEqual(claim.suggested_amount, 300_000_000)

    def test_the_default_category_applies_when_nothing_is_chosen(self):
        solo = self.category("Solo PVP")
        f.srp_settings(default_category=solo)

        self.assertEqual(self.submit().category, solo)

    def test_the_preview_stops_when_a_category_is_required(self):
        f.srp_settings(out_of_fleet_category_required=True)

        with self.assertRaises(SrpError) as caught:
            self.prepare(self.killmail())
        self.assertEqual(caught.exception.code, "category_required")
        self.assertFalse(SrpClaim.objects.exists())

    def test_the_chosen_category_travels_in_the_token(self):
        solo = self.category("Solo PVP")

        preview = self.prepare(self.killmail(), category=solo)

        self.assertEqual(preview.category, solo)
        self.assertEqual(signing.loads(preview.token, salt=claims.PREVIEW_SALT)["cat"], solo.pk)

    def test_a_category_that_was_switched_off_after_the_preview_is_refused(self):
        solo = self.category("Solo PVP")
        preview = self.prepare(self.killmail(), category=solo)
        SrpCategory.objects.filter(pk=solo.pk).update(is_active=False)

        with self.assertRaises(SrpError) as caught:
            claims.submit_claim(self.user, preview.token)
        self.assertEqual(caught.exception.code, "category_unavailable")
        self.assertFalse(SrpClaim.objects.exists())

    def test_a_token_naming_a_category_members_cannot_choose_is_refused(self):
        hidden = self.category("Hidden", member_selectable=False)
        km = self.killmail()

        for category_id in (hidden.pk, self.fleet_category.pk, 999_999):
            with self.subTest(category=category_id):
                with self.assertRaises(SrpError) as caught:
                    claims.submit_claim(self.user, self.token(km, data={"cat": category_id}))
                self.assertEqual(caught.exception.code, "category_unavailable")

    def test_a_token_without_a_category_still_works(self):
        km = self.killmail()

        claim = claims.submit_claim(self.user, self.token(km))

        self.assertIsNone(claim.category)

    def test_the_category_of_a_fleet_claim_ignores_the_token(self):
        operation, link = self.fleet()
        f.member_state(operation, self.main, first_seen=operation.started_at)
        solo = self.category("Solo PVP")
        km = self.killmail(time=operation.started_at)

        claim = claims.submit_claim(self.user, self.token(km, data={"link": link.code, "cat": solo.pk}))

        self.assertEqual(claim.category, self.fleet_category)

    def test_fleet_fields_name_the_category(self):
        solo = self.category("Solo PVP")

        self.assertIs(claims.fleet_fields(None, solo)["category"], solo)
        operation, link = self.fleet()
        self.assertIs(claims.fleet_fields(link, self.fleet_category)["category"], self.fleet_category)


# --- CAT-3: the calculation per category ---------------------------------------------------------------------------


class SnapshotTests(TestCase):
    def test_the_snapshot_is_version_2_and_keeps_the_category(self):
        solo = f.srp_category("Solo PVP", percentage=Decimal("50"), loss_cap_mode=Mode.CUSTOM, loss_cap=250_000_000,
                              cap_applies_to_rules=False, personal_monthly_cap=1_000_000_000)

        data = snapshot(solo).as_json()
        restored = PolicySnapshot.from_json(data)

        self.assertEqual(data["version"], 2)
        self.assertEqual(restored.category, CategorySnapshot.from_category(solo))
        self.assertEqual(restored.category.percentage, Decimal("50.00"))
        self.assertEqual(restored.effective_percentage, Decimal("50.00"))

    def test_an_older_snapshot_has_no_category(self):
        restored = PolicySnapshot.from_json({"version": 1, "percentage": "80", "value_source": "zkill_total"})

        self.assertIsNone(restored.category)
        self.assertEqual(restored.version, 1)
        self.assertEqual(restored.effective_percentage, Decimal("80"))

    def test_a_snapshot_without_a_value_source_reads_as_fitted(self):
        # Only old JSON is read this way; the default of the settings is the total value.
        self.assertEqual(PolicySnapshot.from_json({}).value_source, "zkill_fitted")
        self.assertEqual(PolicySnapshot.from_json(None).category, None)

    def test_a_category_without_its_own_percentage_uses_the_general_one(self):
        solo = f.srp_category("Solo PVP")

        self.assertEqual(snapshot(solo, percentage=Decimal("80")).effective_percentage, Decimal("80"))

    def test_the_rule_snapshot_names_the_category(self):
        solo = f.srp_category("Solo PVP")
        rule = f.payout_rule(FLYCATCHER, category=solo)

        shot = rules.rule_snapshot(SrpPayoutRule.objects.select_related("category").get(pk=rule.pk))

        self.assertEqual((shot.category_id, shot.category_name), (solo.pk, "Solo PVP"))
        self.assertEqual(RuleSnapshot.from_json(shot.as_json()), shot)
        self.assertIsNone(RuleSnapshot.from_json({"id": 1}).category_id)


class CategoryCalculationTests(TestCase):
    def test_a_category_cap_holds_down_a_percentage_payout(self):
        solo = f.srp_category("Solo PVP", loss_cap_mode=Mode.CUSTOM, loss_cap=250_000_000)

        result = calc(category=solo, percentage=Decimal("100"), value=Decimal("300000000"))

        self.assertEqual((result.base, result.suggested, result.cap), (300_000_000, 250_000_000, 250_000_000))
        self.assertEqual(result.binding_limit, "loss_cap")
        self.assertIn("loss_cap", [note.code for note in result.notes])

    def test_a_fleet_loss_is_not_capped_when_the_settings_have_no_fleet_cap(self):
        result = calc(
            scope=IN_FLEET,
            category=f.fleet_category(),
            percentage=Decimal("100"),
            value=Decimal("571000000"),
        )

        self.assertEqual(result.suggested, 571_000_000)
        self.assertIsNone(result.cap)

    def test_the_default_cap_mode_uses_the_cap_of_the_scope(self):
        solo = f.srp_category("Solo PVP")

        out = calc(category=solo, percentage=Decimal("100"), value=Decimal("300000000"), out_of_fleet_loss_cap=100_000_000)
        inside = calc(
            scope=IN_FLEET,
            category=solo,
            percentage=Decimal("100"),
            value=Decimal("300000000"),
            in_fleet_loss_cap=200_000_000,
        )

        self.assertEqual((out.cap, out.suggested), (100_000_000, 100_000_000))
        self.assertEqual((inside.cap, inside.suggested), (200_000_000, 200_000_000))

    def test_the_no_cap_mode_removes_the_cap_of_the_scope(self):
        free = f.srp_category("Free", loss_cap_mode=Mode.NONE)

        result = calc(category=free, percentage=Decimal("100"), value=Decimal("300000000"), out_of_fleet_loss_cap=100_000_000)

        self.assertEqual((result.cap, result.suggested), (None, 300_000_000))

    def test_a_category_percentage_replaces_the_general_one(self):
        half = f.srp_category("Half", percentage=Decimal("50"))
        inherit = f.srp_category("Inherit")

        self.assertEqual(calc(category=half, percentage=Decimal("100"), value=Decimal("200000000")).suggested, 100_000_000)
        self.assertEqual(calc(category=inherit, percentage=Decimal("100"), value=Decimal("200000000")).suggested, 200_000_000)

    def test_a_category_percentage_of_zero_has_no_automatic_amount(self):
        none = f.srp_category("Fixed only", percentage=Decimal("0"))

        result = calc(category=none, percentage=Decimal("100"), value=Decimal("200000000"))

        self.assertEqual((result.basis, result.suggested), ("none", 0))
        self.assertIn("percentage_zero", [note.code for note in result.notes])

    def test_a_category_percentage_can_pay_when_the_general_one_is_zero(self):
        paid = f.srp_category("Paid", percentage=Decimal("40"))

        result = calc(category=paid, percentage=Decimal("0"), value=Decimal("100000000"))

        self.assertEqual((result.basis, result.suggested), ("percentage", 40_000_000))
        self.assertTrue(rules.percentage_applies(snapshot(paid, percentage=Decimal("0")), MOA))

    def test_the_cap_limits_a_rule_amount_unless_the_category_says_otherwise(self):
        rule = f.payout_rule(MOA, amount=400_000_000)
        capped = f.srp_category("Capped", loss_cap_mode=Mode.CUSTOM, loss_cap=250_000_000)
        full = f.srp_category("Full", loss_cap_mode=Mode.CUSTOM, loss_cap=250_000_000, cap_applies_to_rules=False)

        self.assertEqual(calc(category=capped, rule=rule).suggested, 250_000_000)
        uncapped = calc(category=full, rule=rule)
        self.assertEqual((uncapped.suggested, uncapped.cap), (400_000_000, None))

    def test_the_cap_still_limits_percentage_payouts_when_rules_are_exempt(self):
        full = f.srp_category("Full", loss_cap_mode=Mode.CUSTOM, loss_cap=250_000_000, cap_applies_to_rules=False)

        result = calc(category=full, percentage=Decimal("100"), value=Decimal("300000000"))

        self.assertEqual(result.suggested, 250_000_000)

    def test_without_a_category_nothing_changes(self):
        result = calc(percentage=Decimal("80"), value=Decimal("100000000"), out_of_fleet_loss_cap=50_000_000)

        self.assertEqual((result.base, result.suggested, result.cap), (80_000_000, 50_000_000, 50_000_000))

    def test_the_binding_order_ranks_the_limits(self):
        self.assertEqual(
            sorted(BINDING_ORDER, key=BINDING_ORDER.get),
            ["personal", "category_personal", "corporation", "category_corporation", "loss_cap"],
        )

    def test_the_category_end_to_end_uses_the_total_value(self):
        user = f.create_user("pilot", perms=f.SRP_MEMBER_PERMS)
        solo = f.srp_category("Solo PVP", loss_cap_mode=Mode.CUSTOM, loss_cap=250_000_000)
        f.srp_settings(percentage=Decimal("100"), value_source="zkill_total")
        km = f.srp_killmail(f.main_of(user), fitted="100000000", total="300000000", time=SEPTEMBER)
        token = signing.dumps({"km": km.pk, "link": None, "u": user.pk, "cat": solo.pk}, salt=claims.PREVIEW_SALT)

        claim = claims.submit_claim(user, token)

        self.assertEqual((claim.base_amount, claim.suggested_amount), (300_000_000, 250_000_000))
        self.assertEqual(claim.calculation["notes"][0]["code"], "loss_cap")


# --- CAT-4: the payout table per category -----------------------------------------------------------------------------


class PayoutRuleCategoryTests(CategoryBase):
    def test_the_category_key_follows_the_category(self):
        solo = self.category("Solo PVP")

        general = f.payout_rule(FLYCATCHER)
        special = f.payout_rule(FLYCATCHER, category=solo)

        self.assertEqual((general.category_key, special.category_key), (0, solo.pk))

    def test_updating_only_the_category_updates_the_key(self):
        solo = self.category("Solo PVP")
        rule = f.payout_rule(FLYCATCHER)

        rule.category = solo
        rule.save(update_fields=["category"])

        self.assertEqual(SrpPayoutRule.objects.get(pk=rule.pk).category_key, solo.pk)

    def test_the_same_ship_can_have_a_rule_per_category(self):
        solo = self.category("Solo PVP")
        f.payout_rule(FLYCATCHER)
        f.payout_rule(FLYCATCHER, category=solo)
        f.payout_rule(FLYCATCHER, category=self.category("Alliance fleets"))

        self.assertEqual(SrpPayoutRule.objects.count(), 3)

    def test_a_second_rule_in_the_same_category_is_refused(self):
        solo = self.category("Solo PVP")
        f.payout_rule(FLYCATCHER, category=solo)
        duplicate = SrpPayoutRule(ship_type_id=FLYCATCHER, scope="any", amount=1_000_000, category=solo)

        with self.assertRaises(ValidationError) as caught:
            duplicate.full_clean()
        self.assertEqual(
            caught.exception.messages, ["A rule for this ship, doctrine and scope already exists."]
        )
        with self.assertRaises(IntegrityError), transaction.atomic():
            SrpPayoutRule.objects.bulk_create(
                [SrpPayoutRule(ship_type_id=FLYCATCHER, scope="any", amount=1_000_000, category=solo, category_key=solo.pk)]
            )

    def test_two_general_rules_are_still_refused(self):
        f.payout_rule(FLYCATCHER)

        with self.assertRaises(ValidationError):
            SrpPayoutRule(ship_type_id=FLYCATCHER, scope="any", amount=1_000_000).full_clean()

    def test_a_rule_of_the_claim_category_beats_the_general_rule(self):
        solo = self.category("Solo PVP")
        general = f.payout_rule(FLYCATCHER, amount=80_000_000)
        special = f.payout_rule(FLYCATCHER, amount=50_000_000, category=solo)

        self.assertEqual(rules.match_rule(ship_type_id=FLYCATCHER, scope=OUT_OF_FLEET, category=solo), special)
        self.assertEqual(rules.match_rule(ship_type_id=FLYCATCHER, scope=OUT_OF_FLEET, category=None), general)
        self.assertEqual(
            rules.match_rule(ship_type_id=FLYCATCHER, scope=IN_FLEET, category=self.fleet_category), general
        )

    def test_a_rule_of_another_category_never_matches(self):
        f.payout_rule(FLYCATCHER, category=self.category("Alliance fleets"))

        self.assertIsNone(rules.match_rule(ship_type_id=FLYCATCHER, scope=OUT_OF_FLEET, category=None))
        self.assertIsNone(
            rules.match_rule(ship_type_id=FLYCATCHER, scope=OUT_OF_FLEET, category=self.category("Solo PVP"))
        )

    def test_the_level_of_the_rule_comes_before_its_category(self):
        solo = self.category("Solo PVP")
        doctrine = f.payout_rule(FLYCATCHER, amount=90_000_000, doctrine="Fast fleet")
        f.payout_rule(FLYCATCHER, amount=50_000_000, category=solo)
        operation, _link = self.fleet(doctrine_name="Fast fleet")

        found = rules.match_rule(ship_type_id=FLYCATCHER, scope=IN_FLEET, operation=operation, category=solo)

        self.assertEqual(found, doctrine)

    def test_a_category_rule_wins_within_the_level_before_the_scope(self):
        solo = self.category("Solo PVP")
        f.payout_rule(FLYCATCHER, amount=80_000_000, scope=OUT_OF_FLEET)
        special = f.payout_rule(FLYCATCHER, amount=50_000_000, category=solo, scope="any")

        self.assertEqual(rules.match_rule(ship_type_id=FLYCATCHER, scope=OUT_OF_FLEET, category=solo), special)

    def test_a_claim_is_paid_from_its_category_rule(self):
        solo = self.category("Solo PVP")
        f.payout_rule(FLYCATCHER, amount=80_000_000)
        f.payout_rule(FLYCATCHER, amount=50_000_000, category=solo)
        km = self.killmail(ship_type_id=FLYCATCHER, total="900000000")

        in_category = self.submit(km, category=solo)
        plain = self.submit(self.killmail(self.other, ship_type_id=FLYCATCHER, total="900000000"), user=self.other)

        self.assertEqual((in_category.payout_basis, in_category.suggested_amount), ("rule", 50_000_000))
        self.assertEqual(in_category.policy["rule"]["category_id"], solo.pk)
        self.assertEqual((plain.payout_basis, plain.suggested_amount), ("rule", 80_000_000))

    def test_the_capsule_rule_of_a_category_covers_capsules_of_that_category(self):
        solo = self.category("Solo PVP")
        f.payout_rule(670, amount=1_000_000, category=solo, ship_name="Capsule")
        km = self.killmail(ship_type_id=670, total="1000")

        covered = self.prepare(km, category=solo)
        uncovered = self.prepare(self.killmail(self.other, ship_type_id=670), user=self.other)

        self.assertTrue(covered.eligible, covered.blockers)
        self.assertFalse(uncovered.eligible)
        self.assertEqual(uncovered.blockers, ["Capsule losses are not covered."])


# --- CAT-5: monthly limits of a category ----------------------------------------------------------------------------------


class CategoryUsageTests(CategoryBase):
    def month(self):
        return quota.claim_month(SEPTEMBER)

    def test_the_usage_of_a_category_only_counts_its_claims(self):
        solo = self.category("Solo PVP")
        alliance_fleets = self.category("Alliance fleets")
        f.srp_claim(self.user, status=Status.APPROVED, amount=900_000_000, category=solo, time=SEPTEMBER)
        f.srp_claim(self.user, status=Status.APPROVED, amount=100_000_000, category=alliance_fleets, time=SEPTEMBER)
        f.srp_claim(self.user, status=Status.APPROVED, amount=50_000_000, time=SEPTEMBER)
        year, month = self.month()

        self.assertEqual(quota.month_usage(year=year, month=month, user_id=self.user.pk, category_id=solo.pk)[0], 900_000_000)
        self.assertEqual(quota.month_usage(year=year, month=month, user_id=self.user.pk)[0], 1_050_000_000)

    def test_the_counts_flags_do_not_matter_to_a_category(self):
        solo = self.category("Solo PVP")
        f.srp_claim(
            self.user,
            status=Status.APPROVED,
            amount=900_000_000,
            category=solo,
            time=SEPTEMBER,
            counts_personal=False,
            counts_corporation=False,
        )
        year, month = self.month()

        self.assertEqual(quota.month_usage(year=year, month=month, user_id=self.user.pk)[0], 0)
        self.assertEqual(quota.month_usage(year=year, month=month, user_id=self.user.pk, category_id=solo.pk)[0], 900_000_000)
        corporation_id = self.main.corporation_id
        self.assertEqual(
            quota.month_usage(year=year, month=month, corporation_id=corporation_id, category_id=solo.pk)[0],
            900_000_000,
        )

    def test_the_states_have_two_entries_without_a_category_cap(self):
        solo = self.category("Solo PVP")

        states = quota.limit_states(
            user_id=self.user.pk,
            corporation_id=self.main.corporation_id,
            scope=OUT_OF_FLEET,
            killmail_time=SEPTEMBER,
            policy=snapshot(solo),
        )

        self.assertEqual([state.kind for state in states], ["personal", "corporation"])

    def test_the_states_add_the_caps_of_the_category(self):
        solo = self.category("Solo PVP", personal_monthly_cap=1_000_000_000, corporation_monthly_cap=5_000_000_000)
        f.srp_claim(self.user, status=Status.APPROVED, amount=900_000_000, category=solo, time=SEPTEMBER)

        states = quota.limit_states(
            user_id=self.user.pk,
            corporation_id=self.main.corporation_id,
            scope=OUT_OF_FLEET,
            killmail_time=SEPTEMBER,
            policy=snapshot(solo),
        )

        self.assertEqual(
            [state.kind for state in states],
            ["personal", "corporation", "category_personal", "category_corporation"],
        )
        personal = states[2]
        self.assertEqual((personal.cap, personal.used, personal.remaining, personal.applies), (1_000_000_000, 900_000_000, 100_000_000, True))

    def test_a_category_limit_applies_to_fleet_claims_whatever_the_general_switches_say(self):
        solo = self.category("Solo PVP", personal_monthly_cap=1_000_000_000)

        states = quota.limit_states(
            user_id=self.user.pk,
            corporation_id=None,
            scope=IN_FLEET,
            killmail_time=SEPTEMBER,
            policy=snapshot(solo, personal_cap_includes_in_fleet=False),
        )

        self.assertFalse(states[0].applies)
        self.assertTrue(states[-1].applies)


class CategoryLimitFlowTests(CategoryBase):
    def setUp(self):
        super().setUp()
        self.solo = self.category(
            "Solo PVP", loss_cap_mode=Mode.CUSTOM, loss_cap=250_000_000, personal_monthly_cap=1_000_000_000
        )

    def used_up(self, amount=900_000_000, user=None, category=None):
        f.srp_claim(
            user or self.user, status=Status.APPROVED, amount=amount, category=category or self.solo, time=SEPTEMBER
        )

    def test_the_category_limit_cuts_the_amount_and_sends_the_claim_to_review(self):
        self.used_up()

        claim = self.submit(category=self.solo)

        self.assertEqual(claim.status, Status.PENDING)
        self.assertEqual((claim.suggested_amount, claim.limited_amount), (250_000_000, 100_000_000))
        self.assertTrue(claim.over_limit)
        self.assertEqual(claim.calculation["binding_limit"], "category_personal")
        self.assertEqual(
            [state["kind"] for state in claim.calculation["limits"]],
            ["personal", "corporation", "category_personal"],
        )
        warning = next(item for item in claim.warnings if item["code"] == "over_limit")
        self.assertEqual(warning["params"]["kind"], "category_personal")
        self.assertIn("monthly category personal limit", display.warning_text("over_limit", warning["params"]))

    def test_another_category_is_not_affected(self):
        self.used_up()
        other = self.category("Alliance fleets")

        claim = self.submit(category=other)

        self.assertFalse(claim.over_limit)
        self.assertEqual(len(claim.calculation["limits"]), 2)

    def test_a_category_without_caps_has_two_limits(self):
        claim = self.submit(category=self.category("Alliance fleets"))

        self.assertEqual([state["kind"] for state in claim.calculation["limits"]], ["personal", "corporation"])

    def test_in_reject_mode_a_used_up_category_limit_rejects_the_claim(self):
        self.used_up(amount=1_000_000_000)
        f.srp_settings(over_limit_action="reject")

        claim = self.submit(category=self.solo)

        self.assertEqual(claim.status, Status.REJECTED)
        self.assertTrue(claim.auto_rejected)
        self.assertIn("monthly category personal limit", claim.decision_reason)
        self.assertEqual(claim.events.get().data["auto_reject"]["kind"], "category_personal")

    def test_the_general_limit_wins_a_tie(self):
        f.srp_settings(personal_monthly_cap=1_000_000_000)
        self.used_up()

        claim = self.submit(category=self.solo)

        self.assertEqual(claim.calculation["binding_limit"], "personal")

    def test_the_approval_checks_the_category_of_the_snapshot(self):
        claim = self.submit(category=self.solo)
        self.used_up(amount=900_000_000)

        with self.assertRaises(SrpLimitError) as caught:
            review.approve_claim(claim, actor=self.manager, amount=200_000_000)
        self.assertEqual(caught.exception.code, "confirm_over_limit")
        self.assertIn("monthly category personal limit", caught.exception.message)
        approved = review.approve_claim(
            claim, actor=self.manager, amount=100_000_000, reason="Within the limit", confirm_above_policy=False
        )
        self.assertEqual(approved.approved_amount, 100_000_000)

    def test_the_approval_of_a_claim_without_a_category_limit_ignores_the_category_usage(self):
        old = f.srp_claim(self.user, time=SEPTEMBER, amount=250_000_000)
        old.policy = PolicySnapshot().as_json()
        old.save()
        self.used_up(amount=999_000_000)

        approved = review.approve_claim(old, actor=self.manager, amount=250_000_000, reason="Old snapshot", confirm_above_policy=True)

        self.assertEqual(approved.status, Status.APPROVED)

    def test_the_reduced_reason_names_the_category_limit(self):
        from fleetops.views_srp import within_limit_reason

        claim = SimpleNamespace(calculation={"binding_limit": "category_personal"})
        other = SimpleNamespace(calculation={"binding_limit": "category_corporation"})

        self.assertEqual(within_limit_reason(claim), "Reduced to the remaining monthly personal limit of the category.")
        self.assertEqual(within_limit_reason(other), "Reduced to the remaining monthly corporation limit of the category.")

    def test_the_limit_names_are_known(self):
        self.assertEqual(claims.limit_name("category_personal"), "monthly category personal limit")
        self.assertEqual(claims.limit_name("category_corporation"), "monthly category corporation limit")
        self.assertEqual(display.limit_kind_label("category_personal"), "category personal monthly limit")
        self.assertEqual(display.limit_kind_label("unknown_kind"), "Unknown kind")

    def test_the_limits_table_shows_the_new_kinds(self):
        from django.template.loader import render_to_string

        html = render_to_string(
            "fleetops/srp/_limits.html",
            {"limits": [LimitState(kind="category_personal", cap=1_000_000_000, used=900_000_000, remaining=100_000_000)]},
        )

        self.assertIn("Category personal monthly limit", html)

    def test_the_monthly_kind_comes_from_the_calculation(self):
        calculation = SimpleNamespace(
            binding_limit="",
            limits=[
                LimitState(kind="personal", cap=10, used=0, remaining=10),
                LimitState(kind="category_personal", cap=10, used=0, remaining=10),
            ],
        )

        self.assertEqual(claims.monthly_limit_kind(calculation), "personal")


# --- CAT-6: old settings, recalculation, changing the category ---------------------------------------------------


class OutdatedClaimsTests(CategoryBase):
    def test_nothing_is_outdated_after_the_upgrade(self):
        # A claim of 0.2.0b3 (version 1 snapshot, no category) and one in a fleet that the migration put into the
        # built-in category.
        operation, link = self.fleet()
        f.srp_claim(self.user, time=SEPTEMBER)
        old_in_fleet = f.srp_claim(self.other, link=link, time=SEPTEMBER)
        snapshot_v1 = PolicySnapshot(version=1, percentage=Decimal("100"), value_source="zkill_total").as_json()
        snapshot_v1.pop("category")
        SrpClaim.objects.update(policy=snapshot_v1)

        self.assertEqual(old_in_fleet.category, self.fleet_category)
        self.assertEqual(rules.outdated_pending_claims(), [])

    def test_a_fresh_claim_is_not_outdated(self):
        solo = self.category("Solo PVP", loss_cap_mode=Mode.CUSTOM, loss_cap=250_000_000, personal_monthly_cap=10**12)
        self.submit(category=solo)
        self.submit(self.killmail(self.other), user=self.other)

        self.assertEqual(rules.outdated_pending_claims(), [])

    def test_only_the_claims_of_a_changed_category_are_outdated(self):
        solo = self.category("Solo PVP", loss_cap_mode=Mode.CUSTOM, loss_cap=250_000_000)
        alliance_fleets = self.category("Alliance fleets", loss_cap_mode=Mode.CUSTOM, loss_cap=250_000_000)
        in_solo = self.submit(category=solo)
        in_alliance_fleets = self.submit(self.killmail(self.other), user=self.other, category=alliance_fleets)

        SrpCategory.objects.filter(pk=solo.pk).update(loss_cap=300_000_000)

        self.assertEqual(rules.outdated_pending_claims(), [in_solo.pk])
        review.recalculate_pending(actor=self.manager)
        self.assertEqual(rules.outdated_pending_claims(), [])
        self.assertEqual(SrpClaim.objects.get(pk=in_solo.pk).suggested_amount, 300_000_000)
        self.assertEqual(SrpClaim.objects.get(pk=in_alliance_fleets.pk).suggested_amount, 250_000_000)

    def test_every_effect_of_the_category_is_detected(self):
        changes = {
            "percentage": Decimal("60"),
            "loss_cap_mode": Mode.NONE,
            "cap_applies_to_rules": False,
            "personal_monthly_cap": 5_000_000_000,
            "corporation_monthly_cap": 5_000_000_000,
        }
        for field, value in changes.items():
            with self.subTest(field=field):
                SrpClaim.objects.all().delete()
                solo = self.category(f"Solo {field}", loss_cap_mode=Mode.CUSTOM, loss_cap=250_000_000)
                claim = self.submit(self.killmail(), category=solo)
                self.assertEqual(rules.outdated_pending_claims(), [])

                SrpCategory.objects.filter(pk=solo.pk).update(**{field: value})

                self.assertEqual(rules.outdated_pending_claims(), [claim.pk])

    def test_settings_that_do_not_change_what_a_category_pays_are_not_detected(self):
        solo = self.category("Solo PVP")
        self.submit(category=solo)

        SrpCategory.objects.filter(pk=solo.pk).update(description="Changed", sort_order=5, member_selectable=False)

        self.assertEqual(rules.outdated_pending_claims(), [])

    def test_a_rule_that_moved_to_another_category_is_detected(self):
        solo = self.category("Solo PVP")
        rule = f.payout_rule(FLYCATCHER, amount=50_000_000, category=solo)
        claim = self.submit(self.killmail(ship_type_id=FLYCATCHER), category=solo)
        self.assertEqual(rules.outdated_pending_claims(), [])

        rule.category = None
        rule.save()

        self.assertEqual(rules.outdated_pending_claims(), [claim.pk])

    def test_the_number_of_queries_does_not_grow_with_the_claims(self):
        solo = self.category("Solo PVP", loss_cap_mode=Mode.CUSTOM, loss_cap=250_000_000)
        self.submit(category=solo)
        with self.assertNumQueries(4):
            rules.outdated_pending_claims()
        for index in range(5):
            user = f.create_user(f"extra-{index}", perms=f.SRP_MEMBER_PERMS)
            self.submit(self.killmail(user), user=user, category=solo)

        with self.assertNumQueries(4):
            rules.outdated_pending_claims()

    def test_the_category_pages_show_the_notice(self):
        from fleetops.views_config import OUTDATED_CLAIM_SECTIONS

        self.assertIn("srp-categories", OUTDATED_CLAIM_SECTIONS)


class RecategorizeTests(CategoryBase):
    def setUp(self):
        super().setUp()
        self.solo = self.category(
            "Solo PVP", loss_cap_mode=Mode.CUSTOM, loss_cap=250_000_000, percentage=Decimal("50")
        )
        self.claim = self.submit(self.killmail(total="400000000"))

    def test_the_category_changes_and_the_amount_is_calculated_again(self):
        self.assertEqual(self.claim.suggested_amount, 400_000_000)

        claim = review.recategorize_claim(self.claim, actor=self.manager, category=self.solo, reason="It was a solo kill")

        self.assertEqual(claim.category, self.solo)
        self.assertEqual(claim.suggested_amount, 200_000_000)
        self.assertEqual(claim.policy["category"]["id"], self.solo.pk)
        self.assertEqual(SrpClaim.objects.get(pk=claim.pk).suggested_amount, 200_000_000)

    def test_the_timeline_and_the_audit_log_record_the_change(self):
        review.recategorize_claim(self.claim, actor=self.manager, category=self.solo, reason="It was a solo kill")

        event = self.claim.events.get(action=Action.RECATEGORIZE)
        self.assertEqual(event.reason, "It was a solo kill")
        self.assertFalse(event.internal)
        self.assertEqual((event.data["from_category"], event.data["to_category"]), (None, self.solo.key))
        self.assertEqual(event.data["previous"]["suggested_amount"], 400_000_000)
        entry = AuditLog.objects.get(action="srp.claim_recategorize")
        self.assertEqual(entry.old_value, {"category": None, "suggested_amount": 400_000_000})
        self.assertEqual(entry.new_value, {"category": self.solo.key, "suggested_amount": 200_000_000})
        self.assertEqual(entry.reason, "It was a solo kill")

    def test_the_claimant_sees_the_event(self):
        from fleetops.srp import access

        review.recategorize_claim(self.claim, actor=self.manager, category=self.solo, reason="It was a solo kill")

        self.client.force_login(self.user)
        response = self.client.get(f"/fleetops/srp/claims/{self.claim.uuid}/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Category changed")
        self.assertContains(response, "It was a solo kill")
        self.assertTrue(access.can_view_claim(self.user, self.claim))

    def test_a_claim_can_go_back_to_no_category(self):
        review.recategorize_claim(self.claim, actor=self.manager, category=self.solo, reason="Solo")

        claim = review.recategorize_claim(self.claim, actor=self.manager, category=None, reason="No, a gang")

        self.assertIsNone(claim.category)
        self.assertEqual(claim.suggested_amount, 400_000_000)

    def test_a_reason_is_required(self):
        with self.assertRaises(SrpError) as caught:
            review.recategorize_claim(self.claim, actor=self.manager, category=self.solo, reason="  ")
        self.assertEqual(caught.exception.code, "reason_required")

    def test_only_a_reviewer_may_do_it(self):
        for actor in (self.user, self.fc):
            with self.subTest(actor=actor.username):
                with self.assertRaises(SrpError) as caught:
                    review.recategorize_claim(self.claim, actor=actor, category=self.solo, reason="Solo")
                self.assertEqual(caught.exception.code, "forbidden")
        self.assertIsNone(SrpClaim.objects.get(pk=self.claim.pk).category)

    def test_a_manager_cannot_change_their_own_claim(self):
        own = self.submit(self.killmail(self.manager), user=self.manager)

        with self.assertRaises(SrpError) as caught:
            review.recategorize_claim(own, actor=self.manager, category=self.solo, reason="Solo")
        self.assertEqual(caught.exception.code, "own_claim")

    def test_only_pending_claims_can_change(self):
        approved = review.approve_claim(self.claim, actor=self.manager, amount=400_000_000, confirm_above_policy=False)

        with self.assertRaises(SrpStateError):
            review.recategorize_claim(approved, actor=self.manager, category=self.solo, reason="Solo")

    def test_the_same_category_is_refused(self):
        review.recategorize_claim(self.claim, actor=self.manager, category=self.solo, reason="Solo")

        with self.assertRaises(SrpError) as caught:
            review.recategorize_claim(self.claim, actor=self.manager, category=self.solo, reason="Again")
        self.assertEqual(caught.exception.code, "unchanged")

    def test_a_category_that_is_switched_off_is_refused(self):
        closed = self.category("Closed", is_active=False)

        with self.assertRaises(SrpError) as caught:
            review.recategorize_claim(self.claim, actor=self.manager, category=closed, reason="Solo")
        self.assertEqual(caught.exception.code, "category_unavailable")

    def test_the_reviewer_may_use_a_category_members_cannot_choose(self):
        hidden = self.category("Hidden", member_selectable=False)

        claim = review.recategorize_claim(self.claim, actor=self.manager, category=hidden, reason="Special case")

        self.assertEqual(claim.category, hidden)

    def test_the_monthly_limit_of_the_new_category_applies(self):
        limited = self.category("Limited", personal_monthly_cap=100_000_000)
        f.srp_claim(self.user, status=Status.APPROVED, amount=60_000_000, category=limited, time=SEPTEMBER)

        claim = review.recategorize_claim(self.claim, actor=self.manager, category=limited, reason="Limited")

        self.assertEqual(claim.limited_amount, 40_000_000)
        self.assertEqual(claim.calculation["binding_limit"], "category_personal")

    def test_the_page_offers_the_form_and_does_the_change(self):
        self.client.force_login(self.manager)
        url = f"/fleetops/srp/claims/{self.claim.uuid}/"

        page = self.client.get(url)
        self.assertContains(page, "Change the category")

        response = self.client.post(
            f"{url}recategorize/", {"category": self.solo.pk, "reason": "It was a solo kill"}
        )
        self.assertRedirects(response, url, fetch_redirect_response=False)
        self.assertEqual(SrpClaim.objects.get(pk=self.claim.pk).category, self.solo)

    def test_the_page_refuses_a_category_that_does_not_exist(self):
        self.client.force_login(self.manager)

        response = self.client.post(
            f"/fleetops/srp/claims/{self.claim.uuid}/recategorize/", {"category": 99999, "reason": "Solo"}
        )

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(SrpClaim.objects.get(pk=self.claim.pk).category)

    def test_a_member_cannot_use_the_action(self):
        self.client.force_login(self.user)

        response = self.client.post(
            f"/fleetops/srp/claims/{self.claim.uuid}/recategorize/", {"category": self.solo.pk, "reason": "Solo"}
        )

        self.assertEqual(response.status_code, 403)
        self.assertIsNone(SrpClaim.objects.get(pk=self.claim.pk).category)


class ReclassifyCategoryTests(CategoryBase):
    def test_moving_into_a_fleet_gives_the_fleet_category(self):
        solo = self.category("Solo PVP")
        operation, link = self.fleet()
        f.member_state(operation, self.main, first_seen=operation.started_at)
        claim = self.submit(self.killmail(time=operation.started_at), category=solo)

        moved = review.reclassify_claim(claim, actor=self.manager, scope=IN_FLEET, link=link, reason="It was a fleet")

        self.assertEqual(moved.category, self.fleet_category)
        self.assertEqual(moved.policy["category"]["key"], "fleet")
        self.assertEqual(moved.events.get(action=Action.RECLASSIFY).data["from_category"], solo.key)

    def test_moving_out_of_a_fleet_gives_the_default_category(self):
        solo = self.category("Solo PVP")
        f.srp_settings(default_category=solo)
        operation, link = self.fleet()
        f.member_state(operation, self.main, first_seen=operation.started_at)
        claim = self.submit(self.killmail(time=operation.started_at), link=link)

        moved = review.reclassify_claim(claim, actor=self.manager, scope=OUT_OF_FLEET, reason="Not a fleet loss")

        self.assertEqual(moved.category, solo)

    def test_moving_out_of_a_fleet_without_a_default_leaves_no_category(self):
        operation, link = self.fleet()
        f.member_state(operation, self.main, first_seen=operation.started_at)
        claim = self.submit(self.killmail(time=operation.started_at), link=link)

        moved = review.reclassify_claim(claim, actor=self.manager, scope=OUT_OF_FLEET, reason="Not a fleet loss")

        self.assertIsNone(moved.category)
        self.assertIsNone(moved.policy["category"])

    def test_the_reviewer_can_name_the_category_when_moving_out(self):
        solo = self.category("Solo PVP")
        operation, link = self.fleet()
        f.member_state(operation, self.main, first_seen=operation.started_at)
        claim = self.submit(self.killmail(time=operation.started_at), link=link)

        moved = review.reclassify_claim(
            claim, actor=self.manager, scope=OUT_OF_FLEET, reason="Solo kill", category=solo
        )

        self.assertEqual(moved.category, solo)

    def test_a_category_that_is_switched_off_is_refused_when_moving_out(self):
        closed = self.category("Closed", is_active=False)
        operation, link = self.fleet()
        f.member_state(operation, self.main, first_seen=operation.started_at)
        claim = self.submit(self.killmail(time=operation.started_at), link=link)

        with self.assertRaises(SrpError) as caught:
            review.reclassify_claim(claim, actor=self.manager, scope=OUT_OF_FLEET, reason="Solo", category=closed)
        self.assertEqual(caught.exception.code, "category_unavailable")

    def test_the_page_form_takes_a_category(self):
        solo = self.category("Solo PVP")
        operation, link = self.fleet()
        f.member_state(operation, self.main, first_seen=operation.started_at)
        claim = self.submit(self.killmail(time=operation.started_at), link=link)
        self.client.force_login(self.manager)

        self.client.post(
            f"/fleetops/srp/claims/{claim.uuid}/reclassify/",
            {"scope": "out_of_fleet", "link": "", "category": solo.pk, "reason": "Solo kill"},
        )

        self.assertEqual(SrpClaim.objects.get(pk=claim.pk).category, solo)


class NoCategoryLostTests(CategoryBase):
    """Every path that calculates a claim again keeps the category of the claim: a path that forgot it would
    quietly calculate with no category."""

    def setUp(self):
        super().setUp()
        self.solo = self.category(
            "Solo PVP", loss_cap_mode=Mode.CUSTOM, loss_cap=250_000_000, personal_monthly_cap=10_000_000_000
        )
        self.claim = self.submit(self.killmail(total="400000000"), category=self.solo)
        self.assertEqual(self.claim.suggested_amount, 250_000_000)

    def assert_kept(self, claim):
        stored = SrpClaim.objects.get(pk=claim.pk)
        self.assertEqual(stored.category, self.solo)
        self.assertEqual(stored.policy["category"]["id"], self.solo.pk)
        self.assertEqual(stored.suggested_amount, 250_000_000)

    def test_recalculating(self):
        self.assert_kept(review.recalculate_claim(self.claim, actor=self.manager))

    def test_recalculating_every_pending_claim(self):
        review.recalculate_pending(actor=self.manager)
        self.assert_kept(self.claim)

    def test_the_value_arriving_later(self):
        claims.recalculate_under_lock(self.claim, actor=None, reason="Value", action=Action.VALUE_UPDATE)
        self.assert_kept(self.claim)

    def test_withdrawing_and_submitting_again(self):
        claims.withdraw_claim(self.claim, actor=self.user)
        self.assert_kept(claims.resubmit_claim(self.claim, actor=self.user))

    def test_reopening_a_rejected_claim(self):
        review.reject_claim(self.claim, actor=self.manager, reason="No")
        self.assert_kept(review.reopen_claim(self.claim, actor=self.second_manager, reason="Yes"))

    def test_moving_to_another_fleet(self):
        operation, first = self.fleet()
        f.member_state(operation, self.main, first_seen=operation.started_at)
        claim = self.submit(self.killmail(time=operation.started_at, total="400000000"), link=first)
        claim_category = claim.category
        _operation, second = self.fleet()

        moved = review.reclassify_claim(claim, actor=self.manager, scope=IN_FLEET, link=second, reason="Other fleet")

        self.assertEqual(moved.category, claim_category)
        self.assertEqual(moved.policy["category"]["id"], claim_category.pk)

    def test_the_approval_limits_follow_the_snapshot(self):
        approved = review.approve_claim(self.claim, actor=self.manager, amount=250_000_000)
        self.assertEqual(approved.category, self.solo)

    def test_the_evaluation_needs_the_category(self):
        with self.assertRaises(TypeError):
            claims.evaluate(
                km=self.claim.killmail,
                user_id=self.user.pk,
                corporation_id=None,
                scope=OUT_OF_FLEET,
                operation=None,
                settings_obj=self.srp,
            )
