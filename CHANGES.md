# reCamera Pro 扩展 API 改动说明 / 交付报告（CHANGES.md）

> **读者**：接手这套代码的 Seeed 工程师。
> **代码基**：`recamera_v2` manifest / RV1126B，改动位于 `RV1126B_Linux_IPC_SDK/project/app/recamera_ipc`（下称 `$IPC`），在 wsl2-local。
> **规格依据**：`RECAMERA_PRO_API_SPEC.md` v0.4（M0-M6 + 门禁 + 修订记录）；上游需求分析 `RECAMERA_PRO_INFERENCE_SDK_DESIGN.md`；施工图 `IMPLEMENTATION_PLAN_M1.md`（含 3a/3b + 真机结果）。
> **状态**：源码全部为工作区未提交改动（`git status` 不在任何分支上）。本文按 `git diff` 与源码逐项核实，不含推断。

---

## 1. 概述

在**闭源固件**（rkipc / recamera_ipc）里加一层**扩展 API**：方案商不改固件源码、不重编固件，就能通过 `/run/recamera/` 下的 Unix domain socket 拿相机帧（零拷贝 dma-buf）、把自己的推理结果回注到官方 OSD/录像/WS 分发、以及观测内建推理流水线各级张量。

**一句话价值**：把"必须 fork 固件才能扩展"变成"跑自己的进程接 socket 就能扩展"，进程边界即契约。

三条 socket 端点（对应三个里程碑）：

| 端点 | socket | 里程碑 | 本轮状态 |
|---|---|---|---|
| 结果回注 | `/run/recamera/result-in.sock` | M1 | 代码在，真机验过（3a+3b）|
| 帧代理 | `/run/recamera/frame.sock` | M2 | 代码在，真机验过（2a 零拷贝）|
| 观测面 | `/run/recamera/probe.sock` | M3 | 代码在，preproc/npu/metrics tap 已接 |

---

## 2. 改动清单（按模块）

`git diff --stat`：**15 个已跟踪文件修改**（+342 / −110），另有 **untracked 新增目录/文件**（`rc_ext_core/` 11 个、`rc_probe/` 2 个、`rc_notify/rc_result_*` 6 个、`frame_export.{c,h}` 2 个、`ext_api.proto` 1 个、`sdk/` 一整套）。

### 2.1 proto 扩展

| 文件 | 改动 |
|---|---|
| `protobufs/inference.proto:117-118` | `InferenceResult` 新增 `string source_id = 4`（"builtin" 保留给内建）+ `uint64 pts_us = 5`（CLOCK_MONOTONIC 微秒，0=无帧关联）。tag 1/2/3 + oneof 10-14 不动，向后兼容 |
| `protobufs/ext_api.proto`（新增，92 行）| Hello / HelloAck / Capability / FrameSubscribe / ProbeSubscribe / ProbeData 等握手与订阅消息（spec §1.2/§2.2/§4.2）|
| `protobufs/gen/c/inference.pb-c.{c,h}`、`common/rc_infer/src/utils/inference.pb-c.{c,h}`、`common/rc_notify/inference.pb-c.{c,h}` | 三份 C（protobuf-c）生成码同步重生成，带上 source_id/pts_us |
| `protobufs/gen/python/inference_pb2.py` | Python 生成码重生成（含 source_id/pts_us；顺带把 keypoints 的 `_globals[...]` 形式改为新版 protobuf runtime 的裸符号形式，属 protoc 版本升级产物）|
| `common/rc_ext_core/ext_api.pb-c.{c,h}`（新增，1294+525 行）| `ext_api.proto` 的 C 生成码 |

> **keypoints pb2/parser 修复**：notify-server 侧的关键点解析修复留档在 `_verify_artifacts/src_inference_parser.py` 与 `src_notify_pb2.py`（notify-server 不在本 git 树内，需按 M0 文档同步过去核实）。in-tree 的 OSD keypoints 渲染路径见 `common/osd/osd_manager.c:1333`（TASK_TYPE_KEYPOINTS + pose schema 线程本地深拷贝，未在本轮改动）。

