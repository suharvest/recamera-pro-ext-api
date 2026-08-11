#!/usr/bin/env python3
"""04-gpio-trigger -- 用推理结果驱动 GPIO 引脚。

检测到目标（或满足条件）时自动拉高/拉低一个引脚，驱动外部电路
（继电器/LED/蜂鸣器/告警）。**不需要改固件、不需要官方新增功能**——
用设备已有的两个零件拼起来：结果输出（notify WS）+ 引脚控制（gmgr API）。

逻辑与 docs/ext/gpio-result-trigger.md 一致，这里做成可跑脚本。

边界（gpio-result-trigger.md）：
  - 引脚仅 2 根：GPIO3_B2 = pin 106，GPIO3_B3 = pin 107。
  - 仅数字 0/1，不支持 PWM/模拟（gmgr write_value 硬校验 value>1 报错）。
  - 直连 unix socket 免 JWT，需以 root 运行（扩展经 appmgr 启动即是 root）。

结果来源：本机 notify WebSocket ws://127.0.0.1:8123（无需 token）。
消息是 InferenceResult JSON：detection.entries[].{class_name, score, box}。

运行：  python3 gpio_trigger.py [--pin 106] [--class person] [--score 0.5]
需要：  以 root 运行；设备上 gmgr socket /dev/shm/gmgr.sock + notify WS 可用。
       仅标准库，无第三方依赖。
"""
import argparse
import base64
import json
import os
import socket

GMGR_SOCK = "/dev/shm/gmgr.sock"   # 直连，免 JWT（扩展以 root 运行）


def gpio_settings(pin, state="push-pull"):
    """首次用某引脚前设方向（一次）。"""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.connect(GMGR_SOCK)
    body = json.dumps({"state": state})
    req = (f"POST /api/v1/gpio/{pin}/settings HTTP/1.1\r\nHost: localhost\r\n"
           f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\n"
           f"Connection: close\r\n\r\n{body}")
    s.sendall(req.encode())
    s.recv(4096)
    s.close()


def gpio_write(pin, val):
    """写引脚电平 0/1。HTTP over unix socket。"""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.connect(GMGR_SOCK)
    body = str(val)
    req = (f"POST /api/v1/gpio/{pin}/value HTTP/1.1\r\nHost: localhost\r\n"
           f"Content-Type: text/plain\r\nContent-Length: {len(body)}\r\n"
           f"Connection: close\r\n\r\n{body}")
    s.sendall(req.encode())
    resp = s.recv(4096)
    s.close()
    return b"200" in resp.split(b"\r\n")[0]


def ws_connect(host="127.0.0.1", port=8123):
    """连接 notify WebSocket（无 token）。"""
    s = socket.socket()
    s.connect((host, port))
    k = base64.b64encode(os.urandom(16)).decode()
    s.send((f"GET / HTTP/1.1\r\nHost: {host}\r\nUpgrade: websocket\r\n"
            f"Connection: Upgrade\r\nSec-WebSocket-Key: {k}\r\n"
            f"Sec-WebSocket-Version: 13\r\n\r\n").encode())
    s.recv(4096)   # 101 Switching Protocols
    return s


def ws_frames(s):
    """最小 WebSocket 帧解析，产出 text 帧 payload。"""
    buf = b""
    while True:
        d = s.recv(65536)
        if not d:
            break
        buf += d
        while len(buf) >= 2:
            op = buf[0] & 0x0f
            b1 = buf[1]
            ln = b1 & 0x7f
            off = 2
            if ln == 126:
                ln = int.from_bytes(buf[2:4], "big")
                off = 4
            elif ln == 127:
                ln = int.from_bytes(buf[2:10], "big")
                off = 10
            if b1 & 0x80:            # masked
                off += 4
            if len(buf) < off + ln:
                break
            pl = buf[off:off + ln]
            buf = buf[off + ln:]
            if op == 1:              # text frame
                yield pl


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pin", type=int, default=106, choices=[106, 107],
                    help="GPIO3_B2=106 / GPIO3_B3=107")
    ap.add_argument("--class", dest="klass", default="person", help="触发的类名")
    ap.add_argument("--score", type=float, default=0.5, help="触发的最低置信度")
    args = ap.parse_args()

    gpio_settings(args.pin, "push-pull")   # 设方向（一次）
    ws = ws_connect()
    print("watching class=%r score>%.2f -> pin %d  (Ctrl-C 退出)"
          % (args.klass, args.score, args.pin))

    last = None
    for payload in ws_frames(ws):
        try:
            msg = json.loads(payload.decode())
        except Exception:
            continue
        entries = msg.get("detection", {}).get("entries", [])
        hit = any(e.get("class_name") == args.klass and e.get("score", 0) > args.score
                  for e in entries)
        if hit != last:              # 只在状态变化时写，减少 IO
            gpio_write(args.pin, 1 if hit else 0)
            print("pin %d -> %d" % (args.pin, 1 if hit else 0))
            last = hit


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nstopped.")
