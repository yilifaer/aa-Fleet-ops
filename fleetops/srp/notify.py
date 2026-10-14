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
}


def _message(claim: SrpClaim, action: str) -> str:
    lines = [
        gettext("Claim: %(reference)s") % {"reference": claim.reference},
        gettext("Ship: %(ship)s") % {"ship": claim.ship_type_name or claim.ship_type_id},
    ]
    loss_time = format_eve(claim.killmail_time)
    if loss_time:
        lines.append(gettext("Loss time: %(time)s EVE") % {"time": loss_time})
    if action in ("approve", "amount_change", "paid") and claim.approved_amount is not None:
        lines.append(gettext("Amount: %(amount)s") % {"amount": format_isk(claim.approved_amount)})
    if action != "paid" and claim.decision_reason:
        # A reason written by the system is stored in English and read in the language of the claimant.
        lines.append(gettext("Reason: %(reason)s") % {"reason": translate_stored(claim.decision_reason)})
    return "\n".join(lines)


def notify_claimant(claim: SrpClaim, action: str) -> None:
    """Tell the claimant that the claim was approved, paid, rejected (also automatically), had its approval
    revoked or was reopened. Plain text; does nothing when ``notify_members`` is off, and a failure is
    logged and ignored."""
    spec = NOTIFICATIONS.get(action)
    if spec is None or claim.user_id is None:
        return
    try:
        if not SrpSettings.get_solo().notify_members:
            return
        title, level = spec
        ship = claim.ship_type_name or str(claim.ship_type_id)
        # The claimant reads the notification, so it follows their language, not the reviewer's.
        with translation.override(user_language(claim.user)):
            text = f"{title}: {ship}"[:MAX_TITLE_LENGTH]
            message = _message(claim, action)
        getattr(notify, level)(claim.user, text, message)
    except Exception as exc:
        # The decision stands; the claimant sees it on the claim page.
        logger.warning("FleetOps: SRP notification for claim %s failed (%s).", claim.pk, type(exc).__name__)