### 2.2 核心库 `common/rc_ext_core/`（新增，先行）

服务端传输层，三端点共用（spec §8.1"一个核心库 N 个薄端点"）：

| 文件 | 行 | 职责 |
|---|---|---|
| `ext_socket.{c,h}` | 253/84 | SOCK_SEQPACKET 服务端骨架：bind `/run/recamera/*.sock`、accept、事件循环、每连接结构体、SCM_RIGHTS fd 收发 |
| `ext_handshake.{c,h}` | 123/45 | Hello→HelloAck；版本**区间交集协商**（非 min）；返回 Capability + limits |
| `ext_identity.{c,h}` | 89/39 | `SO_PEERCRED` 取 pid/uid；`apps.d/<name>.conf` 查表；`"builtin"` 保留字拒绝（`rc_ext_source_is_reserved`）|
| `ext_ratelimit.{c,h}` | 85/47 | 两级 token bucket（每连接 + 全局）|
| `rc_ext_errno.h` | 17 | 统一错误码（spec §1.3：EVERSION/EAUTH/EBUSY/EFORMAT/EBACKPRESSURE/ERATELIMIT/EINTERNAL）|

### 2.3 端点：结果回注 + 分发 + OSD 合成 `common/rc_notify/`（新增）

| 文件 | 行 | 职责 |
|---|---|---|
| `rc_result_dispatch.{c,h}` | 20/21 | 从 `video.c` 抽出的三路扇出：`vg_inference_enqueue_protobuf`（录像）+ `rc_notify_send_inference`（WS/MQTT/HTTP/UART）+ `rc_result_osd_composite`（OSD）。内建与入站共用同一条（spec §3.3"官方推理吃自己的狗粮"）|
| `rc_result_in.{c,h}` | 148/23 | `result-in.sock` 入站端点（rc_ext_core 薄层）：握手→peercred 定 source_id（外部禁用 "builtin"）→校验是合法 InferenceResult→限速→`rc_result_dispatch` |
| `rc_result_osd.{c,h}` | 201/22 | 单 canvas OSD 合成器：把每个 live source 的最新检测集并进一个 overlay 渲染（spec §3.3 单 canvas 方案，max_sources=8 软上限）|

### 2.4 端点：帧代理 `src/rv1126b_ipc/video/frame_export.{c,h}`（新增，529+64 行）

M2 零拷贝帧扇出（spec §2）：rkipc 进程内一个专用线程，开私有 VI 通道 **pipe 0 / chn 1**（内建流水线空闲，G1 验证）+ 私有 DMABUF 池；仅在有订阅者时取帧。每帧一条 SEQPACKET：96 字节 `frame_hdr`（magic "RCFR"、seq、pts_us、plane descriptor）+ 恰好 1 个 dma-buf fd（SCM_RIGHTS）。所有权按 spec §2.4（单次 MPI Get/Release + subscribers_left 计数，无防御性拷贝，G2 验证）。

### 2.5 端点：观测面 `common/rc_probe/`（新增）+ rc_infer tap 点

| 文件 | 行 | 职责 |
|---|---|---|
| `common/rc_probe/rc_probe.{c,h}` | 695/104 | `probe.sock` 端点 + tap API：`rc_probe_frame_begin/_stage_active/_emit/_metrics_stage`。独立低优先级 worker + 有界队列，无订阅时 tap 为一次原子读（spec §4.1）|

rc_infer 流水线插 tap（编译开关 `RC_INFER_ENABLE_PROBE`）：

