# Permissions

FleetOps uses the permissions of its Django app `fleetops`. Assign them through Alliance Auth groups, states
or single users. Normal administration needs no staff or superuser account.

Permission names appear in the Alliance Auth admin in English whatever language the page is in, so this
document lists the codenames. The codename and the label of `fleetops.manage_attendance` keep the word
"attendance"; the permission covers PAP.

A Simplified Chinese version is in [zh-CN/permissions.md](zh-CN/permissions.md).

## Permission reference

| Codename | Label in the admin | What it allows |
|---|---|---|
| `fleetops.basic_access` | Can access FleetOps | Open FleetOps with the personal dashboard, statistics and history, and the fleets the user took part in (also running fleets where the user is tracked or registered through a PAP link). Register characters through a PAP link. |
| `fleetops.start_fleet` | Can start fleets | Start an ESI-tracked fleet: send the ping, set the MOTD, create the SRP link and start tracking. Send a pre ping and schedule a fleet, see every scheduled fleet and start the fleets the user scheduled. Start a PAP-link fleet. Save and use personal start presets. |
| `fleetops.manage_own_fleet` | Can manage own fleets | End, edit, retry and control the fleets the user leads or is credited FC of through a special role: Regenerate & Push MOTD, kicks, the Fleet tab (wings, squads, moves, invites, structure templates), PAP links, excluding the fleet from statistics, settling a pending multiplier choice, and editing or cancelling the user's own scheduled fleets. |
| `fleetops.view_all_fleets` | Can view all fleet records | Read the whole fleet archive and open every fleet record, including scheduled fleets, the member list of running fleets and their Fleet tab (read-only). Gives no edit or control rights. |
| `fleetops.create_manual_fleet` | Can create manual fleet records | Add a manual (historical) fleet record without ESI tracking. |
| `fleetops.manage_fleets` | Can manage all fleets | Manage every fleet whoever leads it, including editing or cancelling any scheduled fleet and the Fleet tab of any running fleet. It does not allow starting another FC's scheduled fleet. This is also the FC lead permission of the PAP rules: approve or reject manual fleets, manual PAP, pasted rosters, PAP-link fleets and edited fleet times (the user's own entries are approved at once), set a PAP multiplier beyond the duration rule with a written reason, give a fleet more PAP characters per player than the setting allows, or no limit at all, and change that value after the fleet closed (with a written reason), count a fleet in statistics anyway or undo an exclusion, delete approved manual PAP. |
| `fleetops.view_corp_stats` | Can view own corporation statistics | The user's own corporation: statistics, members, inactive members, member pages, PAP history and their exports. |
| `fleetops.view_all_stats` | Can view all FleetOps statistics | Alliance-wide statistics: every corporation, all members and inactive members, every member page, FC statistics, PAP history, the leaderboard and every statistics export. Statistics only; opening fleet records needs `view_all_fleets` or `manage_fleets`. |
| `fleetops.manage_attendance` | Can manage attendance | Add manual PAP (by name, ID or pasted roster), delete the user's own entries until an FC lead approves them, recalculate PAP and change the PAP multiplier of closed fleets within the duration rule, on fleets the user may manage. Together with `manage_fleets` it applies to every fleet. |
| `fleetops.manage_incentives` | Can manage FC incentives | Review, recalculate, waive, finalize and unlock monthly FC incentive periods. The pages exist only while **Incentive enabled** is ticked in the general settings (off by default). |
| `fleetops.manage_configuration` | Can manage FleetOps configuration | Use the FleetOps Administration pages: settings, fleet types, comms presets, Logi and Boost channels, ping channels and roles (including **Sync roles from Discord**), PAP multipliers, special roles, monthly targets, ship functions and rules, structure templates, message templates and the SRP settings, payout table and corporation limits. Classify a ship from the composition of a fleet the user can open. |
| `fleetops.view_audit_log` | Can view FleetOps audit log | Open the **Audit** page. |
| `fleetops.request_srp` | Can request SRP | Claim ship losses in FleetOps SRP (My SRP and the SRP links of fleets) and see the user's own claims and monthly allowance. Needs FleetOps SRP to be enabled. The migration that added SRP gave it to every group, state and user that already held `basic_access`; add it to groups you create later. |
| `fleetops.manage_srp` | Can review and pay SRP claims | Review and pay claims: approve, change amounts, reject, revoke, mark paid, reopen, reclassify, recalculate, check the fleet presence again and add internal notes. Open and close fleet SRP links. See every SRP summary and claim. Not granted to anybody by default because it decides on money. |

