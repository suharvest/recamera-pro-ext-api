"""
Unit tests for the kit.run launch command and -- ★the real risk★ -- for appmgr
still RECOGNISING the processes it starts after the command changed.

_build_cmd() used to exec the entry file directly:

    python3 app.py --model models/x.rknn

and now goes through the kit entry point with an ABSOLUTE entry path:

    python3 -m kit.run /userdata/local/apps/<id>/app.py --model models/x.rknn

Two independent pid inspectors read that command line, and both must survive:

  * _is_ours(pid, app_id) -- the PID-reuse guard. Primary evidence is
    /proc/<pid>/cwd (unchanged: we still launch with cwd=app_dir); the fallback
    is `app_id in /proc/<pid>/cmdline`. Under the OLD command that fallback was
    near-useless (the cmdline named no app); under the new one the absolute
    entry path carries the app id, so it now actually discriminates.
  * is_running(app_id) -- _read_pid + not-a-zombie + _is_ours.

The command line used in these assertions is not hand-typed: it is
" ".join(_build_cmd(...)), so the tests cannot drift away from what start()
actually execs. The dev box is macOS (no /proc at all), so the inspectors are
pointed at a fixture procfs tree via supervisor.PROC_ROOT -- the same technique
test_supervisor_reap.py uses.

Run: python3 -m pytest appmgr/tests/test_supervisor_kit_run.py -q   (from market/)
"""
import os
import sys
import tempfile
import unittest

_BASE = tempfile.mkdtemp(prefix="appmgr-kitrun.")
os.environ.setdefault("APPMGR_APPS_DIR", os.path.join(_BASE, "apps"))
os.environ.setdefault("APPMGR_DIR", os.path.join(_BASE, "appmgr"))

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from appmgr import paths, supervisor  # noqa: E402

APP = "yolo-detector"
OTHER = "retail-vision"
MANIFEST = {"id": APP, "entry": "app.py",
            "models": [{"id": "m", "file": "models/x.rknn"}],
            "output": {"sink": "ws", "port": 8124}}


def _fake_proc(root, pid, state, cwd=None, cmdline=""):
    """Minimal /proc/<pid>/{stat,cmdline,cwd} for the pid inspectors.

    cmdline is NUL-separated in the kernel; _proc_cmdline turns those into
    spaces, so writing spaces here yields the same string it would build from a
    real argv.
    """
    d = os.path.join(root, str(pid))
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "stat"), "w") as f:
        f.write("%d (python3) %s 1 1 0 -1 4194560 0 0 0 0\n" % (pid, state))
    with open(os.path.join(d, "cmdline"), "wb") as f:
        f.write(cmdline.replace(" ", "\x00").encode() if cmdline else b"")
    link = os.path.join(d, "cwd")
    if os.path.islink(link) or os.path.exists(link):
        os.remove(link)
    if cwd is not None:
        os.symlink(cwd, link)
    return d


class TestBuildCmd(unittest.TestCase):
    def test_launches_through_kit_run_with_an_absolute_entry(self):
        cmd = supervisor._build_cmd(APP, MANIFEST)
        self.assertEqual(cmd[0], sys.executable)
        self.assertEqual(cmd[1:3], ["-m", "kit.run"])
        self.assertEqual(cmd[3], os.path.join(paths.app_dir(APP), "app.py"))
        self.assertTrue(os.path.isabs(cmd[3]))

    def test_model_and_ws_port_args_are_unchanged(self):
        cmd = supervisor._build_cmd(APP, MANIFEST)
        self.assertEqual(cmd[4:], ["--model", "models/x.rknn",
                                   "--sink", "ws", "--port", "8124"])

    def test_cpu_only_app_gets_no_model_flag(self):
        cmd = supervisor._build_cmd("qrcode-reader", {"id": "qrcode-reader"})
        self.assertNotIn("--model", cmd)
        self.assertEqual(cmd[1:3], ["-m", "kit.run"])

    def test_custom_manifest_entry_is_honoured(self):
        cmd = supervisor._build_cmd(APP, {"entry": "main.py"})
        self.assertEqual(cmd[3], os.path.join(paths.app_dir(APP), "main.py"))

    def test_unsafe_entry_is_still_rejected(self):
        for bad in ("../../etc/passwd", "/etc/passwd"):
            with self.assertRaises(supervisor.SupervisorError):
                supervisor._build_cmd(APP, {"entry": bad})

    def test_the_app_id_is_now_visible_in_the_command_line(self):
        """The property _is_ours()'s cmdline fallback depends on. The OLD
        command (`python3 app.py --model models/x.rknn`) did NOT have it."""
        self.assertIn(APP, " ".join(supervisor._build_cmd(APP, MANIFEST)))


