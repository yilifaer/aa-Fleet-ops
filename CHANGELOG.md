# Changelog

## [0.2.0b2] - Unreleased

### Changes since the 0.2.0b1 test build

- **PAP characters per player for one fleet.** The setting stays the default, but the FC can now choose the value
  of a single fleet: on the Start Fleet page (also in presets, on the PAP-link fleet page and when editing a
  scheduled fleet) and, while the fleet runs, in a control above the PAP preview on the fleet page. FCs and group
  leaders may only lower it; FC leads may choose any value, including no limit, and are the only ones who can change
  it after the fleet closed, with a written reason. The PAP of a closed fleet is calculated again then. Every
  change is audited as `pap.limit`.

## [0.2.0b1] - Unreleased

The first public pre-release. It covers everything since 0.1.0a5: pre pings and a new ping and MOTD, PAP rules
that are applied when a fleet closes, a Fleet tab to manage the fleet from the web page, monthly statistics
with exports, FC points, FleetOps SRP, English and Simplified Chinese pages, and new documentation. Install it
with the version named (`pip install --no-deps aa-fleetops==0.2.0b1`); once a stable release exists, pip skips
pre-releases unless you name the version or pass `--pre`.

### Upgrade notes

Back up the database first. Then install the release with `--no-deps` (this keeps pip from changing the versions
of Alliance Auth and django-esi), run the two commands below and restart the web process, the Celery worker and
Celery beat.

```bash
python manage.py migrate fleetops
python manage.py collectstatic --noinput
```

**What the commands do**

- `migrate` applies the migrations `0005` to `0018`. `0018` only changes the labels of fields and choices; it
  changes no table. The data steps of the other migrations are described under "Migrations" below.
- `collectstatic` is needed: the Fleet tab, the SRP pages and the translated JavaScript bring new files. Without
  them the Fleet tab shows its first state but does not refresh, and its buttons do nothing.
- No beat entry is added or changed; the two entries from the README stay as they are.

**Settings to check in `local.py`**

- Add `"fleetops"` to `APPS_WITH_PUBLIC_VIEWS`, so pilots who are not logged in can open the comms join links
  in pings and MOTDs.
- `SITE_URL` must be the public https address of Alliance Auth. Links in pings, MOTDs, PAP links and SRP links
  are built from it.
- Set `ESI_USER_CONTACT_EMAIL` (or at least `SITE_URL`): FleetOps SRP sends it in the User-Agent of its
  requests to zKillboard. The server needs outbound HTTPS to `zkillboard.com` and `esi.evetech.net`.
- FleetOps Administration shows a warning while `APPS_WITH_PUBLIC_VIEWS` or `SITE_URL` is missing.
- If aa-charlink is installed, `"fleetops"` must be written literally in `INSTALLED_APPS`.
- Keep the django-esi release that matches your Alliance Auth: django-esi 9 for Alliance Auth 5.4 and older,
  django-esi 10 for Alliance Auth 5.5. FleetOps works with both.

**EVE SSO scopes.** Enable these five scopes in your EVE SSO application:

- `esi-fleets.read_fleet.v1` and `esi-fleets.write_fleet.v1` for FCs. The write scope was already needed for MOTD
  updates and kicks; the Fleet tab uses it for wings, squads, moves and invites. An FC whose token lacks it
  chooses **Authorize / Refresh Fleet ESI** on Start Fleet; until then the Fleet tab is read-only and says why.
- `esi-location.read_location.v1`, `esi-location.read_ship_type.v1` and `esi-location.read_online.v1` for PAP
  links, the same scopes as AFAT's click tracking. Tokens that AFAT or CharLink already hold are reused, so
  pilots do not log in again.
- FleetOps SRP needs no scope.

