# AA FleetOps 0.2.0b2: the MOTD warning length setting, the time of the last incentive calculation and the note that
# goes with a fleet sent back for approval. New columns with defaults; nothing is rewritten.

import django.core.validators
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("fleetops", "0018_translatable_labels"),
    ]

    operations = [
        migrations.AddField(
            model_name="fleetopssettings",
            name="motd_warning_length",
            field=models.PositiveIntegerField(
                default=2000,
                help_text="Warn in the start preview when the MOTD, tags included, is longer than this many characters. The game's limit is not documented; MOTDs of about 1,700 characters are known to work. 0 = no warning.",
                validators=[django.core.validators.MaxValueValidator(100000)],
                verbose_name="MOTD warning length",
            ),
        ),
        migrations.AddField(
            model_name="incentiveperiod",
            name="calculated_at",
            field=models.DateTimeField(blank=True, null=True, verbose_name="Calculated at"),
        ),
        migrations.AddField(
            model_name="fleetoperation",
            name="review_request_note",
            field=models.CharField(blank=True, max_length=255, verbose_name="Resubmission note"),
        ),
    ]
