import uuid
from datetime import datetime, timedelta, timezone as dt_timezone

from allianceauth.authentication.models import CharacterOwnership
from django import forms
from django.db.models import Q
from django.utils import timezone

from fleetops.forms_config import (  # noqa: F401 - configuration forms, re-exported
    ChannelPresetForm,
    CommsPresetForm,
    DiscordWebhookForm,
    FleetOpsSettingsForm,
    FleetTypeForm,
    MessageTemplateForm,
    PingRoleForm,
    PingTargetForm,
)
from fleetops.models import (
    MAX_DATABASE_ID,
    AttendanceRecord,
    ChannelPreset,
    CommsPreset,
    DiscordWebhook,
    FleetType,
    FleetOperation,
    IncentivePeriod,
    MessageTemplate,
    PingRole,
    SpecialRole,
)
from fleetops.providers.doctrines import get_doctrines
from fleetops.providers.esi import has_scope_token
from fleetops.services import roster_import
from fleetops.services.identity import owned_characters
from fleetops.services.mentions import (
    EVERYONE,
    HERE,
    NONE,
    choice_values,
    role_choice,
    selection_from_choices,
    selection_from_operation,
    validate_for_channel,
)
from fleetops.services.periods import EVE_CLOCK_PATTERN, format_eve, parse_eve_datetime
from fleetops.services.roles import participant_rows
from fleetops.services.roster_paste import clean_name, name_error

PING_NOW = "now"
PRE_PING = "pre_ping"
PRE_PING_NEEDS_DISCORD = "A pre ping is a Discord message, so it cannot be sent in attendance tracking only mode."
FLEET_TIME_MISSING = "Enter the fleet date and time in EVE time (UTC)."
FLEET_TIME_PAST = "The fleet time must be in the future (EVE time)."


def _style(fields):
    for field in fields.values():
        widget = field.widget
        if isinstance(widget, (forms.CheckboxInput, forms.CheckboxSelectMultiple, forms.RadioSelect)):
            widget.attrs["class"] = "form-check-input"
        else:
            widget.attrs["class"] = "form-select" if isinstance(widget, forms.Select) else "form-control"


def character_choices(user):
    """The user's characters for a start, with their fleet token state."""
    choices = []
    if user is None:
        return choices
    main_id = getattr(getattr(user.profile, "main_character", None), "character_id", None)
    for ownership in owned_characters(user):
        c = ownership.character
        read = "R✓" if has_scope_token(user, c.character_id, write=False) else "R✗"
        write = "W✓" if has_scope_token(user, c.character_id, write=True) else "W✗"
        marker = "MAIN" if c.character_id == main_id else "ALT"
        choices.append((str(c.character_id), f"{c.character_name} — {marker} — {read} {write}"))
    return choices


