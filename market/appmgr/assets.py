"""assets.py -- "what is already on the device?" for the shared model tree.

Motivation (INSTALL_ASSETS_SPEC §1): the browser install flow used to re-fetch
and re-upload every model in `manifest.models[]` unconditionally. voice-transcribe
ships a 133 MB ASR model that is normally already present and byte-identical, and
at the measured ~800 KB/s browser->device rate that upload cannot finish inside
nginx's `proxy_read_timeout 200s`. So the install simply never succeeded. This
module answers "present? what size? what sha256?" for a batch of model paths so
the front end can skip an asset *before* it downloads it from the CDN.

Two things make this non-trivial:

  * PATH SAFETY. `paths` comes straight from an HTTP query string, so it is
    treated exactly like an installer tar member: relative only, no `..`, no NUL,
    and the realpath must stay inside the root (paths.is_within -- the same
    predicate installer._vet_member and modelstore use). A rejected path fails the
    WHOLE request rather than being silently skipped: a silent skip would read as
    "absent" to the caller and trigger a pointless 133 MB re-upload.
  * HASH COST. sha256 of 133 MB costs real seconds on this CPU, and the front end
    polls this endpoint per install. Digests are therefore memoized on
    (size, mtime_ns, inode): if none of the three moved, the bytes did not move
    either, and any write path that could change them (modelstore's atomic
    os.replace) necessarily lands a new inode. The cache is bounded -- an
    unbounded dict keyed by client-supplied paths is a memory-growth lever for
    anyone who can reach the endpoint.

`present: true` with the sha256 field ABSENT means "the file is there but we
could not read it" (EIO, permissions). Per the spec we omit the field rather than
inventing a digest, and the front end treats that as "size-only match".

Stdlib only; the root is env-overridable (paths.MODELS_DIR) so tests run against
a temp tree.
"""
from __future__ import annotations

import hashlib
import os
from collections import OrderedDict

from . import paths

# One query cannot ask about an unbounded number of files. The largest real app
# manifest lists a handful of models; 64 is generous and caps the stat() work an
# unauthenticated-at-this-layer request can trigger.
MAX_ASSET_PATHS = int(os.environ.get("APPMGR_MAX_ASSET_PATHS", "64"))
# Bound on the memoized digests. Each entry is ~150 bytes and the device tree
# holds well under a hundred models, so this never evicts in practice -- it
# exists so a hostile caller cannot grow the dict forever by querying paths that
# do not exist... (misses are not cached at all, but a large tree of real files
# could still be walked). LRU: least-recently-used entry is dropped first.
MAX_HASH_CACHE = int(os.environ.get("APPMGR_MAX_HASH_CACHE", "512"))

_HASH_READ_BYTES = 1 << 20

# realpath -> (statkey, hexdigest). OrderedDict gives LRU ordering for free.
_hash_cache: "OrderedDict[str, tuple]" = OrderedDict()

# Number of times a digest was actually computed from file bytes (i.e. cache
# misses). The spec's acceptance criterion for §1 is "query the same file twice,
# the second time does not hash again", which needs an observable counter --
# timing alone is flaky on a loaded box. Tests read this; production ignores it.
hash_computations = 0


class AssetPathError(ValueError):
    """A requested path is not a safe relative path under the model root."""


def models_root() -> str:
    """The model tree root, read at call time so tests can rebind the env var."""
    return os.environ.get("APPMGR_MODELS_DIR", paths.MODELS_DIR)


