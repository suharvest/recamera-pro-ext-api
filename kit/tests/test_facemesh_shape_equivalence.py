"""
Equivalence gate for the facemesh-reader app-shape migration (KIT_APP_SHAPE_SPEC §7).

facemesh-reader is the TEMPORAL-STATE cascade: stage 2 (468-pt landmarks) feeds
`kit.logic.drowsiness`, whose EAR/MAR thresholds, PERCLOS deque, continuous-
closure timer, 5-minute yawn window and alert cooldown all live ACROSS frames --
plus the app's own blink rising-edge detector (`_prev_closed`/`_blink_count`).
A per-frame comparison would not catch a reset accumulator, so the fixture drives
a 21-frame script with scripted eye/mouth geometry: three blink bursts, two yawn
bursts, one below-presence frame and one frame with no face at all.

It is also a crop-source case: `CascadePipeline` cuts a padded SQUARE ROI out of
the ORIGINAL camera pixels, so `model_frame` must stay "cpu" while `self.pre()`
letterboxes to 640 -- two DIFFERENT images.

Hardware-free: the frame source (`kit.app.open_frame_source`) and the RKNN
engine (`kit.app.App._load_model`) are stubbed with deterministic fakes, then
the SAME fixed frame sequence is pushed through

  OLD path : the pre-migration facemesh-reader (git d5a40d3), reproduced
             verbatim below -- base `App.run()` loop + `run_postproc()` +
             `on_results()`, including its `self.pipeline` attribute;
  NEW path : the migrated `apps/facemesh-reader/app.py` -- `owns_loop = True`,
             `run()` + `for frame in self.frames()`, stage 2 on `self.cascade`,

and `results` / `events` / `pts` / `stream_id` are compared field for field.

Both fake models are seeded BY CALL NUMBER, so the k-th detector (resp.
landmark) inference of the old run and of the new run are byte-identical -- any
downstream difference is a behaviour difference.

Run: `python3 -m pytest kit/tests/test_facemesh_shape_equivalence.py -q`
"""
import importlib.util
import json
import os
import signal
import sys
import unittest

import numpy as np

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

from kit import app as kit_app                                       # noqa: E402
from kit.tests.legacy_loop import LegacyLoopApp              # noqa: E402
from kit import pipeline                                             # noqa: E402
from kit.adapters.frame_source import Frame                          # noqa: E402
from kit.adapters.result_sink import ResultSink                      # noqa: E402
from kit.logic.drowsiness import (                                   # noqa: E402
    DrowsinessLogic, DrowsinessConfig,
    LEFT_EYE_IDX, RIGHT_EYE_IDX, MOUTH_IDX,
)
from kit.runtime.postprocess import face_detect as face_post         # noqa: E402
from kit.runtime.postprocess import landmark as landmark_post        # noqa: E402
from kit.runtime.preprocess import letterbox                         # noqa: E402

APP_DIR = os.path.join(_REPO, "apps", "facemesh-reader")

FRAME_W, FRAME_H = 640, 480     # camera frame: NOT square, NOT the model size
DET_SIZE = 640                  # stage-1 input side (manifest models[0].input)
LMK_SIZE = 192                  # stage-2 landmark input side
N_GREY = 2                      # camera warm-up placeholders, both paths skip
N_REAL = 22                     # real frames offered by the fake source
DT = 0.2                        # seconds between frames

DET_MODEL = "models/yolov8n_face_rawhead_fp16.rknn"

GRID = 20                       # single FPN level: 20x20 @ stride 32
REG_MAX = 16
HALF_BIN = 2                    # DFL bin -> box half-side = 2 * 32 = 64 px
NO_FACE_DET_CALL = 5            # det call 0 is the kit warm-up -> proc frame 4

N_LMK = 468
EAR_OPEN, EAR_CLOSED = 0.30, 0.10       # around the 0.21 threshold
MAR_SHUT, MAR_YAWN = 0.20, 0.80         # around the 0.65 threshold
PRESENCE_OK, PRESENCE_LOW = 0.9, 0.2    # around the 0.5 threshold

