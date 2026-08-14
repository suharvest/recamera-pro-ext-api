"""
modelstore.py -- safely write a browser-relayed *shared model* file to a
whitelisted directory on the device.

Motivation (one-gen `models[]` + `target_path` parity, APP_CENTER_PORT_DESIGN
§4.9): some apps (e.g. voice-transcribe) do NOT bundle their model inside the
package -- the model is a large, SHARED asset dropped into a well-known device
directory (/userdata/local/models/asr) that several apps can reuse. For a clean
cloud install the browser must be able to push those bytes to a *directory*, not
just the appstage tar path. This module is that write primitive.

This is a NEW attack surface ("write a file to an absolute path the client
chose"), so it is treated as hostile and locked down hard:

  * DESTINATION ROOT WHITELIST: the target directory must resolve *inside* one
    of MODEL_ROOTS (default /userdata/local/models). Anything else -- /etc,
    /oem, /usr, an absolute path outside the roots, a `..` climb, or a symlink
    component that escapes after the dir is created -- is refused. Both a
    lexical (pre-create) check and a realpath (post-mkdir, symlink-resolving)
    check must pass.
  * FILENAME: a bare basename [A-Za-z0-9][A-Za-z0-9._-]{0,127}; no separators,
    no `..`, no leading dot-dot. Never a path.
  * SIZE CAP: 1..MAX_MODEL_BYTES, checked before touching disk.
  * NO SYMLINK CLOBBER: refuse if the destination already exists as a symlink.
  * ATOMIC: write to a temp file in the *same* dir, fsync, then os.replace into
    place -- a crash never leaves a half-written model that later loads garbage.
  * SHA-256: if the caller supplies the expected digest, the written bytes are
    re-hashed and the file is DELETED on mismatch (a truncated/tampered upload
    never survives on disk).

Stdlib only. Env-overridable roots/caps so the unit test can run against a temp
tree on a dev box.
"""
from __future__ import annotations

import hashlib
import os
import re
import tempfile

from . import paths

# Destination whitelist. A single root already covers its subdirs (…/models/asr
# is inside …/models), so the default root is enough for voice-transcribe.
# The default comes from paths.MODELS_DIR so the write path (/putModel) and the
# read path (GET /assets) can never disagree about where models live.
MODEL_ROOTS = tuple(
    os.path.normpath(p)
    for p in os.environ.get("APPMGR_MODEL_ROOTS", paths.MODELS_DIR).split(":")
    if p
)
# nginx caps the request body at 256m (ext_appmgr.conf client_max_body_size), so
# a larger cap here would be unreachable anyway; keep them aligned.
MAX_MODEL_BYTES = int(os.environ.get("APPMGR_MAX_MODEL_BYTES", str(256 * 1024 * 1024)))

# A bare basename: first char alnum, then alnum/._- , max 128 chars. This alone
# forbids "/", "\\", "..", and absolute/relative path fragments.
_FILENAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


class ModelStoreError(Exception):
    pass


def _within(path: str, root: str) -> bool:
    """True iff `path` is `root` itself or lives under it (both normalized)."""
    return paths.is_within(path, root)


def _validate_filename(filename: str) -> str:
    base = os.path.basename(filename or "")
    if base != (filename or ""):
        raise ModelStoreError("invalid filename: expected a bare basename (no path separators)")
    if not _FILENAME_RE.fullmatch(base):
        raise ModelStoreError("invalid filename: must match [A-Za-z0-9][A-Za-z0-9._-]{0,127}")
    if base in (".", "..") or ".." in base.split("/"):
        raise ModelStoreError("invalid filename: traversal not allowed")
    return base


def _resolve_target_dir(target_path: str) -> str:
    """Lexically validate the target directory against the whitelist.

    Returns the normalized absolute dir. Rejects non-absolute paths, `..`
    traversal, and anything not lexically inside a MODEL_ROOTS entry. A second,
    symlink-resolving check happens in write_model() *after* the dir exists.
    """
    if not target_path or not os.path.isabs(target_path):
        raise ModelStoreError("invalid target_path: must be an absolute path")
    norm = os.path.normpath(target_path)
    # normpath collapses a leading ".." to still-absolute, but an interior climb
    # like /userdata/local/models/../../etc becomes /etc -> caught by the root
    # check below. An explicit segment scan rejects it earlier + more clearly.
    if ".." in target_path.split("/"):
        raise ModelStoreError("invalid target_path: '..' traversal not allowed")
    if not any(_within(norm, r) for r in MODEL_ROOTS):
        raise ModelStoreError(
            f"target_path {norm} not under allowed model roots {MODEL_ROOTS}")
    return norm


def write_model(target_path: str, filename: str, data: bytes,
                sha256_expected: str | None = None) -> dict:
    """Write `data` to <target_path>/<filename> under the model-root whitelist.

    Creates the directory tree if needed, writes atomically, and verifies the
    sha256 (deleting the file on mismatch). Returns
    {path, filename, size, sha256}.
    """
    base = _validate_filename(filename)
    tgt_dir = _resolve_target_dir(target_path)

    n = len(data)
    if n == 0:
        raise ModelStoreError("empty upload")
    if n > MAX_MODEL_BYTES:
        raise ModelStoreError(f"model too large: {n} > {MAX_MODEL_BYTES}")

    os.makedirs(tgt_dir, exist_ok=True)

    # Post-mkdir, symlink-RESOLVING check: a pre-existing symlink component (e.g.
    # /userdata/local/models -> /etc) would let a lexically-clean path escape.
    real_dir = os.path.realpath(tgt_dir)
    if not any(_within(real_dir, os.path.realpath(r)) for r in MODEL_ROOTS):
        raise ModelStoreError(
            f"target_path escapes model roots after symlink resolution: {real_dir}")

    dest = os.path.join(tgt_dir, base)
    # Never follow/replace a symlink planted at the destination name.
    if os.path.islink(dest):
        raise ModelStoreError(f"refusing to overwrite a symlink at {dest}")
    real_dest_dir = os.path.realpath(os.path.dirname(dest))
    if not any(_within(real_dest_dir, os.path.realpath(r)) for r in MODEL_ROOTS):
        raise ModelStoreError("destination directory escapes model roots")

    # Atomic write: temp in the same dir, fsync, rename into place.
    fd, tmp = tempfile.mkstemp(prefix=".model.", dir=tgt_dir)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, dest)
        tmp = None
    finally:
        if tmp and os.path.exists(tmp):
            os.unlink(tmp)
    os.chmod(dest, 0o644)

    digest = hashlib.sha256(data).hexdigest()
    if sha256_expected:
        want = sha256_expected.strip().lower()
        if want and want != digest:
            # Do not leave a corrupt model on disk.
            try:
                os.unlink(dest)
            except OSError:
                pass
            raise ModelStoreError(
                f"sha256 mismatch for {base}: got {digest[:12]}… want {want[:12]}…")

    return {"path": dest, "filename": base, "size": n, "sha256": digest}