class TestStillRecognisesItsOwnProcess(unittest.TestCase):
    """★Core regression★ for the launch-command change."""

    def setUp(self):
        self.proc = tempfile.mkdtemp(prefix="fakeproc.")
        self._old_root = supervisor.PROC_ROOT
        supervisor.PROC_ROOT = self.proc
        self.pid = 4242
        os.makedirs(paths.app_dir(APP), exist_ok=True)
        os.makedirs(paths.app_dir(OTHER), exist_ok=True)
        with open(paths.pidfile(APP), "w") as f:
            f.write(str(self.pid))
        # exactly what start() execs
        self.cmdline = " ".join(supervisor._build_cmd(APP, MANIFEST))

    def tearDown(self):
        supervisor.PROC_ROOT = self._old_root
        try:
            os.remove(paths.pidfile(APP))
        except OSError:
            pass

    def _patch_alive(self):
        """kill(pid,0) would fail for a made-up pid; liveness is not what this
        class tests, the /proc-based identity judgement is."""
        old = supervisor._pid_alive
        supervisor._pid_alive = lambda pid: True
        self.addCleanup(lambda: setattr(supervisor, "_pid_alive", old))

    def test_cwd_evidence_still_identifies_the_process(self):
        _fake_proc(self.proc, self.pid, "S", cwd=paths.app_dir(APP),
                   cmdline=self.cmdline)
        self.assertTrue(supervisor._is_ours(self.pid, APP))

    def test_cmdline_fallback_identifies_it_when_cwd_is_unreadable(self):
        """A zombie / restricted pid has no readable cwd link. Under the old
        command this fell through to a cmdline that named no app; the absolute
        entry path fixes that."""
        _fake_proc(self.proc, self.pid, "S", cwd=None, cmdline=self.cmdline)
        self.assertEqual(supervisor._proc_cwd(self.pid), "")
        self.assertTrue(supervisor._is_ours(self.pid, APP))

    def test_is_running_returns_the_pid_for_a_process_started_this_way(self):
        self._patch_alive()
        _fake_proc(self.proc, self.pid, "R", cwd=paths.app_dir(APP),
                   cmdline=self.cmdline)
        self.assertEqual(supervisor.is_running(APP), self.pid)

    def test_is_running_still_rejects_a_zombie_started_this_way(self):
        self._patch_alive()
        _fake_proc(self.proc, self.pid, "Z", cwd=paths.app_dir(APP),
                   cmdline=self.cmdline)
        self.assertIsNone(supervisor.is_running(APP))

    def test_another_apps_kit_run_process_is_not_claimed(self):
        """Anti-vacuity: the new cmdline must discriminate, not just contain
        the words `kit.run`. A different app's launch line must NOT match."""
        self._patch_alive()
        other = " ".join(supervisor._build_cmd(OTHER, {"id": OTHER}))
        _fake_proc(self.proc, self.pid, "R", cwd=None, cmdline=other)
        self.assertFalse(supervisor._is_ours(self.pid, APP))
        self.assertIsNone(supervisor.is_running(APP))

    def test_sweep_stale_keeps_a_live_process_started_this_way(self):
        """sweep_stale() shares the same predicate -- if recognition broke, it
        would delete the run.pid of a perfectly healthy app."""
        self._patch_alive()
        _fake_proc(self.proc, self.pid, "S", cwd=paths.app_dir(APP),
                   cmdline=self.cmdline)
        self.assertNotIn(APP, supervisor.sweep_stale())
        self.assertTrue(os.path.exists(paths.pidfile(APP)))


if __name__ == "__main__":
    unittest.main()
