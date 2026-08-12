# reCamera Pro 扩展 API 部署与运维手册

> **读者**：把扩展 API / 应用中心部署到设备、或负责设备维护的人（Seeed 内部 / 集成商）。
> **对象设备**：reCamera Pro（RV1126B / recamera_v2），kernel 6.1.157、rootfs `/` 与 `/oem` ext4 rw、`/userdata` ext4 rw 无 noexec。
> **依据**：`release/pkg/`（install.sh / rollback.sh / MANIFEST.txt / README.md）、`../../CHANGELOG.md`、`../api/spec.md`、`app-center-publishing.md`，以及真机验证记录（G1-G4 / M1-M3）与踩坑记录。应用中心部署脚本（`S94appmgr` / `ext_appmgr.conf` / `appmgr-restore.sh`）属发布方私有打包流程，不在公开仓。
> **状态**：本文的路径 / 命令 / md5 均取自上述 release 脚本与实测记录，可直接照做；边界（OTA 冲掉、半成品）在文中明确标注。

---

## 1. 部署什么

扩展 API 让方案商**不改固件源码、不重编固件**，跑自己的进程接 `/run/recamera/` 下的 Unix domain socket，就能拿相机帧、把推理结果回注官方 OSD/录像/WS、观测内建流水线。它由以下产物组成：

| 产物 | 安装目标 | md5 | 大小 | 必需性 |
|---|---|---|---|---|
| `rkipc`（含扩展端点：帧代理 / 结果回注 / probe）| `/oem/usr/bin/rkipc` | `2baebbb55155efb0def2c82fb233e345` | 15.5 MB | **核心必需**——扩展端点全在这个二进制里 |
| `entry.cgi`（M4 控制面）| `/oem/usr/www/cgi-bin/entry.cgi` | `75a693c87c317a49c37c4dddb6b9ac7a` | 1.05 MB | 可选（M4 控制面接口）|
| `librecamera_ext.so.1.0.0`（+ `.so.1` `.so` 软链）| `/oem/usr/lib/` | `137251d7e93cb098c5328ec21e2ef61e` | 86 KB | 给方案商 SDK 运行时；仅当有扩展应用用到才需要 |
| `recamera_ext/`（Python ctypes 绑定）| `/userdata/sdk/python/` | — | — | 给方案商，可选（`/userdata` 不受 OTA 影响）|
| `recamera_ext.h`（C 头）| `/userdata/sdk/` | — | — | 给方案商，可选 |

安装后重启，`/run/recamera/` 下出现四个端点：

- `frame.sock` — 帧代理（零拷贝 dma-buf 帧交接，SCM_RIGHTS 传 fd + 96 字节定长头）
- `result-in.sock` — 结果回注（检测 / 分类 / 分割 / 跟踪 / 关键点注入回官方 OSD / 录像 / WS 分发）
- `probe.sock` — 观测面（preproc / npu.raw / postproc / metrics tap）；SDK client `ProbeSource`（v1.2.0）已发布可用，见 `README.md` §4.8。metrics（inline）+ preproc.out（大张量走 memfd）双路已真机验证。
- `apps.d/` — 每 app 控制目录

**已知缺口**：本轮无阻塞性缺口。早先标注的"关键点 WebSocket 解码缺失"已解决——notify 侧 pb2 / parser 已更新，关键点注入与 WS 解码均通。frame / result / probe 主路径完整。

**应用中心**（App Center，可选，方案 B）另有三块产物，见第 4 节：
- 前端 SPA（React，静态 shell）→ `/oem/usr/www`（catalog 静态资源）
- `appmgr` 后端（Python 常驻服务，`python3 -m appmgr serve`，监听 `127.0.0.1:8130`）→ `/userdata/local`
- nginx 边缘配置 `ext_appmgr.conf`（catalog 静态 + `/api/appMgr` 反代）→ `/oem/usr/etc/nginx/`

---

## 2. 两种更新方式

### 2.1 增量 release 包（推荐：快、可回滚）

`release/pkg/` 就是这个增量包。流程：`adb push` 整个包 → 设备上 `sh install.sh`（备份原厂 → md5 校验 → 覆盖 `/oem` → reboot → 自检）。

