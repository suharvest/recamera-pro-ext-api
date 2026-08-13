"""
App base class + generic main loop for reCamera Pro Kit (see docs/guide/kit-design.md §3).

An application is a thin subclass that overrides at most two hooks:

    setup(config)               -- read config_schema params (conf, iou, ...)
    on_results(results, frame)  -- ★the only business-logic entry point★
                                   turn raw detections into app-level events.
                                   Default = passthrough (no events).

Everything else -- open the frame source, skip camera warm-up placeholder
frames, letterbox, RKNN infer, detect post-process, publish via ResultSink,
and collect FPS / latency debug metrics -- lives here in the base loop and is
never re-implemented per app.

Import convention: `kit` is a package. The directory that CONTAINS `kit/` is on
sys.path (the appmgr and each app's bootstrap add it), so kit modules import each
other as `kit.adapters.*` / `kit.runtime.*`. This avoids the app.py/kit.app name
collision that a bare `app` module would cause.
"""
from __future__ import annotations

import argparse
import os
import signal
import sys
import time
from typing import Any, Dict, List, Optional

import numpy as np

from kit.adapters.frame_source import open_frame_source, DEFAULT_SUB_STREAM, Frame
from kit.adapters.result_sink import ResultSink, open_result_sink
from kit.runtime.preprocess import letterbox
from kit.runtime.postprocess.detect import postprocess, COCO80
# NOTE: RknnModel (kit.runtime.engine) is imported LAZILY inside run(), not here.
# It does `from rknnlite.api import RKNNLite` at module top, so importing it
# eagerly would force rknnlite onto every interpreter that touches kit.app --
# including CPU-only / audio apps (e.g. voice-transcribe under the sherpa venv
# /userdata/rknnenv, which has no rknnlite). Model-backed vision apps still get
# it the moment run() constructs a model; behaviour there is unchanged.


