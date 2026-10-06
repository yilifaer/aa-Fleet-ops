from fleetops.models import AuditLog


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