**Permissions.** The new permissions `fleetops.request_srp` and `fleetops.manage_srp` belong to FleetOps SRP.
The migration gives `request_srp` to every group, state and user that holds `fleetops.basic_access` at the time
of the upgrade; give it by hand to groups that get `basic_access` later. `manage_srp` goes to nobody, because it
decides on money: add it to your SRP managers (and `view_all_fleets` if they should open fleets from claims). FC
leads need `fleetops.manage_fleets` to approve manual records. The recommended groups are in
[the permissions reference](https://github.com/yilifaer/aa-fleetops/blob/dev/docs/permissions.md).

**Keep Celery running.** End Fleet finishes in a Celery task. The minute beat also completes ends whose task
was lost, cancels starts that were interrupted and fills in SRP loss values. Kicks of more than 10 members,
moves of several members, invites and structure templates run on the worker too.

**Behaviour that changed**

- **PAP is granted when a fleet closes**, from each character's time in fleet, not while the fleet runs. The new
  **PAP minimum time** setting is 0 by default, so any presence still earns PAP. Fleets that run during the
  upgrade switch to the new rules with an approximated time in fleet (last seen minus first seen).
- The attendance limit is now **PAP characters per player** and counts characters. When a player has more
  characters in a fleet, the FC character gets the PAP first, then the player's main, then the character with
  the longest time in fleet. Alts that got PAP under the old rule may now show "Capped (limit)".
- **FC points are the fleet type's weight times the PAP multiplier.** A fleet whose multiplier choice is pending
  counts at 1x. Finalized incentive periods keep their numbers.
- Manual fleets, manual PAP, pasted rosters and PAP-link fleets of users without `fleetops.manage_fleets` wait
  for approval and count only when approved. Records from before the upgrade stay approved.
- Statistics, FC statistics and incentives count only closed fleets that are approved and not excluded. The
  **Minimum fleet duration** setting (0 by default) leaves shorter fleets out of fleets that close after the
  change; use **Do not count** on the fleet page for older ones.
- **Statistics use EVE time (UTC).** A month runs from the 1st 00:00 UTC to the 1st 00:00 UTC of the next month,
  whatever `TIME_ZONE` the site or the browser uses. Fleets near midnight on the edge of a month can move to
  the neighbouring month, so the totals of such a month can change.
- **Who is a member in the statistics changed.** A member is an active Alliance Auth user with a main character
  that is not in Doomheim. Disabled accounts, users without a main and biomassed mains no longer appear in
  member lists or count as mains of their corporation, so some corporation averages differ slightly. Users who
  registered in Alliance Auth after a finished month are not part of that month. Set **Member alliance IDs**
  (formerly "History alliance IDs") to your alliance ID; the stored value is unchanged.
- FC incentive pages follow the **Incentive enabled** setting. It is off on new installs; an install that
  already has incentive periods gets it switched on by the migration.
- The `auto` SRP provider tries FleetOps SRP first (while it is enabled), then aa-srp, then Alliance Auth SRP.
  With aa-srp installed, links are now created in aa-srp at fleet start; choose **Alliance Auth SRP** in
  General Settings to keep the old behaviour.
- A special role's **Gives FC credit** switch alone decides FC credit; the holder no longer needs
  `fleetops.start_fleet`.
- Ship and system names are always stored in English. To correct fleets tracked while a user had another
  language active, run `python manage.py fleetops_refresh_names --dry-run`, then without `--dry-run`.
- `fleetops_seed --demo` no longer creates a `Manual / Copy Only` ping target. Leave the ping channel empty on
  Start Fleet to copy the ping.
- Webhook URLs are write-only. Only Discord webhook URLs of the form
  `https://discord.com/api/webhooks/<id>/<token>` are accepted (the `ptb.`, `canary.` and `discordapp.com` hosts
  and an API version segment are fine), and the URL is checked again before every send. A webhook stored by an
  older version that does not match is not used until you fix it in Administration > Ping Channels.
- A default message template must be active. If an inactive template is marked as default, tick **Active** or
  untick **Default** before saving it.
- Members need `fittings.access_fittings` to open the doctrine link in a ping or MOTD, and Discord notifies a
  role only when the role is mentionable or the webhook may mention every role.
- With Fittings installed, Start Fleet and the PAP-link fleet page list only the doctrines the FC may see in
  Fittings (`fittings.manage` sees all).

**Security.** In 0.1.0a1 to 0.1.0a5 a Discord ping that failed with a connection error could store the webhook
token in the fleet's error message, where anybody who could open the fleet could read it. The upgrade removes
these tokens. If a ping failed that way, replace that webhook (delete it in Discord, create a new one and save
its URL in FleetOps), because the old token may have been seen.

**Migrations**

- `0005` keeps one default message template per type.
- `0006` removes webhook tokens from stored error messages and turns FC incentives on where periods exist.
- `0007` adds the end reason and the step history. It copies the latest result of every step into the history,
  labels closed fleets as ended manually or automatically, marks pilots that older versions left "in fleet" on
  closed fleets as having left, and shortens step messages that older versions stored as django-esi's full error
  text (with every response header). Audit entries are left as they were.
- `0008` to `0010` replace Ping Targets by **Ping channels** and **Ping roles** and give every comms preset a join
  token. A target's webhook becomes a ping channel that allows the mention the target used (`@everyone` or
  `@here`), each `<@&id>` role mention becomes a ping role, and older fleets keep the channel and mention they
  were sent with. Check **Allow @everyone**, **Allow @here** and the allowed roles of each channel, and give
  converted roles a proper name. Logi and Boost channels that hold a pasted in-game link are split into the
  channel name and the in-game ID. Comms voice links lose personal user names and passwords, and spaces become
  `%20`. Presets whose link does not start with `mumble://`, `ts3server://`, `teamspeak://`, `discord://`,
  `https://` or `http://` are no longer shown in pings and MOTDs; the Comms Presets list marks them "Not a
  supported voice link".
