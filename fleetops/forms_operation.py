# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Forms of fleet records: ending a fleet, its PAP multiplier, manual fleets, edits, incentive periods, manual
PAP and special roles."""

from datetime import datetime, timedelta, timezone as dt_timezone

from allianceauth.authentication.models import CharacterOwnership
from django import forms
from django.db.models import Q
from django.utils import timezone
from django.utils.translation import gettext
from django.utils.translation import gettext_lazy as _

from fleetops.forms_common import _style, _to_minute
from fleetops.models import (
    MAX_DATABASE_ID,
    AttendanceRecord,
    ChannelPreset,
    CommsPreset,
    FleetOperation,
    FleetType,
    IncentivePeriod,
    MessageTemplate,
    SpecialRole,
)
from fleetops.services import roster_import
from fleetops.services.roles import participant_rows
from fleetops.services.roster_paste import clean_name, name_error
from fleetops.texts import translate_stored


def multiplier_choices(values=None):
    """PAP multiplier choices: ``values``, or every configured multiplier."""
    from fleetops.services.multipliers import all_multipliers

    return [(value, f"{value}x") for value in (all_multipliers() if values is None else values)]


class EndFleetForm(forms.Form):
    """The PAP multiplier the FC picks when ending a fleet.

    Every configured multiplier is accepted here; the end view checks it against the fleet's
    duration. ``allowed`` limits the radio buttons to what the duration allows.
    """

    attendance_multiplier = forms.TypedChoiceField(
        label=_("PAP multiplier"), choices=multiplier_choices, coerce=int, widget=forms.RadioSelect
    )

    def __init__(self, *args, allowed=None, **kwargs):
        super().__init__(*args, **kwargs)
        field = self.fields["attendance_multiplier"]
        if allowed is not None:
            field.widget.choices = multiplier_choices(allowed)
        field.widget.attrs["class"] = "form-check-input"


class FleetAttendanceMultiplierForm(forms.Form):
    """Change the PAP multiplier of a closed fleet.

    The fleet's duration limits the choice. FC leads (``can_override``) may also pick any other
    configured multiplier, with a written reason.
    """

    REASON_MIN_LENGTH = 5

    attendance_multiplier = forms.TypedChoiceField(label=_("PAP multiplier"), choices=multiplier_choices, coerce=int)
    reason = forms.CharField(
        label=_("Reason for an override"),
        max_length=255,
        required=False,
        help_text=_("Required when the fleet did not run long enough for the multiplier."),
    )

    def __init__(self, *args, operation=None, can_override=False, **kwargs):
        from fleetops.services.multipliers import all_multipliers, allowed_multipliers, fleet_duration

        super().__init__(*args, **kwargs)
        self.can_override = can_override
        if operation is None:
            self.allowed = all_multipliers()
        else:
            self.allowed = allowed_multipliers(fleet_duration(operation, operation.ended_at))
        field = self.fields["attendance_multiplier"]
        shown = sorted({*self.allowed, *all_multipliers()}) if can_override else self.allowed
        field.widget.choices = [
            (value, f"{value}x" if value in self.allowed else _("%(value)sx (override)") % {"value": value})
            for value in shown
        ]
        if not can_override:
            del self.fields["reason"]
        _style(self.fields)

    def clean(self):
        from fleetops.services.multipliers import not_allowed_message

        cleaned = super().clean()
        multiplier = cleaned.get("attendance_multiplier")
        if multiplier is None or multiplier in self.allowed:
            return cleaned
        if not self.can_override:
            self.add_error("attendance_multiplier", not_allowed_message(multiplier))
        elif len((cleaned.get("reason") or "").strip()) < self.REASON_MIN_LENGTH:
            self.add_error(
                "reason",
                _("%(message)s Give a reason of at least %(count)s characters to override the duration rule.")
                % {"message": not_allowed_message(multiplier), "count": self.REASON_MIN_LENGTH},
            )
        return cleaned

    @property
    def is_override(self):
        return self.cleaned_data["attendance_multiplier"] not in self.allowed


class EveDateTimeField(forms.DateTimeField):
    """A date and time in EVE time (UTC), whatever the site TIME_ZONE or the browser's time zone."""

    widget = forms.DateTimeInput(format="%Y-%m-%dT%H:%M", attrs={"type": "datetime-local"})

    def to_python(self, value):
        # A value without an offset is read as UTC instead of the active time zone.
        with timezone.override(dt_timezone.utc):
            return super().to_python(value)

    def prepare_value(self, value):
        if isinstance(value, datetime) and timezone.is_aware(value):
            return timezone.make_naive(value, dt_timezone.utc)
        return value


