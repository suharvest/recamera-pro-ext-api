# 音频接入：通过 `ai_asr` 获取原始 PCM

> 适用固件：reCamera Pro（RV1126B）。事实来源：固件内 ALSA 配置
> `/etc/asound.conf`（源码路径 `project/cfg/BoardConfig_Recamera2/overlay/overlay-buildroot-asound/etc/asound.conf`）。

## 能干什么

设备的麦克风硬件通过 ALSA `dsnoop` 插件做了多进程共享。固件预定义了 4 个命名采集 PCM：

| PCM 名 | 占用方 | 说明 |
|---|---|---|
| `ai_main` | rkipc（官方主程序，见 `rkipc-3840x2160.ini` `[audio.0] card_name= ai_main`） | 请勿使用 |
| `ai_kws` | acousticslabd（官方关键词检测服务） | 请勿使用 |
| `ai_asr` | **预留空闲，第三方 ASR/音频应用使用这一路** | 推荐 |
| `ai_debug` | 预留空闲，调试录音用 | 可用 |

第三方进程用 `arecord -D ai_asr`（或任何 ALSA 客户端指定 device 名 `ai_asr`）即可拿到麦克风 PCM，**不需要停止 rkipc，也不与官方音频功能冲突**。

## 前置条件

- 能在设备上运行进程（SSH 登录或随扩展包部署的服务）。
- **需要 audio 组或 root（已验证 2026-08-10，固件 V1.0.10 / kernel 6.1.157，验证受阻并纠正原假设）**：dsnoop 的 IPC 权限确为 `0666`，但每个 dsnoop 客户端仍要打开硬件节点 `/dev/snd/pcmC0D0c` 与 `/dev/snd/controlC0`，实测这两者为 `root:audio` 权限 `0660`。SSH 交互用户 `admin`（uid 1000，仅 `admin` 组，不在 `audio` 组）打开失败，`arecord -D ai_asr` 全部报 `Cannot get card index for 0` / `audio open error: No such file or directory`，`cat /dev/snd/pcmC0D0c` 直接权限拒绝。**结论：仅 IPC 0666 不足以让任意用户采集；原文"无需 root，任意本机用户可打开"不成立。** 实际部署路径（经 `appmgr` 拉起的扩展进程）以 root 运行，可正常访问 `/dev/snd`——第三方应随包以 root/audio 组身份运行，而非依赖 SSH admin 用户。
- 若用 Python，需要 `pyalsaaudio` 或直接 subprocess 调 `arecord`（设备自带 alsa-utils）。

## 信号参数（从 asound.conf 推导）

三层结构：

1. **`mic_hw_shared`（dsnoop 层，asound.conf:5-16）**：共享 `hw:0,0`，固定 **6 通道 / 16000 Hz / S16_LE**，period 1024、buffer 4096 帧。这一层决定了硬件侧的真实采样率——**所有下游都只有 16 kHz 这一种源**。
2. **`ai_2mic_2ref`（route 层，asound.conf:23-35）**：把 6 个硬件通道映射为 4 个输出通道：

   | 输出通道 | 来源硬件通道 | 含义 |
   |---|---|---|
   | ch0 | hw ch0 | Mic 1 |
   | ch1 | hw ch2 | Mic 2 |
   | ch2 | hw ch4 | 参考通道（Reference） |
   | ch3 | hw ch4 | 参考通道复制（Reference Fill） |

3. **`ai_main`/`ai_kws`/`ai_asr`/`ai_debug`（plug 层，asound.conf:43-64）**：全部 slave 到 `ai_2mic_2ref`，四个名字路由完全相同，仅用于按模块区分打开者。plug 层可做自动格式/采样率/通道数转换。

由此得出**原生（零转换）采集参数**：

```
rate=16000  format=S16_LE  channels=4
```

## 接入步骤

### 命令行（推荐先用它验证链路）

```sh
# 原生 4 通道采集 5 秒（ch0=Mic1, ch1=Mic2, ch2/ch3=参考）
arecord -D ai_asr -f S16_LE -r 16000 -c 4 -d 5 /tmp/test_4ch.wav

# 只要单声道语音：可请求 1 通道，由 plug 层自动转换
# （plug 的通道下混行为未逐项核实，建议优先采 4 通道后在软件里取 ch0）
arecord -D ai_asr -f S16_LE -r 16000 -c 1 -d 5 /tmp/test_1ch.wav   # 验证受阻（2026-08-10）：admin 用户无 audio 组权限，采集本身无法执行，下混行为未能实测
```

