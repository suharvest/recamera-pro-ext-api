# reCamera Pro 应用中心（App Center）移植设计

> 把一代 `sscma-example-sg200x/solutions/supervisor` 的「云端一键安装 / 应用画廊」移植到 reCamera Pro（RV1126B / aarch64 / nginx + entry.cgi + go2rtc）。

## 0. 一句话结论

不复用一代的 C++ supervisor 二进制，而是**新起一个 Python 常驻服务 `appmgr`**（照抄 `recamera_notify` 的形态），通过 nginx `ext_appmgr.conf` 挂到闭源 dashboard 旁边、复用其 JWT 登录；用 **tar.gz 包 + appmgr 自管进程** 取代一代的 `opkg/.deb + /etc/init.d`；前端做成**可嵌入的独立 SPA** 复用一代 `views/applications` 组件；云端 catalog + 浏览器代取 + manifest + 孤儿检测**几乎照搬**。

### 已拍板决策（2026-08-08）
| # | 决策 | 取值 |
|---|---|---|
| 1 | appmgr 后端语言 | **Python 常驻**（抄 recamera_notify） |
| 2 | 应用并发 | **强制单 active**（同一代语义，见 §2/§4.1） |
| 3 | 前端落点 | **独立 SPA 挂 `/appcenter/`，但按"未来可整块并入官方 dashboard"的约束设计**（见 §4.5） |
| 4 | 当前阶段 | 只出设计，暂不写代码 |

## 1. 一代 vs Pro 关键差异

| 维度 | 一代 (sg200x) | Pro (RV1126B) | 影响 |
|---|---|---|---|
| Web 后端 | 自研 supervisor(libwebsockets) 单进程占 :80，**它就是 dashboard** | nginx + `entry.cgi`(**闭源二进制，不可改**) + go2rtc + recamera_notify | 后端只能**旁挂新服务**，走 `include ext_*.conf` |
| 包管理 | opkg + `*_riscv64.deb` | **无任何包管理器** | 改用 tar.gz + appmgr 自解压/自注册 |
| 应用模型 | **独占相机**，一次只一个 app，switchApp 停 A 起 B | rkipc 常驻独占相机 → go2rtc 出流，app **消费共享流** | 去掉 VPSS 切换/相机所有权那套 |
| 进程生命周期 | app 自带 `/etc/init.d/S9x<id>`，OS 拉起 | appmgr **直接当 process supervisor** | 不依赖 init.d 注册，绕开 overlay dentry 坑 |
| 鉴权 | supervisor 自己 check_token | nginx `auth_request /_jwt_verify`(entry.cgi)，`sensecraft_token`(localStorage JWT) | 新服务内部免鉴权，nginx 挡 JWT |
| 自启动 | init.d S 脚本 | BusyBox rcS 跑 `S<NN>`，OTA 会洗 `/etc`；RkLunch 从 `/userdata/config/system/etc` 回注 | 自启动落 `/userdata` + 一个回注 hook |
| 存储 | — | rootfs 2.9G(剩 2.1G)，**`/userdata` 11.3G 空** | 应用、状态、SPA 全落 `/userdata` |

## 2. Pro「应用」的重新定义（最重要的概念转变）

一代的 app = 一个抢相机的推理二进制。**Pro 的 app = 共享流消费者**：

```
rkipc(独占 ISP/相机) ──► go2rtc(RTSP/WebRTC 出流, :1984/554/8555)
                                 │
                                 ├─ 官方 recamera_notify ─► RKNN(nanodet) ─► WS :8123 结果 + 通知
                                 └─ 【新】用户 app ─► 读共享流 + RKNPU 推理 ─► 自己的 WS/MQTT/通知
```

由此得到的红利：相机崩溃/VPSS 那套缓解逻辑整块删掉——app 不碰相机，切换时无需"停 A 释放 VPSS 再起 B"的等待舞蹈。

**并发策略：强制单 active（决策 #2）。** 虽然流消费者天然可并存，但为省 RKNPU/内存、并直接对齐一代画廊语义，Pro 也**一次只跑一个 user app**：`switch` = 停旧起新，`state.json` 只存单个 `active_app`。注意这是**策略约束而非硬件约束**（相机始终归 rkipc），所以切换成本极低（只是拉起/杀掉一个流消费进程，无 VPSS 释放等待）；将来若要放开并行，改的只是 appmgr 的 active 集合（单值→列表）+ 画廊 UI，链路其余不动。

> ⚠️ **codex 整改——"单 active"没解决资源争用**：官方 `recamera_notify` 是**始终并行**跑的(它不受 appmgr 管)。所以即使 user app 单 active,设备上**至少有 2 个流消费者 + 2 路 RKNN 在跑**(官方 notify + user app)。"单 active"只约束 user app 之间,**不代表 NPU/内存/流连接不冲突**。必须明确:① system service(notify) vs user app 的**资源边界与优先级**(谁 OOM 先被杀);② user app 的 NPU/内存**配额**;③ 故障隔离(user app 崩溃不能拖垮官方 notify/rkipc)。§4.4 的 cgroup 限额 + §7 的 NPU 并跑实测是这条的落地。另:manifest 里"直接接 rkipc VI 通道"的选项会**打破"共享流消费者"定义**(变成争 VI),应默认只允许走 go2rtc 拉流。

