# AA FleetOps 0.3.0b1: SRP intake blocks, schema. The SRP settings get the block lists (ship groups, ships, security
# bands) with their two switches and the ban settings; the SRP killmail gets the ship group, security status,
# constellation, region and origin; SRP bans get a table. Everything is added and nothing is filled in: the block
# lists start empty (nothing is refused) and old killmails keep empty location fields. Going back drops the
# columns and the ban table (the bans are lost).

import django.core.validators
import django.db.models.deletion
import django.utils.timezone
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("fleetops", "0029_srp_links_guard"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="srpkillmail",
            name="constellation_id",
            field=models.BigIntegerField(
                blank=True, null=True, verbose_name="Constellation ID"
            ),
        ),
        migrations.AddField(
            model_name="srpkillmail",
            name="location_checked_at",
            field=models.DateTimeField(
                blank=True, null=True, verbose_name="Location checked at"
            ),
        ),
        migrations.AddField(
            model_name="srpkillmail",
            name="origin",
            field=models.CharField(
                choices=[("esi", "ESI"), ("aasrp", "aa-srp import")],
                default="esi",
                max_length=10,
                verbose_name="Origin",
            ),
        ),
        migrations.AddField(
            model_name="srpkillmail",
            name="region_id",
            field=models.BigIntegerField(
                blank=True, null=True, verbose_name="Region ID"
            ),
        ),
        migrations.AddField(
            model_name="srpkillmail",
            name="ship_group_id",
            field=models.BigIntegerField(
                blank=True, db_index=True, null=True, verbose_name="Ship group ID"
            ),
        ),
        migrations.AddField(
            model_name="srpkillmail",
            name="solar_system_security",
            field=models.DecimalField(
                blank=True,
                decimal_places=4,
                max_digits=5,
                null=True,
                verbose_name="Security status",
            ),
        ),
        migrations.AddField(
            model_name="srpsettings",
            name="auto_ban_over_monthly_cap",
            field=models.BooleanField(
                default=False,
                help_text="A member whose personal monthly limit is used up and who submits another claim cannot submit until the end of the EVE month.",
                verbose_name="Ban members who keep submitting over their monthly cap until the end of the month",
            ),
        ),
        migrations.AddField(
            model_name="srpsettings",
            name="ban_default_days",
            field=models.PositiveSmallIntegerField(
                default=7,
                help_text="Preselected when an SRP ban is made.",
                validators=[
                    django.core.validators.MinValueValidator(1),
                    django.core.validators.MaxValueValidator(3650),
                ],
                verbose_name="Default length of an SRP ban (days)",
            ),
        ),
        migrations.AddField(
            model_name="srpsettings",
            name="ban_max_days",
            field=models.PositiveSmallIntegerField(
                default=365,
                help_text="A permanent ban is not limited by this.",
                validators=[
                    django.core.validators.MinValueValidator(1),
                    django.core.validators.MaxValueValidator(3650),
                ],
                verbose_name="Longest SRP ban (days)",
            ),
        ),
        migrations.AddField(
            model_name="srpsettings",
            name="blocked_security_bands",
            field=models.CharField(
                blank=True,
                default="",
                help_text="Losses in these kinds of space are refused when a claim is checked. Empty = none.",
                max_length=100,
                verbose_name="Security bands that are not covered",
            ),
        ),
        migrations.AddField(
            model_name="srpsettings",
            name="blocked_ship_group_ids",
            field=models.CharField(
                blank=True,
                default="",
                help_text="SDE ship group IDs whose losses are refused when a claim is checked. Empty = none.",
                max_length=500,
                verbose_name="Ship groups that are not covered",
            ),
        ),
        migrations.AddField(
            model_name="srpsettings",
            name="blocked_ship_type_ids",
            field=models.CharField(
                blank=True,
                default="",
                help_text="Ship type IDs whose losses are refused when a claim is checked. Empty = none.",
                max_length=500,
                verbose_name="Ships that are not covered",
            ),
        ),
        migrations.AddField(
            model_name="srpsettings",
            name="highsec_war_losses_covered",
            field=models.BooleanField(
                default=False,
                help_text="Losses in a war (the killmail names a war) are not refused for high security space.",
                verbose_name="Wartime losses in high security are covered",
            ),
        ),
        migrations.AddField(
            model_name="srpsettings",
            name="payout_rule_lifts_ship_block",
            field=models.BooleanField(
                default=True,
                help_text="A ship that has its own entry in the payout table is covered even when its ship group or the ship is not covered. Security bands are not affected.",
                verbose_name="A payout table entry for the ship lifts the ship block",
            ),
        ),
        migrations.CreateModel(
            name="SrpBan",
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
                    "main_character_name",
                    models.CharField(
                        blank=True, max_length=255, verbose_name="Main character"
                    ),
                ),
                (
                    "reason",
                    models.TextField(
                        help_text="Shown to the member.",
                        validators=[django.core.validators.MaxLengthValidator(1000)],
                        verbose_name="Reason",
                    ),
                ),
                (
                    "internal_note",
                    models.TextField(
                        blank=True,
                        help_text="Only SRP staff see this.",
                        validators=[django.core.validators.MaxLengthValidator(1000)],
                        verbose_name="Internal note",
                    ),
                ),
                (
                    "kind",
                    models.CharField(
                        choices=[
                            ("manual", "By a reviewer"),
                            ("auto_monthly_cap", "Over the monthly cap"),
                        ],
                        default="manual",
                        max_length=20,
                        verbose_name="Kind",
                    ),
                ),
                (
                    "starts_at",
                    models.DateTimeField(
                        default=django.utils.timezone.now, verbose_name="Starts at"
                    ),
                ),
                (
                    "expires_at",
                    models.DateTimeField(
                        blank=True,
                        help_text="Empty = permanent.",
                        null=True,
                        verbose_name="Expires at",
                    ),
                ),
                (
                    "created_at",
                    models.DateTimeField(
                        default=django.utils.timezone.now, verbose_name="Created at"
                    ),
                ),
                (
                    "lifted_at",
                    models.DateTimeField(
                        blank=True, null=True, verbose_name="Lifted at"
                    ),
                ),
                (
                    "lift_reason",
                    models.TextField(blank=True, verbose_name="Reason for lifting"),
                ),
                (
                    "created_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="Created by",
                    ),
                ),
                (
                    "lifted_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="Lifted by",
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="fleetops_srp_bans",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="User",
                    ),
                ),
            ],
            options={
                "verbose_name": "SRP ban",
                "verbose_name_plural": "SRP bans",
                "ordering": ["-created_at", "-pk"],
                "default_permissions": (),
                "indexes": [
                    models.Index(
                        fields=["user", "expires_at"], name="fleetops_srpban_user_exp"
                    )
                ],
            },
        ),
    ]
