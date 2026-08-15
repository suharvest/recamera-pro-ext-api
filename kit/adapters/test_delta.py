"""
Offline unit tests for this round's delta on the L0 adapter layer:

  #1  select_probe        -- the probe factory added to registry.py (parallels
                            select_frame_source/result_sink/audio_source/control),
                            wrapping the SDK's recamera_ext.ProbeSource.
  #2  Classification ABI  -- rc_ext_class_t layout: sizeof == 40 and the exact
                            field order (score, class_id, label, has_box, x1..y2);
                            send_classification packs an optional 4th-element box
                            into has_box=1 + coords without crashing.
  #3  CgiControl body     -- set_inference builds the right JSON body + POST path,
                            and non-2xx / code!=0 raise (HTTP fully mocked).
  #4  normalization contract -- send_detections passes coordinates through
                            unchanged (no unit conversion): the [0,1] normalization
                            is the CALLER's job (OfficialResultSink), the SDK
                            wrapper is a byte-for-byte identity on coordinates.
  #5  select_probe / select_control selection logic -- mock capabilities/env and
                            assert the returned adapter TYPES.

No device, no socket, no librecamera_ext.so:
  * The real SDK module (sdk/python/recamera_ext/__init__.py) is a pure-Python
    ctypes wrapper; it imports fine on any host and the .so is only dlopen'd
    inside a constructor. We load it BY PATH (immune to the fake recamera_ext
    that test_official.py injects into sys.modules) and monkeypatch its `_load`
    to a fake lib, so ResultSink packs into ctypes structs we can inspect without
    the aarch64 .so.
  * select_probe is exercised with a fake recamera_ext injected into sys.modules
    (same technique as test_official.py) so we assert the type without a device.

Run:  python3 -m pytest kit/adapters/test_delta.py -q     (from repo root)
"""
import ctypes
import importlib.util
import json
import os
import sys
import types

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_SDK_INIT = os.path.join(_ROOT, "sdk", "python", "recamera_ext", "__init__.py")


