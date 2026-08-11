# reCamera Pro 扩展 API：方案商开发总入口

> 适用设备：reCamera Pro（RV1126B / recamera_v2）。
> 本文是方案商开发的**汇总入口 + 快速上手 + 两个核心 API（帧代理 / 结果注入）详解**；
> 其余能力各有专篇，见下方能力地图链接。
>
> 事实来源：
> - 规格：`RECAMERA_PRO_API_SPEC.md`（§1 握手/身份/错误码、§2 帧代理、§3 结果注入、§8 架构）
> - C ABI：`sdk/librecamera_ext/include/recamera_ext.h`
> - Python 封装：`sdk/librecamera_ext/python/recamera_ext/__init__.py`
> - proto：`common/vigil/protocol/ext_api.proto`、`common/vigil/protocol/inference.proto`

---

## 1. 概览

reCamera Pro 的固件（rkipc 主程序 + 官方推理 + Web 后端）通过一组**运行时接口**向第三方进程开放。核心前提贯穿全文：

> **这些 API 都是运行时接口。方案商在设备上运行自己的进程即可对接，不改固件源码、不重编固件、不刷自编固件。** 你交付的是一个跑在设备上的可执行程序（C/C++ 二进制或 Python 脚本），通过 unix domain socket 与固件通信。

扩展 API 能做什么：

- **拿帧（帧代理）**：从摄像头零拷贝取到 NV12 帧，喂给你自己的模型/算法。
- **回注结果（结果注入）**：把你算出的检测框送回固件，叠加到 RTSP/录像的 OSD，并分发到 WS/MQTT/HTTP/UART。
- **音频**：从预留的 ALSA PCM 通道取麦克风原始音频。
- **GPIO 触发**：用推理结果驱动引脚（继电器/LED/告警）。
- **前端扩展**：把自己的网页和后端挂到官方 Web 入口下，复用官方登录会话。
- **结果推送**：只要分发、不要叠加时，向 legacy notify 通道注入结果。

### 1.1 能力地图

| 能力 | 状态 | 对接方式 | 文档 |
|---|---|---|---|
| **帧代理**（零拷贝取帧） | 现成可用（M2） | `FrameSource` / C ABI `rc_ext_frame_*`，`/run/recamera/frame.sock` | 本文 §3 |
| **结果注入**（OSD+录像+推送） | 现成可用（M1） | `ResultSink` / C ABI `rc_ext_result_*`，`/run/recamera/result-in.sock` | 本文 §4 |
| **音频 PCM** | 现成可用 | `arecord -D ai_asr`（ALSA dsnoop 共享） | [audio-pcm.md](./audio-pcm.md) |
| **GPIO 结果触发** | 现成可用（组合现有零件） | notify WS + gmgr API，无需固件新功能 | [gpio-result-trigger.md](./gpio-result-trigger.md) |
| **前端扩展挂载** | 现成可用 | `ext_<name>.conf` + `/extension/<name>/`（复用 JWT 会话） | [frontend-extension.md](./frontend-extension.md) |
| **结果推送（notify）** | 现成可用 | 向 `/var/tmp/notify` 写 `InferenceResult`（仅分发，不上 OSD） | [result-push.md](./result-push.md) |
| **rkipc RPC / 配置类** | 走 HTTP API | entry.cgi HTTP API（`/var/tmp/rkipc` 是内部接口，勿直连） | [rkipc-rpc-status.md](./rkipc-rpc-status.md) |
| **观测面（M3）** | 已实现（真机验证） | `probe.sock`：preproc/npu.raw/postproc/metrics 采样 | 见本文 §8 与规格 §4 |
| 控制面（M4）/ 显示（M5）/ 生态（M6）/ 沙箱分发 | 规划中 | — | 见本文 §8 与规格 |

> **帧代理 vs 结果注入 vs notify 的选择**：
> - 要拿摄像头画面自己推理 → **帧代理**（§3）。
> - 要让你的结果出现在视频叠加 + 录像 + 推送 → **结果注入**（§4，`result-in.sock`）。
> - 只要把结果推给外部消费者、不需要叠加/录像 → **notify**（`result-push.md`）。