# Landmark-call script. Indices are LANDMARK call numbers (which skip the
# no-face frame), so the eye/mouth geometry is reproducible from the call
# counter alone -- identical in both runs by construction.
CLOSED_CALLS = {2, 3, 6, 11, 12, 13, 14, 15}    # 3 blink bursts
YAWN_CALLS = {4, 5, 6, 16, 17, 18}              # 2 yawn bursts
LOW_PRESENCE_CALLS = {9}                        # landmarks discarded

# Effective config: manifest defaults, with the drowsiness timings pulled in so
# the states are reachable inside a 21-frame (4.2 s) fixture.
EFF = {
    "confidence": 0.4,
    "iou": 0.45,
    "crop_pad": 0.25,
    "presence_threshold": 0.5,
    "ear_threshold": 0.21,
    "mar_threshold": 0.65,
    "yawn_consecutive_frames": 2.0,
    "ear_continuous_sec": 0.5,
    "perclos_window_sec": 60.0,
    "perclos_critical_pct": 20.0,
    "alert_cooldown_sec": 0.0,
    "yawn_count_threshold": 2.0,
}


def _fixed_frames():
    """N_GREY flat-grey warm-up frames, then N_REAL real ones, pts DT apart."""
    out = []
    for i in range(N_GREY):
        data = np.full((FRAME_H, FRAME_W, 3), 114, dtype=np.uint8)
        out.append(Frame(data=data, w=FRAME_W, h=FRAME_H, fmt="RGB",
                         pts=100.0 + i))
    for i in range(N_REAL):
        rng = np.random.default_rng(5500 + i)
        data = rng.integers(0, 256, (FRAME_H, FRAME_W, 3), dtype=np.uint8)
        out.append(Frame(data=data, w=FRAME_W, h=FRAME_H, fmt="RGB",
                         pts=400.0 + i * DT))
    return out


class _FakeSource:
    """Fake camera that HONOURS the frame-source flags `start()` passes it.

    `direct_preprocess=True` (`model_frame = "hw-direct"`) is emulated
    faithfully: the letterboxed model image replaces `data` while `w`/`h` stay
    the original camera geometry -- exactly what would break the stage-2 ROI
    crop.
    """

    def __init__(self, *a, **kw):
        self.closed = False
        self.kw = kw

    def frames(self):
        direct = bool(self.kw.get("direct_preprocess"))
        size = int(self.kw.get("input_size") or 0)
        for f in _fixed_frames():
            if direct and size:
                padded, info = letterbox(f.data, size)
                f = Frame(data=padded, w=f.w, h=f.h, fmt=f.fmt, pts=f.pts,
                          model_info=info)
            yield f

    def close(self):
        self.closed = True


class _FakeDetModel:
    """A scripted yolov8n-face rawhead: 1 box branch + 1 class branch per call.

    One face (score 0.91), except on `NO_FACE_DET_CALL` where the whole class
    branch sits below the threshold so the app must tick its temporal logic with
    no landmarks at all.
    """

    def __init__(self, path):
        self.path = path
        self.calls = 0
        self.input_shapes = []
        self.released = False

    def infer(self, x):
        self.input_shapes.append(tuple(np.asarray(x).shape))
        k = self.calls
        self.calls += 1
        box = np.zeros((1, 4 * REG_MAX, GRID, GRID), dtype=np.float32)
        cls = np.full((1, 1, GRID, GRID), 0.02, dtype=np.float32)
        if k != NO_FACE_DET_CALL:
            col, row = 8 + (k % 3), 7
            cls[0, 0, row, col] = 0.91
            for side in range(4):
                box[0, side * REG_MAX + HALF_BIN, row, col] = 12.0
        return [box, cls]

    def release(self):
        self.released = True


