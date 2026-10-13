# AA FleetOps 0.1.0a6: PAP rules (multiplier tiers, time in fleet, minimum time, per-fleet snapshots),
# FC lead review, statistics inclusion, configurable special roles, PAP links and Fleet Start presets.
# Schema only; 0012 fills the new fields of existing rows.

import django.core.validators
import django.db.models.deletion
import django.utils.timezone
from django.conf import settings
from django.db import migrations, models

MAX_MULTIPLIER = 10
REVIEW_STATUS_CHOICES = [("approved", "Approved"), ("pending", "Pending approval"), ("rejected", "Rejected")]
MULTIPLIER_SOURCE_CHOICES = [
    ("default", "Default"),
    ("fc", "Chosen by the FC"),
    ("auto", "By fleet duration"),
    ("override", "Override by an FC lead"),
    ("manual", "Manual fleet"),
]
STATS_MODE_CHOICES = [("auto", "Automatic"), ("include", "Always count"), ("exclude", "Do not count")]
CORRECTION_CHOICES = [
    ("keep", "Keep separate"),
    ("replace", "Replace tracked PAP"),
    ("merge", "Merge into tracked PAP"),
]
SOURCE_CHOICES = [("automatic", "Automatic"), ("manual", "Manual"), ("imported", "Imported"), ("click", "PAP link")]


def multiplier_validators(lowest):
    return [
        django.core.validators.MinValueValidator(lowest),
        django.core.validators.MaxValueValidator(MAX_MULTIPLIER),
    ]


def review_fields(model_name, related_name):
    return [
        migrations.AddField(
            model_name=model_name,
            name="review_status",
            field=models.CharField(
                choices=REVIEW_STATUS_CHOICES, db_index=True, default="approved", max_length=10
            ),
        ),
        migrations.AddField(
            model_name=model_name,
            name="reviewed_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name=related_name,
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name=model_name,
            name="reviewed_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name=model_name,
            name="review_note",
            field=models.CharField(blank=True, max_length=255),
        ),
    ]


