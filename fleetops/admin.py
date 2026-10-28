# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

from django.contrib import admin, messages
from django.contrib.admin.utils import unquote
from django.db import transaction
from django.shortcuts import redirect
from django.utils.translation import gettext_lazy as _

from fleetops.forms_config import DiscordWebhookForm
from fleetops.models import (
    AttendanceRecord,
    AuditLog,
    ChannelPreset,
    CharacterAffiliation,
    CommsPreset,
    DiscordWebhook,
    EveEntityName,
    FleetMemberEvent,
    FleetMemberState,
    FleetOperation,
    FleetOpsSettings,
    FleetStartPreset,
    FleetStructureTemplate,
    FleetType,
    GroupAccess,
    IncentivePeriod,
    MessageTemplate,
    MonthlyFCStatistic,
    MultiplierTier,
    OperationAction,
    OperationActionAttempt,
    OperationRoleAssignment,
    PapLink,
    PapRegistration,
    PingRole,
    PingTarget,
    ShipFunction,
    ShipFunctionRule,
    SpecialRole,
    SrpClaim,
    SrpClaimEvent,
    SrpCorporationLimit,
    SrpFleetLink,
    SrpKillmail,
    SrpPayoutRule,
    SrpSettings,
    StandalonePing,
    StatisticsTarget,
)
from fleetops.services.audit import audit, cascaded_config_rows
from fleetops.services.mentions import role_delete_refusal


def _audit_snapshot(obj):
    """Small JSON-safe snapshot for FleetOps configuration audit entries."""
    data = {}
    for field in obj._meta.concrete_fields:
        if field.primary_key:
            continue
        value = getattr(obj, field.attname, None)
        if isinstance(obj, DiscordWebhook) and field.name == "webhook_url":
            data[field.name] = "***redacted***" if value else ""
        elif value is None or isinstance(value, (str, int, float, bool)):
            data[field.name] = value
        else:
            data[field.name] = str(value)
    if obj.pk is not None:
        # Many-to-many fields as primary keys, such as the roles a ping channel allows.
        for field in obj._meta.many_to_many:
            data[field.name] = sorted(getattr(obj, field.name).values_list("pk", flat=True))
    return data


class FleetOpsConfigAuditMixin:
    def save_model(self, request, obj, form, change):
        old = None
        if change and obj.pk:
            try:
                old_obj = obj.__class__.objects.get(pk=obj.pk)
                old = _audit_snapshot(old_obj)
            except obj.__class__.DoesNotExist:
                pass
        super().save_model(request, obj, form, change)
        obj._fleetops_audit_old = old

    def save_related(self, request, form, formsets, change):
        # Many-to-many values are saved here, after save_model(), so the audit entry is written now.
        super().save_related(request, form, formsets, change)
        obj = form.instance
        audit(
            request.user,
            "configuration.update" if change else "configuration.create",
            obj,
            getattr(obj, "_fleetops_audit_old", None),
            _audit_snapshot(obj),
        )
        # A new default template takes the flag from the previous one.
        for demoted in getattr(obj, "demoted_defaults", ()):
            new = _audit_snapshot(demoted)
            audit(request.user, "configuration.update", demoted, {**new, "is_default": True}, new)

    def delete_model(self, request, obj):
        # The object first, then the rows deleted with it (a ship function's rules).
        deleted = [
            (row.__class__.__name__, str(row.pk), _audit_snapshot(row)) for row in [obj, *cascaded_config_rows(obj)]
        ]
        with transaction.atomic():
            super().delete_model(request, obj)
            for object_type, object_id, old in deleted:
                AuditLog.objects.create(
                    actor=request.user,
                    action="configuration.delete",
                    object_type=object_type,
                    object_id=object_id,
                    old_value=old,
                    new_value=None,
                )

    def delete_queryset(self, request, queryset):
        # The changelist "delete selected" action must leave the same audit trail as single deletes.
        with transaction.atomic():
            for obj in queryset:
                self.delete_model(request, obj)


class ShipFunctionRuleInline(admin.TabularInline):
    model = ShipFunctionRule
    extra = 0
    fields = ("kind", "target_id", "label")


