# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Forms of the SRP pages, and the ISK input that the configuration forms share."""

import re
import uuid
from decimal import Decimal

from django import forms
from django.core.validators import URLValidator
from django.utils.text import format_lazy
from django.utils.translation import gettext_lazy as _
from django.utils.translation import pgettext_lazy

from fleetops.constants import MAX_ISK
from fleetops.forms import _style
from fleetops.forms_operation import EveDateTimeField
from fleetops.models import MAX_DATABASE_ID, SrpBan, SrpCategory, SrpFleetLink, SrpSettings
from fleetops.srp.categories import default_category, selectable_categories
from fleetops.srp.display import category_label
from fleetops.srp.killmails import parse_killmail_reference
from fleetops.srp.types import SrpError
from fleetops.texts import translate_stored

# What members may paste: an in-game link, a killboard link or an ID with its hash.
KILLMAIL_INPUT_LENGTH = 2000
COMMENT_LENGTH = 1000
REASON_LENGTH = 1000
NOTE_LENGTH = 255
# Claims ticked in one list.
MAX_SELECTED_CLAIMS = 200
# The length of an SRP link code.
LINK_CODE_LENGTH = 32

ISK_EXAMPLES = _("250m, 1.2b or 250,000,000")
ISK_INVALID = format_lazy(_("Enter an amount in ISK, for example {examples}."), examples=ISK_EXAMPLES)
ISK_DECIMAL_HINT = _("Write decimals with a point and thousands with commas, for example 1.5m or 1,500,000.")
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
        raise ValueError(_("Enter a positive amount of ISK."))
    text = re.sub(r"\s*isk$", "", text)
    match = _NUMBER_AND_SUFFIX.fullmatch(text)
    if match is None:
        raise ValueError(ISK_INVALID)
    number, suffix = match.group("number"), match.group("suffix")
    if not any(pattern.fullmatch(number) for pattern in (_PLAIN, _GROUPED_BY_COMMA, _GROUPED_BY_SPACE)):
        raise ValueError(ISK_DECIMAL_HINT if "," in number else ISK_INVALID)
    value = Decimal(number.replace(",", "").replace(" ", "")) * _SUFFIXES[suffix]
    if value != value.to_integral_value():
        raise ValueError(_("ISK amounts must be whole numbers."))
    return int(value)


class IskField(forms.Field):
    """An amount of ISK typed the way players write it; the cleaned value is an ``int`` (None when blank).

    ``min_value`` and ``max_value`` bound the amount (1 to 10**15 by default). Initial values that are
    integers are shown with thousands separators.
    """

    widget = forms.TextInput
    default_error_messages = {
        "invalid": ISK_INVALID,
        "min_value": _("The amount must be at least %(limit)s ISK."),
        "max_value": _("The amount must be at most %(limit)s ISK."),
    }

    def __init__(self, *, min_value=1, max_value=MAX_ISK, **kwargs):
        self.min_value = min_value
        self.max_value = max_value
        super().__init__(**kwargs)
        self.widget.attrs.setdefault("placeholder", _("e.g. 250m"))
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


def _shown_revision():
    """The hidden field that carries the ``member_revision`` the page showed: the decision is refused when the claimant
    changed the claim since. Optional; without it nothing is compared."""
    return forms.IntegerField(required=False, min_value=0, max_value=2**31 - 1, widget=forms.HiddenInput())


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
                _("Select at most %(max)s claims at a time.") % {"max": MAX_SELECTED_CLAIMS}, code="too_many"
            )
        # A repeated box counts once; the order of the list is kept.
        return list(dict.fromkeys(uuid.UUID(str(item)) for item in values))


