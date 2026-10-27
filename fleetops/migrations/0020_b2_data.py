# AA FleetOps 0.2.0b2: data. The untouched default ping gets the local fleet time and the group line, the untouched
# default MOTD gets the clickable FC and form-up links, and step texts written by 0.1.0a5 and 0.1.0a6 are brought
# to the wording the application stores today. Only templates that still equal the 0.2.0b1 default are replaced
# (whatever their name); edited and custom templates are left alone. Going back restores the 0.2.0b1 defaults the
# same way: a template edited after the upgrade is kept, step texts stay as they are.

import re

from django.db import migrations

# The default ping and MOTD of 0.2.0b1, and of 0.2.0b2.
OLD_PING = """{% if ping_mention %}{{ ping_mention }}
{% endif %}{% if additional_message %}{{ additional_message }}

{% endif %}**FC Name:** {{ fc }}
**Formup Location:** {{ formup }}
**PAP Type:** {{ fleet_type }}
{% if comms %}**Comms:** {% if comms_link %}[{{ comms }}]({{ comms_link }}){% else %}{{ comms }}{% endif %}
{% endif %}{% if doctrine_name %}**Doctrine:** {% if doctrine_url %}[{{ doctrine_name }}]({{ doctrine_url }}){% else %}{{ doctrine_name }}{% endif %}{% if doctrine_ships %} ({{ doctrine_ships }}){% endif %}
{% endif %}{% if is_pre_ping %}**Fleet Time:** {{ fleet_time_eve }} ({{ fleet_time_discord }})
{% endif %}
-# This was a FleetOps broadcast from {{ sender }} to {{ ping_target_label }} at {{ sent_at }} EVE"""
NEW_PING = """{% if ping_mention %}{{ ping_mention }}
{% endif %}{% if additional_message %}{{ additional_message }}

{% endif %}**FC Name:** {{ fc }}
**Formup Location:** {{ formup }}
**PAP Type:** {{ fleet_type }}
{% if group %}**Group:** {{ group }}
{% endif %}{% if comms %}**Comms:** {% if comms_link %}[{{ comms }}]({{ comms_link }}){% else %}{{ comms }}{% endif %}
{% endif %}{% if doctrine_name %}**Doctrine:** {% if doctrine_url %}[{{ doctrine_name }}]({{ doctrine_url }}){% else %}{{ doctrine_name }}{% endif %}{% if doctrine_ships %} ({{ doctrine_ships }}){% endif %}
{% endif %}{% if is_pre_ping %}**Fleet Time:** {{ fleet_time_eve }} · {{ fleet_time_local }} ({{ fleet_time_discord }})
{% endif %}
-# This was a FleetOps broadcast from {{ sender }} to {{ ping_target_label }} at {{ sent_at }} EVE"""
OLD_MOTD = """<font color="#bfffffff"><b>{{ fleet_type }}</b><br>FC: {{ fc }}<br>Form Up: {{ formup }}{% if doctrine_name %}<br>Doctrine: {% if doctrine_url %}<a href="{{ doctrine_url }}">{{ doctrine_name }}</a>{% else %}{{ doctrine_name }}{% endif %}{% if doctrine_ships %} ({{ doctrine_ships }}){% endif %}{% endif %}{% if comms %}<br>Comms: {% if comms_link %}<a href="{{ comms_link }}">{{ comms }}</a>{% else %}{{ comms }}{% endif %}{% endif %}{% if logi_channel %}<br>Logi: {{ logi_channel_link }}{% endif %}{% if boost_channel %}<br>Boost: {{ boost_channel_link }}{% endif %}{% if srp_url %}<br>SRP: <a href="{{ srp_url }}">Request SRP</a>{% endif %}{% if additional_message %}<br><br>{{ additional_message|linebreaksbr }}{% endif %}</font>"""
NEW_MOTD = """<font color="#bfffffff"><b>{{ fleet_type }}</b><br>FC: {{ fc_link }}<br>Form Up: {{ formup_link }}{% if doctrine_name %}<br>Doctrine: {% if doctrine_url %}<a href="{{ doctrine_url }}">{{ doctrine_name }}</a>{% else %}{{ doctrine_name }}{% endif %}{% if doctrine_ships %} ({{ doctrine_ships }}){% endif %}{% endif %}{% if comms %}<br>Comms: {% if comms_link %}<a href="{{ comms_link }}">{{ comms }}</a>{% else %}{{ comms }}{% endif %}{% endif %}{% if logi_channel %}<br>Logi: {{ logi_channel_link }}{% endif %}{% if boost_channel %}<br>Boost: {{ boost_channel_link }}{% endif %}{% if srp_url %}<br>SRP: <a href="{{ srp_url }}">Request SRP</a>{% endif %}{% if additional_message %}<br><br>{{ additional_message|linebreaksbr }}{% endif %}</font>"""

