# Configuration reference

Administrators configure FleetOps in the web interface: **Administration** in the FleetOps menu
(`/fleetops/configuration/`). The page needs the permission `fleetops.manage_configuration`
([permissions](permissions.md)). No Django admin access is needed; the Django admin stays an emergency tool.
Every change is written to the audit log with the old and new values.

A Simplified Chinese version of this page is in [zh-CN/configuration.md](zh-CN/configuration.md).

| Administration page | What it controls |
|---|---|
| [General Settings](#general-settings) | Tracking, automatic fleet end, PAP rules, statistics, PAP links, Fleet tab, SRP provider, FC incentives, history |
| [Fleet Types](#fleet-types) | Fleet categories and their FC point weight |
| [PAP Multipliers](#pap-multipliers) | Multipliers for long fleets |
| [Monthly Targets](#monthly-targets) | Corporation PAP target and member minimum per month (a month without a target is not assessed) |
| [Special Roles](#special-roles) | Roles FCs give to pilots in a fleet |
| [Comms Presets](#comms-presets) | Voice choices and their public join link |
| [Logi / Boost Channels](#logi--boost-channels) | In-game channels shown in the MOTD |
| [Ping Channels](#ping-channels) and [Ping Roles](#ping-roles) | Discord webhooks, allowed mentions, mentionable roles, the groups that may use a channel |
| [Groups](#groups) | What the leaders of an Alliance Auth group may do: send pings, start fleets with PAP |
| [Message Templates](#message-templates) | Ping, MOTD and Ping without fleet texts |
| [Doctrine Settings](#doctrine-settings) | Who may ping which doctrine, who may use a custom doctrine name, and the order and the marks of the ships of a doctrine in pings and MOTDs |
| [Ship Functions](#ship-functions-and-rules), [Ship Function Rules](#ship-functions-and-rules), [Fleet Structure Templates](#fleet-structure-templates) | Composition groups and wing and squad layouts |
| [FleetOps SRP](#fleetops-srp), [SRP Payout Table](#srp-payout-table), [SRP Corporation Limits](#srp-corporation-limits) | The ship replacement program |

## Site settings

FleetOps reads a few settings from the Alliance Auth project (`local.py`). The Administration page shows a
warning while the first two are missing.

| Setting | Used for |
|---|---|
| `SITE_URL` | The public https address of Alliance Auth, for example `https://auth.example.com`. Already defined near the top of `local.py`, and `ALLOWED_HOSTS`, `CSRF_TRUSTED_ORIGINS` and `ESI_SSO_CALLBACK_URL` are derived from it: do not add the line again or change it (that breaks the EVE SSO callback), only check that it is your site's https address without a trailing slash. Links in pings and MOTDs (comms join page, doctrines, SRP links, the PAP link) are built from it. Without it they are site paths that open nowhere outside Alliance Auth. |
| `APPS_WITH_PUBLIC_VIEWS` | Add `"fleetops"` so pilots who are not logged in can open the comms join page. |
| `ESI_USER_CONTACT_EMAIL` | The line already exists in `local.py` (empty in the template): fill it in, do not add a second one. Sent in the User-Agent of ESI requests (django-esi requires it) and of zKillboard requests (`SITE_URL` is the fallback there). |
| `DISCORD_GUILD_ID` | Needed by the Alliance Auth Discord service for **Sync roles from Discord**. |
| `LANGUAGE_CODE` | The site language. FleetOps pages follow each user's Alliance Auth language; pings and MOTDs are always English. |

`python manage.py check` also warns (warnings only, nothing stops) when one of these is missing: `fleetops.W001` the
beat entry of fleet tracking, `fleetops.W002` the daily beat entry, `fleetops.W003` an empty `SITE_URL`,
`fleetops.W004` no `"fleetops"` in `APPS_WITH_PUBLIC_VIEWS`, `fleetops.W005` `ATOMIC_REQUESTS` on for the default
database (it must stay off: FleetOps locks rows and needs each request outside one transaction), `fleetops.W006` aa-charlink is installed but `INSTALLED_APPS` does not contain the
literal entry `"fleetops"`. The beat checks read
`CELERYBEAT_SCHEDULE` of `local.py`; a site that keeps its schedule only in the Django admin silences them with
`SILENCED_SYSTEM_CHECKS = ["fleetops.W001", "fleetops.W002"]`, and a site without the comms join page silences
`fleetops.W004` the same way.

## General settings

**Administration** > **General Settings**. The page groups the settings; each group says whether a change
reaches fleets that already started.

### General

| Setting | Default | Meaning |
|---|---|---|
| Tracking interval | 60 | Seconds between two ESI reads of a running fleet. At least 30. The beat task runs every minute and only checks which fleets are due. |
| Stale threshold | 180 | Seconds after which the fleet page marks the last ESI read as stale. At least 60. |
| Auto end enabled | on | End a fleet automatically when ESI keeps reporting it as gone. |
| Auto end missing count | 3 | Unreadable polls in a row before the automatic end (at least 2). Before ending, FleetOps reads the fleet once more and calls the end off if that read works. A successful read resets the count. |
| Incentive enabled | off | Turns the FC incentive pages on. See [FC incentives](#fc-incentives). |
| Incentive minimum fleets | 3 | Default for the minimum number of counted fleets of a new incentive period. |
| Data retention days | 365 | How long PAP history, member events and the texts of pings sent without a fleet are kept. 365 to 36500. `fleetops_prune_history` and the daily beat task remove older rows. |
| SRP auto create | on | Create an SRP link for every fleet when it starts. |
| SRP provider | Automatic | Which SRP app gets the link. See [SRP providers](srp-providers.md). |

When ESI answers "not found" for a running fleet, each poll checks the FC's own fleet. If the FC is still in
the same fleet but somebody else holds the boss role, the fleet ends as a boss handover; otherwise as "fleet
no longer exists or the FC left it". The fleet page warns while polls fail.

### PAP rules

These three settings apply to fleets that start after a change. A running or closed fleet keeps the values it
started with. **PAP characters per player** is only the default: each fleet has its own value (see below).

| Setting | Default | Meaning |
|---|---|---|
| PAP multiplier mode | FC chooses when ending | **FC chooses when ending**: End Fleet asks the FC for the multiplier once the fleet passed a tier; a fleet that ends without a choice gets 1x until the FC chooses. **Automatic by fleet duration**: the fleet gets the highest multiplier its duration allows. |
| PAP minimum time | 0 | Minutes a character must spend in the fleet to receive PAP. 0 means any presence. Up to 1440. |
| PAP characters per player | empty | Most characters of one Alliance Auth user that receive PAP in one fleet. Empty means no limit. It is the default of the field of the same name on the Start Fleet page, and the most FCs may choose there. |

Rules behind these settings:

- Every tracking poll adds the time each character spent in the fleet since the previous poll. A pilot who left
  and came back between two polls is counted from ESI's join time; a pilot who left is counted up to the last
  poll that saw them. A long ESI outage between two good polls counts as time in fleet for pilots present at
  both ends.
- PAP is granted when the fleet closes (End Fleet or the automatic end), never while it runs. A character gets
  PAP when its time in fleet reaches the PAP minimum time, or when it registered through a PAP link.
- Over **PAP characters per player**, the FC character gets the PAP first, then the player's main, then the
  character with the longest time in fleet (then the one seen first). The others are listed as "Capped
  (limit)".
- **PAP characters per player of one fleet.** A fleet starts with the value of the Start Fleet field "PAP characters
  per player" (the setting is the default; empty means no limit). The value is saved in presets, and the PAP-link
  fleet page and the edit page of a scheduled fleet have the same field; a pre-pinged fleet starts with the value
  it was scheduled with. While the fleet runs, FCs change it in the control above the PAP preview on the **PAP**
  tab; the preview follows at once and PAP is granted at the close with the latest value. FCs and group leaders may
  only lower it: the value must not be higher than the setting when the setting is set (when it is empty, any
  value). FC leads (`manage_fleets`) may choose any value, including no limit. After the fleet has closed only FC
  leads can change it, with a written reason of at least 5 characters (the same rule as a multiplier override);
  the PAP of the fleet is then calculated again, PAP rows whose credit changes get a new time, and the FC points
  of the month are checked. Every change is audited as `pap.limit` with the old and the new value and the
  reason. Fleets that were closed before PAP followed time in fleet do not use the limit.
- Characters that are not in Alliance Auth get no PAP and are listed as "Not in Alliance Auth". **Recalculate
  PAP** on the closed fleet grants it once they were added to Alliance Auth.
- PAP belongs to the character's owner and counts for the owner's main and the main's corporation, also when
  the character itself is in another corporation or alliance. When a character changes or loses its owner
  later, recalculating the fleet keeps its PAP where it was.
- A fleet whose end finishes later than the End Fleet click (a busy worker, the recovery after a lost task)
  ends at the time of the click: no time in fleet after it counts, and the multiplier and the minimum fleet
  duration are measured to it.
- Manual and imported PAP rows are corrections: **keep** adds a separate credit, **replace** takes the place of
  the tracked PAP, **merge** adds its value to it. They are applied each time PAP is calculated, so deleting
  one and recalculating restores the tracked PAP.
- While a fleet runs, its **PAP** tab shows who would get PAP if the fleet closed now. Members who do not
  manage the fleet see only their own characters. The dashboard and My Statistics show "PAP in running fleets".
- A fleet that was closed under an earlier release keeps the PAP selection it was closed with and takes no PAP
  links; add manual PAP there.

### Statistics

| Setting | Default | Meaning |
|---|---|---|
| Minimum fleet duration | 0 | Fleets shorter than this many minutes are not counted in statistics and incentives unless an FC lead includes them. 0 counts all fleets. Applies to fleets that close after the change. |
| Member alliance IDs | empty | Alliance IDs, separated by commas or semicolons. When set, only users whose main character is in one of these alliances are members, and the history of departed members can be pruned. Empty means every Alliance Auth user with a main character. |
| Largest share of departed members to prune | 50 | Percent of the users with PAP history that one pruning of departed members may remove, 1 to 99. When more users would go (usually a wrong **Member alliance IDs**), nothing of the departed members is deleted and a warning is logged; see [History and retention](#history-and-retention). `fleetops_prune_history --force` overrides it; the daily task never does. |
| Leaderboard places | 20 | Places shown on the leaderboards, 3 to 100. Members tied on the last place are all shown. A viewer outside the shown places sees their own rank. |
| Leaderboard visibility | Everyone with FleetOps access | **Everyone with FleetOps access**: every user sees the alliance board and the board of their own corporation. **Statistics managers only**: `view_corp_stats` sees the own corporation, `view_all_stats` the alliance and any corporation. Without access the menu entry is hidden and the page answers 403. |

See [Statistics rules](#statistics-rules) for how the figures are counted.

### PAP links

| Setting | Default | Meaning |
|---|---|---|
| PAP link validity | 30 | Minutes a new PAP link stays open unless the FC picks another time. 1 to 1440. |

A registration is written as soon as the pilot's ownership, token and (when the link requires it) online status are
checked. Where the pilot is and what they fly are read afterwards in the background and show on the fleet page a moment
later; if ESI does not answer they stay blank and the PAP is not affected. A request that has spent about 20 seconds on
ESI stops: the characters not yet registered are listed with the reason "EVE did not answer fast enough" and can be
submitted again.

### Fleet tab

| Setting | Default | Meaning |
|---|---|---|
| Live fleet refresh | 15 | While somebody who manages a fleet has its Fleet tab, Members tab or PAP preview open with **Live** on, FleetOps reads the fleet from ESI this often (seconds, 10 to 300). 0 means only the tracking interval and the Refresh button. |
| Ship groups without bridge | Titan, Dreadnought, Freighter, Carrier, Supercarrier, Rorqual, Jump Freighter, Industrial Command Ship, Force Auxiliary, Lancer Dreadnought, Command Carrier (11 groups) | The ship groups whose mass is not counted as bridgeable. The default follows the titan bridge rule. |

**Ship groups without bridge** is a searchable list of ship groups with a tick for each group. The line "Now:"
under it names the ticked groups, each linked to EVE Ref, and "All ship groups on EVE Ref" opens the full list of
groups. Tick **Use the default list (11 groups)** and save to go back to the default. A group that is not in the
list can be added by its ID in **Other ship group IDs** (comma separated); when the SDE is installed, an ID that is
not a ship group is refused, without the SDE it is saved with a notice.

Cost of the live refresh: tracking reads each active fleet every tracking interval (members, and wings and
squads: two ESI calls). The live refresh adds two more calls per refresh with the FC character's token, about
120 calls per 15 minutes at 15 seconds, out of the 1800 that ESI allows per character. However many managers
watch, a fleet is read once per interval, and users with only `view_all_fleets` never cause reads. The **Live**
switch is remembered per browser.

### Messages

| Setting | Default | Meaning |
|---|---|---|
| MOTD warning length | 2000 | The start preview warns when the MOTD, tags included, is longer than this many characters. The game's limit is not documented; MOTDs of about 1,700 characters are known to work. 0 means no warning. At most 100000. |

## Fleet Types

| Field | Meaning |
|---|---|
| Name, Short name | Shown on Start Fleet and in statistics. The short name is shown where space is short. |
| Point weight | FC points one fleet of this type is worth, before the multiplier. FC points of a fleet are the weight times the PAP multiplier. |
| Active | Only active types are offered on Start Fleet. |
| Sort | Order in the lists. |

The weight is copied to each fleet when it starts. Changing it later does not change fleets that already
started. `python manage.py fleetops_seed --demo` creates the example types Peacetime (PCT, 0.5), Strategic
Operation (StratOps, 1.0) and Call To Arms (CTA, 1.5).

## PAP Multipliers

Each tier has a threshold in minutes and a multiplier from 2 to 10. A fleet that ran strictly longer than the
threshold may get that multiplier (90 minutes and 1 second qualifies for a 90-minute tier). Shorter fleets get
1x. Multipliers must rise with the thresholds. A new site starts with 90 minutes for 2x and 180 minutes for 3x.

- In the FC mode, **End Fleet** on a fleet past a tier opens a choice of the allowed multipliers, with none
  preselected. The server checks the choice again and refuses a multiplier the duration does not allow.
- A fleet that ended without a choice (fleet gone, boss handover, finished by the beat) gets 1x and, when it ran
  long enough for more, **Multiplier pending**. Its FC chooses on the fleet page (**Keep 1x** or a higher
  tier); the dashboard lists the fleets waiting for a choice and the incentive review warns about them. Until
  then the fleet counts at 1x.
- After the close, whoever manages the fleet's PAP can change the multiplier within the duration rule. FC leads
  (`fleetops.manage_fleets`) may set any configured multiplier with a written reason of at least 5
  characters; the fleet then shows "Override by an FC lead" and the audit log keeps the reason.
- New times entered by an FC wait for FC lead approval. A multiplier the new times of a closed fleet no longer
  allow is lowered to the highest one they allow (an FC lead's override stays).

## Monthly Targets

An entry has a year and month, a **Corporation average target** (average PAP per main character that a
corporation needs; empty means no target), a **Member minimum PAP** (0 means nobody is listed as inactive) and a
note. An entry applies from its month until the next entry, so a target set for the future never recolours
finished months; edit the older entry to change the past.

**A month without an applicable monthly target is not assessed at all.** There is no green, amber or red, nobody
is listed as inactive, and the pages and exports say "No target for this month". The same holds when no target was
ever configured. The inactive list tells administrators to set a monthly target first (the link is shown to
people who may change the targets).

A corporation meets the target when its total PAP is at least the target times its main characters (exact
arithmetic, no rounding). Corporations and members use the same colours:

| | Reached | Not reached |
|---|---|---|
| Month still running | Green: **Target met** / **OK** | Amber: **Not reached yet** / **Below minimum so far** |
| Finished month | Green | Red: **Below target** / **Below minimum** |

The label is always written out; the colour is never the only signal. A future month has no status.

### Statistics rules

Every statistics page and export follows the same rules.

- **Months are EVE months.** A month runs from the 1st at 00:00 UTC to the 1st at 00:00 UTC of the next month,
  whatever `TIME_ZONE` Alliance Auth uses and whatever time zone the browser has. A fleet belongs to the month
  it started in. All times shown are EVE time.
- **PAP counts** when it is granted and approved and the fleet is closed, approved and not excluded. Active,
  waiting, rejected, excluded and too short fleets never count; management pages show how many fleets were left
  out of the month. A fleet is counted unless it is **Do not count** (anyone who manages the fleet can set it,
  with a reason, for example for a test fleet); **Always count** and **Use the automatic rule** are for FC
  leads. Every change is audited as `fleet.stats_mode`.
- **Statistics are on the main character.** PAP earned by an alt belongs to the main of its owner, also when
  the alt is in another corporation or alliance.
- **Members** are active Alliance Auth users with a main character that is not in Doomheim (biomassed). With
  **Member alliance IDs** set, the main must be in one of those alliances. Alts never count as members, and a
  member's corporation is the corporation of the current main. A user whose Alliance Auth registration is at or
  after the end of a month is not part of that month: not in its member lists, inactive list, leaderboard or
  exports, and not counted as a main in its corporation figures.
- **Corporation total** is the PAP earned while the main was in the corporation. **Average PAP** is that total
  divided by the corporation's current main characters; alts are not counted. PAP earned in corporations
  without members (left the alliance, NPC corporations) is listed in one **Other corporations** row, so the
  figures add up to the alliance total.
- **Member lists** (members, inactive, leaderboard) show the current members of a corporation with all their
  PAP of the month. A member who changed corporation in the month appears with all their PAP in the new
  corporation's list, while the old corporation's total keeps the PAP earned there.
- **Rank** is a competition ranking by the member's PAP (1, 2, 2, 4). Members without PAP have no rank.
- Member and inactive lists have 100 rows per page; filters, sorting and the month stay when you page or
  change the month. **Inactive** is the members list reduced to members below the member minimum.
- **Exports**: the corporations, members, a member's fleets, FC statistics, PAP history and FC incentive
  periods have **Excel** and **CSV** buttons. A file holds what the page shows for the same month and filters.
  CSV is UTF-8 with a byte order mark, so Excel and WPS read Chinese names without an import dialog; text
  starting with `=`, `+`, `-` or `@` is written with a leading apostrophe. The Excel file needs no add-on and
  has an **About** sheet (dataset, scope, month, export time, who exported, filters, target, member minimum
  and the counting rule). Files stop at 200000 rows with a note on the last row.
- **PAP history** (My History, the corporation history, the alliance history) lists the PAP rows within the
  retention period with a month filter. A corporation's history lists the PAP earned while the main was in the
  corporation.

## Special Roles

Roles an FC gives to pilots of a fleet, after the fleet was created and also after it closed, to characters
recorded in the fleet (tracked members, PAP link registrations and characters with PAP). A new site starts
with Back Seat FC (gives FC credit), Logi Anchor and Snowflake Member.

| Field | Meaning |
|---|---|
| Name | Shown on the fleet page. |
| Gives FC credit | The holder gets the fleet as an FC fleet for FC points and incentives. The pilot needs no FC permission for that, and still cannot manage the fleet without `fleetops.manage_own_fleet`. A change applies to new assignments; remove and add an assignment again to apply it there. |
| Active | FCs can assign only active roles. |
| Sort | Order in the lists. |

## Comms Presets

Voice choices FCs pick on Start Fleet.

| Field | Meaning |
|---|---|
| Name, Channel | Shown in the ping and MOTD (`comms`, `comms_channel`). |
| Voice link | `mumble://`, `ts3server://`, `discord://` or `https://` link. A voice client link cannot be clicked in Discord or in game, so pings and MOTDs link to the preset's **join page** (`/fleetops/comms/<token>/join/`), which opens the voice client; `http` and `https` links are used directly. Do not put a personal user name or password in the link; the Administration page removes them from the address and the Django admin refuses them. A password written in the path or in the query (`?password=...`) is not recognised, and the join page is public: leave such a password out of the link. |
| Description | Free text. |
| Active | Only active presets are offered, and only active presets have a working join page. |
| Issue a new join link | Makes the old join link stop working. |

The join page is the only FleetOps page that works without a login. It needs `"fleetops"` in
`APPS_WITH_PUBLIC_VIEWS` and the join links need `SITE_URL`. A preset whose voice link is not one of the
supported kinds (for example a bare `ts.example.com:9987`) is marked "Not a supported voice link"; pings and
MOTDs show only its name until the link is changed.

## Logi / Boost Channels

In-game channels shown in the MOTD (`logi_channel`, `boost_channel`).

| Field | Meaning |
|---|---|
| Name, Type | Type is Logi or Boost. |
| In-game link | In game, right-click the channel, copy its link and paste it here. It fills the channel ID and, if empty, the channel name. |
| Channel name | The name shown in the MOTD. |
| In-game channel ID | The `player_…` ID that turns the channel name into a link in the MOTD. Clear it to remove the link. |
| Active | Only active channels are offered. |

## Ping Channels

A ping channel is a Discord channel reached through a webhook. FCs choose it on Start Fleet by its name.

| Field | Meaning |
|---|---|
| Channel name | Shown to FCs. |
| Webhook URL | The webhook of the Discord channel. Write-only: the page never shows a stored URL. Leave the field empty when editing to keep the URL, or paste a new one to replace it. |
| Allow @everyone, Allow @here | Whether FCs may use these mentions in this channel. |
| Allowed roles | The ping roles FCs may mention here. None ticked means every active role. |
| Groups | The Alliance Auth groups whose leaders may use this channel. Only groups that have a row under [Groups](#groups) are listed. |
| Alliance FCs can use this channel | Only matters while at least one group is ticked: untick it to reserve the channel for the groups. FC leads (`manage_fleets`) can always use every channel. |
| Active, Sort | Only active channels are offered; sort sets the order. |

**Who may use a channel.** A channel without a group is an alliance channel: everybody with `start_fleet` can
use it, as before. Once groups are ticked, group leaders can use the channel for their group, and normal FCs can
only use it while **Alliance FCs can use this channel** is ticked. A group leader can never use an alliance
channel (a channel without groups) or the channel of another group, unless they also hold the normal FC
permissions. The mention rules of the channel (allowed roles, `@everyone`, `@here`) apply to group leaders as to
everybody else. The groups of a channel are set on this page only; Django admin does not show them. If the last Alliance Auth group of a
channel is deleted in Alliance Auth, the channel is an alliance channel again, and the audit log records it.

Only URLs of the form `https://discord.com/api/webhooks/<id>/<token>` are accepted. The hosts
`ptb.discord.com`, `canary.discord.com` and `discordapp.com`, an API version segment
(`/api/v10/webhooks/…`) and a query such as `?thread_id=1` are accepted too. The URL is checked when it is
saved and again before every send, so a webhook stored earlier that does not match is not used until its URL is
fixed.

Leaving the ping channel empty on Start Fleet ("No Discord message (copy only)") is valid: the Discord step is
skipped and the ping stays available to copy by hand. The demo data does not create a ping channel, because a
channel needs a real webhook.

## Groups

Squads and interest groups are Alliance Auth groups. A row under **Groups** gives the **leaders** of one group
rights in FleetOps. The leaders are the group leaders that Alliance Auth records for the group (Alliance Auth's
group management) and the members of its leader groups; the page shows them but cannot change them
(staff accounts get a button to the Alliance Auth page of the group). Having the Alliance Auth permission
`auth.group_management` alone does not make anybody a leader. Group leaders need only `fleetops.basic_access`:
the row is the grant. Only alliance administrators (`manage_configuration`) change these rows, and group leaders
cannot change any setting.

If your alliance records no group leaders in Alliance Auth and keeps two Alliance Auth groups per small group
(for example "Example Squad members" and "Example Squad leaders"), switch on **All members of this group count as its
leaders** on the row of the leaders group. See [Two groups per small group](#two-groups-per-small-group).

| Field | Meaning |
|---|---|
| Alliance Auth group | The group (one row per group). |
| All members of this group count as its leaders | Off by default. When on, every active member of this Alliance Auth group who holds `fleetops.basic_access` is a leader of it in FleetOps, with the same rights as a leader recorded in Alliance Auth. Group leaders and leader groups recorded in Alliance Auth keep working beside it. The leaders card on the edit page then lists the members (read only). Tick it only for the leaders group: on a members group every member would be a leader. The group must be marked Internal in Alliance Auth, otherwise the setting is refused: people can join other groups on their own, and anyone who can change the membership of the group (an Alliance Auth administrator, `auth.group_management`, a group leader of it) decides who leads it. Add and remove people in Alliance Auth: it counts from their next request. |
| Leaders may | **Ping only**: the leaders use **Send Ping** for the group's channels; no fleet record, no PAP. **Pings and fleets with PAP**: they can also start fleets (not PAP-link fleets or pre pings), limited to the fleet types ticked here. The ticked types are kept when you switch back to pings only. |
| Fleet types | The fleet types the leaders may start. A fleet type with the weight 0 gives no FC points, so a group fleet of that type does not change the FC incentive. |
| SRP link for group fleets | Group fleets get an SRP link like alliance fleets, and the leaders get **My fleets** in the SRP menu. |
| Is active | While it is off, the leaders of the group can do nothing in FleetOps. |
| Note | Free text for administrators. |

Group fleets count in the alliance statistics like any fleet of their type (PAP is granted, FC points follow the
fleet type's weight; a weight 0 means no FC points and no effect on the monthly incentive minimum). Pings sent
without a fleet change nothing in the statistics.

Saving a group that runs fleets of a type with a weight above 0 shows a notice (leaders get FC credit for those
fleets). A group without a ping channel shows a notice too: assign it under [Ping Channels](#ping-channels).

### Two groups per small group

Some alliances do not record group leaders in Alliance Auth. Each small group has two Alliance Auth groups instead:
"Example Squad members" for everybody in the squad and "Example Squad leaders" for the people who lead it. FleetOps
handles this without any change in Alliance Auth:

1. In Alliance Auth, make sure the people who lead the squad are members of "Example Squad leaders" and hold
   `fleetops.basic_access` (through that group, their state or another group). Nothing else is needed there; no
   group leaders have to be recorded.
2. Open **Administration** > **Groups** and add a row for "Example Squad leaders" (not for the members group).
3. Switch on **All members of this group count as its leaders**. Choose **Leaders may**, the fleet types and the SRP
   switch as for any other group, and save. A notice shows how many active members who hold FleetOps access count as leaders now.
4. Open **Administration** > **Ping Channels**, edit the channel of the squad and tick "Example Squad leaders" under
   **Groups**. Only groups that have a row are listed.
5. Check the edit page of the row: the leaders card says "All members of Example Squad leaders" and lists them. A
   member who lacks FleetOps access is marked.

Adding someone to "Example Squad leaders" in Alliance Auth gives them these rights on their next request; removing
them takes the rights away on their next request. Members of "Example Squad members" get nothing, because that group
has no row. Inactive accounts and accounts without `fleetops.basic_access` never count. Every change of the row is
in the audit log.

Deleting an Alliance Auth group that has a row removes the row. The audit log records it ("Alliance Auth group
was deleted", without an actor, with the old settings and channels), channels that belonged only to this group
become alliance channels again, channels of other groups stay as they are, and earlier group fleets and pings keep
the group's name. The same happens when the group is deleted in the Django admin by someone who may delete groups: the access row does not stand in the way, and the audit log gets one entry.

### Send Ping

Leaders of a group (and FCs) send pings without a fleet from **Send Ping**. The page offers only the channels the
person may use, shows a preview that looks like Discord, and asks before the ping is posted. The same channel
accepts one ping every 60 seconds, counted together with the pings of fleets (a fleet ping holds the channel for a ping without a fleet and the other way round; if the start ping of a fleet finds the channel cooling down, the step shows the wait and **Retry Ping** posts it later; the pre ping of the same fleet counts too, so a scheduled fleet started within a minute of its pre ping shows the wait), a double click sends one ping, and an answer that does not arrive within five
minutes is shown as "Not confirmed". **Recent pings** lists the last 30 days (own pings and the pings of the
groups the person leads; people with `view_audit_log` see all) with **Use again**. Every send is audited as
`ping.send` (channel, group, targets, status, HTTP status; never the webhook address). The texts of pings sent
without a fleet are deleted after **Data retention days**.

## Ping Roles

Discord roles FCs may mention. Add a role by its ID (a plain number or `<@&id>`), or click **Sync roles from
Discord** when the Alliance Auth Discord service is installed and `DISCORD_GUILD_ID` is set. Synced roles start
inactive: tick **Active** for the roles FCs may ping. Roles that disappear from Discord are marked "Not on
Discord" and switched off. A role that a ping channel allows cannot be deleted; remove it from the channel or
switch it off instead.

Discord notifies a role only when the role is mentionable (Discord server settings, Roles) or when the webhook
may mention every role. FleetOps sends each ping with `allowed_mentions` naming exactly the selected target,
so other `@everyone`, `@here` or `<@&id>` text in the ping notifies nobody.

## Message Templates

The Ping, MOTD and Ping without fleet texts. Variables, the built-in templates and the rules are in
[message templates](message-templates.md).

## Doctrine Settings

**Administration, Doctrine Settings** (`fleetops.manage_configuration`, no permission of its own) holds three things for
the doctrines of a doctrine app such as Fittings: who may ping which doctrine, who may use a custom doctrine name, and
the order of the ships. The page is one form with one **Save**; the Django admin lists the same records
(**Doctrine accesses**, **Doctrine settings**) as a fallback.

### Who may ping which doctrine

Every doctrine has a multi-select of Alliance Auth groups.

- **No group chosen (the default for every doctrine, and after an upgrade): everybody who may start fleets can ping the
  doctrine**, as before.
- **Groups chosen: only members of one of those groups can ping it** (one group is enough; a person in several groups
  needs only one allowed group). Doctrines with groups carry the pill **Restricted**.
- **Superusers and everybody with `fleetops.manage_configuration` (the permission of the FleetOps administration, the
  same one this page needs) are never restricted.** There is no permission of its own for this.
- The rule is one function used by every entry point: **Start Fleet** (also with a group: a group leader is checked by the
  Alliance Auth groups they belong to, not by the group the fleet is for), **Send Ping** (a ping without a fleet),
  **scheduled fleets** (pre ping) and **start presets**. The lists show only the doctrines the person may ping, and the
  server checks the choice again, so a doctrine ID put into a request by hand is refused with *You may not ping this
  doctrine, ask an administrator.* The refusal is also made by the start, schedule and send services, not only by the forms.
- **Scheduled fleets** are checked again when the fleet starts (FleetOps has no timer that starts a fleet; the FC who
  scheduled it starts it from the fleet page, and that start is the check). If the FC may not ping the doctrine any more,
  the fleet is **not started** (it stays scheduled, nothing is asked from ESI), the FC gets an Alliance Auth notification
  in their language and an audit entry `fleet.schedule_start_refused` is written. Editing a scheduled fleet keeps the doctrine
  it already has even if the editor is not in the group (an edit of the time must not fail); changing it to another
  doctrine is checked.
- **Presets**: saving a preset with a doctrine the person may not ping is refused with the same text. A preset that
  already holds such a doctrine says so when it is loaded (*Doctrine '…' of this preset: You may not ping this doctrine,
  ask an administrator.*) and leaves the choice empty: it is never switched to another doctrine and never turned into a typed
  name.
- **Not restricted**: SRP links and the fleets with a PAP link (they post no ping), and **Regenerate and push MOTD** or
  the retry of a ping of a fleet that is already running (the doctrine was checked at the start).
- Fittings keeps deciding who sees a doctrine (the groups of its categories); this is an extra filter on top of it, and
  the doctrines stay visible in Fittings.
- A record of a doctrine that left the doctrine app is kept without an error and listed under **Doctrines that no longer
  exist**; it restricts nothing. Clear its groups to empty it. Deleting an Alliance Auth group takes it out of every list; a
  doctrine whose last group was deleted is open to everybody again.
- Every change writes `configuration.create` or `configuration.update` (object type `DoctrineAccess`) with the doctrine
  name and the group IDs and names before and after.

### Who may use a custom doctrine name

**Custom doctrine name** is the option **None / Custom** of Start Fleet, scheduled fleets, Send Ping and presets.

- **Everybody who may start fleets** (the default, as before), or **Only the chosen groups** with a multi-select. Switching
  back to everybody keeps the chosen groups for later. Superusers and administrators are never limited.
- Without the right the option **None / Custom** and the text box are not shown, and the server refuses a typed name
  (*You may not use a custom doctrine name, ask an administrator.*) and a request that names no doctrine. If there is no
  doctrine at all to choose from, **None** stays so nobody is locked out, but a typed name is still refused.
- Editing the doctrine name of a running fleet needs the same right (a record of a manual fleet does not).
- A scheduled fleet with a typed name is checked again when it starts, like a restricted doctrine (the notice and the audit
  entry say it was the custom name). A preset with a typed name says so when it is loaded.
- Choosing **Only the chosen groups** without any group is allowed (only administrators can type a name; the page warns).
- Changes write `configuration.update` (object type `DoctrineSettings`) with the groups before and after.

### Ship order

- Without a stored order the list is the hulls of the doctrine, each once, in the order of the doctrine, joined with
  ` > `. Two Moa fits give one Moa.
- The editor lists the ships of the doctrine, each hull once. Move a ship with its up and down buttons (they post the
  page, so they work without JavaScript; the result is kept only after **Save**) and choose between every two ships
  one of three marks: `>` higher priority, `=` same priority, `>>>` much higher priority. The preview line shows the
  list as it will be written, for example `Minokawa = Lif > Raven >>> Basilisk`.
- **Back to the automatic order** deletes the stored order.
- The server accepts only those three marks, only ships of the doctrine as it is now, and each ship once. One order is
  stored per doctrine (by the doctrine source and its ID, like the fleet record).
- The order is applied when a ping or MOTD is made, so an open fleet shows a change at its next MOTD or ping (**Regenerate
  and push MOTD**); nothing in the fleet record changes. A ship that left the doctrine is left out (and dropped for good at the
  next save); a ship added to the doctrine later is appended at the end with `>`. When a ship is left out, the mark that
  stood in front of the next ship stays.
- The marks are written as plain text in both messages. In the MOTD `>` is written as it always was in this list,
  so the game shows `A > B`. In Discord the marks come after a ship name inside the line, where `>` is no quote.
- The page lists every doctrine with **Edit order**. Creating, changing and deleting an order write `configuration.create`, `configuration.update` and
  `configuration.delete` entries (object type `DoctrineShipOrder`).

Doctrine names with square brackets: in a Discord ping the names that are the text of a link (`doctrine`, `doctrine_name`
and `comms`) get a backslash in front of `[`, `]` and `\`, so Discord shows the name as typed inside the link. The MOTD and
the other uses of the variables are not changed.

## Ship functions and rules

**Ship Functions** are the groups of the fleet composition. A new site starts with Fleet Command, DPS,
Logistics, Tackle / EWAR, Interdictors, Boosts, Command Destroyers, Support, Cyno, Newbro and Capsules.

| Field | Meaning |
|---|---|
| Name, Colour | Shown in the composition. The colour is a `#rrggbb` value. |
| Fleet positions | Comma-separated fleet positions (`fleet_commander`, `wing_commander`, `squad_commander`) that count here whatever they fly. |
| Default | Ships that no rule matches count here. At most one active function is the default. Recognising ships needs django-eveonline-sde. |
| Active, Sort | Inactive functions are ignored. |

**Ship Function Rules** put a ship type or a whole ship group under a function. Enter the SDE ID, or the exact
English name (names need django-eveonline-sde). A member counts under the first match of: fleet position, ship
type rule, ship group rule, the default function. A type or group can be mapped only once.

Without django-eveonline-sde the composition counts functions from fleet positions and ship type rules only,
lists the other ships as unclassified, and shows "Ship classes need django-eveonline-sde." and "Bridgeable
mass needs django-eveonline-sde." instead of those blocks. Ship types can still be entered by ID.

## Fleet Structure Templates

Wing and squad layouts an FC can apply to a running fleet from its Fleet tab. The layout has one wing per
line: `Logi: Logi 1, Logi 2`. Names are up to 10 characters; a fleet has up to 5 wings with up to 5 squads
each. Lines starting with `#` are comments. A new site starts with one template, "Standard" (a main wing with three
squads, plus logistics, boosts and tackle wings).

## FC incentives

FC incentives are off until **Incentive enabled** is ticked. While it is off, the FC Incentive menu entry is
hidden and periods cannot be reviewed, recalculated, waived, finalized or unlocked, even with
`fleetops.manage_incentives`. FC statistics are not affected.

A fleet gives its FC, and every special-role holder whose role gives FC credit, its FC points: the fleet type
weight (copied when the fleet started) times the PAP multiplier. A fleet with a pending multiplier choice counts
at 1x. Only counted fleets (closed, approved, not excluded) give points.

For each month, on the **FC Incentive** page, an administrator sets a budget and the minimum number of fleets:

```text
eligible = fleet_count >= minimum_fleets
Payout = Monthly Budget × FC Points / Total Eligible Non-Waived Points
```

Once an FC is eligible, all fleets of that month count, including the first ones that reached the threshold.
Each share is rounded down to whole ISK; any remainder goes to the eligible, non-waived FC with the highest
points (the lower user ID wins a tie). An FC can be waived for a month. A period moves from open to review to
finalized; a finalized month must be unlocked before it can be recalculated or a waiver changes. Finalized
periods keep their numbers.

## FleetOps SRP

FleetOps has its own ship replacement program. It is off until you enable it, and it replaces neither aa-srp
nor Alliance Auth SRP: both stay optional link providers ([SRP providers](srp-providers.md)). Members claim a
loss, an SRP manager reviews it, pays it in game and marks it paid. FleetOps keeps the amounts, limits and
history; it never moves ISK.

**Administration** > **FleetOps SRP** > **SRP Settings**:

| Setting | Default | Meaning |
|---|---|---|
| Enabled | off | Members can submit claims, and fleets get FleetOps SRP links. |
| Out-of-fleet losses | on | Allow claims for losses outside fleets. Off limits claims to the SRP links of fleets. |
| Losses inside a tracked fleet | Switch to the fleet link automatically | What happens when a member checks a loss as a loss outside a fleet (from My SRP) but the character was in, or within the polling grace of, a fleet that FleetOps tracked, and the SRP link of that fleet is open. **Switch to the fleet link automatically** calculates the loss with that link and the fleet rules (the member sees which fleet, and can choose **Submit as a loss outside a fleet anyway** with a reason that the reviewer sees). **Block it and ask for the fleet link** shows the check without a submit button and offers **Submit with the link of** the fleet. **Only suggest the fleet link** shows a hint and keeps the loss outside a fleet. Only a loss that fleet tracking places in the fleet switches: a PAP registration alone does not, a closed link does not (the loss stays outside a fleet and the check says the link is closed), and a member who may not claim through that link is not switched. A link that closes between the check and the submit gives an outside-fleet claim with the warning "The SRP link of the fleet was closed before this claim was submitted"; that claim follows the rules of a loss outside a fleet (with out-of-fleet claims switched off it is refused as such; a required category that was not chosen is left empty). **Submit as a loss outside a fleet anyway** is offered only when the loss passes the outside-fleet checks, otherwise the page says why. A check made before the setting was changed to **Block** is refused at submit when the loss lies in an open tracked fleet. 0.2.x and the first 0.3.0b1 builds behaved like **Only suggest**; set it back to get that behaviour. |
| Claim deadline (days) | 0 | Losses older than this cannot be submitted (checked when a loss is submitted, submitted again or moved to another loss). 0 means no deadline, and a site that upgrades keeps the value it had. Members are told the number of days on My SRP and on the SRP pages of links. Reopening a claim is not checked against the deadline. Changing the deadline does not make pending claims "calculated with earlier settings". Making the deadline longer brings back the found losses that were marked too old: saving the setting removes those rows and the next scan reads the characters again. |
| Use zKillboard | on | Use zKillboard for killmail hashes and values. Off means only in-game links work, and **Value source** must be the ESI estimate. |
| Value source | zKillboard total value | zKillboard total value (includes cargo; this is what aa-srp pays), zKillboard fitted value (ship, modules, drones, implants; no cargo), or the ESI market estimate. The estimate is also the fallback when zKillboard has no value. The value only matters for ships that are not in the payout table. Sites that never saved their SRP settings and have no claim get the total value when they upgrade to 0.3.0b1; a site that saved its settings once keeps its choice. |
| Payout percentage | 0 | Share of the loss value paid for ships not in the payout table (rounded down to whole ISK). 0 gives no automatic amount, and the reviewer decides. A [category](#srp-categories) can have its own percentage. |
| In-fleet loss cap, Out-of-fleet loss cap | empty | Most one loss can pay, whatever its rule or percentage. Type amounts the way players write ISK (`250m`, `1.2b`). Empty means no cap. A [category](#srp-categories) can have its own cap, or none. |
| A category claim needs no standing link | on | A member who chooses a category for a loss on a day when the category has no open [standing link](#srp-links) can still submit; the claim only records the category. Off requires the link: the check then says "This category has no open SRP link for the day of the loss." |
| Default category | empty | The category of a loss outside a fleet when nothing else is chosen. Only an active category that members can choose is offered. Empty means no category: the claim follows the general settings. |
| Members must choose a category for losses outside a fleet | off | Without a default category, a claim outside a fleet cannot be checked until a category is chosen. Losses in a fleet always get their fleet's category. The settings page refuses to save this switch while there is no default category and no active category that members can choose, because then nobody could submit a loss outside a fleet; the same check stops deactivating, hiding or deleting the last such category (also in the Django admin). The checks only apply while losses outside a fleet are switched on and refuse only the save that turns a working configuration into that state: a site that is in it already can still save its other settings (the claim deadline, for example) and edit its categories, and a save that leaves the state is always accepted. A loss inside a tracked fleet never needs a category; only a loss that ends up outside a fleet is refused. A site that is in that state already (after an upgrade) shows a warning to administrators on the SRP settings page and on My SRP, and members see "No category can be chosen for a loss outside a fleet right now". Turn the switch off, choose a default category or make a category choosable by members. |
| Over limit action | Send to review with a warning | What happens to a claim that does not fit the remaining monthly limit: **Send to review with a warning** keeps it and flags it red. **Reject automatically** cuts a claim that fits only partly to what is left (amber warning, normal review) and rejects it, with the cap in the reason, only when nothing is left. |
| Personal monthly cap | empty | Most one player (all characters) can get approved per EVE month. |
| In-fleet losses count toward the personal cap | on | |
| Corporation monthly cap | empty | Default cap per corporation and EVE month. [SRP Corporation Limits](#srp-corporation-limits) override it. |
| In-fleet losses count toward the corporation cap | on | |
| Default length of an SRP ban (days) | 7 | Preselected when a reviewer makes an [SRP ban](#srp-bans). Between 1 and 3650, and not longer than the longest ban. |
| Longest SRP ban (days) | 365 | The longest ban a reviewer can make for a number of days. A permanent ban is not limited by it. |
| Ban members who keep submitting over their monthly cap until the end of the month | off | See [SRP bans](#srp-bans). |
| Find losses automatically | off | Read the recent losses of the members' characters from ESI and list them on My SRP. See [SRP loss discovery](#srp-loss-discovery). Needs the scope `esi-killmails.read_killmails.v1` in the EVE application of the site. |
| Minutes between two reads of a character | 60 | Between 15 and 1440. Each authorized character is read about this often (a read is spread over up to ten percent more). |
| Members must authorize loss reading for all their characters | off | A member with a character that did not authorize loss reading cannot check or submit claims until it does. Only in force while **Find losses automatically** is on. |
| Notify members | on | Send an Alliance Auth notification when a claim is approved, changed, paid, rejected, revoked or reopened. |
| Notify members when a payment is undone | off | Also tell the member, with the reason that was given, when a payment is undone (the claim goes back to approved). Only works while **Notify members** is on as well; with that off nothing is sent. The notification is in the member's language. Changing it is audited like every SRP setting. |
| SRP reviewers may decide on their own claims | off | Normally another reviewer decides on a reviewer's own claims. Allow this for small alliances. |
| SRP payers may mark their own claims paid | off | Normally another payer pays a payer's own claims. Allow this for small alliances. Before 0.3.0b1 the setting above also covered payments; the upgrade copies it here, so a site that allowed one keeps allowing the other. |
| The approver of a claim may not pay it | off | Four-eyes rule: neither the person who approved a claim nor anyone who changed its approved amount can mark it paid, another payer has to (claims imported from aa-srp, and claims without recorded approval events, count only their last decider). Undoing a payment is not bound by it. |
| Complete SRP links automatically | off | A closed [SRP link](#srp-links) is marked completed as soon as none of its claims waits for review or payment. A completed link whose claim becomes unsettled again (payment undone, claim reopened or submitted again) goes back to closed whatever this says. |
| Members can change their pending claims | on | A member can change the comment, the category or link and the loss of a claim while it is pending. See [Changing a pending claim](#changing-a-pending-claim). Off removes the button and refuses the request. |
| Members can replace the loss of a pending claim | on | Only in force while the switch above is on. Off leaves members the comment and the category or link. |
| Days of rejection history shown to reviewers | 90 | The claim page tells reviewers how many of the member's claims were rejected in the last days (0 = all). Between 0 and 3650. |
| Show the insurance estimate | on | Next to the amount of each claim FleetOps shows what the pilot would get from insurance, from the prices ESI publishes. Informational only. See [Insurance estimate](#insurance-estimate). |
| Insurance level shown | platinum | One of basic, standard, bronze, silver, gold and platinum. |
| Member instructions | empty | Free text, up to 2000 characters, shown on My SRP and on the SRP page of a fleet. |

The group **Not covered** of the settings page refuses losses when a claim is checked. **Nothing is refused on a new
installation and none of these lists is filled in by an upgrade**; everything a site could submit before it can still
submit:

| Setting | Default | Meaning |
|---|---|---|
| Capsules need a payout rule | on | Capsule losses are paid only from a capsule entry of the payout table. The value of a pod includes its implants, so neither the percentage nor a doctrine rule covers a pod. |
| Ship groups that are not covered, Other ship group IDs | empty | Losses of ships in the ticked ship groups are refused ("Mining Barge ships are not covered."). The list offers the ship groups of django-eveonline-sde, or a built-in list without it. A group ID that the list does not offer goes into **Other ship group IDs**: with django-eveonline-sde an ID that is not a ship group is refused, without it the ID is saved and the page says it could not be checked. |
| Ships that are not covered | empty | Ship type IDs, separated by commas, for ships that sit in a group that is otherwise covered (the Venture is a frigate). The page names the ships that are in force. |
| Kinds of space that are not covered | empty | High security (0.5 and above), low security (0.1 to 0.4), null security (0.0 and below), wormhole space, abyssal space and Pochven. A loss in a ticked kind of space is refused ("Losses in high security space are not covered (Jita 0.9)."). |
| A payout table entry for the ship lifts the ship block | on | A ship that has its own entry in the [payout table](#srp-payout-table) (for the claim's scope, fleet and category) is covered although its ship group or the ship is on the lists. Kinds of space are not lifted. |
| Wartime losses in high security are covered | off | A loss whose killmail names a war is not refused for high security even when high security is ticked. Off means such a loss is refused like any other. |

**Fill in the recommended list** (a button in that group) adds the recommended lists to what is entered and shows the
page again. It saves nothing and writes no audit entry: check the lists and press **Save SRP Settings**, which writes
the usual `configuration.update` entry with the old and new values. The recommended lists are the mining, industrial
and exploration ship groups (Mining Barge, Exhumer, Hauler, Deep Space Transport, Blockade Runner, Freighter, Jump
Freighter, Industrial Command Ship, Capital Industrial Ship, Expedition Frigate, Expedition Command Ship, Prototype
Exploration Ship), the ships Venture, Heron, Imicus, Magnate and Probe, and high security. Shuttles, Navy exploration
frigates and the Pioneer ships are not part of it; add what your site needs.

The check looks at what FleetOps stored with the killmail when it was first read: the ship group, the security status
and the region of the solar system. A value that is not known (the lookup failed, or the killmail was stored before
0.3.0b1) **refuses nothing and adds no warning**. So a claim that exists and is submitted again after its killmail
was stored by an older version is not checked for the lists. Reopening a claim is never checked either: that is how a
reviewer makes an exception. Members read the lists on **My SRP** and on the SRP page of a link ("Not covered").

Where the data comes from: django-eveonline-sde when it is installed (no ESI request is made then); otherwise the
public ESI reads `GET /universe/systems/{id}`, `GET /universe/constellations/{id}` and `GET /universe/types/{id}`,
which need no token. django-esi keeps each answer as long as ESI's `Expires` header says, and FleetOps keeps what it
learned for 7 days (these are static game data) and a failed lookup for one hour. A lookup that fails never stops a
killmail from being stored. The lookups run when the killmail is stored, never while the SRP lock is held and never in
a page that only shows a claim.

Without django-eveonline-sde the lookup therefore runs inside the first web request that reads the killmail (for example checking
or submitting a claim), with at most three ESI reads (system, constellation,
ship type), each cached as described above; later requests find the stored values. **Installing django-eveonline-sde is
recommended**: with it no ESI request is made and the first request is not slowed down.

To enable FleetOps SRP:

1. Run `python manage.py migrate fleetops` and `python manage.py collectstatic`.
2. Tick **Enabled** on the SRP Settings page and set the value source, the percentage, the caps and the limits.
   Add ships to the payout table and, where a corporation needs its own limit, to the corporation limits.
3. Give `fleetops.manage_srp` to your SRP managers, or split the work: `fleetops.review_srp` for the people who decide
   on claims and `fleetops.pay_srp` for the people who pay them (see [Permissions](permissions.md)).
   `fleetops.request_srp` lets members claim.
4. In **General Settings** choose **SRP provider** *Automatic* or *FleetOps SRP*, and keep **SRP auto create**
   on, so every fleet gets an SRP link when it starts, before the ping and the MOTD.
5. Add `{{ srp_url }}` to custom ping and MOTD templates. The untouched Default MOTD already has an SRP line.
6. Keep the Celery worker and beat running; they fill in loss values that zKillboard did not have yet.

### Claims, values and limits

- A member claims a loss from **SRP** > **My SRP** (any loss) or from the SRP link of a fleet
  (`/fleetops/srp/link/<code>/`, in the MOTD and on the fleet page). The member pastes an in-game killmail link
  (works at once), a zKillboard link (zKillboard has a kill only after about 5 minutes), an `esi.evetech.net`
  killmail URL, or the killmail ID and hash. FleetOps reads the killmail from ESI. The victim must be one of the
  member's characters; the claim belongs to the player and counts for the main character's corporation at the
  time of the claim.
- One killmail is one claim, however often and however it is pasted. The pilot can withdraw a pending claim and
  submit it again.
- A loss younger than 6 minutes is not asked about at zKillboard. The claim waits with a "value not available
  yet" warning, and the value sweep fetches it later (after 65 minutes, 3 hours, 12 hours and 24 hours). After
  the fifth try the ESI market estimate is used and the claim says so. The estimate adds the ESI average price
  of the ship and its fitted items (blueprint copies count 0); it is a CCP-wide average, not a Jita price.
- zKillboard etiquette: requests carry a descriptive User-Agent (`AA-FleetOps/<version> (+<project url>;
  <contact>)`, the contact being `ESI_USER_CONTACT_EMAIL`, else `SITE_URL`). At most one request goes out every
  2 seconds for the whole site and the query string is never sent. After a block (a redirect to
  `/api/blocked.json` or HTTP 403) FleetOps stops asking for 60 minutes; after HTTP 429 or 503 for the time
  named in `Retry-After` (15 minutes otherwise); after three failures within five minutes for 15 minutes.
  In-game links, payout-table claims and the ESI estimate keep working meanwhile.
- Killmails nobody claimed are removed after 7 days.
- Warnings never block a claim: a pilot who was not in the fleet at the time of the loss, a value that is
  missing or only an estimate, a ship that differs from the tracked one, a claim over a limit, a killmail also
  claimed in aa-srp or Alliance Auth SRP. Blockers (an SRP ban, a loss of somebody else, a capsule without a rule, a
  closed link, a loss older than the deadline, a ship group, ship or kind of space that is [not covered](#fleetops-srp))
  are shown without a submit button.
- A loss entered outside a fleet that fleet tracking places in a running fleet with an open SRP link is handled by
  **Losses inside a tracked fleet**: by default it is checked and submitted with that fleet's link, so the in-fleet
  rules apply (no out-of-fleet cap). The claim's history says it was switched, or shows the reason the member gave for
  submitting it outside the fleet anyway. A member must be allowed to claim through that link like a member who
  pasted the loss on the link's page (the same checks apply). A loss claimed through a standing link is never switched.
- A claim through a [standing link](#srp-links) is not held back by **Out-of-fleet losses** being off, in any of its
  steps: checking, submitting, submitting again after withdrawing, even when the link was closed meanwhile.
  A withdrawn claim without a link is still refused when that switch is off.
- If the day's standing link closes between checking and submitting, the submit is refused ("This SRP link is closed.")
  and nothing is dropped silently. Submitting a withdrawn claim again under a standing link that has closed since skips
  only the open-link and out-of-fleet checks; an SRP ban and the claim deadline still apply.
- Usage of a monthly limit is the sum of approved and paid claims; claims in review are shown but not
  deducted. A revoked approval releases its amount; a paid claim keeps counting. A claim counts in the EVE (UTC)
  month of the loss, whenever it is submitted or decided. The most restrictive limit wins. Limits are checked
  again under a lock when a claim is submitted or approved. The monthly limits of a [category](#srp-categories)
  count only the claims of that category (in and out of fleets) and are not switched off by the "in-fleet losses
  count" options, which belong to the general limits.

### Review and payment

- **SRP** > **Review** lists claims (pending first, oldest first) with filters. Red rows are over a limit, amber
  rows need a look. A reviewer approves the suggested amount, the amount within the limit, or a typed amount.
  Any other amount needs a reason; more than the policy amount needs "Pay more than the policy amount"; more
  than the remaining monthly limit needs "Approve over the monthly limit". Reject, revoke, undo payment, reopen
  and moving a claim between fleet and out of fleet need a reason. **Change the category** moves a pending claim
  to another category (or to none) and calculates it again with the payout table, percentage, caps and limits of
  that category; it needs a reason, is shown in the claim's timeline ("Category changed") and is audited as
  `srp.claim_recategorize`. Internal notes are never copied into the audit log.
- **SRP** > **Payments** groups approved claims by player and names each group after the player's main
  character as it is now. It has copy buttons for the name, the amount and **Copy name and amount** (the name, a
  tab and the plain number, ready for the in-game window or a spreadsheet). Pay in game, tick the claims, add
  an optional note and **Mark selected paid**. **Mark paid** is refused when the approved amount is not the one
  the payer was asked to send.
- The queue of **SRP** > **Review** can also be filtered by category (including "No category"; a category that is
  switched off is offered too, marked "off"), by link code (the latest 100 links are suggested), by the EVE dates of the
  submission and, through `?user=<id>`, by member. The filters are kept when the month changes and combine with each
  other and with the month. Values that do not check out are ignored. The table shows the category and the link of
  each claim, and a **Banned** badge for members with an [SRP ban](#srp-bans) in force.
- **SRP** > **Payments** has the same kind of filter for the claims to pay: category, link and corporation. The
  group totals, the copy buttons and the count above the list are those of the claims left by the filter. The paid
  claims list shows the category, the link and the payment batch of each claim.
- **By player and by claim.** The payments page has the switch **By player** / **By claim** (buttons that save your choice with a POST; a link with `?view=player` or
  `?view=claim` only shows that view once and saves nothing). **By player** has one card per player with **Copy name**, **Copy amount**, **Copy name and amount**,
  **Copy claim numbers** and a tick box per claim. **By claim** is one flat table, one row per claim that waits for payment, the oldest claim first (not grouped by player): the
  time of the claim (EVE time), the player (main character), the character that lost the ship (with **Copy**), the claim
  number `SRP-<number>` (with **Copy**), the ship, the value of the loss when it is known, the category or link, the
  amount (with **Copy**) and the buttons **Open in game** and **Paid**; the table scrolls sideways on a phone. **Paid**
  pays that one claim: it is the same payment as **Mark selected paid** for one claim, so it makes a payment batch of one
  claim and follows the same rules (a payer's own claim and, with **The approver of a claim may not pay it**, a claim
  the payer approved are refused with the usual message), the same payment note field below the list, the same
  `srp.payment_batch` audit entry and the same notice to the member. The filters, the exports, the totals and the
  tick boxes with **Mark selected paid** work in both views. The last view each payer chose is stored with the payer
  (the table `SrpPayerSetting`, one row per payer, made when they choose); a payer who never chose sees **By player**.
- **Claim numbers.** The claim number `SRP-<number>` is the code to put in the reason of the in-game ISK transfer; it is
  shown on every claim page, in the payment batch and in the lists, and the search of the review list finds it. **Copy
  claim numbers** copies the numbers of the claims of one player card that the payer can pay, separated by spaces. FleetOps
  knows of no documented limit for the length of the reason, so it does not cut or count anything: with very many claims
  the game may cut the text. Paying claim by claim (**By claim**, one number per transfer) is the safe way.
- **Open in game.** Needs the scope `esi-ui.open_window.v1` on the EVE application of the site; the scope is optional and
  only used for this button. A payer (`pay_srp` or `manage_srp`) chooses one of their own characters in the box **Opening
  characters in game** of the payments page: **Add and authorize a character** goes through the EVE login once for that
  scope (django-esi stores the token; FleetOps stores only the character's ID and name), then **Use this character**
  makes it the in-game character. **Remove the choice** forgets the choice and leaves every token alone. The buttons
  show only for a payer whose in-game character has a token with the scope; a payer who never chose a character sees the link **Set up opening
  characters in game**, and a payer whose chosen character needs a new authorization (token expired, revoked or without the scope) sees **Add the opening character again**, which also goes to the box. A click asks ESI (`POST /ui/openwindow/information/`, one call, no retry) to open the information
  window of the character that lost the ship (**By claim**) or of the main character of the player (**By player**) in
  the client of the in-game character. The server takes the claim and `main` or `lost`, never a character number, and
  only for claims the payments page shows (waiting for payment, or paid in the last 30 days). The page shows a short
  message (sent, token missing or expired, ESI error); the game client must be running and logged in with the in-game
  character, and EVE sometimes opens nothing until any character window was opened in the game once. A click writes no
  audit entry (it is not a data change) and logs at debug level only, never a token.
- **Payment batches.** Every payment makes a batch (`PAY-<number>`): the claims a payer marked paid together, the
  number of players, the total and the optional note, which members see in the claim (the note is the payment note).
  The numbers are those of the moment of payment and do not change when a payment is undone later. A single payment
  makes a batch of one. If nothing could be paid (everything was skipped) no batch is left. The batch is audited once as
  `srp.payment_batch` (claims, players, total and the length of the note, the note itself as the reason); every
  claim's payment event names the batch. **Recent payment batches** on the payments page lists the batches of the last 30
  days (50 at most); the page of a batch (payers only; a reviewer without a payment permission gets a 403) lists the
  claims still paid in it, says how many payments were undone since and links to **Undo payment...** on each claim.
  Claims paid before 0.3.0b1 have no batch and show "(before batches)". A batch that stayed empty is deleted by the daily
  cleanup after an hour.
- **Export of the claims to pay.** The payments page offers the approved claims (with the filters of the page) as a
  file, one row per claim or one row per player: whom to pay (the main character as it is now), the character ID, the
  claim, the loss time, the ship, the category, the link and the approved amount; the last row is the total. The dataset
  `srp-payable` is for payers only (`pay_srp` or `manage_srp`) and every download is audited as `srp.export`. The file
  `srp-claims` has one more column at the end, **Payment batch**, filled in for SRP staff only.
- **Review information.** The claim page shows SRP staff, beside the claim, the attackers (how many, players and NPCs,
  the zKillboard solo, NPC and awox flags, the final blow, the attacker with the most damage and the three biggest
  alliances or corporations), the monthly limits as they are now without the claim itself (red when nothing is left; a
  note says how far the suggested amount goes beyond what the tightest limit leaves, the same figures the approval
  checks) and the member's history: claims, rejections by reviewers + automatic rejections, withdrawn claims and the
  rejections of the last days, with the latest three and a link to all claims of the member. Members do not see the
  attackers, the security status of the system, the limits table or the history on their own claims (the system name stays, and the
  reason on the check page for a loss that is not covered keeps the kind of space and its security status). Attackers are stored
  as IDs only (at most the 50 who did the most damage, and always the final blow); names come from FleetOps' name cache and an
  unknown ID is shown as the number; a background task looks the names up after the killmail was stored, a page never calls
  ESI. Killmails stored before 0.3.0b1 show only the number of attackers. `python manage.py fleetops_srp_backfill_killmails
  [--security] [--attackers] [--limit N] [--dry-run]` reads them again (claimed killmails only, one read at a time with a
  pause).
- If the claimant changed a claim after a reviewer opened it, approving, rejecting, moving or changing the category of it
  is refused with "The claimant changed this claim just now. Reload the page and check it again." The bulk approval skips
  such claims and says so. The review page shows when the claimant changed a claim.
- A decision is made on the claim the reviewer looked at: when the suggested amount changed while the page was
  open, the approval is refused with the old and new amount.
- Reviewers cannot decide on their own claims unless "SRP reviewers may decide on their own claims" allows it, and payers cannot mark their own claims paid unless "SRP payers may mark their own claims paid" allows it. With "The approver of a claim may not pay it" on, neither the approver nor a reviewer who changed the approved amount can mark the claim paid either.
- Summaries (**Summary**, **Corporation**, **My fleets**) are by EVE month of the loss and exclude withdrawn
  claims. The files are `srp-claims`, `srp-members`, `srp-corporations` and `srp-fcs`. The alliance summary also shows
  "This month by category" and links to the reports.
- **Reports** (menu **SRP** > **Reports**, `srp/summary/reports/`) show any period, not only a month. Choose a report,
  a period (**From** and **To**, EVE dates with both days included, or a preset: this month, last month, last 3, 6 or 12
  months, this year, all time; dates you enter win over a preset) and press **Show report**. The default is the last six
  months including the running one. A period cannot be longer than 1100 days; "All time" starts at the oldest claim and
  is cut to the last 1100 days with a note. Claims count in the EVE month of the loss, withdrawn claims are left out. A
  report has summary cards (claims, requested, approved, paid, average payout), a table you can search (50 rows per page)
  with the total in the last line, and **Excel** and **CSV** buttons for the same period. Every table shows the average
  payout (approved ISK divided by approved and paid claims). The reports:
  **Trend by month** (one row per month with empty months as zeros, bars for the approved ISK, optionally split by
  category), **Categories** (people, claims, ISK, claims cut by a cap or limit, the used ISK (approved plus paid in the period), the monthly caps of the category and
  what remains of a category-wide monthly cap, only when the period is exactly one calendar month; claims
  without a category are "Uncategorised"), **Ships** (the 500 most lost, with a note when more exist), **Members** (all
  characters of a player under the main), **Corporations** (people, claims, ISK, monthly cap), **Regions** (losses of
  killmails without a stored region are "Unknown" until `fleetops_srp_backfill_killmails` fills them in; names come from
  the name cache and django-eveonline-sde, never from ESI), **FCs** (fleets, claims, ISK and the fleet types led),
  **Reviewers** and **Payers** (SRP staff only; the rules' rejections are one "Automatic" row and imported claims
  without a reviewer or payer one "aa-srp (unknown)" row), **Rejections**, **Payments** (with the payment batch) and
  **Personal limits** (below).
- **Personal limits report.** One row per month, member and limit: one for the general personal monthly cap and one for
  each category with a personal monthly cap in which the member claimed. Columns: used (approved and paid), cap,
  remaining, claims, *over the limit* (claims, not rejected, whose amount a limit cut when it was calculated) and
  *waived* (approved or paid claims whose last approval or amount change went over a limit with the reviewer's
  confirmation, read from the `over_limit_by` figure of that event).
- **Files.** The report files are `srp-trend` (`split=category`), `srp-categories`, `srp-ships`, `srp-regions`,
  `srp-reviewers`, `srp-payers`, `srp-rejections`, `srp-payments` and `srp-limits`; `srp-members`, `srp-corporations`
  and `srp-fcs` got more columns at the end. All SRP files take a period (`from=` and `to=`, `preset=` or `year=` and
  `month=`; the current month without one). Every file is named `...-YYYY-MM-DD_YYYY-MM-DD` after its first and last day, a whole calendar
  month included; the **About** sheet says "Period (EVE time)". Totals of the new files are in the last
  line, not in a data row. `srp-claims` has four more columns after *Payment batch*: *Category*, *Link* (the name of a
  standing link, the fleet's label, or the link name from aa-srp), *Source* and *Security status* (SRP staff only), and
  `category=<key>` filters it (an unknown key is 404).
- The fleet page has an SRP card with the link and a Copy button, **Close SRP link** and **Mark SRP link complete**
  for SRP staff and **Open SRP link** for reviewers (a closed link takes no new claims), **Request SRP for this
  fleet** for pilots, and **Create SRP link** for a fleet without a link. Creating a link twice changes nothing.

### Changing a pending claim

While a claim is pending (and **Members can change their pending claims** is on) the claimant sees **Change claim**
on it: the page offers the comment, a new place (another open fleet the member took part in, an open standing link, or a
category) and a different loss (a pasted link, or one of the recent unclaimed losses of **My SRP**, when losses are found
automatically). The next page shows the claim as it would be, with the new amount; nothing is saved until the change is
confirmed (the preview is valid for an hour and is refused when the claim changed meanwhile). A new place or loss is
checked like a new claim (ban, characters that did not authorize loss reading, deadline, capsule, not-covered ships and
space, open link) and the claim is calculated again with the rules of the new place; the claim number stays. Replacing the
loss moves the claim to the new killmail (the old one is free again and is cleaned up with the other unclaimed killmails
after seven days); a loss another member claimed is refused; a loss of another EVE month uses the monthly limits of that
month and the preview says so. With **Reject automatically**, a change that would leave nothing under a limit is refused
and the claim stays as it was. After a reviewer moved the claim between fleet and out of fleet or changed its category,
the member can still change the comment and the loss. A banned member can only withdraw. Each change counts a revision,
is shown in the timeline ("Changed by the claimant") and audited as `srp.claim_member_edit`; the text of a comment stays in
the timeline and is not copied to the audit log.

### Insurance estimate

With **Show the insurance estimate** on, the claim page and the check page show, for the ship type of the claim,
"compensation X + insurance payout Y = total Z" (compensation: the approved amount of an approved or paid claim, else the
suggested amount; insurance payout: payout minus cost of the chosen level; a dash when ESI has no prices for the ship).
It is an estimate from the current ESI insurance prices and is only paid out if the pilot insured the ship. It is not part
of any amount, limit, export or payment. The prices (`GET /insurance/prices`, public, no token) are read at most once a day
by a task queued from the existing beat task, only while SRP is enabled and the estimate is shown; the stored prices are
replaced as a whole in one transaction, a failed read keeps the old ones (and is tried again after an hour).

## SRP Links

**SRP** > **SRP Links** (reviewers and payers) lists every SRP link: the link of each fleet and the standing links.
A row shows the kind, category, date, state, the claims waiting for review and for payment, and the total paid. The
filters are state, kind, category and a date range in EVE time (a fleet link counts with the day its fleet started);
the list has 50 links per page, and `?codes=<code>,<code>` shows exactly the links it names, ticked.

A **standing link** has no fleet. It has a name, a [category](#srp-categories), an EVE date, an optional doctrine
text, an optional link to the after-action report (http or https only) and an optional time at which it closes.
Members open it at `/fleetops/srp/link/<code>/` like the link of a fleet and submit the losses of that day; a claim
through it is a loss outside a fleet, in the link's category, and no FC statistics count it. Only reviewers make
(**New standing link**) and edit standing links; the name is made from the category's name template when left empty,
and the doctrine text and closing time are filled in from the category. Ticking **Make this the link of the day** takes
the one place per category and day, so a second link of the day is refused. Changing the category of a link does not
move the claims already made: they keep their category. Changing the category or the date of the link of the day
ends its role as the link of the day (the one place per category and day is free again, so the day's link of the
original category can be made again). The link of a fleet can only get a doctrine text and a report, and only a
reviewer edits them: the FC of the fleet gets 403 on the edit page.

**My SRP** has a card **Standing SRP links** with the open standing links (newest date first, then by category, at
most 30). A member who picks a category for a loss on My SRP, without a link, is put under the open standing link of
that category for the **EVE day of the loss** (the newest one when there are several); without one the claim only
records the category, unless **A category claim needs no standing link** is off. A standing link is not limited by
**Out-of-fleet losses**. When the EVE day of the loss is not the date of the link (for example a loss of 11-03 claimed
through the link of 11-04), the claim gets the warning "The loss happened on 2026-11-03 EVE; the link is for
2026-11-04." Batch approval skips a claim with a warning, so a reviewer looks at it. The warning stays when the
fleet presence is checked again and goes when the claim leaves the link.

**Daily links and closing.** A [category](#srp-categories) with **Make a standing link every day** gets a standing link
for the current EVE day: its name from the name template, its doctrine text from the category and, when the
category has **Close links after (hours)**, a closing time of 00:00 EVE of its date plus those hours (48 closes it at
the end of the next day). The same upkeep closes every open standing link whose closing time has passed, also links made
by hand, and writes `srp.link_close` to the audit log without a person as the actor. It only fills in today, never past
days (a beat that was down from 00:00 to 02:10 makes today's link and not yesterday's), makes at most one link per
category and day however many workers run, and does nothing while FleetOps SRP is off, except closing. Closing only stops
new claims; the claims made go on to review and payment. Off by default: a new site makes and closes no link by itself.

**This needs no new beat entry and the app schedules nothing itself.** The upkeep runs inside the task
`fleetops.tasks.schedule_active_fleet_tracking`, which the installation notes already have in `CELERYBEAT_SCHEDULE`
(`fleetops_track_active_fleets`, every minute); the upkeep itself runs at most once every ten minutes. Keep that
entry, the Celery worker and Celery beat running. No migration or code of FleetOps creates a periodic task.

A link is **open** (members can submit), **closed** (no new claims; claims already made go on as usual) or
**completed**. Tick links in the list and choose **Close selected**, **Complete selected** or **Open selected** (reviewers
only); each link is done on its own and the ones that cannot be done are listed with the reason. **Completing** needs a
closed or open link without any claim waiting for review or for payment (approved, not yet paid): pay, reject or
withdraw them first. This rule has no exception and no force option; closing a link is never limited by open claims.
An open link without such claims is closed and completed in one step. Opening a completed link makes it open again.
All steps are audited: `srp.link_create`, `srp.link_update`, `srp.link_open`, `srp.link_close`, `srp.link_complete` and
`srp.link_uncomplete`; the entries hold field values only.

## SRP Categories

A category is a kind of claim with its own rules: fleet losses, solo PVP, a standing doctrine. A claim has at most
one category. A claim without one follows the general SRP settings exactly as before, so a site that creates no
category sees no change.

**Administration** > **FleetOps SRP** > **SRP Categories**. FleetOps creates one category, **Fleet** (key `fleet`),
when you upgrade or install. It holds the losses claimed through a fleet link (the claims already made through
fleets are moved into it when you upgrade). You can rename it and give it a percentage, caps or limits, but not
delete it, switch it off, change its key or let members choose it. It is shown as "Fleet" in English and as the
Chinese word for fleet in the Chinese interface until you rename it. FleetOps creates no other category.

| Setting | Default | Meaning |
|---|---|---|
| Name, Key | | The key is a short identifier (letters, digits, hyphens, underscores); left empty it is made from the name. Both are unique. |
| Members can choose it | on | Members can pick this category for a loss outside a fleet. |
| Payout percentage | empty | Share of the loss value for ships not in the payout table. Empty uses the percentage of the SRP settings; 0 means no automatic amount in this category. |
| Per-loss cap | Use the in-fleet or out-of-fleet cap of the SRP settings | Or **No per-loss cap**, or **Own per-loss cap** with an amount. |
| Per-loss cap also limits payout table amounts | on | Switch off to pay the fixed amounts of the payout table in full while the cap still limits percentage payouts. |
| Personal monthly cap, Corporation monthly cap | empty | Most one player (or one corporation) can get approved in this category per EVE month. Empty means no limit. Checked together with the general limits; the most restrictive one wins. |
| Fleet types | none | Claims through fleets of these types get this category instead of **Fleet**. A fleet type belongs to at most one active category. Leave empty for no mapping. |
| Name template of links | `{category} {date}` | The name of a new [standing link](#srp-links) of this category when none is typed. Only `{category}` and `{date}` can be used. |
| Default doctrine text | empty | Filled in as the doctrine of a new standing link of this category. |
| Make a standing link every day | off | FleetOps makes this category's [standing link](#srp-links) for the current EVE day by itself (see "Daily links and closing"). Off for every category until you tick it. |
| Close links after (hours) | empty | A standing link of this category closes this many hours after 00:00 EVE of its date (1 to 720). Empty means the link stays open until someone closes it. The new-link form fills in the closing time. |
| Active, Sort | on, 0 | A switched-off category is not offered to members; claims that have it keep it and are calculated with its current settings. |

Claims get their category like this: a claim through a fleet link gets the category of the fleet's type (if mapped)
or **Fleet**; a claim outside a fleet gets the category the member chose, else the **Default category** of the
SRP settings, else none. A category that cannot be used any more is refused with a message when the claim is
submitted. Each claim keeps a copy of the category's settings it was calculated with. When a category changes while
claims are pending, the notice "N pending claims were calculated with earlier SRP settings" appears on the category
pages as well; **Recalculate** applies the new settings.

A category cannot be deleted while claims or payout rules use it: switch it off instead. Every change is audited
as `configuration.create`, `configuration.update` or `configuration.delete`. Categories have no Django permissions
of their own; the pages need `fleetops.manage_configuration`, and Django admin shows them to superusers only.

## SRP Bans

A reviewer can ban a member from submitting SRP claims: **SRP** > **SRP Bans** (reviewers only; a reviewer can also start it
from a claim page). The ban belongs to the **Alliance Auth account**, so it covers all characters
of the member; a member who moves to another account is not followed by it.

A ban has a **reason** that the member sees (also in the audit log and in the Alliance Auth notification, when
**Notify members** is on) and an **internal note** that only SRP staff see; the note is never written to the audit log. It
lasts a number of days (1 to the longest ban, 7 preselected) or is **permanent**: a permanent ban has no end date, is not
limited by the longest ban and stays until a reviewer lifts it. Nobody can ban themselves, and a member who is banned
already cannot be banned again until the ban is lifted or over. A ban that has run out needs no task: it simply stops
counting. **Lift ban** (with a reason) ends it at once.

While a ban is in force the member cannot check, submit or **submit again** a claim ("You are banned from submitting
SRP claims until ... EVE: reason"), and **My SRP** shows a red banner with the reason and the end. Claims that wait
for a decision stay as they are and the member can still **withdraw** them, but cannot submit them again while the ban
lasts. When a reviewer ticks **Reject the member's pending claims** while banning (the rejection reason is required), every
pending claim of the member is rejected one by one with that reason, with the usual event, audit entry and notification;
a claim that changed meanwhile is skipped and listed. Approved claims are never touched. Nothing is rejected unless that box is ticked.

**Automatic ban over the monthly cap.** The setting *Ban members who keep submitting over their monthly cap until the
end of the month* is **off** by default. When it is on, a member whose **personal** monthly limit (the general one or
one of a category) is already used up, and who still submits a claim or submits a withdrawn claim again, gets a ban
until 00:00 UTC of the first day of the next EVE month (the month of the day it happens, not of the loss). The claim is
made as usual and treated by **Over limit action**. Not triggered: the preview, a claim that only runs over the limit
(the limit still had some left before it), a used-up corporation limit (not the member's doing), member edits and the
actions of reviewers. The ban is of the kind "Over the monthly cap", audited as `srp.ban_create` without an actor and with the
number of the claim, and a reviewer can lift it like any other. A site without monthly caps never triggers it.

All steps are audited: `srp.ban_create` (member, end, days, permanent, kind, number of claims rejected) and `srp.ban_lift`.

## SRP loss discovery

With **Find losses automatically** on, FleetOps reads the recent killmails of the members' characters from ESI and lists
the losses on **My SRP** under **My recent losses**; a member claims a loss from there without pasting a link. Pasting a
link stays possible for every loss (**Claim with a link**) and is the only way for a character that did not authorize
loss reading. The feature is **off** on a new installation and after an upgrade, and the settings group **Loss
discovery** holds its three settings.

**Set-up.** Add the scope `esi-killmails.read_killmails.v1` to the EVE developer application of the site before you
switch the feature on (the same application that already has the fleet scopes). Without it EVE refuses the login of the
member, and the page shows "Loss reading could not be authorized. If EVE reports an invalid scope, ask an administrator
to add the scope ...". Then every member authorizes each character once: **My SRP** > **Loss reading** lists the
characters (the main is marked) with their state, the time of the last read and an **Authorize** button for each
character without a token. With aa-charlink installed the item **FleetOps SRP loss reading** (for users with
`fleetops.request_srp`, while the feature is on) authorizes all characters in one go; it disappears when the feature
is off. Logging in as a character moves it to the account that logs in (Alliance Auth behaviour); a token for a character
that is not on the member's account is refused and the character is never read.
A token the member still holds for a character that has since moved to another account is left alone (it is not
deleted; token housekeeping stays with Alliance Auth), and the scan only ever uses the token of the character's current
owner.

**What is read.** The scan reads, per character, `GET /characters/{id}/killmails/recent/` (ID and hash only, at most five
pages) with the token of the account that owns the character, and `GET /killmails/{id}/{hash}/` for the killmails it has
not seen. It stores one row per character and killmail: the character's own **losses** (with ship, system, time and, as
for any claim, the ship group, security status and region), **kills** (the character only took part) and rows **older
than the window**. The window is the claim deadline, or 90 days when there is none. Nothing is stored in the SRP
killmail table: a killmail is stored there only when a member checks it for a claim, so the unclaimed-killmail cleanup
and the zKillboard queue are not involved. A killmail that is stored already (with its location) is not read again, and
an imported aa-srp killmail is never read: the loss shows as already claimed. django-esi caches each answer for as long
as ESI's `Expires` says (five minutes for the list), so reading a character again within five minutes costs nothing.

**When.** The existing beat entry `fleetops_track_active_fleets` (task `schedule_active_fleet_tracking`, every
minute) queues the scan task `scan_srp_losses` at most every five minutes while SRP and the feature are on; **no new beat
entry is needed** and FleetOps creates no periodic task. One run stops after 240 seconds, 300 characters, 400 reads of a
list or 600 reads of a killmail or of game data (the module constants of `fleetops/srp/discovery.py`; they are not
settings), at most 30 killmails per character and run. That is enough for about 3600 characters per hour at the default
interval. A character that cannot be read is looked at again later: without a usable token or refused by ESI after
24 hours, after another error after 30 minutes, then 60, 120 and so on up to 24 hours. When ESI says its error limit or a
rate limit was reached the run ends and nothing is queued for 15 minutes.

**Members.** A member can press **Read my characters now** (not more than once in five minutes; the task does the
reading, the page calls no ESI). The characters are read first in the next run; while the general scan is running, the
task tries again every minute for five minutes. Each loss of the list shows the ship, the character, the system, the EVE time, the days
left when there is a deadline, the fleet FleetOps matched it to, and why it is not covered when it is not (from the
values stored with the loss, so the page makes no ESI call). **Claim** opens the usual check with the matched fleet
preselected; the choice can also be an open standing link or a category, or "Outside a fleet". **Ignore** hides a loss from
the member's own list (audited as `srp.loss_ignore` and `srp.loss_unignore`); it does not stop a pasted link. A loss
claimed by somebody else (an imported aa-srp claim included) is shown as "Already claimed" without the name. The list
follows the owner of the character: after a character moves to another account the old owner no longer sees it.

**The fleet match.** For each loss the scan looks for an open fleet SRP link of a fleet the character was in at that time
(the tracking events, with the usual margin: *in fleet*, *near*) or, without tracking, of a manual or PAP-link fleet where
the character has a PAP registration or granted PAP within the fleet's time (*registered*). A standing link is never a
fleet match. The member can choose another fleet; a claim through a standing link is never a claim in a fleet.

**All characters.** With **Members must authorize loss reading for all their characters** on, a member with a character
without a token of its own cannot check, submit or submit again any claim (pasted link, standing link or found loss), and
**My SRP** shows a banner naming the characters with an **Authorize** button for each. Withdrawing is not blocked and a
reviewer's decisions are not checked. The switch does nothing while **Find losses automatically** is off, so switching
the feature off is the way back to pasted links for everybody. With the switch off, characters without a token are not
found automatically; tell your members to authorize all of them, preferably with CharLink.

**Retention.** The daily task deletes kills and rows older than the window one day after they left it, losses 30
days after, and the scan rows of characters that are on no account any more. A row recorded as older than the window is
evaluated again when the claim deadline is made longer: saving the setting removes the rows that fit the longer window (and those without a time), and the next scan reads every character again, so the losses show up again without waiting for the daily task.

**Security.** Tokens are read only with `esi.models.Token` for the owner of the character, used for the one list call and
never stored elsewhere; no log line, audit entry, page or export holds a token or an ESI response header. A failed call
logs the name of the operation, the class of the exception and the HTTP status.

## SRP Payout Table

Fixed amounts. A rule names a ship type (ID or exact English name; it has to be a ship, so a mineral or a module is
refused, and when neither the SDE nor ESI can say whether it is a ship, the rule is saved with a warning), a doctrine (a Fittings doctrine or a custom
name), or both, and applies to losses in a fleet, outside a fleet, or both (**Scope**). A rule can belong to a
[category](#srp-categories) (**Category**); with **All categories** it applies to every claim, with a category only
to claims of that category. The most specific active rule wins: doctrine and ship, then doctrine (any ship), then
ship; at the same level a rule of the claim's category beats a general one, and then a rule for the scope of the claim
beats one for both scopes. The same ship, doctrine and scope can have one rule per category. Doctrine rules match losses in a fleet that has that doctrine. Ships without a rule
are paid the percentage of the loss value. Every claim keeps the rule and the settings it was calculated with;
**Recalculate** applies the current ones to pending claims (up to 50 claims at once while you wait; with more pending claims the
recalculation runs in the background and the page says so). The amount is paid as it is, before the per-loss cap
and the monthly limits. The optional **Label** is shown in the payout table and in the calculation of a claim.

When the SRP settings, the categories, the payout table or the corporation limits change while claims are pending, the
four pages show a notice: "N pending claims were calculated with earlier SRP settings". SRP reviewers can press
**Recalculate** there (it asks first); everybody else sees that an SRP reviewer can recalculate them on the review
page. Claims that were decided already are never changed.

## SRP Corporation Limits

A monthly cap for one corporation, replacing the default corporation cap. An empty cap means no limit for that
corporation.

## Importing the history of aa-srp

`python manage.py fleetops_import_aasrp` copies the SRP history of aa-srp into FleetOps, so that the reports reach
across the day an alliance switches and a loss paid in aa-srp cannot be claimed again. It needs aa-srp installed
(app label `aasrp`) and reads its tables; it never writes to aa-srp. Without aa-srp it stops with `aa-srp (aasrp) is not
installed.` and nothing else in FleetOps is affected (`--undo` and `--enrich` do not need aa-srp, so they still work after it is removed).

**What is imported.**

| aa-srp | FleetOps |
|---|---|
| Approved request | A **paid** claim for the recorded payout (the loss when the payout is 0). The payer is empty, the payment date is the date of the decision, the note says "Imported from aa-srp". There is no payment batch. |
| Rejected request | A **rejected** claim with the reason aa-srp recorded. Its requested amount is the recorded payout (normally 0), not the loss, so the rejection does not add to the requested ISK. |
| Pending request | **Not imported.** Decide on every pending request in aa-srp before the switch: the report counts them and a real run warns when any are left. |
| SRP link | A closed link of the claims imported through it, in FleetOps called an *imported link* (code `aa` and the aa-srp code, no fleet). A link is **completed** when every claim imported through it is paid or rejected and one is paid. Links whose name matches a rule of `--category-map` become standing links of that category and their claims are claims outside a fleet in that category. All other links are fleet links, their claims are in the **Fleet** category and count for the FC of the link when the commander's character belongs to a user. |
| The killmail | A stand-in killmail (no hash, no system, `zKillboard disabled`) that keeps the loss from being claimed again. |

Every imported claim has the source *aa-srp import*, the external ID `aasrp:<request number>`, the payout basis
*Amount as recorded in aa-srp* and an *Imported from aa-srp* event in its timeline (and an audit entry
`srp.claim_import`). The claimant, the reviewer and the main character, corporation and alliance are looked up in
Alliance Auth when the import runs: the creator of the aa-srp request (the owner of the character when the creator is
the "deleted" user), the user who wrote the last status comment (the `creator` of the comment), and the main character the user has **now**, so the
corporation of an old claim is the corporation of the main at the time of the import. A claim whose owner cannot be
found is kept without a user. The time of a loss is the time of its fleet link in aa-srp (the claim says so); use
`--enrich` for the real time. The loss value is taken as aa-srp recorded it, and the value source is set from the
aa-srp setting (total or fitted value).

**Options.**

- `--dry-run` prints the report and writes nothing. Run it first, and again against a copy of the production database.
  The report lists the links and requests read, the requests by month of the link and status (number and ISK, to
  compare with aa-srp), what would be created, changed and left alone, what is skipped and why (no killmail ID, the same
  killmail in another aa-srp request, already a FleetOps claim with its `SRP-` number, pending, ship not found, no
  character), the link names no rule of the category map matched, and how many loss times are estimates.
- `--since YYYY-MM-DD` and `--until YYYY-MM-DD` limit the requests by their posting date (EVE time, `--until` excluded).
- `--category-map PATH` is a JSON list such as `[{"pattern": "(?i)solo", "category": "solo-pvp"}]`; the first rule whose
  regular expression is found in a link's name decides, and the category must exist. Without a map every link is a
  fleet link.
- `--actor USERNAME` names the user the audit entries are written for.
- `--batch-size N` (500): every batch runs in one transaction while it holds the SRP lock, so **a real run blocks every
  SRP submission and decision on the site until it ends: run it in a maintenance window.** The report prints the time.
- Running it again is safe: claims are matched by the aa-srp request number, claims that changed in aa-srp are updated
  (and say what changed in their timeline), unchanged ones are left alone, and **claims that were not imported are
  never touched**. When the same killmail has several aa-srp requests, the approved one wins over the rejected one,
  then the newest; the others are counted as duplicates.
- `--undo` removes everything the import made: the imported claims (their timeline goes with them), the stand-in
  killmails nobody claims any more and the imported links, and writes `srp.import_aasrp_undo`. It deletes only these three: native claims, real killmails (also the ones the import reused) and native links are not touched. `--undo --dry-run` counts. The reason of a rejection is `reject_info`, else the last "Reject Reason" comment, else the text of the status comment. `--undo` takes no filters: combined with `--since`, `--until`, `--category-map` or `--batch-size` the command refuses.
  Claims made in FleetOps are not touched; importing again afterwards gives the same result.
- `--enrich` looks up the real killmail of every stand-in (the hash from zKillboard, then the killmail from ESI) at
  `--rate` lookups per second (0.5, one every two seconds: the pace of FleetOps' own zKillboard slot; a lookup that finds the slot taken waits for it), at most `--limit` (0 = all); it stops with a message while zKillboard is paused (too many failed requests, a request to slow down or a block) and goes on where it stopped when run again; it can be interrupted and run again, and it refuses to run
  while zKillboard is switched off in the SRP settings. It fills in the time, system and attackers of the killmail and
  moves the claim to the real time (which can change its month). It is optional and slow: ten thousand killmails take
  about five and a half hours at the default pace.

**Imported claims cannot be changed.** Nobody can approve, reject, pay, reopen, move, recalculate, withdraw or change an
imported claim, and an imported link cannot be opened again; the claim page says so, shows the amount as recorded in
aa-srp and has no buttons. An internal note can still be added. Imported claims are in the reports, the exports (with
the source *aa-srp import*) and **My SRP**, count toward the monthly limits like any paid claim of their category and
month, and appear in the reviewer report as "aa-srp (unknown)" when no user wrote the decision.

**A loss that was imported cannot be claimed again.** Pasting the link of a stand-in killmail, from zKillboard or from
the game, answers "already claimed". A link from the game (with the hash) also completes the stand-in once from ESI.
The stand-in is never sent to zKillboard for a price.

### Importing a payout table

`python manage.py fleetops_srp_import_payouts PATH --category KEY --column "Amounts"` reads payout rules from a CSV file:
a `type_id` column, a column with the amounts (`--column`, default `amount`) and optionally the English ship name
(`--name-column`, default `name`; without it the ship is looked up). Rows without an amount, or with 0, are skipped; a
rule for the same ship, scope (`--scope`, default `any`) and category is updated, others are created, each with its audit
entry `configuration.create` or `configuration.update` ("Imported from" and the name of the file). Bad rows are listed
with their line number and skipped. Without `--apply` nothing is written and the command only counts; it still looks up the ship names (read only, from the
Eve SDE app or ESI), so a `type_id` that is no EVE item type is listed as a bad row before you apply, and the ships whose name could
not be looked up (ESI not reachable) are named in the output.

## Example: an alliance that moves from aa-srp

These values are one alliance's choices, not defaults. A new installation keeps the defaults of the tables above
(for example the claim deadline is 0, no category exists but **Fleet**, and nothing is required). Enter them by hand
on the pages named; nothing in FleetOps sets them for you.

| Where | Setting | Example value |
|---|---|---|
| SRP Settings | Value source | zKillboard total value |
| | Payout percentage | 100 |
| | In-fleet loss cap | empty (no cap) |
| | Out-of-fleet loss cap | 250m (the fallback for a claim outside a fleet that has no category) |
| | Members must choose a category for losses outside a fleet | on |
| | Default category | none (a category must be chosen, so nothing is filed under the wrong one by accident) |
| | Personal and corporation monthly caps | empty |
| | Over limit action | Send to review with a warning |
| | Capsules need a payout rule | on |
| | Claim deadline (days) | 7 |
| | Ship groups, ships and kinds of space that are not covered | Press **Fill in the recommended list**, check it and save: mining, industrial and exploration ship groups, Venture, Heron, Imicus, Magnate, Probe, and high security |
| | A payout table entry for the ship lifts the ship block | on |
| | Wartime losses in high security are covered | off (such a loss is refused like any other) |
| | A category claim needs no standing link | on |
| | Losses inside a tracked fleet | Switch to the fleet link automatically (the default) |
| | Ban default and longest length | 7 and 365 days (permanent bans are chosen per ban) |
| | Ban members who keep submitting over their monthly cap | off (without monthly caps it would never trigger) |
| | Find losses automatically | on (the scope is in the EVE application) |
| | Minutes between two reads of a character | 60 |
| | Members must authorize loss reading for all their characters | on |
| | Members can change their pending claims, Members can replace the loss of a pending claim | on, on |
| | Complete SRP links automatically | on (a closed link completes only when all its claims are paid, rejected or withdrawn) |
| | Show the insurance estimate, Insurance level shown | on, Platinum |
| | SRP reviewers may decide on their own claims, SRP payers may mark their own claims paid, The approver of a claim may not pay it | left to the alliance; the example does not set them |
| SRP Categories | `fleet` (built in) | Per-loss cap: **No per-loss cap**; percentage inherited |
| | `alliance-fleets`, `home-defense`, `home-defense-ess`, `corp-roaming`, `corp-roaming-ess`, `solo-pvp` | Members can choose it; per-loss cap: **Own per-loss cap**, 250m; percentage inherited; monthly caps empty |
| | Make a standing link every day, Name template of links, Close links after (hours) | on for the six categories above; `{category} {date}`; 48 (the link closes at the end of the next day), or empty to let links stay open |
| SRP Payout Table | | Not used at first; add rules per category when needed |
| Permissions | `review_srp`, `pay_srp`, `request_srp` groups | Not given out by the example; the alliance chooses the groups (nothing is granted by the upgrade) |

With these values, a solo loss of 300m in the `solo-pvp` category is suggested at 250m, and a 571m loss claimed
through a fleet link at 571m.

## Going back to FleetOps 0.2.x

Migrations `0035` and `0036` are the guard of the 0.3.0b1 chain. `0036` adds the pruning share to the
settings; going back runs its copy of the guard before that column is dropped, and `0035` runs the same checks once more.
`0043`, the last migration, creates the tables of the doctrine access (the groups that may ping a doctrine) and of the custom doctrine name setting (undoing it drops both tables and runs the same guard first; no restriction exists after an upgrade, and the groups themselves are never touched); `0042` creates the table of the payers' choices on the payments page (undoing it drops the table and runs the same guard first); `0041` creates the table of the stored doctrine ship orders (undoing it drops the table); `0040` adds the setting **Notify members when a payment is undone**, and `0039` adds **Losses inside a tracked fleet**; both run the guard before they drop their column (so a way back below `0035` is refused before anything is dropped); `0038` only changes a help text and `0037` only adds an index; they are undone next (the help text comes back, the index is dropped, nothing else changes). The
guard checks everything that makes going back unsafe before the first change is undone: payout rules that belong to a category, links without a fleet that have claims (standing
links) or that were imported, and claims imported from aa-srp. It stops **any** way back, a partial one included
(`migrate fleetops 0032` is stopped too). `python manage.py fleetops_srp_rollback_check` runs the same checks, reads only and
changes nothing; it lists rule IDs and link codes and exits with an error while something is in the way. See the
rollback notes of the [changelog](../CHANGELOG.md) for the steps. The target depends on the build you go back to: `migrate fleetops 0023` for 0.2.0b3, `migrate fleetops 0021` for 0.2.0b2 (`0022` and `0023` belong to 0.2.0b3; the 0035 guard covers both ways). Neither build was released, so put the old code back from the wheel or the commit that the site used before.

## History and retention

**Warning: the daily task deletes data.** The beat entry `fleetops_prune_history` (the task
`fleetops.tasks.prune_attendance_history`) permanently removes the rows described below. Set **Data retention days** and
**Member alliance IDs** deliberately, run `python manage.py fleetops_prune_history --dry-run` after every change of
either, and keep database backups. The README section [Data and privacy](../README.md#data-and-privacy) lists everything
FleetOps stores and what is never removed.

`python manage.py fleetops_prune_history` removes PAP older than **Data retention days**, PAP of members who
left the alliances in **Member alliance IDs**, old member events and the old texts of pings sent without a fleet
(their audit entries stay); add `--dry-run` to see the counts first.

**Safety check.** The PAP of departed members is not removed when the setting looks wrong: when none of the **Member
alliance IDs** matches the main character of any user (a wrong digit), or when the users to prune are more than
the share set on the FleetOps settings page under **Largest share of departed members to prune** (1 to 99 percent, default 50) of the users with PAP history. The run then
writes a warning to the log, reports `refused` in the result and the command prints why; the retention of old rows
still runs, and nothing is deleted from the PAP of departed members. Check the alliance IDs, or run
`python manage.py fleetops_prune_history --force` when the pruning is meant. `--dry-run` shows the refusal too. When the pruning of departed members was refused, the command prints the counts as
a warning and exits with an error (status 1) after the retention part has run, so a cron job notices; with `--dry-run`
it exits normally.
The daily task never forces.
The daily beat task `fleetops.tasks.prune_attendance_history` does the same and also deletes SRP killmails
nobody claimed (their location fields go with them) and the losses the scan found that are no use any more (see
[SRP loss discovery](#srp-loss-discovery)). SRP claims with their timelines, payment batches, links and categories are the payment record and are never deleted by retention; the insurance prices are one row per ship that is rewritten daily. SRP bans are never deleted by retention: the reason, the end and
who lifted the ban stay as a history; lifted and expired bans cost nothing. `python manage.py fleetops_refresh_names` rewrites ship and solar system names of fleet member
records in English from the SDE (`--dry-run` available). Ship and solar system names are English everywhere.
