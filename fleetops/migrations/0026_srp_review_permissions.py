# AA FleetOps 0.3.0b1: SRP review and payment permissions. Two new permissions (review_srp, pay_srp; they are
# granted to nobody), the settings "SRP payers may mark their own claims paid" and "The approver of a claim may
# not pay it", and the new wording of "SRP reviewers may decide on their own claims". Only additions and wording;
# reversing 0026 drops the two columns and the permission options return to those of 0025.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("fleetops", "0025_srp_categories_data"),
    ]

    operations = [
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
                    ("review_srp", "Can review SRP claims"),
                    ("pay_srp", "Can pay SRP claims"),
                ],
                "verbose_name": "FleetOps setting",
                "verbose_name_plural": "FleetOps settings",
            },
        ),
        migrations.AddField(
            model_name="srpsettings",
            name="allow_self_payment",
            field=models.BooleanField(
                default=False,
                help_text="By default another payer pays a payer's own claims. Allow this for small alliances.",
                verbose_name="SRP payers may mark their own claims paid",
            ),
        ),
        migrations.AddField(
            model_name="srpsettings",
            name="require_different_payer",
            field=models.BooleanField(
                default=False,
                help_text="Four-eyes rule: the person who approved a claim cannot mark it paid. Undoing a payment is not affected.",
                verbose_name="The approver of a claim may not pay it",
            ),
        ),
        migrations.AlterField(
            model_name="srpsettings",
            name="allow_self_review",
            field=models.BooleanField(
                default=False,
                help_text="By default another reviewer decides on a reviewer's own claims. Allow this for small alliances.",
                verbose_name="SRP reviewers may decide on their own claims",
            ),
        ),
    ]
