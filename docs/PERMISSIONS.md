# FleetOps Permission Model (0.1.0a5)

FleetOps permissions are designed to be assigned through normal Alliance Auth groups. Django staff/superuser access is **not** required for normal FleetOps administration.

## Permission definitions

| Permission | Purpose |
|---|---|
| `fleetops.basic_access` | Open FleetOps, personal dashboard/statistics/history, and fleet records the user participated in. |
| `fleetops.start_fleet` | Start an ESI-tracked Fleet Operation, send a ping, generate/set MOTD, and start attendance tracking. |
| `fleetops.manage_own_fleet` | End/edit/retry/control fleets for which the user is the FC or an FC-credited special-role member. |
| `fleetops.view_all_fleets` | Read the full fleet archive and open all Fleet Operation records. Does not grant edit/control rights. |
| `fleetops.create_manual_fleet` | Create a manual/historical fleet record without ESI tracking. |
| `fleetops.manage_fleets` | Manage every Fleet Operation, regardless of FC. |
| `fleetops.view_corp_stats` | View the user's own corporation statistics and corporation attendance history. |
| `fleetops.view_all_stats` | View alliance-wide corporation and FC statistics/history. Statistics only: opening Fleet Operation records needs `view_all_fleets` or `manage_fleets`. |
| `fleetops.manage_attendance` | Add/delete manual attendance and set 1x/2x/3x attendance multiplier on fleets the user is allowed to manage; with `manage_fleets`, applies alliance-wide. |
| `fleetops.manage_incentives` | Manage monthly FC incentive periods, waivers, recalculation, finalization and unlock. The FC Incentive pages are only available while **Incentive enabled** is ticked in FleetOps Administration → General Settings, which is off by default on new installs. |
| `fleetops.manage_configuration` | Use FleetOps front-end Administration for fleet types, comms, channels, webhooks, templates and settings. |
| `fleetops.view_audit_log` | View FleetOps audit logs. |

## Recommended Alliance Auth role bundles

### Member

- `fleetops.basic_access`

Capabilities:

- View own attendance / fleet participation records.
- View own attendance history.
- Open Fleet Operations in which the member has a granted attendance record or assigned special role.
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
- Send/retry Ping, generate/set/retry MOTD, create/retry SRP for own fleets.
- Track and end own fleets.
- Kick currently detected capsule members from an active fleet.
- Add/remove Back Seat FC, Logi Anchor and Snowflake assignments on own fleets, including after fleet end.
- Correct attendance after fleet end.
- Select 1x/2x/3x fleet attendance multiplier.
- Add manual attendance (1-100 credits per entry) for characters registered to an Alliance Auth user.
- Create manual/historical fleet records.

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

Capabilities include all fleet operations, all statistics, attendance management, FC incentives (once **Incentive enabled** is ticked in the FleetOps settings), configuration, and audit review.

## Important row-level rule

`manage_attendance` alone does not allow an FC to change another FC's fleet attendance. An FC can change attendance only when the user is the operation FC / FC-credited special-role member. `manage_fleets + manage_attendance` gives alliance-wide attendance management.