### 1.2 前置条件（对所有 socket API 通用）

- socket 位于 `/run/recamera/`。**v1 权限模型为 root-only**：目录 `0750 root:root`、socket 文件 `0660`（实测 RV1126B），进程需以 root 运行（麦克风/摄像头/`/dev/mpi` 设备节点均 root 属主，扩展应用经启动脚本以 root 拉起）。
  > 按组隔离的方案（`recamera-ext` 组、socket 0660 组可写）随 P1 沙箱一并落地，v1 未建组——见规格 §1.1 / §6 上机实证。v1 扩展全 root，组隔离无实际意义。
- C 客户端链接 `librecamera_ext.so.1`；Python 客户端 `import recamera_ext`（ctypes 薄封装，运行时加载同一 `.so`）。
- 三条 socket（`frame.sock` / `result-in.sock` / `probe.sock`）连接后都先走一次 **Hello/HelloAck 握手**（§5），SDK 内部自动完成，无需手写 protobuf。

---

## 2. 快速上手（5 分钟跑通）

以下两段代码从 SDK Python 源码逐行核实，可直接复制到设备上运行（需 `pip`/`uv` 装 `numpy`；`to_bgr()` 另需 `opencv`）。

### 2.1 拿帧（5 行）

```python
from recamera_ext import FrameSource

with FrameSource() as src:          # 默认订阅 NPU 同款分辨率/格式（NV12）
    for frame in src:               # frame.array: 零拷贝 Y 平面视图 (height, width)
        infer(frame.array)          # 你的推理；离开本次迭代帧自动释放
```

`frame.array` 是**灰度 Y 平面**的零拷贝视图（形状 `(height, width)`，`uint8`）。多数检测/关键点模型可直接吃灰度或在你侧转换；需要彩色 BGR 时用 `frame.to_bgr()`（返回拷贝，见 §3.3）。

### 2.2 注入结果（5 行）

```python
from recamera_ext import ResultSink

with ResultSink(source_id="face-app") as sink:
    # boxes: (x1, y1, x2, y2, score, label[, class_id]) 像素坐标，score 0..1
    sink.send_detections(pts_us=0, boxes=[(100, 80, 240, 300, 0.92, "person")])
```

`pts_us=0` 表示不与具体帧关联（结果照常叠加/推送）。要与某一帧对齐叠加时，传该帧的 `frame.pts_us`（见 §4）。

### 2.3 拿帧 + 推理 + 回注（组合）

```python
from recamera_ext import FrameSource, ResultSink

with FrameSource() as src, ResultSink(source_id="my-app") as sink:
    for frame in src:
        boxes = my_model(frame.array)          # -> [(x1,y1,x2,y2,score,label), ...]
        sink.send_detections(pts_us=frame.pts_us, boxes=boxes)
```

`frame.pts_us` 与固件内建推理同一时钟源（VI 帧 PTS，`CLOCK_MONOTONIC` 微秒），OSD 侧按 pts 做就近帧匹配。

---

## 3. 帧代理 API（M2 — `frame.sock`）

从摄像头取零拷贝 NV12 帧。服务端在同一 VI pipe 新开一路空闲通道（chn1）+ 私有 DMABUF 池，与内建推理隔离——订阅者行为不影响官方推理帧率（规格 §2.1，门禁 G1 已真机 PASS）。

### 3.1 C ABI（`recamera_ext.h`，v1 冻结）

签名逐一核实自 `sdk/librecamera_ext/include/recamera_ext.h`：

