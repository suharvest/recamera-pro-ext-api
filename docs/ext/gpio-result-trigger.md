# 用推理结果驱动 GPIO 引脚

> 场景:检测到目标(或满足某条件)时,自动拉高/拉低一个引脚,驱动外部电路(继电器/LED/蜂鸣器/告警等)。
> **这不需要改固件源码,也不需要官方新增功能**——设备已有的两个零件拼起来即可:结果输出(WS)+ 引脚控制(gmgr API)。本文给可直接改用的示例。

## 能干什么 / 边界

- ✅ 推理结果 → 自动拉高/拉低引脚(数字触发)
- ✅ 引脚:**2 根** —— `GPIO3_B2`(pin **106**)、`GPIO3_B3`(pin **107**)
- ❌ 仅数字 **0/1**,**不支持 PWM / 模拟 / 波形**(gmgr `write_value` 硬校验 `value>1` 报错)
- ❌ 更多引脚需扩 gmgr 配置(确认目标 line 未被相机/音频等外设 pinmux 占用)

## 两个零件

### 1. 结果输出(订阅推理结果)
- 本机:`ws://127.0.0.1:8123`(notify WebSocket,无需 token)
- 外部:`wss://<设备IP>/ws/inference/results`(经 nginx,需 JWT Cookie,登录流程见 `result-push.md`)
- 消息是 `InferenceResult` JSON:`detection.entries[].{class_name, score, box}` 等

### 2. 引脚控制(gmgr API)
- 端点:`POST /api/v1/gpio/{id}/value`,body `0` 或 `1`(读电平 `GET`,设方向/上下拉 `POST /api/v1/gpio/{id}/settings`)
- **两种访问方式**:
  - **直连 unix socket**(root 扩展推荐,免 JWT):HTTP over `/dev/shm/gmgr.sock`
  - **经 nginx**(带 JWT Cookie):`https://<设备IP>/api/v1/gpio/{id}/value`,Cookie `token=<JWT>`(登录同 `result-push.md`)
- 首次用某引脚前建议先设方向:`POST /api/v1/gpio/106/settings` body `{"state":"push-pull"}`

## Python 示例(检测到 "person" → 拉高 pin 106,否则拉低)

```python
import json, socket, base64, os, http.client

GMGR_SOCK = "/dev/shm/gmgr.sock"   # 直连,免 JWT(扩展以 root 运行)
PIN = 106

def gpio_write(pin, val):
    # HTTP over unix socket
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM); s.connect(GMGR_SOCK)
    body = str(val)
    req = (f"POST /api/v1/gpio/{pin}/value HTTP/1.1\r\nHost: localhost\r\n"
           f"Content-Type: text/plain\r\nContent-Length: {len(body)}\r\n"
           f"Connection: close\r\n\r\n{body}")
    s.sendall(req.encode()); resp = s.recv(4096); s.close()
    return b"200" in resp.split(b"\r\n")[0]

# 先设方向(一次)
def gpio_settings(pin, state="push-pull"):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM); s.connect(GMGR_SOCK)
    body = json.dumps({"state": state})
    req = (f"POST /api/v1/gpio/{pin}/settings HTTP/1.1\r\nHost: localhost\r\n"
           f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\n"
           f"Connection: close\r\n\r\n{body}")
    s.sendall(req.encode()); s.recv(4096); s.close()

def ws_connect(host="127.0.0.1", port=8123):
    s = socket.socket(); s.connect((host, port))
    k = base64.b64encode(os.urandom(16)).decode()
    s.send((f"GET / HTTP/1.1\r\nHost: {host}\r\nUpgrade: websocket\r\n"
            f"Connection: Upgrade\r\nSec-WebSocket-Key: {k}\r\n"
            f"Sec-WebSocket-Version: 13\r\n\r\n").encode())
    s.recv(4096)  # 101
    return s

def ws_frames(s):
    buf = b""
    while True:
        d = s.recv(65536)
        if not d: break
        buf += d
        while len(buf) >= 2:
            op = buf[0] & 0x0f; b1 = buf[1]; ln = b1 & 0x7f; off = 2
            if ln == 126: ln = int.from_bytes(buf[2:4], "big"); off = 4
            elif ln == 127: ln = int.from_bytes(buf[2:10], "big"); off = 10
            if b1 & 0x80: off += 4
            if len(buf) < off + ln: break
            pl = buf[off:off+ln]; buf = buf[off+ln:]
            if op == 1: yield pl

gpio_settings(PIN, "push-pull")
ws = ws_connect()
last = None
for payload in ws_frames(ws):
    try: msg = json.loads(payload.decode())
    except: continue
    entries = msg.get("detection", {}).get("entries", [])
    hit = any(e.get("class_name") == "person" and e.get("score", 0) > 0.5 for e in entries)
    if hit != last:                 # 只在状态变化时写,减少 IO
        gpio_write(PIN, 1 if hit else 0)
        last = hit
```

约 40 行,全部用设备既有接口,不改固件、不重编。把 `PIN` / 条件(`class_name`/`score` 阈值)换成你的业务即可。

## 常见问题

- **拉不动引脚**:先 `POST /settings` 设 `push-pull`;确认引脚没被别的用途占用;直连 socket 需以 root 运行(扩展经 appmgr 启动即是 root)。
- **要经外网/nginx 调**:把 `gpio_write` 改成带 `Cookie: token=<JWT>` 的 HTTPS 请求(登录拿 token 见 `result-push.md`),端点 `https://<ip>/api/v1/gpio/{id}/value`。
- **要 PWM / 可变信号**:gmgr 当前不支持,需官方在 gmgr 增 PWM 后端(需确认对应 pin 的 PWM 复用可用),不在本文范围。
- **要更多引脚**:当前仅 106/107 引出到 gmgr,扩展需改 gmgr 配置并确认 line 未被占用。
