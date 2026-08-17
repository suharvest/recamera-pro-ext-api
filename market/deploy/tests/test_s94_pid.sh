#!/bin/sh
# Functional test for S94appmgr's pid resolution (#27).
#
# The bug: /var/run/appmgr.pid goes stale two ways -- deploy-app.sh recorded
# `$!` of a `setsid ... &` (setsid forks when it is already a process-group
# leader, so that pid dies at once), and /var/run is tmpfs so a pid can later be
# REUSED. `is_running` only did `kill -0`, which cannot tell any of that apart:
# `restart` became a silent no-op and `stop` aimed SIGKILL at a bystander.
#
# Needs a real /proc, so it runs in a Linux container, not on the Mac. Use a
# glibc image (dash): busybox refuses to run under the python3 argv[0] alias the
# fake below relies on ("applet not found").
#   docker run --rm -v $PWD/S94appmgr:/tmp/S94appmgr:ro \
#     -v $PWD/tests/test_s94_pid.sh:/tmp/test_s94_pid.sh:ro \
#     debian:bookworm-slim sh /tmp/test_s94_pid.sh
#
# Case D carries a reverse control (R): it asserts the OLD logic really does
# accept the bystander. Without that, D would pass even if it proved nothing.
#
# A fake "appmgr" is any process whose argv[0] looks like python and whose
# /proc/<pid>/cmdline contains "-m appmgr serve".  A /tmp/python3 -> sh symlink
# run as `/tmp/python3 -c 'sleep 300' -m appmgr serve` gives exactly that: the
# trailing words land in argv as $0/$1/$2 and show up in cmdline.

SCRIPT=${SCRIPT:-/tmp/S94appmgr}
PIDFILE=/var/run/appmgr.pid
fail=0
ck() { # ck <name> <expected> <actual>
  if [ "$2" = "$3" ]; then echo "PASS $1"; else echo "FAIL $1: want [$2] got [$3]"; fail=1; fi
}

# Pull resolve_pid/is_appmgr_pid out of the script without executing its case block.
sed -n '/^MODULE_SIG=/,/^is_running() {/p' "$SCRIPT" | sed '$d' > /tmp/fns.sh
. /tmp/fns.sh

# is_appmgr_pid also requires argv[0] to look like a python interpreter (so the
# shell running a restart, whose cmdline quotes the same words, is never hit).
ln -sf /bin/sh /tmp/python3
/tmp/python3 -c 'sleep 300' -m appmgr serve &
FAKE=$!
sleep 0.3
sh -c 'sleep 300' &
OTHER=$!            # a live, unrelated process -- stands in for a REUSED pid
sleep 0.3

echo "--- fake appmgr=$FAKE  unrelated=$OTHER"

# A. pidfile correct
echo "$FAKE" > $PIDFILE
ck "A pidfile-correct" "$FAKE" "$(resolve_pid)"

# B. pidfile dead (the setsid `$!` case) -> must still find it by scanning
echo "999999" > $PIDFILE
ck "B pidfile-dead-falls-back-to-scan" "$FAKE" "$(resolve_pid)"

# C. pidfile missing entirely
rm -f $PIDFILE
ck "C pidfile-missing-falls-back-to-scan" "$FAKE" "$(resolve_pid)"

# D. pid REUSE: pidfile points at a live but unrelated process.
#    Must NOT return it -- the old code would have, and then SIGKILLed it.
echo "$OTHER" > $PIDFILE
got=$(resolve_pid)
ck "D pid-reuse-not-mistaken-for-appmgr" "$FAKE" "$got"
if kill -0 "$OTHER" 2>/dev/null; then echo "PASS D2 bystander-still-alive"; else echo "FAIL D2 bystander was killed"; fail=1; fi

# REVERSE CONTROL: the OLD logic (kill -0 on the pidfile, no identity check)
# accepts the bystander. If this does not hold, case D proves nothing.
old_is_running() { [ -f $PIDFILE ] && kill -0 "$(cat $PIDFILE 2>/dev/null)" 2>/dev/null; }
if old_is_running; then echo "PASS R old-logic-DOES-accept-bystander (bug reproduced)"; else echo "FAIL R old logic rejected it; test is vacuous"; fail=1; fi

# E. nothing running
kill $FAKE 2>/dev/null; sleep 0.5
rm -f $PIDFILE
ck "E none-running" "" "$(resolve_pid)"

# F. status must exit 3 when not running (LSB)
sh $SCRIPT status >/dev/null 2>&1; ck "F status-exit-3" "3" "$?"

kill $OTHER 2>/dev/null
echo "=== $([ $fail -eq 0 ] && echo ALL PASS || echo FAILURES)"
exit $fail
