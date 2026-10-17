# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Deciding on claims. Every action locks the claim, checks its status, writes a claim event and an audit
entry and notifies the claimant; every action that changes a claim's status or amount runs under
``quota.quota_lock()``, so decisions and submissions queue up and see the usage the previous one left.

A manager does not decide on their own claim unless the SRP settings allow it (``allow_self_review``). The
single actions refuse it; the bulk actions skip such claims and say so.
"""

from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from fleetops.constants import MAX_ISK
from fleetops import texts
from fleetops.models import SrpClaim, SrpClaimEvent, SrpFleetLink, SrpSettings
from fleetops.srp import access, claims, display, quota
from fleetops.srp import policy as rules
from fleetops.srp.types import LIMIT_PERSONAL, BulkResult, PolicySnapshot, SrpError, SrpLimitError, SrpStateError

Status = SrpClaim.Status
Scope = SrpClaim.Scope
Action = SrpClaimEvent.Action

MAX_PAYMENT_NOTE_LENGTH = 255
MAX_NOTE_LENGTH = 2000

# Stored messages (English): the pages show them through ``translate_stored``.
NOT_A_MANAGER = texts.SRP_NOT_A_MANAGER()
OWN_CLAIM = texts.SRP_OWN_CLAIM()
OWN_CLAIM_SKIPPED = texts.SRP_OWN_CLAIM_SKIPPED()


def can_review(user, claim: SrpClaim) -> bool:
    """``manage_srp``, and not on the manager's own claim unless the settings allow self review."""
    return access.can_review(user, claim)


def _require_manager(actor) -> None:
    if not access.is_manager(actor):
        raise SrpError("forbidden", NOT_A_MANAGER)


def _require_reviewer(actor, claim, srp=None) -> None:
    """``SrpError`` unless ``actor`` may decide on ``claim`` (a manager, not on their own claim)."""
    _require_manager(actor)
    if not access.can_review(actor, claim, settings_obj=srp):
        raise SrpError("own_claim", OWN_CLAIM)


def _amount(value) -> int:
    """``value`` as whole ISK between 1 and the largest amount."""
    if isinstance(value, Decimal) and value == value.to_integral_value():
        value = int(value)
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_ISK:
        raise SrpError("invalid_amount", texts.SRP_AMOUNT_RANGE(max=f"{MAX_ISK:,}"))
    return value


def _tightest(states):
    """``(remaining, kind)`` of the limit that leaves least for a claim; ``(None, "")`` without one."""
    tight = [
        (state.effective_remaining, 0 if state.kind == LIMIT_PERSONAL else 1, state.kind)
        for state in states
        if state.effective_remaining is not None
    ]
    if not tight:
        return None, ""
    remaining, _order, kind = min(tight)
    return remaining, kind


def _approval_state(claim) -> dict:
    return {
        "status": claim.status,
        "approved_amount": claim.approved_amount,
    }


def _check_shown(locked: SrpClaim, *, status=None, suggested=None, limited=None) -> None:
    """Refuse a decision made on a claim that is not the one the reviewer looked at: another status, or other
    stored amounts than those on the button that was pressed. ``None`` means nothing was shown to compare."""
    if status is not None and locked.status != status:
        raise SrpStateError(
            "changed",
            texts.SRP_STATUS_CHANGED(now=claims.status_label(locked.status), was=claims.status_label(status)),
        )
    stored = (
        (texts.SRP_SUGGESTED_CHANGED, suggested, locked.suggested_amount),
        (texts.SRP_LIMITED_CHANGED, limited, locked.limited_amount),
    )
    for message, shown, current in stored:
        if shown is not None and shown != current:
            raise SrpStateError(
                "amount_changed", message(shown=display.format_isk(shown), current=display.format_isk(current))
            )


