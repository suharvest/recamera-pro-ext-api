#!/bin/sh
# recamera-ext-api install.sh  (persistent sideload into /oem, survives reboot)
# Run ON THE DEVICE as root:  adb push <this dir>/* /userdata/ext-pkg/ ; adb shell "sh /userdata/ext-pkg/install.sh"
# Idempotent: backs up factory files once, md5-verifies every artifact, then overwrites /oem.
set -e

PKG=$(cd "$(dirname "$0")" && pwd)
RKIPC_MD5=de5b3aa41ba5dd02968632823aac29cf
ENTRY_MD5=75a693c87c317a49c37c4dddb6b9ac7a
SO_MD5=bb9fe0bfff7762c067bf9d502035bc40
FACTORY_RKIPC_MD5=d5e7ca9365dae553e8c7e4c0a0f436ec   # V1.0.x factory rkipc (verified on ref device)

md5of() { md5sum "$1" 2>/dev/null | awk '{print $1}'; }
need() { [ "$(md5of "$1")" = "$2" ] || { echo "FATAL md5 mismatch: $1 (got $(md5of "$1") want $2)"; exit 1; }; }

echo "=== [1/6] verify package artifacts ==="
need "$PKG/rkipc" "$RKIPC_MD5"
need "$PKG/entry.cgi" "$ENTRY_MD5"
need "$PKG/sdk/lib/librecamera_ext.so.1.0.0" "$SO_MD5"
echo "package OK"

echo "=== [2/6] backup factory rkipc (once) ==="
if [ ! -f /userdata/rkipc.factory.bak ]; then
  cp /oem/usr/bin/rkipc /userdata/rkipc.factory.bak
  echo "saved /userdata/rkipc.factory.bak"
else
  echo "backup exists: $(md5of /userdata/rkipc.factory.bak)"
fi
# Safety: refuse to proceed unless a valid rollback target exists.
BAK=$(md5of /userdata/rkipc.factory.bak)
CUR=$(md5of /oem/usr/bin/rkipc)
if [ "$BAK" != "$FACTORY_RKIPC_MD5" ] && [ "$BAK" != "$RKIPC_MD5" ]; then
  echo "WARN: backup md5 ($BAK) is neither known-factory nor our rkipc."
  if [ "$CUR" != "$RKIPC_MD5" ]; then
    echo "FATAL: no valid rollback target and /oem rkipc not yet ours -- ABORT"; exit 1
  fi
fi

echo "=== [3/6] backup factory entry.cgi (once) ==="
if [ ! -f /userdata/entry.cgi.factory.bak ]; then
  cp /oem/usr/www/cgi-bin/entry.cgi /userdata/entry.cgi.factory.bak
  echo "saved /userdata/entry.cgi.factory.bak"
else
  echo "entry.cgi backup exists"
fi

echo "=== [4/6] verify /oem writable ==="
touch /oem/.wtest && rm -f /oem/.wtest && echo "OEM writable" || { echo "FATAL /oem not writable"; exit 1; }

echo "=== [5/6] install into /oem (persistent) ==="
cp "$PKG/rkipc"    /oem/usr/bin/rkipc                    && chmod 755 /oem/usr/bin/rkipc
cp "$PKG/entry.cgi" /oem/usr/www/cgi-bin/entry.cgi       && chmod 755 /oem/usr/www/cgi-bin/entry.cgi
cp "$PKG/sdk/lib/librecamera_ext.so.1.0.0" /oem/usr/lib/ && chmod 755 /oem/usr/lib/librecamera_ext.so.1.0.0
ln -sf librecamera_ext.so.1.0.0 /oem/usr/lib/librecamera_ext.so.1
ln -sf librecamera_ext.so.1     /oem/usr/lib/librecamera_ext.so
# SDK python + header for solution vendors (non-/oem, survives OTA)
mkdir -p /userdata/sdk/python
cp -R "$PKG/sdk/python/recamera_ext" /userdata/sdk/python/
cp "$PKG/sdk/recamera_ext.h" /userdata/sdk/ 2>/dev/null || true
sync
echo "installed. post md5:"; md5of /oem/usr/bin/rkipc; md5of /oem/usr/www/cgi-bin/entry.cgi; md5of /oem/usr/lib/librecamera_ext.so.1.0.0

echo "=== [6/6] reboot to activate new rkipc ==="
echo "run 'reboot' now (or pass --reboot). After ~1-2min self-check: ls /run/recamera/"
if [ "$1" = "--reboot" ]; then sync; reboot; fi
