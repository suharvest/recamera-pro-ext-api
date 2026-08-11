# reCamera Pro 推理扩展能力 — 用户分析与产品建议

> **文档性质**：内部分析。回答一个问题——**reCamera Pro 的软件为什么必须改成"可扩展"的形态，改哪里。**
> **依据**：两轮验证。① 在 V1.0.10 闭源固件上做完的一轮完整 PoC——自建应用中心 + 9 个视觉示例应用（检测/姿态/人脸/OCR/二维码/摔倒/健身/零售）+ 一个「唤醒→采集→STT」语音应用，并把 SenseVoice/Whisper 打通到 RKNPU（rv1126b / rk3576 / rk3588 三芯片实测）。② 2026-08-10 拿到固件源码（`recamera_v2` manifest，82 仓库）后做的源码实证核对——本文所有「现状」结论已逐条对照源码修订，锚点见文末附录。
> **前提修正（2026-08-10）**：初版写作时固件对我们也是闭源的，只能逆向。现在源码可得——**"闭源"从此只对方案商成立，对我们是可自由修改的自有代码**。这不改变第一~四章的用户与需求分析，但第五章的障碍定性、第六章的实现路径与工作量，全部按源码事实重写。

---

## 结论

1. **缺什么**：换个**单模型**（输出结构匹配官方后处理注册表内的 9 种算法）今天已经能跑；但方案商交付的是**方案**——一旦要多模型级联、自定义后处理、有状态逻辑，就既**接不进**官方的相机/叠加/分发（帧拿不到、结果进不了叠加和录像），也**调不动**自己的模型（转换静默失败、推理内部全黑盒）。
2. **加什么**：在**进程边界**上开三条通用契约——**帧 / 结果 / 音频**，再补两套接入能力——**模型上板验证链路 / 推理可观测数据**，外加沙箱与分发。源码核对后的新事实：这批能力中**约一半已有存量实现或预留位**（notify 入站 socket、`ai_asr` 预留 PCM、`ext_*.conf` 前端挂载、后处理注册表、双向 RPC socket），要做的是**把存量整理成版本化契约 + 补齐真缺的帧代理与观测**，比初版估计的工作量小（转换本身仍归 Rockchip 的 rknn-toolkit2，我们只补它管不到的那一半，见第四章）。
3. **为什么值得**：补完这些，Pro 就从"带几个内建模型的相机"变成**方案商的调试底座 + 交付外壳**——前期调试不必自建界面，最终交付直接用设备里现成的东西。这是 Pro 相对裸模组的核心差异，也是目前兑现度最低的一块。

---

## 一、用户是谁

**Pro 的决策用户不是终端用户，是方案商。** 终端用户（工厂、门店、园区）买的是"方案"，方案商才是决定用不用 Pro 的人。后面所有结论都建立在这个判断上。

方案商的三条画像特征：

| 特征 | 含义 | 对平台的推论 |
|---|---|---|
| **模型是他们的** | 各行业自训/采购的模型：新检测架构、关键点、OCR、ASR、异常检测…… | 不能假设"模型来自官方模型库"，不能假设"后处理是某几种枚举" |
| **流水线是他们的** | 单模型 / 检测→识别级联 / 多模态融合 / 带注册库和规则的有状态逻辑 | 不能假设流程长成我们人脸识别那样 |
| **后端系统可能是他们的** | 结果要进他们自己的云 / MES / 告警系统 | 不能假设结果只走我们这套 WS/MQTT |

**推论：平台的价值不在"多给几个模型和流程"，而在"开放稳定的通用能力，让他们把自己的东西接进来、并且调得通"。** 判据是对任意第三方模型和流水线都成立——官方内建推理应当是这套通用能力的一个消费者，而不是特权路径。

方案商按投入度分两档，需求形态不同：

- **Tier A｜轻量**：只想换个模型、复用官方全部流程，零/少代码。**这条路今天基本已经通了**（前提是模型输出结构落在官方后处理注册表覆盖的算法内，详见 5.1）。
- **Tier B｜重度**：自带运行时和多模型级联，只借官方的相机、出流、叠加、UI、分发。**我们自己的 PoC 全部属于 Tier B，也是 Pro 差异化的主战场，而这条路今天是断的。**

他们为什么选 Pro：RV1126B + 3 TOPS 算力够；相机/ISP、go2rtc 出流、OSD 叠加、Web dashboard、模型管理开箱即用，不想重造；Seeed 面向开发者且有「应用中心」方向。**核心诉求**：在 Pro 上跑自己的流水线，最大化复用官方基础设施，**不碰固件源码（他们也拿不到），且能随官方 OTA 存活**。

---

## 二、我们对他们的价值（以及兑现它的前提）

方案商可以拿一块裸的 RV1126B 模组自己搭。选 Pro 的理由是 **Pro 已经把"模型之外的所有东西"做好了**。这个价值分两个阶段兑现。

### 2.1 前期：**零成本调试底座**

方案商上手阶段要验证的只有一件事：自己的模型在这块板子上行不行。为此需要视频播放、结果叠加、日志查看、远程登录——这些与模型无关，但不搭起来就看不到结果。

**Pro 能给的**：开机就有相机、ISP、编码出流（go2rtc/RTSP/WebRTC）、OSD 叠加、Web dashboard、ttyd 终端、系统日志 WS、快照录制、文件存储、模型管理。**方案商只需写模型和业务逻辑，调试台无需自建。**

对方案商的意义：试板成本从"两周搭环境"降到"一天跑通"。

### 2.2 后期：**零成本交付外壳**

交付给终端用户时是第二笔成本：终端用户要看的界面、要录的像、要推的流、要装的包、要升的级。这部分自建相当于再做一个产品。

**Pro 能给的**：结果直接烧进编码流和录像（终端用户在任何播放器上都看得到框和标签）、官方 dashboard 直接当终端用户界面、应用中心一键装、官方 OTA 管升级。**方案商不用做前端，也不用做运维后台。**

