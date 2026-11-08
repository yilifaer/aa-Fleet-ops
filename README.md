# AA FleetOps

Fleet operations for [Alliance Auth](https://gitlab.com/allianceauth/allianceauth). An FC starts a fleet, pings
Discord, sets the MOTD and starts tracking from one page. FleetOps then follows the fleet, grants PAP when it
closes, and feeds the statistics, FC points and SRP of the alliance.

English | [简体中文](https://github.com/yilifaer/aa-Fleet-ops/blob/dev/README.zh-CN.md)

Status: beta. The current pre-release is 0.3.0b1; read the CHANGELOG before every upgrade.

## Features

**Starting a fleet**

- Choose any main or alt character of your Alliance Auth account as FC. FleetOps finds the character's ESI
  fleet and checks that the character is the fleet boss; the boss can sit in any fleet position, and a fleet
  commander who is not the boss is refused with the name of the current boss.
- One record per ESI fleet. Starting a fleet that is already tracked opens the running record. A new boss
  ends the previous FC's record as a boss handover and starts a new one.
- Fleet types with FC point weights, doctrines from the optional `fittings` app (or a custom text), a free-text
  form-up, and comms, Logi and Boost presets.
- Personal start presets, and **Pre ping**: announce a fleet for a later EVE time with a Discord timestamp
  that shows in each reader's own time zone. The scheduled fleet is started later by the FC who scheduled it.
- **PAP Tracking Only** tracks a fleet without Discord ping, MOTD or SRP link. **PAP-link fleets** need no ESI
  fleet at all.
- **Group pings and group fleets.** Squads and interest groups are Alliance Auth groups. Administrators choose,
  on FleetOps's own Administration pages, which ping channels a group's leaders may use and what they may do:
  send a ping only (**Send Ping**, no fleet record, no PAP) or also start fleets with PAP of the fleet types
  the administrators allow for that group. Group leaders are the leaders Alliance Auth already records; they
  cannot change any setting and never reach the alliance channels.

**Discord ping and MOTD**

- Pings go to Discord channels through webhooks. The FC picks the ping channel and the target: no mention,
  `@everyone`, `@here` or Discord roles. Each channel decides which mentions it allows, and only the selected
  mention can notify anybody. A ping over Discord's 2000-character limit is refused before it is sent.
- Editable Ping and MOTD templates with a live preview that looks like Discord and like the in-game window,
  copy buttons that work whatever automation does, and **Regenerate & Push MOTD** on a running fleet. The
  built-in MOTD makes the FC and the form-up system clickable in game (`showinfo` links); a confirmation asks
  before a ping is posted.
- A public join page for voice links such as `mumble://`, so the comms link in Discord and in the in-game MOTD
  can be clicked.

**Tracking and fleet management**

- Current state and change events for every member (join, leave, rejoin, ship, solar system, role, wing and
  squad), and time in fleet for every character.
- The fleet ends automatically only after repeated unreadable polls, and the page says why a fleet ended
  (manually, fleet gone, boss handed over). Every start step keeps its attempt history.
- The **Fleet** tab shows the live fleet tree and the composition by ship function, ship class and system with
  the bridgeable mass. FCs add, rename and delete wings and squads, move members by drag and drop, invite Alliance
  Auth characters and apply structure templates from the web page.
- Kick one member, everyone outside a system, or the capsule pilots.
- A 3-Jump Map of members around the FC when `eve_sde` is installed.

**PAP**

- PAP is granted when the fleet closes, to characters that reached the **PAP minimum time** or registered
  through a PAP link, with an optional limit of characters per player that the FC can tighten for a single fleet
  (FC leads can also loosen it). Alts count for their main.
- PAP multipliers by fleet duration (for example 2x after 90 minutes and 3x after 180), chosen by the FC when
  ending or set automatically.
- Manual fleets, manual PAP and pasted rosters wait for approval by an FC lead. A rejected fleet can be
  corrected and sent back with **Resubmit for approval**; special roles with FC credit are assigned once the
  fleet is approved.
- **PAP links** let pilots register their characters for a fleet without being in the ESI fleet.
- Special roles such as Back Seat FC, with an option to give FC credit.

**Statistics and FC points**

- Personal, corporation, alliance and FC statistics by EVE month; monthly targets with a green, amber or red
  status; a leaderboard; inactive members; PAP history.
- Excel and CSV exports of the corporation, member, FC, PAP history, FC incentive and SRP pages.
- FC points (fleet type weight times the PAP multiplier) and a monthly FC incentive calculation with
  eligibility, waivers and finalization. Periods that are not finalised follow the fleets of their month by
  themselves. A month without a monthly target is not assessed at all.

**SRP**

- Fleets get an SRP link when they start, from FleetOps SRP, aa-srp or Alliance Auth SRP.
- FleetOps SRP: members claim losses with an in-game or zKillboard link; payout table and percentage, per-loss
  caps, monthly limits per player and corporation; review, payment list, summaries and exports. FleetOps never
  moves ISK.
- Standing SRP links (a name, a category and a date, no fleet) and one page that lists, closes and completes all
  SRP links.
- Standing links as an entry point: a card on My SRP, the category's link of the day for a claim, optional daily
  links and automatic closing (inside the beat task that is installed already).
- Losses that are not covered (ship groups, ships, kinds of space; nothing is blocked until you save a list) and SRP
  bans from submitting.
- Automatic loss discovery (off until you switch it on): the recent losses of each member's characters are read from
  ESI with the member's own token and listed on My SRP, matched to the fleet the character was in; pasting a link stays
  possible. Optionally every character of a member must authorize it. It runs inside the beat task that is installed
  already.
- Members can change a pending claim (comment, category or link, the loss) before a reviewer decides; reviewers filter
  the queue by category, link, date and member and see the attackers, the limits as they are now and the member's
  rejection history; payers filter the payments, pay in batches and export the list to pay; an insurance estimate is
  shown next to the amounts (informational only).
- SRP categories (for example fleet, home defense, solo): each with its own per-loss cap, monthly caps, payout rules,
  doctrine text and daily link; claims keep the category they were made in.
- Reports and exports for any period (a month, a quarter, a year or a date range): trend, by category, member, reviewer
  and payer, region and limits, on the page and as CSV or Excel files.
- Import of the aa-srp history (`fleetops_import_aasrp`, with a dry run and an undo) and of a payout table from a
  CSV file, so a site can move to FleetOps SRP with its records.

**Administration**

- All configuration is in the web interface; no Django admin access is needed. Every change and every
  privileged action is in the audit log.
- Permissions for members, corporation managers, FCs, FC leads, SRP reviewers, SRP payers and SRP managers (both roles).
- English and Simplified Chinese pages that follow each user's Alliance Auth language, in every Alliance Auth
  theme (Flatly, Darkly, Materia, Bootstrap and Bootstrap Dark), on phones and on desktops.

## Requirements

- Python 3.10 to 3.14
- Alliance Auth 5.2 or newer in the 5.x series
- django-esi 9 or 10
- Redis and Celery, as for any Alliance Auth site

Keep the django-esi release that matches your Alliance Auth:

| Alliance Auth | django-esi |
|---|---|
| 5.2 to 5.4 | 9 |
| 5.5 and later 5.x | 10 |

Installing FleetOps never upgrades an existing django-esi. Do not force an upgrade (for example with
`--upgrade-strategy eager`); upgrade Alliance Auth and django-esi together. The CI runs the tests on Alliance Auth
5.2 and 5.4 with django-esi 9 and on the latest 5.x with django-esi 10.

FleetOps uses django-esi's OpenAPI client with the ESI compatibility date `2025-11-06`.

## Installation

Install FleetOps into the virtual environment of your Alliance Auth site. `--no-deps` keeps `pip` from changing
the versions of Alliance Auth and django-esi.

```bash
source /path/to/venv/bin/activate

# from the wheel of the GitHub Release
pip install --no-deps https://github.com/yilifaer/aa-Fleet-ops/releases/download/v0.3.0b1/aa_fleetops-0.3.0b1-py3-none-any.whl

# or from Git
pip install --no-deps "git+https://github.com/yilifaer/aa-Fleet-ops.git@v0.3.0b1"

# or from PyPI, only once the version is published on PyPI (0.3.0b1 is a pre-release, so name the version)
pip install --no-deps aa-fleetops==0.3.0b1
```

On a site without the dependencies, leave out `--no-deps`. The extras `aa-fleetops[fittings]` and
`aa-fleetops[sde]` install the optional `fittings` and `django-eveonline-sde` apps.

Add the app and its two scheduled tasks to `local.py` of your Alliance Auth project:

```python
INSTALLED_APPS += [
    "fleetops",
]

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

**Warning: the daily task deletes data.** `fleetops_prune_history` (`fleetops.tasks.prune_attendance_history`) removes
PAP history, member events and the texts of pings sent without a fleet (not PAP link registrations, which stay with their fleet) that are older than **Data retention days**
(at least 365), and, when **Member alliance IDs** is set, the PAP of players whose main is not in one of those
alliances. Check both settings before the first run, and run `python manage.py fleetops_prune_history --dry-run` to
see what would go. A built-in safety check refuses the departed-member part when the setting looks wrong, but it
cannot know your intention; keep database backups. See [Data and privacy](#data-and-privacy).

The one-minute task decides which running fleets are due for a read; the interval itself is a setting. The
same task finishes fleets whose End Fleet task was lost, cancels starts that were interrupted, and starts the
SRP value sweep, so keep it running. If aa-charlink is installed, `"fleetops"` must be written literally in
`INSTALLED_APPS`.

For the comms join page, add the following. `APPS_WITH_PUBLIC_VIEWS` is already defined in `local.py`
(empty on a new site, or listing the other apps with public pages), so put the line below that definition and
add to the list instead of replacing it (if your `local.py` has no such line, write
`APPS_WITH_PUBLIC_VIEWS = ["fleetops"]` instead):

```python
# Lets pilots who are not logged in open the comms join page.
APPS_WITH_PUBLIC_VIEWS += ["fleetops"]
```

The links in pings and MOTDs are built from `SITE_URL`. It is already defined near the top of `local.py`, and
`ALLOWED_HOSTS`, `CSRF_TRUSTED_ORIGINS` and `ESI_SSO_CALLBACK_URL` are derived from it, so do not add the line
again or change it here: a changed value breaks the EVE SSO callback. Only check that it is the https address of
your site without a trailing slash, for example `https://auth.example.com`.

`ESI_USER_CONTACT_EMAIL` also exists in `local.py` already (empty in the template). Fill in that line with a
contact address, as django-esi requires; do not add a second one. Then run:

```bash
python manage.py migrate fleetops
python manage.py collectstatic --noinput
python manage.py fleetops_seed          # default templates, multipliers, special roles, ship functions, structure template
```

`fleetops_seed --demo` also creates three example fleet types for a test site. Restart the web process, the
Celery worker and Celery beat the way your installation does. FleetOps logs to the extension log of Alliance
Auth (`extensions.log`).

Finally run `python manage.py check`. FleetOps adds warnings (never errors, and they never stop the site) for a
missing beat entry, an empty `SITE_URL`, a missing `"fleetops"` in `APPS_WITH_PUBLIC_VIEWS`, `ATOMIC_REQUESTS`
turned on, and aa-charlink installed while `INSTALLED_APPS` does not list `"fleetops"` literally
(`fleetops.W001` to `fleetops.W006`, see
[Configuration](https://github.com/yilifaer/aa-Fleet-ops/blob/dev/docs/configuration.md)). A site that does not want
one of them can silence it with `SILENCED_SYSTEM_CHECKS = ["fleetops.W004"]`. `ATOMIC_REQUESTS` must stay off (the Alliance Auth default): FleetOps
locks rows for the SRP limits and for the end of a fleet, which needs each request to run outside one transaction.

**Docker.** Add the line `aa-fleetops @ https://github.com/yilifaer/aa-Fleet-ops/releases/download/v0.3.0b1/aa_fleetops-0.3.0b1-py3-none-any.whl` (or `aa-fleetops==0.3.0b1`, only once that version is published on PyPI) to the requirements of the image that your web, worker and beat services
share, rebuild it and add the `local.py` lines above. Then open a shell in the web container and run the commands
there with the `auth` shortcut of the Alliance Auth image:

```bash
docker compose exec allianceauth_gunicorn bash
auth migrate fleetops
auth collectstatic --noinput
auth fleetops_seed
```

`auth` is a shell alias, so it only exists inside that shell. Without a shell, call the script directly, for
example `docker compose exec allianceauth_gunicorn python /home/allianceauth/myauth/manage.py migrate fleetops`.
The service names depend on your compose file.

## EVE SSO scopes

The FC authorizes `esi-fleets.read_fleet.v1` and `esi-fleets.write_fleet.v1`. The write scope is used for the
MOTD, kicks and the Fleet tab; FleetOps always uses the token of the FC character, never the token of the
person who clicks. If the MOTD update fails, the rendered MOTD stays available to copy.

PAP links read where a pilot is, what they fly and whether they are online, with the pilot's own token:
`esi-location.read_location.v1`, `esi-location.read_ship_type.v1` and `esi-location.read_online.v1`. These are
the scopes of AFAT's click tracking and of the matching aa-charlink import, so tokens they already hold are
reused. FleetOps SRP can read the recent losses of the members' characters (`esi-killmails.read_killmails.v1`, with the
member's own token, only while **Find losses automatically** is on). Enable all six scopes in your EVE SSO application;
without the last one, loss discovery cannot be authorized and everything else works.

## First steps

1. Create the groups of [permissions](https://github.com/yilifaer/aa-Fleet-ops/blob/dev/docs/permissions.md) in Alliance Auth.
   At least `fleetops.basic_access`, `fleetops.start_fleet` and `fleetops.manage_own_fleet` for an FC, and
   `fleetops.manage_configuration` for the administrators.
2. Open **Administration** (`/fleetops/configuration/`): add fleet types, comms presets, Logi and Boost
   channels, and a ping channel with the Discord webhook. See the
   [configuration reference](https://github.com/yilifaer/aa-Fleet-ops/blob/dev/docs/configuration.md).
3. Open **Start Fleet** and choose **Authorize / Refresh Fleet ESI**.
4. Create an EVE fleet, make the FC character the fleet boss, fill the form and choose **SEND PING & START
   FLEET**. The fleet page shows the result of every step.

Without a ping channel, Start Fleet offers the ping for copying.

## Permissions

| Role | Permissions |
|---|---|
| Member | `fleetops.basic_access`, `fleetops.request_srp` |
| Corporation manager | Member + `fleetops.view_corp_stats` |
| FC | `basic_access`, `start_fleet`, `manage_own_fleet`, `view_all_fleets`, `create_manual_fleet`, `manage_attendance` |
| FC lead | All permissions except the SRP ones |
| SRP manager | `basic_access`, `request_srp`, `manage_srp`, `view_all_fleets` |
| SRP reviewer (decides only) | `basic_access`, `review_srp`, `view_all_fleets` |
| SRP payer (pays only) | `basic_access`, `pay_srp`, `view_all_fleets` |

Every permission and every row-level rule is in [docs/permissions.md](https://github.com/yilifaer/aa-Fleet-ops/blob/dev/docs/permissions.md).
Do not give groups the add, change, delete or view permissions that Django lists for the FleetOps models: they
only matter in the Django admin, which FleetOps does not need for any setting.

## Optional integrations

| App | Adds |
|---|---|
| fittings | Doctrine list, links and hulls in pings and MOTDs |
| django-eveonline-sde | English ship and system names, ship classes, bridgeable mass, 3-Jump Map |
| aa-srp, Alliance Auth SRP | An SRP link for every fleet |
| Alliance Auth Discord service | Reading the server's roles for mentions |
| aa-charlink | The FleetOps scopes on the CharLink page |

FleetOps works without all of them. See [docs/integrations.md](https://github.com/yilifaer/aa-Fleet-ops/blob/dev/docs/integrations.md).

## Upgrading

1. Back up the database.
2. **Before the upgrade to 0.3.0b1**, add the scope `esi-killmails.read_killmails.v1` to the EVE application that belongs to
   your `ESI_SSO_CLIENT_ID` on the EVE developer website. Without it the SSO login of the loss reading ends with an
   "invalid scope" error and members cannot authorize their characters (everything else keeps working).
3. Install the new release into the virtual environment from the wheel of its GitHub Release (`pip install --no-deps <wheel URL>`; `aa-fleetops==<version>` works only once that version is on PyPI).
4. Read the upgrade notes of the release in the [CHANGELOG](https://github.com/yilifaer/aa-Fleet-ops/blob/dev/CHANGELOG.md).
5. Run `python manage.py migrate fleetops` and `python manage.py collectstatic --noinput`.
6. Restart the web process, the Celery worker and Celery beat.

**Going back from 0.3.0b1 to 0.2.x.** Roll the migrations back first (`python manage.py migrate fleetops 0023` for the
0.2.0b3 build, `python manage.py migrate fleetops 0021` for the 0.2.0b2 build), then put the old code back. Neither build
was released, so reinstall the wheel or the commit that the site used before. Run `python manage.py fleetops_srp_rollback_check` before: it lists what stops the
rollback (payout rules of a category, standing links with claims, imported history) and changes nothing. The
[CHANGELOG](https://github.com/yilifaer/aa-Fleet-ops/blob/dev/CHANGELOG.md) has the steps and the list of what the rollback loses.

## Uninstalling

Removing FleetOps deletes its data, so back up the database first and export what you want to keep (the SRP pages
have CSV and Excel exports). Do the steps in this order; the app has to be installed while its tables are removed.

1. Remove the two `CELERYBEAT_SCHEDULE["fleetops_..."]` entries from `local.py` (FleetOps itself creates no schedule
   rows, but Celery beat stores the entries of `local.py` in the database and never removes them). Then delete or disable
   the periodic tasks `fleetops_track_active_fleets` and `fleetops_prune_history` in the Django admin under **Periodic
   Tasks**, and restart Celery beat, so no task is queued for the app any more.
   `manage.py check` then warns `fleetops.W001` and `fleetops.W002` until step 4; ignore them, do not add the
   entries back.
2. Optional: run `python manage.py fleetops_srp_rollback_check`. It only reads and lists what would stop step 3
   (payout rules of a category, links with claims, claims imported from aa-srp); fix those first, as the
   [CHANGELOG](https://github.com/yilifaer/aa-Fleet-ops/blob/dev/CHANGELOG.md) explains.
3. Run `python manage.py migrate fleetops zero`. This drops every FleetOps table and all the data in them. On MariaDB
   and MySQL a migration that fails halfway cannot be rolled back, so keep the backup.
4. Remove `"fleetops"` from `INSTALLED_APPS` and the `APPS_WITH_PUBLIC_VIEWS += ["fleetops"]` line from `local.py`.
5. Run `pip uninstall aa-fleetops`, then restart the web process, the Celery worker and Celery beat.

**What stays.** Django keeps the content types and permission rows of the app (`app_label` `fleetops`), and groups
keep the assignment of those permissions; they do nothing without the app and you can delete them in the Django
admin or the Django shell, filtering by `app_label="fleetops"`. Do not run `remove_stale_contenttypes
--include-stale-apps`, which touches other apps too. Alliance Auth's own data stays untouched: users, groups, characters
and the ESI tokens that django-esi holds (FleetOps stores none of its own). Cached values (keys starting with
`fleetops:`) expire by themselves. Discord messages already sent and the EVE SSO application scopes are not
changed; remove the scopes you no longer need from the EVE application yourself.

## Data and privacy

FleetOps is a tool for your organization; you decide what to keep. It stores, in the database of Alliance Auth:

| Data | What it holds | Kept |
|---|---|---|
| Fleets and members | For each fleet: the FC, its type, times, doctrine and the Discord/ESI results. For each pilot seen in the fleet: character, corporation, alliance, ship, solar system, role and time in fleet, also for people without an Alliance Auth account | Fleets and member states stay. Member events are removed after **Data retention days** (see below) |
| PAP | The PAP rows per character with the main character and player they belong to, corrections | Removed after **Data retention days**, and for departed members when **Member alliance IDs** is set. PAP link registrations (character, ship, solar system, online state) are not part of this: they stay until their fleet is deleted |
| SRP | Claims with the killmail data (victim, ship, system, attackers' names), amounts, the timeline of decisions, payment batches, bans, found losses | Claims, timelines, payments and bans are the payment record and are never removed by the retention; killmails nobody claimed are removed after 7 days, and so are found losses that are no use any more |
| Audit log | Who changed what and when (settings, reviews, payments, PAP corrections); no tokens, no webhook addresses | Never removed by FleetOps |
| Settings | Discord webhook addresses, stored in plain text in the database like other Alliance Auth secrets, SRP and fleet settings | Until you delete them |
| Name caches | Character, corporation and alliance names and affiliations looked up from ESI | Until the next lookup; never removed |
| ESI tokens | Not stored by FleetOps. It reads the tokens django-esi holds for the user and never writes them to a log, the audit log, a page or an export | Managed by Alliance Auth |

Data goes to Discord (the pings you send), to zKillboard and ESI (killmail ids and character ids for SRP, only when
SRP is on), and nowhere else. The hosts FleetOps talks to: ESI (through django-esi), `zkillboard.com` (killmail
values, SRP only), `discord.com` (webhooks you add), and the Discord API of Alliance Auth's own Discord service (roles,
only when you press the button). The browsers of your users load images from `images.evetech.net` and link to
`zkillboard.com`, `evewho.com` and `everef.net`. There is no telemetry and no update check. Retention is set as **Data retention days** on the FleetOps settings page of
**Administration** (365 to 36500). The daily task described above applies it; `python manage.py
fleetops_prune_history --dry-run` shows the effect first.

**Deleting data on request.** FleetOps has no per-person export or erase button. The Django admin shows PAP rows
(`AttendanceRecord`), member rows (`FleetMemberState`, `FleetMemberEvent`) and SRP claims read-only; only
`PapRegistration` can be deleted there. Delete the rows of one person in the Django shell, for example
`AttendanceRecord.objects.filter(character_id=...).delete()`, and the same by character ID for `PapRegistration`,
`FleetMemberState` and `FleetMemberEvent`; SRP claims by the player. SRP bans are deleted with the user in Alliance
Auth; PAP and SRP rows stay as history without the link to an account. A user who started or led a fleet, or who has
FC statistics, cannot be deleted while those records exist (the database protects them). Records of the audit log and
of paid SRP claims are accounting records: delete them only if your rules and the law of your country require it, and
note that this changes the history that FleetOps statistics and reports are built from.

## Translations

FleetOps follows the language menu of Alliance Auth. English and Simplified Chinese are available for the web
pages. Discord pings and in-game MOTDs are always English, whatever the site language: FleetOps's own words in
them are never translated, and text that administrators write in a template is sent as written. To improve a
translation or add a language, see [docs/translations.md](https://github.com/yilifaer/aa-Fleet-ops/blob/dev/docs/translations.md).

## Documentation

| Document | For |
|---|---|
| [Configuration reference](https://github.com/yilifaer/aa-Fleet-ops/blob/dev/docs/configuration.md) | Administrators: every Administration page and setting, the PAP and statistics rules, SRP |
| [Message templates](https://github.com/yilifaer/aa-Fleet-ops/blob/dev/docs/message-templates.md) | Administrators: Ping and MOTD variables and the built-in templates |
| [Permissions](https://github.com/yilifaer/aa-Fleet-ops/blob/dev/docs/permissions.md) | Administrators: permissions and recommended groups |
| [Integrations](https://github.com/yilifaer/aa-Fleet-ops/blob/dev/docs/integrations.md) | Administrators: optional apps |
| [SRP providers](https://github.com/yilifaer/aa-Fleet-ops/blob/dev/docs/srp-providers.md) | Developers: SRP link providers |
| [Architecture](https://github.com/yilifaer/aa-Fleet-ops/blob/dev/docs/architecture.md) | Developers: code layout and flows |
| [Translations](https://github.com/yilifaer/aa-Fleet-ops/blob/dev/docs/translations.md) | Translators |
| [Manual testing](https://github.com/yilifaer/aa-Fleet-ops/blob/dev/docs/manual-testing.md) | Maintainers: release checklist |
| [Releasing](https://github.com/yilifaer/aa-Fleet-ops/blob/dev/docs/releasing.md) | Maintainers: release process |

The configuration reference, permissions, message templates and integrations also exist in Simplified Chinese
in [docs/zh-CN](https://github.com/yilifaer/aa-Fleet-ops/tree/dev/docs/zh-CN).

## Help and contributing

Ask questions in [Discussions](https://github.com/yilifaer/aa-Fleet-ops/discussions). Report bugs, request
features and send translation fixes through [Issues](https://github.com/yilifaer/aa-Fleet-ops/issues/new/choose).
Report security problems privately, as described in
[SECURITY.md](https://github.com/yilifaer/aa-Fleet-ops/blob/dev/SECURITY.md). Development setup, tests and
coding rules are in [CONTRIBUTING.md](https://github.com/yilifaer/aa-Fleet-ops/blob/dev/CONTRIBUTING.md).

## License

AA FleetOps is free software: you can redistribute it and modify it under the terms of the GNU General Public
License version 3. See [LICENSE](https://github.com/yilifaer/aa-Fleet-ops/blob/dev/LICENSE).

Copyright (C) 2026 yilifaer and AA FleetOps contributors.
