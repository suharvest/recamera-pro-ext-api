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
# unified output capability -- injected config_schema group (OUTPUT_SINK_SPEC §3)
# --------------------------------------------------------------------------- #
def _output_schema_items(manifest: dict) -> List[dict]:
    """The 8 flat `output` config keys, defaulted from the manifest `output`
    block. Opaque control types (`mqtt`/`http`/`uart`/`templates`/
    `field_mapping`/`channel_multi_select`/`output_filters`) validate opaquely
    (see `_validate_one`), so the frontend SchemaForm can render them without a
    backend code change."""
    mout = (manifest or {}).get("output") or {}
    dc = mout.get("default_channel")
    channels = [dc] if isinstance(dc, str) else (list(dc) if dc else ["ws"])
    templates = mout.get("templates") or {}
    return [
        {"key": "output_channels", "type": "channel_multi_select",
         "apply": "restart", "label": "Output channels", "default": channels},
        {"key": "iMode", "type": "enum", "apply": "restart",
         "label": "Output mode", "options": ["ha", "custom", "raw"],
         "default": mout.get("default_mode", "raw")},
        {"key": "dMqtt", "type": "mqtt", "apply": "restart", "label": "MQTT",
         "default": {"iPort": 1883, "sClientId": "", "sUsername": "",
                     "sPassword": "", "sTopic": "recamera", "sURL": ""}},
        {"key": "dHttp", "type": "http", "apply": "restart", "label": "HTTP",
         "default": {"sUrl": "", "sToken": ""}},
        {"key": "dUart", "type": "uart", "apply": "restart", "label": "UART",
         "default": {"sPort": "", "sPortDev": ""}},
        {"key": "dTemplate", "type": "templates", "apply": "live",
         "label": "Templates",
         "default": {"sDetection": templates.get("detection", ""),
                     "sClassification": templates.get("classification", ""),
                     "sKeypoint": templates.get("keypoint", ""),
                     "sSegmentation": templates.get("segmentation", ""),
                     "sTracking": templates.get("tracking", "")}},
        {"key": "output_mapping", "type": "field_mapping", "apply": "live",
         "label": "Field mapping", "default": mout.get("default_mapping") or []},
        {"key": "output_filters", "type": "output_filters", "apply": "live",
         "label": "Filters",
         "default": {"only_on_detection": False, "classes": [],
                     "rate_limit_hz": 0, "preserve_edge_events": True}},
    ]


_flat_warned: set = set()


def _flat_to_grouped(cs: dict, app_name: str = "") -> dict:
    """DEPRECATED input form: flat {key: spec} -> {"groups":[{items:[...]}]}.

    ★Canonical `config_schema` is GROUPED★ (`groups[].items[]`) -- every in-repo
    app publishes it and the frontend SchemaForm renders by group. A flat schema
    from a third-party package built before the unification is normalised here,
    once, at the manifest boundary, so every consumer below sees only groups.
    """
    items = [{"key": k, **v} for k, v in cs.items() if isinstance(v, dict)]
    if items and app_name not in _flat_warned:
        _flat_warned.add(app_name)
        print(f"[appmgr.config] {app_name or '<app>'}: flat `config_schema` is "
              f"deprecated; publish the grouped form "
              f"(config_schema.groups[].items[])", flush=True)
    return {"groups": [{"key": "general", "title": "General", "items": items}]}


def _normalized_schema(manifest: dict) -> dict:
    """The manifest's `config_schema` in canonical grouped form."""
    cs = (manifest or {}).get("config_schema") or {}
    if not isinstance(cs, dict):
        return {"groups": []}
    if "groups" in cs:
        return dict(cs)
    return _flat_to_grouped(cs, (manifest or {}).get("id")
                            or (manifest or {}).get("name") or "")


def _has_output_group(cs: dict) -> bool:
    for g in cs.get("groups") or []:
        for it in g.get("items") or []:
            if it.get("key") == "output_channels":
                return True
    return False


def effective_manifest(manifest: dict) -> dict:
    """Return the manifest with its `config_schema` in canonical grouped form,
    plus the `output` group injected when the app declares
    `capabilities:["output"]`.

    Pure + idempotent. This is the single source `schema_specs`, `get_config`,
    and `do_set_config`/`validate_config` share so GET validation, POST
    validation and apply-mode classification all agree on the output keys."""
    if not (manifest or {}).get("config_schema") and \
            "output" not in ((manifest or {}).get("capabilities") or []):
        return manifest                    # nothing to normalise, nothing to add
    cs = _normalized_schema(manifest)
    caps = (manifest or {}).get("capabilities") or []
    if "output" in caps and not _has_output_group(cs):
        cs["groups"] = list(cs.get("groups") or []) + [
            {"title": "Output", "key": "output",
             "items": _output_schema_items(manifest)}]
    m = dict(manifest)
    m["config_schema"] = cs
    return m


