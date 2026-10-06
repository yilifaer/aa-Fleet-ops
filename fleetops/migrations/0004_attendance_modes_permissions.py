# Generated for AA FleetOps 0.1.0a5
import django.core.validators
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("fleetops", "0003_srp_history_roles"),
    ]

    operations = [
        migrations.AddField(
            model_name="fleetoperation",
            name="send_ping",
            field=models.BooleanField(
                default=True,
                help_text="If false, the operation tracks attendance without sending a Discord ping.",
            ),
        ),
        migrations.AddField(
            model_name="fleetoperation",
            name="attendance_multiplier",
            field=models.PositiveIntegerField(
                default=1,
                help_text="Fleet-wide multiplier applied to automatically granted attendance (1x, 2x or 3x).",
                validators=[
                    django.core.validators.MinValueValidator(1),
                    django.core.validators.MaxValueValidator(3),
                ],
            ),
        ),
        migrations.AddField(
            model_name="fleetoperation",
            name="is_manual",
            field=models.BooleanField(
                default=False,
                help_text="Manual fleet record created without ESI fleet detection/tracking.",
            ),
        ),
        migrations.AlterModelOptions(
            name="fleetopssettings",
            options={
                "verbose_name": "FleetOps setting",
                "verbose_name_plural": "FleetOps settings",
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
                ],
            },
        ),
    ]