@admin.register(ShipFunction)
class ShipFunctionAdmin(FleetOpsConfigAuditMixin, admin.ModelAdmin):
    list_display = ("name", "color", "fleet_roles", "is_default", "is_active", "sort_order")
    list_filter = ("is_default", "is_active")
    inlines = [ShipFunctionRuleInline]

    def save_formset(self, request, form, formset, change):
        # Each rule added, changed or removed in the inline gets its own audit entry.
        before = {}
        for inline_form in formset.forms:
            pk = inline_form.instance.pk
            stored = formset.model.objects.filter(pk=pk).first() if pk is not None else None
            if stored is not None:
                before[pk] = _audit_snapshot(stored)
        deleted = [
            (inline_form.instance.pk, inline_form.instance.__class__.__name__)
            for inline_form in formset.deleted_forms
            if inline_form.instance.pk in before
        ]
        super().save_formset(request, form, formset, change)
        for pk, object_type in deleted:
            AuditLog.objects.create(
                actor=request.user,
                action="configuration.delete",
                object_type=object_type,
                object_id=str(pk),
                old_value=before[pk],
                new_value=None,
            )
        for obj in formset.new_objects:
            audit(request.user, "configuration.create", obj, None, _audit_snapshot(obj))
        for obj, _changed in formset.changed_objects:
            audit(request.user, "configuration.update", obj, before.get(obj.pk), _audit_snapshot(obj))


@admin.register(ShipFunctionRule)
class ShipFunctionRuleAdmin(FleetOpsConfigAuditMixin, admin.ModelAdmin):
    list_display = ("function", "kind", "target_id", "label")
    list_filter = ("kind", "function")
    search_fields = ("label", "target_id")


@admin.register(FleetStructureTemplate)
class FleetStructureTemplateAdmin(FleetOpsConfigAuditMixin, admin.ModelAdmin):
    list_display = ("name", "description", "is_active", "sort_order")
    list_filter = ("is_active",)


class ReadOnlyAdmin(admin.ModelAdmin):
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(FleetOpsSettings)
class FleetOpsSettingsAdmin(FleetOpsConfigAuditMixin, admin.ModelAdmin):
    def has_add_permission(self, request):
        return not FleetOpsSettings.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(FleetType)
class FleetTypeAdmin(FleetOpsConfigAuditMixin, admin.ModelAdmin):
    list_display = ("name", "short_name", "point_weight", "is_active", "sort_order")
    list_editable = ("point_weight", "is_active", "sort_order")


@admin.register(MultiplierTier)
class MultiplierTierAdmin(FleetOpsConfigAuditMixin, admin.ModelAdmin):
    list_display = ("threshold_minutes", "multiplier")


@admin.register(SpecialRole)
class SpecialRoleAdmin(FleetOpsConfigAuditMixin, admin.ModelAdmin):
    list_display = ("name", "key", "grants_fc_credit", "is_active", "sort_order")
    list_filter = ("grants_fc_credit", "is_active")
    readonly_fields = ("key",)


@admin.register(StatisticsTarget)
class StatisticsTargetAdmin(FleetOpsConfigAuditMixin, admin.ModelAdmin):
    list_display = ("effective_from", "corporation_average_target", "member_minimum_pap", "note")


@admin.register(CommsPreset)
class CommsPresetAdmin(FleetOpsConfigAuditMixin, admin.ModelAdmin):
    list_display = ("name", "channel_name", "is_active")
    list_editable = ("is_active",)


@admin.register(ChannelPreset)
class ChannelPresetAdmin(FleetOpsConfigAuditMixin, admin.ModelAdmin):
    list_display = ("name", "channel_type", "channel_value", "ingame_channel_id", "is_active")
    list_filter = ("channel_type", "is_active")


