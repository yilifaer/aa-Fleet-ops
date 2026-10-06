import uuid
from decimal import Decimal

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models


class FleetOpsSettings(models.Model):
    attendance_limit = models.PositiveIntegerField(
        null=True,
        blank=True,
        validators=[MinValueValidator(1)],
        help_text="Maximum attendance credits per Alliance Auth user per fleet. Empty = unlimited.",
    )
    tracking_interval = models.PositiveIntegerField(default=60, validators=[MinValueValidator(30)])
    stale_threshold = models.PositiveIntegerField(default=180, validators=[MinValueValidator(60)])
    auto_end_enabled = models.BooleanField(default=True)
    auto_end_missing_count = models.PositiveIntegerField(default=3, validators=[MinValueValidator(2)])
    incentive_enabled = models.BooleanField(default=False)
    incentive_minimum_fleets = models.PositiveIntegerField(default=3, validators=[MinValueValidator(1)])
    data_retention_days = models.PositiveIntegerField(
        default=365,
        validators=[MinValueValidator(365)],
        help_text="Attendance/event history retention in days. Minimum 365 days.",
    )
    history_alliance_ids = models.CharField(
        max_length=500,
        blank=True,
        help_text="Comma-separated alliance IDs whose current members may appear in attendance history. Empty disables membership pruning.",
    )
    srp_auto_create = models.BooleanField(
        default=True,
        help_text="Automatically create/link an SRP fleet when a supported SRP provider is available.",
    )
    srp_provider = models.CharField(
        max_length=50,
        default="auto",
        help_text="SRP provider key. 'auto' prefers Alliance Auth built-in SRP and allows future provider plugins.",
    )

    class Meta:
        verbose_name = "FleetOps setting"
        verbose_name_plural = "FleetOps settings"
        permissions = [
            ("basic_access", "Can access FleetOps"),
            ("start_fleet", "Can start fleets"),
            ("manage_own_fleet", "Can manage own fleets"),
            ("view_all_fleets", "Can view all fleet records"),
            ("create_manual_fleet", "Can create manual fleet records"),
            ("manage_fleets", "Can manage all fleets"),
            ("view_corp_stats", "Can view own corporation statistics"),
            ("view_all_stats", "Can view all FleetOps statistics"),
            ("manage_attendance", "Can manage attendance"),
            ("manage_incentives", "Can manage FC incentives"),
            ("manage_configuration", "Can manage FleetOps configuration"),
            ("view_audit_log", "Can view FleetOps audit log"),
        ]

    def __str__(self):
        return "FleetOps Settings"

    @classmethod
    def get_solo(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj


class FleetType(models.Model):
    name = models.CharField(max_length=100, unique=True)
    short_name = models.CharField(max_length=30, blank=True)
    point_weight = models.DecimalField(max_digits=8, decimal_places=2, default=Decimal("0"))
    is_active = models.BooleanField(default=True)
    sort_order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["sort_order", "name"]

    def __str__(self):
        return self.short_name or self.name


class CommsPreset(models.Model):
    name = models.CharField(max_length=100, unique=True)
    channel_name = models.CharField(max_length=255, blank=True)
    voice_url = models.CharField(max_length=500, blank=True, help_text="Supports mumble://, https:// and other voice links.")
    description = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class ChannelPreset(models.Model):
    class ChannelType(models.TextChoices):
        LOGI = "logi", "Logi"
        BOOST = "boost", "Boost"

    name = models.CharField(max_length=100)
    channel_type = models.CharField(max_length=20, choices=ChannelType.choices)
    channel_value = models.CharField(max_length=255)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["channel_type", "name"]
        constraints = [
            models.UniqueConstraint(fields=["channel_type", "name"], name="fleetops_unique_channel_preset")
        ]

    def __str__(self):
        return f"{self.get_channel_type_display()}: {self.name}"


class DiscordWebhook(models.Model):
    name = models.CharField(max_length=100, unique=True)
    webhook_url = models.TextField()
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class PingTarget(models.Model):
    name = models.CharField(max_length=100, unique=True)
    target_value = models.CharField(max_length=255, blank=True)
    webhook = models.ForeignKey(DiscordWebhook, null=True, blank=True, on_delete=models.SET_NULL)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class MessageTemplate(models.Model):
    class TemplateType(models.TextChoices):
        PING = "ping", "Ping"
        MOTD = "motd", "MOTD"

    name = models.CharField(max_length=100)
    template_type = models.CharField(max_length=20, choices=TemplateType.choices)
    content = models.TextField()
    is_default = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["template_type", "name"]
        constraints = [
            models.UniqueConstraint(fields=["template_type", "name"], name="fleetops_unique_message_template")
        ]

    def __str__(self):
        return f"{self.get_template_type_display()}: {self.name}"


class FleetOperation(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        STARTING = "starting", "Starting"
        ACTIVE = "active", "Active"
        ENDING = "ending", "Ending"
        CLOSED = "closed", "Closed"
        CANCELLED = "cancelled", "Cancelled"
        ERROR = "error", "Error"

    uuid = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    start_request_id = models.UUIDField(null=True, blank=True, unique=True, editable=False)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT, db_index=True)

    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="fleetops_created_operations")
    fc_user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="fleetops_fc_operations")
    fc_character_id = models.BigIntegerField(db_index=True)
    fc_character_name = models.CharField(max_length=255)
    fc_main_character_id = models.BigIntegerField(null=True, blank=True, db_index=True)
    fc_main_character_name = models.CharField(max_length=255, blank=True)
    fleet_boss_character_id = models.BigIntegerField(null=True, blank=True)
    esi_fleet_id = models.BigIntegerField(null=True, blank=True, db_index=True)

    fleet_type = models.ForeignKey(FleetType, on_delete=models.PROTECT)
    fleet_point_weight_snapshot = models.DecimalField(max_digits=8, decimal_places=2, default=Decimal("0"))
    doctrine_name = models.CharField(max_length=255, blank=True)
    doctrine_external_id = models.CharField(max_length=255, blank=True)
    doctrine_source = models.CharField(max_length=50, blank=True)
    formup = models.CharField(max_length=255)

    comms = models.ForeignKey(CommsPreset, null=True, blank=True, on_delete=models.SET_NULL)
    logi_channel = models.ForeignKey(ChannelPreset, null=True, blank=True, related_name="fleetops_logi_operations", on_delete=models.SET_NULL)
    boost_channel = models.ForeignKey(ChannelPreset, null=True, blank=True, related_name="fleetops_boost_operations", on_delete=models.SET_NULL)
    ping_target = models.ForeignKey(PingTarget, null=True, blank=True, on_delete=models.SET_NULL)
    ping_template = models.ForeignKey(MessageTemplate, null=True, blank=True, related_name="fleetops_ping_operations", on_delete=models.SET_NULL)
    motd_template = models.ForeignKey(MessageTemplate, null=True, blank=True, related_name="fleetops_motd_operations", on_delete=models.SET_NULL)

    additional_message = models.TextField(blank=True)
    ping_text = models.TextField(blank=True)
    motd_text = models.TextField(blank=True)
    send_ping = models.BooleanField(
        default=True,
        help_text="If false, the operation tracks attendance without sending a Discord ping.",
    )
    attendance_multiplier = models.PositiveIntegerField(
        default=1,
        validators=[MinValueValidator(1), MaxValueValidator(3)],
        help_text="Fleet-wide multiplier applied to automatically granted attendance (1x, 2x or 3x).",
    )
    is_manual = models.BooleanField(
        default=False,
        help_text="Manual fleet record created without ESI fleet detection/tracking.",
    )

    srp_provider = models.CharField(max_length=50, blank=True)
    srp_reference = models.CharField(max_length=255, blank=True)
    srp_url = models.CharField(max_length=1000, blank=True)
    srp_error = models.TextField(blank=True)

    scheduled_at = models.DateTimeField(null=True, blank=True)
    started_at = models.DateTimeField(null=True, blank=True, db_index=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    last_esi_update = models.DateTimeField(null=True, blank=True)
    tracking_enabled = models.BooleanField(default=False)
    fleet_missing_count = models.PositiveIntegerField(default=0)
    last_error = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-started_at", "-created_at"]
        indexes = [models.Index(fields=["status", "tracking_enabled"], name="fleetops_fl_status_8c6d24_idx")]

    def __str__(self):
        return f"{self.fleet_type} — {self.fc_character_name} ({self.uuid})"


class OperationAction(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        SUCCESS = "success", "Success"
        FAILED = "failed", "Failed"
        SKIPPED = "skipped", "Skipped"

    operation = models.ForeignKey(FleetOperation, on_delete=models.CASCADE, related_name="actions")
    action = models.CharField(max_length=50)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    error_message = models.TextField(blank=True)
    attempts = models.PositiveIntegerField(default=1)
    executed_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["executed_at"]
        constraints = [models.UniqueConstraint(fields=["operation", "action"], name="fleetops_unique_operation_action")]

    def __str__(self):
        return f"{self.operation_id}: {self.action} {self.status}"


class FleetMemberState(models.Model):
    operation = models.ForeignKey(FleetOperation, on_delete=models.CASCADE, related_name="member_states")
    character_id = models.BigIntegerField()
    character_name = models.CharField(max_length=255)
    main_character_id = models.BigIntegerField(null=True, blank=True, db_index=True)
    main_character_name = models.CharField(max_length=255, blank=True)
    auth_user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="fleetops_member_states")
    corporation_id = models.BigIntegerField(null=True, blank=True, db_index=True)
    corporation_name = models.CharField(max_length=255, blank=True)
    alliance_id = models.BigIntegerField(null=True, blank=True)
    alliance_name = models.CharField(max_length=255, blank=True)
    ship_type_id = models.BigIntegerField(null=True, blank=True)
    ship_type_name = models.CharField(max_length=255, blank=True)
    solar_system_id = models.BigIntegerField(null=True, blank=True, db_index=True)
    solar_system_name = models.CharField(max_length=255, blank=True)
    fleet_role = models.CharField(max_length=50, blank=True)
    wing_id = models.BigIntegerField(null=True, blank=True)
    squad_id = models.BigIntegerField(null=True, blank=True)
    first_seen = models.DateTimeField()
    last_seen = models.DateTimeField()
    left_at = models.DateTimeField(null=True, blank=True)
    is_active = models.BooleanField(default=True, db_index=True)

    class Meta:
        ordering = ["character_name"]
        constraints = [models.UniqueConstraint(fields=["operation", "character_id"], name="fleetops_unique_operation_character_state")]

    def __str__(self):
        return f"{self.character_name} @ {self.operation_id}"


class FleetMemberEvent(models.Model):
    class EventType(models.TextChoices):
        JOIN = "join", "Join"
        LEAVE = "leave", "Leave"
        REJOIN = "rejoin", "Rejoin"
        SHIP_CHANGE = "ship_change", "Ship change"
        SYSTEM_CHANGE = "system_change", "System change"
        ROLE_CHANGE = "role_change", "Role change"
        WING_CHANGE = "wing_change", "Wing change"
        SQUAD_CHANGE = "squad_change", "Squad change"

    operation = models.ForeignKey(FleetOperation, on_delete=models.CASCADE, related_name="member_events")
    character_id = models.BigIntegerField(db_index=True)
    character_name = models.CharField(max_length=255)
    event_type = models.CharField(max_length=30, choices=EventType.choices, db_index=True)
    old_value = models.CharField(max_length=255, blank=True)
    new_value = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]


