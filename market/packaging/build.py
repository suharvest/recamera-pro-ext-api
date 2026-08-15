#!/usr/bin/env python3
"""
build.py -- pack an app directory into a distributable app package.

    <id>-<ver>-arm64.tar.gz   (the whole app dir tree, minus junk ; NO kit)

The shared `kit` runtime is deployed once to the device (see appmgr) and is
NOT bundled per app (docs/guide/kit-design.md §0.6): an app package is the app's
own files -- manifest + app.py + any sibling helper modules/templates/data +
models/ -- kept small (a `kit/` subdir, caches and build products are excluded).

Usage:
    python3 build.py apps/yolo-detector
    python3 build.py apps/yolo-detector --out dist/

Stdlib only (tarfile, gzip). Prints the package name, size and md5.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import os
import sys
import tarfile

# Packaging collects the WHOLE app directory tree, not a fixed whitelist.
#
# The launcher (kit/run.py:load_app_module) puts the app dir on sys.path so an
# app may `import` sibling helper modules, and the docs encourage organising an
# app across several files / a package + templates / data assets. A fixed
# whitelist (manifest/app.py/models/hooks/run/icon) silently DROPPED every such
# extra file: the app ran on the dev machine (all files present) but shipped a
# package missing them, failing only on a clean device. So we pack the entire
# tree and instead EXCLUDE a precise deny-list of junk / build products.
#
# Notable members that this naturally includes (no special-casing needed):
#   * `icon.<ext>` -- the app's card artwork (RENDER_DECLARATION_SPEC §5 P0-1):
#     appmgr's installer keeps it and serves GET /api/appMgr/icon?id=<id>, the
#     way a THIRD-PARTY app gets a card image without a front-end rebuild. The
#     installer still enforces raster-only at install time (SVG is refused).
#   * `models/` label files for `models[].classes`, and any sibling helper .py.

# Directory names pruned wholesale (never descended into).
EXCLUDE_DIRS = frozenset((
    "__pycache__", ".git", ".hg", ".svn", "kit",
    "build", "dist", "target", "node_modules",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox",
    ".venv", "venv", ".idea", ".vscode", ".DS_Store",
))
# File suffixes that are build products / editor cruft, never source.
EXCLUDE_SUFFIXES = (".pyc", ".pyo", ".pyd", ".log", ".swp", ".swo", ".orig")
# Exact file names to drop (runtime state a dev run leaves in the app dir).
EXCLUDE_FILES = frozenset(("run.pid", ".DS_Store"))

ID_RE_HINT = "[a-z0-9-]{1,64}"


def _is_valid_id(app_id: str) -> bool:
    import re
    return re.fullmatch(r"[a-z0-9-]{1,64}", app_id or "") is not None


def _match_any(name: str, patterns) -> bool:
    import fnmatch
    return any(fnmatch.fnmatch(name, p) for p in patterns)


def _skip_dir(name: str, rel: str, patterns) -> bool:
    return (name in EXCLUDE_DIRS or name.startswith(".")
            or _match_any(name, patterns) or _match_any(rel, patterns))


def _skip_file(name: str, rel: str, patterns) -> bool:
    # Hidden files (.DS_Store, ._AppleDouble, .gitignore) and build cruft, plus
    # any manifest-declared `package.exclude` glob (matched on basename or the
    # full app-relative path so `evaluation` and `docs/*.md` both work).
    return (name.startswith(".")
            or name in EXCLUDE_FILES
            or name.endswith(EXCLUDE_SUFFIXES)
            or _match_any(name, patterns) or _match_any(rel, patterns))


def _members(app_dir: str, exclude=()):
    """Yield (abspath, arcname) for every file to pack, arcname relative to
    app_dir. Walks the whole tree, pruning EXCLUDE_DIRS / hidden dirs and
    skipping junk / build-product files (see the deny-lists above); `exclude`
    adds app-specific globs declared in `manifest.package.exclude`."""
    patterns = tuple(exclude)
    for root, dirs, files in os.walk(app_dir):
        # Prune in place so os.walk never descends excluded/hidden dirs.
        kept = []
        for d in dirs:
            rel = os.path.relpath(os.path.join(root, d), app_dir)
            if not _skip_dir(d, rel, patterns):
                kept.append(d)
        dirs[:] = sorted(kept)
        for f in sorted(files):
            ap = os.path.join(root, f)
            rel = os.path.relpath(ap, app_dir)
            if _skip_file(f, rel, patterns):
                continue
            yield ap, rel


def build(app_dir: str, out_dir: str) -> str:
    app_dir = os.path.abspath(app_dir)
    manifest_path = os.path.join(app_dir, "manifest.json")
    if not os.path.isfile(manifest_path):
        sys.exit(f"error: no manifest.json in {app_dir}")
    with open(manifest_path) as f:
        manifest = json.load(f)

    app_id = manifest.get("id")
    version = manifest.get("version")
    if not _is_valid_id(app_id):
        sys.exit(f"error: manifest id {app_id!r} must match {ID_RE_HINT}")
    if not version:
        sys.exit("error: manifest has no version")

    os.makedirs(out_dir, exist_ok=True)
    pkg_name = f"{app_id}-{version}-arm64.tar.gz"
    pkg_path = os.path.join(out_dir, pkg_name)

    exclude = (manifest.get("package") or {}).get("exclude") or ()
    if not isinstance(exclude, (list, tuple)):
        sys.exit("error: manifest package.exclude must be a list of globs")
    members = sorted(_members(app_dir, exclude), key=lambda m: m[1])
    if not any(arc == "app.py" for _, arc in members):
        sys.exit("error: app.py missing from app dir")

    # Fully deterministic package: same input bytes -> same output bytes -> same
    # sha256, so the catalog checksum can never drift from the served package
    # (the "checksum mismatch" bug). Two independent sources of nondeterminism
    # have to be pinned:
    #   1. tar member metadata -- mtime/uid/gid/uname/gname/mode are host- and
    #      time-dependent; we reset them to fixed values below.
    #   2. the gzip wrapper -- tarfile.open("w:gz") stamps the CURRENT time (and
    #      the output filename) into the gzip header, so the SAME tar gzips to
    #      DIFFERENT bytes every run. We therefore build an uncompressed tar in
    #      memory and gzip it ourselves with mtime=0 and no embedded filename.
    def _reset(ti: tarfile.TarInfo) -> tarfile.TarInfo:
        ti.uid = ti.gid = 0
        ti.uname = ti.gname = ""
        ti.mtime = 0
        # Normalize mode: 0755 if any exec bit is set (hook/run scripts), else
        # 0644. Drops host umask noise while preserving executability.
        ti.mode = 0o755 if (ti.mode & 0o111) else 0o644
        return ti

    tar_buf = io.BytesIO()
    with tarfile.open(fileobj=tar_buf, mode="w", format=tarfile.GNU_FORMAT) as tar:
        for ap, arc in members:
            tar.add(ap, arcname=arc, filter=_reset)

    with open(pkg_path, "wb") as fout:
        # filename="" -> no FNAME field; mtime=0 -> fixed gzip timestamp.
        with gzip.GzipFile(filename="", mode="wb", fileobj=fout, mtime=0) as gz:
            gz.write(tar_buf.getvalue())

    data = open(pkg_path, "rb").read()
    md5 = hashlib.md5(data).hexdigest()
    print(f"built   {pkg_path}")
    print(f"members {len(members)}: " + ", ".join(a for _, a in members))
    print(f"size    {len(data)} bytes ({len(data)/1024:.1f} KiB)")
    print(f"md5     {md5}")
    return pkg_path


def main(argv=None):
    ap = argparse.ArgumentParser(description="Pack a reCamera Pro app into a tar.gz package")
    ap.add_argument("app_dir", help="path to apps/<id>/")
    ap.add_argument("--out", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "dist"),
                    help="output directory (default: packaging/dist)")
    args = ap.parse_args(argv)
    build(args.app_dir, args.out)


if __name__ == "__main__":
    main()