```c
// 打开：连接 /run/recamera/frame.sock，完成握手 + FrameSubscribe。
// cfg=NULL 用 NPU 同款默认；失败返回 NULL 并置 *err 为 rc_ext_err_t。
rc_ext_frame_t *rc_ext_frame_open(const rc_ext_frame_cfg_t *cfg, int *err);

// 读回实际生效的订阅几何（握手后有效）；任意 out 指针可为 NULL。
int rc_ext_frame_geometry(rc_ext_frame_t *h, uint32_t *width, uint32_t *height,
                          uint32_t *fourcc, uint32_t *pool_depth,
                          uint32_t *max_outstanding);

// 等下一帧，最多 timeout_ms。返回:
//   0  -> *out 有一帧（fd 有效，未 map）
//   1  -> 超时无帧（重试）
//  <0  -> -rc_ext_err_t：EOF/传输错误或协议违规，应停止并 close 句柄
int rc_ext_frame_next(rc_ext_frame_t *h, rc_ext_frame_buf_t *out, int timeout_ms);

// mmap(PROT_READ) + DMA_BUF_IOCTL_SYNC(START|READ)，返回 plane[0](Y) 首地址。
void *rc_ext_frame_map(rc_ext_frame_t *h, rc_ext_frame_buf_t *f);

// SYNC(END|READ) + munmap + 回发 8 字节 release seq + close fd。NULL 安全、幂等。
void rc_ext_frame_release(rc_ext_frame_t *h, rc_ext_frame_buf_t *f);

void rc_ext_frame_close(rc_ext_frame_t *h);
```

典型循环（头文件注释给出的用法）：

```c
int err;
rc_ext_frame_t *h = rc_ext_frame_open(NULL, &err);
rc_ext_frame_buf_t f;
int rc;
while ((rc = rc_ext_frame_next(h, &f, 1000)) >= 0) {
    if (rc == 1) continue;                 // 超时，重试
    void *y = rc_ext_frame_map(h, &f);     // 内部做 dma-buf cache 同步
    // 用 f.plane[i].offset/stride/vstride、f.pts_us、f.width/height ...
    rc_ext_frame_release(h, &f);           // 必须：SYNC END + 归还 buffer + close fd
}
rc_ext_frame_close(h);
```

关键结构体（核实自 `recamera_ext.h`）：

```c
typedef struct {
    uint32_t offset;   // 相对 dma-buf 起始的字节偏移
    uint32_t stride;   // 行跨度（字节）
    uint32_t vstride;  // 行数（含对齐补齐）
} rc_ext_plane_t;

typedef struct {
    uint64_t seq;       // 单调递增；出现 gap = 服务端丢过帧
    uint64_t pts_us;    // VI PTS（CLOCK_MONOTONIC 微秒）
    uint32_t width;     // 有效像素
    uint32_t height;
    uint32_t fourcc;    // RC_EXT_FOURCC_NV12
    uint32_t buf_size;  // dma-buf 有效总长
    uint16_t flags;     // bit0: 此帧之前发生过丢帧
    uint8_t  chn_id;
    uint8_t  n_planes;  // NV12 = 2
    rc_ext_plane_t plane[3]; // NV12: plane[0]=Y, plane[1]=UV
    int fd;             // dma-buf fd（借用期间 >=0，释放后 -1）
    void *_base;        // 内部：mmap base
    size_t _map_len;    // 内部：mmap 长度
} rc_ext_frame_buf_t;

typedef struct {         // 可选订阅配置，NULL = NPU 同款默认
    uint32_t width;      // 0 = 默认
    uint32_t height;     // 0 = 默认
    uint32_t fourcc;     // 0 = NV12
    uint32_t fps_divisor;// 0/1 = 每帧, 2 = 隔帧, ...
} rc_ext_frame_cfg_t;
```

常量：`RC_EXT_FRAME_MAGIC = 0x52434652`（"RCFR"）、`RC_EXT_FRAME_VER = 1`、`RC_EXT_FOURCC_NV12 = 0x3231564E`。

### 3.2 Python（`FrameSource` / `Frame`）

核实自 `python/recamera_ext/__init__.py`：

```python
FrameSource(config=None, timeout_ms=1000, lib_path=None)
```

