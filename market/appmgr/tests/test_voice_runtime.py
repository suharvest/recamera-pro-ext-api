"""On-demand audio runtime -- presence is an import, and installing twice is free.

Why this test exists
--------------------
voice-transcribe installed cleanly on device and then died with
`ModuleNotFoundError: No module named 'voxedge'`: its five aarch64/cp311 wheels
were in no distribution channel at all. voiceruntime.py is that channel, and two
of its properties are easy to regress into something that "looks" right:

  * PRESENCE MUST BE AN IMPORT, NOT A FILE LISTING (INSTALL_ASSETS_SPEC §3.3).
    A wheel built for the wrong ABI unpacks perfectly and leaves a full
    `.dist-info` on disk, and sherpa_onnx only fails when it dlopen()s the native
    libonnxruntime out of sherpa_onnx_core. Anything that checks for files would
    report a runtime that cannot run. The tests below therefore make the probe
    succeed/fail purely through what is importable, never through what exists.
  * IDEMPOTENT (§4). Installing an audio app twice must not re-run pip. Here that
    is asserted the hard way: the fake venv has NO pip binary, so an install()
    that tried would raise instead of quietly succeeding.

Failure messages are also under test. "runtime install failed" on a headless
device costs an SSH session to turn into a fact; the message has to name the
module that is missing.

No network, no device: the "venv" is a shell shim around this interpreter, and
the runtime modules are stub .py files on its PYTHONPATH.
"""
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_MARKET = os.path.dirname(os.path.dirname(_HERE))
if _MARKET not in sys.path:
    sys.path.insert(0, _MARKET)

from appmgr import paths, voiceruntime            # noqa: E402


class FakeVenvTestBase(unittest.TestCase):
    """Builds /tmp/<x>/bin/python3 -- a shim that runs THIS interpreter with a
    controlled PYTHONPATH, so 'is voxedge importable in the venv' is a question
    we can answer both ways without a device."""

    def setUp(self):
        self.venv = tempfile.mkdtemp(prefix="fakevenv.")
        self.addCleanup(shutil.rmtree, self.venv, ignore_errors=True)
        self.site = os.path.join(self.venv, "site")
        os.makedirs(os.path.join(self.venv, "bin"))
        os.makedirs(self.site)

        self._prev_env = paths.RKNNENV_DIR
        paths.RKNNENV_DIR = self.venv
        self.addCleanup(setattr, paths, "RKNNENV_DIR", self._prev_env)

    def _make_python(self):
        # `-S` keeps the DEV BOX's site-packages out of the shim: this machine may
        # well have a (x86/arm mac) sherpa_onnx installed, and without -S the
        # "runtime is absent" cases would pass or fail depending on whose laptop
        # runs the suite. With -S the only importable non-stdlib modules are the
        # stubs we put in self.site.
        py = os.path.join(self.venv, "bin", "python3")
        with open(py, "w") as f:
            f.write("#!/bin/sh\n"
                    f'PYTHONPATH="{self.site}" exec "{sys.executable}" -S "$@"\n')
        os.chmod(py, os.stat(py).st_mode | stat.S_IXUSR | stat.S_IXGRP)
        return py

    def _provide(self, *modules):
        for m in modules:
            with open(os.path.join(self.site, m + ".py"), "w") as f:
                f.write("VERSION = 'stub'\n")


