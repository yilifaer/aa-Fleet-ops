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
- Fleet Boss validation (`fleet_commander`).
- One `FleetOperation` linking Ping, MOTD, tracking, attendance, statistics and FC incentive data.
- Fleet Type presets with configurable point weights managed from the FleetOps front-end Administration Center.
- Doctrine integration with the installed `fittings` app when available, with `None / Custom` fallback.
- **Form Up / Staging is free text entered by the FC for every fleet**.
- Front-end administration for Comms, Logi/Boost channels, Ping targets and Discord webhooks; alliance managers do not need Django Admin access.
- Editable Ping/MOTD templates.
- Manual **Copy Ping / Copy MOTD** fallback regardless of automation status.
- Discord webhook Ping with per-step success/failure status.
- ESI MOTD update with per-step success/failure status.
- Near-real-time active fleet member tracking using ESI cache/ETag behavior.
- Current fleet member state plus change events: Join, Leave, Rejoin, Ship, System, Role, Wing and Squad changes.
- Automatic attendance separated from operational tracking.
- Configurable maximum attendance credits per Auth user per fleet (`NULL` = unlimited).
- Manual attendance correction with audit log.
- Member statistics.
- Corporation statistics where **Average Attendance = Total Granted Attendance / Main Character Count**.
- Alliance-wide corporation and FC statistics for users with `view_all_stats`.
- FC monthly incentive calculation, eligibility threshold, weights, waiver, review/finalize/unlock.
- 3-jump Fleet Proximity view when `eve_sde` Stargate data is available.
- Automatic fleet end only after repeated confirmed `FLEET_NOT_FOUND`, not on transient ESI errors.
- Automatic SRP fleet/link creation through an SRP provider abstraction; current alpha includes a best-effort Alliance Auth built-in SRP adapter and a registration interface for future Better SRP/community providers.
- Front-end historical manual-attendance page for permitted FCs/admins, including closed fleets and multiple manual credits.
- At least 365-day attendance-history views for personal, corporation and alliance scopes, with optional current-alliance membership pruning.
- Operational dashboard cards, recent participation, top tracked ships, active fleets and FC-month summary.
- Post-creation/post-close special-role assignments: Back Seat FC, Logi Anchor and Snowflake Member. Assigned users who currently hold FleetOps FC permission receive FC fleet/point credit for that operation.
- Active-fleet one-click **Kick all Capsules** control using the Fleet Boss ESI write token.

## Compatibility target

- Python `>=3.10,<3.15`
- Alliance Auth `>=5.2,<6`
- Django 5.2 through Alliance Auth
- django-esi `>=9,<10`

FleetOps uses django-esi's OpenAPI3 client and ESI compatibility date `2025-11-06` for the fleet endpoints used by this alpha.

## ESI scopes

FleetOps requests:

```text
esi-fleets.read_fleet.v1
esi-fleets.write_fleet.v1
```

The write scope is used for MOTD updates and Fleet Boss controls such as kicking capsule members. If MOTD automation fails, the generated MOTD remains available for manual copy.

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

FleetOps still respects its own admin-configured `tracking_interval`; the one-minute beat only decides when to check which operations are due.

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
- a `Manual / Copy Only` Ping target
- default Ping and MOTD templates

The demo command **does not create a Discord webhook or secret**.

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

## 2. Configure optional presets

Grant `fleetops.manage_configuration` to the appropriate alliance management group, then open:

```text
/fleetops/configuration/
```

The FleetOps front-end Administration Center can configure:

- Fleet Types
- Comms Presets
- Channel Presets (`Logi` / `Boost`)
- Discord Webhooks
- Ping Targets
- Message Templates
- FleetOps Settings

`Form Up` is intentionally **not** an administrator preset. Django Admin is retained as an IT/emergency interface only; normal FleetOps alliance administration is performed in the FleetOps front-end.

## 3. Authorize ESI

Open:

```text
/fleetops/start/
```

Click **Authorize / Refresh Fleet ESI** and authorize a character with the required fleet scopes.

## 4. Create an EVE fleet

The selected FC character must currently be Fleet Boss / Fleet Commander. FleetOps will reject a selected character that is merely a squad/wing/fleet member.

## 5. Start Fleet

- Select the FC character/alt.
- Confirm the page detects its Fleet ID.
- Choose Fleet Type.
- Choose Doctrine or enter Custom.
- Enter Form Up manually.
- Select Comms / Logi / Boost / Ping Target as required.
- Confirm Ping and MOTD previews.
- Click **SEND PING & START FLEET**.

The Operation page should show individual result states for fleet detection, Discord, MOTD and tracking.

## 6. Live tracking

Move a test member, change ship/system, leave/rejoin, and confirm the Member and Event tabs update after ESI/cache refresh.

## 7. Attendance

Confirm tracked Auth characters create attendance records. If an Attendance Limit is configured, additional characters belonging to the same Auth user remain tracked but are marked capped when the credit limit is reached.

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

FleetOps can automatically create/link an SRP fleet at Fleet Start when a supported provider is available. `auto` currently tries the Alliance Auth built-in SRP adapter and is deliberately non-blocking: an SRP mismatch must not stop the FleetOperation from starting. See [`docs/SRP_PROVIDER.md`](docs/SRP_PROVIDER.md) for the provider interface intended for a future Better SRP app.

## Attendance history

`data_retention_days` is enforced with a minimum of 365 days. Configure `history_alliance_ids` in FleetOps Administration if departed Alliance Auth users should immediately disappear from history/statistics. Run/schedule:

```bash
python manage.py fleetops_prune_history --dry-run
python manage.py fleetops_prune_history
```

## Manual attendance

Grant `fleetops.manage_attendance` to FCs who should be allowed to correct their own credited fleets. They can use `/fleetops/attendance/manual/` for active or historical operations. Alliance fleet managers can manage all fleets.

## Special roles and capsule cleanup

FC/fleet managers may assign Back Seat FC, Logi Anchor and Snowflake Member after an operation is created, including after close. If the selected member currently has `fleetops.start_fleet`, FleetOps snapshots `grants_fc_credit=True` and includes that operation in the member's FC statistics/incentive calculation. During an active fleet, the operation page can kick all currently detected capsule members; this requires a valid Fleet Boss write token and must be staging-tested against ESI before production use.

---

# Discord configuration

Create a `DiscordWebhook` in Admin, then attach it to a `PingTarget`.

A Ping Target without an active webhook is valid: the Discord step is marked failed/non-blocking and the Ping remains available to copy manually. This is useful for test environments.

---

# Doctrine integration

`fittings` is optional. When an installed app with label `fittings` exposes a doctrine-like model, FleetOps attempts to list it in the Doctrine selector. If no supported doctrine model is found, `None / Custom` remains available and FleetOps continues normally.

FleetOps stores the selected doctrine name/source on each operation so historical records do not depend on a doctrine continuing to exist later.

---

# 3-jump proximity map

The proximity feature is intentionally a **fleet-member proximity view**, not a full Dotlan replacement.

If the optional `eve_sde` app and Stargate data are available, FleetOps builds an undirected Stargate graph and shows active fleet members in systems up to 3 jumps from the FC's current tracked system.

If `eve_sde` is not installed or its Stargate schema cannot be detected, all other FleetOps features continue working and the Map tab explains that routing data is unavailable.

---

# FC incentive model

Fleet Type weights are snapshotted onto each `FleetOperation`, so changing a Fleet Type weight later does not silently rewrite the points of fleets that already happened.

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
