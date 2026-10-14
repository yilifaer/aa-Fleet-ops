from django import template
from django.utils.html import format_html

from fleetops.models import SrpClaim, SrpClaimEvent
from fleetops.providers.zkillboard import kill_url
from fleetops.srp import display
from fleetops.srp.types import (
    Calculation,
    CalcNote,
    ClaimWarning,
    PolicySnapshot,
    warnings_from_json,
)

register = template.Library()

# Colour is never the only signal: every badge shows its words as text too.
STATUS_BADGE_CLASSES = {
    SrpClaim.Status.PENDING: "text-bg-warning",
    SrpClaim.Status.APPROVED: "text-bg-primary",
    SrpClaim.Status.PAID: "text-bg-success",
    SrpClaim.Status.REJECTED: "text-bg-danger",
    SrpClaim.Status.WITHDRAWN: "text-bg-secondary",
}
LEVEL_BADGE_CLASSES = {
    display.LEVEL_DANGER: "text-bg-danger",
    display.LEVEL_WARNING: "text-bg-warning",
    display.LEVEL_INFO: "text-bg-info",
    display.LEVEL_OK: "text-bg-success",
}
STATUS_LABELS = dict(SrpClaim.Status.choices)

# What the history says of a person who is neither the viewer nor visible by name: by the kind of step.
CLAIMANT_ACTIONS = frozenset({SrpClaimEvent.Action.SUBMIT, SrpClaimEvent.Action.WITHDRAW, SrpClaimEvent.Action.RESUBMIT})
SYSTEM_ACTIONS = frozenset({SrpClaimEvent.Action.AUTO_REJECT, SrpClaimEvent.Action.VALUE_UPDATE})

ROW_OVER_LIMIT = "fleetops-srp-over-limit"
ROW_WARNING = "fleetops-srp-warning"


# --- ISK ---------------------------------------------------------------------------------------------


@register.filter
def isk(value):
    """``1,234,567,890 ISK``; empty for a missing value."""
    return display.format_isk(value)


@register.filter
def isk_short(value):
    """``1.23b ISK``; empty for a missing value."""
    return display.format_isk_short(value)


@register.filter
def isk_plain(value):
    """Digits only, for the in-game "Give ISK" field and copy buttons; empty for a missing value."""
    return display.format_isk_plain(value)


# --- Status, warnings and checks ------------------------------------------------------------------------


@register.filter
def srp_status_label(status):
    """The words for a claim status; a claim works too."""
    status = getattr(status, "status", status)
    return STATUS_LABELS.get(status, str(status or ""))


@register.filter
def srp_status_badge(status):
    """A badge with the words of a claim status (a status key or a claim)."""
    status = getattr(status, "status", status)
    return format_html(
        '<span class="badge {}">{}</span>',
        STATUS_BADGE_CLASSES.get(status, "text-bg-secondary"),
        srp_status_label(status),
    )


def _warning(value) -> ClaimWarning | None:
    if isinstance(value, ClaimWarning):
        return value
    return ClaimWarning.from_json(value)


@register.filter
def srp_warnings(value):
    """The warnings of a claim (or its stored list) as objects with ``code``, ``level``, ``params`` and ``text``."""
    value = getattr(value, "warnings", value)
    return warnings_from_json(value if isinstance(value, (list, tuple)) else [])


@register.filter
def srp_flags(value):
    """The warnings of a claim, a stored list or a list of ``ClaimWarning`` without the fleet check itself.

    The check is stored as a warning too (bulk approval skips claims that have one), but the pages show it as
    the check badge, so listing it again would say the same sentence twice. A ship that differs stays.
    """
    value = getattr(value, "warnings", value)
    if not isinstance(value, (list, tuple)):
        return []
    found = [item if isinstance(item, ClaimWarning) else ClaimWarning.from_json(item) for item in value]
    return [warning for warning in found if warning is not None and warning.code not in display.PRESENCE_TEXTS]


