# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Alliance Auth notifications for the claimant."""

from allianceauth.notifications import notify
from allianceauth.services.hooks import get_extension_logger
from django.utils import translation
from django.utils.translation import gettext, gettext_lazy

from fleetops.models import SrpClaim, SrpSettings
from fleetops.services.periods import format_eve
from fleetops.srp.display import format_isk
from fleetops.texts import translate_stored, user_language

logger = get_extension_logger(__name__)

MAX_TITLE_LENGTH = 254

# Action of the claim event: (title, notification level).
NOTIFICATIONS = {
    "approve": (gettext_lazy("SRP approved"), "success"),
    "amount_change": (gettext_lazy("SRP amount changed"), "info"),
    "paid": (gettext_lazy("SRP paid"), "success"),
    "reject": (gettext_lazy("SRP rejected"), "danger"),
    "auto_reject": (gettext_lazy("SRP rejected"), "danger"),
    "revoke": (gettext_lazy("SRP approval revoked"), "warning"),
    "reopen": (gettext_lazy("SRP claim reopened"), "info"),
    "unpaid": (gettext_lazy("SRP payment undone"), "warning"),
}


def _message(claim: SrpClaim, action: str, reason: str = "") -> str:
    lines = [
        gettext("Claim: %(reference)s") % {"reference": claim.reference},
        gettext("Ship: %(ship)s") % {"ship": claim.ship_type_name or claim.ship_type_id},
    ]
    loss_time = format_eve(claim.killmail_time)
    if loss_time:
        lines.append(gettext("Loss time: %(time)s EVE") % {"time": loss_time})
    if action in ("approve", "amount_change", "paid") and claim.approved_amount is not None:
        lines.append(gettext("Amount: %(amount)s") % {"amount": format_isk(claim.approved_amount)})
    if action == "unpaid":
        # The payment was undone with a reason of its own; the claim stays approved.
        if reason:
            lines.append(gettext("Reason: %(reason)s") % {"reason": translate_stored(reason)})
        lines.append(gettext("The payment of this claim was undone. The claim is approved and not paid yet."))
    elif action != "paid" and claim.decision_reason:
        # A reason written by the system is stored in English and read in the language of the claimant.
        lines.append(gettext("Reason: %(reason)s") % {"reason": translate_stored(claim.decision_reason)})
    return "\n".join(lines)


def notify_claimant(claim: SrpClaim, action: str, reason: str = "") -> None:
    """Tell the claimant that the claim was approved, paid, rejected (also automatically), had its approval
    revoked or was reopened, or (when the setting asks for it) that its payment was undone, with the ``reason`` given.
    Plain text; does nothing when ``notify_members`` is off, and a failure is logged and ignored."""
    spec = NOTIFICATIONS.get(action)
    if spec is None or claim.user_id is None:
        return
    try:
        srp = SrpSettings.get_solo()
        if not srp.notify_members:
            return
        # Undoing a payment tells the member only when its own switch is on as well.
        if action == "unpaid" and not srp.notify_on_unpaid:
            return
        title, level = spec
        ship = claim.ship_type_name or str(claim.ship_type_id)
        # The claimant reads the notification, so it follows their language, not the reviewer's.
        with translation.override(user_language(claim.user)):
            text = f"{title}: {ship}"[:MAX_TITLE_LENGTH]
            message = _message(claim, action, reason)
        getattr(notify, level)(claim.user, text, message)
    except Exception as exc:
        # The decision stands; the claimant sees it on the claim page.
        logger.warning("FleetOps: SRP notification for claim %s failed (%s).", claim.pk, type(exc).__name__)


def notify_banned(ban) -> None:
    """Tell the member that an SRP ban started. Plain text; does nothing when ``notify_members`` is off, and a
    failure is logged and ignored. The internal note is never part of it."""
    try:
        if not SrpSettings.get_solo().notify_members:
            return
        with translation.override(user_language(ban.user)):
            title = gettext("SRP submissions blocked")
            lines = [gettext("Reason: %(reason)s") % {"reason": translate_stored(ban.reason)}]
            if ban.expires_at is None:
                lines.append(gettext("The ban has no end date."))
            else:
                lines.append(gettext("Until: %(time)s EVE") % {"time": format_eve(ban.expires_at)})
            message = "\n".join(lines)
        notify.danger(ban.user, title, message)
    except Exception as exc:
        # The ban stands; the member sees it on the SRP pages.
        logger.warning("FleetOps: SRP ban notification for ban %s failed (%s).", ban.pk, type(exc).__name__)
