# RK 硬件编解码：给 GStreamer 补上 `rockchipmpp` 插件

> 事实来源：2026-08-15 在 RV1126B recamera_v2 真机上的交叉编译 + 部署 + 三级验收实测。
> 本文只写实测到的东西；没测的在 §未验证项里逐条列出。

## 定位（先读这段）

出厂镜像的 GStreamer 里**一个视频解码器都没有**（`examples/10-video-backends/README.md`
§GStreamer 的逐个 `gst-inspect-1.0` 结果）。这条结论常被读成"这颗芯片的 GStreamer
用不了硬件编解码"，实际不是：

- **缺的是插件层，不是硬件能力**。底层全在设备上：`librockchip_mpp.so`、`librga.so`、
  `librockit.so`，以及 MPP 的私有设备节点 `/dev/mpp_service`。rkipc 本身就是靠这套出
  H.264/H.265 的。
- 缺的只是把 MPP 包成 GStreamer element 的那一层 —— Rockchip 官方的
  `gstreamer-rockchip`。buildroot 配置里没打开它，所以镜像里没有 `libgstrockchipmpp.so`。
- 补这一层**不改固件、不动分区、不碰 `/oem`**：交叉编译一个 `.so`，连同它依赖的一个
  buildroot 里已有的插件一起放进 `/userdata`，用环境变量指过去即可。

**2026-08-15 已验证：按下面的做法补齐后，硬件 H.265 解码端到端可用，Python 的
`gi` 和 `cv2.CAP_GSTREAMER` 两条路都通。** 编码侧本轮没测，见 §未验证项。

要不要走这条路：

- 只是**拿帧做推理** → 不需要。帧代理 dma-buf 零拷贝更短
  （[gstreamer-integration.md](./gstreamer-integration.md)），根本不经过解码器。
- 要**消费 rkipc 已经在推的 RTSP**、并且想用 GStreamer 生态（转码、录制切片、
  多路合并、推到自己的服务器） → 这条路给你硬解。
- 要**硬件编码回推** → 元件是编出来了，但**没测过，且会和 rkipc 抢 VEPU**，见 §未验证项。

## 编出来的是什么

从 RK 的 `gstreamer-rockchip`（JeffyCN/mirrors 分支）交叉编译，**零源码修改**。

| 项 | 值 |
|---|---|
| 产物 | `libgstrockchipmpp.so` |
| md5 | `78152ef4982d0fef1ae3d44dc4fc3d7e` |
| 大小 | 133040 B |
| 格式 | ELF aarch64 |
| 注册 element | `mppvideodec`、`mpph264enc`、`mpph265enc`、`mppjpegdec`、`mppjpegenc` |

11/11 个 meson 目标一次编过，没有为了绕编译错误改过任何一行上游代码。

## 成功的前提（方案商照做前必须先核对）

这次一次编过、一次跑通，靠的是**编译环境和设备环境严格对齐**。差一项就可能编得出来
但装上去加载失败。先核对这张表，再动手：

| 项 | 本次实际 |
|---|---|
| MPP 源码 + 头文件 | SDK 的 `media/mpp/`，含 `rk_mpi.h` 与 `rockchip_mpp.pc`（v1.3.9） |
| MPP 版本一致性 | 设备上的 MPP 与 SDK 里的**同一个 git hash** `01cc7895`（2026-04-29） |
| GStreamer 开发包 | buildroot sysroot，**1.22.6，与设备完全一致** |
| 工具链 | `aarch64-rockchip1240-linux-gnu-gcc 12.4.0` + buildroot 自带的 meson/ninja |
| RV1126B 支持 | **支持**。MPP 加载时正确识别芯片，只丢了确实不存在的 VP8 编码器 |

两条要点：

- **MPP 必须同 hash。** `libgstrockchipmpp.so` 直接链 `librockchip_mpp.so.1`，编译时的
  头文件和运行时的库版本错开，轻则符号找不到，重则结构体布局对不上跑飞。