打开后暴露的属性（握手回填）：`src.width`、`src.height`、`src.fourcc`、`src.pool_depth`、`src.max_outstanding`。迭代 `for frame in src:` 产出 `Frame`。可选自定义订阅：

```python
from recamera_ext import FrameSource, FrameConfig
# 隔帧订阅（约半帧率），其余默认
with FrameSource(FrameConfig(fps_divisor=2)) as src:
    print(src.width, src.height, src.pool_depth, src.max_outstanding)
    for frame in src:
        ...
```

> **v1 生效字段**：`fps_divisor` **已实现**——服务端按 divisor 抽帧（`2` = 隔帧交付，实测到达帧率≈半，`seq` 每被采集帧递增、交付的是每第 N 个），`0`/`1` = 每帧。`width`/`height`/`fourcc` 在 v1 **固定为 NPU 同款几何**（`FrameConfig` 里这三项即使传入也被忽略），实际生效值以握手/订阅 ack 回填的 `src.width`/`src.height`/`src.fourcc` 为准。自定义分辨率/格式随后续里程碑开放。

`Frame` 对象字段（全部核实自源码 `Frame.__init__`）：`seq`、`pts_us`、`width`、`height`、`fourcc`、`buf_size`、`flags`、`chn_id`、`n_planes`、`dropped`（= `flags & 1`）、`planes`（`[(offset, stride, vstride), ...]`）。方法：`array`（属性）、`plane_array(i)`、`to_bgr()`。

### 3.3 零拷贝语义与生命周期陷阱（务必读）

以下语义核实自源码 `Frame`（docstring + `array`/`plane_array`/`to_bgr`）与 `FrameSource.__next__`：

- **`frame.array` 是零拷贝视图，不是拷贝。** 它是对底层 dma-buf `mmap` 内存的 numpy 视图（`plane_array(0)` 经 `np.lib.stride_tricks.as_strided` 构造，再裁到 `[:height, :width]`）。读它 = 直接读硬件刚写入的那块内存。
- **只在当前迭代有效。** `FrameSource.__next__` 每次进入先调 `_release_cur()`——把上一帧 `rc_ext_frame_release`：`munmap` + 回发 release seq + close fd，buffer 归还硬件池。因此**上一次迭代拿到的 `array` 在下一次迭代开始后失效**，其内存可能已被后续采集帧覆写。
- **跨帧保留必须 `.copy()`。** 要把某帧留到循环之外（存盘、放进队列、跨线程传、攒 batch），必须 `frame.array.copy()` 得到独立拷贝。直接存 `frame.array` 引用 = 存了个会被覆写的窗口。
- **`frame.to_bgr()` 返回拷贝。** 它把 NV12（裁掉 stride padding）转成连续 BGR 新数组（`cv2.cvtColor`），可安全保留到迭代之外。代价是一次 NV12→BGR 转换 + 一次拷贝。
- **不要按 width/height 自行推导布局。** plane 的 `offset/stride/vstride` 由服务端按 MPI 实际分配填写，`stride ≥ width`、`vstride ≥ height`（含对齐补齐）。必须用 `frame.planes` / `plane[i]` 的真实值，SDK 的 `plane_array` 已按此构造。
- **cache 同步已由 SDK 内置。** `frame.array` 首次访问触发 `_map()` → `rc_ext_frame_map` → `DMA_BUF_IOCTL_SYNC(START|READ)`；release 时 `SYNC(END|READ)`。裸用 fd 自实现协议者必须自己做这对同步，否则 CPU 读到的可能是脏数据。

**何时必须 copy：**

| 用法 | 是否要 copy | 原因 |
|---|---|---|
| 当前迭代内读 `frame.array` 做推理 | 不用 | 视图在本次迭代有效 |
| 把帧存盘 / 编码 / 上传 | 要 `.copy()` 或 `to_bgr()` | 迭代推进后视图失效 |
| 放进队列给另一线程处理 | 要 `.copy()` 或 `to_bgr()` | 消费时视图可能已被覆写 |
| 攒多帧做 batch / 光流 / 帧差 | 每帧都要 `.copy()` | 只有最新一帧的视图有效 |
| `to_bgr()` 的返回值 | 不用（已是拷贝） | `to_bgr` 内部 new 数组 |

