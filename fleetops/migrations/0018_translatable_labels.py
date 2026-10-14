# AA FleetOps 0.2.0b1: translatable labels. Verbose names, help texts and choice labels of the models are now
# translated; this records them. It changes no table and no column, so it runs no SQL.

import django.core.validators
import django.db.models.deletion
import django.utils.timezone
import fleetops.models
import fleetops.validators
import uuid
from decimal import Decimal
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("fleetops", "0017_srp_data"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AlterModelOptions(
            name="attendancerecord",
            options={
                "ordering": ["character_name", "created_at"],
                "verbose_name": "PAP record",
                "verbose_name_plural": "PAP records",
            },
        ),
        migrations.AlterModelOptions(
            name="auditlog",
            options={
                "ordering": ["-created_at"],
                "verbose_name": "audit log",
                "verbose_name_plural": "audit logs",
            },
        ),
        migrations.AlterModelOptions(
            name="channelpreset",
            options={
                "ordering": ["channel_type", "name"],
                "verbose_name": "channel preset",
                "verbose_name_plural": "channel presets",
            },
        ),
        migrations.AlterModelOptions(
            name="characteraffiliation",
            options={
                "verbose_name": "character affiliation",
                "verbose_name_plural": "character affiliations",
            },
        ),
        migrations.AlterModelOptions(
            name="commspreset",
            options={
                "ordering": ["name"],
                "verbose_name": "comms preset",
                "verbose_name_plural": "comms presets",
            },
        ),
        migrations.AlterModelOptions(
            name="discordwebhook",
            options={
                "ordering": ["sort_order", "name"],
                "verbose_name": "Ping channel",
                "verbose_name_plural": "Ping channels",
            },
        ),
        migrations.AlterModelOptions(
            name="eveentityname",
            options={
                "verbose_name": "EVE entity name",
                "verbose_name_plural": "EVE entity names",
            },
        ),
        migrations.AlterModelOptions(
            name="fleetmemberevent",
            options={
                "ordering": ["-created_at"],
                "verbose_name": "fleet member event",
                "verbose_name_plural": "fleet member events",
            },
        ),
        migrations.AlterModelOptions(
            name="fleetmemberstate",
            options={
                "ordering": ["character_name"],
                "verbose_name": "fleet member state",
                "verbose_name_plural": "fleet member states",
            },
        ),
        migrations.AlterModelOptions(
            name="fleetoperation",
            options={
                "ordering": ["-started_at", "-created_at"],
                "verbose_name": "fleet operation",
                "verbose_name_plural": "fleet operations",
            },
        ),
        migrations.AlterModelOptions(
            name="fleetstartpreset",
            options={
                "ordering": ["name"],
                "verbose_name": "fleet start preset",
                "verbose_name_plural": "fleet start presets",
            },
        ),
        migrations.AlterModelOptions(
            name="fleetstructuretemplate",
            options={
                "ordering": ["sort_order", "name"],
                "verbose_name": "fleet structure template",
                "verbose_name_plural": "fleet structure templates",
            },
        ),
        migrations.AlterModelOptions(
            name="fleettype",
            options={
                "ordering": ["sort_order", "name"],
                "verbose_name": "fleet type",
                "verbose_name_plural": "fleet types",
            },
        ),
        migrations.AlterModelOptions(
            name="incentiveperiod",
            options={
                "ordering": ["-year", "-month"],
                "verbose_name": "incentive period",
                "verbose_name_plural": "incentive periods",
            },
        ),
        migrations.AlterModelOptions(
            name="messagetemplate",
            options={
                "ordering": ["template_type", "name"],
                "verbose_name": "message template",
                "verbose_name_plural": "message templates",
            },
        ),
        migrations.AlterModelOptions(
            name="monthlyfcstatistic",
            options={
                "ordering": ["-total_points", "fc_user__username"],
                "verbose_name": "monthly FC statistic",
                "verbose_name_plural": "monthly FC statistics",
            },
        ),
        migrations.AlterModelOptions(
            name="multipliertier",
            options={
                "ordering": ["threshold_minutes"],
                "verbose_name": "multiplier tier",
                "verbose_name_plural": "multiplier tiers",
            },
        ),
        migrations.AlterModelOptions(
            name="operationaction",
            options={
                "ordering": ["executed_at"],
                "verbose_name": "operation action",
                "verbose_name_plural": "operation actions",
            },
        ),
        migrations.AlterModelOptions(
            name="operationactionattempt",
            options={
                "ordering": ["-created_at", "-pk"],
                "verbose_name": "operation action attempt",
                "verbose_name_plural": "operation action attempts",
            },
        ),
        migrations.AlterModelOptions(
            name="operationroleassignment",
            options={
                "ordering": ["role", "character_name"],
                "verbose_name": "operation role assignment",
                "verbose_name_plural": "operation role assignments",
            },
        ),
        migrations.AlterModelOptions(
            name="paplink",
            options={
                "ordering": ["-created_at"],
                "verbose_name": "PAP link",
                "verbose_name_plural": "PAP links",
            },
        ),
        migrations.AlterModelOptions(
            name="papregistration",
            options={
                "ordering": ["registered_at"],
                "verbose_name": "PAP registration",
                "verbose_name_plural": "PAP registrations",
            },
        ),
        migrations.AlterModelOptions(
            name="pingrole",
            options={
                "ordering": ["sort_order", "name"],
                "verbose_name": "ping role",
                "verbose_name_plural": "ping roles",
            },
        ),
        migrations.AlterModelOptions(
            name="pingtarget",
            options={
                "ordering": ["name"],
                "verbose_name": "ping target",
                "verbose_name_plural": "ping targets",
            },
        ),
        migrations.AlterModelOptions(
            name="shipfunction",
            options={
                "ordering": ["sort_order", "name"],
                "verbose_name": "ship function",
                "verbose_name_plural": "ship functions",
            },
        ),
        migrations.AlterModelOptions(
            name="shipfunctionrule",
            options={
                "ordering": ["function__sort_order", "kind", "label", "target_id"],
                "verbose_name": "ship function rule",
                "verbose_name_plural": "ship function rules",
            },
        ),
        migrations.AlterModelOptions(
            name="specialrole",
            options={
                "ordering": ["sort_order", "name"],
                "verbose_name": "special role",
                "verbose_name_plural": "special roles",
            },
        ),
        migrations.AlterModelOptions(
            name="srpclaim",
            options={
                "ordering": ["-submitted_at"],
                "verbose_name": "SRP claim",
                "verbose_name_plural": "SRP claims",
            },
        ),
        migrations.AlterModelOptions(
            name="srpclaimevent",
            options={
                "ordering": ["-created_at", "-pk"],
                "verbose_name": "SRP claim event",
                "verbose_name_plural": "SRP claim events",
            },
        ),
        migrations.AlterModelOptions(
            name="srpcorporationlimit",
            options={
                "ordering": ["corporation_name", "corporation_id"],
                "verbose_name": "SRP corporation limit",
                "verbose_name_plural": "SRP corporation limits",
            },
        ),
        migrations.AlterModelOptions(
            name="srpfleetlink",
            options={
                "ordering": ["-created_at"],
                "verbose_name": "SRP fleet link",
                "verbose_name_plural": "SRP fleet links",
            },
        ),
        migrations.AlterModelOptions(
            name="srpkillmail",
            options={
                "ordering": ["-killmail_time"],
                "verbose_name": "SRP killmail",
                "verbose_name_plural": "SRP killmails",
            },
        ),
        migrations.AlterModelOptions(
            name="srppayoutrule",
            options={
                "ordering": ["ship_type_name", "doctrine_name", "scope"],
                "verbose_name": "SRP payout rule",
                "verbose_name_plural": "SRP payout rules",
            },
        ),
        migrations.AlterModelOptions(
            name="statisticstarget",
            options={
                "ordering": ["-year", "-month"],
                "verbose_name": "monthly target",
                "verbose_name_plural": "monthly targets",
            },
        ),
        migrations.AlterField(
            model_name="attendancerecord",
            name="attendance_value",
            field=models.PositiveIntegerField(default=1, verbose_name="PAP value"),
        ),
        migrations.AlterField(
            model_name="attendancerecord",
            name="auth_user",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="fleetops_attendance",
                to=settings.AUTH_USER_MODEL,
                verbose_name="Auth user",
            ),
        ),
        migrations.AlterField(
            model_name="attendancerecord",
            name="below_minimum",
            field=models.BooleanField(
                default=False,
                help_text="The character did not reach the PAP minimum time.",
                verbose_name="Below minimum",
            ),
        ),
        migrations.AlterField(
            model_name="attendancerecord",
            name="capped",
            field=models.BooleanField(default=False, verbose_name="Capped"),
        ),
        migrations.AlterField(
            model_name="attendancerecord",
            name="character_id",
            field=models.BigIntegerField(verbose_name="Character ID"),
        ),
        migrations.AlterField(
            model_name="attendancerecord",
            name="character_name",
            field=models.CharField(max_length=255, verbose_name="Character name"),
        ),
        migrations.AlterField(
            model_name="attendancerecord",
            name="corporation_id",
            field=models.BigIntegerField(
                blank=True,
                db_index=True,
                help_text="Main character corporation at PAP time.",
                null=True,
                verbose_name="Corporation ID",
            ),
        ),
        migrations.AlterField(
            model_name="attendancerecord",
            name="corporation_name",
            field=models.CharField(
                blank=True, max_length=255, verbose_name="Corporation name"
            ),
        ),
        migrations.AlterField(
            model_name="attendancerecord",
            name="correction",
            field=models.CharField(
                choices=[
                    ("keep", "Keep separate"),
                    ("replace", "Replace tracked PAP"),
                    ("merge", "Merge into tracked PAP"),
                ],
                default="keep",
                help_text="What a manual or imported row does with the tracked PAP of the same character.",
                max_length=10,
                verbose_name="Correction",
            ),
        ),
        migrations.AlterField(
            model_name="attendancerecord",
            name="created_at",
            field=models.DateTimeField(auto_now_add=True, verbose_name="Created at"),
        ),
        migrations.AlterField(
            model_name="attendancerecord",
            name="created_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="fleetops_attendance_created",
                to=settings.AUTH_USER_MODEL,
                verbose_name="Created by",
            ),
        ),
        migrations.AlterField(
            model_name="attendancerecord",
            name="extra_value",
            field=models.PositiveIntegerField(default=0, verbose_name="Extra value"),
        ),
        migrations.AlterField(
            model_name="attendancerecord",
            name="first_seen",
            field=models.DateTimeField(
                blank=True, null=True, verbose_name="First seen"
            ),
        ),
        migrations.AlterField(
            model_name="attendancerecord",
            name="granted",
            field=models.BooleanField(
                db_index=True, default=True, verbose_name="Granted"
            ),
        ),
        migrations.AlterField(
            model_name="attendancerecord",
            name="last_seen",
            field=models.DateTimeField(blank=True, null=True, verbose_name="Last seen"),
        ),
        migrations.AlterField(
            model_name="attendancerecord",
            name="legacy_extra_value",
            field=models.PositiveIntegerField(
                default=0, verbose_name="Legacy extra value"
            ),
        ),
        migrations.AlterField(
            model_name="attendancerecord",
            name="main_character_id",
            field=models.BigIntegerField(
                blank=True, db_index=True, null=True, verbose_name="Main character ID"
            ),
        ),
        migrations.AlterField(
            model_name="attendancerecord",
            name="main_character_name",
            field=models.CharField(
                blank=True, max_length=255, verbose_name="Main character name"
            ),
        ),
        migrations.AlterField(
            model_name="attendancerecord",
            name="notes",
            field=models.TextField(blank=True, verbose_name="Notes"),
        ),
        migrations.AlterField(
            model_name="attendancerecord",
            name="operation",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="attendance_records",
                to="fleetops.fleetoperation",
                verbose_name="Operation",
            ),
        ),
        migrations.AlterField(
            model_name="attendancerecord",
            name="replaced",
            field=models.BooleanField(
                default=False,
                help_text="Replaced by an approved manual PAP.",
                verbose_name="Replaced",
            ),
        ),
        migrations.AlterField(
            model_name="attendancerecord",
            name="review_note",
            field=models.CharField(
                blank=True, max_length=255, verbose_name="Review note"
            ),
        ),
        migrations.AlterField(
            model_name="attendancerecord",
            name="review_status",
            field=models.CharField(
                choices=[
                    ("approved", "Approved"),
                    ("pending", "Pending approval"),
                    ("rejected", "Rejected"),
                ],
                db_index=True,
                default="approved",
                max_length=10,
                verbose_name="Review status",
            ),
        ),
        migrations.AlterField(
            model_name="attendancerecord",
            name="reviewed_at",
            field=models.DateTimeField(
                blank=True, null=True, verbose_name="Reviewed at"
            ),
        ),
        migrations.AlterField(
            model_name="attendancerecord",
            name="reviewed_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="fleetops_reviewed_attendance",
                to=settings.AUTH_USER_MODEL,
                verbose_name="Reviewed by",
            ),
        ),
        migrations.AlterField(
            model_name="attendancerecord",
            name="source",
            field=models.CharField(
                choices=[
                    ("automatic", "Automatic"),
                    ("manual", "Manual"),
                    ("imported", "Imported"),
                    ("click", "PAP link"),
                ],
                max_length=20,
                verbose_name="Source",
            ),
        ),
        migrations.AlterField(
            model_name="attendancerecord",
            name="tracked_seconds",
            field=models.PositiveIntegerField(
                blank=True, null=True, verbose_name="Tracked seconds"
            ),
        ),
        migrations.AlterField(
            model_name="attendancerecord",
            name="updated_at",
            field=models.DateTimeField(auto_now=True, verbose_name="Updated at"),
        ),
        migrations.AlterField(
            model_name="auditlog",
            name="action",
            field=models.CharField(
                db_index=True, max_length=100, verbose_name="Action"
            ),
        ),
        migrations.AlterField(
            model_name="auditlog",
            name="actor",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="fleetops_audit_entries",
                to=settings.AUTH_USER_MODEL,
                verbose_name="Actor",
            ),
        ),
        migrations.AlterField(
            model_name="auditlog",
            name="created_at",
            field=models.DateTimeField(
                auto_now_add=True, db_index=True, verbose_name="Created at"
            ),
        ),
        migrations.AlterField(
            model_name="auditlog",
            name="new_value",
            field=models.JSONField(blank=True, null=True, verbose_name="New value"),
        ),
        migrations.AlterField(
            model_name="auditlog",
            name="object_id",
            field=models.CharField(max_length=100, verbose_name="Object ID"),
        ),
        migrations.AlterField(
            model_name="auditlog",
            name="object_type",
            field=models.CharField(max_length=100, verbose_name="Object type"),
        ),
        migrations.AlterField(
            model_name="auditlog",
            name="old_value",
            field=models.JSONField(blank=True, null=True, verbose_name="Old value"),
        ),
        migrations.AlterField(
            model_name="auditlog",
            name="reason",
            field=models.TextField(blank=True, verbose_name="Reason"),
        ),
        migrations.AlterField(
            model_name="channelpreset",
            name="channel_type",
            field=models.CharField(
                choices=[("logi", "Logi"), ("boost", "Boost")],
                max_length=20,
                verbose_name="Channel type",
            ),
        ),
        migrations.AlterField(
            model_name="channelpreset",
            name="ingame_channel_id",
            field=models.CharField(
                blank=True,
                max_length=64,
                validators=[
                    django.core.validators.RegexValidator(
                        "^player_[0-9a-fA-F]{32}$",
                        "Enter an in-game channel id such as player_<32 hex digits>.",
                    )
                ],
                verbose_name="Ingame channel ID",
            ),
        ),
        migrations.AlterField(
            model_name="channelpreset",
            name="is_active",
            field=models.BooleanField(default=True, verbose_name="Is active"),
        ),
        migrations.AlterField(
            model_name="channelpreset",
            name="name",
            field=models.CharField(max_length=100, verbose_name="Name"),
        ),
        migrations.AlterField(
            model_name="characteraffiliation",
            name="alliance_id",
            field=models.BigIntegerField(
                blank=True, null=True, verbose_name="Alliance ID"
            ),
        ),
        migrations.AlterField(
            model_name="characteraffiliation",
            name="character_id",
            field=models.BigIntegerField(
                primary_key=True, serialize=False, verbose_name="Character ID"
            ),
        ),
        migrations.AlterField(
            model_name="characteraffiliation",
            name="corporation_id",
            field=models.BigIntegerField(
                blank=True, null=True, verbose_name="Corporation ID"
            ),
        ),
        migrations.AlterField(
            model_name="characteraffiliation",
            name="faction_id",
            field=models.BigIntegerField(
                blank=True, null=True, verbose_name="Faction ID"
            ),
        ),
        migrations.AlterField(
            model_name="characteraffiliation",
            name="updated_at",
            field=models.DateTimeField(db_index=True, verbose_name="Updated at"),
        ),
        migrations.AlterField(
            model_name="commspreset",
            name="channel_name",
            field=models.CharField(
                blank=True, max_length=255, verbose_name="Channel name"
            ),
        ),
        migrations.AlterField(
            model_name="commspreset",
            name="description",
            field=models.TextField(blank=True, verbose_name="Description"),
        ),
        migrations.AlterField(
            model_name="commspreset",
            name="is_active",
            field=models.BooleanField(default=True, verbose_name="Is active"),
        ),
        migrations.AlterField(
            model_name="commspreset",
            name="join_token",
            field=models.UUIDField(
                default=uuid.uuid4,
                editable=False,
                unique=True,
                verbose_name="Join token",
            ),
        ),
        migrations.AlterField(
            model_name="commspreset",
            name="name",
            field=models.CharField(max_length=100, unique=True, verbose_name="Name"),
        ),
        migrations.AlterField(
            model_name="commspreset",
            name="voice_url",
            field=models.CharField(
                blank=True,
                help_text="Supports mumble://, ts3server://, discord:// and https:// voice links. Do not include a personal user name or password.",
                max_length=500,
                validators=[fleetops.validators.validate_voice_url],
                verbose_name="Voice URL",
            ),
        ),
        migrations.AlterField(
            model_name="discordwebhook",
            name="allow_everyone",
            field=models.BooleanField(
                default=False,
                help_text="FCs may ping @everyone in this channel.",
                verbose_name="Allow everyone",
            ),
        ),
        migrations.AlterField(
            model_name="discordwebhook",
            name="allow_here",
            field=models.BooleanField(
                default=False,
                help_text="FCs may ping @here in this channel.",
                verbose_name="Allow here",
            ),
        ),
        migrations.AlterField(
            model_name="discordwebhook",
            name="allowed_roles",
            field=models.ManyToManyField(
                blank=True,
                help_text="Leave empty to allow every active role.",
                related_name="channels",
                to="fleetops.pingrole",
                verbose_name="Allowed roles",
            ),
        ),
        migrations.AlterField(
            model_name="discordwebhook",
            name="is_active",
            field=models.BooleanField(default=True, verbose_name="Is active"),
        ),
        migrations.AlterField(
            model_name="discordwebhook",
            name="name",
            field=models.CharField(max_length=100, unique=True, verbose_name="Name"),
        ),
        migrations.AlterField(
            model_name="discordwebhook",
            name="sort_order",
            field=models.PositiveIntegerField(default=0, verbose_name="Sort order"),
        ),
        migrations.AlterField(
            model_name="discordwebhook",
            name="webhook_url",
            field=models.TextField(
                validators=[fleetops.models.validate_discord_webhook_url],
                verbose_name="Webhook URL",
            ),
        ),
        migrations.AlterField(
            model_name="eveentityname",
            name="category",
            field=models.CharField(
                blank=True,
                help_text='ESI category, or "unresolved".',
                max_length=20,
                verbose_name="Category",
            ),
        ),
        migrations.AlterField(
            model_name="eveentityname",
            name="id",
            field=models.BigIntegerField(
                primary_key=True, serialize=False, verbose_name="ID"
            ),
        ),
        migrations.AlterField(
            model_name="eveentityname",
            name="name",
            field=models.CharField(blank=True, max_length=255, verbose_name="Name"),
        ),
        migrations.AlterField(
            model_name="eveentityname",
            name="updated_at",
            field=models.DateTimeField(db_index=True, verbose_name="Updated at"),
        ),
        migrations.AlterField(
            model_name="fleetmemberevent",
            name="character_id",
            field=models.BigIntegerField(db_index=True, verbose_name="Character ID"),
        ),
        migrations.AlterField(
            model_name="fleetmemberevent",
            name="character_name",
            field=models.CharField(max_length=255, verbose_name="Character name"),
        ),
        migrations.AlterField(
            model_name="fleetmemberevent",
            name="created_at",
            field=models.DateTimeField(
                auto_now_add=True, db_index=True, verbose_name="Created at"
            ),
        ),
        migrations.AlterField(
            model_name="fleetmemberevent",
            name="event_type",
            field=models.CharField(
                choices=[
                    ("join", "Join"),
                    ("leave", "Leave"),
                    ("rejoin", "Rejoin"),
                    ("ship_change", "Ship change"),
                    ("system_change", "System change"),
                    ("role_change", "Role change"),
                    ("wing_change", "Wing change"),
                    ("squad_change", "Squad change"),
                ],
                db_index=True,
                max_length=30,
                verbose_name="Event type",
            ),
        ),
        migrations.AlterField(
            model_name="fleetmemberevent",
            name="new_value",
            field=models.CharField(
                blank=True, max_length=255, verbose_name="New value"
            ),
        ),
        migrations.AlterField(
            model_name="fleetmemberevent",
            name="old_value",
            field=models.CharField(
                blank=True, max_length=255, verbose_name="Old value"
            ),
        ),
        migrations.AlterField(
            model_name="fleetmemberevent",
            name="operation",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="member_events",
                to="fleetops.fleetoperation",
                verbose_name="Operation",
            ),
        ),
        migrations.AlterField(
            model_name="fleetmemberstate",
            name="accumulated_seconds",
            field=models.PositiveIntegerField(
                default=0,
                help_text="Time in fleet so far.",
                verbose_name="Accumulated seconds",
            ),
        ),
        migrations.AlterField(
            model_name="fleetmemberstate",
            name="alliance_id",
            field=models.BigIntegerField(
                blank=True, null=True, verbose_name="Alliance ID"
            ),
        ),
        migrations.AlterField(
            model_name="fleetmemberstate",
            name="alliance_name",
            field=models.CharField(
                blank=True, max_length=255, verbose_name="Alliance name"
            ),
        ),
        migrations.AlterField(
            model_name="fleetmemberstate",
            name="auth_user",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="fleetops_member_states",
                to=settings.AUTH_USER_MODEL,
                verbose_name="Auth user",
            ),
        ),
        migrations.AlterField(
            model_name="fleetmemberstate",
            name="character_id",
            field=models.BigIntegerField(verbose_name="Character ID"),
        ),
        migrations.AlterField(
            model_name="fleetmemberstate",
            name="character_name",
            field=models.CharField(max_length=255, verbose_name="Character name"),
        ),
        migrations.AlterField(
            model_name="fleetmemberstate",
            name="corporation_id",
            field=models.BigIntegerField(
                blank=True, db_index=True, null=True, verbose_name="Corporation ID"
            ),
        ),
        migrations.AlterField(
            model_name="fleetmemberstate",
            name="corporation_name",
            field=models.CharField(
                blank=True, max_length=255, verbose_name="Corporation name"
            ),
        ),
        migrations.AlterField(
            model_name="fleetmemberstate",
            name="first_seen",
            field=models.DateTimeField(verbose_name="First seen"),
        ),
        migrations.AlterField(
            model_name="fleetmemberstate",
            name="fleet_role",
            field=models.CharField(
                blank=True, max_length=50, verbose_name="Fleet role"
            ),
        ),
        migrations.AlterField(
            model_name="fleetmemberstate",
            name="is_active",
            field=models.BooleanField(
                db_index=True, default=True, verbose_name="Is active"
            ),
        ),
        migrations.AlterField(
            model_name="fleetmemberstate",
            name="last_seen",
            field=models.DateTimeField(verbose_name="Last seen"),
        ),
        migrations.AlterField(
            model_name="fleetmemberstate",
            name="left_at",
            field=models.DateTimeField(blank=True, null=True, verbose_name="Left at"),
        ),
        migrations.AlterField(
            model_name="fleetmemberstate",
            name="main_character_id",
            field=models.BigIntegerField(
                blank=True, db_index=True, null=True, verbose_name="Main character ID"
            ),
        ),
        migrations.AlterField(
            model_name="fleetmemberstate",
            name="main_character_name",
            field=models.CharField(
                blank=True, max_length=255, verbose_name="Main character name"
            ),
        ),
        migrations.AlterField(
            model_name="fleetmemberstate",
            name="operation",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="member_states",
                to="fleetops.fleetoperation",
                verbose_name="Operation",
            ),
        ),
        migrations.AlterField(
            model_name="fleetmemberstate",
            name="ship_type_id",
            field=models.BigIntegerField(
                blank=True, null=True, verbose_name="Ship type ID"
            ),
        ),
        migrations.AlterField(
            model_name="fleetmemberstate",
            name="ship_type_name",
            field=models.CharField(
                blank=True, max_length=255, verbose_name="Ship type name"
            ),
        ),
        migrations.AlterField(
            model_name="fleetmemberstate",
            name="solar_system_id",
            field=models.BigIntegerField(
                blank=True, db_index=True, null=True, verbose_name="Solar system ID"
            ),
        ),
        migrations.AlterField(
            model_name="fleetmemberstate",
            name="solar_system_name",
            field=models.CharField(
                blank=True, max_length=255, verbose_name="Solar system name"
            ),
        ),
        migrations.AlterField(
            model_name="fleetmemberstate",
            name="squad_id",
            field=models.BigIntegerField(
                blank=True, null=True, verbose_name="Squad ID"
            ),
        ),
        migrations.AlterField(
            model_name="fleetmemberstate",
            name="station_id",
            field=models.BigIntegerField(
                blank=True,
                help_text="Set while the member is docked.",
                null=True,
                verbose_name="Station ID",
            ),
        ),
        migrations.AlterField(
            model_name="fleetmemberstate",
            name="takes_fleet_warp",
            field=models.BooleanField(
                blank=True, null=True, verbose_name="Takes fleet warp"
            ),
        ),
        migrations.AlterField(
            model_name="fleetmemberstate",
            name="wing_id",
            field=models.BigIntegerField(blank=True, null=True, verbose_name="Wing ID"),
        ),
        migrations.AlterField(
            model_name="fleetoperation",
            name="additional_message",
            field=models.TextField(blank=True, verbose_name="Additional message"),
        ),
        migrations.AlterField(
            model_name="fleetoperation",
            name="attendance_multiplier",
            field=models.PositiveIntegerField(
                default=1,
                help_text="Fleet-wide PAP multiplier applied to automatically granted PAP.",
                validators=[
                    django.core.validators.MinValueValidator(1),
                    django.core.validators.MaxValueValidator(10),
                ],
                verbose_name="PAP multiplier",
            ),
        ),
        migrations.AlterField(
            model_name="fleetoperation",
            name="boost_channel",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="fleetops_boost_operations",
                to="fleetops.channelpreset",
                verbose_name="Boost channel",
            ),
        ),
        migrations.AlterField(
            model_name="fleetoperation",
            name="comms",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                to="fleetops.commspreset",
                verbose_name="Comms",
            ),
        ),
        migrations.AlterField(
            model_name="fleetoperation",
            name="counts_for_stats",
            field=models.BooleanField(
                db_index=True,
                default=True,
                help_text="Counted in statistics and incentives; follows the minimum fleet duration unless overridden.",
                verbose_name="Counts for stats",
            ),
        ),
        migrations.AlterField(
            model_name="fleetoperation",
            name="created_at",
            field=models.DateTimeField(auto_now_add=True, verbose_name="Created at"),
        ),
        migrations.AlterField(
            model_name="fleetoperation",
            name="created_by",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="fleetops_created_operations",
                to=settings.AUTH_USER_MODEL,
                verbose_name="Created by",
            ),
        ),
        migrations.AlterField(
            model_name="fleetoperation",
            name="doctrine_external_id",
            field=models.CharField(
                blank=True, max_length=255, verbose_name="Doctrine external ID"
            ),
        ),
        migrations.AlterField(
            model_name="fleetoperation",
            name="doctrine_fits",
            field=models.JSONField(
                blank=True, default=list, verbose_name="Doctrine fits"
            ),
        ),
        migrations.AlterField(
            model_name="fleetoperation",
            name="doctrine_name",
            field=models.CharField(
                blank=True, max_length=255, verbose_name="Doctrine name"
            ),
        ),
        migrations.AlterField(
            model_name="fleetoperation",
            name="doctrine_source",
            field=models.CharField(
                blank=True, max_length=50, verbose_name="Doctrine source"
            ),
        ),
        migrations.AlterField(
            model_name="fleetoperation",
            name="end_detail",
            field=models.CharField(
                blank=True, max_length=255, verbose_name="End detail"
            ),
        ),
        migrations.AlterField(
            model_name="fleetoperation",
            name="end_reason",
            field=models.CharField(
                blank=True,
                choices=[
                    ("manual", "Ended manually"),
                    ("fleet_gone", "Fleet no longer exists or the FC left it"),
                    ("boss_handover", "Fleet boss handed over"),
                ],
                max_length=20,
                verbose_name="End reason",
            ),
        ),
        migrations.AlterField(
            model_name="fleetoperation",
            name="end_requested_at",
            field=models.DateTimeField(
                blank=True, null=True, verbose_name="End requested at"
            ),
        ),
        migrations.AlterField(
            model_name="fleetoperation",
            name="end_requested_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="fleetops_end_requests",
                to=settings.AUTH_USER_MODEL,
                verbose_name="End requested by",
            ),
        ),
        migrations.AlterField(
            model_name="fleetoperation",
            name="end_requested_multiplier",
            field=models.PositiveIntegerField(
                blank=True,
                null=True,
                validators=[
                    django.core.validators.MinValueValidator(1),
                    django.core.validators.MaxValueValidator(10),
                ],
                verbose_name="End requested multiplier",
            ),
        ),
        migrations.AlterField(
            model_name="fleetoperation",
            name="ended_at",
            field=models.DateTimeField(blank=True, null=True, verbose_name="Ended at"),
        ),
        migrations.AlterField(
            model_name="fleetoperation",
            name="esi_fleet_id",
            field=models.BigIntegerField(
                blank=True, db_index=True, null=True, verbose_name="ESI fleet ID"
            ),
        ),
        migrations.AlterField(
            model_name="fleetoperation",
            name="fc_character_id",
            field=models.BigIntegerField(db_index=True, verbose_name="FC character ID"),
        ),
        migrations.AlterField(
            model_name="fleetoperation",
            name="fc_character_name",
            field=models.CharField(max_length=255, verbose_name="FC character name"),
        ),
        migrations.AlterField(
            model_name="fleetoperation",
            name="fc_main_character_id",
            field=models.BigIntegerField(
                blank=True,
                db_index=True,
                null=True,
                verbose_name="FC main character ID",
            ),
        ),
        migrations.AlterField(
            model_name="fleetoperation",
            name="fc_main_character_name",
            field=models.CharField(
                blank=True, max_length=255, verbose_name="FC main character name"
            ),
        ),
        migrations.AlterField(
            model_name="fleetoperation",
            name="fc_user",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="fleetops_fc_operations",
                to=settings.AUTH_USER_MODEL,
                verbose_name="FC user",
            ),
        ),
        migrations.AlterField(
            model_name="fleetoperation",
            name="fleet_boss_character_id",
            field=models.BigIntegerField(
                blank=True, null=True, verbose_name="Fleet boss character ID"
            ),
        ),
        migrations.AlterField(
            model_name="fleetoperation",
            name="fleet_missing_count",
            field=models.PositiveIntegerField(
                default=0, verbose_name="Fleet missing count"
            ),
        ),
        migrations.AlterField(
            model_name="fleetoperation",
            name="fleet_point_weight_snapshot",
            field=models.DecimalField(
                decimal_places=2,
                default=Decimal("0"),
                max_digits=8,
                verbose_name="Fleet point weight snapshot",
            ),
        ),
        migrations.AlterField(
            model_name="fleetoperation",
            name="fleet_type",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                to="fleetops.fleettype",
                verbose_name="Fleet type",
            ),
        ),
        migrations.AlterField(
            model_name="fleetoperation",
            name="fleet_wings",
            field=models.JSONField(
                blank=True, default=list, verbose_name="Fleet wings"
            ),
        ),
        migrations.AlterField(
            model_name="fleetoperation",
            name="fleet_wings_read_at",
            field=models.DateTimeField(
                blank=True, null=True, verbose_name="Fleet wings read at"
            ),
        ),
        migrations.AlterField(
            model_name="fleetoperation",
            name="formup",
            field=models.CharField(max_length=255, verbose_name="Formup"),
        ),
        migrations.AlterField(
            model_name="fleetoperation",
            name="is_manual",
            field=models.BooleanField(
                default=False,
                help_text="Manual fleet record created without ESI fleet detection/tracking.",
                verbose_name="Is manual",
            ),
        ),
        migrations.AlterField(
            model_name="fleetoperation",
            name="last_error",
            field=models.TextField(blank=True, verbose_name="Last error"),
        ),
        migrations.AlterField(
            model_name="fleetoperation",
            name="last_esi_update",
            field=models.DateTimeField(
                blank=True, null=True, verbose_name="Last ESI update"
            ),
        ),
        migrations.AlterField(
            model_name="fleetoperation",
            name="link_only",
            field=models.BooleanField(
                default=False,
                help_text="Fleet started without ESI; members register through PAP links.",
                verbose_name="Link only",
            ),
        ),
        migrations.AlterField(
            model_name="fleetoperation",
            name="logi_channel",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="fleetops_logi_operations",
                to="fleetops.channelpreset",
                verbose_name="Logi channel",
            ),
        ),
        migrations.AlterField(
            model_name="fleetoperation",
            name="missing_detail",
            field=models.CharField(
                blank=True, max_length=255, verbose_name="Missing detail"
            ),
        ),
        migrations.AlterField(
            model_name="fleetoperation",
            name="missing_reason",
            field=models.CharField(
                blank=True,
                choices=[
                    ("manual", "Ended manually"),
                    ("fleet_gone", "Fleet no longer exists or the FC left it"),
                    ("boss_handover", "Fleet boss handed over"),
                ],
                help_text="Why the latest tracking poll could not read the fleet. Cleared by a successful read.",
                max_length=20,
                verbose_name="Missing reason",
            ),
        ),
        migrations.AlterField(
            model_name="fleetoperation",
            name="motd_template",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="fleetops_motd_operations",
                to="fleetops.messagetemplate",
                verbose_name="MOTD template",
            ),
        ),
        migrations.AlterField(
            model_name="fleetoperation",
            name="motd_text",
            field=models.TextField(blank=True, verbose_name="MOTD text"),
        ),
        migrations.AlterField(
            model_name="fleetoperation",
            name="multiplier_pending",
            field=models.BooleanField(
                db_index=True,
                default=False,
                help_text="The fleet ended without a multiplier choice although a higher multiplier was reached.",
                verbose_name="Multiplier pending",
            ),
        ),
        migrations.AlterField(
            model_name="fleetoperation",
            name="multiplier_source",
            field=models.CharField(
                blank=True,
                choices=[
                    ("default", "Default"),
                    ("fc", "Chosen by the FC"),
                    ("auto", "By fleet duration"),
                    ("override", "Override by an FC lead"),
                    ("manual", "Manual fleet"),
                ],
                max_length=10,
                verbose_name="Multiplier source",
            ),
        ),
        migrations.AlterField(
            model_name="fleetoperation",
            name="pap_character_limit",
            field=models.PositiveIntegerField(
                blank=True, null=True, verbose_name="PAP character limit"
            ),
        ),
        migrations.AlterField(
            model_name="fleetoperation",
            name="pap_minimum_minutes",
            field=models.PositiveIntegerField(
                blank=True, null=True, verbose_name="PAP minimum minutes"
            ),
        ),
        migrations.AlterField(
            model_name="fleetoperation",
            name="pap_rules",
            field=models.PositiveSmallIntegerField(
                choices=[(1, "Legacy"), (2, "Time in fleet")],
                default=2,
                verbose_name="PAP rules",
            ),
        ),
        migrations.AlterField(
            model_name="fleetoperation",
            name="ping_channel",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="fleetops_operations",
                to="fleetops.discordwebhook",
                verbose_name="Ping channel",
            ),
        ),
        migrations.AlterField(
            model_name="fleetoperation",
            name="ping_channel_name",
            field=models.CharField(
                blank=True, max_length=100, verbose_name="Ping channel name"
            ),
        ),
        migrations.AlterField(
            model_name="fleetoperation",
            name="ping_mention_mode",
            field=models.CharField(
                choices=[
                    ("none", "No ping"),
                    ("everyone", "@everyone"),
                    ("here", "@here"),
                    ("roles", "Roles"),
                ],
                default="none",
                max_length=10,
                verbose_name="Ping mention mode",
            ),
        ),
        migrations.AlterField(
            model_name="fleetoperation",
            name="ping_roles",
            field=models.JSONField(blank=True, default=list, verbose_name="Ping roles"),
        ),
        migrations.AlterField(
            model_name="fleetoperation",
            name="ping_target",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                to="fleetops.pingtarget",
                verbose_name="Ping target",
            ),
        ),
        migrations.AlterField(
            model_name="fleetoperation",
            name="ping_template",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="fleetops_ping_operations",
                to="fleetops.messagetemplate",
                verbose_name="Ping template",
            ),
        ),
        migrations.AlterField(
            model_name="fleetoperation",
            name="ping_text",
            field=models.TextField(blank=True, verbose_name="Ping text"),
        ),
        migrations.AlterField(
            model_name="fleetoperation",
            name="review_note",
            field=models.CharField(
                blank=True, max_length=255, verbose_name="Review note"
            ),
        ),
        migrations.AlterField(
            model_name="fleetoperation",
            name="review_status",
            field=models.CharField(
                choices=[
                    ("approved", "Approved"),
                    ("pending", "Pending approval"),
                    ("rejected", "Rejected"),
                ],
                db_index=True,
                default="approved",
                max_length=10,
                verbose_name="Review status",
            ),
        ),
        migrations.AlterField(
            model_name="fleetoperation",
            name="reviewed_at",
            field=models.DateTimeField(
                blank=True, null=True, verbose_name="Reviewed at"
            ),
        ),
        migrations.AlterField(
            model_name="fleetoperation",
            name="reviewed_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="fleetops_reviewed_operations",
                to=settings.AUTH_USER_MODEL,
                verbose_name="Reviewed by",
            ),
        ),
        migrations.AlterField(
            model_name="fleetoperation",
            name="scheduled_at",
            field=models.DateTimeField(
                blank=True, null=True, verbose_name="Scheduled at"
            ),
        ),
        migrations.AlterField(
            model_name="fleetoperation",
            name="send_ping",
            field=models.BooleanField(
                default=True,
                help_text="If false, the operation tracks PAP without sending a Discord ping.",
                verbose_name="Send ping",
            ),
        ),
        migrations.AlterField(
            model_name="fleetoperation",
            name="srp_error",
            field=models.TextField(blank=True, verbose_name="SRP error"),
        ),
        migrations.AlterField(
            model_name="fleetoperation",
            name="srp_provider",
            field=models.CharField(
                blank=True, max_length=50, verbose_name="SRP provider"
            ),
        ),
        migrations.AlterField(
            model_name="fleetoperation",
            name="srp_reference",
            field=models.CharField(
                blank=True, max_length=255, verbose_name="SRP reference"
            ),
        ),
        migrations.AlterField(
            model_name="fleetoperation",
            name="srp_url",
            field=models.CharField(blank=True, max_length=1000, verbose_name="SRP URL"),
        ),
        migrations.AlterField(
            model_name="fleetoperation",
            name="start_request_id",
            field=models.UUIDField(
                blank=True,
                editable=False,
                null=True,
                unique=True,
                verbose_name="Start request ID",
            ),
        ),
        migrations.AlterField(
            model_name="fleetoperation",
            name="started_at",
            field=models.DateTimeField(
                blank=True, db_index=True, null=True, verbose_name="Started at"
            ),
        ),
        migrations.AlterField(
            model_name="fleetoperation",
            name="stats_mode",
            field=models.CharField(
                choices=[
                    ("auto", "Automatic"),
                    ("include", "Always count"),
                    ("exclude", "Do not count"),
                ],
                default="auto",
                max_length=10,
                verbose_name="Stats mode",
            ),
        ),
        migrations.AlterField(
            model_name="fleetoperation",
            name="stats_note",
            field=models.CharField(
                blank=True, max_length=255, verbose_name="Stats note"
            ),
        ),
        migrations.AlterField(
            model_name="fleetoperation",
            name="status",
            field=models.CharField(
                choices=[
                    ("draft", "Draft"),
                    ("scheduled", "Scheduled"),
                    ("starting", "Starting"),
                    ("active", "Active"),
                    ("ending", "Ending"),
                    ("closed", "Closed"),
                    ("cancelled", "Cancelled"),
                    ("error", "Error"),
                ],
                db_index=True,
                default="draft",
                max_length=20,
                verbose_name="Status",
            ),
        ),
        migrations.AlterField(
            model_name="fleetoperation",
            name="tracking_enabled",
            field=models.BooleanField(default=False, verbose_name="Tracking enabled"),
        ),
        migrations.AlterField(
            model_name="fleetoperation",
            name="updated_at",
            field=models.DateTimeField(auto_now=True, verbose_name="Updated at"),
        ),
        migrations.AlterField(
            model_name="fleetoperation",
            name="uuid",
            field=models.UUIDField(
                default=uuid.uuid4, editable=False, unique=True, verbose_name="UUID"
            ),
        ),
        migrations.AlterField(
            model_name="fleetopssettings",
            name="auto_end_enabled",
            field=models.BooleanField(default=True, verbose_name="Auto end enabled"),
        ),
        migrations.AlterField(
            model_name="fleetopssettings",
            name="auto_end_missing_count",
            field=models.PositiveIntegerField(
                default=3,
                validators=[django.core.validators.MinValueValidator(2)],
                verbose_name="Auto end missing count",
            ),
        ),
        migrations.AlterField(
            model_name="fleetopssettings",
            name="data_retention_days",
            field=models.PositiveIntegerField(
                default=365,
                help_text="PAP/event history retention in days. Minimum 365 days, maximum 36500 days (100 years).",
                validators=[
                    django.core.validators.MinValueValidator(365),
                    django.core.validators.MaxValueValidator(36500),
                ],
                verbose_name="Data retention days",
            ),
        ),
        migrations.AlterField(
            model_name="fleetopssettings",
            name="incentive_enabled",
            field=models.BooleanField(default=False, verbose_name="Incentive enabled"),
        ),
        migrations.AlterField(
            model_name="fleetopssettings",
            name="incentive_minimum_fleets",
            field=models.PositiveIntegerField(
                default=3,
                validators=[django.core.validators.MinValueValidator(1)],
                verbose_name="Incentive minimum fleets",
            ),
        ),
        migrations.AlterField(
            model_name="fleetopssettings",
            name="srp_auto_create",
            field=models.BooleanField(
                default=True,
                help_text="Automatically create/link an SRP fleet when a supported SRP provider is available.",
                verbose_name="SRP auto create",
            ),
        ),
        migrations.AlterField(
            model_name="fleetopssettings",
            name="srp_provider",
            field=models.CharField(
                default="auto",
                help_text="SRP provider. 'auto' uses FleetOps SRP when it is enabled, then aa-srp, then Alliance Auth SRP.",
                max_length=50,
                verbose_name="SRP provider",
            ),
        ),
        migrations.AlterField(
            model_name="fleetopssettings",
            name="stale_threshold",
            field=models.PositiveIntegerField(
                default=180,
                validators=[django.core.validators.MinValueValidator(60)],
                verbose_name="Stale threshold",
            ),
        ),
        migrations.AlterField(
            model_name="fleetopssettings",
            name="tracking_interval",
            field=models.PositiveIntegerField(
                default=60,
                validators=[django.core.validators.MinValueValidator(30)],
                verbose_name="Tracking interval",
            ),
        ),
        migrations.AlterField(
            model_name="fleetstartpreset",
            name="created_at",
            field=models.DateTimeField(auto_now_add=True, verbose_name="Created at"),
        ),
        migrations.AlterField(
            model_name="fleetstartpreset",
            name="fc_character_id",
            field=models.BigIntegerField(
                blank=True, null=True, verbose_name="FC character ID"
            ),
        ),
        migrations.AlterField(
            model_name="fleetstartpreset",
            name="is_default",
            field=models.BooleanField(default=False, verbose_name="Is default"),
        ),
        migrations.AlterField(
            model_name="fleetstartpreset",
            name="is_last_used",
            field=models.BooleanField(default=False, verbose_name="Is last used"),
        ),
        migrations.AlterField(
            model_name="fleetstartpreset",
            name="name",
            field=models.CharField(blank=True, max_length=100, verbose_name="Name"),
        ),
        migrations.AlterField(
            model_name="fleetstartpreset",
            name="owner",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="fleetops_start_presets",
                to=settings.AUTH_USER_MODEL,
                verbose_name="Owner",
            ),
        ),
        migrations.AlterField(
            model_name="fleetstartpreset",
            name="updated_at",
            field=models.DateTimeField(auto_now=True, verbose_name="Updated at"),
        ),
        migrations.AlterField(
            model_name="fleetstartpreset",
            name="values",
            field=models.JSONField(blank=True, default=dict, verbose_name="Values"),
        ),
        migrations.AlterField(
            model_name="fleetstructuretemplate",
            name="description",
            field=models.CharField(
                blank=True, max_length=255, verbose_name="Description"
            ),
        ),
        migrations.AlterField(
            model_name="fleetstructuretemplate",
            name="is_active",
            field=models.BooleanField(default=True, verbose_name="Is active"),
        ),
        migrations.AlterField(
            model_name="fleetstructuretemplate",
            name="layout",
            field=models.TextField(
                help_text="One wing per line: 'Wing name: Squad 1, Squad 2'. Names up to 10 characters; up to 5 wings with up to 5 squads each. Lines starting with # are ignored.",
                validators=[fleetops.models.validate_structure_layout],
                verbose_name="Layout",
            ),
        ),
        migrations.AlterField(
            model_name="fleetstructuretemplate",
            name="name",
            field=models.CharField(max_length=100, unique=True, verbose_name="Name"),
        ),
        migrations.AlterField(
            model_name="fleetstructuretemplate",
            name="sort_order",
            field=models.PositiveIntegerField(default=0, verbose_name="Sort order"),
        ),
        migrations.AlterField(
            model_name="fleettype",
            name="is_active",
            field=models.BooleanField(default=True, verbose_name="Is active"),
        ),
        migrations.AlterField(
            model_name="fleettype",
            name="name",
            field=models.CharField(max_length=100, unique=True, verbose_name="Name"),
        ),
        migrations.AlterField(
            model_name="fleettype",
            name="point_weight",
            field=models.DecimalField(
                decimal_places=2,
                default=Decimal("0"),
                max_digits=8,
                validators=[django.core.validators.MinValueValidator(Decimal("0"))],
                verbose_name="Point weight",
            ),
        ),
        migrations.AlterField(
            model_name="fleettype",
            name="short_name",
            field=models.CharField(
                blank=True, max_length=30, verbose_name="Short name"
            ),
        ),
        migrations.AlterField(
            model_name="fleettype",
            name="sort_order",
            field=models.PositiveIntegerField(default=0, verbose_name="Sort order"),
        ),
        migrations.AlterField(
            model_name="incentiveperiod",
            name="budget",
            field=models.BigIntegerField(
                default=0,
                validators=[django.core.validators.MinValueValidator(0)],
                verbose_name="Budget",
            ),
        ),
        migrations.AlterField(
            model_name="incentiveperiod",
            name="finalized_at",
            field=models.DateTimeField(
                blank=True, null=True, verbose_name="Finalized at"
            ),
        ),
        migrations.AlterField(
            model_name="incentiveperiod",
            name="finalized_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="fleetops_finalized_periods",
                to=settings.AUTH_USER_MODEL,
                verbose_name="Finalized by",
            ),
        ),
        migrations.AlterField(
            model_name="incentiveperiod",
            name="minimum_fleets",
            field=models.PositiveIntegerField(
                default=3,
                validators=[django.core.validators.MinValueValidator(1)],
                verbose_name="Minimum fleets",
            ),
        ),
        migrations.AlterField(
            model_name="incentiveperiod",
            name="month",
            field=models.PositiveIntegerField(
                validators=[
                    django.core.validators.MinValueValidator(1),
                    django.core.validators.MaxValueValidator(12),
                ],
                verbose_name="Month",
            ),
        ),
        migrations.AlterField(
            model_name="incentiveperiod",
            name="policy_snapshot",
            field=models.JSONField(
                blank=True, default=dict, verbose_name="Policy snapshot"
            ),
        ),
        migrations.AlterField(
            model_name="incentiveperiod",
            name="status",
            field=models.CharField(
                choices=[
                    ("open", "Open"),
                    ("review", "Review"),
                    ("finalized", "Finalized"),
                ],
                default="open",
                max_length=20,
                verbose_name="Status",
            ),
        ),
        migrations.AlterField(
            model_name="incentiveperiod",
            name="year",
            field=models.PositiveIntegerField(
                validators=[
                    django.core.validators.MinValueValidator(2003),
                    django.core.validators.MaxValueValidator(2200),
                ],
                verbose_name="Year",
            ),
        ),
        migrations.AlterField(
            model_name="messagetemplate",
            name="content",
            field=models.TextField(
                validators=[fleetops.models.validate_template_syntax],
                verbose_name="Content",
            ),
        ),
        migrations.AlterField(
            model_name="messagetemplate",
            name="is_active",
            field=models.BooleanField(default=True, verbose_name="Is active"),
        ),
        migrations.AlterField(
            model_name="messagetemplate",
            name="is_default",
            field=models.BooleanField(default=False, verbose_name="Is default"),
        ),
        migrations.AlterField(
            model_name="messagetemplate",
            name="name",
            field=models.CharField(max_length=100, verbose_name="Name"),
        ),
        migrations.AlterField(
            model_name="messagetemplate",
            name="template_type",
            field=models.CharField(
                choices=[("ping", "Ping"), ("motd", "MOTD")],
                max_length=20,
                verbose_name="Template type",
            ),
        ),
        migrations.AlterField(
            model_name="monthlyfcstatistic",
            name="calculated_payout",
            field=models.BigIntegerField(default=0, verbose_name="Calculated payout"),
        ),
        migrations.AlterField(
            model_name="monthlyfcstatistic",
            name="eligible",
            field=models.BooleanField(default=False, verbose_name="Eligible"),
        ),
        migrations.AlterField(
            model_name="monthlyfcstatistic",
            name="fc_user",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="fleetops_monthly_fc_stats",
                to=settings.AUTH_USER_MODEL,
                verbose_name="FC user",
            ),
        ),
        migrations.AlterField(
            model_name="monthlyfcstatistic",
            name="final_payout",
            field=models.BigIntegerField(default=0, verbose_name="Final payout"),
        ),
        migrations.AlterField(
            model_name="monthlyfcstatistic",
            name="fleet_count",
            field=models.PositiveIntegerField(default=0, verbose_name="Fleet count"),
        ),
        migrations.AlterField(
            model_name="monthlyfcstatistic",
            name="fleet_type_breakdown",
            field=models.JSONField(
                blank=True, default=dict, verbose_name="Fleet type breakdown"
            ),
        ),
        migrations.AlterField(
            model_name="monthlyfcstatistic",
            name="period",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="fc_statistics",
                to="fleetops.incentiveperiod",
                verbose_name="Period",
            ),
        ),
        migrations.AlterField(
            model_name="monthlyfcstatistic",
            name="total_points",
            field=models.DecimalField(
                decimal_places=2,
                default=Decimal("0"),
                max_digits=12,
                verbose_name="Total points",
            ),
        ),
        migrations.AlterField(
            model_name="monthlyfcstatistic",
            name="waived",
            field=models.BooleanField(default=False, verbose_name="Waived"),
        ),
        migrations.AlterField(
            model_name="multipliertier",
            name="multiplier",
            field=models.PositiveSmallIntegerField(
                validators=[
                    django.core.validators.MinValueValidator(2),
                    django.core.validators.MaxValueValidator(10),
                ],
                verbose_name="Multiplier",
            ),
        ),
        migrations.AlterField(
            model_name="multipliertier",
            name="threshold_minutes",
            field=models.PositiveIntegerField(
                help_text="Fleets running strictly longer than this many minutes qualify.",
                unique=True,
                validators=[
                    django.core.validators.MinValueValidator(1),
                    django.core.validators.MaxValueValidator(10080),
                ],
                verbose_name="Threshold minutes",
            ),
        ),
        migrations.AlterField(
            model_name="operationaction",
            name="action",
            field=models.CharField(max_length=50, verbose_name="Action"),
        ),
        migrations.AlterField(
            model_name="operationaction",
            name="attempts",
            field=models.PositiveIntegerField(default=1, verbose_name="Attempts"),
        ),
        migrations.AlterField(
            model_name="operationaction",
            name="error_message",
            field=models.TextField(blank=True, verbose_name="Error message"),
        ),
        migrations.AlterField(
            model_name="operationaction",
            name="executed_at",
            field=models.DateTimeField(auto_now=True, verbose_name="Executed at"),
        ),
        migrations.AlterField(
            model_name="operationaction",
            name="operation",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="actions",
                to="fleetops.fleetoperation",
                verbose_name="Operation",
            ),
        ),
        migrations.AlterField(
            model_name="operationaction",
            name="status",
            field=models.CharField(
                choices=[
                    ("pending", "Pending"),
                    ("success", "Success"),
                    ("failed", "Failed"),
                    ("skipped", "Skipped"),
                ],
                default="pending",
                max_length=20,
                verbose_name="Status",
            ),
        ),
        migrations.AlterField(
            model_name="operationactionattempt",
            name="action",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="history",
                to="fleetops.operationaction",
                verbose_name="Action",
            ),
        ),
        migrations.AlterField(
            model_name="operationactionattempt",
            name="created_at",
            field=models.DateTimeField(
                db_index=True,
                default=django.utils.timezone.now,
                verbose_name="Created at",
            ),
        ),
        migrations.AlterField(
            model_name="operationactionattempt",
            name="message",
            field=models.TextField(blank=True, verbose_name="Message"),
        ),
        migrations.AlterField(
            model_name="operationactionattempt",
            name="status",
            field=models.CharField(
                choices=[
                    ("pending", "Pending"),
                    ("success", "Success"),
                    ("failed", "Failed"),
                    ("skipped", "Skipped"),
                ],
                max_length=20,
                verbose_name="Status",
            ),
        ),
        migrations.AlterField(
            model_name="operationroleassignment",
            name="assigned_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="fleetops_roles_assigned",
                to=settings.AUTH_USER_MODEL,
                verbose_name="Assigned by",
            ),
        ),
        migrations.AlterField(
            model_name="operationroleassignment",
            name="auth_user",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="fleetops_role_assignments",
                to=settings.AUTH_USER_MODEL,
                verbose_name="Auth user",
            ),
        ),
        migrations.AlterField(
            model_name="operationroleassignment",
            name="character_id",
            field=models.BigIntegerField(db_index=True, verbose_name="Character ID"),
        ),
        migrations.AlterField(
            model_name="operationroleassignment",
            name="character_name",
            field=models.CharField(max_length=255, verbose_name="Character name"),
        ),
        migrations.AlterField(
            model_name="operationroleassignment",
            name="corporation_id",
            field=models.BigIntegerField(
                blank=True, null=True, verbose_name="Corporation ID"
            ),
        ),
        migrations.AlterField(
            model_name="operationroleassignment",
            name="corporation_name",
            field=models.CharField(
                blank=True, max_length=255, verbose_name="Corporation name"
            ),
        ),
        migrations.AlterField(
            model_name="operationroleassignment",
            name="created_at",
            field=models.DateTimeField(auto_now_add=True, verbose_name="Created at"),
        ),
        migrations.AlterField(
            model_name="operationroleassignment",
            name="grants_fc_credit",
            field=models.BooleanField(
                default=False,
                help_text="Snapshot: this assignment counts as an FC fleet/points credit for the assigned user.",
                verbose_name="Grants FC credit",
            ),
        ),
        migrations.AlterField(
            model_name="operationroleassignment",
            name="main_character_id",
            field=models.BigIntegerField(
                blank=True, db_index=True, null=True, verbose_name="Main character ID"
            ),
        ),
        migrations.AlterField(
            model_name="operationroleassignment",
            name="main_character_name",
            field=models.CharField(
                blank=True, max_length=255, verbose_name="Main character name"
            ),
        ),
        migrations.AlterField(
            model_name="operationroleassignment",
            name="notes",
            field=models.TextField(blank=True, verbose_name="Notes"),
        ),
        migrations.AlterField(
            model_name="operationroleassignment",
            name="operation",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="role_assignments",
                to="fleetops.fleetoperation",
                verbose_name="Operation",
            ),
        ),
        migrations.AlterField(
            model_name="operationroleassignment",
            name="role",
            field=models.CharField(
                db_index=True,
                help_text="Key of the special role.",
                max_length=30,
                verbose_name="Role",
            ),
        ),
        migrations.AlterField(
            model_name="operationroleassignment",
            name="special_role",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="assignments",
                to="fleetops.specialrole",
                verbose_name="Special role",
            ),
        ),
        migrations.AlterField(
            model_name="operationroleassignment",
            name="updated_at",
            field=models.DateTimeField(auto_now=True, verbose_name="Updated at"),
        ),
        migrations.AlterField(
            model_name="paplink",
            name="code",
            field=models.CharField(max_length=64, unique=True, verbose_name="Code"),
        ),
        migrations.AlterField(
            model_name="paplink",
            name="created_at",
            field=models.DateTimeField(
                default=django.utils.timezone.now, verbose_name="Created at"
            ),
        ),
        migrations.AlterField(
            model_name="paplink",
            name="created_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="fleetops_pap_links",
                to=settings.AUTH_USER_MODEL,
                verbose_name="Created by",
            ),
        ),
        migrations.AlterField(
            model_name="paplink",
            name="expires_at",
            field=models.DateTimeField(db_index=True, verbose_name="Expires at"),
        ),
        migrations.AlterField(
            model_name="paplink",
            name="operation",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="pap_links",
                to="fleetops.fleetoperation",
                verbose_name="Operation",
            ),
        ),
        migrations.AlterField(
            model_name="paplink",
            name="require_online",
            field=models.BooleanField(
                default=True,
                help_text="Characters must be logged in to the game when they register.",
                verbose_name="Require online",
            ),
        ),
        migrations.AlterField(
            model_name="paplink",
            name="revoked_at",
            field=models.DateTimeField(
                blank=True, null=True, verbose_name="Revoked at"
            ),
        ),
        migrations.AlterField(
            model_name="papregistration",
            name="auth_user",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="fleetops_pap_registrations",
                to=settings.AUTH_USER_MODEL,
                verbose_name="Auth user",
            ),
        ),
        migrations.AlterField(
            model_name="papregistration",
            name="character_id",
            field=models.BigIntegerField(db_index=True, verbose_name="Character ID"),
        ),
        migrations.AlterField(
            model_name="papregistration",
            name="character_name",
            field=models.CharField(max_length=255, verbose_name="Character name"),
        ),
        migrations.AlterField(
            model_name="papregistration",
            name="link",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="registrations",
                to="fleetops.paplink",
                verbose_name="Link",
            ),
        ),
        migrations.AlterField(
            model_name="papregistration",
            name="online",
            field=models.BooleanField(blank=True, null=True, verbose_name="Online"),
        ),
        migrations.AlterField(
            model_name="papregistration",
            name="operation",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="pap_registrations",
                to="fleetops.fleetoperation",
                verbose_name="Operation",
            ),
        ),
        migrations.AlterField(
            model_name="papregistration",
            name="registered_at",
            field=models.DateTimeField(
                default=django.utils.timezone.now, verbose_name="Registered at"
            ),
        ),
        migrations.AlterField(
            model_name="papregistration",
            name="ship_type_id",
            field=models.BigIntegerField(
                blank=True, null=True, verbose_name="Ship type ID"
            ),
        ),
        migrations.AlterField(
            model_name="papregistration",
            name="ship_type_name",
            field=models.CharField(
                blank=True, max_length=255, verbose_name="Ship type name"
            ),
        ),
        migrations.AlterField(
            model_name="papregistration",
            name="solar_system_id",
            field=models.BigIntegerField(
                blank=True, null=True, verbose_name="Solar system ID"
            ),
        ),
        migrations.AlterField(
            model_name="papregistration",
            name="solar_system_name",
            field=models.CharField(
                blank=True, max_length=255, verbose_name="Solar system name"
            ),
        ),
        migrations.AlterField(
            model_name="pingrole",
            name="is_active",
            field=models.BooleanField(
                default=True,
                help_text="FCs can pick this role when they send a ping.",
                verbose_name="Is active",
            ),
        ),
        migrations.AlterField(
            model_name="pingrole",
            name="last_synced_at",
            field=models.DateTimeField(
                blank=True, null=True, verbose_name="Last synced at"
            ),
        ),
        migrations.AlterField(
            model_name="pingrole",
            name="missing_from_discord",
            field=models.BooleanField(
                default=False, verbose_name="Missing from Discord"
            ),
        ),
        migrations.AlterField(
            model_name="pingrole",
            name="name",
            field=models.CharField(
                help_text="Role name without the leading @.",
                max_length=100,
                verbose_name="Name",
            ),
        ),
        migrations.AlterField(
            model_name="pingrole",
            name="role_id",
            field=models.BigIntegerField(
                help_text="Discord role ID.",
                unique=True,
                validators=[
                    django.core.validators.MinValueValidator(1),
                    django.core.validators.MaxValueValidator(9223372036854775807),
                ],
                verbose_name="Role ID",
            ),
        ),
        migrations.AlterField(
            model_name="pingrole",
            name="sort_order",
            field=models.PositiveIntegerField(default=0, verbose_name="Sort order"),
        ),
        migrations.AlterField(
            model_name="pingrole",
            name="source",
            field=models.CharField(
                choices=[("manual", "Manual"), ("discord", "Synced from Discord")],
                default="manual",
                max_length=10,
                verbose_name="Source",
            ),
        ),
        migrations.AlterField(
            model_name="pingtarget",
            name="is_active",
            field=models.BooleanField(default=True, verbose_name="Is active"),
        ),
        migrations.AlterField(
            model_name="pingtarget",
            name="name",
            field=models.CharField(max_length=100, unique=True, verbose_name="Name"),
        ),
        migrations.AlterField(
            model_name="pingtarget",
            name="target_value",
            field=models.CharField(
                blank=True, max_length=255, verbose_name="Target value"
            ),
        ),
        migrations.AlterField(
            model_name="pingtarget",
            name="webhook",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                to="fleetops.discordwebhook",
                verbose_name="Webhook",
            ),
        ),
        migrations.AlterField(
            model_name="shipfunction",
            name="color",
            field=models.CharField(
                default="#6c757d",
                max_length=7,
                validators=[
                    django.core.validators.RegexValidator(
                        "^#[0-9a-fA-F]{6}$",
                        "Enter a color as # and six hex digits, such as #0d6efd.",
                    )
                ],
                verbose_name="Color",
            ),
        ),
        migrations.AlterField(
            model_name="shipfunction",
            name="is_active",
            field=models.BooleanField(default=True, verbose_name="Is active"),
        ),
        migrations.AlterField(
            model_name="shipfunction",
            name="name",
            field=models.CharField(max_length=60, unique=True, verbose_name="Name"),
        ),
        migrations.AlterField(
            model_name="shipfunction",
            name="sort_order",
            field=models.PositiveIntegerField(default=0, verbose_name="Sort order"),
        ),
        migrations.AlterField(
            model_name="shipfunctionrule",
            name="function",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="rules",
                to="fleetops.shipfunction",
                verbose_name="Function",
            ),
        ),
        migrations.AlterField(
            model_name="shipfunctionrule",
            name="kind",
            field=models.CharField(
                choices=[("type", "Ship type"), ("group", "Ship group")],
                max_length=10,
                verbose_name="Kind",
            ),
        ),
        migrations.AlterField(
            model_name="shipfunctionrule",
            name="label",
            field=models.CharField(
                blank=True,
                help_text="Type or group name when the rule was saved, shown without the SDE.",
                max_length=255,
                verbose_name="Label",
            ),
        ),
        migrations.AlterField(
            model_name="specialrole",
            name="is_active",
            field=models.BooleanField(
                default=True,
                help_text="FCs can assign this role.",
                verbose_name="Is active",
            ),
        ),
        migrations.AlterField(
            model_name="specialrole",
            name="key",
            field=models.SlugField(
                editable=False, max_length=30, unique=True, verbose_name="Key"
            ),
        ),
        migrations.AlterField(
            model_name="specialrole",
            name="name",
            field=models.CharField(max_length=100, unique=True, verbose_name="Name"),
        ),
        migrations.AlterField(
            model_name="specialrole",
            name="sort_order",
            field=models.PositiveIntegerField(default=0, verbose_name="Sort order"),
        ),
        migrations.AlterField(
            model_name="srpclaim",
            name="alliance_id",
            field=models.BigIntegerField(
                blank=True, null=True, verbose_name="Alliance ID"
            ),
        ),
        migrations.AlterField(
            model_name="srpclaim",
            name="alliance_name",
            field=models.CharField(
                blank=True, max_length=255, verbose_name="Alliance name"
            ),
        ),
        migrations.AlterField(
            model_name="srpclaim",
            name="approved_amount",
            field=models.PositiveBigIntegerField(
                blank=True, null=True, verbose_name="Approved amount"
            ),
        ),
        migrations.AlterField(
            model_name="srpclaim",
            name="auto_rejected",
            field=models.BooleanField(default=False, verbose_name="Auto rejected"),
        ),
        migrations.AlterField(
            model_name="srpclaim",
            name="base_amount",
            field=models.PositiveBigIntegerField(default=0, verbose_name="Base amount"),
        ),
        migrations.AlterField(
            model_name="srpclaim",
            name="calculation",
            field=models.JSONField(
                blank=True, default=dict, verbose_name="Calculation"
            ),
        ),
        migrations.AlterField(
            model_name="srpclaim",
            name="character_id",
            field=models.BigIntegerField(db_index=True, verbose_name="Character ID"),
        ),
        migrations.AlterField(
            model_name="srpclaim",
            name="character_name",
            field=models.CharField(max_length=255, verbose_name="Character name"),
        ),
        migrations.AlterField(
            model_name="srpclaim",
            name="corporation_id",
            field=models.BigIntegerField(
                blank=True, db_index=True, null=True, verbose_name="Corporation ID"
            ),
        ),
        migrations.AlterField(
            model_name="srpclaim",
            name="corporation_name",
            field=models.CharField(
                blank=True, max_length=255, verbose_name="Corporation name"
            ),
        ),
        migrations.AlterField(
            model_name="srpclaim",
            name="corporation_ticker",
            field=models.CharField(
                blank=True, max_length=20, verbose_name="Corporation ticker"
            ),
        ),
        migrations.AlterField(
            model_name="srpclaim",
            name="counts_corporation",
            field=models.BooleanField(default=True, verbose_name="Counts corporation"),
        ),
        migrations.AlterField(
            model_name="srpclaim",
            name="counts_personal",
            field=models.BooleanField(default=True, verbose_name="Counts personal"),
        ),
        migrations.AlterField(
            model_name="srpclaim",
            name="decided_at",
            field=models.DateTimeField(
                blank=True, null=True, verbose_name="Decided at"
            ),
        ),
        migrations.AlterField(
            model_name="srpclaim",
            name="decided_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="+",
                to=settings.AUTH_USER_MODEL,
                verbose_name="Decided by",
            ),
        ),
        migrations.AlterField(
            model_name="srpclaim",
            name="decision_reason",
            field=models.TextField(blank=True, verbose_name="Decision reason"),
        ),
        migrations.AlterField(
            model_name="srpclaim",
            name="fc_main_character_name",
            field=models.CharField(
                blank=True, max_length=255, verbose_name="FC main character name"
            ),
        ),
        migrations.AlterField(
            model_name="srpclaim",
            name="fc_user",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="fleetops_srp_fc_claims",
                to=settings.AUTH_USER_MODEL,
                verbose_name="FC user",
            ),
        ),
        migrations.AlterField(
            model_name="srpclaim",
            name="fleet_check",
            field=models.CharField(
                blank=True, db_index=True, max_length=20, verbose_name="Fleet check"
            ),
        ),
        migrations.AlterField(
            model_name="srpclaim",
            name="fleet_check_params",
            field=models.JSONField(
                blank=True, default=dict, verbose_name="Fleet check params"
            ),
        ),
        migrations.AlterField(
            model_name="srpclaim",
            name="fleet_checked_at",
            field=models.DateTimeField(
                blank=True, null=True, verbose_name="Fleet checked at"
            ),
        ),
        migrations.AlterField(
            model_name="srpclaim",
            name="fleet_label",
            field=models.CharField(
                blank=True, max_length=255, verbose_name="Fleet label"
            ),
        ),
        migrations.AlterField(
            model_name="srpclaim",
            name="killmail",
            field=models.OneToOneField(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="claim",
                to="fleetops.srpkillmail",
                verbose_name="Killmail",
            ),
        ),
        migrations.AlterField(
            model_name="srpclaim",
            name="killmail_time",
            field=models.DateTimeField(db_index=True, verbose_name="Killmail time"),
        ),
        migrations.AlterField(
            model_name="srpclaim",
            name="limited_amount",
            field=models.PositiveBigIntegerField(
                default=0, verbose_name="Limited amount"
            ),
        ),
        migrations.AlterField(
            model_name="srpclaim",
            name="link",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="claims",
                to="fleetops.srpfleetlink",
                verbose_name="Link",
            ),
        ),
        migrations.AlterField(
            model_name="srpclaim",
            name="loss_value",
            field=models.DecimalField(
                blank=True,
                decimal_places=2,
                max_digits=20,
                null=True,
                verbose_name="Loss value",
            ),
        ),
        migrations.AlterField(
            model_name="srpclaim",
            name="main_character_id",
            field=models.BigIntegerField(
                blank=True, null=True, verbose_name="Main character ID"
            ),
        ),
        migrations.AlterField(
            model_name="srpclaim",
            name="main_character_name",
            field=models.CharField(
                blank=True, max_length=255, verbose_name="Main character name"
            ),
        ),
        migrations.AlterField(
            model_name="srpclaim",
            name="member_comment",
            field=models.TextField(blank=True, verbose_name="Member comment"),
        ),
        migrations.AlterField(
            model_name="srpclaim",
            name="operation",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="srp_claims",
                to="fleetops.fleetoperation",
                verbose_name="Operation",
            ),
        ),
        migrations.AlterField(
            model_name="srpclaim",
            name="over_limit",
            field=models.BooleanField(
                db_index=True, default=False, verbose_name="Over limit"
            ),
        ),
        migrations.AlterField(
            model_name="srpclaim",
            name="paid_at",
            field=models.DateTimeField(
                blank=True, db_index=True, null=True, verbose_name="Paid at"
            ),
        ),
        migrations.AlterField(
            model_name="srpclaim",
            name="paid_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="+",
                to=settings.AUTH_USER_MODEL,
                verbose_name="Paid by",
            ),
        ),
        migrations.AlterField(
            model_name="srpclaim",
            name="payment_note",
            field=models.CharField(
                blank=True, max_length=255, verbose_name="Payment note"
            ),
        ),
        migrations.AlterField(
            model_name="srpclaim",
            name="payout_basis",
            field=models.CharField(
                choices=[
                    ("rule", "Payout table"),
                    ("percentage", "Percentage of the value"),
                    ("none", "No automatic amount"),
                ],
                default="none",
                max_length=12,
                verbose_name="Payout basis",
            ),
        ),
        migrations.AlterField(
            model_name="srpclaim",
            name="payout_rule",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="claims",
                to="fleetops.srppayoutrule",
                verbose_name="Payout rule",
            ),
        ),
        migrations.AlterField(
            model_name="srpclaim",
            name="policy",
            field=models.JSONField(blank=True, default=dict, verbose_name="Policy"),
        ),
        migrations.AlterField(
            model_name="srpclaim",
            name="scope",
            field=models.CharField(
                choices=[("in_fleet", "In fleet"), ("out_of_fleet", "Out of fleet")],
                db_index=True,
                max_length=12,
                verbose_name="Scope",
            ),
        ),
        migrations.AlterField(
            model_name="srpclaim",
            name="ship_type_id",
            field=models.BigIntegerField(db_index=True, verbose_name="Ship type ID"),
        ),
        migrations.AlterField(
            model_name="srpclaim",
            name="ship_type_name",
            field=models.CharField(
                blank=True, max_length=255, verbose_name="Ship type name"
            ),
        ),
        migrations.AlterField(
            model_name="srpclaim",
            name="solar_system_name",
            field=models.CharField(
                blank=True, max_length=100, verbose_name="Solar system name"
            ),
        ),
        migrations.AlterField(
            model_name="srpclaim",
            name="status",
            field=models.CharField(
                choices=[
                    ("pending", "Pending"),
                    ("approved", "Approved"),
                    ("paid", "Paid"),
                    ("rejected", "Rejected"),
                    ("withdrawn", "Withdrawn"),
                ],
                db_index=True,
                default="pending",
                max_length=12,
                verbose_name="Status",
            ),
        ),
        migrations.AlterField(
            model_name="srpclaim",
            name="submitted_at",
            field=models.DateTimeField(
                db_index=True,
                default=django.utils.timezone.now,
                verbose_name="Submitted at",
            ),
        ),
        migrations.AlterField(
            model_name="srpclaim",
            name="suggested_amount",
            field=models.PositiveBigIntegerField(
                default=0, verbose_name="Suggested amount"
            ),
        ),
        migrations.AlterField(
            model_name="srpclaim",
            name="updated_at",
            field=models.DateTimeField(auto_now=True, verbose_name="Updated at"),
        ),
        migrations.AlterField(
            model_name="srpclaim",
            name="user",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="fleetops_srp_claims",
                to=settings.AUTH_USER_MODEL,
                verbose_name="User",
            ),
        ),
        migrations.AlterField(
            model_name="srpclaim",
            name="uuid",
            field=models.UUIDField(
                default=uuid.uuid4, editable=False, unique=True, verbose_name="UUID"
            ),
        ),
        migrations.AlterField(
            model_name="srpclaim",
            name="value_source",
            field=models.CharField(
                blank=True, max_length=20, verbose_name="Value source"
            ),
        ),
        migrations.AlterField(
            model_name="srpclaim",
            name="warnings",
            field=models.JSONField(blank=True, default=list, verbose_name="Warnings"),
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
            model_name="srpclaimevent",
            name="actor",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="+",
                to=settings.AUTH_USER_MODEL,
                verbose_name="Actor",
            ),
        ),
        migrations.AlterField(
            model_name="srpclaimevent",
            name="amount",
            field=models.PositiveBigIntegerField(
                blank=True, null=True, verbose_name="Amount"
            ),
        ),
        migrations.AlterField(
            model_name="srpclaimevent",
            name="claim",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="events",
                to="fleetops.srpclaim",
                verbose_name="Claim",
            ),
        ),
        migrations.AlterField(
            model_name="srpclaimevent",
            name="created_at",
            field=models.DateTimeField(
                db_index=True,
                default=django.utils.timezone.now,
                verbose_name="Created at",
            ),
        ),
        migrations.AlterField(
            model_name="srpclaimevent",
            name="data",
            field=models.JSONField(blank=True, default=dict, verbose_name="Data"),
        ),
        migrations.AlterField(
            model_name="srpclaimevent",
            name="from_status",
            field=models.CharField(
                blank=True, max_length=12, verbose_name="From status"
            ),
        ),
        migrations.AlterField(
            model_name="srpclaimevent",
            name="internal",
            field=models.BooleanField(default=False, verbose_name="Internal"),
        ),
        migrations.AlterField(
            model_name="srpclaimevent",
            name="reason",
            field=models.TextField(blank=True, verbose_name="Reason"),
        ),
        migrations.AlterField(
            model_name="srpclaimevent",
            name="to_status",
            field=models.CharField(blank=True, max_length=12, verbose_name="To status"),
        ),
        migrations.AlterField(
            model_name="srpcorporationlimit",
            name="corporation_id",
            field=models.BigIntegerField(unique=True, verbose_name="Corporation ID"),
        ),
        migrations.AlterField(
            model_name="srpcorporationlimit",
            name="corporation_name",
            field=models.CharField(
                blank=True, max_length=255, verbose_name="Corporation name"
            ),
        ),
        migrations.AlterField(
            model_name="srpcorporationlimit",
            name="monthly_cap",
            field=models.PositiveBigIntegerField(
                blank=True,
                help_text="Maximum approved SRP of this corporation per EVE month. Empty = no limit for this corporation.",
                null=True,
                validators=[
                    django.core.validators.MinValueValidator(1),
                    django.core.validators.MaxValueValidator(1000000000000000),
                ],
                verbose_name="Monthly cap",
            ),
        ),
        migrations.AlterField(
            model_name="srpcorporationlimit",
            name="note",
            field=models.CharField(blank=True, max_length=255, verbose_name="Note"),
        ),
        migrations.AlterField(
            model_name="srpfleetlink",
            name="closed_at",
            field=models.DateTimeField(blank=True, null=True, verbose_name="Closed at"),
        ),
        migrations.AlterField(
            model_name="srpfleetlink",
            name="closed_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="+",
                to=settings.AUTH_USER_MODEL,
                verbose_name="Closed by",
            ),
        ),
        migrations.AlterField(
            model_name="srpfleetlink",
            name="code",
            field=models.CharField(max_length=32, unique=True, verbose_name="Code"),
        ),
        migrations.AlterField(
            model_name="srpfleetlink",
            name="created_at",
            field=models.DateTimeField(
                default=django.utils.timezone.now, verbose_name="Created at"
            ),
        ),
        migrations.AlterField(
            model_name="srpfleetlink",
            name="created_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="+",
                to=settings.AUTH_USER_MODEL,
                verbose_name="Created by",
            ),
        ),
        migrations.AlterField(
            model_name="srpfleetlink",
            name="fc_main_character_id",
            field=models.BigIntegerField(
                blank=True, null=True, verbose_name="FC main character ID"
            ),
        ),
        migrations.AlterField(
            model_name="srpfleetlink",
            name="fc_main_character_name",
            field=models.CharField(
                blank=True, max_length=255, verbose_name="FC main character name"
            ),
        ),
        migrations.AlterField(
            model_name="srpfleetlink",
            name="fc_user",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="fleetops_srp_links",
                to=settings.AUTH_USER_MODEL,
                verbose_name="FC user",
            ),
        ),
        migrations.AlterField(
            model_name="srpfleetlink",
            name="is_open",
            field=models.BooleanField(default=True, verbose_name="Is open"),
        ),
        migrations.AlterField(
            model_name="srpfleetlink",
            name="operation",
            field=models.OneToOneField(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="srp_link",
                to="fleetops.fleetoperation",
                verbose_name="Operation",
            ),
        ),
        migrations.AlterField(
            model_name="srpkillmail",
            name="attacker_count",
            field=models.PositiveIntegerField(default=0, verbose_name="Attacker count"),
        ),
        migrations.AlterField(
            model_name="srpkillmail",
            name="esi_estimate",
            field=models.DecimalField(
                blank=True,
                decimal_places=2,
                max_digits=20,
                null=True,
                verbose_name="ESI estimate",
            ),
        ),
        migrations.AlterField(
            model_name="srpkillmail",
            name="esi_estimate_at",
            field=models.DateTimeField(
                blank=True, null=True, verbose_name="ESI estimate at"
            ),
        ),
        migrations.AlterField(
            model_name="srpkillmail",
            name="fetched_at",
            field=models.DateTimeField(
                default=django.utils.timezone.now, verbose_name="Fetched at"
            ),
        ),
        migrations.AlterField(
            model_name="srpkillmail",
            name="items",
            field=models.JSONField(blank=True, default=list, verbose_name="Items"),
        ),
        migrations.AlterField(
            model_name="srpkillmail",
            name="killmail_hash",
            field=models.CharField(max_length=40, verbose_name="Killmail hash"),
        ),
        migrations.AlterField(
            model_name="srpkillmail",
            name="killmail_id",
            field=models.BigIntegerField(
                primary_key=True, serialize=False, verbose_name="Killmail ID"
            ),
        ),
        migrations.AlterField(
            model_name="srpkillmail",
            name="killmail_time",
            field=models.DateTimeField(db_index=True, verbose_name="Killmail time"),
        ),
        migrations.AlterField(
            model_name="srpkillmail",
            name="ship_type_id",
            field=models.BigIntegerField(verbose_name="Ship type ID"),
        ),
        migrations.AlterField(
            model_name="srpkillmail",
            name="ship_type_name",
            field=models.CharField(
                blank=True, max_length=255, verbose_name="Ship type name"
            ),
        ),
        migrations.AlterField(
            model_name="srpkillmail",
            name="solar_system_id",
            field=models.BigIntegerField(verbose_name="Solar system ID"),
        ),
        migrations.AlterField(
            model_name="srpkillmail",
            name="solar_system_name",
            field=models.CharField(
                blank=True, max_length=100, verbose_name="Solar system name"
            ),
        ),
        migrations.AlterField(
            model_name="srpkillmail",
            name="victim_alliance_id",
            field=models.BigIntegerField(
                blank=True, null=True, verbose_name="Victim alliance ID"
            ),
        ),
        migrations.AlterField(
            model_name="srpkillmail",
            name="victim_character_id",
            field=models.BigIntegerField(
                blank=True, db_index=True, null=True, verbose_name="Victim character ID"
            ),
        ),
        migrations.AlterField(
            model_name="srpkillmail",
            name="victim_corporation_id",
            field=models.BigIntegerField(
                blank=True, null=True, verbose_name="Victim corporation ID"
            ),
        ),
        migrations.AlterField(
            model_name="srpkillmail",
            name="war_id",
            field=models.BigIntegerField(blank=True, null=True, verbose_name="War ID"),
        ),
        migrations.AlterField(
            model_name="srpkillmail",
            name="zkill_attempts",
            field=models.PositiveSmallIntegerField(
                default=0, verbose_name="zKillboard attempts"
            ),
        ),
        migrations.AlterField(
            model_name="srpkillmail",
            name="zkill_awox",
            field=models.BooleanField(
                blank=True, null=True, verbose_name="zKillboard awox"
            ),
        ),
        migrations.AlterField(
            model_name="srpkillmail",
            name="zkill_checked_at",
            field=models.DateTimeField(
                blank=True, null=True, verbose_name="zKillboard checked at"
            ),
        ),
        migrations.AlterField(
            model_name="srpkillmail",
            name="zkill_destroyed_value",
            field=models.DecimalField(
                blank=True,
                decimal_places=2,
                max_digits=20,
                null=True,
                verbose_name="zKillboard destroyed value",
            ),
        ),
        migrations.AlterField(
            model_name="srpkillmail",
            name="zkill_dropped_value",
            field=models.DecimalField(
                blank=True,
                decimal_places=2,
                max_digits=20,
                null=True,
                verbose_name="zKillboard dropped value",
            ),
        ),
        migrations.AlterField(
            model_name="srpkillmail",
            name="zkill_fitted_value",
            field=models.DecimalField(
                blank=True,
                decimal_places=2,
                max_digits=20,
                null=True,
                verbose_name="zKillboard fitted value",
            ),
        ),
        migrations.AlterField(
            model_name="srpkillmail",
            name="zkill_labels",
            field=models.JSONField(
                blank=True, default=list, verbose_name="zKillboard labels"
            ),
        ),
        migrations.AlterField(
            model_name="srpkillmail",
            name="zkill_next_attempt_at",
            field=models.DateTimeField(
                blank=True,
                db_index=True,
                null=True,
                verbose_name="zKillboard next attempt at",
            ),
        ),
        migrations.AlterField(
            model_name="srpkillmail",
            name="zkill_npc",
            field=models.BooleanField(
                blank=True, null=True, verbose_name="zKillboard NPC"
            ),
        ),
        migrations.AlterField(
            model_name="srpkillmail",
            name="zkill_solo",
            field=models.BooleanField(
                blank=True, null=True, verbose_name="zKillboard solo"
            ),
        ),
        migrations.AlterField(
            model_name="srpkillmail",
            name="zkill_status",
            field=models.CharField(
                choices=[
                    ("pending", "Waiting"),
                    ("ok", "Value available"),
                    ("missing", "Not on zKillboard yet"),
                    ("unavailable", "zKillboard value unavailable"),
                    ("disabled", "zKillboard disabled"),
                ],
                db_index=True,
                default="pending",
                max_length=12,
                verbose_name="zKillboard status",
            ),
        ),
        migrations.AlterField(
            model_name="srpkillmail",
            name="zkill_total_value",
            field=models.DecimalField(
                blank=True,
                decimal_places=2,
                max_digits=20,
                null=True,
                verbose_name="zKillboard total value",
            ),
        ),
        migrations.AlterField(
            model_name="srppayoutrule",
            name="amount",
            field=models.PositiveBigIntegerField(
                help_text="Whole ISK.",
                validators=[
                    django.core.validators.MinValueValidator(1),
                    django.core.validators.MaxValueValidator(1000000000000000),
                ],
                verbose_name="Amount",
            ),
        ),
        migrations.AlterField(
            model_name="srppayoutrule",
            name="doctrine_external_id",
            field=models.CharField(
                blank=True, max_length=255, verbose_name="Doctrine external ID"
            ),
        ),
        migrations.AlterField(
            model_name="srppayoutrule",
            name="doctrine_key",
            field=models.CharField(
                blank=True,
                db_index=True,
                editable=False,
                max_length=400,
                verbose_name="Doctrine key",
            ),
        ),
        migrations.AlterField(
            model_name="srppayoutrule",
            name="doctrine_name",
            field=models.CharField(
                blank=True, max_length=255, verbose_name="Doctrine name"
            ),
        ),
        migrations.AlterField(
            model_name="srppayoutrule",
            name="doctrine_source",
            field=models.CharField(
                blank=True, max_length=100, verbose_name="Doctrine source"
            ),
        ),
        migrations.AlterField(
            model_name="srppayoutrule",
            name="is_active",
            field=models.BooleanField(default=True, verbose_name="Is active"),
        ),
        migrations.AlterField(
            model_name="srppayoutrule",
            name="label",
            field=models.CharField(blank=True, max_length=100, verbose_name="Label"),
        ),
        migrations.AlterField(
            model_name="srppayoutrule",
            name="notes",
            field=models.CharField(blank=True, max_length=255, verbose_name="Notes"),
        ),
        migrations.AlterField(
            model_name="srppayoutrule",
            name="scope",
            field=models.CharField(
                choices=[
                    ("any", "In fleet and out of fleet"),
                    ("in_fleet", "In-fleet losses only"),
                    ("out_of_fleet", "Out-of-fleet losses only"),
                ],
                default="any",
                max_length=12,
                verbose_name="Scope",
            ),
        ),
        migrations.AlterField(
            model_name="srppayoutrule",
            name="ship_type_name",
            field=models.CharField(
                blank=True, max_length=255, verbose_name="Ship type name"
            ),
        ),
        migrations.AlterField(
            model_name="srppayoutrule",
            name="updated_at",
            field=models.DateTimeField(auto_now=True, verbose_name="Updated at"),
        ),
        migrations.AlterField(
            model_name="srpsettings",
            name="enabled",
            field=models.BooleanField(
                default=False,
                help_text="Members can submit FleetOps SRP claims and fleets get FleetOps SRP links.",
                verbose_name="Enabled",
            ),
        ),
        migrations.AlterField(
            model_name="srpsettings",
            name="member_instructions",
            field=models.TextField(
                blank=True,
                help_text="Shown to members on the SRP pages.",
                validators=[django.core.validators.MaxLengthValidator(2000)],
                verbose_name="Member instructions",
            ),
        ),
        migrations.AlterField(
            model_name="srpsettings",
            name="over_limit_action",
            field=models.CharField(
                choices=[
                    ("review", "Send to review with a warning"),
                    ("reject", "Reject automatically"),
                ],
                default="review",
                help_text="What happens to a claim that does not fit into the remaining monthly limit. When it only partly fits it is cut to the remaining amount and sent to review; it is rejected automatically only when nothing is left.",
                max_length=10,
                verbose_name="Over limit action",
            ),
        ),
        migrations.AlterField(
            model_name="srpsettings",
            name="updated_at",
            field=models.DateTimeField(auto_now=True, verbose_name="Updated at"),
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
                    ("zkill_total", "zKillboard total value (includes cargo)"),
                    ("esi_estimate", "ESI market estimate"),
                ],
                default="zkill_fitted",
                help_text="Where the loss value comes from. The ESI market estimate is also the fallback when zKillboard has no value.",
                max_length=20,
                verbose_name="Value source",
            ),
        ),
        migrations.AlterField(
            model_name="statisticstarget",
            name="month",
            field=models.PositiveSmallIntegerField(
                validators=[
                    django.core.validators.MinValueValidator(1),
                    django.core.validators.MaxValueValidator(12),
                ],
                verbose_name="Month",
            ),
        ),
        migrations.AlterField(
            model_name="statisticstarget",
            name="note",
            field=models.CharField(blank=True, max_length=255, verbose_name="Note"),
        ),
        migrations.AlterField(
            model_name="statisticstarget",
            name="year",
            field=models.PositiveIntegerField(
                validators=[
                    django.core.validators.MinValueValidator(2003),
                    django.core.validators.MaxValueValidator(2200),
                ],
                verbose_name="Year",
            ),
        ),
    ]