class StatusTests(FakeVenvTestBase):

    def test_absent_when_modules_do_not_import(self):
        self._make_python()                      # venv exists, wheels do not
        st = voiceruntime.status("voice")

        self.assertFalse(st["present"])
        missing = {m["module"] for m in st["missing"]}
        self.assertEqual(missing, {"voxedge", "sherpa_onnx"})
        # The message has to name the module, not just say "failed".
        for m in st["missing"]:
            self.assertIn("ModuleNotFoundError", m["error"])
            self.assertIn(m["module"], m["error"])

    def test_present_when_modules_import(self):
        self._make_python()
        self._provide("voxedge", "sherpa_onnx")
        st = voiceruntime.status("voice")
        self.assertTrue(st["present"], st)
        self.assertEqual(st["missing"], [])
        self.assertEqual(st["venv"], self.venv)

    def test_partial_install_names_only_the_missing_one(self):
        """sherpa_onnx present + voxedge absent is the realistic half-failure."""
        self._make_python()
        self._provide("sherpa_onnx")
        st = voiceruntime.status("voice")
        self.assertFalse(st["present"])
        self.assertEqual([m["module"] for m in st["missing"]], ["voxedge"])

    def test_import_failure_that_is_not_ModuleNotFound_still_counts_as_absent(self):
        """A wheel with the wrong ABI imports and RAISES -- files exist, runtime
        is unusable. This is precisely why presence is not a file check."""
        self._make_python()
        with open(os.path.join(self.site, "voxedge.py"), "w") as f:
            f.write("raise ImportError('libonnxruntime.so: wrong ELF class')\n")
        self._provide("sherpa_onnx")
        st = voiceruntime.status("voice")
        self.assertFalse(st["present"])
        self.assertEqual([m["module"] for m in st["missing"]], ["voxedge"])
        self.assertIn("wrong ELF class", st["missing"][0]["error"])

    def test_missing_venv_is_absent_not_a_crash(self):
        """Fresh device with no /userdata/rknnenv is a normal state."""
        st = voiceruntime.status("voice")          # no bin/python3 created
        self.assertFalse(st["present"])
        self.assertIn("venv interpreter missing", st["error"])

    def test_unknown_runtime_name(self):
        with self.assertRaises(ValueError):
            voiceruntime.status("nope")

    def test_capability_name_resolves_to_the_runtime(self):
        """The store asks by CAPABILITY ("audio"), not by runtime name ("voice").

        It reads `capabilities: ["audio"]` off the catalog entry; making it
        translate that to "voice" would put a capability->runtime table in the
        browser, to rot the next time a runtime is added. Both spellings must
        land on the same registry entry.
        """
        by_name = voiceruntime.status("voice")
        by_cap = voiceruntime.status("audio")
        self.assertEqual(by_cap["modules"], by_name["modules"])
        # ...and the answer names the runtime canonically either way, so the
        # caller never has to guess which spelling it will get back.
        self.assertEqual(by_cap["name"], "voice")
        self.assertEqual(by_name["name"], "voice")
        self.assertEqual(by_cap["capability"], "audio")

    def test_capability_lookup_is_case_and_space_tolerant(self):
        self.assertEqual(voiceruntime.status("  AUDIO ")["name"], "voice")

    def test_unknown_name_error_lists_both_spellings(self):
        with self.assertRaises(ValueError) as cm:
            voiceruntime.status("nope")
        msg = str(cm.exception)
        self.assertIn("voice", msg)
        self.assertIn("audio", msg)


class InstallIdempotenceTests(FakeVenvTestBase):

    def test_second_install_skips_pip(self):
        """INSTALL_ASSETS_SPEC §4: install twice, the second is a no-op.

        The fake venv ships no pip, so an install() that decided to run pip
        anyway would raise RuntimeError_ instead of returning already_present.
        """
        self._make_python()
        self._provide("voxedge", "sherpa_onnx")
        self.assertFalse(os.path.exists(voiceruntime.venv_pip()))

        res = voiceruntime.install("voice", pkg_path="/userdata/appstage/x.tar.gz")
        self.assertTrue(res["already_present"])
        self.assertFalse(res["installed"])
        self.assertTrue(res["present"])

    def test_install_without_bundle_names_the_missing_modules(self):
        self._make_python()
        with self.assertRaises(ValueError) as cm:
            voiceruntime.install("voice", pkg_path=None)
        msg = str(cm.exception)
        self.assertIn("voxedge", msg)
        self.assertIn("sherpa_onnx", msg)

    def test_install_refuses_a_bundle_outside_the_allowed_roots(self):
        """The bundle path goes through the app installer's own gate."""
        self._make_python()                        # not present -> reaches the gate
        bad = tempfile.mkdtemp(prefix="badroot.")
        self.addCleanup(shutil.rmtree, bad, ignore_errors=True)
        pkg = os.path.join(bad, "voice-runtime-1.0.0.tar.gz")
        with open(pkg, "wb") as f:
            f.write(b"not really a tarball")
        from appmgr import installer
        with self.assertRaises(installer.InstallError):
            voiceruntime.install("voice", pkg_path=pkg)


class BundleShapeTests(unittest.TestCase):
    """The build script and the installer must agree on the package list.

    They live in different trees (release/build-voice-runtime.sh vs
    appmgr/voiceruntime.py) and `pip install --no-index` resolves by project
    name: a name in one and not the other is a wheel that never gets installed,
    surfacing on device as ModuleNotFoundError.
    """

    SCRIPT = os.path.join(os.path.dirname(_MARKET), "release",
                          "build-voice-runtime.sh")

    def test_build_script_ships_exactly_what_the_installer_asks_for(self):
        with open(self.SCRIPT, encoding="utf-8") as f:
            src = f.read()
        line = [ln for ln in src.splitlines() if ln.startswith("PKGS=")]
        self.assertTrue(line, "build-voice-runtime.sh no longer defines PKGS")
        shipped = set(line[0].split("=", 1)[1].strip().strip('"').split())
        wanted = set(voiceruntime.RUNTIMES["voice"]["packages"])
        self.assertEqual(shipped, wanted,
                         "release/build-voice-runtime.sh PKGS != "
                         "voiceruntime.RUNTIMES['voice']['packages']")
        self.assertEqual(len(wanted), 5)

    def test_pip_invocation_is_offline(self):
        """The device has no network: --no-index --find-links or nothing."""
        import inspect
        # install() is now a two-line dispatch on the entry's `kind`
        # (RUNTIME_BUNDLE_SPEC §2); the pip invocation this pins lives in the
        # wheels branch.
        src = inspect.getsource(voiceruntime._install_wheels)
        self.assertIn("--no-index", src)
        self.assertIn("--find-links", src)


