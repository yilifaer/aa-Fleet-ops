#!/usr/bin/env python
# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Keep copies of the theme and library files Alliance Auth loads from a CDN, verified against its own hashes.

Usage: python tools/ui_check/fetch_assets.py [--cache DIR] [--all] [--force]

Alliance Auth pins every theme file (Bootstrap, Bootswatch themes, Popper, jQuery, Font Awesome ...) with a
Subresource Integrity hash. This tool reads those URLs and hashes from the installed Alliance Auth, downloads
each file once and keeps it in ``tools/ui_check/.cache``. The browser run answers the CDN requests from there,
so a run needs no network, and a file whose hash no longer matches stops the run.

When the CDN cannot be reached, the same file is taken from the npm package it is built from (the files are
identical, which the hash proves). Font files have no hash in Alliance Auth; they come from the npm package,
whose own integrity value is checked.

Run it with the Python of the Alliance Auth environment you test with; the cache holds the files of every
version it has seen. Needs the network once (it honours HTTPS_PROXY and the usual CA settings).
"""

import argparse
import base64
import hashlib
import importlib.util
import io
import json
import re
import sys
import tarfile
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

HERE = Path(__file__).resolve().parent
DEFAULT_CACHE = HERE / ".cache"
CDN_HOST = "cdnjs.cloudflare.com"
NPM_REGISTRY = "https://registry.npmjs.org"
TIMEOUT = 40

# Alliance Auth bundles the interface always needs; the others are fetched with --all.
CORE_BUNDLES = ("fontawesome", "jquery-js", "bootstrap-js-bs5", "moment-js")
# npm package and the folder of the built files for each cdnjs library name.
NPM_PACKAGES = {
    "bootswatch": ("bootswatch", "package/dist/{path}"),
    "bootstrap": ("bootstrap", "package/dist/{path}"),
    "popper.js": ("@popperjs/core", "package/dist/{path}"),
    "jquery": ("jquery", "package/dist/{path}"),
    "font-awesome": ("@fortawesome/fontawesome-free", "package/{path}"),
    "moment.js": ("moment", "package/min/{path}"),
}
FONT_AWESOME_FONTS = ("fa-brands-400.woff2", "fa-regular-400.woff2", "fa-solid-900.woff2", "fa-v4compatibility.woff2")
CONTENT_TYPES = {
    ".css": "text/css",
    ".js": "application/javascript",
    ".woff2": "font/woff2",
    ".woff": "font/woff",
    ".ttf": "font/ttf",
}

PAIR = re.compile(r"""["']url["']\s*:\s*["'](https://[^"']+)["']\s*,\s*["']integrity["']\s*:\s*["'](sha\d+-[^"']+)["']""")
TAG = re.compile(r"""<(?:script|link)\b[^>]*?(?:src|href)=["'](https://[^"']+)["'][^>]*?integrity=["'](sha\d+-[^"']+)["']""", re.S)


class AssetError(Exception):
    pass


class HashMismatch(AssetError):
    """The CDN served a file that does not fit the hash Alliance Auth pins: never use it, stop the run."""


def alliance_auth_dir():
    spec = importlib.util.find_spec("allianceauth")
    if spec is None or not spec.submodule_search_locations:
        raise AssetError("Alliance Auth is not installed in this Python")
    return Path(next(iter(spec.submodule_search_locations)))


def pinned_urls(include_all=False):
    """``{url: integrity}`` of the files the installed Alliance Auth loads from the CDN."""
    root = alliance_auth_dir()
    found = {}
    for path in sorted((root / "theme").glob("*/auth_hooks.py")):
        for url, integrity in PAIR.findall(path.read_text(encoding="utf-8")):
            found[url] = integrity
    bundles = root / "templates" / "bundles"
    for path in sorted(bundles.glob("*.html")):
        if not include_all and path.stem not in CORE_BUNDLES:
            continue
        for url, integrity in TAG.findall(path.read_text(encoding="utf-8")):
            found[url] = integrity
    return found


def sri(data, algorithm="sha512"):
    return f"{algorithm}-" + base64.b64encode(hashlib.new(algorithm, data).digest()).decode()


def matches(data, integrity):
    """True when the data fits one of the space separated hashes."""
    for item in integrity.split():
        algorithm = item.split("-", 1)[0]
        if algorithm in hashlib.algorithms_available and sri(data, algorithm) == item:
            return True
    return False


def fetch(url):
    request = urllib.request.Request(url, headers={"User-Agent": "fleetops-ui-check"})
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        return response.read()


def library_of(url):
    """``(library, version, path)`` of a cdnjs URL, or None."""
    parts = urlsplit(url)
    match = re.match(r"^/ajax/libs/([^/]+)/([^/]+)/(.+)$", parts.path)
    return match.groups() if parts.netloc == CDN_HOST and match else None


_tarballs = {}


def npm_file(package, version, member):
    """A file from the npm package tarball, with the package's own integrity value checked."""
    key = (package, version)
    if key not in _tarballs:
        meta = json.loads(fetch(f"{NPM_REGISTRY}/{package.replace('/', '%2F')}/{version}"))
        data = fetch(meta["dist"]["tarball"])
        integrity = meta["dist"].get("integrity", "")
        if not integrity or not matches(data, integrity):
            raise AssetError(f"npm package {package}@{version} does not match its integrity value")
        _tarballs[key] = tarfile.open(fileobj=io.BytesIO(data), mode="r:gz")
    archive = _tarballs[key]
    names = archive.getnames()
    wanted = member if member in names else None
    if wanted is None:
        base = member.rsplit("/", 1)[-1]
        candidates = sorted((n for n in names if n.rsplit("/", 1)[-1] == base), key=len)
        wanted = candidates[0] if candidates else None
    if wanted is None:
        raise AssetError(f"{member} is not in npm package {package}@{version}")
    handle = archive.extractfile(wanted)
    return handle.read() if handle else b""


def from_npm(url):
    parsed = library_of(url)
    if not parsed:
        raise AssetError(f"no npm package known for {url}")
    library, version, path = parsed
    package, layout = NPM_PACKAGES.get(library, (library.replace(".js", ""), "package/{path}"))
    return npm_file(package, version, layout.format(path=path))


def obtain(url, integrity):
    """The bytes of ``url`` (verified), where they came from and how they were verified."""
    errors = []
    try:
        data = fetch(url)
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        errors.append(f"{CDN_HOST}: {exc}")
    else:
        if integrity and not matches(data, integrity):
            raise HashMismatch(f"{url} does not match the hash Alliance Auth pins; stopped")
        return data, "cdn", "sri" if integrity else "download"
    try:
        data = from_npm(url)
    except (urllib.error.URLError, OSError, TimeoutError, AssetError, KeyError, json.JSONDecodeError) as exc:
        errors.append(f"npm: {exc}")
        raise AssetError(f"cannot get {url} ({'; '.join(errors)})")
    if integrity and not matches(data, integrity):
        # The package builds the file differently from the CDN copy; without the CDN there is nothing to verify.
        raise AssetError(f"{url}: the npm copy differs from the hash Alliance Auth pins")
    return data, "npm", "sri" if integrity else "npm-integrity"


def load_manifest(cache):
    path = cache / "manifest.json"
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {}


def store(cache, manifest, url, data, source, verified):
    digest = hashlib.sha256(data).hexdigest()
    suffix = Path(urlsplit(url).path).suffix
    name = f"{digest[:16]}{suffix}"
    (cache / "files").mkdir(parents=True, exist_ok=True)
    (cache / "files" / name).write_bytes(data)
    manifest[url] = {
        "file": name, "sha256": digest, "bytes": len(data), "source": source, "verified": verified,
        "type": CONTENT_TYPES.get(suffix.lower(), "application/octet-stream"),
    }


def cached_ok(cache, manifest, url, integrity):
    entry = manifest.get(url)
    if not entry:
        return False
    path = cache / "files" / entry["file"]
    if not path.exists():
        return False
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != entry["sha256"]:
        return False
    return not integrity or matches(data, integrity)


def fetch_all(cache, include_all=False, force=False, log=print):
    """Fill the cache; returns the list of URLs that could not be obtained (optional files only)."""
    cache.mkdir(parents=True, exist_ok=True)
    manifest = load_manifest(cache)
    wanted = pinned_urls(include_all)
    # The Font Awesome style sheet names its font files relative to itself; the page asks for them next.
    for url in list(wanted):
        if "/font-awesome/" in url and url.endswith("/css/all.min.css"):
            base = url.rsplit("/css/", 1)[0]
            for font in FONT_AWESOME_FONTS:
                wanted.setdefault(f"{base}/webfonts/{font}", "")
    missing = []
    for url, integrity in sorted(wanted.items()):
        if not force and cached_ok(cache, manifest, url, integrity):
            log(f"cached   {url}")
            continue
        try:
            data, source, verified = obtain(url, integrity)
        except HashMismatch:
            raise
        except AssetError as exc:
            missing.append(url)
            log(f"MISSING  {url}  ({exc})")
            continue
        store(cache, manifest, url, data, source, verified)
        log(f"fetched  {url}  [{source}, {verified}]")
    (cache / "manifest.json").write_text(json.dumps(manifest, indent=1, sort_keys=True), encoding="utf-8")
    return missing


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--cache", default=str(DEFAULT_CACHE), help="cache folder (default: tools/ui_check/.cache)")
    parser.add_argument("--all", action="store_true", help="also the files only some Alliance Auth pages load")
    parser.add_argument("--force", action="store_true", help="download again even when the cache holds the file")
    args = parser.parse_args(argv)
    try:
        missing = fetch_all(Path(args.cache), include_all=args.all, force=args.force)
    except AssetError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    required = [u for u in missing if "/bootswatch/" in u or "/bootstrap/" in u or "/font-awesome/" in u or "/jquery/" in u]
    if required:
        print(f"error: {len(required)} required file(s) are missing", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
