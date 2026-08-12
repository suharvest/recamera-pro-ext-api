# 08 — per-app 依赖（方案 B）· SKELETON

> ⚠️ **状态：设计中，尚未实现。** 本示例是**说明性 skeleton**——照 `docs/guide/per-app-dependencies.md`
> 的设计画出「一个带独有依赖的 app 长啥样」，**目前设备上跑不了**（`deps` 字段、`/putDep`
> 端点、per-app venv 机制都还没落地）。别把它当可用示例。落地进度以设计文档为准。
>
> 事实来源：`docs/guide/per-app-dependencies.md`（设计）。可用的对照见
> [`07-shared-model/`](../07-shared-model/)（共享**模型**下发，已实现）。

## 要解决什么

共享基础环境 `/userdata/rknnenv` 提供大而通用的运行时（rknnlite / numpy / cv2 +
`recamera_ext`）。但某个 app 可能要一份**别人用不到**的独有依赖——比如 **PyAV（`av`）**
（rknnenv 里没有），或一份能跑的 gstreamer-python 绑定。既不该塞进共享 base（污染所有 app），
设备又**离线**（够不到 PyPI，不能 `pip install`）。

设计：**让 app 随包分发自己的 wheel，安装时建一个 per-app venv（叠在 base 上）从离线 wheel
装入。** 复用已经为「共享模型」跑通的离线代取思路（对照 07），只是落点是 wheel 目录。

分两阶段（细节见设计文档 §2.2）：
- **阶段 A / bundled（MVP）**：wheel **打进 app 包**（`wheels/`），随包签名覆盖，走现有
  `/upload`+`/install`，零新网络路径。
- **阶段 B / catalog**：大 wheel 走 catalog `deps[]` + 浏览器 `/putDep` 代取（照抄
  `models[]` + `/putModel`），避免每个包都塞几十 MB。

## manifest 声明 `deps`（**未实现字段**）

manifest 顶层新增可选对象 `deps`。**缺省（无 `deps`）= 现状，行为完全不变。**

```jsonc
{
  "id": "clip-recorder",
  "version": "0.1.0",
  "entry": "app.py",
  "interpreter": "/userdata/rknnenv/bin/python",   // 仍写 base env；appmgr 建 venv 后会改指向 per-app venv
  "deps": {
    "strategy": "bundled",          // "bundled"(阶段A) | "catalog"(阶段B)  ← 未实现
    "base_env": "rknnenv",          // 叠在哪个共享 base 上（venv --system-site-packages 继承其 rknnlite/numpy/cv2）
    "python":   ">=3.11",           // 可选：解释器版本护栏（不满足则安装期报错，不静默降级）
    "wheels": [                     // 期望装进 per-app venv 的 wheel（顺序即安装顺序）
      { "file": "wheels/av-11.0.0-cp311-cp311-linux_aarch64.whl",
        "sha256": "…64hex…", "size": 18234567 },
      { "file": "wheels/pillow-10.3.0-cp311-cp311-linux_aarch64.whl",
        "sha256": "…", "size": 1123344 }
    ],
    "pip_args": ["--no-deps"]       // 可选：透传 pip 的白名单参数；--no-index 由 appmgr 强制注入，作者不可覆盖
  }
}
```

字段语义（对齐设计文档 §2.1）：

| 字段 | 含义 | 备注 |
|---|---|---|
| `strategy` | `bundled` = wheel 在包内；`catalog` = 走浏览器代取 | 未实现 |
| `base_env` | venv 叠在哪个共享 base 上（默认 `rknnenv` → `/userdata/rknnenv/bin/python`） | `--system-site-packages` 继承 base 的包，rknnlite 不重复装 |
| `python` | 解释器版本护栏 | 不满足在安装期显式报错 |
| `wheels[].file` | `bundled` 是**包内相对路径**（`wheels/…​.whl`）；`catalog` 只给**文件名** | |
| `wheels[].sha256` / `size` | 安装期二次校验 | `bundled` 已被包签名覆盖 |
| `pip_args` | 透传 pip 的**白名单**参数 | 禁 `--index-url`/`--extra-index-url`/`--find-links`（find-links 由 appmgr 给）；`--no-index` 强制离线 |

