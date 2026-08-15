"""
supervisor.py -- start / stop an app process. appmgr IS the process supervisor
(no /etc/init.d, no dentry drop -- APP_CENTER_PORT_DESIGN §4.4).

start(id):
  * builds the launch command from manifest (entry + first model + config default)
    and launches it through the kit entry point: `<python> -m kit.run <app_dir>/<entry>`,
  * launches under a NEW session/process group via os.setsid (start_new_session),
  * PYTHONPATH + KIT_PARENT point at the ONE shared kit copy,
  * writes the child pid to <app>/run.pid, redirects stdout/stderr to <app>/logs/app.log.

stop(id):
  * reads run.pid, verifies the pid still belongs to this app (via /proc/<pid>/cmdline),
  * signals the whole PROCESS GROUP: TERM -> grace -> KILL (so ffmpeg children die too),
  * NEVER uses `pkill -f app.py`/`pkill -f python` (would kill the ssh session);
  * additionally `pkill -x ffmpeg` to sweep any stragglers.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from typing import List, Optional

from . import mqtt as mqttcfg, paths, voiceruntime


class SupervisorError(Exception):
    pass


# procfs root. Overridable so the unit tests can point the pid inspectors at a
# fixture tree -- macOS (the dev box) has no /proc at all, and even on Linux you
# cannot conjure a process in an arbitrary state on demand.
PROC_ROOT = os.environ.get("APPMGR_PROC_ROOT", "/proc")


# ---- pid helpers ------------------------------------------------------------ #
def _read_pid(app_id: str) -> Optional[int]:
    pf = paths.pidfile(app_id)
    try:
        with open(pf) as f:
            return int(f.read().strip())
    except (FileNotFoundError, ValueError):
        return None


def _proc_cmdline(pid: int) -> str:
    try:
        with open(f"{PROC_ROOT}/{pid}/cmdline", "rb") as f:
            return f.read().replace(b"\x00", b" ").decode("utf-8", "replace")
    except OSError:
        return ""


def _proc_cwd(pid: int) -> str:
    try:
        return os.readlink(f"{PROC_ROOT}/{pid}/cwd")
    except OSError:
        return ""


def _proc_state(pid: int) -> str:
    """The single-letter process state from /proc/<pid>/stat ("R"/"S"/"D"/"Z"/...).

    Returns "" when procfs is unreadable (pid gone, or no procfs at all, e.g. the
    macOS dev box) -- callers must treat "" as "unknown", never as "dead".

    Parsing note: field 2 (comm) is parenthesised and MAY contain spaces and
    ')' , so the state char is taken as the first token AFTER the LAST ')'.
    """
    try:
        with open(f"{PROC_ROOT}/{pid}/stat", "rb") as f:
            raw = f.read().decode("utf-8", "replace")
    except OSError:
        return ""
    tail = raw[raw.rfind(")") + 1:].split()
    return tail[0] if tail else ""


def _is_zombie(pid: int) -> bool:
    """True only when procfs positively reports state Z (exited, not yet reaped)."""
    return _proc_state(pid) == "Z"


def _is_ours(pid: int, app_id: str) -> bool:
    """PID-reuse guard: the process must belong to THIS app.

    Strong check: its working dir is the app's install dir (we launch with
    cwd=app_dir). Fallback to a cmdline heuristic only if cwd is unreadable --
    since _build_cmd() switched to `python3 -m kit.run <abs app_dir>/app.py`,
    the cmdline DOES contain the app id (it used to be a bare `python3 app.py
    --model models/..`, which named nothing, so the fallback was near-useless).

    An UNREADABLE cwd must not fall through to the realpath comparison: for a
    zombie (and for any pid we cannot introspect) _proc_cwd() returns "", and
    os.path.realpath("") resolves to the APPMGR's OWN cwd -- which would match
    `want` whenever appmgr happened to be started from the app's install dir,
    declaring a corpse "ours and running". Hence the explicit empty guard.
    """
    want = os.path.realpath(paths.app_dir(app_id))
    cwd = _proc_cwd(pid)
    if cwd and os.path.realpath(cwd) == want:
        return True
    cmd = _proc_cmdline(pid)
    return bool(cmd) and app_id in cmd


def _pid_alive(pid: int) -> bool:
    """The pid still occupies a slot in the process table.

    NOTE: a ZOMBIE satisfies this -- kill(pid, 0) succeeds against an unreaped
    corpse. Use _pid_running() for "is it actually executing".
    """
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _pid_running(pid: int) -> bool:
    """Alive AND not a zombie. This is the liveness predicate callers want."""
    return _pid_alive(pid) and not _is_zombie(pid)


def is_running(app_id: str) -> Optional[int]:
    """Return the live pid if this app is running and the pid is really ours.

    A crashed-but-unreaped child (state Z) counts as NOT running: it no longer
    executes, and reporting `running: true` for a corpse would make the UI lie
    and make switch/activate take the "already running" branch.
    """
    pid = _read_pid(app_id)
    if pid is None or not _pid_running(pid):
        return None
    return pid if _is_ours(pid, app_id) else None


# ---- child reaping + last-exit bookkeeping ---------------------------------- #
# appmgr IS the supervisor, so every app it launches is its direct child. Nobody
# ever called waitpid() on them, so a crashed app stayed in the process table as
# `[python] <defunct>` forever (observed on device: pid 4009, ppid 3741=appmgr),
# and the crash itself was completely silent -- only `ps` revealed it.
#
# Split of work, deliberately:
#   * the SIGCHLD handler ONLY calls waitpid(WNOHANG) and appends the raw result
#     to _reaped  -- no file I/O, no locks. A lock would deadlock (the handler
#     runs in the main thread and would block on a lock that same thread holds);
#     file I/O could re-enter a half-written buffered stream.
#   * drain_exits() does the real work (persist last_exit.json, drop the stale
#     pidfile, log) from normal context -- called by reap_and_sweep(), which the
#     read-only endpoints (list/metrics) and stop() invoke.
# list.append / list.pop are single C-level ops, so the queue needs no lock.
#
# SIGCHLD (not polling) because the daemon otherwise sits in select() with no
# tick of its own: a poll loop would need a whole extra thread just to notice a
# crash that the kernel is already telling us about. The handler cost is one
# waitpid syscall per child death.
_reaped: List[tuple] = []


def reap_children() -> int:
    """waitpid(-1, WNOHANG) until drained. Safe to call from a signal handler.

    Returns the number of children reaped. Reaping is process-wide, so it also
    collects short-lived helpers (the `pkill -x ffmpeg` sweep). That is harmless:
    subprocess.Popen tolerates a stolen status (ChildProcessError -> returncode
    0) and drain_exits() ignores pids that match no app pidfile.
    """
    n = 0
    while True:
        try:
            pid, status = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:      # no children at all
            break
        except OSError:
            break
        if pid == 0:                   # children exist, none exited
            break
        _reaped.append((pid, status, time.time()))
        n += 1
    return n


def _sigchld(signum, frame):           # pragma: no cover - trivial, exercised via reap_children
    reap_children()


def install_sigchld() -> bool:
    """Arm the SIGCHLD handler. Only possible from the main thread; returns False
    (rather than raising) elsewhere so importing/serving never breaks."""
    try:
        signal.signal(signal.SIGCHLD, _sigchld)
        return True
    except (ValueError, OSError, AttributeError):
        return False


def describe_status(status: int, at: float = None) -> dict:
    """POSIX wait status -> {"code", "signal", "at"}.

    Killed by a signal N is reported the shell/psutil way: code = -N, signal =
    its name (e.g. {"code": -11, "signal": "SIGSEGV"}).
    """
    ts = time.time() if at is None else at
    if os.WIFSIGNALED(status):
        sig = os.WTERMSIG(status)
        try:
            name = signal.Signals(sig).name
        except ValueError:
            name = f"SIG{sig}"
        return {"code": -sig, "signal": name, "at": ts}
    return {"code": os.WEXITSTATUS(status), "signal": None, "at": ts}


def _app_ids() -> List[str]:
    try:
        names = os.listdir(paths.APPS_DIR)
    except OSError:
        return []
    return [n for n in names
            if paths.valid_app_id(n) and os.path.isdir(paths.app_dir(n))]


def _app_for_pid(pid: int) -> Optional[str]:
    for app_id in _app_ids():
        if _read_pid(app_id) == pid:
            return app_id
    return None


def _write_exit(app_id: str, info: dict) -> None:
    d = paths.app_dir(app_id)
    if not os.path.isdir(d):           # uninstalled meanwhile -- nothing to record
        return
    try:
        with open(paths.exitfile(app_id), "w") as f:
            json.dump(info, f)
    except OSError:
        pass


def _clear_pidfile(app_id: str, pid: int = None) -> None:
    """Remove run.pid, but only if it still names `pid` (never clobber a restart)."""
    if pid is not None and _read_pid(app_id) != pid:
        return
    try:
        os.remove(paths.pidfile(app_id))
    except OSError:
        pass


def last_exit(app_id: str) -> Optional[dict]:
    """The app's most recent recorded process exit, or None if never recorded."""
    try:
        with open(paths.exitfile(app_id)) as f:
            info = json.load(f)
    except (OSError, ValueError):
        return None
    return info if isinstance(info, dict) else None


