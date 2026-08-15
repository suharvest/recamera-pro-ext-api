"""
Unit tests for supervisor child-reaping + crash visibility.

Bug being pinned down (observed on device):
    root 4009 (ppid 3741 = appmgr) [python] <defunct>
appmgr launches every app as its DIRECT child but never called waitpid(), so a
crashed app stayed in the process table as a zombie forever -- and the crash was
completely silent (no log line, no API field; only `ps` showed it).

Two independent things are covered here:

  1. REAPING + VISIBILITY (real processes, works on macOS and Linux alike):
     a child that kills itself with SIGSEGV must end up reaped (its pid gone
     from the process table entirely, not `<defunct>`), its run.pid dropped, and
     its wait status recorded as {"code": -11, "signal": "SIGSEGV"}. The signal
     assertion doubles as the anti-vacuity check: if the child had never started
     -- or had exited normally -- the recorded status could not say SIGSEGV.

  2. LIVENESS JUDGEMENT for a process that is dead-but-not-yet-reaped.
     is_running() must answer False for state Z. That verdict reads
     /proc/<pid>/stat, and the dev box is macOS which has NO /proc at all, so
     supervisor.PROC_ROOT is redirected at a fixture tree holding hand-written
     stat/cmdline/cwd entries for a REAL pid. The pid used is a real unreaped
     child, so kill(pid, 0) genuinely succeeds and the False verdict can only
     come from the state byte -- exactly the discrimination that matters on the
     device, where the same code reads the kernel's own procfs.

Runnable with plain stdlib: `python3 tests/test_supervisor_reap.py`.
"""
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest

_BASE = tempfile.mkdtemp(prefix="appmgr-reap.")
_APPS = os.path.join(_BASE, "apps")
_APPMGR = os.path.join(_BASE, "appmgr")
os.environ["APPMGR_APPS_DIR"] = _APPS
os.environ["APPMGR_DIR"] = _APPMGR

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from appmgr import paths, supervisor  # noqa: E402

# Pristine supervisor entry points, snapshotted at import (before any other test
# module's setUp can stub them out). Sibling suites monkeypatch supervisor.* to
# fake process control; this suite is the one that needs the REAL thing.
_ORIG_SUP = {n: getattr(supervisor, n)
             for n in ("is_running", "start", "stop", "reload")}

APP = "crash-app"
SUICIDE = "import os, signal; os.kill(os.getpid(), signal.SIGSEGV)"
IDLE = "import time; time.sleep(60)"


def _mkapp(app_id=APP):
    d = paths.app_dir(app_id)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "manifest.json"), "w") as f:
        f.write('{"id": "%s", "entry": "app.py"}' % app_id)
    return d


def _spawn(code, app_id=APP):
    """Launch a child the way supervisor.start() does and write its run.pid."""
    d = _mkapp(app_id)
    p = subprocess.Popen([sys.executable, "-c", code], cwd=d,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         stdin=subprocess.DEVNULL, start_new_session=True)
    with open(paths.pidfile(app_id), "w") as f:
        f.write(str(p.pid))
    # Mirror supervisor.start(): reaping now consults an app-child registry (only
    # registered pids are waitpid'd, never waitpid(-1)), so a hand-spawned test
    # child must register the same way its production counterpart does.
    supervisor._register_child(p)
    return p