| 文件 | 位置 | tap |
|---|---|---|
| `common/rc_infer/src/rc_infer.cpp:1545+` | 预处理后喂 NPU 前 | `preproc.out`（RGB888 张量，可还原图）|
| `common/rc_infer/src/rc_infer.cpp:1638+` | rknn_run 后 | `npu.raw`（各输出层 INT8 + shape/scale/zero_point）|
| `common/rc_infer/src/rc_infer.cpp:1687+` | 后处理后 | `metrics`（各级 p50/p99 耗时 + fps）|
| `common/rc_infer/src/utils/serialization.cpp:606` | pack 完成后 | `postproc.out`（打包好的 InferenceResult）|

### 2.6 entry.cgi ext 域（M4）

规格 §5.1 定义 `/api/v1/ext/capabilities` 与 `/api/v1/ext/subscriptions`，实现锚点在 `recamera_web_backend/rest_api.cpp` + 新增 `ext_api.{h,cpp}`。**本 git 树（recamera_ipc）内未见 web_backend 改动**——控制面属另一仓库，本轮未落地（见 §7）。文档侧见 `docs/ext/control-api.md`。

### 2.7 SDK `sdk/librecamera_ext/`（新增，in-tree）

| 文件 | 行 | 职责 |
|---|---|---|
| `include/recamera_ext.h` | 222 | C ABI（v1 冻结对象）：`rc_ext_result_open/send_detections/close`、frame 侧接口 |
| `recamera_ext.c` | 449 | 连 socket + 握手 + inference.pb-c 组包，方案商不碰 protobuf |
| `frame_recv.c` | 358 | 帧接收：SCM_RIGHTS fd 收取 + DMA_BUF_IOCTL_SYNC cache 同步 + release |
| `python/recamera_ext/__init__.py` | 530 | ctypes 薄封装：`ResultSink` / `FrameSource` |
| `CMakeLists.txt` | — | soname `librecamera_ext.so.1`（当前 build 产物 `librecamera_ext.so.1.0.0`）|

### 2.8 bug 修复

| 文件 | 改动 | 原因 |
|---|---|---|
| `common/osd/osd_manager.c:223` | INFER overlay layer **3→1**（主流）| RV1126B VENC 不合成该通道第 4（最高）OSD layer，layer 3 的 region 即便 Create/Attach/SetBitMap 全成功也进不了编码/RTSP 流；layer 0-2 可合成（datetime 用 layer 2 可见）|
| `common/osd/osd_manager.c:287` | INFER overlay layer **7→5**（子流）| 同上，子流块 4-7 的最高层 7 不合成，改到 layer 5（SN slot 默认不用）|
| `common/osd/osd_manager.c:1766` | `osd_manager_load_cfg` 缺 `osd:cfg` 时**降级为内置默认**（inference overlay on），不再返回 `-ENOENT` | 原逻辑缺 cfg 会让 `osd_manager_init()` 失败并拆掉全部 OSD |
| `common/rc_infer/src/rc_infer.cpp:1575` | letterbox padding **114→0x727272** | 对齐灰边填充值 |
| `protobufs/gen/python/inference_pb2.py` | keypoints globals 形式随 protoc 版本重生成 | 见 §2.1 备注；parser 侧修复留档 `_verify_artifacts/` |

### 2.9 main.c 接线 + 构建

| 文件 | 改动 |
|---|---|
| `src/rv1126b_ipc/main.c:19-21,414-416` | include 三个端点头；rkipc init 末尾起 `rc_result_in_start()` / `frame_export_start()` / `rc_probe_start()` |
| `src/rv1126b_ipc/video/video.c:653-655` | 三行内建分发替换为 `rc_result_dispatch(model_id, sr.data, sr.size)`；转换函数多传 `stViFrame.stVFrame.u64PTS` 作 pts_us |
| `common/rc_infer/src/utils/serialization.{h,cpp}` | 转换函数签名加 `uint64_t pts_us`；内建路径补填 `source_id="builtin"` + `pts_us` |
| `src/rv1126b_ipc/CMakeLists.txt` | `aux_source_directory` 收录 `rc_ext_core`/`rc_probe`；加 include 路径；`add_definitions(-DRC_INFER_ENABLE_PROBE)` |

