# 09 — 接入输出组件（声明式结果输出）· 可用/已实现

> 演示怎么让 kit 把 app 的每帧推理结果**声明式**发到 MQTT / HTTP / UART / WS，
> **app.py 里对 `on_results` 一行输出代码都不用写**。
>
> 事实来源：[`docs/guide/output-sink.md`](../../docs/guide/output-sink.md)（用法）+
> `internal/OUTPUT_SINK_SPEC.md`（权威契约）。落地代码 `kit/adapters/output_sink.py`，
> manifest 校验测试 `apps/test_output_manifests.py`。**活样本**：`apps/yolo-detector/manifest.json`。

## 和 01–06 的区别

01–06 是「设备上跑自己的进程调扩展 SDK」的运行时示例（要写 Python 调 `FrameSource`/`ResultSink`）。
本示例是**应用中心声明层**——app 不写发送代码，只在 manifest 里声明输出意图，kit 运行时负责编码 + 发送。
与 08（per-app 依赖）同层次：改的是 manifest，不是运行时 SDK 调用。

## 怎么接（两步）

1. `capabilities` 加 `"output"`。
2. 加一个 `output` 块：`fields`（能被引用的字段）+ `default_channel`/`default_mode` + `templates`/`default_mapping`。

见同目录 [`manifest.snippet.json`](./manifest.snippet.json)。关键点：

- **`fields[]`** 声明哪些字段可被映射/模板引用（`name`/`from`/`type`/`description` 必填；`from` 是只读点路径如 `results[].box`，不是任意 Jinja）。
- **`default_channel`** 可多选并发（`["ws","mqtt"]` = 同时发 WS 叠加通道和 MQTT）。
- **`default_mode`**：`raw`（原始 JSON 兜底）/ `custom`（映射表 + Jinja2）/ `ha`（Home Assistant Discovery 即插即用）。
- **`default_mapping[]`** 是可视化映射行：`source`（取值表达式）→ `target`（输出键）→ `topic`（可插 `{{ app }}`）。kit 按 topic 分组生成 JSON 模板。

## 输出效果

- `default_mode: "custom"` + 上面的映射 → MQTT 收到：
  - `recamera/my-detector/count` → `{"count": 3, "person_count": 1}`
  - `recamera/my-detector/detections` → `{"detections": [{box,score,class_id,label,raw}, ...]}`
- 切 `default_mode: "ha"` + 在 manifest 加 `ha_entities` → HA 自动建实体（含 availability 上下线，kit 内置 LWT）。
- `default_channel` 含 `"ws"` → 结果照常上 /preview canvas 叠加（见 [ai-result-overlay.md](../../docs/guide/ai-result-overlay.md)）。

## 不想用 kit 输出？

**不声明 `output` capability** → kit 完全不介入，行为与现状一致；app 在 `on_results` 里自己
`import paho`/`requests` 发（带 per-app 依赖）。两者可混用，但**别对同一外部 topic 双发**同一份数据。

## 依赖

`custom`/`ha` 模板渲染需 **Jinja2**（设备 `/userdata/rknnenv` 已装 3.1.2）；`raw` 模式无此依赖。

## 真机验证（P3b，已验证）

raw JSON、HA Discovery config 报文（含 `state_topic`/`availability_topic`/`unique_id`/`device`）、
上下线 LWT、custom jinja2 均在设备 + 本地 broker 验证通过。已知修复项：早期 init-race 导致
HA discovery 首连不发（已修/修复中），详见 [output-sink.md](../../docs/guide/output-sink.md) §7。
