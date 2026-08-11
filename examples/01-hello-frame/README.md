# 01 — hello-frame：拿几帧存成图

用 `FrameSource` 从摄像头零拷贝取到 NV12 帧，把灰度 Y 平面存成图。这是最小取帧示例。

## 核心代码（5 行）

```python
from recamera_ext import FrameSource

with FrameSource() as src:          # 默认订阅 NPU 同款分辨率/格式（NV12）
    for frame in src:               # frame.array: 零拷贝 Y 平面视图 (height, width)
        do_something(frame.array)   # 你的处理；离开本次迭代帧自动释放
        break
```

`frame.array` 是**灰度 Y 平面**的零拷贝视图（形状 `(height, width)`，`uint8`）。多数检测/关键点模型可直接吃灰度；需要彩色 BGR 时用 `frame.to_bgr()`（返回拷贝，需 opencv）。

## 依赖

- `numpy`（`frame.array` 依赖）
- 设备上有 `librecamera_ext.so.1` + **含扩展 API 的固件**（`/run/recamera/frame.sock` 存在）
- `recamera_ext` Python 包在脚本可 import 的位置（见总 README「部署到设备」）

本示例存 PGM（P5 二进制灰度图），不依赖 opencv/PIL。

## 怎么跑

```sh
# 拷到设备
adb push hello_frame.py /root/
adb push -p <SDK>/python/recamera_ext /root/recamera_ext   # 若设备上没装这个包

# 在设备上运行（默认存 3 帧到当前目录）
adb shell 'cd /root && python3 hello_frame.py -n 3 -o /tmp/frames'
```

参数：`-n` 存几帧（默认 3），`-o` 输出目录（默认当前目录）。

## 预期输出

```
subscribed: 640x640 fourcc=0x3231564e pool_depth=6 max_outstanding=2
saved /tmp/frames/frame_00042.pgm  (seq=42 pts_us=123456789 dropped=False)
saved /tmp/frames/frame_00043.pgm  (seq=43 pts_us=123489789 dropped=False)
saved /tmp/frames/frame_00044.pgm  (seq=44 pts_us=123522789 dropped=False)
done, 3 frame(s) saved.
```

（分辨率/`pool_depth`/`max_outstanding` 由固件握手返回，实际数值以设备为准。）

拉回来看：

```sh
adb pull /tmp/frames/frame_00042.pgm .
# PGM 可用 ImageMagick / GIMP / macOS 预览（转格式）打开
```

## 零拷贝生命周期（务必读）

`frame.array` 是对底层 dma-buf `mmap` 内存的 **numpy 视图，不是拷贝**。它**只在当前迭代内有效**：`for` 循环推进到下一帧时，SDK 会 `release` 上一帧，其内存可能立刻被新采集的帧覆写。

- 当前迭代内读它做推理 → **不用 copy**。
- 要把帧留到循环外（存盘、进队列、跨线程、攒 batch）→ 必须 `frame.array.copy()` 或 `frame.to_bgr()`（后者本身返回拷贝）。

本示例的 `save_pgm()` 里 `np.ascontiguousarray(y_view)` 已经拷出了一份连续副本，所以存盘安全。如果你把 `frame.array` 的引用塞进 list 攒起来再一起处理，那就错了——存进去的都是同一个会被覆写的窗口。

## 常见问题

- **`rc_ext_frame_open failed: err=3`（EBUSY）**：订阅数达上限，或 NPU 通道未启用。
- **`rc_ext_frame_open failed`（其它）/ 连接失败**：固件不含扩展 API（`ls -l /run/recamera/` 无 `frame.sock`），或权限不足（非 root 且不在 `recamera-ext` 组）。
- **迭代很久不产帧**：`timeout_ms` 默认 1000ms，超时内部重试不报错；确认摄像头 pipeline 在跑（有 RTSP 流即在跑）。