> **stray 文件**：`common/rc_infer/src/rc_infer.cpp.bak_letterbox` 是 letterbox 改动的备份，提交前应删除（见 §7）。

---

## 3. 为什么这么做（关键设计决策）

- **进程边界即契约，不 fork 固件**（设计文档 6.1）：方案商跑自己的进程，经 socket 接入；官方固件不为每个方案改代码。
- **单核心库 + N 个薄端点**（spec §8.1）：所有权/背压/限速这类最难写对的代码只存在一份（`rc_ext_core`），端点层（result-in / frame_export / rc_probe）只含媒体特有逻辑。新增媒体类型 = 新薄端点 + 新 Capability，核心零改动。开发顺序上 `rc_ext_core` 先行，result-in 是它第一个消费者，帧代理第二个——所有权状态机在 M1 阶段就被真实路径覆盖。
- **dma-buf 零拷贝**（spec §2.3）：帧数据面走 SCM_RIGHTS 传 fd + 96 字节定长头，不走 protobuf，免序列化开销；plane descriptor 由服务端按 MPI 实际分配填写，客户端禁止自行按 w/h 推导。
- **单次持帧所有权模型**（spec §2.4）：MPI 每帧只 Get/Release 一次，多订阅者用 `subscribers_left` 计数归零后释放——不假设 MPI 有增引用接口；发送路径原子预留 + 失败回滚。
- **版本区间协商**（spec §1.2）：服务端在客户端 `[version_min, version_max]` 与自身支持集交集取最大，交集为空 → EVERSION 关闭（不用 min）；v1 baseline 能力一经发布不可移除。
- **身份用 SO_PEERCRED**（spec §1.1）：v1 即防 source_id 冒充（"builtin" 保留字拒绝）；auth_mode 协商为将来 app token 留位，不断老客户端。

**评审结论**：spec 经两轮 codex 评审——v0.1 REDESIGN-NEEDED（7 条）→ v0.2 SHIP-WITH-FIXES → v0.3 吸收全部剩余项 → v0.4 增补架构与扩展模型。上述模型（96B 头 + `_Static_assert`、connection generation + outstanding 集合、三条背压铁律、区间协商、结构化 Capability）均为评审发现的直接产物（spec 文末修订记录）。

---

## 4. 真机验证结论

两台设备验过（引自 spec / IMPLEMENTATION_PLAN_M1 已记录结果）：

### M1 结果回注（3b，设备 192.168.42.1，固件 V1.0.10）

方式：运行 `/userdata/rkipc.3b`（不改 `/oem`，保持原厂），经 result-in.sock + notify WS:8123 本地验证。全通过：

| DoD | 结果 | 证据 |
|---|---|---|
| 端点启动 | PASS | 日志 `listening on /run/recamera/result-in.sock (SEQPACKET; handshake + peercred + ratelimit)` |
| 握手协商 | PASS | Hello v[1,1] → `api_version=1, auth_mode="peercred", error=0` |
| 版本不兼容拒绝 | PASS | Hello v[2,2] → `error=1`(EVERSION) |
| builtin 冒充拒绝 | PASS | 日志 `reject: source_id=builtin from uid:0` |
| 限速令牌桶 | PASS | 发 80 条 → WS 收 28 条（60/s+burst15 下合理）|
| 注入→dispatch→notify→WS 端到端 | PASS | WS 收到注入 detection |
| SDK C ABI 端到端 | PASS | `rc_ext_result_open`→`send_detections`(rc=0)→`close`，WS 收到 `SDK-3B-OK` |
| 主体不回归 | PASS | 推理/ISP 正常，pid 稳定 |

设备已回滚原厂（`ef84f99`，dmesg 无崩溃）。

### M2 帧代理（2a，设备记录见 `m2_scratch/`）

