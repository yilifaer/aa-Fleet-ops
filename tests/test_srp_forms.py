# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""The ISK input and the forms of the SRP pages."""

import uuid
from types import SimpleNamespace

from django import forms
from django.test import SimpleTestCase

from fleetops.constants import MAX_ISK
from fleetops.forms_srp import (
    ApproveForm,
    ClaimInputForm,
    ClaimSelectionForm,
    IskField,
    NoteForm,
    PaidForm,
    PaymentFilterForm,
    ReclassifyForm,
    ReasonForm,
    ReviewFilterForm,
    SubmitClaimForm,
    WithdrawForm,
    parse_isk,
)


class AmountForm(forms.Form):
    amount = IskField()


class OptionalAmountForm(forms.Form):
    amount = IskField(required=False, min_value=1_000_000)


def amount_of(text):
    form = AmountForm({"amount": text})
    return form.cleaned_data["amount"] if form.is_valid() else None


class ParseIskTests(SimpleTestCase):
    def test_plain_numbers(self):
        self.assertEqual(parse_isk("250000000"), 250_000_000)
        self.assertEqual(parse_isk("  42 "), 42)

    def test_suffixes_in_either_case(self):
        for text, expected in (
            ("250m", 250_000_000),
            ("250M", 250_000_000),
            ("1.2b", 1_200_000_000),
            ("1b", 1_000_000_000),
            ("750k", 750_000),
            ("1.5m", 1_500_000),
            ("0.5k", 500),
            ("250 m", 250_000_000),
            ("250m ISK", 250_000_000),
            ("1,000m", 1_000_000_000),
        ):
            with self.subTest(text=text):
                self.assertEqual(parse_isk(text), expected)

    def test_thousands_separators(self):
        for text, expected in (
            ("250,000,000", 250_000_000),
            ("1 000 000", 1_000_000),
            ("1\u00a0000\u00a0000", 1_000_000),
            ("1\u202f000\u202f000", 1_000_000),
            ("1,000,000 ISK", 1_000_000),
            ("12,345.5k", 12_345_500),
        ):
            with self.subTest(text=text):
                self.assertEqual(parse_isk(text), expected)

    def test_a_point_is_the_only_decimal_separator(self):
        with self.assertRaisesMessage(ValueError, "decimals with a point"):
            parse_isk("1,5m")
        with self.assertRaises(ValueError):
            parse_isk("1,00")
        with self.assertRaises(ValueError):
            parse_isk("1.000.000")

    def test_fractions_of_an_isk_are_refused(self):
        with self.assertRaisesMessage(ValueError, "whole numbers"):
            parse_isk("1.5")
        with self.assertRaisesMessage(ValueError, "whole numbers"):
            parse_isk("0.0001k")

    def test_garbage_is_refused(self):
        for text in ("", "abc", "m", "1e9", "1m2", "0x10", "1 0 0", "١٢٣", "1_000", "250mm", "NaN", "1" * 60):
            with self.subTest(text=text):
                with self.assertRaises(ValueError):
                    parse_isk(text)

    def test_negative_amounts_are_refused(self):
        with self.assertRaisesMessage(ValueError, "positive"):
            parse_isk("-5m")


class IskFieldTests(SimpleTestCase):
    def test_cleaned_value_is_an_int(self):
        self.assertEqual(amount_of("250m"), 250_000_000)
        self.assertIsInstance(amount_of("1.2b"), int)

    def test_amount_bounds(self):
        self.assertIsNone(amount_of("0"))
        self.assertEqual(amount_of("1"), 1)
        self.assertEqual(amount_of(f"{MAX_ISK}"), MAX_ISK)
        self.assertIsNone(amount_of(f"{MAX_ISK + 1}"))
        self.assertIsNone(amount_of("-1"))
        self.assertIn("at least 1 ISK", AmountForm({"amount": "0"}).errors["amount"][0])
        self.assertIn("1,000,000,000,000,000", AmountForm({"amount": str(MAX_ISK + 1)}).errors["amount"][0])

    def test_required_and_optional(self):
        self.assertFalse(AmountForm({"amount": ""}).is_valid())
        form = OptionalAmountForm({"amount": " "})
        self.assertTrue(form.is_valid())
        self.assertIsNone(form.cleaned_data["amount"])
        self.assertFalse(OptionalAmountForm({"amount": "999999"}).is_valid())

    def test_initial_integers_are_shown_with_separators(self):
        form = AmountForm(initial={"amount": 250_000_000})
        self.assertIn('value="250,000,000"', str(form["amount"]))
        self.assertIn('placeholder="e.g. 250m"', str(form["amount"]))

    def test_unchanged_amount_in_another_notation_is_not_a_change(self):
        form = AmountForm({"amount": "250m"}, initial={"amount": 250_000_000})
        self.assertFalse(form.has_changed())
        form = AmountForm({"amount": "251m"}, initial={"amount": 250_000_000})
        self.assertTrue(form.has_changed())
        form = AmountForm({"amount": "junk"}, initial={"amount": 250_000_000})
        self.assertTrue(form.has_changed())

    def test_booleans_are_not_amounts(self):
        with self.assertRaises(forms.ValidationError):
            IskField().to_python(True)


