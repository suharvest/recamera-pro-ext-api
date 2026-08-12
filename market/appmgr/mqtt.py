"""
mqtt.py -- global MQTT / Home Assistant broker configuration for appmgr.

One broker config is shared by every self-hosted app (they all publish to the
same Home Assistant instance). It lives at /userdata/local/appmgr/mqtt.json and
is surfaced by the loopback API as GET/POST /api/appMgr/mqtt.

When enabled, the supervisor injects the settings as RECAMERA_MQTT_* env vars
into each app it launches; kit.app.run_app reads them and adds an MqttSink
alongside the WS sink. Default is DISABLED -> apps publish WS only (unchanged
behaviour). stdlib only (appmgr must not import the kit package).

mqtt.json schema (all optional; defaults applied on read):
    {
      "enabled": false,
      "host": "",
      "port": 1883,
      "username": "",
      "password": "",
      "base_topic": "recamera",
      "discovery_prefix": "homeassistant"
    }
"""
from __future__ import annotations

import json
import os
import tempfile
from typing import Any, Dict, Tuple

from . import paths

DEFAULTS: Dict[str, Any] = {
    "enabled": False,
    "host": "",
    "port": 1883,
    "username": "",
    "password": "",
    "base_topic": "recamera",
    "discovery_prefix": "homeassistant",
}


def load() -> Dict[str, Any]:
    """Return the stored config merged onto DEFAULTS (never raises)."""
    cfg = dict(DEFAULTS)
    try:
        with open(paths.MQTT_CONFIG) as f:
            data = json.load(f)
        if isinstance(data, dict):
            for k in DEFAULTS:
                if k in data:
                    cfg[k] = data[k]
    except (OSError, ValueError):
        pass
    return cfg


def public_view() -> Dict[str, Any]:
    """Config for the API response -- password redacted to a boolean flag."""
    cfg = load()
    out = dict(cfg)
    out["password_set"] = bool(cfg.get("password"))
    out.pop("password", None)
    return out


def validate(incoming: dict) -> Tuple[Dict[str, Any], list]:
    """Validate/coerce an incoming partial config. Returns (clean_full, errors).

    `clean_full` is the complete new config (stored value merged with incoming),
    so callers may PATCH just the fields they change. A password is only changed
    when the incoming body carries a non-null "password"; omitting it preserves
    the existing secret (so the redacted GET can round-trip through a POST)."""
    errors: list = []
    if not isinstance(incoming, dict):
        return dict(load()), ["config must be an object"]
    cur = load()
    out = dict(cur)

    if "enabled" in incoming:
        if not isinstance(incoming["enabled"], bool):
            errors.append("enabled: expected boolean")
        else:
            out["enabled"] = incoming["enabled"]
    if "host" in incoming:
        if not isinstance(incoming["host"], str):
            errors.append("host: expected string")
        else:
            out["host"] = incoming["host"].strip()
    if "port" in incoming:
        p = incoming["port"]
        if not isinstance(p, int) or isinstance(p, bool) or not (1 <= p <= 65535):
            errors.append("port: expected integer 1..65535")
        else:
            out["port"] = p
    if "username" in incoming:
        if not isinstance(incoming["username"], str):
            errors.append("username: expected string")
        else:
            out["username"] = incoming["username"]
    if "password" in incoming and incoming["password"] is not None:
        if not isinstance(incoming["password"], str):
            errors.append("password: expected string")
        else:
            out["password"] = incoming["password"]
    for k in ("base_topic", "discovery_prefix"):
        if k in incoming:
            if not isinstance(incoming[k], str) or not incoming[k].strip():
                errors.append(f"{k}: expected non-empty string")
            else:
                out[k] = incoming[k].strip().rstrip("/")

    # Enabling requires a broker host.
    if out.get("enabled") and not out.get("host"):
        errors.append("host: required when enabled=true")
    return out, errors


def save(cfg: Dict[str, Any]) -> None:
    """Atomically persist the full config (temp file + fsync + rename)."""
    d = paths.APPMGR_DIR
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".mqtt.", dir=d)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump({k: cfg.get(k, DEFAULTS[k]) for k in DEFAULTS}, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, paths.MQTT_CONFIG)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def env_for_launch() -> Dict[str, str]:
    """RECAMERA_MQTT_* env vars to inject into an app process, or {} if disabled."""
    cfg = load()
    if not cfg.get("enabled") or not cfg.get("host"):
        return {}
    return {
        "RECAMERA_MQTT_ENABLED": "1",
        "RECAMERA_MQTT_HOST": str(cfg["host"]),
        "RECAMERA_MQTT_PORT": str(cfg.get("port", 1883)),
        "RECAMERA_MQTT_USERNAME": str(cfg.get("username", "")),
        "RECAMERA_MQTT_PASSWORD": str(cfg.get("password", "")),
        "RECAMERA_MQTT_BASE_TOPIC": str(cfg.get("base_topic", "recamera")),
        "RECAMERA_MQTT_DISCOVERY_PREFIX": str(cfg.get("discovery_prefix", "homeassistant")),
    }
