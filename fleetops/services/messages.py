# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

import html
import re
import secrets
from dataclasses import dataclass, field
from types import SimpleNamespace
from urllib.parse import urlsplit

from django.template import Context, Template
from django.utils import timezone
from django.utils.html import strip_tags
from django.utils.translation import gettext, gettext_lazy

from fleetops import texts
from fleetops.constants import (
    DEFAULT_MOTD_TEMPLATE,
    DEFAULT_PING_TEMPLATE,
    DEFAULT_STANDALONE_PING_TEMPLATE,
)
from fleetops.models import FleetOpsSettings, MessageTemplate
from fleetops.providers.doctrines import doctrine_url
from fleetops.services import eve_links, links
from fleetops.services.mentions import (
    DISCORD_MESSAGE_LIMIT,
    discord_length,
    mention_text,
    neutralize_mass_mentions,
    selection_from_operation,
    target_label,
)
from fleetops.services.periods import discord_timestamp, format_eve, unix_timestamp

MISSING_MENTION_WARNING = gettext_lazy(
    "The ping template does not contain {{ ping_mention }}; nobody will be notified."
)


def _default_template(template_type: str):
    return (
        MessageTemplate.objects.filter(template_type=template_type, is_active=True, is_default=True).first()
        or MessageTemplate.objects.filter(template_type=template_type, is_active=True).first()
    )


def _sender(operation, actor) -> str:
    # The main of the user sending this message, else the FC of record.
    main = getattr(getattr(actor, "profile", None), "main_character", None)
    return getattr(main, "character_name", "") or operation.fc_main_character_name or operation.fc_character_name


def _fleet_times(scheduled_at) -> dict:
    if scheduled_at is None:
        # FleetOps's own words in a ping or MOTD are plain English (owner decision 5-10).
        return {
            "fleet_time_eve": "",
            "fleet_time_unix": "",
            "fleet_time_discord": "",
            "fleet_time_local": "",
            "fleet_time": "Now",
        }
    eve = f"{format_eve(scheduled_at)} EVE"
    return {
        "fleet_time_eve": eve,
        "fleet_time_unix": str(unix_timestamp(scheduled_at)),
        "fleet_time_discord": discord_timestamp(scheduled_at, "R"),
        # Discord shows this one in the time zone of whoever reads it.
        "fleet_time_local": discord_timestamp(scheduled_at, "t"),
        "fleet_time": eve,
    }


def _doctrine_lists(operation) -> tuple[str, str]:
    """Hull of every fit (duplicates kept) and the fit names, in doctrine order, joined with " > "."""
    fits = [fit for fit in getattr(operation, "doctrine_fits", None) or [] if isinstance(fit, dict)]
    ships = (str(fit.get("ship") or fit.get("ship_type_id") or "").strip() for fit in fits)
    names = (str(fit.get("fit") or "").strip() for fit in fits)
    return " > ".join(ship for ship in ships if ship), " > ".join(name for name in names if name)


def _ping_channel_name(operation) -> str:
    if operation.ping_channel_name:
        return operation.ping_channel_name
    channel = operation.ping_channel if operation.ping_channel_id else None
    return channel.name if channel is not None else ""


def _srp_url(operation) -> str:
    """The fleet's SRP link as a full address, whatever SRP app made it; "" while the fleet has none.

    A link of the site (a path) gets the site address in front; an address with a scheme is used as stored,
    other values (also a path of another host) are not links to use.
    """
    url = (getattr(operation, "srp_url", "") or "").strip()
    if url.startswith("/") and not url.startswith("//"):
        return links.absolute_url(url)
    if urlsplit(url).scheme in ("http", "https"):
        return url
    return ""


def _link_values(fc_name: str, fc_character_id, formup: str, *, lookups: bool = True) -> dict:
    """The MOTD variables that make the FC and the form-up place clickable in the game.

    The FC links to their character, the form-up text to its solar system when it is the name of one; any
    failed lookup leaves the plain (escaped) text, so a message never fails because of a link. ``lookups=False``
    gives the plain text without any lookup (a ping without a fleet has no MOTD).
    """
    if not lookups:
        return {
            "fc_character_id": str(fc_character_id or ""),
            "fc_link": eve_links.escaped(fc_name),
            "formup_system_id": "",
            "formup_link": eve_links.escaped(formup),
        }
    system = eve_links.solar_system(formup)
    return {
        "fc_character_id": str(fc_character_id or ""),
        "fc_link": eve_links.character_link(fc_character_id, fc_name),
        "formup_system_id": str(system[0]) if system else "",
        "formup_link": eve_links.system_link(formup, system),
    }


