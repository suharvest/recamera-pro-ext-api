#!/usr/bin/env bash
#
# deploy-app.sh -- reCamera Pro v1.5.0 application-layer one-shot deploy.
#
# Brings a device to the full v1.5.0 application state, in order:
#   1. kit + SDK + inference wheels (jinja2/markupsafe)  -> /userdata/local/kit, /userdata/sdk, /userdata/rknnenv
#   2. appmgr (App Center manager)                       -> /userdata/local/appmgr   (+ restart)
#   3. frontend web bundle                               -> /oem/usr/www
#   4. app packages (9 apps, code+manifest only)         -> /userdata/local/apps
#   5. activate one app and verify the pipeline is live
#
# SAFE by design: this script NEVER touches rkipc, the camera firmware, nginx
# edge config, or cgi-bin. It only writes /userdata and overlays the web bundle
# under /oem/usr/www. The masking firmware (rkipc/SDK) is deployed SEPARATELY and
# at higher risk by deploy-firmware.sh -- it is NOT part of this flow.
#
# Transport: files/root ops go over ADB (the device's adbd runs as root). SSH
# (admin) is not required. Idempotent: safe to re-run; every replaced tree is
# backed up first with a timestamp suffix.
#
# Usage:
#   ./deploy-app.sh [--host <ip>] [--skip-kit] [--skip-frontend] [--no-activate]
#     --host          device IP (default 192.168.42.1), adb serial = <ip>:5555
#     --activate-app  app id to activate at the end (default retail-vision)
#     --skip-kit      skip step 1 (kit already installed)
#     --skip-frontend skip step 3
#     --no-activate   skip step 5 (no camera touch, no app start)
#
set -euo pipefail

# ---- args ------------------------------------------------------------------
HOST=192.168.42.1
ACTIVATE_APP=retail-vision
SKIP_KIT=0
SKIP_FRONTEND=0
NO_ACTIVATE=0
while [ $# -gt 0 ]; do
  case "$1" in
    --host) HOST="$2"; shift 2;;
    --activate-app) ACTIVATE_APP="$2"; shift 2;;
    --skip-kit) SKIP_KIT=1; shift;;
    --skip-frontend) SKIP_FRONTEND=1; shift;;
    --no-activate) NO_ACTIVATE=1; shift;;
    -h|--help) grep -E '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0;;
    *) echo "unknown arg: $1" >&2; exit 2;;
  esac
done

HERE="$(cd "$(dirname "$0")" && pwd)"
SERIAL="${HOST}:5555"
TS="$(date +%Y%m%d-%H%M%S)"
STAGE=/userdata/_deploy
VER=1.5.0

PKG_KIT="$HERE/recamera-ext-kit-v${VER}.tar.gz"
PKG_APPMGR="$HERE/appmgr-v${VER}.tar.gz"
PKG_FRONTEND="$HERE/frontend-v${VER}.tar.gz"
PKG_APPS="$HERE/apps-v${VER}.tar.gz"