@admin.register(PingRole)
class PingRoleAdmin(FleetOpsConfigAuditMixin, admin.ModelAdmin):
    list_display = ("name", "role_id", "is_active", "source", "missing_from_discord", "sort_order")
    list_filter = ("is_active", "source", "missing_from_discord")

    def has_delete_permission(self, request, obj=None):
        # As on the FleetOps configuration page: a channel that loses its allowed roles may ping every role.
        # Django checks this for every role of a single or "delete selected" delete.
        if obj is not None and role_delete_refusal(obj):
            return False
        return super().has_delete_permission(request, obj)

    def delete_view(self, request, object_id, extra_context=None):
        role = self.get_object(request, unquote(object_id))
        refusal = role_delete_refusal(role) if role is not None else ""
        if refusal:
            self.message_user(request, refusal, messages.ERROR)
            return redirect("admin:fleetops_pingrole_changelist")
        return super().delete_view(request, object_id, extra_context)

    def get_deleted_objects(self, objs, request):
        # The "delete selected" confirmation says why a role cannot be deleted.
        for role in objs:
            refusal = role_delete_refusal(role)
            if refusal:
                self.message_user(request, _("%(name)s: %(refusal)s") % {"name": role.name, "refusal": refusal}, messages.ERROR)
        return super().get_deleted_objects(objs, request)


@admin.register(DiscordWebhook)
class DiscordWebhookAdmin(FleetOpsConfigAuditMixin, admin.ModelAdmin):
    # The configuration page's form: the webhook URL can be replaced but is never shown.
    form = DiscordWebhookForm
    list_display = ("name", "is_active", "allow_everyone", "allow_here", "sort_order")
    filter_horizontal = ("allowed_roles",)
    # Who may use a channel is set on the FleetOps pages only, where every change is audited and checked.
    exclude = ("allowed_groups", "fcs_allowed")
    readonly_fields = ("group_access",)

    @admin.display(description=_("Group access"))
    def group_access(self, obj):
        if obj is None or obj.pk is None:
            return "-"
        groups = ", ".join(obj.allowed_groups.order_by("name").values_list("name", flat=True))
        # A channel without a group is open to every FC, whatever the stored switch says.
        if not groups:
            return _("Groups: none · Alliance FCs: yes. Change this in FleetOps Administration, Ping Channels.")
        if obj.fcs_allowed:
            text = _("Groups: %(groups)s · Alliance FCs: yes. Change this in FleetOps Administration, Ping Channels.")
        else:
            text = _("Groups: %(groups)s · Alliance FCs: no. Change this in FleetOps Administration, Ping Channels.")
        return text % {"groups": groups}

    def get_fields(self, request, obj=None):
        fields = super().get_fields(request, obj)
        if obj is None or self.has_change_permission(request, obj):
            return fields
        # Without the change permission Django shows every field's value as text; the URL holds the
        # webhook token, so only whether one is stored is shown.
        return [name if name != "webhook_url" else "webhook_url_stored" for name in fields]

    @admin.display(description=_("Webhook URL"))
    def webhook_url_stored(self, obj):
        return _("Stored (hidden)") if obj is not None and obj.webhook_url else _("Not set")


@admin.register(PingTarget)
class PingTargetAdmin(FleetOpsConfigAuditMixin, ReadOnlyAdmin):
    """Legacy ping targets; 0.2.0b1 converted them into ping channels and mentions."""

    list_display = ("name", "target_value", "webhook", "is_active")


@admin.register(EveEntityName)
class EveEntityNameAdmin(ReadOnlyAdmin):
    list_display = ("id", "name", "category", "updated_at")
    list_filter = ("category",)
    search_fields = ("name",)


@admin.register(CharacterAffiliation)
class CharacterAffiliationAdmin(ReadOnlyAdmin):
    list_display = ("character_id", "corporation_id", "alliance_id", "faction_id", "updated_at")


@admin.register(MessageTemplate)
class MessageTemplateAdmin(FleetOpsConfigAuditMixin, admin.ModelAdmin):
    list_display = ("name", "template_type", "is_default", "is_active")
    list_filter = ("template_type", "is_default", "is_active")


@admin.register(FleetOperation)
class FleetOperationAdmin(admin.ModelAdmin):
    list_display = ("uuid", "status", "fc_character_name", "fleet_type", "esi_fleet_id", "started_at", "ended_at")
    list_filter = ("status", "fleet_type", "tracking_enabled")
    search_fields = ("fc_character_name", "fc_main_character_name", "esi_fleet_id", "uuid")
    # Who manages a group fleet follows its group, so the group is not edited here.
    readonly_fields = ("uuid", "created_at", "updated_at", "group", "group_name")


