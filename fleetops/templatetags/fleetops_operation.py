from django import template

from fleetops.models import FleetOperation
from fleetops.services.mentions import mention_text, selection_from_operation

register = template.Library()


@register.simple_tag(takes_context=True)
def composition_visible(context, operation):
    """Whether this viewer may see who is in the fleet.

    While a fleet is open only the people who manage it and fleet managers / viewers of all fleets
    see its members; everybody who can open the page sees them once the fleet is closed.
    """
    if operation.status not in FleetOperation.OPEN_STATUSES:
        return True
    if context.get("can_manage"):
        return True
    request = context.get("request")
    user = getattr(request, "user", None)
    if user is None:
        return False
    return user.has_perm("fleetops.view_all_fleets") or user.has_perm("fleetops.manage_fleets")


@register.filter
def owned_by(rows, user):
    """Rows (attendance, special roles) that belong to ``user``."""
    user_id = getattr(user, "pk", None)
    return [row for row in rows if user_id is not None and row.auth_user_id == user_id]


@register.simple_tag(takes_context=True)
def visible_rows(context, rows, show_all):
    """All rows, or only the viewer's own ones while the fleet's members are hidden from them."""
    if show_all:
        return rows
    return owned_by(rows, getattr(context.get("request"), "user", None))


@register.filter
def ping_mention_label(operation):
    """Who the fleet's ping notified, as people read it."""
    return mention_text(selection_from_operation(operation), display=True) or "No ping"


@register.filter
def evewho_url(character_id):
    return f"https://evewho.com/character/{int(character_id)}" if character_id else ""


@register.filter
def zkillboard_url(character_id):
    return f"https://zkillboard.com/character/{int(character_id)}/" if character_id else ""
