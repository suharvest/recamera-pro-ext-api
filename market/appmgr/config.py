"""
config.py -- per-app config_schema handling for appmgr (stdlib only).

appmgr must not import the kit package (kit lives at /userdata/local/kit, appmgr
at /userdata/local/appmgr, deployed independently), so this is a self-contained
mirror of the schema logic. Kit's runtime side is kit/config.py.

Responsibilities:
  * read/flatten a manifest config_schema (flat OR grouped) into per-key specs,
  * compute the effective value of each key (config.json overlaid on default),
  * VALIDATE an incoming {key: value} map against the schema (type/enum/range),
  * atomically write the validated overlay to <app_dir>/config.json.
"""
from __future__ import annotations

import json
import os
import tempfile
from typing import Any, Dict, List, Tuple

from . import paths


# --------------------------------------------------------------------------- #
# schema flatten
# --------------------------------------------------------------------------- #
def schema_specs(manifest: dict) -> Dict[str, dict]:
    """Return {key: item_spec} from a grouped OR flat config_schema."""
    cs = (manifest or {}).get("config_schema") or {}
    out: Dict[str, dict] = {}
    if "groups" in cs:
        for g in cs.get("groups", []):
            for it in g.get("items", []):
                if "key" in it:
                    out[it["key"]] = it
    else:
        for k, v in cs.items():
            if isinstance(v, dict):
                out[k] = {"key": k, **v}
    return out


def schema_defaults(manifest: dict) -> Dict[str, Any]:
    return {k: v["default"] for k, v in schema_specs(manifest).items()
            if "default" in v}


# --------------------------------------------------------------------------- #
# config.json read / write
# --------------------------------------------------------------------------- #
def config_path(app_id: str) -> str:
    return os.path.join(paths.app_dir(app_id), "config.json")


def load_user_config(app_id: str) -> Dict[str, Any]:
    try:
        with open(config_path(app_id)) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def effective_values(manifest: dict, app_id: str) -> Dict[str, Any]:
    """Manifest defaults overlaid by the user's config.json (config.json wins)."""
    eff = schema_defaults(manifest)
    for k, v in load_user_config(app_id).items():
        eff[k] = v
    return eff


def write_user_config(app_id: str, config: Dict[str, Any]) -> None:
    """Atomically write config.json (temp file + fsync + rename)."""
    d = paths.app_dir(app_id)
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".config.", dir=d)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(config, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, config_path(app_id))
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


# --------------------------------------------------------------------------- #
# validation
# --------------------------------------------------------------------------- #
def _is_num(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def _valid_point(p) -> bool:
    return (isinstance(p, (list, tuple)) and len(p) == 2
            and all(_is_num(c) and -0.001 <= c <= 1.001 for c in p))


def _validate_one(spec: dict, value) -> Tuple[bool, Any, str]:
    """Return (ok, coerced_value, error). Type/enum/range per spec['type']."""
    t = spec.get("type", "number")
    key = spec.get("key", "?")

    if t == "number":
        if not _is_num(value):
            return False, None, f"{key}: expected number, got {type(value).__name__}"
        v = float(value)
        if "min" in spec and v < float(spec["min"]) - 1e-9:
            return False, None, f"{key}: {v} < min {spec['min']}"
        if "max" in spec and v > float(spec["max"]) + 1e-9:
            return False, None, f"{key}: {v} > max {spec['max']}"
        # keep ints int (step==1 and integral) so counts stay clean
        if float(spec.get("step", 0)) == 1 and v == int(v):
            v = int(v)
        return True, v, ""

    if t == "boolean":
        if not isinstance(value, bool):
            return False, None, f"{key}: expected boolean"
        return True, value, ""

    if t == "enum":
        opts = spec.get("options") or []
        if value not in opts:
            return False, None, f"{key}: {value!r} not in {opts}"
        return True, value, ""

    if t == "string":
        if not isinstance(value, str):
            return False, None, f"{key}: expected string"
        return True, value, ""

    if t == "zone":
        if value in (None, [], {}):
            return True, None, ""
        if not isinstance(value, list) or not all(_valid_point(p) for p in value):
            return False, None, f"{key}: zone must be a list of [x,y] in [0,1]"
        maxp = spec.get("maxPoints")
        if maxp and len(value) > int(maxp):
            return False, None, f"{key}: zone has {len(value)} > maxPoints {maxp}"
        if 0 < len(value) < 3:
            return False, None, f"{key}: zone polygon needs >= 3 points"
        return True, [[float(a), float(b)] for a, b in value], ""

    if t == "line":
        if value in (None, {}, []):
            return True, None, ""
        if (not isinstance(value, dict) or not _valid_point(value.get("a"))
                or not _valid_point(value.get("b"))):
            return False, None, f"{key}: line needs a=[x,y] and b=[x,y] in [0,1]"
        out = {"a": [float(c) for c in value["a"]],
               "b": [float(c) for c in value["b"]]}
        if "in" in value:
            if str(value["in"]).lower() not in ("left", "right"):
                return False, None, f"{key}: line 'in' must be left|right"
            out["in"] = str(value["in"]).lower()
        return True, out, ""

    # unknown control type: accept opaquely (forward-compat)
    return True, value, ""


def validate_config(manifest: dict, incoming: dict) -> Tuple[Dict[str, Any], List[str]]:
    """Validate incoming {key: value} against the schema.

    Returns (clean, errors). `clean` holds only schema-known, valid keys (a
    sparse overlay to persist). Unknown keys are rejected as errors. On any
    error `clean` is still returned but the caller should refuse to write.
    """
    specs = schema_specs(manifest)
    clean: Dict[str, Any] = {}
    errors: List[str] = []
    if not isinstance(incoming, dict):
        return {}, ["config must be an object"]
    for key, val in incoming.items():
        spec = specs.get(key)
        if spec is None:
            errors.append(f"{key}: unknown parameter")
            continue
        ok, coerced, err = _validate_one(spec, val)
        if ok:
            if coerced is not None:
                clean[key] = coerced
        else:
            errors.append(err)
    return clean, errors


def get_config(manifest: dict, app_id: str) -> dict:
    """Response payload for GET /api/appMgr/config: schema + effective values."""
    return {
        "id": app_id,
        "config_schema": (manifest or {}).get("config_schema") or {},
        "values": effective_values(manifest, app_id),
        "defaults": schema_defaults(manifest),
    }
