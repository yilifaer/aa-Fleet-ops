# Changelog

## 0.1.0a5

- Fixed all six reproducible non-ESI issues reported against 0.1.0a4.
- Added >90-minute Fleet End attendance prompt with 1x/2x/3x finalization.
- Added persistent manual fleet attendance multiplier control.
- Added Attendance Tracking Only start mode (no Discord Ping / automatic MOTD / automatic SRP).
- Added manual/historical fleet creation.
- Members can now browse/open their own participated Fleet Operations.
- Added read-only `view_all_fleets` permission for FC historical archive access.
- Added `create_manual_fleet` permission.
- Added `docs/PERMISSIONS.md` with Member / Corp Management / FC / FC Lead role bundles.
- Added migration `0004_attendance_modes_permissions`.

## 0.1.0a5

- Added reusable year/month selectors across monthly statistics and incentive views.
- Added corporation detail drill-down for alliance statistics.
- Added individual FC historical detail pages.
- Expanded My Statistics with fleet-type, character, ship, role and daily activity data.
- Added Fleet Operations archive with server-side filters and pagination.
- Added front-end fleet metadata editing with audit logging and Fleet Type point snapshot correction.
- Added direct Edit Operation access from operation detail.
- No database migration is required from 0.1.0a3 to 0.1.0a5.


## 0.1.0a3

- Added SRP provider abstraction with a best-effort adapter for Alliance Auth built-in SRP and a registration path for future Better SRP/community providers.
- Fleet Start now attempts SRP creation/linking as a non-blocking operation step and stores SRP reference/link/error snapshots.
- Added front-end historical manual attendance for permitted FCs/admins, including closed fleets and repeated/multiple credits.
- Removed the old one-row-per-character/source attendance uniqueness restriction so deliberate separate manual corrections can coexist.
- Added personal, corporation and alliance attendance-history pages with minimum 365-day retention.
- Added optional current-alliance membership filtering/pruning, a Celery prune task and `fleetops_prune_history` management command.
- Reworked the main dashboard with attendance metric cards, recent participations, top ships, active fleets and FC-month summary.
- Added post-creation/post-close Back Seat FC, Logi Anchor and Snowflake role assignments.
- Added FC-credit snapshots: assigned users with `fleetops.start_fleet` receive the same operation in FC statistics/incentive calculations.
- Added one-click active-fleet capsule cleanup through the ESI fleet-member delete endpoint.
- Added SRP, special-role, retention and capsule-cleanup staging test documentation.

## 0.1.0a2

- Added FleetOps front-end **Administration Center** for alliance administrators.
- Added front-end CRUD for Fleet Types, Comms Presets, Logi/Boost Channels, Discord Webhooks, Ping Targets and Message Templates.
- Added front-end General Settings management.
- All configuration pages are protected by `fleetops.manage_configuration`; Django Admin is now documented as an IT/emergency interface rather than the normal configuration workflow.
- Configuration changes are written to the FleetOps Audit Log.
- Discord webhook URL values are redacted from front-end configuration audit snapshots.
- Kept operational/system-generated rows (FleetMemberState, FleetMemberEvent, OperationAction, AuditLog) out of raw front-end CRUD to protect data integrity.


## 0.1.0a1 - 2026-08-23

Initial testable alpha:
- FleetOperation lifecycle
- Character/alt selection from Alliance Auth
- ESI fleet detection and live member tracking
- Discord webhook ping and MOTD rendering/update
- Current-state + change-event tracking
- Automatic attendance with per-user cap
- Personal/corporation/FC statistics
- FC monthly incentive review and payout calculation
- Admin-configurable Fleet Types, comms, channels, targets and templates
- Manual Ping/MOTD copy fallback

### Full development package documentation
- Added complete product feature target (`FEATURES.md`)
- Added implementation-status matrix
- Added architecture notes
- Added local macOS smoke-test gate
- Added real-EVE staging test plan
- Added GitHub release notes and contribution templates
- Fixed standalone calculation test invocation documentation
