# Changelog

## 0.1.0a6 (unreleased)

### Upgrade notes

- Run `python manage.py migrate fleetops` after upgrading; this release adds migrations `0005` to `0010`.
- Migration `0007` adds the end reason and end request fields to fleets and a history table for operation steps. It copies the latest result of every existing step into that history, labels closed fleets as ended manually or automatically from their recorded end step, and marks pilots that older versions left "in fleet" on closed fleets as having left when the fleet ended. Step messages and fleet errors that older versions stored as django-esi's full error text (with every response header) are shortened to the new form, for example `Could not update fleet MOTD: ESI returned HTTP 403 (Forbidden).`; audit log entries are left as they were. Its data steps are not undone when migrating back.
- End Fleet now finishes in a Celery task, and the `fleetops.tasks.schedule_active_fleet_tracking` beat also completes ends whose task was lost and cancels starts that were interrupted. Keep the Celery worker and beat running; without a reachable broker the end is finished in the web request as before.
- FleetOps now logs through Alliance Auth's extension logger, so its messages appear in `extensions.log` instead of only on stderr.
- Keep the django-esi release that matches your Alliance Auth: django-esi 9 for Alliance Auth 5.4 and older, django-esi 10 for Alliance Auth 5.5.
- FC incentive pages and calculations now follow the **Incentive enabled** setting in FleetOps Administration → General Settings. It is off by default on new installs. On existing installs that already have incentive periods, `python manage.py migrate fleetops` turns it on automatically; if you want FC incentives but have not created a period yet, tick it yourself.
- When a Discord ping failed with a connection error, 0.1.0a1–0.1.0a5 could store the webhook token in the fleet's error message, where anyone who could open the fleet could read it. An upgrade step removes these tokens. If a ping failed with a connection error on one of those versions, replace that webhook (delete it in Discord, create a new one and save its URL in FleetOps), because the old token may already have been seen.
- A default message template must now be active. If an upgraded install has an inactive template marked as default, editing it asks you to tick **Active** or untick **Default** before it can be saved.
- Only Discord webhook URLs of the form `https://discord.com/api/webhooks/<id>/<token>` are accepted (the `ptb.`, `canary.` and `discordapp.com` variants are fine too, as is an API version segment such as `/api/v10/webhooks/...`), and the URL is checked again before every send. A webhook saved by an older version that does not match is not used until its URL is fixed in FleetOps Administration → Ping Channels.
- Ping Targets are replaced by **Ping channels** (FleetOps Administration → Ping Channels, the former Discord Webhooks) and **Ping roles**. Migration `0009` converts every Ping Target: its webhook becomes a ping channel that allows the mention the target used (`@everyone` or `@here`), each `<@&id>` role mention becomes a ping role, and older fleets keep the channel and mention they were sent with. Any other text in a target's value is dropped (the ping texts of older fleets are already stored). Check each channel's **Allow @everyone**, **Allow @here** and allowed roles after the upgrade, and give converted roles a proper name. Ping Targets stay read-only in Django Admin.
- The **Default Ping** and **Default MOTD** templates are replaced by the new built-in texts only if they were never edited. Edited templates are left alone (apart from the Logi/Boost channel variables in MOTD templates, see below) and keep working: `{{ ping_target }}` now renders the mention selected on Fleet Start, `{{ fleet_time }}` is `YYYY-MM-DD HH:MM EVE` (or `Now`) instead of an ISO time, and `{{ comms_link }}` is the comms join page for voice client links (the raw link is `{{ comms_voice_url }}`). See "Message templates" in the README.
- Logi/Boost channel presets whose value is a pasted in-game link (`<a href="joinChannel:player_…">Name</a>`) are split into the channel name and the in-game channel ID, so the MOTD shows a clickable channel link. From this release `{{ logi_channel }}` and `{{ boost_channel }}` are the plain channel names and `{{ logi_channel_link }}` and `{{ boost_channel_link }}` the in-game links; the upgrade switches every MOTD template, also edited and custom ones, from `{{ logi_channel }}` and `{{ boost_channel }}` to the link variables. Ping templates keep the plain names.
- Personal user names and passwords are removed from comms voice links (`mumble://user:pass@host/…` becomes `mumble://host/…`), because the link is shown on a public join page. Spaces in voice links are written as `%20` (`mumble://host/Fleet Ops` becomes `mumble://host/Fleet%20Ops`); voice links can no longer contain spaces or a user name.
- Comms presets whose voice link does not start with `mumble://`, `ts3server://`, `teamspeak://`, `discord://`, `https://` or `http://` (for example a bare `ts.example.com:9987`) are no longer shown in pings and MOTDs and have no join link. The Comms Presets list marks them "Not a supported voice link"; change them to `ts3server://…` or `mumble://…`.
- New site settings to check in `local.py`: add `"fleetops"` to `APPS_WITH_PUBLIC_VIEWS` so pilots who are not logged in can open comms join links, and make sure `SITE_URL` is the public https address of Alliance Auth (links in pings and MOTDs are built from it). The FleetOps Administration index shows a warning while either is missing.
- Discord only notifies a role when the role is mentionable, or when the webhook may mention every role. Members need `fittings.access_fittings` to open the doctrine link in a ping or MOTD.
- When aa-srp (`aasrp`) is installed, the `auto` SRP provider now creates aa-srp links at fleet start (when **SRP auto create** is on). Choose **Alliance Auth SRP** in General Settings to keep the old behaviour.
- Ship and system names are now always stored in English. To fix fleets that were tracked while a user had another language active, run `python manage.py fleetops_refresh_names --dry-run` and then `python manage.py fleetops_refresh_names`.
- `fleetops_seed --demo` no longer creates the `Manual / Copy Only` Ping Target; leave the ping channel empty on Fleet Start to copy the ping instead.

