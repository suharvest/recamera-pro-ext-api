# M1 实现计划：结果回注 + rc_ext_core 骨架 + SDK

> 依据 `RECAMERA_PRO_API_SPEC.md` v0.4 §1/§3/§8。本文是执行 agent 照做的施工图，所有锚点已在 wsl2-local 源码树核实（file:line）。
> **代码基**：`/home/harve/project/recamera_rk/RV1126B_Linux_IPC_SDK/project/app/recamera_ipc`（下称 `$IPC`）。
> **前置门禁**：G3（PTS 方案）、G4（max_sources）——验证中，结果落定后填 §A/§B 的占位。其余部分不依赖门禁，可先施工。

## 目标（本轮 DoD）

1. **链路闭环**（改 rkipc + 烧固件）：外部进程写 `result-in.sock` 一条带 `source_id`/`pts_us` 的 InferenceResult → 官方 OSD 在视频上画出框和标签 → RTSP 流可见 → 录像含框 → WS:8123 收到带 source_id 的结果。
2. **SDK 封装**：`librecamera_ext.so`（C ABI）的 result sink 部分可用，Python 薄封装 `ResultSink` 五行注入。
3. **不回归**：内建推理照常工作（内建走同一条抽取后的 dispatch，source_id="builtin"）。

## 关键实现事实（已核实）

**三路分发点** `$IPC/src/rv1126b_ipc/video/video.c:652-656`：
```c
rc_serialized_result sr = rc_infer_convert_result_to_protobuf_packed(results, model_id, cur_utc_ms, model_classes);
if (RC_INFER_VALIDATE_SERIALIZED_RESULT(sr)) {
    vg_inference_enqueue_protobuf(sr.data, sr.size);   // vigil 录像
    rc_notify_send_inference(model_id, sr.data, sr.size); // WS/MQTT/HTTP/UART
    osd_manager_draw_infer(model_id, sr.data, sr.size);   // OSD 叠加
}
```

**三个分发函数签名**（输入统一为打包好的 InferenceResult protobuf 字节）：
- `common/vigil/include/vigil_capi.h:91` `vg_sta_e vg_inference_enqueue_protobuf(const void *data, size_t data_len);`
- `common/rc_notify/rc_notify.h:16` `int rc_notify_send_inference(int id, const void *payload, size_t payload_size);`
- `common/osd/osd_manager.h:36` `int osd_manager_draw_infer(int id, const void *payload, size_t payload_size);`

**关键推论**：外部注入的结果与内建结果是同一种字节（打包 InferenceResult protobuf）。因此 result-in 端点收到字节后**直接喂同一条 dispatch**，零转换。这是 M1 风险低的根本原因。

**proto 现状** `common/vigil/protocol/inference.proto:110-125`：`InferenceResult` = tag 1/2/3 + oneof 10-14。tag 4/5 空闲。

## 施工步骤

### 步骤 1：proto 扩展（兼容，先做）

`common/vigil/protocol/inference.proto` 的 `InferenceResult` 加两字段：
```proto
message InferenceResult {
  TaskType task_type = 1;
  int64 timestamp_ms = 2;
  int32 model_id = 3;
  string source_id = 4;   // 新增：结果来源，"builtin" 保留给内建
  uint64 pts_us = 5;      // 新增：对应帧 pts（G3 定方案），0=无帧关联
  oneof data { ... }      // 10-14 不动
}
```
重新生成三份代码（源码树里找现有生成命令，通常 CMake 里有 protoc 规则或 Makefile target）：
- `common/rc_notify/inference.pb-c.{c,h}`（C，protobuf-c）
- `common/rc_infer/src/utils/inference.pb-c.{c,h}`
- `recamera_notify/notify-server/protobufs/inference_pb2.py`（Python）
**先确认生成方式**：`grep -rn "protoc\|pb-c\|protobuf" $IPC/CMakeLists.txt $IPC/common/*/CMakeLists.txt`。若无自动规则则手工 `protoc-c`/`protoc`（设备源码树已装 protoc）。

### 步骤 2：抽取 rc_result_dispatch（不改行为）