def resolve(rel: str) -> str:
    """Validate a client-supplied relative model path -> absolute path.

    Refuses absolute paths, `..` traversal, NUL bytes and anything whose
    realpath escapes the root (which is what catches a symlink inside the tree
    pointing at /etc). Returns the joined path (NOT the realpath) so the caller
    stats the file the client actually named.
    """
    if not rel or not rel.strip():
        raise AssetPathError("empty path")
    if "\0" in rel:
        raise AssetPathError(f"invalid path (NUL byte): {rel!r}")
    norm_sep = rel.replace("\\", "/")
    if norm_sep.startswith("/") or os.path.isabs(rel):
        raise AssetPathError(f"path must be relative to the model root: {rel!r}")
    if ".." in norm_sep.split("/"):
        raise AssetPathError(f"'..' traversal not allowed: {rel!r}")

    root = models_root()
    full = os.path.normpath(os.path.join(root, norm_sep))
    # Lexical check first (works even when neither path exists yet), then the
    # symlink-resolving one against the root's own realpath.
    if not paths.is_within(full, os.path.normpath(root)):
        raise AssetPathError(f"path escapes the model root: {rel!r}")
    real_root = os.path.realpath(root)
    if not paths.is_within(os.path.realpath(full), real_root):
        raise AssetPathError(f"path escapes the model root via symlink: {rel!r}")
    return full


def _stat_key(st: os.stat_result) -> tuple:
    """The identity a cached digest is valid for: (size, mtime_ns, inode)."""
    return (st.st_size, st.st_mtime_ns, st.st_ino)


def sha256_file(path: str, st: os.stat_result) -> str:
    """sha256 of `path`, memoized on the file's (size, mtime_ns, inode).

    Raises OSError if the bytes cannot be read -- the caller turns that into an
    omitted `sha256` field.
    """
    global hash_computations
    key = os.path.realpath(path)
    want = _stat_key(st)
    hit = _hash_cache.get(key)
    if hit is not None and hit[0] == want:
        _hash_cache.move_to_end(key)
        return hit[1]

    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(_HASH_READ_BYTES)
            if not chunk:
                break
            h.update(chunk)
    digest = h.hexdigest()
    hash_computations += 1

    _hash_cache[key] = (want, digest)
    _hash_cache.move_to_end(key)
    while len(_hash_cache) > MAX_HASH_CACHE:
        _hash_cache.popitem(last=False)
    return digest


def free_bytes(path: str = None) -> int:
    """Free space on the filesystem holding the model root (0 if unknowable).

    Walks up to the nearest existing ancestor: on a fresh device
    /userdata/local/models may not exist yet, and the front end still wants to
    know whether /userdata can take a 133 MB upload.
    """
    p = os.path.abspath(path or models_root())
    while p and not os.path.exists(p):
        parent = os.path.dirname(p)
        if parent == p:
            return 0
        p = parent
    try:
        st = os.statvfs(p)
    except OSError:
        return 0
    return int(st.f_bavail) * int(st.f_frsize)


def query(rel_paths) -> dict:
    """Report presence/size/sha256 for each requested path under the model root.

    Returns the INSTALL_ASSETS_SPEC §1 shape:
        {root, assets: {<rel>: {present, size?, sha256?}}, free_bytes}
    Raises AssetPathError (-> HTTP 400) if any path is unsafe.
    """
    root = models_root()
    wanted = [p for p in (s.strip() for s in rel_paths) if p]
    if len(wanted) > MAX_ASSET_PATHS:
        raise AssetPathError(
            f"too many paths: {len(wanted)} > {MAX_ASSET_PATHS}")

    assets = {}
    for rel in wanted:
        if rel in assets:            # duplicate in the query -- stat once
            continue
        full = resolve(rel)          # raises -> whole request is refused
        try:
            st = os.stat(full)
        except OSError:
            assets[rel] = {"present": False}
            continue
        if not os.path.isfile(full):
            # A directory (or fifo/device) sitting at the model's name is not a
            # usable model; report absent so the caller re-uploads rather than
            # trusting whatever is there.
            assets[rel] = {"present": False}
            continue
        entry = {"present": True, "size": st.st_size}
        try:
            entry["sha256"] = sha256_file(full, st)
        except OSError:
            pass                     # spec §1: omit rather than invent
        assets[rel] = entry

    return {"root": root, "assets": assets, "free_bytes": free_bytes(root)}
