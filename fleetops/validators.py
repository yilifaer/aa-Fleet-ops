"""Field validators shared by models, forms and message rendering."""

import re
from urllib.parse import urlsplit

from django.core.exceptions import ValidationError

# Voice links offered to fleet members. Anything else (javascript:, data:, file:) is refused so a
# stored link can never run script when somebody clicks it.
VOICE_URL_SCHEMES = ("mumble", "ts3server", "teamspeak", "discord", "https", "http")

# In-game channel ids as EVE shows them in a chat link: player_<32 hex digits>.
INGAME_CHANNEL_ID_RE = r"^player_[0-9a-fA-F]{32}$"

_SCHEME_RE = re.compile(r"^([A-Za-z][A-Za-z0-9+.-]*):")


def voice_url_scheme(value):
    match = _SCHEME_RE.match((value or "").strip())
    return match.group(1).lower() if match else ""


def _has_userinfo(value):
    try:
        return "@" in urlsplit(value).netloc
    except ValueError:
        return False


def validate_voice_url(value):
    value = (value or "").strip()
    if not value:
        return
    if voice_url_scheme(value) not in VOICE_URL_SCHEMES:
        raise ValidationError(
            "Enter a voice link starting with %(schemes)s.",
            code="invalid",
            params={"schemes": ", ".join(f"{scheme}://" for scheme in VOICE_URL_SCHEMES)},
        )
    if any(ch.isspace() or ord(ch) < 32 for ch in value):
        raise ValidationError("A voice link cannot contain spaces; write a space as %20.", code="whitespace")
    # The public join page shows the link to everybody who has it.
    if _has_userinfo(value):
        raise ValidationError("Remove the personal user name and password from the voice link.", code="userinfo")
