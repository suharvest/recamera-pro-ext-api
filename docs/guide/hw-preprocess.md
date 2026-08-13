# 硬件预处理加速（RGA letterbox / `App.model_frame`）

> 适用设备：reCamera Pro（RV1126B / recamera_v2）。
> 读者：**方案商 / app 开发者**——想让自己的视觉 app 少花每帧几十毫秒在 Python 缩放上，**只改一行类属性**。
>
> 事实来源：`kit/app.py`（`App.model_frame` 声明 + 主循环选路）、`kit/adapters/official.py`（`_convert` 三态返回、`_rga_letterbox()`、三层回退）、`kit/adapters/_rga.py`（librga ctypes 绑定）、`kit/adapters/frame_source.py`（`Frame.model_data` / `Frame.model_info`）、`kit/runtime/preprocess.py`（`letterbox()` / `LetterboxInfo` 几何契约）。契约测试 `kit/adapters/test_official.py`。真机 A/B 原始记录见下方 §4。

## 0. 定位

模型推理前要把相机帧变成模型输入：**等比缩放 + 灰边填充（letterbox）**。默认这步在 Python 里做，1280×720 → 640×640 实测 **38–43 ms/帧**，是整条流水线里仅次于 NPU 推理的开销。

RV1126B 的 **RGA**（2D 图形加速器）能直接对帧代理给出的 dma-buf 做 NV12 缩放 + 色彩转换。把 letterbox 挪到 RGA 上，这 38–43 ms 变成 **0 ms**（Python 侧只剩一次灰边填充的 memcpy）。

app 侧的开启方式是**一行类属性**，不改 manifest、不改帧源、不写任何 RGA 代码。

## 1. 如何调用

在你的 `App` 子类上设 `model_frame`：

```python
from kit.app import App, run_app

class MyApp(App):
    id = "my-app"
    postproc = "detect"
    model_frame = "hw"          # ← 就这一行
```

三个取值：

| 取值 | 行为 | `frame.data` 是什么 | 适用 |
|---|---|---|---|
| `"cpu"`（默认） | 主循环里 Python letterbox | 全分辨率原图 | 兜底，永远正确 |
| `"hw"` | RGA 产出 letterbox 放进 `frame.model_data`，**同时**保留原图 | **全分辨率原图** | **任何视觉 app**，包括推理后要裁原图像素的 |
| `"hw-direct"` | RGA 产出的 letterbox **就是** `frame.data`，连全分辨率 NV12→RGB 转换也省掉 | **模型尺寸的 letterbox 图** | 只消费检测框 / 关键点坐标、**从不读 `frame.data`** 的 app |

### 怎么选

**判据只有一条：推理之后，你的 app 还需要读原始分辨率的像素吗？**

- **不需要**（只用框 / 关键点坐标）→ `"hw-direct"`，省得最多。
- **需要**（裁 ROI 做二级识别、透视裁剪、人脸对齐等）→ `"hw"`。原图仍在 `frame.data` 里，同时也省掉了 Python letterbox。
- 拿不准 → `"hw"`。它对所有视觉 app 都安全；最坏情况只是没拿到 `hw-direct` 那部分额外收益。

> ⚠️ `"hw-direct"` 下 `frame.data` **不再是原始像素**，而是 letterbox 后的模型图。如果 app 还去裁它，裁到的是缩放+带灰边的图 —— 这是唯一会出错的用法，所以判据要照上面走。

仓库内 9 个 app 的实际取值：

| 模式 | app |
|---|---|
| `hw-direct` | `fall-detection`、`retail-vision`、`yolo-detector`、`fitness-trainer` |
| `hw` | `face-analysis`、`facemesh-reader`、`ppocr-reader`（480 输入） |
| 不适用 | `qrcode-reader`、`voice-transcribe`（`needs_model = False`，无模型推理） |

## 2. 坐标契约（三种模式完全一致）

**无论哪种模式，`frame.w` / `frame.h` 始终是原始相机几何**，后处理产出的框 / 关键点坐标也始终在原始像素空间。

- 变换信息统一由 `frame.model_info` 携带，类型是 `kit/runtime/preprocess.py` 的 `LetterboxInfo`（`scale` / `pad_w` / `pad_h` / `orig_w` / `orig_h`），**与 CPU `letterbox()` 产出的同构**。
- 后处理反映射、结果 sink 的归一化（按 `frame.w/h`）两条路径共用同一套代码，不需要判断帧从哪来。

主循环选路（`kit/app.py`）：

```python
info   = getattr(frame, "model_info", None)
padded = getattr(frame, "model_data", None)      # "hw"
if padded is None:
    if info is not None:
        padded = frame.data                       # "hw-direct"
    else:
        padded, info = letterbox(frame.data, self.input_size)   # "cpu"
```

