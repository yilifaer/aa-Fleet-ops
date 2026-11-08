# Releasing

This document is for the maintainer. A release is a Git tag. Pushing a tag that starts with `v` runs
`.github/workflows/release.yml`, which checks the package and creates a GitHub Release with the wheel, the source
distribution, checksums and the notes from `CHANGELOG.md`. Publishing to PyPI is optional.

The GitHub repository is `yilifaer/aa-Fleet-ops`. The PyPI distribution is named `aa-fleetops`, the wheel file
`aa_fleetops-*` and the Django app `fleetops`; these names do not change.

## Versions

The version lives in one place: `__version__` in `fleetops/__init__.py` (PEP 440). `pyproject.toml` reads it,
the wheel and the User-Agent carry it, and the tag must be `v` plus the same string.

| Version | Meaning |
|---|---|
| `0.3.0b1` | The first GitHub pre-release: the SRP rework (categories, review and payment, discovery of losses, statistics, import from aa-srp). The earlier `0.2.0b2` and `0.2.0b3` were never released separately; their changelog sections are part of it. |
| `0.3.0b2`, `0.3.0rc1`, ... | Further pre-releases. Every version with `a`, `b`, `rc` or `.dev` becomes a GitHub pre-release. |
| `1.0.0` | The first stable release, once the pre-releases have run stably in production. |
| `0.4.0.dev0` | The development version between releases. |

Once a stable release exists on PyPI, `pip` skips pre-releases unless asked for the version or given `--pre`.

## One-time setup

1. **Enable Discussions** (**Settings** > **General** > **Features**) for questions, and **private vulnerability
   reporting** (**Settings** > **Code security**) for [SECURITY.md](../SECURITY.md). Create the issue labels `bug`,
   `enhancement` and `translation` that the issue forms use.
2. Optional: protect the `dev` branch and require the CI jobs.
3. Optional, only to publish on PyPI:
   - Create an account on pypi.org with two-factor authentication and check that the name `aa-fleetops` is free.
   - On PyPI open **Your account** > **Publishing** and add a pending publisher: project `aa-fleetops`, owner
     `yilifaer`, repository `aa-Fleet-ops`, workflow `release.yml`, environment `pypi`. No token is stored anywhere.
   - In **Settings** > **Environments** create `pypi` and add yourself as a required reviewer.
   - In **Settings** > **Secrets and variables** > **Actions** > **Variables** create `PUBLISH_TO_PYPI` with the value
     `true`. While the variable is missing or not `true`, the release publishes to GitHub only.

## Releasing a version

1. The CI of `dev` is green: every test job, the build job and the translations job.
2. Update and translate the catalogs ([translations](translations.md)) and commit the `.po` and `.mo` files. The
   release run stops on an untranslated or fuzzy entry and on a `.mo` file that does not match its `.po` file.
3. Set `__version__` in `fleetops/__init__.py`. In `CHANGELOG.md` give the section of the version the heading
   `## [0.3.0b1] - YYYY-MM-DD` (the current UTC date, `date -u +%F`; a heading that still says "Unreleased" stops the
   release run) with the upgrade and rollback notes for administrators. Commit as "Release 0.3.0b1".
4. Check the package locally (replace the version):

   ```bash
   python -m pip install build twine
   python -m build
   twine check --strict dist/*
   python tools/check_dist.py dist
   python tools/check_docs.py
   python tools/check_ui.py --strict
   node tools/ui_check/run.js --python .venv/bin/python
   python tools/check_translations.py --no-update
   python tools/changelog_section.py 0.3.0b1
   ```

   The interface run must end without a FAIL. The last command prints the release notes the release run will use.
   Run the [manual testing](manual-testing.md) checklist on a test site with the wheel.
5. Tag and push:

   ```bash
   git tag -a v0.3.0b1 -m "AA FleetOps 0.3.0b1"
   git push origin dev v0.3.0b1
   ```

   Without a way to push tags, push `dev` only and start the release run on the **Actions** tab instead: **Release** >
   **Run workflow** on `dev`. It builds that commit and creates the tag `v` plus `__version__` on it (a lightweight
   tag); it stops when the tag exists already and runs only on `dev`.

6. The release run creates the GitHub Release (a pre-release for `a`, `b`, `rc` and `.dev` versions) with the wheel,
   the source distribution and `SHA256SUMS.txt`, and the notes from the changelog. Check the page.
7. Only with PyPI publishing set up: open the release run on the Actions tab and approve the **Publish to PyPI** job.
   A file on PyPI can never be replaced and a version can never be reused, so approve only after the GitHub Release
   looks right.
8. Change `__version__` to the next development version (for example `0.4.0.dev0`) and commit.

## What the release run does

1. **Build and check**: the tag must equal `v` plus `__version__` (a run started by hand on `dev` uses that tag and
   stops when it exists already); the wheel and the source distribution are built;
   `twine check --strict` and `tools/check_dist.py` verify them (the wheel holds every module, template, static file
   and translation catalog, no test code, and metadata that names `aa-fleetops`, the version, `GPL-3.0-only` and the
   licence file; the source distribution holds the licence, both READMEs, the changelog and `pyproject.toml`, and none
   of the tests); `tools/check_translations.py` requires complete, compiled catalogs; `SHA256SUMS.txt` is written;
   `tools/changelog_section.py` extracts the notes and fails when the section is missing, empty, or still says
   "Unreleased".
2. **GitHub Release**: `gh release create` with the files, the checksums and the notes. A run started by hand also creates the tag on the
   commit it built.
3. **Publish to PyPI** (only when `PUBLISH_TO_PYPI` is `true`, after approval of the `pypi` environment): uploads the
   wheel and the source distribution through trusted publishing. The checksum file is not uploaded.

If a job fails before the release exists, fix the problem, delete the tag
(`git push origin :refs/tags/v0.3.0b1` and `git tag -d v0.3.0b1`) and tag again. Never move or delete a tag after a
GitHub Release or a PyPI upload exists for it; publish the next version instead.

For a run started by hand that failed before the release existed, no tag was created: fix the problem, push `dev` and
start a new run. **Re-run all jobs** would build the old commit, and once the release exists it stops at the tag
check; re-run only the failed job instead (for example **Publish to PyPI**).

## Installing a release

For the users, `README.md` gives these lines:

```bash
pip install aa-fleetops==0.3.0b1
pip install --no-deps https://github.com/yilifaer/aa-Fleet-ops/releases/download/v0.3.0b1/aa_fleetops-0.3.0b1-py3-none-any.whl
pip install --no-deps "git+https://github.com/yilifaer/aa-Fleet-ops.git@v0.3.0b1"
```

The first line works only once the version is on PyPI. `--no-deps` keeps `pip` from changing the Alliance Auth and
django-esi versions of an existing site.

## Optional: a dry run on TestPyPI

To try the upload without touching PyPI, create an account on test.pypi.org and upload the files from your own
machine with `twine upload --repository testpypi dist/*`, using an API token of that account. Install from it in a
clean virtual environment, then discard the token. Do not commit tokens or add them to the repository.