class PipArgvTests(FakeVenvTestBase):
    """What pip is actually CALLED with -- asserted from argv, not from source.

    The flags here encode a deliberate trade, so they are worth pinning:
    voxedge's metadata floor is `numpy>=1.24` while the device venv has 1.23.5,
    and /userdata/rknnenv is SHARED with rknn-toolkit-lite2 (which is why it is
    1.23.5 in the first place). Letting pip enforce that floor would either abort
    the install or upgrade numpy under the nine vision apps. --no-deps declines
    both; the import probe in status() is what actually decides success, so a
    genuinely missing dependency still fails loudly and by name.
    """

    def setUp(self):
        super().setUp()
        self.calls = os.path.join(self.venv, "pip.argv")
        pip = os.path.join(self.venv, "bin", "pip")
        with open(pip, "w") as f:
            f.write('#!/bin/sh\nprintf "%s\\n" "$@" > ' + self.calls + '\nexit 0\n')
        os.chmod(pip, os.stat(pip).st_mode | stat.S_IXUSR | stat.S_IXGRP)
        self._make_python()
        # The bundle lives under the redirected appstage, so the installer's root
        # gate has to be told about that root too -- otherwise install() refuses
        # the package before pip is ever reached and these tests pass vacuously.
        # realpath: on macOS the temp dir is /var/... which is a symlink to
        # /private/var/..., and the gate resolves the package path before
        # comparing -- an unresolved root here would never match.
        prev = paths.ALLOWED_PKG_ROOTS
        paths.ALLOWED_PKG_ROOTS = tuple(
            set(prev) | {os.path.realpath(paths.APPSTAGE_DIR)})
        self.addCleanup(setattr, paths, "ALLOWED_PKG_ROOTS", prev)
        # This suite asserts the pip ARGV for an UNSIGNED fixture bundle; release-
        # signature verification is covered separately (test_runtime_signature.py).
        # Opt out of the require-signature policy so install() reaches pip.
        prev_req = paths.REQUIRE_SIGNATURE
        paths.REQUIRE_SIGNATURE = False
        self.addCleanup(setattr, paths, "REQUIRE_SIGNATURE", prev_req)

    def _bundle(self):
        """A minimal, well-formed voice-runtime tarball under an allowed root."""
        import tarfile
        d = tempfile.mkdtemp(prefix="bundle.", dir=paths.ensure_appstage())
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        wheels = os.path.join(d, "voice-runtime", "wheels")
        os.makedirs(wheels)
        open(os.path.join(wheels, "voxedge-0.0.9a0-py3-none-any.whl"), "wb").close()
        tgz = os.path.join(d, "voice-runtime-9.9.9.tar.gz")
        with tarfile.open(tgz, "w:gz") as tar:
            tar.add(os.path.join(d, "voice-runtime"), arcname="voice-runtime")
        return tgz

    def _argv(self):
        # The install fails at the post-install import probe (the stub modules are
        # not provided); we only care that pip was reached and with what.
        try:
            voiceruntime.install("voice", self._bundle())
        except Exception as e:
            self._why = f"{type(e).__name__}: {e}"
        self.assertTrue(os.path.isfile(self.calls),
                        "pip was never invoked; install() died first with "
                        + getattr(self, "_why", "<no exception>"))
        with open(self.calls) as f:
            return [ln.rstrip("\n") for ln in f]

    def test_pip_runs_offline_and_without_dependency_resolution(self):
        argv = self._argv()
        self.assertIn("--no-index", argv)
        self.assertIn("--find-links", argv)
        self.assertIn("--no-deps", argv,
                      "without --no-deps pip enforces voxedge's numpy>=1.24 "
                      "against the shared venv's 1.23.5 and the install dies")

    def test_pip_is_never_asked_to_touch_numpy(self):
        """Upgrading numpy would put rknn-toolkit-lite2 (all 9 vision apps) at risk."""
        argv = self._argv()
        self.assertEqual([a for a in argv if "numpy" in a.lower()], [])

    def test_all_five_bundled_packages_are_requested(self):
        argv = self._argv()
        for pkg in voiceruntime.RUNTIMES["voice"]["packages"]:
            self.assertIn(pkg, argv)


if __name__ == "__main__":
    unittest.main()
