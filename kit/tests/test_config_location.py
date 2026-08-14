"""
Unit tests for kit-side resolution of the user config location.

appmgr moved the user's settings OUT of the install dir
(<app_dir>/config.json -> <appdata>/<id>/config.json) because installing a new
version swaps the whole install dir and silently deleted them. The app process
reads through kit.config, so both sides must agree on the path -- and the kit
side must keep reading a not-yet-migrated legacy file so an app that starts
before appmgr ever touches it still sees the user's settings.

kit only READS: it must never move/create files from the app process (appmgr,
which runs as the writer, owns migration).

Runnable with plain stdlib: `python3 kit/tests/test_config_location.py`.
"""
import json
import os
import sys
import tempfile
import unittest

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

from kit import config as kcfg   # noqa: E402

MANIFEST = {
    "id": "retail-vision",
    "config_schema": {
        "confidence": {"type": "number", "apply": "live", "default": 0.35},
        "dwell_engaged": {"type": "number", "apply": "live", "default": 2.0},
    },
}


class ConfigLocationTests(unittest.TestCase):
    def setUp(self):
        base = tempfile.mkdtemp(prefix="kit-cfgloc.")
        self.appdata = os.path.join(base, "appdata")
        self.dir = os.path.join(base, "apps", "retail-vision")
        os.makedirs(self.dir)
        with open(os.path.join(self.dir, "manifest.json"), "w") as f:
            json.dump(MANIFEST, f)
        self._orig_env = os.environ.get("APPMGR_APPDATA_DIR")
        os.environ["APPMGR_APPDATA_DIR"] = self.appdata

    def tearDown(self):
        if self._orig_env is None:
            os.environ.pop("APPMGR_APPDATA_DIR", None)
        else:
            os.environ["APPMGR_APPDATA_DIR"] = self._orig_env

    def _write_appdata(self, cfg, app_id="retail-vision"):
        d = os.path.join(self.appdata, app_id)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "config.json"), "w") as f:
            json.dump(cfg, f)

    def _write_legacy(self, cfg):
        with open(os.path.join(self.dir, "config.json"), "w") as f:
            json.dump(cfg, f)

    # -- no config at all --------------------------------------------------- #
    def test_defaults_when_nothing_saved(self):
        eff = kcfg.effective_config(self.dir)
        self.assertEqual(eff, {"confidence": 0.35, "dwell_engaged": 2.0})

    # -- canonical (appdata) location --------------------------------------- #
    def test_reads_appdata_config(self):
        self._write_appdata({"confidence": 0.77})     # != default 0.35
        self.assertNotEqual(kcfg.flatten_schema(MANIFEST)["confidence"], 0.77)
        eff = kcfg.effective_config(self.dir)
        self.assertEqual(eff["confidence"], 0.77)
        self.assertEqual(eff["dwell_engaged"], 2.0, "untouched key keeps default")

    def test_app_id_comes_from_manifest_not_dir_name(self):
        renamed = os.path.join(os.path.dirname(self.dir), "some-stage-dir")
        os.rename(self.dir, renamed)
        self._write_appdata({"confidence": 0.51})     # keyed by manifest id
        self.assertEqual(kcfg.app_id_of_dir(renamed), "retail-vision")
        self.assertEqual(kcfg.effective_config(renamed)["confidence"], 0.51)

    def test_app_id_falls_back_to_dir_name_without_manifest_id(self):
        d = os.path.join(os.path.dirname(self.dir), "no-id-app")
        os.makedirs(d)
        with open(os.path.join(d, "manifest.json"), "w") as f:
            json.dump({"config_schema": {"confidence": {"default": 0.35}}}, f)
        self._write_appdata({"confidence": 0.42}, app_id="no-id-app")
        self.assertEqual(kcfg.effective_config(d)["confidence"], 0.42)

    # -- legacy fallback ---------------------------------------------------- #
    def test_legacy_in_app_dir_is_still_read(self):
        self._write_legacy({"confidence": 0.63, "dwell_engaged": 6.0})
        eff = kcfg.effective_config(self.dir)
        self.assertEqual(eff["confidence"], 0.63)
        self.assertEqual(eff["dwell_engaged"], 6.0)

    def test_appdata_wins_over_legacy(self):
        self._write_legacy({"confidence": 0.11})
        self._write_appdata({"confidence": 0.88})
        self.assertEqual(kcfg.effective_config(self.dir)["confidence"], 0.88)

    def test_kit_never_writes(self):
        """Reading must not create/move anything (the app process may run as a
        different user than appmgr, and may see a read-only tree)."""
        self._write_legacy({"confidence": 0.63})
        before = sorted(os.listdir(self.dir))
        kcfg.effective_config(self.dir)
        self.assertEqual(sorted(os.listdir(self.dir)), before)
        self.assertFalse(os.path.exists(os.path.join(self.appdata, "retail-vision")),
                         "kit must not create the appdata dir")


if __name__ == "__main__":
    unittest.main(verbosity=2)
