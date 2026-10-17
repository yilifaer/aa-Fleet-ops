# Manual testing

The automated suite never calls ESI, zKillboard or Discord. Before a release, run this checklist on a separate
Alliance Auth site with real EVE SSO and ESI, a Discord test server, test characters and, for the fleet
sections, an EVE client in a fleet. Do not share the production database, Redis, Discord webhook or EVE SSO
application with the test site.

Work through the sections that the release touches; run everything before a major release. Write down anything
unexpected as a list of page, what you did, what you saw and what you expected, and report it as an issue.

## 1. Install smoke test

Install the wheel that will be released (the file from the GitHub Release, or the one `python -m build`
produced) with `pip install --no-deps <wheel>` next to the Alliance Auth test site.

- [ ] Alliance Auth starts and works before FleetOps is added.
- [ ] `fleetops` is in `INSTALLED_APPS` and the two beat entries are in `local.py`.
- [ ] `python manage.py check` passes.
- [ ] `python manage.py migrate fleetops` runs without warnings.
- [ ] `python manage.py collectstatic --noinput` passes.
- [ ] `python manage.py fleetops_seed --demo` passes.
- [ ] `python manage.py fleetops_prune_history --dry-run` and `fleetops_refresh_names --dry-run` run.
- [ ] The web process, the Celery worker and Celery beat restart without import errors.
- [ ] `/fleetops/` loads for a user with `fleetops.basic_access`, and the menu entry appears.
- [ ] The Start Fleet and Administration pages render.
- [ ] The site works with `fittings`, `eve_sde`, aa-srp and aa-charlink absent, and with each of them present.
- [ ] A restart keeps the data.
- [ ] No HTTP 500 on basic navigation.

## 2. Authorization and fleet start

- [ ] Authorize a main and an alt with **Authorize / Refresh Fleet ESI**. Both appear only for the owning user,
  and another user's character cannot be selected.
- [ ] A missing, expired or under-scoped token shows a clear message.
- [ ] A character that is not in a fleet: the start is refused.
- [ ] A character in the fleet commander position who is not the fleet boss: refused, and the message names the
  current boss.
- [ ] The fleet boss sitting as squad member, wing commander and fleet commander: accepted. The detected fleet
  ID and member count are shown.
- [ ] Choose fleet type, doctrine (or Custom), form-up, comms, Logi and Boost channels, ping channel and ping
  target. The previews show the missing fields, the length counters and the right mention.
- [ ] **SEND PING & START FLEET**: every step shows its own result (SRP link, ping, MOTD, tracking).
- [ ] Double-clicking the button or sending the form twice creates one fleet.
- [ ] Starting a fleet that is already tracked opens the running record.
- [ ] **PAP Tracking Only** detects the boss and tracks, but skips the ping, the MOTD and SRP; the previews can
  still be copied.
- [ ] A **Pre ping** for a later time posts the ping with a Discord timestamp and creates a scheduled fleet. Only
  the FC who scheduled it can start it; managers can edit and cancel it, and the cancellation notice mentions
  nobody.
- [ ] Hand the boss role to another pilot, then start a fleet with the new boss: the old record ends with "fleet
  boss handed over" and the new record starts.

## 3. Ping, MOTD, comms and channel links

- [ ] The ping arrives in the chosen Discord channel with the exact mention, and `@everyone` or `@here` typed
  into the additional text does not notify anybody.
- [ ] A role is mentioned only when it is mentionable; a role that is not allowed in the channel is refused.
- [ ] A ping over 2000 characters is refused before it is sent.
- [ ] Break the webhook: the start continues, the Discord step shows the failure, **Copy Ping** works and
  **Retry Ping** sends it after the webhook is fixed.
- [ ] The MOTD is set in game, and the comms link opens the join page (logged out too, with
  `APPS_WITH_PUBLIC_VIEWS`). The Logi and Boost names are links to the in-game channels.
- [ ] Remove the write scope from the FC token: the MOTD step fails without stopping the start; **Copy MOTD**
  works.
- [ ] **Regenerate & Push MOTD** after a template, comms or doctrine change updates the MOTD in game.
- [ ] **Issue a new join link** makes the old link stop working.
- [ ] Doctrine and SRP links in the ping and MOTD open the right pages (with `SITE_URL` set).

