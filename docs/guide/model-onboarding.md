# 模型上板：从 ONNX 到设备可运行（zero-to-deployed）

面向**方案商**：把你自己的模型放到 reCamera Pro（Rockchip RV1126B）上跑起来，
端到端主线是

```
ONNX 导出 → 检查 IR/opset → RKNN 转换 → 量化校准 → 放进 app 的 models/ +
manifest 声明 → 打包安装 → 激活 → 验证输出
```

本文是**框架与串联**：每一步给出该用哪个脚本、卡在哪、去哪看细节。模型专属的
算子改写、量化精度调参等**深度细节在转换项目 `models/convert/` 里**（脚本 +
`helpers/`），本文只指路，不复述。拿不准的地方按“需核实 / 详见 `models/convert/`”
处理，不要凭空发挥。

相关文档：
- app 打包与发布：[app-center-publishing.md](./app-center-publishing.md)
- app 形态与 `self.models` 加载：[kit-design.md](./kit-design.md)、
  内部规范 `internal/KIT_APP_SHAPE_SPEC.md`
- 独有 Python 依赖（非模型）：[per-app-dependencies.md](./per-app-dependencies.md)
- 输入预处理（letterbox / RGA 硬件预处理）：[hw-preprocess.md](./hw-preprocess.md)
- 结果输出（overlay / MQTT / HTTP…）：[output-sink.md](./output-sink.md)、
  [result-push.md](./result-push.md)

---

## 0. 前置条件

| 项 | 要求 | 说明 |
|----|------|------|
| 转换工具链 | `rknn-toolkit2` **2.3.x**（x86 Docker） | 版本必须与**设备端** `librknnrt 2.3.2` 一致，否则产物 load 失败 |
| 目标平台串 | `rv1126b`（全小写） | **不是** 老的 `rv1126`（那是 legacy rknn-toolkit 1.x） |
| ONNX | IR ≤ 高版本兼容、opset 常规（需核实你算子的支持面） | 见 §2 检查 |
| 设备端 venv | `/userdata/rknnenv`（`rknnlite` + `numpy` + `cv2`） | 平台共享基础环境，视觉 app 复用；见 per-app-dependencies.md |

`rknn-toolkit2` 是 x86-only 的转换器，**在 Mac / 设备上都跑不了**，必须用 x86
Docker 容器。设备端只装 `rknnlite`（运行时），不装转换器。

---

## 1. 导出 ONNX

从你的训练框架（PyTorch / TF…）导出 ONNX。要点：

- **固定 batch 和输入尺寸**（例如 `[1,3,640,640]`），别留动态维度——NPU 编译期
  需要静态 shape。
- **把后处理留在图外**：YOLO 类模型建议导出 **raw head**（各 stride 的 leaf-Conv
  输出），把 DFL / NMS / decode 放到设备侧 numpy 后处理里。仓内已上板的模型都是
  这个约定（见 `models/rawhead/*.onnx` 与 `models/convert/helpers/export_pose.py`
  等导出脚本）。原因：图内后处理算子常在 NPU 上不支持或掉精度。

模型专属的导出改写（人脸 cls、facemesh NCHW、ppocr、emotion 静态化等）在
`models/convert/helpers/`，每个 `fix_*.py` / `prep_*.py` / `export_*.py` 对应一类
模型，配套 `helpers/README.md`。**你自己的模型**参照最接近的那个改写。

---

## 2. 检查 ONNX（IR / opset / 输入输出）

```bash
python3 models/convert/inspect_onnx.py your_model.onnx
```

打印 IR 版本、opset、每个输入/输出的名字与 shape、节点数。用它确认：
- 输入 shape 是你期望的静态 `[1,3,H,W]`（或模型对应布局）；
- 输出就是你要的 raw head 张量，名字与后处理约定一致；
- opset 落在工具链支持区间（**需核实**你所用算子在 rknn-toolkit2 2.3.x 的支持
  情况；不支持的算子要在导出阶段改写或换等价实现）。

