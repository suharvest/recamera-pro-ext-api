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
# Per-app virtualenvs (future): apps that ship their own deps get an isolated
# venv here, keyed by id. Not created yet for vision apps (they share the system
# python) -- uninstall removes /userdata/local/venvs/<id> only if it exists.
# NOTE: /userdata/local/models is NOT owned here -- models are SHARED across apps
# (one-gen models[]+target_path), so uninstalling a single app must never touch it.
VENVS_DIR = os.environ.get("APPMGR_VENVS_DIR", "/userdata/local/venvs")
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

APP_ID_RE = re.compile(r"[a-z0-9-]{1,64}")
HTTP_HOST = os.environ.get("APPMGR_HTTP_HOST", "127.0.0.1")
HTTP_PORT = int(os.environ.get("APPMGR_HTTP_PORT", "8130"))


def valid_app_id(app_id: str) -> bool:
    return bool(app_id) and APP_ID_RE.fullmatch(app_id) is not None


def app_dir(app_id: str) -> str:
    return os.path.join(APPS_DIR, app_id)


def venv_dir(app_id: str) -> str:
    return os.path.join(VENVS_DIR, app_id)


def pidfile(app_id: str) -> str:
    return os.path.join(app_dir(app_id), "run.pid")


def logdir(app_id: str) -> str:
    return os.path.join(app_dir(app_id), "logs")


def ensure_dirs() -> None:
    for d in (APPS_DIR, APPMGR_DIR):
        os.makedirs(d, exist_ok=True)


def ensure_appstage() -> str:
    os.makedirs(APPSTAGE_DIR, exist_ok=True)
    return APPSTAGE_DIR
