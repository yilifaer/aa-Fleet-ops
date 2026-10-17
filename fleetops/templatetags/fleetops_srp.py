# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

from django import template
from django.utils.translation import pgettext

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
from fleetops.ui import SRP_LEVEL_TONES, SRP_STATUS_TONES, TONE_NEUTRAL, TONE_WARN, pill_html, tone_for

register = template.Library()

# Colour is never the only signal: every pill shows its words as text too. The tone of a status or of a warning
# level is one of the maps of ``fleetops.ui`` (a pending claim is amber, an approved one waits for its payment
# and is information, a paid one is green, a rejected one red).
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
    """A pill with the words of a claim status (a status key or a claim)."""
    status = getattr(status, "status", status)
    return pill_html(tone_for(SRP_STATUS_TONES, status, TONE_NEUTRAL), srp_status_label(status))


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
    """A pill with the sentence of a warning, toned by its level."""
    warning = _warning(warning)
    if warning is None:
        return ""
    # A warning is a sentence: the pill wraps instead of running out of its box.
    return pill_html(
        tone_for(SRP_LEVEL_TONES, warning.level, TONE_WARN), warning.text, class_="fo-pill-wrap fleetops-srp-warning"
    )


@register.filter
def srp_actor(event, viewer):
    """Who took a step of the history, for a viewer who may not see the names of the reviewers.

    ``You`` for the viewer's own steps, ``Claimant`` for what the claimant does (submitting, withdrawing,
    submitting again), ``System`` for automatic steps and ``Reviewer`` for the decisions of other people.
    """
    if event.actor_id is None or event.action in SYSTEM_ACTIONS:
        return pgettext("claim history actor", "System")
    if event.actor_id == getattr(viewer, "pk", None):
        return pgettext("claim history actor", "You")
    if event.action in CLAIMANT_ACTIONS:
        return pgettext("claim history actor", "Claimant")
    return pgettext("claim history actor", "Reviewer")


@register.filter
def srp_check_text(claim):
    """The sentence of the fleet check stored on a claim; empty when it has none."""
    code = getattr(claim, "fleet_check", "")
    return display.presence_text(code, getattr(claim, "fleet_check_params", None)) if code else ""


@register.filter
def srp_check_badge(claim):
    """A pill with the fleet check of a claim, toned by how serious the check is."""
    code = getattr(claim, "fleet_check", "")
    if not code:
        return ""
    return pill_html(
        tone_for(SRP_LEVEL_TONES, display.presence_level(code), TONE_WARN),
        display.presence_text(code, getattr(claim, "fleet_check_params", None)),
        class_="fo-pill-wrap",
    )


@register.filter
def srp_presence_badge(presence):
    """A pill with the fleet check of a loss that is being checked (a ``PresenceResult``), toned by its level."""
    text = getattr(presence, "text", "")
    if not text:
        return ""
    return pill_html(tone_for(SRP_LEVEL_TONES, getattr(presence, "level", ""), TONE_WARN), text, class_="fo-pill-wrap")


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


# --- The steps of a claim -----------------------------------------------------------------------------------


@register.simple_tag
def srp_steps(claim):
    """The progress of a claim as steps for the timeline: submitted, checked, decided and (while it can still be
    paid) paid.

    Each step is a dict with ``key``, ``label``, ``state`` and ``time``. A state is ``done``, ``current`` (the
    next thing that has to happen), ``bad`` (the claim was rejected) or ``todo``. A rejected or withdrawn claim
    has no payment step. The times are the ones the claim stores; a withdrawn claim keeps the time it was last
    saved, which is the withdrawal.
    """
    status = getattr(claim, "status", "")
    calculation = Calculation.from_json(getattr(claim, "calculation", None))
    checked = getattr(claim, "fleet_checked_at", None) or calculation.computed_at
    decided_label = {
        SrpClaim.Status.APPROVED: STATUS_LABELS[SrpClaim.Status.APPROVED],
        SrpClaim.Status.PAID: STATUS_LABELS[SrpClaim.Status.APPROVED],
        SrpClaim.Status.REJECTED: STATUS_LABELS[SrpClaim.Status.REJECTED],
        SrpClaim.Status.WITHDRAWN: STATUS_LABELS[SrpClaim.Status.WITHDRAWN],
    }.get(status, pgettext("claim step", "Decision"))
    decided_state = {
        SrpClaim.Status.PENDING: "current",
        SrpClaim.Status.REJECTED: "bad",
    }.get(status, "done")
    decided_time = (
        getattr(claim, "updated_at", None) if status == SrpClaim.Status.WITHDRAWN else getattr(claim, "decided_at", None)
    )
    steps = [
        {"key": "submitted", "label": pgettext("claim step", "Submitted"), "state": "done", "time": claim.submitted_at},
        {"key": "checked", "label": pgettext("claim step", "Checked"), "state": "done", "time": checked},
        {"key": "decided", "label": decided_label, "state": decided_state, "time": decided_time},
    ]
    if status not in (SrpClaim.Status.REJECTED, SrpClaim.Status.WITHDRAWN):
        paid_state = {SrpClaim.Status.APPROVED: "current", SrpClaim.Status.PAID: "done"}.get(status, "todo")
        steps.append(
            {
                "key": "paid",
                "label": STATUS_LABELS[SrpClaim.Status.PAID],
                "state": paid_state,
                "time": getattr(claim, "paid_at", None),
            }
        )
    return steps


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
