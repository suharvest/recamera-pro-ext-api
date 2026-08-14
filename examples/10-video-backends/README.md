# 10 — 用第三方视频框架取帧（GStreamer / FFmpeg / OpenCV）

> 演示除了 kit 自带的 `self.frames()`，还能怎么把帧接进来：三种第三方框架各写一个
> `FrameSource` 实现，下游（letterbox → RKNN → 后处理 → 结果输出）**一行不改**。
>
> 事实来源：`kit/adapters/frame_source.py`（kit 默认后端 = ffmpeg 拉 RTSP）、
> `kit/adapters/registry.py`（帧代理在就切零拷贝）、`kit/app.py:App.pre()`（前处理选路）、
> [`docs/guide/hw-preprocess.md`](../../docs/guide/hw-preprocess.md)（RGA 实测数据）、
> [`docs/guide/gstreamer-integration.md`](../../docs/guide/gstreamer-integration.md) /
> [`docs/guide/ffmpeg-integration.md`](../../docs/guide/ffmpeg-integration.md)（另一条路：帧代理 dma-buf 直接喂框架）、
> [`docs/guide/deploy-ops.md`](../../docs/guide/deploy-ops.md) §go2rtc（RTSP 地址）。

```sh
python3 video_backends.py --probe                       # 先看这台设备支持哪些后端
python3 video_backends.py --backend ffmpeg --n 60
python3 video_backends.py --backend gst    --n 60
python3 video_backends.py --backend opencv --n 60
python3 video_backends.py --backend kit --hw-direct --model yolo11n.rknn --n 60
```

## 先说清楚：这是旁路，不是第二路采集

**设备上同一时刻只有一个进程占摄像头**——rkipc 独占 VI，其余全部消费它的输出
（`docs/guide/deploy-ops.md`：rkipc 独占相机 → go2rtc 出流）。所以本示例的三个第三方
后端都拉 **rkipc 已经在推的 RTSP 流**：

| 流 | 地址 | 规格 |
|---|---|---|
| sub | `rtsp://admin:admin@127.0.0.1:5554/live/1` | 640×480 H.265（kit 默认） |
| main | `rtsp://admin:admin@127.0.0.1:5554/live/0` | 4K H.265 |

> 端口是 go2rtc 的 **5554**（本机回环），不是对外的 554。用户名/口令取自
> `kit/adapters/frame_source.py:52` 的默认常量；回环上是否真的校验凭据**需真机核实**。

**不要**在你的进程里另开 `/dev/videoN`、另建 VI/VPSS 通道去"自己采一路"。两路采集同时
跑会撞出 `CSIBDG fifo overflow` → VPSS 错误 → 内核 Oops，只能重启设备。第三方框架在这里
的角色是**解码消费者**，不是采集者。

想绕开编解码一整跳、又要用 GStreamer/FFmpeg 的，正确的路是**帧代理 dma-buf**
（`/run/recamera/frame.sock` → `GstDmaBufAllocator` / `AVDRMFrameDescriptor`），
见 [gstreamer-integration.md](../../docs/guide/gstreamer-integration.md) 与
[ffmpeg-integration.md](../../docs/guide/ffmpeg-integration.md)。本示例讲的是另一件事：
**在没有帧代理、或不想写 dma-buf 桥接时，怎么用现成框架接一路 RTSP 进来**。

## 四条路的代价

| 后端 | 帧从哪来 | 到 numpy 前的拷贝/转换 | RGA 硬件 letterbox | 额外延迟 |
|---|---|---|---|---|
| `kit`（帧代理在） | `frame.sock` dma-buf | 零拷贝 fd；NV12→RGB 由 RGA 做 | **可用**（`model_frame="hw-direct"`） | 无编解码跳 |
| `kit`（帧代理不在） | ffmpeg 拉 RTSP | 解码 + NV12→RGB（ffmpeg 内）、管道、Python read | 不可用 | 编码→网络→解码 |
| `ffmpeg` | 自己起 ffmpeg 拉 RTSP | 同上（这条路和上一行是同一个实现） | 不可用 | 同上 |
| `gst` | `uridecodebin` → `appsink` | 解码 + `videoconvert` 到 RGB、`buf.map()` 后必须 `.copy()` | 不可用 | 同上，另加 appsink 队列 |
| `opencv` | `cv2.VideoCapture(CAP_FFMPEG)` | 同 ffmpeg，**再多一次 BGR→RGB 拷贝** | 不可用 | 同上，VideoCapture 内部队列不可控 |