def drain_exits() -> List[dict]:
    """Turn queued waitpid results into visible state. Normal context only."""
    out = []
    while _reaped:
        try:
            pid, status, ts = _reaped.pop(0)
        except IndexError:             # concurrent drain
            break
        app_id = _app_for_pid(pid)
        if app_id is None:
            continue                   # not one of ours (e.g. the pkill helper)
        info = describe_status(status, ts)
        info["pid"] = pid
        _write_exit(app_id, info)
        _clear_pidfile(app_id, pid)
        out.append(dict(info, app=app_id))
        # No auto-restart: a crash loop must not be hidden behind silent
        # respawns. The app stays stopped and the crash is now visible via
        # /list -> last_exit and this line in the appmgr log.
        print(f"[appmgr] app {app_id} (pid {pid}) exited: "
              f"code={info['code']} signal={info['signal']}", flush=True)
    return out


def sweep_stale() -> List[str]:
    """Drop run.pid files whose process is gone/zombie/not-ours.

    Covers the exits appmgr could NOT waitpid: an app started by a previous
    appmgr instance (or by the CLI) is re-parented to init when its starter goes
    away, so its death never reaches our SIGCHLD. Without this, a stale run.pid
    lingers -- harmless for is_running() (which re-validates the pid) but
    confusing in the logs and on disk.
    """
    cleared = []
    for app_id in _app_ids():
        pid = _read_pid(app_id)
        if pid is None:
            continue
        if _pid_running(pid) and _is_ours(pid, app_id):
            continue
        _clear_pidfile(app_id, pid)
        cleared.append(app_id)
    return cleared


