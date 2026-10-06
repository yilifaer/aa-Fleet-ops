# AA FleetOps — Product Feature Target

AA FleetOps is intended to be a complete fleet-lifecycle application for Alliance Auth rather than a single FAT or ping utility.

The product goal is:

> **An FC should be able to prepare, ping, start, track and close a fleet from one workflow, while FleetOps automatically preserves attendance, statistics and monthly FC data.**

This document records the expected product behavior so that contributors and testers can distinguish the long-term target from the current alpha implementation.

## 1. One-click Fleet Start

The FC workflow should be:

1. Select an Alliance Auth-owned FC character / alt.
2. FleetOps detects the character's current ESI fleet automatically.
3. Select Fleet Type.
4. Select Doctrine from the installed Fittings / doctrine provider, or use Custom.
5. Enter **Form Up / Staging manually for this fleet**.
6. Select preconfigured Comms, Logi Channel, Boost Channel and Ping Target.
7. Preview Ping and MOTD.
8. Click **Send Ping & Start Fleet**.
9. FleetOps sends the configured ping, sets MOTD when possible, links the ESI Fleet ID and starts tracking automatically.

The FC must not need to open a second tracking page and select the character/fleet again.

## 2. Character and Fleet Boss Selection

- FCs can choose any main or alt character owned by their Alliance Auth account.
- FleetOps should show whether required fleet read/write ESI scopes are available.
- Selecting a character triggers automatic fleet detection.
- The operation should attach directly to the detected fleet for that selected character.
- Clear errors are required for: no token, invalid token, missing scope, not in fleet, insufficient fleet role, ESI unavailable.

## 3. Form Up / Staging

**Form Up is not an admin preset.**

It is entered by the FC for each fleet because it is operational information that changes frequently.

Examples:

- `R-ARKN`
- `DIBH-Q Keepstar`
- `X-7 Gate @ 0`
- `Titan @ <name>`

A future system-name autocomplete is welcome, but free text must remain possible.

## 4. Admin-configured Fleet Choices

Alliance administrators configure reusable choices from a **FleetOps front-end Administration Center**; they must not require Django Admin/superuser access. Django Admin is an IT/emergency interface only. FCs then select these choices during Fleet Start:

- Fleet Types
- Fleet Type point weights
- Comms + voice links
- Logi Channels
- Boost Channels
- Ping Targets
- Discord webhooks
- Ping templates
- MOTD templates
- Attendance credit limit
- FC incentive settings

## 5. Doctrine

FleetOps should not maintain an unnecessary duplicate doctrine database.

Preferred behavior:

- Read doctrines from Alliance Auth Fittings / a doctrine provider when available.
- Offer `None / Custom` fallback.
- Snapshot the selected doctrine name/source onto the FleetOperation so historical fleets remain readable if the external doctrine later changes or disappears.

## 6. Manual Copy Fallback

Automation must never remove the FC's fallback path.

Fleet Start and the Operation page should always preserve:

- **Copy Ping**
- **Copy MOTD**

If Discord or ESI fails, the fleet should still be startable and the generated text should remain manually usable.

## 7. Live Fleet Tracking

FleetOps should not only record the state seen when a member first joins.

While a fleet is active it should update, within ESI cache/rate-limit constraints:

- Character
- Main character / Auth user mapping
- Corporation
- Alliance
- Ship type
- Solar system
- Fleet role
- Wing
- Squad
- First seen
- Last seen
- Leave/rejoin state

The UI should display the last successful ESI update and clearly mark stale data.

## 8. Current State + Event History

Do not write a full duplicate fleet snapshot every minute.

Maintain:

### Current state

The latest known state for each fleet member.

### Event history

Only meaningful changes, including:

- JOIN
- LEAVE
- REJOIN
- SHIP_CHANGE
- SYSTEM_CHANGE
- ROLE_CHANGE
- WING_CHANGE
- SQUAD_CHANGE

## 9. Fleet Lifecycle

Every fleet is one `FleetOperation` linking:

- FC / selected character
- ESI Fleet ID
- Fleet Type
- Doctrine
- Form Up
- Comms / channels
- Ping
- MOTD
- Tracking
- Attendance
- Statistics
- FC incentive data

The normal lifecycle is:

`Draft -> Starting -> Active -> Ending -> Closed`

Individual automation steps can fail independently. A failed MOTD update must not invalidate a successful ping/tracking start.

## 10. Fleet End

FCs should be able to end their own fleets.

Ending should:

- perform a final ESI update when possible
- stop live tracking
- save end time
- finalize attendance
- prepare fleet statistics

Automatic close should only happen after repeated confirmed evidence that the ESI fleet no longer exists, not on a transient ESI/network error.

## 11. Attendance

Operational member tracking and attendance credits are separate concepts.

A single Auth user may have several characters in one fleet. All characters remain visible operationally, while the administrator can configure a maximum attendance credit per person per fleet:

- 1
- 2
- 3
- ...
- Unlimited

Manual attendance add/edit/delete is required for authorized users and must be audited.

## 12. Member Statistics

Members should be able to see their own monthly statistics, including:

- Total attendance
- Unique fleets
- Fleet Type breakdown
- Characters used
- Common ships
- Monthly history

## 13. Corporation Statistics

Corporation managers can view their own corporation; alliance management can view all corporations.

The required corporation-average formula is intentionally simple:

> **Corporation Average Attendance = Corporation Total Granted Attendance / Main Character Count**

Example:

- Main Characters: 50
- Monthly Granted Attendance: 300
- Average Attendance: 6.0

The denominator is the number of main characters / Auth users, **not total linked characters**.

The same approach may be shown by Fleet Type:

