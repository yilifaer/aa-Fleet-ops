# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

import uuid

from django import forms
from django.db.models import Q
from django.utils import timezone
from django.utils.translation import pgettext_lazy
from django.utils.translation import gettext_lazy as _

from fleetops.forms_common import _style, _to_minute
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
from fleetops.forms_operation import (  # noqa: F401 - record forms, re-exported
    EndFleetForm,
    EveDateTimeField,
    FleetAttendanceMultiplierForm,
    HistoricalManualAttendanceForm,
    IncentivePeriodForm,
    ManualAttendanceForm,
    ManualFleetForm,
    OperationEditForm,
    OperationRoleAssignmentForm,
    _fleet_member_choices,
    _role_label,
    format_duration,
    long_fleet_message,
    manageable_attendance_operations,
    multiplier_choices,
)
from fleetops.models import (
    ChannelPreset,
    CommsPreset,
    DiscordWebhook,
    FleetType,
    MessageTemplate,
    PingRole,
)
from fleetops.providers.doctrines import get_doctrines
from fleetops.providers.esi import has_scope_token
from fleetops.services import group_access
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

PING_NOW = "now"
PRE_PING = "pre_ping"
PRE_PING_NEEDS_DISCORD = _("A pre ping is a Discord message, so it cannot be sent in PAP tracking only mode.")
FLEET_TIME_MISSING = _("Enter the fleet date and time in EVE time (UTC).")
FLEET_TIME_PAST = _("The fleet time must be in the future (EVE time).")
SCHEDULED_CHANNEL_GONE = _("The ping channel of this fleet can no longer be used. Choose another channel.")


def _can_start_alliance_fleets(user):
    return user is not None and user.has_perm("fleetops.start_fleet")


def start_fleet_types(user):
    """Fleet types the start form offers: every active type to FCs, the types of their groups to group leaders."""
    active = FleetType.objects.filter(is_active=True)
    if _can_start_alliance_fleets(user):
        return active
    if user is None:
        return active.none()
    type_ids = set().union(*(grant.fleet_type_ids for grant in group_access.leader_access(user).fleet_grants))
    return active.filter(pk__in=type_ids)


def start_ping_channels(user):
    """Ping channels the start form offers: the channels FCs may use for alliance fleets (FCs only) and the
    channels of the groups whose fleets the user may start. The services check every post again."""
    if user is None:
        return DiscordWebhook.objects.none()
    alliance = group_access.alliance_channels(user) if _can_start_alliance_fleets(user) else None
    groups = group_access.group_channels(group_access.leader_access(user).fleet_group_ids)
    if alliance is None:
        return groups
    if not groups.query.is_empty():
        return DiscordWebhook.objects.filter(Q(pk__in=alliance.values("pk")) | Q(pk__in=groups.values("pk")))
    return alliance


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
        marker = _("MAIN") if c.character_id == main_id else _("ALT")
        choices.append((str(c.character_id), f"{c.character_name} — {marker} — {read} {write}"))
    return choices


