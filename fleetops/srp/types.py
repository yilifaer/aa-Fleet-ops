# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Value objects and errors shared by the SRP services."""

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from fleetops.srp import display


class SrpError(Exception):
    """An SRP request that cannot go ahead. ``message`` is written for the member."""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


class SrpStateError(SrpError):
    """The claim is not in a status the action needs, such as a second click on "Approve"."""


class SrpLimitError(SrpError):
    """An amount that does not fit into a limit or needs a confirmation."""


class SrpDuplicateError(SrpError):
    """The killmail is already claimed; ``claim`` is the existing claim."""

    def __init__(self, message: str, claim: Any = None, code: str = "duplicate"):
        super().__init__(code, message)
        self.claim = claim


@dataclass(frozen=True, slots=True)
class KillmailRef:
    """A killmail as pasted by a member: always the ESI id, the hash when the input had one."""

    killmail_id: int
    killmail_hash: str | None
    source: str


ZKILL_STATUSES = ("ok", "missing", "unavailable", "throttled", "blocked")


@dataclass(slots=True)
class ZkillResult:
    """One answer of the zKillboard killmail API.

    ``requested`` is False when no request was sent (busy, paused or too recent), so a
    retry schedule does not count it as an attempt. ``retry_after`` is in seconds.
    """

    status: str
    killmail_hash: str | None = None
    fitted_value: Decimal | None = None
    total_value: Decimal | None = None
    destroyed_value: Decimal | None = None
    dropped_value: Decimal | None = None
    npc: bool | None = None
    solo: bool | None = None
    awox: bool | None = None
    labels: list[str] = field(default_factory=list)
    retry_after: int | None = None
    message: str = ""
    requested: bool = True

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    @property
    def has_value(self) -> bool:
        return self.ok and (self.fitted_value is not None or self.total_value is not None)


# --- Policy and calculation -----------------------------------------------------------------------

LIMIT_PERSONAL = "personal"
LIMIT_CORPORATION = "corporation"
LIMIT_LOSS_CAP = "loss_cap"

MATCH_DOCTRINE_SHIP = "doctrine_ship"
MATCH_DOCTRINE = "doctrine"
MATCH_SHIP = "ship"

CORPORATION_CAP_DEFAULT = "default"
CORPORATION_CAP_OVERRIDE = "override"


def _decimal(value) -> Decimal | None:
    return display.to_decimal(value)


@dataclass(frozen=True, slots=True)
class RuleSnapshot:
    """The payout rule a claim was calculated with; ``match`` says how specific the match was."""

    id: int | None = None
    label: str = ""
    ship_type_id: int = 0
    ship_type_name: str = ""
    doctrine_name: str = ""
    scope: str = "any"
    amount: int = 0
    match: str = MATCH_SHIP

    def as_json(self) -> dict:
        return {
            "id": self.id,
            "label": self.label,
            "ship_type_id": self.ship_type_id,
            "ship_type_name": self.ship_type_name,
            "doctrine_name": self.doctrine_name,
            "scope": self.scope,
            "amount": self.amount,
            "match": self.match,
        }

    @classmethod
    def from_json(cls, data) -> "RuleSnapshot | None":
        if not isinstance(data, dict):
            return None
        return cls(
            id=data.get("id"),
            label=data.get("label") or "",
            ship_type_id=data.get("ship_type_id") or 0,
            ship_type_name=data.get("ship_type_name") or "",
            doctrine_name=data.get("doctrine_name") or "",
            scope=data.get("scope") or "any",
            amount=data.get("amount") or 0,
            match=data.get("match") or MATCH_SHIP,
        )


