"""
Unit tests for appmgr.modelstore.write_model -- the browser-relayed shared-model
write primitive. Focus: the destination-root whitelist and every escape it must
refuse, plus the atomic-write + sha256-verify happy/failure paths.

Runnable with plain stdlib: `python3 test_modelstore.py` (or via pytest).
The MODEL_ROOTS whitelist is read from the env at import time, so we point it at
a temp dir BEFORE importing the module.
"""
import hashlib
import os
import sys
import tempfile
import unittest

# Whitelist a throwaway root, then import the module fresh against it.
# Imported as `appmgr.modelstore` (not bare `modelstore`): the module now takes
# its default root from appmgr.paths, so it needs its package context.
_ROOT = tempfile.mkdtemp(prefix="modelroots.")
os.environ["APPMGR_MODEL_ROOTS"] = _ROOT
sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))
from appmgr import modelstore  # noqa: E402

# MODEL_ROOTS is read from the env at import time, and another test module may
# already have imported appmgr.modelstore (via appmgr.server) with the real root
# baked in -- so pin the whitelist explicitly rather than relying on this file
# winning the import race.
modelstore.MODEL_ROOTS = (_ROOT,)


class WriteModelTests(unittest.TestCase):
    def setUp(self):
        # Fresh scratch dir per test; MODEL_ROOTS stays fixed at _ROOT.
        self.root = _ROOT
        self.outside = tempfile.mkdtemp(prefix="outside.")
        self.data = b"hello-model-bytes" * 100
        self.sha = hashlib.sha256(self.data).hexdigest()

    # -- happy paths -------------------------------------------------------- #
    def test_write_into_root_ok(self):
        res = modelstore.write_model(self.root, "am.mvn", self.data, self.sha)
        self.assertEqual(res["sha256"], self.sha)
        self.assertEqual(res["size"], len(self.data))
        self.assertTrue(os.path.isfile(res["path"]))
        with open(res["path"], "rb") as f:
            self.assertEqual(f.read(), self.data)

    def test_write_into_subdir_creates_dir(self):
        target = os.path.join(self.root, "asr")
        res = modelstore.write_model(target, "embedding.npy", self.data, self.sha)
        self.assertEqual(res["path"], os.path.join(target, "embedding.npy"))
        self.assertTrue(os.path.isdir(target))

    def test_write_into_nested_subdir_creates_tree(self):
        # A deeper whitelisted subdir (e.g. .../models/asr/kws) must be accepted
        # and its full tree auto-created -- this is exactly the voice-transcribe
        # KWS case (files land in /userdata/local/models/asr/kws).
        target = os.path.join(self.root, "asr", "kws")
        res = modelstore.write_model(target, "encoder.int8.onnx", self.data, self.sha)
        self.assertEqual(res["path"], os.path.join(target, "encoder.int8.onnx"))
        self.assertTrue(os.path.isdir(target))
        self.assertTrue(os.path.isfile(res["path"]))

    def test_reject_dotdot_escape_from_subdir(self):
        # A subdir path that uses `..` to climb back out of the root is refused
        # even though a prefix of it is whitelisted.
        with self.assertRaises(modelstore.ModelStoreError):
            modelstore.write_model(self.root + "/asr/../../etc", "m.onnx", self.data)

    def test_no_sha_supplied_still_writes(self):
        res = modelstore.write_model(self.root, "x.rknn", self.data)
        self.assertEqual(res["sha256"], self.sha)

    # -- sha256 failure ----------------------------------------------------- #
    def test_sha_mismatch_deletes_file(self):
        with self.assertRaises(modelstore.ModelStoreError):
            modelstore.write_model(self.root, "bad.rknn", self.data, "deadbeef" * 8)
        self.assertFalse(os.path.exists(os.path.join(self.root, "bad.rknn")),
                         "corrupt file must not survive on disk")

    # -- traversal / escape rejection -------------------------------------- #
    def test_reject_dotdot_in_target(self):
        with self.assertRaises(modelstore.ModelStoreError):
            modelstore.write_model(self.root + "/../evil", "m.rknn", self.data)

    def test_reject_absolute_outside_root(self):
        for bad in ("/etc", "/oem/x", "/usr/lib", self.outside):
            with self.assertRaises(modelstore.ModelStoreError):
                modelstore.write_model(bad, "m.rknn", self.data)

    def test_reject_relative_target(self):
        with self.assertRaises(modelstore.ModelStoreError):
            modelstore.write_model("local/models", "m.rknn", self.data)

    def test_reject_filename_with_separator(self):
        for bad in ("../m.rknn", "a/b.rknn", "/abs.rknn", "sub/../m"):
            with self.assertRaises(modelstore.ModelStoreError):
                modelstore.write_model(self.root, bad, self.data)

    def test_reject_dotdot_filename(self):
        with self.assertRaises(modelstore.ModelStoreError):
            modelstore.write_model(self.root, "..", self.data)

    # -- symlink escapes ---------------------------------------------------- #
    def test_reject_symlink_dir_escaping_root(self):
        # A symlink INSIDE the root that points outside must not become a write
        # target -- lexically it's under root, but realpath escapes.
        link = os.path.join(self.root, "sneaky")
        os.symlink(self.outside, link)
        with self.assertRaises(modelstore.ModelStoreError):
            modelstore.write_model(link, "m.rknn", self.data)
        self.assertFalse(os.path.exists(os.path.join(self.outside, "m.rknn")))

    def test_reject_symlink_at_destination(self):
        # A symlink planted at the destination *filename* must not be clobbered.
        dest = os.path.join(self.root, "planted.rknn")
        os.symlink(os.path.join(self.outside, "target"), dest)
        with self.assertRaises(modelstore.ModelStoreError):
            modelstore.write_model(self.root, "planted.rknn", self.data)

    # -- size cap ----------------------------------------------------------- #
    def test_reject_oversize(self):
        orig = modelstore.MAX_MODEL_BYTES
        modelstore.MAX_MODEL_BYTES = 16
        try:
            with self.assertRaises(modelstore.ModelStoreError):
                modelstore.write_model(self.root, "big.rknn", self.data)
        finally:
            modelstore.MAX_MODEL_BYTES = orig

    def test_reject_empty(self):
        with self.assertRaises(modelstore.ModelStoreError):
            modelstore.write_model(self.root, "empty.rknn", b"")


if __name__ == "__main__":
    unittest.main(verbosity=2)