对方案商的意义：交付物从"一套系统"缩成"一个 app 包"。

### 2.3 两个阶段是同一条路（差异化所在）

单看"调试阶段有工具"或"交付阶段有界面"，别家平台也有。**Pro 的区别在于两个阶段用的是同一套东西**：调试时看到的那路视频、那个叠加、那个结果流，交付时原样就是终端用户看到的。方案商不需要维护调试和上线两套环境，也不会出现"调试能跑、上线不对"。

### 2.4 当前兑现情况

上述能力都有一个隐含前提：**方案商自己的模型和结果能进得去这套东西。** 这一半目前是缺的。

下表以**方案商自带流水线**（Tier B）为准。**纯 Tier A"换个同架构模型"的场景不适用**——那条路今天走得通，官方推理会把结果画进叠加和录像（边界见 5.1）。但方案商交付的是方案不是模型，绝大多数落在 Tier B。状态按源码核对后的三档标注：✅ 已齐 / ⚠️ 有存量但未成契约 / ❌ 真缺。

| 我们承诺的价值 | 兑现它必须提供什么 | 现状（Tier B 视角，2026-08-10 源码核对） |
|---|---|---|
| 前期：**不用自己搭视频/界面** | 外壳（播放器/终端/日志/快照/录制） | ✅ 已齐 |
| 前期：**能拿到帧喂自己的模型** | 原始帧出口 | ❌ VI 独占，只能解码编码流（VI 有空余通道位，但无对外接口） |
| 前期：**自己的结果能显示在官方视频上** | 结果入站 → OSD/录像 | ⚠️ 分发面已通（`/var/tmp/notify` 0666 入站，可进 WS/MQTT/HTTP/UART）；**OSD 叠加与录像回注仍缺** |
| 前期：**看得到自己模型内部为什么不出结果** | 推理中间数据观测 | ❌ 全黑盒 |
| 前期：**能用固定输入做可复现回归** | 测试输入注入 | ❌ 只能吃实时相机 |
| 前期：**自己的模型能顺利转上板** | 转换配方 + 验证 harness + 芯片矩阵 | ❌ 无，且失败静默 |
| 后期：**结果进编码流和录像** | 服务端 OSD 渲染第三方结果 | ❌ 只画内建模型（渲染函数已有，缺外部入口） |
| 后期：**dashboard 直接当终端用户界面** | 官方 UI 可显示/可挂载第三方内容 | ⚠️ `ext_*.conf` + `/extension/<name>/` 挂载约定已在用（acousticslab、alpkg 两例）；官方组件复用、主界面注入仍缺 |
| 后期：**一键装、能升级** | 打包签名 + Catalog + 版本化协议 | ⚠️ 有 `.alpkg` 包格式（ZIP32+CRC32+origin 白名单），但无签名验签/版本管理/卸载，且面向 acousticslab 而非通用 app |
| 后期：**多 app 共存不互相拖垮** | 沙箱 + NPU/相机配额 | ❌ 无（seccomp/cgroup 全树 0 命中，IPC 端点一律 0666） |
| 后期：**扛得住官方 OTA** | 稳定版本化的配置 API | ⚠️ `entry.cgi` 源码在手（18 个 API 域、集中路由表），可版本化；尚未做版本化与 app 作用域 |

左列是卖点，右列是该卖点当前能否兑现。11 项中 1 项成立、4 项有存量、6 项真缺。

> **外壳装得进一个模型，装不进一套方案。** 换个注册表内算法的模型没问题；但一旦涉及级联、自定义后处理、有状态逻辑——即方案商真正交付的内容——前期调试和后期交付两个价值同时失效，只能退回绕路自建。而绕路自建之后，Pro 相对裸模组不再有价值。

**后面几章的所有接口只服务于一件事：把 2.1 和 2.2 从"Demo 成立"变成"方案商自带模型也成立"。**

---

## 三、他们会做什么

方案商在 Pro 上的真实行为路径（我们 PoC 完整走了一遍）：

```
选型试板 → 转模型上板 → 接流水线 → 调试定位 → 接自己后端 → 打包分发 → 现场迭代
             ↑ 最大流失点              ↑ 第二流失点
```

1. **选型试板**：跑通一个 demo，判断算力/接口够不够。官方现有能力已覆盖。
2. **转模型上板**：把自己的 ONNX 转成 rknn 跑起来。**这是当前最大的流失点**——失败大多是静默的（能转成、能 load、能 infer，但结果全错或全空），没有报错指向根因。（这个不在 recamera pro 设备上）
3. **接流水线**：拿帧 → 自己的多模型级联 → 自定义后处理 → 有状态逻辑（注册库/规则）。
4. **调试定位**：出问题要看每一级中间数据。**第二流失点**——现在完全黑盒。
5. **接自己后端**：结果推去他们的系统。
6. **打包分发**：交付终端用户，要能一键装、能远程升级。
7. **现场迭代**：换模型、调参数、做回归，还要扛得住官方 OTA。

由此收敛出四个驱动场景：

- **S1 自定义多模型流水线**（人脸识别为代表）：自带运行时，复用相机/叠加/UI。
- **S2 调试与回归面板**：复用官方界面做联调和回归测试（详见 `DEBUG_PANEL_REUSE.md`）。
- **S3 应用分发**：打包上架应用中心，终端用户一键装。
- **S4 语音/多模态**：唤醒 → 采集 → STT，可与视觉结果融合、经扬声器 TTS 回应（设备定位本就含 STT/TTS + 双麦）。

---

## 四、他们需要什么

前七条是"把自带的东西接进官方那条路"，后四条是"让自带的东西转得出、跑得通、调得动"。两类**同等重要**，过去的讨论只覆盖了前者。

