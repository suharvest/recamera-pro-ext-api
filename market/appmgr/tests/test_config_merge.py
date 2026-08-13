"""
Unit tests for appmgr write_user_config MERGE semantics (S1 fix).

write_user_config used to REPLACE config.json with only the posted keys, so a
config POST that changed one parameter silently reset every other overlaid key
back to its manifest default. It now MERGES: read the existing config.json,
overlay the posted keys, write back the union (posted value None removes a key).

The filesystem layout is redirected onto a throwaway temp dir via env BEFORE the
package is imported (paths.py snapshots the env at import time), mirroring
test_hotreload_config.py.

Runnable with plain stdlib: `python3 tests/test_config_merge.py` (or pytest).
"""
import os
import sys
import tempfile
import unittest

_BASE = tempfile.mkdtemp(prefix="appmgr-cfgmerge.")
os.environ["APPMGR_APPS_DIR"] = os.path.join(_BASE, "apps")
os.environ["APPMGR_DIR"] = os.path.join(_BASE, "appmgr")
os.environ["APPMGR_VENVS_DIR"] = os.path.join(_BASE, "venvs")
os.environ["APPMGR_MODEL_ROOTS"] = os.path.join(_BASE, "models")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from appmgr import paths, config as appconfig  # noqa: E402


class WriteUserConfigMergeTests(unittest.TestCase):
    APP = "retail-vision"

    def setUp(self):
        paths.ensure_dirs()
        os.makedirs(paths.app_dir(self.APP), exist_ok=True)
        # start from a clean config.json
        cp = appconfig.config_path(self.APP)
        if os.path.exists(cp):
            os.remove(cp)

    def test_posting_one_key_preserves_others(self):
        """A second POST of a single key must NOT clear the first key."""
        appconfig.write_user_config(self.APP, {"confidence": 0.7, "iou": 0.5})
        appconfig.write_user_config(self.APP, {"confidence": 0.3})  # only conf
        merged = appconfig.load_user_config(self.APP)
        self.assertEqual(merged.get("confidence"), 0.3)   # updated
        self.assertEqual(merged.get("iou"), 0.5)          # PRESERVED (was the bug)

    def test_merge_accumulates_disjoint_keys(self):
        appconfig.write_user_config(self.APP, {"confidence": 0.6})
        appconfig.write_user_config(self.APP, {"dwell_engaged": 4.0})
        appconfig.write_user_config(self.APP, {"dwell_assist": 9.0})
        merged = appconfig.load_user_config(self.APP)
        self.assertEqual(merged.get("confidence"), 0.6)
        self.assertEqual(merged.get("dwell_engaged"), 4.0)
        self.assertEqual(merged.get("dwell_assist"), 9.0)

    def test_none_removes_key(self):
        """Posting a key as None reverts that single overlay to its default."""
        appconfig.write_user_config(self.APP, {"confidence": 0.6, "zone": [[0, 0], [1, 1]]})
        appconfig.write_user_config(self.APP, {"zone": None})
        merged = appconfig.load_user_config(self.APP)
        self.assertEqual(merged.get("confidence"), 0.6)   # untouched
        self.assertNotIn("zone", merged)                  # removed


if __name__ == "__main__":
    unittest.main(verbosity=2)
