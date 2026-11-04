# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Field validators shared by models, forms and message rendering."""

import re
from urllib.parse import urlsplit

from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

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
            _("Enter a voice link starting with %(schemes)s."),
            code="invalid",
            params={"schemes": ", ".join(f"{scheme}://" for scheme in VOICE_URL_SCHEMES)},
        )
    if any(ch.isspace() or ord(ch) < 32 for ch in value):
        raise ValidationError(_("A voice link cannot contain spaces; write a space as %20."), code="whitespace")
    # The public join page shows the link to everybody who has it.
    if _has_userinfo(value):
        raise ValidationError(_("Remove the personal user name and password from the voice link."), code="userinfo")


# Placeholders of the name an automatically made SRP link gets, such as "{category} {date}".
LINK_NAME_FIELDS = ("category", "date")


def validate_link_name_template(value):
    """Accept only the placeholders ``{category}`` and ``{date}``, without conversions or format options.

    The template is filled in with ``str.format_map``; anything else in braces (attribute access, indexes, widths)
    is refused, so a template cannot read other data or build huge strings.
    """
    import string

    try:
        parsed = list(string.Formatter().parse(value or ""))
    except ValueError:
        raise ValidationError(
            _("The name template has a brace that is not closed. Write a literal brace twice."), code="invalid"
        ) from None
    for _literal, field, spec, conversion in parsed:
        if field is None:
            continue
        if field not in LINK_NAME_FIELDS or spec or conversion:
            raise ValidationError(
                _("The name template may only use %(fields)s."),
                code="invalid",
                params={"fields": ", ".join("{%s}" % name for name in LINK_NAME_FIELDS)},
            )


def render_link_name(template, *, category, date):
    """The link name from ``template`` (already validated); an invalid template falls back to the default."""
    try:
        validate_link_name_template(template)
        return (template or "").format_map({"category": category, "date": date})
    except ValidationError:
        return f"{category} {date}"