| 能力 | 用户诉求 | 形态 |
|---|---|---|
| **帧输入** | 我的进程要拿到原始帧，不想为了拿图多做一次编解码 | 零拷贝 dma-buf 扇出，多订阅共享一路相机 |
| **结果回注** | 我算出的"人名/自定义框"要能画进官方视频、进官方结果流、进录像 | 入站结果 API，复用同一份 `InferenceResult` protobuf |
| **音频输入** | STT 要干净 PCM（16k 单声道），且要带官方 VQE 的降噪回声消除 | PCM 扇出，与帧扇出同构 |
| **运行时自由** | 我要自己 dlopen rknn、自己串级联、自己管状态 | Tier B「自带运行时」；Tier A 用声明式/插件后处理兜底 |
| **稳定配置面** | 推理开关/模型/FPS/OSD/打码/推流/网络/固件要能稳定调用 | 现有 `entry.cgi` 能力文档化 + 版本化 + app 作用域 |
| **资源边界** | 官方要敢放开，我要不被别的 app 拖垮 | NPU 配额、非 root 沙箱、supervisor 生命周期 |
| **分发信任** | 终端用户一键装，我要能跨固件升级存活 | 清单 + 签名包 + Catalog + SDK |
| **模型上板验证** | 我的模型要能可预期地转上板，出问题要有报错而不是静默失败 | 分任务转换配方 + host/板对齐 harness + 芯片能力矩阵 + 端侧 bring-up 自检 |
| **可调试数据** | 看到预处理后**实际喂进 NPU 的那张图**、模型原始输出、级联每跳中间结果、分级耗时与丢帧 | 中间结果按需旁路上报 + 预处理回显 + 分级 metrics |
| **测试输入注入** | 拿静态图/离线视频当帧源，做可复现回归 | 帧代理的反向 file-source 模式 |
| **可复用外壳** | 官方的视频播放器/终端/日志/快照能嵌进我自己的面板 | 外部 tab 挂载点或稳定内嵌组件 |

两点说明：

- **"模型上板验证"不是要我们重做转换器。** ONNX→RKNN 由 Rockchip 的 `rknn-toolkit2` 在主机上完成，这段没问题。但 toolkit 只给参数位不给答案（`mean_values` 填 `[0,1]` 还是 `[-1,1]`、检测头要不要改导原始多分支、Transformer 末层要不要插 scaling，它一概不管且**填错不报错**）；而"板上跑出来对不对"这半段依赖设备形态（toolkit 的 `accuracy_analysis` connected 模式走 adb，reCamera 是 Buildroot + SSH，接不上），**只能由我们提供**。要交付的是**配方库 + 端侧验证代理 + 芯片能力矩阵**，工作量在文档和一个小端侧工具，不在算法。
- **调试面板的价值不在外壳，在数据。** 外壳官方已给齐（视频、鉴权、ttyd:7681、系统日志 WS:8765、快照、录制、结果流 WS:8123），缺的是推理内部的可观测性。

---

## 五、我们现在缺什么

### 5.1 边界：今天已经能做的部分

现状不是"自带模型完全接不进"，而是"简单的接得进，复杂的接不进"：

| | 今天的状态 |
|---|---|
| ✅ **单模型 + 输出结构落在官方后处理注册表内**（yolov5 / yolov8 / yolov10 / yolox / nanodet / detr / yolo_world / classify / yolov8_pose，共 9 种，源码核对） | **可用。** 自训的对应架构权重转成 rknn 上传选中，即可复用官方推理、OSD 叠加、结果流。这是 Tier A 的适用范围——**比初版文档写的 5 种枚举更宽**（detr / yolo_world / yolov10 已在注册表内）。 |
| ⚠️ **单模型，但输出结构不匹配注册表内任何算法**（新检测架构、自定义头、非常规任务） | **接不进（对方案商）。** 后处理是编译期字符串注册表（`PostProcessFactory` + `REGISTER_POSTPROCESSOR` 宏）——对我们，加一种算法 = 新增一个 .cpp + 一行注册宏 + 重编 rkipc；对方案商仍是黑盒：无声明式描述、无运行时插件位（业务代码 dlopen 零命中）。 |
| ❌ **多模型级联 / 自定义后处理 / 有状态逻辑 / 多模态** | **完全接不进。** 这类应用必须自带运行时（Tier B），而 Tier B 需要的帧入口今天不存在，结果回注只通了分发面（进不了 OSD 和录像）。 |

缺口的准确表述：Tier A 的"换个模型"已经成立，**断掉的是从"换个模型"到"做一个方案"之间的那一段**——而方案商交付的是方案。

我们 PoC 的 9 个应用里，**只有 `yolo-detector` 落在第一行**；其余 8 个（face-analysis、facemesh-reader、fall-detection、fitness-trainer、ppocr-reader、qrcode-reader、retail-vision、voice-transcribe）都因级联、自定义后处理或有状态逻辑而必须走 Tier B。

第一行的"可用"另有一个前提：模型得先正确转上板。转换环节的静默失败对三行一视同仁，见 5.3。

### 5.2 结构性障碍（V1.0.10 实机验证 + 2026-08-10 源码核对）

> 初版把下表定性为"闭源导致的死结"。源码核对后重新定性：**对我们全部是可实现的工程任务；对方案商，在我们把 API 做出来之前，下表依然全部成立。** "fork 固件"从来不是方案商的选项（他们没有源码），真实的选项只有"绕路自建"或"不用 Pro"。

