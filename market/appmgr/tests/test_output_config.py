"""
Unit tests for the injected `output` config_schema group (OUTPUT_SINK_SPEC §3).

`effective_manifest` injects the 8 output keys only for apps that declare
`capabilities:["output"]`; it must be pure + idempotent, and every consumer
(schema_specs / get_config / validate_config / apply-mode) must agree on the
keys. Apps without the capability are untouched (legacy bypass).

Runnable with plain stdlib: `python3 tests/test_output_config.py` (or pytest).
"""
import os
import sys
import tempfile
import unittest

_BASE = tempfile.mkdtemp(prefix="appmgr-outcfg.")
os.environ["APPMGR_APPS_DIR"] = os.path.join(_BASE, "apps")
os.environ["APPMGR_DIR"] = os.path.join(_BASE, "appmgr")
os.environ["APPMGR_VENVS_DIR"] = os.path.join(_BASE, "venvs")
os.environ["APPMGR_MODEL_ROOTS"] = os.path.join(_BASE, "models")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from appmgr import config as appconfig  # noqa: E402


_OUTPUT_KEYS = {"output_channels", "iMode", "dMqtt", "dHttp", "dUart",
                "dTemplate", "output_mapping", "output_filters"}


def _manifest(capabilities, config_schema=None, output=None):
    m = {"name": "T", "capabilities": capabilities}
    if config_schema is not None:
        m["config_schema"] = config_schema
    if output is not None:
        m["output"] = output
    return m


class OutputSchemaInjectionTests(unittest.TestCase):

    def test_bypass_when_no_capability(self):
        man = _manifest([], config_schema={"conf": {"type": "number", "default": 0.25}})
        specs = appconfig.schema_specs(man)
        self.assertIn("conf", specs)
        self.assertFalse(_OUTPUT_KEYS & set(specs))    # nothing injected

    def test_injected_keys_present_when_opted_in(self):
        man = _manifest(["output"],
                        config_schema={"conf": {"type": "number", "default": 0.25}},
                        output={"default_channel": ["mqtt"], "default_mode": "custom"})
        specs = appconfig.schema_specs(man)
        self.assertTrue(_OUTPUT_KEYS <= set(specs))    # all 8 present
        self.assertIn("conf", specs)                   # original preserved
        # defaults flow from the manifest output block
        self.assertEqual(specs["output_channels"]["default"], ["mqtt"])
        self.assertEqual(specs["iMode"]["default"], "custom")

    def test_apply_modes(self):
        man = _manifest(["output"])
        specs = appconfig.schema_specs(man)
        self.assertEqual(specs["dMqtt"]["apply"], "restart")
        self.assertEqual(specs["output_filters"]["apply"], "live")
        self.assertEqual(specs["dTemplate"]["apply"], "live")

    def test_effective_manifest_idempotent(self):
        man = _manifest(["output"], output={"default_channel": ["ws"]})
        once = appconfig.effective_manifest(man)
        twice = appconfig.effective_manifest(once)
        self.assertEqual(len(appconfig.schema_specs(once)),
                         len(appconfig.schema_specs(twice)))
        # original manifest object not mutated
        self.assertNotIn("config_schema", man)

    def test_grouped_schema_gets_output_group(self):
        cs = {"groups": [{"title": "Detection",
                          "items": [{"key": "conf", "type": "number", "default": 0.25}]}]}
        man = _manifest(["output"], config_schema=cs)
        em = appconfig.effective_manifest(man)
        titles = [g.get("title") for g in em["config_schema"]["groups"]]
        self.assertIn("Output", titles)
        specs = appconfig.schema_specs(man)
        self.assertTrue(_OUTPUT_KEYS <= set(specs))

    def test_get_config_exposes_injected_schema(self):
        man = _manifest(["output"], config_schema={"conf": {"type": "number", "default": 0.25}})
        os.makedirs(appconfig.paths.app_dir("t-app"), exist_ok=True)
        resp = appconfig.get_config(man, "t-app")
        # the returned config_schema carries the injected output keys, and their
        # defaults are visible in `values`/`defaults`
        specs = appconfig.schema_specs({"config_schema": resp["config_schema"]})
        self.assertTrue(_OUTPUT_KEYS <= set(specs))
        self.assertIn("dMqtt", resp["defaults"])
        self.assertIn("output_filters", resp["values"])

    def test_validate_accepts_opaque_output_values(self):
        man = _manifest(["output"])
        clean, errors = appconfig.validate_config(man, {
            "iMode": "raw",
            "dMqtt": {"sURL": "127.0.0.1", "iPort": 1883},
            "output_filters": {"only_on_detection": True, "rate_limit_hz": 5},
            "output_channels": ["mqtt", "http"],
        })
        self.assertEqual(errors, [])
        self.assertEqual(clean["iMode"], "raw")
        self.assertEqual(clean["dMqtt"]["sURL"], "127.0.0.1")
        self.assertEqual(clean["output_channels"], ["mqtt", "http"])

    def test_validate_rejects_bad_enum_and_unknown(self):
        man = _manifest(["output"])
        _, errors = appconfig.validate_config(man, {"iMode": "bogus"})
        self.assertTrue(any("iMode" in e for e in errors))
        _, errors2 = appconfig.validate_config(man, {"not_a_key": 1})
        self.assertTrue(any("not_a_key" in e for e in errors2))

    def test_output_keys_rejected_without_capability(self):
        man = _manifest([])          # not opted in -> output keys are unknown
        _, errors = appconfig.validate_config(man, {"dMqtt": {"sURL": "x"}})
        self.assertTrue(any("dMqtt" in e for e in errors))


if __name__ == "__main__":
    unittest.main()