# --------------------------------------------------------------------------- #
# schema flatten
# --------------------------------------------------------------------------- #
def schema_specs(manifest: dict) -> Dict[str, dict]:
    """Return {key: item_spec} from the canonical grouped config_schema.

    The `output` capability group (OUTPUT_SINK_SPEC §3) is injected here so every
    consumer -- GET, POST validation, apply-mode -- sees the output keys."""
    cs = _normalized_schema(effective_manifest(manifest))
    out: Dict[str, dict] = {}
    for g in cs.get("groups") or []:
        for it in g.get("items") or []:
            if "key" in it:
                out[it["key"]] = it
    return out


def schema_defaults(manifest: dict) -> Dict[str, Any]:
    return {k: v["default"] for k, v in schema_specs(manifest).items()
            if "default" in v}


# --------------------------------------------------------------------------- #
# config.json read / write
# --------------------------------------------------------------------------- #
def config_path(app_id: str) -> str:
    """Canonical user-config path: /userdata/local/appdata/<id>/config.json.

    ★Deliberately OUTSIDE the install dir★ -- installer.install() swaps the whole
    /userdata/local/apps/<id>/ directory, so a config living in there was deleted
    by every upgrade (silent loss of thresholds / ROI / output mapping)."""
    return os.path.join(paths.appdata_dir(app_id), "config.json")


def legacy_config_path(app_id: str) -> str:
    """Pre-migration location: inside the install dir (wiped by upgrades)."""
    return os.path.join(paths.app_dir(app_id), "config.json")


def _atomic_write_json(path: str, data: Dict[str, Any]) -> None:
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".config.", dir=d)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def migrate_legacy_config(app_id: str) -> bool:
    """One-shot, idempotent move of <app_dir>/config.json -> appdata.

    Devices upgraded from an older appmgr still carry the user's settings inside
    the install dir. Copy them to the new location (only if nothing is there
    yet -- the new location always wins), then rename the old file to
    `config.json.migrated` so it is not re-read and the trace stays on disk.

    Returns True when a legacy file was consumed/retired. Best-effort: an
    unreadable/corrupt legacy file is left untouched and reported as False.
    """
    old = legacy_config_path(app_id)
    if not os.path.isfile(old):
        return False
    try:
        with open(old) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return False                       # corrupt: leave it, don't destroy
    if not isinstance(data, dict):
        return False
    new = config_path(app_id)
    if not os.path.isfile(new):
        _atomic_write_json(new, data)
    os.replace(old, old + ".migrated")     # keep a trace, stop re-reading it
    return True


def load_user_config(app_id: str) -> Dict[str, Any]:
    try:
        migrate_legacy_config(app_id)
    except OSError:
        pass                               # read-only fs etc: fall through
    for p in (config_path(app_id), legacy_config_path(app_id)):
        try:
            with open(p) as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            continue
    return {}


def effective_values(manifest: dict, app_id: str) -> Dict[str, Any]:
    """Manifest defaults overlaid by the user's config.json (config.json wins)."""
    eff = schema_defaults(manifest)
    for k, v in load_user_config(app_id).items():
        eff[k] = v
    return eff


def write_user_config(app_id: str, config: Dict[str, Any]) -> None:
    """MERGE `config` into the existing config.json and write it back atomically.

    ★MERGE semantics (not replace)★: a config POST carries only the keys the user
    changed. Replacing the whole file would reset every OTHER overlaid key back to
    its manifest default. Instead we read the current config.json, overlay the
    posted keys on top (posted values win), and persist the union. Overlaying one
    parameter therefore never clobbers previously-saved overlays.

    A posted key set to ``None`` is REMOVED from the overlay (reverts that single
    key to its manifest default) -- e.g. clearing a `zone`. Write is atomic
    (temp file + fsync + rename), as before.
    """
    merged = load_user_config(app_id)   # {} if missing/corrupt; also migrates
    for k, v in (config or {}).items():
        if v is None:
            merged.pop(k, None)
        else:
            merged[k] = v
    _atomic_write_json(config_path(app_id), merged)


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
        "config_schema": effective_manifest(manifest).get("config_schema") or {},
        "values": effective_values(manifest, app_id),
        "defaults": schema_defaults(manifest),
    }