# Step texts of 0.1.0a5 and 0.1.0a6 and the wording that replaces them (the stored texts of fleetops.texts).
END_MANUAL = "Ended manually"
END_FLEET_GONE = "Ended automatically: the fleet no longer exists or the FC left it"
NO_PING_CHANNEL = "No Discord channel selected; the ping is available to copy."
JOB_FLEET_MISSING = "The fleet no longer exists."
FLEET_END_TEXTS = {"Manual": END_MANUAL, "Automatic": END_FLEET_GONE}
ANY_STEP_TEXTS = {
    "No active webhook configured. Ping remains available for manual copy.": NO_PING_CHANNEL,
    "Fleet no longer exists.": JOB_FLEET_MISSING,
}
ATTENDANCE_ONLY = re.compile(r"^Attendance-only mode: (Discord ping|MOTD update|SRP creation) intentionally skipped\.$")


def _normalized(content):
    return (content or "").replace("\r\n", "\n").strip()


def _replace_default(apps, template_type, old, new):
    MessageTemplate = apps.get_model("fleetops", "MessageTemplate")
    for pk, content in MessageTemplate.objects.filter(template_type=template_type).values_list("pk", "content"):
        if _normalized(content) == _normalized(old):
            MessageTemplate.objects.filter(pk=pk).update(content=new)


def update_default_ping(apps, schema_editor):
    _replace_default(apps, "ping", OLD_PING, NEW_PING)


def restore_default_ping(apps, schema_editor):
    _replace_default(apps, "ping", NEW_PING, OLD_PING)


def update_default_motd(apps, schema_editor):
    _replace_default(apps, "motd", OLD_MOTD, NEW_MOTD)


def restore_default_motd(apps, schema_editor):
    _replace_default(apps, "motd", NEW_MOTD, OLD_MOTD)


def _rewrite(queryset, column, mapping):
    for old, new in mapping.items():
        queryset.filter(**{column: old}).update(**{column: new})
    for pk, text in queryset.filter(**{column + "__startswith": "Attendance-only mode: "}).values_list("pk", column):
        found = ATTENDANCE_ONLY.fullmatch(text)
        if found:
            queryset.filter(pk=pk).update(**{column: f"PAP-only mode: {found.group(1)} intentionally skipped."})


def rewrite_legacy_step_texts(apps, schema_editor):
    Action = apps.get_model("fleetops", "OperationAction")
    Attempt = apps.get_model("fleetops", "OperationActionAttempt")
    Operation = apps.get_model("fleetops", "FleetOperation")
    # "Manual" and "Automatic" are only the result of the fleet end; other steps keep their own words.
    for queryset, column, prefix in (
        (Action.objects, "error_message", ""),
        (Attempt.objects, "message", "action__"),
    ):
        for old, new in FLEET_END_TEXTS.items():
            queryset.filter(**{prefix + "action": "fleet_end", column: old}).update(**{column: new})
        _rewrite(queryset, column, ANY_STEP_TEXTS)
    _rewrite(Operation.objects, "last_error", ANY_STEP_TEXTS)


class Migration(migrations.Migration):

    dependencies = [
        ("fleetops", "0019_b2_fields"),
    ]

    operations = [
        migrations.RunPython(update_default_ping, restore_default_ping),
        migrations.RunPython(update_default_motd, restore_default_motd),
        migrations.RunPython(rewrite_legacy_step_texts, migrations.RunPython.noop),
    ]