`video.c:653-655` 三行抽成一个函数，内建路径与入站路径共用。新增到 `common/rc_result/`（或就近放 `common/rc_notify/`）：
```c
// rc_result_dispatch.h
void rc_result_dispatch(int model_id, const void *packed, size_t size);
// .c
void rc_result_dispatch(int model_id, const void *packed, size_t size) {
    vg_inference_enqueue_protobuf(packed, size);
    rc_notify_send_inference(model_id, packed, size);
    osd_manager_draw_infer(model_id, packed, size);
}
```
`video.c:653-655` 三行替换为 `rc_result_dispatch(model_id, sr.data, sr.size);`。
**内建路径的 source_id**：`rc_infer_convert_result_to_protobuf_packed()` 里补填 `source_id="builtin"` + `pts_us`（pts 来源见 §A）。定位该函数：`grep -rn "rc_infer_convert_result_to_protobuf_packed" common/rc_infer/`。

### 步骤 3：rc_ext_core 骨架（M1 只用到其一个子集）

新增 `common/rc_ext_core/`。M1 阶段先实现 result-in 需要的最小集，帧代理（M2）复用时再补 fd 传递部分：
- `ext_socket.{c,h}`：SOCK_SEQPACKET 服务端骨架（bind `/run/recamera/*.sock`、accept、事件循环、每连接结构体）
- `ext_handshake.{c,h}`：Hello/HelloAck（proto 见 §1.2，需新建 `ext_api.proto`）、版本区间协商、capabilities 返回
- `ext_identity.{c,h}`：`SO_PEERCRED` 取 uid/pid + `/run/recamera/apps.d/<name>.conf` 查表；`"builtin"` 保留字拒绝
- `ext_ratelimit.{c,h}`：两级 token bucket（每连接 + 全局）
M1 暂不需要 fd 所有权状态机（那是 M2 帧代理的）。SEQPACKET 收发定长消息即可。

### 步骤 4：rc_result_in 端点

新增 `common/rc_result_in/rc_result_in.{c,h}`——rc_ext_core 之上的薄层：
1. 监听 `/run/recamera/result-in.sock`（0660，root:recamera-ext）
2. 握手（复用 rc_ext_core），capability 返回 `result@1` + limits（max_msg_rate 60, max_sources 见 §B）
3. 每条消息：peercred 定 source_id（外部不得用 "builtin"）→ 校验是合法 InferenceResult protobuf（解一次确认 + 覆盖/填充 source_id 字段）→ 限速 → `rc_result_dispatch(EXT_MODEL_ID, data, size)`
4. 限速：每连接 60 msg/s + 全局 120 msg/s（token bucket），超限丢弃计数
5. 启动：在 rkipc main 里起该服务线程。定位 rkipc 服务初始化点：`grep -rn "rkipc_server_init\|pthread_create.*server\|main(" src/rv1126b_ipc/main.c`

**source_id 覆盖策略**：收到的 protobuf 里若 source_id 为空或非法（="builtin"），服务端按 peercred 强制改写。改写需在 protobuf 层面——最省事是解包→改字段→重打包（M1 量小可接受；后续优化为 wire-format 原位改）。

### 步骤 5：init 脚本

`/run/recamera/` 目录创建 + 组 + 权限。找 rkipc 的 init 脚本（`grep -rn "run/recamera\|mkdir.*run" $IPC` 或 rootfs 的 `/etc/init.d/S*rkipc`），加：
```sh
mkdir -p /run/recamera/apps.d
chgrp recamera-ext /run/recamera 2>/dev/null || true
chmod 0750 /run/recamera
```
（`recamera-ext` 组不存在则先建；v1 全 root 环境下组是软约束。）

### 步骤 6：SDK — librecamera_ext（result sink 部分）

新增 `sdk/librecamera_ext/`（放 `recamera_services` 下或独立，发布形态属 G7，本轮先 in-tree）：
```c
// recamera_ext.h — C ABI v1
typedef struct rc_ext_result rc_ext_result_t;
rc_ext_result_t *rc_ext_result_open(const char *source_id, int *err);
int rc_ext_result_send_detections(rc_ext_result_t *h, uint64_t pts_us,
                                  const rc_ext_box_t *boxes, size_t n);
void rc_ext_result_close(rc_ext_result_t *h);
```
内部：连 socket + 握手 + 用 inference.pb-c 组包 InferenceResult(detection)。方案商不碰 protobuf。
Python 薄封装（ctypes）`recamera_ext/__init__.py`：
```python
sink = ResultSink(source_id="face-app")
sink.send_detections(pts_us=..., boxes=[(x1,y1,x2,y2,score,"张三"),...])
```

### 步骤 7：编译 + 验证 — **二进制热替换优先，不烧固件**

M1 改动只落在两个产物：**rkipc 二进制**（含 rc_result_dispatch/rc_ext_core/rc_result_in）+ **notify-server**（Python，改了 pb2）。**不需要烧 update.img**——热替换快、可回滚、不中断整机。