class FleetSettingsForm(forms.Form):
    """Fleet, communication and message fields shared by the start and the scheduled fleet forms."""

    fleet_type = forms.ModelChoiceField(label=_("Fleet type"), queryset=FleetType.objects.none())
    doctrine_choice = forms.ChoiceField(label=pgettext_lazy("FleetOps", "Doctrine"), required=False)
    custom_doctrine = forms.CharField(label=_("Custom Doctrine"), required=False, max_length=255)
    formup = forms.CharField(label=_("Form Up / Staging"), max_length=255)
    fleet_date = forms.DateField(
        label=_("Date"),
        required=False,
        widget=forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"}),
    )
    fleet_clock = forms.CharField(
        label=_("Time (HH:MM)"),
        required=False,
        max_length=5,
        widget=forms.TextInput(
            attrs={"pattern": EVE_CLOCK_PATTERN, "placeholder": "HH:MM", "inputmode": "numeric", "autocomplete": "off"}
        ),
    )
    comms = forms.ModelChoiceField(label=_("Comms"), queryset=CommsPreset.objects.none(), required=False)
    logi_channel = forms.ModelChoiceField(
        label=_("Logi channel"),
        queryset=ChannelPreset.objects.none(),
        required=False,
    )
    boost_channel = forms.ModelChoiceField(
        label=_("Boost channel"),
        queryset=ChannelPreset.objects.none(),
        required=False,
    )
    ping_channel = forms.ModelChoiceField(
        label=_("Ping channel"),
        queryset=DiscordWebhook.objects.none(),
        required=False,
        empty_label=_("No Discord message (copy only)"),
    )
    ping_mentions = forms.MultipleChoiceField(
        label=_("Ping target"), required=False, widget=forms.CheckboxSelectMultiple, initial=[NONE]
    )
    ping_template = forms.ModelChoiceField(
        label=_("Ping template"),
        queryset=MessageTemplate.objects.none(),
        required=False,
    )
    motd_template = forms.ModelChoiceField(
        label=_("MOTD template"),
        queryset=MessageTemplate.objects.none(),
        required=False,
    )
    additional_message = forms.CharField(
        label=_("Additional message"),
        required=False,
        widget=forms.Textarea(attrs={"rows": 4}),
    )

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user
        self.fields["fleet_type"].queryset = start_fleet_types(user)
        self.fields["comms"].queryset = CommsPreset.objects.filter(is_active=True)
        self.fields["logi_channel"].queryset = ChannelPreset.objects.filter(is_active=True, channel_type=ChannelPreset.ChannelType.LOGI)
        self.fields["boost_channel"].queryset = ChannelPreset.objects.filter(is_active=True, channel_type=ChannelPreset.ChannelType.BOOST)
        self.fields["ping_channel"].queryset = start_ping_channels(user)
        self.fields["ping_template"].queryset = MessageTemplate.objects.filter(is_active=True, template_type=MessageTemplate.TemplateType.PING)
        self.fields["motd_template"].queryset = MessageTemplate.objects.filter(is_active=True, template_type=MessageTemplate.TemplateType.MOTD)
        self.fields["ping_mentions"].choices = [(NONE, _("No ping")), (EVERYONE, "@everyone"), (HERE, "@here")] + [
            (role_choice(role), f"@{role.name}") for role in PingRole.objects.filter(is_active=True)
        ]

        doctrines = [("", _("None / Custom"))]
        for doctrine in get_doctrines(user):
            doctrines.append((f"{doctrine.source}|{doctrine.external_id}|{doctrine.name}", doctrine.name))
        self.fields["doctrine_choice"].choices = doctrines
        _style(self.fields)

    def group_rules(self):
        """The fleet types and channels of each group the user may start fleets for, for the page's scripts.

        Empty until the start page offers group fleets.
        """
        return {}

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
                raise forms.ValidationError(_("Invalid doctrine selection."))
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
        label=_("Operation Mode"),
        choices=[
            ("full", _("Send Ping + MOTD/SRP + Track PAP")),
            ("attendance_only", _("PAP Tracking Only (no Discord ping / MOTD / SRP)")),
        ],
        initial="full",
        widget=forms.RadioSelect,
    )
    fleet_timing = forms.ChoiceField(
        label=pgettext_lazy("FleetOps", "Fleet Time"),
        choices=[(PING_NOW, _("Ping now")), (PRE_PING, _("Pre ping"))],
        initial=PING_NOW,
        required=False,
        widget=forms.RadioSelect,
    )
    fc_character_id = forms.ChoiceField(label=_("FC Character / Fleet Boss"))

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

    send_update = forms.BooleanField(label=_("Post the updated pre ping"), required=False)
    # One per edit page, so a double click on Save posts the updated pre ping once.
    edit_token = forms.UUIDField(widget=forms.HiddenInput(), required=False)

    def __init__(self, *args, operation, user=None, **kwargs):
        if not args and "data" not in kwargs:
            kwargs.setdefault("initial", {**scheduled_fleet_initial(operation), "edit_token": uuid.uuid4()})
        super().__init__(*args, user=user, **kwargs)
        self.operation = operation
        self.fields["fleet_date"].required = True
        self.fields["fleet_clock"].required = True
        # Scheduled fleets are alliance fleets: only the channels FCs may use for them.
        channel = self.fields["ping_channel"]
        channel.queryset = group_access.alliance_channels(user) if user is not None else DiscordWebhook.objects.none()
        if operation.ping_channel_id and not channel.queryset.filter(pk=operation.ping_channel_id).exists():
            # The channel was reserved for a group (or switched off) after the fleet was announced: choose another.
            channel.required = True
            channel.help_text = SCHEDULED_CHANNEL_GONE
            if not self.is_bound:
                self.initial["ping_channel"] = None
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
    fc_character_id = forms.ChoiceField(label=_("FC Character / Fleet Boss"))
    send_ping = forms.BooleanField(label=_("Send a form-up ping now"), required=False, initial=True)

    def __init__(self, *args, user=None, operation=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["fc_character_id"].choices = character_choices(user)
        if operation is not None and not self.is_bound:
            self.initial.setdefault("fc_character_id", str(operation.fc_character_id))
        self.fields["fc_character_id"].widget.attrs["class"] = "form-select"
        self.fields["send_ping"].widget.attrs["class"] = "form-check-input"


class CancelScheduledFleetForm(forms.Form):
    notify = forms.BooleanField(label=_("Post a cancellation notice"), required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["notify"].widget.attrs["class"] = "form-check-input"
