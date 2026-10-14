# AA FleetOps 0.2.0b1: fleet management: wing and squad snapshot of each fleet, docked and fleet warp state of
# members, ship functions for the fleet composition, fleet structure templates and the bridge and live
# refresh settings. Schema only; 0014 seeds the default ship functions and structure template.

import django.core.validators
import django.db.models.deletion
from django.db import migrations, models

import fleetops.models

MAX_DATABASE_ID = 2**63 - 1
MAX_LIVE_REFRESH_SECONDS = 300
# Titan, Dreadnought, Freighter, Carrier, Supercarrier, Rorqual, Jump Freighter, Industrial Command Ship,
# Force Auxiliary, Lancer Dreadnought, Command Carrier: hulls that cannot take a titan bridge.
DEFAULT_BRIDGE_EXCLUDED_GROUP_IDS = "30,485,513,547,659,883,902,941,1538,4594,5120"
RULE_KIND_CHOICES = [("type", "Ship type"), ("group", "Ship group")]


class Migration(migrations.Migration):

    dependencies = [
        ("fleetops", "0012_pap_rules_data"),
    ]

    operations = [
        migrations.AddField(
            model_name="fleetopssettings",
            name="bridge_excluded_group_ids",
            field=models.CharField(
                blank=True,
                default=DEFAULT_BRIDGE_EXCLUDED_GROUP_IDS,
                help_text="SDE ship group IDs that cannot take a jump bridge. Their mass is not counted as "
                "bridgeable. Default: Titan, Dreadnought, Freighter, Carrier, Supercarrier, Rorqual, Jump "
                "Freighter, Industrial Command Ship, Force Auxiliary, Lancer Dreadnought, Command Carrier.",
                max_length=500,
                validators=[fleetops.models.validate_id_list],
                verbose_name="Ship groups without bridge",
            ),
        ),
        migrations.AddField(
            model_name="fleetopssettings",
            name="fleet_live_refresh_seconds",
            field=models.PositiveIntegerField(
                default=15,
                help_text="While someone who can manage a fleet has its Fleet tab open, read the fleet from ESI "
                "this often (seconds). 0 = only the tracking interval and the Refresh button.",
                validators=[django.core.validators.MaxValueValidator(MAX_LIVE_REFRESH_SECONDS)],
                verbose_name="Live fleet refresh",
            ),
        ),
        migrations.AddField(
            model_name="fleetoperation",
            name="fleet_wings",
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.AddField(
            model_name="fleetoperation",
            name="fleet_wings_read_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="fleetmemberstate",
            name="station_id",
            field=models.BigIntegerField(blank=True, help_text="Set while the member is docked.", null=True),
        ),
        migrations.AddField(
            model_name="fleetmemberstate",
            name="takes_fleet_warp",
            field=models.BooleanField(blank=True, null=True),
        ),
        migrations.CreateModel(
            name="ShipFunction",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=60, unique=True)),
                (
                    "color",
                    models.CharField(
                        default="#6c757d",
                        max_length=7,
                        validators=[
                            django.core.validators.RegexValidator(
                                "^#[0-9a-fA-F]{6}$", "Enter a color as # and six hex digits, such as #0d6efd."
                            )
                        ],
                    ),
                ),
                (
                    "fleet_roles",
                    models.CharField(
                        blank=True,
                        help_text="Comma-separated fleet positions (fleet_commander, wing_commander, "
                        "squad_commander). Members in these positions count here whatever they fly.",
                        max_length=100,
                        validators=[fleetops.models.validate_fleet_roles],
                        verbose_name="Fleet positions",
                    ),
                ),
                (
                    "is_default",
                    models.BooleanField(
                        default=False,
                        help_text="Ships that no rule matches count here. Needs django-eveonline-sde to recognise "
                        "ships.",
                        verbose_name="Default",
                    ),
                ),
                ("sort_order", models.PositiveIntegerField(default=0)),
                ("is_active", models.BooleanField(default=True)),
            ],
            options={
                "ordering": ["sort_order", "name"],
            },
        ),
        migrations.CreateModel(
            name="ShipFunctionRule",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("kind", models.CharField(choices=RULE_KIND_CHOICES, max_length=10)),
                (
                    "target_id",
                    models.BigIntegerField(
                        help_text="ID of the ship type or ship group in the SDE.",
                        validators=[
                            django.core.validators.MinValueValidator(1),
                            django.core.validators.MaxValueValidator(MAX_DATABASE_ID),
                        ],
                        verbose_name="Type or group ID",
                    ),
                ),
                (
                    "label",
                    models.CharField(
                        blank=True,
                        help_text="Type or group name when the rule was saved, shown without the SDE.",
                        max_length=255,
                    ),
                ),
                (
                    "function",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="rules",
                        to="fleetops.shipfunction",
                    ),
                ),
            ],
            options={
                "ordering": ["function__sort_order", "kind", "label", "target_id"],
                "constraints": [
                    models.UniqueConstraint(fields=("kind", "target_id"), name="fleetops_unique_ship_function_rule")
                ],
            },
        ),
        migrations.CreateModel(
            name="FleetStructureTemplate",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=100, unique=True)),
                (
                    "layout",
                    models.TextField(
                        help_text="One wing per line: 'Wing name: Squad 1, Squad 2'. Names up to 10 characters; "
                        "up to 5 wings with up to 5 squads each. Lines starting with # are ignored.",
                        validators=[fleetops.models.validate_structure_layout],
                    ),
                ),
                ("description", models.CharField(blank=True, max_length=255)),
                ("is_active", models.BooleanField(default=True)),
                ("sort_order", models.PositiveIntegerField(default=0)),
            ],
            options={
                "ordering": ["sort_order", "name"],
            },
        ),
    ]
