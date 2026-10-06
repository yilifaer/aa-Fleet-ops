from django.contrib import admin
from django.db import transaction

from fleetops.models import (
    AttendanceRecord,
    AuditLog,
    ChannelPreset,
    CommsPreset,
    DiscordWebhook,
    FleetMemberEvent,
    FleetMemberState,
    FleetOperation,
    FleetOpsSettings,
    FleetType,
    IncentivePeriod,
    MessageTemplate,
    MonthlyFCStatistic,
    OperationAction,
    OperationRoleAssignment,
    PingTarget,
)
from fleetops.services.audit import audit


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
        audit(
            request.user,
            "configuration.update" if change else "configuration.create",
            obj,
            old,
            _audit_snapshot(obj),
        )

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
    list_display = ("name", "channel_type", "channel_value", "is_active")
    list_filter = ("channel_type", "is_active")


@admin.register(DiscordWebhook)
class DiscordWebhookAdmin(FleetOpsConfigAuditMixin, admin.ModelAdmin):
    list_display = ("name", "is_active")


@admin.register(PingTarget)
class PingTargetAdmin(FleetOpsConfigAuditMixin, admin.ModelAdmin):
    list_display = ("name", "target_value", "webhook", "is_active")


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
admin.site.register(OperationRoleAssignment)
admin.site.register(FleetMemberState)
admin.site.register(FleetMemberEvent)
admin.site.register(AttendanceRecord)
admin.site.register(IncentivePeriod)
admin.site.register(MonthlyFCStatistic)
admin.site.register(AuditLog)
