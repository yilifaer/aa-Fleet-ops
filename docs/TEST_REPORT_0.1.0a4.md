# AA FleetOps 0.1.0a4 — Bug Report

**Tested build:** `aa-fleetops 0.1.0a4` (editable install from `source/`)
**Environment:** Alliance Auth 5.2.0, Django 5.2.17, Python 3.12.14, MariaDB 12.3.3, Redis 8.10.1
**Test data:** 39 fleet operations, 600 attendance records, 581 member states, 1177 member events, 16 users across 3 corporations
**Note:** All ESI-dependent behaviour is still unverified (no SSO configured). Everything below is reproducible without ESI.

---

## Summary

| # | Severity | Issue | Location |
|---|----------|-------|----------|
| 1 | High | Invalid `year` query param causes HTTP 500 | `views.py:293` |
| 2 | High | Non-numeric `fleet_type` query param causes HTTP 500 | `views.py:328` |
| 3 | Medium | Special-role assignment impossible after fleet end | `views.py:430` + `operation_detail.html:165` |
| 4 | Medium | Permission failure redirects to login instead of 403 | `views.py` (9 views) |
| 5 | Low | Non-existent corporation ID returns 200 instead of 404 | `corporation_statistics_detail_view` |
| 6 | Low | `SHA256SUMS.txt` missing `dist/` path prefix | packaging |

---

## 1. [High] Invalid `year` query parameter causes HTTP 500

**Reproduce** (all three return HTTP 500):

```
GET /fleetops/operations/?year=-1
GET /fleetops/operations/?year=0
GET /fleetops/operations/?year=99999
```

**Traceback:**

```
File "fleetops/views.py", line 363, in fleet_operations_view
    page = Paginator(qs, 50).get_page(request.GET.get("page"))
...
File "django/db/backends/base/operations.py", line 636, in year_lookup_bounds_for_datetime_field
    first = datetime.datetime(value, 1, 1)
ValueError: year -1 is out of range
```

**Root cause — duplicated logic that dropped a validation step.**

The project already has a correct helper at `views.py:115`:

```python
def _year_month(request):
    now = timezone.now()
    try:
        year = int(request.GET.get("year", now.year))
        month = int(request.GET.get("month", now.month))
    except (TypeError, ValueError):
        return now.year, now.month
    if month < 1 or month > 12 or year < 2003 or year > 2200:   # range check present
        return now.year, now.month
    return year, month
```

`fleet_operations_view` (added in a4) reimplements this at `views.py:293-302` but only
range-checks `month`, not `year`:

```python
try:
    year = int(request.GET.get("year", now.year))
except (TypeError, ValueError):
    year = now.year
# <-- no year range check here
month_raw = request.GET.get("month", "")
try:
    month = int(month_raw) if month_raw else None
except (TypeError, ValueError):
    month = None
if month is not None and not (1 <= month <= 12):   # month IS checked
    month = None
```

The unvalidated year reaches `qs.filter(started_at__year=year)` and Django raises
`ValueError` while building the SQL bounds.

**Confirmed not affected:** `my_statistics`, `all_fc_statistics`, `incentive_review` — all use
`_year_month()` and survived 11 malformed year/month combinations
(`month=13`, `month=0`, `month=abc`, `year=abc`, `year=-1`, `year=99999`, `year=0`, empty values, etc.).

**Suggested fix** — add the missing range check (keeping the "month may be empty = whole year" semantics):

```python
try:
    year = int(request.GET.get("year", now.year))
except (TypeError, ValueError):
    year = now.year
if year < 2003 or year > 2200:
    year = now.year
```

---

## 2. [High] Non-numeric `fleet_type` query parameter causes HTTP 500

**Reproduce:**

```
GET /fleetops/operations/?fleet_type=abc
```

**Traceback:**

```
File "fleetops/views.py", line 328, in fleet_operations_view
    qs = qs.filter(fleet_type_id=fleet_type)
ValueError: Field 'id' expected a number but got 'abc'.
```

**Code** (`views.py:326-328`):

```python
fleet_type = request.GET.get("fleet_type", "").strip()
if fleet_type:
    qs = qs.filter(fleet_type_id=fleet_type)   # no numeric validation
```

**Suggested fix:**

```python
if fleet_type.isdigit():
    qs = qs.filter(fleet_type_id=fleet_type)
```

Note: `?fleet_type=99999` (valid number, no such row) is already safe — returns an empty list.
Only non-numeric input crashes.

---

## 3. [Medium] Special roles cannot be assigned after a fleet ends

Present since a3, still present in a4. This contradicts `NEW-IN-A3.md`:

> "Assign from tracked fleet members **even after Fleet End**."

**The form is built correctly** — `forms.py:184` deliberately does not filter by `is_active`:

```python
for member in operation.member_states.all().order_by("character_name")
```

**But the template gate uses a filtered queryset.** `views.py:430`:

```python
members = operation.member_states.filter(is_active=True).order_by("character_name")
```

`operation_detail.html:165`:

```django
{% if members %}
  <form ...>Assign Special Role</form>
{% else %}
  <div class="text-muted">No tracked fleet members are available for role assignment yet.</div>
{% endif %}
```

Because `tracking.py:38-42` sets `is_active = False` whenever a member disappears from the fleet,
a fleet that ends after everyone has left has zero active members, so the assignment form is
permanently hidden. The hint text right below it — *"Roles can be changed after a fleet is
closed"* — can then never be acted on.

