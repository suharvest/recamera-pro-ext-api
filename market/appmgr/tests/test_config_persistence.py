"""
Unit tests for user-config durability across install / upgrade / uninstall.

The bug these lock down: installer.install() swaps the whole
/userdata/local/apps/<id>/ directory, and the user's config.json used to live
INSIDE it -- so every upgrade silently reset thresholds, ROI/lines, output
channels and field mapping to the manifest defaults, with no way back.

Fix under test:
  * config lives at <appdata>/<id>/config.json, outside the package lifecycle;
  * a legacy <app_dir>/config.json is migrated there (idempotently) on first
    read / write / install, and retired as config.json.migrated;
  * upgrading keeps the previous install as <app_dir>.prev (one generation);
  * uninstall KEEPS the user config (purge_config=True opts out).

Everything runs on temp dirs (env is set before the package is imported) and the
packages are real signed-policy-off .tar.gz files, so install() is exercised for
real rather than mocked.
"""
import json
import os
import sys
import tarfile
import tempfile
import unittest

# realpath: on macOS /var is a symlink to /private/var and the installer compares
# the package's REAL path against the allowed roots.
_BASE = os.path.realpath(tempfile.mkdtemp(prefix="appmgr-cfgpersist."))
_APPS = os.path.join(_BASE, "apps")
_APPMGR = os.path.join(_BASE, "appmgr")
_APPDATA = os.path.join(_BASE, "appdata")
_PKGS = os.path.join(_BASE, "pkgs")
os.environ["APPMGR_APPS_DIR"] = _APPS
os.environ["APPMGR_DIR"] = _APPMGR
os.environ["APPMGR_APPDATA_DIR"] = _APPDATA
os.environ["APPMGR_ALLOWED_ROOTS"] = _BASE
os.environ["APPMGR_REQUIRE_SIGNATURE"] = "0"

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from appmgr import config as appconfig, installer, paths  # noqa: E402

APP = "retail-vision"


def manifest(version, extra_schema=None):
    cs = {
        "confidence": {"type": "number", "default": 0.35, "apply": "live"},
        "dwell_engaged": {"type": "number", "default": 2.0, "apply": "live"},
        "zone": {"type": "zone", "apply": "live"},
    }
    cs.update(extra_schema or {})
    return {"id": APP, "version": version, "name": "Retail Vision",
            "config_schema": cs}


def make_pkg(version, extra_schema=None, extra_files=None):
    """Build a real .tar.gz app package under an allowed root."""
    os.makedirs(_PKGS, exist_ok=True)
    src = tempfile.mkdtemp(prefix=f"src-{version}.", dir=_PKGS)
    with open(os.path.join(src, "manifest.json"), "w") as f:
        json.dump(manifest(version, extra_schema), f)
    with open(os.path.join(src, "app.py"), "w") as f:
        f.write(f"# version {version}\n")
    for name, body in (extra_files or {}).items():
        with open(os.path.join(src, name), "w") as f:
            f.write(body)
    pkg = os.path.join(_PKGS, f"{APP}-{version}.tar.gz")
    with tarfile.open(pkg, "w:gz") as tar:
        for name in sorted(os.listdir(src)):
            tar.add(os.path.join(src, name), arcname=name)
    return pkg


