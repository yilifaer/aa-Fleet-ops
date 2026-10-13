"""Forms of the fleet records: special roles, approvals and roster imports."""

from django import forms

from fleetops.forms import _style
from fleetops.models import AttendanceRecord, SpecialRole

ROSTER_IMPORT_NOTE = "Imported from fleet roster paste"


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

    action = forms.ChoiceField(choices=[(APPROVE, "Approve"), (REJECT, "Reject")])
    note = forms.CharField(max_length=255, required=False)
    include_attendance = forms.BooleanField(
        label="With its PAP rows waiting for approval", required=False, initial=True
    )

    def clean(self):
        data = super().clean()
        if data.get("action") == self.REJECT and not (data.get("note") or "").strip():
            self.add_error("note", "Give a reason for the rejection.")
        return data


class RosterPasteForm(forms.Form):
    text = forms.CharField(
        label="Roster",
        max_length=200_000,
        widget=forms.Textarea(attrs={"rows": 12}),
        help_text="Copy the fleet window (Chinese or English client), a chat channel member list or chat lines and paste them here.",
    )
    attendance_value = forms.IntegerField(min_value=1, max_value=100, initial=1)
    duplicate_action = forms.ChoiceField(
        label="If a character has tracked PAP",
        choices=AttendanceRecord.Correction.choices,
        initial=AttendanceRecord.Correction.KEEP,
    )
    notes = forms.CharField(
        required=False, max_length=1000, initial=ROSTER_IMPORT_NOTE, widget=forms.Textarea(attrs={"rows": 2})
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        _style(self.fields)
