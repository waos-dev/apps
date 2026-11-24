#!/usr/bin/env python3
"""Collects the latest package of each app catalog.toml lists, and writes the catalog's index.

For each app, its source's index says its latest version. A version newer than the one the
catalog lists is downloaded, checked against the SHA-256 its source lists, verified by
`waos package verify` as signed by the publisher catalog.toml pins, and copied to the bucket at
<id>/<version>/<file>, never to be changed. The catalog's index.json, listing each app's latest
collected version, is written last.

All of it is kept in the bucket's .well-known/waos/ folder, where WaOS reads a website that is a
source: the catalog is added to WaOS by its address alone, list.waos.app.

    collect.py --check        say whether anything is new (for the workflow), change nothing
    collect.py                collect what is new into R2
    collect.py --out DIR      a dry run: collect into DIR, as the bucket would hold it

A source that cannot be read, or does not list its app, is reported and skipped. A package that
fails a check is refused: the app keeps its current listing, and the run fails once the others are
done, so someone looks.

Python's standard library alone, so it runs wherever Python 3.11 does, with nothing to install.
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urljoin, urlparse
from urllib.request import Request, urlopen

BUCKET = "waos-list"
SERVED_AT = "https://list.waos.app/"
# Where a website keeps what it lists for WaOS (RFC 8615's well-known folder), after its address.
SITE_FOLDER = ".well-known/waos/"
INDEX = "index.json"
INDEX_CACHE = "public, max-age=300"
PACKAGE_CACHE = "public, max-age=31536000, immutable"

HEX_KEY = re.compile(r"[0-9a-f]{64}")
SEMVER = re.compile(
    r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?"
)
FILE_NAME = re.compile(r"[0-9A-Za-z_+-][0-9A-Za-z._+-]*")


class Skipped(Exception):
    """An app not looked at this time: its source has nothing to collect, or cannot be read."""


class Refused(Exception):
    """A package that failed a check, and is not collected."""


@dataclass(frozen=True)
class App:
    """An app as catalog.toml lists it."""

    id: str
    source: str
    publisher: str

    def __post_init__(self):
        if not HEX_KEY.fullmatch(self.publisher):
            raise ValueError(f"{self.id}: publisher is not 64 lowercase hex digits")


@dataclass(frozen=True)
class Offered:
    """An app's latest version, as its source's index lists it."""

    name: str
    version: str
    sha256: str
    url: str

    @property
    def file_name(self):
        return unquote(PurePosixPath(urlparse(self.url).path).name)


def main():
    arguments = parse_arguments()
    store = Folder(arguments.out / SITE_FOLDER) if arguments.out else R2(BUCKET, SERVED_AT, SITE_FOLDER)
    apps = read_catalog(arguments.catalog)
    listed = {program["id"]: program for program in read_index(store)}

    new = find_new_versions(apps, listed)
    kept = [listed[app.id] for app in apps if app.id in listed]
    changed = bool(new) or kept != list(listed.values())
    if arguments.check:
        tell_the_workflow(changed)
        return 0
    if not changed:
        print("Nothing new.")
        return 0

    collected, refused = collect_all(apps, new, store, arguments.waos)
    listings = [collected.get(app.id) or listed.get(app.id) for app in apps]
    listings = [listing for listing in listings if listing]
    if listings != list(listed.values()):
        write_index(store, listings)
    return 1 if refused else 0


def parse_arguments():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--catalog", type=Path, default=Path(__file__).with_name("catalog.toml"))
    parser.add_argument("--check", action="store_true", help="say whether anything is new, only")
    parser.add_argument("--out", type=Path, help="a dry run: collect into this folder, not R2")
    parser.add_argument("--waos", default="waos", help="the waos command (default: waos)")
    return parser.parse_args()


# The catalog and its index.


def read_catalog(path):
    with open(path, "rb") as file:
        apps = [App(**app) for app in tomllib.load(file)["app"]]
    ids = [app.id for app in apps]
    if len(ids) != len(set(ids)):
        raise ValueError(f"{path} lists an app twice")
    return apps


def read_index(store):
    """The programs the catalog's index lists now; none before its first run."""
    index = store.read(INDEX)
    return json.loads(index)["programs"] if index else []


