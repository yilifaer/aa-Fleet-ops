# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Result types shared by the statistics services, pages and exports.

They are plain dataclasses without behaviour so that the corporation, member and export code can be written
against them independently.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from fleetops.models import FleetType
from fleetops.services.assessment import MonthTarget
from fleetops.services.periods import MonthRef


@dataclass(slots=True)
class MemberRow:
    """One member of the population with the PAP of a month.

    ``status`` is a ``assessment.member_status`` key; ``rank`` is None without PAP.
    """

    user_id: int
    main_character_id: int
    main_character_name: str
    corporation_id: int
    corporation_name: str
    corporation_ticker: str
    alliance_id: int | None
    alliance_name: str
    registered_at: datetime
    pap: int = 0
    fleets: int = 0
    characters: int = 0
    by_fleet_type: dict[int, int] = field(default_factory=dict)
    rank: int | None = None
    status: str = "ok"
    # Only after members.fill_last_pap().
    last_pap_at: datetime | None = None


@dataclass(slots=True)
class MemberTable:
    """The rows of a members page or export, filtered and sorted, with what the page header needs.

    ``counts`` holds ``all``, ``with_pap`` and ``inactive`` for the whole scope, before the status and
    name filters.
    """

    rows: list[MemberRow]
    month: MonthRef
    target: MonthTarget
    fleet_types: list[FleetType] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    corporation_id: int | None = None


@dataclass(slots=True)
class Leaderboard:
    """The top places of a month. ``viewer_row`` is the viewer's own row when it is outside the shown places."""

    rows: list[MemberRow]
    size: int
    scope_label: str
    month: MonthRef | None = None
    corporation_id: int | None = None
    # Members with PAP in the scope ("Your rank: #34 of 212").
    ranked_count: int = 0
    viewer_row: MemberRow | None = None


@dataclass(slots=True)
class MemberFleetRow:
    """A fleet a member earned PAP in; PAP is the sum of the member's characters in that fleet."""

    operation_uuid: str
    started_at: datetime
    ended_at: datetime | None
    fleet_type: str
    doctrine: str
    fc_name: str
    character_name: str
    source: str
    pap: int
    # The name of the group a group fleet was flown for; empty for an alliance fleet.
    group_name: str = ""


@dataclass(slots=True)
class CorporationRow:
    """A corporation of a month. Mains and member counts are current; PAP totals use the corporation at PAP time."""

    corporation_id: int | None
    corporation_name: str
    corporation_ticker: str
    alliance_id: int | None
    alliance_name: str
    mains: int
    mains_with_pap: int
    mains_below_minimum: int
    total_pap: int
    fleets: int
    by_fleet_type: dict[int, int]
    # Total PAP divided by mains, rounded half up to two places; 0 without mains.
    average: Decimal
    status: str
    # The summary row of corporations without members in scope.
    is_other: bool = False


@dataclass(slots=True)
class CorporationOverview:
    """The all-corporations page: one row per corporation, the "Other corporations" row and the alliance totals."""

    month: MonthRef
    target: MonthTarget
    rows: list[CorporationRow]
    other: CorporationRow | None
    fleet_types: list[FleetType]
    sort: str = "name"
    # Whether Member alliance IDs are set; without them the population is every Alliance Auth main.
    alliance_filter_configured: bool = False
    # Every counted PAP row of the month, and the part earned by members of the population.
    alliance_pap: int = 0
    member_pap: int = 0
    mains: int = 0
    mains_with_pap: int = 0
    # Members' PAP divided by mains.
    average: Decimal = Decimal("0.00")
    corporations_met: int = 0
    corporations_with_target: int = 0
    # Fleets of the month that do not count: excluded, pending, rejected and running.
    not_counted: dict[str, int] = field(default_factory=dict)


@dataclass(slots=True)
class FcRow:
    """An FC with the fleets and FC points credited in a month.

    ``breakdown`` maps a fleet type name to ``{"count": int, "points": Decimal}``. ``operation_ids`` are the
    credited fleets; a fleet credited to its FC and to a special role is in two rows, so counting the fleets
    flown takes the union.
    """

    user: object
    main_name: str
    fleets: int
    points: Decimal
    breakdown: dict[str, dict] = field(default_factory=dict)
    operation_ids: frozenset = frozenset()

    @property
    def user_id(self) -> int:
        return self.user.pk