class ConfigSurvivesUpgradeTests(unittest.TestCase):
    # paths.py snapshots the layout from the env AT IMPORT TIME, and a sibling
    # test module may have imported it first (pytest imports every module before
    # running anything). So pin the constants per-test and restore after, instead
    # of trusting the env we set above.
    _PINNED = {"APPS_DIR": _APPS, "APPMGR_DIR": _APPMGR, "APPDATA_DIR": _APPDATA,
               "ALLOWED_PKG_ROOTS": (_BASE,), "REQUIRE_SIGNATURE": False,
               "STATE_FILE": os.path.join(_APPS, "state.json")}

    def setUp(self):
        self._saved = {k: getattr(paths, k) for k in self._PINNED}
        for k, v in self._PINNED.items():
            setattr(paths, k, v)
        for d in (_APPS, _APPMGR, _APPDATA):
            os.makedirs(d, exist_ok=True)
        # hard reset between tests
        import shutil
        for d in (paths.app_dir(APP), paths.app_dir(APP) + ".prev",
                  paths.appdata_dir(APP)):
            shutil.rmtree(d, ignore_errors=True)
        paths.ensure_dirs()

    def tearDown(self):
        for k, v in self._saved.items():
            setattr(paths, k, v)

    # -- 1. the headline bug ------------------------------------------------ #
    def test_upgrade_preserves_every_user_key(self):
        installer.install(make_pkg("1.0.0"))
        # user tunes the app: values that are NOT the manifest defaults
        tuned = {"confidence": 0.72, "dwell_engaged": 7.5,
                 "zone": [[0.1, 0.1], [0.9, 0.1], [0.9, 0.9]]}
        defaults = appconfig.schema_defaults(manifest("1.0.0"))
        for k, v in tuned.items():
            self.assertNotEqual(defaults.get(k), v,
                                f"{k} must be tuned AWAY from its default "
                                f"(anti-vacuous-test guard)")
        appconfig.write_user_config(APP, tuned)
        self.assertEqual(appconfig.load_user_config(APP), tuned)

        # upgrade to v2, which also introduces a brand-new key
        m2_extra = {"line_cross": {"type": "number", "default": 3, "apply": "live"}}
        _, man2 = installer.install(make_pkg("2.0.0", m2_extra))
        self.assertEqual(man2["version"], "2.0.0")

        after = appconfig.load_user_config(APP)
        for k, v in tuned.items():
            self.assertEqual(after[k], v, f"{k} lost across upgrade")
        eff = appconfig.effective_values(man2, APP)
        self.assertEqual(eff["confidence"], 0.72)
        self.assertEqual(eff["dwell_engaged"], 7.5)
        self.assertEqual(eff["line_cross"], 3,
                         "new v2 default must layer under the kept user config")

    def test_config_is_not_inside_the_install_dir(self):
        installer.install(make_pkg("1.0.0"))
        appconfig.write_user_config(APP, {"confidence": 0.9})
        self.assertTrue(os.path.isfile(
            os.path.join(_APPDATA, APP, "config.json")))
        self.assertFalse(os.path.isfile(
            os.path.join(paths.app_dir(APP), "config.json")))

    # -- 2. migration of devices that already have a legacy config ---------- #
    def test_legacy_config_migrates_on_read(self):
        installer.install(make_pkg("1.0.0"))
        legacy = appconfig.legacy_config_path(APP)
        with open(legacy, "w") as f:
            json.dump({"confidence": 0.61, "dwell_engaged": 9.0}, f)
        self.assertNotEqual(appconfig.schema_defaults(manifest("1.0.0"))["confidence"],
                            0.61, "legacy value must differ from the default")

        loaded = appconfig.load_user_config(APP)          # triggers migration
        self.assertEqual(loaded, {"confidence": 0.61, "dwell_engaged": 9.0})
        self.assertTrue(os.path.isfile(appconfig.config_path(APP)))
        self.assertFalse(os.path.isfile(legacy), "legacy file must be retired")
        self.assertTrue(os.path.isfile(legacy + ".migrated"), "trace kept")
        # idempotent: a second call changes nothing
        self.assertFalse(appconfig.migrate_legacy_config(APP))
        self.assertEqual(appconfig.load_user_config(APP),
                         {"confidence": 0.61, "dwell_engaged": 9.0})

    def test_legacy_config_migrates_on_upgrade_install(self):
        installer.install(make_pkg("1.0.0"))
        with open(appconfig.legacy_config_path(APP), "w") as f:
            json.dump({"confidence": 0.44}, f)
        installer.install(make_pkg("2.0.0"))              # dir swap happens here
        self.assertEqual(appconfig.load_user_config(APP), {"confidence": 0.44})

    def test_new_location_wins_over_stale_legacy(self):
        installer.install(make_pkg("1.0.0"))
        appconfig.write_user_config(APP, {"confidence": 0.8})
        with open(appconfig.legacy_config_path(APP), "w") as f:
            json.dump({"confidence": 0.1}, f)             # stale leftover
        self.assertEqual(appconfig.load_user_config(APP)["confidence"], 0.8)
        self.assertFalse(os.path.isfile(appconfig.legacy_config_path(APP)))

    def test_corrupt_legacy_is_left_alone(self):
        installer.install(make_pkg("1.0.0"))
        legacy = appconfig.legacy_config_path(APP)
        with open(legacy, "w") as f:
            f.write("{not json")
        self.assertFalse(appconfig.migrate_legacy_config(APP))
        self.assertTrue(os.path.isfile(legacy))
        self.assertEqual(appconfig.load_user_config(APP), {})

    # -- 3. rollback copy --------------------------------------------------- #
    def test_upgrade_keeps_one_prev_generation(self):
        installer.install(make_pkg("1.0.0"))
        prev = paths.app_dir(APP) + ".prev"
        self.assertFalse(os.path.exists(prev), "first install has nothing to keep")
        installer.install(make_pkg("2.0.0"))
        self.assertTrue(os.path.isdir(prev), ".prev rollback copy must be kept")
        with open(os.path.join(prev, "manifest.json")) as f:
            self.assertEqual(json.load(f)["version"], "1.0.0")
        with open(os.path.join(paths.app_dir(APP), "manifest.json")) as f:
            self.assertEqual(json.load(f)["version"], "2.0.0")
        installer.install(make_pkg("3.0.0"))              # only ONE generation
        with open(os.path.join(prev, "manifest.json")) as f:
            self.assertEqual(json.load(f)["version"], "2.0.0")

    def test_prev_dir_is_not_listed_as_an_app(self):
        from appmgr import server
        installer.install(make_pkg("1.0.0"))
        installer.install(make_pkg("2.0.0"))
        ids = [a["id"] for a in server.do_list()["apps"]]
        self.assertIn(APP, ids)
        self.assertNotIn(APP + ".prev", ids)

    # -- 4. uninstall semantics: config is KEPT ----------------------------- #
    def test_uninstall_keeps_user_config_and_reinstall_restores_it(self):
        installer.install(make_pkg("1.0.0"))
        appconfig.write_user_config(APP, {"confidence": 0.66, "dwell_engaged": 8.0})
        installer.uninstall(APP)
        self.assertFalse(os.path.isdir(paths.app_dir(APP)))
        self.assertTrue(os.path.isfile(appconfig.config_path(APP)),
                        "user config must survive uninstall by design")
        installer.install(make_pkg("1.0.0"))
        self.assertEqual(appconfig.load_user_config(APP),
                         {"confidence": 0.66, "dwell_engaged": 8.0})

    def test_uninstall_removes_prev_copy(self):
        installer.install(make_pkg("1.0.0"))
        installer.install(make_pkg("2.0.0"))
        installer.uninstall(APP)
        self.assertFalse(os.path.exists(paths.app_dir(APP) + ".prev"))

    def test_uninstall_purge_config_opt_in(self):
        installer.install(make_pkg("1.0.0"))
        appconfig.write_user_config(APP, {"confidence": 0.66})
        installer.uninstall(APP, purge_config=True)
        self.assertFalse(os.path.exists(paths.appdata_dir(APP)))
        self.assertEqual(appconfig.load_user_config(APP), {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
