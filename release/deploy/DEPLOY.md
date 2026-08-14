# reCamera Pro 部署说明(总览)

> **读者**:要把扩展 API / 应用中心装到设备上的人。
> **本文回答**:装什么、用哪个脚本、什么顺序、出事怎么退。
> 与本目录 `README.md` 的分工:**README 讲某一个版本的包(含校验值),本文讲部署流程本身**,不随版本变。
> **校验值不在本文** —— 以随包发布的 `README.md` 文末「校验」表为准(它与包同批上传,不会漂移)。

---

## 0. 一句话

日常只用一条命令:

```bash
cd release/deploy && ./deploy-app.sh --host <设备IP>
```

它只动**应用层**,不碰固件。固件是另一条高危路径,单独跑、且需要你能物理复位设备。

---

## 1. 设备上有哪几层

从下到上,越下越危险、越少动:

| 层 | 位置 | 谁装 | 多久动一次 |
|---|---|---|---|
| **固件**(rkipc / entry.cgi / `librecamera_ext.so`) | `/oem/usr/{bin,www/cgi-bin,lib}` | `deploy-firmware.sh` | 很少;**换 rkipc 必须冷启动** |
| **kit 运行时 + SDK + venv** | `/userdata/local/kit`、`/userdata/sdk`、`/userdata/rknnenv` | `deploy-app.sh` 第 1 步 | 跟随 kit 改动 |
| **appmgr**(应用中心后端) | `/userdata/local/appmgr` | `deploy-app.sh` 第 2 步 | 跟随 appmgr 改动 |
| **前端**(React 静态产物) | `/oem/usr/www` | `deploy-app.sh` 第 3 步 | 跟随前端改动 |
| **应用**(9 个 app 的代码+manifest) | `/userdata/local/apps/<id>` | `deploy-app.sh` 第 4 步 / App Center 安装 | 经常 |
| **用户配置** | `/userdata/local/appdata/<id>/config.json` | 用户在 UI 改 | **不随升级丢失** |
| **共享模型** | `/userdata/local/models/` | catalog `putModel` / provision 脚本 | 很少(体积大) |

> 用户配置**不在**安装目录内。这是有意的:升级是整目录替换,任何放在 `apps/<id>/` 里的东西都活不过一次升级。旧位置的文件会在首次读/写/安装时自动迁移,并留下 `.migrated` 备份。

---

## 2. 常规部署(应用层)

```bash
cd release/deploy
./deploy-app.sh --host 192.168.x.x      # 指定设备 IP(IP 易变,先确认)
./deploy-app.sh --skip-kit              # kit 没变,只更 appmgr/前端/apps
./deploy-app.sh --no-activate           # 不启动 app、不碰摄像头
```

**前提**:控制机装了 `adb`;设备 `adbd` 以 root 跑(`adb connect <ip>:5555`),**不需要 SSH 密码,也不需要 sudo**。`/userdata` 需 ~200 MB 余量。

**5 步(每步幂等,替换前按时间戳备份到 `/userdata/_deploy/backups/`)**:

1. **kit + SDK + wheels** — 跑 kit 包自带的 `INSTALL.sh`
2. **appmgr** — merge-extract(覆盖 `.py` 与 `keys/`,保留运行态 `audit.log`/`mqtt.json`/锁),然后重启服务
3. **前端** — `static/` 整目录替换(清掉旧 hash bundle),**目录 755 / 文件 644**
4. **apps** — merge-extract 代码与 manifest,**保留设备上已有的大模型文件**
5. **激活 + 校验** — 激活一个 app,确认 `:8124` 结果流出帧,查 dmesg 无 VPSS

**内置安全护栏**:
- **绝不碰 rkipc**:部署前后各取一次 `/oem/usr/bin/rkipc` 的 md5,收尾断言未变,变了直接 FATAL
- 每个碰摄像头的步骤后查 `dmesg`,出现 `vpss err` / `CSIBDG fifo overflow` / `Oops` 立即停
- 大文件 push 用 **md5 校验 + 重试**(adb over Tailscale 大包偶发 `EOF`)

---

## 3. 固件层(高危,单独跑)

> ⚠️ **换 rkipc 必须冷启动(整机 `reboot`)激活。热替换会触发 `cv181x_vpss` / CSIBDG FIFO 内核 oops,可能把设备搞挂。只在你能物理复位设备时才跑。**

```bash
./deploy-firmware.sh --host <ip>            # 装入 /oem,停在 reboot 前
./deploy-firmware.sh --host <ip> --reboot   # 装完立即重启
./deploy-firmware.sh --host <ip> --rollback # 回滚原厂 rkipc
```

脚本会要求你手工输入 `I-HAVE-PHYSICAL-ACCESS` 才继续。

**只有需要扩展 API 能力(帧代理 / 结果回注 / 硬件遮罩 / probe)时才装固件。** `deploy-app.sh` 不依赖它也能把 apps/前端/appmgr 跑起来。

