"""GET /api/appMgr/assets -- path safety, presence, and the no-rehash guarantee.

Why this test exists
--------------------
Two device incidents (#25/#26) traced to the same root cause: the browser install
flow re-uploaded models that were already on the device byte-for-byte. The 133 MB
voice-transcribe ASR model at the measured ~800 KB/s takes ~166 s, nginx's
`proxy_read_timeout` is 200 s, and the install therefore failed every time.
`assets.query()` is what lets the front end skip an asset it does not need to
send -- so its answers have to be both SAFE and CHEAP:

  * SAFE. `paths=` comes from a query string. If `../../etc/passwd` were
    answerable, this read-only endpoint would become a filesystem oracle behind
    the JWT edge. Every rejection below is a path shape that must never resolve.
  * CHEAP. The endpoint is polled once per install, and hashing 133 MB on this
    CPU costs seconds. INSTALL_ASSETS_SPEC §4 states the criterion literally:
    "同一文件连查两次,第二次不产生新的哈希计算". `assets.hash_computations`
    exists solely so that can be asserted by count instead of by wall clock,
    which is flaky on a loaded box.

A subtler one: the memo key is (size, mtime_ns, inode), not the path. A model
rewritten in place must invalidate -- otherwise a corrupted/updated file keeps
reporting its old digest and the front end skips the very upload that would fix
it. `test_digest_recomputed_after_rewrite` pins that direction.
"""
import os
import shutil
import sys
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_MARKET = os.path.dirname(os.path.dirname(_HERE))
if _MARKET not in sys.path:
    sys.path.insert(0, _MARKET)

# paths.py snapshots the layout from the env at IMPORT time, and pytest imports
# every test module before running any of them -- so whichever module sorts first
# decides where the whole package thinks /userdata is. This file sorts first, so
# it carries the same redirection the other modules do (test_config_merge.py et
# al); without it they would inherit the real /userdata, which is read-only here.
_BASE = tempfile.mkdtemp(prefix="appmgr-assets.")
os.environ.setdefault("APPMGR_APPS_DIR", os.path.join(_BASE, "apps"))
os.environ.setdefault("APPMGR_DIR", os.path.join(_BASE, "appmgr"))
os.environ.setdefault("APPMGR_VENVS_DIR", os.path.join(_BASE, "venvs"))
os.environ.setdefault("APPMGR_APPSTAGE_DIR", os.path.join(_BASE, "appstage"))
os.environ.setdefault("APPMGR_MODEL_ROOTS", os.path.join(_BASE, "models"))

from appmgr import assets, server            # noqa: E402


class AssetsTestBase(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="assetsroot.")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self._prev = os.environ.get("APPMGR_MODELS_DIR")
        os.environ["APPMGR_MODELS_DIR"] = self.root
        self.addCleanup(self._restore_env)
        assets._hash_cache.clear()

    def _restore_env(self):
        if self._prev is None:
            os.environ.pop("APPMGR_MODELS_DIR", None)
        else:
            os.environ["APPMGR_MODELS_DIR"] = self._prev

    def _write(self, rel, data=b"model-bytes"):
        p = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(data)
        return p


class PathSafetyTests(AssetsTestBase):
    """Every one of these must be refused, loudly (400), not answered."""

    BAD = [
        "../../etc/passwd",
        "..",
        "../models/x.rknn",
        "asr/../../../etc/shadow",
        "/etc/passwd",
        "/userdata/local/models/asr/x.rknn",   # absolute, even if inside the root
        "\\etc\\passwd",
        "asr/..\\..\\etc\\passwd",
        # A `..` that normalizes back INSIDE the root, and an absolute path that
        # points INSIDE it. Both are still refused -- and both are refused ONLY
        # by their own explicit check, since the is_within() backstop is happy
        # with either. Without them here, deleting the `..` guard or the
        # absolute-path guard leaves every other case in this list still failing
        # correctly, i.e. the test would not notice the guard was gone.
        "asr/../x.rknn",
        "  ",
        "",
    ]

    def test_traversal_and_absolute_paths_are_rejected(self):
        cases = list(self.BAD) + [
            os.path.join(self.root, "x.rknn"),          # absolute, inside the root
            os.path.join(self.root, "asr", "am.mvn"),
        ]
        for bad in cases:
            with self.assertRaises(assets.AssetPathError,
                                   msg=f"{bad!r} was NOT rejected"):
                assets.resolve(bad)

    def test_query_refuses_the_whole_request_on_a_bad_path(self):
        """One poisoned path must fail the request, not be dropped silently.

        A dropped path reads as "absent" to the caller, which triggers exactly
        the 133 MB re-upload this endpoint exists to prevent.
        """
        self._write("asr/x.rknn")
        with self.assertRaises(assets.AssetPathError):
            assets.query(["asr/x.rknn", "../../etc/passwd"])

    def test_nul_byte_rejected(self):
        with self.assertRaises(assets.AssetPathError):
            assets.resolve("asr/x\0.rknn")

    def test_symlink_escape_rejected(self):
        """A symlink INSIDE the root pointing out of it must not be readable."""
        outside = tempfile.mkdtemp(prefix="outside.")
        self.addCleanup(shutil.rmtree, outside, ignore_errors=True)
        secret = os.path.join(outside, "secret.bin")
        with open(secret, "wb") as f:
            f.write(b"nope")
        os.symlink(secret, os.path.join(self.root, "link.bin"))
        with self.assertRaises(assets.AssetPathError):
            assets.resolve("link.bin")

    def test_sibling_root_prefix_is_not_inside(self):
        """/…/models-evil must not pass as inside /…/models (trailing-sep bug)."""
        self.assertFalse(assets.paths.is_within(self.root + "-evil", self.root))
        self.assertTrue(assets.paths.is_within(
            os.path.join(self.root, "a"), self.root))

    def test_too_many_paths_rejected(self):
        many = [f"m{i}.bin" for i in range(assets.MAX_ASSET_PATHS + 1)]
        with self.assertRaises(assets.AssetPathError):
            assets.query(many)

    def test_good_paths_resolve_inside_the_root(self):
        for ok in ("x.rknn", "asr/x.rknn", "asr/sub/dir/am.mvn", "./asr/x.rknn"):
            self.assertTrue(assets.resolve(ok).startswith(self.root))