@dataclass(frozen=True, slots=True)
class PolicySnapshot:
    """The SRP settings a claim was calculated with, kept on the claim as JSON."""

    version: int = 1
    value_source: str = "zkill_fitted"
    percentage: Decimal = Decimal("0")
    in_fleet_loss_cap: int | None = None
    out_of_fleet_loss_cap: int | None = None
    personal_monthly_cap: int | None = None
    personal_cap_includes_in_fleet: bool = True
    corporation_monthly_cap: int | None = None
    corporation_cap_source: str = CORPORATION_CAP_DEFAULT
    corporation_cap_includes_in_fleet: bool = True
    over_limit_action: str = "review"
    capsules_need_rule: bool = True
    claim_deadline_days: int = 0
    rule: RuleSnapshot | None = None

    def as_json(self) -> dict:
        return {
            "version": self.version,
            "value_source": self.value_source,
            "percentage": str(self.percentage),
            "in_fleet_loss_cap": self.in_fleet_loss_cap,
            "out_of_fleet_loss_cap": self.out_of_fleet_loss_cap,
            "personal_monthly_cap": self.personal_monthly_cap,
            "personal_cap_includes_in_fleet": self.personal_cap_includes_in_fleet,
            "corporation_monthly_cap": self.corporation_monthly_cap,
            "corporation_cap_source": self.corporation_cap_source,
            "corporation_cap_includes_in_fleet": self.corporation_cap_includes_in_fleet,
            "over_limit_action": self.over_limit_action,
            "capsules_need_rule": self.capsules_need_rule,
            "claim_deadline_days": self.claim_deadline_days,
            "rule": self.rule.as_json() if self.rule else None,
        }

    @classmethod
    def from_json(cls, data) -> "PolicySnapshot":
        """The snapshot stored on a claim; missing or unreadable parts fall back to the defaults."""
        data = data if isinstance(data, dict) else {}
        defaults = cls()
        percentage = _decimal(data.get("percentage"))
        return cls(
            version=data.get("version") or defaults.version,
            value_source=data.get("value_source") or defaults.value_source,
            percentage=defaults.percentage if percentage is None else percentage,
            in_fleet_loss_cap=data.get("in_fleet_loss_cap"),
            out_of_fleet_loss_cap=data.get("out_of_fleet_loss_cap"),
            personal_monthly_cap=data.get("personal_monthly_cap"),
            personal_cap_includes_in_fleet=data.get("personal_cap_includes_in_fleet", True),
            corporation_monthly_cap=data.get("corporation_monthly_cap"),
            corporation_cap_source=data.get("corporation_cap_source") or defaults.corporation_cap_source,
            corporation_cap_includes_in_fleet=data.get("corporation_cap_includes_in_fleet", True),
            over_limit_action=data.get("over_limit_action") or defaults.over_limit_action,
            capsules_need_rule=data.get("capsules_need_rule", True),
            claim_deadline_days=data.get("claim_deadline_days") or 0,
            rule=RuleSnapshot.from_json(data.get("rule")),
        )


@dataclass(frozen=True, slots=True)
class LimitState:
    """One monthly limit for a claim: ``kind`` is ``personal`` or ``corporation``.

    ``cap`` is None without a limit; ``used`` are approved and paid amounts of the month, ``pending`` the
    amounts of claims still in review. ``remaining`` is ``max(cap - used, 0)`` or None without a cap, and
    ``applies`` is False when in-fleet losses do not count toward this limit.
    """

    kind: str
    cap: int | None = None
    used: int = 0
    pending: int = 0
    remaining: int | None = None
    applies: bool = True

    @property
    def effective_remaining(self) -> int | None:
        """What this limit leaves for the claim; None when it does not restrict it."""
        return self.remaining if self.applies else None

    def as_json(self) -> dict:
        return {
            "kind": self.kind,
            "cap": self.cap,
            "used": self.used,
            "pending": self.pending,
            "remaining": self.remaining,
            "applies": self.applies,
        }

    @classmethod
    def from_json(cls, data) -> "LimitState":
        data = data if isinstance(data, dict) else {}
        return cls(
            kind=data.get("kind") or "",
            cap=data.get("cap"),
            used=data.get("used") or 0,
            pending=data.get("pending") or 0,
            remaining=data.get("remaining"),
            applies=data.get("applies", True),
        )


@dataclass(frozen=True, slots=True)
class CalcNote:
    """A remark on the calculation, stored as a code with parameters (``display.note_text``)."""

    code: str
    params: dict = field(default_factory=dict)

    @property
    def text(self) -> str:
        return display.note_text(self.code, self.params)

    def as_json(self) -> dict:
        return {"code": self.code, "params": dict(self.params)}

    @classmethod
    def from_json(cls, data) -> "CalcNote | None":
        if not isinstance(data, dict) or not data.get("code"):
            return None
        params = data.get("params")
        return cls(data["code"], params if isinstance(params, dict) else {})