@register.filter
def srp_warning_text(warning):
    """The sentence of a warning (stored dict or ``ClaimWarning``)."""
    warning = _warning(warning)
    return warning.text if warning else ""


@register.filter
def srp_warning_badge(warning):
    """A badge with the sentence of a warning, coloured by its level."""
    warning = _warning(warning)
    if warning is None:
        return ""
    return format_html(
        '<span class="badge fleetops-srp-warning {}">{}</span>',
        LEVEL_BADGE_CLASSES.get(warning.level, "text-bg-warning"),
        warning.text,
    )


@register.filter
def srp_actor(event, viewer):
    """Who took a step of the history, for a viewer who may not see the names of the reviewers.

    ``You`` for the viewer's own steps, ``Claimant`` for what the claimant does (submitting, withdrawing,
    submitting again), ``System`` for automatic steps and ``Reviewer`` for the decisions of other people.
    """
    if event.actor_id is None or event.action in SYSTEM_ACTIONS:
        return "System"
    if event.actor_id == getattr(viewer, "pk", None):
        return "You"
    return "Claimant" if event.action in CLAIMANT_ACTIONS else "Reviewer"


@register.filter
def srp_check_text(claim):
    """The sentence of the fleet check stored on a claim; empty when it has none."""
    code = getattr(claim, "fleet_check", "")
    return display.presence_text(code, getattr(claim, "fleet_check_params", None)) if code else ""


@register.filter
def srp_check_badge(claim):
    """A badge with the fleet check of a claim, coloured by how serious the check is."""
    code = getattr(claim, "fleet_check", "")
    if not code:
        return ""
    return format_html(
        '<span class="badge {}">{}</span>',
        LEVEL_BADGE_CLASSES.get(display.presence_level(code), "text-bg-warning"),
        display.presence_text(code, getattr(claim, "fleet_check_params", None)),
    )


@register.filter
def srp_row_class(claim):
    """Marks a claim row: red when it is over a limit, amber for any other warning or a doubtful fleet check."""
    if getattr(claim, "over_limit", False):
        return ROW_OVER_LIMIT
    for warning in srp_warnings(claim):
        if warning.level in (display.LEVEL_WARNING, display.LEVEL_DANGER):
            return ROW_WARNING
    code = getattr(claim, "fleet_check", "")
    if code and display.presence_level(code) != display.LEVEL_OK:
        return ROW_WARNING
    return ""


# --- Stored calculation and policy ------------------------------------------------------------------------


@register.filter
def srp_calculation(value):
    """The calculation of a claim (or its stored JSON) as a ``Calculation``."""
    return Calculation.from_json(getattr(value, "calculation", value))


@register.filter
def srp_policy(value):
    """The policy snapshot of a claim (or its stored JSON) as a ``PolicySnapshot``."""
    return PolicySnapshot.from_json(getattr(value, "policy", value))


@register.filter
def srp_note_text(note):
    """The sentence of a calculation note (``CalcNote`` or stored dict)."""
    if not isinstance(note, CalcNote):
        note = CalcNote.from_json(note)
    return note.text if note else ""


@register.filter
def srp_limit_label(kind):
    """The name of a limit: ``loss_cap``, ``personal`` or ``corporation``."""
    return display.limit_kind_label(kind)


@register.filter
def srp_match_label(match):
    """How specific a payout rule matched: ``doctrine_ship``, ``doctrine`` or ``ship``."""
    return display.match_label(match)


# --- Links and images ------------------------------------------------------------------------------------


@register.filter
def zkill_url(killmail_id):
    """The zKillboard page of a killmail (a killmail ID or a claim)."""
    killmail_id = getattr(killmail_id, "killmail_id", killmail_id)
    try:
        return kill_url(int(killmail_id))
    except (TypeError, ValueError):
        return ""


@register.simple_tag
def ship_render_url(type_id, size=64):
    """The CCP image server render of a ship type."""
    try:
        return f"https://images.evetech.net/types/{int(type_id)}/render?size={int(size)}"
    except (TypeError, ValueError):
        return ""