# Minimum spacing between stale-pidfile sweeps on the THROTTLED (read/poll) path.
# Only sweep_stale() is rate-limited -- see reap_and_sweep().
SWEEP_MIN_INTERVAL = float(os.environ.get("APPMGR_SWEEP_MIN_INTERVAL", "1.0"))
_last_sweep = 0.0


def reap_and_sweep(throttle_sweep: bool = False) -> dict:
    """One call for the read paths: reap, publish exits, clean stale pidfiles.

    `throttle_sweep=True` (used by the polled endpoints /list and /metrics) skips
    sweep_stale() when it ran less than SWEEP_MIN_INTERVAL ago. What is throttled
    and what is not, deliberately:

      * reap_children() + drain_exits() ALWAYS run. They are what make a crash
        visible -- drain_exits() writes last_exit.json and drops the dead run.pid
        -- and they are nearly free: one waitpid(WNOHANG) syscall, and a no-op
        when nothing was reaped. Rate-limiting these would make `last_exit` lag.
      * sweep_stale() is throttled. It walks every installed app, and for each
        one with a run.pid does /proc reads (state + cwd + cmdline). Its only
        effect is deleting a run.pid whose process is gone -- pure hygiene:
        is_running() re-validates every pid it reads, so a run.pid that survives
        one extra second never makes the API report a dead app as running.

    Mutating paths (stop(), which calls reap_children()/drain_exits() directly)
    are untouched by the throttle.
    """
    global _last_sweep
    reap_children()
    exits = drain_exits()
    if throttle_sweep:
        now = time.monotonic()
        if (now - _last_sweep) < SWEEP_MIN_INTERVAL:
            return {"exits": exits, "cleared": [], "swept": False}
        _last_sweep = now
    return {"exits": exits, "cleared": sweep_stale(), "swept": True}