**回滚目标有白名单保护**:`install.sh` 只接受经校验的**干净原厂** rkipc 作为回滚点(`VERIFIED_FACTORY_MD5S`);若 `/oem` 当前是**已知扩展构建**或未知构建,它会拒绝把那个当"原厂"备份并退出 —— 否则日后"恢复出厂"会变成空操作、扩展固件永远留在设备上。

---

## 4. 一次性运行时补给

这些**不在** `deploy-app.sh` 里,首装或 OTA 后需要单独跑:

| 脚本 | 解决什么 | 谁需要 |
|---|---|---|
| `market/deploy/provision-runtime.sh` | 让 `recamera_ext` 在 rknn venv 里可导入 | **所有 app** |
| `market/deploy/provision-voice.sh` | 音频依赖(`voxedge`/`sherpa_onnx`/…)+ ASR 模型 | 只有 `voice-transcribe` |
| `market/deploy/appmgr-restore.sh` | OTA 会冲掉 `/etc/init.d/S94appmgr`,靠它恢复 | **OTA 之后** |

> ⚠️ **`provision-voice.sh` 目前无法开箱使用**:脚本在,但它要的 payload(离线 wheel + ~133 MB ASR 模型)既不在仓里、也不在发布包里、也不在 CDN 上,且 `deploy-app.sh` 不调用它。**因此没有手工 provision 过的设备,装上 `voice-transcribe` 也起不来**(表现为运行时 `ModuleNotFoundError: No module named 'voxedge'`,而不是安装失败)。其余 8 个视觉 app 不受影响。

装一个"带 venv + 共享模型"的 app 的完整顺序:
```
provision-runtime.sh  →  provision-voice.sh(仅语音)  →  装 app 包  →  激活
```
视觉 app 只需第 1、3、4 步。

---

## 5. 出事怎么退

| 层 | 回滚方式 |
|---|---|
| **应用层** | `/userdata/_deploy/backups/` 下有每次部署的时间戳备份(`appmgr.<ts>/`、`www.<ts>.tar.gz`、`state.json.<ts>`);kit/SDK 旧副本在 `/userdata/local/kit.bak.<ts>` |
| **单个 app 升级** | 升级会保留上一代为 `<app_dir>.prev`(只留一代),`mv` 回去即可 |
| **用户配置** | 卸载**不删**配置(重装即恢复);旧位置迁移时留有 `config.json.migrated` |
| **固件** | `./deploy-firmware.sh --host <ip> --rollback`,恢复后**需冷启动** |

设备上 `tar` 是 busybox 版,**没有 `-z`**,解压要 `gzip -dc x.tar.gz | tar xf -`。

---

## 6. 从 CDN 取包

```bash
BASE=https://sensecraft-statics.seeed.cc/solution-app/recamera_pro/release/<版本>
mkdir -p deploy && cd deploy
for f in recamera-ext-kit-*.tar.gz recamera-ext-api-*.tar appmgr-*.tar.gz \
         frontend-*.tar.gz apps-*.tar.gz deploy-app.sh deploy-firmware.sh README.md; do
  curl -fSL "$BASE/$f" -o "$f"
done
chmod +x deploy-app.sh deploy-firmware.sh
curl -fsSL "$BASE/README.md" | sed -n '/^## 校验/,$p'    # 核对 md5
```

---

## 7. 踩过的坑(照做能省事)

- **`KIT_PARENT` 是"装着 `kit/` 的那一层"**,即 `/userdata/local`,**不是** `/userdata/local/kit`。写错会让所有 app 起不来(`ModuleNotFoundError: No module named 'kit'`),且 `S94appmgr` 不设这个变量,**重启不会自愈**。现在 appmgr 用绝对路径执行 `<KIT_PARENT>/kit/run.py`(自定位),不再依赖 `PYTHONPATH` 配对。
- **前端目录权限**:`cp -a` 出来的目录可能是 700,nginx(www-data)进不去,`try_files` 会回落到 `index.html`,表现为"JS 返回了 HTML"。**目录 755 / 文件 644**。
- **在 macOS 上打包前端要 `COPYFILE_DISABLE=1`**,否则 tar 里混进 `._*` AppleDouble 文件,一路带到 `/oem/usr/www`。
- **停 appmgr 不要用 `pkill -f 'appmgr serve'`** —— 它会匹配到执行重启的那条命令本身,把自己的 shell 杀掉。扫 `/proc/<pid>/cmdline` 并排除当前 shell,再 `setsid` 起新的。
- **同一时刻只能有一个 app 用摄像头**。切换用 `POST /api/appMgr/activate`(单活互斥),它会先关掉内建推理。
- **Tailscale 上大文件 adb push 偶发 `device offline`** —— 设备通常没事,按 md5 判定并重试,或打成单个 tar 再传。