class ClaimSelectionForm(forms.Form):
    """Claims ticked in the review queue or the payments list (``claim`` boxes).

    Each row also posts the amount it showed (``shown-<claim uuid>``): the suggested amount in the queue, the
    approved amount in the payments list. The services refuse a claim whose amount is another one by then.
    """

    claim = ClaimIdsField(
        label=_("Claim"),
        required=True,
        error_messages={"required": _("Select at least one claim.")},
    )

    def shown_amounts(self) -> dict:
        """``{uuid: amount}`` of the ticked claims that posted a readable amount; others are left out."""
        return self._shown("shown-", 19)

    def shown_revisions(self) -> dict:
        """``{uuid: member_revision}`` of the ticked claims (``rev-<uuid>``): the number of changes by the claimant the
        list showed. Others are left out."""
        return self._shown("rev-", 9)

    def _shown(self, prefix, digits) -> dict:
        shown = {}
        for claim_id in self.cleaned_data.get("claim", ()):
            text = self.data.get(f"{prefix}{claim_id}")
            if isinstance(text, str) and text.isascii() and text.isdigit() and len(text) <= digits:
                shown[claim_id] = int(text)
        return shown


class ClaimInputForm(forms.Form):
    """What a member pastes to start a claim."""

    killmail = forms.CharField(
        label=_("Killmail"),
        max_length=KILLMAIL_INPUT_LENGTH,
        strip=True,
        widget=forms.Textarea(
            attrs={
                "rows": 2,
                "placeholder": _("In-game killmail link, zKillboard link, or killmail ID and hash"),
                "autocomplete": "off",
            }
        ),
        help_text=_("Paste the in-game killmail link (works immediately) or a zKillboard link."),
    )

    def __init__(self, *args, with_category=False, settings_obj=None, **kwargs):
        """``with_category`` adds the category choice of a loss outside a fleet: only the active categories members
        can choose, preselected with the default category of the settings, required while the settings say so.
        Without such a category the field is not there and the form is the one of 0.2.0b3."""
        super().__init__(*args, **kwargs)
        if with_category:
            self._add_category(settings_obj)
        _style(self.fields)

    def _add_category(self, settings_obj):
        srp = settings_obj or SrpSettings.get_solo()
        choices = selectable_categories().order_by("sort_order", "pk")
        if not choices.exists():
            return
        default = default_category(srp)
        required = srp.out_of_fleet_category_required
        self.fields["category"] = CategoryChoiceField(
            queryset=choices,
            required=required,
            label=_("Category"),
            # With a default category there is no empty choice: the default would apply to it anyway.
            empty_label=None if default is not None else (_("Choose a category") if required else _("No category (general rules)")),
            initial=default.pk if default is not None else None,
            help_text=_("Pick the kind of loss. The category decides the payout table and the limits.")
            if required
            else _("Optional. The category decides the payout table and the limits."),
        )

    def clean_killmail(self):
        """The text must hold a killmail the service can look up; the person is told what to paste otherwise."""
        text = self.cleaned_data["killmail"]
        try:
            self.reference = parse_killmail_reference(text)
        except SrpError as exc:
            raise forms.ValidationError(translate_stored(exc.message), code=exc.code) from None
        return text