class AttendanceRecord(models.Model):
    class Source(models.TextChoices):
        AUTOMATIC = "automatic", "Automatic"
        MANUAL = "manual", "Manual"
        IMPORTED = "imported", "Imported"

    operation = models.ForeignKey(FleetOperation, on_delete=models.CASCADE, related_name="attendance_records")
    character_id = models.BigIntegerField()
    character_name = models.CharField(max_length=255)
    main_character_id = models.BigIntegerField(null=True, blank=True, db_index=True)
    main_character_name = models.CharField(max_length=255, blank=True)
    auth_user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="fleetops_attendance")
    corporation_id = models.BigIntegerField(null=True, blank=True, db_index=True, help_text="Main character corporation at attendance time.")
    corporation_name = models.CharField(max_length=255, blank=True)
    source = models.CharField(max_length=20, choices=Source.choices)
    attendance_value = models.PositiveIntegerField(default=1)
    granted = models.BooleanField(default=True, db_index=True)
    capped = models.BooleanField(default=False)
    first_seen = models.DateTimeField(null=True, blank=True)
    last_seen = models.DateTimeField(null=True, blank=True)
    notes = models.TextField(blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="fleetops_attendance_created")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["character_name", "created_at"]
        indexes = [
            models.Index(fields=["operation", "character_id", "source"], name="fleetops_att_op_char_src_idx"),
            models.Index(fields=["auth_user", "created_at"], name="fleetops_att_user_date_idx"),
        ]


