"""Alliance Auth notifications for the claimant."""

from allianceauth.notifications import notify
from allianceauth.services.hooks import get_extension_logger

from fleetops.models import SrpClaim, SrpSettings
from fleetops.services.periods import format_eve
from fleetops.srp.display import format_isk

logger = get_extension_logger(__name__)

MAX_TITLE_LENGTH = 254

# Action of the claim event: (title, notification level).
NOTIFICATIONS = {
    "approve": ("SRP approved", "success"),
    "amount_change": ("SRP amount changed", "info"),
    "paid": ("SRP paid", "success"),
    "reject": ("SRP rejected", "danger"),
    "auto_reject": ("SRP rejected", "danger"),
    "revoke": ("SRP approval revoked", "warning"),
    "reopen": ("SRP claim reopened", "info"),
}


def _message(claim: SrpClaim, action: str) -> str:
    lines = [f"Claim: {claim.reference}", f"Ship: {claim.ship_type_name or claim.ship_type_id}"]
    loss_time = format_eve(claim.killmail_time)
    if loss_time:
        lines.append(f"Loss time: {loss_time} EVE")
    if action in ("approve", "amount_change", "paid") and claim.approved_amount is not None:
        lines.append(f"Amount: {format_isk(claim.approved_amount)}")
    if action != "paid" and claim.decision_reason:
        lines.append(f"Reason: {claim.decision_reason}")
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
        text = f"{title}: {ship}"[:MAX_TITLE_LENGTH]
        getattr(notify, level)(claim.user, text, _message(claim, action))
    except Exception as exc:
        # The decision stands; the claimant sees it on the claim page.
        logger.warning("FleetOps: SRP notification for claim %s failed (%s).", claim.pk, type(exc).__name__)
