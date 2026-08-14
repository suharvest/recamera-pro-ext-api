"""
Equivalence gate for the face-analysis app-shape migration (KIT_APP_SHAPE_SPEC §7).

face-analysis is the THREE-STAGE CASCADE + CROSS-FRAME-STATE case, and it is the
app that pins down two things at once:

  * stages 2 and 3 crop a padded SQUARE ROI out of the ORIGINAL camera pixels
    (`kit.pipeline.crop_square_roi` on `frame.data`), so `model_frame` must stay
    "cpu" while `self.pre()` letterboxes to 640 -- two DIFFERENT images;
  * the demographic window (`_win_start` / `_hist` / `_win_faces`), the frame
    counter (`_frame_idx`) and the per-slot emotion cache (`_emotion_cache`)
    all persist between frames. `emotion_interval` means stage 3 runs only every
    N frames and the cached verdict is reused in between -- a cadence the
    migration must reproduce frame for frame, not just on average.

Hardware-free: the frame source (`kit.app.open_frame_source`) and the RKNN
engine (`kit.app.App._load_model`) are stubbed with deterministic fakes, then
the SAME fixed frame sequence is pushed through

  OLD path : the pre-migration face-analysis (git d5a40d3), reproduced verbatim
             below -- base `App.run()` loop + `run_postproc()` + `on_results()`;
  NEW path : the migrated `apps/face-analysis/app.py` -- `owns_loop = True`,
             `run()` + `for frame in self.frames()` + a plain stage-2/3 loop,

and `results` / `events` / `pts` / `stream_id` are compared field for field.

All three fake models are seeded BY CALL NUMBER, so the k-th detector (resp.
FairFace, resp. emotion) inference of the old run and of the new run are
byte-identical -- any downstream difference is a behaviour difference, and a
diverging number of stage-3 calls (i.e. a broken emotion cadence) shows up
immediately as a differing emotion label.

Run: `python3 -m pytest kit/tests/test_face_shape_equivalence.py -q`
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
from kit import pipeline                                             # noqa: E402
from kit.adapters.frame_source import Frame                          # noqa: E402
from kit.adapters.result_sink import ResultSink                      # noqa: E402
from kit.runtime.postprocess import classify as clf                  # noqa: E402
from kit.runtime.postprocess import face_detect as face_post         # noqa: E402
from kit.runtime.preprocess import letterbox                         # noqa: E402

APP_DIR = os.path.join(_REPO, "apps", "face-analysis")

FRAME_W, FRAME_H = 640, 480     # camera frame: NOT square, NOT the model size
DET_SIZE = 640                  # stage-1 input side (manifest models[0].input)
CLS_SIZE = 224                  # stage-2/3 classifier input side
N_GREY = 2                      # camera warm-up placeholders, both paths skip
N_REAL = 10                     # real frames offered by the fake source
DT = 0.2                        # seconds between frames

DET_MODEL = "models/yolov8n_face_rawhead_fp16.rknn"

GRID = 20                       # single FPN level: 20x20 @ stride 32
STRIDE = DET_SIZE // GRID
REG_MAX = 16
HALF_BIN = 2                    # DFL bin -> box half-side = 2 * 32 = 64 px

# Effective config, as kit.config would hand it over (manifest defaults, except
# the three tuned so the fixture actually exercises the branches):
#   max_faces 2 (< the 3 faces the fake detector emits)  -> top-K slice is live
#   emotion_interval 3                                   -> stage 3 skips frames
#   aggregate_window_sec 0.5 (with DT=0.2)               -> the window fires
EFF = {
    "confidence": 0.4,
    "iou": 0.45,
    "max_faces": 2.0,
    "crop_pad": 0.15,
    "emotion_interval": 3.0,
    "aggregate_window_sec": 0.5,
    "privacy_blur": True,
}


def _fixed_frames():
    """N_GREY flat-grey warm-up frames, then N_REAL real ones, pts DT apart."""
    out = []
    for i in range(N_GREY):
        data = np.full((FRAME_H, FRAME_W, 3), 114, dtype=np.uint8)
        out.append(Frame(data=data, w=FRAME_W, h=FRAME_H, fmt="RGB",
                         pts=100.0 + i))
    for i in range(N_REAL):
        rng = np.random.default_rng(7700 + i)
        data = rng.integers(0, 256, (FRAME_H, FRAME_W, 3), dtype=np.uint8)
        out.append(Frame(data=data, w=FRAME_W, h=FRAME_H, fmt="RGB",
                         pts=300.0 + i * DT))
    return out


class _FakeSource:
    """Fake camera that HONOURS the frame-source flags `start()` passes it.

    `direct_preprocess=True` (what `model_frame = "hw-direct"` asks for) is
    emulated faithfully: the letterboxed model image replaces `data` while
    `w`/`h` stay the original camera geometry -- exactly what would break the
    stage-2/3 ROI crop. That makes the crop-source assertion a real regression
    detector rather than a restatement of the class attribute.
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

    One FPN level (20x20 @ stride 32) is enough for `_decode_dfl`: it pairs the
    64-channel DFL box branch with the 1-channel face-score branch. Three faces
    sit at fixed grid cells (the first one drifting sideways with the call
    number) with scores 0.91 / 0.85 / 0.62, so `max_faces = 2` really drops one.
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
        #        (col,            row, score)
        faces = [(4 + (k % 3),      6, 0.91),
                 (12,               5, 0.85),
                 (8,               12, 0.62)]
        for col, row, score in faces:
            cls[0, 0, row, col] = score
            for side in range(4):
                box[0, side * REG_MAX + HALF_BIN, row, col] = 12.0
        if k == 5:
            # ★one frame with NO face at all★ (det call 0 is the kit warm-up,
            # so this is processed frame #4): the
            # top-K slice is empty, no ROI is cropped, no classifier runs.
            cls[:] = 0.02
        return [box, cls]

    def release(self):
        self.released = True


