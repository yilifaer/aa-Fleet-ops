"""Absolute links used in pings, MOTDs and on the configuration pages."""

import html
import re
from urllib.parse import unquote, urlsplit, urlunsplit

from django.conf import settings
from django.urls import NoReverseMatch, reverse
from django.utils.html import escape, format_html, strip_tags

from fleetops.validators import INGAME_CHANNEL_ID_RE, VOICE_URL_SCHEMES, voice_url_scheme

# Voice clients open these schemes directly; everything else goes through the public join page.
DIRECT_LINK_SCHEMES = ("http", "https")

_CHANNEL_ID_RE = re.compile(INGAME_CHANNEL_ID_RE)
# EVE chat links: <a href="joinChannel:player_…">Name</a> or <url=joinChannel:player_…//None//None>Name</url>.
_JOIN_CHANNEL_RE = re.compile(r"(?i:joinchannel):(player_[0-9a-fA-F]{32})(?![0-9A-Za-z_])")
_BARE_CHANNEL_RE = re.compile(r"(?<![0-9A-Za-z_])player_[0-9a-fA-F]{32}(?![0-9A-Za-z_])")
_LINK_TEXT_RE = re.compile(r"<a\b[^>]*>(.*?)</a\s*>|<url=[^>]*>(.*?)</url\s*>", re.IGNORECASE | re.DOTALL)


def site_url_configured() -> bool:
    return bool(str(getattr(settings, "SITE_URL", "") or "").strip())


def absolute_url(path: str) -> str:
    """Prefix a site path with ``SITE_URL``; without a configured ``SITE_URL`` the path is returned."""
    path = path or ""
    if not path or urlsplit(path).scheme:
        return path
    base = str(getattr(settings, "SITE_URL", "") or "").strip().rstrip("/")
    if not base:
        return path
    return f"{base}{path if path.startswith('/') else '/' + path}"


def strip_url_userinfo(url: str) -> tuple[str, str]:
    """Remove ``user@`` or ``user:password@`` from a link.

    Returns the cleaned link and the removed user name (without any password) so callers can tell the
    admin what was dropped. The user name is "" when the link had none.
    """
    url = (url or "").strip()
    try:
        parts = urlsplit(url)
    except ValueError:
        return url, ""
    if "@" not in parts.netloc:
        return url, ""
    userinfo, host = parts.netloc.rsplit("@", 1)
    user = unquote(userinfo.split(":", 1)[0])
    if f"//{parts.netloc}" in url:
        # Edit the text in place so the rest of the link keeps its exact spelling.
        return url.replace(f"//{parts.netloc}", f"//{host}", 1), user
    return urlunsplit(parts._replace(netloc=host)), user


def public_voice_url(preset) -> str:
    """The preset's voice link as members may see it: never with a user name or password."""
    voice_url = (getattr(preset, "voice_url", "") or "").strip() if preset is not None else ""
    return strip_url_userinfo(voice_url)[0]


def comms_join_url(preset) -> str:
    """Absolute link to the public join page of a comms preset; "" when there is nothing to join."""
    voice_url = public_voice_url(preset)
    token = getattr(preset, "join_token", None)
    if not voice_url or not token or voice_url_scheme(voice_url) not in VOICE_URL_SCHEMES:
        return ""
    try:
        return absolute_url(reverse("fleetops:comms_join", args=[token]))
    except NoReverseMatch:
        return ""


def comms_link(preset) -> str:
    """Link for pings and MOTDs: web links as they are, voice client links through the join page."""
    voice_url = public_voice_url(preset)
    if not voice_url:
        return ""
    if voice_url_scheme(voice_url) in DIRECT_LINK_SCHEMES:
        return voice_url
    return comms_join_url(preset)


def extract_ingame_channel_id(text: str) -> str:
    """The first ``player_<32 hex>`` channel id in a pasted in-game link, or a bare id; "" if none."""
    text = text or ""
    match = _JOIN_CHANNEL_RE.search(text)
    if match:
        return match.group(1)
    match = _BARE_CHANNEL_RE.search(text)
    return match.group(0) if match else ""


def extract_link_text(text: str) -> str:
    """Visible text of the first ``<a …>…</a>`` or ``<url=…>…</url>`` link, without markup."""
    match = _LINK_TEXT_RE.search(text or "")
    if not match:
        return ""
    inner = match.group(1) if match.group(1) is not None else match.group(2)
    return " ".join(html.unescape(strip_tags(inner)).split())


def channel_link_html(preset) -> str:
    """In-game channel link for the MOTD, or the escaped channel name when no channel id is stored."""
    if preset is None:
        return ""
    name = (getattr(preset, "channel_value", "") or "").strip() or (getattr(preset, "name", "") or "").strip()
    channel_id = (getattr(preset, "ingame_channel_id", "") or "").strip()
    if channel_id and _CHANNEL_ID_RE.fullmatch(channel_id):
        return format_html('<a href="joinChannel:{}">{}</a>', channel_id, name)
    return escape(name)
