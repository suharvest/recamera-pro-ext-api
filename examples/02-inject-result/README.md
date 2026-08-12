# 02 — inject-result：注入结果到 OSD / RTSP / WS

用 `ResultSink` 把你算出的结果送回固件。它走**与内建推理完全相同的三路分发**：

1. **OSD 叠加** — 画进 RTSP/预览叠加层（按 `source_id` 哈希分配颜色）
2. **录像** — 进 vigil 录像队列，回放可见
3. **推送** — 转发 WS（`127.0.0.1:8123` / `/ws/inference/results`）/ MQTT / HTTP / UART

## 核心代码（5 行）

```python
from recamera_ext import ResultSink

with ResultSink(source_id="my-app") as sink:
    # boxes: (x1, y1, x2, y2, score, label[, class_id]) 像素坐标, score 0..1
    sink.send_detections(pts_us=0, boxes=[(100, 80, 240, 300, 0.92, "person")])
```

`pts_us=0` 表示不与具体帧关联（结果照常叠加/推送）。要与某帧对齐叠加时传该帧的 `frame.pts_us`（见示例 03）。`label` 支持中文（自动 UTF-8 编码）。

## 各任务类型

本示例 `--task` 覆盖四种（对应 `ResultSink` 的四个 send 方法）：

| `--task` | 方法 | 每条数据格式 |
|---|---|---|
| `detection` | `send_detections` | `(x1, y1, x2, y2, score, label[, class_id])` |
| `classification` | `send_classification` | `(score, class_id, label)` |
| `tracking` | `send_tracking` | `(x1, y1, x2, y2, score, class_id, label, track_id)` |
| `keypoints` | `send_keypoints` | `dict{ points:[(x,y,score,keypoint_id),...], 可选 box/score/class_id/label }` |

还有 `send_segmentation`（带 row-major mask），格式见 `__init__.py` 的 docstring，本示例从略。

## 依赖

- 设备上有 `librecamera_ext.so.1` + **含扩展 API 的固件**（`/run/recamera/result-in.sock` 存在）
- `recamera_ext` Python 包可 import
- 无需 numpy/opencv（本示例只发结构化结果，不碰图像）

## 怎么跑

```sh
adb push inject_result.py /root/
adb shell 'cd /root && python3 inject_result.py --task detection --interval 1'
```

参数：`--task`（默认 detection）、`--source-id`（默认 my-app，**不能是 "builtin"**）、`--interval`（间隔秒，默认 1.0）、`--count`（次数，0=一直发）。

## 预期输出 / 怎么验证

脚本侧：

```
connected, source_id='my-app'  task=detection  (Ctrl-C 退出)
injected #1
injected #2
...
```

看结果去哪了：

- **RTSP/预览**：打开 `rtsp://<设备IP>:...`（见固件文档的流地址），画面上会出现 `person` 框，颜色由 `source_id` 哈希决定。
- **WS 推送**：从设备上或局域网订阅 notify WS，会收到对应的 `InferenceResult` JSON。示例 04 的 `ws_connect()` 可直接拿来订阅 `ws://127.0.0.1:8123`。

## 约束（docs/guide/README.md §4.3）

- `source_id` **不能用保留字 `"builtin"`**（内建推理专用，外部使用被拒 EAUTH）。服务端按连接的 peercred 身份校验，无注册条目时会把 source_id 改写为 `"uid:<n>"`。
- **限速**：每连接 **60 msg/s**（burst 15）+ 全局 120 msg/s（burst 30）；单条 payload ≤ 64 KB。超限**丢弃 + 计数**（不断连接）。别超过帧率发。
- **并发**：≤ 4 个结果注入连接。

## result-in.sock vs notify 的区别

本 API（`result-in.sock`）= OSD + 录像 + 推送三路。若你**只要推送、不要叠加/录像**，用 legacy notify（`docs/guide/result-push.md`，`/var/tmp/notify`）。要框出现在画面里就用本 API。

## 常见问题

- **`send_detections failed: rc=-2`（EAUTH）**：`source_id` 用了 `"builtin"`，换个名字。
- **`rc_ext_result_open failed`**：固件不含扩展 API，或权限不足（见总 README）。
- **框不出现在画面**：确认是 `result-in.sock`（本示例）而非 notify；坐标是否落在画面内（像素坐标，相对订阅分辨率）。
- **发太快没全上屏**：超过 60 msg/s 的会被丢弃计数，把 `--interval` 调大。