前提：设备可通过 adb 以 root 访问（`adb connect <ip>:5555`，adbd 以 root 跑）。

```sh
# 在持包机器（Mac / Linux）上：
adb shell "mkdir -p /userdata/ext-pkg"
adb push ./ /userdata/ext-pkg/                    # push 整个 release/pkg 目录
adb shell "sh /userdata/ext-pkg/install.sh"       # 备份原厂 + md5 校验 + 覆盖 /oem
adb reboot                                         # 或 install.sh 传 --reboot
# 等 ~1-2 分钟自检：
adb shell "ls -l /run/recamera/"                  # 期望 frame.sock result-in.sock probe.sock apps.d/
adb shell "md5sum /oem/usr/bin/rkipc"             # 期望 2baebbb55155efb0def2c82fb233e345
```

`install.sh` 是**幂等 + md5 校验**的（见 `release/pkg/install.sh`）：
1. `[1/6]` 校验包内三件产物 md5，不符立即 `exit 1`；
2. `[2/6]` 首次把原厂 `rkipc` 备份到 `/userdata/rkipc.factory.bak`（原厂 md5 `d5e7ca9365dae553e8c7e4c0a0f436ec`）；**若备份既不是已知原厂值也不是本包 rkipc、且 `/oem` 当前 rkipc 也不是本包的**，则拒绝继续（保证永远有一个可回滚目标）；
3. `[3/6]` 首次备份原厂 `entry.cgi` 到 `/userdata/entry.cgi.factory.bak`；
4. `[4/6]` `touch /oem/.wtest` 验证 `/oem` 可写；
5. `[5/6]` `cp` 三件产物进 `/oem` 并**逐个 `chmod 755`**，建立 `.so.1` / `.so` 软链，SDK python + 头拷进 `/userdata/sdk/`，`sync`；
6. `[6/6]` 提示 reboot（或 `--reboot` 自动重启）激活新 rkipc。

**边界（诚实标注）**：这是 **sideload，覆盖 `/oem`**。`/oem` 是 ext4 rw，普通 reboot 持久；但**一次完整固件 OTA / `update.img` 刷写会重写 `/oem`，把 rkipc 还原成原厂**——**OTA 后必须重跑 `install.sh`**。本包不碰分区、shadow、update.img。

回滚见第 6 节（`rollback.sh`）。

### 2.2 完整 OTA 固件（正式、重）

正式发布形态是把扩展改动全量编进 rootfs，打成 `update_ota.tar` / `update.img`，走 RKDevTool / upgrade_tool 或官方 OTA 分发。这属于**打包分发范畴，本轮暂未做**——此处仅标注方向：编译入口 `./build.sh app`（编 rkipc）→ `./build.sh firmware`（打包分区）→ `./build.sh updateimg` / `allsave`（整包），产物在 `output/image/`（见 `recamera-rk-build` skill）。做成 OTA 后，`/oem` 覆盖就成了固件的一部分，不再需要 sideload。

---

## 3. 手动热替换流程（开发 / 单设备）

用于开发迭代或单台设备验证，不走 install.sh、不烧固件，回滚最快。核心是**逐文件覆盖 + 重启让 init 拉起**。

```sh
# 0. 备份原厂（若尚未备份）
adb shell "[ -f /userdata/rkipc.factory.bak ] || cp /oem/usr/bin/rkipc /userdata/rkipc.factory.bak"

# 1. push 新二进制
adb push ./rkipc /userdata/rkipc.new

# 2. cp 进 /oem —— cp 后【必须】chmod 755（见下方关键坑）
adb shell "cp /userdata/rkipc.new /oem/usr/bin/rkipc && chmod 755 /oem/usr/bin/rkipc"

# 3. reboot 让 SysVinit 干净拉起新 rkipc（不要 while-true 重拉，见第 7 节）
adb reboot

# 4. 验证（等 1-2 分钟）
adb shell "md5sum /oem/usr/bin/rkipc"     # 对上目标 md5
adb shell "ls -l /run/recamera/"          # 三 socket + apps.d/
adb shell "dmesg | grep -iE 'vpss|fifo|Oops' | tail"   # 无 VPSS 崩溃
# RTSP：rtsp://<ip>:554/...   结果 WS（本机免 JWT）：ws://127.0.0.1:8123
```

