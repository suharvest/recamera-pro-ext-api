# AGENTS.md

给基于本 SDK / kit 开发 reCamera Pro（RV1126B / recamera_v2）应用的人及其编码 agent。本仓是**公开仓**：SDK、kit 运行时、示例、发布物。你在这里做的事是**写在设备上跑的方案商进程**——不改固件、不重编固件。

## 项目结构

| 路径 | 是什么 |
|---|---|
| `docs/api/` | 规格与架构：`spec.md`（socket 路径 / protobuf schema / ABI 版本 / 坐标契约）、`architecture.md`（分层与数据流）。**事实来源。** |
| `docs/guide/` | 开发指南：`README.md`（总入口 + §4 结果注入契约）+ 分篇（control-api / result-push / gpio / audio-pcm / frontend-extension / ffmpeg / gstreamer / app-center-publishing / deploy-ops / kit-design / adapter-bootstrap / voice-app）。 |
| `sdk/` | 设备侧 SDK：`include/recamera_ext.h`（C ABI）+ `python/recamera_ext`（ctypes 薄封装）+ `lib/librecamera_ext.so*` + `VERSION`。**权威 SDK 源。** |
| `kit/` | 可复用 Python 推理套件：`app.py`（App 基类 + 主循环）、`adapters/`（L0 适配层：frame/result/audio/mqtt/cgi + registry 探测）、`runtime/`（前后处理）、`logic/`（追踪/区域等）。 |
| `apps/` | 示例应用（yolo-detector / face-analysis / fall-detection / facemesh-reader / voice-transcribe 等），继承 kit。 |
| `examples/` | SDK 最小单文件用法示例（01 取帧 / 02 注入 / 03 帧→算法→OSD / 04 GPIO / 05 C ABI / 06 probe）。 |
| `release/` | 发布物：固件 sideload 包 + kit 分享包 + `build-release.sh`。更新见根目录 `RELEASING.md`。 |
| `market/` | 应用中心前端/打包（**gitignored，不在公开仓**）。 |

## 扩展 API 模型：三条 socket

方案商进程通过 `/run/recamera/` 下三条 Unix domain socket 与固件交互（契约 = 进程边界）：

| socket | 客户端 | 作用 |
|---|---|---|
| `frame.sock` | `FrameSource` | 零拷贝拿相机原始帧（全分辨率 NV12，不预 letterbox），自己推理 |
| `result-in.sock` | `ResultSink` | 把结果回注官方 OSD / 录像 / 推送三路分发 |
| `probe.sock` | `ProbeSource` | 只读观测内建推理流水线各级张量/指标 |

握手 Hello/HelloAck 由 SDK 内部完成，不接触 protobuf。契约细节见 `docs/api/spec.md` 与 `docs/guide/README.md` §4。

## 核心约定（最易踩，动手前先读）

- **坐标一律归一化 `[0,1]`**：所有 box 坐标（检测/分类 ROI/分割 ROI/跟踪/关键点对象框）及关键点 point 的 x/y 均为相对画面宽高的比例。**传像素值会被 OSD clamp 成 1px 隐形框**——手头是像素就除以帧宽/帧高。分割 mask 是行主序原始字节（非坐标）。这是最常见的 BUG。
- **Python 用 uv，不裸 `pip install`**：`uv run pytest` / `uv add`。
- **OSD 单槽后写覆盖**：同一 `source_id` 的结果后写覆盖前写；空 `send_detections` 用于清屏。
- **seg 不上 OSD**：分割 mask 不渲染到 OSD（只走推送/录像元数据）。
- **`source_id` 不能用保留字 `"builtin"`**（内建推理专用，外部用被拒 EAUTH）。
- **限速 60 msg/s/连接**（burst 15）+ 全局 120（burst 30），单条 payload ≤ 64KB，并发注入连接 ≤ 4。超限丢弃+计数，别超过帧率发。
- **`pts_us`**：要与某帧对齐叠加时传该帧的 `frame.pts_us`（同 VI 帧 PTS 时钟）；`0` 表示不与具体帧关联。
- **零拷贝视图跨帧要 `.copy()`**：`frame.array` / `ProbeSample.array` 下一次迭代即失效。

## 开发一个 app

1. 继承 `kit.app.App`，覆盖 `setup(config)`（读 config_schema 参数）和 `on_results(results, frame)`（业务逻辑：原始检测 → app 级事件）。CPU-only app 设 `needs_model=False` 并覆盖 `process_frame`。
2. 写 `manifest.json`（`id` / `version` / `entry: app.py` / `models` / `config_schema` 等，参考 `apps/*/manifest.json`）。
3. 直接用 SDK：`from recamera_ext import FrameSource, ResultSink, ProbeSource`。生产级适配层范本见 `kit/adapters/official.py`（socket 在就用官方 API，不在回退 RTSP/WS，应用一行不改）。

## 构建 / 测试

```sh
# kit 单元测试（mock，不需要设备）：
uv run pytest kit/adapters/

# 跑 examples（设备上，或指向已装 SDK 的路径）：
export PYTHONPATH=/userdata/sdk/python:$PYTHONPATH
export LD_LIBRARY_PATH=/userdata/sdk/lib:/oem/usr/lib:/usr/lib:$LD_LIBRARY_PATH
python3 examples/02-inject-result/inject_result.py --task detection
```

## 设备部署 / 验证

- SDK 装在 `/userdata/sdk`（`python/` + `lib/` + `recamera_ext.h`）；共享 kit 装在 `/userdata/local/kit`（使 `/userdata/local` 在 `sys.path`，`import kit` 生效）。
- 环境变量：`PYTHONPATH=/userdata/local:/userdata/sdk/python`、`LD_LIBRARY_PATH=/userdata/sdk/lib:/oem/usr/lib:/usr/lib:$LD_LIBRARY_PATH`。
- 前置：固件必须含扩展 API（`ls -l /run/recamera/` 应有 `frame.sock`/`result-in.sock`/`probe.sock`）。
- 烟雾 demo：`examples/02-inject-result`，然后 RTSP（`rtsp://<ip>:8554/...`）或 WS（`127.0.0.1:8123 /ws/inference/results`）看注入的框。
- 端到端自检清单见 `docs/guide/deploy-ops.md` §5。

## release（发布物）

- `release/recamera-ext-api-v<ver>.tar` = 固件 sideload 包（rkipc + entry.cgi + SDK）；设备端步骤见 `release/pkg/README.md`。
- `release/recamera-ext-kit-v<ver>.tar.gz` = kit 分享包（kit + sdk + examples + INSTALL.sh，不含固件）。
- 如何更新两个包 → 见根目录 `RELEASING.md`（用 `release/build-release.sh` 可复现重打）。
