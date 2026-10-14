"""Forms of the SRP pages, and the ISK input that the configuration forms share."""

import re
import uuid
from decimal import Decimal

from django import forms

from fleetops.constants import MAX_ISK
from fleetops.forms import _style
from fleetops.srp.killmails import parse_killmail_reference
from fleetops.srp.types import SrpError

# What members may paste: an in-game link, a killboard link or an ID with its hash.
KILLMAIL_INPUT_LENGTH = 2000
COMMENT_LENGTH = 1000
REASON_LENGTH = 1000
NOTE_LENGTH = 255
# Claims ticked in one list.
MAX_SELECTED_CLAIMS = 200

ISK_EXAMPLES = "250m, 1.2b or 250,000,000"
ISK_INVALID = f"Enter an amount in ISK, for example {ISK_EXAMPLES}."
ISK_DECIMAL_HINT = "Write decimals with a point and thousands with commas, for example 1.5m or 1,500,000."
_ISK_TEXT_LENGTH = 40
_SUFFIXES = {"": 1, "k": 10**3, "m": 10**6, "b": 10**9}
_SPACES = str.maketrans({" ": " ", " ": " ", " ": " "})
_NUMBER_AND_SUFFIX = re.compile(r"(?P<number>.+?)\s*(?P<suffix>[kmb]?)", re.ASCII)
# ASCII digits only: other scripts' digits would be accepted by int() and Decimal().
_PLAIN = re.compile(r"\d+(?:\.\d+)?", re.ASCII)
_GROUPED_BY_COMMA = re.compile(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?", re.ASCII)
_GROUPED_BY_SPACE = re.compile(r"\d{1,3}(?: \d{3})+(?:\.\d+)?", re.ASCII)


def parse_isk(text) -> int:
    """Whole ISK from what a person typed: ``250m``, ``1.2b``, ``750k``, ``250,000,000``, ``1 000 000``.

    ``k``, ``m`` and ``b`` mean thousand, million and billion; a trailing ``ISK`` is ignored. Commas and
    spaces group thousands and a point is the decimal separator. A comma that does not group three digits
    is refused rather than read as a decimal comma, because ``1,5m`` as 15 million would be a costly typo.
    Raises ``ValueError`` with a message for the person.
    """
    text = str(text).translate(_SPACES).strip().casefold()
    if len(text) > _ISK_TEXT_LENGTH:
        raise ValueError(ISK_INVALID)
    if text.startswith("-"):
        raise ValueError("Enter a positive amount of ISK.")
    text = re.sub(r"\s*isk$", "", text)
    match = _NUMBER_AND_SUFFIX.fullmatch(text)
    if match is None:
        raise ValueError(ISK_INVALID)
    number, suffix = match.group("number"), match.group("suffix")
    if not any(pattern.fullmatch(number) for pattern in (_PLAIN, _GROUPED_BY_COMMA, _GROUPED_BY_SPACE)):
        raise ValueError(ISK_DECIMAL_HINT if "," in number else ISK_INVALID)
    value = Decimal(number.replace(",", "").replace(" ", "")) * _SUFFIXES[suffix]
    if value != value.to_integral_value():
        raise ValueError("ISK amounts must be whole numbers.")
    return int(value)


class IskField(forms.Field):
    """An amount of ISK typed the way players write it; the cleaned value is an ``int`` (None when blank).

    ``min_value`` and ``max_value`` bound the amount (1 to 10**15 by default). Initial values that are
    integers are shown with thousands separators.
    """

    widget = forms.TextInput
    default_error_messages = {
        "invalid": ISK_INVALID,
        "min_value": "The amount must be at least %(limit)s ISK.",
        "max_value": "The amount must be at most %(limit)s ISK.",
    }

    def __init__(self, *, min_value=1, max_value=MAX_ISK, **kwargs):
        self.min_value = min_value
        self.max_value = max_value
        super().__init__(**kwargs)
        self.widget.attrs.setdefault("placeholder", "e.g. 250m")
        self.widget.attrs.setdefault("autocomplete", "off")
        self.widget.attrs.setdefault("maxlength", str(_ISK_TEXT_LENGTH))

    def to_python(self, value):
        if isinstance(value, str):
            value = value.translate(_SPACES).strip()
        if value in self.empty_values:
            return None
        if isinstance(value, bool):
            raise forms.ValidationError(self.error_messages["invalid"], code="invalid")
        if isinstance(value, int):
            return value
        try:
            return parse_isk(value)
        except ValueError as exc:
            raise forms.ValidationError(str(exc), code="invalid") from None

    def validate(self, value):
        super().validate(value)
        if value is None:
            return
        if self.min_value is not None and value < self.min_value:
            raise forms.ValidationError(
                self.error_messages["min_value"], code="min_value", params={"limit": f"{self.min_value:,}"}
            )
        if self.max_value is not None and value > self.max_value:
            raise forms.ValidationError(
                self.error_messages["max_value"], code="max_value", params={"limit": f"{self.max_value:,}"}
            )

    def prepare_value(self, value):
        if isinstance(value, int) and not isinstance(value, bool):
            return f"{value:,}"
        return value

    def has_changed(self, initial, data):
        try:
            return self.to_python(data) != self.to_python(initial)
        except forms.ValidationError:
            return True


class ClaimIdsField(forms.MultipleChoiceField):
    """The claims ticked in a list, as UUIDs; the view looks the claims up and skips the ones it cannot use."""

    def valid_value(self, value):
        try:
            uuid.UUID(str(value))
        except ValueError:
            return False
        return True

    def clean(self, value):
        values = super().clean(value)
        if len(values) > MAX_SELECTED_CLAIMS:
            raise forms.ValidationError(
                f"Select at most {MAX_SELECTED_CLAIMS} claims at a time.", code="too_many"
            )
        # A repeated box counts once; the order of the list is kept.
        return list(dict.fromkeys(uuid.UUID(str(item)) for item in values))


class ClaimSelectionForm(forms.Form):
    """Claims ticked in the review queue or the payments list (``claim`` boxes).

    Each row also posts the amount it showed (``shown-<claim uuid>``): the suggested amount in the queue, the
    approved amount in the payments list. The services refuse a claim whose amount is another one by then.
    """

    claim = ClaimIdsField(required=True, error_messages={"required": "Select at least one claim."})

    def shown_amounts(self) -> dict:
        """``{uuid: amount}`` of the ticked claims that posted a readable amount; others are left out."""
        shown = {}
        for claim_id in self.cleaned_data.get("claim", ()):
            text = self.data.get(f"shown-{claim_id}")
            if isinstance(text, str) and text.isascii() and text.isdigit() and len(text) <= 19:
                shown[claim_id] = int(text)
        return shown


class ClaimInputForm(forms.Form):
    """What a member pastes to start a claim."""

    killmail = forms.CharField(
        label="Killmail",
        max_length=KILLMAIL_INPUT_LENGTH,
        strip=True,
        widget=forms.Textarea(
            attrs={
                "rows": 2,
                "placeholder": "In-game killmail link, zKillboard link, or killmail ID and hash",
                "autocomplete": "off",
            }
        ),
        help_text="Paste the in-game killmail link (works immediately) or a zKillboard link.",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        _style(self.fields)

    def clean_killmail(self):
        """The text must hold a killmail the service can look up; the person is told what to paste otherwise."""
        text = self.cleaned_data["killmail"]
        try:
            self.reference = parse_killmail_reference(text)
        except SrpError as exc:
            raise forms.ValidationError(exc.message, code=exc.code) from None
        return text


class SubmitClaimForm(forms.Form):
    """The preview's submit button: the signed token of the previewed killmail and an optional comment."""

    token = forms.CharField(max_length=1000, widget=forms.HiddenInput())
    comment = forms.CharField(
        label="Comment",
        required=False,
        max_length=COMMENT_LENGTH,
        strip=True,
        widget=forms.Textarea(attrs={"rows": 2}),
        help_text="Optional. Anything the reviewer should know.",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        _style(self.fields)


class WithdrawForm(forms.Form):
    """Withdraw a pending claim; the reason is optional."""

    reason = forms.CharField(
        label="Reason", required=False, max_length=REASON_LENGTH, strip=True, widget=forms.Textarea(attrs={"rows": 2})
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        _style(self.fields)


class ReasonForm(forms.Form):
    """A decision that needs its reason: reject, revoke an approval, undo a payment, reopen."""

    reason = forms.CharField(
        label="Reason",
        max_length=REASON_LENGTH,
        strip=True,
        widget=forms.Textarea(attrs={"rows": 2}),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        _style(self.fields)


class ApproveForm(forms.Form):
    """The approve buttons: the suggested amount, the amount within the limit, or a typed amount.

    The form also carries what the page showed (``shown_status``, ``shown_suggested``, ``shown_limited``), so
    the decision is made on the claim the reviewer looked at and not on one that changed in the meantime. The
    page writes these fields from the claim itself; they are optional, and without them nothing is compared.
    """

    SUGGESTED = "suggested"
    WITHIN_LIMIT = "within_limit"
    AMOUNT = "amount"
    SHOWN_STATUSES = [("pending", "Pending"), ("approved", "Approved")]

    mode = forms.ChoiceField(
        choices=[
            (SUGGESTED, "Approve suggested"),
            (WITHIN_LIMIT, "Approve within limit"),
            (AMOUNT, "Approve amount"),
        ],
        widget=forms.HiddenInput(),
    )
    amount = IskField(required=False, label="Amount")
    reason = forms.CharField(
        label="Reason",
        required=False,
        max_length=REASON_LENGTH,
        strip=True,
        widget=forms.Textarea(attrs={"rows": 2}),
        help_text=(
            "Required when the amount differs from the suggested amount, when you change an approved amount "
            "and when you approve over a monthly limit."
        ),
    )
    confirm_above_policy = forms.BooleanField(label="Pay more than the policy amount", required=False)
    confirm_over_limit = forms.BooleanField(label="Approve over the monthly limit", required=False)
    shown_status = forms.ChoiceField(required=False, choices=SHOWN_STATUSES, widget=forms.HiddenInput())
    shown_suggested = forms.IntegerField(required=False, min_value=0, max_value=MAX_ISK, widget=forms.HiddenInput())
    shown_limited = forms.IntegerField(required=False, min_value=0, max_value=MAX_ISK, widget=forms.HiddenInput())

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        _style(self.fields)

    def clean(self):
        data = super().clean()
        if data.get("mode") == self.AMOUNT and data.get("amount") is None and "amount" not in self.errors:
            self.add_error("amount", "Enter the amount to approve.")
        return data

    def amount_for(self, claim) -> int:
        """The amount the pressed button stands for: the one the page showed on it, else the claim's own."""
        mode = self.cleaned_data["mode"]
        if mode == self.SUGGESTED:
            shown = self.cleaned_data.get("shown_suggested")
            return claim.suggested_amount if shown is None else shown
        if mode == self.WITHIN_LIMIT:
            shown = self.cleaned_data.get("shown_limited")
            return claim.limited_amount if shown is None else shown
        return self.cleaned_data["amount"]

    def expected(self) -> dict:
        """What the service compares with the claim under its lock: the status the page showed and, for a
        button that stands for a stored amount, that amount."""
        mode = self.cleaned_data["mode"]
        return {
            "expected_status": self.cleaned_data.get("shown_status") or None,
            "expected_suggested": self.cleaned_data.get("shown_suggested") if mode == self.SUGGESTED else None,
            "expected_limited": self.cleaned_data.get("shown_limited") if mode == self.WITHIN_LIMIT else None,
        }


class PaidForm(forms.Form):
    """Mark claims paid after the ISK was sent in game.

    ``shown_amount`` is the approved amount the page asked the payer to send; the service refuses the payment
    when the approved amount is another one by then.
    """

    note = forms.CharField(label="Payment note", required=False, max_length=NOTE_LENGTH, strip=True)
    shown_amount = forms.IntegerField(required=False, min_value=0, max_value=MAX_ISK, widget=forms.HiddenInput())

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        _style(self.fields)


class ReclassifyForm(forms.Form):
    """Move a pending claim between fleet and out-of-fleet."""

    IN_FLEET = "in_fleet"
    OUT_OF_FLEET = "out_of_fleet"

    scope = forms.ChoiceField(
        choices=[(IN_FLEET, "In fleet"), (OUT_OF_FLEET, "Out of fleet")], label="Loss happened"
    )
    link = forms.CharField(
        label="Fleet SRP link code", required=False, max_length=32, strip=True,
        help_text="The code of the fleet's SRP link; needed for an in-fleet loss.",
    )
    reason = forms.CharField(
        label="Reason", max_length=REASON_LENGTH, strip=True, widget=forms.Textarea(attrs={"rows": 2})
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        _style(self.fields)

    def clean(self):
        data = super().clean()
        if data.get("scope") == self.IN_FLEET and not data.get("link"):
            self.add_error("link", "Choose the fleet's SRP link for an in-fleet loss.")
        return data


class NoteForm(forms.Form):
    """An internal note on a claim; the claimant never sees it."""

    text = forms.CharField(
        label="Internal note", max_length=REASON_LENGTH, strip=True, widget=forms.Textarea(attrs={"rows": 2})
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        _style(self.fields)


class ReviewFilterForm(forms.Form):
    """The filters of the review queue. Invalid values are ignored by the view, like other list filters.

    ``status`` defaults to the claims waiting for a decision; ``all`` lists every status. The corporations and
    FCs to choose from are the ones that have claims, given by the view as ``(id, name)`` pairs.
    """

    ANY_STATUS = "all"
    STATUSES = [("pending", "Pending"), ("approved", "Approved"), ("paid", "Paid"), ("rejected", "Rejected"),
                ("withdrawn", "Withdrawn")]
    SCOPES = [("in_fleet", "In fleet"), ("out_of_fleet", "Out of fleet")]
    FLAGS = [("over_limit", "Over limit"), ("fleet_check", "Fleet check"), ("estimate", "Estimated value"),
             ("external", "Also claimed elsewhere")]

    status = forms.ChoiceField(required=False, choices=[(ANY_STATUS, "Any status"), *STATUSES])
    scope = forms.ChoiceField(required=False, choices=[("", "Any scope"), *SCOPES])
    flag = forms.ChoiceField(required=False, choices=[("", "Any flag"), *FLAGS])
    corporation = forms.TypedChoiceField(required=False, coerce=int, empty_value=None, choices=[("", "Any corporation")])
    fc = forms.TypedChoiceField(required=False, coerce=int, empty_value=None, choices=[("", "Any FC")])
    q = forms.CharField(
        required=False,
        max_length=100,
        strip=True,
        label="Search",
        widget=forms.TextInput(attrs={"placeholder": "Pilot, ship or SRP-123"}),
    )

    def __init__(self, *args, corporations=(), fcs=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["corporation"].choices = [("", "Any corporation"), *((str(pk), name) for pk, name in corporations)]
        self.fields["fc"].choices = [("", "Any FC"), *((str(pk), name) for pk, name in fcs)]
        _style(self.fields)


class PaymentFilterForm(forms.Form):
    """The payment dates of the payments list (EVE dates); invalid dates are ignored by the view."""

    paid_from = forms.DateField(required=False, label="Paid from", widget=forms.DateInput(attrs={"type": "date"}))
    paid_to = forms.DateField(required=False, label="Paid to", widget=forms.DateInput(attrs={"type": "date"}))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        _style(self.fields)