def _facemesh_points(ear: float, mar: float) -> np.ndarray:
    """468x3 landmarks in ROI (192) pixel space with a PRESCRIBED EAR / MAR.

    EAR = (|p1-p5| + |p2-p4|) / (2|p0-p3|) with a 40 px horizontal eye width, so
    the half-opening is `ear * 20`. MAR = |upper-lower| / |left-right| with a
    50 px mouth width, so the opening is `mar * 50`. Both survive the ROI->frame
    mapping unchanged: the square crop scales x and y by the same factor.
    """
    pts = np.full((N_LMK, 3), 96.0, dtype=np.float32)
    v = ear * 20.0
    for idx, x0 in ((LEFT_EYE_IDX, 20.0), (RIGHT_EYE_IDX, 100.0)):
        p0, p1, p2, p3, p4, p5 = idx
        pts[p0, :2] = (x0, 60.0)
        pts[p3, :2] = (x0 + 40.0, 60.0)
        pts[p1, :2] = (x0 + 13.0, 60.0 - v)
        pts[p5, :2] = (x0 + 13.0, 60.0 + v)
        pts[p2, :2] = (x0 + 27.0, 60.0 - v)
        pts[p4, :2] = (x0 + 27.0, 60.0 + v)
    m = mar * 50.0
    left, upl, upm, upr, right, lowm = MOUTH_IDX
    pts[left, :2] = (70.0, 150.0)
    pts[right, :2] = (120.0, 150.0)
    pts[upm, :2] = (95.0, 150.0 - m / 2.0)
    pts[lowm, :2] = (95.0, 150.0 + m / 2.0)
    pts[upl, :2] = (82.0, 150.0 - m / 3.0)
    pts[upr, :2] = (108.0, 150.0 - m / 3.0)
    return pts


class _FakeLandmarkModel:
    """A scripted face_landmark head: (1,1,1,1404) points + (1,1,1,1) presence.

    Seeded by call number against the CLOSED_CALLS / YAWN_CALLS / LOW_PRESENCE
    script above, so eye closure, yawns and the presence drop-out land on
    exactly the same frames in both runs.
    """

    def __init__(self, path):
        self.path = path
        self.calls = 0
        self.input_shapes = []
        self.released = False

    def infer(self, x):
        self.input_shapes.append(tuple(np.asarray(x).shape))
        k = self.calls
        self.calls += 1
        ear = EAR_CLOSED if k in CLOSED_CALLS else EAR_OPEN
        mar = MAR_YAWN if k in YAWN_CALLS else MAR_SHUT
        pres = PRESENCE_LOW if k in LOW_PRESENCE_CALLS else PRESENCE_OK
        pts = _facemesh_points(ear, mar)
        return [pts.reshape(1, 1, 1, N_LMK * 3),
                np.array([[[[pres]]]], dtype=np.float32)]

    def release(self):
        self.released = True


class _RecordingSink(ResultSink):
    def __init__(self):
        self.payloads = []
        self.metas = []
        self.frame_sizes = []

    def emit(self, payload, pts):
        self.payloads.append((json.loads(json.dumps(payload)), pts))

    def emit_meta(self, payload):
        self.metas.append(payload)

    def set_frame_size(self, w, h):
        self.frame_sizes.append((w, h))