| # | 障碍（初版表述） | 源码核对结论 | 对实现的影响 |
|---|---|---|---|
| 1 | 主 API `entry.cgi` 闭源二进制，前端闭源 minified bundle | **不再成立（对我们）**。entry.cgi 源码在 `recamera_web_backend/`（C++/cgicc/FastCGI/jwt-cpp，集中路由表 `rest_api.cpp:156-243`，18 个 API 域）；前端是完整 CRA 源码 `recamera_web_react/`。**对方案商仍闭源**——所以版本化 API 必须由我们做。 | 加 API 域 = 路由表加两行 + 一对 `*_api.{h,cpp}`。另有两个存量利好：nginx `include ext_*.conf` 挂载约定已被使用；localhost 请求默认跳过 JWT（`rest_api.cpp:69-72`），本机扩展调 API 零门槛（正式化时应换成 app 作用域 token）。 |
| 2 | 推理是 rkipc 内建 + **固定后处理枚举**（yolox/yolov8/nanodet/classify/pose） | **不成立**。实际是运行时字符串注册表（`rc_infer/include/postprocess/base.h:110-224`），已注册 9 种算法（含 detr/yolo_world/yolov10），另有独立跟踪器工厂（ByteTrack/IoU/Kalman 全源码）。**保留的限制**：注册在编译期，无 dlopen 插件位，方案商无法自带后处理。 | 我们加算法 = 加文件重编；给方案商开放 = 新增 dlopen 加载器（约百行）+ 崩溃隔离设计，或声明式 json 描述。 |
| 3 | rkipc **独占 VI/ISP，不对外供帧** | **成立**。NPU 走 `RK_MPI_VI_GetChnFrame(chn4)` 直取（`video.c:580-586`），拿的是**虚拟地址**（`Handle2VirAddr`）；`RK_MPI_MB_Handle2Fd` 在 recamera_ipc **零命中**（只在 uvc_app_tiny 等旁路 demo 里出现）。无任何对外帧接口。 | 初版"fd 已现成只差暴露"的假设不成立——帧代理是**新增子系统**（fd 导出 + SCM_RIGHTS 扇出 + refcount 池），不是改几行。利好：VI 支持多 chn（现用 0/2/3/4/5），同 pipe 可再开一路，不必抢现有通道。 |
| 4 | 结果通道**只出站**（protobuf → WS:8123），无入站 | **部分不成立**。出站三路分发确认（`video.c:648-657` → vigil 录像 / rc_notify / OSD）。**入站已存在两条**：① `/var/tmp/notify` 是 0666 无鉴权 AF_UNIX 服务端（`socket_server.py:84-90`），任意本机进程写 `<le32 len><InferenceResult>` 即注入 WS/MQTT/HTTP/UART 全部 notifier；② rkipc 自带双向 RPC socket `/var/tmp/rkipc`（`socket_server/server.c`，entry.cgi 就是它的客户端）。**仍缺**：外部结果进不了 OSD 叠加和录像——`osd_manager_draw_infer()` 只从内建推理路径调用。 | 分发面回注 = 零改动可用（按 proto 打包写 socket）。OSD/录像回注 = 在 rkipc 加一个入站口，把 `video.c:648-657` 的三路分发改成同时接受外部来源——改动中等偏小，渲染函数已有。 |
| 5 | 无第三方 app 的**配额与沙箱** | **成立**。`seccomp`/`cgroup` 在 `project/app` 全树 0 命中；所有服务 SysVinit 裸跑；关键 IPC 端点（`/var/tmp/notify`、`/dev/shm/*.sock`、ALSA dsnoop）一律 0666，本机任意进程可读写。 | 从零做。且 0666 现状既是接入便利也是安全债——做沙箱前要先把 IPC 收紧成按 app 授权。 |
| 6 | 无官方**打包/签名/分发**机制（仅内部模型目录） | **部分成立**。存量：`.alpkg` 包格式（ZIP32+CRC32 自校验+origin 白名单，`alpkg/src/`，带测试）+ `ext_*.conf`/`/extension/<name>/` 挂载约定（acousticslab、alpkg 两个实例）。模型管理比预想完善：`/userdata/config/model/<framework>/` + `.info` JSON + 断点续传上传 + md5 校验 + 云端拉取 API（`rc_model.h`）。**仍缺**：签名验签、版本/依赖管理、卸载、通用化（alpkg 面向 acousticslab 不是通用 app）。 | 不是从零造——把 alpkg 泛化 + 加签名层 + Catalog。 |
| 7 | rkipc **独占音频采集**（`RK_MPI_AI`），对外只有 G711A 8kHz | **大部分不成立**。ALSA 层已做硬件共享：`dsnoop`（`ipc_perm 0666`）共享 6ch@16kHz 硬件，暴露 4 个命名 PCM——`ai_main`(rkipc) / `ai_kws`(acousticslabd 在用) / **`ai_asr`(预留空闲)** / `ai_debug`(预留空闲)（`overlay-buildroot-asound/etc/asound.conf`）。`arecord -D ai_asr` 即可拿 PCM，零改动。AED/BCD 事件已实际启用（`audio.c:464-506`，1Hz 轮询）但结果只写 LOG_DEBUG。**仍缺**：带 VQE（AEC/NS）的 PCM 不对外——VQE 在 rkipc 的 `RK_MPI_AI` 链内，dsnoop 分出来的是原始麦克风；rkipc 内无 VAD/唤醒（KWS 在独立进程 acousticslabd，源码在另一 repo）。 | 拿原始 PCM = 零改动（`ai_asr`）。AED/BCD 结果外发 = 改十几行接 rc_notify。VQE-PCM 对外 = rkipc 加导出口，中等。 |

初版由此推出"方案商被逼到 fork 固件或绕路自建二选一"。修正后的表述：**方案商的唯一出路是绕路自建（或放弃），而我们现在有源码，可以把每一条都做成正式 API，让绕路变成正路。**

### 5.3 隐性障碍：模型上板与调试是黑盒

这类问题不表现为"某个接口不存在"，但影响更大。我们作为有背景知识的内部团队，在**每一个**模型上都为此耗费了可观精力。实测撞到的静默雷区（**官方文档和工具零覆盖**）：