## 2.1 官方推理是 rkipc 集成的，可 API 控制；由此分出两类 Pro 应用（实测）

官方 dashboard 的「推理配置（使能/模型/FPS）」和「AI 结果叠加显示」**不是独立服务，而是 rkipc 内建能力**：
- 配置落 `rkipc.ini`：`[video.source] enable_npu=1 / npu_fps`、`[rc_model.0] model=yolox_s.rknn enable=1`（单模型槽 active）、OSD `cfg` JSON 里 `inferenceOverlay":{"iEnabled":1}` 即那个叠加开关。
- 控制接口在 **entry.cgi**（`/cgi-bin/entry.cgi/...`，JWT 后）：实测字符串含「set inference enable state / set inference fps / set inference model / inference-restart / read inference status」+ `osd_manager_get_cfg`/`osd_manager_set_cfg` + 模型上传管理(`/userdata/config/model/rknn`，model_id/model_name)。**→ 我们可以通过官方 API 关掉/切换官方推理**（或直接改 rkipc.ini + 重启 rkipc）。
- rkipc 引擎支持结果类型 **Detection/Classification/Keypoints/Segmentation/Tracking**；每模型带 `.json` sidecar（`algorithm`+`category`+`classes` 标签表）作后处理契约。结果由 rkipc **内部产生、经 notify socket 往外推**（喂 WS :8123/浏览器）——**没有"往叠加层注入外部框"的入口**。

**由此，Pro 应用中心天然分两类**（appmgr 都能装）：

| 类型 | 定义 | 用官方叠加/UI? | NPU/相机 |
|---|---|---|---|
| **A. 模型应用**（轻量） | 一个 `<model>.rknn` + `.json`，装进 rkipc 模型目录、经 API 选中 | ✅ **原生复用**官方 pipeline+叠加+实时预览 UI，零叠加代码 | 占用 rkipc 的**单模型槽**（=替换官方模型，非叠加在其之上） |
| **B. 进程应用**（通用） | appmgr 拉起的独立进程，消费 go2rtc 流 + 自己的 RKNN context + 自己的结果 UI | ❌ 官方叠加喂不进外部结果；结果走**自己的 WS/MQTT + /appcenter/ 页面** | 与官方推理**并行**（§2 的算力争用适用） |

> 设计含义：A 类是"自定义检测/分类模型"这一最常见场景的**超轻量安装路径**——appmgr 只需把模型+json 放进 `/userdata/config/model/rknn` 并调官方"set inference model" API，全套官方 UI/叠加/FPS 免费复用。B 类才是需要独立进程/自定义逻辑（音频、融合、非标渲染）的通用路径。两者可共存于 manifest 的 `type` 字段。

## 2.2 流水线应用（多模型级联，如人脸识别）如何贴官方路径（实测）

rkipc 引擎**固定后处理**（`yolox/yolov8/nanodet/classify/yolov8_pose`，任务类型 DETECTION/CLASSIFICATION/KEYPOINTS/SEGMENTATION/TRACKING），**无人脸识别后处理、无原生级联、2 个模型槽也是各跑全帧并行而非级联**。故 FR 这类"检测→对齐→embedding→比对"流水线**不能塞成一个 rkipc 模型**。

**但检测结果有现成复用出口**：rkipc 将结果打成 **protobuf**（schema `recamera_notify/protobufs/inference_pb2.py`：`InferenceDetectionResult{box,class_id,class_name,label,score}` / Keypoints / Segmentation / Tracking / Classification），作为 client 连 `/var/tmp/notify`（recamera_notify 绑定的 UNIX socket）推出；recamera_notify 解码后经 **WS `:8123`（nginx `/ws/inference/results`，JWT 后）广播**。**on-device 进程可直连 `127.0.0.1:8123` 免 JWT 订阅。**

### 推荐：分层混合流水线（最大化复用官方）

| 段 | 归属 | 复用官方 |
|---|---|---|
| **前段：检测**（如人脸检测） | 转成 rkipc 支持的检测模型 + `.json`(category=Detection)，装入 `/userdata/config/model/rknn`、经 inference API 选中 | ✅ 官方 NPU 推理 + 叠加画框 + 实时预览 UI + FPS 控制 + protobuf feed |
| **后段：对齐/embedding/比对** | appmgr 拉起的 B 类进程：**订阅 `:8123` 取官方检测框**（省一份检测 NPU），对 ROI 跑自有 arcface/mobilefacenet RKNN，比对注册库 | ✅ 复用官方检测结果，自定义逻辑不受 rkipc 约束 |
| **出：身份标签** | `/appcenter` 页面**客户端叠加**（播 go2rtc 流 + canvas 画名字） | ⚠️ 官方服务端叠加只认 rkipc 模型的 class_name（=“face”），身份画不进去 → 必须客户端叠 |

关键约束：① **身份标签只能客户端叠**（官方叠加无外部注入入口）；② detect→recognize 的**级联缝合在你的进程里做**（rkipc 不级联）；③ 注册库+比对本就是你进程的活（非 NN、有状态）；④ NPU 上"官方检测器 + 你的 embedder"时间片共享（§2 预算适用，但你省掉了自跑检测器那份）。

