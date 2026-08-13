"""
MqttSink -- Home Assistant / MQTT result publisher for reCamera Pro (2nd gen).

L0 adapter (same ABC as WsResultSink). Where WsResultSink feeds the /appcenter
overlay a rich per-frame stream (pixel boxes, keypoints, ...), MqttSink feeds a
*home-automation* audience: a compact per-frame state document plus retained
Home Assistant MQTT-Discovery configs so HA auto-creates one entity per app
signal (detection count, fall state, entry/exit counts, QR text, ...).

Why a hand-rolled client (no paho-mqtt)
---------------------------------------
We only ever PUBLISH (QoS 0) -- never subscribe. MQTT 3.1.1 CONNECT + PUBLISH +
PINGREQ is a few dozen bytes of framing, so we implement it directly on a
stdlib socket (~150 LOC below). Zero new dependencies enter the shared device
venv or any app package. A background thread owns the socket: it connects,
publishes the retained discovery configs + an "online" availability message,
then keeps the link alive with PINGREQ and transparently reconnects (re-arming
discovery) after any drop. `emit()` is best-effort and never blocks or raises
into the inference loop -- a dead broker degrades to "WS only", exactly the
behaviour when MQTT is left unconfigured.

MQTT state document (published to <base_topic>/<app>/state each processed frame)
    {
      "app": "yolo-detector",
      "pts": 123.456, "seq": 42,
      "results_count": 3,                 # len(results)
      "person_count": 2,                  # visible person pose results
      "fallen_count": 1,                  # visible person results in fall state
      "counts_by_kind": {"detection": 3}, # tally of events[].kind
      "class_counts": {"person": 1, ...}, # tally of results[].cls_name
      "summary": { ... },                 # scalar event fields (fall aggregate-safe)
      "events": [ ... ]                   # app events (no pixel boxes dropped;
                                          #   kept small -- raw results omitted)
    }
HA entity `value_template`s (declared in each app manifest's `ha_entities`)
reference this document, e.g. `{{ value_json.results_count }}` or
`{{ value_json.summary.fall_detected }}`.
"""
from __future__ import annotations

import json
import os
import socket
import struct
import threading
import time
from typing import Any, Dict, List, Optional

from kit.adapters.result_sink import ResultSink


# --------------------------------------------------------------------------- #
# minimal MQTT 3.1.1 publish-only wire codec
# --------------------------------------------------------------------------- #
def _remaining_length(n: int) -> bytes:
    """Encode an MQTT variable-length integer (remaining length field)."""
    out = bytearray()
    while True:
        b = n % 128
        n //= 128
        if n > 0:
            b |= 0x80
        out.append(b)
        if n == 0:
            break
    return bytes(out)


def _mqtt_str(s: str) -> bytes:
    """UTF-8 string prefixed with a 2-byte big-endian length (MQTT wire string)."""
    data = s.encode("utf-8")
    return struct.pack(">H", len(data)) + data