- **GStreamer 必须同版本。** 1.22.6 对 1.22.6。用宿主机的 GStreamer 头去编，装到设备上
  会因 ABI 不一致被 registry 拒绝或加载崩。

configure 时关掉了需要 DRM 的部分 —— 设备上**没有 `/dev/dri`**
（`docs/api/spec.md` §M6 已核实项 3 有记录），带上这些只会编不过或编出来用不了：

```
-Drkximage=disabled -Dkmssrc=disabled -Drga=disabled -Dvpxalphadec=disabled
```

关掉 `-Drga` 不影响解码：`mppvideodec` 吐出的是 NV12，后面接 `videoconvert` 即可。
RGA 硬件加速在 kit 里走的是另一条路（[hw-preprocess.md](./hw-preprocess.md)）。

## 还缺一个 `h265parse`，但它不用编

`mppvideodec` 的 sink pad 要求 `alignment=au, parsed=true`，而 `rtph265depay` 单独
协商不出来这两项 —— 中间必须夹一个 `h265parse`。出厂镜像里也没有它。

好消息是**它不用编**：`libgstvideoparsersbad.so` 本来就在同一个 buildroot 里预编好了，
只是没打进镜像。连同它依赖的 `libgstcodecparsers-1.0.so.0` 一起拷到设备上即可。

即最终要放到设备上的是三个文件：

| 文件 | 来源 | 放到 |
|---|---|---|
| `libgstrockchipmpp.so` | 本次交叉编译 | `/userdata/lib/gstreamer-1.0/` |
| `libgstvideoparsersbad.so` | buildroot 预编产物 | `/userdata/lib/gstreamer-1.0/` |
| `libgstcodecparsers-1.0.so.0` | buildroot 预编产物 | `/userdata/lib/` |

## 部署与运行

只写 `/userdata`，不碰 `/oem`（只读 rootfs，且整包 OTA 会覆盖）：

```sh
export GST_PLUGIN_PATH=/userdata/lib/gstreamer-1.0
export LD_LIBRARY_PATH=/userdata/lib:$LD_LIBRARY_PATH   # 必须追加
export GST_REGISTRY=/userdata/gst-registry.bin
```

三个变量各自解决一件事：`GST_PLUGIN_PATH` 让 GStreamer 扫到新插件；`LD_LIBRARY_PATH`
让 `libgstvideoparsersbad.so` 找得到 `libgstcodecparsers-1.0.so.0`；`GST_REGISTRY`
指向一个可写的缓存文件（原因见下一节第 2 条）。

## 三条坑

### 1. `LD_LIBRARY_PATH` 必须追加，不能覆盖

```sh
export LD_LIBRARY_PATH=/userdata/lib                    # ← 错
export LD_LIBRARY_PATH=/userdata/lib:$LD_LIBRARY_PATH   # ← 对
```

直接赋值会冲掉设备默认的 `/oem/usr/lib:/oem/lib`，结果是 `librockchip_mpp.so.1`
找不到 —— 插件本身没问题，但它依赖的 MPP 库在 `/oem` 下，路径被你抹掉了。
症状是 `gst-inspect-1.0 rockchipmpp` 报插件加载失败，很容易误判成"编错了"。

### 2. GStreamer registry 缓存会骗人

库已经放对了、路径也对了，`h265parse` 仍然报 `MISSING`。原因是 GStreamer 把上一次
的扫描结果缓存在 registry 里，不会因为你新拷了 `.so` 就重扫。

```sh
rm -rf ~/.cache/gstreamer-1.0        # 清掉旧缓存
# 或者直接指定一个可写位置
export GST_REGISTRY=/userdata/gst-registry.bin
```

设备上 `$HOME` 未必可写，所以推荐后者。

### 3. 上面两条一起，让本次验证多绕了几轮

两个症状都表现为"element 找不到"，但根因一个在动态链接、一个在缓存，跟插件本身
都没关系。遇到 `MISSING` 时先按这两条排查，再怀疑编译产物。

## 三级验收：实际输出

### L1 — 插件能被识别

