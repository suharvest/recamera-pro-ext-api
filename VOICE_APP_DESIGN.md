# 语音应用设计:唤醒词 → 采集音频 → 转录(reCamera Pro)

> 新增一个语音应用 `voice-transcribe`:**本地唤醒词唤醒 → 采集本地音频 → STT 转录 → 出文本**。复用本地 `seeed-local-voice`(OpenVoiceStream)的引擎。
> 关联:`BOOTSTRAP_PATH.md`(AudioSource 适配层)、`RECAMERA_PRO_INFERENCE_SDK_DESIGN.md`(R8 音频代理/VAD)、`PYTHON_KIT_DESIGN.md`(应用分层)、audio 发现(记忆 [[recamera-pro-app-center]])。

## 0. 一句话
`voice-transcribe` = 一个 self-hosted 应用:kit 的 **AudioSource** 拿 16k 单声道 PCM → **KWS 唤醒词**常听 → 触发后录一段 → **voxedge/sherpa ASR** 转录 → 文本经 ResultSink WS 到 `/appcenter`。**复用 voxedge 引擎,不重造 ASR**;音频获取走我们已设计的 AudioSource 适配层(现在接管 mic,官方 R8 到了换成 VQE-clean PCM,应用不改)。

## 1. 复用 seeed-local-voice(OpenVoiceStream)哪部分
它跨三仓,**我们只取最轻的引擎层**:
- ✅ **voxedge**(`../voxedge`,纯 Python/numpy,pip 可装 `pip install --pre voxedge`):ASR/VAD 的 ABC + 后端 `backends/{rk(RKNN),sherpa(sherpa-onnx CPU)}`、conversation loop。**这是我们要嵌的**。
- ✅ **third_party/rkvoice-stream**(RK 运行时):Rockchip 上的 ASR 运行时,voxedge `rk` 后端 shell 到它。RV1126B 若走 RKNN ASR 用这条。
- ❌ **server/**(FastAPI + Docker):完整产品服务,太重,**不用**——我们要的是"嵌进 app 的库",不是"起个语音服务器"。
- ❌ **agent/**(ovs_agent mic→speaker apps):是它自己的对话 agent,我们只要 ASR 转录,不要 TTS/对话回路(先不做)。

> 取舍:reCamera Pro 是 2GB / 3TOPS / 4×A53 的小设备,**优先 voxedge 的 sherpa-onnx CPU 小模型 ASR**(SenseVoice-small/Paraformer),稳;RKNN ASR(rkvoice-stream)作为提速可选,需实测放不放得下。

## 2. 三段管线 + 归属

```
mic ──(AudioSource 16k mono)──► KWS 常听 ──唤醒──► 录音窗口 ──► ASR(voxedge) ──► 文本 ──► ResultSink WS ──► /appcenter
      ▲ kit 适配层(接管/官方R8)      ▲ 轻量常驻        ▲ VAD 判结束        ▲ 复用 voxedge      ▲ 复用现有
```

| 段 | 归属 | 今天做法 | 官方/未来 |
|---|---|---|---|
| **取音频** | kit `AudioSource`(已设计) | **接管 mic**:appmgr 激活时关 rkipc 音频→自开 ALSA(2ch/22050→重采样 16k mono)+ 软件降噪 | R8 官方 PCM 代理(VQE-clean 16k,不用关 rkipc) |
| **唤醒词 KWS** | kit `logic/wakeword` | 轻量常听 KWS(见 §3) | R8 暴露 RK 内置 AAD/wakeup(`fw_aad_aivad`)→ 直接订阅唤醒事件 |
| **VAD 断句** | voxedge VAD 后端 | voxedge 自带 VAD(sherpa/silero) | R8 暴露 RK VAD(`rkvad`) |
| **ASR 转录** | voxedge(嵌入) | sherpa-onnx CPU 小模型(SenseVoice/Paraformer)| RKNN ASR(rkvoice-stream)提速 |
| **出结果** | kit `ResultSink` | WS 文本事件 → `/appcenter` 显示 | R2 若要叠加到视频另说 |

## 3. 唤醒词 KWS 选型(待定,给方案)
seeed-local-voice 侧重 ASR/TTS,**未确认自带 KWS**。候选(轻量、常听、CPU):
1. **openWakeWord**(tflite/onnx,自定义唤醒词,几 MB,CPU 几乎无压力)——推荐首选,自定义词方便。
2. **sherpa-onnx KWS**(和 ASR 同栈,一套 sherpa 依赖搞定 KWS+ASR+VAD)——依赖统一,优。
3. **RK 内置 AAD/wakeup**(`fw_aad_aivad.bin`+`wakeup_words`,VQE 里已有)——最省算力,但**没暴露 API**,要 R8 或改 rkipc,现在拿不到 → 归"官方将来"。
> 建议:**先用 sherpa-onnx KWS**(和 voxedge ASR 同栈,依赖最省),或 openWakeWord。做成 kit `logic/wakeword.py` 藏在接口后,将来换 RK 内置只换实现。

## 4. 应用形态(对齐 app-center)
`apps/voice-transcribe/`:
- `manifest.json`:`type: self-hosted`,`needs_model` 视 ASR 后端(sherpa 模型走 app 包或共享),`capabilities:["audio"]`,`config_schema`:{唤醒词、录音最长时长、静音断句阈值、语言、ASR 后端}。
- `app.py`(薄):重写主循环用 `AudioSource` 而非 FrameSource → KWS → 触发录音 → voxedge ASR → `on_transcript` 出文本事件。**业务逻辑独有部分**:唤醒→录音→转录的状态机(idle/listening/transcribing)。
- kit 侧新增:`kit/adapters/audio_source.py`(BOOTSTRAP 里已规划,此应用是首个消费者)、`kit/logic/wakeword.py`、`kit/asr.py`(voxedge 封装,统一 `transcribe(pcm)->text`)。
- 依赖:**voxedge + sherpa-onnx**(比视觉应用重,装进设备共享 venv 一份,不进 app 包;模型文件走 app 包或共享模型库)。这是唯一一个会引入较大依赖的应用,单独说明。

## 5. reCamera Pro 上的硬约束(实测项)
1. **mic 被 rkipc 独占** → 接管方案要验证"能否只关 rkipc 音频不影响视频/RTSP、关/恢复干净"(BOOTSTRAP §5 的待验证项,语音应用强依赖)。
2. **失去官方 VQE**(接管拿原始麦)→ 自带软件降噪(rnnoise/webrtc-apm);边放 TTS 边听的 AEC 尤其受影响(先不做 TTS 回路,规避)。
3. **算力**:sherpa CPU ASR 在 4×A53 上的实时率(RTF)要实测;放不下再考虑 RKNN ASR 或更小模型/分块转录(非实时,录完再转)。**先做"录完再转"(非流式)最稳**,流式作为后续。
4. **依赖体积**:voxedge+sherpa+模型比视觉应用大,确认 `/userdata` 空间(11GB,够);装共享 venv。
5. **中英文**:选支持中英的 ASR(SenseVoice 中英俱佳)。

## 6. 迁移到官方(R8)——只换适配器
- `AudioSource` 从"接管 ALSA"换成 R8 官方 PCM 代理(VQE-clean,不用关 rkipc,可与官方音频共存);
- KWS 换成 R8 暴露的 RK 内置 wakeup;VAD 换 RK `rkvad`;
- **KWS/ASR/状态机/出结果的应用逻辑不改**。这就是适配层的价值(同视觉侧)。

## 7. 分步(实施时)
- P0:kit `AudioSource`(接管 ALSA + 重采样 16k)+ 上机验证"关 rkipc 音频取麦"可行、录一段 WAV 正常。
- P1:嵌 voxedge + sherpa CPU ASR,**非流式**(录完转)跑通一句中/英转录,实测 RTF。
- P2:KWS(sherpa-onnx-kws/openWakeWord)常听 → 唤醒触发录音 → 转录;状态机 idle/listening/transcribing。
- P3:打包成 app 上架 appmgr,`/appcenter` 显示转录文本;config_schema 可调。
- P4(可选):流式转录、RKNN ASR 提速、TTS 回应(要解 AEC)。

## 8. 一句话
复用 **voxedge**(轻量 pip 库,有 sherpa CPU / RKNN 后端)做 ASR,**不自造**;音频走 kit `AudioSource`(现接管 mic、官方 R8 到了换 VQE-clean);唤醒词用 sherpa-onnx-kws/openWakeWord 藏在 `kit/logic/wakeword` 后;做成标准 self-hosted app,状态机(唤醒→录音→转录)是唯一独有逻辑。硬约束:mic 独占接管 + 无 VQE + CPU ASR 实时率,先做非流式最稳。