`docs/guide/adapter-bootstrap.md` 把 RTSP 这一跳记为 **+1~2 帧延迟 + 一次解码**。
kit 的 ffmpeg 后端里测过：640×480 H.265 **软解只占单核约 17%**（4 核之一，
`kit/adapters/frame_source.py` 头注释），所以解码通常不是瓶颈，瓶颈在拷贝和前处理。

### 会丢掉的那一项：RGA 硬件 letterbox

kit 原生路径在帧代理在场时，可以让 RGA 直接把 dma-buf 上的 NV12 缩放+填充成模型输入
（`model_frame = "hw-direct"`）。实测**端到端 +55%**（retail-vision 640：19.10 → 12.31 fps；
另一轮 fall-detection 18.13 → 12.14 fps），赢点是连全分辨率 NV12→RGB 都跳过了——
见 [hw-preprocess.md](../../docs/guide/hw-preprocess.md) §4。

**第三方后端拿不到这条优化。** 触发 RGA 的前提写在 hw-preprocess.md §3 表里：
「帧源是官方帧代理，能给 dma-buf fd；RTSP / snapshot 后端无 fd → 走 CPU」。第三方框架
交给你的是一块已经解码好的 numpy 内存，dma-buf fd 早就不在了，所以只能走 Python
letterbox（1280×720 → 640×640 实测 38–43 ms/帧）。

> 三种后端**不等价**：能用帧代理就用 kit 原生；用第三方框架是拿性能换生态
> （现成的推流/转码/滤镜/多路管理），不是换来更快。

### 什么时候值得用第三方

- **需要框架本身的能力**：转码、录制切片、加水印、推到自己的 RTSP/RTMP/WebRTC 服务、
  多路流合并——这些 kit 的取帧层不做，GStreamer/FFmpeg 现成。
- **已有 pipeline 要复用**：团队里已经有一套 GStreamer 图，只是想在中间插一段推理。
- **跨设备同一套代码**：同一份 `cv2.VideoCapture` 能在 PC 上开发调试，设备上只换 URL。
- **不需要原始分辨率像素、且吞吐敏感** → 反过来，别用第三方，用 kit + `hw-direct`。

## 每种后端在 reCamera Pro 上的前提

`python3 video_backends.py --probe` 会把下面这些当场打出来。

### FFmpeg — 可用（有实测记录）

- 设备自带 `/usr/bin/ffmpeg` **4.4.4**（aarch64，`--enable-libdrm`），`rawvideo` demuxer 可用
  （`docs/guide/ffmpeg-integration.md` §前置条件，RV1126B recamera_v2 实测）。
- **不需要额外安装**。kit 的默认帧源就是这条路，9 个 app 天天在跑。
- 硬件编码不可用：无 `--enable-rkmpp`，`h264_v4l2m2m` 报 `Could not find a valid device`
  （RK 编码器走 `/dev/mpp_service`）。**解码**侧本示例只用软解 H.265，够用。

### OpenCV — 库在，RTSP 能力需真机核实

- `cv2` 在共享基础环境 `/userdata/rknnenv`（`docs/guide/per-app-dependencies.md`：
  rknnlite / numpy / cv2 走共享 base env），**不需要单独装**。
- 但 `VideoCapture(rtsp)` 依赖该 cv2 是否编进了 FFMPEG backend——**本轮未在设备上核实**。
  自查：
  ```sh
  python3 -c "import cv2; print([l for l in cv2.getBuildInformation().splitlines() if 'FFMPEG' in l])"
  ```
  出现 `FFMPEG: YES` 才走得通；否则只能退回 `--backend ffmpeg`（子进程管道，不依赖 cv2 编译选项）。
- RTSP over TCP 只能经环境变量给 libav（示例里设了 `OPENCV_FFMPEG_CAPTURE_OPTIONS`），没有 API 入口。

