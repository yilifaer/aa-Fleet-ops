FLEET_READ_SCOPE = "esi-fleets.read_fleet.v1"
FLEET_WRITE_SCOPE = "esi-fleets.write_fleet.v1"
FLEET_SCOPES = [FLEET_READ_SCOPE, FLEET_WRITE_SCOPE]
# PAP link registration reads where a character is, its ship and whether it is online. The same scopes as
# AFAT's click tracking, so tokens AFAT or CharLink already hold are reused.
CLICK_SCOPES = [
    "esi-location.read_location.v1",
    "esi-location.read_ship_type.v1",
    "esi-location.read_online.v1",
]

# Built-in templates, also used as the fallback when a stored template cannot be rendered.
# Migration 0009 keeps copies of the ping and of the MOTD as they were before the SRP line, migration 0017 a copy
# of the current MOTD; the copies of the current templates must stay identical to these.
DEFAULT_PING_TEMPLATE = """{% if ping_mention %}{{ ping_mention }}
{% endif %}{% if additional_message %}{{ additional_message }}

{% endif %}**FC Name:** {{ fc }}
**Formup Location:** {{ formup }}
**PAP Type:** {{ fleet_type }}
{% if comms %}**Comms:** {% if comms_link %}[{{ comms }}]({{ comms_link }}){% else %}{{ comms }}{% endif %}
{% endif %}{% if doctrine_name %}**Doctrine:** {% if doctrine_url %}[{{ doctrine_name }}]({{ doctrine_url }}){% else %}{{ doctrine_name }}{% endif %}{% if doctrine_ships %} ({{ doctrine_ships }}){% endif %}
{% endif %}{% if is_pre_ping %}**Fleet Time:** {{ fleet_time_eve }} ({{ fleet_time_discord }})
{% endif %}
-# This was a FleetOps broadcast from {{ sender }} to {{ ping_target_label }} at {{ sent_at }} EVE"""

DEFAULT_MOTD_TEMPLATE = """<font color="#bfffffff"><b>{{ fleet_type }}</b><br>FC: {{ fc }}<br>Form Up: {{ formup }}{% if doctrine_name %}<br>Doctrine: {% if doctrine_url %}<a href="{{ doctrine_url }}">{{ doctrine_name }}</a>{% else %}{{ doctrine_name }}{% endif %}{% if doctrine_ships %} ({{ doctrine_ships }}){% endif %}{% endif %}{% if comms %}<br>Comms: {% if comms_link %}<a href="{{ comms_link }}">{{ comms }}</a>{% else %}{{ comms }}{% endif %}{% endif %}{% if logi_channel %}<br>Logi: {{ logi_channel_link }}{% endif %}{% if boost_channel %}<br>Boost: {{ boost_channel_link }}{% endif %}{% if srp_url %}<br>SRP: <a href="{{ srp_url }}">Request SRP</a>{% endif %}{% if additional_message %}<br><br>{{ additional_message|linebreaksbr }}{% endif %}</font>"""

# Posted without any mention in the pre-ping channel when a scheduled fleet is cancelled.
SCHEDULE_CANCELLED_TEMPLATE = """**Cancelled:** {{ fleet_type }} at {{ fleet_time_eve }} ({{ formup }})
-# FleetOps notice from {{ sender }} at {{ sent_at }} EVE"""

# Longer MOTDs only get a warning; the in-game limit is not documented.
MOTD_SOFT_LIMIT = 1000

# Type IDs of the capsules (Capsule and its Genolution 'Auroral' variant). SRP covers them only when the payout
# table lists a capsule.
CAPSULE_TYPE_IDS = frozenset({670, 33328})

# Largest ISK amount an SRP payout rule, cap or claim can hold.
MAX_ISK = 10**15
