# Changelog

## [0.3.0b6] - Unreleased

Fixes from the hand test of 0.3.0b5 on a test site.

### Fixed

Fixes from the re-test of 0.3.0b5.

- **A rejected claim cannot be submitted again once its SRP link is closed** (a fleet link or a standing link), with *Members may resubmit rejected claims* on. A withdrawn claim still can, as before.
- **A claim submitted again after an automatic rejection carries a warning too.** It reads "Submitted again after being rejected automatically by the rules on <time>: <reason>", so **Approve clean claims** skips it and a reviewer looks at it. The warning of a rejection by a reviewer shows the date and the time in EVE time ("2026-11-14 10:20 EVE"), not only the date.
- **The claim page shows no Submit again button when the submission would be refused** (a banned member, SRP switched off, a loss past the claim deadline, a closed SRP link for a rejected claim). A small text says why instead.
- **Claims rejected by an SRP ban before 0.3.0b5 are protected as well.** A rejection without the ban mark counts as a ban rejection when it was made by the reviewer who created a ban of the claimant, for that claimant, within 5 minutes after the ban was created, whether the ban is lifted or not. The member cannot submit such a claim again (a reviewer can reopen it). There is no data migration; a rejection made by another reviewer, or later than the window, is treated as an ordinary rejection.
- **The ship order page of a doctrine** shows *Nothing was changed.* and writes nothing (no audit entry) when you save the order as it is stored. The first save of an automatic order still counts as a change.
- **The payments page**: a card with nothing you may pay shows "—" instead of a large "0 ISK", and the line under **To pay** counts only the claims you may pay and their players, with "(N claims you may not pay)" for the rest.
- **More words that other plugins translate differently** now have their own FleetOps translation context in the Chinese catalog, in every place they are used (pages, forms, models, exports and the stored word "error"): Clear filters, Created, Eligible, Error, EVE time, FC name, Group name, Inactive, Main characters, Notes, Owner, Ping target, Ship classes, Ship type, Ship types, Webhook, Webhook URL, Target and the member count. A new test checks that no FleetOps text without a context is a word of a list of words known from other plugins.
- The message after editing a scheduled fleet during the ping cooldown has one space between its sentences (and none left over when the reason is empty).
- **Open SRP Fleet** is hidden from people who may not request SRP also for a fleet that records no SRP system but whose address is a FleetOps claim page (addresses of other SRP apps are never hidden).
- The **Changes** column of the audit log shows a replaced webhook address as "Webhook address replaced" (translated) instead of `webhook_url_replaced → true`; no address or token is shown.

### Changed