def _pid_in_table(pid):
    """True while the pid occupies a slot -- INCLUDING as an unreaped zombie."""
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _wait_zombie(pid, timeout=10.0):
    """Block until the child is dead but NOT yet reaped (state Z), via ps.

    Deliberately does not use waitpid: reaping is what the code under test must
    do. `ps -o state=` is available on both macOS and Linux.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        out = subprocess.run(["ps", "-o", "state=", "-p", str(pid)],
                             capture_output=True, text=True).stdout.strip()
        if out.startswith("Z"):
            return True
        time.sleep(0.05)
    return False


def _fake_proc(root, pid, state, cwd=None, cmdline=""):
    """Write a minimal /proc/<pid>/{stat,cmdline,cwd} for the pid inspectors.

    The comm field is intentionally nasty -- "(python3 (x)" contains a space and
    an inner ')' -- so the parser is exercised the way a real `(sshd: user)`
    style comm would exercise it on the device.
    """
    d = os.path.join(root, str(pid))
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "stat"), "w") as f:
        f.write("%d (python3 (x)) %s 1 1 0 -1 4194560 0 0 0 0\n" % (pid, state))
    with open(os.path.join(d, "cmdline"), "wb") as f:
        f.write(cmdline.encode() if cmdline else b"")
    link = os.path.join(d, "cwd")
    if os.path.islink(link) or os.path.exists(link):
        os.remove(link)
    if cwd is not None:
        os.symlink(cwd, link)
    return d


class ReapTests(unittest.TestCase):
    def setUp(self):
        for n, v in _ORIG_SUP.items():
            setattr(supervisor, n, v)
        paths.ensure_dirs()
        self._proc_root = supervisor.PROC_ROOT
        self._procdir = tempfile.mkdtemp(prefix="fakeproc.", dir=_BASE)
        del supervisor._reaped[:]
        supervisor._apps.clear()
        self._children = []

    def tearDown(self):
        supervisor.PROC_ROOT = self._proc_root
        for p in self._children:
            try:
                p.kill()
                p.wait(timeout=5)
            except Exception:
                pass
        del supervisor._reaped[:]
        supervisor._apps.clear()
        try:
            os.remove(paths.pidfile(APP))
        except OSError:
            pass
        try:
            os.remove(paths.exitfile(APP))
        except OSError:
            pass

    # -- 1. reaping + crash visibility ------------------------------------- #
    def test_signal_killed_child_is_reaped_and_reported(self):
        p = _spawn(SUICIDE)
        self.assertTrue(_wait_zombie(p.pid),
                        "child never became a zombie -- test would be vacuous")
        # Precondition: this is EXACTLY the leaked state seen on device.
        self.assertTrue(_pid_in_table(p.pid))

        n = supervisor.reap_children()
        self.assertGreaterEqual(n, 1)
        self.assertTrue(any(e[0] == p.pid for e in supervisor._reaped))

        exits = supervisor.drain_exits()
        mine = [e for e in exits if e["pid"] == p.pid]
        self.assertEqual(len(mine), 1, "the crash was not attributed to the app")
        info = mine[0]

        # ANTI-VACUITY: it really died of SIGSEGV, it did not just fail to start
        # (that would surface as a non-zero exit CODE with signal=None).
        self.assertEqual(info["signal"], "SIGSEGV")
        self.assertEqual(info["code"], -int(signal.SIGSEGV))
        self.assertEqual(info["app"], APP)
        self.assertIsInstance(info["at"], float)

        # The zombie is gone from the process table (not merely "not running").
        self.assertFalse(_pid_in_table(p.pid), "pid still in table -> still defunct")
        # run.pid cleaned up, so nothing blocks a restart.
        self.assertFalse(os.path.exists(paths.pidfile(APP)))
        self.assertIsNone(supervisor.is_running(APP))
        # ...and it is readable back from disk for the API layer.
        self.assertEqual(supervisor.last_exit(APP)["signal"], "SIGSEGV")

    def test_normal_exit_is_recorded_with_code_and_no_signal(self):
        p = _spawn("import sys; sys.exit(3)")
        self.assertTrue(_wait_zombie(p.pid))
        supervisor.reap_children()
        info = [e for e in supervisor.drain_exits() if e["pid"] == p.pid][0]
        self.assertEqual(info["code"], 3)
        self.assertIsNone(info["signal"])

    def test_stop_reaps_the_child_it_kills(self):
        p = _spawn(IDLE)
        self._children.append(p)
        # stop() only signals a process it can prove is ours -> needs procfs.
        supervisor.PROC_ROOT = self._procdir
        _fake_proc(self._procdir, p.pid, "S", cwd=paths.app_dir(APP))
        supervisor.stop(APP, grace=3.0)
        self.assertFalse(_pid_in_table(p.pid), "stop() left a zombie behind")
        self.assertFalse(os.path.exists(paths.pidfile(APP)))
        self.assertEqual(supervisor.last_exit(APP)["signal"], "SIGTERM")

    def test_reap_children_without_children_is_a_noop(self):
        self.assertEqual(supervisor.reap_children(), 0)
        self.assertEqual(supervisor.drain_exits(), [])

    def test_drain_ignores_pids_that_match_no_app(self):
        supervisor._reaped.append((987654, 0, time.time()))
        self.assertEqual(supervisor.drain_exits(), [])

    def test_install_sigchld_arms_the_handler(self):
        old = signal.getsignal(signal.SIGCHLD)
        try:
            self.assertTrue(supervisor.install_sigchld())
            self.assertIs(signal.getsignal(signal.SIGCHLD), supervisor._sigchld)
        finally:
            signal.signal(signal.SIGCHLD, old)

    # -- 2. liveness judgement (fixture procfs, so it runs on macOS too) ---- #
    def test_zombie_is_not_reported_running(self):
        """★Core regression★: dead-but-unreaped must NOT read as running."""
        p = _spawn(SUICIDE)
        self.assertTrue(_wait_zombie(p.pid),
                        "child never became a zombie -- test would be vacuous")
        self._children.append(p)
        supervisor.PROC_ROOT = self._procdir
        # cwd + cmdline are the "ours" evidence and BOTH still point at the app,
        # so the only thing that can make the verdict False is the Z state.
        _fake_proc(self._procdir, p.pid, "Z", cwd=paths.app_dir(APP),
                   cmdline="python3 app.py")
        self.assertTrue(_pid_in_table(p.pid), "pid must still exist for this test")
        self.assertTrue(supervisor._pid_alive(p.pid))
        self.assertTrue(supervisor._is_ours(p.pid, APP))
        self.assertTrue(supervisor._is_zombie(p.pid))

        self.assertIsNone(supervisor.is_running(APP))

        # Same pid, same everything, state S -> running again. Proves the
        # fixture is really wired in and that we did not just break liveness.
        _fake_proc(self._procdir, p.pid, "S", cwd=paths.app_dir(APP),
                   cmdline="python3 app.py")
        self.assertEqual(supervisor.is_running(APP), p.pid)

    def test_live_app_is_reported_running(self):
        p = _spawn(IDLE)
        self._children.append(p)
        supervisor.PROC_ROOT = self._procdir
        _fake_proc(self._procdir, p.pid, "R", cwd=paths.app_dir(APP),
                   cmdline="python3 app.py --model models/x.rknn")
        self.assertEqual(supervisor.is_running(APP), p.pid)

    def test_unreadable_proc_never_resolves_to_appmgrs_own_cwd(self):
        """realpath("") == os.getcwd(): an empty cwd link must not mean "ours".

        A zombie's /proc/<pid>/cwd is a dangling link, so _proc_cwd() returns "".
        The old code fed that straight to os.path.realpath(), which resolves ""
        to the CALLER's cwd -- so an appmgr started from inside the app's install
        dir would have declared any corpse (or any pid-reuse squatter) "ours".
        """
        p = _spawn(IDLE)
        self._children.append(p)
        supervisor.PROC_ROOT = self._procdir
        _fake_proc(self._procdir, p.pid, "Z", cwd=None, cmdline="")
        self.assertEqual(supervisor._proc_cwd(p.pid), "")
        old = os.getcwd()
        try:
            os.chdir(paths.app_dir(APP))
            self.assertFalse(supervisor._is_ours(p.pid, APP))
        finally:
            os.chdir(old)
        self.assertIsNone(supervisor.is_running(APP))

    def test_proc_state_parses_comm_containing_spaces_and_parens(self):
        supervisor.PROC_ROOT = self._procdir
        _fake_proc(self._procdir, 4242, "Z")
        self.assertEqual(supervisor._proc_state(4242), "Z")
        _fake_proc(self._procdir, 4242, "S")
        self.assertEqual(supervisor._proc_state(4242), "S")
        # Unknown pid -> "" (unknown), which must NOT be read as a zombie.
        self.assertEqual(supervisor._proc_state(999999), "")
        self.assertFalse(supervisor._is_zombie(999999))

    def test_sweep_clears_pidfile_of_a_vanished_process(self):
        d = _mkapp()
        with open(paths.pidfile(APP), "w") as f:
            f.write("999999")          # never existed / long gone
        self.assertIn(APP, supervisor.sweep_stale())
        self.assertFalse(os.path.exists(paths.pidfile(APP)))
        self.assertTrue(os.path.isdir(d))

    def test_sweep_leaves_a_live_app_alone(self):
        p = _spawn(IDLE)
        self._children.append(p)
        supervisor.PROC_ROOT = self._procdir
        _fake_proc(self._procdir, p.pid, "S", cwd=paths.app_dir(APP))
        self.assertEqual(supervisor.sweep_stale(), [])
        self.assertTrue(os.path.exists(paths.pidfile(APP)))

    @unittest.skipUnless(os.path.isdir("/proc/self"),
                         "real procfs only (device / Linux CI)")
    def test_real_procfs_reports_zombie_state(self):
        """On Linux the fixture-free path must agree: a real zombie reads Z."""
        p = _spawn(SUICIDE)
        self.assertTrue(_wait_zombie(p.pid))
        self._children.append(p)
        self.assertTrue(supervisor._is_zombie(p.pid))
        self.assertIsNone(supervisor.is_running(APP))
        self.assertFalse(supervisor._is_ours(p.pid, APP))


class ApiVisibilityTests(unittest.TestCase):
    """A crash must be visible through the API, not only through `ps`."""

    def setUp(self):
        from appmgr import server, state
        for n, v in _ORIG_SUP.items():
            setattr(supervisor, n, v)
        self.server, self.state = server, state
        paths.ensure_dirs()
        del supervisor._reaped[:]
        supervisor._apps.clear()
        self._proc_root = supervisor.PROC_ROOT

    def tearDown(self):
        supervisor.PROC_ROOT = self._proc_root
        self.state.set_active(None, None)
        for f in (paths.pidfile(APP), paths.exitfile(APP)):
            try:
                os.remove(f)
            except OSError:
                pass

    def _entry(self, listing, app_id=APP):
        for e in listing["apps"]:
            if e["id"] == app_id:
                return e
        self.fail(f"{app_id} missing from do_list()")

    def test_do_list_reaps_and_surfaces_the_crash(self):
        p = _spawn(SUICIDE)
        self.assertTrue(_wait_zombie(p.pid),
                        "child never became a zombie -- test would be vacuous")
        entry = self._entry(self.server.do_list())
        self.assertFalse(entry["running"])
        self.assertIsNone(entry["pid"])
        self.assertEqual(entry["last_exit"]["signal"], "SIGSEGV")
        self.assertEqual(entry["last_exit"]["code"], -int(signal.SIGSEGV))
        self.assertFalse(_pid_in_table(p.pid), "do_list() left the zombie behind")
        self.assertFalse(os.path.exists(paths.pidfile(APP)))

    def test_do_list_last_exit_is_null_when_nothing_ever_died(self):
        _mkapp()
        self.assertIsNone(self._entry(self.server.do_list())["last_exit"])

    def test_do_metrics_exposes_active_app_liveness(self):
        p = _spawn(SUICIDE)
        self.state.set_active(APP, "0.0.1")
        self.assertTrue(_wait_zombie(p.pid))
        m = self.server.do_metrics()
        self.assertEqual(m["active_app"], APP)
        self.assertFalse(m["active_running"])
        self.assertEqual(m["active_last_exit"]["signal"], "SIGSEGV")


if __name__ == "__main__":
    unittest.main()