```sh
gst-inspect-1.0 rockchipmpp
```

列出 5 个 element：`mppvideodec`、`mpph264enc`、`mpph265enc`、`mppjpegdec`、`mppjpegenc`。

### L2 — 命令行硬解通

```sh
gst-launch-1.0 rtspsrc location=rtsp://127.0.0.1:5554/live/1 protocols=tcp \
  ! rtph265depay ! h265parse ! mppvideodec ! videoconvert \
  ! fakesink num-buffers=30 -e
```

输出 `Got EOS`，`Execution ended after 0:00:05.049738170`，无错误。

### L3 — Python 两条路都通

用共享 base env 的解释器 `/userdata/rknnenv/bin/python3`
（[per-app-dependencies.md](./per-app-dependencies.md)）：

| 路 | 结果 | 耗时 |
|---|---|---|
| `gi` 构管道 → `appsink` 取帧 | `shape=(480,640,3) fmt=BGR` | 1.78 s |
| `cv2.VideoCapture(..., cv2.CAP_GSTREAMER)` | `isOpened: True`，`shape=(480,640,3)` | 1.31 s |

上面两个耗时是各自那次跑通的**脚本总耗时**，不是稳态帧率，别拿去当吞吐用
（吞吐见 §未验证项）。

`CAP_GSTREAMER` 能通这一点值得单独说：设备上的 cv2 4.6.0 **没编 FFMPEG backend**
（`examples/10-video-backends/README.md` §OpenCV），此前 `CAP_GSTREAMER` 也走不通，
断点正是 H.265 解码器缺失。补上插件后，cv2 走 GStreamer 这条路直接可用，
不需要重新交叉编译 OpenCV。

### 帧真实性复核

L3 拿到的帧不是同一张缓冲重复吐出：PTS 严格递增、间隔稳定约 166 ms，10 帧中有
9 帧内容互不相同。

早先观察到的"10 帧均值全相同"是**静止画面被 H.265 编成 skip 块**导致的，
不是解码器吐陈旧缓冲。测这类东西时画面里最好有东西在动，否则容易得出错误结论。

## 未验证项

以下全部**没有实测数据**，写在这里是为了不让读者把上面的结论外推：

- **编码器 `mpph264enc` / `mpph265enc` 完全没测。** 本轮只做了解码。这两个 element
  确实注册进来了（L1 能列出），但"能列出"不等于"能编出正确码流"。另外
  **编码会和 rkipc 抢 VEPU** —— rkipc 常驻在编 H.264/H.265 出 RTSP，再开一路硬编
  是争用同一个硬件编码器，要专门设计测试方案再动，不要直接在跑着业务的设备上试。
- **吞吐上限未测。** 上面 L3 表里的 1.78 s / 1.31 s 是脚本总耗时，不是 fps。硬解相对
  软解能省多少 CPU、能跑到多少帧，本轮没有对照测量。
- **多路并发未测。** 同时开 N 路 `mppvideodec` 会不会撞 VDEC 资源、N 的上限是多少，
  没测。
- **与 NPU 推理同时跑的相互影响未测。** 硬解 + RKNN 推理并行时的互相干扰
  （内存带宽、调度）没有测量。
- **JPEG 元件 `mppjpegdec` / `mppjpegenc` 未测。**

## 相关文档

- [gstreamer-integration.md](./gstreamer-integration.md) —— 另一条 GStreamer 路：
  帧代理 dma-buf 零拷贝喂 `appsrc`。不经过解码器，不受本文限制，拿帧做推理优先走那条。
- [ffmpeg-integration.md](./ffmpeg-integration.md) —— FFmpeg 侧现状（4.4.4，未编 rkmpp）。
- [`examples/10-video-backends/README.md`](../../examples/10-video-backends/README.md)
  —— 出厂镜像上四种取帧后端的实测对比，本文是其中 GStreamer / OpenCV 两条的补救路径。
- `docs/api/spec.md` §M6 —— 生态集成的整体现状与待补项。