def write_index(store, listings):
    with tempfile.TemporaryDirectory() as folder:
        index = Path(folder) / INDEX
        index.write_text(json.dumps({"programs": listings}, indent=2) + "\n")
        store.put(INDEX, index, "application/json", INDEX_CACHE)
    listed = ", ".join(f"{listing['id']} {listing['version']}" for listing in listings)
    print(f"Wrote {INDEX}: {listed or 'no apps'}.")


def tell_the_workflow(changed):
    print("Something is new." if changed else "Nothing new.")
    if "GITHUB_OUTPUT" in os.environ:
        with open(os.environ["GITHUB_OUTPUT"], "a") as output:
            output.write(f"changed={str(changed).lower()}\n")


# Each app's source.


def find_new_versions(apps, listed):
    """Each app whose source offers a version newer than the catalog lists, with that version."""
    new = {}
    for app in apps:
        current = listed.get(app.id, {}).get("version")
        try:
            offered = latest_offered(app)
        except Skipped as reason:
            print(f"{app.id}: skipped, {reason}")
            continue
        if current and precedence(offered.version) <= precedence(current):
            print(f"{app.id}: {current}, up to date")
        else:
            print(f"{app.id}: {offered.version} is new (listed: {current or 'none'})")
            new[app.id] = offered
    return new


def latest_offered(app):
    index_url = index_url_of(app.source)
    try:
        index = fetch(index_url)
    except (URLError, OSError) as error:
        raise Skipped(f"{index_url} could not be read: {error}")
    if index is None:
        raise Skipped(f"{app.source} has no index yet ({index_url} is not found)")
    try:
        entries = [entry for entry in json.loads(index)["programs"] if entry["id"] == app.id]
        if not entries:
            raise Skipped(f"{app.source} does not list it")
        latest = max(entries, key=lambda entry: precedence(entry["version"]))
        offered = Offered(
            latest["name"], latest["version"], latest["sha256"].lower(),
            urljoin(index_url, latest["package"]),
        )
    except (ValueError, KeyError, TypeError, AttributeError) as error:
        raise Skipped(f"{index_url} is not an index of programs: {error!r}")
    if not HEX_KEY.fullmatch(offered.sha256) or not FILE_NAME.fullmatch(offered.file_name):
        raise Skipped(f"{index_url} lists a package this cannot name: {latest}")
    return offered


def index_url_of(source):
    """Where a source's index is read, as WaOS reads it: in a GitHub repository's latest release,
    in a website's well-known folder, or in the folder an address with a path names."""
    if source.startswith("github.com/"):
        return f"https://{source}/releases/latest/download/{INDEX}"
    url = urlparse(source if "://" in source else f"https://{source}")
    on_this_machine = url.scheme == "http" and url.hostname in ("localhost", "127.0.0.1")
    if not url.hostname or not (url.scheme == "https" or on_this_machine):
        raise ValueError(f"{source} is neither github.com/<owner>/<repo> nor a website's address")
    path = url.path.strip("/")
    folder = f"{path}/" if path else SITE_FOLDER
    return f"{url.scheme}://{url.netloc}/{folder}{INDEX}"


def precedence(version):
    """A version's place in semver's order, which sorts as the version does."""
    major, minor, patch, pre_release = SEMVER.fullmatch(version).groups()
    release = (int(major), int(minor), int(patch))
    if pre_release is None:
        return release, (1,)
    identifiers = [(0, int(part), "") if part.isdigit() else (1, 0, part)
                   for part in pre_release.split(".")]
    return release, (0, *identifiers)


# Collecting a package.


