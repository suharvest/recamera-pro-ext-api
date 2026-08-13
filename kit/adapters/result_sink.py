"""
ResultSink adapter for reCamera Pro (Rockchip RV1126B).

L0 adapter layer (see docs/guide/kit-design.md §0 / §0.5). The application never
touches a sink directly -- the `App` base class owns one and calls `emit()` for
every processed frame. The concrete backend is swappable behind the `ResultSink`
ABC:

* `StdoutSink`   -- prints one JSON line per frame. Zero deps, debug/CI use.
* `WsResultSink` -- broadcasts the structured per-frame JSON to every connected
                    WebSocket client on a local port, for the `/appcenter`
                    overlay panel to subscribe to and draw boxes on top of the
                    go2rtc preview. Pure stdlib (socket + threading + hashlib);
                    a hand-rolled RFC6455 server frames text messages itself, so
                    NO `websockets`/`aiohttp`/etc. dependency is pulled in.

Why our own lightweight WS (not the official :8123)
---------------------------------------------------
Per §0.5 the official :8123 result stream belongs to rkipc's own inference and
we must not squat on it. Our self-hosted apps emit their results on a separate
port we own (default 8124). When the official OSD/RGN injection interface lands,
a new `OsdInjectSink` implementation drops in behind this same ABC and the
capability registry selects it -- application code does not change.

Wire format (one JSON object per frame, newline-free, one WS text message):
    {
      "type": "results",
      "app": "<app-id>",
      "pts": 12345.678,          # frame capture timestamp (monotonic seconds)
      "seq": 42,                 # monotonic frame counter
      "results": [ {box,cls,cls_name,score}, ... ],   # raw detections
      "events":  [ ... ]         # app-level events from on_results()
    }
"""
from __future__ import annotations

import base64
import hashlib
import json
import socket
import struct
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, List, Optional

_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class ResultSink(ABC):
    """Abstract result publisher. The App base class owns one; apps never call it."""

    @abstractmethod
    def emit(self, payload: dict, pts: float) -> None:
        """Publish one frame's structured result payload."""
        raise NotImplementedError

    def emit_meta(self, payload: dict) -> None:
        """Publish an out-of-band meta message NOT tied to a frame's results
        (e.g. the periodic pipeline `metrics` event: FPS + per-stage latency).

        Default: no-op. Only sinks whose audience wants live telemetry override
        it -- WsResultSink broadcasts it to the /appcenter debug panel, while
        MqttSink deliberately ignores it (an empty HA state doc every second is
        noise). This keeps metrics strictly additive: existing results/events/
        MQTT behaviour is untouched.
        """
        pass

    def set_frame_size(self, w: int, h: int) -> None:
        """Tell the sink the current frame's pixel dimensions.

        The App base loop calls this once per frame BEFORE emit(). Only sinks
        that must convert pixel coordinates need it: OfficialResultSink divides
        box/keypoint pixel coords by (w, h) to get the normalized [0,1] fractions
        the extension-API OSD renderer requires. Default: no-op, so the
        WS/stdout/MQTT workaround sinks (which keep their own pixel/JSON
        convention and wire format) are completely unaffected.
        """
        pass

    def close(self) -> None:  # optional override
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class StdoutSink(ResultSink):
    """Debug sink: print one compact JSON line per frame to stdout."""

    def __init__(self, only_nonempty: bool = False):
        self.only_nonempty = only_nonempty

    def emit(self, payload: dict, pts: float) -> None:
        if self.only_nonempty and not payload.get("results") and not payload.get("events"):
            return
        print(json.dumps(payload, separators=(",", ":")), flush=True)

    def emit_meta(self, payload: dict) -> None:
        print(json.dumps(payload, separators=(",", ":")), flush=True)


def _ws_encode_text(data: bytes) -> bytes:
    """Encode a single unmasked server->client WebSocket text frame (FIN=1)."""
    header = bytearray([0x81])  # FIN + opcode 0x1 (text)
    n = len(data)
    if n < 126:
        header.append(n)
    elif n < 65536:
        header.append(126)
        header += struct.pack(">H", n)
    else:
        header.append(127)
        header += struct.pack(">Q", n)
    return bytes(header) + data