- `0011` and `0012` add the PAP rules. They seed the multiplier tiers 90 minutes for 2x and 180 minutes for 3x
  and the special roles Back Seat FC (gives FC credit), Logi Anchor and Snowflake Member, and turn extra and
  replacement PAP of earlier versions into correction rows that survive recalculation. Fleets closed before the
  upgrade keep the PAP selection they were closed with.
- `0013` and `0014` add fleet management: the wing and squad snapshot, ship functions and their rules, fleet
  structure templates, and the settings **Live fleet refresh** and **Ship groups without bridge**. The defaults
  are created only while those tables are empty.
- `0015` adds Monthly Targets and the settings **Leaderboard places** (20) and **Leaderboard visibility**
  (everyone with FleetOps access). No data is converted: without a target the corporation pages show no status
  and a member needs at least 1 PAP.
- `0016` and `0017` add FleetOps SRP. SRP stays off until you enable it under Administration > FleetOps SRP. No
  data of aa-srp or Alliance Auth SRP is imported.
- `0018` changes labels only.
- The data steps of `0005`, `0006`, `0007`, `0009`, `0012`, `0014` and `0017` are not undone when migrating back.

**Message templates.** The **Default Ping** and **Default MOTD** are replaced by the new built-in texts only if
they were never edited; edited and custom templates keep working. `{{ ping_target }}` now renders the mention
chosen on Start Fleet, `{{ fleet_time }}` is `YYYY-MM-DD HH:MM EVE` (or `Now`), and `{{ comms_link }}` is the
comms join page for voice client links (the raw link is `{{ comms_voice_url }}`). `{{ logi_channel }}` and
`{{ boost_channel }}` are plain names now; the upgrade switches every MOTD template to
`{{ logi_channel_link }}` and `{{ boost_channel_link }}`. Add `{{ srp_url }}` to a customised ping or MOTD to
put the SRP link in. The variables are listed in the message templates reference.

**Languages.** FleetOps pages follow each user's language in Alliance Auth: English and Simplified Chinese are
available, and other languages show English text. Discord pings and in-game MOTDs are always written in the site
language (`LANGUAGE_CODE`), never in the language of the FC who clicked. Nothing needs configuring.

### Added

**Starting a fleet**

- **Pre ping.** The **Fleet time** choice on Start Fleet announces a fleet for a later EVE time (UTC), with a
  Discord timestamp that shows in every reader's own time zone, and creates a **Scheduled** fleet without
  touching ESI. The scheduled fleet page has a countdown, an "Overdue" badge two hours after the fleet time, and
  cards to start, edit or cancel it. Only the FC who scheduled a fleet can start it; starting checks the fleet
  boss on ESI. Editing and cancelling can post a notice in the same channel, and a cancellation mentions nobody.
- **Ping channels and mentions.** Start Fleet picks a ping channel (or "No Discord message (copy only)") and a
  target: no mention, `@everyone`, `@here` or Discord roles. Each channel decides which mentions it allows, the
  start page disables the rest and the server checks it again. Discord is told exactly which mention may notify
  (`allowed_mentions`), so other mentions in the text never notify anybody.
- **Ping roles.** Add roles by ID, or use **Sync roles from Discord** when the Alliance Auth Discord service is
  installed and `DISCORD_GUILD_ID` is set. Synced roles start inactive; roles that disappear from Discord are
  marked "Not on Discord" and switched off.