def _load_real_sdk():
    """Load the real SDK module BY PATH into a private name.

    Loading by path (not `import recamera_ext`) sidesteps the fake recamera_ext
    that other test modules inject into sys.modules -- we always get the shipped
    ctypes structures regardless of test ordering. No .so is touched: `_load`
    only dlopen's inside a constructor, never at import.
    """
    spec = importlib.util.spec_from_file_location("recamera_ext_real", _SDK_INIT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ===========================================================================
# #2  Classification ABI (rc_ext_class_t): layout is byte-for-byte the C struct
# ===========================================================================
def test_classification_abi_layout():
    sdk = _load_real_sdk()
    Classification = sdk.Classification

    # sizeof == 40 on the 64-bit ABI: score(4) class_id(4) label(ptr@8..16)
    # has_box(4@16) x1..y2(4*4@20..36), tail-padded to 8-byte (pointer) align -> 40.
    assert ctypes.sizeof(Classification) == 40, ctypes.sizeof(Classification)

    names = [f[0] for f in Classification._fields_]
    assert names == ["score", "class_id", "label", "has_box",
                     "x1", "y1", "x2", "y2"], names

    # Field offsets prove the intended C layout (label is the 8-byte pointer,
    # has_box follows it, then the 4 box floats).
    off = {n: getattr(Classification, n).offset for n in names}
    assert off["score"] == 0 and off["class_id"] == 4
    assert off["label"] == 8 and off["has_box"] == 16
    assert (off["x1"], off["y1"], off["x2"], off["y2"]) == (20, 24, 28, 32), off

    # Construct with a normalized box; the struct stores the values verbatim.
    c = Classification(0.9, 3, b"face", 1, 0.30, 0.20, 0.55, 0.60)
    assert c.has_box == 1 and c.class_id == 3
    assert (round(c.x1, 4), round(c.y1, 4), round(c.x2, 4), round(c.y2, 4)) == \
        (0.30, 0.20, 0.55, 0.60)


# --- a fake librecamera_ext.so surface for the ResultSink wrapper ----------- #
class _FakeLib:
    """Captures the ctypes arrays each rc_ext_result_send_* receives so we can
    inspect what the pure-Python wrapper packed -- no .so required."""

    def __init__(self):
        self.sent = []  # list of (method, pts_us, [copied items])

    # rc_ext_result_open(source_id, &err) -> handle (must be truthy)
    def rc_ext_result_open(self, source_id, err_ptr):
        return 0x1234  # non-null handle

    def rc_ext_result_close(self, handle):
        pass

    def _capture(self, method, pts_us, arr, n):
        items = []
        for i in range(n):
            e = arr[i]
            # copy the primitive fields out while the ctypes array is alive
            items.append({k: getattr(e, k) for k in
                          (f[0] for f in type(e)._fields_)
                          if k not in ("label", "mask", "points")})
        self.sent.append((method, int(pts_us.value), items))
        return 0

    def rc_ext_result_send_detections(self, h, pts_us, arr, n):
        return self._capture("send_detections", pts_us, arr, n.value)

    def rc_ext_result_send_classification(self, h, pts_us, arr, n):
        return self._capture("send_classification", pts_us, arr, n.value)


def _sink_with_fake_lib():
    """Return (sdk_module, ResultSink instance) wired to a _FakeLib."""
    sdk = _load_real_sdk()
    fake = _FakeLib()
    sdk._load = lambda *a, **k: fake        # ResultSink.__init__ calls _load()
    sink = sdk.ResultSink(source_id="test")
    return sdk, sink, fake


def test_send_classification_has_box_packs_without_crashing():
    """#2: send_classification accepts an optional 4th-element box and packs it
    into has_box=1 + normalized coords; box-less entries stay has_box=0."""
    _sdk, sink, fake = _sink_with_fake_lib()
    sink.send_classification(pts_us=0, items=[
        (0.9, 3, "face", (0.30, 0.20, 0.55, 0.60)),   # with ROI box
        (0.8, 1, "cat"),                              # box-less (3-tuple)
    ])
    assert len(fake.sent) == 1
    method, pts_us, items = fake.sent[0]
    assert method == "send_classification" and pts_us == 0
    a, b = items
    assert a["has_box"] == 1 and a["class_id"] == 3
    assert (round(a["x1"], 4), round(a["y1"], 4),
            round(a["x2"], 4), round(a["y2"], 4)) == (0.30, 0.20, 0.55, 0.60)
    assert b["has_box"] == 0 and b["class_id"] == 1


# ===========================================================================
# #4  normalization contract: the SDK wrapper is identity on coordinates.
#     The [0,1] normalization is the CALLER's contract (OfficialResultSink
#     divides pixels by frame size before calling send_detections); the SDK
#     wrapper must NOT re-scale -- it packs whatever floats it is handed.
# ===========================================================================
def test_send_detections_passes_coords_through_unchanged():
    _sdk, sink, fake = _sink_with_fake_lib()
    # Caller supplies already-normalized [0,1] fractions (the documented contract
    # in ResultSink.send_detections' docstring).
    sink.send_detections(pts_us=1_000_000, boxes=[
        (0.05, 0.07, 0.62, 0.94, 0.92, "person", 0),
    ])
    method, pts_us, items = fake.sent[0]
    assert method == "send_detections" and pts_us == 1_000_000
    box = items[0]
    # Identity: no unit conversion, no scaling -- exactly what we passed in.
    assert round(box["x1"], 4) == 0.05 and round(box["y1"], 4) == 0.07
    assert round(box["x2"], 4) == 0.62 and round(box["y2"], 4) == 0.94
    assert round(box["score"], 4) == 0.92 and box["class_id"] == 0


# ===========================================================================
# #3  CgiControl request body + POST path (HTTP fully mocked).
# ===========================================================================
class _FakeResponse:
    def __init__(self, status, body):
        self.status = status
        self._body = body

    def read(self):
        return self._body


class _FakeConn:
    """Records the single request; returns a canned response.

    Class-level `last` captures (method, url, body, headers) for assertions and
    `response` sets what getresponse() returns."""

    last = None
    response = _FakeResponse(200, b'{"code":0,"message":"success"}')

    def __init__(self, host, port, timeout=None, context=None):
        self.host = host
        self.port = port

    def request(self, method, url, body=None, headers=None):
        _FakeConn.last = (method, url, body, headers)

    def getresponse(self):
        return _FakeConn.response

    def close(self):
        pass


def _cgi(monkeypatch):
    import http.client
    monkeypatch.setattr(http.client, "HTTPSConnection", _FakeConn)
    _FakeConn.last = None
    _FakeConn.response = _FakeResponse(200, b'{"code":0,"message":"success"}')
    from kit.adapters.cgi_control import CgiControl
    return CgiControl(verbose=False)


def test_cgi_set_inference_disable_body(monkeypatch):
    ctl = _cgi(monkeypatch)
    ctl.set_inference(enable=False)
    method, url, body, _headers = _FakeConn.last
    assert method == "POST"
    assert url == "/cgi-bin/entry.cgi/model/inference?id=0", url
    assert json.loads(body.decode()) == {"iEnable": 0}, body


def test_cgi_set_inference_full_body(monkeypatch):
    ctl = _cgi(monkeypatch)
    ctl.set_inference(enable=True, model="x.rknn", fps=5)
    method, url, body, _headers = _FakeConn.last
    assert method == "POST"
    assert url == "/cgi-bin/entry.cgi/model/inference?id=0", url
    assert json.loads(body.decode()) == {
        "iEnable": 1, "sModel": "x.rknn", "iFPS": 5}, body


def test_cgi_non_2xx_raises(monkeypatch):
    ctl = _cgi(monkeypatch)
    _FakeConn.response = _FakeResponse(500, b"boom")
    with pytest.raises(RuntimeError):
        ctl.set_inference(enable=True)


def test_cgi_envelope_code_nonzero_raises(monkeypatch):
    ctl = _cgi(monkeypatch)
    _FakeConn.response = _FakeResponse(200, b'{"code":7,"message":"bad model"}')
    with pytest.raises(RuntimeError):
        ctl.set_inference(enable=True, model="missing.rknn")


# ===========================================================================
# #5  select_probe / select_control selection logic (mocked capabilities/env).
# ===========================================================================
_ENV_KEYS = ("RECAMERA_FRAME_SOCK", "RECAMERA_RESULT_SOCK", "RECAMERA_AUDIO_SOCK",
             "RECAMERA_PROBE_SOCK", "RECAMERA_RESULT_INGRESS", "RECAMERA_CONTROL_API",
             "RECAMERA_ADAPTER_PREFER")


def _clear_env():
    for k in _ENV_KEYS:
        os.environ.pop(k, None)


class _FakeProbeSource:
    """Mimics recamera_ext.ProbeSource(stages=..., **kw) without a device."""

    def __init__(self, stages, sample_every=1, timeout_ms=1000, lib_path=None):
        self.stages = list(stages)
        self.sample_every = sample_every
        self.timeout_ms = timeout_ms


def _install_fake_ext_with_probe():
    mod = types.ModuleType("recamera_ext")
    mod.ProbeSource = _FakeProbeSource
    sys.modules["recamera_ext"] = mod
    return _FakeProbeSource


def test_select_control_cgi_when_no_official():
    _clear_env()
    from kit.adapters import registry
    from kit.adapters.cgi_control import CgiControl
    registry.capabilities(refresh=True)             # control_api probes False
    ctl = registry.select_control(verbose=False)
    assert isinstance(ctl, CgiControl), type(ctl)


def test_select_control_official_when_forced():
    _clear_env()
    from kit.adapters import registry
    from kit.adapters.official import OfficialControl
    # (a) capability forced on via env
    os.environ["RECAMERA_CONTROL_API"] = "1"
    registry.capabilities(refresh=True)
    assert isinstance(registry.select_control(), OfficialControl)
    # (b) global prefer=official override
    _clear_env()
    os.environ["RECAMERA_ADAPTER_PREFER"] = "official"
    registry.capabilities(refresh=True)
    assert isinstance(registry.select_control(), OfficialControl)
    _clear_env()
    registry.capabilities(refresh=True)


def test_select_probe_returns_probesource():
    """select_probe always wraps the SDK ProbeSource (no workaround branch).

    ProbeSource's real socket behaviour needs a device; here we assert the
    SELECTION + TYPE + arg forwarding with a fake recamera_ext, which is the
    offline-testable contract."""
    _clear_env()
    FakeProbe = _install_fake_ext_with_probe()
    try:
        from kit.adapters import registry
        registry.capabilities(refresh=True)
        probe = registry.select_probe(stages=["metrics"], sample_every=2)
        assert isinstance(probe, FakeProbe), type(probe)
        assert probe.stages == ["metrics"] and probe.sample_every == 2
    finally:
        sys.modules.pop("recamera_ext", None)


def test_select_probe_exported_from_package():
    """__init__.py lazy-exports select_probe (parallel to the other factories)."""
    _install_fake_ext_with_probe()
    try:
        import kit.adapters as adapters
        assert "select_probe" in adapters.__all__
        probe = adapters.select_probe(stages=["npu"])
        assert isinstance(probe, _FakeProbeSource)
    finally:
        sys.modules.pop("recamera_ext", None)


if __name__ == "__main__":
    sys.exit(pytest.main([os.path.abspath(__file__), "-q"]))


# ===========================================================================
# Diagnostic visibility (DX P0): local oversize guard + send stats.
#   Each send_* packs ONE datagram (recamera_ext.h §M1); an oversized datagram
#   is silently dropped/truncated by the kernel while the C ABI can still report
#   success. The wrapper rejects oversize BEFORE the C call and tallies outcomes.
# ===========================================================================
def test_oversize_detections_rejected_before_c_call():
    sdk, sink, fake = _sink_with_fake_lib()
    # sizeof(Box) is 40 B; thousands of boxes + labels blow past the 64 KiB
    # datagram budget. The guard must reject BEFORE the C send.
    big = [(0.1, 0.1, 0.2, 0.2, 0.9, "person", 0)] * 6000
    with pytest.raises(sdk.ResultTooLarge):
        sink.send_detections(pts_us=0, boxes=big)
    assert fake.sent == []                       # C send was never called
    assert sink.stats() == {"sent": 0, "oversize_rejected": 1, "send_error": 0}


def test_stats_counts_successful_sends():
    _sdk, sink, fake = _sink_with_fake_lib()
    sink.send_detections(pts_us=0, boxes=[(0.1, 0.1, 0.2, 0.2, 0.9, "a", 0)])
    sink.send_classification(pts_us=0, items=[(0.8, 1, "cat")])
    assert sink.stats() == {"sent": 2, "oversize_rejected": 0, "send_error": 0}


def test_stats_counts_send_error():
    """A negative rc from the C send raises AND is tallied as send_error."""
    sdk = _load_real_sdk()
    fake = _FakeLib()
    fake.rc_ext_result_send_detections = lambda h, pts_us, arr, n: -5
    sdk._load = lambda *a, **k: fake
    sink = sdk.ResultSink(source_id="t")
    with pytest.raises(RuntimeError):
        sink.send_detections(pts_us=0, boxes=[(0.1, 0.1, 0.2, 0.2, 0.9, "a", 0)])
    st = sink.stats()
    assert st["send_error"] == 1 and st["sent"] == 0 and st["oversize_rejected"] == 0


def test_reverse_payload_under_limit_is_sent():
    """Reverse control: a payload comfortably under the budget is NOT rejected."""
    _sdk, sink, fake = _sink_with_fake_lib()
    sink.send_detections(pts_us=0,
                         boxes=[(0.1, 0.1, 0.2, 0.2, 0.9, "p", 0)] * 100)
    assert len(fake.sent) == 1
    assert sink.stats() == {"sent": 1, "oversize_rejected": 0, "send_error": 0}
