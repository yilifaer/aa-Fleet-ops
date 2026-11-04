# AA FleetOps 0.3.0b1: SRP categories. New table, category columns on claims, payout rules and the SRP settings,
# the payout rule constraint with the category, and the zKillboard total value as the default value source.

import django.core.validators
import django.db.models.deletion
from decimal import Decimal
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("fleetops", "0023_group_members_are_leaders_help"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="SrpCategory",
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
                    "name",
                    models.CharField(max_length=100, unique=True, verbose_name="Name"),
                ),
                (
                    "key",
                    models.SlugField(
                        help_text="Short identifier for links and exports. Letters, digits, hyphens and underscores.",
                        max_length=40,
                        unique=True,
                        verbose_name="Key",
                    ),
                ),
                (
                    "description",
                    models.CharField(
                        blank=True, max_length=255, verbose_name="Description"
                    ),
                ),
                (
                    "is_system",
                    models.BooleanField(
                        default=False, editable=False, verbose_name="Built in"
                    ),
                ),
                (
                    "member_selectable",
                    models.BooleanField(
                        default=True,
                        help_text="Members can pick this category for a loss outside a fleet.",
                        verbose_name="Members can choose it",
                    ),
                ),
                (
                    "percentage",
                    models.DecimalField(
                        blank=True,
                        decimal_places=2,
                        help_text="Share of the loss value paid for ships not in the payout table. Empty = the percentage of the SRP settings. 0 = no automatic amount in this category.",
                        max_digits=5,
                        null=True,
                        validators=[
                            django.core.validators.MinValueValidator(Decimal("0")),
                            django.core.validators.MaxValueValidator(Decimal("100")),
                        ],
                        verbose_name="Payout percentage",
                    ),
                ),
                (
                    "loss_cap_mode",
                    models.CharField(
                        choices=[
                            (
                                "default",
                                "Use the in-fleet or out-of-fleet cap of the SRP settings",
                            ),
                            ("none", "No per-loss cap"),
                            ("custom", "Own per-loss cap"),
                        ],
                        default="default",
                        max_length=10,
                        verbose_name="Per-loss cap",
                    ),
                ),
                (
                    "loss_cap",
                    models.PositiveBigIntegerField(
                        blank=True,
                        help_text="Maximum per loss in this category. Used with “Own per-loss cap”.",
                        null=True,
                        validators=[
                            django.core.validators.MinValueValidator(1),
                            django.core.validators.MaxValueValidator(1000000000000000),
                        ],
                        verbose_name="Own per-loss cap",
                    ),
                ),
                (
                    "cap_applies_to_rules",
                    models.BooleanField(
                        default=True,
                        help_text="Switch off to pay the fixed amounts of the payout table in full.",
                        verbose_name="Per-loss cap also limits payout table amounts",
                    ),
                ),
                (
                    "personal_monthly_cap",
                    models.PositiveBigIntegerField(
                        blank=True,
                        help_text="Maximum approved SRP per player in this category per EVE month. Empty = no cap.",
                        null=True,
                        validators=[
                            django.core.validators.MinValueValidator(1),
                            django.core.validators.MaxValueValidator(1000000000000000),
                        ],
                        verbose_name="Personal monthly cap",
                    ),
                ),
                (
                    "corporation_monthly_cap",
                    models.PositiveBigIntegerField(
                        blank=True,
                        help_text="Maximum approved SRP per corporation in this category per EVE month. Empty = no cap.",
                        null=True,
                        validators=[
                            django.core.validators.MinValueValidator(1),
                            django.core.validators.MaxValueValidator(1000000000000000),
                        ],
                        verbose_name="Corporation monthly cap",
                    ),
                ),
                ("is_active", models.BooleanField(default=True, verbose_name="Active")),
                (
                    "sort_order",
                    models.PositiveIntegerField(default=0, verbose_name="Sort order"),
                ),
                (
                    "updated_at",
                    models.DateTimeField(auto_now=True, verbose_name="Updated at"),
                ),
            ],
            options={
                "verbose_name": "SRP category",
                "verbose_name_plural": "SRP categories",
                "ordering": ["sort_order", "name"],
                "default_permissions": (),
            },
        ),
        migrations.RemoveConstraint(
            model_name="srppayoutrule",
            name="fleetops_unique_srp_rule",
        ),
        migrations.AddField(
            model_name="srppayoutrule",
            name="category_key",
            field=models.PositiveBigIntegerField(
                db_index=True, default=0, editable=False, verbose_name="Category key"
            ),
        ),
        migrations.AddField(
            model_name="srpsettings",
            name="out_of_fleet_category_required",
            field=models.BooleanField(
                default=False,
                help_text="Without a default category, a claim outside a fleet needs a category.",
                verbose_name="Members must choose a category for losses outside a fleet",
            ),
        ),
        migrations.AlterField(
            model_name="srpclaimevent",
            name="action",
            field=models.CharField(
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
                    ("recategorize", "Category changed"),
                    ("recalculate", "Recalculated"),
                    ("recheck", "Fleet presence checked again"),
                    ("value_update", "Loss value updated"),
                    ("note", "Internal note"),
                ],
                max_length=30,
                verbose_name="Action",
            ),
        ),
        migrations.AlterField(
            model_name="srpsettings",
            name="value_source",
            field=models.CharField(
                choices=[
                    (
                        "zkill_fitted",
                        "zKillboard fitted value (ship, modules, drones, implants)",
                    ),
                    (
                        "zkill_total",
                        "zKillboard total value (includes cargo, recommended)",
                    ),
                    ("esi_estimate", "ESI market estimate"),
                ],
                default="zkill_total",
                help_text="Where the loss value comes from. The ESI market estimate is also the fallback when zKillboard has no value. The total value includes cargo; aa-srp pays the total value.",
                max_length=20,
                verbose_name="Value source",
            ),
        ),
        migrations.AddField(
            model_name="srpcategory",
            name="fleet_types",
            field=models.ManyToManyField(
                blank=True,
                help_text="Claims through the fleets of these types get this category. Empty = no mapping.",
                related_name="srp_categories",
                to="fleetops.fleettype",
                verbose_name="Fleet types",
            ),
        ),
        migrations.AddField(
            model_name="srpclaim",
            name="category",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="claims",
                to="fleetops.srpcategory",
                verbose_name="Category",
            ),
        ),
        migrations.AddField(
            model_name="srppayoutrule",
            name="category",
            field=models.ForeignKey(
                blank=True,
                help_text="The rule only applies to claims of this category. Empty = all categories.",
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="payout_rules",
                to="fleetops.srpcategory",
                verbose_name="Category",
            ),
        ),
        migrations.AddField(
            model_name="srpsettings",
            name="default_category",
            field=models.ForeignKey(
                blank=True,
                help_text="The category of a loss outside a fleet when the member chooses none. Empty = no category.",
                limit_choices_to={"is_active": True, "member_selectable": True},
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="+",
                to="fleetops.srpcategory",
                verbose_name="Default category",
            ),
        ),
        migrations.AddIndex(
            model_name="srpclaim",
            index=models.Index(
                fields=["category", "killmail_time"], name="fleetops_srp_cat_time_idx"
            ),
        ),
        migrations.AddConstraint(
            model_name="srppayoutrule",
            constraint=models.UniqueConstraint(
                fields=("ship_type_id", "doctrine_key", "scope", "category_key"),
                name="fleetops_unique_srp_rule_v2",
                violation_error_message="A rule for this ship, doctrine and scope already exists.",
            ),
        ),
    ]
