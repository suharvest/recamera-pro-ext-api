"""
Equivalence gate for the kit app-shape migration (KIT_APP_SHAPE_SPEC §7).

Hardware-free. We stub the two device-only pieces --

  * the frame source  (patch `kit.app.open_frame_source`), and
  * the RKNN engine   (patch `kit.app.App._load_model`)

-- with deterministic fakes, then run the SAME fixed input through

  OLD path : the pre-migration yolo-detector, i.e. the base `App.run()` loop
             + `run_postproc()` + `on_results()` (reproduced verbatim below,
             copied from git history), and
  NEW path : the migrated `apps/yolo-detector/app.py`, i.e. `run()` +
             `for frame in self.frames()` + `pre/infer/postprocess/emit`.

and assert the emitted `results` / `events` are field-for-field identical.

Timing fields (`inference_time_ms`, `pipeline_ms`) are excluded -- they are wall
clock, not behaviour.

Run: `python3 -m pytest kit/tests/test_new_shape_equivalence.py -q`
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
from kit import events as E                                          # noqa: E402
from kit.adapters.frame_source import Frame                          # noqa: E402
from kit.adapters.result_sink import ResultSink                      # noqa: E402

APP_DIR = os.path.join(_REPO, "apps", "yolo-detector")

# ---- fixed synthetic input -------------------------------------------- #
FRAME_W, FRAME_H = 160, 120
N_GREY = 2            # camera warm-up placeholders, both paths must skip these
N_REAL = 6            # real frames offered by the fake source
N_STOP = 4            # --n : stop after this many *emitted* frames
N_ANCHORS = 60        # columns of the fake [1, 84, N] detection head


def _fixed_frames():
    """Deterministic frame sequence: N_GREY flat-grey frames, then N_REAL real ones."""
    out = []
    for i in range(N_GREY):
        data = np.full((FRAME_H, FRAME_W, 3), 114, dtype=np.uint8)
        out.append(Frame(data=data, w=FRAME_W, h=FRAME_H, fmt="RGB",
                         pts=100.0 + i))
    for i in range(N_REAL):
        rng = np.random.default_rng(7000 + i)
        data = rng.integers(0, 256, (FRAME_H, FRAME_W, 3), dtype=np.uint8)
        out.append(Frame(data=data, w=FRAME_W, h=FRAME_H, fmt="RGB",
                         pts=200.0 + i))
    return out


class _FakeSource:
    """Stands in for FfmpegRtspSource: replays `_fixed_frames()` once."""

    def __init__(self, *a, **kw):
        self.closed = False

    def frames(self):
        for f in _fixed_frames():
            yield f

    def close(self):
        self.closed = True


class _FakeModel:
    """Stands in for RknnModel.

    Returns one `[1, 84, N_ANCHORS]` float32 tensor -- the concatenated YOLOv8
    head layout `detect.postprocess()` decodes -- seeded by the call index, so
    the k-th inference of the old run and the k-th inference of the new run get
    byte-identical outputs.
    """

    def __init__(self, path):
        self.path = path
        self.calls = 0
        self.released = False

    def infer(self, x):
        rng = np.random.default_rng(9000 + self.calls)
        self.calls += 1
        head = np.empty((1, 84, N_ANCHORS), dtype=np.float32)
        # rows 0-3: box cx,cy,w,h in network (640) space
        head[0, 0:2, :] = rng.uniform(60, 580, (2, N_ANCHORS))
        head[0, 2:4, :] = rng.uniform(20, 120, (2, N_ANCHORS))
        # rows 4-83: per-class scores
        head[0, 4:, :] = rng.uniform(0.0, 1.0, (80, N_ANCHORS))
        return [head]

    def release(self):
        self.released = True


class _RecordingSink(ResultSink):
    """Captures every payload the app publishes."""

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


# ---- OLD shape: verbatim copy of the pre-migration yolo-detector ------- #
class _LegacyYoloApp(LegacyLoopApp):
    """yolo-detector exactly as it was before the migration (git e2177c7)."""
    id = "yolo-detector"
    name = "YOLO Detector"
    postproc = "detect"
    model_frame = "hw-direct"

    def on_results(self, results, frame):
        return [
            {
                "kind": "detection",
                "label": d["cls_name"],
                "cls": d["cls"],
                "score": d["score"],
                "box": d["box"],
            }
            for d in results
        ]


def _load_new_app_class():
    """Import the migrated apps/yolo-detector/app.py and return YoloDetectorApp."""
    path = os.path.join(APP_DIR, "app.py")
    spec = importlib.util.spec_from_file_location("_yolo_detector_app_under_test",
                                                  path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod.YoloDetectorApp


def _strip_timing(payloads):
    """Drop wall-clock fields; keep everything behavioural."""
    out = []
    for payload, pts in payloads:
        p = {k: v for k, v in payload.items()
             if k not in ("inference_time_ms", "pipeline_ms")}
        out.append((p, pts))
    return out


class NewShapeEquivalenceTests(unittest.TestCase):
    def setUp(self):
        self._orig_open = kit_app.open_frame_source
        self._orig_load = kit_app.App._load_model
        self.models = []

        def _fake_open(*a, **kw):
            return _FakeSource()

        def _fake_load(app_self, path):
            m = _FakeModel(path)
            self.models.append(m)
            return m

        kit_app.open_frame_source = _fake_open
        kit_app.App._load_model = _fake_load
        with open(os.path.join(APP_DIR, "manifest.json")) as f:
            self.manifest = json.load(f)
        self.eff = {"conf": 0.35, "iou": 0.45}

    def tearDown(self):
        kit_app.open_frame_source = self._orig_open
        kit_app.App._load_model = self._orig_load
        try:
            signal.signal(signal.SIGHUP, signal.SIG_DFL)
        except (ValueError, OSError):
            pass

    # -- the gate --------------------------------------------------------- #
    def _run_old(self):
        sink = _RecordingSink()
        app = _LegacyYoloApp()
        app.setup(dict(self.eff))
        app.run("models/yolo8n_rawhead_int8.rknn", source="ffmpeg",
                sink=sink, n=N_STOP, verbose=False)
        return sink

    def _run_new(self):
        sink = _RecordingSink()
        app = _load_new_app_class()()
        app.setup(dict(self.eff))
        app.start("models/yolo8n_rawhead_int8.rknn", source="ffmpeg",
                  sink=sink, n=N_STOP, verbose=False,
                  app_dir=APP_DIR, manifest=self.manifest, config=dict(self.eff))
        try:
            app.run()
        finally:
            app.finish()
        return sink, app

    def test_results_and_events_are_identical(self):
        old = self._run_old()
        new, _app = self._run_new()

        self.assertEqual(len(old.payloads), N_STOP,
                         "old path did not emit the expected frame count")
        self.assertEqual(len(new.payloads), len(old.payloads),
                         "new path emitted a different number of frames")

        old_s, new_s = _strip_timing(old.payloads), _strip_timing(new.payloads)
        for i, ((po, pts_o), (pn, pts_n)) in enumerate(zip(old_s, new_s)):
            self.assertEqual(pts_o, pts_n, f"frame {i}: pts differs")
            self.assertEqual(po["stream_id"], pn["stream_id"])
            self.assertEqual(po["results"], pn["results"],
                             f"frame {i}: results differ")
            self.assertEqual(po["events"], pn["events"],
                             f"frame {i}: events differ")
        # whole-payload compare (catches any added/removed key too)
        self.assertEqual(old_s, new_s)

        # sanity: the fixture must actually produce detections, otherwise the
        # comparison above would be vacuously true on two empty lists.
        total = sum(len(p["results"]) for p, _ in old_s)
        self.assertGreater(total, 0, "fixture produced no detections")
        self.assertEqual(total, sum(len(p["events"]) for p, _ in new_s))

    def test_same_frames_consumed_and_grey_skipped(self):
        old = self._run_old()
        new, _ = self._run_new()
        # Both must skip the grey warm-up frames AND drop the first real
        # (model warm-up) frame's output: N_REAL - 1 emittable, capped by N_STOP.
        self.assertEqual(len(old.payloads), min(N_STOP, N_REAL - 1))
        self.assertEqual(len(new.payloads), min(N_STOP, N_REAL - 1))
        self.assertEqual(old.frame_sizes, new.frame_sizes)

    def test_infer_call_counts_match(self):
        self._run_old()
        old_calls = self.models[-1].calls
        self._run_new()
        new_calls = self.models[-1].calls
        self.assertEqual(old_calls, new_calls)


class NewShapeApiTests(unittest.TestCase):
    """The pieces the equivalence run exercises only indirectly."""

    def setUp(self):
        self._orig_open = kit_app.open_frame_source
        self._orig_load = kit_app.App._load_model
        kit_app.open_frame_source = lambda *a, **kw: _FakeSource()
        kit_app.App._load_model = lambda app_self, path: _FakeModel(path)
        with open(os.path.join(APP_DIR, "manifest.json")) as f:
            self.manifest = json.load(f)

    def tearDown(self):
        kit_app.open_frame_source = self._orig_open
        kit_app.App._load_model = self._orig_load
        try:
            signal.signal(signal.SIGHUP, signal.SIG_DFL)
        except (ValueError, OSError):
            pass

    def _started(self, **kw):
        app = _load_new_app_class()()
        app.start("models/yolo8n_rawhead_int8.rknn", sink=_RecordingSink(),
                  verbose=False, app_dir=APP_DIR, manifest=self.manifest,
                  config={"conf": 0.5, "iou": 0.6}, **kw)
        return app

    def test_models_registry_ids_and_aliases(self):
        app = self._started()
        try:
            self.assertEqual(len(app.models), 1)
            h = app.models.yolo8n_rawhead_int8              # manifest id
            self.assertIs(app.models.detect, h)             # task
            self.assertIs(app.models.det, h)                # short alias
            self.assertIs(app.models.model, h)              # single-model alias
            self.assertIs(app.models[0], h)
            # --model is passed RELATIVE by the supervisor; kit absolutises it
            # against the install dir.
            self.assertEqual(h.path, os.path.join(
                APP_DIR, "models/yolo8n_rawhead_int8.rknn"))
            with self.assertRaises(AttributeError):
                app.models.nope
        finally:
            app.finish()

    def test_model_path_absolutised_from_manifest(self):
        app = _load_new_app_class()()
        app.start(None, sink=_RecordingSink(), verbose=False, app_dir=APP_DIR,
                  manifest=self.manifest, config={})
        try:
            self.assertEqual(app.models.det.path,
                             os.path.join(APP_DIR, "models/yolo8n_rawhead_int8.rknn"))
        finally:
            app.finish()

    def test_params_auto_bound_flat_schema(self):
        app = self._started()
        try:
            self.assertEqual(app.conf, 0.5)
            self.assertEqual(app.iou, 0.6)
            self.assertTrue(app._params_bound)
        finally:
            app.finish()

    def test_params_auto_bound_grouped_schema(self):
        """The other 8 apps use the grouped form; binding must handle both."""
        grouped = {
            "id": "grouped-app",
            "models": [],
            "config_schema": {"groups": [
                {"title": "g", "items": [
                    {"key": "threshold", "type": "number", "apply": "live",
                     "default": 0.4},
                    {"key": "window", "type": "integer", "apply": "restart",
                     "default": 5},
                    {"key": "enabled", "type": "boolean", "apply": "live",
                     "default": True},
                ]},
            ]},
        }

        class _G(kit_app.App):
            id = "grouped-app"
            owns_loop = True
            needs_model = False

            def run(self):
                pass

        app = _G()
        app.start(None, sink=_RecordingSink(), verbose=False, app_dir=APP_DIR,
                  manifest=grouped,
                  config={"threshold": "0.7", "window": 9, "enabled": 0})
        try:
            self.assertEqual(app.threshold, 0.7)     # coerced str -> float
            self.assertEqual(app.window, 9)
            self.assertIs(app.enabled, False)        # coerced 0 -> bool
            # live re-bind only touches apply:"live" keys
            changed = app._bind_params({"threshold": 0.9, "window": 99},
                                       live_only=True)
            self.assertEqual(changed, {"threshold"})
            self.assertEqual(app.threshold, 0.9)
            self.assertEqual(app.window, 9)
            # a None value never wipes a bound attribute
            app._bind_params({"threshold": None}, live_only=True)
            self.assertEqual(app.threshold, 0.9)
        finally:
            app.finish()

    def test_new_shape_rejects_legacy_hooks(self):
        class _Mixed(kit_app.App):
            id = "mixed"
            owns_loop = True
            needs_model = False

            def run(self):
                pass

            def on_results(self, results, frame):
                return []

        with self.assertRaises(RuntimeError):
            _Mixed().start(None, sink=_RecordingSink(), verbose=False,
                           app_dir=APP_DIR, manifest={"id": "mixed"}, config={})

    def test_shape_check_accepts_the_migrated_app(self):
        self.assertIsNone(kit_app._check_loop_shape(_load_new_app_class()()))

    def test_shape_check_rejects_the_pre_migration_app(self):
        """The frozen oracle is NOT a runnable app shape any more."""
        with self.assertRaises(RuntimeError) as cm:
            kit_app._check_loop_shape(_LegacyYoloApp())
        self.assertIn("owns_loop = True", str(cm.exception))

    def test_event_helper_matches_legacy_mapping(self):
        d = {"box": [1.0, 2.0, 3.0, 4.0], "cls": 7, "cls_name": "truck",
             "score": 0.81}
        self.assertEqual(E.detection(d), {
            "kind": "detection", "label": "truck", "cls": 7,
            "score": 0.81, "box": [1.0, 2.0, 3.0, 4.0],
        })

    def test_frames_before_start_raises(self):
        app = _load_new_app_class()()
        with self.assertRaises(RuntimeError):
            next(iter(app.frames()))


class LoopShapeDeclarationTests(unittest.TestCase):
    """`owns_loop` is the ONLY shape switch -- and a mismatch is a hard error.

    The pre-migration callback loop is gone, so "not loop-owning" is no longer a
    runnable state: it is an error too. The remaining matrix is

        owns_loop | run() signature       | expected
        ----------+-----------------------+-------------------------------------
        True      | no positional args    | OK
        True      | positional args       | RuntimeError (removed old signature)
        True      | not overridden        | RuntimeError
        False     | anything              | RuntimeError ("declare owns_loop")

    plus: any leftover on_results / run_postproc / process_frame -> RuntimeError,
    because nothing dispatches to them and the logic would silently never run.
    """

    # 1. declared + loop-owning signature -> accepted
    def test_declared_and_no_positional_is_accepted(self):
        class _Ok(kit_app.App):
            id = "ok"
            owns_loop = True

            def run(self):
                pass

        self.assertIsNone(kit_app._check_loop_shape(_Ok()))

    def test_declared_with_keyword_only_args_is_accepted(self):
        """`def run(self, *, debug=False)` is legitimate -- the old signature
        sniffing rejected it and silently opened a camera."""

        class _Kw(kit_app.App):
            id = "kw"
            owns_loop = True

            def run(self, *, debug=False):
                pass

        self.assertIsNone(kit_app._check_loop_shape(_Kw()))

    # 2. THE TRAP: looks loop-owning but never declared -> hard error
    def test_undeclared_new_signature_raises(self):
        class _Trap(kit_app.App):
            id = "trap"

            def run(self):
                pass

        with self.assertRaises(RuntimeError) as cm:
            kit_app._check_loop_shape(_Trap())
        msg = str(cm.exception)
        self.assertIn("owns_loop = True", msg)
        self.assertIn("_Trap", msg)

    def test_undeclared_keyword_only_run_raises(self):
        class _Trap2(kit_app.App):
            id = "trap2"

            def run(self, *, debug=False):
                pass

        with self.assertRaises(RuntimeError) as cm:
            kit_app._check_loop_shape(_Trap2())
        self.assertIn("owns_loop = True", str(cm.exception))

    def test_undeclared_new_signature_raises_from_start(self):
        """The trap must also fire on a direct start() call, not just run_app."""

        class _Trap3(kit_app.App):
            id = "trap3"
            needs_model = False

            def run(self):
                pass

        with self.assertRaises(RuntimeError):
            _Trap3().start(None, sink=_RecordingSink(), verbose=False,
                           app_dir=APP_DIR, manifest={"id": "trap3"}, config={})

    # 3. the removed pre-migration shapes -> hard error, not silent fallback
    def test_undeclared_legacy_signature_raises(self):
        """The old take-over run(model_path, ...) used to be accepted silently."""

        class _TakeOver(kit_app.App):
            id = "voice-like"

            def run(self, model_path=None, *, source="ffmpeg", url=None,
                    sink=None, n=0, every=1, verbose=True, **kw):
                pass

        with self.assertRaises(RuntimeError) as cm:
            kit_app._check_loop_shape(_TakeOver())
        self.assertIn("owns_loop = True", str(cm.exception))

    def test_no_run_override_raises(self):
        class _Plain(kit_app.App):
            id = "plain"

        with self.assertRaises(RuntimeError):
            kit_app._check_loop_shape(_Plain())

    # 4. declared but run() still takes positional args -> hard error
    def test_declared_with_positional_run_raises(self):
        class _Bad(kit_app.App):
            id = "bad"
            owns_loop = True

            def run(self, model_path=None, **kw):
                pass

        with self.assertRaises(RuntimeError) as cm:
            kit_app._check_loop_shape(_Bad())
        self.assertIn("positional", str(cm.exception))

    def test_declared_without_run_override_raises(self):
        class _Bad2(kit_app.App):
            id = "bad2"
            owns_loop = True

        with self.assertRaises(RuntimeError) as cm:
            kit_app._check_loop_shape(_Bad2())
        self.assertIn("not overridden", str(cm.exception))

    # 5. leftover pre-migration hooks -> hard error
    def test_leftover_callback_hook_raises(self):
        for hook in ("on_results", "run_postproc", "process_frame"):
            with self.subTest(hook=hook):
                cls = type("_Stale", (kit_app.App,), {
                    "id": "stale", "owns_loop": True, "needs_model": False,
                    "run": lambda self: None,
                    hook: lambda self, *a, **kw: [],
                })
                with self.assertRaises(RuntimeError) as cm:
                    kit_app._check_loop_shape(cls())
                self.assertIn(hook, str(cm.exception))

    def test_base_run_raises_not_implemented(self):
        """App.run() is now an abstract stub, not a hidden camera loop."""

        class _Plain(kit_app.App):
            id = "plain2"

        with self.assertRaises(NotImplementedError):
            kit_app.App.run(_Plain())


if __name__ == "__main__":
    unittest.main()
