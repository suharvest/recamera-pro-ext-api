#!/usr/bin/env python3
"""
Tiny stdlib WebSocket client to verify WsResultSink is really broadcasting.

    python3 ws_probe.py [host] [port] [n_messages]

Connects, performs the RFC6455 handshake, prints the first N text messages it
receives, then exits. No dependencies.
"""
import base64
import json
import os
import socket
import sys

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def recv_frame(sock):
    def rd(n):
        b = b""
        while len(b) < n:
            c = sock.recv(n - len(b))
            if not c:
                raise ConnectionError("closed")
            b += c
        return b
    b0, b1 = rd(2)
    ln = b1 & 0x7F
    if ln == 126:
        ln = int.from_bytes(rd(2), "big")
    elif ln == 127:
        ln = int.from_bytes(rd(8), "big")
    masked = b1 & 0x80
    mask = rd(4) if masked else b""
    payload = bytearray(rd(ln))
    if masked:
        for i in range(ln):
            payload[i] ^= mask[i % 4]
    return bytes(payload)


def main():
    host = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1"
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 8124
    n = int(sys.argv[3]) if len(sys.argv) > 3 else 8

    key = base64.b64encode(os.urandom(16)).decode()
    sock = socket.create_connection((host, port), timeout=30)
    req = (
        f"GET / HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\n"
        f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
        f"Sec-WebSocket-Version: 13\r\n\r\n"
    )
    sock.sendall(req.encode())
    resp = b""
    while b"\r\n\r\n" not in resp:
        resp += sock.recv(1024)
    assert b"101" in resp.split(b"\r\n")[0], resp[:80]
    print(f"[ws_probe] connected to {host}:{port}, handshake OK", flush=True)

    for i in range(n):
        msg = recv_frame(sock)
        try:
            obj = json.loads(msg)
            print(f"[ws_probe] msg#{i+1} seq={obj.get('seq')} "
                  f"app={obj.get('app')} results={len(obj.get('results', []))} "
                  f"events={len(obj.get('events', []))} :: {msg.decode()[:300]}",
                  flush=True)
        except Exception:
            print(f"[ws_probe] msg#{i+1} raw={msg[:200]!r}", flush=True)
    sock.close()
    print("[ws_probe] done", flush=True)


if __name__ == "__main__":
    main()
