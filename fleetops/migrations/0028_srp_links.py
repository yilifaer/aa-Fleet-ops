# AA FleetOps 0.3.0b1: SRP links, schema. A link can be a standing link (no fleet; kind, name, category, date, doctrine,
# after-action report, completion, automatic closing, daily key, origin and external ID), so the fleet becomes
# optional. The category gets the settings of its daily link and the SRP settings two switches. Everything is
# added; the only change to an existing column is that the fleet may be empty. Going back makes it required again
# (see 0029, which refuses to go back while a link without a fleet has claims).

import django.core.validators
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("fleetops", "0027_srp_self_payment_data"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AlterModelOptions(
            name="srpfleetlink",
            options={
                "ordering": ["-created_at"],
                "verbose_name": "SRP link",
                "verbose_name_plural": "SRP links",
            },
        ),
        migrations.AddField(
            model_name="srpcategory",
            name="auto_close_after_hours",
            field=models.PositiveSmallIntegerField(
                blank=True,
                help_text="A standing link of this category stops taking claims this many hours after 00:00 EVE of its date. Empty = it is never closed automatically.",
                null=True,
                validators=[
                    django.core.validators.MinValueValidator(1),
                    django.core.validators.MaxValueValidator(720),
                ],
                verbose_name="Close links after (hours)",
            ),
        ),
        migrations.AddField(
            model_name="srpcategory",
            name="auto_create_daily",
            field=models.BooleanField(
                default=False,
                help_text="Create this category's standing SRP link automatically once per EVE day.",
                verbose_name="Make a standing link every day",
            ),
        ),
        migrations.AddField(
            model_name="srpcategory",
            name="daily_link_name",
            field=models.CharField(
                default="{category} {date}",
                help_text="The name of a new standing link of this category when none is typed. Use {category} and {date} as placeholders.",
                max_length=255,
                verbose_name="Name template of links",
            ),
        ),
        migrations.AddField(
            model_name="srpcategory",
            name="default_doctrine_text",
            field=models.CharField(
                blank=True,
                help_text="Filled in as the doctrine of new standing links of this category.",
                max_length=255,
                verbose_name="Default doctrine text",
            ),
        ),
        migrations.AddField(
            model_name="srpfleetlink",
            name="aar_url",
            field=models.CharField(
                blank=True,
                help_text="A link to the after-action report. Only http and https addresses are accepted.",
                max_length=500,
                validators=[
                    django.core.validators.URLValidator(schemes=["http", "https"])
                ],
                verbose_name="After-action report",
            ),
        ),
        migrations.AddField(
            model_name="srpfleetlink",
            name="auto_close_at",
            field=models.DateTimeField(
                blank=True,
                db_index=True,
                null=True,
                verbose_name="Closes automatically at",
            ),
        ),
        migrations.AddField(
            model_name="srpfleetlink",
            name="auto_created",
            field=models.BooleanField(default=False, verbose_name="Made automatically"),
        ),
        migrations.AddField(
            model_name="srpfleetlink",
            name="category",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="links",
                to="fleetops.srpcategory",
                verbose_name="Category",
            ),
        ),
        migrations.AddField(
            model_name="srpfleetlink",
            name="completed_at",
            field=models.DateTimeField(
                blank=True, null=True, verbose_name="Completed at"
            ),
        ),
        migrations.AddField(
            model_name="srpfleetlink",
            name="completed_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="+",
                to=settings.AUTH_USER_MODEL,
                verbose_name="Completed by",
            ),
        ),
        migrations.AddField(
            model_name="srpfleetlink",
            name="daily_key",
            field=models.CharField(
                blank=True,
                max_length=64,
                null=True,
                unique=True,
                verbose_name="Daily key",
            ),
        ),
        migrations.AddField(
            model_name="srpfleetlink",
            name="doctrine_text",
            field=models.CharField(blank=True, max_length=255, verbose_name="Doctrine"),
        ),
        migrations.AddField(
            model_name="srpfleetlink",
            name="external_id",
            field=models.CharField(
                blank=True,
                max_length=64,
                null=True,
                unique=True,
                verbose_name="External ID",
            ),
        ),
        migrations.AddField(
            model_name="srpfleetlink",
            name="kind",
            field=models.CharField(
                choices=[("fleet", "Fleet"), ("standing", "Standing")],
                db_index=True,
                default="fleet",
                max_length=10,
                verbose_name="Kind",
            ),
        ),
        migrations.AddField(
            model_name="srpfleetlink",
            name="link_date",
            field=models.DateField(
                blank=True, db_index=True, null=True, verbose_name="Date (EVE)"
            ),
        ),
        migrations.AddField(
            model_name="srpfleetlink",
            name="name",
            field=models.CharField(blank=True, max_length=255, verbose_name="Name"),
        ),
        migrations.AddField(
            model_name="srpfleetlink",
            name="origin",
            field=models.CharField(
                choices=[("fleetops", "FleetOps"), ("aasrp", "Imported from aa-srp")],
                default="fleetops",
                max_length=10,
                verbose_name="Origin",
            ),
        ),
        migrations.AddField(
            model_name="srpsettings",
            name="auto_complete_links",
            field=models.BooleanField(
                default=False,
                help_text="A closed link is marked completed as soon as none of its claims is waiting for review or payment.",
                verbose_name="Complete SRP links automatically",
            ),
        ),
        migrations.AddField(
            model_name="srpsettings",
            name="category_claims_without_link",
            field=models.BooleanField(
                default=True,
                help_text="A member who chooses a category on a day without an open standing link of that category can still submit; the claim only records the category. Switch off to require the link.",
                verbose_name="A category claim needs no standing link",
            ),
        ),
        migrations.AlterField(
            model_name="srpfleetlink",
            name="operation",
            field=models.OneToOneField(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="srp_link",
                to="fleetops.fleetoperation",
                verbose_name="Operation",
            ),
        ),
    ]
