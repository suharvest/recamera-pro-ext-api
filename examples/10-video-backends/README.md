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

> 2026-08-15 真机实测：**出厂固件**上只有 `ffmpeg` 和 `kit` 两条路能跑，`gst` 与 `opencv`
> 因缺 H.265 解码器 / 缺 FFMPEG backend 不可用，详见 [§前提](#每种后端在-recamera-pro-上的前提)。
>
> 同日另一轮实测：给 GStreamer **补上 RK 硬件解码插件后，`gst` 与 `opencv` 两条路都能跑通**
> （`libgstrockchipmpp.so` + `h265parse`，只写 `/userdata`，不改固件）。补救办法见
> [hw-codec-gstreamer.md](../../docs/guide/hw-codec-gstreamer.md)。下面各节写的"不可用"
> 指的是**未做任何补齐的出厂状态**，这个事实不变。

## 先说清楚：这是旁路，不是第二路采集

**设备上同一时刻只有一个进程占摄像头**——rkipc 独占 VI，其余全部消费它的输出
（`docs/guide/deploy-ops.md`：rkipc 独占相机 → go2rtc 出流）。所以本示例的三个第三方
后端都拉 **rkipc 已经在推的 RTSP 流**：

| 流 | 地址 | 规格 |
|---|---|---|
| sub | `rtsp://admin:admin@127.0.0.1:5554/live/1` | 640×480 H.265（kit 默认） |
| main | `rtsp://admin:admin@127.0.0.1:5554/live/0` | 4K H.265 |

> 端口是 **5554**（本机回环），不是对外的 554。用户名/口令取自
> `kit/adapters/frame_source.py:52` 的默认常量。**回环上不校验凭据**（2026-08-15 实测）：
> `rtsp://wrong:wrong@127.0.0.1:5554/live/1` 与不带凭据的 `rtsp://127.0.0.1:5554/live/1`
> 都能 `Sent PLAY request` 并收到 RTP 包，与正确凭据无差别。写 `admin:admin` 是沿用约定，
> 不是访问条件。
>
> **归属订正 + 对外端口同样不校验**（2026-08-15，`ss -ltnp` 实测）：5554 的持有者是
> **rkipc 自带的 `rtsp_demo` 服务器**（`users:(("rkipc",pid=970))`），不是 go2rtc；
> go2rtc（pid 1007）持有的是对外的 **554**，它把 5554 当上游拉进来（`/tmp/go2rtc/go2rtc.yaml`
> 的 `streams.main/sub` 指向 `rtsp://127.0.0.1:5554/live/{0,1}`）。两个端口都不校验凭据：
> - 5554（rkipc rtsp_demo）**根本没有鉴权**，且 **bind `0.0.0.0` 而非仅回环** —— 只要网络可达
>   就能裸拉，属于需要注意的暴露面。
> - 554（go2rtc）运行时配置里**明确写了** `rtsp: {listen: ":554", username: "admin",
>   password: "admin"}`，但 `rtsp://wrong:wrong@127.0.0.1:554/sub` 照样出
>   `Input #0, rtsp` + `Stream #0:0: Video: hevc (Main) 640x480` —— **播放路径不强制该凭据**。
>   注意 go2rtc 的流名是 `main`/`sub`，不是 `/live/N`（`rtsp://…@127.0.0.1:554/live/1` 返回 404）。

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

> 下面的结论来自 2026-08-15 在 RV1126B recamera_v2 真机上的实测，设备未安装任何额外软件包。

### FFmpeg — 可用（已实测）

- 设备自带 `/usr/bin/ffmpeg` **4.4.4**（aarch64，`--enable-libdrm`），`rawvideo` demuxer 可用。
- **不需要额外安装**。kit 的默认帧源就是这条路，9 个 app 天天在跑。
- 2026-08-15 实测拉流：

  ```sh
  ffmpeg -rtsp_transport tcp -i 'rtsp://admin:admin@127.0.0.1:5554/live/1' \
    -an -vframes 30 -f rawvideo -pix_fmt rgb24 /tmp/out.rgb -y
  ```

  输出 `frame= 30 ... Lsize= 27000kB`，落盘 `27648000` 字节 = 30 × 640 × 480 × 3，逐帧对得上。
  `-decoders` 列出软解 `hevc` / `h264`。
- 软解走 swscale，日志有 `No accelerated colorspace conversion found from yuv420p to rgb24`
  ——YUV→RGB 是纯 CPU，符合上文「瓶颈在拷贝和前处理」的判断。
- 硬件编码不可用：无 `--enable-rkmpp`，`h264_v4l2m2m` 报 `Could not find a valid device`
  （RK 编码器走 `/dev/mpp_service`）。**解码**侧本示例只用软解 H.265，够用。

### OpenCV — 出厂固件不可用（已实测，缺 FFMPEG backend）；补插件后 `CAP_GSTREAMER` 可用

- `cv2` **4.6.0** 在共享基础环境 `/userdata/rknnenv`（`docs/guide/per-app-dependencies.md`：
  rknnlite / numpy / cv2 走共享 base env），**不需要单独装**。
- 但这份 cv2 是 buildroot 交叉编译的，`getBuildInformation()` 的 Video I/O 段只有两行：

  ```
  Video I/O:
    GStreamer:                   YES (1.22.6)
    v4l/v4l2:                    YES (linux/videodev2.h)
  ```

  **没有 `FFMPEG:` 这一行**（全文 grep `FFMPEG` 返回空列表），即 FFMPEG backend 未编入。
- 三种 backend 实测全部 `isOpened() == False`：
  - `CAP_FFMPEG` — 直接返回 False，无报错（backend 不存在）。
  - `CAP_GSTREAMER` / `CAP_ANY` — 报
    `GStreamer warning: your GStreamer installation is missing a required plugin` →
    `module uridecodebin0 reported: Your GStreamer installation is missing a plug-in.` →
    `unable to start pipeline`。根因与下一节的 GStreamer 相同：**没有 H.265 解码器**。
  - 注：`cv2.videoio_registry.getStreamBackends()` 里能看到 `FFMPEG` 字样，那是注册表里的
    枚举名，不代表编进来了。**别拿它当依据**。
- 结论：`--backend opencv` 在**未补齐的出厂固件**上走不通，只能退回 `--backend ffmpeg`
  （子进程管道，不依赖 cv2 编译选项）。
- 方案商要用 cv2 取流，两条路：重新交叉编译 OpenCV 并 `-DWITH_FFMPEG=ON`，
  或给 GStreamer 补上 H.265 解码器。**后者 2026-08-15 已实测走通**：
  补上 `libgstrockchipmpp.so` + `h265parse` 后，这份**未重编**的 cv2 4.6.0 用
  `cv2.VideoCapture(..., cv2.CAP_GSTREAMER)` 拿到 `isOpened: True`、`shape=(480,640,3)`。
  即 `Video I/O` 里那行 `GStreamer: YES (1.22.6)` 是真的能用的，缺的只是它下面的解码器。
  做法见 [hw-codec-gstreamer.md](../../docs/guide/hw-codec-gstreamer.md)。

### GStreamer — 出厂固件不可用（`rtspsrc` 在，但一个视频解码器都没有）；补插件后硬解可用

- GStreamer **1.22.6** + PyGObject（`gi`）在设备上存在，`appsrc` / `GstDmaBufAllocator` /
  `GstVideoMeta` 均已实测（`docs/guide/gstreamer-integration.md` §前置条件）。
- 2026-08-15 逐个 `gst-inspect-1.0` 的结果，比之前预估的好一半、坏一半：

  | element | 状态 | 出处 |
  |---|---|---|
  | `rtspsrc` / `rtpdec` | **OK** | `libgstrtsp.so` |
  | `rtph265depay` / `rtph264depay` | **OK** | `libgstrtp.so` |
  | `uridecodebin` / `decodebin` | **OK** | `libgstplayback.so` |
  | `videoconvert` / `appsink` / `fakesink` | **OK** | — |
  | `h265parse` | **MISSING** | 无 `gst-plugins-bad` videoparsers |
  | `avdec_h265` / `avdec_h264` | **MISSING** | **无 `libgstlibav.so`** |
  | `mppvideodec` / `rkvdec` / `v4l2h265dec` | **MISSING** | 无 Rockchip MPP 插件 |

  `/usr/lib/gstreamer-1.0/` 共 36 个插件 / 366 个 feature，**里面没有任何视频解码器**。
- 所以 RTSP 传输这一段是通的，断在解码。两条管道的实测输出：
  - README 原来给的那条 `rtspsrc ! rtph265depay ! h265parse ! avdec_h265 ! …` —— 连建都建不起来：
    `WARNING: erroneous pipeline: no element "h265parse"`。
  - `rtspsrc ! fakesink` —— **通**。协商出
    `application/x-rtp, media=video, encoding-name=H265`（另有一路 PCMA 音频），
    收满 30 个 RTP 包后 `Got EOS`，耗时 0.83 s。**证明 RTSP 拉流本身没问题**。
  - `uridecodebin ! videoconvert ! video/x-raw,format=RGB ! fakesink` —— 断在解码：
    `Missing decoder: H.265 (video/x-h265, stream-format=hvc1, alignment=au, …)`
    （顺带 `Missing decoder: A-Law`）。
- 复核命令（比 README 早先给的那条更准，逐个查而不是一次传多个参数）：

  ```sh
  for e in rtspsrc uridecodebin h265parse avdec_h265 mppvideodec v4l2h265dec; do
    printf '%-14s ' "$e"; gst-inspect-1.0 $e >/dev/null 2>&1 && echo OK || echo MISSING
  done
  ```

- 方案商要用 `--backend gst`，得自己解决**解码器**这一件事，三选一：
  `gst-libav`（软解，最省事）、`gst-plugins-bad` 的 `h265parse` + 某个解码器、
  或 Rockchip 的 `gstreamer-rockchip`（`mppvideodec`，硬解，要配套 MPP 库）。
  设备是只读 rootfs 的精简系统，**装插件属于自带依赖的事，不在本示例范围**。
- **第三条路 2026-08-15 已实测走通**，写成了专篇
  [hw-codec-gstreamer.md](../../docs/guide/hw-codec-gstreamer.md)：从
  `gstreamer-rockchip` 零源码修改交叉编译出 `libgstrockchipmpp.so`（含 `mppvideodec`），
  `h265parse` 直接取 buildroot 里已预编好、只是没打进镜像的 `libgstvideoparsersbad.so`，
  两者都放 `/userdata`，不改固件。管道
  `rtspsrc ! rtph265depay ! h265parse ! mppvideodec ! videoconvert ! fakesink num-buffers=30`
  实测 `Got EOS`，Python `gi` 与 `cv2.CAP_GSTREAMER` 均取到 `(480,640,3)`。
  **编码器（`mpph264enc`/`mpph265enc`）虽也编出来了但未测**，且会与 rkipc 抢 VEPU。
  上面这张 element 表描述的仍是**出厂状态**，不受此影响。
- 同一份固件上**已确认可用**的 GStreamer 用法是另一条：帧代理 dma-buf → `appsrc` → 下游
  （零拷贝，实测 90 帧 3.03 s / 29.8 fps）。那条路不经过解码器，所以不受本节限制。
  **要在设备上用 GStreamer，走那条。**

### kit 原生（对照组）

- 帧代理 `/run/recamera/frame.sock` 在场 → 零拷贝 + RGA；不在场 → 自动回退 ffmpeg RTSP
  （`kit/adapters/registry.py:select_frame_source`）。
- 2026-08-15 实测该设备上 socket **存在**（`srw-rw---- root root /run/recamera/frame.sock`），
  `--probe` 报「帧代理在（零拷贝 + RGA 可用）」。即这台设备已刷含扩展 API 的固件，
  走的是零拷贝路径而非 ffmpeg 回退。出厂固件（6.1.157）上 socket 不存在、回退生效，
  同一份代码两种固件都不用改。

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

2026-08-15 真机核实后的剩余项：

- **`--backend gst` / `--backend opencv` 在未补齐的出厂固件上跑不了**，原因见上文
  （缺 H.265 解码器 / 缺 FFMPEG backend）。补上 `libgstrockchipmpp.so` + `h265parse` 后
  两条路都能取到帧（[hw-codec-gstreamer.md](../../docs/guide/hw-codec-gstreamer.md)），
  但那一轮只验证了"能解出正确尺寸的帧"，**这两条路的 fps、CPU 占用仍然没有实测数字**，
  也没有跑本示例的 `video_backends.py` 本身。
- `--backend ffmpeg` 的取流已实测（30 帧逐字节对上），但**没有跑通完整的
  取帧 → letterbox → RKNN → 后处理链路**，所以本示例仍未产出自己的端到端 fps
  与 pre/infer/post 分段耗时。上文表格里的性能数字全部引自
  `docs/guide/hw-preprocess.md` 与 `kit/adapters/frame_source.py`，未新造。
- `--backend kit` 未实跑（本轮不动摄像头、不切换正在运行的 app）。

已核实、不再是未知项的：GStreamer RTSP 解码链（出厂不可用，补插件后硬解可用）、
OpenCV 的 FFMPEG backend（未编入，但补插件后 `CAP_GSTREAMER` 可用）、
go2rtc 回环是否校验凭据（不校验）。
