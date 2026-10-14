# Releasing

This document is for the maintainer. A release is a Git tag. Pushing a tag that starts with `v` runs
`.github/workflows/release.yml`, which checks the package, creates a GitHub Release with the wheel, the source
distribution and checksums, and can publish to PyPI.

## Versions

The version lives in one place: `__version__` in `fleetops/__init__.py` (PEP 440). `pyproject.toml` reads it,
the wheel and the User-Agent carry it, and the tag must be `v` plus the same string.

| Version | Meaning |
|---|---|
| `0.2.0b1` | First public pre-release. Install it with the version named; once a stable release exists, `pip` skips pre-releases unless asked for the version or given `--pre`. |
| `0.2.0b2`, `0.2.0rc1`, ... | Further pre-releases. The release marks every version with `a`, `b`, `rc` or `.dev` as a GitHub pre-release. |
| `1.0.0` | The first stable release, once the pre-releases have run stably in production. |
| `0.3.0.dev0` | The development version between releases. |

## One-time setup

These steps are done once by the owner of the repository.

1. **Rename the repository to `aa-fleetops`** (repository **Settings** > **General** > **Repository name**).
   GitHub redirects the old address, but update the remote of every clone
   (`git remote set-url origin https://github.com/yilifaer/aa-fleetops.git`) and the `pip install
   git+https://…` lines of sites that install from Git. The documents and the package metadata already use
   `https://github.com/yilifaer/aa-fleetops`.
2. **Enable Discussions** (**Settings** > **General** > **Features**) for questions, and **private
   vulnerability reporting** (**Settings** > **Code security**) for [SECURITY.md](../SECURITY.md). Create the
   issue labels `bug`, `enhancement` and `translation` that the issue forms use.
3. **PyPI account.** Create an account on pypi.org with two-factor authentication. Check that the name
   `aa-fleetops` is still free (`https://pypi.org/project/aa-fleetops/` answers 404); a pending publisher does
   not reserve it.
4. **Pending trusted publisher.** On PyPI open **Your account** > **Publishing** and add a pending publisher
   with the project name `aa-fleetops`, owner `yilifaer`, repository `aa-fleetops`, workflow `release.yml` and
   environment `pypi`. No PyPI token is needed or stored anywhere; the release run proves its identity to PyPI.
5. **GitHub environment `pypi`.** In **Settings** > **Environments** create `pypi`, add yourself as a required
   reviewer, and limit it to `v*` tags if you like. The PyPI job waits for your approval there.
6. **Repository variable.** In **Settings** > **Secrets and variables** > **Actions** > **Variables** create
   `PUBLISH_TO_PYPI` with the value `true`. While the variable is missing or not `true`, the release
   publishes to GitHub only.
7. Optional: protect the `dev` branch and require the CI jobs.

## Releasing a version

1. The CI of `dev` is green: every test job, the build job and the translations job without errors.
2. Update and translate the catalogs ([translations](translations.md)) and commit the `.po` and `.mo` files.
   The release run stops on any untranslated or fuzzy entry, and on a `.mo` file that does not match its `.po`
   file (a forgotten `compilemessages`).
3. Set `__version__` in `fleetops/__init__.py`. In `CHANGELOG.md` rename the section "Unreleased" to
   `## [0.2.0b1] - YYYY-MM-DD` (with the upgrade notes for administrators) and add a new empty "Unreleased"
   section above it. Commit as "Release 0.2.0b1".
4. Check the package locally:

   ```bash
   python -m pip install build twine
   python -m build
   twine check --strict dist/*
   python tools/check_dist.py dist
   python tools/check_docs.py
   python tools/check_translations.py --no-update
   python tools/changelog_section.py 0.2.0b1
   ```

   The last command prints the release notes the release run will use. Run the [manual testing](manual-testing.md)
   checklist on a test site with the wheel.
5. Tag and push:

   ```bash
   git tag -a v0.2.0b1 -m "AA FleetOps 0.2.0b1"
   git push origin dev v0.2.0b1
   ```

6. The release run creates the GitHub Release (a pre-release for `a`, `b`, `rc` and `.dev` versions) with the wheel,
   the source distribution and `SHA256SUMS.txt`, and the notes from the changelog. Check the page.
7. If PyPI publishing is set up, open the release run on the Actions tab and approve the **Publish to PyPI** job. A file on PyPI
   can never be replaced and a version can never be reused, so approve only after the GitHub Release looks
   right.
8. Change `__version__` to the next development version (for example `0.3.0.dev0`) and commit.

## What the release run does

1. **Build and check**: the tag must equal `v` plus `__version__`; the wheel and the source distribution are
   built; `twine check --strict` and `tools/check_dist.py` verify them (the wheel holds every module, template,
   static file and translation catalog, no test code, and metadata that names `aa-fleetops`, the version,
   `GPL-3.0-only` and the licence file; the source distribution holds the licence, both READMEs, the changelog
   and `pyproject.toml`, and none of the tests, which need the test project of the repository); `tools/check_translations.py` requires complete catalogs that are compiled (each `.mo` equals what `msgfmt` makes of its `.po`); `SHA256SUMS.txt` is written;
   `tools/changelog_section.py` extracts the notes and fails when the section is missing, empty, or still
   says "Unreleased".
2. **GitHub Release**: `gh release create` with the files, the checksums and the notes.
3. **Publish to PyPI** (only when `PUBLISH_TO_PYPI` is `true`, after approval of the `pypi` environment): uploads
   the wheel and the source distribution through trusted publishing. The checksum file is not uploaded, because
   PyPI rejects files that are not distributions.

If a job fails before the release exists, fix the problem, delete the tag
(`git push origin :refs/tags/v0.2.0b1` and `git tag -d v0.2.0b1`) and tag again. Never move or delete a tag
after a GitHub Release or a PyPI upload exists for it; publish the next version instead.

## Installing a release

For the users, `README.md` gives these lines:

```bash
pip install aa-fleetops==0.2.0b1
pip install --no-deps https://github.com/yilifaer/aa-fleetops/releases/download/v0.2.0b1/aa_fleetops-0.2.0b1-py3-none-any.whl
pip install --no-deps "git+https://github.com/yilifaer/aa-fleetops.git@v0.2.0b1"
```

`--no-deps` keeps `pip` from changing the Alliance Auth and django-esi versions of an existing site.

## Optional: a dry run on TestPyPI

To try the upload without touching PyPI, create an account on test.pypi.org and upload the files from your own
machine with `twine upload --repository testpypi dist/*`, using an API token of that account. Install from it in
a clean virtual environment, then discard the token. Do not commit tokens or add them to the repository.