### 3.4 线格式（自实现协议者参考）

每帧一条 SEQPACKET 消息：ancillary data 携带**恰好 1 个** dma-buf fd（`SCM_RIGHTS`），payload 为定长 **96 字节头**（C 布局、小端；数据面不用 protobuf）。字段与上面 `rc_ext_frame_buf_t` 一致（wire 头另含 `magic`/`ver`/`reserved`）。接收纪律（规格 §2.3）：

- `recvmsg` 一律带 `MSG_CMSG_CLOEXEC`；
- 收到 `MSG_CTRUNC`、fd 数 ≠ 1、magic/ver 不符、长度 ≠ 96 → **close 全部收到的 fd** + 计协议错误 + 断开重连；
- CPU 访问前后必须 `DMA_BUF_IOCTL_SYNC(START|READ)` / `(END|READ)`；
- 处理完发回 8 字节 `uint64_t seq` release 消息（无 fd）。

背压（规格 §2.4）：发送端 `MSG_DONTWAIT` 永不阻塞；每连接可同时持有的帧数上限 = `max_outstanding`（握手返回，默认 2）；多订阅者上限 4。慢消费者（持续 5 秒 outstanding 满且零 release）被服务端断开（EBACKPRESSURE）。**及时 release 是你的责任**——Python 迭代自动 release，C 侧必须显式 `rc_ext_frame_release`。

---

## 4. 结果注入 API（M1 — `result-in.sock`）

把你算出的检测结果送回 rkipc，走**与内建推理完全相同的三路分发**（规格 §3.3）：

1. **OSD 叠加**：`osd_manager_draw_infer()` 画进 RTSP/预览叠加层，按 `source_id` 哈希分配颜色；
2. **录像**：结果进 vigil 录像队列，回放可见；
3. **推送**：`rc_notify_send_inference()` 转发 WS（本机 `127.0.0.1:8123` / 外部 `/ws/inference/results`）/ MQTT / HTTP / UART。

### 4.1 C ABI（`recamera_ext.h`，v1 冻结）

```c
typedef struct {
    float x1, y1, x2, y2;  // 像素坐标（左上 / 右下）
    float score;           // 置信度 0..1
    const char *label;     // 类名；NULL -> ""
    int class_id;
} rc_ext_box_t;

// 连接 /run/recamera/result-in.sock 并握手。source_id 为建议值（服务端按
// peercred 身份可能改写）；失败返回 NULL 并置 *err。
rc_ext_result_t *rc_ext_result_open(const char *source_id, int *err);

// 发一条 DETECTION 结果，n 个框，pts_us = CLOCK_MONOTONIC 微秒（0 = 不关联帧）。
// 返回 0 成功，负值 = -rc_ext_err_t。
int rc_ext_result_send_detections(rc_ext_result_t *h, uint64_t pts_us,
                                  const rc_ext_box_t *boxes, size_t n);

void rc_ext_result_close(rc_ext_result_t *h);  // NULL 安全
```

### 4.2 Python（`ResultSink`）

核实自 `__init__.py`：

```python
ResultSink(source_id, lib_path=None)
sink.send_detections(pts_us, boxes)   # boxes: (x1,y1,x2,y2,score,label[,class_id])
```

`label` 可为 str（自动 UTF-8 编码，支持中文）或 bytes；`class_id` 可省略（默认 0）。示例见 §2.2 / §2.3。

### 4.3 约束

