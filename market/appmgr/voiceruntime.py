"""voiceruntime.py -- on-demand audio runtime for apps with capabilities:["audio"].

Motivation (INSTALL_ASSETS_SPEC §3): voice-transcribe needs five aarch64/cp311
wheels (sherpa_onnx_core, sherpa_onnx, sentencepiece, kaldi_native_fbank,
voxedge, ~18 MB). Putting them in the kit package would make all nine vision apps
re-download 18 MB they never import on every kit update, so the bundle ships
separately and is installed only when an audio app is installed. Without it the
app installs fine and then dies with `ModuleNotFoundError: No module named
'voxedge'`.

Mechanism is deliberately the one that already exists -- release/kit-extra
INSTALL.sh's `pip install --no-index --find-links <wheels>` into /userdata/rknnenv
(the device has no network). The bundle reaches the device through the same
browser-relayed POST /api/appMgr/upload used for app packages, so nothing new is
introduced on the transport side either.

Two properties the spec calls out:

  * PRESENCE IS AN IMPORT, NOT A FILE LISTING. `.dist-info` on disk proves a wheel
    was unpacked, not that it loads: sherpa_onnx pulls a native libonnxruntime out
    of sherpa_onnx_core, and a wheel built for the wrong ABI unpacks perfectly and
    then fails at import. So the check runs `import voxedge, sherpa_onnx` inside
    the TARGET venv's interpreter, as a subprocess.
  * FAILURE NAMES THE PACKAGE. The probe reports which module failed and with what
    error, and install() surfaces pip's own stderr tail, because "runtime install
    failed" on a headless device costs an SSH session to turn into a fact.

Idempotent: install() probes first and returns already_present without running
pip.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile

from . import installer, paths

# Runtime registry. `modules` is what must import; `packages` is what pip is asked
# for (pip project names, resolved offline from the bundle's wheels/ dir). The two
# differ on purpose: sherpa_onnx_core is a dependency that has no import of its own
# worth probing, and sentencepiece/kaldi_native_fbank are pulled in by sherpa_onnx.
RUNTIMES = {
    "voice": {
        "modules": ["voxedge", "sherpa_onnx"],
        "packages": ["voxedge", "sherpa-onnx", "sherpa-onnx-core",
                     "sentencepiece", "kaldi-native-fbank"],
        "capability": "audio",
        "about": "SenseVoice ASR runtime (sherpa-onnx + voxedge), aarch64/cp311",
    },
}

# rknnlite/sherpa native libs live here on the device; INSTALL.sh's own self-check
# sets the same value before importing.
LD_LIBRARY_PATH = os.environ.get("APPMGR_RUNTIME_LD_PATH", "/oem/usr/lib")
PROBE_TIMEOUT = int(os.environ.get("APPMGR_RUNTIME_PROBE_TIMEOUT", "60"))
INSTALL_TIMEOUT = int(os.environ.get("APPMGR_RUNTIME_INSTALL_TIMEOUT", "900"))


class RuntimeError_(Exception):
    """Runtime provisioning failure (named to avoid shadowing the builtin)."""


def _spec(name: str) -> dict:
    spec = RUNTIMES.get((name or "").strip().lower())
    if spec is None:
        raise ValueError(f"unknown runtime {name!r}: known {sorted(RUNTIMES)}")
    return spec


def venv_python() -> str:
    return os.path.join(paths.RKNNENV_DIR, "bin", "python3")


def venv_pip() -> str:
    return os.path.join(paths.RKNNENV_DIR, "bin", "pip")


def _probe_env() -> dict:
    env = dict(os.environ)
    env["LD_LIBRARY_PATH"] = LD_LIBRARY_PATH + ":" + env.get("LD_LIBRARY_PATH", "")
    return env


_PROBE_SRC = """
import json, sys
missing = []
for m in {modules!r}:
    try:
        __import__(m)
    except BaseException as e:
        missing.append({{"module": m, "error": "%s: %s" % (type(e).__name__, e)}})
