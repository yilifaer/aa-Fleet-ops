# Architecture

This document is for developers. It describes how the code is organised and how a fleet moves through it.
Administrators want the [configuration reference](configuration.md) instead.

## Principles

- One real fleet is one `FleetOperation`. Pings, MOTD, tracking, PAP, statistics, FC points and SRP attach to
  that record instead of living in separate tools.
- Views stay thin. Rules live in `fleetops/services/` and `fleetops/srp/`; talking to the outside world
  (ESI, Discord, zKillboard, other apps) lives in `fleetops/providers/`.
- Optional apps are soft integrations. They are imported behind a guard, and FleetOps keeps working when they
  are missing or change their schema.
- Anything that needs the network after the click runs in a Celery task, so no request waits for ESI longer
  than one call.
- Secrets never leave the places that need them: no token or webhook URL reaches a log, a page, a stored error
  message or the audit log.

## Package map

| Path | Contents |
|---|---|
| `models.py`, `admin.py`, `apps.py`, `auth_hooks.py`, `charlink_hook.py` | Models and their validators, the Django admin, the Alliance Auth menu and URL hooks, the CharLink import |
| `views.py`, `views_*.py` | Pages and JSON endpoints, split by area: fleet start and page (`views.py`), configuration, PAP rules, records and approvals, PAP links, controls (kicks), presets, the Fleet tab (`views_fleet_*`), statistics, members, exports, history, SRP (`views_srp*`) |
| `forms.py`, `forms_*.py` | Forms of the same areas |
| `services/` | Business rules: `operations` (start and end), `tracking`, `pap`, `multipliers`, `counting`, `statistics`, `population`, `assessment`, `incentives`, `messages` and `mentions` (ping and MOTD), `scheduling`, `fleet_*` (Fleet tab), `composition`, `names`, `periods` (EVE time), `audit` |
| `srp/` | FleetOps SRP: claims, policy, loss values, monthly limits, presence check, review, summaries, access rules, the fleet SRP link, notifications and the value sweep |
| `providers/` | `esi` (fleet reads and writes), `esi_killmails`, `zkillboard`, `pings` (Discord webhook), `doctrines` (Fittings), `srp` (SRP provider registry) |
| `tasks.py` | Celery tasks |
| `texts.py` | Stored messages and their translation ([stored texts](#stored-texts-and-languages)) |
| `templates/fleetops/`, `static/fleetops/` | Templates (with `operation/`, `statistics/`, `srp/`, `configuration/` folders) and the JavaScript and CSS of the pages |
| `templatetags/` | Filters and tags, including `fleetops_i18n` |
| `locale/` | Translation catalogs |
| `management/commands/` | `fleetops_seed`, `fleetops_prune_history`, `fleetops_refresh_names` |
| `migrations/` | Schema and data migrations |

Outside the package: `tests/` (the suite), `testauth/` (a minimal Alliance Auth project for the tests), `tools/`
(release and documentation checks), `docs/`.

## Data model

```text
FleetOperation
├── OperationAction ── OperationActionAttempt   one row per step, with its attempt history
├── FleetMemberState                            latest state of every character seen in the fleet
├── FleetMemberEvent                            meaningful changes only (join, leave, ship, system, role, ...)
├── AttendanceRecord                            the PAP rows (the model keeps its first name)
├── OperationRoleAssignment                     special roles
├── PapLink ── PapRegistration                  PAP links and the characters registered through them
├── SrpFleetLink ── SrpClaim ── SrpClaimEvent   the fleet's SRP link and its claims
└── snapshots: fleet type weight, PAP minimum time and character limit, doctrine and fits,
    ping channel name and mentioned roles, ping text, MOTD text

IncentivePeriod ── MonthlyFCStatistic           FC incentives per month
```

Configuration lives in `FleetOpsSettings` and `SrpSettings` (one row each), `FleetType`, `MultiplierTier`,
`StatisticsTarget`, `SpecialRole`, `CommsPreset`, `ChannelPreset`, `DiscordWebhook` (a ping channel),
`PingRole`, `MessageTemplate`, `ShipFunction`, `ShipFunctionRule`, `FleetStructureTemplate`, `SrpPayoutRule`
and `SrpCorporationLimit`. `AuditLog` records privileged actions with the old and new values. `EveEntityName`
and `CharacterAffiliation` cache what ESI said about characters Alliance Auth does not know.

Rules that change over time are copied onto the fleet when it starts (fleet type weight, PAP minimum time,
PAP character limit), so a later change of a setting never rewrites a fleet that already happened. Usage of the
SRP monthly limits is never stored: it is the sum of the approved and paid claims of the month.

## Fleet lifecycle

A fleet has one of the statuses Draft, Scheduled, Starting, Active, Ending, Closed, Cancelled or Error.

**Start** (`services/operations.start_fleet`). The FC picks one of their characters. FleetOps asks ESI for the
fleet of that character and checks the `fleet_boss_id`: only the boss can read and write the fleet. Inside one
transaction with a lock it claims the ESI fleet. A fleet that is already tracked returns the existing record;
a new boss at an ESI fleet with an open record for another FC ends that record as a boss handover and starts a
new one. A start that stalls is cancelled by the minute beat after about five minutes. Then the steps run, each
one best effort and recorded as an `OperationAction`: SRP link, message rendering, Discord ping, MOTD, first
tracking read. Whatever fails, the fleet becomes Active; the reason stays on the fleet page and the step can be
retried. The SRP step runs first so that the ping and the MOTD can carry `{{ srp_url }}`. Other kinds of
starts: a scheduled fleet (pre ping) is started later by the FC who scheduled it; a manual fleet and a PAP-link
fleet have no ESI fleet, ping, MOTD or tracking.

**Tracking.** The beat task `schedule_active_fleet_tracking` runs every minute. For each active fleet whose
interval is due it queues `track_operation`, which reads members, wings and squads with the FC character's
token and diffs them against `FleetMemberState`, writing `FleetMemberEvent` rows for the changes and adding the
time each character spent in the fleet. While a manager watches the Fleet tab, the page asks for a refresh
and the same read runs more often (the live refresh). When ESI keeps answering "not found", the fleet ends
automatically after the configured number of polls, as "fleet gone" or as a boss handover; a confirming read
runs first.

**End** (`end_fleet`). A manual end sets the status to Ending and queues `finish_fleet_end`, which does the
final sync, chooses the multiplier, closes the record, grants the PAP and writes the audit entry. The end is
timestamped with the request, so a slow or recovered close changes nothing. A fleet that stays in Ending for
about five minutes is finished by the minute beat.

**PAP** (`services/pap.py`). PAP is granted only when the fleet closes, from time in fleet and PAP link
registrations, with the character limit applied per player. Manual and imported rows are corrections applied on
top each time PAP is calculated, so recalculating is safe. Statistics read the result through `services/counting`
(closed, approved and not excluded fleets only).

**SRP.** The fleet's SRP link is created by the SRP provider that the settings select (`providers/srp.py`).
Members claim a loss through the link or **My SRP**; `srp/claims.py` reads the killmail from ESI, `srp/values.py`
finds the value (zKillboard, with the ESI market estimate as the fallback), `srp/policy.py` and `srp/quota.py`
calculate the amount and the limits, and `srp/review.py` makes the decisions under a lock, writing a claim event
and an audit entry for each one. The beat starts the value sweep for claims that wait for a zKillboard value.

## Failure model

Fleet start is not one all-or-nothing call. Each step has a status (success, failed or skipped), a message and
an attempt history. A Discord or MOTD failure never destroys the fleet: the rendered ping and MOTD stay
available to copy. A step that a fleet never performs (for example Discord in PAP-only mode) stays skipped.
Unexpected exceptions are reported through `services/errors.report_failure`. It stores a short message with the
exception class only, and logs the class and the traceback frames but never the exception text, because the text
of a failed request can echo a webhook URL or a token.

ESI errors become a short sentence such as `Could not read fleet members: ESI returned HTTP 503 (Service
unavailable).`; the HTTP status and ESI's `x-esi-request-id` go to the log.

## Stored texts and languages

Pages follow the language of the logged-in user, which Alliance Auth's middleware activates. Three kinds of text
need care:

- **Text shown in the request** (templates, forms, flash messages, JSON for the page, export column titles) is
  translated normally with `gettext`.
- **Text that code stores for people to read later** (step results, end reasons, ESI errors, job and kick
  results, automatic SRP decisions) is stored in English, because the worker that writes it has no user. Each
  message is declared with `StoredText` in `texts.py`; calling it builds the English text, and
  `translate_stored()` (the `stored_text` template filter) recognises the text again and returns it in the
  language of the viewer. Unknown text, such as old rows, user text and ESI text, is returned unchanged.
  `STEP_LABELS` and `FLEET_ROLE_LABELS` translate the codes behind steps and fleet positions.
- **Pings and MOTDs** are rendered inside `outgoing_language()`, the site language, so every reader gets the same
  text whoever clicked.

JavaScript uses Django's `JavaScriptCatalog` at `/fleetops/jsi18n/`; `base.html` loads it with the language and
the version in the query string and keeps a small English fallback if the request fails. The catalogs are in
`fleetops/locale/` and described in [translations](translations.md). Admin-entered data (fleet types, presets,
templates, roles) is never translated.

## Background tasks

| Task | When |
|---|---|
| `schedule_active_fleet_tracking` | Beat, every minute: queues the polls that are due, finishes lost ends, cancels abandoned starts, queues the SRP value sweep |
| `prune_attendance_history` | Beat, daily: retention, departed members, unclaimed SRP killmails |
| `track_operation`, `finish_fleet_end`, `refresh_fleet_structure`, `run_fleet_job`, `resolve_character_names`, `refresh_srp_values` | Queued by the code; no beat entry |

`kick_members` is no longer queued: kicks run as fleet jobs (`run_fleet_job`). The task stays so that messages
an earlier version left in the broker still run.

Tasks never activate a language. They use cache locks (`cache.add`) so that two workers never do the same job,
and database row locks where money or PAP is decided.

## Extension points

- **SRP providers.** Another SRP app registers a provider with `register_srp_provider`
  ([SRP providers](srp-providers.md)).
- **CharLink.** `charlink_hook.py` adds the FleetOps scope imports to aa-charlink; it is loaded only where
  CharLink is installed.
- **Message variables.** `services/messages.build_context` is the one place that defines the variables of
  [message templates](message-templates.md).
- **Statistics datasets.** `services/export_datasets.py` defines who may download which export.

## Security rules

- Webhook URLs are validated when saved and before every send, never rendered back to a page, and never logged.
- A ping notifies exactly the selected target: `allowed_mentions` is set on every post, and other mass mentions
  in the text are neutralised.
- ESI calls for fleet control use the FC character's own token, never the token of the person who clicked.
- The public comms join page is the only view without a login or permission check, and it is the last URL
  pattern so that Alliance Auth's login decoration reaches every other pattern.
- Audit entries hold identifiers, states and amounts, never tokens, webhook addresses or internal notes.
- zKillboard is asked politely: a descriptive User-Agent, one request every two seconds for the whole site, and
  a pause after a block.

## Tests

`tests/` runs on the minimal project in `testauth/` with a Redis cache. `tests/factories.py` builds users,
characters, fleets, PAP rows and SRP claims; `tests/fleet_sim.py` simulates ESI fleets. No test calls ESI,
zKillboard or Discord. Guard tests check the permission matrix of every URL, the packaging, the licence
headers, the translation catalogs and that the documents match the code. See [CONTRIBUTING](../CONTRIBUTING.md).
