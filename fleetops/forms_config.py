# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Forms of the FleetOps configuration pages."""

import re
import uuid
from dataclasses import dataclass
from urllib.parse import urlsplit

from allianceauth.eveonline.models import EveCorporationInfo
from allianceauth.services.hooks import get_extension_logger
from django import forms
from django.core.exceptions import NON_FIELD_ERRORS, ValidationError
from django.utils import translation
from django.utils.dates import MONTHS
from django.utils.text import slugify
from django.utils.translation import gettext_lazy as _
from django.utils.translation import pgettext_lazy

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
    SrpCorporationLimit,
    SrpPayoutRule,
    SrpSettings,
    StatisticsTarget,
    validate_discord_webhook_url,
)
from fleetops.providers import esi as esi_provider
from fleetops.providers.doctrines import get_doctrines
from fleetops.providers.esi import FleetESIError
from fleetops.providers.srp import get_registered_srp_providers
from fleetops.services import sde as sde_service
from fleetops.services.links import extract_ingame_channel_id, extract_link_text, strip_url_userinfo
from fleetops.services.names import entity_names
from fleetops.services.periods import current_year_month

logger = get_extension_logger(__name__)

SRP_AUTO = "auto"
SRP_AUTO_LABEL = _("Automatic (FleetOps SRP when enabled, then aa-srp, then Alliance Auth SRP)")
SRP_PROVIDER_LABELS = {"fleetops": "FleetOps SRP", "aasrp": "aasrp", "allianceauth_builtin": "Alliance Auth SRP"}
# Stored keys that earlier versions used for a provider that is now registered under another key.
SRP_PROVIDER_ALIASES = {"aa_srp": "aasrp"}
_SRP_PROVIDER_ORDER = ("fleetops", "aasrp", "allianceauth_builtin")
# How a provider's state reads next to its name, unless the provider has its own words.
SRP_PROVIDER_STATES = (_("installed"), _("not installed"))

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
        states = getattr(provider, "state_labels", None) or SRP_PROVIDER_STATES
        state = states[0] if _srp_provider_installed(provider) else states[1]
        choices.append((key, _("%(label)s (%(state)s)") % {"label": label, "state": state}))
    current = (current or "").strip()
    if current and current != SRP_AUTO and current not in providers:
        alias = SRP_PROVIDER_ALIASES.get(current)
        note = _("old name of %(alias)s") % {"alias": alias} if alias in providers else _("unknown provider")
        choices.append((current, _("%(label)s (%(state)s)") % {"label": current, "state": note}))
    return choices


class FieldGroup(tuple):
    """``(title, description, bound fields)`` of a settings page; ``key`` is the English slug of the title.

    The key is used for HTML ids and for the links a group shows, so they do not change with the language.
    """

    def __new__(cls, title, text, fields):
        group = super().__new__(cls, (title, text, fields))
        with translation.override("en"):
            group.key = slugify(str(title))
        return group