class OperationRoleAssignment(models.Model):
    class Role(models.TextChoices):
        BACKSEAT_FC = "backseat_fc", "Back Seat FC"
        LOGI_ANCHOR = "logi_anchor", "Logi Anchor"
        SNOWFLAKE = "snowflake", "Snowflake Member"

    operation = models.ForeignKey(
        FleetOperation, on_delete=models.CASCADE, related_name="role_assignments"
    )
    role = models.CharField(max_length=30, choices=Role.choices, db_index=True)
    character_id = models.BigIntegerField(db_index=True)
    character_name = models.CharField(max_length=255)
    main_character_id = models.BigIntegerField(null=True, blank=True, db_index=True)
    main_character_name = models.CharField(max_length=255, blank=True)
    auth_user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="fleetops_role_assignments"
    )
    corporation_id = models.BigIntegerField(null=True, blank=True)
    corporation_name = models.CharField(max_length=255, blank=True)
    grants_fc_credit = models.BooleanField(
        default=False,
        help_text="Snapshot: this assignment counts as an FC fleet/points credit for the assigned user.",
    )
    notes = models.TextField(blank=True)
    assigned_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="fleetops_roles_assigned"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["role", "character_name"]
        constraints = [
            models.UniqueConstraint(
                fields=["operation", "role", "character_id"],
                name="fleetops_unique_operation_role_character",
            )
        ]

    def __str__(self):
        return f"{self.get_role_display()}: {self.character_name}"


