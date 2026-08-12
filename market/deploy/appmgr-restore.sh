#!/bin/sh
# appmgr-restore.sh -- idempotent post-OTA recovery for the reCamera Pro App
# Center launcher (S94appmgr).
#
# WHY THIS EXISTS
#   An A/B OTA reflashes the rootfs, so /etc/init.d/S94appmgr (the SysVinit hook
#   that starts appmgr and re-injects the nginx edge conf) is wiped. The stock
#   RkLunch.sh restore chain only re-injects /etc/passwd, /etc/group,
#   /etc/shadow (+ custom_shadow hashes); it does NOT restore /etc/init.d. The
#   appmgr package itself and this script live under /userdata and survive OTA,
#   but nothing in the stock firmware automatically re-drops the launcher.
#
# WHAT THIS DOES (idempotent -- safe to run any number of times)
#   1. Copy the OTA-surviving master S94appmgr back into /etc/init.d if missing
#      or stale.
#   2. Start appmgr via that launcher (which itself re-injects ext_appmgr.conf
#      into nginx when needed, guarded by `nginx -t`).
#
# TRIGGERING IT AFTER A REAL OTA (honest status -- see RESTORE-README note)
#   There is no OTA-surviving boot hook that sources arbitrary /userdata
#   scripts, so this cannot be made 100% automatic without either baking
#   S94appmgr into the official image or adding an init.d entry to RkLunch
#   (both outside this package's scope). Until then, run this once after an OTA:
#       sh /userdata/local/appcenter/appmgr-restore.sh
#   A boot from a slot that still has S94appmgr needs nothing -- S94appmgr's
#   seed_s94_master keeps the master current on every start.

set -u

MASTER=/userdata/config/system/etc/init.d/S94appmgr
LIVE=/etc/init.d/S94appmgr

log() { echo "[appmgr-restore] $*"; }

if [ ! -f "$MASTER" ]; then
    log "ERROR: no master launcher at $MASTER; cannot restore"
    exit 1
fi

if cmp -s "$MASTER" "$LIVE" 2>/dev/null; then
    log "live launcher already matches master ($LIVE)"
else
    if cp "$MASTER" "$LIVE" 2>/dev/null && chmod +x "$LIVE" 2>/dev/null; then
        log "restored launcher: $MASTER -> $LIVE"
    else
        log "ERROR: failed to install $LIVE from $MASTER"
        exit 1
    fi
fi

log "starting appmgr via $LIVE"
"$LIVE" start
