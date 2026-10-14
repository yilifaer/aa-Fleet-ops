# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""SRP value objects, the texts of stored codes and the template filters."""

import json
from datetime import datetime, timezone as dt_timezone
from decimal import Decimal

from django.template import Context, Template
from django.test import SimpleTestCase, TestCase
from django.utils import translation

from fleetops.models import SrpClaim, SrpClaimEvent
from fleetops.srp import display
from fleetops.srp.types import (
    BulkResult,
    CalcNote,
    Calculation,
    ClaimPreview,
    ClaimWarning,
    LimitState,
    PolicySnapshot,
    PresenceResult,
    RuleSnapshot,
    SrpDuplicateError,
    SrpError,
    SrpLimitError,
    SrpStateError,
    SrpTotals,
    warnings_from_json,
)

from . import factories as f

UTC = dt_timezone.utc


def render(template, **context):
    return Template("{% load fleetops_srp %}" + template).render(Context(context))


class IskFormatTests(SimpleTestCase):
    def test_formats(self):
        cases = [
            (None, "", "", ""),
            ("", "", "", ""),
            ("x", "", "", ""),
            (True, "", "", ""),
            (0, "0 ISK", "0 ISK", "0"),
            (999, "999 ISK", "999 ISK", "999"),
            (1000, "1,000 ISK", "1k ISK", "1000"),
            (1234, "1,234 ISK", "1.23k ISK", "1234"),
            (999_999, "999,999 ISK", "1m ISK", "999999"),
            (250_000_000, "250,000,000 ISK", "250m ISK", "250000000"),
            (999_999_999, "999,999,999 ISK", "1b ISK", "999999999"),
            (1_230_000_000, "1,230,000,000 ISK", "1.23b ISK", "1230000000"),
            (12_000_000_000_000, "12,000,000,000,000 ISK", "12t ISK", "12000000000000"),
            (Decimal("1234567.50"), "1,234,568 ISK", "1.23m ISK", "1234568"),
            ("2500000.4", "2,500,000 ISK", "2.5m ISK", "2500000"),
            (-5_500_000, "-5,500,000 ISK", "-5.5m ISK", "0"),
        ]
        for value, full, short, plain in cases:
            with self.subTest(value=value):
                self.assertEqual(display.format_isk(value), full)
                self.assertEqual(display.format_isk_short(value), short)
                self.assertEqual(display.format_isk_plain(value), plain)

    def test_non_finite_numbers_are_missing(self):
        for value in (Decimal("NaN"), Decimal("Infinity"), float("inf")):
            with self.subTest(value=value):
                self.assertEqual(display.format_isk(value), "")

    def test_numbers_too_large_for_isk_are_missing_instead_of_failing(self):
        for value in ("1e30", "-1e40", "1e999", 10**40):
            with self.subTest(value=value):
                self.assertEqual(display.format_isk(value), "")
                self.assertEqual(display.format_isk_short(value), "")
                self.assertEqual(display.format_isk_plain(value), "")

    def test_amounts_just_below_the_limit_are_written_out(self):
        self.assertEqual(display.format_isk(10**15), "1,000,000,000,000,000 ISK")
        self.assertEqual(display.format_isk_short(10**15), "1000t ISK")


