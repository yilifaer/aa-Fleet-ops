"""Forms of the FleetOps configuration pages."""

import re
import uuid
from urllib.parse import urlsplit

from django import forms
from django.core.exceptions import NON_FIELD_ERRORS, ValidationError

from fleetops.models import (
    MAX_DATABASE_ID,
    ChannelPreset,
    CommsPreset,
    DiscordWebhook,
    FleetOpsSettings,
    FleetType,
    MessageTemplate,
    PingRole,
    PingTarget,
    StatisticsTarget,
    validate_discord_webhook_url,
)
from fleetops.providers.srp import get_registered_srp_providers
from fleetops.services.links import extract_ingame_channel_id, extract_link_text, strip_url_userinfo
from fleetops.services.periods import MONTH_NAMES, current_year_month

SRP_AUTO = "auto"
SRP_AUTO_LABEL = "Automatic (aasrp, then Alliance Auth SRP)"
SRP_PROVIDER_LABELS = {"aasrp": "aasrp", "allianceauth_builtin": "Alliance Auth SRP"}
# Stored keys that earlier versions used for a provider that is now registered under another key.
SRP_PROVIDER_ALIASES = {"aa_srp": "aasrp"}
_SRP_PROVIDER_ORDER = ("aasrp", "allianceauth_builtin")

_ROLE_MENTION_RE = re.compile(r"<@&(\d+)>")


def _srp_provider_installed(provider):
    try:
        return bool(provider.available())
    except Exception:
        return False


def srp_provider_choices(current=""):
    """Automatic, every registered provider marked installed or not, and an unknown stored value."""
    providers = get_registered_srp_providers()
    order = {key: index for index, key in enumerate(_SRP_PROVIDER_ORDER)}
    choices = [(SRP_AUTO, SRP_AUTO_LABEL)]
    for key in sorted(providers, key=lambda key: (order.get(key, len(order)), key)):
        provider = providers[key]
        label = getattr(provider, "label", "") or SRP_PROVIDER_LABELS.get(key, key)
        state = "installed" if _srp_provider_installed(provider) else "not installed"
        choices.append((key, f"{label} ({state})"))
    current = (current or "").strip()
    if current and current != SRP_AUTO and current not in providers:
        alias = SRP_PROVIDER_ALIASES.get(current)
        note = f"old name of {alias}" if alias in providers else "unknown provider"
        choices.append((current, f"{current} ({note})"))
    return choices