1. 编译新 rkipc：`./build.sh app`（~3min）。产物：`find output -name rkipc -type f`。确认 CMake 收录新文件（`grep -rn "GLOB\|aux_source" common/CMakeLists.txt`；非 glob 则手动加）。
2. **热替换（root，先备份）**：
   - 定位 rkipc：`ps | grep rkipc`（V4 见 `rkipc -a /oem/usr/share/iqfiles`，二进制约在 `/oem/usr/bin/rkipc`）
   - `/oem` 若只读 squashfs（先 `mount | grep oem` 判断）→ bind mount 覆盖单文件：`cp new_rkipc /userdata/rkipc && mount --bind /userdata/rkipc /oem/usr/bin/rkipc`（可 umount 回滚）；可写则 `cp rkipc rkipc.orig.bak` 后替换
   - notify-server：`find / -name inference_pb2.py 2>/dev/null`，备份后替换
   - 重启 rkipc：init 脚本或 kill 由 supervisor 拉起（**先确认有守护拉起，否则手动起**；恢复命令预先在手）
3. **DoD 验证**（真机 192.168.42.1）：
   - 外部脚本（或 SDK Python）向 result-in.sock 注入一个 detection（label 高辨识度如 "M1-INJECT"）
   - web 预览 / RTSP `rtsp://192.168.42.1:554/...`：看到框 + "M1-INJECT" 标签
   - 录像回放：含该框
   - WS `wss://192.168.42.1/ws/inference/results`（带 Cookie token，见 M0 文档）：收到 source_id != "builtin" 的结果
   - 内建推理未回归：同屏同时有 builtin 的框（不同色）
   - 冒充测试：注入 source_id="builtin" → 被拒
   - 限速测试：>60/s 注入 → 超出被丢 + 计数可查
4. **回滚**：异常立即 `umount /oem/usr/bin/rkipc`（或 cp 回 .bak）+ 重启 rkipc。不动 bootloader/分区，无变砖风险。
5. 固件烧录（`update.img`）留作 M1 定稿后的正式集成验证，非本轮 DoD 必需。

## §A（G3 已 PASS）：pts_us 来源 — 定案
`u64PTS` = CLOCK_MONOTONIC 微秒（40 帧单调、漂移 <0.5ms、全链路同源）。**内建路径 pts_us = VI 帧 `stVFrame.u64PTS`**，OSD 就近帧匹配（容差 1 帧周期 ≈166ms@6fps）。自打钟退路不启用。第三方进程亦可用 `clock_gettime(CLOCK_MONOTONIC)` 复算同域时间戳。

## §B（G4 已 PASS）：max_sources — 定案
RGN 每通道 attach 上限 8、内建占 1。**优先单 canvas 合成**：所有 source 画进同一 overlay region、source_id 哈希调色，capability `result@1`.limits["max_sources"]=8（软上限，不受 RGN 硬限）。**退路**（osd_manager 改动过大时）：独立 region，max_sources=6。DoD 至少验证 1 个外部 source 上 OSD。

## 风险与回滚
- 首次烧自编固件：备原厂 `update.img`，确认 maskraom 恢复路径
- rc_result_dispatch 抽取是纯重构，先单独烧一版验证内建推理不回归，再加入站端点
- 增量编译 3min，回路快，出问题快速迭代

---

# 阶段 3b：API 封装（rc_ext_core 完整 + C ABI SDK）

> 3a 证明了链路（外部 protobuf → dispatch → WS/OSD）。3b 把裸链路工程化成 spec §1/§8 的版本化契约 + 易用 SDK。
> **3a 遗留**：OSD 可见性 bug（codex 诊断中）——3b 的封装正确性用 **WS 路验证**（已通），不依赖 OSD 可见；可见性修复独立收口。

## 范围
1. **ext_api.proto**（新建 `common/vigil/protocol/ext_api.proto`）：`Hello`/`Capability`/`HelloAck`（字段见 spec §1.2）。生成 C（pb-c）。
2. **rc_ext_core**（新建 `common/rc_ext_core/`）——从 3a 的裸 rc_result_in 抽出可复用传输层：
   - `ext_socket.{c,h}`：SEQPACKET 服务端骨架（bind/accept/事件循环/每连接结构体）
   - `ext_handshake.{c,h}`：收 Hello → 版本区间交集协商（非 min，spec §1.2）→ 回 HelloAck + capabilities
   - `ext_identity.{c,h}`：`SO_PEERCRED` 取 uid/pid + `/run/recamera/apps.d/<name>.conf`（`uid=<n>`）查表；`"builtin"` 保留字拒绝；未注册 uid 的 source_id 强制改写为 `uid:<n>`（spec §1.1，含上机实证的 root 身份校准）
   - `ext_ratelimit.{c,h}`：两级 token bucket（每连接 60/s burst 15 + 全局 120/s burst 30，spec §3.3）