**关键坑**：`adb push` 上来的文件是 `-rw-rw-rw-`（无执行位），busybox `cp` **不保留 / 不补执行位**，拷进 `/oem/usr/bin/` 后 rkipc 会变 `-rw-r--r--`，RkLunch 执行 `rkipc &` 报 `Permission denied`、起不来（无进程、无 `/run/recamera/`）。**cp 二进制进 `/oem` 后紧跟 `chmod 755`**，entry.cgi 同理。

另一种不碰 `/oem` 的验证法（3b 即此法）：直接把自编 rkipc 放 `/userdata/rkipc.xxx` 运行（`/userdata` ext4 rw 无 noexec），原厂 `/oem` 保持不动，验完 `adb reboot` 回原厂。热替换单文件也可用 bind mount（`cp new /userdata/rkipc && mount --bind /userdata/rkipc /oem/usr/bin/rkipc`，`umount` 回滚），适合快速来回切。

> 自编 rkipc 与 entry.cgi 在 V1.0.4 / V1.0.10 两个固件 build 上都能跑（见第 9 节），热替换验证不必逐固件版本重编。

---

## 4. 应用中心部署（方案 B）

应用中心与官方推理**并行**跑：rkipc 独占相机 → go2rtc 出流（RTSP `rtsp://127.0.0.1:5554/live/0` main、`/live/1` sub；HTTP :1984），app 消费共享流 + 自己的 RKNN context。用 `tar.gz 包 + appmgr 自管进程` 取代一代的 `opkg/.deb + /etc/init.d`。

### 4.1 三块产物落位

1. **前端 SPA**：`build` 后落 `/userdata/local/appcenter/www/`（nginx `alias`）。用户从官方 dashboard 加外链，或直接访问 `http(s)://<device>/appcenter/`。
2. **appmgr 后端**：代码 + 状态在 `/userdata/local`（`python3 -m appmgr` 从此解析），`appmgr serve` 监听 loopback `127.0.0.1:8130`，公网面由 nginx 转发。
3. **nginx 边缘**：`ext_appmgr.conf` 放 `/oem/usr/etc/nginx/`，被 `common_relay.conf` 的 `include ext_*.conf`（在 `server{ listen 80; }` 块内）自动加载。它**只新增 location，不改任何官方 conf**：
   - `/appcenter/` → 静态 SPA shell（**匿名**，shell 内无秘密）
   - `/appcenter/ws/results` → 检测结果 WS 反代到 `127.0.0.1:8124`（JWT 门）
   - `/appcenter/go2rtc/` → 视频反代到 `127.0.0.1:1984`（JWT 门）
   - `/api/appMgr/` → 管理 API 反代到 `127.0.0.1:8130`（JWT 门，`client_max_body_size 256m` 给 tar.gz 上传留头、`proxy_read_timeout 200s` 给安装/切换留时间）
   - 鉴权复用官方 `entry.cgi` 的 `auth_request /_jwt_verify`（同源 cookie `token`，浏览器自动带上，WS/`<img>`/`<video>` 同样生效；auth_request 在 WebSocket Upgrade 握手前执行，不破坏 upgrade）。

### 4.2 持久化 S94appmgr（+ OTA restore 机制）

`S94appmgr`（应用中心私有打包内的部署脚本，不在公开仓）是 SysVinit 启停脚本，排在 nginx（late init）之后（S94）：
- `start` 时先 `seed_s94_master`（把自己镜像到 OTA 存活的主拷贝 `/userdata/config/system/etc/init.d/S94appmgr`）→ `reinject_nginx`（`/oem` 上的 `ext_appmgr.conf` 缺失时从 `/userdata/local/appcenter/ext_appmgr.conf` 主拷贝回注，**`nginx -t` 通过才 reload；不通过则删除回注文件、绝不留坏配置**）→ 直接后台 `python3 -m appmgr serve`（不用 busybox `start-stop-daemon -b`，它 mishandle 需要 cd 的 shell wrapper）。

