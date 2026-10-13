# Staging / Real-EVE Test Plan

Use a separate non-production Alliance Auth instance for real EVE SSO/ESI and Discord testing.

Do not share the production database, Redis, Discord webhook or EVE SSO application.

## Stage 1 — Authentication and character ownership

- authorize main character
- authorize alt character
- verify both appear only for the owning Auth user
- verify another user's character cannot be selected
- verify missing/expired ESI token error handling

## Stage 2 — Fleet detection

- selected character not in fleet
- selected character in the fleet commander position but not the Fleet Boss (rejected; the message names the boss)
- selected character is the Fleet Boss while sitting as squad member, wing commander or fleet commander (accepted)
- confirm detected Fleet ID and member count
- during a tracked fleet, hand the boss role to another pilot: the fleet page warns about the handover and the remaining checks; hand it back in time (fleet stays open) or let it end ("fleet boss handed over to <name>")

## Stage 3 — Start Fleet

Test the complete intended workflow:

1. select FC alt
2. detect fleet
3. select Fleet Type
4. select Doctrine / Custom
5. manually enter Form Up
6. select Comms / Logi / Boost
7. select Ping channel and Ping target
8. verify Ping/MOTD preview
9. start fleet
10. verify per-step result states

## Stage 4 — Manual fallback

- break/disable Discord webhook
- verify Copy Ping still works
- remove MOTD write capability
- verify Copy MOTD still works
- verify FleetOperation is not lost because one automation step failed

## Stage 5 — Live tracking

With test characters:

- join
- leave
- rejoin
- change ship
- change solar system
- change role where possible
- validate current member state
- validate event history
- validate Last ESI Update / stale behavior

## Stage 6 — Celery resilience

While a fleet is active:

- restart Celery worker
- restart Celery Beat
- restart Redis if safe in staging
- restart AA web process
- close the FC browser

Tracking should resume from server-side tasks and should not depend on the browser staying open.

## Stage 7 — Attendance

- one character / one user
- multiple alts / same user
- cap = 1
- cap = 2
- unlimited
- manual attendance add/edit/delete
- duplicate warning behavior

## Stage 8 — Corporation statistics

Use known test numbers and verify:

`Average PAP = Total PAP / Main Character Count`

Alt count must not change the denominator. Statistics are on the main character: PAP of an alt in another corporation or alliance counts for its main.

Then check the rest of the statistics with a month that has real PAP:

- Set **Member alliance IDs** in General Settings and add a **Monthly Target** (a corporation average and a member minimum).
- All Corps for a finished month: corporations at or above the target times their mains are green, the others red; for the running month below target is amber "Not reached yet" and reached is green.
- Members and Inactive as a corporation manager: only the own corporation is listed, a corporation's members page by ID (`/statistics/corporation/<id>/members/`) answers 403 and another corporation's member page answers 404; as a `view_all_stats` user the whole alliance.
- Leaderboard as a plain member, then switch **Leaderboard visibility** to statistics managers only: the menu entry disappears and the page answers 403.
- A fleet that started shortly before or after midnight UTC on the edge of a month counts in its EVE month and shows its EVE time, whatever the site's time zone.
- Download Excel and CSV of the members and corporations pages and open them in Excel or WPS: Chinese names are intact, the header row is bold and frozen, the About sheet is there, and the numbers equal the page.
- PAP history: choose a month, page through it, use previous/next month and **All retained**; the month stays in the links.

## Stage 9 — FC incentives

FC incentives are off by default. Tick **Incentive enabled** in FleetOps Administration → General Settings first, and check that the FC Incentive menu entry only appears while it is on.

Verify:

- minimum fleet threshold
- all monthly fleets count after threshold is met
- Fleet Type weights
- non-eligible FC exclusion
- waiver exclusion
- deterministic remainder
- review
- finalize
- unlock/recalculate
- audit entries

## Stage 10 — Proximity view

If SDE routing data is installed:

- FC current system
- fleet member at 0/1/2/3 jumps
- fleet member >3 jumps not included in proximity set
- system drill-down information

## Stage 11 — Multi-fleet / load test

After functional correctness:

- 2+ active fleets concurrently
- increasing member counts
- monitor Celery duration and database growth
- confirm tracking tasks do not serially block all active fleets


## Stage 12 — SRP integration

- Enable Alliance Auth built-in SRP on staging.
- Start a FleetOps operation.
- Confirm the SRP action reports success or a clear non-blocking schema error.
- Confirm the generated Open SRP link points to the expected staging SRP flow.
- Disable SRP and verify Fleet Start still works.

## Stage 13 — Historical attendance and membership pruning

- Grant an FC `fleetops.manage_attendance`.
- Add multiple manual credits to a closed fleet.
- Verify personal/corp/alliance history.
- Configure staging alliance ID(s).
- Move/remove a test member from the configured alliance state and run `fleetops_prune_history --dry-run`, then the real command.
- Confirm mapped departed-member attendance is removed.

## Stage 14 — Special roles / shared FC credit

- Assign Back Seat FC, Logi Anchor and Snowflake Member before and after Fleet End.
- Confirm FC credit follows the role's **Gives FC credit** switch for any pilot in Alliance Auth: Back Seat FC gives credit by default, also to a pilot without `fleetops.start_fleet`, and a role with the switch off gives no credit, also to a pilot who has `fleetops.start_fleet`.
- Confirm a pilot with FC credit cannot manage the fleet without `fleetops.manage_own_fleet`.
- Recalculate the monthly incentive period and verify the credited operation is counted exactly once per FC.

## Stage 15 — Kicks

- Add several pod/capsule characters to an active test fleet.
- Confirm **Kick Capsules** shows the expected count, opens a list of the capsule pilots by name, and kicks only the ticked ones.
- **Kick** a single member from the member list; the confirmation names the pilot and corporation.
- Use **Kick outside system…**: the FC's system is preselected, the members outside it are listed and ticked; untick some to spare them.
- Kick up to 10 members at once and confirm they are kicked in the request; kick more than 10 and confirm the whole batch runs on the Celery worker, with the results under Operation Actions.
- Confirm the FC character and the Fleet Boss are never targeted, and partial ESI failures are reported per member.
- Remove `esi-fleets.write_fleet.v1` from the FC character's token and confirm the kick buttons are disabled.
- Confirm the audit log has `fleet.kick_member`, `fleet.kick_outside_system` and `fleet.kick_capsules` entries, and that a member without `fleetops.view_all_fleets` sees no kicked pilot names under Operation Actions while the fleet runs.