# ---- launch command --------------------------------------------------------- #
def _resolve_interpreter(manifest: dict) -> str:
    """Pick the Python interpreter to launch this app under.

    A manifest MAY name a per-app interpreter via `interpreter` (or its alias
    `python`) -- e.g. voice-transcribe needs `/userdata/rknnenv/bin/python`
    because sherpa-onnx lives in that venv, not the system python. Absent the
    field, we use the appmgr's own interpreter (`sys.executable`, the system
    python), so every existing vision app keeps launching exactly as before.

    The path must be ABSOLUTE and exist on the device (it is app-author supplied
    but arrives inside a signature-verified manifest). A bad value is a hard
    error rather than a silent fallback, so misconfiguration surfaces at switch.
    """
    interp = manifest.get("interpreter") or manifest.get("python")
    if not interp:
        return sys.executable
    if not isinstance(interp, str) or not os.path.isabs(interp):
        raise SupervisorError(f"manifest interpreter must be an absolute path: {interp!r}")
    if not os.path.exists(interp):
        raise SupervisorError(f"manifest interpreter not found on device: {interp!r}")
    return interp


def _build_cmd(app_id: str, manifest: dict) -> List[str]:
    """`<interp> <KIT_PARENT>/kit/run.py <app_dir>/<entry> [--model ...] [--sink ws --port N]`

    We launch through the kit's own entry point (kit/run.py) rather than exec'ing
    the entry file directly. kit.run derives KIT_PARENT from its OWN location and
    puts the app dir on sys.path, which is what let every app.py drop its ~40-line
    sys.path bootstrap (internal/KIT_APP_SHAPE_SPEC.md §5.1).

    run.py is invoked by ABSOLUTE PATH, not `-m kit.run`. `-m` has to resolve the
    `kit` package through PYTHONPATH first, so a wrong KIT_PARENT takes down every
    app at once with `ModuleNotFoundError: No module named 'kit'` -- which is
    exactly what happened on device when the bootstrap was removed while
    KIT_PARENT still pointed one level too deep. Running the file directly makes
    the launch self-locating: run.py recovers KIT_PARENT from `__file__`, so the
    apps come up even if PYTHONPATH is misconfigured (device-verified: works with
    PYTHONPATH unset entirely). PYTHONPATH is still exported below for the SDK.

    The entry is passed as an ABSOLUTE path on purpose: it embeds the app id, so
    `/proc/<pid>/cmdline` now names the app outright and _is_ours()'s cmdline
    fallback works even when /proc/<pid>/cwd is unreadable (it used to see only
    `python3 app.py --model models/x.rknn`, which identifies nothing).
    """
    entry = manifest.get("entry", "app.py")
    if ".." in entry.split("/") or entry.startswith("/"):
        raise SupervisorError(f"unsafe entry path {entry!r}")
    models = manifest.get("models") or []
    cmd = [_resolve_interpreter(manifest),
           os.path.join(paths.KIT_DIR, "run.py"),
           os.path.join(paths.app_dir(app_id), entry)]
    # CPU-only apps (e.g. qrcode-reader) declare no models[]; launch without
    # --model. Model-backed apps must still name a real file.
    if models:
        model_file = models[0].get("file")
        if not model_file:
            raise SupervisorError("manifest models[0] has no file")
        cmd += ["--model", model_file]
    # NOTE: conf/iou are intentionally NOT injected here. The app loads its
    # effective config (manifest config_schema defaults overlaid by
    # <app_dir>/config.json) itself via kit.config, so the user's saved config
    # wins. Passing --conf here would clobber config.json on every restart.
    out = manifest.get("output") or {}
    if out.get("sink") == "ws" and out.get("port"):
        cmd += ["--sink", "ws", "--port", str(out["port"])]
    return cmd