### 变体
- **闭集、人少且固定**：整条塞成**一个闭集分类模型**(类别=人名，SenseCraft 训练或自转)→ 纯 A 类，官方叠加直接显示人名，零自定义进程。代价：换人重转模型、无端上注册、不可扩展。
- **全自定义**：全 B 类，自己检测+识别+自己 UI，最灵活、零官方叠加复用。

### 对 manifest 的含义：新增 `pipeline` 应用类型
`type: pipeline` 的 app 在 manifest 里声明三段：
```json
{
  "type": "pipeline",
  "frontModel": { "rknn": "face_det.rknn", "json": "face_det.json" },   // appmgr 装入 rkipc 模型目录 + 调 API 选中
  "process":    { "entry": "run", "subscribe": "ws://127.0.0.1:8123" }, // 订阅官方检测框，跑后段
  "clientOverlay": "index.html"                                          // /appcenter 里客户端叠加最终结果
}
```
appmgr 装 pipeline app = 放 frontModel 进 `/userdata/config/model/rknn` + 选中（官方 API）+ 起 process + 部署 clientOverlay 到 `/userdata/appcenter/`。**这是"最大化整合官方路径"的标准模式。**

## 2.3 两种推理模式：rkipc 集成 vs 自建（关官方）—— 每 app 声明（实测）

§2.2 的 hybrid（复用官方检测 + 自己后段）对**复杂流水线是 split-brain**：一半逻辑在 rkipc、一半在你进程，结果分散在官方叠加 + 客户端叠加。对 FR 这类多模型级联，**自建整条 + 关官方推理是更干净的选择**，且实测可行：

**帧源问题（自建的真正难点）已有解**：rkipc 独占 VI、**不给外部原始帧口**，但——
- sub 流 `[video.1]` = **640×480 H.265**（`rtsp://127.0.0.1:5554/live/1`），正好推理输入尺寸；
- 设备有 **MPP 硬解**（`librockchip_mpp.so` + `/dev/mpp_service` + m2m `/dev/videoN`）+ **RGA**（`librga.so`）+ **RKNN runtime**（`librknnrt.so`）全套；
- 自建帧源 = **sub 流 → MPP 硬解 → RGA 色转/缩放 → RKNN**，相比 rkipc 内部 `VI→RGA→NPU` 只多一次 640×480 小流硬解，开销小（非 CPU 软解）。
- `[video.source]` 的 `enable_npu` 与 `enable_venc_*/enable_rtsp` **独立** → **关推理释放 NPU、出流照常**。

**两种模式做成 manifest 的 `inferenceMode`（不是全局二选一）**：

| 模式 | 适用 | appmgr 行为 | 结果呈现 |
|---|---|---|---|
| `rkipc` | 简单单模型（标准检测/分类/pose） | 装模型进 `/userdata/config/model/rknn` + 官方 API 选中 | 官方叠加 + 实时预览 UI 复用 |
| `self-hosted` | **流水线/多模型/自定义（FR、音频、融合）** | **激活时调 entry.cgi API 关官方推理**（记录原 enable/model 以便恢复）、起进程独占 NPU；**停用/切走时恢复官方推理** | 自己 WS/MQTT + `/appcenter` 客户端叠加 |

self-hosted 的关/恢复由 appmgr 纳入该 app 生命周期，配合单 active → 设备任一时刻推理状态**确定且可回滚**（state.json 记录 `official_inference_saved`）。

**帧源三条路（self-hosted 的输入）**：
- **(a) 硬解 sub 流（今天就能做，推荐默认）**：`rtsp://127.0.0.1:5554/live/1`(640×480 H.265) → MPP 硬解 → RGA → RKNN。RKVDEC 硬解 640×480 ~1–2ms、几乎不吃 CPU，代价只是 ~1–2 帧延迟 + 编码画质损失。**无需任何源码。**
- **(b) rkipc 模式**：让 rkipc 喂帧，零解码，但被固定后处理锁死。
- **(c) 改 rkipc 导出 VPSS 通道（真·零拷贝）——架构已被参考源码坐实**：Rockchip 官方 rkipc 参考源码（已下载 `reference/rkipc-rockchip-ref/project/app/rkipc/`，`rv1126_aiisp_ipc/video/video.c`）喂 NPU 就是 `RK_MPI_VPSS_GetChnFrame(...)` → `int fd = RK_MPI_MB_Handle2Fd(frame.pMbBlk)` 拿 **dma-buf fd**。外部零拷贝导帧 = 在此 fd 上加一步 **SCM_RIGHTS over unix socket** → 小补丁非重写。**卡点**：补丁要打在能编译的 **Seeed Pro fork** 上，而该 fork **未公开**（GitHub 上 Seeed 只有一代 SG200x；Pro 固件 V1.0.10 才发，设备上仅二进制）→ 需向 Seeed 索取或等其开源。rkipc 本身是 Rockchip 官方应用（rockit/RK_MPI，官方文档 `Rockchip_Developer_Guide_Linux_RKIPC_CN/EN.md` 在下载里）。
- **决策**：先走 (a)（无源码依赖、硬解够便宜），`FrameSource` 抽象留好；实测延迟若真伤场景，再向 Seeed 要源码上 (c)，且只换 `FrameSource` 适配器。

