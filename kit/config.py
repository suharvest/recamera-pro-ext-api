"""
config.py -- unified app configuration loading for the reCamera Pro Kit.

Single source of truth for an app's *effective* configuration:

    effective config = manifest.config_schema defaults
                       overlaid by  <appdata>/<id>/config.json  (user settings,
                                    legacy fallback: <app_dir>/config.json)
                       overlaid by  explicit CLI overrides (manual --conf/--iou)

Before this module each app duplicated a `_flatten_schema()` helper and re-read
its own manifest.json (fall-detection) or an env-named JSON file (retail-vision's
RETAIL_CONFIG). That is now one code path here, so the /appcenter parameter panel
can write config.json and every app picks the change up identically.

Backward compatible: no config.json == use manifest defaults == the old
behaviour. The appmgr side has its own stdlib-only mirror of the flatten/validate
logic in market/appmgr/config.py (appmgr must not import the kit package).
"""
from __future__ import annotations

import json
import os
import sys
from typing import Any, Dict, Optional


def schema_items(manifest: Optional[dict]) -> Dict[str, dict]:
    """Return {key: item_spec} from a manifest `config_schema`.

    ★Canonical form is GROUPED★: `config_schema.groups[].items[] = {key, type,
    default, ...}`. All in-repo apps use it and the frontend SchemaForm renders
    by group, so there is exactly one main code path here.

    A legacy FLAT `config_schema[key] = {type, default, ...}` (no `groups`) is
    still accepted for third-party packages built before the unification; it is
    normalised to the grouped form by `_flat_to_grouped` and logged once per
    process. Support for it will be dropped -- publish grouped.
    """
    cs = (manifest or {}).get("config_schema") or {}
    if not isinstance(cs, dict):
        return {}
    if "groups" not in cs:
        cs = _flat_to_grouped(cs, (manifest or {}).get("id"))
    out: Dict[str, dict] = {}
    for g in cs.get("groups") or []:
        for it in g.get("items") or []:
            key = it.get("key")
            if key:
                out[key] = it
    return out


_flat_warned: set = set()


def _flat_to_grouped(cs: Dict[str, Any], app_id: Optional[str] = None) -> dict:
    """DEPRECATED input form: flat {key: spec} -> {"groups":[{items:[...]}]}."""
    items = [{"key": k, **v} for k, v in cs.items() if isinstance(v, dict)]
    if items and app_id not in _flat_warned:
        _flat_warned.add(app_id)
        print(f"[kit.config] {app_id or '<app>'}: flat `config_schema` is "
              f"deprecated; publish the grouped form "
              f"(config_schema.groups[].items[])", file=sys.stderr, flush=True)
    return {"groups": [{"key": "general", "title": "General", "items": items}]}


def flatten_schema(manifest: dict) -> Dict[str, Any]:
    """Return {key: default} for every schema item that declares one.

    Items with no "default" (e.g. retail zone/line controls) are omitted -- they
    only exist in config.json once the user draws them.
    """
    return {k: v["default"] for k, v in schema_items(manifest).items()
            if "default" in v}


def load_manifest(app_dir: str) -> dict:
    """Read <app_dir>/manifest.json, or {} if missing/corrupt."""
    try:
        with open(os.path.join(app_dir, "manifest.json")) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def appdata_root() -> str:
    """Root of the user-data tree that survives app upgrades.

    Mirrors appmgr's paths.APPDATA_DIR (same env var, same default). Read at
    call time so a test / a manually launched app can redirect it.
    """
    return os.environ.get("APPMGR_APPDATA_DIR", "/userdata/local/appdata")


def app_id_of_dir(app_dir: str, manifest: Optional[dict] = None) -> str:
    """The app id owning `app_dir`: manifest `id` if sane, else the dir name."""
    if manifest is None:
        manifest = load_manifest(app_dir)
    mid = (manifest or {}).get("id")
    if isinstance(mid, str) and mid and "/" not in mid and mid not in (".", ".."):
        return mid
    return os.path.basename(os.path.abspath(app_dir).rstrip("/"))


def user_config_path(app_dir: str, manifest: Optional[dict] = None) -> str:
    """Where this app's user config lives.

    Canonical: <appdata_root>/<id>/config.json -- OUTSIDE the install dir, so an
    app upgrade (which swaps /userdata/local/apps/<id>/ wholesale) can no longer
    delete the user's settings. Falls back to the legacy in-app path while that
    file still exists (appmgr migrates it on the next read/write/install; the kit
    side only ever READS, it never moves files from the app process).
    """
    canonical = os.path.join(appdata_root(), app_id_of_dir(app_dir, manifest),
                             "config.json")
    if os.path.isfile(canonical):
        return canonical
    legacy = os.path.join(app_dir, "config.json")
    return legacy if os.path.isfile(legacy) else canonical


def load_user_config(app_dir: str, manifest: Optional[dict] = None) -> Dict[str, Any]:
    """Read the user's config.json overrides, or {} if absent/corrupt."""
    try:
        with open(user_config_path(app_dir, manifest)) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def effective_config(app_dir: str, manifest: Optional[dict] = None) -> Dict[str, Any]:
    """Merge manifest defaults with the user's config.json (config.json wins)."""
    if manifest is None:
        manifest = load_manifest(app_dir)
    eff = flatten_schema(manifest)
    for k, v in load_user_config(app_dir, manifest).items():
        eff[k] = v
    return eff


def app_dir_of(app) -> str:
    """Best-effort install directory of a running App instance.

    The app object's class lives in the app's app.py, so its module __file__
    directory IS the install dir (where manifest.json / config.json sit). Falls
    back to CWD (appmgr launches each app with cwd=app_dir).
    """
    mod = sys.modules.get(type(app).__module__)
    path = getattr(mod, "__file__", None)
    if path:
        return os.path.dirname(os.path.abspath(path))
    return os.getcwd()