安装：
```sh
# 1. 放 live 脚本 + 主拷贝
adb push S94appmgr /etc/init.d/S94appmgr
adb shell "chmod +x /etc/init.d/S94appmgr"
adb shell "mkdir -p /userdata/config/system/etc/init.d && cp /etc/init.d/S94appmgr /userdata/config/system/etc/init.d/"
# 2. 放 nginx 主拷贝
adb shell "mkdir -p /userdata/local/appcenter && cp ext_appmgr.conf /userdata/local/appcenter/"
# 3. 启动（会自动 seed master + 回注 nginx + 起 appmgr）
adb shell "/etc/init.d/S94appmgr start"
adb shell "/etc/init.d/S94appmgr status"
```

**OTA 存活（诚实标注半成品）**：A/B OTA 会重刷 rootfs，抹掉 `/etc/init.d/S94appmgr`；官方 RkLunch 的 restore 链只回注 `/etc/passwd` `/etc/group` `/etc/shadow`，**不覆盖 `/etc/init.d`**。appmgr 代码、S94 主拷贝、nginx 主拷贝都在 `/userdata`（存活），但**没有 OTA 存活的 boot 钩子会自动 source `/userdata`**，因此**无法在 stock 固件内做到 100% 自动恢复**。当前做法：OTA 后手动跑一次 `appmgr-restore.sh`（幂等）：
```sh
adb shell "sh /userdata/local/appcenter/appmgr-restore.sh"
```
它把主拷贝 `S94appmgr` 复制回 `/etc/init.d/` 并 `start`（后者再自动回注 nginx conf）。从仍有 S94appmgr 的 slot 启动则无需任何操作。

### 4.3 分步验证：先传 appmgr 核心（<1 MB），再传 app 包（81 MB）

验界面 / 验后端**不需要**先传应用包。**只传 appmgr 核心（<1 MB）就能起后端 + 验界面与 API**；真正安装应用时才需要那个 **81 MB 的 app 包**。分两步走，界面/接口出问题能立刻定位到底是"框架没起来"还是"应用装不上"，不用每次拖 81 MB。

---

## 5. 验证清单

reboot / 部署后依次核对：

- [ ] **rkipc md5**：`md5sum /oem/usr/bin/rkipc` = `2baebbb55155efb0def2c82fb233e345`（或热替换目标值）
- [ ] **三 socket**：`ls -l /run/recamera/` 有 `frame.sock` `result-in.sock` `probe.sock`（+ `apps.d/`）
- [ ] **RTSP 出流**：`rtsp://<ip>:554/...` 有画面
- [ ] **内建推理**：官方检测框正常上 OSD / RTSP（内建走同一条 `rc_result_dispatch`）
- [ ] **结果回注端到端**：外部脚本 / SDK 向 `result-in.sock` 注入高辨识度检测 → RTSP 看到框+标签 → WS 收到 `source_id≠"builtin"` 的结果；冒充 `"builtin"` 被拒；超速被丢+计数
- [ ] **SDK 握手**（任何人可连）：
  ```sh
  export LD_LIBRARY_PATH=/oem/usr/lib:$LD_LIBRARY_PATH
  export PYTHONPATH=/userdata/sdk/python:$PYTHONPATH
  python3 - <<'PY'
  import recamera_ext as re
  s = re.ResultSink("selftest")   # 打开 result-in.sock 握手
  print("rc =", s.send_detections(123456, [(10,20,110,220,0.9,"person",0)]))  # 0 = accepted
  PY
  ```
- [ ] **应用中心**（若部署）：`/appcenter/` catalog 页面可开、`/api/appMgr/list` 经 JWT 返回正常、`appmgr` 进程在（`/etc/init.d/S94appmgr status`）
- [ ] **dmesg 无 VPSS 崩溃**：`dmesg | grep -iE 'vpss|fifo|Oops|paging request'` 空

---

## 6. 回滚（三级）

