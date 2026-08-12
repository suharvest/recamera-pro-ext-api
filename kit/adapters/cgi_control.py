"""
CgiControl -- workaround control plane over the device's existing entry.cgi.

L0 adapter layer (BOOTSTRAP_PATH.md §2.4 R4). This is the reverse-engineered
counterpart to `OfficialControl`: instead of a future versioned control API it
drives the endpoints the shipped firmware already exposes through nginx +
`entry.cgi`, so the 9 kit apps get a working `ControlPlane` on TODAY's firmware
with zero application changes (registry.py swaps `OfficialControl` in later).

Two capabilities, two very different mechanisms
-----------------------------------------------
* set_inference(enable/model/fps)  -- a real device endpoint exists:
      POST http://127.0.0.1/cgi-bin/entry.cgi/model/inference?id=<model_id>
      body JSON, all three fields optional and co-sendable:
          {"iEnable":0|1, "sModel":"<file>", "iFPS":<int >=0>}
      (handler model_api.cpp:1052). Success -> {"code":0,"message":"success"}.
      `iFPS` is the NPU inference throttle, NOT the video encoder frame rate.
  Auth: entry.cgi behind nginx trusts 127.0.0.1 (rest_api.cpp:auth_verify top
      level pass-through for HTTP_X_INTERNAL_FROM_LOCALHOST=1), so a plain
      localhost HTTP request needs no JWT. We speak HTTP over TCP to nginx, not
      the gmgr unix socket.

* snapshot()  -- entry.cgi has NO frame-grab endpoint (confirmed). So snapshot
      is implemented as a FRAME PROXY (BOOTSTRAP_PATH decision "方案 A"): pull a
      single frame through the kit's own FrameSource (whichever the registry
      selects -- official dma-buf broker or the ffmpeg RTSP workaround), then
      JPEG-encode it with OpenCV. No new frame-connection logic is invented here.

Stdlib only for HTTP (http.client) -- no third-party dependency. cv2 + numpy are
already present for the vision apps and are imported lazily inside snapshot() so
an audio-only venv can still import this module.
"""
from __future__ import annotations

import http.client
import json
import ssl
from typing import Optional

from .official import ControlPlane

# entry.cgi is mounted under this nginx location; PATH_INFO is appended.
CGI_BASE = "/cgi-bin/entry.cgi"
INFERENCE_PATH = "/model/inference"


