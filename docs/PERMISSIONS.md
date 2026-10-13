# FleetOps Permission Model (0.1.0a6)

FleetOps permissions are designed to be assigned through normal Alliance Auth groups. Django staff/superuser access is **not** required for normal FleetOps administration.

## Permission definitions

| Permission | Purpose |
|---|---|
| `fleetops.basic_access` | Open FleetOps, personal dashboard/statistics/history, and fleet records the user participated in (also running fleets the user is tracked in or registered for by PAP link). Register characters through a PAP link. |
| `fleetops.start_fleet` | Start an ESI-tracked Fleet Operation, send a ping, generate/set MOTD, and start attendance tracking. Send a pre ping and schedule a fleet, see every scheduled fleet, and start the fleets the user scheduled. Start a PAP-link fleet. Save and use personal start presets. |
| `fleetops.manage_own_fleet` | End/edit/retry/control fleets for which the user is the FC or an FC-credited special-role member, including Regenerate & Push MOTD, kicks, PAP links, excluding the fleet from statistics, settling a pending multiplier choice, and editing or cancelling the user's own scheduled fleets. |
| `fleetops.view_all_fleets` | Read the full fleet archive and open all Fleet Operation records, including scheduled fleets and the member list of running fleets. Does not grant edit/control rights. |
| `fleetops.create_manual_fleet` | Create a manual/historical fleet record without ESI tracking. |
| `fleetops.manage_fleets` | Manage every Fleet Operation, regardless of FC, including editing and cancelling any scheduled fleet. It does not allow starting another FC's scheduled fleet. FC lead rights of the PAP rules: approve or reject manual fleets, manual PAP, pasted rosters, PAP-link fleets and edited fleet times (the user's own entries and edits are approved at once); set a PAP multiplier beyond the duration rule with a written reason; count a fleet in statistics anyway or undo an exclusion; delete approved manual PAP. |
| `fleetops.view_corp_stats` | View the user's own corporation statistics and corporation attendance history. |
| `fleetops.view_all_stats` | View alliance-wide corporation and FC statistics/history. Statistics only: opening Fleet Operation records needs `view_all_fleets` or `manage_fleets`. |
| `fleetops.manage_attendance` | Add manual PAP (by name, ID or pasted roster) and delete the user's own entries until an FC lead approved them, recalculate PAP and change the PAP multiplier of closed fleets within the duration rule, on fleets the user is allowed to manage; with `manage_fleets`, applies alliance-wide and any entry can be deleted. |
| `fleetops.manage_incentives` | Manage monthly FC incentive periods, waivers, recalculation, finalization and unlock. The FC Incentive pages are only available while **Incentive enabled** is ticked in FleetOps Administration → General Settings, which is off by default on new installs. |
| `fleetops.manage_configuration` | Use FleetOps front-end Administration for fleet types, comms presets, Logi/Boost channels, ping channels, ping roles (including **Sync roles from Discord**), PAP multiplier tiers, special roles, templates and settings. |
| `fleetops.view_audit_log` | View FleetOps audit logs. |

## Recommended Alliance Auth role bundles

### Member

- `fleetops.basic_access`

Capabilities:

- View own attendance / fleet participation records.
- View own attendance history.
- Open Fleet Operations in which the member has a granted attendance record or assigned special role, and running fleets the member is tracked in or registered for by PAP link (seeing only their own characters until the fleet closes).
- See "PAP in running fleets" on the dashboard and My Statistics.
- Register their own characters through a PAP link.
- View personal statistics.

### Corporation Management

Member permissions plus:

- `fleetops.view_corp_stats`

Capabilities:

- Everything a Member can do.
- View own corporation monthly statistics.
- View own corporation historical attendance.

### FC

- `fleetops.basic_access`
- `fleetops.start_fleet`
- `fleetops.manage_own_fleet`
- `fleetops.view_all_fleets`
- `fleetops.create_manual_fleet`
- `fleetops.manage_attendance`

Capabilities:

- View own participation/history.
- Read the complete historical Fleet Operations archive.
- Start a Fleet Operation and choose Full Mode or Attendance Tracking Only.
- Send/retry Ping, generate/set/retry MOTD, Regenerate & Push MOTD, create/retry SRP for own fleets.
- Send pre pings, see every scheduled fleet, and start, edit or cancel their own scheduled fleets.
- Track and end own fleets.
- Kick single members, members outside a system and capsule pilots from an active own fleet.
- Create and revoke PAP links on own fleets, and start PAP-link fleets.
- Add/remove special-role assignments on own fleets, including after fleet end.
- Correct attendance after fleet end; corrections wait for FC lead approval. Own corrections can be deleted until an FC lead approved them.
- Edit the times of own fleets; new times wait for FC lead approval, and a multiplier the new times of a closed fleet do not allow is lowered.
- Choose the PAP multiplier when ending a fleet, and change it after the close within the duration rule.
- Add manual PAP (1-100 credits per entry) for characters registered to an Alliance Auth user, by name or from a pasted roster; it counts once an FC lead approves it.
- Create manual/historical fleet records, which count once an FC lead approves them.
- Exclude own fleets from statistics with a reason.
- Save start presets.

`view_all_fleets` is intentionally read-only. It lets an FC review every historical fleet without giving that FC the ability to edit another FC's operation.

### FC Lead

Grant **all FleetOps permissions**:

- `fleetops.basic_access`
- `fleetops.start_fleet`
- `fleetops.manage_own_fleet`
- `fleetops.view_all_fleets`
- `fleetops.create_manual_fleet`
- `fleetops.manage_fleets`
- `fleetops.view_corp_stats`
- `fleetops.view_all_stats`
- `fleetops.manage_attendance`
- `fleetops.manage_incentives`
- `fleetops.manage_configuration`
- `fleetops.view_audit_log`

Capabilities include all fleet operations, all statistics, attendance management, approvals, multiplier overrides, FC incentives (once **Incentive enabled** is ticked in the FleetOps settings), configuration, and audit review.

## Scheduled fleets (pre pings)

| Action | Who |
|---|---|
| Send a pre ping and schedule a fleet | `fleetops.start_fleet` |
| See scheduled fleets (dashboard list, scheduled fleet page, archive) | The fleet's FC, and users with `fleetops.start_fleet`, `fleetops.view_all_fleets` or `fleetops.manage_fleets`. Members without these permissions do not see pre pings in FleetOps, because a pre ping may have gone to a restricted channel. |
| Start a scheduled fleet | Only the FC who scheduled it (with `fleetops.start_fleet`), using one of their own characters that ESI confirms as the fleet boss. Other FCs and fleet managers cannot start it. |
| Edit a scheduled fleet (optionally post the updated pre ping) | The fleet's FC with `fleetops.manage_own_fleet`, or `fleetops.manage_fleets`. |
| Cancel a scheduled fleet (optionally post a notice that mentions nobody) | The fleet's FC with `fleetops.manage_own_fleet`, or `fleetops.manage_fleets`. |

Scheduling, editing, cancelling and starting are audited (`fleet.schedule`, `fleet.schedule_edit`, `fleet.schedule_cancel`, and one `fleet.start` per start with `from_schedule`).

## Running fleets

- **Regenerate & Push MOTD** needs `fleetops.manage_own_fleet` on a fleet the user is credited FC of, or `fleetops.manage_fleets`. It is audited as `fleet.motd_regenerate`.
- While a fleet is starting, active or ending, its member list, 3-jump map, events and the details of its kick steps are shown only to users who manage the fleet and to users with `fleetops.view_all_fleets` or `fleetops.manage_fleets`. Other members who can open the fleet see their own attendance and special roles; everything else becomes visible once the fleet is closed.
- When ESI confirms a new fleet boss at Fleet Start, the record of the previous FC for that ESI fleet ends with "fleet boss handed over to <name>", without needing any permission on that record. Both records keep their own attendance and FC credit; the handover is audited as `fleet.boss_handover` on the old record.

## PAP rules