def _load_manifest(app_id: str) -> dict:
    mp = os.path.join(paths.app_dir(app_id), "manifest.json")
    with open(mp) as f:
        return json.load(f)


# ---- public API ------------------------------------------------------------- #
def start(app_id: str) -> int:
    if not paths.valid_app_id(app_id):
        raise SupervisorError(f"invalid app id {app_id!r}")
    d = paths.app_dir(app_id)
    if not os.path.isdir(d):
        raise SupervisorError(f"app not installed: {app_id}")

    existing = is_running(app_id)
    if existing:
        return existing

    manifest = _load_manifest(app_id)
    cmd = _build_cmd(app_id, manifest)

    os.makedirs(paths.logdir(app_id), exist_ok=True)
    logpath = os.path.join(paths.logdir(app_id), "app.log")
    logf = open(logpath, "ab", buffering=0)

    env = dict(os.environ)
    env["KIT_PARENT"] = paths.KIT_PARENT
    # kit.config resolves the user config under this root; export appmgr's own
    # value so the writer (appmgr) and the reader (the app) can never disagree.
    env["APPMGR_APPDATA_DIR"] = paths.APPDATA_DIR
    # Kit first, then the extension SDK python dir. recamera_ext lives in
    # /userdata/sdk/python (dropped there by the firmware install.sh) and is NOT
    # inside the rknnenv venv, so apps launched with the venv interpreter would
    # otherwise die on `ModuleNotFoundError: No module named 'recamera_ext'` --
    # which is exactly what a rollback.sh -> install.sh cycle exposes, since the
    # rollback wipes /userdata/rknnenv and the rebuilt venv only gets wheels.
    _pypath = [paths.KIT_PARENT, paths.SDK_PYTHON]
    if env.get("PYTHONPATH"):
        _pypath.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(_pypath)
    env["PYTHONUNBUFFERED"] = "1"
    # librecamera_ext.so.1 (the official extension API shared lib) ships in
    # /oem/usr/lib, which is NOT on the default musl loader search path, so an
    # app pulling in recamera_ext dies with "librecamera_ext.so.1: cannot open
    # shared object file". Inject the vendor lib dirs here so EVERY app the
    # supervisor launches -- whether started from the UI, the HTTP API, or
    # boot-restore -- inherits them, instead of relying on a hand-typed
    # `export LD_LIBRARY_PATH` in an ssh session. Prepend (don't clobber) any
    # inherited value.
    _extlibs = "/oem/usr/lib" + os.pathsep + "/oem/lib"
    env["LD_LIBRARY_PATH"] = _extlibs + (
        os.pathsep + env["LD_LIBRARY_PATH"] if env.get("LD_LIBRARY_PATH") else "")
    # On-demand runtime environment (RUNTIME_BUNDLE_SPEC §3). A file-shaped
    # runtime (the RK hardware codec plugins) is useless once unpacked unless the
    # loader and GStreamer are told where to look, and that cannot be done for
    # every app: GST_PLUGIN_PATH on all nine vision apps would make an unrelated
    # plugin failure everyone's problem. So the variables go ONLY to apps whose
    # manifest declares the capability, and only when the runtime actually probes
    # present -- an app declaring `hwcodec` on a device without the bundle still
    # starts (and falls back to software decode) instead of being blocked here.
    # merge_env appends rather than assigns for the path variables: assigning
    # LD_LIBRARY_PATH would erase the /oem/... entries set six lines above and
    # librockchip_mpp.so.1 would stop resolving.
    voiceruntime.apply_runtime_env(env, manifest.get("capabilities"))
    # Inject global MQTT/HA broker settings when enabled (app publishes WS+MQTT).
    # Empty dict when disabled -> app stays WS-only (unchanged behaviour).
    try:
        env.update(mqttcfg.env_for_launch())
    except Exception:
        pass

    proc = subprocess.Popen(
        cmd,
        cwd=d,
        env=env,
        stdout=logf,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        start_new_session=True,     # setsid: child is session+group leader, pgid == pid
    )
    logf.close()
    with open(paths.pidfile(app_id), "w") as f:
        f.write(str(proc.pid))
    return proc.pid