灰边填充值为 **114**（与 CPU 路径一致）。

## 3. 回退与约束

开关只表达**意图**。以下任一情况不满足，都会**自动回退到 CPU letterbox**，几何契约不变，不报错、不中断：

| 前提 | 不满足时 |
|---|---|
| `needs_model = True` | 纯 CPU app 本来就不做 letterbox |
| 帧源是官方帧代理（`/run/recamera/frame.sock`），能给 dma-buf fd | RTSP / snapshot 后端无 fd → 走 CPU |
| librga 可用且导出 `imresize_t` | 旧固件符号缺失 → 走 CPU |
| NV12 几何为偶数（源和目标） | 奇数几何 → 走 CPU |
| Y 平面 offset == 0 | fd-wrap 前提不成立 → 走 CPU |
| 模型输入为正方形（`input_size` × `input_size`） | 非方形输入不适配 |

回退是**三层**的：RGA letterbox 失败 → 全分辨率 RGA 转换 → OpenCV。任一层出错只 latch 关掉该层优化并打日志，不会把 app 带崩。

`model_frame` 写成非法值（如 `"HW"`）会**直接报错**，不静默降级 —— 避免手滑导致白白没有加速。

设备日志里可以确认实际走了哪条：

```
[OfficialFrameSource] preprocess backend: RGA (hardware)
[OfficialFrameSource] preprocess path: RGA direct NV12 resize 1280x720 -> RGB 640x640 + gray pad
[OfficialFrameSource] preprocess path: RGA aux NV12 resize 1280x720 -> RGB 640x640 + gray pad
```
`direct` = `hw-direct`，`aux` = `hw`。出现 `latching to full RGB` / `latching to OpenCV` 即表示已回退。

## 4. 实测数据

### `hw-direct`（真机 A/B，2026-08-13）

设备 `recamera-pro-test`（RV1126B，固件 V1.0.4，内核 6.1.157），1280×720 NV12 帧代理，模型输入 640×640，pose 模型，经官方 appMgr 单活 API 切换，测试期间无第二个相机/NPU 应用。原始记录：`apps/fall-detection/evaluation/pro-rga-preprocess-ab-20260813.md`。

| 路径 | 端到端 WS fps（12.02 s） | appMgr fps | preprocess | RKNN 推理 | 后处理 |
|---|---:|---:|---:|---:|---:|
| **RGA direct** | **18.13** | 17.5–18.9 | **0.0 ms** | 42.0–45.3 ms | 1.9–2.4 ms |
| Python letterbox（回退） | 12.14 | 11.4–12.4 | 38.2–43.1 ms | 35.3–37.9 ms | 1.3–1.6 ms |

**端到端吞吐 +49.3%,每帧去掉约 40 ms 的 Python 预处理。** 该轮无 `direct preprocess failed` / `RGA convert failed` / VPSS FIFO 错误 / 内核 Oops。

坐标契约实测：`Frame.w/h` 保持 `1280x720`，仅 `Frame.data` 为 640×640；`model_info` 为 `scale=0.5, pad_w=0, pad_h=140, orig_w=1280, orig_h=720`；灰边严格 `(114,114,114)`。

### `hw`

`hw` 模式省掉的是 Python letterbox（全分辨率 NV12→RGB 转换本来就已经走 RGA），因此**增益小于 `hw-direct`**。真机 A/B 与结果一致性验证进行中，数据回填本节。

## 5. 自己直接调用 RGA？

`kit/adapters/_rga.py` 的 `RgaNV12ToRGB` 接受的是 **dma-buf fd + NV12 平面信息**（`resize_nv12_to_rgb(fd, width, height, y_stride, y_vstride, dst_width, dst_height)`），不是 numpy 数组。它服务的是"帧代理原始缓冲区 → 模型输入"这一段。

因此 app 在 `on_results` 里对 **numpy 图像**做的二次裁剪 / 缩放（`crop_square_roi`、`perspective_crop`、`fit_rec_input` 等）**用不上这个组件** —— 那些数据已经离开 dma-buf。要给那一段也上硬件加速，需要另开一条 numpy/fd 路径，属于新开发，不在当前实现范围内。

## 6. 一句话

视觉 app 加一行 `model_frame = "hw"`（不读原图像素的用 `"hw-direct"`）即可把每帧 38–43 ms 的 Python letterbox 挪到 RGA 上，`hw-direct` 实测端到端 **+49%**；坐标契约、灰边值、后处理代码在三种模式下完全一致，任何前提不满足都自动回退 CPU，不会出错。