class Migration(migrations.Migration):

    dependencies = [
        ("fleetops", "0010_comms_join_token_unique"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        # Settings
        migrations.AlterField(
            model_name="fleetopssettings",
            name="attendance_limit",
            field=models.PositiveIntegerField(
                blank=True,
                help_text="Maximum characters of one Alliance Auth user that receive PAP in one fleet. Empty = unlimited.",
                null=True,
                validators=[django.core.validators.MinValueValidator(1)],
                verbose_name="PAP characters per player",
            ),
        ),
        migrations.AddField(
            model_name="fleetopssettings",
            name="multiplier_mode",
            field=models.CharField(
                choices=[("fc", "FC chooses when ending"), ("auto", "Automatic by fleet duration")],
                default="fc",
                max_length=10,
                verbose_name="PAP multiplier mode",
            ),
        ),
        migrations.AddField(
            model_name="fleetopssettings",
            name="pap_minimum_minutes",
            field=models.PositiveIntegerField(
                default=0,
                help_text="Minutes a character must accumulate in fleet to receive PAP. 0 = any presence.",
                validators=[django.core.validators.MaxValueValidator(1440)],
                verbose_name="PAP minimum time",
            ),
        ),
        migrations.AddField(
            model_name="fleetopssettings",
            name="minimum_fleet_minutes",
            field=models.PositiveIntegerField(
                default=0,
                help_text="Fleets shorter than this are not counted in statistics and incentives unless included "
                "manually. 0 = count all.",
                validators=[django.core.validators.MaxValueValidator(1440)],
                verbose_name="Minimum fleet duration",
            ),
        ),
        migrations.AddField(
            model_name="fleetopssettings",
            name="pap_link_minutes",
            field=models.PositiveIntegerField(
                default=30,
                help_text="Minutes a new PAP link stays open unless the FC chooses another time.",
                validators=[
                    django.core.validators.MinValueValidator(1),
                    django.core.validators.MaxValueValidator(1440),
                ],
                verbose_name="PAP link validity",
            ),
        ),
        migrations.CreateModel(
            name="MultiplierTier",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                (
                    "threshold_minutes",
                    models.PositiveIntegerField(
                        help_text="Fleets running strictly longer than this many minutes qualify.",
                        unique=True,
                        validators=[
                            django.core.validators.MinValueValidator(1),
                            django.core.validators.MaxValueValidator(10080),
                        ],
                    ),
                ),
                ("multiplier", models.PositiveSmallIntegerField(validators=multiplier_validators(2))),
            ],
            options={
                "ordering": ["threshold_minutes"],
            },
        ),
        # Fleets
        migrations.AlterField(
            model_name="fleetoperation",
            name="attendance_multiplier",
            field=models.PositiveIntegerField(
                default=1,
                help_text="Fleet-wide PAP multiplier applied to automatically granted attendance.",
                validators=multiplier_validators(1),
            ),
        ),
        migrations.AlterField(
            model_name="fleetoperation",
            name="end_requested_multiplier",
            field=models.PositiveIntegerField(blank=True, null=True, validators=multiplier_validators(1)),
        ),
        migrations.AddField(
            model_name="fleetoperation",
            name="multiplier_pending",
            field=models.BooleanField(
                db_index=True,
                default=False,
                help_text="The fleet ended without a multiplier choice although a higher multiplier was reached.",
            ),
        ),
        migrations.AddField(
            model_name="fleetoperation",
            name="multiplier_source",
            field=models.CharField(blank=True, choices=MULTIPLIER_SOURCE_CHOICES, max_length=10),
        ),
        migrations.AddField(
            model_name="fleetoperation",
            name="link_only",
            field=models.BooleanField(
                default=False, help_text="Fleet started without ESI; members register through PAP links."
            ),
        ),
        # 0012 marks the fleets that were already finished as legacy (1).
        migrations.AddField(
            model_name="fleetoperation",
            name="pap_rules",
            field=models.PositiveSmallIntegerField(choices=[(1, "Legacy"), (2, "Time in fleet")], default=2),
        ),
        migrations.AddField(
            model_name="fleetoperation",
            name="pap_minimum_minutes",
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="fleetoperation",
            name="pap_character_limit",
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
        *review_fields("fleetoperation", "fleetops_reviewed_operations"),
        migrations.AddField(
            model_name="fleetoperation",
            name="stats_mode",
            field=models.CharField(choices=STATS_MODE_CHOICES, default="auto", max_length=10),
        ),
        migrations.AddField(
            model_name="fleetoperation",
            name="counts_for_stats",
            field=models.BooleanField(
                db_index=True,
                default=True,
                help_text="Counted in statistics and incentives; follows the minimum fleet duration unless overridden.",
            ),
        ),
        migrations.AddField(
            model_name="fleetoperation",
            name="stats_note",
            field=models.CharField(blank=True, max_length=255),
        ),
        # Members and attendance
        migrations.AddField(
            model_name="fleetmemberstate",
            name="accumulated_seconds",
            field=models.PositiveIntegerField(default=0, help_text="Time in fleet so far."),
        ),
        migrations.AlterField(
            model_name="attendancerecord",
            name="source",
            field=models.CharField(choices=SOURCE_CHOICES, max_length=20),
        ),
        migrations.AddField(
            model_name="attendancerecord",
            name="below_minimum",
            field=models.BooleanField(default=False, help_text="The character did not reach the PAP minimum time."),
        ),
        migrations.AddField(
            model_name="attendancerecord",
            name="replaced",
            field=models.BooleanField(default=False, help_text="Replaced by an approved manual PAP."),
        ),
        migrations.AddField(
            model_name="attendancerecord",
            name="extra_value",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="attendancerecord",
            name="legacy_extra_value",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="attendancerecord",
            name="tracked_seconds",
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="attendancerecord",
            name="correction",
            field=models.CharField(
                choices=CORRECTION_CHOICES,
                default="keep",
                help_text="What a manual or imported row does with the tracked PAP of the same character.",
                max_length=10,
            ),
        ),
        *review_fields("attendancerecord", "fleetops_reviewed_attendance"),
        # Special roles; assignments keep the role key and 0012 links them to the role rows.
        migrations.CreateModel(
            name="SpecialRole",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("key", models.SlugField(editable=False, max_length=30, unique=True)),
                ("name", models.CharField(max_length=100, unique=True)),
                (
                    "grants_fc_credit",
                    models.BooleanField(
                        default=False,
                        help_text="Pilots with this role get the fleet as an FC fleet for FC points and incentives. "
                        "A change applies to new assignments; remove and add an assignment again to apply it there.",
                        verbose_name="Gives FC credit",
                    ),
                ),
                ("is_active", models.BooleanField(default=True, help_text="FCs can assign this role.")),
                ("sort_order", models.PositiveIntegerField(default=0)),
            ],
            options={
                "ordering": ["sort_order", "name"],
            },
        ),
        migrations.AlterField(
            model_name="operationroleassignment",
            name="role",
            field=models.CharField(db_index=True, help_text="Key of the special role.", max_length=30),
        ),
        migrations.AddField(
            model_name="operationroleassignment",
            name="special_role",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="assignments",
                to="fleetops.specialrole",
            ),
        ),
        # PAP links
        migrations.CreateModel(
            name="PapLink",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("code", models.CharField(max_length=64, unique=True)),
                ("created_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("expires_at", models.DateTimeField(db_index=True)),
                (
                    "require_online",
                    models.BooleanField(
                        default=True, help_text="Characters must be logged in to the game when they register."
                    ),
                ),
                ("revoked_at", models.DateTimeField(blank=True, null=True)),
                (
                    "created_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="fleetops_pap_links",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "operation",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="pap_links",
                        to="fleetops.fleetoperation",
                    ),
                ),
            ],
            options={
                "verbose_name": "PAP link",
                "ordering": ["-created_at"],
            },
        ),
        migrations.CreateModel(
            name="PapRegistration",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("character_id", models.BigIntegerField(db_index=True)),
                ("character_name", models.CharField(max_length=255)),
                ("ship_type_id", models.BigIntegerField(blank=True, null=True)),
                ("ship_type_name", models.CharField(blank=True, max_length=255)),
                ("solar_system_id", models.BigIntegerField(blank=True, null=True)),
                ("solar_system_name", models.CharField(blank=True, max_length=255)),
                ("online", models.BooleanField(blank=True, null=True)),
                ("registered_at", models.DateTimeField(default=django.utils.timezone.now)),
                (
                    "auth_user",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="fleetops_pap_registrations",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "link",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="registrations",
                        to="fleetops.paplink",
                    ),
                ),
                (
                    "operation",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="pap_registrations",
                        to="fleetops.fleetoperation",
                    ),
                ),
            ],
            options={
                "verbose_name": "PAP registration",
                "ordering": ["registered_at"],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("operation", "character_id"), name="fleetops_unique_pap_registration"
                    )
                ],
            },
        ),
        # Fleet Start presets
        migrations.CreateModel(
            name="FleetStartPreset",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(blank=True, max_length=100)),
                ("is_last_used", models.BooleanField(default=False)),
                ("is_default", models.BooleanField(default=False)),
                ("values", models.JSONField(blank=True, default=dict)),
                ("fc_character_id", models.BigIntegerField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "owner",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="fleetops_start_presets",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "ordering": ["name"],
                "constraints": [
                    models.UniqueConstraint(fields=("owner", "name"), name="fleetops_unique_start_preset")
                ],
            },
        ),
    ]
