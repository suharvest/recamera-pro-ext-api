"""
Unit tests for the kit App SIGHUP config hot-reload machinery (DESIGN §4).

Hardware-free: we never construct a model, frame source, or run the real loop.
We exercise the three moving parts in isolation --

  * SIGHUP handler only FLIPS a flag (no work in the signal handler);
  * _maybe_reload() re-reads the effective config (manifest defaults overlaid by
    config.json) and hands it to on_config_reload;
  * the base on_config_reload default reapplies conf/iou by VALUE only.

app_dir_of() normally derives the install dir from the app class's module file;
here we point it at a throwaway temp dir holding a manifest + config.json.

Runnable with plain stdlib: `python3 kit/tests/test_hotreload.py` (or via pytest)
from the repo root (recamera_pro/).
"""
import json
import os
import signal
import sys
import tempfile
import unittest

# Make `import kit` work when run directly from the repo root or the tests dir.
_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

from kit.app import App          # noqa: E402
from kit import config as kcfg   # noqa: E402


def _write(path, obj):
    with open(path, "w") as f:
        json.dump(obj, f)


class _RecordingApp(App):
    """App subclass that records on_config_reload payloads (override path)."""
    needs_model = False

    def __init__(self):
        super().__init__()
        self.reload_calls = []

    def on_config_reload(self, config):
        self.reload_calls.append(dict(config))
        # still drive the base value-replacement so conf/iou reflect the change
        super().on_config_reload(config)


class HotReloadTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="kit-hotreload.")
        # manifest with apply-annotated live params (mirrors real manifests)
        _write(os.path.join(self.dir, "manifest.json"), {
            "id": "unit-app",
            "config_schema": {
                "conf": {"type": "number", "apply": "live", "default": 0.35},
                "iou": {"type": "number", "apply": "live", "default": 0.45},
            },
        })
        # Point app_dir_of() at our temp dir so _maybe_reload reads THIS config.
        self._orig_app_dir_of = kcfg.app_dir_of
        kcfg.app_dir_of = lambda app: self.dir

    def tearDown(self):
        kcfg.app_dir_of = self._orig_app_dir_of
        # Restore default SIGHUP disposition so a stray signal can't kill pytest.
        try:
            signal.signal(signal.SIGHUP, signal.SIG_DFL)
        except (ValueError, OSError):
            pass

    def _set_config_json(self, **kv):
        _write(os.path.join(self.dir, "config.json"), kv)

    # -- signal handler only flips the flag -------------------------------- #
    def test_sighup_handler_only_sets_flag(self):
        app = _RecordingApp()
        self.assertFalse(app._reload_flag)
        app._on_sighup(signal.SIGHUP, None)
        self.assertTrue(app._reload_flag)
        # handler must NOT have done any reload work itself
        self.assertEqual(app.reload_calls, [])

    # -- _maybe_reload re-reads config and calls the hook ------------------ #
    def test_maybe_reload_rereads_and_invokes_hook(self):
        app = _RecordingApp()
        app.setup(kcfg.effective_config(self.dir))          # defaults: 0.35/0.45
        self.assertEqual(app.reload_calls, [])

        # user edits config.json (as appmgr write_user_config would), then SIGHUP
        self._set_config_json(conf=0.7, iou=0.6)
        app._on_sighup(signal.SIGHUP, None)
        app._maybe_reload()

        self.assertEqual(len(app.reload_calls), 1, "hook called exactly once")
        self.assertAlmostEqual(app.reload_calls[0]["conf"], 0.7)
        self.assertAlmostEqual(app.reload_calls[0]["iou"], 0.6)
        # flag cleared so we don't reload again every frame
        self.assertFalse(app._reload_flag)
        # a second _maybe_reload without a new signal is a no-op
        app._maybe_reload()
        self.assertEqual(len(app.reload_calls), 1)

    # -- base default reapplies conf/iou by value (no override) ------------ #
    def test_base_default_reapplies_conf_iou(self):
        app = App()                     # plain base, no override
        app.needs_model = False
        app.setup(kcfg.effective_config(self.dir))
        self.assertAlmostEqual(app.conf, 0.35)
        self.assertAlmostEqual(app.iou, 0.45)

        self._set_config_json(conf=0.9, iou=0.2)
        app._on_sighup(signal.SIGHUP, None)
        app._maybe_reload()

        self.assertAlmostEqual(app.conf, 0.9, msg="conf hot-reloaded by value")
        self.assertAlmostEqual(app.iou, 0.2, msg="iou hot-reloaded by value")

    # -- real signal delivery flips the flag ------------------------------- #
    def test_real_sighup_delivery_sets_flag(self):
        app = _RecordingApp()
        app._install_reload_handler()   # main thread -> handler installs
        os.kill(os.getpid(), signal.SIGHUP)
        # signal is delivered synchronously on the main thread before this line
        self.assertTrue(app._reload_flag, "delivered SIGHUP set the reload flag")

    # -- a broken hook must not escape into the loop ----------------------- #
    def test_hook_exception_is_swallowed(self):
        class _BadApp(App):
            needs_model = False
            def on_config_reload(self, config):
                raise RuntimeError("boom")
        app = _BadApp()
        self._set_config_json(conf=0.5)
        app._on_sighup(signal.SIGHUP, None)
        app._maybe_reload()             # must not raise
        self.assertFalse(app._reload_flag)


if __name__ == "__main__":
    unittest.main(verbosity=2)