@dataclass(frozen=True, slots=True)
class Calculation:
    """The outcome of ``policy.calculate``.

    ``basis`` is ``rule``, ``percentage`` or ``none``; ``base`` the amount before caps; ``cap`` the per-loss cap
    of the claim's scope (None without one); ``suggested`` the amount after the per-loss cap; ``limited`` the
    amount after the monthly limits. ``over_limit`` is ``limited < suggested``. ``binding_limit`` names the most
    restrictive limit (``loss_cap``, ``personal`` or ``corporation``) and is empty when nothing reduced the
    amount; with equal results the monthly limits come before the per-loss cap and personal before corporation.
    """

    basis: str = "none"
    loss_value: Decimal | None = None
    value_source: str = ""
    base: int = 0
    cap: int | None = None
    suggested: int = 0
    limits: tuple[LimitState, ...] = ()
    limited: int = 0
    over_limit: bool = False
    binding_limit: str = ""
    notes: tuple[CalcNote, ...] = ()
    computed_at: datetime | None = None

    def limit(self, kind: str) -> LimitState | None:
        for state in self.limits:
            if state.kind == kind:
                return state
        return None

    def as_json(self) -> dict:
        return {
            "basis": self.basis,
            "loss_value": None if self.loss_value is None else str(self.loss_value),
            "value_source": self.value_source,
            "base": self.base,
            "cap": self.cap,
            "suggested": self.suggested,
            "limits": [state.as_json() for state in self.limits],
            "limited": self.limited,
            "over_limit": self.over_limit,
            "binding_limit": self.binding_limit,
            "notes": [note.as_json() for note in self.notes],
            "computed_at": self.computed_at.isoformat() if self.computed_at else None,
        }

    @classmethod
    def from_json(cls, data) -> "Calculation":
        """The calculation stored on a claim; missing or unreadable parts fall back to the defaults."""
        data = data if isinstance(data, dict) else {}
        computed_at = data.get("computed_at")
        if isinstance(computed_at, str):
            from django.utils.dateparse import parse_datetime

            computed_at = parse_datetime(computed_at)
        notes = (CalcNote.from_json(item) for item in data.get("notes") or ())
        return cls(
            basis=data.get("basis") or "none",
            loss_value=_decimal(data.get("loss_value")),
            value_source=data.get("value_source") or "",
            base=data.get("base") or 0,
            cap=data.get("cap"),
            suggested=data.get("suggested") or 0,
            limits=tuple(LimitState.from_json(item) for item in data.get("limits") or ()),
            limited=data.get("limited") or 0,
            over_limit=bool(data.get("over_limit")),
            binding_limit=data.get("binding_limit") or "",
            notes=tuple(note for note in notes if note is not None),
            computed_at=computed_at if isinstance(computed_at, datetime) else None,
        )


# --- Fleet presence and warnings -------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PresenceResult:
    """Whether the victim was in the fleet at the loss time.

    ``code`` is one of ``in_fleet``, ``near``, ``not_in_fleet``, ``never_seen``, ``registered``,
    ``outside_window`` and ``not_applicable`` (``display.PRESENCE_TEXTS``); ``level`` is ``ok``, ``warning`` or
    ``danger``. ``params`` are the numbers and names the text needs.
    """

    code: str
    level: str = display.LEVEL_OK
    params: dict = field(default_factory=dict)
    tracked_ship_type_id: int | None = None
    tracked_ship_name: str = ""

    @property
    def text(self) -> str:
        return display.presence_text(self.code, self.params)

    def as_json(self) -> dict:
        return {
            "code": self.code,
            "level": self.level,
            "params": dict(self.params),
            "tracked_ship_type_id": self.tracked_ship_type_id,
            "tracked_ship_name": self.tracked_ship_name,
        }


@dataclass(frozen=True, slots=True)
class ClaimWarning:
    """A remark that stays with a claim: a code, how serious it is and the numbers or names for its text."""

    code: str
    level: str = display.LEVEL_WARNING
    params: dict = field(default_factory=dict)

    @property
    def text(self) -> str:
        return display.warning_text(self.code, self.params)

    def as_json(self) -> dict:
        return {"code": self.code, "level": self.level, "params": dict(self.params)}

    @classmethod
    def from_json(cls, data) -> "ClaimWarning | None":
        if not isinstance(data, dict) or not data.get("code"):
            return None
        params = data.get("params")
        return cls(data["code"], data.get("level") or display.LEVEL_WARNING, params if isinstance(params, dict) else {})