---

## 3. ONNX → RKNN 转换

统一入口 `models/convert/convert.py`（在 x86 Docker 内运行）。**先做 FP16 打通
链路**，再上 INT8。

FP16（不量化，最快跑通）：
```bash
python convert.py --onnx your_model.onnx --out your_model_fp16.rknn
```

关键参数（`convert.py --help` 为准）：

| 参数 | 默认 | 说明 |
|------|------|------|
| `--platform` | `rv1126b` | 目标平台，勿改 |
| `--quant` | `fp16` | `fp16` 不量化 / `int8` 量化（需 dataset，见 §4） |
| `--mean` / `--std` | `0,0,0` / `255,255,255` | 归一化。若把归一化烘进 RKNN（如 YOLO 的 /255），**设备端 app 就喂 RAW uint8 像素，不要在 host 再 /255** |
| `--yolo-head` | `none` | `auto/detect/pose`：自动识别 YOLO head 分支分组（box/cls/kpt），非 YOLO 用 `none` |
| `--dataset-dir` / `--dataset-count` | — / 200 | INT8 校准图目录与张数 |

**mean/std 与设备端预处理必须成对**：归一化放在哪一侧要一致，否则精度全错。
参考 hw-preprocess.md 的 letterbox 约定（padding 值、RGA 预处理）。

---

## 4. 量化校准（INT8）

INT8 更快更省，但需要一批**有代表性的校准图**（覆盖真实部署场景的分布）：

```bash
python convert.py --onnx your_model.onnx --out your_model_int8.rknn \
    --quant int8 --dataset-dir /path/to/calib_images --dataset-count 200
```

`convert.py` 会从 `--dataset-dir` 采样 `--dataset-count` 张写成 `.dataset.txt`
（与产物同名，见 `models/converted/*.dataset.txt` 的既有样例），再用它跑
`rknn.build(do_quantization=True)`。

要点：
- **校准图要贴近现场**（光照、角度、目标尺度），拿不相关的图校准会掉精度。
- **量化算法 / per-channel / 混合精度等深度调参** 依赖具体模型，属于
  `models/convert/` 项目范畴——**详见 `models/convert/` 与各 `helpers/`**，本文不
  复述。仓内可见 `fairface` 的 `int8` / `int8_kl` / `int8_mmse` 多套量化产物即是
  这类调参的产出，作为参照。
- FP16 与 INT8 产物都保留、对比精度后再决定上哪个（见 §5 设备验证）。

---

## 5. 设备端验证（上板前的第一道门）

先证明产物能被**设备端** `librknnrt` 加载、初始化：

```bash
# 拷到设备后，在设备上：
python3 device_verify.py /path/to/your_model.rknn
```

`load_rknn` + `init_runtime` 返回 0 才算通过（工具链版本不匹配、算子不支持都会在
这一步暴露）。进一步的**推理数值验证**（喂真实输入、比对 host ONNX 输出）用
`models/convert/device_infer_verify.py` / `device_pose_verify.py` / `validate_pose.py`
（按任务类型选）。**先过设备验证，再接 app**——否则 app 起不来时分不清是模型问题
还是 app 问题。

---

## 6. 放进 app：目录 + manifest 声明

把验证过的 `.rknn` 放到 app 的 `models/` 下，用相对路径在 `manifest.json` 里声明：

```
apps/<your-app>/
├── manifest.json
├── app.py
└── models/
    └── your_model_int8.rknn
```

```jsonc
{
  "id": "your-app",
  "version": "1.0.0",
  "entry": "app.py",
  "models": [
    {
      "id": "your_model",
      "file": "models/your_model_int8.rknn",   // 相对 app 目录
      "task": "detection",                       // detection/pose/classification/…
      "input": [1, 640, 640, 3],
      "quant": "int8"
    }
  ],
  "default_model": "your_model"
}
```

