# 07 — 共享模型分发（`models[]` + `target_path`）

> 状态：**可用 / 已实现**。本示例讲的是**应用中心「上架 / 发布」流程**，不是设备上的 SDK 调用。
> 活样本：`apps/voice-transcribe`（一个真实在用的 app）。

## 解决什么问题

大多数 reCamera Pro app 的模型**打进包**（tar 里带 `models/`，`build.py` 的
`INCLUDE_TOP` 含 `"models"`），安装即到位，无需这套机制。

但有些 app 的模型是**又大又共享**的资产：多个 app 复用同一份、放在设备上一个约定目录里。
典型是 **`voice-transcribe`** 的 SenseVoice ASR 模型——单个 `.rknn` 就 **~127 MiB**，落在
`/userdata/local/models/asr/`，别的语音 app 也能用。把它塞进**每个** app 包既浪费带宽、又
撑爆包体上限（`MAX_PKG_BYTES`）。

**方案**：app 包**不带**大模型，模型走 catalog 的 `models[]` 单独下发。设备离线（usb0
默认路由指回自己，够不到公网），所以由**浏览器**（用户实际坐在的、有公网的机器）代取模型、
校验 sha256、在 `/install` 之前推给设备。设备端一个字节都不主动外联。

## 三个角色（发布链路）

```
market/catalog/models.json     # 声明：app id -> {target_path, files[]}   ← 人手写这一处
        │
        ▼
market/packaging/models/<app_id>/<file>   # 暂存真实模型字节（不进 git 大概率）
        │
   gen_catalog.py               # 读 models.json + 暂存字节 → 算真实 sha256/size
        │                       #   → 产出 catalog.json 里每个 app 的 models[]
        ▼
catalog.json  apps[].models[] = [{url, filename, sha256, size, target_path}, ...]
        │
        ▼
浏览器安装环                    # 逐个 model: download(url) → 校验 sha256
        │                       #   → POST /api/appMgr/putModel（raw bytes）
        ▼
设备 /userdata/local/models/asr/*   # modelstore.write_model 原子落盘 + 复算 sha256
```

## 1) `models.json`：声明共享模型（唯一手写处）

`market/catalog/models.json`——把 **app id** 映射到它需要下发的**共享模型文件**：

```jsonc
{
  "voice-transcribe": {
    "target_path": "/userdata/local/models/asr",
    "files": [
      "sensevoice_rv1126b_w4a16.rknn",
      "am.mvn",
      "embedding.npy",
      "chn_jpn_yue_eng_ko_spectok.bpe.model"
    ]
  }
}
```

- `target_path`：设备上的落点目录（**绝对路径**，必须落在 `/userdata/local/models` 白名单内，
  见下方「安全」）。
- `files[]`：只写**裸文件名**。真实字节暂存在 `market/packaging/models/<app_id>/<filename>`
  （默认 `--models-dir`），`gen_catalog` 从那里读并算哈希。
- **不在此 map 里的 app** → catalog 里 `models: []`（它的模型打在包里）。
- `_` 开头的 key 是注释，`gen_catalog` 会忽略。

> app 侧配合：`voice-transcribe/manifest.json` 里 `"models": []`、`"needs_model": false`，
> app 目录下**没有** `models/`（只有 `app.py` + `manifest.json`）——所以打出来的包不含模型。
> 模型完全靠这条 `models[]` 链路下发。app 代码从 `target_path`（这里通过 config
> `model_dir` 默认 `/userdata/local/models/asr`）读模型。

## 2) 暂存模型字节

```
market/packaging/models/voice-transcribe/
├── sensevoice_rv1126b_w4a16.rknn        (~127 MiB)
├── am.mvn
├── embedding.npy
└── chn_jpn_yue_eng_ko_spectok.bpe.model
```

缺文件是**硬错误**：`gen_catalog` 宁可当场失败，也不产出一个「指向浏览器取不到的模型」的
catalog（`_build_models` 里 `raise SystemExit`）。

## 3) `gen_catalog.py`：产出 `models[]`

