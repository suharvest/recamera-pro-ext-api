"""
Offline tests for WsResultSink's wire format (result_sink.py).

Focus: the `frame:{width,height}` reference-size field the /appcenter overlay
needs to map box/keypoint PIXELS into the video display area. Without it the
panel guesses a default reference (640x480) and every coordinate is scaled wrong
(the retail/fall regression this fixes).

We stand up a real WsResultSink on an ephemeral port, connect a minimal RFC6455
client, drive set_frame_size()/emit() the way the base loop does, and assert the
decoded JSON.

Run:  python3 -m kit.adapters.test_result_sink      (from repo root)
"""
import base64
import hashlib
import json
import os
import socket
import struct
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from kit.adapters.result_sink import WsResultSink

_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def _ws_connect(port):
    """Open a socket, do the client-side RFC6455 handshake, return the socket."""
    s = socket.create_connection(("127.0.0.1", port), timeout=5)
    key = base64.b64encode(os.urandom(16)).decode()
    req = (
        f"GET / HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
        "Upgrade: websocket\r\nConnection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
    )
    s.sendall(req.encode())
    resp = b""
    while b"\r\n\r\n" not in resp:
        resp += s.recv(1024)
    accept = base64.b64encode(
        hashlib.sha1((key + _WS_GUID).encode()).digest()
    ).decode()
    assert f"Sec-WebSocket-Accept: {accept}" in resp.decode(), resp
    return s


def _ws_recv_text(s):
    """Read one unmasked server->client text frame, return the decoded str."""
    s.settimeout(5)
    b0 = s.recv(1)[0]
    assert (b0 & 0x0F) == 0x1, f"expected text opcode, got {b0:#x}"
    b1 = s.recv(1)[0]
    n = b1 & 0x7F
    if n == 126:
        n = struct.unpack(">H", s.recv(2))[0]
    elif n == 127:
        n = struct.unpack(">Q", s.recv(8))[0]
    data = b""
    while len(data) < n:
        data += s.recv(n - len(data))
    return data.decode("utf-8")


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def test_emit_includes_frame_size():
    """emit() after set_frame_size() must carry frame:{width,height} matching the
    inference-frame pixel size the base loop passed (retail/fall = 1280x720)."""
    port = _free_port()
    sink = WsResultSink(host="127.0.0.1", port=port, app_id="retail-vision")
    try:
        client = _ws_connect(port)
        # Let the server register the client before broadcasting.
        for _ in range(50):
            if sink.client_count() >= 1:
                break
            time.sleep(0.02)
        assert sink.client_count() == 1, "client did not register"

        # Base loop order: set_frame_size(frame.w, frame.h) THEN emit.
        sink.set_frame_size(1280, 720)
        sink.emit({"results": [{"box": [345, 467, 575, 717], "cls": 0,
                                "cls_name": "person", "score": 0.9}],
                   "events": []}, pts=12.5)

        msg = json.loads(_ws_recv_text(client))
        assert msg["type"] == "results", msg
        assert msg["app"] == "retail-vision", msg
        assert "frame" in msg, f"missing frame reference size: {msg}"
        assert msg["frame"] == {"width": 1280, "height": 720}, msg["frame"]
        # Coords are still raw inference-frame PIXELS (overlay scales them itself).
        assert msg["results"][0]["box"] == [345, 467, 575, 717], msg
        client.close()
    finally:
        sink.close()
    print("PASS test_emit_includes_frame_size")


def test_emit_without_frame_size_omits_field():
    """If the base loop never called set_frame_size (should not happen for vision
    apps), emit() omits `frame` rather than lying with a bogus size -- the
    overlay then falls back deliberately instead of trusting wrong numbers."""
    port = _free_port()
    sink = WsResultSink(host="127.0.0.1", port=port, app_id="x")
    try:
        client = _ws_connect(port)
        for _ in range(50):
            if sink.client_count() >= 1:
                break
            time.sleep(0.02)
        sink.emit({"results": [], "events": []}, pts=1.0)
        msg = json.loads(_ws_recv_text(client))
        assert "frame" not in msg, f"frame must be absent when size unknown: {msg}"
        client.close()
    finally:
        sink.close()
    print("PASS test_emit_without_frame_size_omits_field")


def test_frame_size_updates_on_stream_switch():
    """Switching stream resolution updates the announced reference size on the
    next emit (so main->sub stream change is reflected, not stuck stale)."""
    port = _free_port()
    sink = WsResultSink(host="127.0.0.1", port=port, app_id="x")
    try:
        client = _ws_connect(port)
        for _ in range(50):
            if sink.client_count() >= 1:
                break
            time.sleep(0.02)
        sink.set_frame_size(1280, 720)
        sink.emit({"results": [], "events": []}, pts=1.0)
        m1 = json.loads(_ws_recv_text(client))
        assert m1["frame"] == {"width": 1280, "height": 720}, m1

        sink.set_frame_size(640, 360)
        sink.emit({"results": [], "events": []}, pts=2.0)
        m2 = json.loads(_ws_recv_text(client))
        assert m2["frame"] == {"width": 640, "height": 360}, m2
        client.close()
    finally:
        sink.close()
    print("PASS test_frame_size_updates_on_stream_switch")


if __name__ == "__main__":
    test_emit_includes_frame_size()
    test_emit_without_frame_size_omits_field()
    test_frame_size_updates_on_stream_switch()
    print("ALL WS RESULT SINK TESTS PASSED")