| Action | Who |
|---|---|
| Approve or reject manual fleets, manual PAP rows, pasted rosters, PAP-link fleets and fleets whose times an FC edited (Approvals page and fleet page) | `fleetops.manage_fleets`. Entries and time edits of users with `manage_fleets` are approved at once; everybody else's wait for approval and do not count until then. |
| Delete a manual or imported PAP row | Its author while it is pending or rejected (with `manage_attendance` on the fleet); any row with `fleetops.manage_fleets`. |
| Choose the PAP multiplier when ending a fleet | Whoever may end the fleet. The server allows only the multipliers the fleet's duration reached. |
| Change the multiplier of a closed fleet within the duration rule | `fleetops.manage_attendance` on the fleet (see the row-level rule below). The fleet's FC (`manage_own_fleet`) can always settle a pending choice. |
| Set a multiplier beyond the duration rule | `fleetops.manage_fleets`, with a written reason of at least 5 characters (audited as `attendance.multiplier` with source "override"). |
| Recalculate PAP of a closed fleet | `fleetops.manage_attendance` on the fleet. |
| Exclude a fleet from statistics (**Do not count**, reason required) | Whoever manages the fleet (`manage_own_fleet` on own fleets, or `manage_fleets`). |
| Count a fleet anyway (**Always count**) or go back to the automatic rule | `fleetops.manage_fleets`. |
| Create or revoke a PAP link | `fleetops.manage_own_fleet` on own and credited fleets, or `fleetops.manage_fleets`; active or closed fleets only, not fleets closed before the upgrade to 0.1.0a6. |
| Register characters through a PAP link | `fleetops.basic_access`, for the user's own characters with the user's own ESI token. |
| Start a PAP-link fleet | `fleetops.start_fleet`. The fleet waits for approval unless the starter has `manage_fleets`. |
| Kick a member, kick outside a system, Kick Capsules | `fleetops.manage_own_fleet` on own and credited fleets, or `fleetops.manage_fleets`. The FC character needs a token with `esi-fleets.write_fleet.v1`; the FC and the fleet boss are never kicked. |
| Save, load, rename and delete start presets | `fleetops.start_fleet`; every user sees only their own presets. Presets are not audited. |
| Configure PAP multiplier tiers and special roles | `fleetops.manage_configuration`. |

A special role's **Gives FC credit** switch alone decides whether its holder gets FC fleet/point credit: the holder needs no FC permission for that. Managing the fleet still needs `fleetops.manage_own_fleet` (or `manage_fleets`). Roles go to characters recorded in the fleet; manual PAP that waits for approval or was rejected does not make a character eligible, so an FC cannot hand out FC credit through unreviewed manual PAP.

Approvals, multiplier changes, statistics inclusion, PAP links, registrations, recalculations and kicks are audited (`approval.*`, `attendance.multiplier`, `fleet.stats_mode`, `pap_link.create`, `pap_link.revoke`, `pap.register`, `pap.recalculate`, `fleet.kick_member`, `fleet.kick_outside_system`, `fleet.kick_capsules`).

## Configuration

- **Sync roles from Discord** on the Ping Roles page needs `fleetops.manage_configuration`, the Alliance Auth Discord service and `DISCORD_GUILD_ID`. Synced roles start inactive; an admin decides which roles FCs may ping. Each sync is audited as `configuration.discord_role_sync`.
- Ping channel webhook URLs are write-only: users with `fleetops.manage_configuration` can replace a URL, but no page shows a stored one. Django Admin users who may only view ping channels see "Stored (hidden)" instead of the URL.
- The comms join page (`/fleetops/comms/<id>/join/`) is the only FleetOps page without a permission check. It needs no login when `"fleetops"` is in `APPS_WITH_PUBLIC_VIEWS`; otherwise it needs an Alliance Auth login with a main character, like every Alliance Auth page.

## Important row-level rule

`manage_attendance` alone does not allow an FC to change another FC's fleet attendance. An FC can change attendance only when the user is the operation FC / FC-credited special-role member. `manage_fleets + manage_attendance` gives alliance-wide attendance management (and approved-at-creation entries).