- **New default Ping and MOTD.** The ping carries the mention, the additional information, FC, form-up, PAP type,
  comms and doctrine links, the fleet time on pre pings and a footer naming the sender, the target and the EVE
  time. The MOTD links the comms name to the join page, the doctrine to Fittings and the Logi and Boost channels
  to the in-game channels. Embeds are suppressed so links do not unfurl. A ping over Discord's 2000 characters is
  refused before anything is created; the preview counts the characters.
- **Comms join page.** Every comms preset has a public join page (`/fleetops/comms/<token>/join/`) that opens the
  voice client, so `mumble://` and other voice links can be clicked in Discord and in game. **Issue a new join
  link** invalidates the old one. Logi and Boost channel forms accept the link copied in game.
- **Start presets.** Save the Start Fleet form as a named preset, load it, make one the default, and keep the
  last used settings.
- **Regenerate & Push MOTD** on an active fleet renders the MOTD again from the current templates and pushes it.
- **PAP-link fleets.** "No ESI fleet? Start a PAP-link fleet" on Start Fleet opens a fleet without an ESI fleet,
  ping or tracking, with its first PAP link. It waits for FC lead approval unless an FC lead started it.
- New template variables: `sender`, `doctrine_name`, `doctrine_url`, `doctrine_ships`, `doctrine_fits`,
  `comms_voice_url`, `logi_channel_link`, `boost_channel_link`, `fleet_time_eve`, `fleet_time_unix`,
  `fleet_time_discord`, `is_pre_ping`, `ping_mention`, `ping_target_label`, `ping_target_name`, `ping_channel`,
  `sent_at` and `srp_url`.

**Tracking and the Fleet tab**

- **Fleet tab.** An active ESI fleet opens on the fleet as EVE shows it: wings, squads, every member with
  portrait, ship, system and docked and fleet-warp marks, the boss crown and the ship function colour. A status
  bar shows the last ESI read, a **Refresh** button and a **Live** switch. The tab updates in place; the one-minute
  page reload of active fleets is gone. The people who manage the fleet see the controls; users with
  `view_all_fleets` see the tab read-only; other members see the composition after the fleet closes.
- **Wings, squads and members.** Add, rename and delete wings and squads, move members by drag and drop or from
  the member menu (several at once as a background job), set squad, wing and fleet commanders, and invite
  characters registered in Alliance Auth. **Apply template…** builds a wing and squad layout from a Fleet
  Structure Template after showing what changes; members are never moved.
- **Composition** by ship function, ship type, ship class and system, and the **bridgeable mass** with the titan
  bridge rule. Ship functions and rules are configured under Administration (a function can also count fleet
  positions). Ship classes and the mass need django-eveonline-sde.
- **Kicks.** Kick one member, **Kick outside system…** or **Kick Capsules**, with a confirmation that names the
  pilots. The FC and the fleet boss are never kicked. Every kick is audited and followed by a fresh fleet read.
- **Time in fleet.** Every poll adds each character's time in fleet, using ESI's join time for pilots who left and
  came back. The Members tab shows **Time in fleet**, and join and rejoin events show "joined HH:MM EVE".
- Pilots without an Alliance Auth account are looked up on ESI in the background and shown with a "Not in
  Alliance Auth" badge and EveWho and zKillboard links.
- The management command `fleetops_refresh_names [--dry-run]` rewrites stored ship and system names in English.

**PAP**

- **PAP minimum time and PAP characters per player.** PAP is granted when the fleet closes, to characters that
  reached the minimum time or registered through a PAP link. Every character keeps a PAP row with its status
  (Granted, Capped (limit), Below minimum, Replaced, Merged, Pending approval, Rejected). **Recalculate PAP**
  grants PAP again, for example after an alt joined Alliance Auth. A fleet keeps the rules in force when it started.
- **PAP preview.** A running fleet shows who would get PAP if it closed now. The dashboard and My Statistics show
  "PAP in running fleets" for the user's characters.
- **PAP multipliers by duration.** Administration > PAP Multipliers sets which multiplier a fleet may get after a
  number of minutes. In the **FC chooses when ending** mode, End Fleet asks for the multiplier; in the
  **Automatic by fleet duration** mode the highest allowed multiplier applies. A fleet that ends without a choice
  gets 1x and is marked **Multiplier pending** until its FC settles it. After the close, changes stay within the
  duration rule; FC leads can go beyond it with a written reason of at least 5 characters (audited).