def _approve_locked(
    locked: SrpClaim,
    *,
    actor,
    amount: int,
    reason: str,
    confirm_above_policy: bool,
    confirm_over_limit: bool,
    expected_status=None,
    expected_suggested=None,
    expected_limited=None,
) -> SrpClaim:
    """Approve (or change the amount of) a locked claim. Call it inside ``quota_lock``."""
    if locked.status == Status.PAID:
        raise SrpStateError("paid", texts.SRP_PAID_CHANGE())
    if locked.status not in (Status.PENDING, Status.APPROVED):
        raise claims.wrong_state(locked, texts.SRP_ONLY_PENDING_APPROVED())
    changing = locked.status == Status.APPROVED
    if changing and amount == locked.approved_amount:
        raise SrpStateError("already", texts.SRP_ALREADY_APPROVED(amount=display.format_isk(amount)))
    _check_shown(locked, status=expected_status, suggested=expected_suggested, limited=expected_limited)

    policy = PolicySnapshot.from_json(locked.policy)
    states = quota.limit_states(
        user_id=locked.user_id,
        corporation_id=locked.corporation_id,
        scope=locked.scope,
        killmail_time=locked.killmail_time,
        policy=policy,
        exclude_claim_id=locked.pk,
        locking=True,
    )
    remaining, kind = _tightest(states)
    over_by = amount - remaining if remaining is not None and amount > remaining else 0

    # With no automatic amount (no payout rule, no percentage) there is no policy to go above: the reviewer
    # types the amount and a reason, and the "above policy" box would only be a second question.
    no_automatic_amount = locked.suggested_amount == 0 and locked.payout_basis == SrpClaim.PayoutBasis.NONE
    needs_confirmation = amount > locked.suggested_amount and not no_automatic_amount
    if needs_confirmation and not confirm_above_policy:
        raise SrpLimitError(
            "confirm_above_policy",
            texts.SRP_ABOVE_POLICY(
                amount=display.format_isk(amount), policy=display.format_isk(locked.suggested_amount)
            ),
        )
    if over_by:
        if policy.over_limit_action == SrpSettings.OverLimitAction.REJECT:
            raise SrpLimitError(
                "over_limit",
                texts.SRP_LIMIT_LEFT(remaining=display.format_isk(remaining), limit=claims.limit_name(kind)),
            )
        if not confirm_over_limit:
            raise SrpLimitError(
                "confirm_over_limit",
                texts.SRP_OVER_LIMIT(
                    over=display.format_isk(over_by),
                    limit=claims.limit_name(kind),
                    remaining=display.format_isk(remaining),
                ),
            )
    if (changing or over_by or amount != locked.suggested_amount) and not reason:
        raise SrpError("reason_required", texts.SRP_AMOUNT_REASON())

    previous = _approval_state(locked)
    locked.status = Status.APPROVED
    locked.approved_amount = amount
    locked.decided_by = actor
    locked.decided_at = timezone.now()
    locked.decision_reason = reason
    locked.auto_rejected = False
    locked.save()
    action = Action.AMOUNT_CHANGE if changing else Action.APPROVE
    claims.record_event(
        locked,
        action,
        actor=actor,
        from_status=previous["status"],
        amount=amount,
        reason=reason,
        data={
            "previous_amount": previous["approved_amount"],
            "suggested_amount": locked.suggested_amount,
            "limited_amount": locked.limited_amount,
            "above_policy_confirmed": needs_confirmation,
            **({"no_automatic_amount": True} if amount > locked.suggested_amount and no_automatic_amount else {}),
            "over_limit_by": over_by,
        },
        old=previous,
        new=_approval_state(locked),
    )
    claims.notify_after_commit(locked, action.value)
    return locked


