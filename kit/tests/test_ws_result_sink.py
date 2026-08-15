"""WsResultSink is hardened against an unauthenticated reach and a slow client
(C9 / C10).

Two failure modes this pins:

  * BIND LOOPBACK BY DEFAULT (C9). The result stream is published behind the
    nginx JWT edge (proxy_pass -> 127.0.0.1:<port>). A default 0.0.0.0 bind put
    an UNAUTHENTICATED subscription on the LAN; the default is now 127.0.0.1,
    and the front end still reaches it because nginx proxies from loopback.
  * A SLOW/DEAD CLIENT MUST NOT FREEZE INFERENCE (C10). The old code set each
    client blocking and did a synchronous sendall() on the inference thread, so
    one client that stopped reading stalled every app. Each client now has a
    bounded latest-wins queue drained by its own writer thread; the producer
    only ever enqueues (non-blocking), and a persistently-behind client is
    dropped.

The pure helpers (host resolution, queue drop policy, admission caps) are tested
directly; the end-to-end non-blocking property is tested with a writer whose
socket never drains.
"""
import socket
import sys
import threading
import time
import os
import queue as _queue
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from kit.adapters.result_sink import (              # noqa: E402
    WsResultSink,
    _WsClient,
    admit_reason,
    effective_bind_host,
    offer_latest_wins,
)


class BindHostTests(unittest.TestCase):
    def test_defaults_and_aliases_resolve_to_loopback(self):
        for h in (None, "", "  ", "localhost", "LOOPBACK", "local", "127.0.0.1",
                  "::1"):
            self.assertEqual(effective_bind_host(h), "127.0.0.1", repr(h))

    def test_explicit_routable_address_is_honoured(self):
        self.assertEqual(effective_bind_host("0.0.0.0"), "0.0.0.0")
        self.assertEqual(effective_bind_host("192.168.42.1"), "192.168.42.1")

    def test_constructor_default_binds_loopback(self):
        sink = WsResultSink(port=0, app_id="t")
        self.addCleanup(sink.close)
        self.assertEqual(sink.host, "127.0.0.1")
        self.assertGreater(sink.port, 0)  # real ephemeral port recorded


class OfferLatestWinsTests(unittest.TestCase):
    def test_drops_oldest_when_full(self):
        q = _queue.Queue(maxsize=2)
        self.assertFalse(offer_latest_wins(q, "a"))
        self.assertFalse(offer_latest_wins(q, "b"))
        self.assertTrue(offer_latest_wins(q, "c"))   # full -> drop oldest ("a")
        self.assertEqual(q.get_nowait(), "b")
        self.assertEqual(q.get_nowait(), "c")
        self.assertTrue(q.empty())

    def test_returns_false_while_room_remains(self):
        q = _queue.Queue(maxsize=4)
        self.assertFalse(offer_latest_wins(q, 1))
        self.assertEqual(q.qsize(), 1)


class AdmitReasonTests(unittest.TestCase):
    def test_admits_under_caps(self):
        self.assertIsNone(admit_reason(5, 2, 32, 8))

    def test_global_cap(self):
        r = admit_reason(32, 1, 32, 8)
        self.assertIsNotNone(r)
        self.assertIn("client limit", r)

    def test_per_ip_cap(self):
        r = admit_reason(10, 8, 32, 8)
        self.assertIsNotNone(r)
        self.assertIn("per-ip", r)

    def test_zero_disables_a_cap(self):
        self.assertIsNone(admit_reason(1000, 1000, 0, 0))


class _StuckConn:
    """A socket whose sendall() blocks until released -- models a client that
    completed the handshake and then stopped reading."""

    def __init__(self, release: threading.Event):
        self._release = release
        self.closed = False

    def settimeout(self, _t):
        pass

    def sendall(self, _data):
        # Block as a real sendall would once the peer's receive window is full.
        self._release.wait(5.0)

    def close(self):
        self.closed = True


class SlowClientBackpressureTests(unittest.TestCase):
    def test_offer_never_blocks_and_laggard_is_dropped(self):
        release = threading.Event()
        conn = _StuckConn(release)
        client = _WsClient(conn, "1.2.3.4", maxq=4, send_timeout=0.1,
                           lag_limit=10)
        try:
            # Hammer the client far past (maxq + lag_limit) frames; every offer
            # must return effectively instantly even though the writer is stuck.
            t0 = time.monotonic()
            for i in range(500):
                client.offer(b"frame-%d" % i)
            elapsed = time.monotonic() - t0
            self.assertLess(elapsed, 1.0,
                            f"offer() blocked on a stuck writer ({elapsed:.2f}s)")
            # A client this far behind is disconnected, freeing its slot.
            self.assertFalse(client.alive())
        finally:
            release.set()
            client.close()


class SlowClientEndToEndTests(unittest.TestCase):
    """Real loopback socket: a subscriber that never reads must not stall emit()."""

    @staticmethod
    def _handshake(sock: socket.socket) -> None:
        import base64
        import os as _os
        key = base64.b64encode(_os.urandom(16)).decode()
        req = (
            "GET / HTTP/1.1\r\n"
            "Host: 127.0.0.1\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        ).encode()
        sock.sendall(req)
        resp = b""
        sock.settimeout(5)
        while b"\r\n\r\n" not in resp:
            chunk = sock.recv(1024)
            if not chunk:
                raise AssertionError("server closed during handshake")
            resp += chunk
        assert b"101" in resp.split(b"\r\n", 1)[0], resp[:64]

    def test_non_reading_subscriber_does_not_block_inference(self):
        sink = WsResultSink(host="127.0.0.1", port=0, app_id="t",
                            client_queue=8, send_timeout=0.2, lag_limit=20)
        self.addCleanup(sink.close)
        cli = socket.create_connection(("127.0.0.1", sink.port), timeout=5)
        self.addCleanup(cli.close)
        self._handshake(cli)
        # give the accept loop a moment to register the client
        deadline = time.time() + 2
        while time.time() < deadline and sink.client_count() < 1:
            time.sleep(0.02)
        self.assertGreaterEqual(sink.client_count(), 1)

        # The client never calls recv() again. Spam results; emit() must stay
        # fast regardless (per-client queue absorbs/drops; writer is isolated).
        t0 = time.monotonic()
        for i in range(1000):
            sink.emit({"results": [{"i": i}]}, pts=float(i))
        elapsed = time.monotonic() - t0
        self.assertLess(elapsed, 3.0,
                        f"emit() stalled on a non-reading client ({elapsed:.2f}s)")


if __name__ == "__main__":
    unittest.main()
