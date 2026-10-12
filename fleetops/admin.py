from django.contrib import admin, messages
from django.contrib.admin.utils import unquote
from django.db import transaction
from django.shortcuts import redirect

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
    FleetType,
    IncentivePeriod,
    MessageTemplate,
    MonthlyFCStatistic,
    OperationAction,
    OperationActionAttempt,
    OperationRoleAssignment,
    PingRole,
    PingTarget,
)
from fleetops.services.audit import audit
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
        old = _audit_snapshot(obj)
        object_id = str(obj.pk)
        object_type = obj.__class__.__name__
        super().delete_model(request, obj)
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
                self.message_user(request, f"{role.name}: {refusal}", messages.ERROR)
        return super().get_deleted_objects(objs, request)


@admin.register(DiscordWebhook)
class DiscordWebhookAdmin(FleetOpsConfigAuditMixin, admin.ModelAdmin):
    list_display = ("name", "is_active", "allow_everyone", "allow_here", "sort_order")
    filter_horizontal = ("allowed_roles",)


@admin.register(PingTarget)
class PingTargetAdmin(FleetOpsConfigAuditMixin, ReadOnlyAdmin):
    """Legacy ping targets; 0.1.0a6 converted them into ping channels and mentions."""

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
    readonly_fields = ("uuid", "created_at", "updated_at")


admin.site.register(OperationAction)
admin.site.register(OperationActionAttempt)
admin.site.register(OperationRoleAssignment)
admin.site.register(FleetMemberState)
admin.site.register(FleetMemberEvent)
admin.site.register(AttendanceRecord)
admin.site.register(IncentivePeriod)
admin.site.register(MonthlyFCStatistic)
admin.site.register(AuditLog)