- **`source_id` 不能用保留字 `"builtin"`**——那是内建推理专用，外部使用被拒（EAUTH，规格 §1.1）。服务端按连接的 peercred 身份校验；无注册条目时会把 source_id 改写为 `"uid:<n>"`。
- **限速**：两级 token bucket——每连接 **60 msg/s**（burst 15）+ 全局 120 msg/s（burst 30）；单条 payload ≤ 64 KB。超限**丢弃 + 计数**（不断连接）。控制你的发送速率不要超过帧率。
- **并发**：≤ 4 个结果注入连接。
- **pts 对齐**：传 `frame.pts_us` 让 OSD 按就近帧匹配（容差 1 帧周期）；传 0 则不关联，照常叠加/推送。
- v1 只提供 `send_detections`（TASK_TYPE_DETECTION）。proto 层 `InferenceResult` 已支持分类/分割/跟踪/关键点（见 `inference.proto` 的 oneof 10-14），但 SDK 便捷封装 v1 只覆盖检测。

### 4.4 结果去向 vs notify 的区别

`result-in.sock`（本 API）= OSD + 录像 + 推送三路；`/var/tmp/notify`（[result-push.md](./result-push.md)）= **只推送、不叠加、不录像**、0666 无鉴权的 legacy 通道。要框出现在画面里就用本 API。

---

## 5. 握手与版本协商（规格 §1.2）

三条 socket 连接后都先做一次 protobuf 握手（定义在 `ext_api.proto`），**SDK 内部自动完成**，方案商通常无需手写；这里说明其语义，便于自适应与自实现协议。

```proto
message Hello {
  uint32 version_min = 1;   // 客户端支持的版本区间（含端点），v1 客户端两者都填 1
  uint32 version_max = 2;
  string client_name = 3;   // 诊断用，如 "face-app"
  string auth = 4;          // peercred 模式忽略；token 模式启用
}
message Capability {
  string name = 1;                 // "frame" / "result" / "probe"
  uint32 version = 2;
  map<string, uint32> limits = 3;  // 如 {"max_msg_rate":60,"max_sources":8,"pool_depth":6}
}
message HelloAck {
  uint32 api_version = 1;          // 服务端在 [version_min, version_max] 内选的最高共同版本
  uint32 server_build = 2;
  string auth_mode = 3;            // v1 = "peercred"
  repeated Capability capabilities = 4;
  int32 error = 5;                 // 0=OK；非 0（EVERSION）时服务端关闭连接
  string error_msg = 6;
}
```

- **协商规则**：服务端在客户端 `[version_min, version_max]` 与自身支持集合的**交集**内取最大值；交集为空 → `error = EVERSION` 并关闭连接（不是 `min(client, server)`）。
- **认证模式**：v1 `auth_mode = "peercred"`（用 `SO_PEERCRED` 取连接 pid/uid/gid 做身份）。将来 app token 作为**新增模式**并行提供，peercred 模式保留，老客户端不断。
- **按 limits 自适应，不要硬编码**：并发数、速率、池深都在 `Capability.limits` 里返回，可能随固件变化。例如结果注入按 `limits["max_msg_rate"]` 控发送速率、帧代理按 `max_outstanding` 控持帧数。Python 侧 `src.pool_depth` / `src.max_outstanding` 即来自握手回填。
- **v1 baseline 承诺**：能力 `frame@1` / `result@1` / `probe@1` 一经发布不可移除——只要 `/run/recamera/` 存在，v1 客户端就能工作。

---

## 6. 错误码表（规格 §1.3，SDK 统一暴露）

C ABI 中 `open` 失败置 `*err` 为正值码，`send`/`next` 失败返回其负值（`-code`）。

| 码 | 名称 | 含义 / 典型场景 |
|---|---|---|
| 0 | OK | 成功 |
| 1 | EVERSION | 版本区间无交集 |
| 2 | EAUTH | source_id 冒充（如用了 `"builtin"`）/ token 无效 |
| 3 | EBUSY | 订阅数达上限 / NPU 通道未启用 |
| 4 | EFORMAT | 请求的格式/分辨率不支持 / 消息解析失败 |
| 5 | EBACKPRESSURE | 慢消费者被服务端断开（帧代理背压，见 §3.4） |
| 6 | ERATELIMIT | 超出消息速率/配额（丢弃计数在 metrics 可见） |
| 7 | EINTERNAL | 服务端内部错误，附 `error_msg` |

