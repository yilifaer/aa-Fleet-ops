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
  **Retry Ping** sends it after the webhook is fixed (it renders the ping again first; an ended fleet has no retry buttons).
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
  answers 404 with the reason ("You can only open ..."), the same page for a corporation that does not exist.
  As `view_all_stats` the whole alliance is listed.
- [ ] The leaderboard as a plain member; after switching **Leaderboard visibility** to statistics managers
  only, the entry disappears and the page answers 403.
- [ ] A fleet that started shortly before or after midnight UTC at the edge of a month counts in its EVE month.
- [ ] Download Excel and CSV of members and corporations and open them in Excel or WPS: Chinese names are
  intact, the numbers equal the page, the Excel file has an About sheet.
- [ ] PAP history: month filter, previous and next month, **All retained**.

## 10. FC incentives

Tick **Incentive enabled** first; the FC Incentive entry (in the More menu) appears only while it is on.

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
- [ ] Standing links: make a category with **Make a standing link every day**; after the next beat the link of the
  day exists and My SRP lists it. A loss of yesterday claimed through today's link shows the warning "The loss
  happened on ... EVE; the link is for ...". With **Close links after (hours)** set, the link closes at its time
  and the audit log has `srp.link_close` without an actor.
- [ ] Not covered: on SRP Settings press **Fill in the recommended list** (nothing is saved or audited yet), save it,
  then check a Covetor loss and a high security loss: both are refused with their reason; with a payout table entry
  for the Covetor it is accepted. A loss in low security is accepted. My SRP shows the "Not covered" card.
- [ ] SRP ban (reviewer): ban a member for 7 days with the pending claims rejected; the member sees the banner, cannot
  check or submit, can still withdraw. Lift the ban: the member can check again. Payers and members get 403 on
  **SRP** > **SRP Bans**.
- [ ] If aa-srp or Alliance Auth SRP holds a request for the same killmail, the preview shows "Also claimed in …".
- [ ] With aa-srp or Alliance Auth SRP as the provider, the fleet gets a link there; with no SRP app, the start
  continues and the SRP step shows the reason.

## 12. Permissions

Create the five groups of [permissions](permissions.md) and test one user in each.

- [ ] Member: own fleets, history and statistics only; no Start Fleet.
- [ ] Corporation manager: the own corporation only.
- [ ] FC: starts and manages own fleets; reads every fleet; cannot change another FC's fleet.
- [ ] FC lead: everything except SRP unless the SRP permissions are added.
- [ ] SRP manager (`manage_srp`): decides on other people's claims, not on their own, and pays other people's claims only.
- [ ] SRP reviewer only (`review_srp`): approves and rejects, can press Recalculate, sees no Mark paid; cannot decide on own claims unless the self-review setting allows it.
- [ ] Payments page, by claim: the switch **By player** / **By claim** keeps the choice after a reload and for the next visit; a payer who never chose sees the cards. In **By claim** every row has the claim number with **Copy** (paste it into a text field: `SRP-<number>`), **Copy** for the pilot and the amount; **Paid** on a row pays only that claim (a batch of one, the same note field, the same notice to the member); your own claim and, with the four-eyes rule on, a claim you approved have no **Paid** button; the filters, the exports and **Mark selected paid** work as before; on a phone the table scrolls sideways and the buttons are usable. In **By player** **Copy claim numbers** pastes `SRP-1 SRP-2` for the payable claims of that card only.
- [ ] Open in game (needs `esi-ui.open_window.v1` on the EVE application): a payer without an in-game character sees the link **Set up opening characters in game**; **Add and authorize a character** goes through the EVE login and comes back to the box; **Use this character** makes the buttons appear; **Open in game** shows a short message and opens the character in the client that is logged in with the in-game character (if not, open any character window in the game once and try again); with the game closed the message tells what to do; **Remove the choice** removes the buttons and keeps the token (Alliance Auth still lists it). A user without `pay_srp` and without `manage_srp` cannot call the endpoint (403).
- [ ] SRP payer only (`pay_srp`): marks paid, sees no Approve and no Recalculate (only the note that a reviewer can recalculate); cannot pay own claims unless the self-payment setting allows it; with the four-eyes rule on, cannot pay a claim they approved.
- [ ] Superuser: acts as reviewer and payer and is bound by the same three settings.
- [ ] A user without `basic_access` gets 403 on every page; the comms join page works as configured.