sys.stdout.write(json.dumps({{"missing": missing, "python": sys.executable}}))
"""


def status(name: str = "voice") -> dict:
    """Is the runtime importable in the target venv?

    Returns {name, venv, present, missing: [{module, error}], ...}. Never raises
    for a missing venv/interpreter -- that is just `present: false` with a reason,
    since "the venv was never created" is a normal fresh-device state the front
    end handles the same way as "the wheels are not installed".
    """
    spec = _spec(name)
    out = {
        "name": name,
        "about": spec["about"],
        "venv": paths.RKNNENV_DIR,
        "modules": list(spec["modules"]),
        "present": False,
        "missing": [],
    }
    py = venv_python()
    if not os.path.isfile(py):
        out["missing"] = [{"module": m, "error": f"venv interpreter missing: {py}"}
                          for m in spec["modules"]]
        out["error"] = f"venv interpreter missing: {py}"
        return out
    try:
        proc = subprocess.run(
            [py, "-c", _PROBE_SRC.format(modules=list(spec["modules"]))],
            capture_output=True, timeout=PROBE_TIMEOUT, env=_probe_env())
    except (OSError, subprocess.SubprocessError) as e:
        out["error"] = f"probe failed to run: {e!r}"
        out["missing"] = [{"module": m, "error": repr(e)} for m in spec["modules"]]
        return out
    text = (proc.stdout or b"").decode("utf-8", "replace").strip()
    try:
        parsed = json.loads(text)
    except ValueError:
        out["error"] = ("probe produced no usable output; stderr="
                        + (proc.stderr or b"").decode("utf-8", "replace")[-500:])
        out["missing"] = [{"module": m, "error": "probe output unparsable"}
                          for m in spec["modules"]]
        return out
    out["missing"] = parsed.get("missing", [])
    out["present"] = not out["missing"]
    return out


def _find_wheel_dir(root: str) -> str:
    """Locate the bundle's wheels/ dir (top level or one level down)."""
    direct = os.path.join(root, "wheels")
    if os.path.isdir(direct):
        return direct
    for entry in sorted(os.listdir(root)):
        cand = os.path.join(root, entry, "wheels")
        if os.path.isdir(cand):
            return cand
    # A bundle that is just a flat pile of .whl files is acceptable too.
    if any(f.endswith(".whl") for f in os.listdir(root)):
        return root
    raise RuntimeError_(
        "runtime bundle contains no wheels/ directory and no .whl files")


def install(name: str = "voice", pkg_path: str = None) -> dict:
    """Install a runtime bundle into the venv, offline. Idempotent.

    `pkg_path` is a device path to voice-runtime-<ver>.tar.gz, normally the value
    POST /api/appMgr/upload just returned; it goes through the app installer's own
    path gate and member vetting (installer.validate_pkg_path / extract_vetted),
    so a hostile bundle cannot write outside the temp dir.

    Returns the post-install status() dict plus {installed, already_present}.
    Raises RuntimeError_ naming the failing step / the modules still missing.
    """
    spec = _spec(name)
    before = status(name)
    if before["present"]:
        before["installed"] = False
        before["already_present"] = True
        return before

    if not pkg_path:
        raise ValueError(
            f"runtime {name!r} is not installed and no bundle path was given "
            "(upload voice-runtime-<ver>.tar.gz via /api/appMgr/upload first); "
            "missing: " + ", ".join(m["module"] for m in before["missing"]))
    installer.validate_pkg_path(pkg_path)

    pip = venv_pip()
    if not os.path.isfile(pip):
        raise RuntimeError_(
            f"venv {paths.RKNNENV_DIR} has no pip ({pip}) -- run "
            "release/kit-extra/INSTALL.sh to create the runtime venv first")

    work = tempfile.mkdtemp(prefix=".runtime.", dir=paths.ensure_appstage())
    try:
        installer.extract_vetted(pkg_path, work)
        wheels = _find_wheel_dir(work)
        cmd = [pip, "install", "--no-index", "--find-links", wheels] + spec["packages"]
        try:
            proc = subprocess.run(cmd, capture_output=True,
                                  timeout=INSTALL_TIMEOUT, env=_probe_env())
        except (OSError, subprocess.SubprocessError) as e:
            raise RuntimeError_(f"pip install failed to run: {e!r}")
        if proc.returncode != 0:
            tail = (proc.stderr or proc.stdout or b"").decode("utf-8", "replace")[-800:]
            raise RuntimeError_(
                f"pip install returned {proc.returncode} for "
                f"{' '.join(spec['packages'])} from {wheels}:\n{tail}")
    finally:
        shutil.rmtree(work, ignore_errors=True)

    after = status(name)
    if not after["present"]:
        detail = "; ".join(f"{m['module']}: {m['error']}" for m in after["missing"])
        raise RuntimeError_(
            f"runtime {name!r} still incomplete after install -- {detail}")
    after["installed"] = True
    after["already_present"] = False
    return after
