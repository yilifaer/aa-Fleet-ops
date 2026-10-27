# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Forms of the fleet records: special roles, approvals and roster imports."""

from django import forms
from django.utils.translation import gettext_lazy as _
from django.utils.translation import pgettext_lazy

from fleetops import texts
from fleetops.forms import _style
from fleetops.models import AttendanceRecord, SpecialRole
from fleetops.texts import translate_stored


def roster_import_note():
    """The note a roster import offers, in the language of the person who imports."""
    return translate_stored(texts.ROSTER_IMPORT_NOTE())


class SpecialRoleForm(forms.ModelForm):
    class Meta:
        model = SpecialRole
        fields = ["name", "grants_fc_credit", "is_active", "sort_order"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        _style(self.fields)

    def clean_name(self):
        name = (self.cleaned_data.get("name") or "").strip()
        if not name:
            raise forms.ValidationError(forms.Field.default_error_messages["required"], code="required")
        return name


class ReviewForm(forms.Form):
    APPROVE = "approve"
    REJECT = "reject"

    action = forms.ChoiceField(label=pgettext_lazy("FleetOps", "Action"), choices=[(APPROVE, _("Approve")), (REJECT, _("Reject"))])
    note = forms.CharField(label=_("Note"), max_length=255, required=False)
    include_attendance = forms.BooleanField(
        label=_("With its PAP rows waiting for approval"), required=False, initial=True
    )

    def clean(self):
        data = super().clean()
        if data.get("action") == self.REJECT and not (data.get("note") or "").strip():
            self.add_error("note", _("Give a reason for the rejection."))
        return data


class ResubmitForm(forms.Form):
    """Sending a rejected fleet back for approval: an optional note, and whether its rejected PAP rows go along."""

    note = forms.CharField(label=_("Note"), max_length=255, required=False)
    include_attendance = forms.BooleanField(
        label=_("Also send its PAP rows rejected with it back for approval"), required=False, initial=True
    )


class RosterPasteForm(forms.Form):
    text = forms.CharField(
        label=_("Roster"),
        max_length=200_000,
        widget=forms.Textarea(attrs={"rows": 12}),
        help_text=_(
            "Copy the fleet window (Chinese or English client), a chat channel member list or chat lines and "
            "paste them here."
        ),
    )
    attendance_value = forms.IntegerField(label=_("PAP value"), min_value=1, max_value=100, initial=1)
    duplicate_action = forms.ChoiceField(
        label=_("If a character has tracked PAP"),
        choices=AttendanceRecord.Correction.choices,
        initial=AttendanceRecord.Correction.KEEP,
    )
    notes = forms.CharField(
        label=_("Notes"),
        required=False, max_length=1000, initial=roster_import_note, widget=forms.Textarea(attrs={"rows": 2})
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        _style(self.fields)

    def clean_notes(self):
        notes = self.cleaned_data["notes"]
        # The offered note is a message of the system, whatever language it was shown in; a note that was
        # changed is the importer's own text.
        offered = texts.ROSTER_IMPORT_NOTE()
        return offered if notes in (offered, roster_import_note()) else notes
