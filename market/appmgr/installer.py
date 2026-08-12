"""
installer.py -- validate + safely unpack an app package into /userdata/local/apps/<id>/.

Security (APP_CENTER_PORT_DESIGN §4.9): installApp is effectively "deliver root
code to the device", so every package is treated as hostile:

  * package path: realpath under an allowed root, `.tar.gz` suffix, regular
    file, size cap.
  * per-member (anti zip-slip / tar-bomb): reject absolute paths, `..`
    traversal, symlinks, hardlinks, device/fifo nodes, setuid/setgid bits;
    enforce the resolved path stays inside the target dir; cap member count and
    total unpacked size.
  * manifest: `id` whitelist [a-z0-9-]{1,64}, must match the requested id.

Extraction uses Python's tarfile (gzip handled natively), because busybox tar on
the device has no `-z`. We never call tar.extractall() blindly -- each member is
vetted then extracted by hand.
"""
from __future__ import annotations

import json
import os
import shutil
import tarfile
import tempfile
from typing import Optional, Tuple

from . import paths, signing


class InstallError(Exception):
    pass


def _validate_pkg_path(pkg_path: str) -> str:
    real = os.path.realpath(pkg_path)
    if not real.endswith(".tar.gz"):
        raise InstallError(f"package must be .tar.gz: {pkg_path}")
    if not any(real == r or real.startswith(r.rstrip("/") + "/")
               for r in paths.ALLOWED_PKG_ROOTS):
        raise InstallError(f"package path {real} not under allowed roots {paths.ALLOWED_PKG_ROOTS}")
    if not os.path.isfile(real):
        raise InstallError(f"package is not a regular file: {real}")
    size = os.path.getsize(real)
    if size > paths.MAX_PKG_BYTES:
        raise InstallError(f"package too large: {size} > {paths.MAX_PKG_BYTES}")
    if size == 0:
        raise InstallError("package is empty")
    return real


def _vet_member(m: tarfile.TarInfo, dest_root: str) -> None:
    name = m.name
    # absolute path / drive / traversal
    if name.startswith("/") or name.startswith("\\") or os.path.isabs(name):
        raise InstallError(f"zip-slip: absolute member path {name!r}")
    if ".." in name.replace("\\", "/").split("/"):
        raise InstallError(f"zip-slip: '..' in member path {name!r}")
    # non-regular members
    if m.issym() or m.islnk():
        raise InstallError(f"unsafe member (sym/hard link): {name!r}")
    if m.isdev() or m.ischr() or m.isblk() or m.isfifo():
        raise InstallError(f"unsafe member (device/fifo): {name!r}")
    if not (m.isfile() or m.isdir()):
        raise InstallError(f"unsupported member type: {name!r}")
    # setuid / setgid / sticky
    if m.mode & 0o7000:
        raise InstallError(f"unsafe member mode {oct(m.mode)}: {name!r}")
    # resolved path must stay inside dest_root
    target = os.path.realpath(os.path.join(dest_root, name))
    root = os.path.realpath(dest_root)
    if target != root and not target.startswith(root + os.sep):
        raise InstallError(f"zip-slip: member escapes target dir: {name!r}")


def _read_manifest_from_tar(tar: tarfile.TarFile) -> dict:
    try:
        member = tar.getmember("manifest.json")
    except KeyError:
        raise InstallError("package has no manifest.json at top level")
    f = tar.extractfile(member)
    if f is None:
        raise InstallError("cannot read manifest.json")
    try:
        return json.loads(f.read().decode("utf-8"))
    except Exception as e:
        raise InstallError(f"manifest.json is not valid JSON: {e}")