class CodeTextTests(SimpleTestCase):
    def test_warning_texts(self):
        cases = [
            (
                "over_limit",
                {"kind": "personal", "requested": 300_000_000, "remaining": 10_000_000},
                "Over the monthly personal limit: 300,000,000 ISK requested, 10,000,000 ISK left.",
            ),
            (
                "over_limit",
                {"kind": "corporation", "requested": 5, "remaining": 0},
                "Over the monthly corporation limit: 5 ISK requested, 0 ISK left.",
            ),
            (
                "reduced_by_limit",
                {"kind": "personal", "requested": 300_000_000, "remaining": 10_000_000},
                "Reduced to the remaining monthly personal limit: 10,000,000 ISK of 300,000,000 ISK requested.",
            ),
            ("value_estimate", {}, "Value is an ESI market estimate."),
            ("value_pending", {}, "The value from zKillboard is not available yet."),
            ("value_missing", {}, "No value available."),
            ("external_claim", {"claims": ["aa-srp: Pending"]}, "Also claimed in aa-srp: Pending."),
            (
                "external_claim",
                {"claims": ["aa-srp: Pending", "Alliance Auth SRP: Approved"]},
                "Also claimed in aa-srp: Pending, Alliance Auth SRP: Approved.",
            ),
            ("ship_mismatch", {"ship": "Moa"}, "Tracked ship before the loss: Moa."),
        ]
        for code, params, text in cases:
            with self.subTest(code=code, params=params):
                self.assertEqual(display.warning_text(code, params), text)

    def test_a_tracked_ship_without_a_name_is_named_by_its_type_in_the_language_of_the_reader(self):
        params = {"ship": "", "ship_type_id": 587}

        self.assertEqual(display.warning_text("ship_mismatch", params), "Tracked ship before the loss: Type 587.")
        with translation.override("zh-hans"):
            text = display.warning_text("ship_mismatch", params)
        self.assertIn("类型 587", text)
        self.assertNotIn("Type", text)
        self.assertEqual(
            display.warning_text("ship_mismatch", {"ship": "", "ship_type_id": None}),
            "Tracked ship before the loss: another ship.",
        )

    def test_presence_texts(self):
        self.assertEqual(display.presence_text("in_fleet"), "In the fleet at the time of the loss.")
        self.assertEqual(display.presence_text("not_in_fleet"), "Not in the fleet at the time of the loss.")
        self.assertEqual(display.presence_text("never_seen"), "This character was never seen in the fleet.")
        self.assertEqual(
            display.presence_text("registered"), "No tracking data; registered for PAP in this fleet."
        )
        self.assertEqual(
            display.presence_text("outside_window"), "No tracking data; the loss is outside the time of the fleet."
        )
        self.assertEqual(display.presence_text("not_applicable"), "Not a fleet loss.")
        self.assertEqual(
            display.presence_text("near", {"direction": "join", "seconds": 40, "interval": 60}),
            "Not in the fleet at the loss time by the tracking data, but within 40 s of joining; "
            "tracking polls every 60 s.",
        )
        self.assertEqual(
            display.presence_text("near", {"direction": "leave", "seconds": 5, "interval": 30}),
            "Not in the fleet at the loss time by the tracking data, but within 5 s of leaving; "
            "tracking polls every 30 s.",
        )
        self.assertEqual(
            display.presence_text("near", {}), "Not in the fleet at the loss time by the tracking data."
        )

    def test_presence_codes_are_warnings_too(self):
        self.assertEqual(display.warning_text("never_seen"), display.presence_text("never_seen"))

    def test_note_texts(self):
        self.assertEqual(display.note_text("no_rule"), "No payout rule for this ship.")
        self.assertEqual(display.note_text("no_value"), "Value not available yet.")
        self.assertEqual(display.note_text("percentage_zero"), "The payout percentage is 0.")
        self.assertEqual(
            display.note_text("loss_cap", {"cap": 100_000_000}), "Capped at the per-loss cap of 100,000,000 ISK."
        )
        self.assertEqual(
            display.note_text("monthly_limit", {"kind": "corporation", "remaining": 7}),
            "Limited to the remaining monthly corporation limit of 7 ISK.",
        )

    def test_unknown_codes_and_bad_params_never_fail(self):
        self.assertEqual(display.warning_text("brand_new_code", {"x": 1}), "Brand new code")
        self.assertEqual(display.note_text("another_one"), "Another one")
        self.assertEqual(display.warning_text("over_limit", None), "Over the monthly limit: 0 ISK requested, 0 ISK left.")
        self.assertEqual(display.warning_text("external_claim", "oops"), "Also claimed in another SRP system.")
        self.assertEqual(display.warning_text("", None), "")

    def test_presence_levels(self):
        self.assertEqual(display.presence_level("in_fleet"), "ok")
        self.assertEqual(display.presence_level("near"), "warning")
        self.assertEqual(display.presence_level("registered"), "warning")
        self.assertEqual(display.presence_level("not_in_fleet"), "danger")
        self.assertEqual(display.presence_level("never_seen"), "danger")
        self.assertEqual(display.presence_level("outside_window"), "danger")
        self.assertEqual(display.presence_level("something_new"), "warning")

    def test_labels(self):
        self.assertEqual(display.limit_kind_label("loss_cap"), "per-loss cap")
        self.assertEqual(display.limit_kind_label("personal"), "personal monthly limit")
        self.assertEqual(display.match_label("doctrine_ship"), "Doctrine and ship")
        self.assertEqual(display.match_label("ship"), "Ship")
        self.assertEqual(display.match_label("odd_one"), "Odd one")