## 13. Language

- [ ] Switch the Alliance Auth language menu to Simplified Chinese: walk every FleetOps page (menu, Start Fleet,
  the fleet page, the End Fleet dialog, PAP pages, statistics, exports opened in WPS, administration, SRP,
  audit), start and end a test fleet, and check that step messages and the end reason are in Chinese.
- [ ] The Discord ping and the in-game MOTD are exactly the same as when started in English. With `LANGUAGE_CODE`
  set to a Chinese code they stay English as well, field labels and footer included, while the pages are Chinese.
- [ ] Switch back to English and open the same fleet page. A second account in the other language sees the same
  fleet in its own language.
- [ ] Report wording to change as a list: page, current text, wanted text (the translation issue form).

## 14. Upgrade from the previous release

Restore a copy of the data of a site that runs the previous release, then:

- [ ] Back up the database, install the new wheel, run `migrate fleetops` and `collectstatic`, restart.
- [ ] Old fleets, PAP, statistics, history, SRP claims and settings are intact.
- [ ] The notes in the CHANGELOG for the release were enough to finish the upgrade.

## 15. Interface, groups and the 0.2.0b2 additions

- [ ] **Theme and width.** Flatly (the default), Darkly and Materia on a phone (375 px) and on a desktop, and Bootstrap
  Dark if a site uses it: the dashboard, Start Fleet, the fleet page, the approvals, SRP review and Send Ping have
  readable text, buttons that can be tapped, and nothing hidden behind the fixed action bar. There is one solid
  primary button in each region and no decoration that carries no meaning. In Materia the primary button is darker
  inside FleetOps than in Alliance Auth's own pages.
- [ ] **Drag and drop.** On the Fleet tab drag a member to another squad in Chrome, Edge and Firefox at 100,
  125 and 150 % browser zoom: the dragged label stays small, the tree does not jump, and only valid squads light up.
- [ ] **Keyboard.** Tab through the fleet tree, open the "..." menu of a member with Enter, move a member from the
  Move dialog, confirm and cancel the End Fleet dialog with the keyboard.
- [ ] **Export.** Open the members export of a month that has members without a rank in Excel, WPS, Numbers
  and macOS Quick Look: every row keeps its columns and nothing shifts to the left.
- [ ] **Group pings.** For a group that sends pings only and a group that also starts fleets: Send Ping reaches only
  the group's channel in Discord, a second ping to the same channel within 60 seconds is refused, the group fleet's
  ping carries the "Group" line, and a leader never sees an alliance channel.
- [ ] **MOTD links.** Start a fleet with the built-in MOTD: in the game the FC name opens the character's information
  window and a known form-up system opens the system's. Try an FC whose bloodline is not Amarr.
- [ ] **Confirmations.** Posting a ping, Reject (fleet, PAP rows, SRP claim) and Undo payment ask first; approving
  does not.

## 16. The SRP rework of 0.3.0b1

Do this on a test site, never on a production site. Back up the test database first; the rollback drill below needs the
backup of the state before the upgrade. Every step ends with a look at the **Audit log**: the action is there with the
right actor.

**Preparation**

- [ ] The EVE application of the test site has `esi-killmails.read_killmails.v1`; then the package was upgraded
  (README, *Upgrading*). Without the scope, the loss reading shows the hint for administrators and nothing else breaks.
