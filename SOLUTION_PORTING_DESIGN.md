# 一代 Solutions 移植到 reCamera Pro — 设计（定稿）

> 把一代(SG200x/CVITEK)已适配应用中心的 9 个应用移植到 Pro(RV1126B/RKNN)，作为 Pro 应用市场首批 catalog。
> 关联：`BOOTSTRAP_PATH.md`(适配器)、`RECAMERA_PRO_INFERENCE_SDK_DESIGN.md`(Tier/官方口)、`APP_CENTER_PORT_DESIGN.md`(打包/清单/appmgr)。
> 本稿已用逐应用 deep-map + sscma-micro 结构核实数据定稿。

## 0. 决定性发现：移植路径因此大幅简化

一代 9 个应用**全部**基于 **`sscma-micro`** 框架，而 sscma-micro 有**清晰的 engine 抽象层**（`sscma/core/engine/ma_engine_base.h`），已内置 **3 个后端：CVI / TFLite / Hailo**，**唯独缺 RKNN**。

→ **最高杠杆的一步 = 给 sscma-micro 写一个 `ma_engine_rknn` 后端**（照抄同为边缘 NPU 的 `ma_engine_hailo.cpp` 模板）。一旦有了它：
- `ModelFactory` / `Detector` / `Pose` 托管层**原样可用**；
- 各应用 `main/` 里 `engine_->getInput/getOutput` 的裸张量代码**原样可用**（接口不变）；
- 应用的前后处理(DB/CTC/softmax/landmark/对齐)、上层 CPU 逻辑(跟踪/时序/计数)**几乎照搬**。

**移植不是"逐个重写应用"，而是"补一个 RKNN 后端 + 换取帧/出流层，然后应用近乎重编译"。**

## 1. 移植套件 = 把共享组件层移到 Pro（做一次，9 个全受益）

一代应用都建在同一套 components 上。移植 = 提供这些组件的 Pro 实现，**API 保持兼容**，应用层少改：

| 组件 | 一代实现 | Pro 需要 | 工作量 | 说明 |
|---|---|---|---|---|
| **推理引擎** | `ma_engine_cvi`(CVITEK TPU) | **新写 `ma_engine_rknn`** | **中(关键)** | 照 `ma_engine_hailo.cpp` 模板，封 `librknnrt`；一次做，托管层+裸张量全通 |
| **取帧** | `components/sophgo/video`(CVITEK VI/VPSS) | 换成 **FrameSource** 后端 | 中 | 一代 app 独占 VI；Pro 上 rkipc 占 VI → 用 BOOTSTRAP_PATH 的 FrameSource(sub流+MPP解码)喂帧，保持 `registerVideoFrameHandler` 同款 API |
| **结果输出** | `rtsp_server`(8554 OSD) + `debug_stream`(8001 WS) | 换成 **ResultSink** | 中 | 一代每 app 自建 RTSP+预览；Pro 有 go2rtc → 不再每 app 起 RTSP，改由 ResultSink(客户端叠加/(未来)R2官方OSD) + 结果 WS |
| **MQTT** | `ha_mqtt`(HA Discovery) | 照搬(需 broker) | 低 | 平台无关；Pro 上跑 mosquitto 即可 |
| **前后处理/工具** | `geometry` / `privacy_blur` / `rgn` / `quirc` / opencv | 照搬(图像算子可换 RGA) | 低 | 多为平台无关 C++ |
| **应用中心注册** | manifest `<id>.json` + `K92<id>` init + postinst | 映射到 **appmgr 清单 + 进程管理** | 低 | 清单字段(models/pipeline/config_schema/debug_ws)直接沿用；init.d→appmgr 生命周期 |

> 三个真正要动的：**① `ma_engine_rknn`(关键杠杆) ② 取帧接 FrameSource ③ 输出从"每app自建RTSP"改 ResultSink**。其余组件与应用 main/ 近乎照搬。

## 2. 逐应用移植清单（deep-map 定稿）

推理路径分两类（决定改动量，非 Tier A/B）：**托管(ModelFactory)** vs **裸张量**。两类在 Pro 上都靠 `ma_engine_rknn`，差别只在前后处理是否自带。

