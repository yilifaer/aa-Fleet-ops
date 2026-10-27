# Interface check

A developer tool (not part of the package) that opens the FleetOps pages in a real browser, in every theme Alliance
Auth ships and at phone, tablet and desktop width, and reports what is unreadable, cut off or inconsistent. It needs
Node with Playwright, Chromium and Redis; it never touches the network while it runs.

Two checks belong together:

- `python tools/check_ui.py` greps the sources for the interface rules R1-R9 (classes, theme variables, colours,
  type, decoration, images ...). It needs nothing but Python. `--strict` fails on a broken rule.
- `node tools/ui_check/run.js` (this folder) renders the pages and measures the result.

## Requirements

- The Python of the Alliance Auth environment you test with (`.venv/bin/python`): the example data is created
  with the test factories and the site is started with the test settings.
- Node 18 or newer and the `playwright` package (a local `node_modules` or the global one: `npm root -g`).
  Chromium comes from `PLAYWRIGHT_BROWSERS_PATH` (default `/opt/pw-browsers` when it exists). Never run
  `playwright install` on the build machine.
- `redis-server` (the check starts a throw-away one on a free port; `--redis-url` uses an existing one).
- For Chinese pages a CJK font (for example `fonts-wqy-zenhei`).
- The theme and library files of the CDN, kept once in `tools/ui_check/.cache`:
  `python tools/ui_check/fetch_assets.py`. `run.js` does it by itself when the cache is missing. Each file is
  checked against the hash Alliance Auth pins (Subresource Integrity); when the CDN cannot be reached the same
  file is taken from its npm package, and a file that does not fit its hash stops the run.

## Run

```bash
# every page, five modes, three widths (English); Chinese at 375 and 1280 in Flatly and Darkly
node tools/ui_check/run.js --python .venv/bin/python

# the pages of one lane, or a few pages in a few modes
node tools/ui_check/run.js --python .venv/bin/python --lane C
node tools/ui_check/run.js --python .venv/bin/python --pages 'start*,presets' --themes darkly,flatly --widths 375
node tools/ui_check/run.js --python .venv/bin/python --pages start --themes darkly,materia --widths 375 --lang zh-hans

# which pages exist
node tools/ui_check/run.js --python .venv/bin/python --list [--lane F] [--pages 'fleet-*']
```

Every run uses a Redis of its own (`--redis-server` is the default and may be written out), a database file of
its own and a free port, so several runs can go on at the same time (lanes in separate checkouts do not disturb
each other). Never point `--redis-url` at a Redis another job uses.

| Option | Meaning |
|---|---|
| `--python PATH` | Python of the Alliance Auth environment (default: `FLEETOPS_UI_PYTHON`, `.venv/bin/python`, `python3`) |
| `--pages LIST` | page ids or patterns with `*`, comma separated (default: all) |
| `--lane A..F`, `S0`, `all` | the pages of one lane (see below) |
| `--themes LIST` | `bootstrap,bootstrap-dark,flatly,darkly,materia` (default: all five) |
| `--widths LIST` | `375,768,1280` (default: all three) |
| `--lang MODE` | `auto` (default): English in every mode plus Chinese at 375 and 1280 in Flatly and Darkly; `en`, `zh-hans`, `all` |
| `--jobs N` | browser contexts at the same time (default 3; the machine has 4 CPUs) |
| `--out DIR`, `--name NAME` | output folder (default `tools/ui_check/out/<start time>`) |
| `--shots all\|fail\|none` | which screenshots to keep (default all) |
| `--keep` | keep the database and the server log of the run |
| `--no-focus` | skip the keyboard focus check |
| `--fail-on-warn` | exit 1 on warnings too |

Exit codes: `0` no FAIL, `1` at least one FAIL, `2` the run itself broke (seed, server, browser, assets).

## What it does

1. `seed.py` deletes and rebuilds a SQLite database (a migrated empty copy is reused while the migrations do not
   change) and fills it with example data: roles and users, 30 pilots in three corporations, closed fleets of the
   current and the previous month, a running fleet with 25 members, wings and squads, a second running fleet of an
   FC without the write scope, a pre-pinged fleet, manual fleets pending, rejected and resubmitted, a PAP-link fleet,
   SRP rules, claims in every state, an incentive period in review, a month without a monthly target, and
   the group roles, channels, group fleet and pings when the tree has the group models. All names and IDs are examples.
2. The site starts with `tools/ui_check/settings.py` (the test project, the network guard of the tests, the two
   Bootstrap theme hooks). A page that tries to reach ESI or the SSO while it is only displayed fails loudly.
3. For each language, theme and width one browser context runs. Each role's login cookie is set, the theme is chosen
   with **Alliance Auth's own theme form** (the same form the menu shows), and the pages of the role are opened
   (`html data-theme` is checked after the switch).
4. Every page is loaded, its scripted states are run, then the checks below run and a full-height screenshot is saved.
5. `report.md`, `report.json` and the contact sheet `index.html` are written; the server, Redis and the browser stop.

