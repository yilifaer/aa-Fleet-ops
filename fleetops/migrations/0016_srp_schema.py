# AA FleetOps 0.2.0b1: FleetOps SRP: settings, payout rules, corporation limits, fleet SRP links, fetched
# killmails, claims and the claim history, the request_srp and manage_srp permissions and the new help text of
# the SRP provider setting. Schema only; 0017 fills the settings row, grants request_srp and adds the SRP line
# to the untouched default MOTD.

import django.core.validators
import django.db.models.deletion
import django.utils.timezone
import uuid
from decimal import Decimal
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("fleetops", "0015_statistics_targets"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="SrpCorporationLimit",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("corporation_id", models.BigIntegerField(unique=True)),
                ("corporation_name", models.CharField(blank=True, max_length=255)),
                (
                    "monthly_cap",
                    models.PositiveBigIntegerField(
                        blank=True,
                        help_text="Maximum approved SRP of this corporation per EVE month. Empty = no limit for "
                        "this corporation.",
                        null=True,
                        validators=[
                            django.core.validators.MinValueValidator(1),
                            django.core.validators.MaxValueValidator(1000000000000000),
                        ],
                    ),
                ),
                ("note", models.CharField(blank=True, max_length=255)),
            ],
            options={
                "verbose_name": "SRP corporation limit",
                "ordering": ["corporation_name", "corporation_id"],
            },
        ),
        migrations.CreateModel(
            name="SrpKillmail",
            fields=[
                (
                    "killmail_id",
                    models.BigIntegerField(primary_key=True, serialize=False),
                ),
                ("killmail_hash", models.CharField(max_length=40)),
                ("killmail_time", models.DateTimeField(db_index=True)),
                ("solar_system_id", models.BigIntegerField()),
                ("solar_system_name", models.CharField(blank=True, max_length=100)),
                (
                    "victim_character_id",
                    models.BigIntegerField(blank=True, db_index=True, null=True),
                ),
                (
                    "victim_corporation_id",
                    models.BigIntegerField(blank=True, null=True),
                ),
                ("victim_alliance_id", models.BigIntegerField(blank=True, null=True)),
                ("ship_type_id", models.BigIntegerField()),
                ("ship_type_name", models.CharField(blank=True, max_length=255)),
                ("attacker_count", models.PositiveIntegerField(default=0)),
                ("war_id", models.BigIntegerField(blank=True, null=True)),
                ("items", models.JSONField(blank=True, default=list)),
                ("fetched_at", models.DateTimeField(default=django.utils.timezone.now)),
                (
                    "zkill_status",
                    models.CharField(
                        choices=[
                            ("pending", "Waiting"),
                            ("ok", "Value available"),
                            ("missing", "Not on zKillboard yet"),
                            ("unavailable", "zKillboard value unavailable"),
                            ("disabled", "zKillboard disabled"),
                        ],
                        db_index=True,
                        default="pending",
                        max_length=12,
                    ),
                ),
                ("zkill_attempts", models.PositiveSmallIntegerField(default=0)),
                (
                    "zkill_next_attempt_at",
                    models.DateTimeField(blank=True, db_index=True, null=True),
                ),
                ("zkill_checked_at", models.DateTimeField(blank=True, null=True)),
                (
                    "zkill_fitted_value",
                    models.DecimalField(
                        blank=True, decimal_places=2, max_digits=20, null=True
                    ),
                ),
                (
                    "zkill_total_value",
                    models.DecimalField(
                        blank=True, decimal_places=2, max_digits=20, null=True
                    ),
                ),
                (
                    "zkill_destroyed_value",
                    models.DecimalField(
                        blank=True, decimal_places=2, max_digits=20, null=True
                    ),
                ),
                (
                    "zkill_dropped_value",
                    models.DecimalField(
                        blank=True, decimal_places=2, max_digits=20, null=True
                    ),
                ),
                ("zkill_npc", models.BooleanField(blank=True, null=True)),
                ("zkill_solo", models.BooleanField(blank=True, null=True)),
                ("zkill_awox", models.BooleanField(blank=True, null=True)),
                ("zkill_labels", models.JSONField(blank=True, default=list)),
                (
                    "esi_estimate",
                    models.DecimalField(
                        blank=True, decimal_places=2, max_digits=20, null=True
                    ),
                ),
                ("esi_estimate_at", models.DateTimeField(blank=True, null=True)),
            ],
            options={
                "verbose_name": "SRP killmail",
                "ordering": ["-killmail_time"],
            },
        ),
        migrations.CreateModel(
            name="SrpSettings",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "enabled",
                    models.BooleanField(
                        default=False,
                        help_text="Members can submit FleetOps SRP claims and fleets get FleetOps SRP links.",
                    ),
                ),
                (
                    "out_of_fleet_enabled",
                    models.BooleanField(
                        default=True,
                        help_text="Allow claims for losses outside FleetOps fleets.",
                        verbose_name="Out-of-fleet losses",
                    ),
                ),
                (
                    "zkillboard_enabled",
                    models.BooleanField(
                        default=True,
                        help_text="Use zKillboard for killmail hashes and values. Without it only in-game killmail "
                        "links work.",
                        verbose_name="Use zKillboard",
                    ),
                ),
                (
                    "value_source",
                    models.CharField(
                        choices=[
                            (
                                "zkill_fitted",
                                "zKillboard fitted value (ship, modules, drones, implants)",
                            ),
                            ("zkill_total", "zKillboard total value (includes cargo)"),
                            ("esi_estimate", "ESI market estimate"),
                        ],
                        default="zkill_fitted",
                        help_text="Where the loss value comes from. The ESI market estimate is also the fallback "
                        "when zKillboard has no value.",
                        max_length=20,
                    ),
                ),
                (
                    "percentage",
                    models.DecimalField(
                        decimal_places=2,
                        default=Decimal("0"),
                        help_text="Share of the loss value paid for ships not in the payout table. 0 = no "
                        "automatic amount.",
                        max_digits=5,
                        validators=[
                            django.core.validators.MinValueValidator(Decimal("0")),
                            django.core.validators.MaxValueValidator(Decimal("100")),
                        ],
                        verbose_name="Payout percentage",
                    ),
                ),
                (
                    "in_fleet_loss_cap",
                    models.PositiveBigIntegerField(
                        blank=True,
                        help_text="Maximum per in-fleet loss. Empty = no cap.",
                        null=True,
                        validators=[
                            django.core.validators.MinValueValidator(1),
                            django.core.validators.MaxValueValidator(1000000000000000),
                        ],
                        verbose_name="In-fleet loss cap",
                    ),
                ),
                (
                    "out_of_fleet_loss_cap",
                    models.PositiveBigIntegerField(
                        blank=True,
                        help_text="Maximum per out-of-fleet loss. Empty = no cap.",
                        null=True,
                        validators=[
                            django.core.validators.MinValueValidator(1),
                            django.core.validators.MaxValueValidator(1000000000000000),
                        ],
                        verbose_name="Out-of-fleet loss cap",
                    ),
                ),
                (
                    "personal_monthly_cap",
                    models.PositiveBigIntegerField(
                        blank=True,
                        help_text="Maximum approved SRP per player (all characters) per EVE month. Empty = no cap.",
                        null=True,
                        validators=[
                            django.core.validators.MinValueValidator(1),
                            django.core.validators.MaxValueValidator(1000000000000000),
                        ],
                        verbose_name="Personal monthly cap",
                    ),
                ),
                (
                    "personal_cap_includes_in_fleet",
                    models.BooleanField(
                        default=True,
                        verbose_name="In-fleet losses count toward the personal cap",
                    ),
                ),
                (
                    "corporation_monthly_cap",
                    models.PositiveBigIntegerField(
                        blank=True,
                        help_text="Maximum approved SRP per corporation and EVE month; the default for every "
                        "corporation. Corporation limits override it. Empty = no cap.",
                        null=True,
                        validators=[
                            django.core.validators.MinValueValidator(1),
                            django.core.validators.MaxValueValidator(1000000000000000),
                        ],
                        verbose_name="Corporation monthly cap",
                    ),
                ),
                (
                    "corporation_cap_includes_in_fleet",
                    models.BooleanField(
                        default=True,
                        verbose_name="In-fleet losses count toward the corporation cap",
                    ),
                ),
                (
                    "over_limit_action",
                    models.CharField(
                        choices=[
                            ("review", "Send to review with a warning"),
                            ("reject", "Reject automatically"),
                        ],
                        default="review",
                        help_text="What happens to a claim that does not fit into the remaining monthly limit. "
                        "When it only partly fits it is cut to the remaining amount and sent to review; "
                        "it is rejected automatically only when nothing is left.",
                        max_length=10,
                    ),
                ),
                (
                    "capsules_need_rule",
                    models.BooleanField(
                        default=True,
                        help_text="Capsule losses are only covered when the payout table lists the capsule (fixed "
                        "amount).",
                        verbose_name="Capsules need a payout rule",
                    ),
                ),
                (
                    "claim_deadline_days",
                    models.PositiveIntegerField(
                        default=0,
                        help_text="Losses older than this cannot be submitted. 0 = no deadline.",
                        validators=[django.core.validators.MaxValueValidator(3650)],
                        verbose_name="Claim deadline (days)",
                    ),
                ),
                (
                    "allow_self_review",
                    models.BooleanField(
                        default=False,
                        help_text="By default another SRP manager decides on a manager's own claims. Allow this "
                        "for small alliances.",
                        verbose_name="SRP managers may decide on their own claims",
                    ),
                ),
                (
                    "notify_members",
                    models.BooleanField(
                        default=True,
                        help_text="Send an Alliance Auth notification when a claim is decided or paid.",
                        verbose_name="Notify members",
                    ),
                ),
                (
                    "member_instructions",
                    models.TextField(
                        blank=True,
                        help_text="Shown to members on the SRP pages.",
                        validators=[django.core.validators.MaxLengthValidator(2000)],
                    ),
                ),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "verbose_name": "SRP setting",
                "verbose_name_plural": "SRP settings",
            },
        ),
        migrations.AlterModelOptions(
            name="fleetopssettings",
            options={
                "permissions": [
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
                    ("request_srp", "Can request SRP"),
                    ("manage_srp", "Can review and pay SRP claims"),
                ],
                "verbose_name": "FleetOps setting",
                "verbose_name_plural": "FleetOps settings",
            },
        ),
        migrations.AlterField(
            model_name="fleetopssettings",
            name="srp_provider",
            field=models.CharField(
                default="auto",
                help_text="SRP provider. 'auto' uses FleetOps SRP when it is enabled, then aa-srp, then Alliance "
                "Auth SRP.",
                max_length=50,
            ),
        ),
        migrations.CreateModel(
            name="SrpClaim",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "uuid",
                    models.UUIDField(default=uuid.uuid4, editable=False, unique=True),
                ),
                (
                    "scope",
                    models.CharField(
                        choices=[
                            ("in_fleet", "In fleet"),
                            ("out_of_fleet", "Out of fleet"),
                        ],
                        db_index=True,
                        max_length=12,
                    ),
                ),
                ("fleet_label", models.CharField(blank=True, max_length=255)),
                (
                    "fc_main_character_name",
                    models.CharField(blank=True, max_length=255),
                ),
                ("character_id", models.BigIntegerField(db_index=True)),
                ("character_name", models.CharField(max_length=255)),
                ("main_character_id", models.BigIntegerField(blank=True, null=True)),
                ("main_character_name", models.CharField(blank=True, max_length=255)),
                (
                    "corporation_id",
                    models.BigIntegerField(blank=True, db_index=True, null=True),
                ),
                ("corporation_name", models.CharField(blank=True, max_length=255)),
                ("corporation_ticker", models.CharField(blank=True, max_length=20)),
                ("alliance_id", models.BigIntegerField(blank=True, null=True)),
                ("alliance_name", models.CharField(blank=True, max_length=255)),
                ("killmail_time", models.DateTimeField(db_index=True)),
                ("ship_type_id", models.BigIntegerField(db_index=True)),
                ("ship_type_name", models.CharField(blank=True, max_length=255)),
                ("solar_system_name", models.CharField(blank=True, max_length=100)),
                (
                    "loss_value",
                    models.DecimalField(
                        blank=True, decimal_places=2, max_digits=20, null=True
                    ),
                ),
                ("value_source", models.CharField(blank=True, max_length=20)),
                (
                    "payout_basis",
                    models.CharField(
                        choices=[
                            ("rule", "Payout table"),
                            ("percentage", "Percentage of the value"),
                            ("none", "No automatic amount"),
                        ],
                        default="none",
                        max_length=12,
                    ),
                ),
                ("policy", models.JSONField(blank=True, default=dict)),
                ("calculation", models.JSONField(blank=True, default=dict)),
                ("base_amount", models.PositiveBigIntegerField(default=0)),
                ("suggested_amount", models.PositiveBigIntegerField(default=0)),
                ("limited_amount", models.PositiveBigIntegerField(default=0)),
                ("over_limit", models.BooleanField(db_index=True, default=False)),
                ("counts_personal", models.BooleanField(default=True)),
                ("counts_corporation", models.BooleanField(default=True)),
                (
                    "fleet_check",
                    models.CharField(blank=True, db_index=True, max_length=20),
                ),
                ("fleet_check_params", models.JSONField(blank=True, default=dict)),
                ("fleet_checked_at", models.DateTimeField(blank=True, null=True)),
                ("warnings", models.JSONField(blank=True, default=list)),
                ("member_comment", models.TextField(blank=True)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("pending", "Pending"),
                            ("approved", "Approved"),
                            ("paid", "Paid"),
                            ("rejected", "Rejected"),
                            ("withdrawn", "Withdrawn"),
                        ],
                        db_index=True,
                        default="pending",
                        max_length=12,
                    ),
                ),
                ("auto_rejected", models.BooleanField(default=False)),
                (
                    "approved_amount",
                    models.PositiveBigIntegerField(blank=True, null=True),
                ),
                ("decided_at", models.DateTimeField(blank=True, null=True)),
                ("decision_reason", models.TextField(blank=True)),
                ("paid_at", models.DateTimeField(blank=True, db_index=True, null=True)),
                ("payment_note", models.CharField(blank=True, max_length=255)),
                (
                    "submitted_at",
                    models.DateTimeField(
                        db_index=True, default=django.utils.timezone.now
                    ),
                ),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "decided_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "fc_user",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="fleetops_srp_fc_claims",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "operation",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="srp_claims",
                        to="fleetops.fleetoperation",
                    ),
                ),
                (
                    "paid_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="fleetops_srp_claims",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "verbose_name": "SRP claim",
                "ordering": ["-submitted_at"],
            },
        ),
        migrations.CreateModel(
            name="SrpClaimEvent",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "action",
                    models.CharField(
                        choices=[
                            ("submit", "Submitted"),
                            ("auto_reject", "Rejected automatically"),
                            ("withdraw", "Withdrawn"),
                            ("resubmit", "Submitted again"),
                            ("approve", "Approved"),
                            ("amount_change", "Amount changed"),
                            ("reject", "Rejected"),
                            ("revoke", "Approval revoked"),
                            ("paid", "Marked paid"),
                            ("unpaid", "Payment undone"),
                            ("reopen", "Reopened"),
                            ("reclassify", "Moved between in fleet and out of fleet"),
                            ("recalculate", "Recalculated"),
                            ("recheck", "Fleet presence checked again"),
                            ("value_update", "Loss value updated"),
                            ("note", "Internal note"),
                        ],
                        max_length=30,
                    ),
                ),
                ("from_status", models.CharField(blank=True, max_length=12)),
                ("to_status", models.CharField(blank=True, max_length=12)),
                ("amount", models.PositiveBigIntegerField(blank=True, null=True)),
                ("reason", models.TextField(blank=True)),
                ("internal", models.BooleanField(default=False)),
                ("data", models.JSONField(blank=True, default=dict)),
                (
                    "created_at",
                    models.DateTimeField(
                        db_index=True, default=django.utils.timezone.now
                    ),
                ),
                (
                    "actor",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "claim",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="events",
                        to="fleetops.srpclaim",
                    ),
                ),
            ],
            options={
                "verbose_name": "SRP claim event",
                "ordering": ["-created_at", "-pk"],
            },
        ),
        migrations.CreateModel(
            name="SrpFleetLink",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("code", models.CharField(max_length=32, unique=True)),
                ("fc_main_character_id", models.BigIntegerField(blank=True, null=True)),
                (
                    "fc_main_character_name",
                    models.CharField(blank=True, max_length=255),
                ),
                ("is_open", models.BooleanField(default=True)),
                ("closed_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(default=django.utils.timezone.now)),
                (
                    "closed_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "created_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "fc_user",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="fleetops_srp_links",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "operation",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="srp_link",
                        to="fleetops.fleetoperation",
                    ),
                ),
            ],
            options={
                "verbose_name": "SRP fleet link",
                "ordering": ["-created_at"],
            },
        ),
        migrations.AddField(
            model_name="srpclaim",
            name="link",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="claims",
                to="fleetops.srpfleetlink",
            ),
        ),
        migrations.AddField(
            model_name="srpclaim",
            name="killmail",
            field=models.OneToOneField(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="claim",
                to="fleetops.srpkillmail",
            ),
        ),
        migrations.CreateModel(
            name="SrpPayoutRule",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("label", models.CharField(blank=True, max_length=100)),
                (
                    "ship_type_id",
                    models.PositiveBigIntegerField(
                        default=0,
                        help_text="0 = any ship (doctrine rules only).",
                        verbose_name="Ship type ID",
                    ),
                ),
                ("ship_type_name", models.CharField(blank=True, max_length=255)),
                ("doctrine_source", models.CharField(blank=True, max_length=100)),
                ("doctrine_external_id", models.CharField(blank=True, max_length=255)),
                ("doctrine_name", models.CharField(blank=True, max_length=255)),
                (
                    "doctrine_key",
                    models.CharField(
                        blank=True, db_index=True, editable=False, max_length=400
                    ),
                ),
                (
                    "scope",
                    models.CharField(
                        choices=[
                            ("any", "In fleet and out of fleet"),
                            ("in_fleet", "In-fleet losses only"),
                            ("out_of_fleet", "Out-of-fleet losses only"),
                        ],
                        default="any",
                        max_length=12,
                    ),
                ),
                (
                    "amount",
                    models.PositiveBigIntegerField(
                        help_text="Whole ISK.",
                        validators=[
                            django.core.validators.MinValueValidator(1),
                            django.core.validators.MaxValueValidator(1000000000000000),
                        ],
                    ),
                ),
                ("is_active", models.BooleanField(default=True)),
                ("notes", models.CharField(blank=True, max_length=255)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "verbose_name": "SRP payout rule",
                "ordering": ["ship_type_name", "doctrine_name", "scope"],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("ship_type_id", "doctrine_key", "scope"),
                        name="fleetops_unique_srp_rule",
                        violation_error_message="A rule for this ship, doctrine and scope already exists.",
                    )
                ],
            },
        ),
        migrations.AddField(
            model_name="srpclaim",
            name="payout_rule",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="claims",
                to="fleetops.srppayoutrule",
            ),
        ),
        migrations.AddIndex(
            model_name="srpclaim",
            index=models.Index(
                fields=["status", "killmail_time"], name="fleetops_srp_status_time_idx"
            ),
        ),
        migrations.AddIndex(
            model_name="srpclaim",
            index=models.Index(
                fields=["user", "killmail_time"], name="fleetops_srp_user_time_idx"
            ),
        ),
        migrations.AddIndex(
            model_name="srpclaim",
            index=models.Index(
                fields=["corporation_id", "killmail_time"],
                name="fleetops_srp_corp_time_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="srpclaim",
            index=models.Index(
                fields=["fc_user", "killmail_time"], name="fleetops_srp_fc_time_idx"
            ),
        ),
    ]
