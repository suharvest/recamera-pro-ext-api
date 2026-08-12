#!/bin/sh
# recamera-ext-api rollback.sh  -- restore factory rkipc (and entry.cgi) into /oem, then reboot.
# Run ON THE DEVICE as root.
set -e
FACTORY_RKIPC_MD5=d5e7ca9365dae553e8c7e4c0a0f436ec
md5of() { md5sum "$1" 2>/dev/null | awk '{print $1}'; }

if [ ! -f /userdata/rkipc.factory.bak ]; then
  echo "FATAL: /userdata/rkipc.factory.bak missing -- cannot roll back"; exit 1
fi
BAK=$(md5of /userdata/rkipc.factory.bak)
echo "restoring rkipc from backup ($BAK)"
[ "$BAK" = "$FACTORY_RKIPC_MD5" ] || echo "WARN: backup md5 not the known factory value, restoring anyway"
cp /userdata/rkipc.factory.bak /oem/usr/bin/rkipc && chmod 755 /oem/usr/bin/rkipc

if [ -f /userdata/entry.cgi.factory.bak ]; then
  cp /userdata/entry.cgi.factory.bak /oem/usr/www/cgi-bin/entry.cgi && chmod 755 /oem/usr/www/cgi-bin/entry.cgi
  echo "entry.cgi restored"
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
