# AA FleetOps Test Plan

This alpha should be tested on a non-production Alliance Auth instance first.

## Packaging smoke test

```bash
python -m compileall fleetops
python -m unittest tests.test_calculations
```

## AA integration smoke test

After installation:

```bash
python manage.py check
python manage.py showmigrations fleetops
python manage.py migrate fleetops
python manage.py fleetops_seed --demo
python manage.py collectstatic --noinput
python manage.py fleetops_prune_history --dry-run
```

Then verify `/fleetops/` loads for a user with `fleetops.basic_access`.

## Required real-EVE scenarios

1. FC character with valid read/write scopes and Fleet Commander role.
2. Alt character under the same Auth user.
3. Character not in fleet.
4. Character in fleet but not Fleet Commander.
5. Read scope missing.
6. Write scope missing: tracking may work but MOTD must fail non-fatally.
7. Discord webhook success.
8. Discord webhook unavailable: Start Fleet must continue and manual copy must remain usable.
9. Member join/leave/rejoin.
10. Ship and system changes.
11. Attendance cap with multiple characters on one Auth user.
12. Manual attendance correction.
13. End Fleet.
14. Repeated ESI fleet-not-found auto-end.
15. Monthly FC calculation, waiver, finalize and unlock.
16. Built-in SRP available: Fleet Start should create/link SRP without blocking the fleet if SRP fails.
17. Manual attendance permission: FC can add multiple credits to a closed fleet; another FC cannot edit that fleet unless separately credited/admin.
18. Attendance history: personal/corp/alliance views show up to configured retention; a departed mapped member is filtered/pruned when membership filtering is enabled.
19. Assign Back Seat FC / Logi Anchor / Snowflake on active and closed fleets; an assigned user with `fleetops.start_fleet` receives FC credit.
20. Capsule cleanup: place pod/capsule members in an active EVE fleet and verify one-click kick removes all detected pods except the Fleet Boss.
21. Dashboard renders metric cards, top ships, recent participation and active-fleet data with both light/dark AA themes.

## Bug report data

Please include:

- AA version
- Python version
- django-esi version
- FleetOps version
- whether `fittings` / `eve_sde` are installed
- traceback or Celery task error
- FleetOps `OperationAction` step that failed

Never include ESI refresh tokens or Discord webhook URLs in a public issue.


## a4 historical statistics and fleet archive checks

1. Open My Statistics and switch Year/Month; verify attendance, unique fleets, FC metrics, characters, ships, special roles and daily activity all change to the selected month.
2. Open Corporation Statistics and switch months; verify Total Attendance / Main Character Count / Average and Fleet Type breakdown.
3. With `view_all_stats`, open All Corporation Statistics, switch months, then drill into a corporation and verify the selected period is preserved.
4. Open FC Statistics, switch months, then drill into an FC and verify credited fleets and point breakdown.
5. Open FC Incentive and switch historical months using the same selector.
6. With `manage_own_fleet`, open Fleets and verify only credited fleets are visible. With `manage_fleets`, verify all fleets are visible.
7. Test filters for year, month, Fleet Type, status, FC, doctrine and search text.
8. Edit a closed fleet from the archive. Verify metadata changes, AuditLog entry and Fleet Type point snapshot correction.
9. Verify editing does not resend a ping, rewrite MOTD or create tracking events.

## 0.1.0a5 regression checklist

### a4 bug report

- `/fleetops/operations/?year=-1` -> 200, current year fallback.
- `/fleetops/operations/?year=0` -> 200, current year fallback.
- `/fleetops/operations/?year=99999` -> 200, current year fallback.
- `/fleetops/operations/?fleet_type=abc` -> 200, no crash.
- Closed fleet with zero active members but historical member states -> Special Role form remains visible.
- Authenticated user without Fleet Manager permissions -> protected manage URLs return 403, not login redirect.
- Unknown corporation statistics ID -> 404.
- `shasum -a 256 -c SHA256SUMS.txt` from release package root succeeds.

### Attendance finalization

1. Start a tracked fleet and create automatic attendance.
2. With fleet duration <= 90 minutes, End Fleet directly closes using the current multiplier.
3. With fleet duration > 90 minutes, End Fleet opens the 1x/2x/3x confirmation modal.
4. Select 2x and confirm all granted automatic rows now have `attendance_value=2`.
5. Confirm capped automatic rows remain ungranted.
6. Confirm manual attendance rows retain their explicit values.
7. From the closed fleet Attendance tab, change 2x -> 3x and confirm automatic rows update.

### Attendance Tracking Only

1. Select `Attendance Tracking Only` on Start Fleet.
2. Confirm Fleet Boss detection and ESI tracking still start.
3. Confirm `discord_ping`, `motd_update` and `srp_link` actions are `SKIPPED`.
4. Confirm no Discord webhook is sent and no automatic MOTD/SRP call is made.
5. Confirm Ping/MOTD previews can still be copied manually.

### Permission bundles

Test four real Alliance Auth groups using `docs/PERMISSIONS.md`:

- Member: own participated fleets/history only.
- Corp Management: Member + own corporation statistics/history.
- FC: complete read-only fleet archive plus own fleet control/attendance/manual fleet creation.
- FC Lead: all FleetOps capabilities.

### Manual fleet

1. FC creates a manual historical fleet record.
2. Record appears as `Manual` in the Fleet archive.
3. It has no ESI fleet ID and no active tracking.
4. FC can add manual attendance after creation.
5. The manual fleet counts in FC monthly fleet count/points.