## Recommended groups

These five groups cover most alliances. Create them as Alliance Auth groups and add the permissions.

### Member

- `fleetops.basic_access`
- `fleetops.request_srp`

A member sees their own PAP, history, monthly minimum and rank, the PAP leaderboard of the alliance and of
their own corporation (the default of **Leaderboard visibility**), "PAP in running fleets", and the fleets they
took part in. A member can register their own characters through a PAP link, and claim ship losses once FleetOps
SRP is enabled. Of a running fleet a member sees only their own characters until the fleet closes.

### Corporation manager

The Member permissions plus `fleetops.view_corp_stats`.

A corporation manager sees the own corporation's monthly statistics against the target, its members, its
inactive list, the page of each of its members, its PAP history, and downloads the members and the PAP history
as Excel or CSV. Another corporation's members, member pages and exports stay closed: the pages that need
`view_all_stats` answer 403, a member page of another corporation and its exports answer 404.

### FC

- `fleetops.basic_access`
- `fleetops.start_fleet`
- `fleetops.manage_own_fleet`
- `fleetops.view_all_fleets`
- `fleetops.create_manual_fleet`
- `fleetops.manage_attendance`

An FC starts fleets (with the full mode or **PAP Tracking Only**), sends and retries the ping and MOTD,
creates the SRP link, sends pre pings and starts, edits or cancels their own scheduled fleets. The FC tracks and
ends their own fleets, chooses the PAP multiplier when ending, kicks members, manages the fleet from the Fleet
tab, creates and revokes PAP links, starts PAP-link fleets, assigns special roles (also after the fleet ended),
adds manual PAP (it counts once an FC lead approves it), creates manual fleet records and edits the times of
their own fleets (new times wait for approval). An FC reads every historical fleet and the Fleet tab of every
running fleet, read-only.

`view_all_fleets` never allows changes to another FC's fleet.

### FC lead

All FleetOps permissions except the SRP ones:

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

An FC lead manages every fleet, approves manual records, overrides multipliers and PAP character limits, sees all statistics and the
audit log, runs the FC incentives (once **Incentive enabled** is ticked) and administers FleetOps. Add
`fleetops.request_srp` and `fleetops.manage_srp` when FC leads also run SRP.

### SRP manager

- `fleetops.basic_access`
- `fleetops.request_srp`
- `fleetops.manage_srp`
- `fleetops.view_all_fleets` (only to open a fleet from a claim)

An SRP manager reviews, approves, changes, rejects, revokes and pays claims with the reasons the rules ask for,
opens and closes fleet SRP links, reads the claims of every fleet, sees every SRP summary and downloads the SRP
files, and adds internal notes that only SRP managers read. Another SRP manager decides on the manager's own
claims, unless **SRP managers may decide on their own claims** is ticked.

## Row-level rules

- A fleet is managed by its FC, by members whose special role gives FC credit and who hold
  `fleetops.manage_own_fleet`, and by anybody with `fleetops.manage_fleets`.
- `manage_attendance` alone does not let an FC change another FC's fleet. It applies to fleets the user may
  manage; with `manage_fleets` it applies to all of them, and entries are approved at once.
- A special role's **Gives FC credit** switch alone decides whether its holder gets FC fleet and point credit;
  the holder needs no FC permission for that. Managing the fleet still needs `manage_own_fleet`. Roles go to
  characters recorded in the fleet; manual PAP that waits for approval or was rejected does not make a
  character eligible, so FC credit cannot be handed out through unreviewed manual PAP.
- Users see their own corporation through the corporation of their main character. A user without a main
  character gets 404 on the corporation pages.

## The incentive switch

FC incentives are off on a new site. `fleetops.manage_incentives` alone does not show the FC Incentive pages;
tick **Incentive enabled** under **Administration** > **General Settings** first. While the switch is off the
menu entry is hidden and periods cannot be reviewed, recalculated, waived, finalized, unlocked or exported. FC
statistics are not affected.

## Statistics