class ValueObjectTests(SimpleTestCase):
    def test_policy_snapshot_round_trip(self):
        policy = PolicySnapshot(
            value_source="zkill_total",
            percentage=Decimal("80.50"),
            in_fleet_loss_cap=500_000_000,
            personal_monthly_cap=1_000_000_000,
            personal_cap_includes_in_fleet=False,
            corporation_monthly_cap=2_000_000_000,
            corporation_cap_source="override",
            over_limit_action="reject",
            capsules_need_rule=False,
            claim_deadline_days=14,
            rule=RuleSnapshot(
                id=3,
                label="Frigates",
                ship_type_id=587,
                ship_type_name="Rifter",
                doctrine_name="Frigate Fleet",
                scope="in_fleet",
                amount=20_000_000,
                match="doctrine_ship",
            ),
        )

        stored = json.loads(json.dumps(policy.as_json()))

        self.assertEqual(PolicySnapshot.from_json(stored), policy)
        self.assertEqual(stored["version"], 1)
        self.assertEqual(stored["percentage"], "80.50")
        self.assertEqual(stored["rule"]["match"], "doctrine_ship")

    def test_policy_snapshot_without_rule_and_with_nothing_stored(self):
        self.assertIsNone(PolicySnapshot.from_json(PolicySnapshot().as_json()).rule)
        self.assertEqual(PolicySnapshot.from_json({}), PolicySnapshot())
        self.assertEqual(PolicySnapshot.from_json(None), PolicySnapshot())
        self.assertEqual(PolicySnapshot.from_json({"percentage": "junk", "rule": "junk"}), PolicySnapshot())

    def test_policy_keys_follow_the_design(self):
        self.assertEqual(
            set(PolicySnapshot().as_json()),
            {
                "version",
                "value_source",
                "percentage",
                "in_fleet_loss_cap",
                "out_of_fleet_loss_cap",
                "personal_monthly_cap",
                "personal_cap_includes_in_fleet",
                "corporation_monthly_cap",
                "corporation_cap_source",
                "corporation_cap_includes_in_fleet",
                "over_limit_action",
                "capsules_need_rule",
                "claim_deadline_days",
                "rule",
            },
        )

    def test_calculation_round_trip(self):
        calculation = Calculation(
            basis="percentage",
            loss_value=Decimal("123456789.50"),
            value_source="zkill_fitted",
            base=98_765_431,
            cap=50_000_000,
            suggested=50_000_000,
            limits=(
                LimitState("personal", cap=100, used=60, pending=10, remaining=40),
                LimitState("corporation", cap=None, used=0, pending=0, remaining=None, applies=False),
            ),
            limited=40,
            over_limit=True,
            binding_limit="personal",
            notes=(CalcNote("loss_cap", {"cap": 50_000_000}), CalcNote("no_rule")),
            computed_at=datetime(2026, 10, 1, 12, 30, 5, tzinfo=UTC),
        )

        stored = json.loads(json.dumps(calculation.as_json()))

        self.assertEqual(Calculation.from_json(stored), calculation)
        self.assertEqual(stored["loss_value"], "123456789.50")
        self.assertEqual(stored["limits"][0]["remaining"], 40)

    def test_calculation_from_nothing(self):
        for stored in ({}, None, "junk"):
            with self.subTest(stored=stored):
                result = Calculation.from_json(stored)
                self.assertEqual((result.basis, result.suggested, result.limits, result.notes), ("none", 0, (), ()))

    def test_calculation_limit_lookup(self):
        calculation = Calculation(limits=(LimitState("personal"), LimitState("corporation", cap=5)))

        self.assertEqual(calculation.limit("corporation").cap, 5)
        self.assertIsNone(calculation.limit("loss_cap"))

    def test_effective_remaining(self):
        self.assertEqual(LimitState("personal", cap=10, used=4, remaining=6).effective_remaining, 6)
        self.assertIsNone(LimitState("personal", cap=10, used=4, remaining=6, applies=False).effective_remaining)
        self.assertIsNone(LimitState("personal").effective_remaining)

    def test_claim_warnings(self):
        warning = ClaimWarning("over_limit", "danger", {"kind": "personal", "requested": 5, "remaining": 1})

        self.assertEqual(ClaimWarning.from_json(json.loads(json.dumps(warning.as_json()))), warning)
        self.assertIn("personal limit", warning.text)

    def test_warnings_from_json_skips_what_cannot_be_read(self):
        stored = [
            {"code": "value_estimate", "level": "info", "params": {}},
            "junk",
            {"level": "danger"},
            {"code": "ship_mismatch", "params": "junk"},
            None,
        ]

        found = warnings_from_json(stored)

        self.assertEqual([warning.code for warning in found], ["value_estimate", "ship_mismatch"])
        self.assertEqual(found[1].params, {})
        self.assertEqual(found[1].level, "warning")
        self.assertEqual(warnings_from_json(None), [])

    def test_presence_result(self):
        result = PresenceResult("near", "warning", {"direction": "join", "seconds": 10, "interval": 60}, 587, "Rifter")

        self.assertIn("within 10 s of joining", result.text)
        self.assertEqual(result.as_json()["tracked_ship_name"], "Rifter")
        self.assertEqual(PresenceResult("in_fleet").level, "ok")
        json.dumps(result.as_json())

    def test_calc_note(self):
        note = CalcNote("no_rule")

        self.assertEqual(note.text, "No payout rule for this ship.")
        self.assertEqual(CalcNote.from_json(note.as_json()), note)
        self.assertIsNone(CalcNote.from_json({"params": {}}))

    def test_errors(self):
        error = SrpError("unrecognised", "Paste a link.")
        self.assertEqual((error.code, error.message, str(error)), ("unrecognised", "Paste a link.", "Paste a link."))
        for cls in (SrpStateError, SrpLimitError):
            with self.subTest(cls=cls.__name__):
                self.assertTrue(issubclass(cls, SrpError))
                self.assertEqual(cls("state", "Already approved.").message, "Already approved.")
        duplicate = SrpDuplicateError("Already claimed.", claim="the claim")
        self.assertIsInstance(duplicate, SrpError)
        self.assertEqual((duplicate.code, duplicate.claim), ("duplicate", "the claim"))

    def test_defaults_are_not_shared_between_instances(self):
        first, second = ClaimPreview(killmail=None, scope="out_of_fleet"), ClaimPreview(killmail=None, scope="out_of_fleet")
        first.blockers.append("x")
        first.warnings.append(ClaimWarning("value_estimate"))
        result, other = BulkResult(), BulkResult()
        result.done.append(1)

        self.assertEqual((second.blockers, second.warnings, other.done, other.skipped), ([], [], [], []))
        self.assertEqual(SrpTotals().outstanding, 0)