def collect_all(apps, new, store, waos):
    """The catalog's listing of each new version collected, and the ids of those refused."""
    collected, refused = {}, []
    for app in apps:
        if app.id not in new:
            continue
        try:
            collected[app.id] = collect(app, new[app.id], store, waos)
            print(f"{app.id}: {new[app.id].version} collected")
        except Refused as reason:
            print(f"{app.id}: {new[app.id].version} refused, {reason}", file=sys.stderr)
            refused.append(app.id)
    return collected, refused


def collect(app, offered, store, waos):
    """Downloads, checks and copies one package; its listing in the catalog's index."""
    key = f"{app.id}/{offered.version}/{offered.file_name}"
    with tempfile.TemporaryDirectory() as folder:
        package = Path(folder) / offered.file_name
        download(offered.url, package)
        check_sha256(package, offered.sha256)
        check_signed(package, app, offered.version, waos)
        copy_once(store, key, package, offered.sha256)
    return {
        "id": app.id,
        "name": offered.name,
        "version": offered.version,
        "package": key,
        "sha256": offered.sha256,
    }


def download(url, file):
    try:
        package = fetch(url)
    except (URLError, OSError) as error:
        raise Refused(f"{url} could not be downloaded: {error}")
    if package is None:
        raise Refused(f"{url} is not found")
    file.write_bytes(package)


def check_sha256(file, expected):
    actual = sha256_of(file.read_bytes())
    if actual != expected:
        raise Refused(f"its SHA-256 is {actual}, its source lists {expected}")


def check_signed(file, app, version, waos):
    """Refuses a package unless WaOS opens it, signed by the app's publisher, as that version."""
    verify = [waos, "package", "verify", str(file), "--publisher", app.publisher]
    result = subprocess.run(verify, capture_output=True, text=True)
    if result.returncode != 0:
        raise Refused(result.stderr.strip() or f"waos package verify exited {result.returncode}")
    described = dict(line.split(None, 1) for line in result.stdout.splitlines() if line.strip())
    if (described.get("id"), described.get("version")) != (app.id, version):
        raise Refused(f"it is {described.get('id')} {described.get('version')}, not {app.id} {version}")


def copy_once(store, key, package, sha256):
    """Copies a package to the store, unless it is there already; never changes one that is."""
    existing = store.read(key)
    if existing is None:
        store.put(key, package, "application/octet-stream", PACKAGE_CACHE)
    elif sha256_of(existing) != sha256:
        raise Refused(f"{key} is already in the bucket with other bytes, which are never changed")


def sha256_of(data):
    return hashlib.sha256(data).hexdigest()


def fetch(url):
    """The body at `url`; None when it is not found."""
    request = Request(url, headers={"User-Agent": "waos-apps-catalog"})
    try:
        with urlopen(request, timeout=120) as response:
            return response.read()
    except HTTPError as error:
        if error.code == 404:
            return None
        raise


# Where the catalog is kept.


class R2:
    """A folder of the bucket, read where the bucket is served and written with wrangler."""

    def __init__(self, bucket, served_at, folder):
        self.bucket, self.served_at, self.folder = bucket, served_at, folder

    def read(self, key):
        return fetch(urljoin(self.served_at, self.folder + key))

    def put(self, key, file, content_type, cache_control):
        subprocess.run(
            ["npx", "--yes", "wrangler@4", "r2", "object", "put",
             f"{self.bucket}/{self.folder}{key}",
             "--file", str(file), "--content-type", content_type,
             "--cache-control", cache_control, "--remote"],
            check=True,
        )


class Folder:
    """A folder in place of the bucket, for a dry run."""

    def __init__(self, root):
        self.root = root

    def read(self, key):
        path = self.root / key
        return path.read_bytes() if path.exists() else None

    def put(self, key, file, content_type, cache_control):
        path = self.root / key
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(file, path)


if __name__ == "__main__":
    sys.exit(main())