class WsResultSink(ResultSink):
    """
    Minimal broadcast WebSocket server, stdlib only.

    Runs a background accept loop; each accepted client is upgraded (RFC6455
    handshake) and added to a broadcast set. `emit()` serialises the payload
    once and best-effort sends it to every client, dropping any that error.
    Slow/dead clients never block the inference loop.
    """

    def __init__(self, host: str = "0.0.0.0", port: int = 8124,
                 app_id: str = "app", preserve_envelope: bool = False):
        self.host = host
        self.port = port
        self.app_id = app_id
        # When True, emit()/publish_envelope() broadcast the payload verbatim
        # (no self-generated type/app/pts/seq/frame). ConfigurableSink's
        # WsChannel sets this so the canonical envelope built ONCE upstream is
        # not stamped a second time here. Default False keeps the standalone
        # WsResultSink behaviour (legacy overlay path) byte-for-byte unchanged.
        self.preserve_envelope = preserve_envelope
        self._clients: List[socket.socket] = []
        self._lock = threading.Lock()
        self._srv: Optional[socket.socket] = None
        self._accept_thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._seq = 0
        # Current inference-frame pixel size (base loop calls set_frame_size once
        # per frame BEFORE emit). Emitted as `frame:{width,height}` so the
        # /appcenter overlay knows the coordinate reference space the box/keypoint
        # PIXELS live in -- without it the panel guesses (default 640x480) and
        # every coordinate is scaled wrong. None until the first frame is seen.
        self._frame_w: Optional[int] = None
        self._frame_h: Optional[int] = None
        self._start()

    def set_frame_size(self, w: int, h: int) -> None:
        """Record the current inference-frame pixel size (base loop calls this
        per frame). Unlike OfficialResultSink -- which DIVIDES coords by this to
        normalize -- WsResultSink keeps pixel coords verbatim and just ANNOUNCES
        the reference size in the wire message so the overlay maps 1:1."""
        if w and h and w > 0 and h > 0:
            self._frame_w = int(w)
            self._frame_h = int(h)

    # -- lifecycle -------------------------------------------------------- #
    def _start(self) -> None:
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind((self.host, self.port))
        self._srv.listen(8)
        self._srv.settimeout(0.5)
        self._accept_thread = threading.Thread(target=self._accept_loop,
                                               daemon=True)
        self._accept_thread.start()

    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _addr = self._srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                self._handshake(conn)
                conn.setblocking(True)
                with self._lock:
                    self._clients.append(conn)
            except Exception:
                try:
                    conn.close()
                except Exception:
                    pass

    @staticmethod
    def _handshake(conn: socket.socket) -> None:
        conn.settimeout(5)
        req = b""
        while b"\r\n\r\n" not in req:
            chunk = conn.recv(1024)
            if not chunk:
                raise ConnectionError("client closed during handshake")
            req += chunk
            if len(req) > 65536:
                raise ValueError("handshake too large")
        key = None
        for line in req.split(b"\r\n"):
            if line.lower().startswith(b"sec-websocket-key:"):
                key = line.split(b":", 1)[1].strip()
                break
        if not key:
            raise ValueError("no Sec-WebSocket-Key")
        accept = base64.b64encode(
            hashlib.sha1(key + _WS_GUID.encode()).digest()
        )
        resp = (
            b"HTTP/1.1 101 Switching Protocols\r\n"
            b"Upgrade: websocket\r\n"
            b"Connection: Upgrade\r\n"
            b"Sec-WebSocket-Accept: " + accept + b"\r\n\r\n"
        )
        conn.sendall(resp)

    # -- ResultSink ------------------------------------------------------- #
    def _broadcast(self, obj: dict) -> None:
        """Serialise one JSON object and best-effort send it as a text frame to
        every connected client, dropping any that error. Never blocks the loop."""
        frame = _ws_encode_text(
            json.dumps(obj, separators=(",", ":")).encode("utf-8")
        )
        with self._lock:
            clients = list(self._clients)
        dead = []
        for c in clients:
            try:
                c.sendall(frame)
            except Exception:
                dead.append(c)
        if dead:
            with self._lock:
                for c in dead:
                    if c in self._clients:
                        self._clients.remove(c)
            for c in dead:
                try:
                    c.close()
                except Exception:
                    pass

    def publish_envelope(self, envelope: dict) -> None:
        """Broadcast a pre-built canonical envelope verbatim (used by
        ConfigurableSink's WsChannel). No second seq/timestamp/frame stamp."""
        obj = dict(envelope)
        obj.setdefault("type", "results")
        self._broadcast(obj)

    def emit(self, payload: dict, pts: float) -> None:
        if self.preserve_envelope:
            # Upstream already built the canonical envelope (app/seq/timestamp/
            # frame). Broadcast it as-is; do not add a second sequence.
            self.publish_envelope(payload)
            return
        self._seq += 1
        payload = dict(payload)
        payload.setdefault("type", "results")
        payload.setdefault("app", self.app_id)
        payload["pts"] = pts
        payload["seq"] = self._seq
        # Announce the inference-frame reference size the result coords map to.
        # The overlay reads this to scale box/keypoint PIXELS into the video
        # display area; without it the panel falls back to a hard-coded guess.
        if self._frame_w and self._frame_h:
            payload["frame"] = {"width": self._frame_w, "height": self._frame_h}
        self._broadcast(payload)

    def emit_meta(self, payload: dict) -> None:
        """Broadcast a metrics/meta message on the SAME WS channel as results.
        Tagged with its own `type` (e.g. "metrics") so the panel can demux; it
        does NOT advance the results `seq` counter."""
        payload = dict(payload)
        payload.setdefault("type", "meta")
        payload.setdefault("app", self.app_id)
        self._broadcast(payload)

    def client_count(self) -> int:
        with self._lock:
            return len(self._clients)

    def close(self) -> None:
        self._stop.set()
        if self._srv:
            try:
                self._srv.close()
            except Exception:
                pass
        with self._lock:
            clients = list(self._clients)
            self._clients.clear()
        for c in clients:
            try:
                c.close()
            except Exception:
                pass


