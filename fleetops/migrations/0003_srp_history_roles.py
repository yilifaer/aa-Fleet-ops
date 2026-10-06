# AA FleetOps 0.1.0a3: SRP links, history retention and special roles
import django.core.validators
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("fleetops", "0002_seed_defaults"),
    ]

    operations = [
        migrations.AlterField(
            model_name="fleetopssettings",
            name="data_retention_days",
            field=models.PositiveIntegerField(
                default=365,
                help_text="Attendance/event history retention in days. Minimum 365 days.",
                validators=[django.core.validators.MinValueValidator(365)],
            ),
        ),
        migrations.AddField(
            model_name="fleetopssettings",
            name="history_alliance_ids",
            field=models.CharField(
                blank=True,
                help_text="Comma-separated alliance IDs whose current members may appear in attendance history. Empty disables membership pruning.",
                max_length=500,
            ),
        ),
        migrations.AddField(
            model_name="fleetopssettings",
            name="srp_auto_create",
            field=models.BooleanField(
                default=True,
                help_text="Automatically create/link an SRP fleet when a supported SRP provider is available.",
            ),
        ),
        migrations.AddField(
            model_name="fleetopssettings",
            name="srp_provider",
            field=models.CharField(
                default="auto",
                help_text="SRP provider key. 'auto' prefers Alliance Auth built-in SRP and allows future provider plugins.",
                max_length=50,
            ),
        ),
        migrations.AddField(
            model_name="fleetoperation",
            name="srp_provider",
            field=models.CharField(blank=True, max_length=50),
        ),
        migrations.AddField(
            model_name="fleetoperation",
            name="srp_reference",
            field=models.CharField(blank=True, max_length=255),
        ),
        migrations.AddField(
            model_name="fleetoperation",
            name="srp_url",
            field=models.CharField(blank=True, max_length=1000),
        ),
        migrations.AddField(
            model_name="fleetoperation",
            name="srp_error",
            field=models.TextField(blank=True),
        ),
        migrations.AlterField(
            model_name="operationaction",
            name="status",
            field=models.CharField(
                choices=[
                    ("pending", "Pending"),
                    ("success", "Success"),
                    ("failed", "Failed"),
                    ("skipped", "Skipped"),
                ],
                default="pending",
                max_length=20,
            ),
        ),
        migrations.RemoveConstraint(
            model_name="attendancerecord",
            name="fleetops_unique_attendance_source",
        ),
        migrations.AddIndex(
            model_name="attendancerecord",
            index=models.Index(
                fields=["operation", "character_id", "source"],
                name="fleetops_att_op_char_src_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="attendancerecord",
            index=models.Index(
                fields=["auth_user", "created_at"],
                name="fleetops_att_user_date_idx",
            ),
        ),
        migrations.CreateModel(
            name="OperationRoleAssignment",
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
                    "role",
                    models.CharField(
                        choices=[
                            ("backseat_fc", "Back Seat FC"),
                            ("logi_anchor", "Logi Anchor"),
                            ("snowflake", "Snowflake Member"),
                        ],
                        db_index=True,
                        max_length=30,
                    ),
                ),
                ("character_id", models.BigIntegerField(db_index=True)),
                ("character_name", models.CharField(max_length=255)),
                ("main_character_id", models.BigIntegerField(blank=True, db_index=True, null=True)),
                ("main_character_name", models.CharField(blank=True, max_length=255)),
                ("corporation_id", models.BigIntegerField(blank=True, null=True)),
                ("corporation_name", models.CharField(blank=True, max_length=255)),
                (
                    "grants_fc_credit",
                    models.BooleanField(
                        default=False,
                        help_text="Snapshot: this assignment counts as an FC fleet/points credit for the assigned user.",
                    ),
                ),
                ("notes", models.TextField(blank=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "assigned_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="fleetops_roles_assigned",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "auth_user",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="fleetops_role_assignments",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "operation",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="role_assignments",
                        to="fleetops.fleetoperation",
                    ),
                ),
            ],
            options={"ordering": ["role", "character_name"]},
        ),
        migrations.AddConstraint(
            model_name="operationroleassignment",
            constraint=models.UniqueConstraint(
                fields=("operation", "role", "character_id"),
                name="fleetops_unique_operation_role_character",
            ),
        ),
    ]
