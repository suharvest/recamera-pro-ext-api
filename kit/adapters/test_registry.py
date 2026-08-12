"""
Offline unit tests for the L0 capability registry (docs/guide/adapter-bootstrap.md §3).

Run:  python3 -m kit.adapters.test_registry     (from repo root)
No device, no network: FfmpegRtspSource is constructed with explicit
width/height so it never probes the RTSP stream.
"""
import os
import sys
import tempfile

# Allow running as a plain script from the repo root.
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from kit.adapters import registry
from kit.adapters.frame_source import FfmpegRtspSource, SnapshotSource, open_frame_source
from kit.adapters.result_sink import StdoutSink, WsResultSink, open_result_sink
from kit.adapters.official import OfficialFrameSource, OsdInjectResultSink


def _clear_env():
    for k in ("RECAMERA_FRAME_SOCK", "RECAMERA_RESULT_SOCK", "RECAMERA_AUDIO_SOCK",
              "RECAMERA_RESULT_INGRESS", "RECAMERA_CONTROL_API",
              "RECAMERA_ADAPTER_PREFER"):
        os.environ.pop(k, None)


def test_no_official_selects_workaround():
    """No official endpoints -> workaround implementations, behaviour unchanged."""
    _clear_env()
    # Point the frame-broker probe at a path guaranteed absent.
    os.environ["RECAMERA_FRAME_SOCK"] = "/nonexistent/frame.sock.absent"
    os.environ["RECAMERA_RESULT_SOCK"] = "/nonexistent/result-in.sock.absent"
    os.environ["RECAMERA_AUDIO_SOCK"] = "/nonexistent/audio.sock.absent"
    caps = registry.capabilities(refresh=True)
    assert caps.frame_broker is False, caps
    assert caps.result_ingress is False, caps

    src = open_frame_source(url="rtsp://x", prefer="ffmpeg", width=640, height=480)
    assert isinstance(src, FfmpegRtspSource), type(src)
    src.close()

    snap = open_frame_source(url="rtsp://x", prefer="snapshot")
    assert isinstance(snap, SnapshotSource), type(snap)
    snap.close()

    dbg = open_result_sink("stdout")
    assert isinstance(dbg, StdoutSink), type(dbg)

    ws = open_result_sink("ws", host="127.0.0.1", port=0, app_id="t")
    assert isinstance(ws, WsResultSink), type(ws)
    ws.close()
    print("PASS test_no_official_selects_workaround "
          "(FfmpegRtspSource / SnapshotSource / StdoutSink / WsResultSink)")


def test_simulated_official_selects_official():
    """Fake /run/recamera/frame.sock present -> OfficialFrameSource chosen."""
    _clear_env()
    with tempfile.NamedTemporaryFile(prefix="frame-", suffix=".sock") as tf:
        os.environ["RECAMERA_FRAME_SOCK"] = tf.name  # exists on disk now
        caps = registry.capabilities(refresh=True)
        assert caps.frame_broker is True, caps

        src = open_frame_source(url="rtsp://x", prefer="ffmpeg")
        assert isinstance(src, OfficialFrameSource), type(src)
        assert src.sock == tf.name, src.sock
        # explicit snapshot fallback is still honoured verbatim
        snap = open_frame_source(url="rtsp://x", prefer="snapshot")
        assert isinstance(snap, SnapshotSource), type(snap)
        snap.close()
        print("PASS test_simulated_official_selects_official "
              f"(OfficialFrameSource, sock={tf.name})")


def test_prefer_override():
    """RECAMERA_ADAPTER_PREFER forces selection regardless of probe."""
    _clear_env()
    os.environ["RECAMERA_FRAME_SOCK"] = "/nonexistent/frame.sock.absent"
    os.environ["RECAMERA_RESULT_INGRESS"] = "1"  # pretend ingress probes true
    os.environ["RECAMERA_ADAPTER_PREFER"] = "workaround"
    registry.capabilities(refresh=True)
    ws = open_result_sink("ws", host="127.0.0.1", port=0, app_id="t")
    assert isinstance(ws, WsResultSink), type(ws)  # forced workaround despite ingress
    ws.close()

    os.environ["RECAMERA_ADAPTER_PREFER"] = "official"
    registry.capabilities(refresh=True)
    sink = open_result_sink("ws", host="127.0.0.1", port=0, app_id="t")
    assert isinstance(sink, OsdInjectResultSink), type(sink)  # forced official
    print("PASS test_prefer_override (workaround-force + official-force)")


if __name__ == "__main__":
    test_no_official_selects_workaround()
    test_simulated_official_selects_official()
    test_prefer_override()
    _clear_env()
    print("ALL REGISTRY TESTS PASSED")