def approve_claim(
    claim: SrpClaim,
    *,
    actor,
    amount: int,
    reason: str = "",
    confirm_above_policy: bool = False,
    confirm_over_limit: bool = False,
    expected_status: str | None = None,
    expected_suggested: int | None = None,
    expected_limited: int | None = None,
) -> SrpClaim:
    """Pending to approved (or a changed amount on an approved claim). A reason is required when ``amount``
    differs from the suggested amount, for every change of an approved amount and when going over a limit;
    more than the policy needs ``confirm_above_policy``, more than the remaining monthly limit
    ``confirm_over_limit`` (refused in reject mode).

    The remaining limit is read again under the lock, so the second of two decisions near a cap sees what
    the first one approved. ``expected_status`` is the status the reviewer's page showed (an amount change
    must not approve a claim that was revoked meanwhile), ``expected_suggested`` and ``expected_limited``
    the stored amount behind the button that was pressed; the decision is refused when the claim is not as
    shown.
    """
    amount = _amount(amount)
    reason = claims.clean_reason(reason)
    with quota.quota_lock() as srp:
        locked = claims.lock_claim(claim)
        _require_reviewer(actor, locked, srp)
        _approve_locked(
            locked,
            actor=actor,
            amount=amount,
            reason=reason,
            confirm_above_policy=confirm_above_policy,
            confirm_over_limit=confirm_over_limit,
            expected_status=expected_status,
            expected_suggested=expected_suggested,
            expected_limited=expected_limited,
        )
    return claims.sync_claim(claim, locked)


def _approve_clean(claim, actor, expected_suggested=None) -> SrpClaim:
    with quota.quota_lock() as srp:
        locked = claims.lock_claim(claim)
        _require_reviewer(actor, locked, srp)
        if locked.status != Status.PENDING:
            raise claims.wrong_state(locked, texts.SRP_ONLY_PENDING_APPROVED(), Status.APPROVED)
        _check_shown(locked, suggested=expected_suggested)
        if locked.warnings or locked.over_limit:
            raise SrpError("not_clean", texts.SRP_HAS_WARNINGS())
        if locked.suggested_amount <= 0:
            raise SrpError("no_amount", texts.SRP_NO_SUGGESTION())
        _approve_locked(
            locked,
            actor=actor,
            amount=locked.suggested_amount,
            reason="",
            confirm_above_policy=False,
            confirm_over_limit=False,
        )
    return claims.sync_claim(claim, locked)


def _each(items, action) -> BulkResult:
    """Run ``action(claim)`` for every claim, oldest loss first; the ones that cannot be done are skipped
    with the reason, so one refusal does not stop the others."""
    result = BulkResult()
    ordered = sorted(items, key=lambda claim: (claim.killmail_time, claim.pk))
    for claim in ordered:
        try:
            done = action(claim)
        except SrpError as exc:
            result.skipped.append((claim, OWN_CLAIM_SKIPPED if exc.code == "own_claim" else exc.message))
        else:
            result.done.append(done)
    return result


def _claim_list(claims_) -> list:
    return [claims_] if isinstance(claims_, SrpClaim) else list(claims_)


def approve_clean_claims(claims_, *, actor, expected_amounts=None) -> BulkResult:
    """Approve pending claims without warnings at the suggested amount, each under its own lock.

    Claims with warnings, without a suggested amount or that no longer fit into a monthly limit are skipped,
    and so are the manager's own claims. ``expected_amounts`` maps a claim's ID to the suggested amount the
    reviewer saw; a claim whose suggested amount is another one by now is skipped as well.
    """
    _require_manager(actor)
    expected = expected_amounts or {}
    return _each(_claim_list(claims_), lambda claim: _approve_clean(claim, actor, expected.get(claim.pk)))


def reject_claim(claim: SrpClaim, *, actor, reason: str) -> SrpClaim:
    """Pending to rejected; the reason is shown to the claimant."""
    reason = claims.clean_reason(reason, required=True, what="reason for the rejection")
    with quota.quota_lock() as srp:
        locked = claims.lock_claim(claim)
        _require_reviewer(actor, locked, srp)
        if locked.status != Status.PENDING:
            raise claims.wrong_state(locked, texts.SRP_ONLY_PENDING_REJECTED(), Status.REJECTED)
        previous = locked.status
        locked.status = Status.REJECTED
        locked.approved_amount = None
        locked.auto_rejected = False
        locked.decided_by = actor
        locked.decided_at = timezone.now()
        locked.decision_reason = reason
        locked.save()
        claims.record_event(
            locked,
            Action.REJECT,
            actor=actor,
            from_status=previous,
            reason=reason,
            old={"status": previous},
            new={"status": locked.status},
        )
        claims.notify_after_commit(locked, "reject")
    return claims.sync_claim(claim, locked)