Python 侧这些码经 `RuntimeError` 抛出（消息含 `err=` / `rc=`）；帧迭代遇负码返回时 `FrameSource.__next__` 抛 `StopIteration`（EOF/服务端错误，迭代自然结束）。

---

## 7. 其他现成通路

各自有专篇，此处一句话定位 + 链接：

- **音频 PCM** — [audio-pcm.md](./audio-pcm.md)：`arecord -D ai_asr -r 16000 -c 4 -f S16_LE` 从预留的 ALSA `ai_asr` 通道取 4 通道麦克风原始 PCM（软件取 ch0，勿用 `-c 1`），dsnoop 共享、不与 rkipc 音频冲突、无 VQE（AEC/NS 自理）。
- **GPIO 结果触发** — [gpio-result-trigger.md](./gpio-result-trigger.md)：订阅 notify WS 拿结果 + gmgr API 写引脚（`GPIO3_B2`=pin106 / `GPIO3_B3`=pin107），检测到目标即拉高/拉低引脚。仅数字 0/1，无 PWM。不需改固件。
- **前端扩展挂载** — [frontend-extension.md](./frontend-extension.md)：放一个 `ext_<name>.conf` 到 nginx 配置目录，把你的页面/后端挂到 `/extension/<name>/`，复用官方 dashboard 的 JWT 登录会话。
- **结果推送（notify）** — [result-push.md](./result-push.md)：向 `/var/tmp/notify` 写 `<le32 len><InferenceResult>`，分发到 WS/MQTT/HTTP/UART。仅分发、不上 OSD、无鉴权、受全局限速。要叠加/录像请改用本文 §4 的结果注入。
- **rkipc RPC 现状** — [rkipc-rpc-status.md](./rkipc-rpc-status.md)：`/var/tmp/rkipc` 是 rkipc↔entry.cgi 的内部 RPC，不承诺稳定、勿直连；配置类需求走 entry.cgi HTTP API，等 M4 版本化控制面。

---

## 8. 能力现状与路线

| 能力 | 里程碑 | 状态 |
|---|---|---|
| 结果注入（OSD+录像+推送） | M1 | 现成可用 |
| 帧代理（零拷贝取帧 + C ABI） | M2 | 现成可用（塌方级门禁 G1-G4 均真机 PASS，2026-08-11） |
| 音频 PCM / notify / 前端挂载 / rkipc 文档化 | M0 | 现成可用 |
| 观测面（`probe.sock`：preproc/npu.raw/postproc/metrics 采样） | M3 | 已实现（真机验证；复用 M1 socket 骨架） |
| 控制面（`/api/v1/ext/*` 版本化域 + capabilities + app token） | M4 | 规划中 |
| 显示（M5）/ 生态框架接入 gstreamer/ffmpeg（M6）/ 沙箱与打包签名分发 | M5/M6 | 规划中 |

细节以 `RECAMERA_PRO_API_SPEC.md` 为准（§2 帧代理、§3 结果注入、§4 观测面、§8 架构与扩展模型）。

**对方案商的兼容性承诺（规格 §8.2 扩展五规则）**：
1. **加法优先**：新能力 = 新 socket 路径 + 新 Capability + 新 proto 消息，现有端点线格式/路径/语义不动。
2. **数量演进走 limits**：配额变化只改 `Capability.limits` 数值，客户端按握手返回值自适应（不得硬编码）。
3. **结构演进走保留位**：`frame_hdr` 的 `ver` + `reserved` 承载新字段；重排/删字段才升 `ver`，旧 `ver` 至少再支持两个固件版本。
4. **任务类型演进走 oneof 追加**：`InferenceResult` 新任务 = 新 oneof 分支（tag 15+），旧读者跳过未知分支。
5. **只增不减**：`/run/recamera/` 存在期间 v1 baseline 能力与线格式永不移除。

C ABI 交付物 soname `librecamera_ext.so.1`（semver，libabigail 做符号级兼容检查）。