# ---- OLD shape: verbatim copy of the pre-migration facemesh-reader ------- #
class _LegacyFacemeshApp(LegacyLoopApp):
    """facemesh-reader exactly as it was before the migration (git d5a40d3).

    Only two mechanical deviations, both test-harness plumbing:
      * the manifest is handed in rather than re-read off disk;
      * the stage-2 CascadePipeline ADOPTS `self._load_model(path)` instead of
        loading `RknnModel(path)` itself -- `_load_model` *is* `RknnModel(path)`
        (kit/app.py), and routing through it lets one stub cover both paths
        (importing kit.runtime.engine off-device would need rknnlite).
    Note it keeps the OLD attribute name `self.pipeline` on purpose: this class
    is the pre-rename reference.
    """
    id = "facemesh-reader"
    name = "Facemesh Reader"
    postproc = "face_detect"

    def __init__(self, manifest):
        super().__init__()
        self._legacy_manifest = manifest

    def setup(self, config):
        super().setup(config)
        manifest = self._legacy_manifest
        params = {k: v for k, v in (config or {}).items() if v is not None}

        self.conf = float(params.get("confidence", 0.4))
        self.iou = float(params.get("iou", 0.45))
        self.crop_pad = float(params.get("crop_pad", 0.25))
        self.presence_threshold = float(params.get("presence_threshold", 0.5))

        lmk_file = "models/face_landmark_fp16.rknn"
        lmk_input = 192
        for m in manifest.get("models", []):
            if m.get("role") == "stage2_landmark" or m.get("task") == "landmark":
                lmk_file = m.get("file", lmk_file)
                inp = m.get("input")
                if isinstance(inp, list) and len(inp) == 4:
                    lmk_input = int(inp[1])
        lmk_path = (lmk_file if os.path.isabs(lmk_file)
                    else os.path.join(APP_DIR, lmk_file))

        self.pipeline = pipeline.CascadePipeline(
            model=self._load_model(lmk_path),
            input_size=lmk_input,
            decode_fn=landmark_post.decode,
            pad=self.crop_pad,
            max_targets=1,
        )

        cfg = DrowsinessConfig(
            ear_threshold=float(params.get("ear_threshold", 0.21)),
            ear_continuous_sec=float(params.get("ear_continuous_sec", 2.0)),
            perclos_window_sec=float(params.get("perclos_window_sec", 60.0)),
            perclos_critical_pct=float(params.get("perclos_critical_pct", 20.0)),
            alert_cooldown_sec=float(params.get("alert_cooldown_sec", 5.0)),
            yawn_count_threshold=int(params.get("yawn_count_threshold", 3)),
        )
        self.logic = DrowsinessLogic(
            drowsy_cfg=cfg,
            mar_threshold=float(params.get("mar_threshold", 0.65)),
            yawn_consecutive_frames=int(params.get("yawn_consecutive_frames", 5)),
            ear_threshold=float(params.get("ear_threshold", 0.21)),
        )
        self._prev_closed = False
        self._blink_count = 0
        self._prev_yawn_count = 0

    def run_postproc(self, outs, info):
        return face_post.postprocess(outs, info, conf_thres=self.conf,
                                     iou_thres=self.iou)

    def on_results(self, results, frame):
        for r in results:
            r["kind"] = "face"

        t = frame.pts
        primary = results[0] if results else None

        landmarks = None
        presence = 0.0
        if primary is not None:
            stage2 = self.pipeline.process(frame.data, [primary])
            if stage2:
                lm_xyz, presence = stage2[0]["decoded"]
                if presence >= self.presence_threshold:
                    landmarks = lm_xyz

        metrics, yawn_state, drowsy_state, yawn_event = self.logic.update(landmarks, t)

        events = []

        if primary is not None:
            primary["presence"] = round(float(presence), 3)
            primary["landmark_count"] = int(len(landmarks)) if landmarks is not None else 0
            if metrics.valid:
                primary["ear"] = round(metrics.avg_ear, 3)
                primary["mar"] = round(metrics.mar, 3)
                primary["keypoints"] = [
                    [round(float(p[0]), 1), round(float(p[1]), 1)]
                    for p in landmarks
                ] if landmarks is not None else []

        events.append({
            "kind": "metrics",
            "face_valid": bool(metrics.valid),
            "avg_ear": round(metrics.avg_ear, 3),
            "left_ear": round(metrics.left_ear, 3),
            "right_ear": round(metrics.right_ear, 3),
            "mar": round(metrics.mar, 3),
            "eyes_closed": bool(metrics.eyes_closed),
            "mouth_open": bool(metrics.mouth_open),
            "state": drowsy_state.state,
            "drowsiness_level": round(drowsy_state.drowsiness_level, 3),
            "perclos_pct": round(drowsy_state.perclos_pct, 1),
            "continuous_closure_sec": round(drowsy_state.continuous_closure_sec, 2),
            "is_yawning": bool(yawn_state.is_yawning_now),
            "yawn_count_5min": int(yawn_state.yawn_count_5min),
            "alert_active": bool(drowsy_state.alert_active),
        })

        if metrics.valid:
            if metrics.eyes_closed and not self._prev_closed:
                self._blink_count += 1
                events.append({"kind": "blink", "blink_count": self._blink_count,
                               "avg_ear": round(metrics.avg_ear, 3)})
            self._prev_closed = metrics.eyes_closed
        else:
            self._prev_closed = False

        if yawn_event:
            events.append({"kind": "yawn",
                           "yawn_count_5min": int(yawn_state.yawn_count_5min),
                           "mar": round(metrics.mar, 3)})

        if drowsy_state.alert_active:
            events.append({
                "kind": "drowsiness",
                "state": drowsy_state.state,
                "drowsiness_level": round(drowsy_state.drowsiness_level, 3),
                "drowsy_by_ear": bool(drowsy_state.drowsy_by_ear),
                "drowsy_by_perclos": bool(drowsy_state.drowsy_by_perclos),
                "drowsy_by_yawn": bool(drowsy_state.drowsy_by_yawn),
                "perclos_pct": round(drowsy_state.perclos_pct, 1),
            })

        return events