| Action | Who |
|---|---|
| My Statistics, My History, the monthly minimum and the rank card | `fleetops.basic_access` |
| Leaderboard | `fleetops.basic_access` and **Leaderboard visibility**: with **Everyone with FleetOps access** (default) every user sees the alliance board and the board of their own corporation; `view_all_stats` also any corporation's board. With **Statistics managers only**, `view_corp_stats` sees the own corporation's board and `view_all_stats` the alliance and any corporation. Without access the page answers 403 and the menu entry is hidden. The rank card on My Statistics follows the same setting. |
| Own corporation: Statistics, Members, Inactive, History | `fleetops.view_corp_stats` |
| One member's page | The member (redirected to My Statistics), `view_corp_stats` for members whose main is in the own corporation, `view_all_stats` for everybody. Users outside the statistics (no main, disabled account, left the member alliances) have no page (404). Links to fleets on it need `view_all_fleets` or `manage_fleets`. |
| Alliance: All Corps, any corporation's page and members list, Members, Inactive, FC Stats, PAP History | `fleetops.view_all_stats` |
| Monthly Targets, Leaderboard places, Leaderboard visibility, Member alliance IDs | `fleetops.manage_configuration`; every change is audited |

Exports are at `/fleetops/statistics/export/<dataset>/?format=xlsx` or `format=csv`:

| Dataset | Who |
|---|---|
| `corporations`, `fcs` | `view_all_stats` |
| `members` | `view_all_stats` for the alliance or any corporation (`corporation=<id>`); `view_corp_stats` only with the own corporation's ID, any other corporation is 404 |
| `member-fleets` | Whoever may open that member's page (`user=<id>`), otherwise 404 |
| `attendance` (PAP history) | `scope=me`: `basic_access`; `scope=corporation`: `view_corp_stats`; `scope=alliance`: `view_all_stats` |
| `incentives` | `manage_incentives`, while **Incentive enabled** is ticked |

An unknown dataset or format answers 404, only GET is accepted, and responses are private and not cached. Every
download is audited as `statistics.export` with the dataset, scope, month, format and filters.

## SRP

| Action | Who |
|---|---|
| My SRP, the SRP link page of a fleet, checking and submitting a loss, withdrawing and submitting a claim again, the page of an own claim | `fleetops.request_srp`. The claim must be for a character on the user's own Alliance Auth account. |
| SRP card on the fleet page | The link and its Copy button: the fleet's managers, its FC and SRP managers. **Request SRP for this fleet**: `request_srp`, while the link is open. |
| Create the SRP link of a fleet (**Create SRP link**, **Retry SRP**) | `fleetops.manage_own_fleet` on the fleet, or `fleetops.manage_fleets` |
| Open or close a fleet's SRP link | `fleetops.manage_srp` |
| Review queue, payments page, decisions on a claim, bulk approval, **Recalculate** | `fleetops.manage_srp`; not on the manager's own claims unless **SRP managers may decide on their own claims** is ticked (403; bulk actions skip them with a message). Checking the fleet presence again and an internal note need only `manage_srp`. |
| Any claim page | The claimant, `manage_srp`, the FC of the claim's fleet, `view_all_stats` (read-only), and `view_corp_stats` for claims of the own corporation. Anybody else gets 404. Internal notes and the names of the people who acted on a claim are visible to `manage_srp` only; everybody else sees "Claimant", "Reviewer" or "System". The monthly usage is shown to the claimant, `manage_srp`, `view_all_stats` and `view_corp_stats` of the claim's corporation, not to the FC of the fleet. |
| SRP Summary (alliance, corporations, FCs) | `manage_srp` or `view_all_stats` (read-only) |
| SRP Corporation (totals, members, claims of the own corporation) | `view_corp_stats` for the own corporation (a user without a main gets 404); `manage_srp` or `view_all_stats` for any corporation |
| SRP My fleets (claims and totals of the fleets the user led) | Users with an FC permission (`start_fleet`, `manage_own_fleet`, `manage_fleets` or `create_manual_fleet`) and anybody who led a fleet with an SRP link see their own page; `manage_srp` and `view_all_stats` also see any FC's page |
| SRP settings, SRP Payout Table, SRP Corporation Limits | `fleetops.manage_configuration`; every change is audited with the old and new values |

SRP files are at `/fleetops/statistics/export/<dataset>/`:

| Dataset | Who |
|---|---|
| `srp-claims`, `srp-members` | `manage_srp` or `view_all_stats` for the alliance or any corporation (`corporation=<id>`); `view_corp_stats` only with the own corporation's ID (any other corporation is 404, no corporation is 403) |
| `srp-corporations`, `srp-fcs` | `manage_srp` or `view_all_stats` |

The *Decided by* and *Paid by* columns of `srp-claims` are filled for `manage_srp` only. FCs and members have no
SRP file. Every download is audited as `srp.export`. Every other SRP action is audited and shown in the
claim's timeline: `srp.link_create`, `srp.link_open`, `srp.link_close`, `srp.claim_submit`,
`srp.claim_auto_reject`, `srp.claim_withdraw`, `srp.claim_resubmit`, `srp.claim_approve`,
`srp.claim_amount_change`, `srp.claim_reject`, `srp.claim_revoke`, `srp.claim_paid`, `srp.claim_unpaid`,
`srp.claim_reopen`, `srp.claim_reclassify`, `srp.claim_recalculate`, `srp.claim_recheck`, `srp.claim_note`.
The entries hold IDs, statuses and amounts, never a token or a webhook URL; `srp.claim_note` holds only the
length of the note.

## Scheduled fleets (pre pings)

| Action | Who |
|---|---|
| Send a pre ping and schedule a fleet | `fleetops.start_fleet` |
| See scheduled fleets (dashboard list, scheduled fleet page, archive) | The fleet's FC, and users with `start_fleet`, `view_all_fleets` or `manage_fleets`. Other members do not see pre pings in FleetOps, because a pre ping may have gone to a restricted channel. |
| Start a scheduled fleet | Only the FC who scheduled it (with `start_fleet`), with one of their own characters that ESI confirms as the fleet boss. Other FCs and fleet managers cannot start it. |
| Edit a scheduled fleet (optionally post the updated pre ping) | The fleet's FC with `manage_own_fleet`, or `manage_fleets` |
| Cancel a scheduled fleet (optionally post a notice that mentions nobody) | The fleet's FC with `manage_own_fleet`, or `manage_fleets` |

Scheduling, editing, cancelling and starting are audited as `fleet.schedule`, `fleet.schedule_edit`,
`fleet.schedule_cancel` and `fleet.start` (once per start, with `from_schedule` when it came from a schedule).

## Running fleets

- **Regenerate & Push MOTD** needs `manage_own_fleet` on a fleet the user is credited FC of, or
  `manage_fleets`. It is audited as `fleet.motd_regenerate`.
- While a fleet is starting, active or ending, its member list, 3-Jump Map, events and the details of its kick
  steps are shown only to the people who manage the fleet and to users with `view_all_fleets` or
  `manage_fleets`. Other members who can open the fleet see their own PAP and special roles; the rest becomes
  visible when the fleet is closed.
- When ESI confirms a new fleet boss at fleet start, the record of the previous FC for that ESI fleet ends with
  "fleet boss handed over", whatever permission the new boss has on that record. Both records keep their own
  PAP and FC credit. The handover is audited as `fleet.boss_handover` on the old record.

## Fleet tab

| Action | Who |
|---|---|
| See the Fleet tab of an active or ending fleet (tree and composition) | The fleet's managers (its FC and FC-credited special-role members with `manage_own_fleet`, and `manage_fleets`), and `view_all_fleets` read-only. Other members who can open the fleet page do not see it while the fleet is open. |
| See the composition of a closed fleet | Everybody who can open the fleet page |
| Refresh the fleet from ESI, add, rename and delete wings and squads, move members, set squad, wing and fleet commanders, kick from the tree, invite characters, apply a structure template | `manage_own_fleet` on own and credited fleets, or `manage_fleets`; active ESI fleets only. ESI is always called with the FC character's token, which needs `esi-fleets.write_fleet.v1`. |
| Invite a character | Any character registered in Alliance Auth by an active user, whether or not that user has FleetOps access. Characters outside Alliance Auth and characters of inactive users are refused. |
| Quick classify a ship from the composition | `manage_configuration`, on a fleet the user can open |
| Ship functions, rules, structure templates, **Ship groups without bridge**, **Live fleet refresh** | `manage_configuration` |

Changes are refused, with the reason, after the FC handed the fleet boss role to another pilot, while the fleet
cannot be read, while the FC character has no token with the write scope, and while a background job runs for
the fleet. The live ESI refresh runs only for the fleet's managers, never for read-only viewers.