### 迁移保险 = 解耦抽象，不是"自建"这个动作本身
无论 rkipc 还是 self-hosted，管线都拆两个适配器接口，核心逻辑与之解耦：
- **`FrameSource`**：今 = sub 流 + MPP 硬解；未来官方开放原始帧口 → 只换适配器。
- **`ResultSink`**：今 = 自有 WS + 客户端 canvas 叠加；未来官方开放"外部结果注入叠加" → 只换适配器。
- **核心（detect→align→embed→match）source/sink 无关** → 官方将来加不加多阶段，核心都不重写：要么继续自建，要么把前段换成官方 API，皆为换适配器。**这才是"好迁移"的来源。**

## 3. 目标架构

```
浏览器
  │  http(s)://<device>/
  ├─ /                (官方 dashboard, entry.cgi + React, 不动)
  ├─ /appcenter/      ──► 【新】独立 SPA(静态, /userdata/appcenter/www)   ← ext_appmgr.conf
  └─ /api/appMgr/*    ──► 【新】appmgr 常驻服务(127.0.0.1:8130)            ← ext_appmgr.conf
                            (nginx auth_request /_jwt_verify 挡 JWT)
appmgr(Python 常驻, 仿 recamera_notify)
  ├─ 应用仓库    /userdata/local/apps/<id>/{manifest.json, bin/, models/, run}
  ├─ 状态       /userdata/local/apps/state.json  (active[], versions)
  ├─ 安装引擎    校验 → 解压 tar.gz → 落盘 → 注册
  └─ 进程管理    start/stop/restart 各 app(自己 fork，写 pid，看护)
```

## 4. 组件设计

### 4.1 后端 `appmgr`（Python 常驻，推荐）

- **为什么 Python 常驻而不是 CGI**：一代用原子 busy-gate + poll 线程跨异步作业串行化安装/切换（`api_app.h:96`）。CGI/fcgiwrap 每请求短命进程，无法自然持锁/持状态。常驻进程天然解决 busy-gate、state machine、进程看护。`recamera_notify` 已证明这条路在 Pro 上成立，直接抄它的打包/启动形态。
- **模板**：`/oem/usr/lib/python3.11/site-packages/recamera_notify/`（`notify_server.py` 启动、`notifiers/websocket_server.py` WS、`core/config_manager.py` 配置）。
- **监听**：`127.0.0.1:8130`，只认本地，公网面由 nginx 转发。
- **接口**（对齐一代 `/api/appMgr/*`，前端零改动即可复用）：
  | 路径 | 说明 | 对应一代 |
  |---|---|---|
  | `GET  /api/appMgr/list` | 画廊：合并内置+用户 manifest，输出 `installed/active/status/hw_supported` | `api_app.cpp:676` |
  | `GET  /api/appMgr/current` | 当前 active | `api_app.cpp:719` |
  | `POST /api/appMgr/switch` | 设 active（**单 active**：先 stop 当前 active，再 start 目标；写 state.json） | `switchApp` |
  | `POST /api/appMgr/stop` | 停 app | — |
  | `POST /api/appMgr/installApp` | body `{path:/userdata/.../x.tar.gz}` → 安装 | `api_app.cpp:1223` |
  | `POST /api/appMgr/uninstallApp` | body `{id}` → 卸载 | `api_app.cpp:1350` |
  | `GET/POST getConfig/setConfig/setModel/getIntegrationDoc` | 照搬 | `api_app.h:42-44` |
  | `GET  /api/appMgr/storageInfo` | `df /userdata`（一代在 fileMgr，这里并进来） | `api_file.h` |
  | `POST /api/appMgr/upload` | 分块上传暂存包到 `/userdata`（一代用 fileMgr/upload） | files api |
- **并发(codex 整改)**：`asyncio.Lock` 只保证单进程内互斥,不能防"两个 appmgr 实例"。需 **① 单实例锁**:启动时 `flock` 一个 `/userdata/local/appmgr/appmgr.lock`,拿不到就退出,防重复实例;**② busy-gate**:所有变更类接口(install/uninstall/switch)串行化,`try/finally` 释放,忙时返一代同款 `-2`;**③ 事务状态机**:每个变更操作状态持久化(pending/committing/done/failed),appmgr 重启后据此恢复或清理半完成操作。

### 4.2 nginx 接入 `ext_appmgr.conf`

放到 `/oem/usr/etc/nginx/ext_appmgr.conf`（`common_relay.conf:14` 的 `include ext_*.conf` 会加载）。

> ⚠️ **codex 整改**：SPA 静态资源**不能**挂 `auth_request`。浏览器首次拉 HTML/JS 不会带 `localStorage.sensecraft_token`，会在 SPA 加载前就吃 401，导致"跳登录页"永远执行不到。正确做法 = 官方 dashboard 同款：**静态外壳匿名可取，鉴权只挡 API**；SPA 加载后调 API 收到 401 再跳登录。

