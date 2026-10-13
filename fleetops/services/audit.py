from django.forms.models import model_to_dict

from fleetops.models import AuditLog, DiscordWebhook, ShipFunction

REDACTED = "***redacted***"


def audit(actor, action, obj, old_value=None, new_value=None, reason=""):
    return AuditLog.objects.create(
        actor=actor,
        action=action,
        object_type=obj.__class__.__name__,
        object_id=str(getattr(obj, "pk", "") or getattr(obj, "uuid", "")),
        old_value=old_value,
        new_value=new_value,
        reason=reason,
    )


def config_snapshot(obj):
    """JSON-safe snapshot of a configuration object for ``configuration.*`` audit entries.

    Discord webhook URLs carry the webhook token and are never written into it.
    """
    data = model_to_dict(obj)
    if isinstance(obj, DiscordWebhook) and "webhook_url" in data:
        data["webhook_url"] = REDACTED if data["webhook_url"] else ""
    many_to_many = {field.name for field in obj._meta.many_to_many}
    for key, value in list(data.items()):
        if key in many_to_many:
            # Such as the roles a ping channel allows.
            data[key] = sorted(item.pk for item in value)
        elif hasattr(value, "pk"):
            data[key] = value.pk
        elif not isinstance(value, (str, int, float, bool, type(None), list, dict)):
            data[key] = str(value)
    return data


def cascaded_config_rows(obj) -> list:
    """Configuration rows that deleting ``obj`` deletes with it, such as a ship function's rules.

    They get their own ``configuration.delete`` entries, so the audit log keeps what was mapped.
    """
    if isinstance(obj, ShipFunction) and obj.pk is not None:
        return list(obj.rules.all())
    return []