- **PAP links.** On an active or closed fleet the FC creates a link that stays open for the **PAP link validity**
  (30 minutes by default). Pilots tick their characters and register them without being in the ESI fleet.
  **Require online** is ticked by default and can be unticked per link.
- **Counting rules.** The fleet page says whether a fleet counts in statistics and why not. Whoever manages a fleet
  can exclude it with a reason (**Do not count**); FC leads can count it anyway (**Always count**).
- **FC lead approvals.** The **Approvals** page lists fleets and PAP rows that wait for approval, with approve or
  reject (a rejection needs a note) and bulk decisions. An FC who edits the times of an approved fleet sends it back
  for approval.
- **Manual PAP by name and roster paste.** Add manual PAP by character name, or paste a fleet window, a chat
  channel list or chat lines from the Chinese or English client; the preview shows who is added, over the limit,
  already has PAP or is not in Alliance Auth.
- **Special roles** are configurable (Administration > Special Roles), each with its own **Gives FC credit**
  switch, and can be given to anybody recorded in the fleet, also after it closed.
- **CharLink.** With aa-charlink installed, CharLink offers **FleetOps FC fleet control** and **FleetOps PAP links**.

**Statistics, FC points and exports**

- **Monthly Targets** (Administration) hold the corporation average target and the member minimum PAP of a month.
  Status colours: green when reached, amber while the month runs, red only for a finished month that missed the
  target. The status is always written out.
- **Corporation overview** with mains, mains with PAP, mains below the minimum, total and average PAP, the target and
  a status; PAP of corporations without members is shown in one **Other corporations** row.
- **Members, inactive list and member pages** for the alliance (`view_all_stats`) and for the own corporation
  (`view_corp_stats`), with rank, PAP per fleet type, characters used and status; filters survive paging and
  month changes.
- **PAP leaderboard** for the alliance and for one corporation with competition ranking. **Leaderboard places** and
  **Leaderboard visibility** are settings.
- **My Statistics** shows a **Monthly minimum** card and a **PAP rank** card.
- **FC statistics** show FC points per FC and fleet type, and use the same rows as the incentive calculation.
- **Exports.** Excel (.xlsx) and CSV for corporations, members, member fleets, FC statistics, PAP history, FC
  incentive periods and SRP. CSV is UTF-8 with a byte order mark, so Excel and WPS read Chinese names, and cells
  that start with `=`, `+`, `-` or `@` are written with a leading apostrophe. Workbooks need no extra dependency
  and carry an About sheet. Every download is audited as `statistics.export` (SRP files as `srp.export`).

**FleetOps SRP**

- Members claim ship losses on **SRP > My SRP** or through the SRP link of a fleet. They paste an in-game
  killmail link, a zKillboard link, or the killmail ID and hash; FleetOps reads the killmail from ESI, checks that
  the victim is one of the member's characters and shows a preview with the value, the payout, the caps, what is
  left of the monthly limits and warnings. A killmail can be claimed once.
- **Loss value** from zKillboard's fitted or total value, or an ESI market estimate. A loss younger than 6
  minutes is not asked about at zKillboard; a sweep fills in its value later.
- **Payout table** with fixed amounts per ship, per doctrine or both, and a percentage for ships without a rule.
  Capsule losses are paid only from a capsule entry (**Capsules need a payout rule**, on by default).
- **Caps and monthly limits** per loss, per player and per corporation (**SRP Corporation Limits** override the
  default). A claim that does not fit goes to review with a warning, or in **Reject automatically** mode is cut to
  what is left.
- **Review and payment.** A review queue with filters and bulk approval, a claim page with the calculation, the
  limits, the fleet presence check and a timeline, and a payments page that groups approved claims by player with
  copy buttons for the name and the amount. SRP managers cannot decide on their own claims unless the setting
  allows it. FleetOps never moves ISK.
- **Summaries and exports** by EVE month of the loss: alliance, corporations, FCs, own corporation and the fleets an
  FC led. Internal notes and the names of the people who decided and paid a claim are visible to SRP managers only.
- The fleet page has an SRP card with the link, **Open** and **Close**, **Request SRP for this fleet** and
  **Create SRP link**.