@admin.register(PapLink)
class PapLinkAdmin(admin.ModelAdmin):
    list_display = ("pk", "operation", "created_by", "created_at", "expires_at", "require_online", "revoked_at")
    list_filter = ("require_online",)


@admin.register(PapRegistration)
class PapRegistrationAdmin(admin.ModelAdmin):
    list_display = ("character_name", "operation", "auth_user", "ship_type_name", "solar_system_name", "registered_at")
    search_fields = ("character_name",)


@admin.register(FleetStartPreset)
class FleetStartPresetAdmin(admin.ModelAdmin):
    list_display = ("name", "owner", "is_default", "is_last_used", "updated_at")
    list_filter = ("is_default", "is_last_used")


@admin.register(SrpSettings)
class SrpSettingsAdmin(FleetOpsConfigAuditMixin, admin.ModelAdmin):
    list_display = ("__str__", "enabled", "value_source", "percentage", "over_limit_action", "updated_at")

    def has_add_permission(self, request):
        return not SrpSettings.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(SrpPayoutRule)
class SrpPayoutRuleAdmin(FleetOpsConfigAuditMixin, admin.ModelAdmin):
    list_display = ("__str__", "scope", "amount", "is_active", "updated_at")
    list_filter = ("scope", "is_active")
    search_fields = ("ship_type_name", "doctrine_name", "label")


@admin.register(SrpCorporationLimit)
class SrpCorporationLimitAdmin(FleetOpsConfigAuditMixin, admin.ModelAdmin):
    list_display = ("corporation_name", "corporation_id", "monthly_cap", "note")
    search_fields = ("corporation_name", "corporation_id")


class SrpReadOnlyAdmin(ReadOnlyAdmin):
    """SRP records change only through the audited SRP actions, never in the admin."""

    def has_delete_permission(self, request, obj=None):
        return False


class RecordAdmin(ReadOnlyAdmin):
    """Records that change only through the audited FleetOps pages, never in the admin."""

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(GroupAccess)
class GroupAccessAdmin(RecordAdmin):
    """Group access is set on the FleetOps Administration pages, where every change is checked and audited."""

    list_display = ("group", "mode", "members_are_leaders", "srp_enabled", "is_active", "updated_at")
    list_filter = ("mode", "is_active")
    search_fields = ("group__name",)


@admin.register(StandalonePing)
class StandalonePingAdmin(RecordAdmin):
    """Pings without a fleet are records of what was sent; they are never edited."""

    list_display = ("created_at", "channel_name", "group_name", "sender_name", "status")
    list_filter = ("status",)
    search_fields = ("channel_name", "group_name", "sender_name")


@admin.register(SrpFleetLink)
class SrpFleetLinkAdmin(SrpReadOnlyAdmin):
    list_display = ("operation", "fc_main_character_name", "is_open", "created_at")
    list_filter = ("is_open",)
    search_fields = ("fc_main_character_name",)


@admin.register(SrpKillmail)
class SrpKillmailAdmin(SrpReadOnlyAdmin):
    list_display = ("killmail_id", "ship_type_name", "killmail_time", "zkill_status", "fetched_at")
    list_filter = ("zkill_status",)
    search_fields = ("killmail_id", "ship_type_name")


@admin.register(SrpClaim)
class SrpClaimAdmin(SrpReadOnlyAdmin):
    list_display = (
        "pk",
        "character_name",
        "main_character_name",
        "ship_type_name",
        "scope",
        "status",
        "approved_amount",
        "killmail_time",
    )
    list_filter = ("status", "scope", "over_limit")
    search_fields = ("character_name", "main_character_name", "corporation_name", "killmail__killmail_id")


@admin.register(SrpClaimEvent)
class SrpClaimEventAdmin(SrpReadOnlyAdmin):
    list_display = ("claim", "action", "actor", "amount", "created_at")
    list_filter = ("action", "internal")


admin.site.register(OperationAction)
admin.site.register(OperationActionAttempt)
admin.site.register(OperationRoleAssignment)
admin.site.register(FleetMemberState)
admin.site.register(FleetMemberEvent)
admin.site.register(AttendanceRecord)
admin.site.register(IncentivePeriod)
admin.site.register(MonthlyFCStatistic)
admin.site.register(AuditLog)
