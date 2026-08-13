"""
builtin.py -- driver for the firmware's built-in inference, exposed to appmgr as
a first-class "builtin" app (DESIGN-inference-as-app §1.2/§3.3).

The official detection pipeline is NOT an appmgr-supervised process: it runs
inside the shipped firmware (rkipc + entry.cgi). This module drives it through
the very endpoints the firmware already exposes over nginx + entry.cgi, so the
UI can list / activate / configure it exactly like a self-hosted app, with the
appmgr HTTP surface returning the SAME shapes (list entry, GET/POST config).

Endpoints (localhost 443, self-signed, no JWT -- mirrors kit/adapters/cgi_control.py):
  * GET/POST /cgi-bin/entry.cgi/model/inference
        GET  -> {iEnable,iFPS,iActualFPS,sModel,sStatus}
        POST body {iEnable?,sModel?,iFPS?} (each field independent/optional)
  * GET/POST /cgi-bin/entry.cgi/model/info?File-name=<model>
        GET  -> {algorithm,category,classes[],metrics:{confidence,iou,max_obj},...}
        POST wants the FULL info object back (validated field-by-field), so
        writes are read-modify-write: GET, overlay metrics, POST the whole thing.

Apply semantics: the firmware snapshots every threshold/model/fps at model LOAD
time and never re-reads per frame, so EVERY builtin config item is apply:"restart"
(verified 2026-08-13, DESIGN §6). A /model/info write alone does NOT reload the
model (firmware bug, DESIGN §1.2); set_config therefore always follows a
model_info change with a /model/inference POST to force `rc_model_infer_restart`
when inference is enabled.

stdlib only (http.client) -- appmgr must not import the kit package.
"""
from __future__ import annotations

import http.client
import json
import ssl
from typing import Any, Dict, Optional

from . import config as appconfig

# --------------------------------------------------------------------------- #
# constants
# --------------------------------------------------------------------------- #
BUILTIN_ID = "builtin"

_HOST = "127.0.0.1"
_PORT = 443
_TIMEOUT = 10.0
_CGI_BASE = "/cgi-bin/entry.cgi"
_INFERENCE = "/model/inference"
_MODEL_INFO = "/model/info"
_MODEL_ID = 0

# The firmware default model (current shipped detector). Used as the fallback
# File-name for /model/info reads before /model/inference has reported sModel.
_DEFAULT_MODEL = "yolov5.rknn"


class BuiltinError(Exception):
    pass