## 4. Tracking

With test characters:

- [ ] Members appear on the Members tab; join, leave, rejoin, ship, solar system and role changes show on the
  Events tab.
- [ ] Time in fleet grows with each poll; the last ESI update and the stale marker behave.
- [ ] Restart the Celery worker, then Celery beat, then the web process: tracking resumes without the browser.
- [ ] Two fleets at once are tracked without blocking each other.
- [ ] Disband the fleet: after the configured number of unreadable polls the fleet ends as "Fleet no longer
  exists or the FC left it". In a second fleet, hand the boss role over: the page warns, handing it back in time
  keeps the fleet open, otherwise it ends as "fleet boss handed over".
- [ ] A short ESI outage does not end the fleet.
- [ ] With `eve_sde`, the 3-Jump Map lists members up to three jumps from the FC's system.

## 5. Fleet tab and kicks

- [ ] The Fleet tab shows the live tree with wing and squad names and the composition (ship functions, ship
  class, bridgeable mass), and refreshes by itself.
- [ ] Add, rename and delete wings and squads; drag a member onto a squad, a squad head, a wing head and the fleet
  head; use **Move to…**; select several members and move them as a background job.
- [ ] **Invite…** an Alliance Auth character; the invitation shows for up to two minutes.
- [ ] **Apply template…** with and without deleting empty wings and squads.
- [ ] After the boss role is handed over, or without the write scope, the controls are disabled with the reason.
- [ ] **Kick** one member, **Kick outside system…** (the FC's system preselected, some members spared) and
  **Kick Capsules**. More than 10 kicks run on the worker and show under the fleet's steps. The FC and the fleet
  boss are never kicked.
- [ ] Members without `view_all_fleets` see no tree and no kick details while the fleet runs, and see the
  composition after it closes.

## 6. End, PAP and multipliers

- [ ] End a fleet shorter than the first tier: it closes at once at 1x.
- [ ] End a fleet longer than a tier: the dialog offers the allowed multipliers; a multiplier the duration does
  not allow is refused by the server.
- [ ] The fleet page shows "Ending…" and then the closed fleet; PAP appears.
- [ ] **PAP minimum time**: characters below it get no PAP.
- [ ] **PAP characters per player** (try 1 and 2): further characters are "Capped (limit)"; the FC character, then
  the main, then the longest time in fleet keep the PAP.
- [ ] **PAP characters per player of one fleet**: the Start Fleet page (and the PAP-link fleet page) offers the
  field with the setting as default. As a normal FC a value above the setting, or an empty field, is refused; as an
  FC lead any value and an empty field work. A preset keeps the value.
- [ ] While the fleet runs, the control above the PAP preview lowers the limit; the preview changes at once and the
  PAP at the close follows the latest value. A normal FC cannot raise it above the setting; an FC lead can.
- [ ] After the close only an FC lead sees the control, asks for a reason, and the PAP is calculated again; the audit
  log has the old and the new value and the reason (`pap.limit`).
- [ ] Characters that are not in Alliance Auth are listed as "Not in Alliance Auth" and get no PAP.
- [ ] A fleet that ended without a choice shows **Multiplier pending**; the FC settles it.
- [ ] An FC lead overrides a multiplier with a reason; the audit log keeps it.
- [ ] **Do not count** removes a fleet from the statistics; **Minimum fleet duration** leaves out short fleets.
- [ ] **Recalculate PAP** keeps the same result.

## 7. Manual records and approvals

- [ ] An FC creates a manual fleet in EVE time (the end must be after the start; over 6 hours asks for a
  confirmation). It appears as manual in the archive and waits for approval.
- [ ] An FC adds manual PAP by name and from a pasted roster; the entries wait for approval.
- [ ] An FC lead approves and rejects (a rejection needs a note) on **Approvals**. The FC lead's own entries
  are approved at once.
- [ ] Changing the times of an approved fleet puts it back to pending.
- [ ] An FC deletes their own pending manual PAP; an FC lead deletes approved rows.

## 8. PAP links

- [ ] An FC creates a PAP link on an active and on a closed fleet. A pilot opens it, selects characters and
  registers them with the three location scopes (**Add a character (EVE login)** adds a token).
- [ ] **Require online** blocks an offline character; unticking it allows it.
- [ ] A character registers once per fleet. An expired or revoked link accepts nothing.
- [ ] A registration on a closed fleet recalculates PAP at once.
- [ ] **No ESI fleet? Start a PAP-link fleet** creates a fleet without ping, MOTD or tracking; End Fleet closes
  it, and it waits for approval unless an FC lead started it.

## 9. Statistics and exports

Use a month with real PAP.

- [ ] My Statistics, My History, the monthly minimum and the rank are right.
- [ ] Set **Member alliance IDs** and a **Monthly Target**. All Corps for a finished month: corporations at or
  above the target are green, the others red; for the running month "Not reached yet" is amber.
- [ ] Average PAP equals total PAP divided by the main characters; alts do not change the denominator.
- [ ] Members and Inactive as a corporation manager list only the own corporation; another corporation's page
  answers 403 or 404. As `view_all_stats` the whole alliance is listed.
- [ ] The leaderboard as a plain member; after switching **Leaderboard visibility** to statistics managers
  only, the entry disappears and the page answers 403.
- [ ] A fleet that started shortly before or after midnight UTC at the edge of a month counts in its EVE month.
- [ ] Download Excel and CSV of members and corporations and open them in Excel or WPS: Chinese names are
  intact, the numbers equal the page, the Excel file has an About sheet.
- [ ] PAP history: month filter, previous and next month, **All retained**.

## 10. FC incentives

Tick **Incentive enabled** first; the FC Incentive menu entry appears only while it is on.

- [ ] Minimum fleets, fleet type weights and multipliers give the expected points.
- [ ] A non-eligible FC and a waived FC get no payout; the remainder goes to the top FC.
- [ ] Review, finalize, unlock and recalculate work, and every step is in the audit log.

## 11. SRP

- [ ] Enable FleetOps SRP, set the provider to *Automatic*, add `{{ srp_url }}` to a custom MOTD and ping, give
  `manage_srp` to an SRP manager.
- [ ] Add a doctrine ship to the payout table, set a percentage and the caps and monthly limits.
- [ ] Start a fleet: the link is in the MOTD and the ping; open it in game.
- [ ] Lose a cheap ship in the fleet and claim it with the in-game link at once; claim a second loss with a
  zKillboard link after ten minutes; claim an out-of-fleet loss from My SRP.
- [ ] Try an over-limit claim in both modes, approve with a changed amount, mark paid, revoke, and open the
  summaries and the Excel file.
- [ ] If aa-srp or Alliance Auth SRP holds a request for the same killmail, the preview shows "Also claimed in …".
- [ ] With aa-srp or Alliance Auth SRP as the provider, the fleet gets a link there; with no SRP app, the start
  continues and the SRP step shows the reason.

## 12. Permissions

Create the five groups of [permissions](permissions.md) and test one user in each.

- [ ] Member: own fleets, history and statistics only; no Start Fleet.
- [ ] Corporation manager: the own corporation only.
- [ ] FC: starts and manages own fleets; reads every fleet; cannot change another FC's fleet.
- [ ] FC lead: everything except SRP unless the SRP permissions are added.
- [ ] SRP manager: decides on other people's claims, not on their own.
- [ ] A user without `basic_access` gets 403 on every page; the comms join page works as configured.

## 13. Language

- [ ] Switch the Alliance Auth language menu to Simplified Chinese: walk every FleetOps page (menu, Start Fleet,
  the fleet page, the End Fleet dialog, PAP pages, statistics, exports opened in WPS, administration, SRP,
  audit), start and end a test fleet, and check that step messages and the end reason are in Chinese.
- [ ] The Discord ping and the in-game MOTD are exactly the same as when started in English.
- [ ] Switch back to English and open the same fleet page. A second account in the other language sees the same
  fleet in its own language.
- [ ] Report wording to change as a list: page, current text, wanted text (the translation issue form).

## 14. Upgrade from the previous release

Restore a copy of the data of a site that runs the previous release, then:

- [ ] Back up the database, install the new wheel, run `migrate fleetops` and `collectstatic`, restart.
- [ ] Old fleets, PAP, statistics, history, SRP claims and settings are intact.
- [ ] The notes in the CHANGELOG for the release were enough to finish the upgrade.
