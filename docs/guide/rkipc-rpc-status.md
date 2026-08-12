# rkipc RPC 现状：`/var/tmp/rkipc` 是内部接口，配置需求请走 entry.cgi

> 事实来源：`project/app/recamera_ipc/common/socket_server/{socket.h,server.c}`、
> `project/app/recamera_web/recamera_web_backend/src/`（socket_client、rest_api.cpp、各 *_api.cpp）。

## 定位声明（本文的结论，先说）

- `/var/tmp/rkipc` 是 rkipc 主程序与官方 Web 后端（entry.cgi）之间的**内部 RPC socket**。
- **不承诺稳定**：命令名、参数布局、语义都可能随固件版本变化，无版本协商、无兼容层。方案商不应直接对接它。
- **配置类需求今天的正路是 entry.cgi HTTP API**（见下文"今天能用的部分"）。
- 后续 M4 里程碑会提供版本化的正式控制面（`/api/v1/ext/*` 域，含能力发现），届时以其为准。

写这篇的目的不是教你调它，而是让你理解架构——知道 entry.cgi 背后是什么、边界在哪，避免绕过 HTTP API 直连内部 socket 后被固件升级打断。

## 架构：entry.cgi ↔ rkipc 的关系

```
浏览器/脚本 --HTTP--> nginx --FastCGI--> entry.cgi --unix socket RPC--> rkipc
                                        (/var/tmp/rkipc, AF_UNIX SOCK_STREAM)
```

- socket 路径由 `socket.h:13` 定义：`#define CS_PATH "/var/tmp/rkipc"`。
- rkipc 侧起一个 server 线程（`server.c` `rkipc_server_thread`，`serv_listen(CS_PATH)`），每个连接一个处理线程。
- entry.cgi 是它的客户端：`recamera_web_backend/src/socket_client/`（`socket.cpp:21` `cli_begin()` 连接并发送函数名，`client.cpp` 是几百个 `rk_*` 包装函数，如 `client.cpp:840` `rk_video_set()`、`:862` `rk_video_get_gop()`）。

### 协议形态（说明性质，非对接指南）

命令分发是**函数名字符串查表**：server 端维护 `struct FunMap { char *fun_name; int (*fun)(int); }`（`server.c:49-52`）静态表 `map[]`（`server.c:6239` 起，数百项），收到 `<le32 len><函数名\0>` 后逐项 `strcmp` 调用对应处理函数（`server.c:6608-6612`），处理完写回一个 int 返回码，连接可复用继续发下一条。

参数没有统一编码，每个函数自定义读写序列。三个代表性形态：

| 命令（fun_name） | 参数线格式 | 说明 |
|---|---|---|
| `rk_video_set` | `<le32 len><JSON 字符串>` | 整块 JSON 配置下发（`server.c` `ser_rk_video_set`） |
| `rk_isp_get_contrast` | 读 `<int cam_id>`，回 `<int scene_id><int value><int err>` | 裸 int 序列，无标记无长度 |
| `rk_isp_set_contrast` | 读 `<int id><int scene_id><int value>`，回 `<int err>` | 同上，set 方向 |

命令族覆盖 isp（曝光/白平衡/降噪/HDR…）、video（编码参数/GOP/码率…）、audio、osd、event、model 等——与 entry.cgi 的 HTTP 域大体对应。**正因为参数是裸内存序列且靠字符串匹配，任何固件改动都可能静默改变布局——这是"不承诺稳定"的技术根源。**

## 今天能用的：entry.cgi HTTP API

### 入口与鉴权

- 入口：`http://<设备IP>/cgi-bin/entry.cgi/<域>/<资源>`（nginx `/cgi-bin/` location → FastCGI，`common_relay.conf:113-131`）。
- 域路由表在 `rest_api.cpp:157-243`，共 18 域：`network / network-ntp / network-ddns / network-pppoe / network-port / video / audio / image / system / osd / event / peripherals / model / notify / web / ftp / record / config`（分派逻辑 `rest_api.cpp:270-274`，取路径首段前 20 字符匹配）。
- 鉴权（`rest_api.cpp:67-81`）三选一即通过：
  1. **本机直通**：从 `127.0.0.1` 发起的请求免鉴权（nginx `geo` 注入 `HTTP_X_INTERNAL_FROM_LOCALHOST=1`，`rest_api.cpp:69-73`）。设备上的扩展进程调 API 零门槛。
  2. 内部 API key（`VerifyInternalApiKey`，官方组件间使用）。
  3. **JWT**：外部访问用。登录获取：`POST /cgi-bin/entry.cgi/system/login`，body 为 `{"sUserName":"admin","sPassword":"<RSA 加密后的密码>"}`（`system_api.cpp:3477` 登录分支；`:2776` `is_register_user()` 做 `rsa_decrypt` + shadow 校验）。密码需先用设备下发的 RSA 公钥加密。**公钥获取端点已验证（2026-08-10，固件 V1.0.10 / kernel 6.1.157）**：`GET /cgi-bin/entry.cgi/system/key`（`system_api.cpp:2913` `EnsureRSAKeysExist()` 分支），返回 `{"sPublicKey":"-----BEGIN RSA PUBLIC KEY-----\n...\n-----END RSA PUBLIC KEY-----\n"}`——注意是 PKCS#1 格式（`BEGIN RSA PUBLIC KEY`，非 SPKI），Python 用 pycryptodome `RSA.import_key` + `PKCS1_v1_5` 加密（与前端 jsencrypt 一致）。**登录已跑通**：`POST /cgi-bin/entry.cgi/system/login`，body `{"sUserName":"admin","sPassword":"<base64(PKCS1v15 密文)>"}`，成功返回 `{"iAuth":1,"iStatus":0,"sWaittime":0}` 且 JWT 经 `Set-Cookie: token=<187 字节>` 下发（**token 在 Cookie 里，不在响应 body**）。连续失败会按 IP 锁定递增等待（`system_api.cpp:3491-3520`）。