`models/` 目录在打包时**整树进包**（打包器 `market/packaging/build.py` 收整个 app
目录树，模型随之一起）。多个模型（级联）就在 `models[]` 里逐个声明。

> 只需**模型**是共享大文件时，可走 catalog 的 `models[]` + `putModel` 让多个 app
> 复用同一份权重，避免每个包都塞一份大模型——见
> [app-center-publishing.md](./app-center-publishing.md) 的“模型放哪、怎么被加载”。

---

## 7. kit 怎么加载模型（app 不用自己 load）

**kit 在 `App.start()` 里预加载 manifest 的全部 `models[]`**（把相对路径按安装目录
转绝对，逐个构建 NPU 模型），并挂到 `self.models` 上。app **不调用任何
`self.models.load(...)`**（没有这个 API）——直接用即可：

```python
class YourApp(App):
    def setup(self, config):
        pass  # 模型已由 start() 预加载

    def on_frame(self, x):
        out = self.models.your_model.infer(x.data)   # 按 manifest id 取
        # 或单模型时 self.models[0].infer(...)
        ...
```

`--model` CLI 参数（supervisor 用 `models[0].file` 传入）会覆盖**第一个** manifest
模型的路径，用于临时换模型调试。app 形态细节见
`internal/KIT_APP_SHAPE_SPEC.md` 与 [kit-design.md](./kit-design.md)。

**热更新**：`apply:"live"` 的配置项经 SIGHUP 由 kit 自动重新绑定到 `self`，需要
重建派生对象时覆盖 `on_params_changed(changed)`（不是 `on_config_reload`——后者是
更底层的兜底钩子，一般不用覆盖）。**换模型 / 改 input_size / 换 backend 属于
`apply:"restart"`，不走热更**。

---

## 8. 打包 → 安装 → 激活 → 验证输出

1. **打包**：`python3 market/packaging/build.py apps/<your-app>` → 生成
   `<id>-<ver>-arm64.tar.gz`（整树进包，`__pycache__`/构建产物自动排除）。
2. **安装**：走应用中心 / appmgr 装包（见
   [app-center-publishing.md](./app-center-publishing.md)、
   [deploy-ops.md](./deploy-ops.md)）。
3. **激活**：推理即应用——激活你的 app（单活语义，激活即停掉上一个占用摄像头的
   app）。
4. **验证输出**：结果通过声明的 output 通道出来（overlay WS / MQTT / HTTP…），看
   [output-sink.md](./output-sink.md)、[ai-result-overlay.md](./ai-result-overlay.md)。
   发送侧的本地计数（`sent` / `oversize_rejected` / `send_error`）可用 sink 的
   `stats()` 查（诊断“结果发出去没有”），细节见 [result-push.md](./result-push.md)。

---

## 9. 常见卡点

| 现象 | 多半是 | 对策 |
|------|--------|------|
| `load_rknn` / `init_runtime` 非 0 | 工具链版本 ≠ 设备 `librknnrt 2.3.2` | 用 2.3.x 重转 |
| 转换报算子不支持 | 图内后处理 / 冷门算子 | 导出 raw head，把后处理移到设备 numpy 侧；参照 `helpers/` 改写 |
| 检测框飘 / 精度崩 | mean/std 两侧不一致，或归一化被做了两次 | 对齐 host 转换与设备预处理，二选一做归一化 |
| INT8 精度明显掉 | 校准集不具代表性 / 数量太少 | 换贴近现场的校准图、加量；必要时回退 FP16 或做量化调参（`models/convert/`） |
| app 起不来但模型已过 `device_verify` | app 侧问题（manifest / 依赖 / 摄像头占用） | 分段排查；见 app-center-publishing.md、deploy-ops.md |

深度的量化 / 算子 / 模型专属改写：**详见 `models/convert/` 项目及其 `helpers/`**。
本文覆盖不到的模型类型，按最接近的既有转换脚本类比，验证通过再上板。