> **OPEN QUESTION（设计文档 §2.1 / §7）**：rknnenv Python 的实际 ABI 标签（cp3x / musl vs
> glibc / aarch64）待真机核实——带 `.so` 的 wheel 标签不匹配会在 `pip install` 阶段直接失败。
> 方案商必须提供**该平台预编译**（`linux_aarch64` + musl 兼容）的 wheel；纯 Python wheel 通用。

## 目录布局（阶段 A / bundled）

作者侧打包前：

```
apps/clip-recorder/
├── manifest.json          # deps.strategy=bundled, wheels[]={av-…-linux_aarch64.whl, sha256}
├── app.py                 # import av  (per-app) + import rknnlite (来自 base)
└── wheels/                # ← 作者预下的 aarch64/musl wheel，随包分发
    ├── av-11.0.0-cp311-cp311-linux_aarch64.whl
    └── pillow-10.3.0-cp311-cp311-linux_aarch64.whl
```

打包时 `build.py` 的 `INCLUDE_TOP` **需加 `"wheels"`**（设计文档 §5.1，未实现——现在只含
`manifest.json`/`app.py`/`models`/`hooks`/`run`）。wheel 进 tar → 被 `sign.py` 签名覆盖。

设备端安装后（预期）：

```
/userdata/local/apps/clip-recorder/wheels/*.whl      # 解包落点
/userdata/local/venvs/clip-recorder/                 # per-app venv（app 目录之外）
    └── bin/python                                    # ← interpreter 被改指向这里
```

## 安装时预期行为（**未实现**，设计文档 §2.3 / §2.5）

`do_install()` 解包后、`busy_gate` 内，调用一个新的 `deps.provision(app_id, manifest)`：

```text
1) 幂等重建：rmtree 旧 venv，从干净起
2) 叠在 base 上：python -m venv --system-site-packages /userdata/local/venvs/<id>
   （若设备无 ensurepip → 退化为「解压式」：直接把 wheel(zip) 解到 .../venvs/<id>/site/，
     用 PYTHONPATH 注入。设计文档 §2.5 建议 MVP 走这条，技术风险最低）
3) 离线装：pip install --no-index --find-links=<wheeldir> <wheels...>
   （--no-index 由 appmgr 强制；pip_args 经白名单过滤，禁止引入外部源）
4) 自检：import 关键包，失败 → install 整体失败回滚，不留半成品
```

启动时 `supervisor._resolve_interpreter()` **优先探测 per-app venv**（设计文档 §2.4）：有
`deps` 且 venv 建好 → 用 `/userdata/local/venvs/<id>/bin/python`；无 `deps` → 现状路径，零回归。

## 卸载（uninstall 入口已实现，venv 清理待实现）

- **uninstall 入口已经有了**（对照 07 提到的生命周期）：
  - CLI：`python3 -m appmgr uninstall <id>`
  - HTTP：`POST /api/appMgr/uninstall {id}`（停进程 → 清 active → 删 app 目录）
  - `do_uninstall` 的注释已预留：「删 `/userdata/local/apps/<id>/` 和 **if present** 未来的
    per-app venv `/userdata/local/venvs/<id>`」——即卸载路径**已为 per-app venv 留好钩子**，
    等 `deps` 落地即连带清理。共享模型（`/userdata/local/models`）**不删**（跨 app 资产）。

## 与另外两条机制的关系（三者正交）

| 机制 | 解决 | 示例 | 状态 |
|---|---|---|---|
| 共享 base env `/userdata/rknnenv` | 大而通用依赖（rknnlite/numpy/cv2） | voice-transcribe 的 sherpa 等 | 已有 |
| **共享模型** `models[]` + `target_path` + `/putModel` | 大而共享的**模型**字节 | voice-transcribe 的 ASR 模型 | **已实现**（见 07） |
| **per-app 依赖** `deps[]` + per-app venv | app **独有**的 Python 依赖 | clip-recorder 的 PyAV | **设计中（本示例）** |

同一个 app 可同时用：例如既要 per-app PyAV（`deps[]`）、又要共享 ASR 模型（`models[]`）。

## 想落地这套机制？

从设计文档的「§5 落地增量（改哪儿，file:line）」开始，逐项对现有锚点改动，并先回答
「§7 OPEN QUESTIONS」（尤其 wheel ABI 标签、ensurepip 是否可用、`recamera_ext` 的 `.pth`
在 venv 下是否可见）。**在这些没核实、`deps.provision` 没实现之前，`deps` 字段不会有任何效果。**
</content>
