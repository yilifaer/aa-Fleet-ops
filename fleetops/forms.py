from allianceauth.authentication.models import CharacterOwnership
from django import forms
from django.db.models import Q

from fleetops.models import (
    MAX_DATABASE_ID,
    ChannelPreset,
    CommsPreset,
    DiscordWebhook,
    FleetOpsSettings,
    FleetType,
    FleetOperation,
    IncentivePeriod,
    MessageTemplate,
    OperationRoleAssignment,
    PingTarget,
)
from fleetops.providers.doctrines import get_doctrines
from fleetops.providers.esi import has_scope_token
from fleetops.services.identity import owned_characters


class StartFleetForm(forms.Form):
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
    fc_character_id = forms.ChoiceField(label="FC Character / Fleet Boss")
    fleet_type = forms.ModelChoiceField(queryset=FleetType.objects.none())
    doctrine_choice = forms.ChoiceField(label="Doctrine", required=False)
    custom_doctrine = forms.CharField(label="Custom Doctrine", required=False, max_length=255)
    formup = forms.CharField(label="Form Up / Staging", max_length=255)
    comms = forms.ModelChoiceField(queryset=CommsPreset.objects.none(), required=False)
    logi_channel = forms.ModelChoiceField(queryset=ChannelPreset.objects.none(), required=False)
    boost_channel = forms.ModelChoiceField(queryset=ChannelPreset.objects.none(), required=False)
    ping_target = forms.ModelChoiceField(queryset=PingTarget.objects.none(), required=False)
    ping_template = forms.ModelChoiceField(queryset=MessageTemplate.objects.none(), required=False)
    motd_template = forms.ModelChoiceField(queryset=MessageTemplate.objects.none(), required=False)
    scheduled_at = forms.DateTimeField(label="Fleet Time", required=False, widget=forms.DateTimeInput(attrs={"type": "datetime-local"}))
    additional_message = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 4}))

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user
        self.fields["fleet_type"].queryset = FleetType.objects.filter(is_active=True)
        self.fields["comms"].queryset = CommsPreset.objects.filter(is_active=True)
        self.fields["logi_channel"].queryset = ChannelPreset.objects.filter(is_active=True, channel_type=ChannelPreset.ChannelType.LOGI)
        self.fields["boost_channel"].queryset = ChannelPreset.objects.filter(is_active=True, channel_type=ChannelPreset.ChannelType.BOOST)
        self.fields["ping_target"].queryset = PingTarget.objects.filter(is_active=True)
        self.fields["ping_template"].queryset = MessageTemplate.objects.filter(is_active=True, template_type=MessageTemplate.TemplateType.PING)
        self.fields["motd_template"].queryset = MessageTemplate.objects.filter(is_active=True, template_type=MessageTemplate.TemplateType.MOTD)

        character_choices = []
        if user is not None:
            main_id = getattr(getattr(user.profile, "main_character", None), "character_id", None)
            for ownership in owned_characters(user):
                c = ownership.character
                read = "R✓" if has_scope_token(user, c.character_id, write=False) else "R✗"
                write = "W✓" if has_scope_token(user, c.character_id, write=True) else "W✗"
                marker = "MAIN" if c.character_id == main_id else "ALT"
                character_choices.append((str(c.character_id), f"{c.character_name} — {marker} — {read} {write}"))
        self.fields["fc_character_id"].choices = character_choices

        doctrines = [("", "None / Custom")]
        for doctrine in get_doctrines(user):
            doctrines.append((f"{doctrine.source}|{doctrine.external_id}|{doctrine.name}", doctrine.name))
        self.fields["doctrine_choice"].choices = doctrines

        for name, field in self.fields.items():
            if name == "operation_mode":
                continue
            existing = field.widget.attrs.get("class", "")
            field.widget.attrs["class"] = (existing + " form-control").strip()
        for name in ("fleet_type", "doctrine_choice", "comms", "logi_channel", "boost_channel", "ping_target", "ping_template", "motd_template", "fc_character_id"):
            self.fields[name].widget.attrs["class"] = "form-select"

    def clean(self):
        data = super().clean()
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
        return data