class SubmitClaimForm(forms.Form):
    """The preview's submit button: the signed token of the previewed killmail and an optional comment."""

    token = forms.CharField(max_length=1000, widget=forms.HiddenInput())
    comment = forms.CharField(
        label=pgettext_lazy("FleetOps", "Comment"),
        required=False,
        max_length=COMMENT_LENGTH,
        strip=True,
        widget=forms.Textarea(attrs={"rows": 2}),
        help_text=_("Optional. Anything the reviewer should know."),
    )
    # Only the "submit as a loss outside a fleet anyway" button of a loss that lies in a tracked fleet asks for it.
    out_of_fleet_reason = forms.CharField(
        label=_("Why outside the fleet?"),
        required=False,
        max_length=COMMENT_LENGTH,
        strip=True,
        widget=forms.Textarea(attrs={"rows": 2}),
        help_text=_("The reviewer sees this reason."),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        _style(self.fields)


class WithdrawForm(forms.Form):
    """Withdraw a pending claim; the reason is optional."""

    reason = forms.CharField(
        label=_("Reason"),
        required=False,
        max_length=REASON_LENGTH,
        strip=True,
        widget=forms.Textarea(attrs={"rows": 2}),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        _style(self.fields)


class ReasonForm(forms.Form):
    """A decision that needs its reason: reject, revoke an approval, undo a payment, reopen.

    ``what`` names the reason (a key of ``claims.REASON_MESSAGES``, such as "reason for the rejection"), so an
    empty or too long reason is answered with the sentence of that decision and not with Django's "This field
    is required."
    """

    reason = forms.CharField(
        label=_("Reason"),
        max_length=REASON_LENGTH,
        strip=True,
        widget=forms.Textarea(attrs={"rows": 2}),
    )
    shown_revision = _shown_revision()

    def __init__(self, *args, what="reason", **kwargs):
        super().__init__(*args, **kwargs)
        _style(self.fields)
        from fleetops.srp.claims import MAX_REASON_LENGTH, REASON_MESSAGES

        needed, too_long = REASON_MESSAGES[what]
        self.fields["reason"].error_messages["required"] = translate_stored(needed())
        self.fields["reason"].error_messages["max_length"] = translate_stored(too_long(max=MAX_REASON_LENGTH))


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
            (SUGGESTED, _("Approve suggested")),
            (WITHIN_LIMIT, _("Approve within limit")),
            (AMOUNT, _("Approve amount")),
        ],
        widget=forms.HiddenInput(),
    )
    amount = IskField(required=False, label=_("Amount"))
    reason = forms.CharField(
        label=_("Reason"),
        required=False,
        max_length=REASON_LENGTH,
        strip=True,
        widget=forms.Textarea(attrs={"rows": 2}),
        help_text=(
            _(
                "Required when the amount differs from the suggested amount, when you change an approved amount "
                "and when you approve over a monthly limit."
            )
        ),
    )
    confirm_above_policy = forms.BooleanField(label=_("Pay more than the policy amount"), required=False)
    confirm_over_limit = forms.BooleanField(label=_("Approve over the monthly limit"), required=False)
    shown_status = forms.ChoiceField(required=False, choices=SHOWN_STATUSES, widget=forms.HiddenInput())
    shown_suggested = forms.IntegerField(required=False, min_value=0, max_value=MAX_ISK, widget=forms.HiddenInput())
    shown_limited = forms.IntegerField(required=False, min_value=0, max_value=MAX_ISK, widget=forms.HiddenInput())
    shown_revision = _shown_revision()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        _style(self.fields)

    def clean(self):
        data = super().clean()
        if data.get("mode") == self.AMOUNT and data.get("amount") is None and "amount" not in self.errors:
            self.add_error("amount", _("Enter the amount to approve."))
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
        expected = {
            "expected_status": self.cleaned_data.get("shown_status") or None,
            "expected_suggested": self.cleaned_data.get("shown_suggested") if mode == self.SUGGESTED else None,
            "expected_limited": self.cleaned_data.get("shown_limited") if mode == self.WITHIN_LIMIT else None,
        }
        if self.cleaned_data.get("shown_revision") is not None:
            # Also what the claimant had changed when the page was made.
            expected["expected_revision"] = self.cleaned_data["shown_revision"]
        return expected


class PaidForm(forms.Form):
    """Mark claims paid after the ISK was sent in game.

    ``shown_amount`` is the approved amount the page asked the payer to send; the service refuses the payment
    when the approved amount is another one by then.
    """

    note = forms.CharField(label=_("Payment note"), required=False, max_length=NOTE_LENGTH, strip=True)
    shown_amount = forms.IntegerField(required=False, min_value=0, max_value=MAX_ISK, widget=forms.HiddenInput())

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        _style(self.fields)


class CategoryChoiceField(forms.ModelChoiceField):
    """A choice of the active SRP categories; the built-in category is named in the language of the viewer."""

    def __init__(self, **kwargs):
        kwargs.setdefault("queryset", SrpCategory.objects.filter(is_active=True))
        super().__init__(**kwargs)

    def label_from_instance(self, obj):
        return category_label(obj)


