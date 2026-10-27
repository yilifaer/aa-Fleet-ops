# AA FleetOps 0.2.0b2: group pings and group fleets. The leaders of an Alliance Auth group can be given a ping channel
# of their own, and optionally run fleets with PAP for fleet types chosen per group. Existing ping channels stay open to
# every FC and existing fleets belong to no group, so today's behaviour does not change. There is no foreign key to
# the group management app: leaders are read from Alliance Auth when they are needed.

import django.db.models.deletion
import django.utils.timezone
import uuid
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("auth", "0012_alter_user_first_name_max_length"),
        ("fleetops", "0020_b2_data"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="discordwebhook",
            name="allowed_groups",
            field=models.ManyToManyField(
                blank=True,
                help_text="Leaders of these groups may send pings here.",
                related_name="fleetops_ping_channels",
                to="auth.group",
                verbose_name="Groups",
            ),
        ),
        migrations.AddField(
            model_name="discordwebhook",
            name="fcs_allowed",
            field=models.BooleanField(
                default=True,
                help_text="Untick to reserve the channel for the leaders of the groups below. FC leads (manage_fleets) can always use it. Only applies while the channel is assigned to at least one group.",
                verbose_name="Alliance FCs can use this channel",
            ),
        ),
        migrations.AddField(
            model_name="fleetoperation",
            name="group",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="fleetops_operations",
                to="auth.group",
                verbose_name="Group",
            ),
        ),
        migrations.AddField(
            model_name="fleetoperation",
            name="group_name",
            field=models.CharField(blank=True, max_length=150, verbose_name="Group name"),
        ),
        migrations.AlterField(
            model_name="messagetemplate",
            name="template_type",
            field=models.CharField(
                choices=[("ping", "Ping"), ("motd", "MOTD"), ("standalone", "Ping without fleet")],
                max_length=20,
                verbose_name="Template type",
            ),
        ),
        migrations.CreateModel(
            name="GroupAccess",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                (
                    "mode",
                    models.CharField(
                        choices=[("ping", "Ping only"), ("fleets", "Pings and fleets with PAP")],
                        default="ping",
                        max_length=10,
                        verbose_name="Leaders may",
                    ),
                ),
                (
                    "srp_enabled",
                    models.BooleanField(
                        default=False,
                        help_text="Group fleets get an SRP link like alliance fleets.",
                        verbose_name="SRP link for group fleets",
                    ),
                ),
                ("is_active", models.BooleanField(default=True, verbose_name="Is active")),
                ("note", models.CharField(blank=True, max_length=255, verbose_name="Note")),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="Created at")),
                ("updated_at", models.DateTimeField(auto_now=True, verbose_name="Updated at")),
                (
                    "fleet_types",
                    models.ManyToManyField(
                        blank=True,
                        help_text="Fleet types the leaders may start. Only used for pings and fleets with PAP.",
                        related_name="group_accesses",
                        to="fleetops.fleettype",
                        verbose_name="Fleet types",
                    ),
                ),
                (
                    "group",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="fleetops_access",
                        to="auth.group",
                        verbose_name="Group",
                    ),
                ),
            ],
            options={
                "verbose_name": "group access",
                "verbose_name_plural": "group accesses",
                "ordering": ["group__name"],
            },
        ),
        migrations.CreateModel(
            name="StandalonePing",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("uuid", models.UUIDField(default=uuid.uuid4, editable=False, unique=True, verbose_name="UUID")),
                ("request_id", models.UUIDField(editable=False, unique=True, verbose_name="Request ID")),
                ("sender_name", models.CharField(blank=True, max_length=255, verbose_name="Sender name")),
                ("group_name", models.CharField(blank=True, max_length=150, verbose_name="Group name")),
                ("channel_name", models.CharField(max_length=100, verbose_name="Ping channel name")),
                (
                    "mention_mode",
                    models.CharField(
                        choices=[("none", "No ping"), ("everyone", "@everyone"), ("here", "@here"), ("roles", "Roles")],
                        default="none",
                        max_length=10,
                        verbose_name="Ping mention mode",
                    ),
                ),
                ("mention_roles", models.JSONField(blank=True, default=list, verbose_name="Ping roles")),
                ("fleet_time", models.DateTimeField(blank=True, null=True, verbose_name="Fleet time")),
                ("formup", models.CharField(blank=True, max_length=255, verbose_name="Formup")),
                ("doctrine_name", models.CharField(blank=True, max_length=255, verbose_name="Doctrine name")),
                ("doctrine_source", models.CharField(blank=True, max_length=50, verbose_name="Doctrine source")),
                (
                    "doctrine_external_id",
                    models.CharField(blank=True, max_length=255, verbose_name="Doctrine external ID"),
                ),
                ("additional_message", models.TextField(verbose_name="Additional message")),
                ("text", models.TextField(blank=True, verbose_name="Ping text")),
                ("display_text", models.TextField(blank=True, verbose_name="Ping text as read")),
                (
                    "status",
                    models.CharField(
                        choices=[("pending", "Pending"), ("sent", "Sent"), ("failed", "Failed")],
                        db_index=True,
                        default="pending",
                        max_length=10,
                        verbose_name="Status",
                    ),
                ),
                ("result_message", models.TextField(blank=True, verbose_name="Result")),
                ("http_status", models.PositiveSmallIntegerField(blank=True, null=True, verbose_name="HTTP status")),
                (
                    "created_at",
                    models.DateTimeField(
                        db_index=True, default=django.utils.timezone.now, verbose_name="Created at"
                    ),
                ),
                ("sent_at", models.DateTimeField(blank=True, null=True, verbose_name="Sent at")),
                (
                    "channel",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="standalone_pings",
                        to="fleetops.discordwebhook",
                        verbose_name="Ping channel",
                    ),
                ),
                (
                    "comms",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to="fleetops.commspreset",
                        verbose_name="Comms",
                    ),
                ),
                (
                    "group",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="fleetops_pings",
                        to="auth.group",
                        verbose_name="Group",
                    ),
                ),
                (
                    "sender",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="fleetops_pings",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="Sender",
                    ),
                ),
                (
                    "template",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to="fleetops.messagetemplate",
                        verbose_name="Template",
                    ),
                ),
            ],
            options={
                "verbose_name": "ping without fleet",
                "verbose_name_plural": "pings without fleet",
                "ordering": ["-created_at", "-pk"],
                "indexes": [models.Index(fields=["channel", "created_at"], name="fleetops_ping_channel_time_idx")],
            },
        ),
    ]
