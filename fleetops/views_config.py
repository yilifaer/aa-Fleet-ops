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
from django.views.decorators.http import require_POST

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

UNSUPPORTED_VOICE_LINK = "Not a supported voice link"
# The SRP settings page has the address of a list ("configuration/srp/"); it is not a list of rows.
SRP_SETTINGS_SECTION = "srp"

CONFIG_SECTIONS = {
    "fleet-types": {
        "title": "Fleet Types",
        "description": "Fleet categories and the FC point weight attached to each type.",
        "model": FleetType,
        "form": FleetTypeForm,
        "columns": [
            ("Name", "name"),
            ("Short name", "short_name"),
            ("Point weight", "point_weight"),
            ("Active", "is_active"),
            ("Sort", "sort_order"),
        ],
    },
    "multiplier-tiers": {
        "title": "PAP Multipliers",
        "description": "Fleets running longer than a threshold can get a higher PAP multiplier. Shorter fleets get 1x.",
        "model": MultiplierTier,
        "form": MultiplierTierForm,
        "columns": [
            ("Longer than (minutes)", "threshold_minutes"),
            ("PAP multiplier", "multiplier"),
        ],
    },
    "statistics-targets": {
        "title": "Monthly Targets",
        "description": "Corporation average PAP target and member minimum. Each entry applies from its month "
        "(EVE time) until the next entry.",
        "model": StatisticsTarget,
        "form": StatisticsTargetForm,
        "columns": [
            ("From", "effective_from"),
            ("Corporation average target", "corporation_average_target"),
            ("Member minimum PAP", "member_minimum_pap"),
            ("Note", "note"),
        ],
    },
    "special-roles": {
        "title": "Special Roles",
        "description": "Roles FCs assign to pilots in a fleet, such as Back Seat FC, and whether a role gives FC credit.",
        "model": SpecialRole,
        "form": SpecialRoleForm,
        "columns": [
            ("Name", "name"),
            ("Gives FC credit", "grants_fc_credit"),
            ("Active", "is_active"),
            ("Sort", "sort_order"),
        ],
    },
    "comms": {
        "title": "Comms Presets",
        "description": "Reusable voice/comms choices shown to FCs when starting a fleet.",
        "model": CommsPreset,
        "form": CommsPresetForm,
        "columns": [
            ("Name", "name"),
            ("Channel", "channel_name"),
            ("Voice link", "voice_url"),
            ("Join link", "join_link"),
            ("Active", "is_active"),
        ],
        "link_hints": True,
    },
    "channels": {
        "title": "Logi / Boost Channels",
        "description": "Reusable Logi and Boost channel choices shown on Fleet Start.",
        "model": ChannelPreset,
        "form": ChannelPresetForm,
        "columns": [
            ("Name", "name"),
            ("Type", "channel_type"),
            ("Channel name", "channel_value"),
            ("In-game channel ID", "ingame_channel_id"),
            ("Active", "is_active"),
        ],
    },
    "ping-channels": {
        "title": "Ping Channels",
        "description": "Discord channels FCs can send pings to, and the mentions each channel allows. Webhook URLs can be replaced but are never shown again.",
        "model": DiscordWebhook,
        "form": DiscordWebhookForm,
        "columns": [
            ("Name", "name"),
            ("@everyone", "allow_everyone"),
            ("@here", "allow_here"),
            ("Allowed roles", "allowed_roles"),
            ("Active", "is_active"),
            ("Sort", "sort_order"),
        ],
    },
    "ping-roles": {
        "title": "Ping Roles",
        "description": "Discord roles FCs can mention in a ping. Only active roles are offered on Fleet Start.",
        "model": PingRole,
        "form": PingRoleForm,
        "columns": [
            ("Name", "name"),
            ("Role ID", "role_id"),
            ("Discord", "discord_state"),
            ("Active", "is_active"),
            ("Sort", "sort_order"),
        ],
        "actions": [
            {
                "url_name": "fleetops:sync_discord_roles",
                "label": "Sync roles from Discord",
                "status": lambda: discord_roles.discord_service_status(),
            }
        ],
    },
    "templates": {
        "title": "Message Templates",
        "description": "Ping and Fleet MOTD templates. Template variables are rendered when the fleet is prepared.",
        "model": MessageTemplate,
        "form": MessageTemplateForm,
        "columns": [
            ("Name", "name"),
            ("Type", "template_type"),
            ("Default", "is_default"),
            ("Active", "is_active"),
        ],
    },
    "ship-functions": {
        "title": "Ship Functions",
        "description": "Groups of the fleet composition, such as DPS or Logistics, and the fleet positions that count in them.",
        "model": ShipFunction,
        "form": ShipFunctionForm,
        "columns": [
            ("Name", "name"),
            ("Colour", "color"),
            ("Fleet positions", "fleet_roles"),
            ("Default", "is_default"),
            ("Active", "is_active"),
            ("Sort", "sort_order"),
        ],
        "default_noun": "ship function",
    },
    "ship-function-rules": {
        "title": "Ship Function Rules",
        "description": "Which ship types and ship groups count under which ship function in the fleet composition.",
        "model": ShipFunctionRule,
        "form": ShipFunctionRuleForm,
        "columns": [
            ("Function", "function"),
            ("Kind", "kind"),
            ("ID", "target_id"),
            ("Name", "label"),
        ],
    },
    "structure-templates": {
        "title": "Fleet Structure Templates",
        "description": "Wing and squad names FCs can apply to a running fleet from its Fleet tab.",
        "model": FleetStructureTemplate,
        "form": FleetStructureTemplateForm,
        "columns": [
            ("Name", "name"),
            ("Layout", "layout"),
            ("Description", "description"),
            ("Active", "is_active"),
            ("Sort", "sort_order"),
        ],
    },
    "srp-rules": {
        "title": "SRP Payout Table",
        "description": "Fixed SRP amounts per ship type and/or fleet doctrine. The most specific active rule wins; "
        "ships without a rule get value × percentage.",
        "model": SrpPayoutRule,
        "form": SrpPayoutRuleForm,
        "columns": [
            ("Ship", "ship"),
            ("Doctrine", "doctrine"),
            ("Scope", "scope"),
            ("Amount", "amount"),
            ("Active", "is_active"),
        ],
        "default_noun": "payout rule",
    },
    "srp-corporation-limits": {
        "title": "SRP Corporation Limits",
        "description": "A monthly SRP cap for one corporation, replacing the default corporation cap of the SRP "
        "settings. An empty cap means no limit for that corporation.",
        "model": SrpCorporationLimit,
        "form": SrpCorporationLimitForm,
        "columns": [
            ("Corporation", "corporation"),
            ("Monthly cap", "monthly_cap"),
            ("Note", "note"),
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
            return "Not on Discord"
        return "Synced" if obj.source == PingRole.Source.DISCORD else "Added manually"
    if isinstance(obj, SrpPayoutRule):
        # The ship and the doctrine are shown by name; an ID or a key is only the fallback.
        if field_name == "ship":
            return obj.ship_type_name or (f"Ship {obj.ship_type_id}" if obj.ship_type_id else "Any ship")
        if field_name == "doctrine":
            return obj.doctrine_name or obj.doctrine_key or "Any doctrine"
        if field_name == "amount":
            return srp_display.format_isk(obj.amount)
    if isinstance(obj, SrpCorporationLimit):
        if field_name == "corporation":
            return obj.corporation_name or str(obj.corporation_id)
        if field_name == "monthly_cap":
            return srp_display.format_isk(obj.monthly_cap) if obj.monthly_cap is not None else "No limit"
    value = getattr(obj, field_name)
    if field_name == "allowed_roles":
        return ", ".join(str(role) for role in value.all()) or "Every active role"
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
                reason="Updated from FleetOps front-end administration",
            )
            messages.success(request, "FleetOps settings updated.")
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
                reason="Updated from FleetOps front-end administration",
            )
            messages.success(request, "SRP settings updated.")
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
                reason="Changed from FleetOps front-end administration",
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
                    reason=f"Default {config.get('default_noun', 'template')} replaced from FleetOps front-end administration",
                )
            messages.success(request, f"{config['title']} saved.")
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
                    reason="Deleted from FleetOps front-end administration",
                )
    except ProtectedError:
        messages.error(
            request,
            "This item is referenced by existing fleet data and cannot be deleted. Disable it instead.",
        )
    else:
        messages.warning(request, f"{config['title']} item deleted.")
    return redirect("fleetops:configuration_list", section=section)


@permission_required("fleetops.manage_configuration", raise_exception=True)
@require_POST
def sync_discord_roles(request):
    result = discord_roles.sync_discord_roles(actor=request.user)
    if result.ok:
        messages.success(request, result.message)
    else:
        messages.error(request, result.message)
    return redirect("fleetops:configuration_list", section="ping-roles")