| 应用 | 功能 | 模型 | 路径 | 上层逻辑 | 复杂度 | 备注 |
|---|---|---|---|---|---|---|
| **yolo-detector** | COCO80 检测(可切yolo11n/pose/yolo26n) | 1(检测) | 托管 | 跟踪 | **低** | **标准模板，最先做**，跑通即复制 |
| **weather-classifier** | 5类天气分类 | 1(MobileNetV3) | 裸张量 | softmax | **低-中** | 后处理最简单 |
| **qrcode-reader** | 二维码解码 | **0(纯CPU quirc)** | 无NN | quirc解码 | **低** | **无需转模型**，只换取帧/出流 |
| **retail-vision** | 人流计数/停留 | 1(检测person) | 托管 | 跟踪/区域/计数 | **中** | config_schema 最丰富(区域/进出线) |
| **fall-detection** | 姿态跌倒检测 | 1(yolo11n-pose) | 托管 | 时序状态机 | **中** | 有单测；12参数config_schema |
| **fitness-trainer** | 健身计数 | 1(yolo11n-pose) | 托管 | 角度计数状态机 | **中** | 与 fall 同构，一起做 |
| **facemesh-reader** | 困倦检测 | **2**(人脸检测+468landmark) | 托管+裸张量 | EAR/MAR/时序 | **高** | 流水线 |
| **face-analysis** | 客群年龄/性别/情绪 | **3**(人脸+FairFace+情绪) | 托管+裸张量 | 分类/回归+对齐 | **高** | 3级流水线 |
| **ppocr-reader** | 中英文OCR | **2**(DB检测+CTC识别) | 全裸张量 | DB+CTC+字典 | **最高** | 全自研前后处理，压轴 |
| ~~detection-blur~~ | 检测+打码 | 1 | 托管 | — | **跳过** | 遗留原型，未进应用中心，被 yolo/retail 取代 |

**要转成 RKNN 的模型清单(9 个 + qrcode 无)**：
`yolo11n_detection` · `yolo11n_pose` · `yolo26n` · `yolov8n_face` · `fairface` · `enet_b0`(情绪) · `face_landmark`(468) · `ppocr_det`(DB) · `ppocr_rec`(CTC) · `weather_mobilenetv3_small`。

## 3. 模型转换（并行关键路径）

`cvimodel` 不能直接转 → 从**原始 ONNX** 走 rknn-toolkit2：
```
原始 ONNX ─► rknn-toolkit2(INT8需校准集) ─► <model>.rknn ─► 设备验精度/算子
```
- **先盘 ONNX 来源**（移植可行性的第一张表）：
  - yolo 系(11n/pose/26n)：一代 `model_conversion/recamera_yolo_detection`、`recamera_yolo26` 有 ONNX 导出+校准，**源在**；
  - yolov8n-face / fairface / enet_b0情绪 / face_landmark468 / ppocr_det / ppocr_rec / weather_mobilenetv3：**逐个确认 ONNX 来源**(SSCMA/上游开源/训练源)，缺的先补/延后该应用。
- **算子/精度风险**：pose 解码头、DB 分割、CTC、landmark 回归是 RKNN 算子不支持/精度掉的高发区 → 转换期逐个试，必要时后处理挪 CPU、退 FP16。
- **落位**：托管走官方目录也可(Tier A)，但本方案 9 个都以**自建进程**跑(见 §4)，模型放应用包 `models/`。

## 4. 架构取舍：沿用"自建独立应用"（全 Tier B），不强套官方推理

一代每个应用是**独立二进制**(自取帧+自推理+自出流)，由 supervisor 单例拉起(K92+抢摄像头)。移植到 Pro 最省力、改动最小的路径 = **沿用这个形态**：每个应用仍是独立进程，由 **appmgr** 管理，帧来自 **FrameSource**、结果走 **ResultSink**。即**全部走 Tier B(自建流水线)**。

- **为什么不逐个改成 Tier A(官方 rkipc 推理)**：那样要把每个应用的模型塞进 rkipc、逻辑改成订阅 `:8123`，等于重写；而沿用自建形态**近乎重编译**。
- **代价**：不复用官方 OSD 叠加(用客户端叠加)、多个应用共享 FrameSource 而非官方单模型槽——但这正是 BOOTSTRAP_PATH 设计好的，**官方 R1/R2 到了换适配器即可**，应用不动。
- **例外**：yolo-detector / weather 这类单模型，若想要官方 UI 体验，可**额外**出一个 Tier A 版(转 rknn 上传官方)，但非移植主线。

