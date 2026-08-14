# AI 结果软件叠加与结果流（:8124 / 前端 canvas）

> 事实来源：
> - kit 结果流：`kit/adapters/result_sink.py`（`WsResultSink`，端口 8124）、`kit/adapters/registry.py`（`select_result_sink` 默认 ws + OSD opt-in）。
> - kit 主循环：`kit/app.py`（每帧 `set_frame_size()` → `emit()`）。
> - 官方 React 前端：`recamera_web_react/`（真实路径在 wsl2-local `project/app/recamera_web/recamera_web_react/`，闭源前端的可改副本）——`components/live_feed/PreviewPage.js`（`/preview` 路由）挂 `AiResultOverlay`。
> - 内建推理开关：entry.cgi `POST /model/inference`（见 [control-api.md](./control-api.md) §2.2、[rkipc-rpc-status.md](./rkipc-rpc-status.md)）。

## 0. 定位（先读这段）

自建应用（kit apps）算出的检测/姿态/分类/OCR/语音结果，**默认走软件叠加**：应用把结构化结果广播到本机 WS `:8124`，官方 React 前端 `/preview` 页上覆一层透明 canvas 订阅并逐帧画框/骨架/标签。

- **软件叠加不进码流。** RTSP 主/子码流里没有这些框；框只画在浏览器 canvas 上。关掉内建推理后 RTSP 码流无任何叠加——已抓帧核实（见 §4）。
- 要让结果**烧进码流 + 录像**（OSD/RGN），是另一条路（`result-in.sock` 结果注入 / `kind=osd`），默认不启用，见 §2 与 [README.md](./README.md) §4。
- 本文只讲 **kit 自建应用**的结果流；固件内建推理的原生结果流（`:8123` / notify）见 [result-push.md](./result-push.md)。

## 1. 结果流：`WsResultSink` @ :8124

kit 的 `App` 基类持有一个 `ResultSink`，每帧调 `emit()` 发一条结果。默认后端是 `WsResultSink`（`registry.select_result_sink(kind="ws")`，`registry.py:168`），一个纯 stdlib 手写 RFC6455 广播服务：

- 监听本机 **8124**（`result_sink.py:132`，不占 rkipc 的 `:8123`）。
- 外部经 nginx 路由 `/appcenter/ws/results`（`ext_appmgr.conf`，带 `auth_request /_jwt_verify`）。
- **单向下行广播**：服务端只推、不收注入。

### 1.1 线格式（每帧一条 WS text 消息）

```jsonc
{
  "type": "results",
  "app":  "<app-id>",
  "pts":  12345.678,              // 帧采集时间戳（monotonic 秒）
  "seq":  42,                     // 单调帧计数
  "frame": { "width": 640, "height": 480 },   // ★推理帧像素尺寸 = 坐标参考系
  "results": [ { "box": [x1,y1,x2,y2], "cls": 0, "cls_name": "person", "score": 0.92 }, ... ],
  "events":  [ ... ]             // run() 通过 self.emit() 产出的应用级事件
}
```

- **`frame:{width,height}` 是坐标参考系**（`result_sink.py:143-147`）：`results[].box` 的像素坐标就落在这个 `width×height` 的推理帧空间里。`kit/app.py` 基类每帧在 `emit()` 前先调一次 `set_frame_size(w,h)`（`result_sink.py:70-80`），把当前推理帧尺寸写进消息。**消费端必须用消息里的 `frame`，不能硬编码 640×480**——不同应用/模型输入尺寸不同（facemesh 192、ppocr 480 等）。
- `results[].box` 单位是**像素**（推理帧空间），非归一化。这与 `OfficialResultSink`（OSD 注入路径，坐标归一化 [0,1]）是两套约定，见 §3。

## 2. 默认 ws vs 内建 OSD opt-in

`select_result_sink` 的选择逻辑（`registry.py:140-183`）：

| 场景 | 选中的 sink | 触发 |
|---|---|---|
| **默认** | `WsResultSink`（软件叠加，:8124） | 不设任何变量 |
| 烧进码流/录像（OSD/RGN） | `OfficialResultSink`（`result-in.sock`） | `RECAMERA_RESULT_OSD=1` **或** `kind="osd"` **或** `RECAMERA_ADAPTER_PREFER=official` |
| 调试打印 | `StdoutSink` | `kind="stdout"` |

要点：**OSD 烧流是 opt-in，不因 `result-in.sock` 存在就自动切**（`registry.py:12-13`）。`RECAMERA_ADAPTER_PREFER=workaround` 永远强制软件叠加。默认路径下方案商拿到的是 canvas 叠加，视频码流保持干净。

## 3. 坐标映射（canvas 叠加怎么画对）

`AiResultOverlay`（挂在 `PreviewPage.js`）订阅 `:8124`，把每个 `box` 从推理帧空间映射到浏览器显示区：

```
显示坐标 = box(推理帧像素) / frame(width,height)   // 先归一化到 [0,1]
         × 显示区尺寸（object-fit: contain 的实际绘制矩形，非容器尺寸）
         × devicePixelRatio (DPR)                 // canvas 位图按 DPR 放大避免糊
```

- **`object-fit: contain`**：go2rtc 预览按 contain 适配容器，画面四周可能留黑边。canvas 必须按**实际绘制矩形**（含 letterbox 偏移）映射，不能按容器整块，否则框会偏。
- **DPR**：canvas 位图尺寸 = CSS 尺寸 × `devicePixelRatio`，画笔坐标同乘 DPR，Retina 屏才不糊。
- **主/子码流切换每帧重算**：预览可在主码流/子码流间切换（分辨率不同），显示区尺寸变化时**每帧**用当前 `frame` + 当前显示区重算，不缓存缩放系数。
- 开关默认开（overlay 默认可见）。

> 参考系一致性：消息里的 `frame` 是**推理帧**尺寸，go2rtc 预览是**编码帧**尺寸——两者宽高比一致（同一 VI 源），contain 映射用归一化中间量对齐，不依赖两者像素相等。

## 4. 内建检测开关与"不进码流"的验证

端侧内建检测（固件自带 NPU 推理，结果可上 OSD）的开关是 entry.cgi：

```bash
# 设备本机，localhost 免 JWT（见 control-api.md §0 鉴权）
curl -k -X POST https://127.0.0.1/cgi-bin/entry.cgi/model/inference \
  -H 'Content-Type: application/json' -d '{"iEnable":0}'   # 0=关内建, 1=开
```

- **走 HTTPS 443**，不要打 80（80→307 跳 443，POST body 会丢，见 control-api.md §1.3 踩坑）。
- 关掉内建推理后，RTSP 主码流里**无任何检测框**——已抓帧铁证：软件叠加（:8124 canvas）不进码流，码流里的框只可能来自内建 OSD。这条区分了"软件叠加"与"内建 OSD 烧流"两条路。

## 5. 与其他结果通路的关系

| 通路 | 端口/入口 | 谁产结果 | 进码流? | 文档 |
|---|---|---|---|---|
| **kit 软件叠加（本文）** | WS `:8124` → `/appcenter/ws/results` | 自建 kit 应用 | 否（canvas） | 本文 |
| 内建推理原生结果 | WS `:8123` → `/ws/inference/results` | 固件内建 NPU 推理 | 内建 OSD 可上 | [result-push.md](./result-push.md) |
| notify 分发 | `/var/tmp/notify` → WS/MQTT/HTTP/UART | 任意本机进程 | 否 | [result-push.md](./result-push.md) |
| 结果注入（OSD+录像） | `result-in.sock`（`OfficialResultSink`） | 自建应用（opt-in） | 是 | [README.md](./README.md) §4 |