class FleetSettingsForm(forms.Form):
    """Fleet, communication and message fields shared by the start and the scheduled fleet forms."""

    fleet_type = forms.ModelChoiceField(queryset=FleetType.objects.none())
    doctrine_choice = forms.ChoiceField(label="Doctrine", required=False)
    custom_doctrine = forms.CharField(label="Custom Doctrine", required=False, max_length=255)
    formup = forms.CharField(label="Form Up / Staging", max_length=255)
    fleet_date = forms.DateField(
        label="Date",
        required=False,
        widget=forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"}),
    )
    fleet_clock = forms.CharField(
        label="Time (HH:MM)",
        required=False,
        max_length=5,
        widget=forms.TextInput(
            attrs={"pattern": EVE_CLOCK_PATTERN, "placeholder": "HH:MM", "inputmode": "numeric", "autocomplete": "off"}
        ),
    )
    comms = forms.ModelChoiceField(queryset=CommsPreset.objects.none(), required=False)
    logi_channel = forms.ModelChoiceField(queryset=ChannelPreset.objects.none(), required=False)
    boost_channel = forms.ModelChoiceField(queryset=ChannelPreset.objects.none(), required=False)
    ping_channel = forms.ModelChoiceField(
        label="Ping channel",
        queryset=DiscordWebhook.objects.none(),
        required=False,
        empty_label="No Discord message (copy only)",
    )
    ping_mentions = forms.MultipleChoiceField(
        label="Ping target", required=False, widget=forms.CheckboxSelectMultiple, initial=[NONE]
    )
    ping_template = forms.ModelChoiceField(queryset=MessageTemplate.objects.none(), required=False)
    motd_template = forms.ModelChoiceField(queryset=MessageTemplate.objects.none(), required=False)
    additional_message = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 4}))

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user
        self.fields["fleet_type"].queryset = FleetType.objects.filter(is_active=True)
        self.fields["comms"].queryset = CommsPreset.objects.filter(is_active=True)
        self.fields["logi_channel"].queryset = ChannelPreset.objects.filter(is_active=True, channel_type=ChannelPreset.ChannelType.LOGI)
        self.fields["boost_channel"].queryset = ChannelPreset.objects.filter(is_active=True, channel_type=ChannelPreset.ChannelType.BOOST)
        self.fields["ping_channel"].queryset = DiscordWebhook.objects.filter(is_active=True)
        self.fields["ping_template"].queryset = MessageTemplate.objects.filter(is_active=True, template_type=MessageTemplate.TemplateType.PING)
        self.fields["motd_template"].queryset = MessageTemplate.objects.filter(is_active=True, template_type=MessageTemplate.TemplateType.MOTD)
        self.fields["ping_mentions"].choices = [(NONE, "No ping"), (EVERYONE, "@everyone"), (HERE, "@here")] + [
            (role_choice(role), f"@{role.name}") for role in PingRole.objects.filter(is_active=True)
        ]

        doctrines = [("", "None / Custom")]
        for doctrine in get_doctrines(user):
            doctrines.append((f"{doctrine.source}|{doctrine.external_id}|{doctrine.name}", doctrine.name))
        self.fields["doctrine_choice"].choices = doctrines
        _style(self.fields)

    def channel_rules(self):
        """Mentions each ping channel allows, keyed by channel pk; ``roles`` None allows every active role."""
        rules = {}
        for channel in self.fields["ping_channel"].queryset.prefetch_related("allowed_roles"):
            roles = [role.pk for role in channel.allowed_roles.all()]
            rules[str(channel.pk)] = {
                "everyone": channel.allow_everyone,
                "here": channel.allow_here,
                "roles": roles or None,
            }
        return rules

    def clean_fleet_clock(self):
        # The format is checked with the date, and only when the fleet has a fleet time.
        return (self.cleaned_data.get("fleet_clock") or "").strip()

    def _clean_doctrine(self, data):
        choice = data.get("doctrine_choice") or ""
        custom = (data.get("custom_doctrine") or "").strip()
        if choice:
            try:
                source, external_id, name = choice.split("|", 2)
            except ValueError:
                raise forms.ValidationError("Invalid doctrine selection.")
            data["doctrine_source"] = source
            data["doctrine_external_id"] = external_id
            data["doctrine_name"] = name
        else:
            data["doctrine_source"] = "custom" if custom else "none"
            data["doctrine_external_id"] = ""
            data["doctrine_name"] = custom

    def _clean_fleet_time(self, data, pre_ping, stored=None):
        """``scheduled_at`` from the EVE date and time of a pre ping, never in the site's time zone.

        ``stored`` is the fleet time already saved; it is kept as it is, even when it has passed.
        """
        data["is_pre_ping"] = pre_ping
        data["scheduled_at"] = None
        if not pre_ping or "fleet_date" in self.errors or "fleet_clock" in self.errors:
            return
        day, clock = data.get("fleet_date"), data.get("fleet_clock")
        if not day or not clock:
            self.add_error("fleet_date" if not day else "fleet_clock", FLEET_TIME_MISSING)
            return
        try:
            scheduled_at = parse_eve_datetime(day, clock)
        except ValueError as exc:
            self.add_error("fleet_clock", forms.ValidationError(str(exc), code="invalid"))
            return
        if stored is not None and scheduled_at == _to_minute(stored):
            # An overdue fleet stays editable as long as its time is not changed.
            data["scheduled_at"] = stored
            return
        if scheduled_at <= timezone.now():
            self.add_error("fleet_clock", FLEET_TIME_PAST)
            return
        data["scheduled_at"] = scheduled_at

    def _clean_mentions(self, data, sends):
        """``mention_selection``; when the ping is posted, also the rules of its channel."""
        if "ping_mentions" in self.errors:
            return
        try:
            selection = selection_from_choices(data.get("ping_mentions") or [])
        except forms.ValidationError as exc:
            self.add_error("ping_mentions", exc)
            return
        data["mention_selection"] = selection
        # A ping that is only copied may name anybody; the channel decides what can be posted.
        if sends:
            for message in validate_for_channel(selection, data.get("ping_channel")):
                self.add_error("ping_mentions", message)