# --------------------------------------------------------------------------- #
# low-level HTTP to entry.cgi (localhost, no JWT)
# --------------------------------------------------------------------------- #
def _request(method: str, path: str, body: Optional[bytes] = None) -> dict:
    """One HTTP request to entry.cgi -> parsed JSON dict.

    Raises BuiltinError on transport failure, non-2xx, non-JSON, or a JSON
    envelope with a non-zero `code`.
    """
    ctx = ssl._create_unverified_context()   # self-signed loopback cert
    conn = http.client.HTTPSConnection(_HOST, _PORT, timeout=_TIMEOUT, context=ctx)
    headers = {"Host": "localhost", "Connection": "close"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    try:
        conn.request(method, _CGI_BASE + path, body=body, headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
        status = resp.status
    except OSError as e:
        raise BuiltinError("entry.cgi %s %s -> transport error: %s"
                           % (method, path, e))
    finally:
        try:
            conn.close()
        except Exception:
            pass

    if not (200 <= status < 300):
        raise BuiltinError("entry.cgi %s %s -> HTTP %d: %s"
                           % (method, path, status,
                              raw[:200].decode("utf-8", "replace")))
    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception as e:
        raise BuiltinError("entry.cgi %s %s -> non-JSON (%s): %s"
                           % (method, path, e,
                              raw[:200].decode("utf-8", "replace")))
    if isinstance(data, dict) and data.get("code", 0) != 0:
        raise BuiltinError("entry.cgi %s %s -> code=%s message=%s"
                           % (method, path, data.get("code"), data.get("message")))
    return data if isinstance(data, dict) else {"data": data}


def _inference_q() -> str:
    return "%s?id=%d" % (_INFERENCE, _MODEL_ID)


def _info_q(model: str) -> str:
    # File-name must be a query parameter (both GET and POST reject a body-only
    # File-name with HTTP 400 "Missing File-name parameter").
    from urllib.parse import quote
    return "%s?File-name=%s" % (_MODEL_INFO, quote(str(model)))


# --------------------------------------------------------------------------- #
# inference endpoint
# --------------------------------------------------------------------------- #
def get_inference() -> dict:
    return _request("GET", _inference_q())


def set_inference(enable: Optional[bool] = None, model: Optional[str] = None,
                  fps: Optional[int] = None) -> dict:
    """POST /model/inference with only the supplied fields (each independent)."""
    payload: Dict[str, Any] = {}
    if enable is not None:
        payload["iEnable"] = 1 if enable else 0
    if model is not None:
        payload["sModel"] = str(model)
    if fps is not None:
        payload["iFPS"] = int(fps)
    body = json.dumps(payload).encode("utf-8")
    return _request("POST", _inference_q(), body=body)


def is_running() -> bool:
    try:
        return int(get_inference().get("iEnable", 0)) == 1
    except BuiltinError:
        return False


def current_model() -> str:
    try:
        return get_inference().get("sModel") or _DEFAULT_MODEL
    except BuiltinError:
        return _DEFAULT_MODEL


def start() -> dict:
    """Enable built-in inference, keeping the firmware's persisted model/fps."""
    return set_inference(enable=True)


def stop() -> dict:
    """Disable built-in inference (does not touch rkipc / the video pipeline)."""
    return set_inference(enable=False)


# --------------------------------------------------------------------------- #
# model_info endpoint (metrics: confidence / iou / max_obj)
# --------------------------------------------------------------------------- #
def get_model_info(model: str) -> dict:
    return _request("GET", _info_q(model))


def set_model_metrics(model: str, updates: Dict[str, Any]) -> dict:
    """Read-modify-write /model/info metrics for `model`.

    The handler validates the WHOLE info object (category/algorithm/... all
    required), so we GET the current object, overlay only the changed metrics,
    and POST it back. Returns the POST envelope.
    """
    info = get_model_info(model)
    metrics = dict(info.get("metrics") or {})
    for k, v in updates.items():
        metrics[k] = v
    info["metrics"] = metrics
    body = json.dumps(info).encode("utf-8")
    return _request("POST", _info_q(model), body=body)


# --------------------------------------------------------------------------- #
# synthesized manifest (bundled with appmgr, never downloaded)
# --------------------------------------------------------------------------- #
def manifest() -> dict:
    """The built-in app's manifest. type:"builtin"; every config item carries a
    bind{endpoint,field} + apply:"restart" (DESIGN §1.2, verified §6)."""
    return {
        "id": BUILTIN_ID,
        "name": "Official Detection",
        "name_zh": "官方检测",
        "type": "builtin",
        "scene": "official",
        "scene_zh": "官方",
        "version": "firmware",
        "image": "/appcenter/apps/builtin.png",
        "author": "reCamera (firmware)",
        "description": "The firmware's built-in object detection (rkipc + NPU). "
                       "Managed through entry.cgi -- enable/disable, switch model, "
                       "set NPU fps and detection thresholds.",
        "description_zh": "固件内建目标检测（rkipc + NPU）。经 entry.cgi 管理："
                          "开关、切换模型、设置 NPU 帧率与检测阈值。",
        "config_schema": {
            "groups": [
                {
                    "key": "inference",
                    "title": "Inference",
                    "title_zh": "推理",
                    "items": [
                        {
                            "key": "model", "type": "string", "apply": "restart",
                            "title": "Model file", "title_zh": "模型文件",
                            "default": _DEFAULT_MODEL,
                            "bind": {"endpoint": "inference", "field": "sModel"},
                        },
                        {
                            "key": "fps", "type": "number", "apply": "restart",
                            "title": "NPU inference FPS", "title_zh": "NPU 推理帧率",
                            "min": 0, "max": 30, "step": 1, "default": 20,
                            "bind": {"endpoint": "inference", "field": "iFPS"},
                        },
                    ],
                },
                {
                    "key": "detection",
                    "title": "Detection",
                    "title_zh": "检测",
                    "items": [
                        {
                            "key": "confidence", "type": "number", "apply": "restart",
                            "title": "Confidence threshold", "title_zh": "置信度阈值",
                            "min": 0.05, "max": 0.95, "step": 0.05, "default": 0.25,
                            "bind": {"endpoint": "model_info", "field": "confidence"},
                        },
                        {
                            "key": "iou", "type": "number", "apply": "restart",
                            "title": "NMS IoU threshold", "title_zh": "NMS IoU 阈值",
                            "min": 0.1, "max": 0.9, "step": 0.05, "default": 0.45,
                            "bind": {"endpoint": "model_info", "field": "iou"},
                        },
                        {
                            "key": "max_obj", "type": "number", "apply": "restart",
                            "title": "Max objects", "title_zh": "最大目标数",
                            "min": 1, "max": 200, "step": 1, "default": 100,
                            "bind": {"endpoint": "model_info", "field": "max_obj"},
                        },
                    ],
                },
            ]
        },
    }


# --------------------------------------------------------------------------- #
# config get/set (reverse-assembled from the endpoints; app-isomorphic shape)
# --------------------------------------------------------------------------- #
def _binds() -> Dict[str, dict]:
    """{config key -> bind dict} from the synthesized schema."""
    out = {}
    for k, spec in appconfig.schema_specs(manifest()).items():
        if isinstance(spec.get("bind"), dict):
            out[k] = spec["bind"]
    return out


def get_config() -> dict:
    """Return {id, config_schema, values, defaults} -- IDENTICAL shape to a
    self-hosted app's GET /api/appMgr/config, so the frontend needs zero
    branching. `values` are read live off the endpoints via each item's bind.
    """
    man = manifest()
    defaults = appconfig.schema_defaults(man)
    values = dict(defaults)   # start from defaults, overlay live reads

    inf = {}
    try:
        inf = get_inference()
    except BuiltinError:
        inf = {}
    model = inf.get("sModel") or defaults.get("model") or _DEFAULT_MODEL

    info_metrics = {}
    try:
        info_metrics = (get_model_info(model).get("metrics") or {})
    except BuiltinError:
        info_metrics = {}

    for key, bind in _binds().items():
        ep = bind.get("endpoint")
        field = bind.get("field")
        if ep == "inference":
            if field in inf:
                values[key] = inf[field]
        elif ep == "model_info":
            if field in info_metrics:
                values[key] = info_metrics[field]

    return {
        "id": BUILTIN_ID,
        "config_schema": man.get("config_schema") or {},
        "values": values,
        "defaults": defaults,
    }


def set_config(incoming: dict) -> dict:
    """Validate + apply a config change by dispatching each item to its bound
    endpoint. Every builtin item is apply:"restart"; a model_info change does not
    reload the model on its own, so when inference is enabled we always follow
    with a /model/inference POST to force a reload (DESIGN §1.2)."""
    man = manifest()
    clean, errors = appconfig.validate_config(man, incoming)
    if errors:
        raise ValueError("; ".join(errors))
    if not clean:
        return {"id": BUILTIN_ID, "saved": True, "applied": "restart",
                "restarted": False, "config": {}}

    binds = _binds()
    inf_updates: Dict[str, Any] = {}   # {firmware field -> value} for /model/inference
    metric_updates: Dict[str, Any] = {}   # {metric field -> value} for /model/info

    for key, val in clean.items():
        bind = binds.get(key) or {}
        ep = bind.get("endpoint")
        field = bind.get("field")
        if ep == "inference":
            inf_updates[field] = val
        elif ep == "model_info":
            metric_updates[field] = val

    running = is_running()
    # Target model: an incoming model change wins, else the currently loaded one.
    target_model = inf_updates.get("sModel") or current_model()

    # 1) metrics: read-modify-write /model/info for the target model.
    if metric_updates:
        set_model_metrics(target_model, metric_updates)

    # 2) inference model/fps + forced reload. POST /model/inference whenever there
    #    is an inference-field change OR a metrics change that needs a reload while
    #    enabled. Preserve the current iEnable so we never silently start/stop.
    need_inf_post = bool(inf_updates) or (bool(metric_updates) and running)
    if need_inf_post:
        set_inference(
            enable=running,
            model=inf_updates.get("sModel"),
            fps=inf_updates.get("iFPS"),
        )

    return {"id": BUILTIN_ID, "saved": True, "applied": "restart",
            "restarted": bool(need_inf_post and running), "config": clean}
