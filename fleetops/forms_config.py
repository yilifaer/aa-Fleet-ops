"""Forms of the FleetOps configuration pages."""

import re
import uuid
from urllib.parse import urlsplit

from django import forms
from django.core.exceptions import ValidationError

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
)
from fleetops.providers.srp import get_registered_srp_providers
from fleetops.services.links import extract_ingame_channel_id, extract_link_text, strip_url_userinfo

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
    srp_provider = forms.ChoiceField(
        label="SRP provider",
        help_text="The SRP app that gets an SRP link when a fleet starts and SRP auto create is on.",
    )

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
        self.fields["srp_provider"].choices = srp_provider_choices(self.instance.srp_provider)
        for name, field in self.fields.items():
            if isinstance(field.widget, forms.Select):
                field.widget.attrs["class"] = "form-select"
            elif not isinstance(field.widget, forms.CheckboxInput):
                field.widget.attrs["class"] = "form-control"


class FleetTypeForm(forms.ModelForm):
    class Meta:
        model = FleetType
        fields = ["name", "short_name", "point_weight", "is_active", "sort_order"]


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
    class Meta:
        model = DiscordWebhook
        fields = ["name", "webhook_url", "is_active", "allow_everyone", "allow_here", "allowed_roles", "sort_order"]
        field_classes = {"allowed_roles": PingRoleChoiceField}
        labels = {
            "name": "Channel name",
            "webhook_url": "Webhook URL",
            "allow_everyone": "Allow @everyone",
            "allow_here": "Allow @here",
            "allowed_roles": "Allowed roles",
        }
        help_texts = {
            "name": "Shown to FCs when they choose where the ping goes.",
            "allowed_roles": "Leave empty to allow every active role.",
        }
        widgets = {
            "webhook_url": forms.TextInput(
                attrs={
                    "autocomplete": "off",
                    "placeholder": "https://discord.com/api/webhooks/...",
                }
            ),
            "allowed_roles": forms.CheckboxSelectMultiple,
        }


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
