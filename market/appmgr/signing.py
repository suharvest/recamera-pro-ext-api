"""
signing.py -- release-signature verification for appmgr (device side).

TODO #4 (package authenticity). The existing installer already gives us
integrity (sha256) and safe-unpack (zip-slip); this adds *authenticity*: proof
the package was published by the holder of the release private key, so a hostile
party who can serve a catalog cannot deliver arbitrary root code.

Trust model
-----------
  * ONE asymmetric keypair. The PRIVATE key lives only on the publisher's
    workstation (`~/.recamera_release_key/`, never in repo, never on device).
  * The PUBLIC key is baked into the appmgr deploy at `keys/release_pub.pem`
    (paths.RELEASE_PUBKEY) and is the device's sole trust anchor.
  * Each package carries a DETACHED signature over the *raw .tar.gz bytes*:
        ECDSA, curve prime256v1 (P-256), digest SHA-256.
    Distributed base64 in the catalog (`package.signature`) and/or as a
    `<pkg>.tar.gz.sig` sidecar.

Why ECDSA-P256 via `openssl dgst` and not Ed25519
--------------------------------------------------
The device ships OpenSSL 1.1.1, whose `pkeyutl` has no `-rawin` (a 3.0 flag), so
Ed25519 one-shot verify is not reachable from the 1.1.1 CLI. `openssl dgst
-sha256 -verify` with an EC key is solid on both 1.1.1 (device) and 3.x (build
host). Zero new Python deps -- we shell out to the device's own openssl.
"""
from __future__ import annotations

import base64
import binascii
import os
import shutil
import subprocess
import tempfile

from . import paths

SIGNATURE_ALG = "ecdsa-sha256"          # curve prime256v1


class SignatureError(Exception):
    """Raised when a package fails authenticity verification (or is unsigned
    while a signature is required)."""


def _openssl() -> str:
    exe = shutil.which("openssl") or "/usr/bin/openssl"
    if not os.path.exists(exe):
        raise SignatureError("openssl not found on device; cannot verify signature")
    return exe


def sidecar_path(pkg_path: str) -> str:
    return pkg_path + ".sig"


def load_signature(pkg_path: str, signature_b64: str | None) -> str | None:
    """Return the base64 signature to check: explicit arg wins, else the
    `<pkg>.sig` sidecar if present, else None (unsigned)."""
    if signature_b64:
        return signature_b64.strip()
    side = sidecar_path(pkg_path)
    if os.path.isfile(side):
        try:
            with open(side, "r") as f:
                s = f.read().strip()
            return s or None
        except OSError:
            return None
    return None


def _verify_raw(pkg_path: str, sig_der: bytes, pubkey: str) -> tuple[bool, str]:
    """Run `openssl dgst -sha256 -verify pub -signature sig pkg`. Returns
    (ok, detail). openssl prints 'Verified OK' + exit 0 on success."""
    fd, sigfile = tempfile.mkstemp(prefix=".sigverify.", suffix=".der")
    try:
        with os.fdopen(fd, "wb") as w:
            w.write(sig_der)
        proc = subprocess.run(
            [_openssl(), "dgst", "-sha256", "-verify", pubkey,
             "-signature", sigfile, pkg_path],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=60,
        )
        out = (proc.stdout or b"").decode("utf-8", "replace").strip()
        ok = proc.returncode == 0 and "Verified OK" in out
        return ok, out or f"openssl exit {proc.returncode}"
    except subprocess.TimeoutExpired:
        return False, "openssl verify timed out"
    finally:
        try:
            os.unlink(sigfile)
        except OSError:
            pass


def verify_package(pkg_path: str, signature_b64: str | None = None,
                   *, require: bool | None = None,
                   pubkey: str | None = None) -> dict:
    """Verify a package's release signature and apply the require-signature
    policy. Returns a status dict; RAISES SignatureError on any hard failure.

    status = {
      "signed": bool,       # a signature was supplied/found
      "verified": bool,     # it checked out against the trust anchor
      "alg": "ecdsa-sha256",
      "detail": "<openssl line or policy note>",
    }

    Rules:
      * present signature -> MUST verify; bad/garbled -> SignatureError (always,
        independent of `require`).
      * no signature      -> require=True  -> SignatureError ("unsigned");
                             require=False -> allowed, status verified=False.
    """
    require = paths.REQUIRE_SIGNATURE if require is None else require
    pubkey = pubkey or paths.RELEASE_PUBKEY
    sig_b64 = load_signature(pkg_path, signature_b64)

    if not sig_b64:
        if require:
            raise SignatureError(
                "package is unsigned and signature verification is required "
                "(set APPMGR_REQUIRE_SIGNATURE=0 to allow unsigned installs)")
        return {"signed": False, "verified": False, "alg": SIGNATURE_ALG,
                "detail": "unsigned package allowed by policy (require_signature=0)"}

    if not os.path.isfile(pubkey):
        # fail closed: a signature is present but we have no trust anchor to
        # check it against -> we cannot vouch for it, so refuse.
        raise SignatureError(f"no release public key at {pubkey}; cannot verify signature")

    try:
        sig_der = base64.b64decode(sig_b64, validate=True)
    except (binascii.Error, ValueError) as e:
        raise SignatureError(f"signature is not valid base64: {e}")
    if not sig_der:
        raise SignatureError("signature is empty after base64 decode")

    ok, detail = _verify_raw(pkg_path, sig_der, pubkey)
    if not ok:
        raise SignatureError(f"signature verification FAILED: {detail}")
    return {"signed": True, "verified": True, "alg": SIGNATURE_ALG, "detail": detail}
