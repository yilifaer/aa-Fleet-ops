# AA FleetOps 0.1.0a6: ping channels and roles, scheduled fleets, comms join links and name caches

import django.core.validators
import django.db.models.deletion
from django.db import migrations, models

import fleetops.validators

STATUS_CHOICES = [
    ("draft", "Draft"),
    ("scheduled", "Scheduled"),
    ("starting", "Starting"),
    ("active", "Active"),
    ("ending", "Ending"),
    ("closed", "Closed"),
    ("cancelled", "Cancelled"),
    ("error", "Error"),
]
MENTION_MODE_CHOICES = [("none", "No ping"), ("everyone", "@everyone"), ("here", "@here"), ("roles", "Roles")]


class Migration(migrations.Migration):

    dependencies = [
        ("fleetops", "0007_end_reasons_and_step_history"),
    ]

    operations = [
        migrations.CreateModel(
            name="CharacterAffiliation",
            fields=[
                ("character_id", models.BigIntegerField(primary_key=True, serialize=False)),
                ("corporation_id", models.BigIntegerField(blank=True, null=True)),
                ("alliance_id", models.BigIntegerField(blank=True, null=True)),
                ("faction_id", models.BigIntegerField(blank=True, null=True)),
                ("updated_at", models.DateTimeField(db_index=True)),
            ],
        ),
        migrations.CreateModel(
            name="EveEntityName",
            fields=[
                ("id", models.BigIntegerField(primary_key=True, serialize=False)),
                ("category", models.CharField(blank=True, help_text='ESI category, or "unresolved".', max_length=20)),
                ("name", models.CharField(blank=True, max_length=255)),
                ("updated_at", models.DateTimeField(db_index=True)),
            ],
            options={
                "verbose_name": "EVE entity name",
            },
        ),
        migrations.CreateModel(
            name="PingRole",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(help_text="Role name without the leading @.", max_length=100)),
                (
                    "role_id",
                    models.BigIntegerField(
                        help_text="Discord role ID.",
                        unique=True,
                        validators=[
                            django.core.validators.MinValueValidator(1),
                            django.core.validators.MaxValueValidator(9223372036854775807),
                        ],
                    ),
                ),
                (
                    "is_active",
                    models.BooleanField(default=True, help_text="FCs can pick this role when they send a ping."),
                ),
                (
                    "source",
                    models.CharField(
                        choices=[("manual", "Manual"), ("discord", "Synced from Discord")],
                        default="manual",
                        max_length=10,
                    ),
                ),
                ("missing_from_discord", models.BooleanField(default=False)),
                ("last_synced_at", models.DateTimeField(blank=True, null=True)),
                ("sort_order", models.PositiveIntegerField(default=0)),
            ],
            options={
                "ordering": ["sort_order", "name"],
            },
        ),
        migrations.AlterModelOptions(
            name="discordwebhook",
            options={"ordering": ["sort_order", "name"], "verbose_name": "Ping channel"},
        ),
        migrations.AddField(
            model_name="channelpreset",
            name="ingame_channel_id",
            field=models.CharField(
                blank=True,
                max_length=64,
                validators=[
                    django.core.validators.RegexValidator(
                        "^player_[0-9a-fA-F]{32}$", "Enter an in-game channel id such as player_<32 hex digits>."
                    )
                ],
            ),
        ),
        migrations.AlterField(
            model_name="channelpreset",
            name="channel_value",
            field=models.CharField(help_text="Channel name shown in the MOTD", max_length=255, verbose_name="Channel name"),
        ),
        # Unique and with a default only after 0009 has given every existing preset its own token:
        # MySQL and MariaDB cannot roll back a failed schema change.
        migrations.AddField(
            model_name="commspreset",
            name="join_token",
            field=models.UUIDField(editable=False, null=True),
        ),
        migrations.AlterField(
            model_name="commspreset",
            name="voice_url",
            field=models.CharField(
                blank=True,
                help_text="Supports mumble://, ts3server://, discord:// and https:// voice links. "
                "Do not include a personal user name or password.",
                max_length=500,
                validators=[fleetops.validators.validate_voice_url],
            ),
        ),
        migrations.AddField(
            model_name="discordwebhook",
            name="allow_everyone",
            field=models.BooleanField(default=False, help_text="FCs may ping @everyone in this channel."),
        ),
        migrations.AddField(
            model_name="discordwebhook",
            name="allow_here",
            field=models.BooleanField(default=False, help_text="FCs may ping @here in this channel."),
        ),
        migrations.AddField(
            model_name="discordwebhook",
            name="allowed_roles",
            field=models.ManyToManyField(
                blank=True,
                help_text="Leave empty to allow every active role.",
                related_name="channels",
                to="fleetops.pingrole",
            ),
        ),
        migrations.AddField(
            model_name="discordwebhook",
            name="sort_order",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AlterField(
            model_name="fleetoperation",
            name="status",
            field=models.CharField(choices=STATUS_CHOICES, db_index=True, default="draft", max_length=20),
        ),
        migrations.AddField(
            model_name="fleetoperation",
            name="ping_channel",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="fleetops_operations",
                to="fleetops.discordwebhook",
            ),
        ),
        migrations.AddField(
            model_name="fleetoperation",
            name="ping_channel_name",
            field=models.CharField(blank=True, max_length=100),
        ),
        migrations.AddField(
            model_name="fleetoperation",
            name="ping_mention_mode",
            field=models.CharField(choices=MENTION_MODE_CHOICES, default="none", max_length=10),
        ),
        migrations.AddField(
            model_name="fleetoperation",
            name="ping_roles",
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.AddField(
            model_name="fleetoperation",
            name="doctrine_fits",
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.AlterField(
            model_name="fleetopssettings",
            name="srp_provider",
            field=models.CharField(
                default="auto",
                help_text="SRP provider key. 'auto' uses aasrp, then Alliance Auth SRP, then any other registered provider.",
                max_length=50,
            ),
        ),
    ]