class App:
    """Base application. Subclass and override `setup` / `on_results`."""

    # Subclasses set these (usually mirrored from manifest.json).
    id: str = "app"
    name: str = "App"
    postproc: str = "detect"          # which post-processor the base loop runs
    input_size: int = 640             # stage-1 model input side (letterbox target);
                                      # ppocr-reader overrides to 480 for the DB detector
    needs_model: bool = True          # CPU-only apps (e.g. qrcode-reader) set this
                                      # False: the loop skips RknnModel + letterbox +
                                      # infer and calls process_frame(frame) instead.

    def __init__(self) -> None:
        # config_schema-backed knobs (defaults; overridden in setup())
        self.conf: float = 0.25
        self.iou: float = 0.45
        self.class_names = COCO80
        self.config: Dict[str, Any] = {}
        # Hot-reload (SIGHUP) flag. appmgr set_config sends SIGHUP after writing
        # config.json when ALL changed items are apply:"live" (see DESIGN
        # §3.2/§4). The signal handler only FLIPS this flag; the main loop does
        # the real re-read on the next frame -- signal handlers must stay tiny
        # and must not touch the model / pipeline.
        self._reload_flag: bool = False

    # -- application hooks (override these) -------------------------------- #
    def setup(self, config: Dict[str, Any]) -> None:
        """Read config_schema parameters. Override + call super().setup(config)."""
        self.config = config or {}
        self.conf = float(self.config.get("conf", self.conf))
        self.iou = float(self.config.get("iou", self.iou))

    # -- live-reload value-replace helpers (shared by every app override) --- #
    @staticmethod
    def _reload_params(config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """Drop keys whose value is None so a missing/cleared config item never
        overwrites a live attribute (the None-filter every app applied by hand)."""
        return {k: v for k, v in (config or {}).items() if v is not None}

    @staticmethod
    def _reload_float(params: Dict[str, Any], key: str, cur: float) -> float:
        """Value-replace a float knob from `params`; keep `cur` on absence/garbage."""
        try:
            return float(params.get(key, cur))
        except (TypeError, ValueError):
            return cur

    @staticmethod
    def _reload_int(params: Dict[str, Any], key: str, cur: int) -> int:
        """Value-replace an int knob from `params`; keep `cur` on absence/garbage."""
        try:
            return int(params.get(key, cur))
        except (TypeError, ValueError):
            return cur

    def on_config_reload(self, config: Dict[str, Any]) -> None:
        """★Live config hot-reload hook★ (SIGHUP -> re-read config.json).

        Called from the main loop when appmgr signals a LIVE-only config change.
        `config` is the freshly re-read effective config (manifest defaults
        overlaid by the updated config.json).

        Base default: reapply ONLY the base-managed live knobs (conf/iou) and
        refresh self.config. This is deliberately safe -- it just replaces
        values, and NEVER rebuilds the model, frame source, or any pipeline
        state. Subclasses that snapshot extra params into their own attributes
        (e.g. self.max_faces, thresholds, ROI geometry) override this to reapply
        those the same value-replacing way -- use the `_reload_float/_reload_int`
        helpers above. Anything structural (model swap, input_size, backend,
        buffer resize) must NOT be hot-reloaded -- those params are
        apply:"restart" in the manifest and never reach here.
        """
        self.config = config or {}
        params = self._reload_params(config)
        self.conf = self._reload_float(params, "conf", self.conf)
        self.iou = self._reload_float(params, "iou", self.iou)

    # -- hot-reload plumbing (base; apps do not touch) -------------------- #
    def _install_reload_handler(self) -> None:
        """Install the SIGHUP handler. Best-effort: signal.signal only works on
        the main thread, so a non-main-thread run() silently skips hot-reload
        (the app still works, config changes just need a restart)."""
        try:
            signal.signal(signal.SIGHUP, self._on_sighup)
        except (ValueError, OSError):
            pass

    def _on_sighup(self, signum, frame) -> None:
        # Tiny by design: only flip the flag; the loop does the work.
        self._reload_flag = True

    def _maybe_reload(self) -> None:
        """If a SIGHUP arrived, re-read the effective config and hand it to
        on_config_reload. Never raises into the loop."""
        if not self._reload_flag:
            return
        self._reload_flag = False
        from kit import config as _cfg
        try:
            cfg = _cfg.effective_config(_cfg.app_dir_of(self))
        except Exception as e:                 # config unreadable -> keep running
            print(f"[app:{self.id}] config reload skipped (read failed: {e})",
                  flush=True)
            return
        try:
            self.on_config_reload(cfg)
            print(f"[app:{self.id}] config hot-reloaded ({len(cfg)} keys)",
                  flush=True)
        except Exception as e:                 # app hook bug must not kill loop
            print(f"[app:{self.id}] config reload failed: {e}", flush=True)

    def run_postproc(self, outs, info) -> List[dict]:
        """Turn raw RKNN outputs into result dicts. Default = YOLO detect.

        Pose apps override this to run kit.runtime.postprocess.pose instead.
        Kept as a hook so the generic run() loop stays post-processor agnostic.
        """
        return postprocess(outs, info, conf_thres=self.conf, iou_thres=self.iou,
                           class_names=self.class_names)

    def process_frame(self, frame: Frame) -> List[dict]:
        """CPU-only entry point for `needs_model = False` apps.

        Turn one `Frame` straight into result dicts WITHOUT any NPU model /
        letterbox / infer. Only called when `needs_model` is False; model-backed
        apps ignore this and use the letterbox->infer->run_postproc path instead.
        qrcode-reader overrides this to run cv2.QRCodeDetector on the frame.
        """
        raise NotImplementedError("process_frame() must be overridden when needs_model=False")

    def on_results(self, results: List[dict], frame: Frame) -> List[dict]:
        """★Business logic★. Map raw detections -> app-level events.

        Default: passthrough (no derived events). yolo-detector overrides this
        to format detection events; fall-detection would run its state machine
        here, etc. `results` is the list of detect dicts; return a list of
        JSON-serialisable event dicts.
        """
        return []

    # -- generic main loop (base; apps do not touch) ---------------------- #
    def run(
        self,
        model_path: str,
        *,
        source: str = "ffmpeg",
        url: str = DEFAULT_SUB_STREAM,
        sink: Optional[ResultSink] = None,
        n: int = 0,
        every: int = 1,
        skip_gray_std: float = 8.0,
        max_gray_skip: int = 120,
        verbose: bool = True,
    ) -> None:
        """Run the live pipeline: frames -> infer -> post -> on_results -> emit.

        n=0 runs until the stream ends / interrupted; n>0 stops after n frames.
        `skip_gray_std`: frames whose pixel std is below this are treated as the
        camera's warm-up placeholder (grey, std~0) and skipped (up to
        `max_gray_skip`).
        """
        own_sink = sink is None
        if sink is None:
            sink = open_result_sink("stdout")

        # Enable SIGHUP config hot-reload for the lifetime of this loop.
        self._install_reload_handler()

        if self.needs_model:
            from kit.runtime.engine import RknnModel  # lazy: only vision apps need rknnlite
            model = RknnModel(model_path)
        else:
            model = None
        src = open_frame_source(url=url, prefer=source)

        t_pre = t_inf = t_post = 0.0
        processed = 0
        grays_skipped = 0
        got_real = False
        warmed = False
        loop_start = None
        fidx = 0

        # -- live telemetry (debug panel): FPS + per-stage latency ---------- #
        # Accumulated over a short window and flushed as a `metrics` meta event
        # once per METRICS_PERIOD s. Purely additive -- rides the existing WS
        # channel via emit_meta (a no-op on MQTT), never touches results/events.
        METRICS_PERIOD = 1.0
        m_t0 = time.monotonic()
        m_frames = 0
        m_pre = m_inf = m_post = 0.0

        if verbose:
            print(f"[app:{self.id}] model={model_path} source={source} url={url} "
                  f"conf={self.conf} iou={self.iou} sink={type(sink).__name__}",
                  flush=True)
        try:
            for frame in src.frames():
                # -- apply any pending live config hot-reload (SIGHUP) ------ #
                self._maybe_reload()
                # -- skip camera warm-up placeholder (grey) frames --------- #
                if not got_real:
                    std = float(np.asarray(frame.data).std())
                    if std < skip_gray_std and grays_skipped < max_gray_skip:
                        grays_skipped += 1
                        continue
                    got_real = True
                    if verbose:
                        print(f"[app:{self.id}] skipped {grays_skipped} grey "
                              f"warm-up frames; first real frame std={std:.1f}",
                              flush=True)

                fidx += 1
                if every > 1 and (fidx % every) != 0:
                    continue

                t0 = time.monotonic()
                if self.needs_model:
                    padded, info = letterbox(frame.data, self.input_size)
                    t1 = time.monotonic()
                    outs = model.infer(padded)
                    t2 = time.monotonic()
                    results = self.run_postproc(outs, info)
                    t3 = time.monotonic()
                else:
                    # CPU-only path: no letterbox / NPU infer. process_frame does
                    # all the work (counted under the "infer" timing bucket).
                    t1 = time.monotonic()
                    results = self.process_frame(frame)
                    t2 = t3 = time.monotonic()

                if not warmed:
                    warmed = True
                    loop_start = time.monotonic()
                    if verbose:
                        if self.needs_model:
                            print(f"[app:{self.id}] warmup output_shapes="
                                  f"{[o.shape for o in outs]} "
                                  f"frame={frame.w}x{frame.h} fmt={frame.fmt}",
                                  flush=True)
                        else:
                            print(f"[app:{self.id}] warmup (cpu, no model) "
                                  f"frame={frame.w}x{frame.h} fmt={frame.fmt}",
                                  flush=True)
                    continue

                events = self.on_results(results, frame)

                # Hand the sink the current frame's pixel dimensions before
                # emit so coordinate-normalizing sinks (OfficialResultSink ->
                # extension-API [0,1] contract) can divide by the ORIGINAL
                # full-res frame size our result coords are mapped to. No-op on
                # the WS/stdout/MQTT workaround sinks (they keep pixel coords).
                sink.set_frame_size(frame.w, frame.h)
                sink.emit({"results": results, "events": events}, frame.pts)

                t_pre += t1 - t0
                t_inf += t2 - t1
                t_post += t3 - t2
                processed += 1

                # -- periodic metrics meta event (FPS + per-stage latency) --- #
                m_frames += 1
                m_pre += t1 - t0
                m_inf += t2 - t1
                m_post += t3 - t2
                m_now = time.monotonic()
                m_dt = m_now - m_t0
                if m_dt >= METRICS_PERIOD and m_frames:
                    try:
                        sink.emit_meta({
                            "type": "metrics",
                            "kind": "metrics",
                            "app": self.id,
                            "fps": round(m_frames / m_dt, 1),
                            "latency_ms": {
                                "pre": round(m_pre / m_frames * 1000, 1),
                                "infer": round(m_inf / m_frames * 1000, 1),
                                "post": round(m_post / m_frames * 1000, 1),
                            },
                            "frames": processed,
                            "pts": frame.pts,
                        })
                    except Exception:
                        pass   # telemetry must never break the inference loop
                    m_t0 = m_now
                    m_frames = 0
                    m_pre = m_inf = m_post = 0.0

                if verbose:
                    def _label(d):
                        name = (d.get("cls_name") or d.get("text")
                                or d.get("label") or "?")
                        return (f"{name}:{d['score']:.2f}" if "score" in d
                                else f"{name}")
                    names = ", ".join(_label(d) for d in results[:6])
                    print(f"[app:{self.id}] frame#{processed:03d} "
                          f"dets={len(results):2d} events={len(events)} "
                          f"clients={getattr(sink, 'client_count', lambda: 0)()} "
                          f"{names}", flush=True)

                if n and processed >= n:
                    break
        finally:
            src.close()
            if model is not None:
                model.release()
            if own_sink:
                sink.close()

        if processed and loop_start:
            wall = time.monotonic() - loop_start
            comp = (t_pre + t_inf + t_post) / processed * 1000
            print(f"\n[app:{self.id}] === {processed} frames "
                  f"(grey-skipped {grays_skipped}) ===", flush=True)
            print(f"[app:{self.id}] preprocess {t_pre/processed*1000:5.1f} ms | "
                  f"infer {t_inf/processed*1000:5.1f} ms | "
                  f"post {t_post/processed*1000:5.1f} ms", flush=True)
            print(f"[app:{self.id}] compute-only {comp:5.1f} ms -> "
                  f"{1000.0/comp:4.1f} fps | end-to-end {processed/wall:4.1f} fps",
                  flush=True)
        elif verbose:
            print(f"[app:{self.id}] no frames processed", file=sys.stderr)


def run_app(app: App, argv: Optional[List[str]] = None) -> None:
    """Generic CLI entry an app's app.py calls from __main__.

    Wires argparse -> config -> sink -> app.run(). Keeps app.py thin.
    """
    ap = argparse.ArgumentParser(description=f"reCamera Pro app: {app.id}")
    ap.add_argument("--model", default=None,
                    help="path to .rknn model (omitted for CPU-only apps)")
    # conf/iou default to None: the effective config (manifest defaults overlaid
    # by config.json) supplies them. A value here is a MANUAL override that wins
    # over config.json -- used for on-device debugging, not by appmgr.
    ap.add_argument("--conf", type=float, default=None)
    ap.add_argument("--iou", type=float, default=None)
    ap.add_argument("--source", default="ffmpeg", choices=["ffmpeg", "snapshot"])
    ap.add_argument("--url", default=DEFAULT_SUB_STREAM)
    ap.add_argument("--sink", default="ws", choices=["ws", "stdout"])
    ap.add_argument("--port", type=int, default=8124)
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--n", type=int, default=0, help="stop after N frames (0=forever)")
    ap.add_argument("--every", type=int, default=1)
    ap.add_argument("--quiet", action="store_true")
    # MQTT / Home Assistant fan-out (optional; default OFF). Normally driven by
    # appmgr via RECAMERA_MQTT_* env; these flags are for on-device manual test.
    ap.add_argument("--mqtt-host", default=None,
                    help="enable MQTT/HA output to this broker (adds to WS)")
    ap.add_argument("--mqtt-port", type=int, default=None)
    ap.add_argument("--mqtt-user", default=None)
    ap.add_argument("--mqtt-pass", default=None)
    ap.add_argument("--mqtt-base-topic", default=None)
    ap.add_argument("--mqtt-discovery-prefix", default=None)
    args = ap.parse_args(argv)

    # Unified config load (kit-design / parameter hot-tuning): manifest
    # config_schema defaults overlaid by <app_dir>/config.json. Explicit CLI
    # --conf/--iou win over that (manual debugging override).
    from kit import config as _cfg
    app_dir = _cfg.app_dir_of(app)
    # Read the manifest ONCE and thread it through both consumers (effective
    # config overlay + output-sink assembly) instead of loading it twice.
    manifest = _cfg.load_manifest(app_dir)
    eff = _cfg.effective_config(app_dir, manifest=manifest)
    if args.conf is not None:
        eff["conf"] = args.conf
    if args.iou is not None:
        eff["iou"] = args.iou
    app.setup(eff)

    if args.sink == "stdout":
        primary: ResultSink = open_result_sink("stdout")
    else:
        primary = open_result_sink("ws", host=args.host, port=args.port,
                                   app_id=app.id)

    # Unified configurable output (internal/OUTPUT_SINK_SPEC.md §3). Apps that
    # declare `capabilities:["output"]` get channels/formatters/filters assembled
    # from the manifest `output` block + persisted config; apps that do NOT opt
    # in bypass this entirely and keep the legacy MQTT fan-out below unchanged.
    from kit.adapters.result_sink import MultiSink
    from kit.adapters.output_sink import assemble_output_sink
    out_sink, opted_in = assemble_output_sink(
        app, app_dir, manifest, eff, verbose=not args.quiet)

    sinks: List[ResultSink] = [primary]
    if opted_in:
        if out_sink is not None:
            sinks.append(out_sink)
    else:
        # Legacy path: optional MQTT / Home Assistant fan-out, enabled when a
        # broker host is supplied via appmgr's RECAMERA_MQTT_* env or --mqtt-host.
        # Fully best-effort: any failure here degrades to WS-only, never aborts.
        mqtt = _maybe_open_mqtt_sink(app, app_dir, args, verbose=not args.quiet)
        if mqtt is not None:
            sinks.append(mqtt)
    sink: ResultSink = MultiSink(sinks) if len(sinks) > 1 else sinks[0]
    try:
        app.run(args.model, source=args.source, url=args.url, sink=sink,
                n=args.n, every=args.every, verbose=not args.quiet)
    finally:
        sink.close()


def _env_flag(name: str) -> bool:
    return str(os.environ.get(name, "")).strip().lower() in ("1", "true", "yes", "on")


def _maybe_open_mqtt_sink(app: "App", app_dir: str, args, *, verbose: bool):
    """Build an MqttSink from CLI flags (win) or RECAMERA_MQTT_* env, or None.

    Precedence: --mqtt-host flag > RECAMERA_MQTT_ENABLED+RECAMERA_MQTT_HOST env >
    disabled. HA entities come from the app manifest's `ha_entities` array.
    """
    env = os.environ
    host = args.mqtt_host or (env.get("RECAMERA_MQTT_HOST")
                              if _env_flag("RECAMERA_MQTT_ENABLED") else None)
    if not host:
        return None
    port = args.mqtt_port or int(env.get("RECAMERA_MQTT_PORT", "1883") or 1883)
    user = args.mqtt_user if args.mqtt_user is not None else env.get("RECAMERA_MQTT_USERNAME", "")
    pw = args.mqtt_pass if args.mqtt_pass is not None else env.get("RECAMERA_MQTT_PASSWORD", "")
    base = args.mqtt_base_topic or env.get("RECAMERA_MQTT_BASE_TOPIC", "recamera")
    prefix = args.mqtt_discovery_prefix or env.get("RECAMERA_MQTT_DISCOVERY_PREFIX", "homeassistant")

    from kit import config as _cfg
    manifest = _cfg.load_manifest(app_dir)
    entities = manifest.get("ha_entities") or []
    device_name = manifest.get("name") or app.name or "reCamera Pro"
    try:
        from kit.adapters.mqtt_sink import MqttSink
        return MqttSink(host=host, port=port, app_id=app.id, base_topic=base,
                        discovery_prefix=prefix, username=user or "", password=pw or "",
                        entities=entities, device_name=device_name, verbose=verbose)
    except Exception as e:  # never let MQTT setup break WS output
        if verbose:
            print(f"[app:{app.id}] MQTT sink disabled (setup error: {e})", flush=True)
        return None