def _context(operation, selection, mention: str, *, actor, pre_ping, now, lookups: bool = True) -> dict:
    """The variables of a fleet's messages. Callers run inside ``texts.outgoing_language()``.

    ``lookups`` looks up the clickable FC and form-up place of the MOTD; a ping without a fleet leaves it off.
    """
    comms = operation.comms
    logi = operation.logi_channel
    boost = operation.boost_channel
    doctrine_ships, doctrine_fits = _doctrine_lists(operation)
    label = target_label(selection)
    return {
        "operation_id": str(operation.uuid),
        "fc": operation.fc_character_name,
        "fc_main": operation.fc_main_character_name or operation.fc_character_name,
        "sender": _sender(operation, actor),
        "fleet_type": str(operation.fleet_type),
        "doctrine": operation.doctrine_name or "None / Custom",
        "doctrine_name": operation.doctrine_name or "",
        "doctrine_url": doctrine_url(operation.doctrine_source, operation.doctrine_external_id),
        "doctrine_ships": doctrine_ships,
        "doctrine_fits": doctrine_fits,
        "formup": operation.formup,
        **_link_values(
            operation.fc_character_name,
            getattr(operation, "fc_character_id", ""),
            operation.formup,
            lookups=lookups,
        ),
        "group": getattr(operation, "group_name", "") or "",
        "comms": comms.name if comms else "",
        "comms_channel": comms.channel_name if comms else "",
        "comms_link": links.comms_link(comms),
        "comms_voice_url": links.public_voice_url(comms),
        "logi_channel": logi.channel_value if logi else "",
        "logi_channel_link": links.channel_link_html(logi),
        "boost_channel": boost.channel_value if boost else "",
        "boost_channel_link": links.channel_link_html(boost),
        "srp_url": _srp_url(operation),
        **_fleet_times(operation.scheduled_at),
        "is_pre_ping": bool(pre_ping),
        "ping_mention": mention,
        "ping_target": mention,
        "ping_target_label": label,
        "ping_target_name": label,
        "ping_channel": _ping_channel_name(operation),
        "sent_at": format_eve(now or timezone.now()),
        "additional_message": operation.additional_message,
    }


def build_context(operation, *, actor=None, pre_ping=False, now=None, display=False) -> dict:
    """Template variables of a fleet; every value is a plain string except ``is_pre_ping``.

    ``actor`` is the user sending the message (its main is the ``sender``), ``pre_ping`` marks a ping
    for a scheduled fleet and ``now`` is the send time. ``display`` shows role mentions as ``@Name``.
    Times are EVE time (UTC), whatever time zone the site or request uses. FleetOps's own words in the values
    (the target of the ping, ``Now``, ``None / Custom``) are English, whatever language is active.
    ``fc_link`` and ``formup_link`` are meant for the MOTD; in a Discord ping they are no use.
    """
    with texts.outgoing_language():
        selection = selection_from_operation(operation)
        mention = mention_text(selection, display=display)
        return _context(operation, selection, mention, actor=actor, pre_ping=pre_ping, now=now)


def _ping_as_operation(ping, actor):
    """A ping without a fleet seen as a fleet, so both are described by the same variables."""
    main = getattr(getattr(actor, "profile", None), "main_character", None)
    name = getattr(main, "character_name", "") or ping.sender_name
    return SimpleNamespace(
        uuid="",
        fc_character_id=getattr(main, "character_id", "") or "",
        fc_character_name=name,
        fc_main_character_name=name,
        fleet_type="",
        doctrine_name=ping.doctrine_name,
        doctrine_source=ping.doctrine_source,
        doctrine_external_id=ping.doctrine_external_id,
        doctrine_fits=[],
        formup=ping.formup,
        comms=ping.comms,
        logi_channel=None,
        boost_channel=None,
        srp_url="",
        scheduled_at=ping.fleet_time,
        ping_channel_name=ping.channel_name,
        ping_channel_id=ping.channel_id,
        group_name=ping.group_name,
        additional_message=ping.additional_message,
    )


def _ping_selection(ping):
    return selection_from_operation(SimpleNamespace(ping_mention_mode=ping.mention_mode, ping_roles=ping.mention_roles))


def standalone_context(ping, *, actor, now=None, display=False) -> dict:
    """Template variables of a ping without a fleet: the keys of :func:`build_context`.

    The sender is the main of ``actor``; the fleet-only variables (fleet type, SRP link, channels) are empty.
    """
    with texts.outgoing_language():
        selection = _ping_selection(ping)
        mention = mention_text(selection, display=display)
        return _context(
            _ping_as_operation(ping, actor),
            selection,
            mention,
            actor=actor,
            pre_ping=bool(ping.fleet_time),
            now=now,
            lookups=False,
        )