### 三个域各举一例（本机直通形态，脚本可直接跑）

```sh
# model 域：列出已上传的模型（model_api.cpp:339，GET 分支）
curl http://127.0.0.1/cgi-bin/entry.cgi/model/list

# model 域：查询/控制推理（model_api.cpp:406 GET inference；:1137 inference-restart）
curl http://127.0.0.1/cgi-bin/entry.cgi/model/inference

# video 域：读取视频/编码配置（video_api.cpp:948-978，GET 返回配置 JSON）
curl http://127.0.0.1/cgi-bin/entry.cgi/video

# osd 域：读取 OSD 配置；POST/PUT 同路径写入（osd_api.cpp:271-293）
curl http://127.0.0.1/cgi-bin/entry.cgi/osd
```

**已验证（2026-08-10，固件 V1.0.10 / kernel 6.1.157，外部经 nginx + JWT Cookie 调用）**返回结构示例：

- `GET /model/list` → 数组，每项 `{"model":"yolox_s.rknn","modelInfo":{"algorithm":"yolox","category":"Detection","classes":["person",...80 类 COCO...]}}`。
- `GET /system/device-info` → `{"sBasePlateModel":"Base Board-V1.0,Expand Board-V1.0","sFirmwareVersion":"V1.0.10","sSensorModel":"SC850SL","sSerialNumber":"<设备序列号>"}`。
- `GET /video`（无子资源）→ `{"code":501,"message":"Not Implemented"}`；video 域读取需带具体子资源路径，裸 `/video` 的 GET 未实现。

（外部访问这些 API 与 WS 一样，JWT 必须放 Cookie；实测 `Authorization: Bearer` 头/`?token=` 之外，dashboard 同款 Cookie 携带最稳。）

外部访问同样的 URL，加 `-H "Authorization: Bearer <JWT>"`（或登录后 Cookie）。

model 域的完整资源名（`model_api.cpp` 路由）：GET `list / info / algorithm / inference / download_status`，PUT `upload / info / get_model / inference / inference-restart`，DELETE 相关 `delete`——按方法与资源组合使用；各资源的请求体 schema 以官方 Web API 文档（`recamera_web/backend/reCamera WEB API.pdf`）为准。

## M4 预告：版本化控制面（规划中）

M4 里程碑将在 entry.cgi 路由表新增 `ext` 域，为方案商提供带承诺的控制面（以下内容以正式发布为准，当前不可用）：

- `GET /api/v1/ext/capabilities`：返回设备当前扩展能力集（与扩展 socket 握手 `HelloAck` 一致的 Capability 列表——能力名、版本、limits），供 app 启动时做能力发现与降级。
- `GET /api/v1/ext/subscriptions`：当前帧/结果/观测连接的诊断视图（连接身份、限速计数）。
- 现有 18 域中挑出方案商必需子集（model / video / osd / system）做字段级文档化与冻结承诺，其余标注 internal。
- app 作用域 token 作为新增认证模式并行提供，替代 localhost 直通（老路径保留过渡期）。

对照两条路的差异：

| | 直连 `/var/tmp/rkipc` | entry.cgi（今天）→ `/api/v1/ext/*`（M4） |
|---|---|---|
| 稳定性 | 无任何承诺，裸内存布局 | HTTP+JSON；M4 起版本化冻结 |
| 鉴权 | 无 | localhost 直通 / JWT / （M4）app token |
| 升级兼容 | 固件升级即可能断 | 版本号显式演进 |
| 报障受理 | 不受理 | 受理 |

## 边界与限制

- `/var/tmp/rkipc` socket 文件权限为默认权限，本机进程技术上连得上——**能连 ≠ 该连**。直连内部 RPC 的集成没有任何跨版本保障，问题报障也不受理。
- entry.cgi 的 18 域中，哪些字段承诺跨版本冻结尚未官宣；M4 会挑出方案商必需子集（model/video/osd/system 等）做文档化冻结，其余标注 internal。当前阶段建议：只依赖官方 Web 界面同款调用（抓 dashboard 的请求照抄最稳）。
- localhost 直通会在 app token 机制落地后被替代（过渡期保留）；新集成不要把"免鉴权"当长期前提。
- HTTP API 是配置面，不是数据面：拿帧、注结果、收 PCM 分别见帧代理（M2 规划）、《结果推送接入》、《音频接入》。

## 故障排查

| 现象 | 排查 |
|---|---|
| entry.cgi 返回 401 | 非本机来源且无 JWT/JWT 过期；先在设备上 `curl 127.0.0.1` 验证 API 本身，再排查鉴权 |
| 返回错误信息 `Token expired` / `purpose mismatch` | `rest_api.cpp:263-268` 的细分错误：重新登录换新 token |
| API 超时无响应 | rkipc 未运行或 RPC 挂起：`ls -l /var/tmp/rkipc`、`ps \| grep rkipc`；socket_client 侧有 30 秒接收超时（`socket.cpp:76-79`） |
| 同名 API 行为随固件升级变化 | 属预期内风险；对照新版固件的 Web API 文档调整。M4 版本化控制面发布后迁移到 `/api/v1/ext/*` |
