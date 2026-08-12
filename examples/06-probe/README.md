# 06 — probe：观测内建推理流水线

`ProbeSource` 是**只读观测面**：不改流水线、不注入结果，只从 `/run/recamera/probe.sock`
订阅内建推理各级（stage）的采样。用来调试/监控内建推理，或把内建推理的中间张量喂给你自己的后处理。

## 这个示例做了什么

- 用 `ProbeSource(stages=["metrics"])` 订阅指标 stage，取几条样本，打印 `stage_id / seq / payload_len`；
- 换 `--stage preproc.out` 订阅预处理输出张量，打印 `meta` 的 `shape` 和分辨率（`width`x`height`），并展示 `sample.array`（按 meta 定型的 numpy 视图）。

## 核心代码

```python
from recamera_ext import ProbeSource

with ProbeSource(stages=["metrics"]) as ps:      # 也可 ["preproc.out"] 等
    for s in ps:
        print(s.stage_id, s.seq, s.payload_len)  # 每条样本的基本字段
        if s.meta is not None:                   # 大张量带 TensorMeta
            print(s.meta["shape"], s.meta["width"], s.meta["height"])
            arr = s.array                        # 零拷贝 numpy 视图（本次迭代内有效）
```

## stage 与传输方式

| stage | 内容 | 传输 |
|---|---|---|
| `metrics` | 每帧/周期指标 JSON（小） | inline —— payload 随消息直接带字节 |
| `preproc.out` | 预处理输出张量（大） | memfd —— 经 SCM_RIGHTS 传 fd，SDK mmap 成只读视图 |
| `npu.raw` | NPU 原始输出 | 视大小 inline / memfd |
| `postproc.out` | 后处理输出 | 视大小 inline / memfd |

- **inline vs memfd**：小样本直接内联在消息里；大张量走 memfd（普通只读 mmap，**不是 dma-buf**）。两者都通过 `sample.payload`（bytes 拷贝）或 `sample.array`（numpy 视图）统一访问，调用方无需区分。
- `sample.array` / `sample.payload` 仅在**当前迭代步**有效，循环推进后底层缓冲即释放；要留用请 `.copy()`。

## 依赖

- `librecamera_ext.so.1` **>= 1.2.0**（含 probe 符号 `rc_ext_probe_*`；旧版会抛 "lacks probe support"）+ 含扩展 API 的固件（`/run/recamera/probe.sock` 存在）
- `recamera_ext` Python 包可 import
- `numpy`（仅 `--stage preproc.out` 等读张量 `sample.array` 时需要；`metrics` 不需要）

## 怎么跑

```sh
adb push examples/06-probe/probe_example.py /root/
adb shell 'cd /root && python3 probe_example.py --stage metrics --count 5'
adb shell 'cd /root && python3 probe_example.py --stage preproc.out --count 3'
```

参数：`--stage`（默认 metrics）、`--count`（取多少条后退出，默认 5）、`--sample-every`（每 N 帧采一次，服务端下采样减负载，默认 1）。

若设置了 SDK 路径环境变量：

```sh
export PYTHONPATH=/userdata/sdk/python:$PYTHONPATH
export LD_LIBRARY_PATH=/userdata/sdk/lib:/oem/usr/lib:/usr/lib:$LD_LIBRARY_PATH
```

## 预期输出

`metrics`（inline，payload 是一段 JSON）：

```
subscribed stage='metrics'  mask=0x8  sample_every=1  (Ctrl-C 退出)
[1] stage=metrics seq=101 pts=1699999999 payload_len=326
...
done, 5 sample(s) from 'metrics'.
```

`preproc.out`（memfd，带 TensorMeta）：

```
subscribed stage='preproc.out'  mask=0x1  sample_every=1  (Ctrl-C 退出)
[1] stage=preproc.out seq=101 pts=1699999999 payload_len=1228800
    meta shape=[1, 640, 640, 3] dtype=0 640x640 stride=1920 scale=1.0000 zp=0
    array shape=(1, 640, 640, 3) dtype=uint8
...
```

## 常见问题

- **`librecamera_ext lacks probe support`**：`.so` 是旧版（< 1.2.0）。装 1.2.0 的 SDK。
- **`rc_ext_probe_open failed`**：固件不含扩展 API（`/run/recamera/probe.sock` 不存在），或权限不足（见 `examples/README.md` 通用前置条件）。
- **一直没样本**：确认内建推理正在跑（有模型加载、有帧在推理），probe 只在流水线有数据流动时才有采样。
- **`ModuleNotFoundError: numpy`**：读 `sample.array` 需要 numpy；只看 `metrics` 的 `payload_len` 不需要。