- CTA Attendance / Main Character Count
- StratOps Attendance / Main Character Count
- PCT Attendance / Main Character Count

No complex participation-population model is required.

## 14. Fleet Type Statistics

Fleet Types are admin configurable and may include examples such as:

- PCT / Peacetime
- StratOps
- CTA
- Home Defense
- Roaming
- Training
- Industry
- Custom types

Statistics should support monthly Fleet Type breakdowns.

## 15. FC Monthly Statistics

Each month FleetOps should summarize per FC:

- Fleet count
- Fleet Type count
- Fleet points
- Eligibility
- Waiver status
- Calculated payout
- Final payout

FCs can see their own data; authorized alliance management can see all FCs.

## 16. FC Incentive

The incentive model should stay simple and open-source friendly.

Admin-configurable parameters:

- Enable/disable incentive
- Minimum fleets for eligibility
- Fleet Type point weights
- Monthly budget
- Waiver support

Example IGC-style defaults may be configured, but must not be hard-coded:

- PCT = 0.5
- StratOps = 1.0
- CTA = 1.5
- Minimum fleets = 3

Once an FC reaches the minimum number of fleets, **all fleets in that month count**, including the fleets used to reach the threshold.

Payout formula:

`Monthly Budget × FC Eligible Points / Total Eligible Non-Waived Points`

Payouts are whole ISK. Remainder handling must be deterministic.

## 17. Monthly Review / Finalization

Admin workflow:

`Open -> Review -> Finalized`

A finalized month keeps a stable historical result. Reopening/recalculating requires an explicit privileged action and an audit entry.

## 18. Fleet Proximity Map

An active fleet should have a lightweight proximity view centered on the FC's current system:

- Current system
- 1 jump
- 2 jumps
- 3 jumps

Show current fleet members grouped by system, with drill-down for pilot, ship, corporation and role.

This is not intended to replace Dotlan; it is a fleet-member operational view.

## 19. Permissions

Expected permission groups include:

- basic access / own statistics
- start fleet
- manage own fleet
- manage all fleets
- view own corporation statistics
- view alliance-wide statistics
- manage attendance
- manage FC incentives
- manage configuration
- view audit log

All permissions must be enforced server-side.

## 20. SRP Integration

When a FleetOperation starts, FleetOps should be able to create or link the corresponding fleet in the installed Alliance Auth SRP app and expose an **Open SRP** link from the operation page. SRP automation is non-blocking.

SRP integration must use a provider boundary rather than hard-coding one app so a future **Better SRP** app can register its own provider without changing FleetOps core.

## 21. Historical Manual Attendance

Authorized FCs may receive `fleetops.manage_attendance` without full alliance-admin access. They can add manual attendance to their own credited historical fleets as well as active fleets.

Manual attendance may be entered repeatedly for the same character/fleet or with an `attendance_value` greater than 1 when multiple credits are intended. Each privileged change is audited.

## 22. Attendance History and Retention

FleetOps should retain at least **365 days** of granted attendance and expose personal, corporation and alliance history views.

When alliance-membership filtering is configured, users who are no longer current members must disappear from attendance history/statistics and their mapped attendance rows should be pruned. Retention longer than one year remains admin configurable.

## 23. Operational Dashboard

The FleetOps landing page should provide a compact operational dashboard inspired by common fleet dashboards without copying a specific app UI. Useful widgets include:

- current-month attendance by Fleet Type
- last 30 / last 90 day totals
- recent participations
- top tracked ships
- active fleets
- current FC-month fleet/point summary

## 24. Special Fleet Roles / FC Credit

After a FleetOperation is created — including after it is closed — the operation FC or an authorized fleet manager may assign tracked members as:

- Back Seat FC
- Logi Anchor
- Snowflake Member

If the assigned member is an authorized FleetOps FC at assignment time, the operation also grants that user an FC fleet/point credit. This is snapshotted so historical FC calculations remain stable.

## 25. One-click Capsule Cleanup

While a fleet is active, an authorized FC/fleet manager may execute **Kick all Capsules**. FleetOps detects currently tracked capsule/pod members and removes them through the Fleet Boss ESI write token. The Fleet Boss character must never be targeted. Per-member failures must be reported without hiding successful kicks.

## 26. Open-source Design

FleetOps should remain useful outside one alliance.

Core workflow should be fixed and understandable, while alliance-specific reusable data/policy remains configurable.

Preferred optional integrations:

- Alliance Auth Fittings / doctrine providers
- Discord webhook / future ping providers
- EVE SDE routing data
- future AFAT / Fleet Pings import or compatibility helpers

AFAT and AA Fleet Pings should not be hard runtime dependencies of FleetOps core.


## Historical statistics navigation

All monthly summary pages must support explicit year/month selection. This includes personal, corporation, alliance corporation, FC and incentive views. Historical months are read-only summaries unless the user also holds the relevant management permission.

## Fleet Operations archive

FCs can browse fleets credited to them. Fleet managers can browse all fleets. The archive provides filters for year, month, Fleet Type, status, FC, doctrine and free-text metadata, with direct links to open or edit an operation.

## Rich personal statistics

Personal statistics should present selected-period attendance, unique fleets, FC credits/points, Fleet Type breakdown, characters used, tracked ships, special roles and daily activity. The layout may take inspiration from existing fleet dashboards but must remain FleetOps-native.

## 0.1.0a5 additions

- Attendance Tracking Only start mode.
- >90-minute End Fleet 1x/2x/3x attendance prompt.
- Manual fleet-wide attendance multiplier correction after fleet end.
- Manual/historical Fleet Operation creation.
- Member access to own participated fleet records.
- Read-only all-fleet archive permission for FCs.
- Recommended Member / Corp Management / FC / FC Lead permission bundles.
