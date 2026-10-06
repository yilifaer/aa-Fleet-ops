# New in AA FleetOps 0.1.0a5

## Bug fixes from the 0.1.0a4 test report

- Invalid archive years are range-checked before Django `__year` filtering.
- Non-numeric `fleet_type` archive parameters no longer raise HTTP 500.
- Closed fleets retain the Special Role assignment form by using all tracked members as assignable members.
- Fleet manager permission failures now use Alliance Auth `permissions_required(..., raise_exception=True)` instead of redirecting authenticated users to login.
- Unknown corporation drill-down IDs return 404 instead of an empty 200 page.
- Release `SHA256SUMS.txt` now uses the correct `dist/` prefix for the wheel.

## Attendance finalization

When a tracked fleet has run for more than 90 minutes, the Fleet Detail page asks the FC to choose:

- 1x attendance
- 2x attendance
- 3x attendance

before closing the fleet.

Automatic granted attendance rows are updated to the selected fleet multiplier. Manual attendance rows remain explicit corrections and are not silently multiplied.

The Attendance tab also contains a persistent manual multiplier control, so an authorized FC can correct a fleet to 1x/2x/3x before or after Fleet End.

## Attendance Tracking Only mode

Fleet Start now offers two modes:

1. Full: Ping + MOTD/SRP + ESI attendance tracking.
2. Attendance Tracking Only: detect Fleet Boss and track attendance, but deliberately skip Discord Ping, automatic MOTD write and automatic SRP creation.

Ping/MOTD previews remain available for manual copy.

## Manual fleet records

FCs with `fleetops.create_manual_fleet` can add historical/manual fleet records without ESI. The record can then receive manual attendance and is included in the normal fleet archive/FC statistics.

## Expanded fleet archive access

`fleetops.basic_access` users can open the Fleet archive, but only see operations in which they participated / were FC / held a special role.

`fleetops.view_all_fleets` adds read-only access to the complete fleet archive. This is intended for the FC role.

See `docs/PERMISSIONS.md` for the recommended Member / Corp Management / FC / FC Lead bundles.
