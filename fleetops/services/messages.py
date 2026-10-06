import re
from dataclasses import dataclass, field

from django.template import Context, Template
from django.utils.html import strip_tags

from fleetops.constants import DEFAULT_MOTD_TEMPLATE, DEFAULT_PING_TEMPLATE
from fleetops.models import MessageTemplate


def _default_template(template_type: str):
    return (
        MessageTemplate.objects.filter(template_type=template_type, is_active=True, is_default=True).first()
        or MessageTemplate.objects.filter(template_type=template_type, is_active=True).first()
    )


def build_context(operation) -> dict:
    comms = operation.comms
    logi = operation.logi_channel
    boost = operation.boost_channel
    doctrine = operation.doctrine_name or "None / Custom"
    return {
        "operation_id": str(operation.uuid),
        "fc": operation.fc_character_name,
        "fc_main": operation.fc_main_character_name or operation.fc_character_name,
        "fleet_type": str(operation.fleet_type),
        "doctrine": doctrine,
        "formup": operation.formup,
        "comms": comms.name if comms else "",
        "comms_channel": comms.channel_name if comms else "",
        "comms_link": comms.voice_url if comms else "",
        "logi_channel": logi.channel_value if logi else "",
        "boost_channel": boost.channel_value if boost else "",
        "fleet_time": operation.scheduled_at.isoformat(timespec="minutes") if operation.scheduled_at else "Now",
        "ping_target": operation.ping_target.target_value if operation.ping_target else "",
        "additional_message": operation.additional_message,
    }


@dataclass(slots=True)
class RenderedMessages:
    ping: str
    motd: str
    errors: list[str] = field(default_factory=list)


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


def render_messages(operation) -> RenderedMessages:
    """Render ping and MOTD, falling back to the built-in templates if a stored one fails."""
    values = build_context(operation)
    errors = []
    ping_record = operation.ping_template or _default_template(MessageTemplate.TemplateType.PING)
    motd_record = operation.motd_template or _default_template(MessageTemplate.TemplateType.MOTD)
    ping = _render(ping_record, DEFAULT_PING_TEMPLATE, values, "Ping", errors)
    motd = _render(motd_record, DEFAULT_MOTD_TEMPLATE, values, "MOTD", errors)
    # EVE MOTD is HTML-like. Strip dangerous script-ish constructs while preserving basic tags.
    motd = re.sub(r"<(/?script)", r"&lt;\1", motd, flags=re.IGNORECASE)
    return RenderedMessages(ping, motd, errors)


def render_operation_messages(operation):
    rendered = render_messages(operation)
    return rendered.ping, rendered.motd


def plain_motd(motd: str) -> str:
    return strip_tags(motd)
