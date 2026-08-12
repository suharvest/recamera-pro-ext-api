#!/usr/bin/env python3
"""
build.py -- pack an app directory into a distributable app package.

    <id>-<ver>-arm64.tar.gz   (manifest.json + app.py + models/ ; NO kit)

The shared `kit` runtime is deployed once to the device (see appmgr) and is
NOT bundled per app (docs/guide/kit-design.md §0.6): an app package is just the
model + thin app.py + manifest, kept a few hundred KB.

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

# Files/dirs that belong in an app package. Anything else in the app dir
# (kit/, __pycache__, logs/, run.pid, .DS_Store, hidden files) is excluded.
INCLUDE_TOP = ("manifest.json", "app.py", "models", "hooks", "run")
ID_RE_HINT = "[a-z0-9-]{1,64}"


def _is_valid_id(app_id: str) -> bool:
    import re
    return re.fullmatch(r"[a-z0-9-]{1,64}", app_id or "") is not None


def _members(app_dir: str):
    """Yield (abspath, arcname) for each file to pack, arcname relative to app_dir."""
    for top in INCLUDE_TOP:
        p = os.path.join(app_dir, top)
        if not os.path.exists(p):
            continue
        if os.path.isfile(p):
            yield p, top
        else:
            for root, dirs, files in os.walk(p):
                # skip caches / hidden dirs
                dirs[:] = [d for d in dirs if d != "__pycache__" and not d.startswith(".")]
                for f in files:
                    if f.startswith(".") or f.endswith(".pyc"):
                        continue
                    ap = os.path.join(root, f)
                    arc = os.path.relpath(ap, app_dir)
                    yield ap, arc


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

    members = sorted(_members(app_dir), key=lambda m: m[1])
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
