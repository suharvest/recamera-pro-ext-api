"""
Capability registry for the L0 adapter layer (PYTHON_KIT_DESIGN.md §L0,
BOOTSTRAP_PATH.md §3).

At startup the kit probes whether the *official* firmware endpoints exist, and
every adapter factory picks its implementation from the resulting capability
set:

    FrameSource  = caps.frame_broker   ? OfficialFrameSource  : FfmpegRtspSource
    ResultSink   = caps.result_ingress ? OsdInjectResultSink  : WsResultSink
    AudioSource  = caps.audio_broker   ? OfficialPcmSource     : (workaround TBD)
    ControlPlane = caps.control_api    ? OfficialControl       : CgiControl

On today's firmware (6.1.157) none of the official endpoints exist, so every
factory selects the existing verified workaround and behaviour is byte-for-byte
unchanged. When a firmware upgrade adds an official endpoint, the next probe
hits, the factory returns the `Official*` implementation, and **no application
code and no repackaging is required** -- exactly the "smooth migration" contract
in BOOTSTRAP_PATH.md §3.

Overrides (both for real deployments and for testing the switch logic)
----------------------------------------------------------------------
* `RECAMERA_FRAMES_SOCK` / `RECAMERA_AUDIO_SOCK` -- point the probe at a custom
  socket path (lets a test create a fake socket and prove the switch).
* `RECAMERA_RESULT_INGRESS` / `RECAMERA_CONTROL_API` -- "1"/"0" to force those
  (currently un-probeable) capabilities on/off.
* `RECAMERA_ADAPTER_PREFER` = `auto` (default) | `official` | `workaround`
  -- a global manifest-style override of the per-capability auto selection
  (BOOTSTRAP_PATH.md §3: "可留 manifest 里 prefer: official|workaround 供覆盖").
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional


# -- capability probing ------------------------------------------------------- #
# Canonical extension-API socket paths (RECAMERA_PRO_API_SPEC.md §1: all live
# under /run/recamera/). These are the *real* names the shipped librecamera_ext
# uses -- singular `frame.sock`, and `result-in.sock` -- not the earlier
# placeholder `/var/run/recamera/frames.sock`.
def _frames_sock_path() -> str:
    return os.environ.get("RECAMERA_FRAMES_SOCK", "/run/recamera/frame.sock")


def _result_sock_path() -> str:
    return os.environ.get("RECAMERA_RESULT_SOCK", "/run/recamera/result-in.sock")


def _audio_sock_path() -> str:
    return os.environ.get("RECAMERA_AUDIO_SOCK", "/run/recamera/audio.sock")


def _env_bool(name: str) -> bool:
    return str(os.environ.get(name, "")).strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Capabilities:
    """Result of one capability probe (mirrors BOOTSTRAP_PATH.md §3 `caps`)."""
    frame_broker: bool = False
    result_ingress: bool = False
    audio_broker: bool = False
    control_api: bool = False


def probe_capabilities() -> Capabilities:
    """Probe the official firmware endpoints. Cheap + side-effect free."""
    return Capabilities(
        frame_broker=os.path.exists(_frames_sock_path()),
        # Both frame + result sockets are filesystem-probeable now that the real
        # paths are known. RECAMERA_RESULT_INGRESS still force-overrides (used by
        # tests, and to opt in before the socket-perms are relaxed).
        result_ingress=(os.path.exists(_result_sock_path())
                        or _env_bool("RECAMERA_RESULT_INGRESS")),
        audio_broker=os.path.exists(_audio_sock_path()),
        control_api=_env_bool("RECAMERA_CONTROL_API"),
    )


_CACHED: Optional[Capabilities] = None


def capabilities(refresh: bool = False) -> Capabilities:
    """Return the cached capability probe (probed once per process).

    `refresh=True` re-probes -- used by tests that mutate the environment, and
    available to appmgr if it ever needs to re-evaluate after a firmware event.
    """
    global _CACHED
    if refresh or _CACHED is None:
        _CACHED = probe_capabilities()
    return _CACHED


# -- selection policy --------------------------------------------------------- #
def _prefer_official(cap_present: bool) -> bool:
    """Apply the global prefer override on top of the auto (cap-present) choice."""
    pref = str(os.environ.get("RECAMERA_ADAPTER_PREFER", "auto")).strip().lower()
    if pref == "official":
        return True
    if pref == "workaround":
        return False
    return cap_present  # auto


# -- factories ---------------------------------------------------------------- #
def select_frame_source(url: str, prefer: str = "ffmpeg", **kw):
    """Pick a FrameSource implementation.

    `prefer` selects the *workaround backend* ("ffmpeg" streaming | "snapshot"
    low-fps fallback). The official broker, when present, supersedes both --
    except when the caller explicitly asks for the "snapshot" debug fallback,
    which is honoured verbatim.
    """
    caps = capabilities()
    if prefer != "snapshot" and _prefer_official(caps.frame_broker):
        from .official import OfficialFrameSource
        return OfficialFrameSource(url=url, sock=_frames_sock_path(), **kw)
    from .frame_source import FfmpegRtspSource, SnapshotSource
    if prefer == "snapshot":
        return SnapshotSource(url=url, **kw)
    return FfmpegRtspSource(url=url, **kw)


def select_result_sink(kind: str = "ws", **kw):
    """Pick a ResultSink implementation.

    `kind` = "ws" (broadcast overlay) | "stdout" (debug). The "stdout" debug
    sink is always honoured verbatim; the official OSD ingress, when present,
    supersedes the "ws" path only.
    """
    caps = capabilities()
    if kind != "stdout" and _prefer_official(caps.result_ingress):
        from .official import OsdInjectResultSink
        return OsdInjectResultSink(**kw)
    from .result_sink import StdoutSink, WsResultSink
    if kind == "stdout":
        return StdoutSink(**kw)
    return WsResultSink(**kw)


def select_audio_source(prefer: str = "rtsp", **kw):
    """Pick an AudioSource implementation (16k mono PCM).

    Selection order:
      1. Official R8 clean-PCM broker (`/var/run/recamera/audio.sock`) when
         probed present (or forced via RECAMERA_ADAPTER_PREFER=official).
      2. `RtspAudioSource` (DEFAULT workaround) -- demuxes the audio track from
         rkipc's combined RTSP stream via ffmpeg. Does NOT touch the mic or
         video, needs no /dev/snd takeover. This is the verified-feasible path
         on firmware 6.1.157 and therefore the default.
      3. `AlsaTakeoverSource` (degraded fallback, `prefer="alsa"`) -- direct mic
         takeover via arecord. On shipping firmware the mic is held exclusively
         by rkipc, so its `open()` raises `AudioDeviceBusy` unless the caller
         opts into freeing it (which blips video) -- see audio_source.py verdict.

    `prefer` selects the workaround backend when no official broker is present:
    "rtsp" (default) | "alsa". The official broker, when present, supersedes both.
    """
    caps = capabilities()
    if _prefer_official(caps.audio_broker):
        from .official import OfficialPcmSource
        return OfficialPcmSource(sock=_audio_sock_path(), **kw)
    if prefer == "alsa":
        from .audio_source import AlsaTakeoverSource
        return AlsaTakeoverSource(**kw)
    from .audio_source import RtspAudioSource
    return RtspAudioSource(**kw)


def select_control(**kw):
    """Pick a ControlPlane implementation.

    The official versioned control API (`OfficialControl`) is preferred when
    probed present (or forced via RECAMERA_ADAPTER_PREFER=official). On today's
    firmware it is not present, so this falls back to `CgiControl`, the
    workaround plane that drives the device's existing `entry.cgi` endpoints
    (localhost, no JWT) for set_inference and proxies a FrameSource frame for
    snapshot.
    """
    caps = capabilities()
    if _prefer_official(caps.control_api):
        from .official import OfficialControl
        return OfficialControl(**kw)
    from .cgi_control import CgiControl
    return CgiControl(**kw)
