FLEET_READ_SCOPE = "esi-fleets.read_fleet.v1"
FLEET_WRITE_SCOPE = "esi-fleets.write_fleet.v1"
FLEET_SCOPES = [FLEET_READ_SCOPE, FLEET_WRITE_SCOPE]

DEFAULT_PING_TEMPLATE = """{{ ping_target }}\n**{{ fleet_type }}**\nFC: {{ fc }}\nForm Up: {{ formup }}\nDoctrine: {{ doctrine }}\nComms: {{ comms }}{% if comms_link %} — {{ comms_link }}{% endif %}\n{% if logi_channel %}Logi: {{ logi_channel }}\n{% endif %}{% if boost_channel %}Boost: {{ boost_channel }}\n{% endif %}{% if fleet_time %}Time: {{ fleet_time }}\n{% endif %}{% if additional_message %}\n{{ additional_message }}{% endif %}"""

DEFAULT_MOTD_TEMPLATE = """<b>{{ fleet_type }}</b><br>FC: {{ fc }}<br>Form Up: {{ formup }}<br>Doctrine: {{ doctrine }}<br>Comms: {{ comms }}{% if comms_link %} — {{ comms_link }}{% endif %}{% if logi_channel %}<br>Logi: {{ logi_channel }}{% endif %}{% if boost_channel %}<br>Boost: {{ boost_channel }}{% endif %}{% if additional_message %}<br><br>{{ additional_message }}{% endif %}"""
