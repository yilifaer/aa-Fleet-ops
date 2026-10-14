# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Front-end configuration center."""

from dataclasses import dataclass

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import permission_required
from django.db import transaction
from django.db.models.deletion import ProtectedError
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy, pgettext_lazy
from django.views.decorators.http import require_POST

from fleetops import texts
from fleetops.forms_config import (
    ChannelPresetForm,
    CommsPresetForm,
    DiscordWebhookForm,
    FleetOpsSettingsForm,
    FleetTypeForm,
    MessageTemplateForm,
    PingRoleForm,
    SrpCorporationLimitForm,
    SrpPayoutRuleForm,
    SrpSettingsForm,
    StatisticsTargetForm,
)
from fleetops.forms_fleet import FleetStructureTemplateForm, ShipFunctionForm, ShipFunctionRuleForm
from fleetops.forms_pap import MultiplierTierForm
from fleetops.forms_records import SpecialRoleForm
from fleetops.models import (
    AuditLog,
    ChannelPreset,
    CommsPreset,
    DiscordWebhook,
    FleetOpsSettings,
    FleetStructureTemplate,
    FleetType,
    MessageTemplate,
    MultiplierTier,
    PingRole,
    ShipFunction,
    ShipFunctionRule,
    SpecialRole,
    SrpCorporationLimit,
    SrpPayoutRule,
    SrpSettings,
    StatisticsTarget,
)
from fleetops.services import discord_roles, links
from fleetops.services.audit import audit, cascaded_config_rows, config_snapshot
from fleetops.services.mentions import role_delete_refusal
from fleetops.srp import display as srp_display
from fleetops.validators import VOICE_URL_SCHEMES, voice_url_scheme
from fleetops.views import _render

UNSUPPORTED_VOICE_LINK = gettext_lazy("Not a supported voice link")
# The SRP settings page has the address of a list ("configuration/srp/"); it is not a list of rows.
SRP_SETTINGS_SECTION = "srp"

