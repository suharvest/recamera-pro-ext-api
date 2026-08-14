"""
Tests for gen_catalog._build_models -- the shared-model resolver that turns a
models.json entry into catalog `models[]` entries.

Covers:
  * the real voice-transcribe entry: 10 entries across TWO target dirs (asr +
    asr/kws), each entry's sha256/size equal to the actual staged bytes, and the
    KWS files' URLs carrying the `kws/` staging segment;
  * per-entry structure is exactly {url, filename, sha256, size, target_path}
    (unchanged shape so the closed-source frontend's per-entry putModel loop
    keeps working);
  * backward compatibility: the OLD single-target form {target_path, files}
    still resolves.

Runnable with plain stdlib: `python3 test_gen_catalog.py` (or via pytest).
"""
import hashlib
import json
import os
import sys
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
import gen_catalog  # noqa: E402

MODELS_DIR = os.path.normpath(os.path.join(_HERE, "..", "packaging", "models"))
BASE = "https://cdn.example/recamera_pro/models/"
ENTRY_KEYS = {"url", "filename", "sha256", "size", "target_path"}


def _sha_size(path):
    h = hashlib.sha256()
    n = 0
    with open(path, "rb") as f:
        for c in iter(lambda: f.read(1 << 20), b""):
            h.update(c)
            n += len(c)
    return h.hexdigest(), n


class CapabilitiesForwardingTests(unittest.TestCase):
    """`capabilities` must survive manifest -> catalog.

    The store decides BEFORE install whether an app needs something the device
    may not have (`"audio"` -> the ~18 MB voice runtime, INSTALL_ASSETS_SPEC
    §3.1). It can only see the catalog. voice-transcribe's manifest has carried
    `["audio", "output"]` all along, but build_catalog listed a fixed set of keys
    and this was not among them, so the front end's check could never fire.
    """
    DIST = os.path.normpath(os.path.join(_HERE, "..", "packaging", "dist"))

    def _catalog(self):
        return gen_catalog.build_catalog(self.DIST, "https://cdn.example/pkgs/")

    def test_voice_transcribe_keeps_audio_capability(self):
        apps = {a["id"]: a for a in self._catalog()["apps"]}
        self.assertIn("voice-transcribe", apps, "voice-transcribe not in dist/")
        self.assertEqual(apps["voice-transcribe"].get("capabilities"),
                         ["audio", "output"])

    def test_catalog_never_invents_capabilities(self):
        """Only apps whose manifest declares them get the key at all."""
        for app in self._catalog()["apps"]:
            pkg = os.path.join(self.DIST, app["package"]["filename"])
            declared = gen_catalog._read_manifest(pkg).get("capabilities")
            if isinstance(declared, list):
                self.assertEqual(app.get("capabilities"), declared, app["id"])
            else:
                self.assertNotIn("capabilities", app, app["id"])

    def test_non_list_capabilities_is_dropped_not_forwarded(self):
        """A malformed manifest must not put a string where the UI expects a list."""
        with tempfile.TemporaryDirectory() as d:
            man = {"id": "bogus-app", "name": "Bogus", "version": "0.0.1",
                   "capabilities": "audio"}          # string, not a list
            pkg = os.path.join(d, "bogus-app-0.0.1-arm64.tar.gz")
            import io
            import tarfile as _tf
            blob = json.dumps(man).encode()
            with _tf.open(pkg, "w:gz") as tar:
                ti = _tf.TarInfo("manifest.json")
                ti.size = len(blob)
                tar.addfile(ti, io.BytesIO(blob))
            app = gen_catalog.build_catalog(d, "https://cdn.example/pkgs/")["apps"][0]
            self.assertNotIn("capabilities", app)


class BuildModelsTests(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(_HERE, "models.json")) as f:
            self.spec = {k: v for k, v in json.load(f).items()
                         if not k.startswith("_")}

    def test_voice_transcribe_two_groups(self):
        models = gen_catalog._build_models(
            "voice-transcribe", self.spec, MODELS_DIR, BASE)
        # 5 (asr) + 5 (asr/kws) = 10 entries.
        self.assertEqual(len(models), 10)

        by_target = {}
        for m in models:
            self.assertEqual(set(m), ENTRY_KEYS, f"entry shape changed: {set(m)}")
            by_target.setdefault(m["target_path"], []).append(m)

        self.assertEqual(set(by_target), {
            "/userdata/local/models/asr",
            "/userdata/local/models/asr/kws",
        })
        self.assertEqual(len(by_target["/userdata/local/models/asr"]), 5)
        self.assertEqual(len(by_target["/userdata/local/models/asr/kws"]), 5)

    def test_sha256_and_url_match_staged(self):
        models = gen_catalog._build_models(
            "voice-transcribe", self.spec, MODELS_DIR, BASE)
        app_stage = os.path.join(MODELS_DIR, "voice-transcribe")
        for m in models:
            kws = m["target_path"].endswith("/kws")
            staged = os.path.join(app_stage, "kws" if kws else "", m["filename"])
            sha, size = _sha_size(staged)
            self.assertEqual(m["sha256"], sha, m["filename"])
            self.assertEqual(m["size"], size, m["filename"])
            # KWS files carry the staging subdir in their URL; others do not.
            expect = BASE + "voice-transcribe/" + ("kws/" if kws else "") + m["filename"]
            self.assertEqual(m["url"], expect)

    def test_backward_compatible_single_target(self):
        # The pre-groups form must still resolve. Stage a throwaway file and
        # point an old-style entry at it.
        tmp = tempfile.mkdtemp(prefix="models_bc.")
        app_stage = os.path.join(tmp, "legacy-app")
        os.makedirs(app_stage)
        with open(os.path.join(app_stage, "model.rknn"), "wb") as f:
            f.write(b"legacy-bytes" * 50)
        spec = {"legacy-app": {
            "target_path": "/userdata/local/models",
            "files": ["model.rknn"],
        }}
        models = gen_catalog._build_models("legacy-app", spec, tmp, BASE)
        self.assertEqual(len(models), 1)
        m = models[0]
        self.assertEqual(set(m), ENTRY_KEYS)
        self.assertEqual(m["target_path"], "/userdata/local/models")
        self.assertEqual(m["url"], BASE + "legacy-app/model.rknn")

    def test_absent_app_yields_empty(self):
        self.assertEqual(gen_catalog._build_models("nope", self.spec, MODELS_DIR, BASE), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
