# AA FleetOps

Fleet lifecycle management for **Alliance Auth 5.2+**.

AA FleetOps is an open-source Alliance Auth community app designed to turn the FC workflow into one operation:

**Select FC character → detect ESI fleet → choose fleet settings → preview/copy Ping + MOTD → send Ping → set MOTD → track members → attendance → statistics → FC incentive review.**

> Current release: **0.1.0a5 (test alpha)**. This build is intentionally packaged for installation on a test Alliance Auth before public production use.

## Product specification and test status

The repository includes the full expected feature target and an explicit distinction between implemented alpha code and functionality that still requires real AA/ESI validation:

- [`FEATURES.md`](FEATURES.md) — complete expected FleetOps feature set
- [`docs/IMPLEMENTATION_STATUS.md`](docs/IMPLEMENTATION_STATUS.md) — implemented vs. still-to-prove status
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — architecture principles
- [`docs/LOCAL_MACOS_TEST.md`](docs/LOCAL_MACOS_TEST.md) — local AA smoke-test gate
- [`docs/STAGING_TEST_PLAN.md`](docs/STAGING_TEST_PLAN.md) — real EVE/ESI staging plan
- [`docs/GITHUB_RELEASE.md`](docs/GITHUB_RELEASE.md) — GitHub publishing/release notes

## Current alpha features

