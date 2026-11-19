"""Versioned data releases: the zone tables the app serves, published to a store and switched on by a pointer.

Layout in the data store:

    releases/<version>/levels/<level>.parquet   one feature table per zone level, built by prepare_data.py
    releases/<version>/manifest.json            what's in the release: levels, labels, row counts, checksums
    current.json                                {"version": ...}: the release the app serves

A release is never changed after publishing. Going live, or rolling back, only rewrites current.json, and running
apps pick the change up within minutes. The manifest is uploaded last, so a release without one is incomplete and
ignored.

Usage (the store is DATA_STORE_URL unless --store is given):

    uv run release.py prepare                  write a release under build/release/, to upload by hand
    uv run release.py publish [--promote]      upload data/levels as a new release
    uv run release.py promote <version>        make a release live, or roll back to an older one
    uv run release.py list                     releases in the store, newest first, marking the live one
    uv run release.py copy <version> --to <url> [--promote]   copy a release to another store, e.g. test -> prod
"""
import argparse
import hashlib
import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

import pyarrow.parquet as pq

from store import open_store

HERE = Path(__file__).parent
SOURCE = HERE / "data" / "levels"  # where prepare_data.py writes the tables
BUILD = HERE / "build" / "release"

# Display order and labels of the zone levels, finest first. Levels not listed here are added after, labelled by key.
LEVEL_LABELS = {"h3_8": "H3 8", "h3_7": "H3 7", "msoa": "MSOA", "boroughs": "Boroughs"}
BASE_LEVEL = "h3_8"  # the level the app opens on