class SrpFormTests(SimpleTestCase):
    def test_claim_input_is_stripped_and_limited(self):
        form = ClaimInputForm({"killmail": "  https://zkillboard.com/kill/123/  "})
        self.assertTrue(form.is_valid())
        self.assertEqual(form.cleaned_data["killmail"], "https://zkillboard.com/kill/123/")
        self.assertFalse(ClaimInputForm({"killmail": "x" * 2001}).is_valid())
        self.assertFalse(ClaimInputForm({"killmail": "   "}).is_valid())

    def test_reason_is_required(self):
        self.assertFalse(ReasonForm({"reason": " "}).is_valid())
        self.assertTrue(ReasonForm({"reason": "Wrong fleet"}).is_valid())

    def test_approve_amount_buttons(self):
        claim = SimpleNamespace(suggested_amount=300_000_000, limited_amount=200_000_000)
        for mode, expected in (("suggested", 300_000_000), ("within_limit", 200_000_000)):
            form = ApproveForm({"mode": mode})
            self.assertTrue(form.is_valid(), form.errors)
            self.assertEqual(form.amount_for(claim), expected)
        form = ApproveForm({"mode": "amount", "amount": "250m", "reason": "Fit was cheaper"})
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.amount_for(claim), 250_000_000)

    def test_approve_buttons_stand_for_the_amounts_they_showed(self):
        claim = SimpleNamespace(suggested_amount=300_000_000, limited_amount=200_000_000)
        shown = {"shown_status": "pending", "shown_suggested": "500000", "shown_limited": "400000"}
        for mode, amount, expected in (
            ("suggested", 500_000, {"expected_suggested": 500_000, "expected_limited": None}),
            ("within_limit", 400_000, {"expected_suggested": None, "expected_limited": 400_000}),
            ("amount", None, {"expected_suggested": None, "expected_limited": None}),
        ):
            with self.subTest(mode=mode):
                form = ApproveForm({"mode": mode, "amount": "9m", **shown})
                self.assertTrue(form.is_valid(), form.errors)
                self.assertEqual(form.amount_for(claim), amount or 9_000_000)
                self.assertEqual(form.expected(), {"expected_status": "pending", **expected})

    def test_approve_without_shown_values_compares_nothing(self):
        form = ApproveForm({"mode": "suggested"})
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.expected(), {"expected_status": None, "expected_suggested": None, "expected_limited": None})

    def test_unreadable_shown_values_are_refused(self):
        for bad in ({"shown_status": "paid"}, {"shown_suggested": "x"}, {"shown_limited": "-1"}, {"shown_suggested": str(MAX_ISK + 1)}):
            with self.subTest(bad=bad):
                self.assertFalse(ApproveForm({"mode": "suggested", **bad}).is_valid())

    def test_the_paid_form_carries_the_shown_amount(self):
        form = PaidForm({"shown_amount": "10000000"})
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["shown_amount"], 10_000_000)
        empty = PaidForm({})
        self.assertTrue(empty.is_valid())
        self.assertIsNone(empty.cleaned_data["shown_amount"])
        self.assertFalse(PaidForm({"shown_amount": "ten"}).is_valid())
        self.assertFalse(PaidForm({"shown_amount": "-3"}).is_valid())

    def test_approve_amount_needs_an_amount(self):
        form = ApproveForm({"mode": "amount"})
        self.assertFalse(form.is_valid())
        self.assertIn("amount", form.errors)
        self.assertFalse(ApproveForm({"mode": "other"}).is_valid())

    def test_reclassify_in_fleet_needs_a_link(self):
        form = ReclassifyForm({"scope": "in_fleet", "reason": "Lost in the fleet"})
        self.assertFalse(form.is_valid())
        self.assertIn("link", form.errors)
        self.assertTrue(ReclassifyForm({"scope": "out_of_fleet", "reason": "Not in the fleet"}).is_valid())

    def test_claim_selection(self):
        first, second = uuid.uuid4(), uuid.uuid4()
        form = ClaimSelectionForm({"claim": [str(first), str(second), str(first)]})
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["claim"], [first, second])
        self.assertFalse(ClaimSelectionForm({"claim": ["not-a-uuid"]}).is_valid())
        self.assertFalse(ClaimSelectionForm({}).is_valid())
        many = [str(uuid.uuid4()) for _ in range(201)]
        self.assertFalse(ClaimSelectionForm({"claim": many}).is_valid())