Every change is audited with its result: `fleet.wing_create`, `fleet.wing_rename`, `fleet.wing_delete`,
`fleet.squad_create`, `fleet.squad_rename`, `fleet.squad_delete`, `fleet.member_move`, `fleet.invite`, and for
background jobs `fleet.move_job`, `fleet.invite_job` and `fleet.template_job`. Quick classifications are
audited as `configuration.create` or `configuration.update`.

## PAP rules

| Action | Who |
|---|---|
| Approve or reject manual fleets, manual PAP, pasted rosters, PAP-link fleets and fleets whose times an FC edited (Approvals page and fleet page) | `fleetops.manage_fleets`. Entries and time edits of users with `manage_fleets` are approved at once; everybody else's wait and do not count until then. |
| Delete a manual or imported PAP row | Its author while it is pending or rejected (with `manage_attendance` on the fleet); any row with `manage_fleets` |
| Choose the PAP multiplier when ending a fleet | Whoever may end the fleet. The server allows only the multipliers the fleet's duration reached. |
| Change the multiplier of a closed fleet within the duration rule | `manage_attendance` on the fleet. The fleet's FC (`manage_own_fleet`) can always settle a pending choice. |
| Set a multiplier beyond the duration rule | `manage_fleets`, with a written reason of at least 5 characters (audited as `attendance.multiplier` with source "override") |
| Choose **PAP characters per player** for a fleet on the Start Fleet page, the PAP-link fleet page and the edit page of a scheduled fleet | FCs and group leaders: up to the setting **PAP characters per player** when it is set (any value when it is empty); `manage_fleets`: any value, including no limit |
| Change **PAP characters per player** of a running fleet (control above the PAP preview) | Whoever manages the fleet, with the same rule: up to the setting, or any value with `manage_fleets` (audited as `pap.limit`) |
| Change **PAP characters per player** of a closed fleet (PAP is calculated again) | `manage_fleets` only, with a written reason of at least 5 characters (audited as `pap.limit`). FCs and group leaders cannot change it any more. |
| Recalculate PAP of a closed fleet | `manage_attendance` on the fleet |
| Exclude a fleet from statistics (**Do not count**, reason required) | Whoever manages the fleet |
| Count a fleet anyway (**Always count**) or return to the automatic rule | `manage_fleets` |
| Create or revoke a PAP link | `manage_own_fleet` on own and credited fleets, or `manage_fleets`; active or closed fleets only |
| Register characters through a PAP link | `basic_access`, for the user's own characters with the user's own ESI token |
| Start a PAP-link fleet | `start_fleet`; the fleet waits for approval unless the starter has `manage_fleets` |
| Kick a member, kick outside a system, Kick Capsules | `manage_own_fleet` on own and credited fleets, or `manage_fleets`. The FC character needs a token with `esi-fleets.write_fleet.v1`; the FC and the fleet boss are never kicked. |
| Save, load, rename and delete start presets | `start_fleet`; every user sees only their own presets. Presets are not audited. |
| Configure PAP multipliers and special roles | `manage_configuration` |

Approvals, multiplier changes, statistics inclusion, PAP links, registrations, recalculations and kicks are
audited as `approval.*`, `attendance.multiplier`, `fleet.stats_mode`, `pap_link.create`, `pap_link.revoke`,
`pap.register`, `pap.recalculate`, `fleet.kick_member`, `fleet.kick_outside_system` and `fleet.kick_capsules`.

## Configuration

- **Sync roles from Discord** on the Ping Roles page needs `fleetops.manage_configuration`, the Alliance Auth
  Discord service and `DISCORD_GUILD_ID`. Synced roles start inactive; an administrator decides which roles
  FCs may ping. Each sync is audited as `configuration.discord_role_sync`.
- Webhook URLs of ping channels are write-only: users with `manage_configuration` can replace a URL, but no
  page shows a stored one. In the Django admin, users who may only view ping channels see "Stored (hidden)".
- The comms join page (`/fleetops/comms/<token>/join/`) is the only FleetOps page without a permission check.
  It needs no login when `"fleetops"` is in `APPS_WITH_PUBLIC_VIEWS`; otherwise it needs an Alliance Auth login
  with a main character, like every Alliance Auth page.