- [ ] Configure the site as in the [example for an alliance that moves from aa-srp](configuration.md#example-an-alliance-that-moves-from-aa-srp)
  (categories, caps, block lists, loss discovery, daily links). Give `review_srp` and `pay_srp` to test groups; the
  upgrade grants nothing.

**Walk through**

- [ ] Authorize the main character and two alts for loss reading (CharLink or the **Loss reading** card). A character
  without a token is listed as not authorized; with the setting **Members must authorize loss reading for all their
  characters** on, checking and submitting are refused until it is done, and a token of another account does not count.
- [ ] Lose a ship with each of the three characters: all three appear under **My recent losses** after the scan (or after
  **Refresh**, at most every five minutes).
- [ ] A loss that matches a tracked fleet is preselected as in fleet. A solo loss of 300m in a category with a 250m cap is
  suggested at 250m; a 571m loss through a fleet link at 571m.
- [ ] As the member: change the category and the comment of a pending claim, replace the loss; the audit has
  `srp.claim_member_edit`. A banned member can only withdraw.
- [ ] As a reviewer: change the claim while the member edits it (the page shows the old number of changes) and see the
  refusal; filter the queue by category, link, date and member; approve; recalculate.
- [ ] As a payer: mark several claims paid in one step (one batch `PAY-n`, one audit entry), export the claims to pay and
  copy "name and amount" (tab-separated); the total of the file is the total on the page. A reviewer gets 403 on the batch.
- [ ] Close several links at once, then complete the ones whose claims are all settled; a link with a claim waiting is
  refused with the reason and not closed by the attempt.
- [ ] The next day: the daily link of every category exists and the ones past their closing time are closed
  (`srp.link_close` without an actor).
- [ ] Ban a member for 7 days with the pending claims rejected, then lift the ban.
- [ ] A Covetor loss and a high security loss are refused with their reason on the check page and on My SRP (the
  buttons are missing there); a payout table entry for the Covetor lifts the block.
- [ ] Open **SRP** > **Reports**: a custom range, the presets, the category report with used and remaining for a single
  month, the personal limits report; download two files and check the file names (`...-YYYY-MM-DD_YYYY-MM-DD`).
- [ ] Import: on a **copy** of the aa-srp database run `fleetops_import_aasrp --dry-run` (pending must be 0 after you
  decided them in aa-srp), compare the report with the counts of the old site (links, requests, approved ISK per month), import for real in a maintenance window, open the reports over the imported months, try to claim
  an imported killmail (refused), then `--undo` and import once more: the same result.
- [ ] Each step above left its entry in the audit log.

**Rollback drill** (on a copy, README and CHANGELOG rollback notes)

- [ ] Functional rollback: switch off loss discovery, daily links and member changes; clear the block lists.
- [ ] Data rollback: `fleetops_import_aasrp --undo`, delete or generalise the category payout rules, delete the standing links
  with claims, run `fleetops_srp_rollback_check` (it must pass), `migrate fleetops 0023`, put the 0.2.0b3 code back (the wheel or commit the site used before; it was never released) and start it. From a 0.2.0b2 site the target is `migrate fleetops 0021`.
  Claims, amounts and statuses are as before; the categories, standing links, batches, bans and found losses are gone.
- [ ] With imported claims, rules of a category or standing links with claims still in place, `migrate fleetops 0023`
  stops at `0044` (its guard runs before the fleet type columns are dropped), so nothing is undone; without the stop, the help text of `0038` and the index of `0037` would be undone next, and `0036` runs its guard before the pruning share column is dropped.
- [ ] A loss of a pilot who was in a tracked fleet, checked from My SRP as a loss outside a fleet: with the default setting the check shows the fleet and the fleet amount; **Submit as a loss outside a fleet anyway** needs a reason and the claim page shows it; with **Block** the check has no submit button and offers the link of the fleet; with **Only suggest** it only asks "Submit it as a fleet loss?". Close the fleet link after the check and submit: the claim is made outside a fleet with the closed-link warning.

**Version and database matrix** (CI runs SQLite and MariaDB with the whole suite and the migration tests on PostgreSQL; the PostgreSQL job covers the migrations only, so run the rest of this list on PostgreSQL by hand for a release)

- [ ] Alliance Auth 5.2 with django-esi 9, the latest 5.x with django-esi 9 and with django-esi 10: the app starts, and
  the loss reading, the universe lookups and the insurance prices work (their ESI operations exist in the client).
- [ ] SQLite, PostgreSQL and MariaDB: an empty database migrates, `migrate fleetops 0023` and `migrate` go round, and so
  does a database with 0.2.0b3 data (claim amounts and statuses unchanged, 0 claims "calculated with earlier settings").
- [ ] Without aa-srp the import command stops with a clear message; without django-eveonline-sde the location and the ship
  group come from ESI and a failed lookup lets the claim through; without aa-charlink the authorization page works; without
  fittings nothing changes.

## 17. Fixes of 0.3.0b2

- [ ] SRP settings: turn on **Members must choose a category for losses outside a fleet** with no default category and
  no category members can choose: saving is refused with the three ways out. With one choosable category, deactivating,
  hiding or deleting it is refused; with a second one or a default category it is allowed. On a site that is already in
  that state, the settings page and **My SRP** show the warning, and the other settings (the claim deadline, for example) can still be saved.
- [ ] Open an older fleet that was started by 0.2.x and use **Retry Ping**: the posted ping has no `fitting:` or `<font`.
  A ping without a fleet and a fleet ping to the same channel within 60 seconds: the second one is refused with the wait.
  The same for a scheduled fleet started less than a minute after its pre ping: the fleet starts, the ping step shows the wait.
- [ ] Edit a fleet and save without changing anything: the start and end keep their seconds and the audit log has no
  new **operation.edit** entry. Start a fleet with automatic SRP link: **srp.link_create** names the person who started it; press **Retry SRP** as another FC lead on a fleet without a link: it names that lead.
- [ ] A group leader who started a group fleet does not see the roles marked FC credit when assigning a special role; an FC
  lead does.
- [ ] Delete an Alliance Auth group that has FleetOps group settings in the Django admin: it works and the audit log has one
  **Group deleted** entry.
- [ ] With **SRP reviewers may decide on their own claims** off, press **Recalculate** while only your own claims wait: the
  message is a warning that says nothing was recalculated; with other claims waiting it says how many of yours were skipped.
- [ ] A Chinese export or report ends with its total row in Chinese, on a site where other apps are installed too.
- [ ] An ended fleet shows no **Retry Ping** or **Retry MOTD**.
- [ ] The ship image on the check page, the claim page and the loss cards is square (64 by 64) at every width.
- [ ] Make the claim deadline longer: losses that were marked too old show up on **My SRP** after the next scan.
- [ ] Turn on **Notify members when a payment is undone** (with **Notify members** on), undo a payment with a reason: the
  member gets a notification with the reason, in their language. With either switch off nothing is sent.
- [ ] Doctrine Settings (ship order): open the page from the Administration index, edit a doctrine with two fits of one hull: each hull is
  listed once. Move a ship with the buttons (also with JavaScript off and on a phone), change a mark, watch the preview, save.
  Regenerate the MOTD of an open fleet with that doctrine: the in-game MOTD and the next ping show the stored order, the marks
  as `>`, `=` and `>>>`. Add a fit with a new hull to the doctrine: it is appended with `>`. **Back to the automatic order**
  removes the order. The audit log has the three entries. A user without the configuration permission gets 403.
- [ ] Start a fleet with a doctrine named `[ACTIVE] Example Doctrine`: the Discord ping shows the doctrine as a link with the
  whole name, the MOTD shows the name as typed.

## 18. Fixes of 0.3.0b3

- [ ] Start a fleet with a doctrine that has a stored ship order with `>` and `>>>` marks and a form-up text with `<`,
  `>` or `&`: the MOTD in the game shows the marks and the text as typed; the FC and the form-up system are still
  clickable with the built-in MOTD template. A custom MOTD template that uses `{{ doctrine_ships|force_escape }}`
  shows the marks once, not as `&gt;`.
- [ ] A MOTD template that uses `{{ fc }}` and `{{ formup }}` shows plain text in the game; the template edit page says
  that `{{ fc_link }}` and `{{ formup_link }}` make them clickable.
- [ ] On a site in the dead end of the SRP settings (required category, no default, no choosable category) save another
  setting such as the claim deadline: it is saved and the warning stays. Turning the requirement on in a working
  configuration that has no way out is still refused.
- [ ] Send two pings to the same channel quickly: the refusal says "1 second ago" or "N seconds ago" and never 0.
- [ ] Payments page: after the token of the chosen in-game character was removed in Alliance Auth, the rows show
  **Add the opening character again**; a payer who never chose a character sees **Set up opening characters in game**.
- [ ] Chinese interface: doctrine, the paid state and button, and the open-in-game button each use one word everywhere (see
  `docs/translations.md`); the old variants are gone from every page.

## 19. Doctrine access of 0.3.0b3

- [ ] Administration, Doctrine Settings: restrict a flagship doctrine to the Caplead group. The doctrine shows **Restricted**; Save writes a
  `configuration.create` entry with the group names, a second change writes `configuration.update` with the old and the new groups.
- [ ] With an FC account that is not in Caplead: Start Fleet, Send Ping and a pre ping do not list the doctrine; the doctrine
  ID put into the request by hand (browser developer tools) is refused with *You may not ping this doctrine, ask an
  administrator.* (also in Chinese). Adding the account to Caplead shows the doctrine at once.
- [ ] A group leader of a fleet group starts a group fleet: the same list, judged by the leader's own groups.
- [ ] Schedule a fleet with the doctrine (account in Caplead), take the account out of Caplead, press **Start** on the scheduled fleet when
  due: the fleet is not started, the FC has an Alliance Auth notification in their language and the audit log has
  `fleet.schedule_start_refused`. Editing the time of that scheduled fleet still works.
- [ ] A start preset with the doctrine, loaded by an account that lost the right, says so and does not pick another doctrine.
- [ ] An administrator (with `fleetops.manage_configuration`) and a superuser can ping every doctrine without being in the group.
- [ ] In Fittings the doctrine is still visible to a plain member.
- [ ] Custom doctrine name: choose **Only the chosen groups** with the FC group. A Caplead-only account sees no **None / Custom**
  and no text box in Start Fleet, scheduled fleets and Send Ping; a typed name sent by hand is refused. Back to
  **Everybody who may start fleets** and the option is back.
- [ ] Delete a doctrine in Fittings that has a record: the page lists it under **Doctrines that no longer exist** without an error.
- [ ] Rollback: `migrate fleetops 0042` drops the two doctrine access tables (the groups stay); the doctrines are open to everybody
  who may start fleets again.

## 20. Fleet type colours and dashboard cards of 0.3.0b3

- [ ] Administration, Fleet Types, edit StratOps: choose **Custom colour**, type `#e67e22`. The preview shows the dot and a sample
  with black text. Save: the dashboard card dot, the fleet lists, the history and the statistics labels of StratOps take the colour.
- [ ] Type `#e67e2` or `red` or `#fff;}`: the page refuses it with a message and nothing is saved; **Automatic** brings back the
  old colour, a theme colour takes that dot.
- [ ] A custom colour such as `#ffffff` (invisible on the light theme) or `#222222` (invisible on the dark theme): the preview and
  the message after saving warn. Check the dot in both themes (switch the Alliance Auth theme) and at phone width.
- [ ] Turn on **Show on dashboard** for the fifth type (Alliance JOB): a fifth card appears and wraps below the first row; at
  phone width the cards are two per row. Turn it off for another type: its card is gone.
- [ ] The audit log has `configuration.update` entries with the old and the new colour and switch.
- [ ] Upgrade check: after `migrate`, the dashboard shows the same four cards as before and every type has its old colour.
- [ ] Rollback: `migrate fleetops 0043` drops the two columns (the guard runs first); the dashboard shows the first four active types again.

## 21. Fixes of 0.3.0b5

- [ ] Turn on **The approver of a claim may not pay it**. As a payer who approved one claim of a player (10 million) while another approver approved a second one (25 million): the card of that player shows 25 million, **Copy amount** and **Copy name and amount** give 25 million, the **To pay** total at the top counts 25 million, and small text says "plus 10,000,000 ISK you may not pay". A player whose claims you all approved has a card without the amount buttons.
- [ ] Administration, Ping Roles and Ping Channels: open an entry and press save without a change. The message is *Nothing was changed.* and the audit log has no new entry. Type a new webhook address and save: one entry, which never shows the address.
- [ ] Switch the interface to Chinese: the buttons **Request SRP**, **First**, **Previous**, **Next**, **Last**, the column **Created at** and the tab **Overview** have the FleetOps wording (with aa-srp installed on the site too).
- [ ] Edit a scheduled fleet right after another ping went to the same channel and tick **Publish the updated pre ping**: the message is a warning that the pre ping was not posted, with the wait, and Retry Ping works later.
- [ ] The **Audit** page has a **Changes** column with old and new values; a long entry folds out; no webhook address or token is visible.
- [ ] Reject a manual fleet or manual PAP rows with a reason: the reason is in the **Reason** column of the audit log.
- [ ] An FC without the SRP request right does not see **Open SRP Fleet** on the fleet page for a FleetOps link; a link of another SRP app still has the button.
- [ ] A loss outside a fleet without a category shows *Choose a category for this loss.*; Doctrine Settings saved without a change shows *Nothing was changed.* as an information message.