class CgiControl(ControlPlane):
    """Control plane backed by the device's existing `entry.cgi` endpoints.

    Signature mirrors the other adapters (accepts and ignores extra kw) so the
    registry can construct it with the same `**kw` it passes everywhere.
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 443,
                 use_tls: bool = True, model_id: int = 0, timeout: float = 10.0,
                 frame_url: Optional[str] = None, verbose: bool = True,
                 **_ignored):
        self.host = host
        self.port = int(port)
        # nginx redirects plain HTTP (port 80) to HTTPS with a 307, so entry.cgi
        # is reachable only over TLS (self-signed cert). Default to HTTPS on 443.
        self.use_tls = bool(use_tls)
        self.model_id = int(model_id)
        self.timeout = float(timeout)
        # Optional RTSP url override forwarded to the workaround FrameSource;
        # None lets the registry/FrameSource use its own default sub-stream.
        self.frame_url = frame_url
        self.verbose = verbose

    # -- low-level HTTP to entry.cgi (localhost, no JWT) -------------------- #
    def _request(self, method: str, path: str,
                 body: Optional[bytes] = None) -> dict:
        """Send one HTTP request to entry.cgi and return the parsed JSON dict.

        Raises RuntimeError on transport failure, non-2xx status, or a JSON
        envelope whose `code` is present and non-zero.
        """
        if self.use_tls:
            # Cert is self-signed and this is a loopback request; skip verify.
            ctx = ssl._create_unverified_context()
            conn = http.client.HTTPSConnection(self.host, self.port,
                                               timeout=self.timeout,
                                               context=ctx)
        else:
            conn = http.client.HTTPConnection(self.host, self.port,
                                               timeout=self.timeout)
        headers = {"Host": "localhost", "Connection": "close"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        try:
            conn.request(method, CGI_BASE + path, body=body, headers=headers)
            resp = conn.getresponse()
            raw = resp.read()
            status = resp.status
        finally:
            conn.close()

        if not (200 <= status < 300):
            raise RuntimeError(
                "entry.cgi %s %s -> HTTP %d: %s"
                % (method, path, status, raw[:200].decode("utf-8", "replace")))

        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception as e:
            raise RuntimeError(
                "entry.cgi %s %s -> non-JSON response (%s): %s"
                % (method, path, e, raw[:200].decode("utf-8", "replace")))

        # entry.cgi envelope: {"code":0,"message":"success", ...payload...}.
        if isinstance(data, dict) and data.get("code", 0) != 0:
            raise RuntimeError(
                "entry.cgi %s %s -> code=%s message=%s"
                % (method, path, data.get("code"), data.get("message")))
        return data if isinstance(data, dict) else {"data": data}

    def _inference_query(self) -> str:
        return "%s?id=%d" % (INFERENCE_PATH, self.model_id)

    # -- ControlPlane ABC --------------------------------------------------- #
    def set_inference(self, *, enable: bool, model: Optional[str] = None,
                      fps: Optional[int] = None) -> None:
        """Enable/disable inference and optionally switch model / set NPU fps.

        Maps to POST /model/inference?id=<model_id> with a JSON body carrying
        only the fields actually supplied (the handler treats each key as an
        independent optional update).
        """
        payload: dict = {"iEnable": 1 if enable else 0}
        if model is not None:
            payload["sModel"] = str(model)
        if fps is not None:
            payload["iFPS"] = int(fps)
        body = json.dumps(payload).encode("utf-8")
        self._request("POST", self._inference_query(), body=body)

    def get_inference(self) -> dict:
        """Read current inference state (helper for verification / callers).

        Returns the handler payload, e.g.
        {"iEnable","sModel","iFPS","iActualFPS","sStatus", ...}.
        """
        return self._request("GET", self._inference_query())

    def snapshot(self) -> bytes:
        """Grab one frame via the kit FrameSource and return JPEG bytes.

        entry.cgi has no frame-grab endpoint, so this proxies a single frame
        through whichever FrameSource the capability registry selects (official
        broker or ffmpeg RTSP workaround), then JPEG-encodes it with OpenCV.
        """
        try:
            import cv2
        except Exception as e:  # pragma: no cover - device has cv2
            raise RuntimeError(
                "snapshot() needs OpenCV (cv2) to JPEG-encode the frame: %s" % e)
        try:
            from .registry import select_frame_source
        except Exception as e:
            raise RuntimeError("snapshot() could not import FrameSource: %s" % e)

        url = self.frame_url or _DEFAULT_URL()
        # prefer_rga=False: a one-shot snapshot has no throughput need, and the
        # RGA hardware NV12->RGB path in OfficialFrameSource can fault on some
        # librga builds; the OpenCV convert is correct and safe. The kwarg is
        # ignored by the ffmpeg/snapshot workaround sources (they take **_ignored).
        src = select_frame_source(url=url, prefer_rga=False)

        frame = None
        try:
            for f in src.frames():
                frame = f
                break
        finally:
            src.close()

        if frame is None:
            raise RuntimeError("snapshot() got no frame from the FrameSource")

        # kit Frame.data is contiguous HWC uint8. fmt is "RGB" for every current
        # backend; cv2.imencode expects BGR, so flip the channel order. (If a
        # future backend yields NV12, convert here.)
        arr = frame.data
        if frame.fmt == "RGB":
            bgr = arr[:, :, ::-1]
        else:
            raise RuntimeError("snapshot() unsupported frame fmt: %s" % frame.fmt)

        ok, buf = cv2.imencode(".jpg", bgr)
        if not ok:
            raise RuntimeError("snapshot() cv2.imencode failed")
        return bytes(buf.tobytes())


def _DEFAULT_URL() -> str:
    """Default RTSP sub-stream for the workaround FrameSource (imported lazily
    so this module stays importable without numpy/cv2)."""
    from .frame_source import DEFAULT_SUB_STREAM
    return DEFAULT_SUB_STREAM
