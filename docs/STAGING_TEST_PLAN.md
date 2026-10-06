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
- selected character in fleet but not appropriate boss/commander role
- selected character is Fleet Boss/Commander
- confirm detected Fleet ID and member count

## Stage 3 — Start Fleet

Test the complete intended workflow:

1. select FC alt
2. detect fleet
3. select Fleet Type
4. select Doctrine / Custom
5. manually enter Form Up
6. select Comms / Logi / Boost
7. select Ping Target
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

`Average Attendance = Total Granted Attendance / Main Character Count`

Alt count must not change the denominator.

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
- Confirm only assigned users who currently have `fleetops.start_fleet` receive FC credit.
- Recalculate the monthly incentive period and verify the credited operation is counted exactly once per FC.

## Stage 15 — Capsule cleanup

- Add several pod/capsule characters to an active test fleet.
- Confirm FleetOps detects the expected pod count.
- Execute Kick all Capsules.
- Confirm pods are removed, Fleet Boss is never targeted, and partial ESI failures are reported per action.
