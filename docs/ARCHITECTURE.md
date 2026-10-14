# Architecture

## Core principle

One real fleet equals one `FleetOperation`.

Everything else attaches to that operation instead of creating disconnected ping/FAT/statistics workflows.

```text
FleetOperation
├── OperationAction
├── FleetMemberState
├── FleetMemberEvent
├── AttendanceRecord
├── OperationRoleAssignment
├── SRP provider/reference/link snapshot
├── Ping snapshot
├── MOTD snapshot
├── Fleet Type / point-weight snapshot
└── operation timestamps and ESI tracking state

IncentivePeriod
└── MonthlyFCStatistic
```

## Services

The current implementation separates core behavior into service/provider modules so that HTTP views do not contain all business logic.

Important areas include:

- `providers/esi.py` — ESI access abstraction
- `providers/doctrines.py` — optional doctrine discovery
- `providers/pings.py` — ping delivery abstraction
- `providers/srp.py` — SRP provider registry: FleetOps SRP, aa-srp and the built-in AA adapter
- `providers/zkillboard.py`, `providers/esi_killmails.py` — zKillboard lookups (throttled, with a pause after a block) and ESI killmails/market prices for FleetOps SRP
- `srp/` — FleetOps SRP: killmail intake and values (`killmails.py`, `values.py`), rules and amounts (`policy.py`, `quota.py`, `presence.py`), claims and decisions (`claims.py`, `review.py`, `notify.py`), fleet links (`links.py`), summaries (`summaries.py`), access rules (`access.py`)
- `services/operations.py` — operation start/end lifecycle
- `services/tracking.py` — fleet-member current-state/event diffing
- `services/attendance.py` — attendance credit logic
- `services/history.py` — retention/current-membership history policy
- `services/roles.py` — Back Seat FC / Logi Anchor / Snowflake assignments and FC-credit snapshots
- `services/fleet_controls.py` — Fleet Boss operational controls such as capsule cleanup
- `services/statistics.py` — user/corporation statistics
- `services/incentives.py` — monthly FC calculations/finalization
- `services/routing.py` — proximity calculations
- `services/audit.py` — privileged-action audit records

## Automation failure model

Fleet start is not a single all-or-nothing external call.

Expected step behavior:

```text
Fleet Detection   success/failure
Ping Delivery     success/failure
MOTD Update       success/failure
Tracking Start    success/failure
```

A Discord or MOTD failure must not destroy a successfully created FleetOperation. The generated Ping/MOTD remains available for manual copying.

## Tracking model

FleetOps stores the latest state separately from meaningful historical changes.

This avoids writing a complete duplicate member table every minute.

## Optional integrations

FleetOps core is intended to run without AFAT or AA Fleet Pings.

Optional integration boundaries should remain soft so upstream changes in another community app do not break FleetOps startup.