CONFIG_SECTIONS = {
    "fleet-types": {
        "title": gettext_lazy("Fleet Types"),
        "description": gettext_lazy("Fleet categories and the FC point weight attached to each type."),
        "model": FleetType,
        "form": FleetTypeForm,
        "columns": [
            (pgettext_lazy("FleetOps", "Name"), "name"),
            (gettext_lazy("Short name"), "short_name"),
            (gettext_lazy("Point weight"), "point_weight"),
            (pgettext_lazy("enabled", "Active"), "is_active"),
            (gettext_lazy("Sort"), "sort_order"),
        ],
    },
    "multiplier-tiers": {
        "title": gettext_lazy("PAP Multipliers"),
        "description": gettext_lazy(
            "Fleets running longer than a threshold can get a higher PAP multiplier. Shorter fleets get 1x."
        ),
        "model": MultiplierTier,
        "form": MultiplierTierForm,
        "columns": [
            (gettext_lazy("Longer than (minutes)"), "threshold_minutes"),
            (gettext_lazy("PAP multiplier"), "multiplier"),
        ],
    },
    "statistics-targets": {
        "title": gettext_lazy("Monthly Targets"),
        "description": gettext_lazy(
            "Corporation average PAP target and member minimum. Each entry applies from its month "
            "(EVE time) until the next entry."
        ),
        "model": StatisticsTarget,
        "form": StatisticsTargetForm,
        "columns": [
            (pgettext_lazy("effective from", "From"), "effective_from"),
            (gettext_lazy("Corporation average target"), "corporation_average_target"),
            (gettext_lazy("Member minimum PAP"), "member_minimum_pap"),
            (gettext_lazy("Note"), "note"),
        ],
    },
    "special-roles": {
        "title": gettext_lazy("Special Roles"),
        "description": gettext_lazy(
            "Roles FCs assign to pilots in a fleet, such as Back Seat FC, and whether a role gives FC credit."
        ),
        "model": SpecialRole,
        "form": SpecialRoleForm,
        "columns": [
            (pgettext_lazy("FleetOps", "Name"), "name"),
            (gettext_lazy("Gives FC credit"), "grants_fc_credit"),
            (pgettext_lazy("enabled", "Active"), "is_active"),
            (gettext_lazy("Sort"), "sort_order"),
        ],
    },
    "comms": {
        "title": gettext_lazy("Comms Presets"),
        "description": gettext_lazy("Reusable voice/comms choices shown to FCs when starting a fleet."),
        "model": CommsPreset,
        "form": CommsPresetForm,
        "columns": [
            (pgettext_lazy("FleetOps", "Name"), "name"),
            (gettext_lazy("Channel"), "channel_name"),
            (gettext_lazy("Voice link"), "voice_url"),
            (gettext_lazy("Join link"), "join_link"),
            (pgettext_lazy("enabled", "Active"), "is_active"),
        ],
        "link_hints": True,
    },
    "channels": {
        "title": gettext_lazy("Logi / Boost Channels"),
        "description": gettext_lazy("Reusable Logi and Boost channel choices shown on Fleet Start."),
        "model": ChannelPreset,
        "form": ChannelPresetForm,
        "columns": [
            (pgettext_lazy("FleetOps", "Name"), "name"),
            (pgettext_lazy("record type", "Type"), "channel_type"),
            (gettext_lazy("Channel name"), "channel_value"),
            (gettext_lazy("In-game channel ID"), "ingame_channel_id"),
            (pgettext_lazy("enabled", "Active"), "is_active"),
        ],
    },
    "ping-channels": {
        "title": gettext_lazy("Ping Channels"),
        "description": gettext_lazy(
            "Discord channels FCs can send pings to, and the mentions each channel allows. Webhook URLs can be "
            "replaced but are never shown again."
        ),
        "model": DiscordWebhook,
        "form": DiscordWebhookForm,
        "columns": [
            (pgettext_lazy("FleetOps", "Name"), "name"),
            (gettext_lazy("@everyone"), "allow_everyone"),
            (gettext_lazy("@here"), "allow_here"),
            (gettext_lazy("Allowed roles"), "allowed_roles"),
            (pgettext_lazy("enabled", "Active"), "is_active"),
            (gettext_lazy("Sort"), "sort_order"),
        ],
    },
    "ping-roles": {
        "title": gettext_lazy("Ping Roles"),
        "description": gettext_lazy(
            "Discord roles FCs can mention in a ping. Only active roles are offered on Fleet Start."
        ),
        "model": PingRole,
        "form": PingRoleForm,
        "columns": [
            (pgettext_lazy("FleetOps", "Name"), "name"),
            (gettext_lazy("Role ID"), "role_id"),
            (gettext_lazy("Discord"), "discord_state"),
            (pgettext_lazy("enabled", "Active"), "is_active"),
            (gettext_lazy("Sort"), "sort_order"),
        ],
        "actions": [
            {
                "url_name": "fleetops:sync_discord_roles",
                "label": gettext_lazy("Sync roles from Discord"),
                "status": lambda: discord_roles.discord_service_status(),
            }
        ],
    },
    "templates": {
        "title": gettext_lazy("Message Templates"),
        "description": gettext_lazy(
            "Ping and Fleet MOTD templates. Template variables are rendered when the fleet is prepared."
        ),
        "model": MessageTemplate,
        "form": MessageTemplateForm,
        "columns": [
            (pgettext_lazy("FleetOps", "Name"), "name"),
            (pgettext_lazy("record type", "Type"), "template_type"),
            (gettext_lazy("Default"), "is_default"),
            (pgettext_lazy("enabled", "Active"), "is_active"),
        ],
    },
    "ship-functions": {
        "title": gettext_lazy("Ship Functions"),
        "description": gettext_lazy(
            "Groups of the fleet composition, such as DPS or Logistics, and the fleet positions that count in "
            "them."
        ),
        "model": ShipFunction,
        "form": ShipFunctionForm,
        "columns": [
            (pgettext_lazy("FleetOps", "Name"), "name"),
            (gettext_lazy("Colour"), "color"),
            (gettext_lazy("Fleet positions"), "fleet_roles"),
            (gettext_lazy("Default"), "is_default"),
            (pgettext_lazy("enabled", "Active"), "is_active"),
            (gettext_lazy("Sort"), "sort_order"),
        ],
        "default_reason": texts.AUDIT_DEFAULT_FUNCTION_REPLACED,
    },
    "ship-function-rules": {
        "title": gettext_lazy("Ship Function Rules"),
        "description": gettext_lazy(
            "Which ship types and ship groups count under which ship function in the fleet composition."
        ),
        "model": ShipFunctionRule,
        "form": ShipFunctionRuleForm,
        "columns": [
            (gettext_lazy("Function"), "function"),
            (gettext_lazy("Kind"), "kind"),
            (gettext_lazy("ID"), "target_id"),
            (pgettext_lazy("FleetOps", "Name"), "label"),
        ],
    },
    "structure-templates": {
        "title": gettext_lazy("Fleet Structure Templates"),
        "description": gettext_lazy("Wing and squad names FCs can apply to a running fleet from its Fleet tab."),
        "model": FleetStructureTemplate,
        "form": FleetStructureTemplateForm,
        "columns": [
            (pgettext_lazy("FleetOps", "Name"), "name"),
            (gettext_lazy("Layout"), "layout"),
            (pgettext_lazy("FleetOps", "Description"), "description"),
            (pgettext_lazy("enabled", "Active"), "is_active"),
            (gettext_lazy("Sort"), "sort_order"),
        ],
    },
    "srp-rules": {
        "title": gettext_lazy("SRP Payout Table"),
        "description": gettext_lazy(
            "Fixed SRP amounts per ship type and/or fleet doctrine. The most specific active rule wins; "
            "ships without a rule get value × percentage."
        ),
        "model": SrpPayoutRule,
        "form": SrpPayoutRuleForm,
        "columns": [
            (gettext_lazy("Ship"), "ship"),
            (pgettext_lazy("FleetOps", "Doctrine"), "doctrine"),
            (gettext_lazy("Scope"), "scope"),
            (gettext_lazy("Amount"), "amount"),
            (pgettext_lazy("enabled", "Active"), "is_active"),
        ],
        "default_reason": texts.AUDIT_DEFAULT_PAYOUT_RULE_REPLACED,
    },
    "srp-corporation-limits": {
        "title": gettext_lazy("SRP Corporation Limits"),
        "description": gettext_lazy(
            "A monthly SRP cap for one corporation, replacing the default corporation cap of the SRP "
            "settings. An empty cap means no limit for that corporation."
        ),
        "model": SrpCorporationLimit,
        "form": SrpCorporationLimitForm,
        "columns": [
            (pgettext_lazy("FleetOps", "Corporation"), "corporation"),
            (gettext_lazy("Monthly cap"), "monthly_cap"),
            (gettext_lazy("Note"), "note"),
        ],
    },
}