Network: requests for the CDN are answered from the cache, `images.evetech.net` with a grey square, Google Fonts with
an empty style sheet (so Lato and Roboto fall back to other fonts; widths of text differ a little from a real
browser), anything else with an empty answer and a line in the report.

## Modes

| Mode | Theme in Alliance Auth |
|---|---|
| `bootstrap` | Bootstrap |
| `bootstrap-dark` | Bootstrap Dark (`data-bs-theme="dark"`) |
| `flatly` | Flatly (the default) |
| `darkly` | Darkly (`data-theme=darkly` only; Bootstrap's own dark variables do not apply) |
| `materia` | Materia |

375 uses a phone profile (touch, mobile user agent, so Alliance Auth collapses its menu), 768 a tablet profile,
1280 a desktop window. Reduced motion is switched on for every run.

## Pages and lanes

`seed.py` holds the page list (`build_pages`): an id, the role that opens it, the URL (built with `reverse()`), the
status the page must answer (200, 403 or 404; any other answer is a FAIL), optional states and what the tree must
have for the page to exist (`requires`). A page whose `requires` the tree does not meet is listed as **not in this tree**
and skipped, not failed. Every page of the list answers in this tree (the group pages and the Send Ping placeholder
included), so nothing is skipped.

The lane of a page follows from its id (design 4.1, rule 6):

| Lane | Page ids |
|---|---|
| A | `start`, `start-*`, `presets`, `scheduled`, `edit-scheduled` |
| B | `fleet-*` (except `fleet-pending`, `fleet-rejected`, `fleet-resubmitted`), `pap-link-register`, `pap-fleet-start` |
| C | `operations*`, `manual-fleet`, `roster-import`, `edit-operation`, `approvals`, `incentives`, `fleet-pending`, `fleet-rejected`, `fleet-resubmitted` |
| D | `dashboard*`, `my-*`, `leaderboard`, `corp-*`, `all-corps`, `alliance-members`, `history-*`, `fc-stats*`, `member-detail` |
| E | `srp-*`, `config*` (except `config-groups`, `config-group-edit`), `config-error`, `audit` |
| F | `send-ping-*`, `config-groups`, `config-group-edit` |
| S0 | `denied-*`, `notfound-*` (checks of Step 0, no lane) |

`--list` prints the pages with lane, role, status and states.

### States

A page with a `states` entry is loaded and then driven by `states.js`; the page it ends on is checked like any other.

- `start-error` (role `fc`): the start page is filled in (FC character, fleet type, form-up, ping channel "Alliance Pings",
  full mode), 2,100 characters go into the additional information and the form is sent with
  `HTMLFormElement.submit()` so no script check stops it. The server answers with the page-level error "The ping is
  ... characters; Discord accepts at most 2000." (no ESI call). In English the text must be on the page afterwards.
- `config-error` (role `admin`): the SRP payout rule form is sent with an amount but neither ship nor doctrine; the
  model's "Choose a ship type, a doctrine, or both." is a form-level error.
- `pap-tab` (roles `fc` and `lead`, pages `fleet-active-manager-pap` and `fleet-active-lead-pap`): the PAP tab of the
  running fleet is opened, so the control for the PAP characters per player above the preview is on the page. In
  English the text "PAP characters per player" must be on the page afterwards.

The interaction states (harness v2) are pages of their own, so each one is checked and photographed in every mode:

| State (page id) | Role | What it does |
|---|---|---|
| `hover-member` (`fleet-hover-member`) | `fc` | hovers a member row: the Move and Kick buttons show |
| `focus-tree` (`fleet-focus-tree`) | `fc` | Tab into the fleet tree and back (focus rings) |
| `member-menu` (`fleet-member-menu`) | `fc` | opens the "..." menu of a member |
| `rename` (`fleet-rename`) | `fc` | opens the rename field of a wing |
| `highlight` (`fleet-highlight`) | `fc` | clicks a function row: its pilots are highlighted, the others dimmed |
| `move-dialog` (`fleet-move-dialog`) | `fc` | ticks a member, opens Move to... |
| `invite` (`fleet-invite`) | `fc` | opens the invite dialog and waits for the candidates |
| `drag` (`fleet-drag`) | `fc` | dispatches `dragstart` and `dragover` with a `DataTransfer` whose `setDragImage` is watched (a missing drag image fails) |
| `confirm-end` (`fleet-confirm-end`) | `fc` | opens the End Fleet confirmation (dialog or shared question) |
| `start-preview` (`start-preview`) | `fc` | fills the start page, waits for the Discord preview, switches to the raw text |
| `start-confirm` (`start-confirm`) | `fc` | submits with a channel: the question is open, nothing is sent |
| `start-group` (`start-group`) | `fleetleader` | the group start page: the preview has the group line |
| `send-ping-preview`, `send-ping-confirm` | `pingleader` | Send Ping: the preview, then the question before posting |
| `confirm-reject` | `lead` | Reject on the approvals page asks first |
| `srp-select` | `srp` | ticks a claim: the bulk button is enabled |
| `srp-check-loading` | `member` | the loss check shows the skeleton loss card while its answer is held back |
| `month-pick` | `stats` | focuses the month selector and moves it |
| `toast` | `member` | shows every level of `FleetOps.toast` |

States use field names (`name="additional_message"`) and the data attributes of the controls, not layout. A state
that cannot find what it needs fails with a message naming it.

## Checks

Every finding has a level: **FAIL** (the run exits 1), **WARN** (listed for review; some are owned by the theme) or
**INFO**. Exempt everywhere: `[data-ui-check-skip="reason"]`, `.fo-ingame` and `.fo-discord` (they imitate other products).

| Check id | What | Level |
|---|---|---|
| `status` | answer is not the expected status, or the page redirected somewhere else | FAIL |
| `contrast` | WCAG 2.x: text 4.5:1 (3:1 large text) against the composited background, in `.fleetops`, toasts, confirm dialog, open modals, dropdowns, tooltips and the counts in the menu bar. Gradients at every stop and their average; a background image is INFO. Disabled controls and their labels are exempt. The solid `.btn-primary` of the theme is a WARN | FAIL / WARN |
| `overflow` | the page, or the area Alliance Auth scrolls, is wider than the window | FAIL |
| `clipped` | text with `overflow: hidden` or an ellipsis that does not fit its box | FAIL |
| `touch` | `.btn`, `.page-link`, `.nav-link`, `.dropdown-item`, `[role=button]`, `summary`, `.fo-icon-btn`, `.fleet-toggle`, checkbox/radio (label box counts), `select` smaller than 44x44 | FAIL at 375, WARN at 768 |
| `names` | a control without text, `aria-label` or `title`; an icon-only control without `title` | FAIL |
| `classes` | a class of rule R1 present in the DOM (finds markup inserted by scripts) | FAIL |
| `type` | text under 12 px or lighter than 400; `h2` not 28 px (24 px under 576 px) / 600; card and dialog titles not 17 px / 600; metric values not 36-44 px / 500 | FAIL |
| `type-body` | paragraph and table text outside 15-16 px, `.form-text` outside 13-14 px | WARN |
| `numbers` | text with a digit without `font-variant-numeric: tabular-nums` | FAIL |
| `images` | an image without a 1 px border and a radius | FAIL |
| `primary` | more than one solid `.btn-primary` in a card, form, modal or action bar | WARN |
| `focus` | Tab through up to 40 controls of the page: each needs an outline or box shadow; an outline needs 3:1 against what lies behind it (the parent's background for an outline outside the control, the control's own background for one inside it) | FAIL |
| `motion` | with reduced motion asked for, an animation is running (`document.getAnimations()`), or `.fo-live-dot` still animates | FAIL |
| `console` | console error or script error (errors about files that were not served from the cache are skipped) | FAIL |
| `http` | an answer of 400 or more, or a failed request, for a file of the site | FAIL |
| `state` | a scripted state could not run or did not produce its message | FAIL |
| `harness` | the page or the theme switch could not be done | FAIL |

## Report

`report.md` (read it first): run data, FAIL counts per theme and width, per page, then every FAIL and WARN
grouped by page, check and element with the modes it was seen in, pages not in this tree, external files that were
not served from the cache, and problems found in the server log. `report.json` has everything: `run` (parameters,
checkout, versions), `summary` and `results`, one entry per page load:

```json
{"page": "dashboard", "lane": "D", "role": "member", "url": "/fleetops/", "lang": "en", "theme": "darkly", "width": 375,
 "status": 200, "expected": 200, "screenshot": "en/darkly/375/dashboard.jpg", "contrastChecked": 63, "focusChecked": 4,
 "findings": [{"check": "contrast", "level": "FAIL", "selector": "div.fleetops-metric > div.fleetops-metric-title",
               "text": "CTA PAP this month", "ratio": 1.05, "need": 4.5, "fg": "rgb(255, 255, 255)", "bg": "rgb(248,249,250)",
               "count": 1}]}
```

Identical findings on one page (for example 25 table rows) are counted once with a `count`. `index.html` is the contact
sheet: pick the language and width at the top, a frame is red when the page has a FAIL; screenshots are in
`<lang>/<theme>/<width>/<page>.jpg`.

## Notes on the example data

- The seeded FC characters have tokens that look valid but are not: the start page shows its "ESI denied access to this
  character's fleet" hint. That is a state of the page, not a failure of the check.
- Ping channels have example webhook addresses; nothing is ever posted (the network guard blocks it).
- The fleet page shows the stored state of the seeded fleets; live refresh is switched off (`fleet_live_refresh_seconds = 0`).

## Extending it

- A new page: add a `Page(...)` to `build_pages` in `seed.py` (use ids that follow the lane patterns) and any object it
  needs to `build_data`. Give `requires` for things a tree may not have.
- A new scripted state: add a function to `states.js` and name it in the page's `states`.
- A new check: add it to `checks.js` (layout, DOM) or `contrast.js`; return findings with `check`, `level`, `selector`, `message`.
- The class and variable lists of rules R1 and R2 are in `rules.json`; `tools/check_ui.py` and the browser check read it.

`tools/ui_check/.work/`, `.cache/` and `out/` are not committed (`.gitignore`), and the folder is not in the source distribution (`MANIFEST.in`).
