# reCamera Pro — 仓库地图（README 索引）

本文件是整个 `recamera_pro/` 的**导航索引**，一眼看清各设计文档与各目录的作用。
> 注：顶层设计文档被 20+ 个文件按文件名引用，**请勿移动/改名**；本 README 只做索引，不搬家。

## 顶层设计文档

| 文档 | 一句话作用 |
|------|-----------|
| `APP_CENTER_PORT_DESIGN.md` | App Center（应用中心）移植设计。⚠️ 其中 `/appcenter` 静态 SPA 前端已被官方 React `/app-center` 取代（见文首 banner）；appmgr 后端/打包/catalog 仍有效。 |
| `ARCHITECTURE.md` | reCamera Pro 扩展 API 整体架构说明。 |
| `BOOTSTRAP_PATH.md` | "先行自建、平滑切官方" 的路径设计（能力注册表 + 适配器自动迁移）。 |
| `CHANGES.md` | 扩展 API 改动说明 / 交付报告。 |
| `DEBUG_PANEL_REUSE.md` | 复用 reCamera Pro 界面作为调试/测试面板的方案。 |
| `IMPLEMENTATION_PLAN_M1.md` | M1 实现计划：结果回注 + rc_ext_core 骨架 + SDK。 |
| `PYTHON_KIT_DESIGN.md` | Python 应用套件的通用/应用分层设计（L0 适配器等）。 |
| `RECAMERA_PRO_API_SPEC.md` | 扩展 API 规格（socket 路径、protobuf schema、ABI 版本）。 |
| `RECAMERA_PRO_INFERENCE_SDK_DESIGN.md` | 推理扩展能力的用户分析与产品建议。 |
| `SOLUTION_PORTING_DESIGN.md` | 一代 Solutions 移植到 reCamera Pro 的设计（定稿）。 |
| `VOICE_APP_DESIGN.md` | 语音应用设计：唤醒词 → 采集音频 → 转录。 |

## 目录

| 目录 | 一句话作用 | git |
|------|-----------|-----|
| `sdk/` | 设备侧扩展 API SDK：`librecamera_ext.so` + `recamera_ext` Python 包 + C 头文件（官方 SDK，勿改实质内容）。 | tracked |
| `kit/` | 可复用 Python 推理套件：L0 适配器（frame/audio/result/control）、runtime 前后处理、logic、`app.py`。 | tracked |
| `apps/` | 各示例应用（yolo-detector / facemesh-reader / fall-detection / voice-transcribe / face-analysis / retail-vision / qrcode-reader / fitness-trainer / ppocr-reader），打包成 tar.gz 供 App Center 安装。 | tracked |
| `market/` | App Center 相关：`appmgr/` 后端服务、`spa/` 历史前端（已废弃，见其 `DEPRECATED.md`）、`deploy/` nginx conf、`packaging/` 打包脚本与产物、catalog。 | untracked (gitignored) |
| `models/` | 模型转换（ONNX→RKNN，target rv1126b）与权重。 | 权重 gitignored |
| `docs/` | 扩展 API / 发布 / 音频等运行手册（如 `docs/ext/`）。 | tracked |
| `examples/` | SDK 用法示例（`02-inject-result` 等）。 | tracked |
| `release/` | **交付产物**：`recamera-ext-api-v1.2.0.tar` + `pkg/`（entry.cgi、rkipc、librecamera_ext.so、install.sh、sdk 头文件）。真实可交付物，勿动。 | tracked |
| `_local_backups/` | 本地备份：从各处移入的 `*.bak` 与旧 scratch（`_m4_work/`），保留可回溯，不进 git。 | gitignored |

## 约定

- 临时产物（`__pycache__/`、`*.pyc`、`.DS_Store`、`dist/`、`.pytest_cache/`）已在 `.gitignore` 忽略，可随时清理再生。
- 非再生的开发回滚点统一放 `_local_backups/`，不直接删除。
