# Changelog

## 0.1.0a6 (unreleased)

### Upgrade notes

- Run `python manage.py migrate fleetops` after upgrading; this release adds migrations `0005` and `0006`.
- Keep the django-esi release that matches your Alliance Auth: django-esi 9 for Alliance Auth 5.4 and older, django-esi 10 for Alliance Auth 5.5.
- FC incentive pages and calculations now follow the **Incentive enabled** setting in FleetOps Administration → General Settings. It is off by default on new installs. On existing installs that already have incentive periods, `python manage.py migrate fleetops` turns it on automatically; if you want FC incentives but have not created a period yet, tick it yourself.
- When a Discord ping failed with a connection error, 0.1.0a1–0.1.0a5 could store the webhook token in the fleet's error message, where anyone who could open the fleet could read it. An upgrade step removes these tokens. If a ping failed with a connection error on one of those versions, replace that webhook (delete it in Discord, create a new one and save its URL in FleetOps), because the old token may already have been seen.
- A default message template must now be active. If an upgraded install has an inactive template marked as default, editing it asks you to tick **Active** or untick **Default** before it can be saved.
- Only Discord webhook URLs of the form `https://discord.com/api/webhooks/<id>/<token>` are accepted (the `ptb.`, `canary.` and `discordapp.com` variants are fine too, as is an API version segment such as `/api/v10/webhooks/...`), and the URL is checked again before every send. A webhook saved by an older version that does not match is not used until its URL is fixed in FleetOps Administration → Discord Webhooks.

### Fixed

- Fleet tracking, fleet detection and MOTD updates no longer fail when ESI data is unchanged since the last read (django-esi ETag/304 handling).
- MOTD updates now send the request body in the form django-esi expects; previously every MOTD write was rejected.
- A failed tracking poll now waits for the configured tracking interval before retrying, so the auto-end window is no longer shortened.
- Tracking polls follow the configured interval even when the worker starts a little after the beat. Previously such a poll was put off until the next beat, so a 60-second interval polled every 120 seconds.
- Discord webhook URLs and tokens are no longer stored in step error messages or shown on fleet pages. An upgrade step also removes tokens that older versions stored in fleet error messages (see the upgrade notes).
- Discord webhook URLs are checked again before every send, not only when they are saved, so a URL stored before validation existed is never posted to unless it is a Discord webhook URL.
- A broken Ping/MOTD template no longer blocks fleet start or crashes the preview; the built-in default text is used, the problem is reported on the fleet and the Start Fleet preview shows a template warning.
- Unexpected errors in Discord, MOTD, tracking or SRP steps are recorded as failed steps and no longer leave a fleet stuck in Starting.
- Unexpected step failures, including SRP and background tracking, are logged without exposing secrets and stored as a generic message instead of the raw error text.
- A double-submitted Start Fleet request returns the fleet that was already started.
- Retrying SRP no longer creates duplicate SRP fleets or erases an existing SRP link. Built-in AA SRP fleets get a valid SRP code and link.
- Retry Ping/MOTD/SRP do nothing for Attendance Tracking Only fleets.
- Retry Ping and Retry MOTD re-render empty messages (for example after a failed render at start) and never send an empty ping or clear the in-game MOTD.
- FC incentives only count Closed fleets, follow the Incentive enabled setting (see the upgrade notes), and refuse invalid period actions instead of returning HTTP 500.
- Departed members (when current alliance filtering is configured) are excluded from FC statistics and incentives.
- `view_all_stats` no longer grants access to fleet records; use `view_all_fleets` or `manage_fleets`.
- The 1x/2x/3x end prompt appears as soon as a fleet has run longer than 90 minutes.
- Attendance history and the audit log are paginated instead of silently truncated.
- Manual attendance and special roles can only be added to Active or Closed fleets; manual attendance requires a registered character and a sane value.
- Invalid archive filters, oversized values and out-of-range periods no longer cause HTTP 500.
- Configuration input is validated: Discord webhook URLs, message template syntax, alliance ID lists, retention days (365 to 36500) and fleet type weights. Only one default template per type is kept, a default template must be active, and a default that is replaced by another template is recorded in the audit log.
- `fleetops_seed` no longer overwrites existing templates or fleet types.
- Bulk deletes in Django admin are audited, and history pruning reports accurate totals.
- Added the missing migration for the attendance record ordering.

### Changed

- Supports django-esi 9 and 10, so Alliance Auth 5.5 can be installed alongside FleetOps.
- Added a Django test suite that runs against SQLite and MariaDB.

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

## 0.1.0a4

- Added reusable year/month selectors across monthly statistics and incentive views.
- Added corporation detail drill-down for alliance statistics.
- Added individual FC historical detail pages.
- Expanded My Statistics with fleet-type, character, ship, role and daily activity data.
- Added Fleet Operations archive with server-side filters and pagination.
- Added front-end fleet metadata editing with audit logging and Fleet Type point snapshot correction.
- Added direct Edit Operation access from operation detail.
- No database migration is required from 0.1.0a3 to 0.1.0a4.


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