```sh
# 扫 ../packaging/dist/*.tar.gz + 读 models.json + 算暂存模型哈希 → catalog.json
python3 market/catalog/gen_catalog.py

# 生产：模型走 CDN，用 --models-base-url 指定前缀（默认从包 base 推导：
#   .../packages/ -> .../models/）
python3 market/catalog/gen_catalog.py \
    --base-url https://cdn.example/recamera_pro/packages/ \
    --models-base-url https://cdn.example/recamera_pro/models/
```

每个 app 在 catalog 里得到一段 `models[]`（哈希/大小是对**真实暂存字节**算的，非手写）：

```jsonc
{
  "id": "voice-transcribe",
  "name": "Voice Transcribe",
  "version": "0.1.0",
  "arch": "arm64",
  "package": { "url": "...", "filename": "...", "sha256": "...", "size": ... },
  "models": [
    {
      "url": "/appcenter/models/voice-transcribe/sensevoice_rv1126b_w4a16.rknn",
      "filename": "sensevoice_rv1126b_w4a16.rknn",
      "sha256": "…64hex…",
      "size": 133468923,
      "target_path": "/userdata/local/models/asr"
    }
    /* am.mvn / embedding.npy / *.bpe.model 各一条，target_path 相同 */
  ]
}
```

## 4) 浏览器：安装前 `putModel` 下发

安装环在 `/install` **之前**，对每个 `app.models[]`：download → 校验 sha256 → 把原始字节
POST 到设备的 `putModel` 端点。请求头（**与 `server.py` 一致**）：

```
POST /api/appMgr/putModel        # 注意路由是 /api/appMgr/putModel
Content-Type: application/octet-stream

X-Filename:     sensevoice_rv1126b_w4a16.rknn   # 裸 basename，禁路径分隔符
X-Target-Path:  /userdata/local/models/asr      # 必须在 /userdata/local/models 白名单内
X-Sha256:       <64hex>                          # 可选；服务端复算，不匹配即删文件

<raw model bytes as body>
```

伪代码：

```js
for (const m of app.models || []) {
  const buf = await downloadBuf(m.url);
  if (await sha256Hex(buf) !== m.sha256) throw new Error("model sha256 mismatch");
  await fetch("/api/appMgr/putModel", {
    method: "POST",
    headers: {
      "X-Filename":    m.filename,
      "X-Target-Path": m.target_path,
      "X-Sha256":      m.sha256,
    },
    body: buf,
  });
}
// 所有 model 到位后，再 /upload 包 + /install
```

## 设备端落盘（`modelstore.write_model`，已硬化）

`putModel` 把字节交给 `modelstore.write_model()`，它把请求当**敌意输入**处理：

- **目标根白名单**：`target_path` 必须解析在 `/userdata/local/models`（`APPMGR_MODEL_ROOTS`）
  之内——`/etc`、`/oem`、`..` 逃逸、逃逸性 symlink 全部拒绝（词法 + realpath 双重检查）。
- **文件名**：裸 basename `[A-Za-z0-9][A-Za-z0-9._-]{0,127}`，无分隔符 / 无 `..`。
- **大小上限**：`MAX_MODEL_BYTES`（默认 256 MiB，与 nginx `client_max_body_size 256m` 对齐）。
- **原子写**：同目录 temp → fsync → `os.replace`，崩溃不留半个模型。
- **sha256**：调用方给了期望值就复算，不匹配**删文件**再报错。

## 要点回顾

- **打包时**：app 包**不带**大模型（app 目录无 `models/`，manifest `models: []`）。
- **声明**：`market/catalog/models.json` 一处手写 `id -> {target_path, files[]}`。
- **产出**：`gen_catalog.py` 算真实 sha256/size，emit catalog `models[]`
  `{url, filename, sha256, size, target_path}`。
- **下发**：浏览器逐个 `POST /api/appMgr/putModel`（`X-Filename`/`X-Target-Path`/`X-Sha256`
  + raw bytes），设备原子落到 `target_path`。
- **对照**：per-app **依赖**（不是模型）的下发见 [`08-app-with-deps/`](../08-app-with-deps/)
  （设计中，未实现）。
</content>
</invoke>