- **量化静默塌陷**：检测头 concat 输出在 INT8 下，类别分数被坐标量程主导 → 全 0 零检出（须改导原始多分支头）。Transformer 在 fp16 激活溢出 → logits NaN → 空输出（须末层插 scaling）；INT8 w8a8 同样崩，须 w4a16。
- **每模型归一化各异**：YOLO=[0,1]、MediaPipe=[-1,1]、分类器=ImageNet、OCR-rec=[-1,1]。烘错**不报错，模型直接"死"**。
- **跨芯片不一致**：同一模型 rv1126b/rk3576 认 w4a16、**rk3588 拒 w4a16**；`init_runtime(core_mask)` 单核（rv1126b）会报错、多核才需要。**同一份端侧代码跨芯片会崩。**
- **工具链版本敏感**：`onnx` 须钉特定版本、`target_platform` 串、`librknnrt` 路径与 root 权限，任一不对就转不出或跑不动。
- **黑盒调试的代价**：PoC 里我们因设备 libjpeg 把测试图静默截断成黑图，误判"模型坏了"，耗掉半天。**有一个"回显实际喂进 NPU 的图"的观测口，这是 30 秒的事。**

**共同点：失败是静默的，且每一个自带模型都会遇到。** 不解决，方案商在行为路径第 2 步就流失，后续接口开放与否对他没有意义。

（源码核对补充：这一节的问题全部在主机侧转换环节或跨芯片行为上，**源码可得不改变本节任何结论**——rknn-toolkit2 与 librknnrt 仍是 Rockchip 闭源。）

### 5.4 次要缺口：打码 ≠ 不识别

安防/隐私场景要"某区域不参与识别"。实测：打码（cover/mosaic）挂在**输出侧 VPSS/VENC 通道**，NPU 从**另一路** VI chn 取帧 → **打码区域 NPU 照样检测**。`rk_roi_*` 是编码质量 ROI、`regional_invasion` 是检测 include 区，没有一个是"这块别检测"。Tier B 可在自己后处理里做多边形过滤绕开（我们已采用）。（源码核对更新：现在可以直接在 `rc_infer` 后处理链里加引擎级忽略区多边形过滤，Tier A 也能覆盖，见 6.2 P2。）

---

## 六、建议

### 6.1 五条设计原则

1. **内外分层：源码内实现，对外只暴露契约。** 我们在 rkipc / web_backend 源码里正经实现能力（不再旁挂、不再逆向）；方案商只接触进程边界上的版本化契约。内部实现可自由演进——方案商拿不到源码这件事，从缺陷变成了我们可以大胆重构的前提。
2. **契约定在进程边界上，且版本化**（unix socket + protobuf schema）。第三方跑自己的进程，不需要 link 进 rkipc；官方升级不破坏它，**方案商永远不需要（也不可能）fork**。
3. **内外对称：同一套帧语言（dma-buf）+ 结果语言（`InferenceResult`）。** 好处是双向的——第三方结果天生是一等公民（能上官方叠加和 WS）；某个第三方流水线成熟后，官方可**零接口改动**收进基座。源码核对证实这条路可行：出站三路分发（vigil/notify/OSD）已经共用同一份 proto，入站复用它即可。
4. **复用优先于替换**：相机、出流、叠加、dashboard、模型管理全复用，第三方只加"模型 + 逻辑"。存量盘点后这条更有底：`ext_*.conf` 挂载、notify 入站、`ai_asr`、模型断点续传上传全是现成的。
5. **仲裁与沙箱是平台的责任**，不是第三方的自觉。这是官方敢放开的前提。当前 IPC 端点一律 0666 是反例——接入方便，但"坏 app 拖垮基座"和"任意进程注入结果"是同一扇门。正式化时把 0666 收成按 app 颁发的 socket 凭据。

### 6.2 增量清单（按源码核对后的真实起点重估）