### GStreamer — 运行时在，RTSP 解码链需真机核实

- GStreamer **1.22.6** + PyGObject（`gi`）在设备上存在，`appsrc` / `GstDmaBufAllocator` /
  `GstVideoMeta` 均已实测（`docs/guide/gstreamer-integration.md` §前置条件）。
- **但那次实测是为 dma-buf 推流方向做的**，清点的 element 是
  `videoconvert / jpegenc / pngenc / filesink / fakesink / rtph264pay / rtspclientsink /
  udpsink / v4l2src|sink`——**清单里没有 `rtspsrc`，也没有任何 H.265/H.264 解码器**。
  本示例要的恰好是这两个，所以 **GStreamer 拉 RTSP 这条路当前状态是「需真机核实」，
  不能假定可用**。核实命令：
  ```sh
  gst-inspect-1.0 rtspsrc uridecodebin avdec_h265 2>&1 | grep -E 'Factory|No such element'
  ```
  缺 `rtspsrc` 或解码器就得额外装 `gst-plugins-good`（rtsp）/ `gst-libav`（avdec_*），
  设备是只读 rootfs 的精简系统，**装插件属于自带依赖的事，不在本示例范围**。
- 同一份固件上**已确认可用**的 GStreamer 用法是另一条：帧代理 dma-buf → `appsrc` → 下游
  （零拷贝，实测 90 帧 3.03 s / 29.8 fps）。要在设备上用 GStreamer，优先走那条。

### kit 原生（对照组）

- 帧代理 `/run/recamera/frame.sock` 在场 → 零拷贝 + RGA；不在场 → 自动回退 ffmpeg RTSP
  （`kit/adapters/registry.py:select_frame_source`）。当前出厂固件（6.1.157）上 socket
  不存在，回退生效；换成含扩展 API 的固件后同一份代码自动切换，app 不改。

## 代码结构（约 380 行，单文件）

| 部分 | 说明 |
|---|---|
| `FfmpegPipeSource` | ffmpeg 子进程 → `rawvideo rgb24` 管道按帧读。等价于 kit 的 `FfmpegRtspSource`，此处重写一遍是为了逐行可读；**生产用 kit 那个** |
| `GstRtspSource` | `uridecodebin ! videoconvert ! video/x-raw,format=RGB ! appsink`，同步 `pull-sample` |
| `OpenCvRtspSource` | `cv2.VideoCapture(url, CAP_FFMPEG)`，BGR→RGB |
| `open_kit_source` | 对照组：`registry.select_frame_source()` |
| `prepare()` / `run_loop()` | **四个后端共用的那段代码**，逐字相同 |
| `probe()` | 探依赖，不连流 |

三个第三方类都继承 `kit.adapters.frame_source.FrameSource` 并 yield `Frame`，这就是接入点：
**只要产出 `Frame(data=RGB uint8 HWC, w, h, fmt="RGB", pts)`，kit 下游全部照常工作**——
`App.pre()` 的选路、结果 sink 的坐标参考系（按 `frame.w/h`）都不需要知道帧从哪来。

想把第三方后端接进一个正式 app（而不是这个独立脚本）：在 `App` 子类上声明
`needs_frames = False`（kit 就不开相机了），在 `run()` 里自己迭代上面的 source，
照常调 `self.pre()` / `self.models.<id>.infer()` / `self.emit()` / `self.tick()`——
`KIT_APP_SHAPE_SPEC.md` §3 的「接管」形态，voice-transcribe 就是这么写的。
代价是 `self.frames()` 替你做的那些（暖机跳灰帧、`--every` 跳帧、帧边界热更、
帧释放、分段计时）要自己负责。

## 已知未验证项

- 三个第三方后端**均未在设备上实跑**（本轮只做静态编写 + 语法自检），fps / CPU 占用
  没有本示例自己的实测数字；表格里的性能数字全部引自 `docs/guide/hw-preprocess.md`
  与 `kit/adapters/frame_source.py`，未新造。
- GStreamer RTSP 解码链、OpenCV 的 FFMPEG backend 是否编入，见上文两处「需真机核实」。
- go2rtc RTSP 在回环上是否校验 `admin:admin`，未核实。
