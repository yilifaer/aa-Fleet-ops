# FleetOps Permission Model (0.1.0a6)

FleetOps permissions are designed to be assigned through normal Alliance Auth groups. Django staff/superuser access is **not** required for normal FleetOps administration.

## Permission definitions

| Permission | Purpose |
|---|---|
| `fleetops.basic_access` | Open FleetOps, personal dashboard/statistics/history, and fleet records the user participated in (also running fleets the user is tracked in or registered for by PAP link). Register characters through a PAP link. |
| `fleetops.start_fleet` | Start an ESI-tracked Fleet Operation, send a ping, generate/set MOTD, and start attendance tracking. Send a pre ping and schedule a fleet, see every scheduled fleet, and start the fleets the user scheduled. Start a PAP-link fleet. Save and use personal start presets. |
| `fleetops.manage_own_fleet` | End/edit/retry/control fleets for which the user is the FC or an FC-credited special-role member, including Regenerate & Push MOTD, kicks, fleet management on the Fleet tab (wings, squads, member moves, invites, structure templates), PAP links, excluding the fleet from statistics, settling a pending multiplier choice, and editing or cancelling the user's own scheduled fleets. |
| `fleetops.view_all_fleets` | Read the full fleet archive and open all Fleet Operation records, including scheduled fleets, the member list of running fleets and their Fleet tab (read-only). Does not grant edit/control rights. |
| `fleetops.create_manual_fleet` | Create a manual/historical fleet record without ESI tracking. |
| `fleetops.manage_fleets` | Manage every Fleet Operation, regardless of FC, including editing and cancelling any scheduled fleet and fleet management on the Fleet tab of any running fleet. It does not allow starting another FC's scheduled fleet. FC lead rights of the PAP rules: approve or reject manual fleets, manual PAP, pasted rosters, PAP-link fleets and edited fleet times (the user's own entries and edits are approved at once); set a PAP multiplier beyond the duration rule with a written reason; count a fleet in statistics anyway or undo an exclusion; delete approved manual PAP. |
| `fleetops.view_corp_stats` | View the user's own corporation: statistics, members, inactive members, member pages, PAP history and the exports of that corporation. See [Statistics](#statistics). |
| `fleetops.view_all_stats` | View alliance-wide statistics: every corporation, the alliance members and inactive pages, every member page, FC statistics, PAP history, the leaderboard and all statistics exports. Statistics only: opening Fleet Operation records needs `view_all_fleets` or `manage_fleets`. |
| `fleetops.manage_attendance` | Add manual PAP (by name, ID or pasted roster) and delete the user's own entries until an FC lead approved them, recalculate PAP and change the PAP multiplier of closed fleets within the duration rule, on fleets the user is allowed to manage; with `manage_fleets`, applies alliance-wide and any entry can be deleted. |
| `fleetops.manage_incentives` | Manage monthly FC incentive periods, waivers, recalculation, finalization and unlock. The FC Incentive pages are only available while **Incentive enabled** is ticked in FleetOps Administration → General Settings, which is off by default on new installs. |
| `fleetops.manage_configuration` | Use FleetOps front-end Administration for fleet types, comms presets, Logi/Boost channels, ping channels, ping roles (including **Sync roles from Discord**), PAP multiplier tiers, special roles, ship functions and their rules, fleet structure templates, templates and settings. Classify a ship from the composition of a fleet the user can open. |
| `fleetops.view_audit_log` | View FleetOps audit logs. |

## Recommended Alliance Auth role bundles

### Member

- `fleetops.basic_access`

Capabilities:

- View own attendance / fleet participation records.
- View own attendance history.
- See the PAP leaderboard of the alliance and of their own corporation (the default of **Leaderboard visibility**), their monthly minimum and their rank on My Statistics.
- Open Fleet Operations in which the member has a granted attendance record or assigned special role, and running fleets the member is tracked in or registered for by PAP link (seeing only their own characters until the fleet closes).
- See "PAP in running fleets" on the dashboard and My Statistics.
- Register their own characters through a PAP link.
- View personal statistics.

### Corporation Management

Member permissions plus:

- `fleetops.view_corp_stats`

Capabilities:

- Everything a Member can do.
- View own corporation monthly statistics against the monthly target.
- View the members of the own corporation, the inactive list and the page of each member of the own corporation.
- View the own corporation's PAP history.
- Download the members and the PAP history of the own corporation as Excel or CSV.
- A corporation manager never sees another corporation's members, member pages or exports. The pages that need `view_all_stats` (the alliance members list, and a corporation's page or members list by its ID, including the own corporation's) answer 403; the member page of another corporation and its members or member-fleets export answer 404.

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
- Manage an active own fleet from the Fleet tab: add, rename and delete wings and squads, move members and change their positions, invite characters registered in Alliance Auth and apply structure templates. Every change uses the FC character's ESI token.
- See the read-only Fleet tab (tree and composition) of every running fleet (`view_all_fleets`).
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

## Statistics

| Action | Who |
|---|---|
| My Statistics, My History, the monthly minimum and the rank card | `fleetops.basic_access`. |
| Leaderboard | `fleetops.basic_access` and **Leaderboard visibility**: with **Everyone with FleetOps access** (default) every user sees the alliance board and the board of their own corporation, `view_all_stats` also any corporation's board; with **Statistics managers only** `view_corp_stats` sees the own corporation's board and `view_all_stats` the alliance and any corporation. A user without access gets 403 and no menu entry. The rank card on My Statistics follows the same setting. |
| Own corporation: Statistics, Members, Inactive, History | `fleetops.view_corp_stats`. A user without a main character gets 404. |
| One member's page | The member (redirected to My Statistics), `view_corp_stats` for members whose main is in the own corporation, `view_all_stats` for every member. Members outside the statistics (no main, disabled account, departed from the member alliances) have no page (404). Fleet links on it need `view_all_fleets` or `manage_fleets`. |
| Alliance: All Corps, any corporation's page and members list, Members, Inactive, FC Stats, PAP History | `fleetops.view_all_stats`. |
| Monthly Targets, Leaderboard places, Leaderboard visibility, Member alliance IDs | `fleetops.manage_configuration`. Every change is audited. |

Exports (`/fleetops/statistics/export/<dataset>/?format=xlsx|csv`):

| Dataset | Who |
|---|---|
| `corporations`, `fcs` | `view_all_stats` |
| `members` | `view_all_stats` for the alliance or any corporation (`corporation=<id>`); `view_corp_stats` only with the own corporation's ID, any other corporation is 404 |
| `member-fleets` | whoever may open that member's page (`user=<id>`), otherwise 404 |
| `attendance` (PAP history) | `scope=me`: `basic_access`; `scope=corporation`: `view_corp_stats` (own corporation); `scope=alliance`: `view_all_stats` |
| `incentives` | `manage_incentives`, while **Incentive enabled** is ticked |

An unknown dataset or format is 404, only GET is accepted, responses are private and not cached, and every download is audited as `statistics.export` (dataset, scope, month, format and the validated filters; no tokens or secrets).

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

## Fleet tab (fleet management)

| Action | Who |
|---|---|
| See the Fleet tab of an active or ending fleet (fleet tree and composition) | The people who manage the fleet (its FC and FC-credited special-role members with `fleetops.manage_own_fleet`, and `fleetops.manage_fleets`), and `fleetops.view_all_fleets` read-only. Other members who can open the fleet page do not see it while the fleet is open. |
| See the composition of a closed fleet | Everybody who can open the fleet page. |
| Refresh the fleet from ESI, add/rename/delete wings and squads, move members, make squad/wing/fleet commanders, kick from the tree, invite characters, apply a structure template | `fleetops.manage_own_fleet` on own and credited fleets, or `fleetops.manage_fleets`; active ESI fleets only. ESI is always called with the FC character's token, which needs `esi-fleets.write_fleet.v1`. |
| Invite a character | Any character registered in Alliance Auth by an active user, whether or not that user has FleetOps access. Characters outside Alliance Auth and characters of inactive users are refused. |
| Quick classify a ship from the composition | `fleetops.manage_configuration`, on a fleet the user can open. |
| Ship functions, ship function rules, fleet structure templates, **Ship groups without bridge**, **Live fleet refresh** | `fleetops.manage_configuration` (FleetOps Administration). |

Changes are refused, with the reason, after the FC handed the fleet boss role to another pilot, while the fleet cannot be read, while the FC character has no token with the write scope and while a background job runs for the fleet. The live ESI refresh runs only for the fleet's managers, never for read-only viewers.

Every change is audited with its result: `fleet.wing_create`, `fleet.wing_rename`, `fleet.wing_delete`, `fleet.squad_create`, `fleet.squad_rename`, `fleet.squad_delete`, `fleet.member_move`, `fleet.invite`, the background jobs `fleet.move_job`, `fleet.invite_job` and `fleet.template_job`, and quick classifications as `configuration.create` or `configuration.update`.

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