```nginx
# 应用中心 SPA —— 匿名可取(与官方 location / 一致，静态资源无秘密)
location /appcenter/ {
    alias /userdata/appcenter/www/;
    try_files $uri $uri/ /appcenter/index.html;   # 待 nginx -T 验证 alias+try_files 路径
}
# 应用管理 API —— 唯一鉴权边界
location /api/appMgr/ {
    auth_request /_jwt_verify;                     # 复用 entry.cgi 的 JWT 校验
    proxy_pass http://127.0.0.1:8130;
    proxy_read_timeout 200s;                       # 安装/卸载长耗时，对齐一代 150s
    client_max_body_size 256m;                     # tar 包上传
}
```
> `/_jwt_verify` 已在 `common_relay.conf` 定义（fastcgi 回 entry.cgi `/auth_verify`，转发 `HTTP_AUTHORIZATION`+`HTTP_COOKIE`+原始 URI/方法）。appmgr 内部**不做鉴权**（同 recamera_notify 的 `--no-jwt-auth`）。
>
> **上机必验（codex）**：① 用**完整合成配置** `nginx -T` 打印、`nginx -t` 校验,确认 include 上下文、与 entry.cgi 现有 regex location 的优先级/冲突、`alias+try_files` 实际落盘路径、401 行为都符合预期;② 拿一个真实 `sensecraft_token` 打 `/api/appMgr/list`,确认经 `/_jwt_verify` 后 `entry.cgi` 语义(header/cookie/URI/method)成立、通过与拒绝都对。**未验证前不得声称"零冲突自动加载"。**

### 4.3 应用包格式：`.tar.gz` 取代 `.deb/opkg`

一代整条链假设 opkg+deb+riscv64，Pro 无包管理器，改为自描述 tar：

```
<id>-<ver>-arm64.tar.gz
├── manifest.json          # 见 4.3.1，格式与一代 app.d.ts 对齐
├── run                    # 可执行：启动脚本(appmgr 调 `run start|stop`)，取代 /etc/init.d/S9x
├── bin/                   # 应用二进制
├── models/                # .rknn（也可走 catalog 的 target_path 单独下发）
└── hooks/ preinst postinst prerm postrm   # 可选，appmgr 按序调
```

#### 4.3.1 manifest（沿用一代 schema，改两个字段）
沿用一代 `www/src/api/app/app.d.ts:31-79` 的 JSON：`id`(白名单 `[a-z0-9-]{1,64}`)、`name/name_zh`、`type`、`models[]`、`requires[]`(硬件能力键)、`version` 等**照搬**。改动：
- `init_script`（一代 `/etc/init.d/S9x<id>`）→ **`entry`**（包内 `run` 相对路径），生命周期交给 appmgr，不再依赖 init.d。
- `type` 白名单：`native` 保留；一代的 `external-firmware` Pro 暂不需要可去。
- 新增可选 `stream`: `{ source: "go2rtc://main" }` 声明消费哪路共享流。

内置 manifest 放 `/userdata/local/apps/builtin/`，用户装的放 `/userdata/local/apps/<id>/manifest.json`；同 id 用户覆盖内置（同一代双目录扫描）。

### 4.4 安装 / 卸载 / 生命周期

**安装** `installApp`（对齐一代 `api_app.cpp:1223` + `main.sh app_install:1461`，去掉 opkg/dentry）：
1. 前端已 `upload` 把 tar 传到 `/userdata/appstage/`。
2. 校验：`realpath` 必须在 `/userdata/` 下、`.tar.gz` 后缀、大小 < 200MB、安全字符集（照抄一代 `api_app.cpp:1248-1260`）。
3. `busy_acquire()`（忙→`-2`）。
4. 解 tar 到临时目录 → 读 manifest 校验（`id` 白名单、`entry` 无 `../` 路径遍历，照抄一代 `valid_app_id:164` / `valid_init_script_path:179`）→ 原子 `mv` 到 `/userdata/local/apps/<id>/`。
5. 跑 `hooks/postinst`（可选）→ 重扫 manifest → 回 `{id, version, exit_code, output, apps_count}`。
6. 前端删暂存包（一代 `index.tsx:221`）。

**卸载** `uninstallApp`（对齐 `api_app.cpp:1350`）：`valid_app_id` 校验 → 拒卸 appmgr 自身 → 若 active 先 stop → `hooks/prerm` → `rm -rf /userdata/local/apps/<id>` → 清 state.json → 回结果。

**生命周期**：appmgr 直接当 supervisor。**不写 /etc/init.d、不 drop dentry**（Pro 非同布局 overlay，一代 `drop_dentry_cache:219` 整块删）。**单 active**：`state.json` 只存 `{active_app, active_version}`。

**进程看护契约(codex 整改——原设计过于简略)**：
- app `run` 脚本**必须前台运行**(foreground contract),appmgr 用 `setsid` 起独立**进程组**,`stop` 时对整个进程组发信号,避免漏杀子进程。
- 停止序列 **TERM → 宽限 N 秒 → KILL**;回收退出码,`waitpid` 防僵尸。
- **PID 复用防护**:不只存 pid,同时校验 `/proc/<pid>/cmdline` 或用 pidfd/进程组,防止杀错重用 pid 的无关进程。
- **崩溃退避**:app 异常退出按指数退避重启,超过阈值标 `crashed` 停止重启并告警,不无限重启刷屏。
- **stdout/stderr 接管**:重定向到 `/userdata/local/apps/<id>/logs/`,带轮转(见 §4.9)。
- **appmgr 重启后孤儿认领**:appmgr 自己崩溃重启时,按 state.json + 进程组/cmdline 重新认领仍在跑的 app,而非盲目再起一份。