**一级 · 应用级 / 增量包回滚**（推荐，最快）：
```sh
adb shell "sh /userdata/ext-pkg/rollback.sh --reboot"
```
`rollback.sh`（见 `release/pkg/rollback.sh`）从 `/userdata/rkipc.factory.bak` 恢复原厂 rkipc（缺备份直接 `exit 1`），有 `entry.cgi.factory.bak` 一并恢复，`chmod 755` 后 reboot。扩展 `.so` 留着无害（没人加载它除非方案主动连），要彻底干净可手动删 `librecamera_ext.so*`。

**二级 · 整机文件级回滚**：设备上有分级备份可 `cp` 回：
- `/userdata/rkipc.factory.bak` — 原厂 rkipc（install.sh 首次保存）
- 版本备份如 `/userdata/rkipc.2baebbb.bak` 等 — 各版本 rkipc
- `entry.cgi.factory.bak` — 原厂 entry.cgi
- 应用中心的官方 www 备份（`www-official-backup`）— 覆盖 `/oem/usr/www` 前先备份的原厂前端

手动回滚：`cp <备份> <目标> && chmod 755 <目标>` → `adb reboot`。

**三级 · 官方 OTA 重刷**：刷任意官方 `update.img` / OTA 即把 `/oem` 全量还原成原厂（附带把扩展 API 也冲掉，见第 2.1 节边界）。这是"回到出厂"的兜底，不用于日常回滚。

---

## 7. 运维故障速查

| 现象 | 规避 / 处理 |
|---|---|
| **cp 二进制进 `/oem` 后 rkipc 起不来**（无进程、无 `/run/recamera/`，RkLunch 日志 `Permission denied`）| busybox `cp` 不保留执行位。cp 进 `/oem/usr/bin/` 后**必 `chmod 755`**（entry.cgi 同理）。热替换脚本 cp 后紧跟 chmod。|
| **整机 hang：ping 通但 SSH/adb/HTTP 全无响应** | 多半是 `while true; do rkipc; ...` 兜守护进程触发 **fork 风暴**耗尽 pid/内存（内核网络栈还应答 ICMP，用户态服务全 fork 不出会话）。**禁止 while-true 重拉 rkipc**；保活用设备自己的 SysVinit（`adb reboot` 让 init 干净拉起）。已 hang **只能物理断电重启**（fork 风暴不写盘，重启后干净）。别密集重试 SSH（加剧 + 触发 sshd MaxStartups）。|
| **热替换后双实例抢相机 → VPSS 崩溃** | `killall rkipc` 精确匹配进程名，漏杀 `rkipc.m2` 这种带后缀的自编版。一律 `for p in $(pgrep -f rkipc); do kill $p; done`（`pgrep -f` 匹配完整命令行）。**kill 后 `sleep 3` 再起新实例**，等 VPSS/VI 资源释放。|
| **dmesg 出现 `CSIBDG fifo overflow` / `vpss ... fifo overflow` / `Unable to handle kernel paging request` / `Oops`** | cv181x/rkvpss 内核驱动处理 FIFO 溢出时崩溃（驱动 bug，救不回）。一见立即 kill rkipc + `adb reboot` + 停手排查。**预防**：同一时刻只一个进程用相机、别碰 rkipc 正用的 chn、kill 干净再起。|
| **后台起 rkipc / adb shell 一退就没了** | adbd 退出会 reap 它的子进程树，`setsid` / `start-stop-daemon -b` 挡不住。改用：脚本 push 后 `adb shell 'sh run.sh'`（脚本内 `exec rkipc`，reparent 到 init）、或验完 `adb reboot` 让 init 拉起、或起进程后别退 adb shell（前台盯）。|
| **画了检测框但 RTSP / 编码流里看不到**（datetime 却可见）| VENC 只合成部分 RGN layer：每通道最高层不被合成。INFER overlay 用被合成的低 layer（主 **1** 子 **5**，不是 3/7）。排查"画了不显示"先怀疑 layer 值，不是坐标/颜色/SetBitMap。附带：OSD 调色板注意 ARGB/BGRA 字节序（品红写反显示成青色）。|
| **改过 osd 的自编 rkipc 起不来**（`osd_manager_init` 因 `rkipc.ini` 缺 `[osd] cfg=` 返 `-ENOENT`）| 已修：`osd_manager_load_cfg` 缺 cfg 降级为内置默认（inferenceOverlay 默认开）而非 init 失败。用含此修复的 rkipc（本 release 已含）。|
| **扩展应用连麦克风 / 相机权限拒绝** | `/dev/snd` `/dev/video` `/dev/mpi` 全 root 属主，SSH 的 admin（uid 1000）不在 audio 组、开不了硬件。**扩展应用必须以 root 运行**（appmgr / 约定的 SysVinit 脚本以 root 启动）。调试用 adb（root）而非 ssh admin。|
| **nginx 挂扩展前端 404 / 冲突** | 出厂 nginx 只显式列 `svc_*`，`ext_` 是新约定；确认 `common_relay.conf` 有 `include ext_*.conf` 且 `ext_appmgr.conf` 已回注到 `/oem/usr/etc/nginx/`。校验必须**用完整合成配置**：`nginx -t`（或 `nginx -T` 打印）通过再 `nginx -s reload`；reload 失败就删掉该 conf 再 reload 恢复。（S94appmgr 的 `reinject_nginx` 已内建这套 `nginx -t` 守卫。）|
| **busybox 工具缺失 / 行为不同** | 设备 busybox：**无 `stat`、无 `timeout`**；`cp` 不保留执行位（cp 后 chmod）；`tar` **不认 `-z`**，解压 `.tar.gz` 用 `gunzip -c x.tar.gz \| tar -x`。校验 nginx 用 `nginx -t`（必要时 `-c` 指定合成配置路径）。复杂命令别在 `adb shell "sh -c \"...$(...)...\""` 里嵌套引号（busybox 报 `syntax error unexpected "("`）→ echo 成脚本文件 push 后 `sh`。|
| **Tailscale 慢链路传大文件超时 / 卡死** | 设备在 Tailscale（100.x）时延迟 35-82ms，adb/scp 给足 timeout、别在一条命令里死等。大文件用小包 / 逐 tar 单文件传，或走 Mac 中转（`fleet pull <src> ... <mac>` → `adb push`）。局域网同网段（192.168.x）可直连（<1ms），优先直连。|

