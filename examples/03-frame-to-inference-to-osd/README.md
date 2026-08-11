# 03 — frame → inference → OSD：完整闭环

最能体现"方案商自带流水线"价值的示例：

```
FrameSource 拿帧  →  你自己的推理/算法  →  ResultSink 回注 OSD/RTSP/推送
```

固件负责取流、编码、OSD 叠加、录像、分发；**推理这一环换成你的**。你的框和内建推理的框在同一套 OSD/RTSP/WS/MQTT 通路里输出。

## 这个示例做了什么

- 用 `FrameSource` 逐帧拿灰度 Y 平面；
- 用一个**纯 numpy 的帧差运动检测**当"推理"占位（比较相邻两帧，把变化区域框出来）；
- 把运动框经 `ResultSink.send_detections(pts_us=frame.pts_us, ...)` 回注，出现在 RTSP/预览的 OSD 上。

> **占位逻辑说明**：`detect()` 是演示用的帧差运动检测，**不是真实模型**。它存在只为不依赖任何具体模型就能跑通闭环。真实项目里把 `detect()` 换成你的模型 forward + 后处理，输入 `frame.array`（灰度）或 `frame.to_bgr()`（彩色，需 opencv），输出同样的 `[(x1,y1,x2,y2,score,label), ...]` 即可。

## 核心代码

```python
from recamera_ext import FrameSource, ResultSink

prev = None
with FrameSource() as src, ResultSink(source_id="motion") as sink:
    for frame in src:
        cur = frame.array                        # 零拷贝 Y 平面视图
        if prev is not None:
            boxes = my_model(prev, cur)          # -> [(x1,y1,x2,y2,score,label), ...]
            if boxes:
                sink.send_detections(pts_us=frame.pts_us, boxes=boxes)
        prev = cur.copy()                        # 跨帧保留必须 .copy()
```

两处关键：

- **`prev = cur.copy()`**：`frame.array` 是零拷贝视图，下一帧迭代开始后底层内存会被覆写。要把上一帧留到下次迭代比较，必须 `.copy()`。这是帧差/光流/攒 batch 类算法的通用纪律。
- **`pts_us=frame.pts_us`**：让 OSD 按就近帧对齐叠加（容差约 1 帧周期）。`frame.pts_us` 与固件内建推理同一时钟源（VI 帧 PTS，`CLOCK_MONOTONIC` 微秒）。

## 依赖

- `numpy`
- 设备上有 `librecamera_ext.so.1` + **含扩展 API 的固件**（`frame.sock` 和 `result-in.sock` 都要有）
- `recamera_ext` Python 包可 import
- 若把 `detect()` 换成需要彩色输入的模型，`frame.to_bgr()` 需要 opencv

## 怎么跑

```sh
adb push motion_to_osd.py /root/
adb shell 'cd /root && python3 motion_to_osd.py --thresh 25 --min-pixels 800'
```

参数：`--source-id`（OSD 颜色由它哈希决定，默认 motion）、`--thresh`（像素差阈值，默认 25）、`--min-pixels`（判定运动的最小变化像素数，默认 800）。

## 预期输出 / 验证

脚本侧（画面里有东西动时）：

```
running: 640x640 source_id='motion'  (Ctrl-C 退出)
motion seq=101 pts=1699999999 box=(120,80,300,360) score=0.42
motion seq=102 pts=1700033332 box=(118,78,305,362) score=0.55
```

打开 RTSP/预览：在摄像头前挥手，会看到一个 `motion` 框跟着变化区域走。静止时没有框（不发）。

## 常见问题

- **框一直满屏 / 一直有框**：光照抖动或自动曝光在动。调大 `--thresh`（如 40）或 `--min-pixels`。
- **完全没框**：调小 `--thresh` / `--min-pixels`；确认画面里确实有运动；确认 `result-in.sock` 存在。
- **框慢半拍**：帧差算法本身就比当前帧滞后一帧（用的是"上一帧 vs 当前帧"），属正常。真实模型无此问题。
- **发送被限速**：运动持续时每帧都发，若帧率 > 60fps 会触发限速丢弃；正常帧率（≤30fps）无碍。
- **CPU 占用**：`frame.array` 是灰度视图，帧差是整帧 numpy 运算。要更省可只在下采样后的小图上做差分。