**切换/安装的原子性与回滚(codex 整改)**：
- `switch`：先 `stop(当前 active)` 再 `start(目标)`;**若 start 失败必须回滚**——尝试恢复原 active,并保证 state.json 不指向一个起不来的 app(否则重启会错误复活)。全程 `try/finally` 释放 busy-gate。
- 安装落盘：解到临时目录 → 校验 → **同 id 升级时保留旧版本目录**(`<id>/versions/<ver>`,current 符号链接切换)以便失败回滚 → `fsync` 后原子切换 → 失败可退回旧 current。断电恢复:启动时检测半完成的临时目录/未切换的 current 并清理。

**空间检查**：保留一代前端估算 `needed = 下载量*3 + 8MB`（`index.tsx:248`），后端 `storageInfo` 换成 `df /userdata`。

### 4.5 前端：可嵌入的独立 SPA（决策 #3）

Pro 官方前端是闭源 minified bundle，当前**无法注入路由** → 先做**独立 SPA** served 在 `/appcenter/`；但要求**按"未来能整块并入官方 dashboard"设计**，不锁死在 standalone 外壳里。

- **复用**一代 `www/src/views/applications/index.tsx`（画廊+安装弹窗+卸载，~1000 行）、`www/src/api/app/*`（`index.ts`/`catalog.ts`/`app.d.ts`）几乎原样。
- **改**：请求器 baseURL 指同源 `/api/appMgr`；token 读取见下。
- 构建产物落 `/userdata/appcenter/www/`。用户从官方 dashboard 加一个外链，或直接访问 `http(s)://<device>/appcenter/`。
- **鉴权流(codex 整改)**：SPA 静态外壳匿名可取；加载后**首个 API 调用**带 `Authorization: Bearer <sensecraft_token>`，若 401 → 跳官方登录页。不依赖静态资源被 auth 挡（那样跳不动）。需实现与官方一致的 401 拦截 + token 刷新/过期处理。

**为"未来合并"预留的三条约束**（现在就照做，几乎零额外成本）：
1. **UI 收敛为一个自包含 feature 模块**：把画廊/安装/卸载做成 `AppCenter` 单一根组件 + 一组路由，standalone 外壳只是薄薄一层（AuthGuard + Router + 挂载点）。将来搬进官方 dashboard = 换掉外壳、把该模块挂到官方路由树下。
2. **鉴权抽象成一个 provider**：token 来源封装成 `getToken()`（standalone 下读 `localStorage.sensecraft_token`；内嵌时改成读官方 dashboard 的上下文），组件内不直接摸 localStorage。
3. **API 契约固定为 `/api/appMgr/*` 同源相对路径**：standalone 与内嵌走同一后端、同一路径，nginx 层不用区分来源。后端 appmgr 不感知前端是独立还是内嵌。

### 4.6 云端 catalog + 浏览器代取（几乎照搬，平台无关）

一代 `www/src/api/app/catalog.ts` **整体可搬**（设备无公网、浏览器代取的前提在 Pro 一样成立）：
- catalog URL 换 Pro 命名空间：`https://sensecraft-statics.seeed.cc/solution-app/recamera_pro_ecosystem/catalog.json`。
- `ICatalogApp.package` 从 `.deb` 改指 `.tar.gz`；加 `arch: "arm64"` 字段供前端过滤。
- 浏览器流程不变：`downloadToFile` → `sha256Hex` 校验 → models 传各自 `target_path` → package `uploadAndInstall` → `installApp`（`index.tsx:239-330` 照搬，仅 upload 目标 & install 参数换到 appmgr）。

### 4.7 自启动 & OTA 持久化

跟随 Pro 官方套路（RkLunch 从 `/userdata/config/system/etc` 回注 `/etc`）。**所有落在 rootfs/`/oem` 的东西 OTA 都会被洗，必须全部纳入回注链**：

需要 OTA 后恢复的三样(codex 整改——之前漏了 nginx 入口):
1. **appmgr 代码** → 落 `/userdata/local/appmgr/`,**不放 /oem**,天然存活。
2. **自启动脚本** `/etc/init.d/S94appmgr` → 主拷贝存 `/userdata/config/system/etc/init.d/`,由回注 hook 恢复。
3. **nginx 入口** `ext_appmgr.conf`(它在 `/oem/usr/etc/nginx/`,**OTA 会消失**) → 主拷贝存 `/userdata/config/system/nginx/`,回注 hook 复制回 `/oem/usr/etc/nginx/` 并 `nginx -s reload`。**否则 OTA 后服务活着但入口没了。**

**回注 hook 自身如何存活**：不新造 hook,而是**挂进现有 `custom_shadow` 回注链**(RkLunch.sh `ensure_custom_shadow_seeded`,已随 RkLunch 在 /oem——但 RkLunch 本体 OTA 也会更新)。更稳的落点是找一个官方保证会执行、且读 /userdata 的 boot 钩子;**需上机确认 OTA 后 RkLunch 是否仍调该链**(见 §7)。保底方案:S94appmgr 自己在 start 时先做"检查并回注 nginx conf"再启服务。
- `S94appmgr start`：先回注 nginx 入口(保底)→ 启 appmgr 常驻 → appmgr 读 state.json **恢复上次 active 应用**（对齐一代 `S93sscma-supervisor` 的 `app_restore`）。排在 nginx（late init S50+）之后。

### 4.8 发布侧