| 能力 | 现状（源码核对，V1.1.2） | 要补 | 工作量定性 | 优先级 |
|---|---|---|---|---|
| **帧代理** | ❌ 无对外帧接口。NPU 走 VI chn4 直取虚拟地址（`video.c:580-586`）；`Handle2Fd` 在 rkipc 零命中（仅旁路 demo 用过）；VI 有空余 chn 位 | 新增子系统：同 pipe 开一路 VI chn → `Handle2Fd` 导 fd → `SCM_RIGHTS` 扇出 socket + 定长头 + refcount 生命周期 + 慢消费者丢帧策略 + 独立 buffer 池 | **新增子系统**（初版"暴露已有能力"的估计过于乐观） | **P0** |
| **结果注入** | ⚠️ 分发面已通：`/var/tmp/notify` 0666 入站直达 WS/MQTT/HTTP/UART；出站三路分发 `video.c:648-657`；OSD 渲染函数已有，缺外部入口 | ① 把 notify 入站正式化（鉴权 + `source_id`/`pts` 字段）② rkipc 加入站口，外部 `InferenceResult` 走同一条三路分发（进 OSD + 录像 + WS） | ①零改动起步，②**改几处已有分发点** | **P0** |
| **模型上板验证链路** | ❌ 只有上传/选中（上传已带断点续传+md5，`model_api.cpp:219-260`），无自带模型的配方/验证/芯片指南 | ① 分任务转换配方库 ② host vs 板对齐 harness（把静默失败变显式报错）③ 芯片能力矩阵 ④ 端侧 bring-up 自检 | 文档 + 小端侧工具（不受源码影响，照原计划） | **P0** |
| **可调试数据** | ❌ 推理内部全黑盒；但现在可直接改 `rc_infer` 流水线 | 张量/中间结果按需旁路上报（带 stage_id，默认关）+ 预处理回显 + 分级 metrics + 回归夹具 | 源码内加旁路 tap，**中等**（不再需要黑盒外猜） | **P0** |
| 音频代理 | ⚠️ 原始 PCM 已对外：`ai_asr`/`ai_debug` 预留 PCM 空闲（dsnoop 共享，零改动可用）；AED/BCD 已启用但结果只落 LOG_DEBUG（`audio.c:556-582`）；VQE-PCM 不对外；rkipc 无 VAD（KWS 在 acousticslabd 独立进程） | ① `ai_asr` 用法文档化（即刻可交付）② AED/BCD 结果接 rc_notify（十几行）③ VQE-PCM 导出口（与帧扇出同构） | ①②**改几行/纯文档**，③中等 | P1 |
| 沙箱与配额 | ❌ 全无：seccomp/cgroup 0 命中、SysVinit 裸进程、IPC 全 0666 | NPU 预算声明 + 时分调度（保视觉稳帧率）+ 非 root cgroup/seccomp 沙箱 + supervisor 生命周期 + 推理让渡 + **IPC 端点从 0666 收紧为按 app 凭据** | 从零造（多模型并发已实测可行，单核时分下视觉吞吐掉 ~2/3） | P1 |
| 打包分发 | ⚠️ 存量：`.alpkg` 格式（ZIP32+CRC32+origin 信任，带测试）+ `ext_*.conf`/`/extension/` 挂载约定（两个实例在用）+ 模型云端拉取 API | 泛化 alpkg 为通用 app 包 + 签名验签 + 版本/依赖/卸载 + Catalog + 清单 schema（tier/模型/帧需求/NPU 预算/能力/UI 入口）+ SDK | **泛化 + 补签名**，不是从零造 | P1 |
| 配置 API | ⚠️ `entry.cgi` 源码在手：18 API 域、集中路由表（`rest_api.cpp:156-243`）、JWT 鉴权在 CGI 内、localhost 直通已存在 | 文档化 + 版本号 + app 作用域 token（替代 localhost 直通）+ 加新 API 域的贡献规范 | 源码内**工程任务**（不再是逆向） | P1 |
| 后处理开放 | ⚠️ 编译期字符串注册表已有 9 算法（`postprocess/base.h:110-224`）+ 独立跟踪器工厂，无 dlopen | ① 我们侧：按需求加算法（加文件+重编，常态化）② 方案商侧：声明式后处理（json 描述 anchors/layout/decode）+ 插件 ABI（独立进程或 dlopen + 崩溃隔离） | ①**加文件级**；②中等（注册表架构现成，加载器约百行 + 稳定性设计） | P2 |
| 测试输入注入 | ❌ 只能吃实时相机 | 帧代理反向 file-source 模式（覆盖 Tier A 与"喂官方 NPU 通道"） | 依附帧代理子系统 | P2 |
| 推理忽略区 | ❌ 打码在 VENC 输出侧与 NPU 通道分离（实测确认） | `rc_infer` 后处理链加引擎级忽略区多边形 + 「mask 是否同时对推理生效」开关 | 源码内**小改动**（初版只能靠 Tier B 自己绕） | P2 |
| 可嵌入 UI | ⚠️ 前端是完整 CRA 源码（`recamera_web_react/`）；`/extension/<name>/` 挂载约定已在用 | ① 扩展挂载约定文档化（即刻可交付）② 官方组件（播放器/终端/日志）抽成可嵌入形态或稳定 iframe 契约 | ①纯文档；②中等（不再是"注入不进"） | P2 |

三点说明：

- **帧代理和音频代理（VQE-PCM 部分）是同一个设计的两种介质**，共用"扇出 + refcount + 隔离池"。原始 PCM 已由 ALSA dsnoop 解决，不必等这套。
- **P0 里"模型上板验证链路 + 可调试数据"与"帧/结果契约"同等关键。** 后者让自带的东西接进官方路，前者让它转得出、跑得通、调得动。只做后者，方案商在行为路径第 2 步流失，接口无人使用。
- **每条"⚠️ 存量"都应先文档化再增强。** `ai_asr`、notify 入站、`ext_*.conf` 挂载今天就能用，只是没人知道——把它们写成官方文档是零开发成本的第一批交付物。

### 6.3 对外 API 设计要点（性能 × 易用度）

> 本节是 6.2 各条的横切设计约束——先设计，不开发。目标：**性能上限由底层契约保证，易用下限由 SDK 保证。**

**四个平面，各自选性能恰当的介质：**

| 平面 | 介质 | 性能要求 | 设计锚点 |
|---|---|---|---|
| 数据面（帧/VQE-PCM） | unix socket + `SCM_RIGHTS` 传 dma-buf fd，定长头 `{ver,seq,pts,w,h,stride,fmt,chn}` | **零拷贝硬要求**（解码编码流的代价已实测：多一次 VENC+VDEC、1–2 帧延迟）；新增延迟 < 1 帧 | 帧：VI 空余 chn；PCM：与帧同构 |
| 事件面（结果进出） | protobuf（复用 `inference.proto` 的 `InferenceResult` + 新增 `source_id`/`pts`）over unix socket | 小消息，性能不敏感；关键是 **pts 对齐**（结果画到哪一帧）与背压策略（慢消费者丢事件不丢连接） | 出站三路分发与入站共用同一 schema |
| 控制面（配置/模型/生命周期） | HTTP REST（entry.cgi 路由表内新增版本化域 `/api/v1/ext/*`） | 低频，无性能要求 | `rest_api.cpp` 集中路由表 |
| 观测面（中间张量/metrics/回显） | 拉模式 + 默认关（订阅指定 stage_id 才旁路上报），避免观测本身拖垮推理 | 旁路开销只在开启时发生；单 stage 上报不阻塞主流水线 | `rc_infer` 流水线 tap 点 |

**易用度七条（每条都对着 5.3 的流失教训）：**