class MultiSink(ResultSink):
    """Fan-out sink: forward every emit() to a list of child sinks.

    Lets one app run publish results to several backends at once -- e.g. the
    /appcenter overlay WS *and* an MQTT/Home-Assistant broker -- without the
    app or the base loop knowing there is more than one. Each child is isolated:
    a raising/slow child never blocks or breaks the others (WsResultSink and
    MqttSink are both already best-effort internally, so this is belt-and-braces).
    """

    def __init__(self, sinks: List[ResultSink]):
        self.sinks = [s for s in sinks if s is not None]

    def emit(self, payload: dict, pts: float) -> None:
        for s in self.sinks:
            try:
                s.emit(payload, pts)
            except Exception:
                pass

    def emit_meta(self, payload: dict) -> None:
        for s in self.sinks:
            try:
                s.emit_meta(payload)
            except Exception:
                pass

    def set_frame_size(self, w: int, h: int) -> None:
        # Fan the frame size out so a wrapped OfficialResultSink can normalize.
        for s in self.sinks:
            try:
                s.set_frame_size(w, h)
            except Exception:
                pass

    def client_count(self) -> int:
        total = 0
        for s in self.sinks:
            fn = getattr(s, "client_count", None)
            if callable(fn):
                try:
                    total += fn()
                except Exception:
                    pass
        return total

    def close(self) -> None:
        for s in self.sinks:
            try:
                s.close()
            except Exception:
                pass


# --------------------------------------------------------------------------- #
# Configurable-output interfaces (docs internal/OUTPUT_SINK_SPEC.md §2)
#
# These are the ABCs for the unified `ConfigurableSink` output pipeline. The
# concrete channels/formatters and ConfigurableSink itself live in
# kit/adapters/output_sink.py to keep this module lean and avoid disturbing the
# legacy sink path; they import these three names from here.
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class OutputMessage:
    """One formatted message ready for a channel to publish.

    `topic` is None for channels that carry their own destination (WS, or an
    MQTT channel falling back to its default state topic); formatters that
    target specific MQTT topics (mapping rows, HA discovery) set it explicitly.
    """
    body: bytes
    topic: Optional[str] = None
    content_type: str = "application/json"
    retain: bool = False
    metadata: dict = field(default_factory=dict)


class OutputChannel(ABC):
    """A transport (ws/mqtt/http/uart). Best-effort; never raises into emit()."""
    name: str = "channel"

    @abstractmethod
    def publish(self, message: "OutputMessage") -> None:
        raise NotImplementedError

    def client_count(self) -> int:
        return 0

    def close(self) -> None:
        pass


class OutputFormatter(ABC):
    """Turns one canonical envelope into zero+ OutputMessages for a channel."""

    @abstractmethod
    def format(self, envelope: dict, *, channel: str) -> List["OutputMessage"]:
        raise NotImplementedError

    def on_channel_ready(self, channel: "OutputChannel") -> List["OutputMessage"]:
        """Messages to publish when a channel (re)connects -- e.g. HA discovery
        + retained `online`. Default: nothing."""
        return []


def open_result_sink(kind: str = "ws", **kw) -> ResultSink:
    """Factory. `kind` = "ws" (broadcast) | "stdout" (debug).

    Delegates to the capability registry, which probes for the official R2
    result ingress and returns an `OsdInjectResultSink` when present. On today's
    firmware there is no official ingress, so the registry falls back to the
    workaround sink selected by `kind` and behaviour is unchanged. The "stdout"
    debug sink is always honoured verbatim.
    """
    from .registry import select_result_sink
    return select_result_sink(kind=kind, **kw)