@dataclass(frozen=True)
class CopyableText:
    """A list cell shown in a read-only input with a Copy button."""

    text: str
    copyable = True

    def __str__(self):
        return self.text


def _configuration_section(section):
    config = CONFIG_SECTIONS.get(section)
    if config is None:
        raise Http404
    return config


def _link_settings():
    """What the configuration pages say about the settings the public comms join links need."""
    return {
        "public_views_enabled": "fleetops" in (getattr(settings, "APPS_WITH_PUBLIC_VIEWS", None) or ()),
        "site_url_configured": links.site_url_configured(),
    }


def _cell_value(obj, field_name):
    if field_name == "join_link":
        url = links.comms_join_url(obj) if obj.is_active else ""
        if url:
            return CopyableText(url)
        # Earlier versions stored any text, e.g. a bare server address that pings and MOTDs cannot link.
        if obj.voice_url.strip() and voice_url_scheme(obj.voice_url) not in VOICE_URL_SCHEMES:
            return UNSUPPORTED_VOICE_LINK
        return ""
    if field_name == "discord_state":
        if obj.missing_from_discord:
            return _("Not on Discord")
        return _("Synced") if obj.source == PingRole.Source.DISCORD else _("Added manually")
    if isinstance(obj, SrpPayoutRule):
        # The ship and the doctrine are shown by name; an ID or a key is only the fallback.
        if field_name == "ship":
            ship = _("Ship %(id)s") % {"id": obj.ship_type_id} if obj.ship_type_id else _("Any ship")
            return obj.ship_type_name or ship
        if field_name == "doctrine":
            return obj.doctrine_name or obj.doctrine_key or _("Any doctrine")
        if field_name == "amount":
            return srp_display.format_isk(obj.amount)
    if isinstance(obj, SrpCorporationLimit):
        if field_name == "corporation":
            return obj.corporation_name or str(obj.corporation_id)
        if field_name == "monthly_cap":
            return srp_display.format_isk(obj.monthly_cap) if obj.monthly_cap is not None else _("No limit")
    value = getattr(obj, field_name)
    if field_name == "allowed_roles":
        return ", ".join(str(role) for role in value.all()) or _("Every active role")
    display = getattr(obj, f"get_{field_name}_display", None)
    if field_name in ("channel_type", "template_type", "kind", "scope") and display is not None:
        return display()
    return value