def _load_new_app_module():
    path = os.path.join(APP_DIR, "app.py")
    spec = importlib.util.spec_from_file_location(
        "_facemesh_reader_app_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _strip_timing(payloads):
    out = []
    for payload, pts in payloads:
        p = {k: v for k, v in payload.items()
             if k not in ("inference_time_ms", "pipeline_ms")}
        out.append((p, pts))
    return out


class _Base(unittest.TestCase):
    def setUp(self):
        self._orig_open = kit_app.open_frame_source
        self._orig_load = kit_app.App._load_model
        self._orig_crop = pipeline.crop_square_roi

        self.det_models = []
        self.lmk_models = []
        self.crop_source_shapes = []
        self.crop_out_sizes = []

        def _fake_load(app_self, path):
            if "landmark" in os.path.basename(path):
                m = _FakeLandmarkModel(path)
                self.lmk_models.append(m)
            else:
                m = _FakeDetModel(path)
                self.det_models.append(m)
            return m

        def _spy_crop(frame, box, out_size, pad=0.25):
            # ★the load-bearing assertion source★: what pixels does stage 2 get?
            self.crop_source_shapes.append(tuple(np.asarray(frame).shape))
            self.crop_out_sizes.append(int(out_size))
            return self._orig_crop(frame, box, out_size, pad)

        kit_app.open_frame_source = lambda *a, **kw: _FakeSource(*a, **kw)
        kit_app.App._load_model = _fake_load
        pipeline.crop_square_roi = _spy_crop

        with open(os.path.join(APP_DIR, "manifest.json")) as f:
            self.manifest = json.load(f)

    def tearDown(self):
        kit_app.open_frame_source = self._orig_open
        kit_app.App._load_model = self._orig_load
        pipeline.crop_square_roi = self._orig_crop
        try:
            signal.signal(signal.SIGHUP, signal.SIG_DFL)
        except (ValueError, OSError):
            pass

    def _run_old(self, eff):
        sink = _RecordingSink()
        app = _LegacyFacemeshApp(self.manifest)
        app.setup(dict(eff))
        app.run(DET_MODEL, source="ffmpeg", sink=sink, n=0, verbose=False)
        return sink, app

    def _run_new(self, eff, cls=None):
        sink = _RecordingSink()
        app = (cls or _load_new_app_module().FacemeshReaderApp)()
        app.start(DET_MODEL, source="ffmpeg", sink=sink, n=0, verbose=False,
                  app_dir=APP_DIR, manifest=self.manifest, config=dict(eff))
        try:
            app.run()
        finally:
            app.finish()
        return sink, app


class FacemeshEquivalenceTests(_Base):

    def _compare(self, eff, label):
        old, old_app = self._run_old(eff)
        old_crops = list(self.crop_source_shapes)
        old_lmk_calls = self.lmk_models[-1].calls
        self.crop_source_shapes = []
        self.crop_out_sizes = []
        new, new_app = self._run_new(eff)
        new_crops = list(self.crop_source_shapes)

        self.assertEqual(len(old.payloads), N_REAL - 1,
                         f"{label}: old path emitted an unexpected frame count")
        self.assertEqual(len(new.payloads), len(old.payloads),
                         f"{label}: new path emitted a different frame count")

        old_s, new_s = _strip_timing(old.payloads), _strip_timing(new.payloads)
        for i, ((po, pts_o), (pn, pts_n)) in enumerate(zip(old_s, new_s)):
            self.assertEqual(pts_o, pts_n, f"{label} frame {i}: pts differs")
            self.assertEqual(po["stream_id"], pn["stream_id"],
                             f"{label} frame {i}: stream_id differs")
            self.assertEqual(po["results"], pn["results"],
                             f"{label} frame {i}: results differ")
            self.assertEqual(po["events"], pn["events"],
                             f"{label} frame {i}: events differ")
        self.assertEqual(old_s, new_s, f"{label}: payload streams differ")
        self.assertEqual(old.frame_sizes, new.frame_sizes)
        self.assertEqual(old_crops, new_crops,
                         f"{label}: stage-2 crop sources differ")
        self.assertEqual(old_lmk_calls, self.lmk_models[-1].calls,
                         f"{label}: stage-2 call counts differ")
        # the blink counter is the app's own cross-frame state
        self.assertEqual(old_app._blink_count, new_app._blink_count,
                         f"{label}: blink counters diverged")
        return old_s, new_s, old_app, new_app

    def test_deep_equal(self):
        old_s, _new_s, old_app, new_app = self._compare(EFF, "default")

        evs = [e for p, _ in old_s for e in p["events"]]
        kinds = {k: sum(1 for e in evs if e["kind"] == k)
                 for k in sorted({e["kind"] for e in evs})}
        print("\n--- event kind distribution (old path) ---")
        print(kinds)
        print(f"blink_count old={old_app._blink_count} new={new_app._blink_count}")

        # -- anti-vacuous-pass assertions --------------------------------- #
        self.assertGreater(len(evs), 0, "fixture produced no events")
        self.assertGreater(kinds.get("metrics", 0), 0, "no metrics events")
        self.assertGreater(kinds.get("blink", 0), 0, "no blink ever fired")
        self.assertGreater(kinds.get("yawn", 0), 0, "no yawn ever fired")
        self.assertGreater(kinds.get("drowsiness", 0), 0,
                           "the drowsiness alert never became active")
        mets = [e for e in evs if e["kind"] == "metrics"]
        self.assertTrue(any(not e["face_valid"] for e in mets),
                        "the no-face / low-presence branch never ran")
        self.assertTrue(any(e["face_valid"] for e in mets),
                        "no frame ever had a valid face")
        self.assertGreater(max(e["perclos_pct"] for e in mets), 0.0,
                           "PERCLOS never rose above zero")
        self.assertGreater(len({e["state"] for e in mets}), 1,
                           "the drowsiness state machine never changed state")

    def test_temporal_state_frame_by_frame(self):
        """blink / yawn / PERCLOS / state, printed and compared per frame."""
        old, _ = self._run_old(EFF)
        self.crop_source_shapes = []
        self.crop_out_sizes = []
        new, _ = self._run_new(EFF)

        def _table(sink):
            rows = []
            for p, pts in sink.payloads:
                m = [e for e in p["events"] if e["kind"] == "metrics"][0]
                blink = [e for e in p["events"] if e["kind"] == "blink"]
                yawn = [e for e in p["events"] if e["kind"] == "yawn"]
                drow = [e for e in p["events"] if e["kind"] == "drowsiness"]
                rows.append((round(pts, 2), m["face_valid"], m["avg_ear"],
                             m["mar"], m["eyes_closed"], m["yawn_count_5min"],
                             m["perclos_pct"], m["state"], m["alert_active"],
                             blink[0]["blink_count"] if blink else None,
                             bool(yawn), bool(drow)))
            return rows

        old_tbl, new_tbl = _table(old), _table(new)
        header = ("pts, valid, ear, mar, closed, yawns, perclos, state, "
                  "alert, blink#, yawn_ev, drowsy_ev")
        print(f"\n--- per-frame ({header}) ---")
        for i, (o, n) in enumerate(zip(old_tbl, new_tbl)):
            print(f"frame {i:2d}: OLD {o}")
            print(f"frame {i:2d}: NEW {n}")
            print(f"frame {i:2d}: EQUAL={o == n}")
        self.assertEqual(old_tbl, new_tbl, "temporal state diverged")

    def test_infer_call_counts_match(self):
        self._run_old(EFF)
        old = (self.det_models[-1].calls, self.lmk_models[-1].calls)
        self.crop_source_shapes = []
        self.crop_out_sizes = []
        self._run_new(EFF)
        new = (self.det_models[-1].calls, self.lmk_models[-1].calls)
        print(f"\ninfer calls (det, landmark): OLD {old} NEW {new}")
        self.assertEqual(old, new)
        self.assertGreater(old[1], 0, "stage 2 never ran")
        self.assertLess(old[1], old[0] - 1,
                        "the no-face frame did not skip stage 2")


class FacemeshFrameGeometryTests(_Base):
    """★The design point★: the model image and the crop source are two images."""

    def test_crop_source_is_the_original_frame_not_the_model_image(self):
        self._run_new(EFF)
        shapes = set(self.crop_source_shapes)
        self.assertTrue(self.crop_source_shapes, "stage 2 never cropped")
        print("\ncrop_square_roi input shapes (new path):", shapes)
        self.assertEqual(shapes, {(FRAME_H, FRAME_W, 3)},
                         "stage 2 was handed something other than the original "
                         f"{FRAME_H}x{FRAME_W} frame")
        self.assertNotIn((DET_SIZE, DET_SIZE, 3), shapes,
                         "stage 2 got the 640x640 model image")

    def test_stage1_input_is_640_and_landmark_input_is_192(self):
        self._run_new(EFF)
        det_shapes = set(self.det_models[-1].input_shapes)
        lmk_shapes = set(self.lmk_models[-1].input_shapes)
        print("det infer input shapes:", det_shapes)
        print("landmark infer input shapes:", lmk_shapes)
        print("crop out_size values:", set(self.crop_out_sizes))
        self.assertEqual(det_shapes, {(DET_SIZE, DET_SIZE, 3)},
                         "self.pre() did not letterbox to the stage-1 640")
        self.assertEqual(lmk_shapes, {(LMK_SIZE, LMK_SIZE, 3)},
                         "the landmark model did not get a 192x192 ROI")
        self.assertEqual(set(self.crop_out_sizes), {LMK_SIZE})

    def test_new_app_keeps_cpu_frame_mode(self):
        mod = _load_new_app_module()
        self.assertEqual(mod.FacemeshReaderApp.model_frame, "cpu",
                         "facemesh-reader must not letterbox into frame.data")

    def test_negative_control_hw_direct_would_break_the_crop(self):
        """Proof the assertion above is load-bearing, not a tautology."""
        cls = _load_new_app_module().FacemeshReaderApp
        cls.model_frame = "hw-direct"
        self._run_new(EFF, cls=cls)
        shapes = set(self.crop_source_shapes)
        print("\nnegative control (hw-direct) crop source shapes:", shapes)
        self.assertEqual(shapes, {(DET_SIZE, DET_SIZE, 3)},
                         "the fixture no longer detects a model_frame change")
        self.assertNotIn((FRAME_H, FRAME_W, 3), shapes)


class FacemeshNewShapeTests(_Base):
    """New-shape specifics: rename, auto-binding, registry, in-place re-bind."""

    def _started(self, eff=None):
        app = _load_new_app_module().FacemeshReaderApp()
        app.start(DET_MODEL, sink=_RecordingSink(), verbose=False,
                  app_dir=APP_DIR, manifest=self.manifest,
                  config=dict(eff or EFF))
        return app

    def test_pipeline_attribute_is_gone(self):
        """`self.pipeline` shadowed nothing today, but must not come back."""
        app = self._started()
        try:
            self.assertFalse(hasattr(app, "pipeline"),
                             "self.pipeline is back; the stage-2 object is "
                             "self.cascade")
            self.assertIsInstance(app.cascade, pipeline.CascadePipeline)
        finally:
            app.finish()

    def test_params_auto_bound_from_manifest_schema(self):
        app = self._started({"confidence": 0.6, "iou": 0.5, "crop_pad": 0.4,
                             "presence_threshold": 0.7, "ear_threshold": 0.18,
                             "mar_threshold": 0.8,
                             "yawn_consecutive_frames": 7.0,
                             "ear_continuous_sec": 3.0,
                             "perclos_window_sec": 30.0,
                             "perclos_critical_pct": 25.0,
                             "alert_cooldown_sec": 1.0,
                             "yawn_count_threshold": 4.0})
        try:
            self.assertEqual(app.confidence, 0.6)
            self.assertEqual(app.crop_pad, 0.4)
            self.assertEqual(app.presence_threshold, 0.7)
            self.assertEqual(app.cascade.pad, 0.4)
            self.assertEqual(app.logic.ear_threshold, 0.18)
            self.assertEqual(app.logic.mar_threshold, 0.8)
            self.assertEqual(app.logic.yawn.consecutive_frames, 7)
            self.assertEqual(app.logic.drowsy.cfg.perclos_window_sec, 30.0)
            self.assertEqual(app.logic.drowsy.cfg.yawn_count_threshold, 4)
        finally:
            app.finish()

    def test_live_rebind_mutates_derived_objects_in_place(self):
        """★crop_pad / EAR / MAR mutate; the temporal accumulators survive★."""
        app = self._started()
        try:
            cascade_before = app.cascade
            logic_before = app.logic
            yawn_before = app.logic.yawn
            cfg_before = app.logic.drowsy.cfg
            # seed some cross-frame state
            app._blink_count = 4
            app._prev_closed = True
            app.logic.drowsy.update(0.1, 1000.0, 0)
            perclos_before = len(app.logic.drowsy._perclos)

            changed = app._bind_params({"crop_pad": 0.5, "ear_threshold": 0.15,
                                        "mar_threshold": 0.9,
                                        "yawn_consecutive_frames": 9.0,
                                        "alert_cooldown_sec": 2.0},
                                       live_only=True)
            self.assertEqual(changed, {"crop_pad", "ear_threshold",
                                       "mar_threshold",
                                       "yawn_consecutive_frames",
                                       "alert_cooldown_sec"})
            app.on_params_changed(changed)

            self.assertIs(app.cascade, cascade_before, "cascade was REBUILT")
            self.assertIs(app.logic, logic_before, "logic was REBUILT")
            self.assertIs(app.logic.yawn, yawn_before, "yawn tracker REBUILT")
            self.assertIs(app.logic.drowsy.cfg, cfg_before, "cfg REBUILT")
            self.assertEqual(app.cascade.pad, 0.5)
            self.assertEqual(app.logic.ear_threshold, 0.15)
            self.assertEqual(app.logic.mar_threshold, 0.9)
            self.assertEqual(app.logic.yawn.mar_threshold, 0.9)
            self.assertEqual(app.logic.yawn.consecutive_frames, 9)
            self.assertEqual(app.logic.drowsy.cfg.ear_threshold, 0.15)
            self.assertEqual(app.logic.drowsy.cfg.alert_cooldown_sec, 2.0)
            # ★the point★: accumulators untouched
            self.assertEqual(len(app.logic.drowsy._perclos), perclos_before)
            self.assertGreater(perclos_before, 0, "the PERCLOS deque was empty; the survival check is vacuous")
            self.assertEqual(app._blink_count, 4)
            self.assertTrue(app._prev_closed)
        finally:
            app.finish()

    def test_restart_param_not_rebound_live(self):
        app = self._started()
        try:
            changed = app._bind_params({"perclos_window_sec": 5.0},
                                       live_only=True)
            self.assertEqual(changed, set())
            self.assertEqual(app.logic.drowsy.cfg.perclos_window_sec, 60.0)
        finally:
            app.finish()

    def test_both_models_come_from_the_registry(self):
        app = self._started()
        try:
            self.assertEqual(len(app.models), 2)
            self.assertEqual(os.path.basename(app.models.det.path),
                             "yolov8n_face_rawhead_fp16.rknn")
            self.assertEqual(os.path.basename(app.models.lmk.path),
                             "face_landmark_fp16.rknn")
            self.assertTrue(os.path.isabs(app.models.lmk.path))
            # the cascade ADOPTED the preloaded handle, it did not load a 2nd
            self.assertIs(app.cascade.model, app.models.lmk)
            self.assertEqual(len(self.lmk_models), 1,
                             "the landmark rknn was loaded twice")
        finally:
            app.finish()


if __name__ == "__main__":
    unittest.main()