class ReclassifyForm(forms.Form):
    """Move a pending claim between fleet and out-of-fleet."""

    IN_FLEET = "in_fleet"
    OUT_OF_FLEET = "out_of_fleet"

    scope = forms.ChoiceField(
        choices=[(IN_FLEET, _("In fleet")), (OUT_OF_FLEET, _("Out of fleet"))], label=_("Loss happened")
    )
    link = forms.CharField(
        label=_("Fleet SRP link code"), required=False, max_length=32, strip=True,
        help_text=_("The code of the fleet's SRP link; needed for an in-fleet loss."),
    )
    category = CategoryChoiceField(
        label=_("Category"),
        required=False,
        empty_label=_("Default category"),
        help_text=_("For a loss moved out of the fleet. A loss moved into a fleet gets the category of the fleet."),
    )
    reason = forms.CharField(
        label=_("Reason"), max_length=REASON_LENGTH, strip=True, widget=forms.Textarea(attrs={"rows": 2})
    )
    shown_revision = _shown_revision()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        _style(self.fields)

    def clean(self):
        data = super().clean()
        if data.get("scope") == self.IN_FLEET and not data.get("link"):
            self.add_error("link", _("Choose the fleet's SRP link for an in-fleet loss."))
        return data


class RecategorizeForm(forms.Form):
    """Change the category of a pending claim; the claim is calculated again with the category's rules."""

    category = CategoryChoiceField(
        label=_("Category"),
        required=False,
        empty_label=_("No category (general rules)"),
    )
    reason = forms.CharField(
        label=_("Reason"), max_length=REASON_LENGTH, strip=True, widget=forms.Textarea(attrs={"rows": 2})
    )
    shown_revision = _shown_revision()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        _style(self.fields)


