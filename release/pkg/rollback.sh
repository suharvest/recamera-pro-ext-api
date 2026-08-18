#!/bin/sh
# recamera-ext-api rollback.sh  -- restore factory rkipc (and entry.cgi) into /oem, then reboot.
# Run ON THE DEVICE as root:  sh rollback.sh [--reboot]
set -e
# A rollback target must be a FACTORY build -- one that does NOT carry our
# extension. That is a content property, so test the content (see install.sh for
# why an md5 allowlist is the wrong instrument here).
RKIPC_EXT_MARKERS='/run/recamera|rc_ext_'
ENTRY_EXT_MARKERS='ExtApiHandler'
md5of() { md5sum "$1" 2>/dev/null | awk '{print $1}'; }
has_ext() {
  if command -v strings >/dev/null 2>&1; then
    strings -a "$1" 2>/dev/null | grep -qE "$2"
  else
    grep -aqE "$2" "$1"
  fi
}
is_factory_rkipc() { [ -f "$1" ] && ! has_ext "$1" "$RKIPC_EXT_MARKERS"; }
is_factory_entry() { [ -f "$1" ] && ! has_ext "$1" "$ENTRY_EXT_MARKERS"; }

if [ ! -f /userdata/rkipc.factory.bak ]; then
  echo "FATAL: /userdata/rkipc.factory.bak missing -- cannot roll back locally."
  echo "       Recover with a full OTA / update.img flash, which rewrites /oem to factory."
  exit 1
fi
BAK=$(md5of /userdata/rkipc.factory.bak)
if ! is_factory_rkipc /userdata/rkipc.factory.bak; then
  echo "FATAL: backup ($BAK) CARRIES extension markers -- it is one of our builds, not a"
  echo "       factory rkipc. Restoring it would be a no-op. Nothing changed."
  echo "       Recover with a full OTA / update.img flash, which rewrites /oem to factory."
  exit 1
fi
[ -f /userdata/rkipc.factory.bak.info ] && { echo "backup provenance:"; sed 's/^/  /' /userdata/rkipc.factory.bak.info; }
echo "restoring factory rkipc from backup ($BAK)"
cp /userdata/rkipc.factory.bak /oem/usr/bin/rkipc && chmod 755 /oem/usr/bin/rkipc

if [ -f /userdata/entry.cgi.factory.bak ]; then
  if is_factory_entry /userdata/entry.cgi.factory.bak; then
    cp /userdata/entry.cgi.factory.bak /oem/usr/www/cgi-bin/entry.cgi && chmod 755 /oem/usr/www/cgi-bin/entry.cgi
    echo "entry.cgi restored"
  else
    echo "WARN: entry.cgi backup carries extension markers -- skipped (restoring it is a no-op)"
  fi
fi
# ext .so left in place is harmless (nothing loads it unless a solution asks); remove if you want a clean factory:
# rm -f /oem/usr/lib/librecamera_ext.so /oem/usr/lib/librecamera_ext.so.1 /oem/usr/lib/librecamera_ext.so.1.0.0

# --- rknnlite python runtime rollback (idempotent, best-effort) ---------------
# Remove only the /usr/lib/librknnrt.so symlink we created (leave a real file untouched),
# then drop the venv we provisioned. Both are safe to skip if absent.
if [ -L /usr/lib/librknnrt.so ]; then
  rm -f /usr/lib/librknnrt.so && echo "removed our /usr/lib/librknnrt.so symlink"
elif [ -e /usr/lib/librknnrt.so ]; then
  echo "leaving /usr/lib/librknnrt.so (real file, not our symlink)"
fi
if [ -d /userdata/rknnenv ]; then
  rm -rf /userdata/rknnenv && echo "removed /userdata/rknnenv"
fi
sync
echo "rolled back. post md5: $(md5of /oem/usr/bin/rkipc)"
echo "reboot now to run factory rkipc."
if [ "$1" = "--reboot" ]; then reboot; fi