---

## 8. 网络拓扑

- **局域网同网段直连**（设备 192.168.x，如 192.168.42.1 / 192.168.42.1）：Mac 或 wsl2-local 可直连，<1-4ms 延迟，adb / scp / ssh 直接走。首选。
- **Tailscale**（设备 100.x）：wsl2-local 可能连不上（Tailscale 不互通），走 **Mac 中转**（`fleet pull` 到 Mac → `adb push`）。延迟高，所有 adb/scp 给足 timeout。
- 设备访问凭据：SSH `admin` / `***REDACTED***`（uid 1000，**无 sudo**）；root 走 `adb connect <ip>:5555`（adbd 以 root 跑）。`/userdata` ext4 rw 无 noexec，可放交叉编译二进制直接跑。

---

## 9. 跨固件 build 兼容

基于 `recamera_v2` 源码交叉编译的 rkipc / entry.cgi 在不同固件 build 上**跨 build ABI 兼容**，一次编译可跨 build 部署：

- 实证 build：V1.0.4（`-g2288d28`）与 V1.0.10（`-g6a9166b`），rkipc md5 不同、build 串不同。
- 兼容根据：两 build kernel 同为 **6.1.157**，MPI / rockit 库在位；动态库逐行解析一致（libcgicc / libssl / libcurl / librockit / glibc 2.38 全在）。
- 含义：扩展 API 改动**不必逐固件版本重编**。M1 3b 在 V1.0.10 上直接跑自编 rkipc（握手 / 身份 / 限速 / 端到端全通、主体不回归），门禁 G1-G4 也在原厂 V1.0.10 上完成（无需先刷自编固件）。
- 热替换（cp 覆盖或 bind mount 单文件）即时可回滚，不动 bootloader / 分区，无变砖风险。