class EndFleetForm(forms.Form):
    attendance_multiplier = forms.TypedChoiceField(
        label="Attendance Credit",
        choices=[(1, "1x attendance"), (2, "2x attendance"), (3, "3x attendance")],
        coerce=int,
        initial=1,
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["attendance_multiplier"].widget.attrs["class"] = "form-select"


class FleetAttendanceMultiplierForm(EndFleetForm):
    pass


class ManualFleetForm(forms.Form):
    fleet_type = forms.ModelChoiceField(queryset=FleetType.objects.none())
    doctrine_name = forms.CharField(max_length=255, required=False)
    formup = forms.CharField(label="Form Up / Staging", max_length=255)
    started_at = forms.DateTimeField(
        label="Start Time", widget=forms.DateTimeInput(attrs={"type": "datetime-local"})
    )
    ended_at = forms.DateTimeField(
        label="End Time", required=False, widget=forms.DateTimeInput(attrs={"type": "datetime-local"})
    )
    attendance_multiplier = forms.TypedChoiceField(
        choices=[(1, "1x attendance"), (2, "2x attendance"), (3, "3x attendance")],
        coerce=int,
        initial=1,
    )
    notes = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 3}))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["fleet_type"].queryset = FleetType.objects.filter(is_active=True)
        for field in self.fields.values():
            if isinstance(field.widget, forms.Select):
                field.widget.attrs["class"] = "form-select"
            else:
                field.widget.attrs["class"] = "form-control"

    def clean(self):
        data = super().clean()
        started = data.get("started_at")
        ended = data.get("ended_at")
        if started and ended and ended < started:
            raise forms.ValidationError("End Time cannot be earlier than Start Time.")
        return data