class StartFleetForm(FleetSettingsForm):
    field_order = [
        "request_id",
        "operation_mode",
        "fleet_timing",
        "fc_character_id",
        "fleet_type",
        "doctrine_choice",
        "custom_doctrine",
        "formup",
    ]

    request_id = forms.UUIDField(widget=forms.HiddenInput())
    operation_mode = forms.ChoiceField(
        label="Operation Mode",
        choices=[
            ("full", "Send Ping + MOTD/SRP + Track Attendance"),
            ("attendance_only", "Attendance Tracking Only (no Discord ping / MOTD / SRP)"),
        ],
        initial="full",
        widget=forms.RadioSelect,
    )
    fleet_timing = forms.ChoiceField(
        label="Fleet Time",
        choices=[(PING_NOW, "Ping now"), (PRE_PING, "Pre ping")],
        initial=PING_NOW,
        required=False,
        widget=forms.RadioSelect,
    )
    fc_character_id = forms.ChoiceField(label="FC Character / Fleet Boss")

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, user=user, **kwargs)
        self.fields["fc_character_id"].choices = character_choices(user)

    def clean(self):
        data = super().clean()
        self._clean_doctrine(data)
        full_mode = data.get("operation_mode", "full") == "full"
        pre_ping = data.get("fleet_timing") == PRE_PING
        if pre_ping and not full_mode:
            self.add_error("fleet_timing", PRE_PING_NEEDS_DISCORD)
        self._clean_fleet_time(data, pre_ping)
        self._clean_mentions(data, sends=full_mode)
        return data


class ScheduledFleetForm(FleetSettingsForm):
    """Edit a scheduled fleet: the start form without FC, mode and timing choice."""

    send_update = forms.BooleanField(label="Post the updated pre ping", required=False)
    # One per edit page, so a double click on Save posts the updated pre ping once.
    edit_token = forms.UUIDField(widget=forms.HiddenInput(), required=False)

    def __init__(self, *args, operation, user=None, **kwargs):
        if not args and "data" not in kwargs:
            kwargs.setdefault("initial", {**scheduled_fleet_initial(operation), "edit_token": uuid.uuid4()})
        super().__init__(*args, user=user, **kwargs)
        self.operation = operation
        self.fields["fleet_date"].required = True
        self.fields["fleet_clock"].required = True
        doctrine = self.initial.get("doctrine_choice")
        if doctrine and doctrine not in dict(self.fields["doctrine_choice"].choices):
            # The doctrine is no longer offered; keep its name as a custom doctrine.
            self.initial["doctrine_choice"] = ""
            self.initial["custom_doctrine"] = operation.doctrine_name

    def clean(self):
        data = super().clean()
        self._clean_doctrine(data)
        self._clean_fleet_time(data, True, stored=self.operation.scheduled_at)
        self._clean_mentions(data, sends=True)
        data["operation_mode"] = "full"
        return data


