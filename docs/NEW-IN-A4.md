# New in AA FleetOps 0.1.0a4

## Historical month browsing

Corporation, alliance corporation, FC, personal and incentive pages now expose explicit Year / Month selectors. Alliance-level tables link to detailed corporation and FC pages while preserving the selected period.

## Personal statistics dashboard

The personal statistics page now includes attendance, unique fleets, FC fleet/point totals, Fleet Type breakdown, characters used, tracked ships, special-role counts and daily activity.

## Fleet Operations archive

A new `Fleets` navigation entry is available to users with `manage_own_fleet` or `manage_fleets`. FCs see fleets credited to them; fleet managers see all fleets. Filters include year, month, type, status, FC, doctrine and text search.

## Front-end editing

Authorized FCs/managers can edit Fleet Type, doctrine label, Form Up, comms/channel choices, notes and start/end timestamps. Changes are stored in AuditLog. Correcting Fleet Type also refreshes the operation's FC point-weight snapshot to the selected Fleet Type's current weight.

## Upgrade

No new migration is required from 0.1.0a3. Run the normal `migrate` and `collectstatic` commands after upgrading the wheel.
