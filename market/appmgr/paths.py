"""
paths.py -- filesystem layout + small helpers for appmgr.

All state lives under /userdata (survives OTA per APP_CENTER_PORT_DESIGN §4.7).
Every constant is env-overridable so the module can run against a temp tree on a
dev box (used by the zip-slip unit test).

Device layout (reconciled -- kit is ONE shared copy, apps hold only apps):

    /userdata/local/kit/kit/            shared Kit package  (PYTHONPATH parent = /userdata/local/kit)
    /userdata/local/apps/<id>/          installed app: manifest.json, app.py, models/, run.pid, logs/
    /userdata/local/apps/state.json     { active_app, active_version }
    /userdata/local/appmgr/             appmgr code + locks
"""
from __future__ import annotations

import os
import re

APPS_DIR = os.environ.get("APPMGR_APPS_DIR", "/userdata/local/apps")
# The directory that CONTAINS the `kit` package -- NOT the package itself.
# The kit installer puts the package at /userdata/local/kit and tells you to
# `export PYTHONPATH=/userdata/local` (release/kit-extra/INSTALL.sh:4,95), so the
# parent is /userdata/local. This defaulted to /userdata/local/kit for a long
# time; it was harmless only because every app.py carried a ~40-line sys.path
# bootstrap that probed for the real location. Once that bootstrap was removed
# (KIT_APP_SHAPE_SPEC §5.1) the wrong value killed all 9 apps with
# `ModuleNotFoundError: No module named 'kit'`. test_kit_parent_layout.py pins
# this against the installer so the two cannot drift again.
KIT_PARENT = os.environ.get("APPMGR_KIT_PARENT", "/userdata/local")
KIT_DIR = os.path.join(KIT_PARENT, "kit")     # the package itself
APPMGR_DIR = os.environ.get("APPMGR_DIR", "/userdata/local/appmgr")
# The extension SDK's python package (recamera_ext) is installed OUTSIDE the kit
# tree by the firmware installer, and it is NOT inside /userdata/rknnenv's
# site-packages -- so an app launched with the venv interpreter cannot import it
# unless we put this on PYTHONPATH (or a .pth lands in site-packages).
SDK_PYTHON = os.environ.get("APPMGR_SDK_PYTHON", "/userdata/sdk/python")
# Per-app virtualenvs (future): apps that ship their own deps get an isolated
# venv here, keyed by id. Not created yet for vision apps (they share the system
# python) -- uninstall removes /userdata/local/venvs/<id> only if it exists.
# NOTE: /userdata/local/models is NOT owned here -- models are SHARED across apps
# (one-gen models[]+target_path), so uninstalling a single app must never touch it.
VENVS_DIR = os.environ.get("APPMGR_VENVS_DIR", "/userdata/local/venvs")
# ★User data (config.json) lives OUTSIDE the install dir★. An upgrade replaces
# /userdata/local/apps/<id>/ wholesale (dir swap), so anything the user tuned
# inside it -- thresholds, ROI/lines, output channels + field mapping -- used to
# be wiped on every reinstall. Keeping it here decouples user settings from the
# package lifecycle: install/upgrade/uninstall never touch this tree.
# Legacy configs still sitting in <app_dir>/config.json are migrated here on
# first read/write/install (see config.migrate_legacy_config).
# Default derives from APPS_DIR's parent (/userdata/local/apps -> /userdata/local
# /appdata), so a test tree that only redirects APPMGR_APPS_DIR stays fully
# self-contained instead of writing into the real /userdata.
APPDATA_DIR = os.environ.get(
    "APPMGR_APPDATA_DIR", os.path.join(os.path.dirname(APPS_DIR.rstrip("/")), "appdata"))