@dataclass(slots=True)
class RenderedMessages:
    ping: str
    motd: str
    # Template fallback notes.
    errors: list[str] = field(default_factory=list)
    # The ping as people read it (role mentions shown as @Name).
    ping_display: str = ""
    # Problems that must stop a send, and problems that are only shown.
    blocking: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    # Discord length (UTF-16 code units) of the ping; plain length of the MOTD.
    ping_length: int = 0
    motd_length: int = 0
    # The MOTD as the game shows it (tags dropped, a line break counted once) and the length from which the
    # preview warns (0 = never).
    motd_visible_length: int = 0
    motd_limit: int = 0


def _render(record, fallback: str, values: dict, label: str, errors: list[str]) -> str:
    if record is not None:
        try:
            return Template(record.content).render(Context(values, autoescape=False)).strip()
        except Exception as exc:
            # Admin-edited templates must never block a fleet start or the preview.
            errors.append(texts.TEMPLATE_FALLBACK(label=label, name=record.name, error_type=type(exc).__name__))
    return Template(fallback).render(Context(values, autoescape=False)).strip()


def _mention_placeholder() -> str:
    # Digits between control characters: unknown to the template author, untouched by filters such as
    # upper or escape, and free of anything Discord reads as a mention.
    return f"\x02{secrets.randbelow(10**12):012d}\x03"


def ping_too_long_message(length: int) -> str:
    """The warning for a ping over Discord's limit, in the language of whoever looks at the preview."""
    return gettext(
        "The ping is %(length)s characters; Discord accepts at most %(limit)s. Shorten the additional information."
    ) % {"length": length, "limit": DISCORD_MESSAGE_LIMIT}


def _relative_links(values: dict, *texts: str) -> bool:
    # Without SITE_URL the comms join and doctrine links are site paths, which open nowhere outside the site.
    if links.site_url_configured():
        return False
    for link in (values["comms_link"], values["doctrine_url"]):
        if link.startswith("/") and any(link in text for text in texts):
            return True
    return False


def _relative_srp_link(values: dict, *texts: str) -> bool:
    # Without SITE_URL a FleetOps SRP link is a site path, which opens nowhere outside the site.
    srp_url = values["srp_url"]
    return srp_url.startswith("/") and any(srp_url in text for text in texts)


@dataclass(slots=True)
class _Ping:
    """A rendered ping: as posted, as people read it, and whether its mention is missing from the template."""

    text: str
    display: str
    lost_mention: bool


def _mention_for(selection) -> str:
    return _mention_placeholder() if mention_text(selection) else ""


def _render_ping(record, fallback: str, values: dict, selection, errors: list[str]) -> _Ping:
    """Render a ping so that only the mention FleetOps adds can notify anybody.

    ``values["ping_mention"]`` holds a placeholder instead of the mention: templates see no mention they could
    change, every ``@everyone``/``@here`` in the rendered text is neutralised, and then the placeholder is replaced
    with the selected mention.
    """
    placeholder = values["ping_mention"]
    ping = _render(record, fallback, values, "Ping", errors)
    lost_mention = selection.notifies and placeholder not in ping
    ping = neutralize_mass_mentions(ping)
    display = ping
    if placeholder:
        display = ping.replace(placeholder, mention_text(selection, display=True))
        ping = ping.replace(placeholder, mention_text(selection))
    return _Ping(ping, display, lost_mention)