1. **一份契约三种介质同构**（帧/PCM/结果同一套头格式与握手）——学一次会三个。
2. **SDK 双语言起步**（C + Python）：方案商不手写 `SCM_RIGHTS` 收 fd；Python 端 5 行拿到第一帧是验收标准。
3. **握手即版本协商 + 能力发现**：连接后第一条消息返回 `{api_version, capabilities[]}`；OTA 后旧 app 明确知道"哪个能力没了"，而不是莫名崩。
4. **错误显式化**：每个失败路径有错误码 + 人话诊断（对齐 5.3"静默失败是最大流失点"的教训）；连不上、格式不对、配额不足、版本不兼容四类错误在 SDK 层区分。
5. **默认值即最佳实践**：帧订阅默认给 NPU 同款分辨率/格式；PCM 默认 16k 单声道 S16——方案商不查文档也拿到能直接喂模型的数据。
6. **凭据按 app 颁发**：替代现状的 0666 socket 与 localhost JWT 直通；安装时由包管理器发 socket 凭据 + 作用域 token，为沙箱铺路。
7. **官方推理吃自己的狗粮**：内建推理逐步改为走同一套帧/结果契约（原则 3 的落地检验）——契约不够用，第一个疼的是我们自己。

### 6.4 路线

- **第零步｜存量文档化（新增，零开发）**：`ai_asr` PCM 用法、notify 入站写入格式、`ext_*.conf`/`/extension/` 挂载约定、`/var/tmp/rkipc` RPC 现状。今天就能让 Tier B 先跑起来（帧还缺，但音频类和结果分发类应用已可开工）。
- **第一步｜生态最小闭环**：帧代理 + 结果注入（含 OSD/录像回注） + 模型上板验证链路 + 可调试数据。前两条让第三方"拿帧 → 推理 → 把结果画回官方"，后两条让他们真能把自己的模型跑通。S1/S2 立刻成立。
- **第二步｜可运营 + 语音**：沙箱配额（含 IPC 收紧） + 打包分发（alpkg 泛化 + 签名） + 配置 API 版本化 + 音频代理补全（AED/BCD 外发、VQE-PCM）。第三方 app 能安全分发、稳定调用，解锁 S3/S4。
- **第三步｜覆盖面与体验**：后处理开放（声明式 + 插件，含 Tier A）+ 测试输入注入 + 推理忽略区 + 可嵌入 UI 组件化。

### 6.5 验证：用人脸识别串一遍

1. 方案商清单声明 `tier: B`，申请帧订阅 + NPU 预算，安装时获发 socket 凭据。
2. **帧代理**拿零拷贝帧 → 自带 rknn 的 检测→对齐→embedding 级联 → 比对自带注册库 → 忽略区在自己后处理里过滤。
3. **结果注入**把 `{box, label=人名, source_id, pts}` 回注 → 官方 OSD 在视频上画人名、dashboard 显示识别事件、**录像里也有**。
4. **调试**复用官方视频/日志/终端/快照，经 `/extension/face-app/` 挂自有面板：逐级中间结果、参数热调、注册库 CRUD、测试图注入做回归。
5. **沙箱**保证非 root、NPU 不超额、崩溃自恢复、退出恢复官方推理。
6. **分发**打签名包上架，终端用户一键装。

→ 方案商只写了检测、识别、注册、面板这部分核心业务，**相机、出流、叠加、UI、分发全部复用官方，且从头到尾没接触固件源码。** 这是本文建议要达到的形态。

---

## 附：技术锚点（2026-08-10 源码核对版，供开发团队核对）

以下锚点来自 `recamera_v2` manifest（main / V1.1.2）源码，路径基于 `project/app/`：