# Browser-relayed cloud installs land here first (POST /api/appMgr/upload), then
# do_install() unpacks from this path. It sits under /userdata so it is already
# inside ALLOWED_PKG_ROOTS (the installer refuses packages outside those roots).
APPSTAGE_DIR = os.environ.get("APPMGR_APPSTAGE_DIR", "/userdata/appstage")
# ★Shared model root★ (INSTALL_ASSETS_SPEC §1). Models are NOT owned by any one
# app: several apps reference the same file through manifest `models[]` +
# `target_path`, so they live in one flat tree that install/uninstall never
# touches. This is the `root` reported by GET /api/appMgr/assets, and it is also
# the default destination whitelist for /putModel (modelstore.MODEL_ROOTS derives
# from it, so the "where do models live" answer exists exactly once).
# Env-overridable so the assets unit test can point at a temp tree.
MODELS_DIR = os.environ.get("APPMGR_MODELS_DIR", "/userdata/local/models")
# The Python venv that runs NPU/audio apps, created by release/kit-extra/INSTALL.sh
# (RKNNENV there). On-demand runtimes (INSTALL_ASSETS_SPEC §3) are pip-installed
# into it offline, and "is the runtime present" is answered by importing inside
# it -- never by looking for files.
RKNNENV_DIR = os.environ.get("APPMGR_RKNNENV", "/userdata/rknnenv")

STATE_FILE = os.path.join(APPS_DIR, "state.json")
LOCK_FILE = os.path.join(APPMGR_DIR, "appmgr.lock")   # single-instance (server)
BUSY_FILE = os.path.join(APPMGR_DIR, "busy.lock")     # busy-gate for mutations
AUDIT_LOG = os.path.join(APPMGR_DIR, "audit.log")
MQTT_CONFIG = os.path.join(APPMGR_DIR, "mqtt.json")    # global MQTT/HA broker cfg

# Release-signing trust anchor (APP_CENTER_PORT_DESIGN §4.9 / TODO #4).
# The publisher's PUBLIC key is baked into the appmgr deploy; the matching
# private key never touches repo or device. Packages carry a detached ECDSA
# (P-256, SHA-256) signature over the raw .tar.gz bytes, verified here with the
# device's own openssl before install.
KEYS_DIR = os.environ.get("APPMGR_KEYS_DIR", os.path.join(APPMGR_DIR, "keys"))
RELEASE_PUBKEY = os.environ.get(
    "APPMGR_RELEASE_PUBKEY", os.path.join(KEYS_DIR, "release_pub.pem"))

# Signature policy switch (backward-compat / migration lever):
#   1/true  (default) -- a package with NO signature is REFUSED. A package with
#                        a BAD signature is ALWAYS refused regardless of this.
#   0/false           -- unsigned packages are allowed (audited as a warning);
#                        a present-but-bad signature is still refused.
# Already-installed apps are never re-verified, so flipping this never bricks a
# running device; it only governs NEW installs.
REQUIRE_SIGNATURE = os.environ.get("APPMGR_REQUIRE_SIGNATURE", "1").strip().lower() \
    not in ("0", "false", "no", "off", "")

# Package validation limits (APP_CENTER_PORT_DESIGN §4.9)
ALLOWED_PKG_ROOTS = tuple(
    p for p in os.environ.get("APPMGR_ALLOWED_ROOTS", "/userdata").split(":") if p
)
MAX_PKG_BYTES = int(os.environ.get("APPMGR_MAX_PKG_BYTES", str(200 * 1024 * 1024)))  # 200 MB
MAX_UNPACKED_BYTES = int(os.environ.get("APPMGR_MAX_UNPACKED_BYTES", str(400 * 1024 * 1024)))
MAX_MEMBERS = int(os.environ.get("APPMGR_MAX_MEMBERS", "4096"))

# ★Package-bundled app icon★ (RENDER_DECLARATION_SPEC §5 P0-1).
# manifest's `"image": "/appcenter/apps/<id>.png"` is a dead URL on the device:
# that nginx alias serves /userdata/local/appcenter/apps/, which holds only
# install packages + the catalog, while the app itself is unpacked to
# /userdata/local/apps/<id>/. A package may therefore ship its own `icon.<ext>`
# at the tar's top level; appmgr serves it from the install dir via
# GET /api/appMgr/icon?id=<id>, so third-party apps stop depending on the
# front-end's hard-coded image bundle.
# SVG is deliberately NOT allowed: it is an active document (script/xlink) and
# we serve it same-origin behind the JWT edge.
ICON_EXTS = (".png", ".webp", ".jpg", ".jpeg")
ICON_CONTENT_TYPES = {
    ".png": "image/png",
    ".webp": "image/webp",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
}
MAX_ICON_BYTES = int(os.environ.get("APPMGR_MAX_ICON_BYTES", str(1024 * 1024)))  # 1 MiB

