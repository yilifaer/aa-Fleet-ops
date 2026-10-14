#!/usr/bin/env python
# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Check the built wheel and source distribution before they are published.

Usage: python tools/check_dist.py [DIST_DIR]
       python tools/check_dist.py --print-version

DIST_DIR defaults to ``dist``. The wheel must hold every module, template, static file and translation
catalog of the repository and no test code, and its metadata must match the repository. The source
distribution must hold the licence, both READMEs, the changelog and ``pyproject.toml``. Run it from a
checkout after ``python -m build``. It needs nothing but the standard library.
"""

import argparse
import ast
import email.parser
import re
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = "fleetops"
DISTRIBUTION = "aa-fleetops"
LICENSE_EXPRESSION = "GPL-3.0-only"
LICENSE_FILE = "LICENSE"
# Folders of the package whose files are package data and must all be shipped.
DATA_FOLDERS = ("templates", "static", "locale")
# Files that stay in the repository to keep an empty folder.
IGNORED_NAMES = {".gitkeep"}
# Top-level folders that must never be in a wheel.
FORBIDDEN_TOP_LEVEL = ("tests", "testauth", "tools", "docs")
SDIST_FILES = (
    "LICENSE",
    "README.md",
    "README.zh-CN.md",
    "CHANGELOG.md",
    "CONTRIBUTING.md",
    "SECURITY.md",
    "pyproject.toml",
)


def source_version(root=ROOT):
    """``__version__`` of the package, read without importing it."""
    tree = ast.parse((root / PACKAGE / "__init__.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", "") == "__version__" for t in node.targets):
            return ast.literal_eval(node.value)
    raise SystemExit(f"No __version__ in {PACKAGE}/__init__.py")


def normalize_name(name):
    return re.sub(r"[-_.]+", "-", name).lower()


def same_version(left, right):
    try:
        from packaging.version import Version
    except ImportError:
        return left == right
    try:
        return Version(left) == Version(right)
    except Exception:
        return left == right


def repository_files(root=ROOT):
    """Files of the package that a wheel must contain, as ``fleetops/...`` paths."""
    package = root / PACKAGE
    try:
        listed = subprocess.run(
            ["git", "ls-files", "-z", "--", PACKAGE],
            cwd=root,
            capture_output=True,
            check=True,
        ).stdout.decode("utf-8")
        paths = [Path(name) for name in listed.split("\0") if name]
    except (OSError, subprocess.CalledProcessError):
        # Not a git checkout (an unpacked sdist): use what is on disk.
        paths = [p.relative_to(root) for p in package.rglob("*") if p.is_file()]
    wanted = set()
    for path in paths:
        parts = path.parts
        if path.name in IGNORED_NAMES or "__pycache__" in parts or not (root / path).is_file():
            continue
        if path.suffix == ".py" or (len(parts) > 2 and parts[1] in DATA_FOLDERS):
            wanted.add(path.as_posix())
    return wanted


def check_wheel(wheel, wanted, version):
    """Problems found in one wheel file."""
    problems = []
    name = wheel.name
    with zipfile.ZipFile(wheel) as archive:
        members = set(archive.namelist())
        dist_info = sorted({m.split("/", 1)[0] for m in members if m.split("/", 1)[0].endswith(".dist-info")})
        metadata = archive.read(f"{dist_info[0]}/METADATA").decode("utf-8") if dist_info else ""
    if not same_version(name.split("-")[1] if name.count("-") >= 2 else "", version):
        problems.append(f"{name}: the file name does not carry version {version}")
    missing = sorted(wanted - members)
    if missing:
        shown = ", ".join(missing[:10]) + (f" and {len(missing) - 10} more" if len(missing) > 10 else "")
        problems.append(f"{name}: {len(missing)} repository file(s) missing from the wheel: {shown}")
    for member in sorted(members):
        top = member.split("/", 1)[0]
        if top in FORBIDDEN_TOP_LEVEL:
            problems.append(f"{name}: contains {member}")
            break
    cached = sorted(m for m in members if "__pycache__" in m or m.endswith((".pyc", ".pyo")))
    if cached:
        problems.append(f"{name}: contains compiled files such as {cached[0]}")
    if not dist_info:
        problems.append(f"{name}: no .dist-info folder")
        return problems
    problems += check_metadata(name, metadata, version)
    if not any(m.startswith(f"{dist_info[0]}/") and m.rsplit("/", 1)[-1] == LICENSE_FILE for m in members):
        problems.append(f"{name}: the {LICENSE_FILE} file is not in {dist_info[0]}")
    return problems


def check_metadata(name, text, version):
    problems = []
    message = email.parser.Parser().parsestr(text)
    if normalize_name(message.get("Name", "")) != DISTRIBUTION:
        problems.append(f"{name}: metadata Name is {message.get('Name')!r}, expected {DISTRIBUTION!r}")
    if not same_version(message.get("Version", ""), version):
        problems.append(f"{name}: metadata Version is {message.get('Version')!r}, expected {version!r}")
    if message.get("License-Expression") != LICENSE_EXPRESSION:
        problems.append(
            f"{name}: metadata License-Expression is {message.get('License-Expression')!r}, "
            f"expected {LICENSE_EXPRESSION!r}"
        )
    if LICENSE_FILE not in (message.get_all("License-File") or []):
        problems.append(f"{name}: metadata has no 'License-File: {LICENSE_FILE}'")
    if any(c.startswith("License ::") for c in message.get_all("Classifier") or []):
        problems.append(f"{name}: a 'License ::' classifier next to the licence expression")
    for header in ("Author-email", "Maintainer-email"):
        if message.get(header):
            problems.append(f"{name}: metadata has an {header} header")
    for header in ("Author", "Maintainer"):
        if "@" in (message.get(header) or ""):
            problems.append(f"{name}: metadata {header} holds an e-mail address")
    return problems


def check_sdist(sdist, wanted, version):
    """Problems found in one source distribution."""
    problems = []
    name = sdist.name
    with tarfile.open(sdist, "r:gz") as archive:
        members = [m.name for m in archive.getmembers()]
    roots = {m.split("/", 1)[0] for m in members}
    if len(roots) != 1:
        return [f"{name}: expected one top-level folder, found {sorted(roots)}"]
    root = next(iter(roots))
    if not same_version(root.rsplit("-", 1)[-1], version):
        problems.append(f"{name}: the top-level folder {root} does not carry version {version}")
    present = {m.split("/", 1)[1] for m in members if "/" in m}
    for required in SDIST_FILES + ("PKG-INFO",):
        if required not in present:
            problems.append(f"{name}: {required} is missing")
    missing = sorted(wanted - present)
    if missing:
        shown = ", ".join(missing[:10]) + (f" and {len(missing) - 10} more" if len(missing) > 10 else "")
        problems.append(f"{name}: {len(missing)} repository file(s) missing from the sdist: {shown}")
    if not any(p.startswith("docs/") for p in present):
        problems.append(f"{name}: the docs folder is missing")
    tests = sorted(p for p in present if p.startswith("tests/"))
    if tests:
        problems.append(
            f"{name}: {len(tests)} test file(s) in the sdist, for example {tests[0]}; "
            "they cannot run without the test project, so MANIFEST.in has to prune them"
        )
    return problems


def check_directory(dist_dir, root=ROOT):
    """All problems of the distributions in ``dist_dir``; also the list of files that were checked."""
    version = source_version(root)
    wanted = repository_files(root)
    wheels = sorted(dist_dir.glob("*.whl"))
    sdists = sorted(dist_dir.glob("*.tar.gz"))
    problems = []
    if not wheels:
        problems.append(f"No wheel in {dist_dir}")
    if not sdists:
        problems.append(f"No source distribution in {dist_dir}")
    for wheel in wheels:
        problems += check_wheel(wheel, wanted, version)
    for sdist in sdists:
        problems += check_sdist(sdist, wanted, version)
    return problems, wheels + sdists


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("dist_dir", nargs="?", default="dist", help="folder with the built files (default: dist)")
    parser.add_argument("--root", default=str(ROOT), help="repository checkout the files are compared with")
    parser.add_argument("--print-version", action="store_true", help="print the version of the checkout and stop")
    args = parser.parse_args(argv)
    if args.print_version:
        print(source_version(Path(args.root)))
        return 0
    problems, checked = check_directory(Path(args.dist_dir), Path(args.root))
    for problem in problems:
        print(f"error: {problem}", file=sys.stderr)
    if problems:
        return 1
    for path in checked:
        print(f"ok: {path.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
