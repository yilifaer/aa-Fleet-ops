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
| [Monthly Targets](#monthly-targets) | Corporation PAP target and member minimum per month |
| [Special Roles](#special-roles) | Roles FCs give to pilots in a fleet |
| [Comms Presets](#comms-presets) | Voice choices and their public join link |
| [Logi / Boost Channels](#logi--boost-channels) | In-game channels shown in the MOTD |
| [Ping Channels](#ping-channels) and [Ping Roles](#ping-roles) | Discord webhooks, allowed mentions, mentionable roles |
| [Message Templates](#message-templates) | Ping and MOTD texts |
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
| Leaderboard places | 20 | Places shown on the leaderboards, 3 to 100. Members tied on the last place are all shown. A viewer outside the shown places sees their own rank. |
| Leaderboard visibility | Everyone with FleetOps access | **Everyone with FleetOps access**: every user sees the alliance board and the board of their own corporation. **Statistics managers only**: `view_corp_stats` sees the own corporation, `view_all_stats` the alliance and any corporation. Without access the menu entry is hidden and the page answers 403. |

See [Statistics rules](#statistics-rules) for how the figures are counted.

### PAP links

| Setting | Default | Meaning |
|---|---|---|
| PAP link validity | 30 | Minutes a new PAP link stays open unless the FC picks another time. 1 to 1440. |

### Fleet tab

| Setting | Default | Meaning |
|---|---|---|
| Live fleet refresh | 15 | While somebody who manages a fleet has its Fleet tab, Members tab or PAP preview open with **Live** on, FleetOps reads the fleet from ESI this often (seconds, 10 to 300). 0 means only the tracking interval and the Refresh button. |
| Ship groups without bridge | Titan, Dreadnought, Freighter, Carrier, Supercarrier, Rorqual, Jump Freighter, Industrial Command Ship, Force Auxiliary, Lancer Dreadnought, Command Carrier | SDE ship group IDs whose mass is not counted as bridgeable. The default follows the titan bridge rule. |

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
corporation needs; empty means no target), a **Member minimum PAP** (default 1; 0 means nobody is listed as
inactive) and a note. An entry applies from its month until the next entry, so a target set for the future
never recolours finished months; edit the older entry to change the past. Without an entry there is no
corporation status and the member minimum is 1.

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
| Voice link | `mumble://`, `ts3server://`, `discord://` or `https://` link. A voice client link cannot be clicked in Discord or in game, so pings and MOTDs link to the preset's **join page** (`/fleetops/comms/<token>/join/`), which opens the voice client; `http` and `https` links are used directly. Do not put a personal user name or password in the link; the Administration page removes them and the Django admin refuses them. |
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
| Active, Sort | Only active channels are offered; sort sets the order. |

Only URLs of the form `https://discord.com/api/webhooks/<id>/<token>` are accepted. The hosts
`ptb.discord.com`, `canary.discord.com` and `discordapp.com`, an API version segment
(`/api/v10/webhooks/…`) and a query such as `?thread_id=1` are accepted too. The URL is checked when it is
saved and again before every send, so a webhook stored earlier that does not match is not used until its URL is
fixed.

Leaving the ping channel empty on Start Fleet ("No Discord message (copy only)") is valid: the Discord step is
skipped and the ping stays available to copy by hand. The demo data does not create a ping channel, because a
channel needs a real webhook.

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
| Claim deadline (days) | 0 | Losses older than this cannot be submitted. 0 means no deadline. |
| Use zKillboard | on | Use zKillboard for killmail hashes and values. Off means only in-game links work, and **Value source** must be the ESI estimate. |
| Value source | zKillboard fitted value | zKillboard fitted value (ship, modules, drones, implants; no cargo), zKillboard total value, or the ESI market estimate. The estimate is also the fallback when zKillboard has no value. The value only matters for ships that are not in the payout table. |
| Payout percentage | 0 | Share of the loss value paid for ships not in the payout table (rounded down to whole ISK). 0 gives no automatic amount, and the reviewer decides. |
| In-fleet loss cap, Out-of-fleet loss cap | empty | Most one loss can pay, whatever its rule or percentage. Type amounts the way players write ISK (`250m`, `1.2b`). Empty means no cap. |
| Capsules need a payout rule | on | Capsule losses are paid only from a capsule entry of the payout table. The value of a pod includes its implants, so neither the percentage nor a doctrine rule covers a pod. |
| Over limit action | Send to review with a warning | What happens to a claim that does not fit the remaining monthly limit: **Send to review with a warning** keeps it and flags it red. **Reject automatically** cuts a claim that fits only partly to what is left (amber warning, normal review) and rejects it, with the cap in the reason, only when nothing is left. |
| Personal monthly cap | empty | Most one player (all characters) can get approved per EVE month. |
| In-fleet losses count toward the personal cap | on | |
| Corporation monthly cap | empty | Default cap per corporation and EVE month. [SRP Corporation Limits](#srp-corporation-limits) override it. |
| In-fleet losses count toward the corporation cap | on | |
| Notify members | on | Send an Alliance Auth notification when a claim is approved, changed, paid, rejected, revoked or reopened. |
| SRP managers may decide on their own claims | off | Normally another SRP manager decides on a manager's own claims. Allow this for small alliances. |
| Member instructions | empty | Free text, up to 2000 characters, shown on My SRP and on the SRP page of a fleet. |

To enable FleetOps SRP:

1. Run `python manage.py migrate fleetops` and `python manage.py collectstatic`.
2. Tick **Enabled** on the SRP Settings page and set the value source, the percentage, the caps and the limits.
   Add ships to the payout table and, where a corporation needs its own limit, to the corporation limits.
3. Give `fleetops.manage_srp` to your SRP managers. `fleetops.request_srp` lets members claim.
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
  claimed in aa-srp or Alliance Auth SRP. Blockers (a loss of somebody else, a capsule without a rule, a closed
  link, a loss older than the deadline) are shown without a submit button.
- Usage of a monthly limit is the sum of approved and paid claims; claims in review are shown but not
  deducted. A revoked approval releases its amount; a paid claim keeps counting. A claim counts in the EVE (UTC)
  month of the loss, whenever it is submitted or decided. The most restrictive limit wins. Limits are checked
  again under a lock when a claim is submitted or approved.

### Review and payment

- **SRP** > **Review** lists claims (pending first, oldest first) with filters. Red rows are over a limit, amber
  rows need a look. A reviewer approves the suggested amount, the amount within the limit, or a typed amount.
  Any other amount needs a reason; more than the policy amount needs "Pay more than the policy amount"; more
  than the remaining monthly limit needs "Approve over the monthly limit". Reject, revoke, undo payment, reopen
  and moving a claim between fleet and out of fleet need a reason. Internal notes are never copied into the
  audit log.
- **SRP** > **Payments** groups approved claims by player and names each group after the player's main
  character as it is now. It has copy buttons for the name and the amount. Pay in game, tick the claims, add
  an optional note and **Mark selected paid**. **Mark paid** is refused when the approved amount is not the one
  the payer was asked to send.
- A decision is made on the claim the reviewer looked at: when the suggested amount changed while the page was
  open, the approval is refused with the old and new amount.
- SRP managers cannot decide on their own claims unless the setting above allows it.
- Summaries (**Summary**, **Corporation**, **My fleets**) are by EVE month of the loss and exclude withdrawn
  claims. The files are `srp-claims`, `srp-members`, `srp-corporations` and `srp-fcs`.
- The fleet page has an SRP card with the link and a Copy button, **Open** and **Close** for SRP managers (a
  closed link takes no new claims), **Request SRP for this fleet** for pilots, and **Create SRP link** for a
  fleet without a link. Creating a link twice changes nothing.

## SRP Payout Table

Fixed amounts. A rule names a ship type (ID or exact English name), a doctrine (a Fittings doctrine or a custom
name), or both, and applies to losses in a fleet, outside a fleet, or both (**Scope**). The most specific
active rule wins: doctrine and ship, then doctrine (any ship), then ship; a rule for the scope of the claim
beats one for both scopes. Doctrine rules match losses in a fleet that has that doctrine. Ships without a rule
are paid the percentage of the loss value. Every claim keeps the rule and the settings it was calculated with;
**Recalculate** applies the current ones to pending claims. The amount is paid as it is, before the per-loss cap
and the monthly limits.

## SRP Corporation Limits

A monthly cap for one corporation, replacing the default corporation cap. An empty cap means no limit for that
corporation.

## History and retention

`python manage.py fleetops_prune_history` removes PAP older than **Data retention days**, PAP of members who
left the alliances in **Member alliance IDs**, old member events and the old texts of pings sent without a fleet
(their audit entries stay); add `--dry-run` to see the counts first.
The daily beat task `fleetops.tasks.prune_attendance_history` does the same and also deletes SRP killmails
nobody claimed. `python manage.py fleetops_refresh_names` rewrites ship and solar system names of fleet member
records in English from the SDE (`--dry-run` available). Ship and solar system names are English everywhere.