class NoteForm(forms.Form):
    """An internal note on a claim; the claimant never sees it."""

    text = forms.CharField(
        label=_("Internal note"), max_length=REASON_LENGTH, strip=True, widget=forms.Textarea(attrs={"rows": 2})
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
    STATUSES = [
        ("pending", pgettext_lazy("FleetOps", "Pending")),
        ("approved", pgettext_lazy("FleetOps", "Approved")),
        ("paid", _("Paid")),
        ("rejected", pgettext_lazy("FleetOps", "Rejected")),
        ("withdrawn", _("Withdrawn")),
    ]
    SCOPES = [("in_fleet", _("In fleet")), ("out_of_fleet", _("Out of fleet"))]
    FLAGS = [
        ("over_limit", _("Over limit")),
        ("fleet_check", _("Fleet check")),
        ("estimate", _("Estimated value")),
        ("external", _("Also claimed elsewhere")),
    ]

    status = forms.ChoiceField(label=_("Status"), required=False, choices=[(ANY_STATUS, _("Any status")), *STATUSES])
    scope = forms.ChoiceField(label=_("Scope"), required=False, choices=[("", _("Any scope")), *SCOPES])
    flag = forms.ChoiceField(label=_("Flag"), required=False, choices=[("", _("Any flag")), *FLAGS])
    corporation = forms.TypedChoiceField(
        label=pgettext_lazy("FleetOps", "Corporation"),
        required=False,
        coerce=int,
        empty_value=None,
        choices=[("", _("Any corporation"))],
    )
    fc = forms.TypedChoiceField(
        label=_("FC"),
        required=False,
        coerce=int,
        empty_value=None,
        choices=[("", _("Any FC"))],
    )
    q = forms.CharField(
        required=False,
        max_length=100,
        strip=True,
        label=_("Search"),
        widget=forms.TextInput(attrs={"placeholder": _("Pilot, ship or SRP-123")}),
    )
    category = forms.ChoiceField(label=_("Category"), required=False, choices=[("", _("Any category"))])
    link = forms.CharField(
        label=_("Link"),
        required=False,
        max_length=LINK_CODE_LENGTH,
        strip=True,
        widget=forms.TextInput(attrs={"placeholder": _("Link code"), "list": "srp-link-codes", "autocomplete": "off"}),
    )
    submitted_from = forms.DateField(
        required=False, label=_("Submitted from"), widget=forms.DateInput(attrs={"type": "date"})
    )
    submitted_to = forms.DateField(
        required=False, label=_("Submitted to"), widget=forms.DateInput(attrs={"type": "date"})
    )
    # Not shown as a field: the claim page links to the claims of one member.
    user = forms.IntegerField(required=False, min_value=1, max_value=MAX_DATABASE_ID, widget=forms.HiddenInput())

    def __init__(self, *args, corporations=(), fcs=(), categories=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["corporation"].choices = [
            ("", _("Any corporation")),
            *((str(pk), name) for pk, name in corporations),
        ]
        self.fields["fc"].choices = [("", _("Any FC")), *((str(pk), name) for pk, name in fcs)]
        self.fields["category"].choices = category_choices(categories)
        _style(self.fields)


NO_CATEGORY = "none"


def category_choices(categories=()):
    """The choices of a category filter: any, the categories given as ``(id, label)`` and the claims without one."""
    return [
        ("", _("Any category")),
        *((str(pk), label) for pk, label in categories),
        (NO_CATEGORY, _("No category")),
    ]


class PayableFilterForm(forms.Form):
    """The filters of the claims waiting for payment (and of their export): category, link and corporation. Invalid
    values are ignored by the view."""

    category = forms.ChoiceField(label=_("Category"), required=False, choices=[("", _("Any category"))])
    link = forms.CharField(
        label=_("Link"),
        required=False,
        max_length=LINK_CODE_LENGTH,
        strip=True,
        widget=forms.TextInput(attrs={"placeholder": _("Link code"), "list": "srp-link-codes", "autocomplete": "off"}),
    )
    corporation = forms.TypedChoiceField(
        label=pgettext_lazy("FleetOps", "Corporation"),
        required=False,
        coerce=int,
        empty_value=None,
        choices=[("", _("Any corporation"))],
    )

    def __init__(self, *args, corporations=(), categories=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["category"].choices = category_choices(categories)
        self.fields["corporation"].choices = [
            ("", _("Any corporation")),
            *((str(pk), name) for pk, name in corporations),
        ]
        _style(self.fields)


class PaymentFilterForm(forms.Form):
    """The payment dates of the payments list (EVE dates); invalid dates are ignored by the view."""

    paid_from = forms.DateField(required=False, label=_("Paid from"), widget=forms.DateInput(attrs={"type": "date"}))
    paid_to = forms.DateField(required=False, label=_("Paid to"), widget=forms.DateInput(attrs={"type": "date"}))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        _style(self.fields)


class LinkFilterForm(forms.Form):
    """The filters of the link list; a value that does not check out is ignored by the view."""

    STATE_CHOICES = [
        ("", _("Any state")),
        (SrpFleetLink.State.OPEN.value, SrpFleetLink.State.OPEN.label),
        (SrpFleetLink.State.CLOSED.value, SrpFleetLink.State.CLOSED.label),
        (SrpFleetLink.State.COMPLETED.value, SrpFleetLink.State.COMPLETED.label),
    ]
    KIND_CHOICES = [
        ("", _("Any kind")),
        (SrpFleetLink.Kind.FLEET.value, SrpFleetLink.Kind.FLEET.label),
        (SrpFleetLink.Kind.STANDING.value, SrpFleetLink.Kind.STANDING.label),
    ]

    state = forms.ChoiceField(label=_("State"), required=False, choices=STATE_CHOICES)
    kind = forms.ChoiceField(label=_("Kind"), required=False, choices=KIND_CHOICES)
    category = CategoryChoiceField(
        label=_("Category"), required=False, empty_label=_("Any category"), queryset=SrpCategory.objects.all()
    )
    date_from = forms.DateField(required=False, label=_("From"), widget=forms.DateInput(attrs={"type": "date"}))
    date_to = forms.DateField(required=False, label=_("To"), widget=forms.DateInput(attrs={"type": "date"}))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        _style(self.fields)


def _report_field(**kwargs):
    """The after-action report address: http and https only (a ``javascript:`` or ``data:`` address is refused)."""
    return forms.URLField(
        label=_("After-action report"),
        required=False,
        max_length=500,
        assume_scheme="https",
        validators=[URLValidator(schemes=["http", "https"])],
        help_text=_("A link to the after-action report. Only http and https addresses are accepted."),
        **kwargs,
    )


class FleetLinkForm(forms.Form):
    """What can be changed on the SRP link of a fleet: the doctrine text and the report."""

    doctrine_text = forms.CharField(
        label=pgettext_lazy("FleetOps", "Doctrine"), required=False, max_length=255, strip=True
    )
    aar_url = _report_field()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        _style(self.fields)


class StandingLinkForm(forms.Form):
    """A standing SRP link: a name (made from the category's template when empty), a category, a date and the rest."""

    name = forms.CharField(
        label=pgettext_lazy("FleetOps", "Name"),
        required=False,
        max_length=255,
        strip=True,
        help_text=_("Left empty, the name is made from the name template of the category."),
    )
    category = CategoryChoiceField(label=_("Category"), empty_label=_("Choose a category"))
    link_date = forms.DateField(
        label=_("Date (EVE)"),
        widget=forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"}),
        help_text=_("Losses of this day belong to the link."),
    )
    doctrine_text = forms.CharField(
        label=pgettext_lazy("FleetOps", "Doctrine"), required=False, max_length=255, strip=True
    )
    aar_url = _report_field()
    auto_close_at = EveDateTimeField(
        label=_("Close automatically at (EVE time)"),
        required=False,
        help_text=_("Empty = the link stays open until someone closes it."),
    )
    daily = forms.BooleanField(
        label=_("Make this the link of the day for the category"),
        required=False,
        help_text=_("There is one such link per category and day; the automatic daily link takes the same place."),
    )

    def __init__(self, *args, link=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.link = link
        category = self.fields["category"]
        if link is not None and link.category_id:
            # The category of an existing link stays selectable when it was switched off since.
            category.queryset = SrpCategory.objects.filter(pk=link.category_id) | SrpCategory.objects.filter(
                is_active=True
            )
            del self.fields["daily"]
        _style(self.fields)
        # Hooks of the page script that fills in the defaults of the chosen category.
        category.widget.attrs["data-srp-link-category"] = ""
        self.fields["doctrine_text"].widget.attrs["data-srp-link-doctrine"] = ""
        self.fields["auto_close_at"].widget.attrs["data-srp-link-close"] = ""
        self.fields["link_date"].widget.attrs["data-srp-link-date"] = ""

    def category_data(self):
        """``{category id: {"doctrine": text, "hours": hours or ""}}`` for the page script that fills in the defaults."""
        return {
            str(category.pk): {
                "doctrine": category.default_doctrine_text,
                "hours": category.auto_close_after_hours or "",
            }
            for category in self.fields["category"].queryset
        }


class BanFilterForm(forms.Form):
    """The filters of the ban list; a value that does not check out is ignored by the view."""

    STATE_CHOICES = [
        ("active", _("In force")),
        ("lifted", _("Lifted")),
        ("expired", _("Expired")),
        ("all", _("All")),
    ]
    state = forms.ChoiceField(label=_("State"), required=False, choices=STATE_CHOICES)
    kind = forms.ChoiceField(
        label=_("Kind"),
        required=False,
        choices=[("", _("Any kind")), *SrpBan.Kind.choices],
    )
    member = forms.CharField(label=_("Member"), required=False, max_length=100, strip=True)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        _style(self.fields)


class BanForm(forms.Form):
    """Ban a member from submitting SRP claims.

    The member is named by the main character (the exact name) or arrives as ``user`` from the claim page. The
    reason is shown to the member; the internal note is for SRP staff only.
    """

    user = forms.IntegerField(required=False, widget=forms.HiddenInput())
    member = forms.CharField(
        label=pgettext_lazy("FleetOps", "Main character"),
        required=False,
        max_length=100,
        strip=True,
        help_text=_("The exact name of the member's main character."),
    )
    days = forms.IntegerField(label=_("Length (days)"), required=False, min_value=1)
    permanent = forms.BooleanField(
        label=_("Permanent"),
        required=False,
        help_text=_("The ban has no end date until a reviewer lifts it."),
    )
    reason = forms.CharField(
        label=_("Reason"),
        max_length=REASON_LENGTH,
        strip=True,
        widget=forms.Textarea(attrs={"rows": 2}),
        help_text=_("The member sees this reason."),
    )
    internal_note = forms.CharField(
        label=_("Internal note"),
        required=False,
        max_length=REASON_LENGTH,
        strip=True,
        widget=forms.Textarea(attrs={"rows": 2}),
        help_text=_("Only SRP staff see this."),
    )
    reject_pending = forms.BooleanField(
        label=_("Reject the member's pending claims"),
        required=False,
        help_text=_("Approved claims are not touched."),
    )
    reject_reason = forms.CharField(
        label=_("Reason for the rejection"),
        required=False,
        max_length=REASON_LENGTH,
        strip=True,
        widget=forms.Textarea(attrs={"rows": 2}),
        help_text=_("Required when the pending claims are rejected. The member sees it."),
    )

    def __init__(self, *args, default_days=7, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["days"].initial = default_days
        _style(self.fields)

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("reject_pending") and not cleaned.get("reject_reason"):
            self.add_error("reject_reason", _("Give the reason for rejecting the pending claims."))
        if not cleaned.get("permanent") and not cleaned.get("days") and "days" not in self.errors:
            self.add_error("days", _("Enter the length in days, or tick Permanent."))
        if cleaned.get("permanent") and cleaned.get("days"):
            cleaned["days"] = None
        return cleaned


_TARGET = re.compile(r"(?:fleet|link|category):[A-Za-z0-9_-]{1,64}", re.ASCII)


class TargetField(forms.ChoiceField):
    """Where a loss belongs, as ``fleet:<code>``, ``link:<code>`` or ``category:<id>``; empty keeps it as it is. Any
    well-formed value is accepted here, the service decides whether the link or category can be used."""

    def valid_value(self, value):
        return bool(_TARGET.fullmatch(str(value)))


class ClaimEditForm(forms.Form):
    """What a member can change on a pending claim: the comment, where the loss belongs and the loss itself.

    ``target`` is left out when a reviewer already moved the claim; ``killmail`` and ``loss`` are left out when
    replacing the loss is switched off (``loss`` also while found losses are not read).
    """

    comment = forms.CharField(
        label=pgettext_lazy("FleetOps", "Comment"),
        required=False,
        max_length=COMMENT_LENGTH,
        strip=True,
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text=_("Anything the reviewer should know."),
    )
    target = TargetField(
        label=_("Category or link"),
        required=False,
        help_text=_("Move the claim to another fleet, a standing link or a category. The claim is calculated again."),
    )
    killmail = forms.CharField(
        label=_("Replace the loss with a killmail link"),
        required=False,
        max_length=KILLMAIL_INPUT_LENGTH,
        strip=True,
        widget=forms.Textarea(attrs={"rows": 2, "autocomplete": "off"}),
        help_text=_("Paste the in-game killmail link or a zKillboard link of your other loss."),
    )
    loss = forms.IntegerField(
        label=_("or choose one of your recent losses"),
        required=False,
        min_value=1,
        max_value=MAX_DATABASE_ID,
    )

    def __init__(self, *args, target_choices=None, loss_choices=None, allow_swap=True, **kwargs):
        """``target_choices`` are optgroups ``[(group, [(value, label)])]`` (None leaves the field out),
        ``loss_choices`` ``[(killmail id, label)]`` (None leaves the select out)."""
        super().__init__(*args, **kwargs)
        if target_choices is None:
            del self.fields["target"]
        else:
            self.fields["target"].choices = [("", _("Keep as it is")), *target_choices]
        if not allow_swap:
            del self.fields["killmail"]
            del self.fields["loss"]
        elif loss_choices is None:
            del self.fields["loss"]
        else:
            self.fields["loss"] = forms.TypedChoiceField(
                label=self.fields["loss"].label,
                required=False,
                coerce=int,
                empty_value=None,
                choices=[("", _("Keep this loss")), *((str(pk), label) for pk, label in loss_choices)],
            )
        _style(self.fields)
