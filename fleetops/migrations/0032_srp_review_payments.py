# AA FleetOps 0.3.0b1: member edits, payment batches, insurance and review information, schema. Adds the revision
# number and edit time of a claim, the payment batch table and a claim's batch, the table of insurance prices,
# the attackers and damage of a killmail and five settings (members may change pending claims and swap the loss,
# insurance shown at the platinum level, 90 days of rejection history). Nothing is filled in.
# Going back drops the tables and columns: batches, insurance prices, the stored attackers and the revision
# numbers are lost; claims keep everything else.

import django.core.validators
import django.db.models.deletion
import django.utils.timezone
import uuid
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("fleetops", "0031_srp_discovery"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="SrpInsurancePrice",
            fields=[
                (
                    "ship_type_id",
                    models.BigIntegerField(
                        primary_key=True, serialize=False, verbose_name="Ship type ID"
                    ),
                ),
                (
                    "levels",
                    models.JSONField(blank=True, default=dict, verbose_name="Levels"),
                ),
                (
                    "fetched_at",
                    models.DateTimeField(
                        default=django.utils.timezone.now, verbose_name="Fetched at"
                    ),
                ),
            ],
            options={
                "verbose_name": "SRP insurance price",
                "verbose_name_plural": "SRP insurance prices",
                "ordering": ["ship_type_id"],
                "default_permissions": (),
            },
        ),
        migrations.AddField(
            model_name="srpclaim",
            name="member_edited_at",
            field=models.DateTimeField(
                blank=True, null=True, verbose_name="Changed by the claimant at"
            ),
        ),
        migrations.AddField(
            model_name="srpclaim",
            name="member_revision",
            field=models.PositiveIntegerField(
                default=0, verbose_name="Member revision"
            ),
        ),
        migrations.AddField(
            model_name="srpkillmail",
            name="attackers",
            field=models.JSONField(blank=True, default=list, verbose_name="Attackers"),
        ),
        migrations.AddField(
            model_name="srpkillmail",
            name="damage_taken",
            field=models.PositiveBigIntegerField(
                blank=True, null=True, verbose_name="Damage taken"
            ),
        ),
        migrations.AddField(
            model_name="srpsettings",
            name="insurance_level",
            field=models.CharField(
                choices=[
                    ("basic", "Basic"),
                    ("standard", "Standard"),
                    ("bronze", "Bronze"),
                    ("silver", "Silver"),
                    ("gold", "Gold"),
                    ("platinum", "Platinum"),
                ],
                default="platinum",
                help_text="The level the estimate is made for.",
                max_length=10,
                verbose_name="Insurance level shown",
            ),
        ),
        migrations.AddField(
            model_name="srpsettings",
            name="member_edit_enabled",
            field=models.BooleanField(
                default=True,
                help_text="A member can change the comment, the category or link and the loss of a claim that is still pending. Not part of the policy snapshot.",
                verbose_name="Members can change their pending claims",
            ),
        ),
        migrations.AddField(
            model_name="srpsettings",
            name="member_swap_loss_enabled",
            field=models.BooleanField(
                default=True,
                help_text="Only in force while members can change their pending claims.",
                verbose_name="Members can replace the loss of a pending claim",
            ),
        ),
        migrations.AddField(
            model_name="srpsettings",
            name="review_history_days",
            field=models.PositiveSmallIntegerField(
                default=90,
                help_text="The review page counts the member's rejections of the last days. 0 = all.",
                validators=[django.core.validators.MaxValueValidator(3650)],
                verbose_name="Days of rejection history shown to reviewers",
            ),
        ),
        migrations.AddField(
            model_name="srpsettings",
            name="show_insurance",
            field=models.BooleanField(
                default=True,
                help_text="Show the net insurance payout next to the SRP amount, from the insurance prices ESI publishes. Informational only; it changes no amount, limit or payment.",
                verbose_name="Show the insurance estimate",
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
                    ("member_edit", "Changed by the claimant"),
                    ("note", "Internal note"),
                ],
                max_length=30,
                verbose_name="Action",
            ),
        ),
        migrations.CreateModel(
            name="SrpPaymentBatch",
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
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        unique=True,
                        verbose_name="UUID",
                    ),
                ),
                (
                    "created_by_name",
                    models.CharField(
                        blank=True, max_length=255, verbose_name="Created by (name)"
                    ),
                ),
                (
                    "created_at",
                    models.DateTimeField(
                        db_index=True,
                        default=django.utils.timezone.now,
                        verbose_name="Created at",
                    ),
                ),
                (
                    "claim_count",
                    models.PositiveIntegerField(default=0, verbose_name="Claims"),
                ),
                (
                    "payee_count",
                    models.PositiveIntegerField(default=0, verbose_name="Payees"),
                ),
                (
                    "total",
                    models.PositiveBigIntegerField(default=0, verbose_name="Total"),
                ),
                (
                    "note",
                    models.CharField(
                        blank=True,
                        help_text="Shown to the members.",
                        max_length=255,
                        verbose_name="Note",
                    ),
                ),
                (
                    "filters",
                    models.JSONField(blank=True, default=dict, verbose_name="Filters"),
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
            ],
            options={
                "verbose_name": "SRP payment batch",
                "verbose_name_plural": "SRP payment batches",
                "ordering": ["-created_at", "-pk"],
                "default_permissions": (),
            },
        ),
        migrations.AddField(
            model_name="srpclaim",
            name="payment_batch",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="claims",
                to="fleetops.srppaymentbatch",
                verbose_name="Payment batch",
            ),
        ),
    ]