class TemplateFilterTests(TestCase):
    def test_isk_filters(self):
        self.assertEqual(render("{{ v|isk }}|{{ v|isk_short }}|{{ v|isk_plain }}", v=1_234_567_890), "1,234,567,890 ISK|1.23b ISK|1234567890")
        self.assertEqual(render("{{ v|isk }}|{{ v|isk_short }}|{{ v|isk_plain }}", v=None), "||")
        self.assertEqual(render("{{ v|isk|default:'n/a' }}", v=None), "n/a")

    def test_status_badges(self):
        expected = {
            "pending": ("text-bg-warning", "Pending"),
            "approved": ("text-bg-primary", "Approved"),
            "paid": ("text-bg-success", "Paid"),
            "rejected": ("text-bg-danger", "Rejected"),
            "withdrawn": ("text-bg-secondary", "Withdrawn"),
        }
        for status, (css, label) in expected.items():
            with self.subTest(status=status):
                self.assertEqual(render("{{ s|srp_status_badge }}", s=status), f'<span class="badge {css}">{label}</span>')
                self.assertEqual(render("{{ s|srp_status_label }}", s=status), label)

    def test_status_badge_of_a_claim_and_of_an_unknown_status(self):
        claim = SrpClaim(status="paid")

        self.assertIn("Paid", render("{{ c|srp_status_badge }}", c=claim))
        self.assertEqual(render("{{ s|srp_status_badge }}", s="odd"), '<span class="badge text-bg-secondary">odd</span>')

    def test_warning_badge_from_stored_json(self):
        warning = {"code": "over_limit", "level": "danger", "params": {"kind": "personal", "requested": 5, "remaining": 1}}

        html = render("{{ w|srp_warning_badge }}", w=warning)

        self.assertEqual(
            html,
            '<span class="badge fleetops-srp-warning text-bg-danger">'
            "Over the monthly personal limit: 5 ISK requested, 1 ISK left.</span>",
        )
        self.assertEqual(render("{{ w|srp_warning_text }}", w=warning), "Over the monthly personal limit: 5 ISK requested, 1 ISK left.")

    def test_warning_levels_have_their_own_colour(self):
        for level, css in (("danger", "text-bg-danger"), ("warning", "text-bg-warning"), ("info", "text-bg-info")):
            with self.subTest(level=level):
                self.assertIn(css, render("{{ w|srp_warning_badge }}", w=ClaimWarning("value_estimate", level)))

    def test_warning_text_is_escaped(self):
        warning = ClaimWarning("ship_mismatch", "warning", {"ship": "<script>alert(1)</script>"})

        html = render("{{ w|srp_warning_badge }}", w=warning)

        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)

    def test_unreadable_warnings_render_nothing(self):
        self.assertEqual(render("{{ w|srp_warning_badge }}", w="junk"), "")
        self.assertEqual(render("{{ w|srp_warning_text }}", w=None), "")

    def test_warnings_of_a_claim(self):
        pilot = f.create_user(perms=f.SRP_MEMBER_PERMS)
        claim = f.srp_claim(
            pilot,
            warnings=[
                {"code": "value_estimate", "level": "info", "params": {}},
                {"code": "external_claim", "level": "warning", "params": {"claims": ["aa-srp: Pending"]}},
            ],
        )

        html = render("{% for w in c|srp_warnings %}[{{ w.level }}:{{ w.text }}]{% endfor %}", c=claim)

        self.assertEqual(html, "[info:Value is an ESI market estimate.][warning:Also claimed in aa-srp: Pending.]")

    def test_flags_leave_out_the_fleet_check_that_the_pages_show_as_a_badge(self):
        pilot = f.create_user(perms=f.SRP_MEMBER_PERMS)
        claim = f.srp_claim(
            pilot,
            fleet_check="never_seen",
            warnings=[
                {"code": "never_seen", "level": "danger", "params": {}},
                {"code": "ship_mismatch", "level": "info", "params": {"ship": "Moa"}},
                {"code": "external_claim", "level": "warning", "params": {"claims": ["aa-srp: Pending"]}},
                "junk",
            ],
        )
        template = "{% for w in c|srp_flags %}[{{ w.code }}]{% endfor %}"

        self.assertEqual(render(template, c=claim), "[ship_mismatch][external_claim]")
        # The stored list and the warnings of a preview work as well.
        self.assertEqual(render(template, c=claim.warnings), "[ship_mismatch][external_claim]")
        self.assertEqual(
            render(template, c=[ClaimWarning("near"), ClaimWarning("over_limit"), None]), "[over_limit]"
        )
        self.assertEqual(render(template, c=None), "")
        # The claim itself still stores the check as a warning.
        self.assertIn("never_seen", [w.code for w in warnings_from_json(claim.warnings)])

    def test_actor_labels_follow_the_kind_of_step(self):
        pilot = f.create_user("claimant", perms=f.SRP_MEMBER_PERMS)
        reviewer = f.create_user("reviewer", perms=f.SRP_MANAGER_PERMS)
        viewer = f.create_user("viewer", perms=f.MEMBER_PERMS)
        claim = f.srp_claim(pilot)
        actions = SrpClaimEvent.Action
        cases = [
            (actions.SUBMIT, pilot, "Claimant"),
            (actions.WITHDRAW, pilot, "Claimant"),
            (actions.RESUBMIT, pilot, "Claimant"),
            (actions.AUTO_REJECT, pilot, "System"),
            (actions.APPROVE, reviewer, "Reviewer"),
            (actions.PAID, reviewer, "Reviewer"),
            (actions.RECALCULATE, reviewer, "Reviewer"),
            (actions.RECALCULATE, None, "System"),
            (actions.VALUE_UPDATE, None, "System"),
            # A manager who decides on their own claim is a reviewer for that step.
            (actions.APPROVE, pilot, "Reviewer"),
        ]
        for action, actor, expected in cases:
            event = SrpClaimEvent.objects.create(claim=claim, action=action, actor=actor)
            with self.subTest(action=action, actor=getattr(actor, "username", None)):
                self.assertEqual(render("{{ e|srp_actor:v }}", e=event, v=viewer), expected)
        own = SrpClaimEvent.objects.create(claim=claim, action=actions.APPROVE, actor=viewer)
        self.assertEqual(render("{{ e|srp_actor:v }}", e=own, v=viewer), "You")

    def test_fleet_check_of_a_claim(self):
        pilot = f.create_user(perms=f.SRP_MEMBER_PERMS)
        near = f.srp_claim(pilot, fleet_check="near", fleet_check_params={"direction": "join", "seconds": 20, "interval": 60})
        clean = f.srp_claim(pilot, fleet_check="in_fleet")
        none = f.srp_claim(pilot)

        self.assertIn("within 20 s of joining", render("{{ c|srp_check_text }}", c=near))
        self.assertIn("text-bg-warning", render("{{ c|srp_check_badge }}", c=near))
        self.assertIn("text-bg-success", render("{{ c|srp_check_badge }}", c=clean))
        self.assertEqual(render("{{ c|srp_check_text }}{{ c|srp_check_badge }}", c=none), "")

    def test_row_classes(self):
        pilot = f.create_user(perms=f.SRP_MEMBER_PERMS)
        plain = f.srp_claim(pilot)
        over = f.srp_claim(pilot, over_limit=True, warnings=[{"code": "over_limit", "level": "danger", "params": {}}])
        estimate = f.srp_claim(pilot, warnings=[{"code": "value_estimate", "level": "warning", "params": {}}])
        info_only = f.srp_claim(pilot, warnings=[{"code": "value_pending", "level": "info", "params": {}}])
        absent = f.srp_claim(pilot, fleet_check="not_in_fleet")
        present = f.srp_claim(pilot, fleet_check="in_fleet")

        classes = {
            "plain": render("{{ c|srp_row_class }}", c=plain),
            "over": render("{{ c|srp_row_class }}", c=over),
            "estimate": render("{{ c|srp_row_class }}", c=estimate),
            "info_only": render("{{ c|srp_row_class }}", c=info_only),
            "absent": render("{{ c|srp_row_class }}", c=absent),
            "present": render("{{ c|srp_row_class }}", c=present),
        }
        self.assertEqual(
            classes,
            {
                "plain": "",
                "over": "fleetops-srp-over-limit",
                "estimate": "fleetops-srp-warning",
                "info_only": "",
                "absent": "fleetops-srp-warning",
                "present": "",
            },
        )

    def test_calculation_and_policy_of_a_claim(self):
        pilot = f.create_user(perms=f.SRP_MEMBER_PERMS)
        claim = f.srp_claim(pilot, amount=12_000_000)
        claim.calculation = Calculation(
            basis="rule", base=12_000_000, suggested=12_000_000, limited=12_000_000, notes=(CalcNote("no_rule"),)
        ).as_json()

        html = render(
            "{% with calc=c|srp_calculation policy=c|srp_policy %}"
            "{{ calc.basis }}|{{ calc.suggested|isk }}|{% for n in calc.notes %}{{ n.text }}{% endfor %}|{{ policy.version }}"
            "{% endwith %}",
            c=claim,
        )

        self.assertEqual(html, "rule|12,000,000 ISK|No payout rule for this ship.|1")

    def test_limit_and_match_labels(self):
        self.assertEqual(render("{{ k|srp_limit_label }}|{{ m|srp_match_label }}", k="corporation", m="doctrine"), "corporation monthly limit|Doctrine")

    def test_links_and_images(self):
        self.assertEqual(render("{{ id|zkill_url }}", id=131234567), "https://zkillboard.com/kill/131234567/")
        self.assertEqual(render("{{ id|zkill_url }}", id="junk"), "")
        pilot = f.create_user(perms=f.SRP_MEMBER_PERMS)
        claim = f.srp_claim(pilot, killmail=f.srp_killmail(f.main_of(pilot), killmail_id=777))
        self.assertEqual(render("{{ c.killmail|zkill_url }}", c=claim), "https://zkillboard.com/kill/777/")
        self.assertEqual(render("{% ship_render_url 587 %}"), "https://images.evetech.net/types/587/render?size=64")
        self.assertEqual(render("{% ship_render_url 587 128 %}"), "https://images.evetech.net/types/587/render?size=128")
        self.assertEqual(render("{% ship_render_url none %}", none=None), "")
