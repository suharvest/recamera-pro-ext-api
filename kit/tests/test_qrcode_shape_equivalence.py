"""
Equivalence gate for the qrcode-reader app-shape migration (KIT_APP_SHAPE_SPEC §3).

qrcode-reader is the NO-MODEL case: `needs_model = False`, so there is no RKNN
model, no letterbox and no infer stage at all -- `run()` is frame -> CPU decode
-> emit. The migration replaces the base loop's `process_frame()` + `on_results()`
callbacks with an explicit `run()` loop, and this file pins that the observable
output did not move.

Hardware-free: the frame source (`kit.app.open_frame_source`) is stubbed and the
`QrDecoder` is replaced by a scripted stub keyed BY FRAME PTS -- so the decode
result for a given frame is identical in both runs by construction, and a
divergence can only be a behaviour difference. `kit.app.App._load_model` is
stubbed with a tripwire that FAILS if it is ever called.

The same fixed frame sequence is pushed through

  OLD path : the pre-migration qrcode-reader (git 75bd143), reproduced verbatim
             below -- base `App.run()` loop + `process_frame()` + `on_results()`;
  NEW path : the migrated `apps/qrcode-reader/app.py` -- `owns_loop = True`,
             `run()` + `for frame in self.frames()`,

and `results` / `events` / `pts` / `stream_id` are compared field for field.

★One deliberate, benign difference★ -- the WARM-UP frame. The legacy loop ran
`process_frame()` on the first real frame and only THEN `continue`d, so it
decoded a frame whose output it threw away. The new shape's warm-up
(`App._warm_up`) returns immediately when there is no model, so that frame is
skipped without a decode. Both paths therefore PUBLISH exactly the same
sequence; the new path just does one less throw-away decode. Asserted
explicitly in `test_no_model_path_does_no_infer_and_no_wasted_decode`.

Run: `python3 -m pytest kit/tests/test_qrcode_shape_equivalence.py -q`
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
from kit.adapters.frame_source import Frame                          # noqa: E402
from kit.adapters.result_sink import ResultSink                      # noqa: E402

APP_DIR = os.path.join(_REPO, "apps", "qrcode-reader")

FRAME_W, FRAME_H = 640, 480
N_GREY = 2                      # camera warm-up placeholders, both paths skip
N_REAL = 14                     # real frames offered by the fake source
DT = 0.2

# Decode script, keyed by the frame INDEX among the real frames (0-based).
# Zero / one / two codes per frame, plus a frame whose decode raises nothing but
# returns an empty list -- the published `events` list must then be empty too.
SCRIPT = {
    0: [("https://seeed.cc", [[10, 10], [90, 12], [88, 90], [12, 88]])],
    1: [],
    2: [("hello world", [[100, 20], [180, 22], [178, 100], [102, 98]])],
    3: [("hello world", [[101, 21], [181, 23], [179, 101], [103, 99]])],
    4: [],
    5: [("A", [[0, 0], [40, 0], [40, 40], [0, 40]]),
        ("B", [[200, 200], [260, 200], [260, 260], [200, 260]])],
    6: [("A", [[1, 1], [41, 1], [41, 41], [1, 41]]),
        ("B", [[201, 201], [261, 201], [261, 261], [201, 261]]),
        ("C", [[300, 300], [360, 300], [360, 360], [300, 360]])],
    7: [],
    8: [("recamera-pro", [[5, 300], [95, 302], [93, 390], [7, 388]])],
    9: [],
    10: [("recamera-pro", [[6, 301], [96, 303], [94, 391], [8, 389]])],
    11: [("tail-1", [[50, 50], [110, 50], [110, 110], [50, 110]])],
    12: [],
    13: [("tail-2", [[60, 60], [120, 60], [120, 120], [60, 120]])],
}

PTS0 = 700.0


def _pts(i):
    return PTS0 + i * DT


# pts -> decoded codes, so BOTH runs get the same answer for the same frame
# regardless of how many times decode() has been called before it.
BY_PTS = {round(_pts(i), 6): [{"text": t, "quad": [list(p) for p in q]}
                              for t, q in codes]
          for i, codes in SCRIPT.items()}


def _fixed_frames():
    out = []
    for i in range(N_GREY):
        data = np.full((FRAME_H, FRAME_W, 3), 114, dtype=np.uint8)
        out.append(Frame(data=data, w=FRAME_W, h=FRAME_H, fmt="RGB",
                         pts=100.0 + i))
    for i in range(N_REAL):
        rng = np.random.default_rng(7700 + i)
        data = rng.integers(0, 256, (FRAME_H, FRAME_W, 3), dtype=np.uint8)
        out.append(Frame(data=data, w=FRAME_W, h=FRAME_H, fmt="RGB",
                         pts=_pts(i)))
    return out


class _FakeSource:
    def __init__(self, *a, **kw):
        self.closed = False
        self.kw = kw

    def frames(self):
        for f in _fixed_frames():
            yield f

    def close(self):
        self.closed = True


class _StubDecoder:
    """Scripted stand-in for `kit.logic.qrcode.QrDecoder`.

    Keyed by the frame's pixel content -> pts (recovered from a lookup table
    built at import time), so it is a pure function of the frame, exactly like
    the real stateless decoder. Records every call for the infer-count
    assertions.
    """

    _BY_SIG = None

    def __init__(self, model_dir=None):
        self.model_dir = model_dir
        self.calls = 0
        self.seen_shapes = []
        if _StubDecoder._BY_SIG is None:
            _StubDecoder._BY_SIG = {}
            for f in _fixed_frames():
                _StubDecoder._BY_SIG[int(np.asarray(f.data).sum())] = f.pts

    def decode(self, frame):
        self.calls += 1
        self.seen_shapes.append(tuple(np.asarray(frame).shape))
        pts = self._BY_SIG.get(int(np.asarray(frame).sum()))
        if pts is None:
            return []
        # deep-copy so neither path can mutate the shared script
        return [{"text": c["text"], "quad": [list(p) for p in c["quad"]]}
                for c in BY_PTS.get(round(pts, 6), [])]


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


# ---- OLD shape: verbatim copy of the pre-migration qrcode-reader ---------- #
class _LegacyQrcodeApp(LegacyLoopApp):
    """qrcode-reader exactly as it was before the migration (git 75bd143)."""

    id = "qrcode-reader"
    name = "QR Code Reader"
    postproc = "qrcode"
    needs_model = False

    def __init__(self, decoder):
        super().__init__()
        self._decoder = decoder

    def setup(self, config):
        super().setup(config or {})

    def process_frame(self, frame):
        return self._decoder.decode(frame.data)

    def on_results(self, results, frame):
        return [
            {
                "kind": "qrcode",
                "text": r["text"],
                "quad": r["quad"],
            }
            for r in results
        ]


def _load_new_app_module():
    path = os.path.join(APP_DIR, "app.py")
    spec = importlib.util.spec_from_file_location(
        "_qrcode_reader_app_under_test", path)
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
        self.model_loads = []

        def _tripwire(app_self, path):
            # ★load-bearing★: a CPU-only app must never construct an NPU model.
            self.model_loads.append(path)
            raise AssertionError(
                f"_load_model({path!r}) was called by a needs_model=False app")

        kit_app.open_frame_source = lambda *a, **kw: _FakeSource(*a, **kw)
        kit_app.App._load_model = _tripwire

        with open(os.path.join(APP_DIR, "manifest.json")) as f:
            self.manifest = json.load(f)

    def tearDown(self):
        kit_app.open_frame_source = self._orig_open
        kit_app.App._load_model = self._orig_load
        try:
            signal.signal(signal.SIGHUP, signal.SIG_DFL)
        except (ValueError, OSError):
            pass

    def _run_old(self):
        sink = _RecordingSink()
        dec = _StubDecoder()
        app = _LegacyQrcodeApp(dec)
        app.setup({})
        app.run(None, source="ffmpeg", sink=sink, n=0, verbose=False)
        return sink, app, dec

    def _run_new(self):
        sink = _RecordingSink()
        mod = _load_new_app_module()
        dec = _StubDecoder()
        mod.QrDecoder = lambda model_dir=None: dec      # no cv2, no model files
        app = mod.QrcodeReaderApp()
        app.start(None, source="ffmpeg", sink=sink, n=0, verbose=False,
                  app_dir=APP_DIR, manifest=self.manifest, config={})
        try:
            app.run()
        finally:
            app.finish()
        return sink, app, dec


class QrcodeEquivalenceTests(_Base):

    def test_deep_equal(self):
        old, _old_app, old_dec = self._run_old()
        new, _new_app, new_dec = self._run_new()

        self.assertEqual(len(old.payloads), N_REAL - 1,
                         "old path emitted an unexpected frame count")
        self.assertEqual(len(new.payloads), len(old.payloads),
                         "new path emitted a different frame count")

        old_s, new_s = _strip_timing(old.payloads), _strip_timing(new.payloads)
        print("\n--- per-frame (pts, results, events) ---")
        for i, ((po, pts_o), (pn, pts_n)) in enumerate(zip(old_s, new_s)):
            print(f"frame {i:2d}: OLD pts={pts_o} results={po['results']} "
                  f"events={po['events']}")
            print(f"frame {i:2d}: NEW pts={pts_n} results={pn['results']} "
                  f"events={pn['events']}")
            print(f"frame {i:2d}: EQUAL={(po, pts_o) == (pn, pts_n)}")
            self.assertEqual(pts_o, pts_n, f"frame {i}: pts differs")
            self.assertEqual(po["stream_id"], pn["stream_id"],
                             f"frame {i}: stream_id differs")
            self.assertEqual(po["results"], pn["results"],
                             f"frame {i}: results differ")
            self.assertEqual(po["events"], pn["events"],
                             f"frame {i}: events differ")
        self.assertEqual(old_s, new_s, "payload streams differ")
        self.assertEqual(old.frame_sizes, new.frame_sizes)

        evs = [e for p, _ in old_s for e in p["events"]]
        kinds = {k: sum(1 for e in evs if e["kind"] == k)
                 for k in sorted({e["kind"] for e in evs})}
        print("--- event kind distribution (old path) ---")
        print(kinds)
        print(f"decode calls: OLD {old_dec.calls} NEW {new_dec.calls}")

        # -- anti-vacuous-pass assertions --------------------------------- #
        self.assertGreater(len(evs), 0, "fixture produced no events")
        self.assertEqual(set(kinds), {"qrcode"},
                         "the published event kind is 'qrcode' and nothing else")
        self.assertGreater(kinds["qrcode"], 5, "too few codes to be meaningful")
        self.assertGreater(len({e["text"] for e in evs}), 1,
                           "every event carried the same text")
        # multi-code frames and zero-code frames must BOTH occur
        counts = [len(p["events"]) for p, _ in old_s]
        self.assertIn(0, counts, "no empty frame in the fixture")
        self.assertGreater(max(counts), 1, "no multi-code frame in the fixture")

    def test_event_contract_kind_quad_text(self):
        """★The overlay contract★: {kind:"qrcode", text, quad} -- exact keys."""
        new, _app, _dec = self._run_new()
        evs = [e for p, _pts in new.payloads for e in p["events"]]
        self.assertTrue(evs, "no events to check the contract against")
        for e in evs:
            self.assertEqual(set(e), {"kind", "text", "quad"},
                             f"event key set changed: {sorted(e)}")
            self.assertEqual(e["kind"], "qrcode")
            self.assertIsInstance(e["text"], str)
            self.assertEqual(len(e["quad"]), 4, "quad must have 4 corners")
            for pt in e["quad"]:
                self.assertEqual(len(pt), 2, "each corner is [x, y]")
        # and results[] still carries the raw decoder output (text + quad)
        res = [r for p, _pts in new.payloads for r in p["results"]]
        self.assertTrue(res)
        for r in res:
            self.assertEqual(set(r), {"text", "quad"})

    def test_no_model_path_does_no_infer_and_no_wasted_decode(self):
        """(a) zero model loads / infers, (b) the warm-up did not mis-fire."""
        old, _old_app, old_dec = self._run_old()
        new, new_app, new_dec = self._run_new()

        print(f"\nmodel loads: {self.model_loads} (tripwire raises if non-empty)")
        print(f"len(app.models) = {len(new_app.models)}")
        print(f"decode calls: OLD {old_dec.calls} NEW {new_dec.calls} "
              f"(real frames = {N_REAL}, published = {len(new.payloads)})")

        # (a) no NPU model was ever constructed, and none is registered.
        self.assertEqual(self.model_loads, [], "an RKNN model was loaded")
        self.assertEqual(len(new_app.models), 0,
                         "a needs_model=False app registered a model")
        with self.assertRaises(AttributeError):
            _ = new_app.models.model         # nothing to infer with

        # (b) the kit warm-up ran but produced NOTHING: no decode, no emit.
        #     Old path decoded the warm-up frame and threw the result away;
        #     the new path skips it. Published output is identical either way.
        self.assertTrue(new_app._warmed, "the warm-up bookkeeping never ran")
        self.assertEqual(old_dec.calls, N_REAL,
                         "old path decoded every real frame incl. the warm-up")
        self.assertEqual(new_dec.calls, N_REAL - 1,
                         "new path must decode exactly the published frames")
        self.assertEqual(new_dec.calls, len(new.payloads),
                         "one decode per published frame")
        self.assertEqual(len(new.payloads), len(old.payloads))

    def test_decoder_sees_the_original_frame_not_a_letterbox(self):
        """No `pre()` in this shape: the decoder gets full camera pixels."""
        _new, _app, dec = self._run_new()
        shapes = set(dec.seen_shapes)
        print("\ndecoder input shapes (new path):", shapes)
        self.assertEqual(shapes, {(FRAME_H, FRAME_W, 3)},
                         "the decoder was handed something other than the "
                         f"original {FRAME_H}x{FRAME_W} frame")

    def test_new_shape_flags(self):
        mod = _load_new_app_module()
        cls = mod.QrcodeReaderApp
        self.assertTrue(cls.owns_loop, "qrcode-reader must own its loop")
        self.assertFalse(cls.needs_model, "qrcode-reader has no NPU model")
        # legacy callbacks are gone (start() would refuse to run otherwise)
        for hook in ("process_frame", "on_results", "run_postproc"):
            # the pre-migration hooks were deleted from kit.app.App; defining
            # one on an app is now dead code (and a _check_loop_shape error)
            self.assertFalse(hasattr(kit_app.App, hook))
            self.assertFalse(hasattr(cls, hook),
                             f"removed hook {hook} is still defined")


if __name__ == "__main__":
    unittest.main()