def reload(app_id: str) -> bool:
    """Hot-reload a running app's config via SIGHUP (DESIGN §3.2/§4).

    Sends SIGHUP to the app's MAIN pid ONLY -- never the process group -- so the
    kit App base loop re-reads config.json in place. ffmpeg children must NOT
    receive SIGHUP (default disposition would terminate them), which is exactly
    why we target the single pid rather than killpg.

    Returns True if the signal was delivered, False if the app is not running
    (in which case the caller has already persisted config.json and there is
    nothing to signal -- the new values apply on the next start).
    """
    if not paths.valid_app_id(app_id):
        raise SupervisorError(f"invalid app id {app_id!r}")
    pid = is_running(app_id)
    if pid is None:
        return False
    try:
        os.kill(pid, signal.SIGHUP)
        return True
    except ProcessLookupError:
        return False


def _killpg(pid: int, sig: int) -> None:
    try:
        os.killpg(os.getpgid(pid), sig)
    except ProcessLookupError:
        pass
    except OSError:
        try:
            os.kill(pid, sig)
        except OSError:
            pass


def stop(app_id: str, grace: float = 5.0) -> dict:
    if not paths.valid_app_id(app_id):
        raise SupervisorError(f"invalid app id {app_id!r}")
    pid = _read_pid(app_id)
    result = {"app": app_id, "pid": pid, "signalled": False, "killed": False}

    # Only signal a process group we own (PID-reuse guard via /proc cwd).
    if pid and _pid_running(pid) and _is_ours(pid, app_id):
        _killpg(pid, signal.SIGTERM)
        result["signalled"] = True
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline:
            # _pid_running (not _pid_alive): once the child dies it becomes a
            # ZOMBIE in OUR process table until reaped, and kill(pid,0) keeps
            # succeeding -- the old loop therefore always burned the full grace
            # window and then SIGKILLed a corpse's (recycled) process group.
            reap_children()
            if not _pid_running(pid):
                break
            time.sleep(0.2)
        if _pid_running(pid):
            _killpg(pid, signal.SIGKILL)
            result["killed"] = True
            time.sleep(0.3)
    # Collect the corpse and publish its exit status before we drop run.pid.
    reap_children()
    drain_exits()

    # sweep any ffmpeg children by EXACT name only (never pkill -f python/app.py).
    try:
        subprocess.run(["pkill", "-x", "ffmpeg"], check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except FileNotFoundError:
        pass

    try:
        os.remove(paths.pidfile(app_id))
    except FileNotFoundError:
        pass
    return result