class _FakeFairFaceModel:
    """A scripted FairFace head: one (1,18) logit vector per call.

    Seeded by call number so race / gender / age all move: the k-th call of the
    old run and of the new run are byte-identical.
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
        vec = np.full((1, 18), 0.1, dtype=np.float32)
        vec[0, 0 + (k % 7)] = 3.0          # race head  [0:7]
        vec[0, 7 + (k % 2)] = 2.5          # gender head [7:9]
        vec[0, 9 + (k % 9)] = 4.0          # age head    [9:18]
        return [vec]

    def release(self):
        self.released = True


class _FakeEmotionModel:
    """A scripted emotion head: one (1,8) logit vector per call, seeded by k."""

    def __init__(self, path):
        self.path = path
        self.calls = 0
        self.input_shapes = []
        self.released = False

    def infer(self, x):
        self.input_shapes.append(tuple(np.asarray(x).shape))
        k = self.calls
        self.calls += 1
        vec = np.full((1, 8), 0.1, dtype=np.float32)
        vec[0, k % 8] = 3.0
        return [vec]

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


# ---- OLD shape: verbatim copy of the pre-migration face-analysis --------- #
class _LegacyFaceApp(kit_app.App):
    """face-analysis exactly as it was before the migration (git d5a40d3).

    Only three mechanical deviations, all test-harness plumbing:
      * the manifest is handed in rather than re-read off disk;
      * the stage-2/3 models are built through `self._load_model(path)` instead
        of `RknnModel(path)` -- `_load_model` *is* `RknnModel(path)`
        (kit/app.py), and routing through it lets one stub cover both paths
        (importing kit.runtime.engine off-device would need rknnlite);
      * `crop_square_roi` is reached through the `pipeline` module so the same
        spy sees both paths (it is the identical function object).
    """
    id = "face-analysis"
    name = "Face Analysis"
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
        self.max_faces = int(params.get("max_faces", 5))
        self.crop_pad = float(params.get("crop_pad", 0.15))

        self.emotion_interval = max(1, int(params.get("emotion_interval", 1)))
        self.agg_window = float(params.get("aggregate_window_sec", 30.0))
        self.privacy_blur = bool(params.get("privacy_blur", True))

        ff_file, ff_input = "models/fairface_fp16.rknn", 224
        emo_file, emo_input = "models/emotion_enet_b0_fp16.rknn", 224
        for m in manifest.get("models", []):
            role = m.get("role")
            inp = m.get("input")
            side = int(inp[1]) if isinstance(inp, list) and len(inp) == 4 else None
            if role == "stage2_fairface" or m.get("id", "").startswith("fairface"):
                ff_file = m.get("file", ff_file)
                if side:
                    ff_input = side
            elif role == "stage3_emotion" or m.get("id", "").startswith("emotion"):
                emo_file = m.get("file", emo_file)
                if side:
                    emo_input = side

        def _abs(p):
            return p if os.path.isabs(p) else os.path.join(APP_DIR, p)

        self.ff_input = ff_input
        self.emo_input = emo_input
        self.ff_model = self._load_model(_abs(ff_file))
        self.emo_model = self._load_model(_abs(emo_file))

        self._win_start = None
        self._win_faces = 0
        self._hist = {"gender": {}, "age": {}, "race": {}, "emotion": {}}
        self._frame_idx = 0
        self._emotion_cache = {}

    def run_postproc(self, outs, info):
        return face_post.postprocess(outs, info, conf_thres=self.conf,
                                     iou_thres=self.iou)

    def _bump(self, head, label):
        if label is None:
            return
        d = self._hist[head]
        d[label] = d.get(label, 0) + 1

    def _roll_window(self, t):
        if self._win_start is None:
            self._win_start = t
            return None
        if (t - self._win_start) < self.agg_window:
            return None
        event = {
            "kind": "demographics",
            "window_sec": round(float(t - self._win_start), 1),
            "faces": int(self._win_faces),
            "gender": dict(self._hist["gender"]),
            "age": dict(self._hist["age"]),
            "race": dict(self._hist["race"]),
            "emotion": dict(self._hist["emotion"]),
        }
        self._win_start = t
        self._win_faces = 0
        for k in self._hist:
            self._hist[k] = {}
        return event

    def on_results(self, results, frame):
        self._frame_idx += 1
        t = frame.pts
        run_emotion = (self._frame_idx % self.emotion_interval) == 0

        faces = results[: self.max_faces]
        for i, r in enumerate(faces):
            r["kind"] = "face"
            r["blur"] = self.privacy_blur

            roi, _roi_map = pipeline.crop_square_roi(frame.data, r["box"],
                                                     self.ff_input,
                                                     self.crop_pad)

            ff = clf.fairface_decode(self.ff_model.infer(roi))
            r["gender"] = ff["gender"]["label"]
            r["gender_conf"] = ff["gender"]["confidence"]
            r["age"] = ff["age"]["label"]
            r["age_conf"] = ff["age"]["confidence"]
            r["race"] = ff["race"]["label"]
            r["race_conf"] = ff["race"]["confidence"]

            if run_emotion:
                if self.emo_input != self.ff_input:
                    roi_e, _ = pipeline.crop_square_roi(frame.data, r["box"],
                                                        self.emo_input,
                                                        self.crop_pad)
                else:
                    roi_e = roi
                em = clf.emotion_decode(self.emo_model.infer(roi_e))
                self._emotion_cache[i] = em
            em = self._emotion_cache.get(i)
            if em is not None:
                r["emotion"] = em["label"]
                r["emotion_conf"] = em["confidence"]

            self._win_faces += 1
            self._bump("gender", r.get("gender"))
            self._bump("age", r.get("age"))
            self._bump("race", r.get("race"))
            if em is not None:
                self._bump("emotion", em["label"])

        events = []
        for r in faces:
            events.append({
                "kind": "face",
                "box": r["box"],
                "score": r.get("score"),
                "gender": r.get("gender"),
                "gender_conf": r.get("gender_conf"),
                "age": r.get("age"),
                "age_conf": r.get("age_conf"),
                "race": r.get("race"),
                "race_conf": r.get("race_conf"),
                "emotion": r.get("emotion"),
                "emotion_conf": r.get("emotion_conf"),
                "blur": bool(self.privacy_blur),
            })

        agg = self._roll_window(t)
        if agg is not None:
            events.append(agg)
        return events


def _load_new_app_module():
    path = os.path.join(APP_DIR, "app.py")
    spec = importlib.util.spec_from_file_location(
        "_face_analysis_app_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    # The app does `from kit.pipeline import crop_square_roi`, so re-point the
    # module-level name at whatever kit.pipeline currently exposes (the spy).
    mod.crop_square_roi = pipeline.crop_square_roi
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
        self.ff_models = []
        self.emo_models = []
        self.crop_source_shapes = []
        self.crop_out_sizes = []

        def _fake_load(app_self, path):
            base = os.path.basename(path)
            if "fairface" in base:
                m = _FakeFairFaceModel(path)
                self.ff_models.append(m)
            elif "emotion" in base:
                m = _FakeEmotionModel(path)
                self.emo_models.append(m)
            else:
                m = _FakeDetModel(path)
                self.det_models.append(m)
            return m

        def _spy_crop(frame, box, out_size, pad=0.25):
            # ★the load-bearing assertion source★: what pixels do stages 2/3 get?
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
        app = _LegacyFaceApp(self.manifest)
        app.setup(dict(eff))
        app.run(DET_MODEL, source="ffmpeg", sink=sink, n=0, verbose=False)
        return sink, app

    def _run_new(self, eff, cls=None):
        sink = _RecordingSink()
        app = (cls or _load_new_app_module().FaceAnalysisApp)()
        app.start(DET_MODEL, source="ffmpeg", sink=sink, n=0, verbose=False,
                  app_dir=APP_DIR, manifest=self.manifest, config=dict(eff))
        try:
            app.run()
        finally:
            app.finish()
        return sink, app


class FaceEquivalenceTests(_Base):

    def _compare(self, eff, label):
        old, old_app = self._run_old(eff)
        old_crops = list(self.crop_source_shapes)
        old_ff_calls = self.ff_models[-1].calls
        old_emo_calls = self.emo_models[-1].calls
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
                         f"{label}: stage-2/3 crop sources differ")
        self.assertEqual((old_ff_calls, old_emo_calls),
                         (self.ff_models[-1].calls, self.emo_models[-1].calls),
                         f"{label}: classifier call counts differ")
        return old_s, new_s

    def test_deep_equal(self):
        old_s, _new_s = self._compare(EFF, "default")

        # -- anti-vacuous-pass assertions --------------------------------- #
        evs = [e for p, _ in old_s for e in p["events"]]
        self.assertGreater(len(evs), 0, "fixture produced no events")
        faces = [e for e in evs if e["kind"] == "face"]
        demog = [e for e in evs if e["kind"] == "demographics"]
        self.assertGreater(len(faces), 0, "no face attribute events")
        self.assertGreater(len(demog), 0, "the aggregation window never fired")
        self.assertTrue(any(e["emotion"] is None for e in faces),
                        "every face carried an emotion: the interval never skipped")
        self.assertTrue(any(e["emotion"] is not None for e in faces),
                        "no face ever carried an emotion")
        self.assertTrue(any(len(p["results"]) == 0 for p, _ in old_s),
                        "the no-face frame is missing from the fixture")
        self.assertTrue(any(len(p["results"]) > len(
            [e for e in p["events"] if e["kind"] == "face"]) for p, _ in old_s),
            "max_faces never dropped a detection: the top-K slice is untested")
        print("\n--- event kind distribution (old path) ---")
        print({k: sum(1 for e in evs if e["kind"] == k)
               for k in sorted({e["kind"] for e in evs})})

    def test_cross_frame_state_frame_by_frame(self):
        """Emotion cadence + window aggregation, printed and compared per frame."""
        old, _ = self._run_old(EFF)
        self.crop_source_shapes = []
        self.crop_out_sizes = []
        new, _ = self._run_new(EFF)

        def _table(sink):
            rows = []
            for p, pts in sink.payloads:
                faces = [e for e in p["events"] if e["kind"] == "face"]
                demo = [e for e in p["events"] if e["kind"] == "demographics"]
                rows.append((round(pts, 2), len(p["results"]), len(faces),
                             [e["emotion"] for e in faces],
                             [e["age"] for e in faces],
                             demo[0] if demo else None))
            return rows

        old_tbl, new_tbl = _table(old), _table(new)
        print("\n--- per-frame (pts, dets, faces, emotions, ages, demographics) ---")
        for i, (o, n) in enumerate(zip(old_tbl, new_tbl)):
            print(f"frame {i}: OLD {o}")
            print(f"frame {i}: NEW {n}")
            print(f"frame {i}: EQUAL={o == n}")
        self.assertEqual(old_tbl, new_tbl)
        self.assertEqual(old_tbl, new_tbl, "cross-frame state diverged")

    def test_emotion_interval_cadence(self):
        """Stage 3 must run on frames 3/6/9 only, and cache in between."""
        self._run_new(EFF)
        emo_calls = self.emo_models[-1].calls
        ff_calls = self.ff_models[-1].calls
        print(f"\nfairface calls={ff_calls} emotion calls={emo_calls}")
        self.assertGreater(emo_calls, 0, "stage 3 never ran")
        self.assertLess(emo_calls, ff_calls,
                        "stage 3 ran as often as stage 2: the interval is dead")

    def test_infer_call_counts_match(self):
        self._run_old(EFF)
        old = (self.det_models[-1].calls, self.ff_models[-1].calls,
               self.emo_models[-1].calls)
        self.crop_source_shapes = []
        self.crop_out_sizes = []
        self._run_new(EFF)
        new = (self.det_models[-1].calls, self.ff_models[-1].calls,
               self.emo_models[-1].calls)
        print(f"\ninfer calls (det, fairface, emotion): OLD {old} NEW {new}")
        self.assertEqual(old, new)


class FaceFrameGeometryTests(_Base):
    """★The design point★: the model image and the crop source are two images."""

    def test_crop_source_is_the_original_frame_not_the_model_image(self):
        self._run_new(EFF)
        shapes = set(self.crop_source_shapes)
        self.assertTrue(self.crop_source_shapes, "stage 2/3 never cropped")
        print("\ncrop_square_roi input shapes (new path):", shapes)
        self.assertEqual(shapes, {(FRAME_H, FRAME_W, 3)},
                         "stage 2/3 was handed something other than the original "
                         f"{FRAME_H}x{FRAME_W} frame")
        self.assertNotIn((DET_SIZE, DET_SIZE, 3), shapes,
                         "stage 2/3 got the 640x640 model image")

    def test_stage1_input_is_640_and_classifier_input_is_224(self):
        self._run_new(EFF)
        det_shapes = set(self.det_models[-1].input_shapes)
        ff_shapes = set(self.ff_models[-1].input_shapes)
        emo_shapes = set(self.emo_models[-1].input_shapes)
        print("det infer input shapes:", det_shapes)
        print("fairface infer input shapes:", ff_shapes)
        print("emotion infer input shapes:", emo_shapes)
        print("crop out_size values:", set(self.crop_out_sizes))
        self.assertEqual(det_shapes, {(DET_SIZE, DET_SIZE, 3)},
                         "self.pre() did not letterbox to the stage-1 640")
        self.assertEqual(ff_shapes, {(CLS_SIZE, CLS_SIZE, 3)},
                         "FairFace did not get a 224x224 ROI")
        self.assertEqual(emo_shapes, {(CLS_SIZE, CLS_SIZE, 3)},
                         "the emotion model did not get a 224x224 ROI")
        self.assertEqual(set(self.crop_out_sizes), {CLS_SIZE})

    def test_new_app_keeps_cpu_frame_mode(self):
        mod = _load_new_app_module()
        self.assertEqual(mod.FaceAnalysisApp.model_frame, "cpu",
                         "face-analysis must not letterbox into frame.data")

    def test_negative_control_hw_direct_would_break_the_crop(self):
        """Proof the assertion above is load-bearing, not a tautology.

        Flip the app to "hw-direct" and the fake source (which honours the flag
        the way OfficialFrameSource does) hands stages 2/3 the 640x640 model
        image -- i.e. the previous test WOULD fail. If this control ever stops
        producing 640x640 crop sources, the guard has gone blind.
        """
        cls = _load_new_app_module().FaceAnalysisApp
        cls.model_frame = "hw-direct"
        self._run_new(EFF, cls=cls)
        shapes = set(self.crop_source_shapes)
        print("\nnegative control (hw-direct) crop source shapes:", shapes)
        self.assertEqual(shapes, {(DET_SIZE, DET_SIZE, 3)},
                         "the fixture no longer detects a model_frame change")
        self.assertNotIn((FRAME_H, FRAME_W, 3), shapes)


class FaceNewShapeTests(_Base):
    """New-shape specifics: auto-binding, model registry, live re-bind."""

    def _started(self, eff=None):
        app = _load_new_app_module().FaceAnalysisApp()
        app.start(DET_MODEL, sink=_RecordingSink(), verbose=False,
                  app_dir=APP_DIR, manifest=self.manifest,
                  config=dict(eff or EFF))
        return app

    def test_params_auto_bound_from_manifest_schema(self):
        app = self._started({"confidence": 0.55, "iou": 0.5, "max_faces": 3.0,
                             "crop_pad": 0.3, "emotion_interval": 4.0,
                             "aggregate_window_sec": 12.0,
                             "privacy_blur": False})
        try:
            self.assertEqual(app.confidence, 0.55)
            self.assertEqual(app.iou, 0.5)
            self.assertEqual(app.max_faces, 3.0)
            self.assertEqual(app.crop_pad, 0.3)
            self.assertEqual(app.emotion_interval, 4.0)
            self.assertEqual(app.aggregate_window_sec, 12.0)
            self.assertIs(app.privacy_blur, False)
        finally:
            app.finish()

    def test_live_rebind_keeps_cross_frame_state(self):
        app = self._started()
        try:
            app._win_faces = 7
            app._hist["gender"]["Male"] = 3
            app._emotion_cache[0] = {"label": "Happiness", "confidence": 0.9}
            app._frame_idx = 5
            changed = app._bind_params({"confidence": 0.7, "max_faces": 4.0},
                                       live_only=True)
            self.assertEqual(changed, {"confidence", "max_faces"})
            app.on_params_changed(changed)
            self.assertEqual(app.confidence, 0.7)
            self.assertEqual(app.max_faces, 4.0)
            # ★the point★: the aggregation window / cache survived untouched
            self.assertEqual(app._win_faces, 7)
            self.assertEqual(app._hist["gender"], {"Male": 3})
            self.assertEqual(app._emotion_cache[0]["label"], "Happiness")
            self.assertEqual(app._frame_idx, 5)
        finally:
            app.finish()

    def test_restart_param_not_rebound_live(self):
        app = self._started()
        try:
            changed = app._bind_params({"aggregate_window_sec": 999.0},
                                       live_only=True)
            self.assertEqual(changed, set())
            self.assertEqual(app.aggregate_window_sec,
                             EFF["aggregate_window_sec"])
        finally:
            app.finish()

    def test_all_three_models_come_from_the_registry(self):
        app = self._started()
        try:
            self.assertEqual(len(app.models), 3)
            self.assertEqual(os.path.basename(app.models.det.path),
                             "yolov8n_face_rawhead_fp16.rknn")
            self.assertEqual(os.path.basename(app.models["fairface_fp16"].path),
                             "fairface_fp16.rknn")
            self.assertEqual(
                os.path.basename(app.models["emotion_enet_b0_fp16"].path),
                "emotion_enet_b0_fp16.rknn")
            self.assertTrue(os.path.isabs(app.models["fairface_fp16"].path))
            self.assertFalse(hasattr(app, "ff_model"),
                             "the hand-rolled stage-2 loader should be gone")
            self.assertFalse(hasattr(app, "emo_model"),
                             "the hand-rolled stage-3 loader should be gone")
        finally:
            app.finish()

    def test_classify_alias_is_ambiguous_by_design(self):
        """Two models claim task `classify`, so the alias must NOT resolve."""
        app = self._started()
        try:
            with self.assertRaises(AttributeError):
                _ = app.models.cls
        finally:
            app.finish()


if __name__ == "__main__":
    unittest.main()
