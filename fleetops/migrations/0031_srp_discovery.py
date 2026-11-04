# AA FleetOps 0.3.0b1: automatic loss discovery, schema. Adds the tables for the losses found by reading the
# members' killmail lists (SrpLoss, SrpLossScan), the source of a claim (default: pasted link) and the three
# settings of the feature (discovery off, 60 minutes, all characters need not be authorized). Nothing is filled in.
# Going back drops the two tables and the columns; the found losses are lost, claims keep everything else.

import django.core.validators
import django.db.models.deletion
import django.utils.timezone
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("fleetops", "0030_srp_intake_blocks"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="srpclaim",
            name="source",
            field=models.CharField(
                choices=[
                    ("paste", "Pasted link"),
                    ("discovered", "Found automatically"),
                    ("aasrp", "aa-srp import"),
                ],
                default="paste",
                max_length=10,
                verbose_name="Source",
            ),
        ),
        migrations.AddField(
            model_name="srpsettings",
            name="loss_discovery_enabled",
            field=models.BooleanField(
                default=False,
                help_text="Read the recent losses of the members' characters from ESI and list them on My SRP. Members must authorize loss reading for each character.",
                verbose_name="Find losses automatically",
            ),
        ),
        migrations.AddField(
            model_name="srpsettings",
            name="loss_discovery_interval_minutes",
            field=models.PositiveSmallIntegerField(
                default=60,
                help_text="Between 15 and 1440 minutes.",
                validators=[
                    django.core.validators.MinValueValidator(15),
                    django.core.validators.MaxValueValidator(1440),
                ],
                verbose_name="Minutes between two reads of a character",
            ),
        ),
        migrations.AddField(
            model_name="srpsettings",
            name="require_all_alts_authorized",
            field=models.BooleanField(
                default=False,
                help_text="A member with a character that has not authorized loss reading cannot submit claims. Without this switch such characters are not found automatically; their losses can still be pasted as links. Only in force while losses are found automatically.",
                verbose_name="Members must authorize loss reading for all their characters",
            ),
        ),
        migrations.CreateModel(
            name="SrpLossScan",
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
                    "character_id",
                    models.BigIntegerField(unique=True, verbose_name="Character ID"),
                ),
                (
                    "last_checked_at",
                    models.DateTimeField(
                        blank=True, null=True, verbose_name="Last checked at"
                    ),
                ),
                (
                    "next_check_at",
                    models.DateTimeField(
                        blank=True,
                        db_index=True,
                        null=True,
                        verbose_name="Next check at",
                    ),
                ),
                (
                    "status",
                    models.CharField(
                        blank=True,
                        choices=[
                            ("ok", "Read successfully"),
                            ("no_token", "Not authorized"),
                            ("denied", "Refused by ESI"),
                            ("error", "Failed"),
                            ("throttled", "Waiting for ESI"),
                        ],
                        default="",
                        max_length=12,
                        verbose_name="Status",
                    ),
                ),
                (
                    "status_at",
                    models.DateTimeField(
                        blank=True, null=True, verbose_name="Status at"
                    ),
                ),
                (
                    "failures",
                    models.PositiveSmallIntegerField(
                        default=0, verbose_name="Failures"
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="User",
                    ),
                ),
            ],
            options={
                "verbose_name": "SRP loss scan",
                "verbose_name_plural": "SRP loss scans",
                "ordering": ["character_id"],
                "default_permissions": (),
            },
        ),
        migrations.CreateModel(
            name="SrpLoss",
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
                ("character_id", models.BigIntegerField(verbose_name="Character ID")),
                ("killmail_id", models.BigIntegerField(verbose_name="Killmail ID")),
                (
                    "killmail_hash",
                    models.CharField(
                        blank=True, max_length=40, verbose_name="Killmail hash"
                    ),
                ),
                (
                    "kind",
                    models.CharField(
                        choices=[
                            ("loss", "Loss"),
                            ("kill", "Kill"),
                            ("old", "Too old"),
                        ],
                        max_length=4,
                        verbose_name="Kind",
                    ),
                ),
                (
                    "killmail_time",
                    models.DateTimeField(
                        blank=True, null=True, verbose_name="Killmail time"
                    ),
                ),
                (
                    "ship_type_id",
                    models.BigIntegerField(
                        blank=True, null=True, verbose_name="Ship type ID"
                    ),
                ),
                (
                    "ship_type_name",
                    models.CharField(
                        blank=True, max_length=255, verbose_name="Ship type name"
                    ),
                ),
                (
                    "solar_system_id",
                    models.BigIntegerField(
                        blank=True, null=True, verbose_name="Solar system ID"
                    ),
                ),
                (
                    "solar_system_name",
                    models.CharField(
                        blank=True, max_length=100, verbose_name="Solar system name"
                    ),
                ),
                (
                    "ship_group_id",
                    models.BigIntegerField(
                        blank=True, null=True, verbose_name="Ship group ID"
                    ),
                ),
                (
                    "solar_system_security",
                    models.DecimalField(
                        blank=True,
                        decimal_places=4,
                        max_digits=5,
                        null=True,
                        verbose_name="Security status",
                    ),
                ),
                (
                    "region_id",
                    models.BigIntegerField(
                        blank=True, null=True, verbose_name="Region ID"
                    ),
                ),
                (
                    "war_id",
                    models.BigIntegerField(
                        blank=True, null=True, verbose_name="War ID"
                    ),
                ),
                (
                    "match_code",
                    models.CharField(blank=True, max_length=20, verbose_name="Match"),
                ),
                (
                    "ignored_at",
                    models.DateTimeField(
                        blank=True, null=True, verbose_name="Ignored at"
                    ),
                ),
                (
                    "discovered_at",
                    models.DateTimeField(
                        default=django.utils.timezone.now, verbose_name="Discovered at"
                    ),
                ),
                (
                    "ignored_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="Ignored by",
                    ),
                ),
                (
                    "matched_link",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to="fleetops.srpfleetlink",
                        verbose_name="Matched link",
                    ),
                ),
            ],
            options={
                "verbose_name": "SRP loss",
                "verbose_name_plural": "SRP losses",
                "ordering": ["-killmail_time", "-killmail_id"],
                "default_permissions": (),
                "indexes": [
                    models.Index(
                        fields=["character_id", "kind", "killmail_time"],
                        name="fleetops_srploss_char_idx",
                    )
                ],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("character_id", "killmail_id"),
                        name="fleetops_unique_srp_loss",
                    )
                ],
            },
        ),
    ]