- Select any Alliance Auth-owned main or alt character as FC / Fleet Boss.
- ESI fleet auto-detection from the selected character.
- Fleet Boss validation from ESI's `fleet_boss_id`: the boss may sit in any fleet position, and a fleet commander who is not the boss is rejected with the name of the current boss.
- One open record per ESI fleet: starting a fleet that is already tracked opens the running operation instead of creating a second one. A start that was interrupted holds its fleet for about five minutes at most; then its Starting record is cancelled and the fleet can be started again.
- One `FleetOperation` linking Ping, MOTD, tracking, attendance, statistics and FC incentive data.
- Fleet Type presets with configurable point weights managed from the FleetOps front-end Administration Center.
- Doctrine integration with the installed `fittings` app when available, with `None / Custom` fallback.
- **Form Up / Staging is free text entered by the FC for every fleet**.
- Front-end administration for Comms, Logi/Boost channels, Ping channels and Ping roles; alliance managers do not need Django Admin access.
- Editable Ping/MOTD templates (see [Message templates](#message-templates)).
- Manual **Copy Ping / Copy MOTD** fallback regardless of automation status.
- Discord webhook Ping with per-step success/failure status. The FC picks a **ping channel** and a **ping target** (No ping, `@everyone`, `@here` or ping roles); each channel decides which mentions it allows, and Discord is told exactly which mention may notify, so `@everyone` typed into the text never pings anybody. A ping over Discord's 2000-character limit is refused before anything is posted.
- **Pre ping**: announce a fleet for a later EVE time (UTC) with a Discord timestamp in every reader's local time. FleetOps keeps a **Scheduled** fleet that only the scheduling FC can start later (with an optional form-up ping); managers can edit or cancel it, and a cancellation notice mentions nobody.
- Public **comms join page** for `mumble://` and other voice links, so the comms link in Discord and in the in-game MOTD can be clicked; Logi/Boost channels link to the in-game channel.
- **Regenerate & Push MOTD** on an active fleet after the templates, comms or doctrine changed.
- Pilots without an Alliance Auth account are shown with their ESI name, corporation and alliance, a "Not in Alliance Auth" badge and EveWho/zKillboard links.
- ESI MOTD update with per-step success/failure status.
- Near-real-time active fleet member tracking. Fleet reads use django-esi response caching and always return the fleet data, also when it has not changed (ETags are not used for fleet reads).
- Current fleet member state plus change events: Join, Leave, Rejoin, Ship, System, Role, Wing and Squad changes.
- Time in fleet tracked for every character (ESI's join time covers pilots who leave and come back between polls). PAP is granted when the fleet closes, to characters that reached the **PAP minimum time** (0 = any presence); see [PAP rules](#pap-rules).
- Configurable **PAP characters per player** (empty = unlimited). Over the limit the FC character gets the PAP first, then the player's main, then the character with the longest time in fleet.
- PAP preview on running fleets and "PAP in running fleets" on the Dashboard and My Statistics, so pilots see their time in fleet and whether they reached the minimum.
- Configurable PAP multiplier tiers (90 minutes → 2x and 180 minutes → 3x by default): the FC chooses when ending, or FleetOps picks by fleet duration. FC points are the fleet type's weight times the multiplier.
- Manual PAP by character name or from a pasted fleet roster, with audit log. Manual fleets, manual PAP and PAP-link fleets of FCs wait for FC lead approval on the **Approvals** page.
- **PAP links**: pilots register their characters for PAP without being in the ESI fleet (same ESI scopes as AFAT click tracking), also for fleets without an ESI fleet (**PAP-link fleets**).
- Per-fleet statistics inclusion: exclude a test fleet, and leave fleets shorter than the **Minimum fleet duration** out of statistics and incentives.
- Personal **start presets** on Fleet Start.
- Optional aa-charlink integration for the fleet and PAP link scopes.
- Member statistics.
- Corporation statistics where **Average Attendance = Total Granted Attendance / Main Character Count**.
- Alliance-wide corporation and FC statistics for users with `view_all_stats`.
- FC monthly incentive calculation, eligibility threshold, weights, waiver, review/finalize/unlock.
- 3-jump Fleet Proximity view when `eve_sde` Stargate data is available.
- Automatic fleet end only after repeated unreadable polls, not on transient ESI errors. Each poll that gets a 404 checks the FC's own fleet: if the FC is still in the same fleet but someone else holds the boss role, it counts as a **boss handover**, otherwise as **fleet gone** (disbanded, or the FC left it). A successful read resets the count, so handing the boss back in time keeps the fleet open. Before ending, FleetOps reads the fleet once more and calls the end off if that read works. The fleet page warns while polls fail, and a closed fleet shows why it ended ("Ended manually", "Ended automatically: the fleet no longer exists or the FC left it", "Ended automatically: fleet boss handed over to <name>").
- End Fleet returns at once: the final sync, the attendance multiplier and the close run in a Celery task while the page shows "Ending…". A fleet that is still Ending after about five minutes (for example after a worker restart) is finished by the tracking beat.
- Every operation step keeps its attempt history, so the reason of an earlier failure stays visible after a retry succeeds.
- ESI errors are shown as a short status line such as `Could not read fleet members: ESI returned HTTP 503 (Service unavailable).`; the HTTP status and ESI's `x-esi-request-id` are logged.
- Automatic SRP fleet/link creation through an SRP provider abstraction: aa-srp (`aasrp`) when installed, otherwise the Alliance Auth built-in SRP app, plus a registration interface for other SRP apps.
- Front-end historical manual-attendance page for permitted FCs/admins, including closed fleets and multiple manual credits.
- Manual fleet records take their start and end time in EVE time (UTC), whatever the site or browser time zone; the end time is required, must be after the start, and a fleet longer than 6 hours needs an extra confirmation.
- At least 365-day attendance-history views for personal, corporation and alliance scopes, with optional current-alliance membership pruning.
- Operational dashboard cards, recent participation, top tracked ships, active fleets and FC-month summary.
- Post-creation/post-close special-role assignments with configurable roles (Back Seat FC, Logi Anchor and Snowflake Member by default). A role's **Gives FC credit** switch decides whether its holder gets FC fleet/point credit for that operation.
- Kicks from an active fleet using the Fleet Boss ESI write token: one member, everyone outside a chosen system, or the capsule pilots (listed by name). The FC and the fleet boss are never kicked or counted.

## Compatibility target

- Python `>=3.10,<3.15`
- Alliance Auth `>=5.2,<6`
- Django 5.2 through Alliance Auth
- django-esi `>=9,<11` (django-esi 10 is required by Alliance Auth 5.5)

Keep the django-esi version that matches your Alliance Auth release: Alliance Auth 5.4 and older run on django-esi 9, Alliance Auth 5.5 needs django-esi 10. Installing FleetOps does not upgrade an existing django-esi, so do not force an upgrade (for example with `--upgrade-strategy eager`) on Alliance Auth 5.4 or older; upgrade Alliance Auth and django-esi together.

FleetOps uses django-esi's OpenAPI3 client and ESI compatibility date `2025-11-06` for the fleet endpoints used by this alpha.

## ESI scopes

FleetOps requests:

```text
esi-fleets.read_fleet.v1
esi-fleets.write_fleet.v1
```

The write scope is used for MOTD updates and Fleet Boss controls such as kicks. If MOTD automation fails, the rendered MOTD remains available for manual copy.

PAP links read where a pilot is, what they fly and whether they are online, with the pilot's own token:

```text
esi-location.read_location.v1
esi-location.read_ship_type.v1
esi-location.read_online.v1
```

These are the same scopes as AFAT's click tracking and CharLink's "AFAT Click Tracking" import, so tokens they already hold are reused. Enable them in your EVE SSO application (they already are where AFAT click tracking works).

Your Alliance Auth installation must also have a valid `ESI_USER_CONTACT_EMAIL` configured as required by current django-esi guidance.

---

# Test installation — bare metal / venv

Unpack or clone the repository, activate the same Python virtualenv used by your test Alliance Auth, then install it:

```bash
source /path/to/venv/bin/activate
pip install -e /path/to/aa-fleetops
```

For a wheel supplied with a release:

```bash
pip install /path/to/aa_fleetops-0.1.0a5-py3-none-any.whl
```

Add `fleetops` to `INSTALLED_APPS` in your Alliance Auth project's `local.py`:

```python
INSTALLED_APPS += [
    "fleetops",
]
```

If aa-charlink is installed, `"fleetops"` must appear literally like this in `INSTALLED_APPS`: CharLink checks the app label of every hook and refuses the FleetOps one otherwise. CharLink is optional; without it nothing changes.

Add the tracking scheduler to the same `local.py`:

```python
from celery.schedules import crontab

CELERYBEAT_SCHEDULE["fleetops_track_active_fleets"] = {
    "task": "fleetops.tasks.schedule_active_fleet_tracking",
    "schedule": crontab(minute="*"),
    "apply_offset": True,
}

CELERYBEAT_SCHEDULE["fleetops_prune_history"] = {
    "task": "fleetops.tasks.prune_attendance_history",
    "schedule": crontab(hour=4, minute=15),
    "apply_offset": True,
}
```

FleetOps still respects its own admin-configured `tracking_interval`; the one-minute beat only decides when to check which operations are due. The same beat also finishes fleets whose End Fleet task was lost (still Ending after about five minutes) and cancels starts that were interrupted (still Starting after about five minutes), so keep it running on every install.

FleetOps logs through Alliance Auth's extension logger, so its warnings and errors appear in `extensions.log` (`allianceauth.services.hooks.get_extension_logger`).

Run maintenance:

```bash
python manage.py migrate fleetops
python manage.py collectstatic --noinput
```

For a fresh test installation you can create safe demo configuration:

```bash
python manage.py fleetops_seed --demo
```

This creates:

- PCT = 0.50
- StratOps = 1.00
- CTA = 1.50
- default Ping and MOTD templates

The demo command **does not create a ping channel, Discord webhook or secret**. Without a ping channel, Fleet Start offers the ping for copying.

Restart the Alliance Auth web process, Celery worker and Celery beat using the normal method for your installation.

---

# Test installation — Alliance Auth Docker

For a quick disposable test you may copy/install the wheel into the relevant AA containers. For a persistent deployment, bake the package or Git repository into the image used by all AA web/worker/beat services.

After `fleetops` has been installed into the image/environment and added to `local.py`:

```bash
docker compose exec allianceauth_gunicorn auth migrate fleetops
docker compose exec allianceauth_gunicorn auth collectstatic --noinput
docker compose exec allianceauth_gunicorn auth fleetops_seed --demo
```

Then restart the AA services. Exact service names vary between Alliance Auth Docker layouts, so use the service names from your own compose file.

---

# First test checklist

## 1. Permissions

In Alliance Auth Admin, grant a test group/user at least:

```text
fleetops.basic_access
fleetops.start_fleet
fleetops.manage_own_fleet
```

For full admin testing also grant:

```text
fleetops.manage_fleets
fleetops.view_corp_stats
fleetops.view_all_stats
fleetops.manage_attendance
fleetops.manage_incentives
fleetops.manage_configuration
fleetops.view_audit_log
```

`fleetops.manage_incentives` alone does not show the FC Incentive pages: FC incentives are off by default on a new install. Tick **Incentive enabled** in FleetOps Administration → General Settings (see step 2) to test them.

## 2. Configure optional presets

Grant `fleetops.manage_configuration` to the appropriate alliance management group, then open:

```text
/fleetops/configuration/
```

The FleetOps front-end Administration Center can configure:

- Fleet Types
- Comms Presets (each with a public join link)
- Logi / Boost Channels (paste the in-game channel link)
- Ping Channels (Discord webhooks and the mentions each channel allows)
- Ping Roles (Discord roles FCs may mention)
- Message Templates
- FleetOps Settings, including the **Incentive enabled** switch for FC incentives (off by default) and the SRP provider

`Form Up` is intentionally **not** an administrator preset. Django Admin is retained as an IT/emergency interface only; normal FleetOps alliance administration is performed in the FleetOps front-end.

## 3. Authorize ESI

Open:

```text
/fleetops/start/
```

Click **Authorize / Refresh Fleet ESI** and authorize a character with the required fleet scopes.

## 4. Create an EVE fleet

The selected FC character must currently be the Fleet Boss. ESI reports the boss in `fleet_boss_id`, and only the boss can read and write the fleet. The boss may sit in any position (squad member, wing commander or fleet commander); a character in the fleet commander position who is not the boss is rejected, and the message names the current boss.

## 5. Start Fleet

- Select the FC character/alt.
- Confirm the page detects its Fleet ID.
- Choose Fleet Type.
- Choose Doctrine or enter Custom.
- Enter Form Up manually.
- Select Comms / Logi / Boost, the Ping channel and the Ping target as required.
- Confirm Ping and MOTD previews. While required fields are still empty the preview lists what is missing and marks those fields. The counter shows the ping length against Discord's 2000-character limit.
- Click **SEND PING & START FLEET**.

The Operation page should show individual result states for fleet detection, Discord, MOTD and tracking.

To announce a fleet for later, choose **Pre ping** under Fleet time, enter the EVE date and time (UTC) and click **SEND PRE PING & SCHEDULE FLEET**. No ESI fleet is needed yet. When it is time, open the scheduled fleet (dashboard → Scheduled Fleets), pick the fleet boss character and click Start; tick **Send a form-up ping now** to ping again.

## 6. Live tracking

Move a test member, change ship/system, leave/rejoin, and confirm the Member and Event tabs update after ESI/cache refresh.

## 7. Attendance

Confirm the Members tab shows **Time in fleet** growing with each poll and that the Attendance tab shows a PAP preview while the fleet runs. End the fleet: characters with at least the PAP minimum time get PAP. If **PAP characters per player** is set, a player's further characters stay tracked but are marked "Capped (limit)" (the FC character, then the main, then the longest time in fleet keep the PAP).

## 8. Statistics

Corporation average attendance is intentionally simple:

```text
Corporation Average Attendance
= Corporation Total Granted Attendance
/ Corporation Main Character Count
```

Alt count does not change the denominator.

---

# 0.1.0a5 operational additions

## SRP

FleetOps can automatically create/link an SRP fleet at Fleet Start when a supported provider is available and **SRP auto create** is on. `auto` tries aa-srp (`aasrp`) first, then the Alliance Auth built-in SRP app, and is deliberately non-blocking: an SRP mismatch must not stop the FleetOperation from starting. The aa-srp link is created for the FC and points back to the FleetOps fleet page. See [`docs/SRP_PROVIDER.md`](docs/SRP_PROVIDER.md) for the providers and the interface for other SRP apps.

## Attendance history

`data_retention_days` must be between 365 and 36500 days. Configure `history_alliance_ids` in FleetOps Administration if departed Alliance Auth users should immediately disappear from history/statistics. Run/schedule:

```bash
python manage.py fleetops_prune_history --dry-run
python manage.py fleetops_prune_history
```

## Manual attendance

Grant `fleetops.manage_attendance` to FCs who should be allowed to correct their own credited fleets. They can use `/fleetops/attendance/manual/` for active or historical operations, by character name or ID, or paste a roster. Alliance fleet managers can manage all fleets. Entries of users without `fleetops.manage_fleets` count once an FC lead approves them (see [Approvals](#approvals)).

## Special roles and kicks

FC/fleet managers may assign special roles after an operation is created, including after close, to characters recorded in the fleet: tracked members, PAP-link registrations and characters with PAP (manual PAP only once an FC lead approved it, unless the whole fleet still waits for approval). The roles are configured in FleetOps Administration → Special Roles; Back Seat FC, Logi Anchor and Snowflake Member are created by the upgrade. When the role has **Gives FC credit** ticked and the pilot is in Alliance Auth, FleetOps snapshots `grants_fc_credit=True` and includes that operation in the pilot's FC statistics/incentive calculation; the pilot needs no FC permission for that, and still cannot manage the fleet without `fleetops.manage_own_fleet`. During an active fleet, the operation page can kick single members, everyone outside a system, or the capsule pilots; this requires a valid Fleet Boss write token and must be staging-tested against ESI before production use.

---

# Discord configuration

Create a webhook in the Discord channel, then add it in FleetOps Administration → **Ping Channels**. The channel name is what FCs pick on Fleet Start. Per channel, choose whether `@everyone` and `@here` may be used and which ping roles are allowed (none ticked = every active role).

Add the Discord roles FCs may mention under **Ping Roles**: paste the role ID (or `<@&id>`), or click **Sync roles from Discord** when the Alliance Auth Discord service is installed and `DISCORD_GUILD_ID` is set. Synced roles start inactive, so tick **Active** for the roles FCs may ping. Roles that disappear from Discord are marked "Not on Discord" and switched off. A role that a ping channel allows cannot be deleted; remove it from the channel or switch it off instead.

Discord only notifies a role when the role is **mentionable** (Discord server settings → Roles) or when the webhook may mention every role. FleetOps sends each ping with `allowed_mentions` naming exactly the selected target, so other `@everyone`, `@here` or `<@&id>` text in the ping does not notify anybody, and with link previews suppressed.

Only Discord webhook URLs of the form `https://discord.com/api/webhooks/<id>/<token>` are accepted. The `ptb.discord.com`, `canary.discord.com` and `discordapp.com` hosts and an API version segment (`https://discord.com/api/v10/webhooks/<id>/<token>`) are accepted too. The URL is checked when it is saved and again before every send, so a webhook stored by an older FleetOps version that does not match is not used until its URL is fixed. Webhook URLs are write-only: the ping channel form (and Django Admin) never shows a stored URL. Leave the field empty to keep the URL, or paste a new one to replace it.

Leaving the ping channel empty on Fleet Start ("No Discord message (copy only)") is valid: the Discord step is skipped and the Ping remains available to copy manually. This is useful for test environments.

---

# Links in pings and MOTDs

Add these to your Alliance Auth `local.py`:

```python
# Lets pilots who are not logged in open comms join links.
APPS_WITH_PUBLIC_VIEWS = ["fleetops"]
```

and make sure `SITE_URL` is the public https address of Alliance Auth (for example `SITE_URL = "https://auth.example.com"`). Links in pings and MOTDs are built from it; without it they are site paths that do not open outside Alliance Auth. FleetOps Administration shows a warning while either setting is missing.

- **Comms:** a voice link such as `mumble://…` cannot be clicked in Discord or in game, so pings and MOTDs link to the preset's join page (`/fleetops/comms/<id>/join/`), which opens the voice client. `http`/`https` voice links are used directly. The join page is the only FleetOps page that works without a login, and only for active presets; **Issue a new join link** on the preset form makes the old link stop working. Do not put a personal user name or password in a voice link; FleetOps Administration removes them and Django Admin refuses them.
- **Logi/Boost:** in game, right-click the channel and copy its link, then paste it into **In-game link** on the channel form. The MOTD then links the channel name to the in-game channel.
- **Doctrine:** Fittings doctrines are linked to their Fittings page. Members need `fittings.access_fittings` to open it.

---

# Message templates

Ping and MOTD templates use the Django template language. Both get the variables below; all values are plain text except `is_pre_ping`. Times are EVE time (UTC).

| Variable | Value |
|---|---|
| `ping_mention` | The selected mention: `@everyone`, `@here`, the role mentions (`<@&id>`), or empty for No ping. Only this mention notifies anybody. |
| `ping_target` | Same as `ping_mention` (kept for older templates). |
| `ping_target_label`, `ping_target_name` | Who the ping is for: `all`, `online members`, the role names (`Sig A, Sig B`) or `this channel`. |
| `ping_channel` | Name of the ping channel, or empty when the ping is only copied. |
| `sender` | Main character of the user sending the message, else the FC's main, else the FC character. |
| `fc` | FC (fleet boss) character name. |
| `fc_main` | Main character of the FC. |
| `fleet_type` | Fleet type name. |
| `formup` | Form Up / Staging text. |
| `doctrine` | Doctrine name, or `None / Custom`. |
| `doctrine_name` | Doctrine name, or empty. |
| `doctrine_url` | Link to the Fittings doctrine page, or empty for other doctrines. |
| `doctrine_ships` | Hull of every fit of the Fittings doctrine in doctrine order, joined with ` > ` (for example `Moa > Moa > Osprey`). |
| `doctrine_fits` | Fit names of the Fittings doctrine, joined with ` > `. |
| `comms` | Comms preset name. |
| `comms_channel` | Comms channel name of the preset. |
| `comms_link` | Link to open the comms: the join page for voice client links, the link itself for `http`/`https`. |
| `comms_voice_url` | The preset's voice link as stored, without a user name or password. |
| `logi_channel`, `boost_channel` | Logi/Boost channel names. |
| `logi_channel_link`, `boost_channel_link` | In-game channel links for the MOTD (`<a href="joinChannel:…">Name</a>`), or the plain name when no in-game link is set. |
| `fleet_time` | `YYYY-MM-DD HH:MM EVE` for a pre-pinged fleet, otherwise `Now`. |
| `fleet_time_eve` | `YYYY-MM-DD HH:MM EVE` for a pre-pinged fleet, otherwise empty. |
| `fleet_time_unix` | Unix time of the fleet time, or empty. |
| `fleet_time_discord` | Discord relative timestamp `<t:…:R>`, shown in each reader's local time, or empty. |
| `is_pre_ping` | True for a pre ping (true/false, use with `{% if %}`). |
| `sent_at` | Time the message was rendered, `YYYY-MM-DD HH:MM`. |
| `additional_message` | The FC's additional information. |
| `operation_id` | FleetOps operation id. |

Template output is not HTML-escaped. The MOTD may use EVE's HTML-like tags; `{{ additional_message|linebreaksbr }}` keeps the FC's line breaks in game. If a template does not contain `{{ ping_mention }}` (or `{{ ping_target }}`) while a mention is selected, the preview warns that nobody will be notified. A broken template never blocks a start: FleetOps uses the built-in text and reports the problem.

The built-in **Default Ping**:

```django
{% if ping_mention %}{{ ping_mention }}
{% endif %}{% if additional_message %}{{ additional_message }}

{% endif %}**FC Name:** {{ fc }}
**Formup Location:** {{ formup }}
**PAP Type:** {{ fleet_type }}
{% if comms %}**Comms:** {% if comms_link %}[{{ comms }}]({{ comms_link }}){% else %}{{ comms }}{% endif %}
{% endif %}{% if doctrine_name %}**Doctrine:** {% if doctrine_url %}[{{ doctrine_name }}]({{ doctrine_url }}){% else %}{{ doctrine_name }}{% endif %}{% if doctrine_ships %} ({{ doctrine_ships }}){% endif %}
{% endif %}{% if is_pre_ping %}**Fleet Time:** {{ fleet_time_eve }} ({{ fleet_time_discord }})
{% endif %}
-# This was a FleetOps broadcast from {{ sender }} to {{ ping_target_label }} at {{ sent_at }} EVE
```

The built-in **Default MOTD** (one line):

```django
<font color="#bfffffff"><b>{{ fleet_type }}</b><br>FC: {{ fc }}<br>Form Up: {{ formup }}{% if doctrine_name %}<br>Doctrine: {% if doctrine_url %}<a href="{{ doctrine_url }}">{{ doctrine_name }}</a>{% else %}{{ doctrine_name }}{% endif %}{% if doctrine_ships %} ({{ doctrine_ships }}){% endif %}{% endif %}{% if comms %}<br>Comms: {% if comms_link %}<a href="{{ comms_link }}">{{ comms }}</a>{% else %}{{ comms }}{% endif %}{% endif %}{% if logi_channel %}<br>Logi: {{ logi_channel_link }}{% endif %}{% if boost_channel %}<br>Boost: {{ boost_channel_link }}{% endif %}{% if additional_message %}<br><br>{{ additional_message|linebreaksbr }}{% endif %}</font>
```

The MOTD gets a warning above 1000 characters, because the game may cut longer MOTDs.

---

# PAP rules

## Settings

FleetOps Administration → General Settings groups the PAP settings:

| Setting | Default | Meaning |
|---|---|---|
| **PAP multiplier mode** (PAP rules) | FC chooses when ending | **FC chooses when ending**: End Fleet asks the FC for the multiplier once the fleet passed a tier. **Automatic by fleet duration**: the fleet gets the highest multiplier its duration allows. |
| **PAP minimum time** (PAP rules) | 0 | Minutes a character must spend in fleet to receive PAP. 0 = any presence. |
| **PAP characters per player** (PAP rules) | empty | Maximum characters of one Alliance Auth user that receive PAP in one fleet. Empty = unlimited. |
| **Minimum fleet duration** (Statistics) | 0 | Fleets shorter than this are not counted in statistics and incentives unless included manually. 0 = count all. |
| **PAP link validity** (PAP links) | 30 | Minutes a new PAP link stays open unless the FC chooses another time (1 to 1440). |

A fleet keeps the PAP minimum time and the character limit in force when it started; later changes of the settings do not change fleets that already started.

## Time in fleet and PAP

Every tracking poll adds the time each character spent in fleet since the previous poll. A pilot who left and came back between two polls is counted from ESI's join time; a pilot who left is counted up to the last poll that saw them. A long ESI outage between two good polls counts as time in fleet for pilots present at both ends.

PAP is granted when the fleet closes (End Fleet or the automatic end), never while it runs:

- A character gets PAP when its time in fleet is at least the PAP minimum time, or when it registered through a PAP link.
- Over **PAP characters per player** the FC character gets the PAP first, then the player's main, then the character with the longest time in fleet (then the one seen first). The others are listed as "Capped (limit)".
- Characters that are not in Alliance Auth get no PAP; they are listed as "Not in Alliance Auth". **Recalculate PAP** on a fleet closed after the upgrade to 0.1.0a6 grants them PAP once they were added to Alliance Auth.
- PAP is credited to the character's owner and counted for the owner's main and the main's corporation, also when the character itself is in another corporation or alliance. It stays with that player: when a character changes or loses its owner later, recalculating the fleet keeps its PAP where it was.
- A fleet whose end finishes later than the End Fleet click (a busy worker, the recovery after a lost task) ends at the time of the click: no time in fleet after it counts, and the multiplier and the minimum fleet duration are measured to it.
- Manual and imported PAP rows are corrections: **keep** adds a separate credit, **replace** takes the place of the tracked PAP, **merge** adds its value to it. They are applied each time PAP is calculated, so deleting one and recalculating restores the tracked PAP.

While a fleet runs, its Attendance tab shows the PAP preview (who would get PAP if the fleet closed now); members who are not managers of the fleet see only their own characters. Dashboard and My Statistics show "PAP in running fleets" for the user's characters.

## Multiplier tiers

FleetOps Administration → PAP Multipliers lists the tiers: a fleet that ran strictly longer than the threshold may get that multiplier (90 min 01 s qualifies for a 90-minute tier). The upgrade creates 90 minutes → 2x and 180 minutes → 3x; shorter fleets get 1x. Multipliers must rise with the thresholds.

- In the FC mode, End Fleet on a fleet past a tier opens a choice of the allowed multipliers, with none preselected. The server checks the choice again: "3x needs the fleet to run longer than 180 minutes" refuses a multiplier the duration does not allow, and a fleet that passed a tier after the page was loaded asks for the choice first.
- A fleet that ended without a choice (fleet gone, boss handover, finished by the beat) gets 1x and, when it ran long enough for more, **Multiplier pending**. Its FC chooses on the fleet page (**Keep 1x** or a higher tier); the dashboard lists "Fleets waiting for your multiplier choice" and the incentive review warns about them. Until then the fleet counts at 1x.
- After the close the multiplier can be changed within the duration rule by whoever manages the fleet's attendance. FC leads (`fleetops.manage_fleets`) may set any configured multiplier with a written reason of at least 5 characters; the fleet shows "Override by an FC lead" and the audit log keeps the reason.
- New fleet times entered by an FC wait for FC lead approval (see [Approvals](#approvals)), and a multiplier the new times of a closed fleet no longer allow is lowered to the highest one they allow (audited as `attendance.multiplier`; an FC lead's override stays).

FC points of a fleet are its fleet type's weight (snapshotted at the start) times its PAP multiplier.

## Counting in statistics

A fleet counts in member, corporation and FC statistics and in FC incentives when it is closed, approved and not excluded. The fleet page says whether it counts and why not:

- **Do not count** (needs a reason): anyone who manages the fleet, for example for a test fleet.
- **Always count** (also when shorter than the minimum fleet duration) and **Use the automatic rule**: FC leads only.

Every change is audited as `fleet.stats_mode`; the fleet archive marks fleets that do not count with "Not counted".

## Approvals

Manual fleets, manual PAP rows, pasted rosters and PAP-link fleets created by users without `fleetops.manage_fleets` are "Pending FC lead approval" and do not count until approved; an FC lead's own entries are approved at once. FC leads find them under **Approvals** (with a count in the menu) and on the fleet page. A rejection needs a note; approving a fleet can approve its waiting PAP rows with it. When an FC changes the times of an approved fleet, ESI-tracked fleets included, or the fleet type of an approved manual or PAP-link fleet, it goes back to pending: the multiplier tiers and the minimum fleet duration are measured from the times. The Approvals page marks tracked fleets with "Times edited" and shows the multiplier of closed fleets. FCs can delete their own manual PAP until it is approved; approved rows are deleted by FC leads.

## PAP links

On an active or closed fleet, the FC creates a PAP link on the fleet page (**Create PAP link**) and shares it (the link is built from `SITE_URL`). A pilot opens it, ticks the characters that flew and registers them:

- Each character must belong to the pilot and have the pilot's own token with the three location scopes; **Add a character (EVE login)** gets a token through EVE SSO.
- **Require online** is ticked by default: the character must be logged in to the game. Untick it for out-of-game events.
- A character registers once per fleet. It needs no minimum time but counts towards **PAP characters per player**. A registration on a closed fleet recalculates its PAP at once.
- Links expire after their time and can be revoked; an expired or revoked link says so and accepts nothing.
- Fleets closed before the upgrade to 0.1.0a6 keep the PAP selection they were closed with and take no PAP links; add manual PAP there.

"No ESI fleet? Start a PAP-link fleet" on Fleet Start creates a **PAP-link fleet**: no ESI fleet, ping, MOTD or tracking, just a fleet record with its first PAP link. End it with End Fleet; the multiplier follows its duration as usual. It waits for FC lead approval unless an FC lead started it.

## CharLink

With aa-charlink installed (and `"fleetops"` literally in `INSTALLED_APPS`), the CharLink page offers two FleetOps imports:

- **FleetOps FC fleet control**: `esi-fleets.read_fleet.v1` and `esi-fleets.write_fleet.v1`, for users with `fleetops.start_fleet`, not ticked by default.
- **FleetOps PAP links**: the three location scopes, for users with `fleetops.basic_access`, ticked by default.

Characters linked there are ready on Fleet Start and on PAP link pages. Without CharLink the hook is never loaded.

## Kicks

On an active ESI fleet the FC (and fleet managers) can kick from the fleet page: **Kick** on a member row, **Kick outside system…** (choose the system that stays, the FC's system is preselected, untick members to spare them) and **Kick Capsules** (the capsule pilots by name). The FC character and the fleet boss are never kicked. Up to 10 kicks run at once; more run on the Celery worker and their results appear under Operation Actions. After kicks FleetOps reads the fleet again, so the member list is right at once.

## Start presets

**Save as preset…** on Fleet Start keeps the form (without the fleet time and, unless **Remember FC character** is ticked, without the FC character) under a name. Load a preset from the select at the top of the page; **My presets** renames and deletes presets and sets the default that Fleet Start opens with. The last used settings are kept as well. A preset item that was disabled or deleted since loads blank with a warning.

---

# Upgrading to 0.1.0a6

Read the upgrade notes in [`CHANGELOG.md`](CHANGELOG.md) before running `python manage.py migrate fleetops`. In short: Ping Targets become Ping channels and Ping roles (check each channel's mention switches and allowed roles afterwards), the Default Ping/MOTD templates are replaced only if they were never edited, pasted in-game channel links are split into name and link, and user names are removed from voice links. After the upgrade:

- Add `APPS_WITH_PUBLIC_VIEWS = ["fleetops"]` and check `SITE_URL` (see [Links in pings and MOTDs](#links-in-pings-and-motds)).
- Make the Discord roles FCs ping mentionable.
- If comms presets were changed to `https://` pages only to make voice links clickable, switch them back to the `mumble://…` link; the join page does that now.
- Replace hard-coded doctrine links in custom MOTD templates with `{{ doctrine_url }}`, or switch back to the Default MOTD.
- MOTD templates now link the Logi/Boost channels with `{{ logi_channel_link }}` and `{{ boost_channel_link }}`; the upgrade switches `{{ logi_channel }}` and `{{ boost_channel }}` in every MOTD template to them. `{{ logi_channel }}` and `{{ boost_channel }}` are the plain channel names from this release on.
- Change comms presets marked "Not a supported voice link" on the Comms Presets list (for example a bare `ts.example.com:9987`) to a `ts3server://…` or `mumble://…` link; until then pings and MOTDs show only the comms name.
- Custom ping templates keep working: `{{ ping_target }}` now renders the mention selected on Fleet Start.
- Run `python manage.py fleetops_refresh_names --dry-run`, then without `--dry-run`, if older fleets show ship or system names in another language.

The PAP rules (see [PAP rules](#pap-rules)) change how PAP and FC points are counted from the upgrade on:

- PAP is granted when a fleet closes, from time in fleet. **PAP minimum time** is 0 after the upgrade, so any presence still counts; raise it when you want a minimum.
- The attendance limit is now **PAP characters per player** and counts characters, with the FC character, then the main, then the longest time in fleet first.
- FC points are the fleet type weight times the PAP multiplier. Finalized incentive periods keep their numbers; recalculate open periods to apply the new points.
- Manual fleets, manual PAP and PAP-link fleets of FCs need FC lead approval. Records from before the upgrade stay approved. Make sure your FC leads have `fleetops.manage_fleets`.
- Fleets closed before the upgrade keep their PAP selection; changing their multiplier or approving corrections recalculates their values only. PAP links cannot be created for them.
- Check the multiplier tiers (PAP Multipliers) and the special roles (Special Roles) in FleetOps Administration. Back Seat FC gives FC credit; tick **Gives FC credit** on other roles that should.
- Enable the three `esi-location.*` scopes in your EVE SSO application for PAP links, and add `"fleetops"` literally to `INSTALLED_APPS` if you use aa-charlink.
- Exclude old test fleets from statistics with **Do not count** on their fleet pages; **Minimum fleet duration** applies only to fleets that close after it is set.
- Ping channel webhook URLs are no longer shown; keep a copy of a URL elsewhere if you need it.

---

# Doctrine integration

`fittings` is optional. When an installed app with label `fittings` exposes a doctrine-like model, FleetOps attempts to list it in the Doctrine selector. If no supported doctrine model is found, `None / Custom` remains available and FleetOps continues normally.

With Fittings, FCs see the doctrines Fittings shows them: a doctrine in a category limited to groups the FC is not in is hidden on Fleet Start and the PAP-link fleet page; users with `fittings.manage` see all doctrines. Administration forms list every doctrine.

FleetOps stores the selected doctrine name/source on each operation so historical records do not depend on a doctrine continuing to exist later. For Fittings doctrines it also stores the fits (fit name and hull, in doctrine order) when a fleet starts or is pre pinged, for the `doctrine_ships` and `doctrine_fits` template variables; **Regenerate & Push MOTD** reads them again. Ship and system names are always stored in English.

---

# 3-jump proximity map

The proximity feature is intentionally a **fleet-member proximity view**, not a full Dotlan replacement.

If the optional `eve_sde` app and Stargate data are available, FleetOps builds an undirected Stargate graph and shows active fleet members in systems up to 3 jumps from the FC's current tracked system.

If `eve_sde` is not installed or its Stargate schema cannot be detected, all other FleetOps features continue working and the Map tab explains that routing data is unavailable.

---

# FC incentive model

FC incentives are off until **Incentive enabled** is ticked in FleetOps Administration → General Settings. While it is off, the FC Incentive menu entry is hidden and incentive periods cannot be reviewed, recalculated, waived, finalized or unlocked, even with `fleetops.manage_incentives`. FC statistics are not affected. New installs start with the setting off; when upgrading to 0.1.0a6, the data migration turns it on for installs that already have incentive periods.

Fleet Type weights are snapshotted onto each `FleetOperation`, so changing a Fleet Type weight later does not silently rewrite the points of fleets that already happened.

A fleet gives its FC (and every special-role holder with FC credit) its weight times its PAP multiplier as FC points; a fleet whose multiplier choice is pending counts at 1x. Only counted fleets (closed, approved, not excluded) give points.

For each month:

```text
eligible = fleet_count >= minimum_fleets
```

Once an FC is eligible, all fleets in that month count, including the first fleets that reached the threshold.

For eligible, non-waived FCs:

```text
Payout = Monthly Budget × FC Points / Total Eligible Non-Waived Points
```

Each share is rounded down to whole ISK. Any remainder goes to the eligible non-waived FC with the highest point total; `user_id` ascending is the deterministic tie-break.

A finalized month must be unlocked before recalculation or waiver changes.

---

# Architecture

The core object is `FleetOperation`. FleetOps intentionally does not require AFAT or AA Fleet Pings as runtime dependencies.

```text
FleetOperation
├── OperationAction
├── FleetMemberState
├── FleetMemberEvent
├── AttendanceRecord
├── Ping / MOTD snapshots
└── Fleet Type point-weight snapshot

Monthly IncentivePeriod
└── MonthlyFCStatistic
```

The app is designed so that AFAT / AA Fleet Pings can later be supported as migration or compatibility integrations without making their internal model schemas a hard dependency.

---

# GitHub / source release

The repository is already laid out as a normal Python package and can be pushed directly to GitHub. After creating the GitHub repository, add your actual repository URL to project metadata and release from a tag.

Example installation once published:

```bash
pip install git+https://github.com/YOUR-ORG/aa-fleetops.git@main
```

Do not use the alpha directly in production until it has passed your test AA's real ESI / Celery / database test cycle.

## License

GPL-3.0-only.


## 0.1.0a5 statistics and fleet archive additions

- Month/year selectors on corporation, alliance corporation, FC and incentive summaries.
- Alliance managers can drill into any corporation and any FC for a selected historical month.
- Richer personal statistics inspired by fleet activity dashboards: fleet-type totals, characters used, tracked ships, special roles and daily activity.
- New Fleet Operations archive for FCs/managers with year/month/type/status/FC/doctrine/search filters.
- Fleet records can be opened and edited from the front end; edits are audited.


## Permission bundles

FleetOps 0.1.0a5 defines recommended Member, Corporation Management, FC and FC Lead permission bundles. See [`docs/PERMISSIONS.md`](docs/PERMISSIONS.md).

FCs can be given read-only access to every historical fleet with `fleetops.view_all_fleets` while `fleetops.manage_own_fleet` continues to restrict edit/control operations to their own FC-credited fleets.
