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

echo "=== [1/7] verify package artifacts ==="
need "$PKG/rkipc" "$RKIPC_MD5"
need "$PKG/entry.cgi" "$ENTRY_MD5"
need "$PKG/sdk/lib/librecamera_ext.so.1.0.0" "$SO_MD5"
echo "package OK"

echo "=== [2/7] backup factory rkipc (once) ==="
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

echo "=== [3/7] backup factory entry.cgi (once) ==="
if [ ! -f /userdata/entry.cgi.factory.bak ]; then
  cp /oem/usr/www/cgi-bin/entry.cgi /userdata/entry.cgi.factory.bak
  echo "saved /userdata/entry.cgi.factory.bak"
else
  echo "entry.cgi backup exists"
fi

echo "=== [4/7] verify /oem writable ==="
touch /oem/.wtest && rm -f /oem/.wtest && echo "OEM writable" || { echo "FATAL /oem not writable"; exit 1; }

echo "=== [5/7] install into /oem (persistent) ==="
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

echo "=== [6/7] provision rknnlite python runtime (best-effort) ==="
# Adds the Python inference runtime (rknn-toolkit-lite2 + deps) so vision apps run
# out of the box. Failures here only WARN -- the rkipc install above is already done,
# so we never block the main path.  Idempotent: skips work already present.
RKNNENV=/userdata/rknnenv
WHEELS="$PKG/wheels"
provision_rknnlite() {
  # 1) stock rknnlite hardcodes /usr/lib/librknnrt.so -- point it at the OEM copy.
  if [ ! -e /usr/lib/librknnrt.so ]; then
    if [ -e /oem/usr/lib/librknnrt.so ]; then
      ln -sf /oem/usr/lib/librknnrt.so /usr/lib/librknnrt.so && echo "  linked /usr/lib/librknnrt.so -> /oem/usr/lib/librknnrt.so"
    else
      echo "  WARN: /oem/usr/lib/librknnrt.so missing -- rknnlite will fail to load"
    fi
  else
    echo "  /usr/lib/librknnrt.so already present"
  fi
  # 2) venv with system site-packages (numpy comes from the system, not a wheel).
  if [ ! -d "$RKNNENV" ]; then
    python3 -m venv --system-site-packages "$RKNNENV" && echo "  created venv $RKNNENV" || { echo "  WARN: venv create failed"; return 1; }
  else
    echo "  venv $RKNNENV already exists"
  fi
  # 3) offline install of the 4 aarch64/cp311 wheels (device has no network).
  "$RKNNENV/bin/pip" install --no-index --find-links "$WHEELS" \
      rknn-toolkit-lite2 psutil ruamel.yaml ruamel.yaml.clib \
    && echo "  wheels installed offline from $WHEELS" || { echo "  WARN: offline pip install failed"; return 1; }
  # 4) self-check: import RKNNLite with the OEM runtime on LD path.
  if LD_LIBRARY_PATH=/oem/usr/lib "$RKNNENV/bin/python3" \
       -c "from rknnlite.api import RKNNLite; RKNNLite(); print('rknnlite OK')"; then
    echo "  self-check PASSED"
  else
    echo "  WARN: rknnlite self-check FAILED (import/runtime issue) -- inspect on device"
    return 1
  fi
  return 0
}
if provision_rknnlite; then
  echo "rknnlite runtime provisioned."
else
  echo "WARN: rknnlite provision incomplete -- main install unaffected; fix on device before running vision apps."
fi
echo "  vision apps run with:"
echo "    PYTHONPATH=/userdata/local:/userdata/sdk/python \\"
echo "    LD_LIBRARY_PATH=/oem/usr/lib \\"
echo "    $RKNNENV/bin/python3 <app>.py"

echo "=== [7/7] reboot to activate new rkipc ==="
echo "run 'reboot' now (or pass --reboot). After ~1-2min self-check: ls /run/recamera/"
if [ "$1" = "--reboot" ]; then sync; reboot; fi
