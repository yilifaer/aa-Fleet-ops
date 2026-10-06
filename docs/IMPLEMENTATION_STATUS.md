# Implementation Status — 0.1.0a5

This file separates **implemented alpha behavior** from **product target**.

The presence of a feature in `FEATURES.md` does not by itself mean it has been proven in a real Alliance Auth + EVE ESI environment.

## Implemented in the current source tree

- FleetOperation data model and lifecycle fields
- OperationAction step results
- Fleet Type presets and point weights
- Comms presets
- Logi / Boost channel presets
- Ping targets and Discord webhook configuration
- FleetOps front-end Administration Center protected by `fleetops.manage_configuration`
- Front-end CRUD for Fleet Types, Comms, Logi/Boost Channels, Webhooks, Ping Targets and Message Templates
- Front-end General Settings editor; Django Admin remains IT/emergency only
- Ping/MOTD templates
- FC Form Up free-text input
- Main/alt character selection based on Alliance Auth ownership
- ESI fleet provider/detection layer
- Doctrine provider with optional Fittings discovery and Custom fallback
- Ping and MOTD rendering
- Discord webhook ping provider
- MOTD update path
- Fleet member current-state model
- Fleet member change-event model
- Tracking service and Celery task entry points
- Attendance records and per-user attendance cap logic
- Manual attendance management views/services
- Member statistics views
- Corporation statistics views
- Corporation average attendance helper: total granted attendance / main character count
- FC monthly statistics model/view
- Incentive period and payout calculation
- Waiver/finalize/unlock workflow support
- Audit log model/service
- Optional SDE routing/proximity service
- Test seed command
- Basic calculation unit tests
- SRP provider registry and best-effort built-in Alliance Auth SRP adapter
- FleetOperation SRP provider/reference/URL/error snapshot and non-blocking Fleet Start SRP action
- Historical manual-attendance front-end with closed-fleet selection and repeated/manual multi-credit support
- Personal/corporation/alliance 365-day attendance-history pages
- Configurable retention (minimum 365 days) and current-alliance membership pruning service/task/management command
- Dashboard attendance cards, top tracked ships, recent participations and FC-month summary
- Back Seat FC / Logi Anchor / Snowflake role assignments on active or closed operations
- FC-credit snapshot for assigned special-role users who have `fleetops.start_fleet`
- ESI kick-member provider call and active-fleet one-click capsule cleanup action

## Requires local Alliance Auth smoke testing

These are implemented paths but must be validated against the user's local AA test installation:

- clean `pip install`
- app loading through `INSTALLED_APPS`
- Django migrations against the real AA database stack
- Alliance Auth menu hook
- Alliance Auth permissions
- forms/templates/static rendering
- Celery worker task discovery
- Celery Beat scheduling
- front-end configuration CRUD
- app behavior when optional Fittings/SDE packages are absent
- app behavior when optional Fittings/SDE packages are present

## Requires staging + real EVE integration testing

These cannot be honestly considered production-proven until tested with real ESI tokens and fleets:

- selected alt -> ESI fleet detection
- fleet boss/commander validation
- MOTD write behavior
- ESI member refresh/cache behavior
- JOIN / LEAVE / REJOIN detection
- ship/system/role/wing/squad changes
- auto-end behavior when a real ESI fleet disappears
- Redis/Celery interruption and recovery
- Discord webhook delivery
- concurrent active fleets
- larger fleet performance
- exact Alliance Auth 5.2 built-in SRP model/URL behavior and successful SRP link creation
- ESI `DELETE /fleets/{fleet_id}/members/{member_id}/` behavior for capsule cleanup
- special-role FC credit recalculation after post-fleet assignment
- membership-pruning behavior against the staging alliance's real UserProfile data

## Target / future hardening

Depending on test results and community feedback:

- richer API endpoints
- stronger provider interfaces
- migration/import from AFAT / AA Fleet Pings
- richer system-name autocomplete
- additional statistics charts
- import/export utilities
- load testing for large alliances
- upgrade compatibility tests between FleetOps releases
- localization/i18n
- documentation screenshots

## Release rule

`0.1.0a5` is an **alpha test build**.

It should not be advertised as production-ready until it has passed:

1. local AA smoke test
2. staging AA test
3. real EVE ESI fleet test
4. Celery/Redis recovery test
5. statistics/incentive verification
6. upgrade/reinstall test


## Added in a4

- Monthly historical selection: implemented in front-end statistics pages.
- Corporation and FC drill-down: implemented.
- Fleet Operations archive/filter/edit workflow: implemented.
- Rich personal statistics: implemented using retained AttendanceRecord, FleetMemberState and OperationRoleAssignment data.
- No schema migration is required for a3 -> a4.

## 0.1.0a5

Implemented in code:

- a4 non-ESI bug report fixes.
- Attendance Tracking Only operation mode.
- Fleet-wide 1x/2x/3x automatic attendance multiplier.
- >90-minute Fleet End attendance prompt.
- Manual post-fleet multiplier correction.
- Manual Fleet Operation creation.
- Member own-fleet archive visibility.
- Read-only `view_all_fleets` and `create_manual_fleet` permissions.

Still requires real Alliance Auth integration testing:

- Permission bundles against real AA Groups.
- Migration `0004_attendance_modes_permissions` on MariaDB.
- Full template rendering under Alliance Auth 5.2.
- Attendance-only mode with real ESI Fleet Boss token.
- >90-minute end flow with live tracked members.
- All previously unverified ESI write operations.