def warnings_from_json(data) -> list[ClaimWarning]:
    """The warnings stored on a claim, without entries that cannot be read."""
    found = (ClaimWarning.from_json(item) for item in data or ())
    return [warning for warning in found if warning is not None]


# --- Preview and bulk results ------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ClaimIdentity:
    """The victim character, the owner's main and the main's corporation and alliance when a claim is made."""

    character_id: int
    character_name: str
    main_character_id: int | None = None
    main_character_name: str = ""
    corporation_id: int | None = None
    corporation_name: str = ""
    corporation_ticker: str = ""
    alliance_id: int | None = None
    alliance_name: str = ""


@dataclass(slots=True)
class ClaimPreview:
    """What a member sees before submitting: the killmail, the checks and the calculation.

    ``blockers`` are texts for the member; with any blocker the claim cannot be submitted. ``token`` is the
    signed reference the submit step needs. ``suggested_link`` is the fleet link whose fleet contains an
    out-of-fleet loss.
    """

    killmail: Any
    scope: str
    link: Any = None
    user: Any = None
    identity: ClaimIdentity | None = None
    eligible: bool = False
    blockers: list[str] = field(default_factory=list)
    warnings: list[ClaimWarning] = field(default_factory=list)
    presence: PresenceResult | None = None
    policy: PolicySnapshot | None = None
    calculation: Calculation | None = None
    existing_claim: Any = None
    suggested_link: Any = None
    token: str = ""


@dataclass(slots=True)
class BulkResult:
    """The outcome of an action on several claims: the claims done and the ones skipped with a reason."""

    done: list = field(default_factory=list)
    skipped: list[tuple[Any, str]] = field(default_factory=list)


# --- Summaries -----------------------------------------------------------------------------------------


@dataclass(slots=True)
class SrpTotals:
    """Claims and ISK of a month. ``requested`` adds the suggested amounts of pending, approved, paid and
    rejected claims, ``approved`` the amounts of approved and paid ones, ``paid`` those of paid ones and
    ``outstanding`` is ``approved - paid``."""

    claims: int = 0
    pending: int = 0
    approved_count: int = 0
    paid_count: int = 0
    rejected: int = 0
    requested: int = 0
    approved: int = 0
    paid: int = 0
    outstanding: int = 0


@dataclass(slots=True)
class PersonalSrp:
    """The claims of one player in a month with the limits that apply to the player now."""

    year: int
    month: int
    totals: SrpTotals = field(default_factory=SrpTotals)
    claims: list = field(default_factory=list)
    limits: tuple[LimitState, ...] = ()


@dataclass(slots=True)
class SrpMemberRow:
    user_id: int | None
    main_character_id: int | None
    main_character_name: str
    corporation_id: int | None
    corporation_name: str
    totals: SrpTotals = field(default_factory=SrpTotals)


@dataclass(slots=True)
class SrpCorporationRow:
    corporation_id: int | None
    corporation_name: str
    corporation_ticker: str = ""
    totals: SrpTotals = field(default_factory=SrpTotals)
    cap: int | None = None
    used: int = 0
    remaining: int | None = None


@dataclass(slots=True)
class SrpFcRow:
    fc_user_id: int | None
    fc_name: str
    fleets: int = 0
    totals: SrpTotals = field(default_factory=SrpTotals)


@dataclass(slots=True)
class SrpFleetRow:
    link_code: str
    operation_uuid: Any
    fleet_label: str
    is_open: bool = True
    totals: SrpTotals = field(default_factory=SrpTotals)


@dataclass(slots=True)
class SrpShipRow:
    ship_type_id: int
    ship_type_name: str
    losses: int = 0
    approved: int = 0


@dataclass(slots=True)
class SrpAllianceSummary:
    year: int
    month: int
    totals: SrpTotals = field(default_factory=SrpTotals)
    in_fleet: SrpTotals = field(default_factory=SrpTotals)
    out_of_fleet: SrpTotals = field(default_factory=SrpTotals)
    top_ships: list[SrpShipRow] = field(default_factory=list)
    by_status: dict[str, int] = field(default_factory=dict)


@dataclass(slots=True)
class SrpPayRow:
    """Approved claims that are not paid yet, grouped by the main character to pay."""

    user_id: int | None
    main_character_id: int | None
    main_character_name: str
    total: int = 0
    claims: list = field(default_factory=list)
