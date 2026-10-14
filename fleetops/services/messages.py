import re
import secrets
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from django.template import Context, Template
from django.utils import timezone
from django.utils.html import strip_tags

from fleetops.constants import (
    DEFAULT_MOTD_TEMPLATE,
    DEFAULT_PING_TEMPLATE,
    MOTD_SOFT_LIMIT,
    SCHEDULE_CANCELLED_TEMPLATE,
)
from fleetops.models import MessageTemplate
from fleetops.providers.doctrines import doctrine_url
from fleetops.services import links
from fleetops.services.mentions import (
    DISCORD_MESSAGE_LIMIT,
    discord_length,
    mention_text,
    neutralize_mass_mentions,
    selection_from_operation,
    target_label,
)
from fleetops.services.periods import discord_timestamp, format_eve, unix_timestamp

MISSING_MENTION_WARNING = "The ping template does not contain {{ ping_mention }}; nobody will be notified."


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
        return {"fleet_time_eve": "", "fleet_time_unix": "", "fleet_time_discord": "", "fleet_time": "Now"}
    eve = f"{format_eve(scheduled_at)} EVE"
    return {
        "fleet_time_eve": eve,
        "fleet_time_unix": str(unix_timestamp(scheduled_at)),
        "fleet_time_discord": discord_timestamp(scheduled_at, "R"),
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


def _context(operation, selection, mention: str, *, actor, pre_ping, now) -> dict:
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
    Times are EVE time (UTC), whatever time zone the site or request uses.
    """
    selection = selection_from_operation(operation)
    mention = mention_text(selection, display=display)
    return _context(operation, selection, mention, actor=actor, pre_ping=pre_ping, now=now)


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


def _render(record, fallback: str, values: dict, label: str, errors: list[str]) -> str:
    if record is not None:
        try:
            return Template(record.content).render(Context(values, autoescape=False)).strip()
        except Exception as exc:
            # Admin-edited templates must never block a fleet start or the preview.
            errors.append(
                f'{label} template "{record.name}" could not be rendered ({type(exc).__name__}); '
                "the built-in default was used."
            )
    return Template(fallback).render(Context(values, autoescape=False)).strip()


def _mention_placeholder() -> str:
    # Digits between control characters: unknown to the template author, untouched by filters such as
    # upper or escape, and free of anything Discord reads as a mention.
    return f"\x02{secrets.randbelow(10**12):012d}\x03"


def ping_too_long_message(length: int) -> str:
    return (
        f"The ping is {length} characters; Discord accepts at most {DISCORD_MESSAGE_LIMIT}. "
        "Shorten the additional information."
    )


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


def render_messages(operation, *, actor=None, pre_ping=False, now=None) -> RenderedMessages:
    """Render ping and MOTD, falling back to the built-in templates if a stored one fails.

    ``actor`` is the user sending this render, ``pre_ping`` marks a ping for a scheduled fleet and
    ``now`` is the send time (default: the current time).

    Only the mention FleetOps adds can notify anybody: templates see a placeholder instead of it,
    every ``@everyone``/``@here`` in the rendered ping is neutralised, and then the placeholder is
    replaced with the selected mention.
    """
    selection = selection_from_operation(operation)
    placeholder = _mention_placeholder() if mention_text(selection) else ""
    values = _context(operation, selection, placeholder, actor=actor, pre_ping=pre_ping, now=now)
    errors = []
    ping_record = operation.ping_template or _default_template(MessageTemplate.TemplateType.PING)
    motd_record = operation.motd_template or _default_template(MessageTemplate.TemplateType.MOTD)
    ping = _render(ping_record, DEFAULT_PING_TEMPLATE, values, "Ping", errors)
    motd = _render(motd_record, DEFAULT_MOTD_TEMPLATE, values, "MOTD", errors)

    warnings = []
    if selection.notifies and placeholder not in ping:
        warnings.append(MISSING_MENTION_WARNING)
    ping = neutralize_mass_mentions(ping)
    ping_display = ping
    if placeholder:
        ping_display = ping.replace(placeholder, mention_text(selection, display=True))
        ping = ping.replace(placeholder, mention_text(selection))
        motd = motd.replace(placeholder, mention_text(selection, display=True))
    # EVE MOTD is HTML-like. Strip dangerous script-ish constructs while preserving basic tags.
    motd = re.sub(r"<(/?script)", r"&lt;\1", motd, flags=re.IGNORECASE)

    blocking = []
    ping_length = discord_length(ping)
    if ping_length > DISCORD_MESSAGE_LIMIT:
        # Only a ping that FleetOps posts must fit; a copy-only ping can still be shortened by hand.
        (blocking if operation.ping_channel_id else warnings).append(ping_too_long_message(ping_length))
    if len(motd) > MOTD_SOFT_LIMIT:
        warnings.append(f"The MOTD is {len(motd)} characters; the game may cut a MOTD longer than {MOTD_SOFT_LIMIT}.")
    if _relative_links(values, ping, motd):
        warnings.append("SITE_URL is not set, so the comms and doctrine links in the messages are incomplete.")
    if _relative_srp_link(values, ping, motd):
        warnings.append("SITE_URL is not set, so the SRP link in the messages is incomplete.")
    return RenderedMessages(
        ping,
        motd,
        errors,
        ping_display=ping_display,
        blocking=blocking,
        warnings=warnings,
        ping_length=ping_length,
        motd_length=len(motd),
    )


def render_operation_messages(operation):
    rendered = render_messages(operation)
    return rendered.ping, rendered.motd


def render_cancellation_notice(operation, *, actor=None, now=None) -> str:
    """The notice posted when a scheduled fleet is cancelled; it never mentions anybody."""
    values = build_context(operation, actor=actor, now=now, display=True)
    text = Template(SCHEDULE_CANCELLED_TEMPLATE).render(Context(values, autoescape=False)).strip()
    return neutralize_mass_mentions(text)


def plain_motd(motd: str) -> str:
    return strip_tags(motd)