- **aa-srp provider.** With aa-srp installed, FleetOps creates an aa-srp link at fleet start.

**Administration and audit**

- The Administration pages cover fleet types, PAP multipliers, monthly targets, special roles, comms presets, Logi
  and Boost channels, ping channels and roles, message templates, ship functions and rules, fleet structure
  templates and the SRP settings, payout table and corporation limits. Every change is audited.
- Audit entries for start step results, retries, tracking errors, boss handovers, approvals, multiplier changes,
  statistics modes, PAP links, kicks, fleet changes, exports and every SRP action.

**Languages, documentation and packaging**

- **English and Simplified Chinese.** Menus, pages, forms, messages, exports and JavaScript follow the Alliance Auth
  language of each user. Messages that FleetOps stores (results of start steps, why a fleet ended, ESI errors,
  automatic SRP decisions) are stored in English and translated when shown. Ship and solar system names stay
  English. The wording in the interface is "PAP" instead of "attendance"; the permission codename
  `fleetops.manage_attendance` is unchanged. To add or improve a language, see the translations guide.
- **Documentation.** A new README and a Chinese README (`README.zh-CN.md`), the configuration reference, permissions,
  message templates and integrations also in Simplified Chinese, and guides for translators, developers and
  release managers. Issue forms for bugs, feature requests and translations, a pull request template and a
  security policy.
- **Licence and packaging.** The full GPL-3.0 text, SPDX headers in all source files, the licence in the package
  metadata, and no e-mail address in the metadata. The wheel contains all templates, static files and translations.
  A GitHub Actions job builds and checks every release, creates the GitHub Release and, when enabled, uploads
  to PyPI.

### Changed

- Supports django-esi 9 and 10, so Alliance Auth 5.5 can be installed alongside FleetOps. Python 3.10 to 3.14.
- Every start step keeps its attempts (status, short message, time), so a failure stays visible after a retry
  succeeds.
- Fleets record why they ended (manual, fleet gone, boss handover). The fleet page, the archive and the audit entry
  show it.
- When ESI confirms a new fleet boss at Start Fleet and another FC's record still tracks that fleet, the old
  record ends at once as a boss handover and the new boss gets a separate record with its own PAP and FC credit.
- The boss check at Start Fleet always reads ESI fresh.
- All EVE times in pings, MOTDs and FleetOps forms are UTC. The fleet edit form takes EVE time too; a fleet longer
  than 6 hours needs a confirmation.
- While a fleet is starting, active or ending, its members, map and events are shown only to the people who manage
  it and to users with `view_all_fleets` or `manage_fleets`; other members see their own PAP and roles until it closes.
- `view_corp_stats` also covers the members, inactive list and member pages of the own corporation and its exports;
  `view_all_stats` covers the alliance members, every member page and all exports. It no longer grants access to
  fleet records; use `view_all_fleets` or `manage_fleets`.
- The Members tab shows each pilot's own corporation and alliance, wing and squad names and a docked mark. The
  Events tab shows changes by name.
- The statistics pages run a fixed number of queries whatever the number of corporations, members and fleets.
- Logging goes through Alliance Auth's extension logger, so messages appear in `extensions.log`.
- The fleet detection endpoint returns `fleet_boss_id` and names the boss when the selected character is not the
  boss.

### Fixed

- The fleet boss is taken from ESI's `fleet_boss_id` instead of the fleet position. A boss sitting as squad member
  or wing commander can start a fleet; a fleet commander who is not the boss is refused with the boss's name.
- Handing the boss role to another pilot is no longer reported as "Fleet no longer exists". Handovers and missing
  fleets are told apart, a successful read resets the count towards the automatic end, and the fleet is read once
  more before it ends automatically.
- A fleet that ends keeps a consistent state: pilots still marked as in fleet are marked as having left, and a poll
  that was still running no longer puts pilots back or adds PAP to the closed fleet.
- Starting a fleet whose ESI fleet already has a record opens that record instead of creating a second one. An
  interrupted start no longer blocks the fleet for more than about five minutes.
- ESI errors no longer show django-esi's exception text with every response header. Steps and pages show a short
  line such as `Could not read fleet members: ESI returned HTTP 503 (Service unavailable).`
- End Fleet no longer keeps the browser waiting for the final ESI sync. A background task finishes the fleet; the
  beat finishes it if the worker was killed.
