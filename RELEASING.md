# 发布维护指南 (RELEASING)

面向**维护者**：如何重新打包并更新 `release/` 下的两个发布物。日常开发/使用不需要读本文，只在要出新版或改了随包内容（SDK / kit / examples）时用。

## 两个包，各自用途

| 包 | 内容 | 给谁 | 装到哪 |
|---|---|---|---|
| `release/recamera-ext-api-v<ver>.tar` | **固件 sideload 包**：patched `rkipc` + `entry.cgi` + SDK + **`wheels/`（rknnlite 运行时）** + `install.sh`/`rollback.sh`/`MANIFEST.txt` | 给设备刷入扩展 API 固件的人 | 覆盖 `/oem`（持久，OTA 会还原）+ provision `/userdata/rknnenv`。设备端步骤见 `release/pkg/README.md` |
| `release/recamera-ext-kit-v<ver>.tar.gz` | **kit 分享包**：`kit/` + `sdk/`（含 `.so` 软链）+ `examples/` + **`wheels/`（rknnlite 运行时）** + `INSTALL.sh` + `SHARE-README.md`。**不含固件** | 给已刷好固件、要在设备上开发 app 的方案商 | `INSTALL.sh` 装到 `/userdata/local/kit` + `/userdata/sdk` + provision `/userdata/rknnenv` |

两者都由 `release/build-release.sh` 从仓内源可复现地组装。

## Python 推理运行时 (rknnlite) 随包 provision

两个包都带 `wheels/`（源在 `release/pkg/wheels/`，`git` tracked），`install.sh` / `INSTALL.sh` 在设备上按以下配方离线装好 Python 推理运行时，vision app 开箱能跑（设备无网）：

1. `ln -sf /oem/usr/lib/librknnrt.so /usr/lib/librknnrt.so`（stock `rknnlite` 硬编码此路径）；
2. `python3 -m venv --system-site-packages /userdata/rknnenv`（numpy 用系统的，不打 wheel）；
3. `/userdata/rknnenv/bin/pip install --no-index --find-links wheels/ rknn-toolkit-lite2 psutil ruamel.yaml ruamel.yaml.clib`；
4. 自检 `from rknnlite.api import RKNNLite; RKNNLite()`。

该段是 **best-effort**：失败仅告警，不阻塞主 `rkipc`/kit 安装。设备运行 vision app：

```sh
PYTHONPATH=/userdata/local:/userdata/sdk/python \
LD_LIBRARY_PATH=/oem/usr/lib \
/userdata/rknnenv/bin/python3 <app>.py
```

换 rknnlite 版本 → 替换 `release/pkg/wheels/` 下 wheel + 更新 `MANIFEST.txt` 的 md5/size，重打两个包即可。

## 何时重打

- 改了随 kit 包分发的内容：`sdk/`、`kit/`、`examples/`、`release/kit-extra/{SHARE-README.md,INSTALL.sh}` → **至少重打 kit 包**。
- 换了 `rkipc` / `entry.cgi`（新固件构建产物）→ 重打固件包（并核对 md5）。
- 升版本号 → 两个都重打（文件名带版本）。

## 从哪拿 rkipc / entry.cgi

固件包需要两个二进制，来源二选一：

1. **wsl 构建产物**：`recamera_ipc` 构建输出目录 `out/bin/`（`rkipc`），以及 M4 控制面 `entry.cgi`。跨机开发见 `recamera-rk-build` skill（代码/编译在 wsl2-local）。
2. **设备现役**：从已验证设备的 `/oem/usr/bin/rkipc`、`/oem/usr/www/cgi-bin/entry.cgi` 拉回（`adb pull` 或 `scp`）。
3. **仓内现役副本**：`release/pkg/rkipc`、`release/pkg/entry.cgi` 就是当前发布的现役二进制（`de5b3aa4` / `75a693c8`）。内容不变时直接用它们重打即可。

## 重打步骤

```sh
# 版本不变、仅随包内容变化（如修了 examples），用仓内现役二进制：
release/build-release.sh \
  --rkipc release/pkg/rkipc \
  --entry-cgi release/pkg/entry.cgi \
  --version 1.2.0

# 换了固件二进制 / 升版本：
release/build-release.sh \
  --rkipc <path/to/new/rkipc> \
  --entry-cgi <path/to/new/entry.cgi> \
  --version 1.3.0 \
  [--factory-md5 <原厂 rkipc md5>]   # 不传则沿用 install.sh 现有值
```

脚本会：
1. 计算 `rkipc`/`entry.cgi`/`.so` 的 md5 与 size；
2. **自动写回** `release/pkg/{install.sh,rollback.sh,MANIFEST.txt,README.md}` 的 md5 常量、size、版本、构建日期（消除手工同步漂移）；
3. 确定性组装两个包（成员排序、`mtime=0`、`gzip` 去时间戳）→ 同输入得同 md5；
4. 自检：`install.sh` 的常量与实际 artifact md5 一致、固件 tar 内成员 md5 正确，否则报错退出。

`--factory-md5`：设备原厂（未打补丁）`rkipc` 的 md5，供 `install.sh`/`rollback.sh` 校验回滚目标。不传则沿用现值 `d5e7ca93…`。

## 验证

```sh
# 脚本末尾已打印两个 tar 的 size + md5；再核对随包内容：
tar tzf release/recamera-ext-kit-v<ver>.tar.gz | grep -iE 'rkipc|entry.cgi|market|models|internal'   # 应为空（kit 包不含固件/权重）
tar tf  release/recamera-ext-api-v<ver>.tar                                                          # rkipc/entry.cgi/sdk/install.sh 齐全
tar tf  release/recamera-ext-api-v<ver>.tar | grep wheels                                            # 4 个 rknnlite wheel 在包内
tar tzf release/recamera-ext-kit-v<ver>.tar.gz | grep wheels                                         # kit 包同样带 wheels

# 重复跑一次，两次 md5 应完全相同（确定性）：
release/build-release.sh --rkipc release/pkg/rkipc --entry-cgi release/pkg/entry.cgi --version <ver>
```

设备端端到端验证（刷固件包后）见 `docs/guide/deploy-ops.md` §5 自检清单。

## 提交

`release/build-release.sh` 会顺带改动 `release/pkg/` 的元数据文件。按功能拆 commit：

```sh
git add release/pkg/{install.sh,rollback.sh,MANIFEST.txt,README.md}   # 若 md5/版本有变
git add release/recamera-ext-api-v<ver>.tar release/recamera-ext-kit-v<ver>.tar.gz
git commit -m "chore(release): 重打 v<ver> 两包"
```

> 注：`market/` 是 gitignored，其内容不进包也不进 commit。