APP_ID_RE = re.compile(r"[a-z0-9-]{1,64}")
HTTP_HOST = os.environ.get("APPMGR_HTTP_HOST", "127.0.0.1")
HTTP_PORT = int(os.environ.get("APPMGR_HTTP_PORT", "8130"))


def is_within(path: str, root: str) -> bool:
    """True iff `path` is `root` itself or lives under it.

    The single containment predicate behind every "did this escape?" check in
    appmgr -- installer's zip-slip member vetting, modelstore's /putModel root
    whitelist and assets.py's model-path resolution. Both arguments must already
    be normalized/realpath'd by the caller: this function is purely lexical, it
    does not touch the filesystem, so it cannot see through symlinks on its own.
    Kept in one place because three copies of `startswith(root)` is exactly how a
    trailing-slash bug (`/userdata/local/models-evil` passing as inside
    `/userdata/local/models`) gets fixed in two of them.
    """
    root = root.rstrip("/") or "/"
    return path == root or path.startswith(root + os.sep)


def valid_app_id(app_id: str) -> bool:
    return bool(app_id) and APP_ID_RE.fullmatch(app_id) is not None


def app_dir(app_id: str) -> str:
    return os.path.join(APPS_DIR, app_id)


def venv_dir(app_id: str) -> str:
    return os.path.join(VENVS_DIR, app_id)


def appdata_dir(app_id: str) -> str:
    """Per-app user-data dir (survives install/upgrade/uninstall)."""
    return os.path.join(APPDATA_DIR, app_id)


def icon_file(app_id: str):
    """Absolute path of the app's bundled icon, or None if it ships none.

    Extensions are probed in ICON_EXTS order so a package shipping both
    icon.webp and icon.png gets a deterministic answer. Returns None (never
    raises) for an unknown/invalid id -- callers turn that into a 404 / a null
    icon_url and the front end falls back to its own placeholder.
    """
    if not valid_app_id(app_id):
        return None
    d = app_dir(app_id)
    for ext in ICON_EXTS:
        p = os.path.join(d, "icon" + ext)
        if os.path.isfile(p):
            return p
    return None


def pidfile(app_id: str) -> str:
    return os.path.join(app_dir(app_id), "run.pid")


def readyfile(app_id: str) -> str:
    """Where a freshly launched app signals it reached its main loop (READY).

    supervisor.start() clears this before launch, injects its path as
    APPMGR_READY_FILE, and waits for the app (kit.run_app) to create it once
    start() has loaded models / opened the sink / bound the frame source. Its
    presence is what lets a switch/upgrade COMMIT `active` only after the process
    is actually up, instead of the moment Popen returned (§lifecycle). Lives in
    the install dir next to run.pid so an upgrade/uninstall drops it too."""
    return os.path.join(app_dir(app_id), "run.ready")


def logdir(app_id: str) -> str:
    return os.path.join(app_dir(app_id), "logs")


def exitfile(app_id: str) -> str:
    """Where supervisor records the app's LAST process exit (code/signal/ts).

    Sits next to run.pid inside the install dir: it describes this installation's
    process lifecycle (not user data), so an upgrade/uninstall correctly drops it.
    """
    return os.path.join(app_dir(app_id), "last_exit.json")


def ensure_dirs() -> None:
    for d in (APPS_DIR, APPMGR_DIR):
        os.makedirs(d, exist_ok=True)


def ensure_appstage() -> str:
    os.makedirs(APPSTAGE_DIR, exist_ok=True)
    return APPSTAGE_DIR