零拷贝帧代理实测：`frame_export` 从 chn1 取帧，SCM_RIGHTS 传 fd，客户端拿到正确画面。证据（本机）：

- `m2_scratch/frame_00.png` / `frame_01.png` / `frame_first.png`（导出帧还原图）
- `m2_scratch/rkipc.m2` + `rkipc.m2.log`（带帧代理的二进制 + 运行日志）
- `m2_scratch/dev/mal_noread.py` / `mal_norelease.py` / `sub_hold.py`（恶意压测脚本：不读 socket / 收帧不 release / 持帧回绕）
- `m2_scratch/dev/gst_dmabuf_test.py` / `ffmpeg_feed_test.py`（M6 生态桥接：GStreamer appsrc dma-buf 零拷贝、FFmpeg drm_prime 实测）

### M3 观测面

- `_verify_artifacts/preproc_out_AFTER.png`、`preproc_out.rgb` + `.meta`（preproc.out tap 还原图 + 张量元数据）
- `_verify_artifacts/preproc_letterbox_BEFORE_AFTER.png`（letterbox 114→0x727272 前后对照）

---

## 5. 门禁结论（G1-G4 真机 PASS，2026-08-11）

| # | 假设 | 结论对设计的影响 |
|---|---|---|
| **G1** | VI 同 pipe 多 chn 隔离 | PASS：独立进程 `SetChnAttr(0,1)`/`EnableChn(0,1)` ret=0，连取 45/45 帧，rkipc ISP 无系统性掉帧，dmesg 零 VPSS 崩溃。**"帧代理必须编进 rkipc 进程内"的硬约束解除**——外部进程可新开自己的 chn1（但取不了 rkipc 的 chn4）|
| **G2** | dma-buf 持有期硬件不复用 | PASS：持有一帧 fd 不 Release，强制池回绕 39 帧（13×池深），被持帧内容不变、CRC 一致。**单次持帧 + 计数模型成立，无需服务端持帧兜底拷贝** |
| **G3** | u64PTS 单调且 VI/VENC 同源 | PASS：u64PTS = CLOCK_MONOTONIC 微秒，40 帧单调、漂移 <0.5ms、全链路同源。**主方案成立，自打钟退路无需启用**（`pts_us` 直接透传 `stVFrame.u64PTS`）|
| **G4** | RGN 支持 ≥2 外部 source 叠加 | PASS：RGN handle 每进程私有（池 128），每通道 attach 上限 8、内建占 1。**优先单 canvas 合成**，`max_sources=8` 软上限（`rc_result_osd.c` 即此实现）|

G5（恶意负载相机线程零阻塞）/ G6（池深最坏情况）/ G7（SDK 发布形态）为核实级，阻发布不阻开工。

---

## 6. ABI 适配性

自编 rkipc + entry.cgi 在不同固件 build 上跨 build 兼容的实证：

- **M1 3b 验证在 V1.0.10**：`/userdata/rkipc.3b` 直接运行（不改 `/oem` squashfs），握手/身份/限速/端到端全通，主体不回归。
- **门禁验证载体（spec §7 记录）**：原厂 V1.0.10 即可获 root（`custom_shadow` 开机注入、`telnetd -F`/`adbd` 以 root 跑），`/userdata` ext4 rw 无 noexec，可放置并执行交叉编译二进制。G1-G4 无需先刷自编固件。
- **旧设备 V1.0.10 + 新设备 V1.0.4**：spec 记录两固件 build 均可承载自编 rkipc 与门禁验证——自编 rkipc + rkipc 生态在两个 build 上跨 build 可运行。
- 热替换（bind mount 覆盖 `/oem/usr/bin/rkipc` 单文件）可即时回滚，不动 bootloader/分区。

---

## 7. 已知问题 / 待办