class IncentivePeriod(models.Model):
    class Status(models.TextChoices):
        OPEN = "open", "Open"
        REVIEW = "review", "Review"
        FINALIZED = "finalized", "Finalized"

    year = models.PositiveIntegerField()
    month = models.PositiveIntegerField(validators=[MinValueValidator(1), MaxValueValidator(12)])
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.OPEN)
    budget = models.BigIntegerField(default=0, validators=[MinValueValidator(0)])
    minimum_fleets = models.PositiveIntegerField(default=3, validators=[MinValueValidator(1)])
    policy_snapshot = models.JSONField(default=dict, blank=True)
    finalized_at = models.DateTimeField(null=True, blank=True)
    finalized_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="fleetops_finalized_periods")

    class Meta:
        ordering = ["-year", "-month"]
        constraints = [models.UniqueConstraint(fields=["year", "month"], name="fleetops_unique_incentive_period")]

    def __str__(self):
        return f"{self.year}-{self.month:02d}"


class MonthlyFCStatistic(models.Model):
    period = models.ForeignKey(IncentivePeriod, on_delete=models.CASCADE, related_name="fc_statistics")
    fc_user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="fleetops_monthly_fc_stats")
    fleet_count = models.PositiveIntegerField(default=0)
    total_points = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0"))
    fleet_type_breakdown = models.JSONField(default=dict, blank=True)
    eligible = models.BooleanField(default=False)
    waived = models.BooleanField(default=False)
    calculated_payout = models.BigIntegerField(default=0)
    final_payout = models.BigIntegerField(default=0)

    class Meta:
        ordering = ["-total_points", "fc_user__username"]
        constraints = [models.UniqueConstraint(fields=["period", "fc_user"], name="fleetops_unique_monthly_fc_stat")]


class AuditLog(models.Model):
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="fleetops_audit_entries")
    action = models.CharField(max_length=100, db_index=True)
    object_type = models.CharField(max_length=100)
    object_id = models.CharField(max_length=100)
    old_value = models.JSONField(null=True, blank=True)
    new_value = models.JSONField(null=True, blank=True)
    reason = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.created_at}: {self.action} {self.object_type}:{self.object_id}"