## 5. 输出层的现实调整（一代 vs Pro 最大行为差异）

一代每 app 自建 RTSP(8554)+debug_stream(8001) 做预览。Pro 上 rkipc/go2rtc 已负责出流 → **不要每 app 再起 RTSP**：
- **预览**：统一用 go2rtc 流(`/appcenter` 里播)，不再 per-app RTSP。
- **结果**：应用把结果经 **ResultSink** → `/appcenter` 客户端叠加(+ 未来 R2 官方 OSD)；结构化结果走结果 WS/MQTT。
- **MQTT**：`ha_mqtt` 照搬(Pro 起 mosquitto)。
- **OSD 叠加逻辑**：一代在帧上画框的代码(rgn/opencv)→ 移到客户端叠加或 ResultSink，不在设备编码流里烧(除非上 R2)。

## 6. 分阶段路线

- **P0 · 套件地基(最关键)**：`ma_engine_rknn`(照 hailo 模板) + video 组件接 FrameSource + ResultSink + 打包/转换脚本。**先转 `yolo11n_detection` 一个模型**。
- **P1 · 打样(证明整条链)**：`yolo-detector` 端到端跑通(FrameSource→ma_engine_rknn→ModelFactory Detector→ResultSink→/appcenter 看框)。**这一个跑通 = 套件验证完成**。
- **P2 · 托管类批量**：retail-vision / fall-detection / fitness-trainer / weather-classifier / qrcode-reader（都靠已验证的托管层或简单裸张量，上层逻辑照搬）。
- **P3 · 流水线类**：facemesh-reader → face-analysis → ppocr-reader（多模型 + 自研前后处理，逐个转模型 + 验后处理）。
- **P4 · 收尾**：config_schema→调试面板参数、catalog 上架、与一代行为回归对照。

## 7. 优先级（先易+先证链路）
1. **yolo-detector**(低，标准模板，打样套件)
2. **qrcode-reader**(低，无模型，验证无 NN 应用骨架)
3. **weather-classifier**(低-中，验证裸张量+分类)
4. **fall-detection + fitness-trainer**(中，姿态托管+时序，同构一起做)
5. **retail-vision**(中，跟踪/区域逻辑)
6. **facemesh-reader**(高，2模型流水线)
7. **face-analysis**(高，3模型)
8. **ppocr-reader**(最高，DB+CTC 压轴)
- detection-blur：跳过。

## 8. 风险与对策
| 风险 | 对策 |
|---|---|
| **`ma_engine_rknn` 是否顺利** | 有 `ma_engine_hailo` 现成模板 + `ma_engine_base` 稳定接口 → 风险可控；P0 先做并用 yolo 打样验证 |
| **模型缺原始 ONNX** | 移植前先盘 10 模型 ONNX 来源；缺的走上游/SSCMA/重训或延后该应用 |
| **RKNN 算子不支持**(pose头/DB/CTC/landmark) | 转换期逐个试；改结构/自定义算子/后处理挪 CPU；退 FP16 |
| **取帧：rkipc 占 VI** | 走 FrameSource(sub流+MPP解码)，不直占 VI；video 组件保持同款 API |
| **输出行为差异**(每app RTSP vs go2rtc) | §5：改 ResultSink + 统一 go2rtc 预览，不 per-app RTSP |
| **9 个重复造轮子** | §1 套件先行，应用近乎重编译 |
| **appmgr 生命周期 vs 一代 init.d/抢摄像头** | Pro 上帧共享(FrameSource 扇出)，不再抢 VI；单 active 兜资源 |

## 9. 一句话
**核心杠杆 = 给 sscma-micro 补一个 `ma_engine_rknn` 后端(照 hailo 模板)**——之后托管层/裸张量/前后处理/上层逻辑近乎照搬。移植 = **套件先行(RKNN引擎+FrameSource取帧+ResultSink出流) → yolo-detector 打样证链 → 托管类批量 → 流水线类逐个**。并行盘 10 个模型的 ONNX 来源、转 RKNN。detection-blur 跳过，其余 9 个全走自建(Tier B)，官方 R1/R2 到了换适配器不动应用。
