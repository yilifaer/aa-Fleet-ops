# AA FleetOps

Fleet operations for [Alliance Auth](https://gitlab.com/allianceauth/allianceauth). An FC starts a fleet, pings
Discord, sets the MOTD and starts tracking from one page. FleetOps then follows the fleet, grants PAP when it
closes, and feeds the statistics, FC points and SRP of the alliance.

English | [简体中文](https://github.com/yilifaer/aa-fleetops/blob/dev/README.zh-CN.md)

Status: beta. The first public pre-release is 0.2.0b1; read the CHANGELOG before every upgrade.

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

**Discord ping and MOTD**

- Pings go to Discord channels through webhooks. The FC picks the ping channel and the target: no mention,
  `@everyone`, `@here` or Discord roles. Each channel decides which mentions it allows, and only the selected
  mention can notify anybody. A ping over Discord's 2000-character limit is refused before it is sent.
- Editable Ping and MOTD templates with live preview, copy buttons that work whatever automation does, and
  **Regenerate & Push MOTD** on a running fleet.
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
- Manual fleets, manual PAP and pasted rosters wait for approval by an FC lead.
- **PAP links** let pilots register their characters for a fleet without being in the ESI fleet.
- Special roles such as Back Seat FC, with an option to give FC credit.

**Statistics and FC points**

- Personal, corporation, alliance and FC statistics by EVE month; monthly targets with a green, amber or red
  status; a leaderboard; inactive members; PAP history.
- Excel and CSV exports of the corporation, member, FC, PAP history, FC incentive and SRP pages.
- FC points (fleet type weight times the PAP multiplier) and a monthly FC incentive calculation with
  eligibility, waivers and finalization.

**SRP**

- Fleets get an SRP link when they start, from FleetOps SRP, aa-srp or Alliance Auth SRP.
- FleetOps SRP: members claim losses with an in-game or zKillboard link; payout table and percentage, per-loss
  caps, monthly limits per player and corporation; review, payment list, summaries and exports. FleetOps never
  moves ISK.

**Administration**

- All configuration is in the web interface; no Django admin access is needed. Every change and every
  privileged action is in the audit log.
- Permissions for members, corporation managers, FCs, FC leads and SRP managers.
- English and Simplified Chinese pages that follow each user's Alliance Auth language.

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

# from PyPI (0.2.0b1 is a pre-release, so name the version)
pip install --no-deps aa-fleetops==0.2.0b1

# or from the wheel of a GitHub Release
pip install --no-deps https://github.com/yilifaer/aa-fleetops/releases/download/v0.2.0b1/aa_fleetops-0.2.0b1-py3-none-any.whl

# or from Git
pip install --no-deps "git+https://github.com/yilifaer/aa-fleetops.git@v0.2.0b1"
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

**Docker.** Add `aa-fleetops==0.2.0b1` to the requirements of the image that your web, worker and beat services
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
reused. Enable all five scopes in your EVE SSO application. FleetOps SRP needs no scope.

## First steps

1. Create the groups of [permissions](https://github.com/yilifaer/aa-fleetops/blob/dev/docs/permissions.md) in Alliance Auth.
   At least `fleetops.basic_access`, `fleetops.start_fleet` and `fleetops.manage_own_fleet` for an FC, and
   `fleetops.manage_configuration` for the administrators.
2. Open **Administration** (`/fleetops/configuration/`): add fleet types, comms presets, Logi and Boost
   channels, and a ping channel with the Discord webhook. See the
   [configuration reference](https://github.com/yilifaer/aa-fleetops/blob/dev/docs/configuration.md).
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

Every permission and every row-level rule is in [docs/permissions.md](https://github.com/yilifaer/aa-fleetops/blob/dev/docs/permissions.md).

## Optional integrations

| App | Adds |
|---|---|
| fittings | Doctrine list, links and hulls in pings and MOTDs |
| django-eveonline-sde | English ship and system names, ship classes, bridgeable mass, 3-Jump Map |
| aa-srp, Alliance Auth SRP | An SRP link for every fleet |
| Alliance Auth Discord service | Reading the server's roles for mentions |
| aa-charlink | The FleetOps scopes on the CharLink page |

FleetOps works without all of them. See [docs/integrations.md](https://github.com/yilifaer/aa-fleetops/blob/dev/docs/integrations.md).

## Upgrading

1. Back up the database.
2. Install the new release into the virtual environment (`pip install --no-deps aa-fleetops==<version>`).
3. Read the upgrade notes of the release in the [CHANGELOG](https://github.com/yilifaer/aa-fleetops/blob/dev/CHANGELOG.md).
4. Run `python manage.py migrate fleetops` and `python manage.py collectstatic --noinput`.
5. Restart the web process, the Celery worker and Celery beat.

## Translations

FleetOps follows the language menu of Alliance Auth. English and Simplified Chinese are available. Discord pings
and in-game MOTDs are written in the site language, not in the language of the FC who clicked. To improve a
translation or add a language, see [docs/translations.md](https://github.com/yilifaer/aa-fleetops/blob/dev/docs/translations.md).

## Documentation

| Document | For |
|---|---|
| [Configuration reference](https://github.com/yilifaer/aa-fleetops/blob/dev/docs/configuration.md) | Administrators: every Administration page and setting, the PAP and statistics rules, SRP |
| [Message templates](https://github.com/yilifaer/aa-fleetops/blob/dev/docs/message-templates.md) | Administrators: Ping and MOTD variables and the built-in templates |
| [Permissions](https://github.com/yilifaer/aa-fleetops/blob/dev/docs/permissions.md) | Administrators: permissions and recommended groups |
| [Integrations](https://github.com/yilifaer/aa-fleetops/blob/dev/docs/integrations.md) | Administrators: optional apps |
| [SRP providers](https://github.com/yilifaer/aa-fleetops/blob/dev/docs/srp-providers.md) | Developers: SRP link providers |
| [Architecture](https://github.com/yilifaer/aa-fleetops/blob/dev/docs/architecture.md) | Developers: code layout and flows |
| [Translations](https://github.com/yilifaer/aa-fleetops/blob/dev/docs/translations.md) | Translators |
| [Manual testing](https://github.com/yilifaer/aa-fleetops/blob/dev/docs/manual-testing.md) | Maintainers: release checklist |
| [Releasing](https://github.com/yilifaer/aa-fleetops/blob/dev/docs/releasing.md) | Maintainers: release process |

The configuration reference, permissions, message templates and integrations also exist in Simplified Chinese
in [docs/zh-CN](https://github.com/yilifaer/aa-fleetops/tree/dev/docs/zh-CN).

## Help and contributing

Ask questions in [Discussions](https://github.com/yilifaer/aa-fleetops/discussions). Report bugs, request
features and send translation fixes through [Issues](https://github.com/yilifaer/aa-fleetops/issues/new/choose).
Report security problems privately, as described in
[SECURITY.md](https://github.com/yilifaer/aa-fleetops/blob/dev/SECURITY.md). Development setup, tests and
coding rules are in [CONTRIBUTING.md](https://github.com/yilifaer/aa-fleetops/blob/dev/CONTRIBUTING.md).

## License

AA FleetOps is free software: you can redistribute it and modify it under the terms of the GNU General Public
License version 3. See [LICENSE](https://github.com/yilifaer/aa-fleetops/blob/dev/LICENSE).

Copyright (C) 2026 yilifaer and AA FleetOps contributors.