class PresenceTests(AssetsTestBase):

    def test_present_and_absent(self):
        data = b"a" * 5000
        self._write("asr/x.rknn", data)
        res = assets.query(["asr/x.rknn", "asr/am.mvn"])

        self.assertEqual(res["root"], self.root)
        got = res["assets"]["asr/x.rknn"]
        self.assertTrue(got["present"])
        self.assertEqual(got["size"], len(data))
        self.assertEqual(len(got["sha256"]), 64)

        self.assertEqual(res["assets"]["asr/am.mvn"], {"present": False})
        self.assertGreater(res["free_bytes"], 0)

    def test_sha256_matches_hashlib(self):
        import hashlib
        data = b"deadbeef" * 999
        self._write("m.bin", data)
        res = assets.query(["m.bin"])
        self.assertEqual(res["assets"]["m.bin"]["sha256"],
                         hashlib.sha256(data).hexdigest())

    def test_directory_at_the_model_name_reads_as_absent(self):
        os.makedirs(os.path.join(self.root, "asr", "x.rknn"))
        self.assertEqual(assets.query(["asr/x.rknn"])["assets"]["asr/x.rknn"],
                         {"present": False})

    def test_free_bytes_survives_a_missing_root(self):
        """Fresh device: /userdata/local/models may not exist yet."""
        os.environ["APPMGR_MODELS_DIR"] = os.path.join(self.root, "not", "there")
        self.assertGreater(assets.free_bytes(), 0)

    @unittest.skipIf(os.geteuid() == 0, "root bypasses the permission bits")
    def test_unreadable_file_omits_sha256_instead_of_inventing_one(self):
        p = self._write("locked.bin", b"x" * 100)
        os.chmod(p, 0o000)
        self.addCleanup(os.chmod, p, 0o644)
        entry = assets.query(["locked.bin"])["assets"]["locked.bin"]
        self.assertTrue(entry["present"])
        self.assertEqual(entry["size"], 100)
        self.assertNotIn("sha256", entry)


class HashCacheTests(AssetsTestBase):

    def _computations(self):
        return assets.hash_computations

    def test_second_query_does_not_rehash(self):
        """INSTALL_ASSETS_SPEC §4: same file queried twice -> one computation."""
        self._write("big.rknn", b"z" * 100000)

        before = self._computations()
        first = assets.query(["big.rknn"])["assets"]["big.rknn"]["sha256"]
        after_first = self._computations()
        self.assertEqual(after_first - before, 1, "first query must hash once")

        second = assets.query(["big.rknn"])["assets"]["big.rknn"]["sha256"]
        self.assertEqual(self._computations(), after_first,
                         "second query recomputed the digest -- the (size, "
                         "mtime_ns, inode) memo is not working")
        self.assertEqual(first, second)

    def test_duplicate_paths_in_one_query_hash_once(self):
        self._write("dup.bin", b"q" * 4096)
        before = self._computations()
        res = assets.query(["dup.bin", "dup.bin", "dup.bin"])
        self.assertEqual(self._computations() - before, 1)
        self.assertTrue(res["assets"]["dup.bin"]["present"])

    def test_digest_recomputed_after_rewrite(self):
        """A model rewritten in place must invalidate the memo.

        Otherwise a corrupted or upgraded file keeps reporting its old digest and
        the front end skips the upload that would repair it.
        """
        p = self._write("m.bin", b"one" * 1000)
        old = assets.query(["m.bin"])["assets"]["m.bin"]["sha256"]

        # Same path, new bytes AND a new size -> new stat key.
        with open(p, "wb") as f:
            f.write(b"two" * 2000)
        before = self._computations()
        new = assets.query(["m.bin"])["assets"]["m.bin"]["sha256"]

        self.assertEqual(self._computations() - before, 1,
                         "rewritten file was served from the memo")
        self.assertNotEqual(old, new)

    def test_cache_is_bounded(self):
        """The memo must not grow without limit on client-supplied paths."""
        prev = assets.MAX_HASH_CACHE
        assets.MAX_HASH_CACHE = 4
        self.addCleanup(setattr, assets, "MAX_HASH_CACHE", prev)
        for i in range(20):
            self._write(f"m{i}.bin", bytes([i]) * 64)
            assets.query([f"m{i}.bin"])
        self.assertLessEqual(len(assets._hash_cache), 4)


class EndpointWiringTests(AssetsTestBase):
    """do_assets() is the HTTP-layer adapter -- comma splitting + 400 mapping."""

    def test_do_assets_splits_on_commas(self):
        self._write("a.bin", b"a")
        res = server.do_assets("a.bin,b.bin")
        self.assertTrue(res["assets"]["a.bin"]["present"])
        self.assertFalse(res["assets"]["b.bin"]["present"])

    def test_do_assets_rejects_empty_and_traversal_as_ValueError(self):
        # The handler maps ValueError -> HTTP 400; AssetPathError is one.
        for bad in ("", "   ", ",,,"):
            with self.assertRaises(ValueError):
                server.do_assets(bad)
        with self.assertRaises(ValueError):
            server.do_assets("../../etc/passwd")


if __name__ == "__main__":
    unittest.main()
