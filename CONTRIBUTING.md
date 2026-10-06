# Contributing

AA FleetOps is intended as an open-source Alliance Auth community app.

## Development setup

Work in a virtualenv (Python 3.10 or newer) and install FleetOps in editable mode with the test extra from the repository root:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[test]"
```

This installs Alliance Auth and django-esi as well. Alliance Auth depends on `mysqlclient`, which needs the MySQL/MariaDB client headers and `pkg-config` to build (for example `default-libmysqlclient-dev pkg-config` on Debian/Ubuntu).

The tests run against the small Alliance Auth project in `testauth/` and need a running Redis server, which the test settings use as the Django cache. By default they use `redis://localhost:6379/13`; set `FLEETOPS_TEST_REDIS` (for example `redis://localhost:6379/9`) to use another server or database.

## Before submitting a change

Run the same checks as CI from the repository root:

```bash
python manage.py check
python manage.py makemigrations fleetops --check --dry-run
python runtests.py
```

`python runtests.py` runs the whole suite in `tests/` on an in-memory SQLite database. Pass a module to run only part of it, for example `python runtests.py tests.test_attendance`.

To run against MariaDB or MySQL instead, set `FLEETOPS_TEST_DB`; Django creates and drops a `test_<name>` database for the run, so the user needs permission to create databases:

```bash
FLEETOPS_TEST_DB=mysql://user:pass@host:3306/name python runtests.py
```

Coverage is optional locally, but this is what CI reports:

```bash
coverage run --source=fleetops --omit='fleetops/migrations/*' runtests.py tests
coverage report --skip-covered --sort=cover
```

If you change a model, create the migration with `python manage.py makemigrations fleetops` and include it in the same change.

CI runs these checks for Alliance Auth 5.2 and 5.4 with django-esi 9 and for Alliance Auth 5.5 with django-esi 10, on SQLite and MariaDB. [TESTING.md](TESTING.md) has the full matrix.

## Guidelines

Keep alliance policy configurable but keep the core workflow simple. Avoid adding a general rule DSL unless a future version has a demonstrated cross-alliance requirement for it.

Do not log or expose Discord webhook URLs, ESI access tokens or ESI refresh tokens. That includes log messages, error text stored on fleets or steps, and anything shown on a page.