改一代 `scripts/release-app.py`（278 行）：
- 架构串 `riscv64` → `arm64`；正则 `_DEB_LINE_RE` 改匹配 `.tar.gz`。
- 打包步骤：`cpack/opkg` → `tar czf`。
- catalog 生成脚本换 Pro 命名空间；OSS 上传 + CDN 回验 sha256 逻辑（`release-app.py:159-170`）照搬。
- 「构建版本 vs 已发布版本」护栏（`--check`）逻辑不变，只换版本来源路径。

### 4.9 安全设计（codex 整改——安装接口 = root 代码执行，必须硬化）

appmgr 以 root 跑、并执行应用自带的 `run`/`hooks` 脚本 → **installApp 实质是"往设备投递 root 可执行代码"的接口**。一代有过"先匹配路由后鉴权"的 RCE 教训,这里攻击面更大,必须逐条防:

1. **后端强制验签,不信浏览器**：浏览器端 sha256(`catalog.ts` WebCrypto)只是 UX,**可绕过**。appmgr 收到包后**必须自己重算 sha256 比对 catalog 值**;进一步应支持**发布方签名**(catalog 带签名 + 设备内置公钥验签),否则任何能过 JWT 的人都能装任意 root 代码。
2. **tar 解压防 zip-slip / 炸弹**：逐成员校验——拒绝绝对路径、`..`、**符号链接/硬链接**、设备文件/FIFO、setuid 位;限制解压后总大小与文件数(防解压炸弹);解压目标严格限定在 `/userdata/local/apps/<id>/` 内(canonical path 前缀校验)。
3. **manifest 校验**：`id` 白名单 `[a-z0-9-]{1,64}`、`entry`/`hooks` 路径无 `..`、`models[].target_path` 必须落在**白名单目录**内(不能任意绝对路径,一代 `target_path` 是可信来源,Pro 要当不可信处理)。
4. **installApp 路径校验**(照搬一代 `api_app.cpp:1248-1260` 并加严)：`realpath` 必须在 `/userdata/` 下、`.tar.gz` 后缀、大小上限、安全字符集、regular file。
5. **兼容性 gate**(codex 遗漏项)：后端(不只前端)校验 `arch=aarch64`、固件版本区间、RKNN runtime 版本、模型 ABI;不匹配拒装,避免装上必崩的包。
6. **run 脚本降权(可选强化)**：能不用 root 跑 app 就别用——考虑以专用低权用户跑 user app,只给 appmgr 自身 root;或用 cgroup/资源限额(见 §4.4 看护)隔离 user app 故障。
7. **审计日志**：install/uninstall/switch 全部记审计日志(谁、何时、装了什么 sha256),便于事后追溯。

## 5. 照搬 / 改写 / 删除 清单

**可直接照搬**：manifest schema(`app.d.ts`)、孤儿检测 `installed=lstat(entry)`(`api_app.cpp:697`)、catalog 代取全流程(`catalog.ts`)、前端画廊/安装弹窗 UI(`views/applications`)、空间估算、release OSS+sha256 回验、安装参数校验（路径/后缀/大小/字符集）。

**必须改写**：opkg→tar 解压；init.d 注册→appmgr 进程管理；C++/lws→Python asyncio；鉴权→nginx auth_request；打包 arch/后缀；前端 token key + baseURL。

**可删除**：VPSS/相机独占切换、`drop_dentry_cache` overlay 补丁、`external-firmware` 类型（除非要）、opkg 依赖处理。

## 6. 落地步骤（分阶段 MVP）

- **M1 打通链路**：appmgr 骨架(list/install/uninstall/start/stop) + ext_appmgr.conf + S94 自启动；手动 scp 一个 tar 包，验证装→起→出 WS 结果→卸。
- **M2 前端**：移植 `views/applications` 成独立 SPA，接 Pro token，落 /appcenter/。
- **M3 云端**：Pro catalog.json + 浏览器代取（catalog.ts 移植）+ sha256 校验。
- **M4 发布链**：release-app.py 适配 tar/arm64 + catalog 生成 + 护栏。
- **M5 硬化**：安全(§4.9 验签/zip-slip/兼容 gate)、OTA 回注(含 nginx 入口)、进程看护(进程组/退避/孤儿认领)、切换回滚、日志轮转/健康检查/资源限额、升级降级回滚、断电恢复。

## 7. 待验证（决策已定，剩下的是需上机实测的项）

> 语言(Python)、并发(单 active)、UI(可嵌入独立 SPA) 已拍板，见 §0 决策表。以下为实施前需在设备上确认的技术点：