class ShownAmountsTests(SimpleTestCase):
    def test_the_amount_each_ticked_row_showed(self):
        first, second, third = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        form = ClaimSelectionForm(
            {"claim": [str(first), str(second), str(third)], f"shown-{first}": "500000", f"shown-{second}": "0"}
        )
        self.assertTrue(form.is_valid(), form.errors)

        self.assertEqual(form.shown_amounts(), {first: 500_000, second: 0})

    def test_unreadable_or_missing_amounts_are_left_out(self):
        first, second, third, other = uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        data = {
            "claim": [str(first), str(second), str(third)],
            f"shown-{first}": "12 000",
            f"shown-{second}": "-5",
            f"shown-{other}": "99",
            f"shown-{third}": "١٢٣",
        }
        form = ClaimSelectionForm(data)
        self.assertTrue(form.is_valid(), form.errors)

        # Not ticked, negative, with spaces or in other digits: no amount to compare.
        self.assertEqual(form.shown_amounts(), {})

    def test_an_absurdly_long_number_is_ignored(self):
        claim = uuid.uuid4()
        form = ClaimSelectionForm({"claim": [str(claim)], f"shown-{claim}": "9" * 40})
        self.assertTrue(form.is_valid())
        self.assertEqual(form.shown_amounts(), {})


class ClaimInputFormTests(SimpleTestCase):
    HASH = "ab" * 20

    def test_every_accepted_input_form_passes_and_keeps_its_reference(self):
        for text, killmail_id in (
            (f"<url=killReport:123456:{self.HASH}>Rifter</url>", 123456),
            (f'<a href="killReport:123456:{self.HASH}">Rifter</a>', 123456),
            (f"https://esi.evetech.net/latest/killmails/123456/{self.HASH}/?datasource=tranquility", 123456),
            (f"123456 {self.HASH}", 123456),
            ("https://zkillboard.com/kill/123456/", 123456),
            ("zkillboard.com/kill/123456", 123456),
            ("https://eve-kill.com/kill/123456", 123456),
        ):
            with self.subTest(text=text):
                form = ClaimInputForm({"killmail": text})
                self.assertTrue(form.is_valid(), form.errors)
                self.assertEqual(form.reference.killmail_id, killmail_id)

    def test_text_without_a_killmail_is_refused_with_the_hint(self):
        for text in ("hello", "https://example.com/kill/1/", "12345", "killReport:1:abc"):
            with self.subTest(text=text):
                form = ClaimInputForm({"killmail": text})
                self.assertFalse(form.is_valid())
                self.assertEqual(
                    form.errors["killmail"],
                    ["Paste an in-game killmail link, a zKillboard link, or the killmail ID and hash."],
                )

    def test_a_killmail_id_that_is_not_an_id_is_refused(self):
        form = ClaimInputForm({"killmail": "https://zkillboard.com/kill/" + "9" * 19 + "/"})

        self.assertFalse(form.is_valid())
        self.assertIn("killmail", form.errors)

    def test_the_field_is_styled_and_has_a_placeholder(self):
        html = str(ClaimInputForm()["killmail"])

        self.assertIn("form-control", html)
        self.assertIn("In-game killmail link, zKillboard link, or killmail ID and hash", html)


class SubmitFormTests(SimpleTestCase):
    def test_token_is_required_and_the_comment_is_optional(self):
        self.assertTrue(SubmitClaimForm({"token": "abc"}).is_valid())
        self.assertFalse(SubmitClaimForm({"comment": "x"}).is_valid())

    def test_comment_is_limited_to_1000_characters(self):
        self.assertTrue(SubmitClaimForm({"token": "abc", "comment": "x" * 1000}).is_valid())
        self.assertFalse(SubmitClaimForm({"token": "abc", "comment": "x" * 1001}).is_valid())

    def test_token_is_a_hidden_field(self):
        self.assertIn('type="hidden"', str(SubmitClaimForm(initial={"token": "abc"})["token"]))