class OperationEditForm(forms.ModelForm):
    class Meta:
        model = FleetOperation
        fields = [
            "fleet_type",
            "doctrine_name",
            "formup",
            "comms",
            "logi_channel",
            "boost_channel",
            "additional_message",
            "started_at",
            "ended_at",
        ]
        widgets = {
            "additional_message": forms.Textarea(attrs={"rows": 4}),
            "started_at": forms.DateTimeInput(format="%Y-%m-%dT%H:%M", attrs={"type": "datetime-local"}),
            "ended_at": forms.DateTimeInput(format="%Y-%m-%dT%H:%M", attrs={"type": "datetime-local"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["fleet_type"].queryset = FleetType.objects.all()
        self.fields["comms"].queryset = CommsPreset.objects.all()
        self.fields["logi_channel"].queryset = ChannelPreset.objects.filter(channel_type=ChannelPreset.ChannelType.LOGI)
        self.fields["boost_channel"].queryset = ChannelPreset.objects.filter(channel_type=ChannelPreset.ChannelType.BOOST)
        self.fields["started_at"].input_formats = ["%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S"]
        self.fields["ended_at"].input_formats = ["%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S"]
        for field in self.fields.values():
            if isinstance(field.widget, forms.Select):
                field.widget.attrs["class"] = "form-select"
            else:
                field.widget.attrs["class"] = "form-control"

    def clean(self):
        data = super().clean()
        started = data.get("started_at")
        ended = data.get("ended_at")
        if started and ended and ended < started:
            raise forms.ValidationError("End Time cannot be earlier than Start Time.")
        return data


class IncentivePeriodForm(forms.ModelForm):
    class Meta:
        model = IncentivePeriod
        fields = ["year", "month", "budget", "minimum_fleets"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs["class"] = "form-control"


class ManualAttendanceForm(forms.Form):
    character_id = forms.IntegerField(min_value=1, max_value=MAX_DATABASE_ID)
    character_name = forms.CharField(max_length=255, required=False)
    attendance_value = forms.IntegerField(min_value=1, max_value=100, initial=1)
    duplicate_action = forms.ChoiceField(
        choices=[("keep", "Keep separate"), ("replace", "Replace automatic"), ("merge", "Merge into automatic")],
        initial="keep",
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
        if character_id and not CharacterOwnership.objects.filter(character__character_id=character_id).exists():
            raise forms.ValidationError(
                f"Character {character_id} is not registered to any Alliance Auth user, so it cannot receive attendance."
            )
        return data


class HistoricalManualAttendanceForm(ManualAttendanceForm):
    operation = forms.ModelChoiceField(
        queryset=FleetOperation.objects.none(),
        label="Historical Fleet",
        help_text="Active and closed fleets are available. Repeat entries or use Attendance Value > 1 when multiple credits are required.",
    )

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        qs = FleetOperation.objects.filter(
            status__in=[FleetOperation.Status.ACTIVE, FleetOperation.Status.CLOSED]
        ).select_related("fleet_type", "fc_user")
        if user is not None and not user.has_perm("fleetops.manage_fleets"):
            qs = qs.filter(
                Q(fc_user=user)
                | Q(role_assignments__auth_user=user, role_assignments__grants_fc_credit=True)
            ).distinct()
        self.fields["operation"].queryset = qs.order_by("-started_at")
        self.fields["operation"].widget.attrs["class"] = "form-select"


class OperationRoleAssignmentForm(forms.Form):
    role = forms.ChoiceField(choices=OperationRoleAssignment.Role.choices)
    character_id = forms.ChoiceField(label="Fleet Member")
    notes = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 2}))

    def __init__(self, *args, operation=None, **kwargs):
        super().__init__(*args, **kwargs)
        choices = []
        if operation is not None:
            choices = [
                (str(member.character_id), f"{member.character_name} — {member.corporation_name or 'Unknown corp'}")
                for member in operation.member_states.all().order_by("character_name")
            ]
        self.fields["character_id"].choices = choices
        self.fields["role"].widget.attrs["class"] = "form-select"
        self.fields["character_id"].widget.attrs["class"] = "form-select"
        self.fields["notes"].widget.attrs["class"] = "form-control"

    def clean_character_id(self):
        return int(self.cleaned_data["character_id"])


class FleetOpsSettingsForm(forms.ModelForm):
    class Meta:
        model = FleetOpsSettings
        fields = [
            "attendance_limit",
            "tracking_interval",
            "stale_threshold",
            "auto_end_enabled",
            "auto_end_missing_count",
            "incentive_enabled",
            "incentive_minimum_fleets",
            "data_retention_days",
            "history_alliance_ids",
            "srp_auto_create",
            "srp_provider",
        ]
        widgets = {
            "auto_end_enabled": forms.CheckboxInput(attrs={"class": "form-check-input"}),
            "incentive_enabled": forms.CheckboxInput(attrs={"class": "form-check-input"}),
            "srp_auto_create": forms.CheckboxInput(attrs={"class": "form-check-input"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name, field in self.fields.items():
            if not isinstance(field.widget, forms.CheckboxInput):
                field.widget.attrs["class"] = "form-control"


class FleetTypeForm(forms.ModelForm):
    class Meta:
        model = FleetType
        fields = ["name", "short_name", "point_weight", "is_active", "sort_order"]


class CommsPresetForm(forms.ModelForm):
    class Meta:
        model = CommsPreset
        fields = ["name", "channel_name", "voice_url", "description", "is_active"]
        widgets = {"description": forms.Textarea(attrs={"rows": 3})}


class ChannelPresetForm(forms.ModelForm):
    class Meta:
        model = ChannelPreset
        fields = ["name", "channel_type", "channel_value", "is_active"]


class DiscordWebhookForm(forms.ModelForm):
    class Meta:
        model = DiscordWebhook
        fields = ["name", "webhook_url", "is_active"]
        widgets = {
            "webhook_url": forms.TextInput(
                attrs={
                    "autocomplete": "off",
                    "placeholder": "https://discord.com/api/webhooks/...",
                }
            )
        }


class PingTargetForm(forms.ModelForm):
    class Meta:
        model = PingTarget
        fields = ["name", "target_value", "webhook", "is_active"]


class MessageTemplateForm(forms.ModelForm):
    class Meta:
        model = MessageTemplate
        fields = ["name", "template_type", "content", "is_default", "is_active"]
        widgets = {"content": forms.Textarea(attrs={"rows": 12, "class": "font-monospace form-control"})}


for _form_class in (
    FleetTypeForm,
    CommsPresetForm,
    ChannelPresetForm,
    DiscordWebhookForm,
    PingTargetForm,
    MessageTemplateForm,
):
    _original_init = _form_class.__init__

    def _styled_init(self, *args, __original_init=_original_init, **kwargs):
        __original_init(self, *args, **kwargs)
        for field in self.fields.values():
            if isinstance(field.widget, forms.CheckboxInput):
                field.widget.attrs["class"] = "form-check-input"
            elif isinstance(field.widget, forms.Select):
                field.widget.attrs["class"] = "form-select"
            else:
                existing = field.widget.attrs.get("class", "")
                field.widget.attrs["class"] = (existing + " form-control").strip()

    _form_class.__init__ = _styled_init