- **M2 EBACKPRESSURE**：慢消费者断开路径已补（spec §2.4 五秒挂死判定 + 断线清理）。
- **M4 subscriptions**：`/api/v1/ext/subscriptions` 诊断视图待 rkipc 暴露端点内部状态；`/api/v1/ext/capabilities` 与 web_backend `ext_api.cpp` 未在本 git 树落地（属另一仓库）。
- **M5 DSI 显示**：选定方案 A（整屏出租 + 帧代理自绘），依赖 M2；待接屏（RPi 7寸 DSI）实测 VOP 分层 / `/dev/dri/card0` 权限 / enable_vo=0 让屏。
- **沙箱 / 打包分发**：P1，调研中。v1 全 root 环境下扩展间无强隔离（spec §1.1 上机实证：扩展经 appmgr 以 root 启动，peercred 在 v1 只防手滑不防恶意 root）。
- **OSD 可见性收口**：layer 3/7→1/5 已修（§2.8），随修复窗口在真机复验后收口。
- **源码未 commit**：全部改动为工作区未跟踪状态。建议按模块拆 commit：
  1. proto 扩展（inference.proto + 4 份生成码 + ext_api.proto + ext_api.pb-c）
  2. `rc_ext_core/` 核心库
  3. `rc_result_dispatch` 抽取 + `video.c` 接线（纯重构，先单独验内建不回归）
  4. `rc_result_in` + `rc_result_osd` 入站端点
  5. `frame_export` 帧代理
  6. `rc_probe` + rc_infer tap 点
  7. `sdk/librecamera_ext/`
  8. bug 修复（osd layer / osd cfg 降级 / letterbox）—— **建议先删 `rc_infer.cpp.bak_letterbox` 与 `sdk/librecamera_ext/build/`（编译产物不入库）**
  9. main.c 接线 + CMakeLists

---

## 8. 怎么继续

**编译**（在 wsl2-local，人在 Mac 用 fleet 跨机；见 `recamera-rk-build` skill）：

```
# 唯一合法入口
./build.sh app          # 编 rkipc（~3min）；产物 find output -name rkipc -type f
# CMake 已收录 rc_ext_core / rc_probe（aux_source_directory）；新增 .c 记得确认被 glob 到
```

SDK 单独编：`sdk/librecamera_ext/`（CMake，产 `librecamera_ext.so.1`）。

**部署验证（热替换优先，不烧固件）**：

1. 编新 rkipc → `find output -name rkipc`
2. 上设备（V4：`rkipc -a /oem/usr/share/iqfiles`，二进制约 `/oem/usr/bin/rkipc`）：
   - `/oem` 只读 squashfs → bind mount 覆盖单文件：`cp new /userdata/rkipc && mount --bind /userdata/rkipc /oem/usr/bin/rkipc`（`umount` 回滚）
   - 或直接运行 `/userdata/rkipc.xxx`（3b 即此法，不碰 `/oem`）
3. DoD：外部脚本/SDK 向 result-in.sock 注入高辨识度检测（如 "M1-INJECT"）→ RTSP `rtsp://<ip>:554/...` 看到框+标签 → 录像回放含框 → WS `wss://<ip>/ws/inference/results` 收到 source_id≠"builtin" 的结果；冒充 "builtin" 被拒；超速被丢+计数。
4. 异常立即 `umount`（或 cp 回 .bak）+ 重启 rkipc，不动 bootloader，无变砖风险。

**设备访问**：代码/编译在 wsl2-local；真机 192.168.42.1（64BIT 板级），历史验证机 192.168.42.1 / 192.168.42.1。通过 `~/.rpty/bin/fleet` 跨机操作，SSH 详见 `recamera-rk-build` skill 与 memory `recamera-pro-rv1126b-build.md`。

**文档**：方案商侧接入文档在 `docs/ext/`（audio-pcm / result-push / frontend-extension / rkipc-rpc-status / control-api / gstreamer-integration / ffmpeg-integration / gpio-result-trigger / README）。