### Added

- **Pre ping.** Fleet Start has a **Fleet time** choice: **Ping now** starts the fleet as before, **Pre ping** takes an EVE date and time (UTC), posts a pre ping with a Discord timestamp that shows each reader's local time, and creates a **Scheduled** fleet without any ESI call. The scheduled fleet page shows a countdown (and an "Overdue" badge two hours after the fleet time), the pre ping step, and cards to start, edit or cancel the fleet. Only the FC who scheduled a fleet can start it; starting it checks the fleet boss on ESI, optionally posts a form-up ping, sets the MOTD and starts tracking from that moment. Editing can post an updated pre ping (once per edit page, so a double click does not ping twice; a new ping channel needs it, so the channel of the pre ping is also the channel of a cancellation notice), and an overdue fleet can be edited without moving its time; cancelling can post a notice in the same channel that mentions nobody. The dashboard lists scheduled fleets, and the fleet archive files scheduled and cancelled pre pings under their fleet time.
- **Ping channels and mentions.** Fleet Start picks a ping channel (or "No Discord message (copy only)") and a ping target: No ping, `@everyone`, `@here`, or one or more ping roles. Each channel decides whether `@everyone`, `@here` and which roles may be used; the start page disables what a channel does not allow and the server checks it again before every post. Discord is told exactly which mention may notify (`allowed_mentions`), so `@everyone`, `@here` or a role mention typed into the additional information never pings anybody; a stray `@everyone` in an `@here` ping stored by an older version is broken when it is retried.
- **Ping roles.** Roles can be added by ID (digits or `<@&id>`) or read with **Sync roles from Discord** on the Ping Roles page when the Alliance Auth Discord service is installed and `DISCORD_GUILD_ID` is set. Synced roles start inactive; roles that disappear from Discord are marked "Not on Discord" and switched off. A role that a ping channel allows cannot be deleted, also not in Django Admin.
- **New default Ping and MOTD.** The Ping is a broadcast with the mention, the additional information, FC, form-up, PAP type, comms and doctrine links, the fleet time on pre pings and a small footer line naming the sender, the target and the EVE time. It has no Logi/Boost lines. The MOTD uses the normal in-game text colour, keeps the line breaks of the additional information, links the comms name to the join page, the doctrine to Fittings and the Logi/Boost channels to the in-game channels. Empty lines are no longer printed.
- New template variables: `sender`, `doctrine_name`, `doctrine_url`, `doctrine_ships`, `doctrine_fits`, `comms_voice_url`, `logi_channel_link`, `boost_channel_link`, `fleet_time_eve`, `fleet_time_unix`, `fleet_time_discord`, `is_pre_ping`, `ping_mention`, `ping_target_label`, `ping_target_name`, `ping_channel` and `sent_at`. `doctrine_ships` lists the hull of every fit of a Fittings doctrine in doctrine order (for example `Moa > Moa > Osprey`); the fits are saved with the fleet when it starts or is pre pinged.
- **Comms join page.** Every comms preset has a public join page (`/fleetops/comms/<id>/join/`) that opens the voice client, so `mumble://` and other voice links can be clicked in Discord and in game. Pings and MOTDs link to it; the Comms Presets list shows each link with a Copy button. **Issue a new join link** on the preset form makes the old link stop working.
- **In-game channel links.** The Logi/Boost channel form takes the link copied in game (the HTML, a `<url=…>` link, `joinChannel:player_…` or the bare ID) and fills in the channel name.
- **Regenerate & Push MOTD** on an active fleet renders the MOTD again from the current templates, comms, channels and doctrine and pushes it to the fleet. Each push is kept in the MOTD step history and audited as `fleet.motd_regenerate`.
- **aa-srp provider.** With aa-srp installed, FleetOps creates an aa-srp link at fleet start (see `docs/SRP_PROVIDER.md`). General Settings offers the SRP provider as a list that says which providers are installed.
- **Pilots without an Alliance Auth account.** FleetOps looks up their name, corporation and alliance on ESI in the background (Celery task `fleetops.tasks.resolve_character_names`, no new beat entry; pilots who left before they were looked up are named by a later poll or when the fleet ends) and shows them with a "Not in Alliance Auth" badge and EveWho and zKillboard links.
- New management command `fleetops_refresh_names [--dry-run]` that rewrites stored ship and system names of fleet members in English.

### Fixed