def scheduled_fleet_initial(operation):
    doctrine = ""
    if operation.doctrine_source not in ("", "custom", "none", "manual"):
        doctrine = f"{operation.doctrine_source}|{operation.doctrine_external_id}|{operation.doctrine_name}"
    return {
        "fleet_type": operation.fleet_type_id,
        "doctrine_choice": doctrine,
        "custom_doctrine": "" if doctrine else operation.doctrine_name,
        "formup": operation.formup,
        "fleet_date": format_eve(operation.scheduled_at, "%Y-%m-%d"),
        "fleet_clock": format_eve(operation.scheduled_at, "%H:%M"),
        "comms": operation.comms_id,
        "logi_channel": operation.logi_channel_id,
        "boost_channel": operation.boost_channel_id,
        "ping_channel": operation.ping_channel_id,
        "ping_mentions": choice_values(selection_from_operation(operation)),
        "ping_template": operation.ping_template_id,
        "motd_template": operation.motd_template_id,
        "additional_message": operation.additional_message,
    }


class StartScheduledFleetForm(forms.Form):
    fc_character_id = forms.ChoiceField(label="FC Character / Fleet Boss")
    send_ping = forms.BooleanField(label="Send a form-up ping now", required=False, initial=True)

    def __init__(self, *args, user=None, operation=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["fc_character_id"].choices = character_choices(user)
        if operation is not None and not self.is_bound:
            self.initial.setdefault("fc_character_id", str(operation.fc_character_id))
        self.fields["fc_character_id"].widget.attrs["class"] = "form-select"
        self.fields["send_ping"].widget.attrs["class"] = "form-check-input"


class CancelScheduledFleetForm(forms.Form):
    notify = forms.BooleanField(label="Post a cancellation notice", required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["notify"].widget.attrs["class"] = "form-check-input"


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
        label="PAP multiplier", choices=multiplier_choices, coerce=int, widget=forms.RadioSelect
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

    attendance_multiplier = forms.TypedChoiceField(label="PAP multiplier", choices=multiplier_choices, coerce=int)
    reason = forms.CharField(
        label="Reason for an override",
        max_length=255,
        required=False,
        help_text="Required when the fleet did not run long enough for the multiplier.",
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
            (value, f"{value}x" if value in self.allowed else f"{value}x (override)") for value in shown
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
                f"{not_allowed_message(multiplier)} Give a reason of at least {self.REASON_MIN_LENGTH} "
                "characters to override the duration rule.",
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
    return f"{hours} h {minutes:02d} min" if hours else f"{minutes} min"


class ManualFleetForm(forms.Form):
    # Longer fleets are usually a mistyped date or time, so they need a confirmation.
    LONG_FLEET = timedelta(hours=6)

    fleet_type = forms.ModelChoiceField(queryset=FleetType.objects.none())
    doctrine_name = forms.CharField(max_length=255, required=False)
    formup = forms.CharField(label="Form Up / Staging", max_length=255)
    started_at = EveDateTimeField(label="Start Time — EVE time (UTC)")
    ended_at = EveDateTimeField(label="End Time — EVE time (UTC)")
    confirm_long_fleet = forms.BooleanField(
        label="This fleet really ran for more than 6 hours",
        required=False,
    )
    attendance_multiplier = forms.TypedChoiceField(
        label="PAP multiplier",
        choices=multiplier_choices,
        coerce=int,
        initial=1,
        help_text="Multipliers above 1x need a fleet that ran long enough.",
    )
    notes = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 3}))

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
                self.add_error("ended_at", "End Time must be after Start Time.")
            elif ended - started > self.LONG_FLEET and not data.get("confirm_long_fleet"):
                self.add_error(
                    "confirm_long_fleet",
                    f"This fleet would last {format_duration(ended - started)}. "
                    "Check the times, or tick this box if the fleet really ran for more than 6 hours.",
                )
        return data


def _to_minute(value):
    # The form shows times to the minute.
    return value.replace(second=0, microsecond=0) if value else None