1. **root 权限**：appmgr 需以 root 跑（管进程、写 /userdata 外）；确认 `S94appmgr` 由 init 以 root 启（当前所有服务均 root，预期 OK）。
2. **OTA 是否真的洗 /etc**：需一次 OTA 实测确认「/userdata 回注 hook」生效；若 OTA 也保留 /etc/init.d 则回注可简化。
3. **单 active 下的相机语义**：确认 user app 停止后，官方 recamera_notify 的推理是否继续/是否受影响（两者都只是 rkipc 的流消费者，预期互不干扰，需实测 :8123 结果流）。
4. **共享流接入约定**：user app 读流的标准姿势——go2rtc RTSP 拉流 vs 直接接 rkipc VI 通道——需定一个 SDK 约定并实测延迟/占用。
   - **视频**：go2rtc 现有两路 `rtsp://127.0.0.1:5554/live/0`(main) 与 `/live/1`(sub)，app 走 RTSP 拉流即可。
   - **音频（更正：mic 被 rkipc 独占，非空闲）**：RV1126B 内置 acodec，`/dev/snd/pcmC0D0c`(mic) + `pcmC0D0p`(播放) 在，`arecord`/`aplay` 已装。**但 rkipc `[audio.0] enable=1` 用 `RK_MPI_AI` 独占了麦克风**（2ch/S16/22050Hz，还带 VQE AEC/降噪/AGC）→ **app 直开 ALSA 会冲突**，且直开拿到的是原始麦、丢了 VQE。对外现只有 G711A 8kHz(RTSP)/MP3(录像)，**不适合 STT**。→ 干净音频（尤其 STT）需要官方开 **PCM 出口**（见 SDK PRD 的 **R8 音频代理**）；短期绕法：若要独占 mic，appmgr 需先停 rkipc 的音频（影响 RTSP 音轨），代价大。**STT 类应用强依赖 R8**。平台自带 SED/AED/唤醒资产可作音频+NPU 参考。
5. **RKNPU 占用（已初步实测）**：RV1126B **单 NPU 核**（device-tree 仅 `npu@22000000`），多 RKNN context **时间片串行、非真并行**——不会崩，但**抢算力致双方降速/掉帧**。曾观测 NPU load **69%**（官方栈常驻推理，非 root 读不稳定，需 M1 用 root 采 `/sys/kernel/debug/rknpu/load` 定标）。**内存不是瓶颈**：2GB 总量、1.3GB 可用，NPU 用共享 DDR 无独立显存，加载额外小模型无压力。→ 结论：给 user app 设 **NPU 算力配额**（非内存配额），实测"官方 + user"合计负载余量。
6. **登录态复用细节**：确认 `sensecraft_token` 的刷新/过期机制，standalone SPA 需与官方一致处理 401 跳转。

## 8. codex 评审整改清单（round 1）

评审时间 2026-08-08。状态：✅=已并入上文设计 / 🔬=需上机验证 / 📌=实施期落地。

### 致命问题（已在设计层修正）
- ✅ **SPA 不能挂 auth_request**（首屏 401 跳不动登录）→ §4.2 改为静态匿名、只 API 鉴权；§4.5 改为 SPA 首个 API 401 再跳。
- 🔬 **nginx include 冲突/加载顺序/alias 路径** 未验证 → §4.2 增"上机必验"：完整合成配置跑 `nginx -T/-t` + 真 token 打 `/api/appMgr/list` 验 `/_jwt_verify` 语义。
- ✅ **OTA 洗 /oem 会带走 nginx 入口**（原设计只备份 S94）→ §4.7 三样(代码/S94/nginx conf)全纳入回注，S94 start 时保底回注 nginx conf。🔬 回注 hook 自身存活需实测 OTA 后 RkLunch 链是否仍执行。
- ✅ **安装=root 代码执行**（后端不验签、tar 不防 zip-slip）→ 新增 §4.9：后端强制重算 sha256 +（建议）发布方签名验签、tar 逐成员防绝对路径/../符号硬链接/设备文件/炸弹、manifest 与 target_path 白名单、run 脚本降权/cgroup。

### 值得改进（已并入）
- ✅ **单 active 未解决资源争用**（官方 notify 始终并行）→ §2 增资源边界/配额/故障隔离说明 + §4.4 cgroup + §7 NPU 实测。
- ✅ **进程看护过简**（无前台契约/进程组/僵尸/TERM→KILL/PID 复用/退避/日志/孤儿认领）→ §4.4 补齐进程看护契约。
- ✅ **asyncio.Lock 语义不足**（防不了重复 appmgr 实例）→ §4.1 改为 flock 单实例锁 + try/finally busy-gate + 事务状态机。
- ✅ **switch 失败无回滚 / 升级无版本保留** → §4.4 补原子性与回滚、`<id>/versions/<ver>` + current 符号链接、fsync、断电恢复。

### 遗漏项（已补进设计/步骤）
- ✅ 审计日志、stdout/stderr 轮转、健康检查/watchdog、资源限额、启动超时 → §4.4/§4.9/§6 M5。
- ✅ 原地升级/降级/失败回滚/卸载残留/断点续传/临时文件回收/磁盘满/断电中断恢复 → §4.4 + §6 M5。📌 断点续传/并发上传属前端 catalog 层，实施期落地。
- ✅ 后端兼容性校验（固件版本/aarch64 ABI/RKNN runtime/模型兼容/target_path 边界）→ §4.9 第 5 条。
- 📌 **前端可嵌入三约束仍不够**（缺 router basename、静态资源基址、CSS/React 依赖隔离、挂载/卸载契约、主题/i18n、CSP、token 刷新+统一 401、API 版本协商）→ 已记入 §4.5 精神，具体在 M2 实施时逐条落地。

### codex 认可放行
- Python sidecar / 仅监听 loopback / 同源 API / 数据全落 /userdata —— 方向符合 Pro 闭源 dashboard + 无包管理器约束。
- rkipc 保留相机所有权、user app 消费 go2rtc 流、单 active 定义为策略而非硬件限制 —— 概念成立。
- 自包含前端模块 + 鉴权 provider + 固定相对 API 契约 —— 未来嵌入的正确起点。
