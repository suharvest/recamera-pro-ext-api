# 04 — gpio-trigger：结果驱动 GPIO

检测到目标（或满足条件）时自动拉高/拉低一个引脚，驱动继电器/LED/蜂鸣器/告警。

**不需要改固件、不需要官方新增功能**——用设备已有的两个零件拼起来：

1. **结果输出** — notify WebSocket `ws://127.0.0.1:8123`（本机，无需 token）。消息是 `InferenceResult` JSON，`detection.entries[].{class_name, score, box}`。
2. **引脚控制** — gmgr API，直连 unix socket `/dev/shm/gmgr.sock`（root 免 JWT）。

本示例是 `docs/guide/gpio-result-trigger.md` 逻辑的可跑脚本版，只用 Python 标准库，无第三方依赖。

## 边界（重要）

- 引脚**仅 2 根**：`GPIO3_B2` = pin **106**、`GPIO3_B3` = pin **107**。
- 仅数字 **0/1**，**不支持 PWM/模拟/波形**（gmgr `write_value` 硬校验 `value>1` 报错）。
- 直连 socket 需以 **root** 运行（扩展经 appmgr 启动即是 root）。
- 更多引脚需扩 gmgr 配置并确认对应 line 未被相机/音频 pinmux 占用。

## 与前几个示例的关系

01/02/03 直接对接 `librecamera_ext` 的 socket（帧代理 / 结果注入）。本示例**不用 librecamera_ext**——它消费的是**内建推理**（或示例 02/03 注入后经推送出来）的结果 WS，再去写引脚。三者可组合：示例 03 注入的框也会经 notify WS 出来，本脚本能据此触发引脚。

## 依赖

- 以 **root** 运行
- 设备上 `/dev/shm/gmgr.sock` 可用（gmgr 服务在跑）
- notify WS `127.0.0.1:8123` 可连（固件推理/推送在跑）
- 仅 Python 标准库

## 怎么跑

```sh
adb push gpio_trigger.py /root/
adb shell 'python3 /root/gpio_trigger.py --pin 106 --class person --score 0.5'
```

参数：`--pin`（106 或 107，默认 106）、`--class`（触发类名，默认 person）、`--score`（最低置信度，默认 0.5）。

把 `--class` / `--score` / `--pin` 换成你的业务即可。要更复杂的条件（多类、区域、计数），改 `main()` 里 `hit = ...` 那一行。

## 预期输出

```
watching class='person' score>0.50 -> pin 106  (Ctrl-C 退出)
pin 106 -> 1        # 画面出现 person
pin 106 -> 0        # person 离开
```

只在**状态变化**时写引脚（减少 IO），所以稳定时不刷屏。

用万用表/示波器量 pin 106（GPIO3_B2）电平，或接个 LED 观察。

## 常见问题

- **拉不动引脚**：先确认 `gpio_settings` 设了 `push-pull`；确认引脚没被别的用途占用；确认以 root 运行。
- **连不上 gmgr**：`ls -l /dev/shm/gmgr.sock`；gmgr 服务未起则无此文件。
- **WS 连不上 / 收不到消息**：确认内建推理在跑（有结果才会推 WS）；`127.0.0.1:8123` 是本机 notify，无需 token。
- **要经外网/nginx 调 gmgr**：改成带 `Cookie: token=<JWT>` 的 HTTPS 请求，端点 `https://<设备IP>/api/v1/gpio/{id}/value`（登录拿 token 见 `docs/guide/result-push.md`）。
- **要 PWM / 更多引脚**：当前不支持，见 `docs/guide/gpio-result-trigger.md`。