def _section_actions(config):
    actions = []
    for action in config.get("actions", ()):
        enabled, status = action["status"]()
        actions.append(
            {"url": reverse(action["url_name"]), "label": action["label"], "enabled": enabled, "status": status}
        )
    return actions


def _delete_refusal(obj):
    """Why an item must not be deleted, or ""."""
    if isinstance(obj, PingRole):
        return role_delete_refusal(obj)
    return ""


@permission_required("fleetops.manage_configuration", raise_exception=True)
def configuration_index(request):
    settings_obj = FleetOpsSettings.get_solo()
    cards = [
        {
            "key": key,
            "title": config["title"],
            "description": config["description"],
            "count": config["model"].objects.count(),
        }
        for key, config in CONFIG_SECTIONS.items()
    ]
    return _render(
        request,
        "fleetops/configuration/index.html",
        {"settings_obj": settings_obj, "srp_settings": SrpSettings.get_solo(), "cards": cards, **_link_settings()},
    )


@permission_required("fleetops.manage_configuration", raise_exception=True)
def configuration_settings(request):
    obj = FleetOpsSettings.get_solo()
    old = config_snapshot(obj)
    if request.method == "POST":
        form = FleetOpsSettingsForm(request.POST, instance=obj)
        if form.is_valid():
            obj = form.save()
            audit(
                request.user,
                "configuration.update",
                obj,
                old,
                config_snapshot(obj),
                reason=texts.AUDIT_CONFIG_UPDATED(),
            )
            messages.success(request, _("FleetOps settings updated."))
            return redirect("fleetops:configuration_index")
    else:
        form = FleetOpsSettingsForm(instance=obj)
    return _render(
        request,
        "fleetops/configuration/settings_form.html",
        {"form": form, "settings_obj": obj},
    )


@permission_required("fleetops.manage_configuration", raise_exception=True)
def configuration_srp(request):
    """The FleetOps SRP settings (``configuration/srp/``); every change is audited with the old and new values."""
    obj = SrpSettings.get_solo()
    old = config_snapshot(obj)
    if request.method == "POST":
        form = SrpSettingsForm(request.POST, instance=obj)
        if form.is_valid():
            obj = form.save()
            audit(
                request.user,
                "configuration.update",
                obj,
                old,
                config_snapshot(obj),
                reason=texts.AUDIT_CONFIG_UPDATED(),
            )
            messages.success(request, _("SRP settings updated."))
            return redirect("fleetops:configuration_index")
    else:
        form = SrpSettingsForm(instance=obj)
    fleetops_settings = FleetOpsSettings.get_solo()
    return _render(
        request,
        "fleetops/configuration/srp_settings.html",
        {
            "form": form,
            "settings_obj": obj,
            "fleetops_settings": fleetops_settings,
            # Fleets get their SRP link from another SRP app, so FleetOps SRP is not offered at their start.
            "provider_elsewhere": fleetops_settings.srp_provider.strip() not in ("auto", "fleetops", ""),
        },
    )


