"""
supervisor.py -- start / stop an app process. appmgr IS the process supervisor
(no /etc/init.d, no dentry drop -- APP_CENTER_PORT_DESIGN §4.4).

start(id):
  * builds the launch command from manifest (entry + first model + config default),
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

from . import mqtt as mqttcfg, paths


class SupervisorError(Exception):
    pass


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
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            return f.read().replace(b"\x00", b" ").decode("utf-8", "replace")
    except OSError:
        return ""


def _proc_cwd(pid: int) -> str:
    try:
        return os.readlink(f"/proc/{pid}/cwd")
    except OSError:
        return ""


def _is_ours(pid: int, app_id: str) -> bool:
    """PID-reuse guard: the process must belong to THIS app.

    Strong check: its working dir is the app's install dir (we launch with
    cwd=app_dir). The app_id is NOT in argv (`python3 app.py --model models/..`),
    so cwd is the reliable discriminator. Fallback to a cmdline heuristic only
    if cwd is unreadable.
    """
    want = os.path.realpath(paths.app_dir(app_id))
    if os.path.realpath(_proc_cwd(pid)) == want:
        return True
    cmd = _proc_cmdline(pid)
    return app_id in cmd


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def is_running(app_id: str) -> Optional[int]:
    """Return the live pid if this app is running and the pid is really ours."""
    pid = _read_pid(app_id)
    if pid is None or not _pid_alive(pid):
        return None
    return pid if _is_ours(pid, app_id) else None


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
    entry = manifest.get("entry", "app.py")
    if ".." in entry.split("/") or entry.startswith("/"):
        raise SupervisorError(f"unsafe entry path {entry!r}")
    models = manifest.get("models") or []
    cmd = [_resolve_interpreter(manifest), entry]
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
    if pid and _pid_alive(pid) and _is_ours(pid, app_id):
        _killpg(pid, signal.SIGTERM)
        result["signalled"] = True
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline:
            if not _pid_alive(pid):
                break
            time.sleep(0.2)
        if _pid_alive(pid):
            _killpg(pid, signal.SIGKILL)
            result["killed"] = True
            time.sleep(0.3)

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
