#!/usr/bin/env python3
"""
sign.py -- sign built app packages with the release private key (TODO #4).

For each `packaging/dist/*.tar.gz` this produces a DETACHED signature over the
raw package bytes:

    <pkg>.tar.gz.sig      base64 of the DER ECDSA-SHA256 signature (P-256)

The signature is what proves authenticity: the device verifies it against the
baked-in public key (market/appmgr/keys/release_pub.pem) with its own openssl
before installing. gen_catalog.py then reads these `.sig` sidecars and embeds
the base64 into catalog.json (`package.signature`).

Signing shells out to the local `openssl` (no Python crypto dep, matching the
device-side approach):

    openssl dgst -sha256 -sign <priv.pem> -out <sig.der> <pkg.tar.gz>

Usage:
    python3 sign.py                       # sign all dist/*.tar.gz
    python3 sign.py --key ~/.recamera_release_key/release_priv.pem
    python3 sign.py --dist DIR --pkg fall-detection-0.1.0-arm64.tar.gz
    python3 sign.py --verify              # re-verify existing .sig against pubkey

Stdlib only (subprocess, base64).
"""
from __future__ import annotations

import argparse
import base64
import glob
import os
import subprocess
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DIST = os.path.join(_HERE, "dist")
DEFAULT_KEY = os.path.join(os.path.expanduser("~"), ".recamera_release_key",
                           "release_priv.pem")
DEFAULT_PUB = os.path.normpath(os.path.join(_HERE, "..", "appmgr", "keys",
                                            "release_pub.pem"))


def _openssl(*args: str, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(["openssl", *args], stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, **kw)


def sign_one(pkg: str, key: str) -> str:
    """Sign one package; write `<pkg>.sig` (base64). Return the base64 signature."""
    fd, der = tempfile.mkstemp(prefix=".sign.", suffix=".der")
    os.close(fd)
    try:
        p = _openssl("dgst", "-sha256", "-sign", key, "-out", der, pkg)
        if p.returncode != 0:
            raise SystemExit(f"openssl sign failed for {os.path.basename(pkg)}: "
                             f"{p.stdout.decode('utf-8','replace').strip()}")
        with open(der, "rb") as f:
            sig_der = f.read()
    finally:
        try:
            os.unlink(der)
        except OSError:
            pass
    if not sig_der:
        raise SystemExit(f"empty signature produced for {os.path.basename(pkg)}")
    b64 = base64.b64encode(sig_der).decode("ascii")
    with open(pkg + ".sig", "w") as f:
        f.write(b64 + "\n")
    return b64


def verify_one(pkg: str, pub: str) -> bool:
    """Verify `<pkg>.sig` against the public key (same call the device makes)."""
    sig_path = pkg + ".sig"
    if not os.path.isfile(sig_path):
        print(f"  MISSING  {os.path.basename(sig_path)}", file=sys.stderr)
        return False
    with open(sig_path) as f:
        b64 = f.read().strip()
    fd, der = tempfile.mkstemp(prefix=".verify.", suffix=".der")
    try:
        with os.fdopen(fd, "wb") as w:
            w.write(base64.b64decode(b64))
        p = _openssl("dgst", "-sha256", "-verify", pub, "-signature", der, pkg)
        out = p.stdout.decode("utf-8", "replace").strip()
        ok = p.returncode == 0 and "Verified OK" in out
        print(f"  {'OK ' if ok else 'BAD'}     {os.path.basename(pkg)}: {out}")
        return ok
    finally:
        try:
            os.unlink(der)
        except OSError:
            pass


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Sign reCamera Pro app packages.")
    ap.add_argument("--dist", default=DEFAULT_DIST,
                    help=f"dir of *.tar.gz packages (default: {DEFAULT_DIST})")
    ap.add_argument("--key", default=DEFAULT_KEY,
                    help=f"release private key (default: {DEFAULT_KEY})")
    ap.add_argument("--pub", default=DEFAULT_PUB,
                    help=f"release public key for --verify (default: {DEFAULT_PUB})")
    ap.add_argument("--pkg", help="sign/verify only this basename under --dist")
    ap.add_argument("--verify", action="store_true",
                    help="verify existing .sig files instead of signing")
    args = ap.parse_args(argv)

    if args.pkg:
        pkgs = [os.path.join(args.dist, args.pkg)]
    else:
        pkgs = sorted(glob.glob(os.path.join(args.dist, "*.tar.gz")))
    if not pkgs:
        raise SystemExit(f"no *.tar.gz packages in {args.dist}")

    if args.verify:
        print(f"verifying {len(pkgs)} package(s) against {args.pub}", file=sys.stderr)
        ok = all(verify_one(p, args.pub) for p in pkgs)
        return 0 if ok else 1

    if not os.path.isfile(args.key):
        raise SystemExit(
            f"private key not found: {args.key}\n"
            f"run ./keygen.sh first (or pass --key). The key is never in the repo.")

    print(f"signing {len(pkgs)} package(s) with {args.key}", file=sys.stderr)
    for p in pkgs:
        b64 = sign_one(p, args.key)
        print(f"  signed  {os.path.basename(p)}  -> {os.path.basename(p)}.sig "
              f"({len(b64)} b64 chars, {b64[:16]}…)", file=sys.stderr)
    print(f"\nwrote {len(pkgs)} .sig sidecar(s). Next: gen_catalog.py to embed them.",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