- The fleet boss is now taken from ESI's `fleet_boss_id` instead of the fleet position. A boss sitting as squad member or wing commander can start a fleet, and a character in the fleet commander position that is not the boss is rejected with the name of the current boss.
- Handing the boss role to another pilot during a tracked fleet is no longer reported as "Fleet no longer exists." When a fleet read gets a 404, FleetOps checks the FC's own fleet and records either a boss handover or a fleet that is gone (disbanded, or the FC left it). Both count towards the automatic end, a successful read resets the count (so handing the boss back in time keeps the fleet open), and the fleet page shows which case it is and how many checks are left. A failed check is not counted.
- Before ending a fleet automatically, FleetOps reads it once more and keeps it open if that read works. The automatic end itself no longer runs another sync.
- A fleet that ends keeps a consistent state: the missing-fleet counter is saved, the last error is the end reason for automatic ends and empty for manual ends, and every pilot still marked as in fleet is marked as having left at the end time. Closed fleets no longer show the active-fleet warnings. A tracking poll that was still reading ESI when the fleet ended no longer puts pilots back in fleet, adds attendance or stores an error on the closed fleet.
- Starting a fleet whose ESI fleet already has a Starting, Active or Ending record opens that record instead of creating a second one, also from a second browser tab or with a new request id. A start that was interrupted (for example a web worker killed mid-start) holds its fleet for about five minutes at most: after that the next start or the tracking beat cancels the stuck Starting record (audited as `fleet.start_abandoned`) and the fleet can be started again. A user who may not open the running record, such as a new boss after a handover, stays on the start page with an explanation, and the handover hint says when automatic end is disabled.
- ESI errors no longer show django-esi's exception text, which includes every response header. Steps and pages show a short line such as `Could not read fleet members: ESI returned HTTP 503 (Service unavailable).`, and the log gets the HTTP status and `x-esi-request-id`, also for the 420 error limit and for gateway error pages that are not JSON.
- The Kick Capsules button counts exactly the pilots that would be kicked: the FC and the fleet boss are never counted or kicked, and the button is disabled when nobody would be kicked. Unexpected kick errors are stored without their text.
- The Start Fleet preview answers an incomplete form with HTTP 200, lists the missing fields and marks them, instead of returning HTTP 400; preview requests wait 400 ms after the last change.
- End Fleet no longer keeps the browser waiting for the final ESI sync (the request could time out while the fleet still ended). The button disables itself and shows "Ending…", the request only records the end, and a background task runs the final sync, applies the attendance multiplier and closes the fleet. Repeated clicks do nothing and every end request is audited. If the worker is killed during the end task, the tracking beat finishes the fleet after about five minutes.
- Manual fleet times are labelled and read as EVE time (UTC), whatever the site or browser time zone. The end time is required and must be after the start, the page shows the current EVE time and the duration, and a fleet longer than 6 hours needs an extra confirmation.
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
- Every operation step keeps its attempts (status, short message, time). The fleet page lists earlier attempts under each step, so a failure stays visible after a retry succeeds.
- Fleets record why they ended (manual, fleet gone, boss handover). The fleet page, the fleet archive and the audit entry show it, and the end request is audited separately (`fleet.end_request`).
- FleetOps logs through `allianceauth.services.hooks.get_extension_logger`.
- A closed fleet lists every pilot that was tracked in it on its Members tab.
- The fleet detection endpoint returns `fleet_boss_id` and a message naming the boss when the selected character is not the boss.
- The test settings now really run Celery tasks eagerly.
- When ESI confirms a new fleet boss at Fleet Start and another FC's record still tracks that ESI fleet, the old record ends at once with "fleet boss handed over to <name>" and the new boss gets a separate record, instead of having to wait for the automatic end. Each record keeps its own attendance and FC credit. Both are audited (`fleet.boss_handover`, and one `fleet.start` per start).
- The boss check at Fleet Start always reads ESI fresh instead of using the cached fleet data.
- Discord pings are posted with embeds suppressed, so the join and doctrine links do not unfurl into preview cards. A ping over Discord's 2000-character limit is refused before anything is created or posted; the Start Fleet preview counts the characters, and a ping that is only copied gets a warning. A MOTD over 1000 characters gets a warning.
- All EVE times in pings, MOTDs and FleetOps forms are UTC, whatever the site or browser time zone.
- Ship and system names are stored in English whatever language the FC uses.
- The fleet edit form takes its times in EVE time (UTC) like the manual fleet form; the end must be after the start, and new times that make a fleet longer than 6 hours need a confirmation. Changing the doctrine name drops the Fittings link and fits, and the form can change the Ping and MOTD templates for the next retry.
- While a fleet is starting, active or ending, its members, map and events are only shown to people who manage the fleet or hold `view_all_fleets` or `manage_fleets`; other members see their own attendance and roles until the fleet closes.
- The `auto` SRP provider tries aa-srp first, then Alliance Auth SRP, then any other registered provider. The stored key `aa_srp` is read as `aasrp`, and an explicit provider that is not installed is reported as "SRP provider '<key>' is not installed."
- The audit entry of a manual end records the status the fleet had before the end was requested.
- The manual fleet page hides the long-fleet confirmation again when the duration drops below 6 hours.

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