class FleetOpsSettingsForm(forms.ModelForm):
    # Field groups of the settings page, in order; fields of no group are listed under "General".
    GROUPS = [
        (
            "PAP rules",
            "Apply to fleets that start after a change; running and closed fleets keep their rules.",
            ["multiplier_mode", "pap_minimum_minutes", "attendance_limit"],
        ),
        (
            "Statistics",
            "Corporation targets and the member minimum are set under Monthly Targets.",
            ["minimum_fleet_minutes", "history_alliance_ids", "leaderboard_size", "leaderboard_visibility"],
        ),
        ("PAP links", "", ["pap_link_minutes"]),
        (
            "Fleet tab",
            "The live fleet view and its composition.",
            ["fleet_live_refresh_seconds", "bridge_excluded_group_ids"],
        ),
    ]

    srp_provider = forms.ChoiceField(
        label="SRP provider",
        help_text="The SRP app that gets an SRP link when a fleet starts and SRP auto create is on.",
    )

    class Meta:
        model = FleetOpsSettings
        fields = [
            "multiplier_mode",
            "pap_minimum_minutes",
            "attendance_limit",
            "minimum_fleet_minutes",
            "leaderboard_size",
            "leaderboard_visibility",
            "pap_link_minutes",
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
            "fleet_live_refresh_seconds",
            "bridge_excluded_group_ids",
        ]
        widgets = {
            "auto_end_enabled": forms.CheckboxInput(attrs={"class": "form-check-input"}),
            "incentive_enabled": forms.CheckboxInput(attrs={"class": "form-check-input"}),
            "srp_auto_create": forms.CheckboxInput(attrs={"class": "form-check-input"}),
        }
        help_texts = {
            "multiplier_mode": "FC chooses: when ending a fleet that ran long enough for more than 1x, the FC "
            "picks the multiplier; fleets that end automatically get 1x until the FC chooses. Automatic: "
            "the highest multiplier the fleet's duration allows. Configure the multipliers under "
            "PAP multiplier tiers.",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["srp_provider"].choices = srp_provider_choices(self.instance.srp_provider)
        bridge = self.fields.get("bridge_excluded_group_ids")
        if bridge is not None:
            # Imported here: the fleet services import the models this module's forms are built on.
            from fleetops.services.composition_config import describe_group_ids

            current = describe_group_ids(self.instance.bridge_excluded_group_ids)
            if current:
                bridge.help_text = f"{bridge.help_text} Now: {current}."
        for name, field in self.fields.items():
            if isinstance(field.widget, forms.Select):
                field.widget.attrs["class"] = "form-select"
            elif not isinstance(field.widget, forms.CheckboxInput):
                field.widget.attrs["class"] = "form-control"

    def field_groups(self):
        """``(title, description, bound fields)`` for each group of the settings page."""
        grouped = {name for _title, _text, names in self.GROUPS for name in names}
        groups = [
            (title, text, [self[name] for name in names if name in self.fields]) for title, text, names in self.GROUPS
        ]
        rest = [self[name] for name in self.fields if name not in grouped]
        return [("General", "", rest), *groups]


class FleetTypeForm(forms.ModelForm):
    class Meta:
        model = FleetType
        fields = ["name", "short_name", "point_weight", "is_active", "sort_order"]


class StatisticsTargetForm(forms.ModelForm):
    """A monthly target; it applies from its month (EVE time) until the next entry."""

    month = forms.TypedChoiceField(
        label="Month",
        coerce=int,
        choices=[(number, name) for number, name in enumerate(MONTH_NAMES, start=1)],
    )

    class Meta:
        model = StatisticsTarget
        fields = ["year", "month", "corporation_average_target", "member_minimum_pap", "note"]
        labels = {"year": "Year"}
        help_texts = {
            "year": "The target applies from the first day of this month (EVE time) until the next entry.",
        }
        error_messages = {NON_FIELD_ERRORS: {"unique_together": "A target for this month already exists."}}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk is None:
            # A new entry starts with the month that is running now in EVE time.
            year, month = current_year_month()
            if self.initial.get("year") is None:
                self.initial["year"] = year
            if self.initial.get("month") is None:
                self.initial["month"] = month


class CommsPresetForm(forms.ModelForm):
    new_join_link = forms.BooleanField(
        label="Issue a new join link",
        required=False,
        help_text="The current join link stops working. Use it when the link was shared where it should not be.",
    )

    class Meta:
        model = CommsPreset
        fields = ["name", "channel_name", "voice_url", "description", "is_active"]
        widgets = {"description": forms.Textarea(attrs={"rows": 3})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Shown to the admin after saving.
        self.warnings = []
        # Facts the audit entry cannot read from the model fields.
        self.audit_extra = {}
        if self.instance.pk is None:
            del self.fields["new_join_link"]

    def clean_voice_url(self):
        value = (self.cleaned_data.get("voice_url") or "").strip()
        clean, user = strip_url_userinfo(value)
        if clean == value:
            return value
        # The join page shows the link to everybody, so personal log-in details must not stay in it.
        has_password = urlsplit(value).password is not None
        if user and has_password:
            self.warnings.append(f"The personal user name '{user}' and the password were removed from the voice link.")
        elif user:
            self.warnings.append(f"The personal user name '{user}' was removed from the voice link.")
        else:
            self.warnings.append("The personal log-in details were removed from the voice link.")
        return clean

    def save(self, commit=True):
        if self.cleaned_data.get("new_join_link"):
            self.instance.join_token = uuid.uuid4()
            self.audit_extra["join_link_reissued"] = True
        return super().save(commit)


class ChannelPresetForm(forms.ModelForm):
    ingame_link = forms.CharField(
        label="In-game link",
        required=False,
        widget=forms.Textarea(attrs={"rows": 2, "autocomplete": "off"}),
        help_text="Paste the channel link copied in game, or joinChannel:player_…. It replaces the in-game "
        "channel ID and fills an empty channel name.",
    )

    class Meta:
        model = ChannelPreset
        fields = ["name", "channel_type", "ingame_link", "channel_value", "ingame_channel_id", "is_active"]
        labels = {"ingame_channel_id": "In-game channel ID"}
        help_texts = {
            "channel_value": "Channel name shown in the MOTD. Left empty, it is taken from the pasted in-game link.",
            "ingame_channel_id": "The player_… ID that makes the channel name a link in the MOTD. "
            "Clear it to remove the link.",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # A pasted link can supply the channel name; clean() still requires one.
        self.fields["channel_value"].required = False

    def clean(self):
        cleaned = super().clean()
        link = (cleaned.get("ingame_link") or "").strip()
        if link:
            channel_id = extract_ingame_channel_id(link)
            if not channel_id:
                self.add_error("ingame_link", "No joinChannel:player_… link found.")
            else:
                cleaned["ingame_channel_id"] = channel_id
                if not (cleaned.get("channel_value") or "").strip():
                    cleaned["channel_value"] = extract_link_text(link)
        if not (cleaned.get("channel_value") or "").strip() and "channel_value" not in self.errors:
            self.add_error(
                "channel_value", ValidationError(forms.Field.default_error_messages["required"], code="required")
            )
        return cleaned


class PingRoleChoiceField(forms.ModelMultipleChoiceField):
    def label_from_instance(self, role):
        notes = []
        if not role.is_active:
            notes.append("inactive")
        if role.missing_from_discord:
            notes.append("not on Discord")
        return f"{role} ({', '.join(notes)})" if notes else str(role)


class DiscordWebhookForm(forms.ModelForm):
    """A ping channel. The webhook URL carries its token, so it can be replaced but is never shown again."""

    webhook_url = forms.CharField(
        label="Webhook URL",
        widget=forms.PasswordInput(
            render_value=False,
            attrs={"autocomplete": "new-password", "placeholder": "https://discord.com/api/webhooks/..."},
        ),
    )

    class Meta:
        model = DiscordWebhook
        fields = ["name", "webhook_url", "is_active", "allow_everyone", "allow_here", "allowed_roles", "sort_order"]
        field_classes = {"allowed_roles": PingRoleChoiceField}
        labels = {
            "name": "Channel name",
            "allow_everyone": "Allow @everyone",
            "allow_here": "Allow @here",
            "allowed_roles": "Allowed roles",
        }
        help_texts = {
            "name": "Shown to FCs when they choose where the ping goes.",
            "allowed_roles": "Leave empty to allow every active role.",
        }
        widgets = {"allowed_roles": forms.CheckboxSelectMultiple}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Facts the audit entry cannot read from the model fields.
        self.audit_extra = {}
        if self.instance.pk is not None:
            self.fields["webhook_url"].required = False
            self.fields["webhook_url"].help_text = "Leave empty to keep the stored URL."

    def clean_webhook_url(self):
        # Errors name the expected form only, never the value that was entered.
        value = (self.cleaned_data.get("webhook_url") or "").strip()
        if not value and self.instance.pk is not None:
            return self.instance.webhook_url
        validate_discord_webhook_url(value)
        if self.instance.pk is not None and value != self.instance.webhook_url:
            self.audit_extra["webhook_url_replaced"] = True
        return value


class PingRoleForm(forms.ModelForm):
    role_id = forms.CharField(
        label="Role ID",
        max_length=40,
        help_text="In Discord: Server Settings → Roles → ⋯ → Copy Role ID (needs Developer Mode). "
        "A role mention such as <@&123…> works too.",
        widget=forms.TextInput(attrs={"inputmode": "numeric", "autocomplete": "off"}),
    )

    class Meta:
        model = PingRole
        fields = ["name", "role_id", "is_active", "sort_order"]

    def clean_name(self):
        name = (self.cleaned_data.get("name") or "").strip().lstrip("@").strip()
        if not name:
            raise ValidationError(forms.Field.default_error_messages["required"], code="required")
        return name

    def clean_role_id(self):
        value = (self.cleaned_data.get("role_id") or "").strip()
        match = _ROLE_MENTION_RE.fullmatch(value)
        if match:
            value = match.group(1)
        if not (value.isascii() and value.isdigit()) or not 0 < int(value) <= MAX_DATABASE_ID:
            raise ValidationError("Enter the numeric Discord role ID.", code="invalid")
        return int(value)


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
    StatisticsTargetForm,
    CommsPresetForm,
    ChannelPresetForm,
    DiscordWebhookForm,
    PingRoleForm,
    PingTargetForm,
    MessageTemplateForm,
):
    _original_init = _form_class.__init__

    def _styled_init(self, *args, __original_init=_original_init, **kwargs):
        __original_init(self, *args, **kwargs)
        for field in self.fields.values():
            if isinstance(field.widget, (forms.CheckboxInput, forms.CheckboxSelectMultiple)):
                field.widget.attrs["class"] = "form-check-input"
            elif isinstance(field.widget, forms.Select):
                field.widget.attrs["class"] = "form-select"
            else:
                existing = field.widget.attrs.get("class", "")
                field.widget.attrs["class"] = (existing + " form-control").strip()

    _form_class.__init__ = _styled_init
