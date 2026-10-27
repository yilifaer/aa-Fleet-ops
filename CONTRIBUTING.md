# Contributing

AA FleetOps is free software for Alliance Auth sites, and contributions are welcome: bug reports, ideas,
translations, documentation and code.

- Found a problem? Open an [issue](https://github.com/yilifaer/aa-fleetops/issues/new/choose) with the bug form.
- Have a question about installing or using FleetOps? Ask in
  [Discussions](https://github.com/yilifaer/aa-fleetops/discussions).
- Want to fix a wording or add a language? Use the translation form, or read
  [docs/translations.md](docs/translations.md) and send a pull request.
- Found a security problem? Follow [SECURITY.md](SECURITY.md); do not open a public issue.

Open an issue or a discussion before you start a large change, so the direction can be agreed first.

## Development setup

Work in a virtual environment (Python 3.10 or newer). From the repository root:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[test]"
```

This installs Alliance Auth and django-esi as well. Alliance Auth depends on `mysqlclient`, which needs the
MySQL/MariaDB client headers and `pkg-config` to build (on Debian or Ubuntu:
`default-libmysqlclient-dev pkg-config`). The translation tools need the GNU gettext package
(`gettext` on Debian and Ubuntu).

The tests run against the small Alliance Auth project in `testauth/` and need a running Redis server, which the
test settings use as the Django cache. The default is `redis://localhost:6379/13`; set `FLEETOPS_TEST_REDIS`
(for example `redis://localhost:6379/9`) to use another server or database.

## Running the tests

Run the same checks as CI from the repository root:

```bash
python manage.py check
python manage.py makemigrations fleetops --check --dry-run
python runtests.py
```

`python runtests.py` runs everything in `tests/` on an in-memory SQLite database. Celery tasks run eagerly, so
for example End Fleet closes the fleet within the request. Pass a module to run only part of the suite:

```bash
python runtests.py tests.test_pap_engine
```

To run against MariaDB or MySQL, set `FLEETOPS_TEST_DB`. Django creates and drops a `test_<name>` database for
the run, so the user needs the right to create and drop databases:

```bash
FLEETOPS_TEST_DB=mysql://user:password@127.0.0.1:3306/fleetops python runtests.py
```

Coverage is optional locally; this is what CI reports:

```bash
coverage run --source=fleetops --omit='fleetops/migrations/*' runtests.py tests
coverage report --skip-covered --sort=cover
```

The tests never call ESI, zKillboard or Discord. Create data with the helpers in `tests/factories.py` and mock
the provider functions in new tests.

### Continuous integration

GitHub Actions runs the Django checks, the migration check and the full suite with coverage on every push and
pull request, with a Redis and a MariaDB service:

| Python | Alliance Auth | django-esi | Database |
|---|---|---|---|
| 3.10 | 5.2 | 9 | SQLite |
| 3.12 | 5.4 | 9 | MariaDB |
| 3.13 | latest 5.x | 10 | MariaDB |
| 3.14 | latest 5.x | 10 | SQLite |

Alliance Auth 5.4 and older stay on django-esi 9; Alliance Auth 5.5 requires django-esi 10. Two more jobs
check the translation catalogs (`tools/check_translations.py`, which only warns on branches and pull requests)
and build the wheel and source distribution (with `twine check`, `tools/check_dist.py` and
`tools/check_docs.py`). You can run the same commands locally:

```bash
python -m pip install build twine
python -m build
twine check --strict dist/*
python tools/check_dist.py dist
python tools/check_docs.py
```

## Coding rules

- Keep alliance policy configurable and the core process simple. Do not add a general rule language unless
  several alliances need one.
- Support Alliance Auth 5.2 to 5.x and django-esi 9 and 10. Do not add a required dependency. Optional apps
  (fittings, eve_sde, aasrp, Alliance Auth SRP, the Alliance Auth Discord service, CharLink) are imported
  behind a guard and FleetOps keeps working without them. AFAT, django-eveuniverse and aadiscordbot are not
  imported at all.
- A model change comes with its migration in the same change. Migrations only add; they are not edited after a
  release.
- Never put ESI tokens or Discord webhook URLs into logs, pages, stored error messages, audit entries or
  messages shown to users.
- Log through `allianceauth.services.hooks.get_extension_logger`.
- Every new `.py`, `.js` and `.css` file starts with the two SPDX lines that the other files have
  (`SPDX-FileCopyrightText` and `SPDX-License-Identifier: GPL-3.0-only`); `tests/test_license_headers.py`
  checks it. Templates, migrations and documents have none.

### Checking the interface

Every page has to look right in every Alliance Auth theme and on a phone. Two checks belong together and are
described in [tools/ui_check/README.md](tools/ui_check/README.md):

- `python tools/check_ui.py --strict` finds forbidden classes, colours and theme variables in the sources. It needs
  nothing but Python, and the CI runs it.
- `node tools/ui_check/run.js --python .venv/bin/python` opens every page in a real browser (five themes, three
  widths, English and Chinese, the interaction states such as menus, dialogs, drag and the previews) and reports
  contrast, clipped text, overflow, touch size, focus rings and missing names. Run it before a release (see
  [docs/releasing.md](docs/releasing.md)) and look at the screenshots at 375 px yourself.

New interface text and colours use the tokens and components of the FleetOps stylesheet (pills, `fo-` classes); do not
write colours or Bootstrap utility classes that only look right in one theme.

### Text and translations

All text people read is translatable. The rules are short:

- Module level (field options, choices, constants): `from django.utils.translation import gettext_lazy as _`.
  Inside functions: `from django.utils.translation import gettext as _`. Use `ngettext` for counts and
  `pgettext` when a short word is ambiguous. Do not import them under another name; `makemessages` only finds
  `_`, `gettext*`, `ngettext*`, `pgettext*` and `gettext_noop`.
- Use named placeholders: `_("Kicked %(count)s pilots.") % {"count": n}`. Never build a translatable sentence
  with an f-string, `.format()` or by joining translated pieces.
- Text that code writes to the database for people to read later (step results, end reasons, ESI errors, job
  results, automatic SRP decisions) is stored in English. Declare it in `fleetops/texts.py` with `StoredText`;
  pages show it through `translate_stored()` or the `stored_text` template filter. Do not store lazy strings
  in the database, the cache, JSON or Celery arguments.
- Discord pings and in-game MOTDs are always English: render them inside `outgoing_language()`, write FleetOps's
  own words in them as plain English and never mark them for translation. Text that administrators write in a
  template is sent as written.
- Templates use `{% translate %}` and `{% blocktranslate %}`; static JavaScript uses `gettext()`,
  `ngettext()` and `interpolate()`.
- The term is "PAP". Ship and solar system names stay English everywhere.

The commands to update the catalogs and the glossary are in [docs/translations.md](docs/translations.md).
Run `makemessages` when you add or change texts if you can; otherwise a maintainer does it before the next
release.

### Style of text

Write English text in the interface, documents, changelog entries, commit messages and pull request
descriptions in plain, specific sentences in the present tense. Use the imperative for steps, sentence case for
headings, and the labels of the interface in bold exactly as they appear. No filler, no marketing words, no
emojis.

## Documentation

The documents are in `docs/`. The administrator documents `configuration.md`, `permissions.md`,
`message-templates.md` and `integrations.md` also have a Simplified Chinese version in `docs/zh-CN/`; change
both when you change one. Run `python tools/check_docs.py` to find broken links.

## Pull requests

Make small, focused changes and describe what changed and why. Before you open the pull request:

- the full suite passes (`python runtests.py`), and the migration check reports no changes;
- new behavior has tests;
- texts are marked for translation;
- the CHANGELOG has an entry under "Unreleased";
- the documentation matches the change.

The pull request template repeats this list. Releases are made by the maintainer; the steps are in
[docs/releasing.md](docs/releasing.md).