def revoke_approval(claim: SrpClaim, *, actor, reason: str) -> SrpClaim:
    """Approved back to pending; the amount counts toward the limits no more."""
    reason = claims.clean_reason(reason, required=True, what="reason for revoking the approval")
    with quota.quota_lock() as srp:
        locked = claims.lock_claim(claim)
        _require_reviewer(actor, locked, srp)
        if locked.status == Status.PAID:
            raise SrpStateError("paid", texts.SRP_PAID_REVOKE())
        if locked.status != Status.APPROVED:
            raise claims.wrong_state(locked, texts.SRP_ONLY_APPROVED_REVOKED(), Status.PENDING)
        previous = _approval_state(locked)
        locked.status = Status.PENDING
        locked.approved_amount = None
        locked.decided_by = actor
        locked.decided_at = timezone.now()
        locked.decision_reason = reason
        locked.save()
        claims.record_event(
            locked,
            Action.REVOKE,
            actor=actor,
            from_status=previous["status"],
            amount=previous["approved_amount"],
            reason=reason,
            old=previous,
            new=_approval_state(locked),
        )
        claims.notify_after_commit(locked, "revoke")
    return claims.sync_claim(claim, locked)


def _mark_paid(claim, actor, note, expected_amount=None) -> SrpClaim:
    with quota.quota_lock() as srp:
        locked = claims.lock_claim(claim)
        _require_reviewer(actor, locked, srp)
        if locked.status != Status.APPROVED:
            raise claims.wrong_state(locked, texts.SRP_ONLY_APPROVED_PAID(), Status.PAID)
        if expected_amount is not None and expected_amount != locked.approved_amount:
            raise SrpStateError(
                "amount_changed",
                texts.SRP_APPROVED_CHANGED(
                    expected=display.format_isk(expected_amount), current=display.format_isk(locked.approved_amount)
                ),
            )
        locked.status = Status.PAID
        locked.paid_by = actor
        locked.paid_at = timezone.now()
        locked.payment_note = note
        locked.save()
        claims.record_event(
            locked,
            Action.PAID,
            actor=actor,
            from_status=Status.APPROVED,
            amount=locked.approved_amount,
            reason=note,
            old={"status": Status.APPROVED},
            new={"status": locked.status, "approved_amount": locked.approved_amount},
        )
        claims.notify_after_commit(locked, "paid")
    return claims.sync_claim(claim, locked)


def mark_paid(claims_, *, actor, note: str = "", expected_amounts=None) -> BulkResult:
    """Approved to paid, each claim under its own row lock.

    Claims that are not approved are skipped with the reason, and so are the manager's own claims.
    ``expected_amounts`` maps a claim's ID to the approved amount the payer was shown and sent; a claim whose
    approved amount is another one by now is skipped, so a payment is never booked for a different amount.
    """
    _require_manager(actor)
    note = (note or "").strip()
    if len(note) > MAX_PAYMENT_NOTE_LENGTH:
        raise SrpError("note_too_long", texts.SRP_NOTE_TOO_LONG(max=MAX_PAYMENT_NOTE_LENGTH))
    expected = expected_amounts or {}
    return _each(_claim_list(claims_), lambda claim: _mark_paid(claim, actor, note, expected.get(claim.pk)))


def undo_payment(claim: SrpClaim, *, actor, reason: str) -> SrpClaim:
    """Paid back to approved; the amount stays approved and counted."""
    reason = claims.clean_reason(reason, required=True, what="reason for undoing the payment")
    with quota.quota_lock() as srp:
        locked = claims.lock_claim(claim)
        _require_reviewer(actor, locked, srp)
        if locked.status != Status.PAID:
            raise claims.wrong_state(locked, texts.SRP_ONLY_PAID_UNPAID(), Status.APPROVED)
        previous = {"status": locked.status, "paid_at": locked.paid_at.isoformat() if locked.paid_at else None}
        locked.status = Status.APPROVED
        locked.paid_by = None
        locked.paid_at = None
        locked.payment_note = ""
        locked.save()
        claims.record_event(
            locked,
            Action.UNPAID,
            actor=actor,
            from_status=Status.PAID,
            amount=locked.approved_amount,
            reason=reason,
            old=previous,
            new={"status": locked.status},
        )
    return claims.sync_claim(claim, locked)