3. **rc_result_in 升级**：3a 裸版 → 走 rc_ext_core（握手 → 身份定 source_id → 限速 → `rc_result_dispatch`）。capability 返回 `result@1` + limits{max_msg_rate:60, max_sources:8}。
4. **librecamera_ext**（新建 `sdk/librecamera_ext/`，in-tree）C ABI（spec §2.5/§3.5）：
   - `recamera_ext.h`：`rc_ext_result_open(source_id,&err)` / `rc_ext_result_send_detections(h,pts_us,boxes,n)` / `rc_ext_result_close(h)`
   - 内部：连 result-in.sock + 握手 + 用 inference.pb-c 组包 InferenceResult(detection)。soname `librecamera_ext.so.1`
5. **Python 薄封装**（ctypes）`recamera_ext/__init__.py`：`ResultSink(source_id).send_detections(pts_us,boxes)`。

## DoD（3b）
- 握手：客户端 Hello(version_min=1,max=1) → HelloAck(api_version=1, auth_mode="peercred", capabilities=[result@1{limits}])。版本无交集 → EVERSION。
- 身份：注册表内 uid 用注册名，未注册用 `uid:N`，`"builtin"` 被拒（EAUTH）。**WS 里 source_id 正确**。
- 限速：注入 >60/s → 超出被丢 + metrics 计数可查（WS 收到数 ≤ 限速）。
- SDK：Python `ResultSink` 五行注入，WS 收到对应 detection（**不依赖 OSD 可见**）。
- 不回归：内建推理照常（WS 见 builtin 流）。

## 编译协调
- 3b 代码可与 codex 可见性诊断**并行编写**（改不同文件：3b 碰 rc_ext_core/rc_result_in/ext_api.proto/sdk；codex 碰 rc_result_osd.c/osd_manager）。
- **编译-烧录-验证串行**：由收口阶段统一做——合并「可见性修复 + 3b」→ 一次 `build.sh app` → 一次热替换 → 一次跑全部 3a+3b DoD。
- 护栏同 3a：只改 recamera_ipc + notify-server；热替换不烧固件；备份在手；VPSS 异常立即回滚。

---

# 3b 真机验证结果（2026-08-11，设备 192.168.42.1，固件 V1.0.10）

验证方式：直接运行 `/userdata/rkipc.3b`（不改 `/oem`，保持原厂），从设备本地经 result-in.sock + notify WS:8123 验证。全部通过：

| DoD | 结果 | 证据 |
|---|---|---|
| rc_result_in 端点启动 | ✅ | 日志 `listening on /run/recamera/result-in.sock (SEQPACKET; handshake + peercred + ratelimit)` |
| 握手协商 | ✅ | Hello v[1,1] → `api_version=1, auth_mode="peercred", error=0` |
| 版本不兼容拒绝 | ✅ | Hello v[2,2] → `error=1`(EVERSION) |
| 身份 builtin 冒充拒绝 | ✅ | 日志 `reject: source_id=builtin from uid:0` |
| 限速（令牌桶） | ✅ | 发 80 条 → WS 收 28 条（丢 52，60/s+burst15 瞬时爆发下合理） |
| 注入→dispatch→notify→WS 端到端 | ✅ | WS 收到注入的 detection |
| SDK C ABI 端到端 | ✅ | `rc_ext_result_open`(handle 非空/err=0)→`send_detections`(rc=0)→`close`，WS 收到 `SDK-3B-OK` |
| rkipc 主体不回归 | ✅ | 推理/ISP 正常，pid 稳定 |
| 缺 osd cfg 不致命 | ✅ | rkipc 主体+socket 正常（3a 引入的 cfg 硬依赖是待修 bug，但不阻塞 WS 路） |

**设备已回滚原厂**（`ef84f99`，dmesg 无崩溃，测试残留清理，备份保留）。

## 仍挂起
- **OSD 可见性**：INFER slot 的 RGN layer 问题（假设 B，已定位），需授权稳定窗口跑 layer-swap 实验
- **3a 的 osd cfg 硬依赖**：`osd_manager_load_cfg` 缺 cfg= 返回 -ENOENT 应降级为默认值而非致 init 失败（健壮性 bug，与 OSD 可见性一起修）