@permission_required("fleetops.manage_configuration", raise_exception=True)
def configuration_list(request, section):
    if section == SRP_SETTINGS_SECTION:
        return configuration_srp(request)
    config = _configuration_section(section)
    queryset = config["model"].objects.all()
    if config["model"] is DiscordWebhook:
        queryset = queryset.prefetch_related("allowed_roles")
    rows = [
        {"object": obj, "values": [(label, _cell_value(obj, field_name)) for label, field_name in config["columns"]]}
        for obj in queryset
    ]
    context = {"section": section, "config": config, "rows": rows, "actions": _section_actions(config)}
    if config.get("link_hints"):
        context.update(_link_settings())
    return _render(request, "fleetops/configuration/list.html", context)


@permission_required("fleetops.manage_configuration", raise_exception=True)
def configuration_edit(request, section, pk=None):
    config = _configuration_section(section)
    model = config["model"]
    obj = get_object_or_404(model, pk=pk) if pk is not None else None
    old = config_snapshot(obj) if obj is not None else None
    if request.method == "POST":
        form = config["form"](request.POST, instance=obj)
        if form.is_valid():
            saved = form.save()
            audit(
                request.user,
                "configuration.update" if obj is not None else "configuration.create",
                saved,
                old,
                {**config_snapshot(saved), **getattr(form, "audit_extra", {})},
                reason=texts.AUDIT_CONFIG_CHANGED(),
            )
            # A new default template takes the flag from the previous one.
            for demoted in getattr(saved, "demoted_defaults", ()):
                new = config_snapshot(demoted)
                audit(
                    request.user,
                    "configuration.update",
                    demoted,
                    {**new, "is_default": True},
                    new,
                    reason=config.get("default_reason", texts.AUDIT_DEFAULT_TEMPLATE_REPLACED)(),
                )
            messages.success(request, _("%(title)s saved.") % {"title": config["title"]})
            for warning in getattr(form, "warnings", ()):
                messages.warning(request, warning)
            return redirect("fleetops:configuration_list", section=section)
    else:
        form = config["form"](instance=obj)
    return _render(
        request,
        "fleetops/configuration/form.html",
        {"section": section, "config": config, "form": form, "object": obj},
    )


@permission_required("fleetops.manage_configuration", raise_exception=True)
@require_POST
def configuration_delete(request, section, pk):
    config = _configuration_section(section)
    obj = get_object_or_404(config["model"], pk=pk)
    refusal = _delete_refusal(obj)
    if refusal:
        messages.error(request, refusal)
        return redirect("fleetops:configuration_list", section=section)
    # The parent first, then the rows deleted with it (a ship function's rules).
    deleted = [
        (row.__class__.__name__, str(row.pk), config_snapshot(row)) for row in [obj, *cascaded_config_rows(obj)]
    ]
    try:
        with transaction.atomic():
            obj.delete()
            for object_type, object_id, old in deleted:
                AuditLog.objects.create(
                    actor=request.user,
                    action="configuration.delete",
                    object_type=object_type,
                    object_id=object_id,
                    old_value=old,
                    new_value=None,
                    reason=texts.AUDIT_CONFIG_DELETED(),
                )
    except ProtectedError:
        messages.error(
            request,
            _("This item is referenced by existing fleet data and cannot be deleted. Disable it instead."),
        )
    else:
        messages.warning(request, _("%(title)s item deleted.") % {"title": config["title"]})
    return redirect("fleetops:configuration_list", section=section)


@permission_required("fleetops.manage_configuration", raise_exception=True)
@require_POST
def sync_discord_roles(request):
    result = discord_roles.sync_discord_roles(actor=request.user)
    if result.ok:
        messages.success(request, texts.translate_stored(result.message))
    else:
        messages.error(request, texts.translate_stored(result.message))
    return redirect("fleetops:configuration_list", section="ping-roles")