def reopen_claim(claim: SrpClaim, *, actor, reason: str) -> SrpClaim:
    """Rejected back to pending, recalculated with the current policy and limits."""
    reason = claims.clean_reason(reason, required=True, what="reason for reopening the claim")
    now = timezone.now()
    with quota.quota_lock() as srp:
        locked = claims.lock_claim(claim)
        _require_reviewer(actor, locked, srp)
        if locked.status != Status.REJECTED:
            raise claims.wrong_state(locked, texts.SRP_ONLY_REJECTED_REOPENED(), Status.PENDING)
        km = locked.killmail
        previous = claims.calculation_snapshot(locked)
        evaluation = claims.evaluate(
            km=km,
            user_id=locked.user_id,
            corporation_id=locked.corporation_id,
            scope=locked.scope,
            operation=locked.operation,
            settings_obj=srp,
            exclude_claim_id=locked.pk,
            locking=True,
            now=now,
        )
        claims.apply_evaluation(locked, evaluation, km)
        locked.status = Status.PENDING
        locked.auto_rejected = False
        locked.approved_amount = None
        locked.decided_by = actor
        locked.decided_at = now
        locked.decision_reason = reason
        locked.save()
        claims.record_event(
            locked,
            Action.REOPEN,
            actor=actor,
            from_status=Status.REJECTED,
            amount=locked.suggested_amount,
            reason=reason,
            data={"previous": previous},
            old={"status": Status.REJECTED},
            new={"status": locked.status, "suggested_amount": locked.suggested_amount},
        )
        claims.notify_after_commit(locked, "reopen")
    return claims.sync_claim(claim, locked)


def reclassify_claim(claim: SrpClaim, *, actor, scope: str, link=None, reason: str) -> SrpClaim:
    """Move a pending claim between in fleet and out of fleet; in fleet needs an open fleet link.

    The claim is calculated again (payout rule, caps and which limits count) and the fleet presence is
    checked against the new fleet.
    """
    reason = claims.clean_reason(reason, required=True, what="reason")
    if scope not in (Scope.IN_FLEET, Scope.OUT_OF_FLEET):
        raise SrpError("invalid_scope", texts.SRP_CHOOSE_SCOPE())
    now = timezone.now()
    with quota.quota_lock() as srp:
        locked = claims.lock_claim(claim)
        _require_reviewer(actor, locked, srp)
        if locked.status != Status.PENDING:
            raise claims.wrong_state(locked, texts.SRP_ONLY_PENDING_MOVED())
        target = None
        if scope == Scope.IN_FLEET:
            if link is None:
                raise SrpError("link_required", texts.SRP_CHOOSE_FLEET())
            target = (
                SrpFleetLink.objects.select_related("operation__fleet_type", "fc_user").filter(pk=link.pk).first()
            )
            if target is None:
                raise SrpError("link_required", texts.SRP_CHOOSE_FLEET())
            if not target.is_open:
                raise SrpError("link_closed", texts.SRP_LINK_CLOSED())
            if locked.scope == Scope.IN_FLEET and locked.link_id == target.pk:
                raise SrpError("unchanged", texts.SRP_SAME_FLEET())
        else:
            if locked.scope == Scope.OUT_OF_FLEET:
                raise SrpError("unchanged", texts.SRP_SAME_OUT_OF_FLEET())
            if not srp.out_of_fleet_enabled:
                raise SrpError("fleet_only", str(rules.BLOCK_FLEET_ONLY))

        previous = claims.calculation_snapshot(locked)
        previous_link = locked.link.code if locked.link_id and locked.link else None
        km = locked.killmail
        locked.scope = scope
        for field, value in claims.fleet_fields(target).items():
            setattr(locked, field, value)
        evaluation = claims.evaluate(
            km=km,
            user_id=locked.user_id,
            corporation_id=locked.corporation_id,
            scope=scope,
            operation=locked.operation,
            settings_obj=srp,
            exclude_claim_id=locked.pk,
            locking=True,
            now=now,
        )
        claims.apply_evaluation(locked, evaluation, km)
        claims.apply_presence(locked, claims.loss_presence(scope, target, km), now)
        locked.save()
        claims.record_event(
            locked,
            Action.RECLASSIFY,
            actor=actor,
            from_status=Status.PENDING,
            amount=locked.suggested_amount,
            reason=reason,
            data={
                "from_scope": previous["scope"],
                "to_scope": scope,
                "from_link": previous_link,
                "to_link": target.code if target is not None else None,
                "previous": previous,
            },
            old={"scope": previous["scope"], "suggested_amount": previous["suggested_amount"]},
            new={"scope": scope, "suggested_amount": locked.suggested_amount},
        )
    return claims.sync_claim(claim, locked)