class OperationEditForm(forms.ModelForm):
    LONG_FLEET = ManualFleetForm.LONG_FLEET

    started_at = EveDateTimeField(label="Start Time — EVE time (UTC)")
    ended_at = EveDateTimeField(label="End Time — EVE time (UTC)", required=False)
    confirm_long_fleet = forms.BooleanField(
        label="This fleet really ran for more than 6 hours",
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
                self.add_error("ended_at", "End Time must be after Start Time.")
            elif times_changed and ended - started > self.LONG_FLEET and not data.get("confirm_long_fleet"):
                self.add_error(
                    "confirm_long_fleet",
                    f"This fleet would last {format_duration(ended - started)}. "
                    "Check the times, or tick this box if the fleet really ran for more than 6 hours.",
                )
        return data


class IncentivePeriodForm(forms.ModelForm):
    """Budget and minimum fleets of an incentive month; the month itself comes from the page."""

    class Meta:
        model = IncentivePeriod
        fields = ["budget", "minimum_fleets"]
        labels = {"budget": "Budget (ISK)", "minimum_fleets": "Minimum fleets"}
        help_texts = {"minimum_fleets": "Credited fleets an FC needs in this month to receive a payout."}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs["class"] = "form-control"


class ManualAttendanceForm(forms.Form):
    character_name = forms.CharField(
        label="Character name",
        max_length=255,
        required=False,
        help_text="Looked up in Alliance Auth, then in EVE.",
    )
    character_id = forms.IntegerField(
        label="Character ID",
        min_value=1,
        max_value=MAX_DATABASE_ID,
        required=False,
        help_text="Optional; use it when the name cannot be looked up.",
    )
    attendance_value = forms.IntegerField(min_value=1, max_value=100, initial=1)
    duplicate_action = forms.ChoiceField(
        label="If the character has tracked PAP",
        choices=AttendanceRecord.Correction.choices,
        initial=AttendanceRecord.Correction.KEEP,
    )
    notes = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 2}))

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
                    f"Character {character_id} is not registered to any Alliance Auth user, so it cannot receive attendance."
                )
            return data
        if self.errors:
            # The name is only looked up for an entry that can be saved.
            return data
        name = clean_name(data.get("character_name", ""))
        if not name:
            raise forms.ValidationError("Enter a character name or a character ID.")
        error = name_error(name)
        if error:
            self.add_error("character_name", f"'{name}' cannot be an EVE character name. {error}")
            return data
        resolved = roster_import.resolve_character_name(name)
        if resolved.status == roster_import.AUTH:
            data["character_id"] = resolved.character_id
            data["character_name"] = resolved.name
        elif resolved.status == roster_import.NOT_IN_AUTH:
            self.add_error(
                "character_name",
                f"Character '{resolved.name}' exists in EVE but is not registered in Alliance Auth, "
                "so it cannot receive PAP.",
            )
        elif resolved.status == roster_import.ERROR:
            self.add_error(
                "character_name",
                f"Could not look the name up in ESI ({resolved.error.rstrip('.')}). Enter the character ID instead.",
            )
        else:
            self.add_error("character_name", f"No EVE character named '{name}' was found.")
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
        label="Historical Fleet",
        help_text="Active and closed fleets are available. Repeat entries or use Attendance Value > 1 when multiple credits are required.",
    )
    field_order = ["operation"]

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["operation"].queryset = manageable_attendance_operations(user)
        self.fields["operation"].widget.attrs["class"] = "form-select"


class OperationRoleAssignmentForm(forms.Form):
    role = forms.ModelChoiceField(
        queryset=SpecialRole.objects.none(),
        to_field_name="key",
        empty_label=None,
        help_text="Roles marked FC credit count the fleet as an FC fleet for the pilot's FC points and incentives.",
    )
    character_id = forms.ChoiceField(label="Fleet Member")
    notes = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 2}))

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
    return f"{role.name} (FC credit)" if role.grants_fc_credit else role.name


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
    return [
        (str(character_id), f"{name} — {corporations[character_id] or 'Unknown corp'}" if character_id in corporations else name)
        for character_id, name in sorted(names.items(), key=lambda item: (item[1].casefold(), item[0]))
    ]