class FleetOpsSettingsForm(forms.ModelForm):
    # Field groups of the settings page, in order; fields of no group are listed under "General".
    GROUPS = [
        (
            _("PAP rules"),
            _("Apply to fleets that start after a change; running and closed fleets keep their rules."),
            ["multiplier_mode", "pap_minimum_minutes", "attendance_limit"],
        ),
        (
            _("Statistics"),
            _("Corporation targets and the member minimum are set under Monthly Targets."),
            ["minimum_fleet_minutes", "history_alliance_ids", "leaderboard_size", "leaderboard_visibility"],
        ),
        (_("PAP links"), "", ["pap_link_minutes"]),
        (
            _("Fleet tab"),
            _("The live fleet view and its composition."),
            ["fleet_live_refresh_seconds", "bridge_excluded_group_ids"],
        ),
    ]

    srp_provider = forms.ChoiceField(
        label=_("SRP provider"),
        help_text=_("The SRP app that gets an SRP link when a fleet starts and SRP auto create is on."),
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
            "multiplier_mode": _(
                "FC chooses: when ending a fleet that ran long enough for more than 1x, the FC "
                "picks the multiplier; fleets that end automatically get 1x until the FC chooses. Automatic: "
                "the highest multiplier the fleet's duration allows. Configure the multipliers under "
                "PAP multiplier tiers."
            ),
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
                bridge.help_text = _("%(help)s Now: %(current)s.") % {"help": bridge.help_text, "current": current}
        for name, field in self.fields.items():
            if isinstance(field.widget, forms.Select):
                field.widget.attrs["class"] = "form-select"
            elif not isinstance(field.widget, forms.CheckboxInput):
                field.widget.attrs["class"] = "form-control"

    def field_groups(self):
        """``(title, description, bound fields)`` for each group of the settings page."""
        grouped = {name for _title, _text, names in self.GROUPS for name in names}
        groups = [
            FieldGroup(title, text, [self[name] for name in names if name in self.fields])
            for title, text, names in self.GROUPS
        ]
        rest = [self[name] for name in self.fields if name not in grouped]
        return [FieldGroup(_("General"), "", rest), *groups]


class FleetTypeForm(forms.ModelForm):
    class Meta:
        model = FleetType
        fields = ["name", "short_name", "point_weight", "is_active", "sort_order"]


class StatisticsTargetForm(forms.ModelForm):
    """A monthly target; it applies from its month (EVE time) until the next entry."""

    month = forms.TypedChoiceField(
        label=_("Month"),
        coerce=int,
        choices=[(number, MONTHS[number]) for number in range(1, 13)],
    )

    class Meta:
        model = StatisticsTarget
        fields = ["year", "month", "corporation_average_target", "member_minimum_pap", "note"]
        labels = {"year": _("Year")}
        help_texts = {
            "year": _("The target applies from the first day of this month (EVE time) until the next entry."),
        }
        error_messages = {NON_FIELD_ERRORS: {"unique_together": _("A target for this month already exists.")}}

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
        label=_("Issue a new join link"),
        required=False,
        help_text=_("The current join link stops working. Use it when the link was shared where it should not be."),
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
            self.warnings.append(
                _("The personal user name '%(user)s' and the password were removed from the voice link.")
                % {"user": user}
            )
        elif user:
            self.warnings.append(_(
                "The personal user name '%(user)s' was removed from the voice link."
            ) % {"user": user})
        else:
            self.warnings.append(_("The personal log-in details were removed from the voice link."))
        return clean

    def save(self, commit=True):
        if self.cleaned_data.get("new_join_link"):
            self.instance.join_token = uuid.uuid4()
            self.audit_extra["join_link_reissued"] = True
        return super().save(commit)


class ChannelPresetForm(forms.ModelForm):
    ingame_link = forms.CharField(
        label=_("In-game link"),
        required=False,
        widget=forms.Textarea(attrs={"rows": 2, "autocomplete": "off"}),
        help_text=_(
            "Paste the channel link copied in game, or joinChannel:player_…. It replaces the in-game "
            "channel ID and fills an empty channel name."
        ),
    )

    class Meta:
        model = ChannelPreset
        fields = ["name", "channel_type", "ingame_link", "channel_value", "ingame_channel_id", "is_active"]
        labels = {"ingame_channel_id": _("In-game channel ID")}
        help_texts = {
            "channel_value": _("Channel name shown in the MOTD. Left empty, it is taken from the pasted in-game link."),
            "ingame_channel_id": _(
                "The player_… ID that makes the channel name a link in the MOTD. "
                "Clear it to remove the link."
            ),
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
                self.add_error("ingame_link", _("No joinChannel:player_… link found."))
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
        if not role.is_active and role.missing_from_discord:
            note = _("inactive, not on Discord")
        elif not role.is_active:
            note = _("inactive")
        elif role.missing_from_discord:
            note = _("not on Discord")
        else:
            return str(role)
        return _("%(role)s (%(note)s)") % {"role": role, "note": note}


class DiscordWebhookForm(forms.ModelForm):
    """A ping channel. The webhook URL carries its token, so it can be replaced but is never shown again."""

    webhook_url = forms.CharField(
        label=_("Webhook URL"),
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
            "name": _("Channel name"),
            "allow_everyone": _("Allow @everyone"),
            "allow_here": _("Allow @here"),
            "allowed_roles": _("Allowed roles"),
        }
        help_texts = {
            "name": _("Shown to FCs when they choose where the ping goes."),
            "allowed_roles": _("Leave empty to allow every active role."),
        }
        widgets = {"allowed_roles": forms.CheckboxSelectMultiple}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Facts the audit entry cannot read from the model fields.
        self.audit_extra = {}
        if self.instance.pk is not None:
            self.fields["webhook_url"].required = False
            self.fields["webhook_url"].help_text = _("Leave empty to keep the stored URL.")

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
        label=_("Role ID"),
        max_length=40,
        help_text=_(
            "In Discord: Server Settings → Roles → ⋯ → Copy Role ID (needs Developer Mode). "
            "A role mention such as <@&123…> works too."
        ),
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
            raise ValidationError(_("Enter the numeric Discord role ID."), code="invalid")
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


# --- FleetOps SRP ---------------------------------------------------------------------------------------


def _isk_field(**kwargs):
    """An ``IskField``; imported here because the SRP forms import ``fleetops.forms``, which imports this module."""
    from fleetops.forms_srp import IskField

    return IskField(**kwargs)


class SrpSettingsForm(forms.ModelForm):
    """The SRP settings page. Caps are typed the way players write ISK (``250m``, ``1.2b``); empty means no cap."""

    # Field groups of the page, in order.
    GROUPS = [
        (
            _("General"),
            _(
                "Members can only submit claims while FleetOps SRP is enabled. The SRP provider and the automatic "
                "link creation at fleet start are set in the general settings."
            ),
            ["enabled", "out_of_fleet_enabled", "claim_deadline_days"],
        ),
        (
            pgettext_lazy("SRP loss value", "Value"),
            _("Where the loss value comes from. It is only used for ships that are not in the payout table."),
            ["zkillboard_enabled", "value_source", "percentage"],
        ),
        (
            _("Payouts and caps"),
            _(
                "Ships in the payout table get their fixed amount; every other ship gets the percentage of its value. "
                "The per-loss caps apply to both."
            ),
            ["in_fleet_loss_cap", "out_of_fleet_loss_cap", "capsules_need_rule", "over_limit_action"],
        ),
        (
            _("Monthly limits"),
            _(
                "Approved SRP per EVE month of the loss. The most restrictive limit wins. Corporations can have their "
                "own limit."
            ),
            [
                "personal_monthly_cap",
                "personal_cap_includes_in_fleet",
                "corporation_monthly_cap",
                "corporation_cap_includes_in_fleet",
            ],
        ),
        (
            _("Members and review"),
            "",
            ["notify_members", "allow_self_review", "member_instructions"],
        ),
    ]
    ISK_FIELDS = ("in_fleet_loss_cap", "out_of_fleet_loss_cap", "personal_monthly_cap", "corporation_monthly_cap")

    class Meta:
        model = SrpSettings
        fields = [
            "enabled",
            "out_of_fleet_enabled",
            "zkillboard_enabled",
            "value_source",
            "percentage",
            "in_fleet_loss_cap",
            "out_of_fleet_loss_cap",
            "personal_monthly_cap",
            "personal_cap_includes_in_fleet",
            "corporation_monthly_cap",
            "corporation_cap_includes_in_fleet",
            "over_limit_action",
            "capsules_need_rule",
            "claim_deadline_days",
            "allow_self_review",
            "notify_members",
            "member_instructions",
        ]
        widgets = {
            "percentage": forms.NumberInput(attrs={"step": "0.01", "min": "0", "max": "100"}),
            "claim_deadline_days": forms.NumberInput(attrs={"min": "0", "max": "3650"}),
            "member_instructions": forms.Textarea(attrs={"rows": 4, "maxlength": "2000"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name in self.ISK_FIELDS:
            model_field = SrpSettings._meta.get_field(name)
            self.fields[name] = _isk_field(
                required=False, label=model_field.verbose_name, help_text=model_field.help_text
            )

    def field_groups(self):
        """``(title, description, bound fields)`` for each group of the page."""
        return [FieldGroup(title, text, [self[name] for name in names]) for title, text, names in self.GROUPS]


@dataclass(frozen=True)
class ShipLookup:
    """A ship type found for a payout rule; ``warning`` is set when its name could not be looked up."""

    type_id: int
    name: str
    warning: str = ""


NO_SHIP_FOUND = _("No ship called “%(name)s” was found. Use its exact English name or its type ID.")
SHIP_LOOKUP_FAILED = _("ESI could not look up the ship right now. Enter the ship type ID instead.")
SHIP_NAME_MISSING = _("The ship name could not be looked up, so the rule was saved with the ship type ID only.")
NOT_A_TYPE = _("This ID is not an item type.")
INVENTORY_TYPE = "inventory_type"


def _esi_type_name(type_id: int) -> str:
    """The English name of a type from ESI; "" when ESI does not know the ID. Raises FleetESIError."""
    category, name = esi_provider.resolve_names([type_id]).get(type_id, ("", ""))
    if category and category != INVENTORY_TYPE and name:
        raise ValidationError(NOT_A_TYPE, code="invalid")
    return name if category == INVENTORY_TYPE else ""


def _esi_type_by_name(name: str):
    """``(type_id, name)`` of the type with this exact name from ESI, or None."""
    try:
        found = esi_provider.universe_type_ids([name])
    except FleetESIError as exc:
        logger.warning("Looking up a ship name failed: %s", exc.code)
        raise ValidationError(SHIP_LOOKUP_FAILED, code="esi") from None
    return found.get(" ".join(name.split()).casefold())


def lookup_ship(text) -> ShipLookup:
    """The ship type a payout rule form names: a type ID or an exact English ship name.

    The Eve SDE app answers when it is installed (only ships by name); otherwise ESI does. An ID is accepted
    with a warning when ESI cannot be asked. Raises ``ValidationError`` for something that is no ship.
    """
    text = " ".join(str(text or "").split())
    if not text:
        return ShipLookup(0, "")
    if text.isascii() and text.isdigit():
        type_id = int(text)
        if not 0 < type_id <= MAX_DATABASE_ID:
            raise ValidationError(_("Enter a valid ship type ID."), code="invalid")
        name = sde_service.item_type_names([type_id]).get(type_id) or ""
        if not name:
            try:
                name = _esi_type_name(type_id)
            except FleetESIError:
                return ShipLookup(type_id, "", SHIP_NAME_MISSING)
            if not name:
                raise ValidationError(NOT_A_TYPE, code="invalid")
        return ShipLookup(type_id, name)
    if sde_service.sde_installed():
        found = sde_service.find_item_type(text)
    else:
        found = _esi_type_by_name(text)
    if not found:
        raise ValidationError(NO_SHIP_FOUND % {"name": text}, code="not_found")
    return ShipLookup(int(found[0]), found[1])


CUSTOM_DOCTRINE = "custom"
DOCTRINE_NEEDS_FLEET = _(
    "A doctrine rule only applies to losses in a fleet; choose “In fleet and out of fleet” "
    "or “In-fleet losses only”."
)


class SrpPayoutRuleForm(forms.ModelForm):
    """A row of the payout table: a fixed amount for a ship type, a doctrine, or a ship of a doctrine."""

    ship = forms.CharField(
        label=_("Ship"),
        required=False,
        max_length=100,
        help_text=_("Ship type ID or exact English name. Leave empty for a rule on a doctrine only."),
    )
    doctrine_choice = forms.ChoiceField(
        label=pgettext_lazy("FleetOps", "Doctrine"),
        required=False,
        help_text=_("Doctrine rules only apply to losses in a fleet that flew this doctrine."),
    )
    doctrine_custom = forms.CharField(
        label=_("Custom doctrine name"),
        required=False,
        max_length=255,
        help_text=_("For fleets with a typed doctrine name; matched ignoring case. Used with “Custom name”."),
    )

    class Meta:
        model = SrpPayoutRule
        fields = ["label", "scope", "amount", "is_active", "notes"]
        labels = {"amount": _("Amount (ISK)")}
        help_texts = {
            "label": _("Optional name shown in the list."),
            "amount": _("Paid as it is, before the per-loss cap and the monthly limits."),
            "notes": _("Optional remark for the other SRP managers."),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.warnings = []
        self.fields["amount"] = _isk_field(
            required=True, label=_("Amount (ISK)"), help_text=self.fields["amount"].help_text
        )
        choices = [("", _("Any doctrine")), (CUSTOM_DOCTRINE, _("Custom name"))]
        known = set()
        for doctrine in get_doctrines():
            value = f"{doctrine.source}|{doctrine.external_id}|{doctrine.name}"
            choices.append((value, doctrine.name))
            known.add(value)
        initial = self._initial_doctrine()
        if initial and initial not in known and initial != CUSTOM_DOCTRINE:
            choices.append((initial, _("%(name)s (no longer listed)") % {"name": initial.split("|", 2)[2]}))
        self.fields["doctrine_choice"].choices = choices
        if self.instance.pk is not None:
            self.initial["doctrine_choice"] = initial
            self.initial["doctrine_custom"] = self.instance.doctrine_name if initial == CUSTOM_DOCTRINE else ""
            self.initial["ship"] = self.instance.ship_type_name or (
                str(self.instance.ship_type_id) if self.instance.ship_type_id else ""
            )
        self.order_fields(["label", "ship", "doctrine_choice", "doctrine_custom", "scope", "amount", "is_active", "notes"])

    def _initial_doctrine(self) -> str:
        rule = self.instance
        if rule.pk is None:
            return ""
        if rule.doctrine_source and rule.doctrine_external_id:
            return f"{rule.doctrine_source}|{rule.doctrine_external_id}|{rule.doctrine_name}"
        return CUSTOM_DOCTRINE if rule.doctrine_name else ""

    def clean_ship(self):
        lookup = lookup_ship(self.cleaned_data.get("ship"))
        if lookup.warning:
            self.warnings.append(lookup.warning)
        return lookup

    def clean(self):
        cleaned = super().clean()
        lookup = cleaned.get("ship")
        choice = cleaned.get("doctrine_choice") or ""
        source = external_id = name = ""
        if choice == CUSTOM_DOCTRINE:
            name = " ".join((cleaned.get("doctrine_custom") or "").split())
            if not name:
                self.add_error("doctrine_custom", _("Enter the doctrine name."))
        elif choice:
            source, external_id, name = (choice.split("|", 2) + ["", ""])[:3]
        if lookup is not None:
            if choice and cleaned.get("scope") == SrpPayoutRule.Scope.OUT_OF_FLEET:
                self.add_error("scope", DOCTRINE_NEEDS_FLEET)
            # The rule is stored with what was resolved; doctrine_key follows from the doctrine fields. A rule
            # with neither a ship nor a doctrine is refused by the model's own check.
            self.instance.ship_type_id, self.instance.ship_type_name = lookup.type_id, lookup.name
            self.instance.doctrine_source = source
            self.instance.doctrine_external_id = external_id
            self.instance.doctrine_name = name
        return cleaned


class SrpCorporationLimitForm(forms.ModelForm):
    """The monthly cap of one corporation; it replaces the default corporation cap of the SRP settings."""

    class Meta:
        model = SrpCorporationLimit
        fields = ["corporation_id", "corporation_name", "monthly_cap", "note"]
        labels = {"corporation_id": _("Corporation ID"), "corporation_name": _("Corporation name")}
        help_texts = {
            "corporation_id": _("The corporation ID, for example from its zKillboard page."),
            "corporation_name": _("Left empty, it is looked up."),
        }
        error_messages = {"corporation_id": {"unique": _("This corporation already has a limit.")}}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.warnings = []
        self.fields["corporation_name"].required = False
        self.fields["monthly_cap"] = _isk_field(
            required=False,
            label=_("Monthly cap (ISK)"),
            help_text=_(
                "Maximum approved SRP of this corporation per EVE month. Empty means no limit for this "
                "corporation, whatever the default is."
            ),
        )

    def clean_corporation_id(self):
        value = self.cleaned_data["corporation_id"]
        if not 0 < value <= MAX_DATABASE_ID:
            raise ValidationError(_("Enter a valid corporation ID."), code="invalid")
        return value

    def clean(self):
        cleaned = super().clean()
        corporation_id = cleaned.get("corporation_id")
        if corporation_id and not (cleaned.get("corporation_name") or "").strip():
            name = corporation_name_of(corporation_id)
            if name:
                cleaned["corporation_name"] = name
            else:
                self.warnings.append(_(
                    "The corporation name could not be looked up; the limit was saved with the ID only."
                ))
        return cleaned


def corporation_name_of(corporation_id: int) -> str:
    """The name of a corporation: Alliance Auth's copy first, then the name cache and ESI; "" when unknown."""
    name = EveCorporationInfo.objects.filter(corporation_id=corporation_id).values_list(
        "corporation_name", flat=True
    ).first()
    return name or entity_names([corporation_id]).get(corporation_id, "")


for _form_class in (
    FleetTypeForm,
    StatisticsTargetForm,
    CommsPresetForm,
    ChannelPresetForm,
    DiscordWebhookForm,
    PingRoleForm,
    PingTargetForm,
    MessageTemplateForm,
    SrpSettingsForm,
    SrpPayoutRuleForm,
    SrpCorporationLimitForm,
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
