"""
state.py -- single-active state (APP_CENTER_PORT_DESIGN §4.4).

state.json holds only { active_app, active_version } -- one user app runs at a
time. Reads tolerate a missing/corrupt file; writes are atomic (temp + rename).
"""
from __future__ import annotations

import json
import os
import tempfile
from typing import Optional

from . import paths


def load() -> dict:
    try:
        with open(paths.STATE_FILE) as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except (FileNotFoundError, ValueError, OSError):
        pass
    return {"active_app": None, "active_version": None}


def save(data: dict) -> None:
    paths.ensure_dirs()
    d = os.path.dirname(paths.STATE_FILE)
    fd, tmp = tempfile.mkstemp(prefix=".state.", dir=d)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, paths.STATE_FILE)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def get_active() -> Optional[str]:
    return load().get("active_app")


def set_active(app_id: Optional[str], version: Optional[str] = None) -> None:
    save({"active_app": app_id, "active_version": version})


def clear_active_if(app_id: str) -> None:
    if load().get("active_app") == app_id:
        set_active(None, None)
