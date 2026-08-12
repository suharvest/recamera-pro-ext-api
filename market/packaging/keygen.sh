#!/bin/sh
# keygen.sh -- one-time generation of the reCamera Pro release signing keypair.
#
# TODO #4 (package authenticity). Produces an EC prime256v1 (P-256) keypair used
# to sign app packages (ECDSA-SHA256). The PRIVATE key is the crown jewel:
#
#   * it NEVER enters the git repo and NEVER goes onto a device;
#   * it lives under ~/.recamera_release_key/ (chmod 600), or wherever you point
#     RELEASE_KEY_DIR -- ideally an offline / hardware-backed store for real use;
#   * anyone holding it can publish packages the whole fleet will trust, so
#     losing or leaking it means rotating the public key on every device.
#
# The PUBLIC key is committed to the repo at market/appmgr/keys/release_pub.pem
# and deployed with appmgr; it is the device's sole trust anchor.
#
# Why P-256 + openssl dgst (not Ed25519): the device ships OpenSSL 1.1.1, whose
# `pkeyutl` lacks `-rawin` (a 3.0 flag) so Ed25519 CLI verify is unreachable
# there. `openssl dgst -sha256 -verify` with an EC key works on 1.1.1 and 3.x.
#
# Usage:
#   ./keygen.sh                 # generate (refuses to clobber an existing key)
#   ./keygen.sh --force         # overwrite an existing private key (DANGEROUS)
#   RELEASE_KEY_DIR=/path ./keygen.sh
#
# zsh/bash/POSIX-sh compatible (macOS /bin/sh is fine).
set -eu

HERE="$(cd "$(dirname "$0")" && pwd)"
PUB_DEST="$HERE/../appmgr/keys/release_pub.pem"
KEY_DIR="${RELEASE_KEY_DIR:-$HOME/.recamera_release_key}"
PRIV="$KEY_DIR/release_priv.pem"

FORCE=0
[ "${1:-}" = "--force" ] && FORCE=1

command -v openssl >/dev/null 2>&1 || { echo "error: openssl not found" >&2; exit 1; }

if [ -f "$PRIV" ] && [ "$FORCE" -ne 1 ]; then
    echo "error: private key already exists at $PRIV" >&2
    echo "       refusing to overwrite (pass --force to rotate; you must then" >&2
    echo "       re-sign every package and redeploy release_pub.pem to devices)" >&2
    exit 1
fi

mkdir -p "$KEY_DIR"
chmod 700 "$KEY_DIR"
mkdir -p "$(dirname "$PUB_DEST")"

# EC P-256 private key (no password on the key file itself; protect via fs perms
# / an encrypted volume. For production, consider `-aes256` + a passphrase).
umask 077
openssl ecparam -name prime256v1 -genkey -noout -out "$PRIV"
chmod 600 "$PRIV"

# Public key -> committed into the repo / deployed to devices.
openssl ec -in "$PRIV" -pubout -out "$PUB_DEST" 2>/dev/null
chmod 644 "$PUB_DEST"

echo "generated release signing keypair:"
echo "  private (KEEP SECRET, not in repo/device): $PRIV"
echo "  public  (committed + deployed):            $PUB_DEST"
echo
echo "public key fingerprint (sha256):"
openssl pkey -pubin -in "$PUB_DEST" -outform DER 2>/dev/null | openssl dgst -sha256 | sed 's/^/  /'
echo
echo "next: sign packages with"
echo "  python3 $HERE/sign.py           # signs packaging/dist/*.tar.gz"
echo "  python3 $HERE/../catalog/gen_catalog.py   # embeds signatures into catalog.json"