def recalculate_claim(claim: SrpClaim, *, actor=None, reason: str = "") -> SrpClaim:
    """Recalculate a pending claim with the current policy; the old calculation is kept in the event.

    Without ``actor`` it is the system (such as a value that arrived). A person needs to be allowed to
    decide on the claim.
    """
    reason = claims.clean_reason(reason)
    if actor is not None:
        _require_reviewer(actor, claim)
    return claims.recalculate_under_lock(claim, actor=actor, reason=reason)


def recalculate_pending(*, actor) -> int:
    """Recalculate every pending claim, oldest loss first; returns how many.

    The manager's own claims are left to another manager unless the settings allow self review.
    """
    _require_manager(actor)
    srp = SrpSettings.get_solo()
    done = 0
    pending = SrpClaim.objects.filter(status=Status.PENDING).order_by("killmail_time", "pk")
    for claim in pending:
        if not access.can_review(actor, claim, settings_obj=srp):
            continue
        try:
            claims.recalculate_under_lock(claim, actor=actor, reason=texts.SRP_REASON_RECALCULATED())
        except SrpError:
            # Decided or withdrawn since the list was read.
            continue
        done += 1
    return done


def recheck_presence(claim: SrpClaim, *, actor) -> SrpClaim:
    """Look up again whether the pilot was in the fleet at the loss time (the fleet may have been read since).

    Needs ``manage_srp`` only; the claim may be in any status.
    """
    _require_manager(actor)
    now = timezone.now()
    with transaction.atomic():
        locked = claims.lock_claim(claim)
        km = locked.killmail
        if locked.scope == Scope.IN_FLEET and locked.operation is None:
            raise SrpError("no_fleet", texts.SRP_FLEET_GONE())
        previous = {"fleet_check": locked.fleet_check, "fleet_check_params": locked.fleet_check_params}
        result = claims.loss_presence(locked.scope, locked.link, km)
        claims.apply_presence(locked, result, now)
        locked.save()
        claims.record_event(
            locked,
            Action.RECHECK,
            actor=actor,
            from_status=locked.status,
            data={"previous": previous, "fleet_check": result.code, "fleet_check_params": result.params},
            old={"fleet_check": previous["fleet_check"]},
            new={"fleet_check": result.code},
        )
    return claims.sync_claim(claim, locked)


def add_note(claim: SrpClaim, *, actor, text: str) -> SrpClaimEvent:
    """An internal note, visible to managers only."""
    _require_manager(actor)
    text = (text or "").strip()
    if not text:
        raise SrpError("note_required", texts.SRP_NOTE_REQUIRED())
    if len(text) > MAX_NOTE_LENGTH:
        raise SrpError("note_too_long", texts.SRP_NOTE_TOO_LONG(max=MAX_NOTE_LENGTH))
    with transaction.atomic():
        locked = claims.lock_claim(claim)
        return claims.record_event(
            locked,
            Action.NOTE,
            actor=actor,
            from_status=locked.status,
            reason=text,
            internal=True,
            new={"note_length": len(text)},
        )
