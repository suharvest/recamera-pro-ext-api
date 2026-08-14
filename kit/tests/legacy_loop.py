"""
legacy_loop.py -- the pre-migration kit app shape, kept ONLY as a test oracle.

Every app in this repo owns its loop (`owns_loop = True` + `def run(self)`), so
`kit/app.py` no longer carries the old callback shape: the generic
`run(model_path, ...)` main loop and its `run_postproc` / `process_frame` /
`on_results` hooks were deleted from the kit.

The `test_*_shape_equivalence.py` gates still need that old behaviour to compare
against -- they run the SAME fixed input through the pre-migration app and the
migrated one and assert the emitted `results` / `events` are field-for-field
identical. So the loop lives on here, VERBATIM, as a frozen historical
reference:

    class _LegacyFooApp(LegacyLoopApp):     # instead of kit_app.App
        ...pre-migration hooks...

★Do not "improve" this file★ -- its only value is being byte-identical to what
shipped before the migration. It is never imported by kit or by any app.
"""
import os
import sys
import time
from typing import List, Optional

import numpy as np

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from kit import app as kit_app                                       # noqa: E402
from kit.app import DEFAULT_SUB_STREAM                             # noqa: E402
from kit.adapters.frame_source import Frame                        # noqa: E402
from kit.adapters.result_sink import ResultSink                    # noqa: E402


class LegacyLoopApp(kit_app.App):
    """`kit.app.App` as it was before KIT_APP_SHAPE_SPEC: the base class drives
    the loop and calls back into run_postproc / process_frame / on_results."""

    def run_postproc(self, outs, info) -> List[dict]:
        """Turn raw RKNN outputs into result dicts. Default = YOLO detect.

        Pose apps override this to run kit.runtime.postprocess.pose instead.
        Kept as a hook so the generic run() loop stays post-processor agnostic.
        """
        return kit_app.postprocess(outs, info, conf_thres=self.conf, iou_thres=self.iou,
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
            sink = kit_app.open_result_sink("stdout")

        # Enable SIGHUP config hot-reload for the lifetime of this loop.
        self._install_reload_handler()

        if self.needs_model:
            model = self._load_model(model_path)
        else:
            model = None
        mode = self.model_frame if self.needs_model else "cpu"
        if mode not in ("cpu", "hw", "hw-direct"):
            raise ValueError(
                "%s: model_frame must be 'cpu', 'hw' or 'hw-direct' (got %r)"
                % (self.id, self.model_frame))
        src = kit_app.open_frame_source(
            url=url,
            prefer=source,
            input_size=self.input_size if mode != "cpu" else 0,
            direct_preprocess=(mode == "hw-direct"),
            hw_letterbox=(mode == "hw"),
        )

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
                    # OfficialFrameSource may have performed the aspect-ratio
                    # resize on RGA already.  It preserves original ``Frame.w``
                    #/``h`` and supplies a LetterboxInfo-compatible object;
                    # post-processing therefore still maps detections back to
                    # camera pixels while Python skips the second resize.
                    # "hw" delivers the letterbox alongside the original pixels
                    # (model_data); "hw-direct" letterboxes into data itself.
                    info = getattr(frame, "model_info", None)
                    padded = getattr(frame, "model_data", None)
                    if padded is None:
                        if info is not None:
                            padded = frame.data
                        else:
                            padded, info = kit_app.letterbox(frame.data, self.input_size)
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
                sink.emit({
                    "results": results,
                    "events": events,
                    "inference_time_ms": round((t2 - t1) * 1000.0, 3),
                    "pipeline_ms": round((time.monotonic() - t0) * 1000.0, 3),
                    "stream_id": "camera-0",
                }, frame.pts)

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
