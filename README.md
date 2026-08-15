# reCamera Pro 扩展 API

reCamera Pro（RV1126B / recamera_v2）扩展 API：方案商**不改固件源码、不重编固件**，在设备上跑自己的进程，通过 `/run/recamera/` 下的 Unix domain socket 拿相机帧、把推理结果回注官方 OSD/录像/推送、观测内建流水线、经 HTTP 做配置与控制。进程边界即契约。

三条 socket 端点：

| 端点 | socket | 作用 |
|------|--------|------|
| 帧代理 | `/run/recamera/frame.sock` | 零拷贝拿相机原始帧，自己推理 |
| 结果注入 | `/run/recamera/result-in.sock` | 把结果回注官方 OSD / 录像 / WS 三路分发 |
| 观测面 | `/run/recamera/probe.sock` | 采样内建推理流水线各级张量 |

## 目录导航

| 目录 | 作用 |
|------|------|
| [`docs/api/`](docs/api/) | 规格与架构：[`spec.md`](docs/api/spec.md)（socket 路径 / protobuf schema / ABI 版本）、[`architecture.md`](docs/api/architecture.md)（系统分层与数据流）。 |
| [`docs/guide/`](docs/guide/) | 开发指南：[总入口](docs/guide/README.md) + 各专题手册（控制 API、结果推送、GPIO、音频 PCM、前端扩展、FFmpeg / GStreamer 集成、应用中心上架、部署运维）+ Kit 设计 / 适配器路径 / 语音应用设计。 |
| [`sdk/`](sdk/) | 设备侧 SDK：`librecamera_ext.so.1` + Python 包 `recamera_ext`（ctypes 薄封装）+ C 头文件。 |
| [`kit/`](kit/) | 可复用 Python 推理套件：L0 适配器、runtime 前后处理、logic 库、`app.py`。 |
| [`apps/`](apps/) | 示例应用（yolo-detector / face-analysis / fall-detection / voice-transcribe 等）。 |
| [`examples/`](examples/) | SDK 最小用法示例（拿帧 / 注入结果 / 帧→推理→OSD / GPIO 触发 / C 帧）。 |
| [`release/`](release/) | 交付产物：`recamera-ext-api-v<版本>.tar` + `pkg/`（rkipc / entry.cgi / SDK / install.sh）。 |

## 快速上手

拿帧 + 推理 + 回注（Python，完整说明见 [docs/guide/README.md](docs/guide/README.md) §2）：

```python
from recamera_ext import FrameSource, ResultSink

with FrameSource() as src, ResultSink(source_id="my-app") as sink:
    for frame in src:                          # frame.array: 零拷贝帧视图
        boxes = my_model(frame.array)          # -> [(x1,y1,x2,y2,score,label), ...] 归一化 [0,1]
        sink.send_detections(pts_us=frame.pts_us, boxes=boxes)
```

坐标一律为归一化 `[0,1]`（相对画面宽高的比例）。更多示例见 [`examples/`](examples/)。

## 安装 / 环境

设备上运行扩展应用需要指向 SDK 的库与 Python 包路径：

```sh
export PYTHONPATH=/userdata/sdk/python:$PYTHONPATH
export LD_LIBRARY_PATH=/userdata/sdk/lib:$LD_LIBRARY_PATH
```

或直接 sideload [`release/`](release/) 下的发布包（`recamera-ext-api-v<版本>.tar`），按 `pkg/install.sh` 安装 rkipc / entry.cgi / SDK。前置条件（socket 权限、握手）见 [docs/guide/README.md](docs/guide/README.md) §1.2；部署与运维见 [docs/guide/deploy-ops.md](docs/guide/deploy-ops.md)。

## 发布物 / Release

`release/` 下有两个发布包：

| 包 | 用途 | 说明 |
|---|---|---|
| [`release/recamera-ext-api-v1.5.0.tar`](release/) | **固件 sideload 包** | patched `rkipc` + `entry.cgi` + SDK + `install.sh`/`rollback.sh`。覆盖 `/oem`（持久，OTA 会还原）。设备端安装步骤见 [`release/pkg/README.md`](release/pkg/README.md)。 |
| [`release/recamera-ext-kit-v1.5.0.tar.gz`](release/) | **kit 分享包** | `kit/` + `sdk/` + `examples/` + `INSTALL.sh`（一键装到 `/userdata`）。**不含固件**，给已刷好扩展 API 固件、要在设备上开发 app 的方案商。 |

如何更新 release（重打包 / 换 rkipc / 升版本）见 [RELEASING.md](RELEASING.md)；用 [`release/build-release.sh`](release/build-release.sh) 可复现地从仓内源组装。

## 版本

两条版本轴，勿混淆：

- **发布 train（产品发布包）**：当前 **v1.5.0**（`release/` 下 `recamera-ext-api` / `recamera-ext-kit` / `appmgr` / `frontend` / `apps` 五包同号）。部署见 [docs/guide/deploy-ops.md](docs/guide/deploy-ops.md)。
- **SDK / C ABI 版本**：当前 **1.2.0**（soname `librecamera_ext.so.1`，API `frame@1 / result@1 / probe@1`）。ABI 向后兼容、soname 不变时**不随发布 train 跳版**；SDK 变更记录见 [CHANGELOG.md](CHANGELOG.md)、[sdk/VERSION](sdk/VERSION)。

## License

[Apache License 2.0](LICENSE)。