- **推理与后处理**：`recamera_ipc/common/rc_infer/`（C++）。后处理工厂 `include/postprocess/base.h:110-224`（`PostProcessFactory` + `REGISTER_POSTPROCESSOR` 宏，编译期注册）；已注册 yolov5/yolov8/yolov10/yolox/nanodet/detr/yolo_world/classify/yolov8_pose；任务类型枚举 `include/rc_infer.h:52-60`（7 值）；跟踪器工厂 `src/tracking/factory.cpp`（ByteTrack/IoU/Kalman）。业务代码无 dlopen。
- **帧路径**：`recamera_ipc/src/rv1126b_ipc/video/video.c:66-70` VI 五路 chn（0 gdc / 2 子码流 / 3 主码流 / 4 NPU / 5 VO）；NPU 取帧 `video.c:580-586`（默认 `RK_MPI_VI_GetChnFrame` chn4 直取，`enable_fec` 时走 VPSS(0,2)），取**虚拟地址** `Handle2VirAddr`；`RK_MPI_MB_Handle2Fd` 在 recamera_ipc 零命中（存在于 `uvc_app_tiny/uvc/nn_process.cpp:252` 等旁路 demo，SDK 内有 API 可用）；VPSS 拓扑 `video.c:1231-1342`。
- **结果链路**：出站 `video.c:648-657` 三路分发（vigil 录像队列 / `rc_notify_send_inference` / `osd_manager_draw_infer`）。schema `recamera_ipc/common/vigil/protocol/inference.proto`（`InferenceResult`：task_type/timestamp_ms/model_id + oneof detection/classification/segmentation/tracking/keypoints）；C 生成代码 `common/rc_notify/inference.pb-c.*`；Python 生成代码 `recamera_notify/notify-server/protobufs/inference_pb2.py`。
- **notify-server（Python 源码）**：`recamera_notify/notify-server/`。入站 socket `core/socket_server.py:18` `/var/tmp/notify`、`:84-90`（AF_UNIX + **0666**、`<le32 len><payload>` 帧格式）→ 四种 notifier（`notifiers/{websocket_server,mqtt_notifier,http_notifier,uart_notifier}.py`）；WS 端口 8123（`websocket_server.py:29`，监听 127.0.0.1，nginx `/ws/inference/results` 反代；`:200` recv 路径现仅 echo）。
- **rkipc 控制 RPC**：`recamera_ipc/common/socket_server/socket.h:13` `CS_PATH "/var/tmp/rkipc"`，双向（`server.c`）；entry.cgi 是客户端（`recamera_web_backend/src/socket_client/socket.cpp:25`）。
- **entry.cgi（源码可得）**：`recamera_web_backend/src/CMakeLists.txt:38` `add_executable(entry.cgi)`，C++/cgicc/FastCGI/nlohmann_json/jwt-cpp；路由表 `rest_api.cpp:156-243`（18 域：network/video/audio/image/system/osd/event/peripherals/model/notify/web/ftp/record/config 等），分派 `:270-274`（前 20 字符前缀比较）；鉴权 `rest_api.cpp:67-76`（JWT 在 CGI 内实现，nginx `auth_request /_jwt_verify` 回调它）；**localhost 直通** `:69-72`（`HTTP_X_INTERNAL_FROM_LOCALHOST=1` 免鉴权）。
- **前端（源码可得）**：`recamera_web/recamera_web_react/`，CRA（react 18.2 + react-router-dom 7 + xterm + mp4-muxer），`npm run build` 产物落 `/oem/usr/www`；API 文档在 `backend/`（`reCamera WEB API.pdf`、`INFERENCE_API_CHANGES.md`）。**扩展挂载约定**：nginx `common_relay.conf` 的 `include ext_*.conf;` + `/extension/<name>/` 路径，实例 `recamera_services/acousticslabd/deploy/etc/nginx/ext_acousticslabd.conf`、`alpkg/deploy/etc/nginx/ext_alpkg.conf`。
- **音频**：ALSA 共享 `project/cfg/BoardConfig_Recamera2/overlay/overlay-buildroot-asound/etc/asound.conf`——`dsnoop`（6ch@16kHz S16，`ipc_perm 0666`）+ 命名 PCM `ai_main`(rkipc) / `ai_kws`(acousticslabd) / **`ai_asr`(预留)** / **`ai_debug`(预留)**。rkipc 侧 `rkipc-3840x2160.ini:1-9`（card_name=ai_main, 2ch S16 22050）；VQE `common/audio/audio.c:511-552`（`config_aivqe.json`）；AED/BCD 启用 `audio.c:464-506`、1Hz 轮询 `:556-582`（结果仅 LOG_DEBUG）；编码出口 G711A(RTSP)/MP3(录像)。rkipc 内无 VAD/唤醒；KWS 在 acousticslabd（独立 repo `linux-ipc-app-recamera2-acousticslab`，SDK 内仅 deploy 产物：rknn backbone + `/dev/shm/acousticslabd-{api,result}.sock` + toml 配置）。
- **模型管理**：根目录 `/userdata/config/model/<framework>/`（`recamera_web_backend/src/model_api.cpp:36`），`.info` JSON 元数据（`:148-212`），断点续传上传 + md5（`:219-260`）；运行时切换 `common/rc_model/rc_model.h`（`rc_model_infer_set_model/set_enable/set_fps/restart` + 云端拉取 `rc_model_get_model/get_public_model/download_status_get`）。
- **recamera_services**（Rust 为主）：`gmgr`(GPIO HTTP/WS) / `skt2ws`(unix socket→WS 桥) / `tmir`(串口多路) / `rcisd`(事件 daemon) / `alpkg`(TS，`.alpkg` 解包导入：`src/{unpack,trust,protocol,receiver}.ts`，ZIP32+CRC32+origin 白名单，带测试)。通信总模式：unix socket（`/dev/shm/*.sock`）+ nginx 反代 + JWT `auth_request`。
- **沙箱现状**：`seccomp`/`cgroup` 在 `project/app` 全树 0 命中；服务 SysVinit 裸跑（`S40gmgr` 等）；IPC 端点一律 0666。
- **打码**：OSD `maskOverlay.privacyMask[]`（cover/mosaic RGN，挂 VPSS/VENC 输出通道），与 NPU 取帧通道分离（5.4 实测结论维持）。
- **可复用端口**：go2rtc `:1984`（`recamera_go2rtc/`，配置由 `S50go2rtc` 从 rkipc.ini 生成）、WebRTC `:8555`、RTSP `554/5554`、ttyd `:7681`、系统日志 WS `:8765`、结果 WS `:8123`。
- **相关文档**：调试面板复用分析 `DEBUG_PANEL_REUSE.md`；应用中心移植 `APP_CENTER_PORT_DESIGN.md`。

### 初版 → 本版的主要论断修正（供对照）

| 初版论断 | 修正 |
|---|---|
| entry.cgi / 前端闭源，只能逆向、旁挂 | 源码可得（web_backend C++ / web_react CRA），直接开发；对方案商仍闭源，故版本化 API 由我们提供 |
| 后处理是 5 种固定 C 枚举 | 是 9 算法的编译期字符串注册表（含 detr/yolo_world/yolov10）；限制从"加不了"修正为"方案商无插件位" |
| `Handle2Fd` 拿 fd 已现成、只差暴露 | rkipc 内零命中，NPU 路径用虚拟地址；帧代理是新增子系统 |
| 结果通道只出站、无入站 | notify socket（0666）与 rkipc RPC 两条入站已存在；缺的收窄为 OSD/录像回注 |
| rkipc 独占音频、PCM 不对外 | dsnoop 已做硬件共享，`ai_asr`/`ai_debug` 预留 PCM 零改动可用；缺的收窄为 VQE-PCM |
| VAD/唤醒内置于 rkipc 但无 API | rkipc 内无 VAD；KWS 在独立进程 acousticslabd（另一 repo） |
| 无任何打包/分发机制 | 有 `.alpkg` 格式与 `ext_*.conf` 挂载约定；缺签名/版本/卸载/通用化 |
| 方案商被逼"fork 固件或绕路自建"二选一 | fork 从来不是选项（无源码）；真实选项是"绕路自建或放弃"，我们的任务是把绕路变成正路 |
