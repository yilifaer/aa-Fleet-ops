"""Value objects and errors shared by the SRP services."""

from dataclasses import dataclass, field
from decimal import Decimal


class SrpError(Exception):
    """An SRP request that cannot go ahead. ``message`` is written for the member."""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


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