class _MqttConnection:
    """A single live MQTT publish-only connection. Not thread-safe on its own;
    the owning MqttSink serialises access with a lock."""

    def __init__(self, host: str, port: int, client_id: str,
                 keepalive: int = 30, username: str = "", password: str = "",
                 will_topic: str = "", will_payload: bytes = b"",
                 will_retain: bool = True):
        self.host = host
        self.port = port
        self.client_id = client_id
        self.keepalive = keepalive
        self.username = username
        self.password = password
        self.will_topic = will_topic
        self.will_payload = will_payload
        self.will_retain = will_retain
        self._sock: Optional[socket.socket] = None

    def connect(self, timeout: float = 5.0) -> None:
        sock = socket.create_connection((self.host, self.port), timeout=timeout)
        sock.settimeout(timeout)
        # -- CONNECT variable header -------------------------------------- #
        flags = 0x02  # clean session
        payload = _mqtt_str(self.client_id)
        if self.will_topic:
            flags |= 0x04                       # will flag
            if self.will_retain:
                flags |= 0x20                   # will retain
            payload += _mqtt_str(self.will_topic)
            payload += struct.pack(">H", len(self.will_payload)) + self.will_payload
        if self.username:
            flags |= 0x80
            payload += _mqtt_str(self.username)
            if self.password:
                flags |= 0x40
                payload += _mqtt_str(self.password)
        var_header = (
            _mqtt_str("MQTT")                   # protocol name
            + bytes([0x04])                     # protocol level 4 (3.1.1)
            + bytes([flags])
            + struct.pack(">H", self.keepalive)
        )
        pkt = var_header + payload
        sock.sendall(bytes([0x10]) + _remaining_length(len(pkt)) + pkt)
        # -- read CONNACK (0x20, len 2, [ack_flags, return_code]) --------- #
        hdr = self._recv_exact(sock, 2)
        if not hdr or hdr[0] != 0x20:
            sock.close()
            raise ConnectionError(f"unexpected CONNACK header {hdr!r}")
        body = self._recv_exact(sock, hdr[1])
        if len(body) < 2 or body[1] != 0x00:
            rc = body[1] if len(body) >= 2 else -1
            sock.close()
            raise ConnectionError(f"MQTT CONNECT refused rc={rc}")
        self._sock = sock

    @staticmethod
    def _recv_exact(sock: socket.socket, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = sock.recv(n - len(buf))
            if not chunk:
                break
            buf += chunk
        return buf

    def publish(self, topic: str, payload: bytes, retain: bool = False) -> None:
        if self._sock is None:
            raise ConnectionError("not connected")
        header0 = 0x30 | (0x01 if retain else 0x00)   # PUBLISH, QoS0, no dup
        body = _mqtt_str(topic) + payload             # QoS0 -> no packet id
        self._sock.sendall(bytes([header0]) + _remaining_length(len(body)) + body)

    def ping(self) -> None:
        if self._sock is None:
            raise ConnectionError("not connected")
        self._sock.sendall(b"\xc0\x00")   # PINGREQ (server replies PINGRESP)

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.sendall(b"\xe0\x00")   # DISCONNECT
            except OSError:
                pass
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None


# --------------------------------------------------------------------------- #
# device identity
# --------------------------------------------------------------------------- #
def device_identifier() -> str:
    """Stable per-device id for the HA `device.identifiers` grouping.

    RECAMERA_SN env (set by the platform) wins; else the U-Boot `sn`; else the
    hostname. Sanitised to [a-z0-9_] so it is topic/entity-id safe."""
    cand = os.environ.get("RECAMERA_SN", "").strip()
    if not cand:
        try:
            import subprocess
            out = subprocess.run(["fw_printenv", "-n", "sn"], capture_output=True,
                                 text=True, timeout=2)
            if out.returncode == 0:
                cand = out.stdout.strip()
        except Exception:
            cand = ""
    if not cand:
        cand = socket.gethostname() or "recamera"
    safe = "".join(c if (c.isalnum() or c == "_") else "_" for c in cand.lower())
    return safe or "recamera"


# --------------------------------------------------------------------------- #
# reusable HA MQTT-discovery primitives (module-level so ConfigurableSink's
# HaDiscoveryFormatter can WRAP -- not duplicate -- them; MqttSink's own methods
# below delegate here, so its behaviour is byte-for-byte unchanged)
# --------------------------------------------------------------------------- #
def ha_discovery_topic(discovery_prefix: str, node: str, app_id: str,
                       ent: dict) -> str:
    """`<prefix>/<component>/recamera_<node>_<app>/<object_id>/config`."""
    component = ent.get("component", "sensor")
    object_id = ent["object_id"]
    return (f"{discovery_prefix}/{component}/"
            f"recamera_{node}_{app_id}/{object_id}/config")


def ha_discovery_payload(node: str, app_id: str, state_topic: str,
                         status_topic: str, device_name: str,
                         ent: dict) -> dict:
    """The retained HA MQTT-Discovery config document for one entity.

    Availability (`availability_topic`/`payload_available`/`payload_not_available`)
    and a stable `unique_id` are always emitted so HA marks the entity
    online/offline off the LWT and never creates duplicates on reconnect."""
    object_id = ent["object_id"]
    cfg: Dict[str, Any] = {
        "name": ent.get("name", object_id),
        "unique_id": f"recamera_{node}_{app_id}_{object_id}",
        "state_topic": state_topic,
        "value_template": ent.get("value_template", ""),
        "availability_topic": status_topic,
        "payload_available": "online",
        "payload_not_available": "offline",
        "device": {
            "identifiers": [f"recamera_{node}"],
            "name": device_name,
            "manufacturer": "Seeed Studio",
            "model": "reCamera Pro",
        },
    }
    for k in ("device_class", "unit_of_measurement", "state_class", "icon",
              "entity_category"):
        if ent.get(k):
            cfg[k] = ent[k]
    return cfg


# --------------------------------------------------------------------------- #
# MqttSink
# --------------------------------------------------------------------------- #
class MqttSink(ResultSink):
    """Best-effort HA/MQTT publisher. Construct once per app run alongside the
    WS sink (see kit.app.run_app). Never raises into emit()."""

    def __init__(
        self,
        *,
        host: str,
        port: int = 1883,
        app_id: str = "app",
        base_topic: str = "recamera",
        discovery_prefix: str = "homeassistant",
        username: str = "",
        password: str = "",
        entities: Optional[List[dict]] = None,
        device_name: str = "reCamera Pro",
        keepalive: int = 30,
        verbose: bool = False,
    ):
        self.host = host
        self.port = int(port)
        self.app_id = app_id
        self.base_topic = base_topic.rstrip("/") or "recamera"
        self.discovery_prefix = discovery_prefix.rstrip("/") or "homeassistant"
        self.username = username or ""
        self.password = password or ""
        self.entities = entities or []
        self.device_name = device_name
        self.keepalive = max(10, int(keepalive))
        self.verbose = verbose

        self.node = device_identifier()
        self.state_topic = f"{self.base_topic}/{self.app_id}/state"
        self.status_topic = f"{self.base_topic}/{self.app_id}/status"

        self._seq = 0
        self._conn: Optional[_MqttConnection] = None
        self._lock = threading.Lock()
        self._connected = threading.Event()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    # -- background connect / keepalive / reconnect ----------------------- #
    def _run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            if not self._connected.is_set():
                try:
                    self._open()
                    backoff = 1.0
                    if self.verbose:
                        print(f"[mqtt:{self.app_id}] connected "
                              f"{self.host}:{self.port} node={self.node}", flush=True)
                except Exception as e:
                    if self.verbose:
                        print(f"[mqtt:{self.app_id}] connect failed: {e} "
                              f"(retry {backoff:.0f}s)", flush=True)
                    self._stop.wait(backoff)
                    backoff = min(backoff * 2, 30.0)
                    continue
            # keepalive tick
            self._stop.wait(self.keepalive / 2.0)
            if self._stop.is_set():
                break
            with self._lock:
                conn = self._conn
            if conn is not None:
                try:
                    conn.ping()
                except Exception:
                    self._drop()

    def _open(self) -> None:
        conn = _MqttConnection(
            self.host, self.port,
            client_id=f"recamera-{self.app_id}-{self.node}"[:23] + str(os.getpid() % 1000),
            keepalive=self.keepalive,
            username=self.username, password=self.password,
            will_topic=self.status_topic, will_payload=b"offline", will_retain=True,
        )
        conn.connect()
        with self._lock:
            self._conn = conn
            self._connected.set()
        # availability + retained discovery on every (re)connect
        self._safe_publish(self.status_topic, b"online", retain=True)
        self._publish_discovery()

    def _drop(self) -> None:
        with self._lock:
            conn, self._conn = self._conn, None
            self._connected.clear()
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    def _safe_publish(self, topic: str, payload: bytes, retain: bool = False) -> bool:
        with self._lock:
            conn = self._conn
        if conn is None:
            return False
        try:
            conn.publish(topic, payload, retain=retain)
            return True
        except Exception:
            self._drop()
            return False

    # -- HA MQTT discovery ------------------------------------------------ #
    def _discovery_topic(self, ent: dict) -> str:
        return ha_discovery_topic(self.discovery_prefix, self.node,
                                  self.app_id, ent)

    def _discovery_payload(self, ent: dict) -> dict:
        return ha_discovery_payload(self.node, self.app_id, self.state_topic,
                                    self.status_topic, self.device_name, ent)

    def _publish_discovery(self) -> None:
        for ent in self.entities:
            if "object_id" not in ent:
                continue
            topic = self._discovery_topic(ent)
            payload = json.dumps(self._discovery_payload(ent),
                                 separators=(",", ":")).encode("utf-8")
            self._safe_publish(topic, payload, retain=True)

    # -- ResultSink ------------------------------------------------------- #
    @staticmethod
    def _is_person_result(result: dict) -> bool:
        """Return whether a result represents a person.

        Fall detection marks every pose with ``kind=person``.  The class-name
        checks preserve useful counts for generic YOLO results, while the
        track/state shape is a compatibility fallback for older fall app
        payloads that did not set ``kind``.
        """
        if not isinstance(result, dict):
            return False
        for key in ("kind", "cls_name", "label"):
            value = result.get(key)
            if isinstance(value, str) and value.strip().lower() == "person":
                return True
        return (result.get("track_id") is not None and
                ("fall_detected" in result or "state" in result))

    @staticmethod
    def _build_state(app_id: str, seq: int, pts: float,
                     results: List[dict], events: List[dict]) -> dict:
        counts_by_kind: Dict[str, int] = {}
        summary: Dict[str, Any] = {}
        for ev in events:
            k = ev.get("kind")
            if k is not None:
                counts_by_kind[k] = counts_by_kind.get(k, 0) + 1
            for key, val in ev.items():
                if key == "kind":
                    continue
                # last-wins union of scalar fields across all events
                if val is None or isinstance(val, (str, int, float, bool)):
                    summary[key] = val
        class_counts: Dict[str, int] = {}
        for r in results:
            name = r.get("cls_name")
            if isinstance(name, str):
                class_counts[name] = class_counts.get(name, 0) + 1
        person_results = [r for r in results
                          if MqttSink._is_person_result(r)]
        person_count = len(person_results)
        fallen_count = sum(1 for r in person_results
                           if bool(r.get("fall_detected")))
        # Keep the counts in summary too: existing HA/custom consumers often
        # read all app-level values from that object, while the top-level keys
        # make the new values unambiguous and easy to template.
        summary["person_count"] = person_count
        summary["fallen_count"] = fallen_count
        if person_results:
            # The old single-person template reads summary.fall_detected.  Keep
            # that compatibility field aggregate-safe for multi-person frames
            # instead of letting the last pose_state event win.
            summary["fall_detected"] = fallen_count > 0
        fall_event_ids = [
            int(ev["event_id"])
            for ev in events
            if isinstance(ev, dict) and ev.get("kind") == "fall"
            and isinstance(ev.get("event_id"), (int, float))
        ]
        if fall_event_ids:
            # A later normal person's pose_state must not erase the edge event
            # id emitted for another person in this same frame.
            summary["event_id"] = max(fall_event_ids)
        return {
            "app": app_id,
            "pts": pts,
            "seq": seq,
            "results_count": len(results),
            "person_count": person_count,
            "fallen_count": fallen_count,
            "counts_by_kind": counts_by_kind,
            "class_counts": class_counts,
            "summary": summary,
            "events": events,
        }

    def emit(self, payload: dict, pts: float) -> None:
        self._seq += 1
        state = self._build_state(
            self.app_id, self._seq, pts,
            payload.get("results") or [], payload.get("events") or [],
        )
        body = json.dumps(state, separators=(",", ":")).encode("utf-8")
        self._safe_publish(self.state_topic, body, retain=False)

    def client_count(self) -> int:
        # For the base-loop debug line: 1 if broker link is up, else 0.
        return 1 if self._connected.is_set() else 0

    def close(self) -> None:
        self._stop.set()
        # best-effort graceful offline before tearing down
        self._safe_publish(self.status_topic, b"offline", retain=True)
        self._drop()