- **One FleetOps item in CharLink.** With aa-charlink installed its page now offers a single item, **FleetOps (PAP links, SRP, fleet control)**, for everybody with `fleetops.basic_access`. It is ticked by default and asks for all seven scopes FleetOps uses in one EVE login: `esi-fleets.read_fleet.v1`, `esi-fleets.write_fleet.v1`, `esi-location.read_location.v1`, `esi-location.read_ship_type.v1`, `esi-location.read_online.v1`, `esi-killmails.read_killmails.v1` and `esi-ui.open_window.v1`. The former items for fleet control and for loss reading are gone, and the loss reading item no longer disappears while SRP loss discovery is off. The fleet-control and open-window permissions are used only when you are the FC of a fleet or a payer; a member's token is never used to change a fleet. When a character is added while SRP and **Find losses automatically** are on, it is read in the next loss scan. Every sign-in flow that does not go through CharLink (Start Fleet, PAP links, **Authorize** on My SRP, the payments page) still asks for only its own scopes, and the **Authorize all characters with CharLink** button on My SRP is unchanged.
- **A member sees only their own part of a fleet.** A member who may open a fleet only because they took part in it (not `view_all_fleets` or `manage_fleets`, not a credited FC, not able to manage the fleet and not the leader of the fleet's group) now gets a slim fleet page: type, FC and status, start, end and duration, the text of the ping, the **Request SRP for this fleet** button while the SRP link is open (or when it closed), and **My attendance** with their own characters. Statistics, PAP rules, start steps, composition, members, map, events, special roles, MOTD and every button are no longer shown, and the live fleet state and every other fleet address answer *not found* to this member. Holders of a special role without FC credit count as members. Links to fleets stay as they are; on those rows the fleet list, the dashboard and the running-fleet cards show "—" for form-up, PAP total, tracked count, ESI fleet, member count and last ESI update.
- The **Fleet** column of **My PAP history** (and of **Last participations** on the dashboard) is called **FC**, because it shows the name of the FC; the link still opens the fleet.
- **The PAP rank card of My Statistics no longer depends on the leaderboard.** It is shown to every member who has PAP in the month, as numbers only. The **Leaderboard** link next to it appears only for people who may open the leaderboard, so limiting **Leaderboard visibility** to statistics managers no longer hides the member's own rank.

### Added

- **Rank shown to members** (Settings, **Statistics**): **Alliance members** (default), **Members of the own corporation** or **Not shown**. The sentence under the card follows the choice. Changing it is audited, and saving the page without a change still writes nothing.

### Upgrade notes

- **Every member and FC authorizes once more in CharLink.** A character counts as added only with all seven scopes, so the characters authorized so far show as not added until the member ticks the single FleetOps item and logs in once.
- **Before the upgrade, check that the EVE developer application of the site has all seven scopes** (the list above). With one missing, the CharLink login fails for everybody. The install instructions in the README and the integration page list them. FleetOps cannot read the scopes of the application, so there is no system check for it.
- One new migration, `0046`: it adds the setting **Rank shown to members**, set to the alliance. Nothing changes visibly by default, except that the rank card is also shown when **Leaderboard visibility** is limited to statistics managers. Run `python manage.py migrate`, then `python manage.py collectstatic --noinput`, and restart Alliance Auth and Celery.
- **From 0.3.0b4 or earlier:** claims an SRP ban rejected in those versions are recognised automatically (see *Fixed*); there is no data to convert. The recognition covers a rejection made within 5 minutes after the ban by the same reviewer; a claim rejected later, by someone else or by hand stays an ordinary rejection that the member may submit again when *Members may resubmit rejected claims* is on.
- The CharLink setting stored for the former PAP item (`fleetops_clickpap`) keeps working; the rows of the two removed items stay in CharLink's table unused.

### Rollback notes

- Run `python manage.py fleetops_srp_rollback_check` first and roll back only when all checks are OK (migrations 0045 and 0046 have the same guard as 0040 to 0044: it refuses a rollback that would lose data, for example while claims imported from aa-srp or payout rules with a category exist). Then `python manage.py migrate fleetops 0045` removes the setting **Rank shown to members**, and you install 0.3.0b5 (or go on to `0044`, which removes the setting **Members may resubmit rejected claims**, and install 0.3.0b4). Nothing else in the database changes.

## [0.3.0b5] - 2026-11-13

Fixes from the re-test of 0.3.0b4 on a test site.

### Fixed

- **The payments page counts only what you may pay.** On the **By player** cards the amount, **Copy amount**, **Copy name and amount** and the **To pay** total at the top now add up only the claims you may pay yourself (for example not the claims you approved while *The approver of a claim may not pay it* is on), so a transfer made from the copied amount is never too high. The rest is shown in small text ("plus X you may not pay"), and a card with nothing you may pay has no copy buttons for the amount.
- **Saving without a change on every administration page.** The Ping Roles and Ping Channels edit pages still said "saved" and wrote an audit entry with identical old and new values when nothing had changed. The decision now compares the stored values before and after the save on every page, so an unchanged save shows *Nothing was changed.* and writes nothing, while a real change (a newly typed webhook address included) is still audited, with the address hidden.
- **Words that other plugins translate differently.** A site that also runs the SRP app (aa-srp) showed its wording for some FleetOps texts, for example "Request SRP", "Registered", "SRP link", "Total", "First", "Last", "Previous", "Next", "Created at", "Expires at", "Overview", "Search" and "Killmail". These words now have their own FleetOps translation context in the Chinese catalog.
- **Editing a scheduled fleet during the ping cooldown.** With *Publish the updated pre ping* ticked, the page said the pre ping was posted although the cooldown had held it back. It now shows a warning with the reason and says that the pre ping can be retried later.
- **Rejection reasons in the audit log.** The reason typed when a fleet or manual PAP rows are rejected is now in the **Reason** column.
- **The file of the claims to pay lists only what you may pay.** The `srp-payable` export now leaves out the claims you may not pay (your own, or those you approved or re-priced under the four-eyes rule), like the payments page, so its totals and the per-player amounts are the ones to transfer. The About sheet of the workbook says how many claims were left out.
- **Open SRP Fleet** on the fleet page is hidden only for a FleetOps link when you may not request SRP (the link of another SRP app is always shown, whatever its address looks like, and a closed FleetOps link keeps its button for people who may request).
- **Words that other plugins translate differently**, continued: "Approve", "Actions", "Additional information" and "FC" have their own FleetOps context as well.
- **The Changes column hides login data in old voice links** (`mumble://user:password@host` is shown as `mumble://***@host`), webhook addresses with an API version, and shows the *replaced* flag of a webhook as `true`.
- A loss outside a fleet without a category says *Choose a category for this loss.* on the form, as the server check does.
- The rank text on **My Statistics** takes its full stop from the translation (a Chinese page no longer shows an English full stop).
- Saving Doctrine Settings without a change shows *Nothing was changed.* as an information message like the other pages.
- The message about an unknown PAP multiplier lists the multipliers with the separator of the language ("1 倍、2 倍、3 倍").
- Pending manual PAP rows say "Pending approval" (待审批) like pending fleets, in English and Chinese.
- The MOTD length counts characters such as `&gt;` as the one character they stand for; the text that is sent is unchanged.

### Added

- The **Audit** page has a **Changes** column that shows what an entry changed (`old → new`, folded out when there is a lot). Webhook addresses, tokens and similar secrets are never shown.
- **Members may resubmit rejected claims** (SRP settings, **Members and review**; off by default). Until now only a withdrawn claim could be submitted again by its member; a rejected one needed a reviewer to reopen it. With the setting on, the member sees **Submit again** on their own rejected claim. It works like submitting a withdrawn claim again: the claim is calculated again with the current rules and limits and goes back to the review queue as pending. The earlier rejection stays in the claim history and in the audit log. A banned member, SRP switched off, a loss past the claim deadline and the other rules of a withdrawn resubmission apply the same way; claims imported from aa-srp can never be resubmitted. The change of the setting is audited, and saving the page without a change still writes nothing. A claim a reviewer rejected carries the warning "Submitted again after a rejection" afterwards, so **Approve clean claims** skips it and the queue shows it; a claim rejected by an SRP ban (**Reject pending claims**) cannot be submitted again by the member at all, only reopened by a reviewer; and the member's rejection count on the claim page counts a resubmitted claim through its rejection events, so submitting again does not reset it. Withdrawn claims work as before.
- The **SRP link in the default ping.** The built-in Default Ping has an `**SRP:** [Request SRP](address)` line, on its own line that is there only while the fleet has an SRP link (no empty "SRP:" label otherwise). The address is written so that no character of it can end the Discord link, and the length limit of 2000 characters counts it. **Only the built-in text changed**: a Default Ping that is stored on your site, a new installation's stored Default Ping and every template you edited stay as they are, and no migration rewrites them. The new text is used by `fleetops_seed` for a ping template that does not exist yet and when a stored ping cannot be rendered. To get the line into an existing ping template, add `**SRP:** [Request SRP]({{ srp_url }})` inside `{% if srp_url %}` … `{% endif %}` yourself (or a plain `{{ srp_url }}`). Retry SRP on an ended fleet stays as it is.

### Changed

- The **FC Statistics** page of the alliance shows the fleet type labels with the colour of the type (a theme colour or a custom colour with readable text), like the other statistics pages.

### Upgrade notes

- One new migration, `0045`: it adds the setting **Members may resubmit rejected claims** to the SRP settings. Nothing changes until an administrator turns it on. Run `python manage.py migrate`, then `python manage.py collectstatic --noinput`, and restart Alliance Auth and Celery.
- The default ping gets the SRP line only for new ping templates; stored templates are not changed (see above).

### Rollback notes

- Run `python manage.py fleetops_srp_rollback_check` first and roll back only when all checks are OK. Then `python manage.py migrate fleetops 0044` removes the setting (a refused rollback, for example while claims imported from aa-srp exist, leaves the column in place; the same guard as before runs first). Then install 0.3.0b4 again. Nothing else in the database changes.

## [0.3.0b4] - 2026-11-13

Fixes from the re-test of 0.3.0b3 on a test site.

### Fixed

- **Saving the SRP settings in the dead-end state.** A site that cannot take losses outside a fleet (a category is required, but there is no default category and none that members can choose) can still save its other SRP settings. The save now ends with the same warning as the settings page, so it is no longer visible only after opening the page again.
- **The ping cooldown message** never contradicts itself: the time since the last ping is rounded down (1 to 59 seconds) and the wait up, so the wait is never too short (no more "60 seconds ago, wait 1 second").
- **The fc_link and formup_link hint** on the message template page is shown only for MOTD templates, and follows the type when you change it in the form.
- **Restricted doctrines and the custom doctrine name.** A custom name that is the name of a restricted doctrine the person may not ping is refused with *You may not ping this doctrine, ask an administrator.* in every place that takes a typed name: Start Fleet, group fleets, Send Ping, scheduled fleets (saving and starting), presets and the doctrine name of a running fleet. Case, spaces, rich text tags, zero-width characters and full-width letters are ignored, doctrines that no longer exist do not count, and administrators are not limited. **Doctrine Settings** also shows a warning while a doctrine is restricted and the custom name is open to everybody.
- **A refused start of a scheduled fleet** writes one audit entry and one notification, not one per click. Pressing **Start** again for the same reason shows the message on the page only, until the fleet is edited (its doctrine or FC too) or started.
- **Saving a configuration page without changes** (the general settings, the SRP settings and every list page edit form) writes no audit entry and shows the message *Nothing was changed.*, the same as Doctrine Settings.
- The statistics of all corporations no longer put an unused class on the labels of fleet types with a custom colour.
- Documentation: the Chinese pages say **在总览显示** (Dashboard is 总览 in the interface), the colour contrast warning is described as it works (live preview on the edit page, message on the list page after saving), and the menu names in the manual test list match the interface (后台设置, 建制设置).

### Upgrade notes

- No migrations. Install the new version, run `python manage.py collectstatic --noinput` and restart Alliance Auth and Celery.

### Rollback notes

- Install 0.3.0b3 again. Nothing in the database changes.

## [0.3.0b3] - 2026-11-12

Fixes from the second run of 0.3.0b2 on a test site, a new setting (who may ping which doctrine), and colours and dashboard cards for fleet types. There are two new migrations (`0043`, two small tables; `0044`, two columns of the fleet types); install them the way you installed 0.3.0b2 and read the upgrade and rollback notes below.

### Added

- **Who may ping which doctrine.** Under **Administration, Doctrine Settings** (the page of the doctrine ship order, now
  with more on it) every doctrine has a choice of Alliance Auth groups. A doctrine without a group stays open to everybody
  who may start fleets, as before; with groups, only members of one of them can ping it, and the doctrine shows the pill
  **Restricted**. Superusers and everybody who can use the FleetOps administration (`fleetops.manage_configuration`) are
  never restricted. The rule covers **Start Fleet** (also for group fleets: a group leader is judged by the Alliance
  Auth groups they are in), **Send Ping**, **scheduled fleets** and **start presets**. The lists show only the doctrines
  the person may ping, and the server checks the choice again: a doctrine put into the request by hand is refused with
  *You may not ping this doctrine, ask an administrator.* (also in Chinese). SRP links and the PAP-link fleets are not
  restricted, nor is the retry of the MOTD or the ping of a fleet that is already running. Fittings still decides who
  sees a doctrine; this is an extra filter on top.
- **A scheduled fleet is checked again when it starts.** If the FC who scheduled it may not ping its doctrine any more,
  the fleet is not started (it stays scheduled and nothing is asked from ESI), the FC gets an Alliance Auth notification
  in their language and the audit log gets `fleet.schedule_start_refused`. Editing a scheduled fleet keeps the doctrine it
  already has. A start preset whose doctrine is no longer allowed says so when it is loaded and is never switched to
  another doctrine.
- **Who may use a custom doctrine name.** The same page has a second setting for the option **None / Custom**: everybody
  who may start fleets (the default, as before) or only chosen groups. Without the right the option and the text box
  are not shown and a typed name is refused on the server; scheduled fleets and presets follow the same rule. The
  dropdown then starts on a "Choose a doctrine" entry, so no doctrine is picked unasked. Changing the doctrine name of a
  running fleet is checked the same way, so Retry ping cannot send a name the user may not use. Administrators are not
  limited.
- Every change of these settings is written to the audit log as `configuration.create` or `configuration.update`
  (object types `DoctrineAccess` and `DoctrineSettings`) with the groups before and after. A record of a doctrine that was
  deleted in the doctrine app is kept and listed as such; it restricts nothing.

- **Fleet type colours.** In **Administration, Fleet Types** every type can have a colour: **Automatic** (the default; the
  colour it has now, so nothing changes after the upgrade), one of the six theme colours, or a **custom colour** (`#e67e22`;
  `#` and 3 or 6 hexadecimal digits, checked on the server). A preview on the edit page shows the dot and a sample with black
  or white text by the brightness of the colour, and warns when the colour is hard to see on the light or the dark theme.
  The colour is used everywhere the type is shown with a dot: dashboard cards, fleet lists, history, statistics and the
  running fleets.
- **Show on dashboard.** Every fleet type has a switch that shows its PAP card on the dashboard. The cards follow the sort
  order. More than four cards wrap onto further rows and work at phone width. The upgrade turns the switch on for the first
  four active types (by sort order and name) and off for the rest, so the dashboard looks the same after the upgrade; a
  type created later starts with the switch on.
- Changes of the colour and the switch are written to the audit log (`configuration.update`, old and new values).

### Changed

- The administration page **Doctrine Ship Order** is now **Doctrine Settings**; the ship order is edited with the button
  **Edit order** of each doctrine.

### Fixed

- The fleet MOTD no longer loses the `>` and `>>>` marks of the doctrine ship order in the game. The game reads the MOTD
  as HTML-like text and dropped every `>`; now every value of a person or of FleetOps (ship list, ship and doctrine names,
  comms, form-up place, FC, notes) is escaped (`&lt;`, `&gt;`, `&amp;`) when the MOTD is rendered. The FC, form-up,
  logi and boost links keep their markup, and your own MOTD templates are covered as well, not only the default one.
  The length warning counts the escaped text. Discord pings are not changed.
- The SRP settings page no longer refuses every save while the site is in the state in which nobody can submit a loss
  outside a fleet (a category is required, but there is no default category and none that members can choose). Only the
  save that turns a working configuration into that state is refused; a site that is in it already can save its other
  settings, such as the claim deadline, and the warning stays until the state is resolved. Editing categories follows
  the same rule.
- The refusal of a ping to a channel that just received one never says "0 seconds ago" any more (the time is rounded
  up) and says "1 second" instead of "1 seconds".
- On the payments page, a payer whose chosen in-game character has an expired or revoked token, or lacks the scope, now
  sees **Add the opening character again** on the rows instead of the general setup link. Payers who never chose a
  character still see **Set up opening characters in game**.
- The message template edit page tells that the FC and the form-up system are clickable in the game only with
  `{{ fc_link }}` and `{{ formup_link }}`, and that `{{ fc }}` and `{{ formup }}` are plain text. The message template
  document says so too.
- Chinese interface: doctrine is 建制 everywhere (it was 教义 in 39 texts), the paid state and button are 已付款, and
  opening a character is 在游戏里打开, one word each.

### Upgrade notes

- One migration, `0044`, adds two columns to the fleet types: the colour (empty, so every type keeps its colour) and
  **Show on dashboard** (on for the first four active types by sort order and name, off for the others, as the dashboard
  showed them). No other data is read or changed.
- One migration, `0043`, creates two empty tables (the groups per doctrine and the custom doctrine name setting). It changes
  and reads no data: after the upgrade no doctrine is restricted and the custom name is open to everybody who may start
  fleets, so every page behaves as in 0.3.0b2 until an administrator sets something. Upgrade the package, run `python manage.py migrate`, `python manage.py collectstatic --noinput` (the pages are templates; the command keeps the static files in step) and restart the web process.
- The restriction is read from Alliance Auth groups at the time of the request. Deleting a group takes it out of every
  list, so a doctrine whose last group is deleted is open to everybody again; check **Doctrine Settings** after deleting a group.
- A custom MOTD template that you changed to `{{ doctrine_ships|force_escape }}` (or `|escape`) to work around the lost
  `>` marks can keep that or go back to `{{ doctrine_ships }}`: a value that the template escapes itself is not escaped
  twice (checked by a test), so both show the marks once.
- A custom MOTD template that builds markup out of values with `|safe` keeps working; a value that you put into the
  template without a filter is now escaped, so a `<` or `&` of a name shows as typed.

### Rollback notes

- `python manage.py migrate fleetops 0043` drops the two new columns of the fleet types and runs the rollback guard first (a
  rollback below `0035` is still refused before anything is dropped). Only the chosen colours and switches are lost: every
  type has its automatic colour again and the dashboard shows the first four active types, as in 0.3.0b2. Without the
  migration step 0.3.0b2 ignores the columns.
- `python manage.py migrate fleetops 0042` drops the two new tables and runs the rollback guard first (a rollback below
  `0035` is still refused before anything is dropped). Only the doctrine restrictions and the custom name setting are
  lost: every doctrine is open to everybody who may start fleets again, and the Alliance Auth groups are not touched.
  Then install 0.3.0b2 again. Without the migration step 0.3.0b2 simply ignores the tables.
- The MOTD of fleets that are already open keeps the text it was rendered with;
  **Regenerate & Push MOTD** renders it again with the installed version.

## [0.3.0b2] - 2026-11-10

Fixes from the run of 0.3.0b1 on a test site, one new SRP setting, the order of the ships of a doctrine and a payment page that is easier for the people who pay. Install it the way you installed 0.3.0b1; read the
upgrade notes and the rollback notes below first.

### Added

- **SRP payments, by claim.** The payments page has a switch **By player** / **By claim**. **By player** is the page as
  it was. **By claim** lists one row per claim that waits for payment: when it was claimed, the player, the character
  that lost the ship, the claim number, the ship, the value of the loss, the category or link, the amount to pay and the
  buttons **Copy**, **Open in game** and **Paid**. **Paid** pays that one claim (a payment batch of one claim) with the
  same rules as every other payment: not your own claim, not a claim you approved while the four-eyes rule is on, and
  the same payment note, audit entry and member notice. The filters, the exports, the totals and **Mark selected paid**
  work in both views. **By claim** is one flat list, the oldest claim first. The switch buttons save the payer's choice with a POST; a `?view=` link only shows that view once and writes nothing. Every payer's last choice is remembered; anybody who never chose sees **By player**, as before.
- **Claim numbers for the transfer reason.** The claim number (`SRP-123`) is the code to type in the reason of the
  ISK transfer in game. **By claim** has a copy button next to every number and every player card of **By player** has
  **Copy claim numbers**, which copies the numbers of the claims of that card that you can pay, separated by spaces
  (`SRP-123 SRP-124`). EVE's limit for the length of the reason is not documented anywhere FleetOps could check, so
  nothing is cut or counted: with very many claims the game may cut the text, and paying claim by claim is the safe
  way.
- **Open in game** (optional). Each payer can choose one of their own characters as their **in-game character** in the
  new box **Opening characters in game** of the payments page. The buttons **Open in game** then open the character that
  lost the ship (**By claim**) or the main character of the player (**By player**) in the EVE client of that character.
  Adding the character asks for one new scope, `esi-ui.open_window.v1`, only at that moment and only from the payer who
  adds it; payers who do not use the button are never asked and nothing else changes. Without the scope on the EVE
  application of your site the authorization fails with a clear message and everything else works. The button works
  only for the claims of the payments page (waiting for payment, or paid in the last 30 days) and never takes a
  character number from the request. If nothing opens in the game, open any character window once and try again. A
  click is not a data change: it writes no audit entry.

- **Doctrine Ship Order** (Administration). For a doctrine of a doctrine app such as Fittings an administrator can store
  the order in which its ships are listed in pings and MOTDs, with a mark between every two ships: `>` (higher
  priority), `=` (same priority) or `>>>` (much higher priority), for example `Minokawa = Lif > Raven >>> Basilisk`.
  The ships move with up and down buttons (they work without JavaScript and on a phone), a preview line shows the
  result and **Back to the automatic order** deletes the stored order. It needs the permission that already
  allows changing the FleetOps configuration; every change is audited. The order is applied whenever a ping or MOTD is
  made, so open fleets show it at their next MOTD or ping. Ships added to the doctrine later are listed at the end
  with `>`, and ships that left the doctrine are left out.
- **Notify members when a payment is undone** (SRP settings, **Members and review**; off by default). When it is on and
  **Notify members** is on as well, undoing a payment sends the member an Alliance Auth notification with the reason
  that was given, in the member's language. With **Notify members** off nothing is sent, whatever this setting says.
  Changing it is audited like every SRP setting.

### Changed

- Each ship appears once in the ship list of a doctrine (`doctrine_ships`) in pings, MOTDs and on the page of a
  scheduled fleet: a doctrine with two Moa fits now reads `Moa > Osprey`, not `Moa > Moa > Osprey`. The list of fit
  names (`doctrine_fits`) still names every fit.
- **Retry Ping** renders the ping again from the current data of the fleet before it posts it, so a fleet started
  by an older version no longer posts its stored text (with the game's markup) a second time.
- One cooldown per ping channel: a ping for a fleet and a ping without a fleet both hold the channel for 60 seconds, in
  both directions. If the start ping of a fleet meets a cooling channel, the step shows the wait and **Retry Ping** posts
  it later; the fleet still starts. This includes the pre ping of the same fleet: a scheduled fleet that is started
  less than a minute after its pre ping (or its edit with **Send update**) shows the wait on the start ping step.
- Only FC leads, and the FC of a fleet through the permissions they already have, can give special roles that carry FC
  credit. Group leaders who started a group fleet no longer get those roles offered, and the server refuses them.
  Roles given before stay as they are.
- A group that FleetOps has settings for can be deleted in the Django admin by someone who may delete groups. Its
  FleetOps settings are handled as for any other deletion of a group, and the audit log gets one **Group deleted**
  entry, not two.
- **Recalculate** on the review page says how many pending claims were recalculated and how many of your own claims were
  skipped because reviewers may not decide their own claims. If only your own claims were waiting, the message is a
  warning, not a green "0". The same goes for a long list that is recalculated in the background: the message says how
  many of your own claims are skipped, and nothing is queued when all waiting claims are your own.
- The word "Total" of the total rows of exports and reports has its own translation context, so another app that translates
  "Total" differently can no longer change the total row in Chinese.

### Fixed

- A doctrine name with square brackets, such as `[ACTIVE] Example Doctrine`, no longer breaks the doctrine link in a
  Discord ping: the link shows the whole name. The same is done for the name of the comms preset in its link, and a
  name can no longer end the link text early or put a second address into the link. The stored message templates do not
  change; the MOTD shows the names as typed.
- The SRP settings page refuses a configuration in which nobody can submit a loss outside a fleet (a category is
  required, but there is no default category and no category members can choose). The same goes for deactivating,
  hiding or deleting the last category members can choose. Sites that are in that state already show a warning on the
  SRP settings page and on **My SRP**, and members see one clear sentence instead of "Choose a category". Only
  a loss that ends up outside a fleet is refused: a loss inside a tracked fleet still goes to the fleet's link and needs
  no category. The checks apply only while losses outside a fleet are switched on, they refuse only what makes things
  worse (a site that is in that state already can still rename or edit its categories), and the Django admin follows
  them when it deletes categories.
- Editing a fleet without changing its times keeps the start and end exactly as they were (they lost their seconds before)
  and writes no audit entry when nothing changed.
- The SRP link that is made when a fleet starts is audited with the person who started the fleet as the actor, and the
  link made by **Retry SRP** with the person who pressed it; only an automatic start without a person names the FC.
- The ship image on the SRP check page, the claim page and the loss card stays 64 by 64 pixels.
- When the claim deadline is made longer, the losses that were marked too old are looked at again at once and show up
  again with the next scan; they no longer wait for the daily cleanup.
- An ended fleet no longer shows **Retry Ping** and **Retry MOTD**, and those actions are refused for it.

### Upgrade notes

1. Back up the database and upgrade the package as for any release.
2. Run `python manage.py migrate fleetops`. It applies `0040`, which adds the new SRP setting (off), `0041`, which creates the table for the ship orders (empty, so every doctrine keeps its automatic order, now with each ship once), and `0042`, which creates the table of the payers' choices (the view of the payments page and the in-game character; empty, so every payer starts with **By player**). It changes no data.
3. Collect the static files (`python manage.py collectstatic --noinput`): the style sheet changed.
4. Optional: to use **Open in game**, add the scope `esi-ui.open_window.v1` to the EVE application of your site (the one
   that belongs to `ESI_SSO_CLIENT_ID`) before a payer adds an in-game character. Without it only that authorization
   fails; nothing else needs the scope.
5. Nothing else needs to be done. If the SRP settings page now shows the warning about a missing category, turn off
   **Members must choose a category for losses outside a fleet**, choose a default category, or make a category
   choosable by members.

### Rollback notes

- `python manage.py migrate fleetops 0039` undoes `0042` (drops the table of the payers' choices, so every payer
  starts with **By player** again and has to choose the in-game character again), `0041` (drops the table of the stored
  ship orders; the doctrines are listed in their own order again) and `0040` (drops the new setting); the guard of `0042`
  runs first and refuses a way back below `0035` as before (see the rollback notes of 0.3.0b1, which apply unchanged).
  Going back to a 0.3.0b1 install after that needs no further step. The ESI tokens that were added for **Open in
  game** stay in django-esi; remove them in Alliance Auth if you want them gone.

## [0.3.0b1] - 2026-11-08

The SRP rework: categories, separate review and payment permissions, standing links, automatic loss discovery, rules
for what is not covered, bans, changes by the claimant, payment batches, reports for any period and the import of the
aa-srp history. This release also contains everything of the 0.2.0b2 and 0.2.0b3 sections of the
[CHANGELOG](https://github.com/yilifaer/aa-Fleet-ops/blob/v0.3.0b1/CHANGELOG.md), which were never released
separately. Install it from the wheel of this GitHub Release
(`pip install --no-deps https://github.com/yilifaer/aa-Fleet-ops/releases/download/v0.3.0b1/aa_fleetops-0.3.0b1-py3-none-any.whl`);
`pip install --no-deps aa-fleetops==0.3.0b1` works only once the version is published on PyPI.

**Before you upgrade:** add the scope `esi-killmails.read_killmails.v1` to the EVE application of your site (the one
that belongs to `ESI_SSO_CLIENT_ID` on the EVE developer website), then upgrade the package. Without the scope the SSO
login of the loss reading fails with "invalid scope". Read the upgrade notes and the rollback notes below first.

### Added

- **SRP categories** (**Administration** > **FleetOps SRP** > **SRP Categories**): kinds of claims such as fleet losses,
  solo PVP or a standing doctrine, each with its own payout percentage, per-loss cap, payout table entries and monthly
  personal and corporation caps (counted for that category and checked together with the general limits). A claim has at
  most one category. FleetOps creates only the built-in **Fleet** category; the documents show an example set. A payout
  rule can belong to a category and then beats a general rule of the same specificity. Reviewers can move a pending claim
  to another category (reason required, audited as `srp.claim_recategorize`). Settings: **Default category** and
  **Members must choose a category for losses outside a fleet**; members get a **Category** list on the form for a loss
  outside a fleet.
- **Separate permissions for reviewing and paying.** `fleetops.review_srp` and `fleetops.pay_srp` are new;
  `fleetops.manage_srp` stays and means both. The menu shows **Review** and **Payments** to the matching role. Nothing is
  granted to any group or user by the upgrade. New settings **SRP payers may mark their own claims paid** and **The
  approver of a claim may not pay it** (a four-eyes rule that also covers anyone who changed the approved amount), both
  off by default.
- **Standing SRP links**: links without a fleet, with a name, a category, an EVE date, a doctrine text, a report link and
  an optional closing time. A claim through one is a claim outside a fleet in the link's category and counts in no FC
  statistics. Categories can make a standing link every day and close their links after a number of hours (both off by
  default; the upkeep runs inside the existing task `schedule_active_fleet_tracking`, no beat entry is added). **My SRP**
  lists the open standing links; the setting **A category claim needs no standing link** (on) decides whether a claim
  needs one.
- **SRP Links page** (**SRP** > **SRP Links**, for reviewers and payers): fleet links and standing links with state,
  waiting claims and totals, filters, and closing, completing and opening of several links at once. A closed link whose
  claims are all settled can be completed (also automatically with **Complete SRP links automatically**, off by default).
  Audited as `srp.link_create`, `srp.link_update`, `srp.link_open`, `srp.link_close`, `srp.link_complete` and
  `srp.link_uncomplete`.
- **Not covered**: ship groups, ships and kinds of space (high, low and null security, wormhole, abyssal, Pochven) whose
  losses are refused when a claim is checked, with a **Fill in the recommended list** button. **Nothing is blocked after
  an install or an upgrade until an administrator saves a list.** A killmail now stores its ship group, security status,
  constellation and region, looked up from django-eveonline-sde when it is installed, otherwise from public ESI reads
  (no token, cached as ESI's `Expires` says).
- **SRP bans** (**SRP** > **SRP Bans**, for reviewers): a member can be banned from checking and submitting claims for a
  number of days or permanently; the reason is visible to the member, the internal note to staff only. Optional automatic
  ban for members who keep submitting over their monthly cap (off by default). Audited as `srp.ban_create` and
  `srp.ban_lift`.
- **Automatic loss discovery** (off by default). With **Find losses automatically** on, FleetOps reads the recent
  killmails of each member's authorized characters from ESI (`esi-killmails.read_killmails.v1`, the member's own token)
  and lists the losses on **My SRP** under **My recent losses** with the matched fleet and the reason a loss is not
  covered. The scan runs inside the existing beat task within a time and request budget and respects ESI's limits. A
  **Loss reading** card (and the aa-charlink import **FleetOps SRP loss reading**) lets members authorize characters.
  New settings group **Loss discovery**. Audited as `srp.loss_ignore` and `srp.loss_unignore`.
- **Members change a pending claim**: comment, category or link and the loss, previewed and calculated like a new claim
  (settings **Members can change their pending claims** and **Members can replace the loss of a pending claim**, both
  on; audited as `srp.claim_member_edit`). Decisions of reviewers carry the revision the page showed and are refused when
  the claimant changed the claim since.
- **Review and payment pages**: more filters (category, link, dates, member, corporation), a **Banned** badge, and for
  SRP staff the attackers of a killmail, the monthly limits as they are now and the claimant's recent rejections
  (**Days of rejection history shown to reviewers**, default 90). The command `fleetops_srp_backfill_killmails` fills in
  attackers and locations of killmails stored earlier.
- **Payment batches**: every payment makes a batch (`PAY-<n>`) with a page, a list of recent batches and the audit entry
  `srp.payment_batch`. New export `srp-payable` (claims to pay) and a **Payment batch** column in `srp-claims`.
- **Insurance estimate** on claims and on the check page, from the public ESI insurance prices (read once a day); a hint
  only. Settings **Show the insurance estimate** (on) and **Insurance level shown**.
- **SRP reports and periods**: **SRP** > **Reports** shows any period (presets or two EVE dates, at most 1100 days) with
  summary cards, searchable tables and Excel and CSV downloads: trend, categories, ships, members, corporations, regions,
  FCs, reviewers, payers, rejections, payments and personal limits. All SRP exports take a period and are named after
  its first and last day; new files `srp-trend`, `srp-categories`, `srp-ships`, `srp-regions`, `srp-reviewers`,
  `srp-payers`, `srp-rejections`, `srp-payments` and `srp-limits`. Existing columns did not move; some files gained
  columns at the end.
- **Import of the aa-srp history**: `python manage.py fleetops_import_aasrp` copies the links and requests of aa-srp
  (read only) into FleetOps: approved requests become paid claims, rejected ones rejected claims, pending ones are not
  imported. It can run again, has `--dry-run`, `--since`, `--until`, `--category-map`, `--enrich` and `--undo`, and holds
  the SRP lock batch by batch, so run it in a maintenance window. Imported claims are records and cannot be decided,
  paid or changed. See [Importing the history of aa-srp](docs/configuration.md#importing-the-history-of-aa-srp).
- **Payout table import**: `python manage.py fleetops_srp_import_payouts PATH --category KEY --column NAME` (dry run
  unless `--apply`).
- **Rollback check**: `python manage.py fleetops_srp_rollback_check` shows whether `migrate fleetops 0023` would be
  stopped (see the rollback notes); it changes nothing.
- **Checks and documents**: `python manage.py check` warns (never stops the site) about a missing beat entry, an empty
  `SITE_URL`, a missing `"fleetops"` in `APPS_WITH_PUBLIC_VIEWS`, `ATOMIC_REQUESTS` on, and aa-charlink without the
  literal `"fleetops"` entry (`fleetops.W001` to `fleetops.W006`; silence with `SILENCED_SYSTEM_CHECKS`). The README and
  the configuration reference have **Uninstalling** and **Data and privacy** sections.

### Changed

- **A loss inside a tracked fleet is no longer paid as a loss outside a fleet by accident.** When a member checks a loss
  as a loss outside a fleet but fleet tracking shows the character was in a fleet whose SRP link is open, the check now
  uses that fleet's link. The member can still **Submit as a loss outside a fleet anyway** with a reason the reviewers
  see. The new setting **Losses inside a tracked fleet** has three choices: switch automatically (the default), block
  the submit, or only suggest the fleet link (the behaviour of earlier builds).
- **"SRP managers may decide on their own claims" is now "SRP reviewers may decide on their own claims"** and covers
  decisions only; paying follows **SRP payers may mark their own claims paid**. The upgrade copies the old value into the
  new setting, so a site that allowed both keeps doing so.
- Opening an SRP link again, editing links (doctrine text, report, category, date) and making standing links are for
  reviewers; a payer can still close and complete a link. Pages say reviewer or payer where they said SRP manager.
- The security status of a system is shown to SRP staff only.
- **The default value source is now the zKillboard total value** (it includes cargo; aa-srp pays the total value). Only a
  site that never saved its SRP settings and has no claim is switched, by the upgrade.
- The claim deadline no longer makes pending claims "calculated with earlier settings" when it changes; an upgrade keeps
  the value a site had.
- Custom doctrine names (fleets, pings, PAP links, payout rules) keep only the plain name, accept a pasted fitting link
  of up to 4000 characters and report an error instead of cutting a name longer than 255 characters. The MOTD and the
  ping show doctrine, form-up place, group and additional message as plain text, so a pasted fitting link can no longer
  break the Comms, Logi, Boost and SRP lines.
- The pruning of departed members refuses to run when no Member alliance ID matches any user, or when it would remove
  more than **Largest share of departed members to prune** (default 50 percent) of the users with PAP history;
  `fleetops_prune_history --force` overrides it, and the command exits with an error when it refused.
- Log lines of the standalone pings and Send Ping go to the extension log of Alliance Auth like the rest of FleetOps.
- The Django admin shows the audit log, PAP rows, fleet member records, role assignments, incentive periods and monthly
  FC statistics read-only.
- The models of FleetOps 0.2.x keep Django's default permissions (add, change, delete, view) in this release; taking them
  away is planned for a later release. Do not give them to groups.

### Fixed

- Deleting rows of other apps' models is fast again: the receiver for deleted groups is connected to `auth.Group` only.
- PAP: rows of a pilot whose account was deleted are kept on recalculation; a registration made while the fleet's end is
  settled is recalculated; a manual replacement no longer counts next to a merge for the same character; registering
  through a PAP link writes the registration first and reads location and ship in the background.
- A closed fleet cannot be saved without an End Time; a fleet that ended because it could no longer be read ends at the
  time it was last read; a fleet read that is due while another one runs is tried again a few seconds later. The
  incentive page no longer lists rejected and excluded fleets.
- SRP: a manual ban and the automatic ban cannot both be created at once; the value sweep no longer overwrites a
  killmail a preview has put back into the zKillboard queue; **Recalculate** handles up to 50 claims while you wait and
  more in the background; a daily link is no longer made and closed again in the same minute; the SRP member report no
  longer shows claims without a player in one "Unknown" row; a payout rule must name a ship; claims imported from aa-srp
  no longer offer **Check fleet presence again**; importing again updates an imported request when aa-srp has a newer one.
- Loss discovery: names and places of new losses are read in one counted request, no lookups are made after ESI asked for
  a pause, and losses without names are read again in the next run; **Read my characters now** is no longer dropped while
  the general scan runs.
- `fleetops_import_aasrp --enrich` keeps the zKillboard pace, waits when the slot is taken and stops with a message while
  zKillboard is paused. `fleetops_srp_import_payouts` without `--apply` also checks the ship names.
- Faster pages: SRP member report, My SRP, review and payment filters, the fleet list, the fleet page, the ship function
  rules and name lookups of pasted rosters; downloads in Excel format are streamed like CSV; one new index for found
  losses.

### Upgrade notes

1. Add the scope `esi-killmails.read_killmails.v1` to the EVE application of the site (see above).
2. Back up the database and install the release with `--no-deps`.
3. Run `python manage.py migrate fleetops`. It applies the migrations `0022` to `0039` to a site on 0.2.0b2 (a site
   on an older build reads the 0.2.0b2 notes in the
   [CHANGELOG](https://github.com/yilifaer/aa-Fleet-ops/blob/v0.3.0b1/CHANGELOG.md) too).
4. Run `python manage.py collectstatic --noinput`, then restart the web process, the Celery worker and Celery beat (the
   worker has to know the new tasks). No beat entry is added; keep `fleetops_track_active_fleets` and the daily entry.
5. Run `python manage.py check` once: the warnings name the lines of `local.py` that are missing.

What the migrations do to existing data:

- Nothing is granted to any group, state or user. Add `fleetops.review_srp` and `fleetops.pay_srp` to the groups of
  people who should only review or only pay; `manage_srp` keeps meaning both.
- Existing claims keep their amounts, statuses and limits. Claims made through a fleet link get the **Fleet** category.
  Every existing link stays a fleet link. Old payments have no batch; old killmails have no attackers or location until
  `fleetops_srp_backfill_killmails` has read them (the region report shows "Unknown" until then).
- The block lists of **Not covered** are empty, nobody is banned and the loss discovery is off. To use them, open **SRP
  Settings**, press **Fill in the recommended list** and save, and switch **Find losses automatically** on.
- **Members can change their pending claims** and **Show the insurance estimate** are on after the upgrade; untick them
  to keep the old behaviour.
- **Changed default:** the setting **Losses inside a tracked fleet** is "Switch to the fleet link automatically" for
  every site. Set it to "Only suggest the fleet link" if members should keep entering such losses outside a fleet.
- **Daily pruning:** the daily task `fleetops_prune_history` deletes old rows according to the retention settings, and
  now refuses to prune departed members when it would remove more than the share set in the settings. If the task logs
  "Departed members were not pruned", check **Member alliance IDs** before using `--force`.
- Nothing is imported from aa-srp by the upgrade. Run `fleetops_import_aasrp --dry-run` yourself and decide on every
  pending request in aa-srp first.
- A site that ran a development build of this release may already have the column added by migration `0036`; then
  `migrate` stops with "already exists" (PostgreSQL) or error 1060 "Duplicate column name" (MySQL/MariaDB). Run
  `python manage.py migrate fleetops 0036 --fake` once, then `python manage.py migrate`.

### Rollback notes

**The order that works.** (1) To stay on this version a little longer, switch the new functions off (below). (2) To go
back to 0.2.x: export what you need (categories, standing links, bans, batches, reports), run
`python manage.py fleetops_import_aasrp --undo` if the aa-srp history was imported, delete or generalise the payout rules
of a category, and deal with the standing links that have claims (delete the link: its claims stay, without a link, as
claims outside a fleet; or restore the backup taken before the upgrade). (3) Run `python manage.py fleetops_srp_rollback_check`
until every line passes. (4) Run `python manage.py migrate fleetops 0023` if you go back to the 0.2.0b3 build (commit `66a1eb8`), or
`python manage.py migrate fleetops 0021` if you go back to the 0.2.0b2 build (commit `01adbc7`; `0022` and `0023`
belong to 0.2.0b3, and leaving them in place keeps the column `members_are_leaders`, which the 0.2.0b2 code does not
know). (5) Only then put the old code back. 0.2.0b2 and 0.2.0b3 were never released, so there is no package for them:
reinstall the wheel or the commit that the site used before, or install from a Git tag that you set on that commit.
**Migrations first, package second:** the older package does not know the newer migrations and cannot undo them.

The rollback is refused, and the database is left as it is, while:

- a payout rule belongs to a category (the rule IDs are listed; delete them or make them general first),
- a link without a fleet has claims, or a link came from aa-srp (the codes are listed). Delete those links in the Django
  shell (their claims stay, without a link, as claims outside a fleet; imported links lose their import data):
  `python manage.py shell -c "from django.apps import apps; apps.get_model('fleetops', 'SrpFleetLink').objects.filter(code__in=['code-1', 'code-2']).delete()"`
  with the codes from the message, or restore the backup,
- claims were imported from aa-srp (run `fleetops_import_aasrp --undo` first; the imported history is lost by the undo).

Migration `0035` is the guard for the whole way back (`0036` runs the same checks first); the check command makes the
same checks and only reads.

**What the rollback loses:** the categories and the category of every claim, standing links and the new fields of every
link (the doctrine text, the report link, the completion, the category and the others, also on fleet links), payment
batches, bans with their reasons and notes, insurance prices, attackers and location fields of killmails, the found
losses and their scan state, the source and revision number of claims, the external IDs of claims, and the new settings.
Claims, amounts, statuses and payments stay. The value source is not changed back. The permission rows `review_srp` and
`pay_srp` and the tokens members authorized stay in the database, harmless; remove the two permissions from your groups
before rolling back, because the older version does not know them and its pages need `manage_srp`. Self-payment changes too: after the rollback `manage_srp` holders may pay their own
claims whenever **SRP managers may decide on their own claims** (the old name of **SRP reviewers may decide on their
own claims**) is on, whatever **SRP payers may mark their own claims paid** said; switch the first one off before rolling
back if they must not. The reports and the
new files store nothing: the previous package removes the report page and the new files, and the older files lose the
columns added at their end. Code-only changes (names in plain text, fixes) simply behave as the older code does.

**To behave like 0.2.x without rolling back:** untick **Find losses automatically**, **Make a standing link every day**,
**Members can change their pending claims** and **Show the insurance estimate**, clear the block lists of **Not covered**,
set **Losses inside a tracked fleet** to "Only suggest the fleet link", and set **Value source** back to *zKillboard
fitted value* if the upgrade switched it. Keep using `manage_srp`: it still means review and pay together.

## [0.2.0b3] - not released separately; included in 0.3.0b1

A small addition for alliances that keep two Alliance Auth groups per small group and record no group leaders.
Included in 0.3.0b1; install that release.

### Added

- **All members of this group count as its leaders.** Every row under **Administration** > **Groups** has a new option
  of this name, off by default. With it on, every active member of that Alliance Auth group who holds
  `fleetops.basic_access` is a leader of the group in FleetOps, with exactly the rights of a leader recorded in
  Alliance Auth: the channels assigned to the group, the group's **Leaders may** mode, group fleets, the read-only
  views of the group's fleets and, when the row has the SRP switch on, SRP for group fleets. For an alliance with
  "Example Squad members" and "Example Squad leaders" groups, add the row for the leaders group and assign the squad's
  channel to it; nothing has to be set in Alliance Auth. Leaders and leader groups recorded in Alliance Auth keep
  working. Removing someone from the group, an inactive account or a missing `basic_access` takes the rights away on
  the next request. The leaders card of the row lists the members read only, the list shows "All members", and the
  change is audited like every other change of the row. The documents explain the setup step by step
  ([configuration](docs/configuration.md#two-groups-per-small-group), [permissions](docs/permissions.md#group-leaders)).

### Upgrade notes

Back up the database first, install the release with `--no-deps`, then run `python manage.py migrate fleetops` and
restart the web process, the Celery worker and Celery beat. Migration `0022` adds one switch to the group rows and
leaves it off, so nothing changes until an administrator turns it on. If you come from 0.2.0b1 or earlier, read
the notes of 0.2.0b2 below as well.

## [0.2.0b2] - not released separately; included in 0.3.0b1

The second pre-release. The 0.2.0b1 build was a test build that was never published, so this section covers
everything since 0.1.0a5: pre pings and a new ping and MOTD, PAP rules that are applied when a fleet closes, a Fleet
tab to manage the fleet from the web page, monthly statistics with exports, FC points, FleetOps SRP, group pings
and group fleets, English and Simplified Chinese pages, a refined interface in every Alliance Auth theme, and new
documentation. Included in 0.3.0b1; install that release.

### Changes since the 0.2.0b1 test build

Everything the 0.2.0b1 test found, and the work that came with it. The upgrade notes below cover both ways of
arriving here.

**Groups**

- **Group pings and group fleets.** Squads and interest groups are Alliance Auth groups. Administrators open
  **Administration** > **Groups** and choose, for each group, whether its leaders may only send pings (the new
  **Send Ping** page, no fleet record, no PAP) or also start fleets with PAP of the fleet types they allow for the
  group. Leaders are the group leaders Alliance Auth already records (and the members of its leader groups). On
  **Ping Channels** each channel names the groups whose leaders may use it; a channel without a group stays usable by
  every FC, and **Alliance FCs can use this channel** decides whether FCs may also use a channel that has groups.
  Group access is set on these pages only, never in Django admin, and leaders can change no setting. Send Ping shows a
  preview that looks like Discord, asks before posting, allows one ping per channel every 60 seconds, keeps a list
  of recent pings with **Use again**, and is audited as `ping.send`. Group fleets show a group pill, count in the
  statistics like their type (a fleet type of weight 0 gives no FC points and does not count toward the incentive
  minimum), and can get an SRP link when the group has the SRP switch on. Deleting an Alliance Auth group is
  audited and returns channels that only it used to the alliance. The texts of pings sent without a fleet are
  deleted after **Data retention days**.
- **PAP characters per player for one fleet**. The setting stays the default, but the FC can
  choose the value of a single fleet: on the Start Fleet page (also in presets, on the PAP-link fleet page and when
  editing a scheduled fleet) and, while the fleet runs, in a control above the PAP preview on the fleet page. FCs and
  group leaders may only lower it; FC leads may choose any value, including no limit, and are the only ones who can
  change it after the fleet closed, with a written reason. The PAP of a closed fleet is calculated again then. Every
  change is audited as `pap.limit`.

**Pings, MOTD and messages**

- **Pings and MOTDs are always English**, also on a site whose `LANGUAGE_CODE` is Chinese. The
  0.2.0b1 test build wrote FleetOps's own words in them (the target words, `Now`, the cancellation notice, the field
  labels and the footer) in the site language; now they are English everywhere, and text that administrators write
  in a template is sent as written. The pages keep following each user's Alliance Auth language.
- **Clickable MOTD**. The built-in default MOTD makes the FC name open the character's
  information window and the form-up system open the system's, with the new variables `fc_link`, `formup_link`,
  `fc_character_id` and `formup_system_id`. Only a MOTD template that still equals the old default is replaced
  (reversible); edited and custom templates are not touched.
- The MOTD warning is a setting, **MOTD warning length** (default 2000, 0 = off), counted with the tags; the preview
  names both counts. The default pre ping shows the fleet time in each reader's local time with a
  Discord timestamp. The default ping has a group line for group fleets. Pings and MOTDs have a live preview that
  looks like Discord and like the game window, and a confirmation asks before a ping is posted.
- A start form no longer asks the SSO for the R/W markers: a broken SSO answer is a translated message, never an
  error page. Job summaries are translated, and step texts written by 0.1.0a5 and 0.1.0a6 are
  converted to current ones by the upgrade.

**Fleets, PAP and approvals**

- **A rejected fleet needs Resubmit for approval**. Edits alone keep it rejected; the
  button takes an optional note that the approvers see in the queue.
- **Special roles with FC credit are assigned after approval**; a credit role given earlier is
  flagged in the queue. The approval count in the menu shows fleets and PAP rows separately.
- **FC incentive periods recalculate themselves** when a fleet of the month ends, its multiplier, type,
  times or statistics mode change, or it is approved, rejected or resubmitted, or a credit role changes. Finalised
  periods never change; the page shows the time of the last calculation and says when the numbers are stale.
- A month **without a monthly target is not assessed**: no colours, nobody inactive, "No target for
  this month"; the exports say "No target". The earlier default member minimum of 1 is gone.
- Shortening a closed fleet lowers a multiplier the new duration no longer allows; the End Fleet dialog lists only
  what is not reached yet, and multipliers are written the Chinese way ("N times") in Chinese. Manual fleets and edited
  times more than five minutes in the future, in a year before the minimum or older than the PAP retention are
  refused, and a record older than a year asks for a confirmation. In a pasted roster, a character that
  already has PAP is offered only with Replace or Merge and only when the PAP is tracked.
- A recalculation of PAP updates only the rows whose credit changed (N6-2). Squad change events name the wing
  (`DPS / 111 -> FAX / 111`). Dragging a member shows a small label and the tree no longer jumps.
  The PAP-link card of fleets closed before the upgrade is translated.

**Statistics, SRP and administration**

- The statistics overview explains the alliance total and the corporation rows; "Not assessed: you
  registered after this month." replaces a dash; FC points show two decimals. The claims and
  members exports end with a total row, and empty cells are written as cells, so a row without a rank no
  longer shifts left in Excel, WPS and Numbers. The incentive page shows ISK with separators.
- **Other corporations' pages answer 404 with a reason**, the same page for a corporation that does not exist;
  every unknown or hidden FleetOps page answers a FleetOps 404 page, and a page you may not
  open shows a FleetOps page with the reason and the permission name.
- SRP: every FC and every leader of an SRP group gets **My fleets** and a summary of their own fleets; new claims use the short fleet type name; the payout rule has a **Label**; the reason
  messages say what is missing; an own duplicate claim is explained in Chinese; no
  "above policy" confirmation without an automatic amount; and a notice names the pending claims that were
  calculated with earlier SRP settings, with a **Recalculate** button for SRP managers.
- **Ship groups without bridge** is a searchable list with links to EVE Ref, a button for the default list and a
  field for IDs outside the list. The positions of ship functions and the objects of the audit log show names
  in your language. Relative times in Chinese have no space before the words for "later" and "ago".

**Interface**

- Every page was refined within its current layout: numbers and headings, spacing, surfaces,
  pills and buttons, image frames, and a check of every page in Flatly, Darkly, Materia, Bootstrap and Bootstrap
  Dark on a phone, a tablet and a desktop. The dashboard keeps all its blocks and gains a compact **Running fleets**
  strip while a fleet runs (one short row per fleet on a phone), and metric cards two per row on a phone.
- **Flatly secondary buttons and alerts look different.** Outline-secondary buttons, pills and notices now have
  the contrast the page checks require in every theme, so in Flatly they are darker and calmer than in 0.2.0b1. The
  Materia primary button is darker inside FleetOps. This is intended. For example, an outline-secondary button in Flatly
  was grey text (#95a5a6) on a light border and is now body-coloured text on a light border.
- Closed fleets and members open on the **PAP** tab; running fleets stay on the Fleet tab. Reject (fleet, PAP rows,
  SRP claim), Undo payment, Revoke approval, End Fleet without a dialog, regenerating the MOTD and revoking a PAP link
  ask first. Messages of Alliance Auth appear as notifications on FleetOps pages. Failure notices say what to do
  next, and an unreadable form error is shown in the page instead of an empty bar.
- Readable colours for metric cards, the month selector, sort links, `code` in notices, approval group rows,
  pagination text, composition function pills and position labels, and the fleet tree states.

### Upgrade notes

Back up the database first. Then install the release with `--no-deps` (this keeps pip from changing the versions
of Alliance Auth and django-esi), run the two commands below and restart the web process, the Celery worker and
Celery beat.

```bash
python manage.py migrate fleetops
python manage.py collectstatic --noinput
```

**What the commands do**

- `migrate` applies the migrations `0005` to `0021` (from the 0.2.0b1 test build only `0019` to `0021`). `0018` only
  changes the labels of fields and choices; it changes no table. `0019` adds the new fields, `0020` updates the
  default ping and the default MOTD where they are still untouched (reversibly) and converts old step texts, and
  `0021` adds group access. Send Ping uses a built-in text until a **Ping without fleet** template exists; `python manage.py fleetops_seed` creates it
  (it never overwrites a template you edited). The data steps are described under "Migrations" below.
- `collectstatic` is needed: the Fleet tab, the SRP pages and the translated JavaScript bring new files. Without
  them the Fleet tab shows its first state but does not refresh, and its buttons do nothing.
- No beat entry is added or changed; the two entries from the README stay as they are.
- No setting changes. Custom MOTD templates keep working: add `{{ fc_link }}` and `{{ formup_link }}` by hand if the
  FC and the form-up place should be clickable in game. Sites that want Chinese text in pings write it in their
  templates. Existing incentive periods that are not finalised show a note that their numbers are stale until the
  next fleet change or **Recalculate**.

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
[the permissions reference](https://github.com/yilifaer/aa-Fleet-ops/blob/dev/docs/permissions.md).

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
  (everyone with FleetOps access). No data is converted; a month without a target is not assessed (see above).
- `0016` and `0017` add FleetOps SRP. SRP stays off until you enable it under Administration > FleetOps SRP. No
  data of aa-srp or Alliance Auth SRP is imported.
- `0018` changes labels only.
- `0019` adds the fields of this release (the MOTD warning length, the time of the last incentive calculation and the
  note of a resubmitted fleet). `0020` replaces the **Default Ping** and **Default MOTD** only while they still equal
  the text of the version before, and rewrites step texts that 0.1.0a5 and 0.1.0a6 stored. `0021` adds group access, the groups of ping
  channels (existing channels stay open to every FC), the group of a fleet and the pings sent without a fleet.
- The data steps of `0005`, `0006`, `0007`, `0009`, `0012`, `0014` and `0017`, and the step-text rewrite of `0020`, are not undone when migrating back (the default ping
  and MOTD of `0020` are restored).

**Message templates.** The **Default Ping** and **Default MOTD** are replaced by the new built-in texts only if
they were never edited; edited and custom templates keep working. `{{ ping_target }}` now renders the mention
chosen on Start Fleet, `{{ fleet_time }}` is `YYYY-MM-DD HH:MM EVE` (or `Now`), and `{{ comms_link }}` is the
comms join page for voice client links (the raw link is `{{ comms_voice_url }}`). `{{ logi_channel }}` and
`{{ boost_channel }}` are plain names now; the upgrade switches every MOTD template to
`{{ logi_channel_link }}` and `{{ boost_channel_link }}`. Add `{{ srp_url }}` to a customised ping or MOTD to
put the SRP link in. The variables are listed in the message templates reference.

**Languages.** FleetOps pages follow each user's language in Alliance Auth: English and Simplified Chinese are
available, and other languages show English text. Discord pings and in-game MOTDs are always English, whatever
`LANGUAGE_CODE` says and whoever clicked: FleetOps's own words in them are never translated, and text that you write
in a template is sent as you wrote it. Nothing needs configuring.

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