say()  { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
ok()   { printf '  \033[32mOK\033[0m %s\n' "$*"; }
warn() { printf '  \033[33mWARN\033[0m %s\n' "$*"; }
die()  { printf '\n\033[1;31mFATAL: %s\033[0m\n' "$*" >&2; exit 1; }

# adb shell with sentinel extraction so the device login banner never pollutes
# captured output. Prints only what the command emitted between markers.
ash() {  # ash "<remote shell command>"
  adb -s "$SERIAL" shell "echo __B__; { $1; }; echo __E__=\$?" 2>&1 \
    | tr -d '\r' | sed -n '/^__B__$/,/^__E__=/p' | sed '1d;/^__E__=/d'
}
ash_rc() {  # run remote command, return its exit code (banner-safe)
  local out; out="$(adb -s "$SERIAL" shell "{ $1; }; echo __RC__=\$?" 2>&1 | tr -d '\r')"
  echo "$out" | grep -q '__RC__=0'
}

lmd5() { md5 -q "$1" 2>/dev/null || md5sum "$1" | cut -d' ' -f1; }
push_verified() {  # push_verified <local> <remote> ; adb-over-Tailscale can
                   # report a spurious EOF on large pushes, so verify by md5.
  local lf="$1" rf="$2" want got i
  want="$(lmd5 "$lf")"
  for i in 1 2 3 4; do
    adb -s "$SERIAL" push "$lf" "$rf" >/dev/null 2>&1 || true
    got="$(ash "md5sum '$rf' 2>/dev/null | cut -d' ' -f1")"
    [ "$got" = "$want" ] && { ok "pushed $(basename "$lf") (md5 $got)"; return 0; }
    warn "push incomplete (try $i/4): want $want got ${got:-none} -- retrying"
    sleep 3
  done
  die "failed to push $lf (md5 never matched)"
}

check_vpss() {  # STOP if the vpss/CSIBDG kernel oops signature appears
  local hits
  hits="$(ash "dmesg 2>/dev/null | tail -80 | grep -iE 'vpss.*err|CSIBDG.*fifo overflow|Unable to handle kernel|Oops \[#' || true")"
  if [ -n "$hits" ]; then
    printf '%s\n' "$hits"
    die "VPSS/kernel oops detected in dmesg -- STOP (reboot device before retrying)"
  fi
  ok "dmesg clean (no vpss/CSIBDG/Oops)"
}

# ---- preflight -------------------------------------------------------------
say "preflight: packages + adb root on $HOST"
for f in "$PKG_KIT" "$PKG_APPMGR" "$PKG_FRONTEND" "$PKG_APPS"; do
  [ -f "$f" ] || die "missing package: $f"
done
command -v adb >/dev/null || die "adb not found on PATH"
# adb-over-Tailscale can briefly report the device 'offline' right after connect;
# retry the root probe a few times before giving up.
ID=""
for i in 1 2 3 4 5 6; do
  adb connect "$SERIAL" >/dev/null 2>&1 || true
  adb -s "$SERIAL" wait-for-device >/dev/null 2>&1 || true
  ID="$(ash 'id -u')"
  [ "$ID" = "0" ] && break
  sleep 2
done
[ "$ID" = "0" ] || die "adb shell is not root (uid=$ID) on $SERIAL"
ok "adb root shell on $SERIAL"

RKIPC_BEFORE="$(ash 'md5sum /oem/usr/bin/rkipc 2>/dev/null | cut -d" " -f1')"
[ -n "$RKIPC_BEFORE" ] || die "cannot read /oem/usr/bin/rkipc"
ok "rkipc md5 (baseline, MUST NOT change): $RKIPC_BEFORE"

ash "mkdir -p $STAGE/backups" >/dev/null

# ---- step 1: kit + sdk + wheels -------------------------------------------
if [ "$SKIP_KIT" = "1" ]; then
  say "step 1/5 kit -- skipped (--skip-kit)"
else
  say "step 1/5 kit + SDK + inference wheels"
  push_verified "$PKG_KIT" "$STAGE/kit.tar.gz"
  ash "rm -rf $STAGE/kit-extra && mkdir -p $STAGE/kit-extra && gzip -dc $STAGE/kit.tar.gz | tar -xf - -C $STAGE/kit-extra" >/dev/null
  # kit tar.gz has its own INSTALL.sh (installs kit->/userdata/local/kit,
  # sdk->/userdata/sdk, wheels->/userdata/rknnenv; backs up old copies; idempotent).
  INSTALL_DIR="$(ash "ls -d $STAGE/kit-extra/*/ 2>/dev/null | head -1; [ -f $STAGE/kit-extra/INSTALL.sh ] && echo $STAGE/kit-extra/" | tail -1)"
  INSTALL_DIR="${INSTALL_DIR%/}"
  ash "cd '$INSTALL_DIR' && sh INSTALL.sh" | sed 's/^/  /'
  ok "kit INSTALL.sh done"
fi

# ---- step 2: appmgr --------------------------------------------------------
say "step 2/5 appmgr -> /userdata/local/appmgr"
ash "[ -d /userdata/local/appmgr ] && cp -a /userdata/local/appmgr $STAGE/backups/appmgr.$TS || true" >/dev/null
ok "backed up existing appmgr -> $STAGE/backups/appmgr.$TS"
push_verified "$PKG_APPMGR" "$STAGE/appmgr.tar.gz"
# Merge-extract: overwrites code + keys, preserves runtime state
# (audit.log, mqtt.json, locks) that is not in the package.
ash "mkdir -p /userdata/local && gzip -dc $STAGE/appmgr.tar.gz | tar -xf - -C /userdata/local" >/dev/null
ok "appmgr code deployed (keys/ preserved from package)"
# Restart appmgr serve with setsid, no env hacks: cd into the package parent so
# `python3 -m appmgr` resolves, background under a new session, record the pid
# so the S94appmgr init script stays consistent. Stop the old serve by scanning
# /proc for its cmdline (NOT `pkill -f`, which would match this very restart
# command and kill our own shell); exclude this shell's own pid ($$).
# The pid is recovered from /proc AFTER the process is up, not from `$!`: setsid
# forks when it is already a process-group leader -- which a shell background job
# always is -- so `$!` is setsid's own pid and it exits immediately. Writing that
# left /var/run/appmgr.pid dead from birth, which is what made `S94appmgr
# restart` a silent no-op afterwards.
ash 'MOD="-m appmgr serve"; MY=$$; \
     [ -x /etc/init.d/S94appmgr ] && /etc/init.d/S94appmgr stop >/dev/null 2>&1; \
     [ -f /var/run/appmgr.pid ] && kill "$(cat /var/run/appmgr.pid)" 2>/dev/null; \
     for pid in $(ls /proc 2>/dev/null | grep -E "^[0-9]+$"); do \
       [ "$pid" = "$MY" ] && continue; \
       cmd=$(tr "\0" " " < /proc/$pid/cmdline 2>/dev/null); \
       case "$cmd" in *"$MOD"*) kill "$pid" 2>/dev/null;; esac; \
     done; sleep 3; \
     cd /userdata/local && setsid /usr/bin/python3 -m appmgr serve >> /var/log/appmgr.log 2>&1 < /dev/null & \
     sleep 4; \
     rm -f /var/run/appmgr.pid; \
     for pid in $(ls /proc 2>/dev/null | grep -E "^[0-9]+$"); do \
       [ "$pid" = "$MY" ] && continue; \
       cmd=$(tr "\0" " " < /proc/$pid/cmdline 2>/dev/null); \
       case "$cmd" in *"$MOD"*) echo "$pid" > /var/run/appmgr.pid; break;; esac; \
     done' >/dev/null
if ash_rc "curl -s -m 8 http://127.0.0.1:8130/api/appMgr/list >/dev/null"; then
  ok "appmgr serve up on 127.0.0.1:8130"
else
  die "appmgr did not answer on :8130 after restart (see /var/log/appmgr.log)"
fi

# ---- step 3: frontend (differential sync) ----------------------------------
# The bundle is ~36 MB, but 34 MB of that is three Source Han Sans woff2 files
# that never change between releases. Pushing the whole tarball every time wastes
# minutes of adb-over-Tailscale bandwidth on bytes the device already has.
# Instead: unpack locally, md5 every file, ask the device for the md5 of the same
# paths, and push only what differs. Stale files under static/ (old hashed
# bundles) are pruned explicitly.
WWW=/oem/usr/www
if [ "$SKIP_FRONTEND" = "1" ]; then
  say "step 3/5 frontend -- skipped (--skip-frontend)"
else
  say "step 3/5 frontend web bundle -> $WWW (differential sync)"
  T0=$(date +%s)
  LOCAL_FE="$(mktemp -d "${TMPDIR:-/tmp}/rcfrontend.XXXXXX")"
  trap 'rm -rf "$LOCAL_FE" "$LOCAL_FE".manifest "$LOCAL_FE".device "$LOCAL_FE".push "$LOCAL_FE".del' EXIT
  tar xzf "$PKG_FRONTEND" -C "$LOCAL_FE" || die "cannot unpack $PKG_FRONTEND"
  LOCAL_MAN="$LOCAL_FE.manifest"   # lines: "<relpath> <md5>"
  DEV_MAN="$LOCAL_FE.device"       # lines: "<relpath> <md5>"
  : > "$LOCAL_MAN"
  while IFS= read -r rel; do
    printf '%s %s\n' "$rel" "$(lmd5 "$LOCAL_FE/$rel")" >> "$LOCAL_MAN"
  done < <(cd "$LOCAL_FE" && find . -type f | sed 's|^\./||' | LC_ALL=C sort)
  N_LOCAL=$(wc -l < "$LOCAL_MAN" | tr -d ' ')
  [ "$N_LOCAL" -gt 0 ] || die "frontend package unpacked to 0 files"
  ok "package unpacked locally: $N_LOCAL files, $(du -sh "$LOCAL_FE" | cut -f1)"

  # backup current www BEFORE any write (symlinks stored as-is; cgi-bin included)
  ash "cd /oem/usr && tar cf - www 2>/dev/null | gzip > $STAGE/backups/www.$TS.tar.gz" >/dev/null
  ok "backed up $WWW -> $STAGE/backups/www.$TS.tar.gz"

  # Device-side manifest: top-level regular files + everything under static/.
  # `find` (no -L) never descends the sdcard/usb0/userdata symlinks, and cgi-bin
  # is outside both scopes, so it is never even listed.
  ash "cd $WWW && { find . -maxdepth 1 -type f; find ./static -type f 2>/dev/null; } \
       | sed 's|^\\./||' | while read -r f; do md5sum \"\$f\"; done" \
    | awk 'NF>=2 { h=$1; $1=""; sub(/^[ \t]+/,""); print $0" "h }' | LC_ALL=C sort > "$DEV_MAN"
  ok "device manifest: $(wc -l < "$DEV_MAN" | tr -d ' ') files already under $WWW"

  # ---- diff: which local files must be pushed -------------------------------
  PUSH_LIST="$LOCAL_FE.push"; : > "$PUSH_LIST"
  N_SKIP=0
  while read -r rel md5; do
    dev="$(awk -v p="$rel" '{ r=$0; sub(/ [0-9a-f]*$/,"",r); if (r==p) print $NF }' "$DEV_MAN" | head -1)"
    if [ "$dev" = "$md5" ]; then
      N_SKIP=$((N_SKIP+1))
    else
      echo "$rel" >> "$PUSH_LIST"
    fi
  done < "$LOCAL_MAN"
  N_PUSH=$(wc -l < "$PUSH_LIST" | tr -d ' ')
  PUSH_BYTES=0
  if [ "$N_PUSH" -gt 0 ]; then
    while IFS= read -r rel; do
      sz=$(wc -c < "$LOCAL_FE/$rel" | tr -d ' ')
      PUSH_BYTES=$((PUSH_BYTES+sz))
      printf '     + %s (%s KB)\n' "$rel" "$((sz/1024))"
    done < "$PUSH_LIST"
  fi
  ok "diff: push $N_PUSH file(s) / $((PUSH_BYTES/1024)) KB, skip $N_SKIP identical file(s)"

  # ---- prune: files on the device that the new bundle no longer contains ----
  # HARD LIMITS: only paths under static/ are ever deletable. Top-level files are
  # overlaid, never removed. cgi-bin and the sdcard/usb0/userdata symlinks are
  # rejected explicitly rather than relying on "they aren't under static/".
  DEL_LIST="$LOCAL_FE.del"; : > "$DEL_LIST"
  while read -r rel md5; do
    case "$rel" in
      static/*) ;;
      *) continue;;                                   # top level: overlay only
    esac
    case "$rel" in
      cgi-bin/*|*/cgi-bin/*|sdcard/*|usb0/*|userdata/*|*/..*|/*)
        warn "refusing to consider protected path for deletion: $rel"; continue;;
    esac
    grep -q "^$rel " "$LOCAL_MAN" || echo "$rel" >> "$DEL_LIST"
  done < "$DEV_MAN"
  N_DEL=$(wc -l < "$DEL_LIST" | tr -d ' ')
  if [ "$N_DEL" -gt 0 ]; then
    echo "  stale files to delete under $WWW/static ($N_DEL):"
    sed 's/^/     - /' "$DEL_LIST"
    [ "$N_DEL" -le "$N_LOCAL" ] || die "refusing to delete $N_DEL files (more than the $N_LOCAL files in the new bundle) -- STOP"
    QDEL=""
    while IFS= read -r rel; do QDEL="$QDEL '$WWW/$rel'"; done < "$DEL_LIST"
    ash "rm -f $QDEL" >/dev/null
    ok "pruned $N_DEL stale file(s)"
  else
    ok "no stale files to prune"
  fi

  # ---- push the differing files --------------------------------------------
  if [ "$N_PUSH" -gt 0 ]; then
    DIRS="$(sed 's|/[^/]*$||' "$PUSH_LIST" | grep '/' | LC_ALL=C sort -u || true)"
    if [ -n "$DIRS" ]; then
      QDIR=""; for d in $DIRS; do QDIR="$QDIR '$WWW/$d'"; done
      ash "mkdir -p $QDIR" >/dev/null
    fi
    # read the list on fd 3: `adb push` / `adb shell` inside push_verified read
    # stdin and would otherwise eat the rest of the list after the first file.
    while IFS= read -r rel <&3; do
      push_verified "$LOCAL_FE/$rel" "$WWW/$rel"
    done 3< "$PUSH_LIST"
  else
    ok "device already byte-identical with the bundle -- nothing pushed"
  fi

  # perms: 755 dirs / 644 files, scoped to what we deployed (cgi-bin untouched).
  ash "find $WWW/static -type d -exec chmod 755 {} +; find $WWW/static -type f -exec chmod 644 {} +; \
       cd $WWW && for f in index.html asset-manifest.json favicon.png login-device.svg login-path.svg sensecraft-callback.html; do [ -f \"\$f\" ] && chmod 644 \"\$f\"; done" >/dev/null
  BUNDLE="$(ash "ls $WWW/static/js/ | grep -E '^main\\..*\\.js$' | head -1")"
  ok "frontend deployed in $(( $(date +%s) - T0 ))s (bundle: $BUNDLE)"
fi

# ---- step 4: apps ----------------------------------------------------------
say "step 4/5 app packages -> /userdata/local/apps"
push_verified "$PKG_APPS" "$STAGE/apps.tar.gz"
ash "[ -f /userdata/local/apps/state.json ] && cp -a /userdata/local/apps/state.json $STAGE/backups/state.json.$TS || true" >/dev/null
# Merge-extract: overlays manifest.json + app.py + small configs, PRESERVES the
# large shared model files already on the device (they are not in the package).
ash "mkdir -p /userdata/local/apps && gzip -dc $STAGE/apps.tar.gz | tar -xf - -C /userdata/local/apps" >/dev/null
INSTALLED="$(ash 'for d in /userdata/local/apps/*/; do [ -f "$d/manifest.json" ] && basename "$d"; done | tr "\n" " "')"
ok "apps deployed: $INSTALLED"

# ---- step 5: activate + verify --------------------------------------------
if [ "$NO_ACTIVATE" = "1" ]; then
  say "step 5/5 activate -- skipped (--no-activate)"
else
  say "step 5/5 activate '$ACTIVATE_APP' and verify pipeline"
  ash "curl -s -m 20 -XPOST http://127.0.0.1:8130/api/appMgr/switch -H 'Content-Type: application/json' -d '{\"id\":\"$ACTIVATE_APP\"}'" | sed 's/^/  /'
  sleep 5
  check_vpss
  # probe the :8124 result WebSocket on the device (localhost); the app must be
  # broadcasting inference frames. Bounded by a background kill (no `timeout` on device).
  adb -s "$SERIAL" push "$HERE/ws_probe.py" "$STAGE/ws_probe.py" >/dev/null 2>&1 || \
    adb -s "$SERIAL" push "$HERE/../../kit/ws_probe.py" "$STAGE/ws_probe.py" >/dev/null 2>&1 || true
  PROBE="$(ash "python3 $STAGE/ws_probe.py 127.0.0.1 8124 2 & P=\$!; sleep 18; kill \$P 2>/dev/null; wait \$P 2>/dev/null; true")"
  printf '%s\n' "$PROBE" | sed 's/^/  /'
  if printf '%s' "$PROBE" | grep -q 'msg#1'; then
    ok ":8124 result stream is live (received broadcast frames)"
  else
    warn ":8124 produced no frame within 18s -- app may need a moment or the camera is busy; re-check manually"
  fi
fi

# ---- postflight: rkipc guard ----------------------------------------------
say "postflight"
RKIPC_AFTER="$(ash 'md5sum /oem/usr/bin/rkipc 2>/dev/null | cut -d" " -f1')"
if [ "$RKIPC_AFTER" = "$RKIPC_BEFORE" ]; then
  ok "rkipc UNCHANGED ($RKIPC_AFTER) -- firmware not touched"
else
  die "rkipc md5 CHANGED ($RKIPC_BEFORE -> $RKIPC_AFTER) -- this script must never do this"
fi
check_vpss

printf '\n\033[1;32m=== deploy-app.sh complete: device at v%s application state ===\033[0m\n' "$VER"
echo "  frontend : https://$HOST/  (bundle above)"
echo "  appmgr   : 127.0.0.1:8130  (list OK)"
echo "  active   : $([ "$NO_ACTIVATE" = 1 ] && echo '(unchanged)' || echo "$ACTIVATE_APP -> :8124")"
echo "  backups  : $STAGE/backups/*.$TS"
