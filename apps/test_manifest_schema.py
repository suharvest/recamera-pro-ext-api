"""
Manifest hygiene tests for the in-repo apps.

Pins the invariants the kit/appmgr schema code now relies on:

  * `config_schema` is the CANONICAL GROUPED form (`groups[].items[]`) in every
    app. The flat `{key: spec}` form still parses (deprecated compat branch in
    `kit.config._flat_to_grouped` / `appmgr.config._flat_to_grouped`) but no app
    in this repo may reintroduce it -- two parallel shapes is what forced every
    reader to carry a double branch.
  * no `pipeline` key: nothing reads it, so a published one silently misleads.

Run:  python3 -m pytest apps/test_manifest_schema.py
"""
import glob
import json
import os
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from kit import config as kitconfig                                # noqa: E402

_MANIFESTS = sorted(glob.glob(os.path.join(_ROOT, "apps", "*", "manifest.json")))


def _load(path):
    with open(path) as f:
        return json.load(f)


class ManifestSchemaShapeTests(unittest.TestCase):

    def test_manifests_found(self):
        self.assertGreaterEqual(len(_MANIFESTS), 9, _MANIFESTS)

    def test_config_schema_is_grouped(self):
        for path in _MANIFESTS:
            with self.subTest(app=os.path.basename(os.path.dirname(path))):
                cs = _load(path).get("config_schema")
                self.assertIsInstance(cs, dict)
                self.assertIn("groups", cs,
                              "flat config_schema is deprecated; publish "
                              "config_schema.groups[].items[]")
                for g in cs["groups"]:
                    self.assertIn("items", g)
                    for it in g["items"]:
                        self.assertIn("key", it)

    def test_no_dead_pipeline_field(self):
        for path in _MANIFESTS:
            with self.subTest(app=os.path.basename(os.path.dirname(path))):
                self.assertNotIn("pipeline", _load(path),
                                 "`pipeline` has no consumer; drop it")

    def test_schema_items_reads_every_declared_key(self):
        """The single grouped reader sees the same keys the JSON declares."""
        for path in _MANIFESTS:
            man = _load(path)
            declared = {it["key"]
                        for g in man["config_schema"].get("groups", [])
                        for it in g.get("items", [])}
            with self.subTest(app=os.path.basename(os.path.dirname(path))):
                self.assertEqual(set(kitconfig.schema_items(man)), declared)


class FlatCompatBranchTests(unittest.TestCase):
    """The deprecated flat form must still parse (third-party packages)."""

    def test_flat_schema_still_flattens(self):
        man = {"id": "legacy-flat",
               "config_schema": {"conf": {"type": "number", "default": 0.3}}}
        self.assertEqual(kitconfig.schema_items(man)["conf"]["default"], 0.3)
        self.assertEqual(kitconfig.flatten_schema(man), {"conf": 0.3})


if __name__ == "__main__":
    unittest.main(verbosity=2)
