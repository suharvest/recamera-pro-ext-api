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
KIT_PARENT = os.environ.get("APPMGR_KIT_PARENT", "/userdata/local/kit")
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