**Controlled experiment** (same closed operation, `is_active` as the only variable):

| Active members | Assignment form |
|---|---|
| 21 / 26 | rendered |
| 0 / 26 | hidden |

**Suggested fix** — pass an unfiltered queryset for the gate, keep `members` for the roster table:

```python
# views.py, operation_detail
members = operation.member_states.filter(is_active=True).order_by("character_name")
assignable_members = operation.member_states.all().order_by("character_name")
```

```django
{# operation_detail.html:165 #}
{% if assignable_members %}
```

---

## 4. [Medium] Permission failures redirect to the login page instead of returning 403

Nine views use `@user_passes_test(_fleet_manager_test)`:

```
views.py:289  fleet_operations_view
views.py:384  edit_operation_view
views.py:467  end_fleet_view
views.py:478  retry_ping_view
views.py:489  retry_motd_view
views.py:500  retry_srp_view
views.py:835  add_operation_role
views.py:859  delete_operation_role
views.py:871  kick_capsules_view
```

`user_passes_test` redirects to `settings.LOGIN_URL` when the test fails. An authenticated user
without `manage_own_fleet` / `manage_fleets` who opens the operations archive is therefore sent to
`/account/login/?next=/fleetops/operations/` and appears to have been logged out, instead of being
told they lack permission.

Every other FleetOps view uses `permission_required(..., raise_exception=True)` and correctly
returns 403. Permission matrix (15 pages × 8 permission tiers) — only this row differs:

```
page                            required perm      none  basic  start  corp  all  config  own_fleet  all-10
dashboard                       basic_access        403   200    200   200   200   200      200      200
start_fleet                     start_fleet         403   403    200   403   403   403      403      200
configuration_index             manage_configuration 403   403    403   403   403   200      403      200
...                             (11 more rows, all 403 when denied)
fleet_operations                manage_own_fleet    302   302    302   302   302   302      200      200
                                                    ^^^ redirect to login, not 403
```

**Suggested fix** — Alliance Auth ships a decorator built for exactly this case
(any-of permissions + `raise_exception`), see `allianceauth/authentication/decorators.py:53`:

```python
from allianceauth.authentication.decorators import permissions_required

@permissions_required(("fleetops.manage_own_fleet", "fleetops.manage_fleets"), raise_exception=True)
def fleet_operations_view(request):
    ...
```

The access logic stays identical; only the denial response changes from 302 to 403.

---

## 5. [Low] Non-existent corporation ID returns an empty page instead of 404

```
GET /fleetops/statistics/corporation/99999999/   -> 200 (empty statistics page)
GET /fleetops/statistics/fcs/99999999/           -> 404
```

The two drill-down pages disagree on how a missing target is handled. A corporation ID is not a
foreign key so there is no automatic 404, but from a user's perspective a non-existent corporation
should not render as "this corporation had zero attendance this month".

---

## 6. [Low] `SHA256SUMS.txt` is missing the `dist/` path prefix

In the a4 package:

```
97f35fd694a841a9de6b71ee8585cd46f23f73439d772285bb1e6ee0381decec  aa_fleetops-0.1.0a4-py3-none-any.whl
ff389f8b4f55c1feed1a0f04468026360751e8db2816772244e17daf62472c1f  aa-fleetops-0.1.0a4-full-source.zip
```

The wheel actually lives at `dist/aa_fleetops-0.1.0a4-py3-none-any.whl`, so `shasum -a 256 -c
SHA256SUMS.txt` from the package root reports `FAILED open or read` for the wheel. The a3 package
used the `dist/` prefix correctly — this is a regression in the packaging script.

The files themselves are intact; hashes match when compared manually.

---

## Verified clean — no need to re-investigate

| Area | Method | Result |
|---|---|---|
| Permission matrix | 15 pages × 8 permission tiers with purpose-built test users | No over-permissive or over-restrictive page found |
| Write-operation access control | Code review + live requests | `edit_operation`, `kick_capsules`, `add/delete_operation_role`, `end_fleet`, `retry_*` all double-check via `_operation_for_user(manage=True)` + `_can_manage_operation`; `kick_capsules` additionally requires `status == ACTIVE` |
| Row-level data isolation | Archive page as non-`manage_fleets` user | Correctly limited to the user's own FC operations and FC-credited role assignments |
| SQL injection | `?q=' OR 1=1--` | 200, ORM parameterisation holds |
| XSS | `?fc=<script>alert(1)</script>` | 200, no injection |
| Pagination edge cases | `page=0`, `-1`, `abc`, `99999` | All safe |
| Year/month params on other stat pages | 11 malformed combinations | All safe (they use `_year_month()`) |
| a3→a4 migration with data in place | 600 attendance rows present | `No migrations to apply`, zero data loss |
| ESI failure degradation | Tracking task run without any ESI token | Records accurate `last_error`, does not increment `fleet_missing_count`, does not destroy existing member/attendance rows — this is handled well |
| Configuration audit logging | Created + deleted config rows via front end | `configuration.create` / `configuration.delete` written correctly |
| Discord webhook redaction | Stored a webhook containing a known secret string, then searched audit rows | Secret correctly redacted, not present in audit JSON |
| Front-end operation editing | POST changed `formup` | Persisted, `operation.edit` audit entry written |
| Archive filters | Compared rendered row counts against DB | 39 total / 1 active / 38 closed — exact match |
| Month navigation | Compared returned data across 3 months | Distinct per-month data, not a static page |
