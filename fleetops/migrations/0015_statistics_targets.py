# AA FleetOps 0.1.0a6: monthly statistics targets (corporation average PAP and member minimum), the
# leaderboard settings and the "Member alliance IDs" label of the alliance filter. Schema only; existing PAP
# rows already carry the corporation at PAP time, so no data is converted.

from decimal import Decimal

import django.core.validators
from django.db import migrations, models

import fleetops.models


class Migration(migrations.Migration):

    dependencies = [
        ("fleetops", "0014_seed_fleet_management"),
    ]

    operations = [
        migrations.CreateModel(
            name="StatisticsTarget",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                (
                    "year",
                    models.PositiveIntegerField(
                        validators=[
                            django.core.validators.MinValueValidator(2003),
                            django.core.validators.MaxValueValidator(2200),
                        ]
                    ),
                ),
                (
                    "month",
                    models.PositiveSmallIntegerField(
                        validators=[
                            django.core.validators.MinValueValidator(1),
                            django.core.validators.MaxValueValidator(12),
                        ]
                    ),
                ),
                (
                    "corporation_average_target",
                    models.DecimalField(
                        blank=True,
                        decimal_places=2,
                        help_text="Average PAP per main character a corporation needs in a month. Empty = no target.",
                        max_digits=8,
                        null=True,
                        validators=[django.core.validators.MinValueValidator(Decimal("0"))],
                        verbose_name="Corporation average target",
                    ),
                ),
                (
                    "member_minimum_pap",
                    models.PositiveIntegerField(
                        default=1,
                        help_text="Members with less PAP than this in a month are listed as inactive. "
                        "0 = nobody is listed.",
                        validators=[django.core.validators.MaxValueValidator(10000)],
                        verbose_name="Member minimum PAP",
                    ),
                ),
                ("note", models.CharField(blank=True, max_length=255)),
            ],
            options={
                "verbose_name": "monthly target",
                "ordering": ["-year", "-month"],
                "constraints": [
                    models.UniqueConstraint(fields=("year", "month"), name="fleetops_unique_statistics_target")
                ],
            },
        ),
        migrations.AddField(
            model_name="fleetopssettings",
            name="leaderboard_size",
            field=models.PositiveSmallIntegerField(
                default=20,
                help_text="Places shown on PAP leaderboards. Members tied on the last place are all shown.",
                validators=[
                    django.core.validators.MinValueValidator(3),
                    django.core.validators.MaxValueValidator(100),
                ],
                verbose_name="Leaderboard places",
            ),
        ),
        migrations.AddField(
            model_name="fleetopssettings",
            name="leaderboard_visibility",
            field=models.CharField(
                choices=[("members", "Everyone with FleetOps access"), ("managers", "Statistics managers only")],
                default="members",
                help_text="Who sees the PAP leaderboard. Everyone with FleetOps access sees the alliance board and "
                "their own corporation's board; statistics managers see the boards their statistics permission "
                "covers.",
                max_length=10,
                verbose_name="Leaderboard visibility",
            ),
        ),
        migrations.AlterField(
            model_name="fleetopssettings",
            name="history_alliance_ids",
            field=models.CharField(
                blank=True,
                help_text="Comma-separated alliance IDs. When set, statistics, leaderboards and history only "
                "include users whose main character is currently in one of these alliances, and departed "
                "members' history can be pruned. Empty = every Alliance Auth user with a main character.",
                max_length=500,
                validators=[fleetops.models.validate_alliance_ids],
                verbose_name="Member alliance IDs",
            ),
        ),
    ]