def sha256(path):
    """Hex SHA-256 of a file."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def build_manifest(src, version=None):
    """Describe the tables in a folder of ``<level>.parquet`` files.

    Args:
        src: Folder holding one parquet file per zone level.
        version: Release version to record. By default, the UTC time plus a fingerprint of the files, e.g.
            ``20261112-153012-3fa9c1``: sortable, and two different releases never share a version even when
            published in the same second.

    Returns:
        Manifest dict: ``version``, ``created``, ``base_level`` and ``levels``, a list in display order of
        ``{key, label, file, rows, sha256}``.

    Raises:
        FileNotFoundError: If the folder holds no parquet files.
    """
    files = {p.stem: p for p in Path(src).glob("*.parquet")}
    if not files:
        raise FileNotFoundError(f"No .parquet tables in {src}: run uv run prepare_data.py first")
    order = [k for k in LEVEL_LABELS if k in files] + sorted(k for k in files if k not in LEVEL_LABELS)
    levels = [{"key": k, "label": LEVEL_LABELS.get(k, k), "file": f"levels/{k}.parquet",
               "rows": pq.ParquetFile(files[k]).metadata.num_rows,
               "sha256": sha256(files[k])} for k in order]
    now = datetime.now(timezone.utc)
    if version is None:
        fingerprint = hashlib.sha256("".join(lv["key"] + lv["sha256"] for lv in levels).encode()).hexdigest()[:6]
        version = f"{now:%Y%m%d-%H%M%S}-{fingerprint}"
    return {"version": version, "created": now.isoformat(timespec="seconds"),
            "base_level": BASE_LEVEL if BASE_LEVEL in files else order[0], "levels": levels}


def prepare(src=SOURCE, out=BUILD):
    """Lay out a new release in a local folder, exactly as it will sit in the store, for uploading by hand.

    Args:
        src: Folder of level tables.
        out: Output folder. Emptied first.

    Returns:
        The new release's version.
    """
    manifest = build_manifest(src)
    version = manifest["version"]
    shutil.rmtree(out, ignore_errors=True)
    folder = out / "releases" / version
    (folder / "levels").mkdir(parents=True)
    for level in manifest["levels"]:
        shutil.copyfile(Path(src) / f"{level['key']}.parquet", folder / level["file"])
    (folder / "manifest.json").write_text(json.dumps(manifest, indent=2))
    (out / "current.json").write_text(json.dumps({"version": version}, indent=2))
    return version


def publish(store, src=SOURCE, promote_it=False):
    """Upload a folder of level tables as a new release.

    Args:
        store: Data store.
        src: Folder of level tables.
        promote_it: Also make the new release live.

    Returns:
        The new release's version.

    Raises:
        FileExistsError: If a release with that version is already in the store (releases never change).
    """
    manifest = build_manifest(src)
    version = manifest["version"]
    if store.exists(f"releases/{version}/manifest.json"):
        raise FileExistsError(f"Release {version} already exists")
    for level in manifest["levels"]:
        store.upload(f"releases/{version}/{level['file']}", Path(src) / f"{level['key']}.parquet")
    store.write(f"releases/{version}/manifest.json", json.dumps(manifest, indent=2).encode())  # last: marks it complete
    if promote_it:
        promote(store, version)
    return version


def manifest_of(store, version):
    """A release's manifest. Raises FileNotFoundError if the release doesn't exist or is incomplete."""
    return json.loads(store.read(f"releases/{version}/manifest.json"))


def promote(store, version):
    """Make a published release the live one. Raises FileNotFoundError if it doesn't exist."""
    manifest_of(store, version)
    pointer = {"version": version, "promoted": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    store.write("current.json", json.dumps(pointer, indent=2).encode())


def current_version(store):
    """Version of the live release, or None if nothing has been promoted yet."""
    try:
        return json.loads(store.read("current.json"))["version"]
    except FileNotFoundError:
        return None


def list_releases(store):
    """Versions of the complete releases in a store, newest first."""
    return sorted({k.split("/")[1] for k in store.list("releases/") if k.endswith("/manifest.json")}, reverse=True)


def copy(src_store, version, dst_store, promote_it=False):
    """Copy a release to another store, e.g. from test to prod, keeping its version.

    Args:
        src_store: Store holding the release.
        version: Release version.
        dst_store: Store to copy it to.
        promote_it: Also make it live in the destination.
    """
    manifest = manifest_of(src_store, version)
    if dst_store.exists(f"releases/{version}/manifest.json"):
        raise FileExistsError(f"Release {version} is already in {dst_store.url}")
    for level in manifest["levels"]:
        key = f"releases/{version}/{level['file']}"
        dst_store.write(key, src_store.read(key))
    dst_store.write(f"releases/{version}/manifest.json", json.dumps(manifest, indent=2).encode())
    if promote_it:
        promote(dst_store, version)


def fetch(store, cache, version=None):
    """Download a release into a local cache folder, once, checking every file's checksum.

    Args:
        store: Data store.
        cache: Local cache folder; the release lands in ``cache/<version>/``.
        version: Release to fetch; the live one if None.

    Returns:
        ``(folder, manifest)``, or ``(None, None)`` if no release is live.

    Raises:
        ValueError: If a downloaded file doesn't match its checksum.
    """
    version = version or current_version(store)
    if version is None:
        return None, None
    folder = Path(cache) / version
    if (folder / "manifest.json").exists():  # already cached: releases never change
        return folder, json.loads((folder / "manifest.json").read_text())
    manifest = manifest_of(store, version)
    tmp = Path(cache) / f".{version}.partial"
    shutil.rmtree(tmp, ignore_errors=True)
    (tmp / "levels").mkdir(parents=True)
    for level in manifest["levels"]:
        dest = tmp / level["file"]
        store.download(f"releases/{version}/{level['file']}", dest)
        if sha256(dest) != level["sha256"]:
            shutil.rmtree(tmp)
            raise ValueError(f"Checksum mismatch for {level['file']} in release {version}")
    (tmp / "manifest.json").write_text(json.dumps(manifest, indent=2))
    try:
        tmp.rename(folder)  # whole release appears at once
    except OSError:  # another worker cached it first
        shutil.rmtree(tmp, ignore_errors=True)
    return folder, manifest


def local(src=SOURCE):
    """The tables in data/levels as an unversioned release, for running without a data store.

    Returns:
        ``(folder, manifest)`` with version ``"local"``. The folder is the parent of ``src``, so the manifest's
        ``levels/<level>.parquet`` paths resolve.
    """
    return Path(src).parent, build_manifest(src, "local")


def main(argv=None):
    """Command-line entry point; see the module docstring."""
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--store", default=os.environ.get("DATA_STORE_URL"),
                    help="Data store URL or folder (default: $DATA_STORE_URL)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("prepare", help="write a release under build/release/ to upload by hand")
    p = sub.add_parser("publish", help="upload data/levels as a new release")
    p.add_argument("--promote", action="store_true", help="also make it live")
    p = sub.add_parser("promote", help="make a release live, or roll back")
    p.add_argument("version")
    sub.add_parser("list", help="releases in the store, newest first")
    p = sub.add_parser("copy", help="copy a release to another store")
    p.add_argument("version")
    p.add_argument("--to", required=True, help="destination store URL or folder")
    p.add_argument("--promote", action="store_true", help="also make it live there")
    args = ap.parse_args(argv)

    if args.cmd == "prepare":
        version = prepare()
        print(f"Release {version} written to {BUILD}. Upload its contents to the data container, keeping the paths.")
        return
    if not args.store:
        sys.exit("No store: set DATA_STORE_URL or pass --store")
    store = open_store(args.store)
    if args.cmd == "publish":
        version = publish(store, promote_it=args.promote)
        print(f"Published {version}" + (" and made it live" if args.promote else f"; make it live with: promote {version}"))
    elif args.cmd == "promote":
        promote(store, args.version)
        print(f"{args.version} is live")
    elif args.cmd == "list":
        live = current_version(store)
        for v in list_releases(store):
            print(v + ("  (live)" if v == live else ""))
    elif args.cmd == "copy":
        copy(store, args.version, open_store(args.to), promote_it=args.promote)
        print(f"Copied {args.version} to {args.to}" + (" and made it live" if args.promote else ""))


if __name__ == "__main__":
    main()