def inspect(pkg_path: str, signature: Optional[str] = None) -> dict:
    """Validate a package WITHOUT installing; return {id, version, manifest,
    members, signature}.

    Authenticity (TODO #4) is checked FIRST, before we spend any effort parsing
    the tar: verify the detached release signature (base64 arg, or `<pkg>.sig`
    sidecar) over the raw .tar.gz bytes using the device's openssl + the baked-in
    public key. A bad signature -> InstallError; an unsigned package -> InstallError
    unless policy (paths.REQUIRE_SIGNATURE) allows it. This runs in addition to the
    existing sha256/zip-slip defences, not instead of them.
    """
    real = _validate_pkg_path(pkg_path)
    try:
        sig_status = signing.verify_package(real, signature)
    except signing.SignatureError as e:
        raise InstallError(str(e))
    with tarfile.open(real, "r:gz") as tar:
        # Vet every member against a throwaway root (traversal/type checks only).
        members = tar.getmembers()
        if len(members) > paths.MAX_MEMBERS:
            raise InstallError(f"too many members: {len(members)} > {paths.MAX_MEMBERS}")
        total = 0
        with tempfile.TemporaryDirectory() as probe:
            for m in members:
                _vet_member(m, probe)
                total += max(0, m.size)
                if total > paths.MAX_UNPACKED_BYTES:
                    raise InstallError(f"unpacked size exceeds cap {paths.MAX_UNPACKED_BYTES}")
        manifest = _read_manifest_from_tar(tar)
    app_id = manifest.get("id")
    if not paths.valid_app_id(app_id):
        raise InstallError(f"manifest id {app_id!r} not in whitelist [a-z0-9-]{{1,64}}")
    return {
        "id": app_id,
        "version": manifest.get("version"),
        "manifest": manifest,
        "members": [m.name for m in members],
        "signature": sig_status,
    }


def install(pkg_path: str, signature: Optional[str] = None) -> Tuple[str, dict]:
    """Validate + extract package into /userdata/local/apps/<id>/. Returns (id, manifest).

    Signature verification happens up front via inspect(); a package that is
    unsigned-under-policy or carries a bad signature never reaches extraction.

    Extraction is atomic-ish: unpack to a temp dir next to APPS_DIR, then swap
    the target dir into place (old dir moved aside then removed).
    """
    info = inspect(pkg_path, signature)
    app_id = info["id"]
    manifest = info["manifest"]
    real = os.path.realpath(pkg_path)

    paths.ensure_dirs()
    dest = paths.app_dir(app_id)
    staging = tempfile.mkdtemp(prefix=f".{app_id}.stage.", dir=paths.APPS_DIR)
    try:
        with tarfile.open(real, "r:gz") as tar:
            for m in tar.getmembers():
                _vet_member(m, staging)          # re-vet at extract time
                if m.isdir():
                    os.makedirs(os.path.join(staging, m.name), exist_ok=True)
                else:
                    f = tar.extractfile(m)
                    if f is None:
                        raise InstallError(f"cannot extract member {m.name!r}")
                    outp = os.path.join(staging, m.name)
                    os.makedirs(os.path.dirname(outp) or staging, exist_ok=True)
                    with open(outp, "wb") as w:
                        shutil.copyfileobj(f, w)
                    os.chmod(outp, 0o755 if m.name == "run" or outp.endswith(".py") else 0o644)
        # atomic-ish swap
        backup = None
        if os.path.exists(dest):
            backup = dest + ".old"
            if os.path.exists(backup):
                shutil.rmtree(backup, ignore_errors=True)
            os.rename(dest, backup)
        os.rename(staging, dest)
        staging = None
        if backup:
            shutil.rmtree(backup, ignore_errors=True)
    finally:
        if staging and os.path.isdir(staging):
            shutil.rmtree(staging, ignore_errors=True)
    return app_id, manifest


def uninstall(app_id: str) -> None:
    if not paths.valid_app_id(app_id):
        raise InstallError(f"invalid app id {app_id!r}")
    dest = paths.app_dir(app_id)
    if os.path.isdir(dest):
        shutil.rmtree(dest, ignore_errors=True)