不要写 `-r 48000` 之类的其他采样率：plug 层会做软件重采样，源仍是 16 kHz，只增加 CPU 开销不增加信息量。

### Python（subprocess + arecord，零额外依赖）

```python
import subprocess, numpy as np

RATE, CH = 16000, 4
proc = subprocess.Popen(
    ["arecord", "-D", "ai_asr", "-f", "S16_LE",
     "-r", str(RATE), "-c", str(CH), "-t", "raw", "-q"],
    stdout=subprocess.PIPE)

CHUNK = RATE // 10  # 100 ms
while True:
    raw = proc.stdout.read(CHUNK * CH * 2)      # S16 = 2 bytes
    if not raw:
        break
    pcm = np.frombuffer(raw, dtype=np.int16).reshape(-1, CH)
    mic1 = pcm[:, 0]        # 喂 ASR 的单声道
    ref  = pcm[:, 2]        # AEC 参考信号
    # ... your model ...
```

### Python（pyalsaaudio，需 `uv add pyalsaaudio`；设备上是否可直接 pip 装取决于你的部署方式）

```python
import alsaaudio
pcm = alsaaudio.PCM(alsaaudio.PCM_CAPTURE, device="ai_asr",
                    rate=16000, channels=4,
                    format=alsaaudio.PCM_FORMAT_S16_LE, periodsize=1024)
while True:
    n, data = pcm.read()   # 验证受阻（2026-08-10）：同上，admin 无 audio 组权限，未能实测
```

## dsnoop 共享原理（为什么与 rkipc 不冲突）

普通 `hw:0,0` 采集是独占的——第二个进程 open 会得到 `Device or resource busy`。`dsnoop` 在内核设备之上建了一块共享内存环形缓冲（`ipc_key 2026`），由第一个打开者驱动硬件，后续打开者只从共享缓冲读拷贝。rkipc（`ai_main`）、acousticslabd（`ai_kws`）和你的进程（`ai_asr`）读的是同一份 6ch/16k 数据流的各自游标，互不阻塞、互不抢占。

## 边界与限制

- **拿到的是原始麦克风信号，没有 VQE（无 AEC/ANS/AGC）。** rkipc 的 VQE 处理发生在它自己的 `RK_MPI_AI` 链路内，不经过 dsnoop 对外。要回声消除/降噪需自理。
- **做 AEC 的参考信号就在流里**：输出通道 ch2/ch3（来源硬件 ch4）按配置注释为播放参考（Reference）。把 ch0 当近端、ch2 当远端参考喂给 AEC 算法（如 speexdsp、WebRTC AEC）即可。参考通道实际是否随扬声器播放出信号，**验证受阻（2026-08-10）**：因 admin 用户无 audio 组权限，未能实际采集 4 通道数据对 ch2/ch3 做 RMS 分析，参考通道假设仍待以 root 身份的进程实测。
- **采样率上限 16 kHz**：dsnoop slave 固定 16000，任何更高采样率都是软件插值。
- **双麦间距/阵列几何参数**：源码内未见声明，做波束成形需自测。（待验证）
- `ai_debug` 与 `ai_asr` 路由相同，同时占用两路没有额外收益；一个应用开一路即可。
- dsnoop IPC 为 0666：任何本机进程都能读麦克风。部署多方共存的机器时注意这一点（固件后续里程碑会收紧权限模型）。

## 故障排查

| 现象 | 排查 |
|---|---|
| `Cannot get card index for 0` / `audio open error: No such file or directory` | **首查权限（2026-08-10 实测最常见原因）**：`ls -l /dev/snd/`——实测为 `root:audio 0660`，当前用户不在 `audio` 组即打开失败，需以 root 或 audio 组身份运行；再 `cat /proc/asound/cards` 确认 card 0 存在。注意 `aplay -L` 不列出这些自定义 PCM（实测仅列 `null`），改用 `grep -nE 'pcm\.ai_' /etc/asound.conf` 确认注册即可 |
| 打开报 busy | `ai_asr` 本身不会 busy（dsnoop 共享）；确认没有直接打开 `hw:0,0` 的旧进程绕过了 dsnoop |
| 采到全零 | 先用 `arecord -D ai_debug` 交叉验证；再确认拾音硬件（拨动开关/隐私开关，如有） |
| 有声音但 ASR 效果差 | 确认没把 ch2/ch3（参考通道）当语音喂进去；确认未做二次重采样 |
| 参考通道全零 | 扬声器未在播放时参考通道无信号属正常；播放中仍全零则参考回采链路需上机核实（2026-08-10：因权限受阻未能实测 4 通道 RMS） |
