"""The PAP history behind the history pages and the ``attendance`` export.

Both read the rows through :func:`history_queryset`, so a download always holds what its page lists.
"""

from __future__ import annotations

from fleetops.models import MIN_RETENTION_DAYS, FleetOpsSettings
from fleetops.services.periods import MAX_YEAR, MIN_YEAR, month_bounds
from fleetops.services.statistics import attendance_history_queryset

SCOPES = ("me", "corporation", "alliance")


def retention_days() -> int:
    """How far back the history reaches: the configured retention, at least the minimum."""
    return max(MIN_RETENTION_DAYS, FleetOpsSettings.get_solo().data_retention_days)


def history_month(params) -> tuple[int, int] | None:
    """The EVE month chosen with ``year`` and ``month``; None (the whole retention window) unless both are valid."""
    try:
        year = int(params.get("year", ""))
        month = int(params.get("month", ""))
    except (TypeError, ValueError):
        return None
    if not 1 <= month <= 12 or not MIN_YEAR <= year <= MAX_YEAR:
        return None
    return year, month


def history_queryset(scope: str, *, user=None, corporation_id=None, month: tuple[int, int] | None = None):
    """Counted PAP rows, newest fleet first.

    ``scope`` is ``me`` (the PAP of ``user``), ``corporation`` (PAP earned while the main was in
    ``corporation_id``) or ``alliance``. ``month`` is a ``(year, month)`` pair in EVE time; None keeps the whole
    retention window.
    """
    if scope not in SCOPES:
        raise ValueError(f"Unknown history scope {scope!r}.")
    # Without the user or the corporation the base queryset would hold everybody's PAP.
    if (scope == "me" and user is None) or (scope == "corporation" and corporation_id is None):
        raise ValueError(f"The {scope} history needs a {'user' if scope == 'me' else 'corporation'}.")
    queryset = attendance_history_queryset(
        user=user if scope == "me" else None,
        corporation_id=corporation_id if scope == "corporation" else None,
        alliance=scope == "alliance",
        days=retention_days(),
    )
    if month is not None:
        start, end = month_bounds(*month)
        queryset = queryset.filter(operation__started_at__gte=start, operation__started_at__lt=end)
    return queryset
