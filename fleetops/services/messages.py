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


def render_operation_messages(operation):
    context = Context(build_context(operation), autoescape=False)
    ping_record = operation.ping_template or _default_template(MessageTemplate.TemplateType.PING)
    motd_record = operation.motd_template or _default_template(MessageTemplate.TemplateType.MOTD)
    ping_source = ping_record.content if ping_record else DEFAULT_PING_TEMPLATE
    motd_source = motd_record.content if motd_record else DEFAULT_MOTD_TEMPLATE
    ping = Template(ping_source).render(context).strip()
    motd = Template(motd_source).render(context).strip()
    # EVE MOTD is HTML-like. Strip dangerous script-ish constructs while preserving basic tags.
    motd = motd.replace("<script", "&lt;script").replace("</script", "&lt;/script")
    return ping, motd


def plain_motd(motd: str) -> str:
    return strip_tags(motd)