- Fleet tracking and MOTD updates work when ESI data is unchanged since the last read (django-esi ETag handling), and
  MOTD updates send the body in the form django-esi expects. Previously every MOTD write was rejected.
- A failed tracking poll waits for the tracking interval before it is retried, and polls follow the interval when the
  worker starts after the beat.
- Discord webhook URLs and tokens no longer appear in step errors or on pages.
- A broken Ping or MOTD template no longer blocks fleet start or the preview; the built-in text is used and the
  problem is reported.
- Retrying a ping, MOTD or SRP step does nothing for PAP-only fleets, never sends an empty ping or clears the MOTD,
  and never creates a duplicate SRP link. A double-submitted Start Fleet returns the fleet that was already started.
- Unexpected errors in Discord, MOTD, tracking and SRP steps are recorded as failed steps, logged without secrets
  and no longer leave a fleet stuck in Starting.
- The Kick Capsules button counts exactly the pilots that would be kicked. Kicks are refused with the reason after a
  boss handover or without the FC's write token, and kick batches stop at a handover or ESI's error limit.
- The Start Fleet preview answers an incomplete form with the missing fields instead of an error page.
- FC incentives count only closed fleets, refuse invalid period actions instead of returning HTTP 500, and exclude
  departed members.
- Months, days and times in statistics, incentives, the dashboard, the archive and the PAP history no longer
  follow the site's `TIME_ZONE`. On MariaDB the statistics no longer need the time zone tables.
- Merged extra PAP is no longer lost when the multiplier is changed, and the per-player PAP limit no longer depends on
  the multiplier.
- Invalid archive filters, oversized values and out-of-range periods no longer return HTTP 500. Configuration input is
  validated.
- `fleetops_seed` no longer overwrites existing templates or fleet types, bulk deletes in the Django admin are
  audited, and history pruning reports accurate totals.

## 0.1.0a5

- Fixed all six reproducible non-ESI issues reported against 0.1.0a4.
- Added >90-minute Fleet End attendance prompt with 1x/2x/3x finalization.
- Added persistent manual fleet attendance multiplier control.
- Added Attendance Tracking Only start mode (no Discord Ping / automatic MOTD / automatic SRP).
- Added manual/historical fleet creation.
- Members can now browse/open their own participated Fleet Operations.
- Added read-only `view_all_fleets` permission for FC historical archive access.
- Added `create_manual_fleet` permission.
- Added a permissions document with Member / Corp Management / FC / FC Lead role bundles.
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

- Added SRP provider abstraction with a best-effort adapter for Alliance Auth built-in SRP and a registration path for other providers.
- Fleet Start now attempts SRP creation/linking as a non-blocking operation step and stores SRP reference/link/error snapshots.
- Added front-end historical manual attendance for permitted FCs/admins, including closed fleets and repeated/multiple credits.
- Removed the old one-row-per-character/source attendance uniqueness restriction so separate manual corrections can coexist.
- Added personal, corporation and alliance attendance-history pages with minimum 365-day retention.
- Added optional current-alliance membership filtering/pruning, a Celery prune task and `fleetops_prune_history` management command.
- Reworked the main dashboard with attendance metric cards, recent participations, top ships, active fleets and FC-month summary.
- Added post-creation/post-close Back Seat FC, Logi Anchor and Snowflake role assignments.
- Added FC-credit snapshots: assigned users with `fleetops.start_fleet` receive the same operation in FC statistics/incentive calculations.
- Added one-click active-fleet capsule cleanup through the ESI fleet-member delete endpoint.
- Added SRP, special-role, retention and capsule-cleanup test documentation.

## 0.1.0a2

- Added FleetOps front-end **Administration Center** for alliance administrators.
- Added front-end CRUD for Fleet Types, Comms Presets, Logi/Boost Channels, Discord Webhooks, Ping Targets and Message Templates.
- Added front-end General Settings management.
- All configuration pages are protected by `fleetops.manage_configuration`; Django Admin is now documented as an IT/emergency interface rather than the normal way to configure FleetOps.
- Configuration changes are written to the FleetOps Audit Log.
- Discord webhook URL values are redacted from front-end configuration audit snapshots.
- Kept operational/system-created rows (FleetMemberState, FleetMemberEvent, OperationAction, AuditLog) out of raw front-end CRUD to protect data integrity.

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

Added documentation and contribution templates.