def format_duration(delta):
    minutes = int(delta.total_seconds() // 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return gettext("%(hours)s h %(minutes)02d min") % {"hours": hours, "minutes": minutes}
    return gettext("%(minutes)s min") % {"minutes": minutes}


def long_fleet_message(duration):
    """Why a fleet of this length needs the confirmation box."""
    return _(
        "This fleet would last %(duration)s. "
        "Check the times, or tick this box if the fleet really ran for more than 6 hours."
    ) % {"duration": format_duration(duration)}


class ManualFleetForm(forms.Form):
    # Longer fleets are usually a mistyped date or time, so they need a confirmation.
    LONG_FLEET = timedelta(hours=6)

    fleet_type = forms.ModelChoiceField(label=_("Fleet type"), queryset=FleetType.objects.none())
    doctrine_name = forms.CharField(label=_("Doctrine name"), max_length=255, required=False)
    formup = forms.CharField(label=_("Form Up / Staging"), max_length=255)
    started_at = EveDateTimeField(label=_("Start Time — EVE time (UTC)"))
    ended_at = EveDateTimeField(label=_("End Time — EVE time (UTC)"))
    confirm_long_fleet = forms.BooleanField(
        label=_("This fleet really ran for more than 6 hours"),
        required=False,
    )
    attendance_multiplier = forms.TypedChoiceField(
        label=_("PAP multiplier"),
        choices=multiplier_choices,
        coerce=int,
        initial=1,
        help_text=_("Multipliers above 1x need a fleet that ran long enough."),
    )
    notes = forms.CharField(label=_("Notes"), required=False, widget=forms.Textarea(attrs={"rows": 3}))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["fleet_type"].queryset = FleetType.objects.filter(is_active=True)
        for field in self.fields.values():
            if isinstance(field.widget, forms.CheckboxInput):
                field.widget.attrs["class"] = "form-check-input"
            elif isinstance(field.widget, forms.Select):
                field.widget.attrs["class"] = "form-select"
            else:
                field.widget.attrs["class"] = "form-control"

    def clean_attendance_multiplier(self):
        # The same duration rule as ending a tracked fleet; the times are cleaned before this field.
        from fleetops.services.multipliers import allowed_multipliers, not_allowed_message

        multiplier = self.cleaned_data["attendance_multiplier"]
        started = self.cleaned_data.get("started_at")
        ended = self.cleaned_data.get("ended_at")
        if started and ended and ended > started and multiplier not in allowed_multipliers(ended - started):
            raise forms.ValidationError(not_allowed_message(multiplier))
        return multiplier

    def clean(self):
        data = super().clean()
        started = data.get("started_at")
        ended = data.get("ended_at")
        if started and ended:
            if ended <= started:
                self.add_error("ended_at", _("End Time must be after Start Time."))
            elif ended - started > self.LONG_FLEET and not data.get("confirm_long_fleet"):
                self.add_error("confirm_long_fleet", long_fleet_message(ended - started))
        return data


class OperationEditForm(forms.ModelForm):
    LONG_FLEET = ManualFleetForm.LONG_FLEET

    started_at = EveDateTimeField(label=_("Start Time — EVE time (UTC)"))
    ended_at = EveDateTimeField(label=_("End Time — EVE time (UTC)"), required=False)
    confirm_long_fleet = forms.BooleanField(
        label=_("This fleet really ran for more than 6 hours"),
        required=False,
    )

    class Meta:
        model = FleetOperation
        fields = [
            "fleet_type",
            "doctrine_name",
            "formup",
            "comms",
            "logi_channel",
            "boost_channel",
            "ping_template",
            "motd_template",
            "additional_message",
            "started_at",
            "ended_at",
        ]
        widgets = {
            "additional_message": forms.Textarea(attrs={"rows": 4}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["fleet_type"].queryset = FleetType.objects.all()
        self.fields["comms"].queryset = CommsPreset.objects.all()
        self.fields["logi_channel"].queryset = ChannelPreset.objects.filter(channel_type=ChannelPreset.ChannelType.LOGI)
        self.fields["boost_channel"].queryset = ChannelPreset.objects.filter(channel_type=ChannelPreset.ChannelType.BOOST)
        self.fields["ping_template"].queryset = MessageTemplate.objects.filter(template_type=MessageTemplate.TemplateType.PING)
        self.fields["motd_template"].queryset = MessageTemplate.objects.filter(template_type=MessageTemplate.TemplateType.MOTD)
        for field in self.fields.values():
            if isinstance(field.widget, forms.CheckboxInput):
                field.widget.attrs["class"] = "form-check-input"
            elif isinstance(field.widget, forms.Select):
                field.widget.attrs["class"] = "form-select"
            else:
                field.widget.attrs["class"] = "form-control"

    def clean(self):
        data = super().clean()
        started = data.get("started_at")
        ended = data.get("ended_at")
        if started and ended:
            # Only new times are checked strictly; a fleet that was already stored stays editable.
            stored = (_to_minute(self.instance.started_at), _to_minute(self.instance.ended_at))
            times_changed = (_to_minute(started), _to_minute(ended)) != stored
            if ended < started or (ended == started and times_changed):
                self.add_error("ended_at", _("End Time must be after Start Time."))
            elif times_changed and ended - started > self.LONG_FLEET and not data.get("confirm_long_fleet"):
                self.add_error("confirm_long_fleet", long_fleet_message(ended - started))
        return data


class IncentivePeriodForm(forms.ModelForm):
    """Budget and minimum fleets of an incentive month; the month itself comes from the page."""

    class Meta:
        model = IncentivePeriod
        fields = ["budget", "minimum_fleets"]
        labels = {"budget": _("Budget (ISK)"), "minimum_fleets": _("Minimum fleets")}
        help_texts = {"minimum_fleets": _("Credited fleets an FC needs in this month to receive a payout.")}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs["class"] = "form-control"


class ManualAttendanceForm(forms.Form):
    character_name = forms.CharField(
        label=_("Character name"),
        max_length=255,
        required=False,
        help_text=_("Looked up in Alliance Auth, then in EVE."),
    )
    character_id = forms.IntegerField(
        label=_("Character ID"),
        min_value=1,
        max_value=MAX_DATABASE_ID,
        required=False,
        help_text=_("Optional; use it when the name cannot be looked up."),
    )
    attendance_value = forms.IntegerField(label=_("PAP value"), min_value=1, max_value=100, initial=1)
    duplicate_action = forms.ChoiceField(
        label=_("If the character has tracked PAP"),
        choices=AttendanceRecord.Correction.choices,
        initial=AttendanceRecord.Correction.KEEP,
    )
    notes = forms.CharField(label=_("Notes"), required=False, widget=forms.Textarea(attrs={"rows": 2}))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs["class"] = "form-control"
        self.fields["duplicate_action"].widget.attrs["class"] = "form-select"

    def clean(self):
        data = super().clean()
        character_id = data.get("character_id")
        if character_id:
            if not CharacterOwnership.objects.filter(character__character_id=character_id).exists():
                raise forms.ValidationError(
                    _("Character %(id)s is not registered to any Alliance Auth user, so it cannot receive PAP.")
                    % {"id": character_id}
                )
            return data
        if self.errors:
            # The name is only looked up for an entry that can be saved.
            return data
        name = clean_name(data.get("character_name", ""))
        if not name:
            raise forms.ValidationError(_("Enter a character name or a character ID."))
        error = name_error(name)
        if error:
            self.add_error(
                "character_name",
                _("'%(name)s' cannot be an EVE character name. %(error)s") % {"name": name, "error": error},
            )
            return data
        resolved = roster_import.resolve_character_name(name)
        if resolved.status == roster_import.AUTH:
            data["character_id"] = resolved.character_id
            data["character_name"] = resolved.name
        elif resolved.status == roster_import.NOT_IN_AUTH:
            self.add_error(
                "character_name",
                _(
                    "Character '%(name)s' exists in EVE but is not registered in Alliance Auth, "
                    "so it cannot receive PAP."
                )
                % {"name": resolved.name},
            )
        elif resolved.status == roster_import.ERROR:
            self.add_error(
                "character_name",
                _("Could not look the name up in ESI (%(error)s). Enter the character ID instead.")
                % {"error": translate_stored(resolved.error).rstrip(".")},
            )
        else:
            self.add_error("character_name", _("No EVE character named '%(name)s' was found.") % {"name": name})
        return data


def manageable_attendance_operations(user):
    """Active and closed fleets whose PAP ``user`` may correct, newest first."""
    qs = FleetOperation.objects.filter(
        status__in=[FleetOperation.Status.ACTIVE, FleetOperation.Status.CLOSED]
    ).select_related("fleet_type", "fc_user")
    if user is not None and not user.has_perm("fleetops.manage_fleets"):
        qs = qs.filter(
            Q(fc_user=user)
            | Q(role_assignments__auth_user=user, role_assignments__grants_fc_credit=True)
        ).distinct()
    return qs.order_by("-started_at")


class HistoricalManualAttendanceForm(ManualAttendanceForm):
    operation = forms.ModelChoiceField(
        queryset=FleetOperation.objects.none(),
        label=_("Historical Fleet"),
        help_text=_(
            "Active and closed fleets are available. Repeat entries or use PAP value > 1 when multiple credits "
            "are required."
        ),
    )
    field_order = ["operation"]

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["operation"].queryset = manageable_attendance_operations(user)
        self.fields["operation"].widget.attrs["class"] = "form-select"


class OperationRoleAssignmentForm(forms.Form):
    role = forms.ModelChoiceField(
        label=_("Role"),
        queryset=SpecialRole.objects.none(),
        to_field_name="key",
        empty_label=None,
        help_text=_("Roles marked FC credit count the fleet as an FC fleet for the pilot's FC points and incentives."),
    )
    character_id = forms.ChoiceField(label=_("Fleet Member"))
    notes = forms.CharField(label=_("Notes"), required=False, widget=forms.Textarea(attrs={"rows": 2}))

    def __init__(self, *args, operation=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["role"].queryset = SpecialRole.objects.filter(is_active=True)
        self.fields["role"].label_from_instance = _role_label
        self.fields["character_id"].choices = _fleet_member_choices(operation) if operation is not None else []
        self.fields["role"].widget.attrs["class"] = "form-select"
        self.fields["character_id"].widget.attrs["class"] = "form-select"
        self.fields["notes"].widget.attrs["class"] = "form-control"

    def clean_character_id(self):
        return int(self.cleaned_data["character_id"])


def _role_label(role):
    return _("%(name)s (FC credit)") % {"name": role.name} if role.grants_fc_credit else role.name


def _fleet_member_choices(operation):
    """Everybody recorded in the fleet: tracked members, PAP link registrations and PAP rows (``participant_rows``)."""
    names = {}
    corporations = {}
    for character_id, name, corporation in operation.member_states.values_list(
        "character_id", "character_name", "corporation_name"
    ):
        names.setdefault(character_id, name)
        corporations.setdefault(character_id, corporation)
    for source in (operation.pap_registrations, participant_rows(operation)):
        for character_id, name in source.values_list("character_id", "character_name"):
            names.setdefault(character_id, name)
    unknown = _("Unknown corp")
    return [
        (
            str(character_id),
            f"{name} — {corporations[character_id] or unknown}" if character_id in corporations else name,
        )
        for character_id, name in sorted(names.items(), key=lambda item: (item[1].casefold(), item[0]))
    ]