def render_messages(operation, *, actor=None, pre_ping=False, now=None) -> RenderedMessages:
    """Render ping and MOTD, falling back to the built-in templates if a stored one fails.

    ``actor`` is the user sending this render, ``pre_ping`` marks a ping for a scheduled fleet and
    ``now`` is the send time (default: the current time).

    Only the mention FleetOps adds can notify anybody: templates see a placeholder instead of it,
    every ``@everyone``/``@here`` in the rendered ping is neutralised, and then the placeholder is
    replaced with the selected mention.
    """
    selection = selection_from_operation(operation)
    errors = []
    # The messages are read by the whole fleet and are always English: they follow neither the language of the FC
    # who clicks nor the site language (and the preview shows exactly what is sent). Warnings are for the person
    # looking at the preview and follow their language.
    with texts.outgoing_language():
        placeholder = _mention_for(selection)
        values = _context(operation, selection, placeholder, actor=actor, pre_ping=pre_ping, now=now)
        ping_record = operation.ping_template or _default_template(MessageTemplate.TemplateType.PING)
        motd_record = operation.motd_template or _default_template(MessageTemplate.TemplateType.MOTD)
        rendered_ping = _render_ping(ping_record, DEFAULT_PING_TEMPLATE, values, selection, errors)
        ping, ping_display = rendered_ping.text, rendered_ping.display
        motd = _render(motd_record, DEFAULT_MOTD_TEMPLATE, values, "MOTD", errors)
        if placeholder:
            motd = motd.replace(placeholder, mention_text(selection, display=True))
        # EVE MOTD is HTML-like. Strip dangerous script-ish constructs while preserving basic tags.
        motd = re.sub(r"<(/?script)", r"&lt;\1", motd, flags=re.IGNORECASE)

    warnings = []
    if rendered_ping.lost_mention:
        warnings.append(str(MISSING_MENTION_WARNING))
    blocking = []
    ping_length = discord_length(ping)
    if ping_length > DISCORD_MESSAGE_LIMIT:
        # Only a ping that FleetOps posts must fit; a copy-only ping can still be shortened by hand.
        (blocking if operation.ping_channel_id else warnings).append(ping_too_long_message(ping_length))
    motd_limit = FleetOpsSettings.get_solo().motd_warning_length
    motd_visible = _visible_length(motd)
    # The game's limit is not documented: the setting decides when to warn (0 never), counted with the tags.
    if 0 < motd_limit < len(motd):
        warnings.append(
            gettext(
                "The MOTD is %(length)s characters long with its tags (%(visible)s visible); "
                "the game may cut a MOTD longer than %(limit)s."
            )
            % {"length": len(motd), "visible": motd_visible, "limit": motd_limit}
        )
    if _relative_links(values, ping, motd):
        warnings.append(
            gettext("SITE_URL is not set, so the comms and doctrine links in the messages are incomplete.")
        )
    if _relative_srp_link(values, ping, motd):
        warnings.append(gettext("SITE_URL is not set, so the SRP link in the messages is incomplete."))
    return RenderedMessages(
        ping,
        motd,
        errors,
        ping_display=ping_display,
        blocking=blocking,
        warnings=warnings,
        ping_length=ping_length,
        motd_length=len(motd),
        motd_visible_length=motd_visible,
        motd_limit=motd_limit,
    )


def _visible_length(motd: str) -> int:
    """What the game shows of a MOTD: the tags are dropped, a line break counts once and entities as one character."""
    return len(html.unescape(strip_tags(re.sub(r"<br\s*/?>", "\n", motd))))


def render_standalone_ping(ping, *, actor, now=None) -> RenderedMessages:
    """Render a ping without a fleet (``StandalonePing``, saved or not): always English, like every ping.

    The ping is posted by FleetOps, so one over Discord's limit blocks. There is no MOTD.
    """
    selection = _ping_selection(ping)
    errors = []
    with texts.outgoing_language():
        values = _context(
            _ping_as_operation(ping, actor),
            selection,
            _mention_for(selection),
            actor=actor,
            pre_ping=bool(ping.fleet_time),
            now=now,
            lookups=False,
        )
        record = ping.template or _default_template(MessageTemplate.TemplateType.STANDALONE)
        rendered = _render_ping(record, DEFAULT_STANDALONE_PING_TEMPLATE, values, selection, errors)

    warnings = []
    if rendered.lost_mention:
        warnings.append(str(MISSING_MENTION_WARNING))
    blocking = []
    ping_length = discord_length(rendered.text)
    if ping_length > DISCORD_MESSAGE_LIMIT:
        blocking.append(ping_too_long_message(ping_length))
    if _relative_links(values, rendered.text):
        warnings.append(
            gettext("SITE_URL is not set, so the comms and doctrine links in the messages are incomplete.")
        )
    return RenderedMessages(
        rendered.text,
        "",
        errors,
        ping_display=rendered.display,
        blocking=blocking,
        warnings=warnings,
        ping_length=ping_length,
    )


def render_operation_messages(operation):
    rendered = render_messages(operation)
    return rendered.ping, rendered.motd


def render_cancellation_notice(operation, *, actor=None, now=None) -> str:
    """The notice posted when a scheduled fleet is cancelled; it never mentions anybody. Always English."""
    with texts.outgoing_language():
        values = build_context(operation, actor=actor, now=now, display=True)
        text = (
            f"**Cancelled:** {values['fleet_type']} at {values['fleet_time_eve']} ({values['formup']})\n"
            f"-# FleetOps notice from {values['sender']} at {values['sent_at']} EVE"
        )
    return neutralize_mass_mentions(text.strip())


def plain_motd(motd: str) -> str:
    return strip_tags(motd)
