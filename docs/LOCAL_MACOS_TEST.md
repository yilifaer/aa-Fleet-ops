# Local macOS Smoke-Test Plan

This environment is for **development and smoke testing only**, not production hosting.

The goal is to prove that AA FleetOps can load inside a clean local Alliance Auth installation before moving to a staging server with real EVE SSO/ESI.

## Recommended local stack

- Homebrew
- dedicated Python virtualenv
- MariaDB
- Redis
- Alliance Auth 5.x
- Celery worker
- Celery Beat
- AA FleetOps installed from the exact wheel intended for staging

Keep all project files in an isolated working directory such as `~/fleetops-lab/` and do not use the macOS system Python.

## Smoke-test gate

Do not proceed to staging until all of the following are green:

- [ ] clean Alliance Auth starts before FleetOps is installed
- [ ] FleetOps wheel installs
- [ ] `fleetops` can be added to `INSTALLED_APPS`
- [ ] `python manage.py check` passes
- [ ] `python manage.py migrate fleetops` passes
- [ ] `python manage.py collectstatic --noinput` passes
- [ ] `python manage.py fleetops_seed --demo` passes
- [ ] Alliance Auth web process starts
- [ ] FleetOps menu/page loads for an authorized user
- [ ] Start Fleet page renders
- [ ] Admin configuration pages work
- [ ] Celery worker imports FleetOps tasks without error
- [ ] Celery Beat accepts the FleetOps schedule
- [ ] restart preserves data
- [ ] Fittings absent does not crash FleetOps
- [ ] optional integration presence does not crash FleetOps
- [ ] no unexplained migration warnings
- [ ] no HTTP 500 on basic navigation

## Suggested diagnostic commands

Run from the Alliance Auth project/venv as appropriate:

```bash
python manage.py check
python manage.py showmigrations fleetops
python manage.py migrate fleetops
python manage.py fleetops_seed --demo
python manage.py collectstatic --noinput
```

For the package's pure calculation tests from the source root:

```bash
python -m unittest tests.test_calculations
```

The full Django test suite (`python runtests.py`, which needs Redis) is described in [`TESTING.md`](../TESTING.md).

## Important boundary

Local macOS smoke testing is not proof that EVE ESI behavior works. Real fleet tracking belongs in staging after the application passes this gate.