class ReasonFormsTests(SimpleTestCase):
    def test_withdraw_reason_is_optional(self):
        self.assertTrue(WithdrawForm({}).is_valid())
        form = WithdrawForm({"reason": " Wrong ship "})
        self.assertTrue(form.is_valid())
        self.assertEqual(form.cleaned_data, {"reason": "Wrong ship"})

    def test_reasons_and_notes_are_limited(self):
        self.assertFalse(ReasonForm({"reason": "x" * 1001}).is_valid())
        self.assertFalse(NoteForm({"text": ""}).is_valid())
        self.assertFalse(NoteForm({"text": "x" * 1001}).is_valid())
        self.assertTrue(NoteForm({"text": "Asked the pilot"}).is_valid())

    def test_payment_note_is_optional_and_limited_to_255_characters(self):
        self.assertTrue(PaidForm({}).is_valid())
        self.assertTrue(PaidForm({"note": "x" * 255}).is_valid())
        self.assertFalse(PaidForm({"note": "x" * 256}).is_valid())

    def test_each_form_can_have_its_own_element_ids(self):
        first = str(ReasonForm(auto_id="id_reject_%s")["reason"])
        second = str(ReasonForm(auto_id="id_revoke_%s")["reason"])

        self.assertIn('id="id_reject_reason"', first)
        self.assertIn('id="id_revoke_reason"', second)

    def test_approve_modes_are_the_three_buttons(self):
        self.assertEqual(
            [value for value, _label in ApproveForm.base_fields["mode"].choices],
            ["suggested", "within_limit", "amount"],
        )

    def test_approve_confirmations_default_to_off(self):
        form = ApproveForm({"mode": "suggested"})

        self.assertTrue(form.is_valid(), form.errors)
        self.assertFalse(form.cleaned_data["confirm_above_policy"])
        self.assertFalse(form.cleaned_data["confirm_over_limit"])

    def test_approve_amount_is_checked_against_the_isk_limits(self):
        self.assertFalse(ApproveForm({"mode": "amount", "amount": "0"}).is_valid())
        self.assertFalse(ApproveForm({"mode": "amount", "amount": str(MAX_ISK + 1)}).is_valid())
        self.assertFalse(ApproveForm({"mode": "amount", "amount": "1,5m"}).is_valid())
        self.assertTrue(ApproveForm({"mode": "amount", "amount": "1 000 000", "reason": "x"}).is_valid())


class ReviewFilterFormTests(SimpleTestCase):
    def test_choices_are_the_corporations_and_fcs_given(self):
        form = ReviewFilterForm(corporations=[(98_000_001, "Test Corp"), (98_000_002, "Other Corp")], fcs=[(7, "Fiona")])

        self.assertEqual(
            form.fields["corporation"].choices,
            [("", "Any corporation"), ("98000001", "Test Corp"), ("98000002", "Other Corp")],
        )
        self.assertEqual(form.fields["fc"].choices, [("", "Any FC"), ("7", "Fiona")])

    def test_values_come_back_as_numbers(self):
        form = ReviewFilterForm(
            {"status": "approved", "scope": "in_fleet", "flag": "over_limit", "corporation": "98000001", "fc": "7", "q": " pilot "},
            corporations=[(98_000_001, "Test Corp")],
            fcs=[(7, "Fiona")],
        )

        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(
            form.cleaned_data,
            {"status": "approved", "scope": "in_fleet", "flag": "over_limit", "corporation": 98_000_001, "fc": 7, "q": "pilot"},
        )

    def test_values_that_do_not_check_out_are_dropped_and_the_rest_kept(self):
        form = ReviewFilterForm({"status": "zzz", "scope": "in_fleet", "corporation": "98000009", "fc": "x", "q": "ship"})

        self.assertFalse(form.is_valid())
        self.assertEqual(form.cleaned_data, {"scope": "in_fleet", "flag": "", "q": "ship"})
        self.assertEqual(set(form.errors), {"status", "corporation", "fc"})

    def test_any_status_is_a_value_of_its_own(self):
        form = ReviewFilterForm({"status": ReviewFilterForm.ANY_STATUS})

        self.assertTrue(form.is_valid())
        self.assertEqual(form.cleaned_data["status"], "all")

    def test_search_is_limited_to_100_characters(self):
        self.assertFalse(ReviewFilterForm({"q": "x" * 101}).is_valid())


class PaymentFilterFormTests(SimpleTestCase):
    def test_dates_are_read_as_iso_dates(self):
        form = PaymentFilterForm({"paid_from": "2026-04-01", "paid_to": "2026-04-30"})

        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(str(form.cleaned_data["paid_from"]), "2026-04-01")
        self.assertEqual(str(form.cleaned_data["paid_to"]), "2026-04-30")

    def test_either_date_may_be_missing_and_a_bad_one_is_dropped(self):
        form = PaymentFilterForm({"paid_from": "", "paid_to": "next week"})

        self.assertFalse(form.is_valid())
        self.assertEqual(form.cleaned_data, {"paid_from": None})


class ReclassifyFormExtraTests(SimpleTestCase):
    def test_link_code_is_limited_to_the_length_of_a_code(self):
        form = ReclassifyForm({"scope": "in_fleet", "link": "x" * 33, "reason": "It was in the fleet"})

        self.assertFalse(form.is_valid())
        self.assertIn("link", form.errors)

    def test_reason_is_required_in_both_directions(self):
        self.assertFalse(ReclassifyForm({"scope": "out_of_fleet"}).is_valid())
