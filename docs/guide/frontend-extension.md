# 前端扩展挂载：`ext_<name>.conf` 与 `/extension/<name>/`

> 事实来源：`project/app/recamera_web/recamera_web_backend/ipcweb-env-rv1126b/etc/nginx/`
> （nginx.conf、common_relay.conf）与两个在用实例：
> `recamera_services/acousticslabd/deploy/etc/nginx/ext_acousticslabd.conf`、
> `recamera_services/alpkg/deploy/etc/nginx/ext_alpkg.conf`。

## 能干什么

设备 Web 服务（nginx:80）的主配置链是：`nginx.conf:61` → `include /oem/usr/etc/nginx/common_relay.conf` → `common_relay.conf:14` → **`include ext_*.conf;`**。

任何放进 nginx 配置目录、名字匹配 `ext_*.conf` 的文件都会被加载。第三方 app 借此把自己的页面和后端挂到官方 Web 入口下，约定路径前缀为 `/extension/<name>/`，并**免费复用官方的 JWT 登录会话**——用户在 dashboard 登录一次，你的页面同样受保护、同样可访问。

## 前置条件

- 能把文件安装到系统分区（随扩展包部署；`/oem` 分区在多数固件上为只读挂载，能否直接写入取决于你的分发渠道——待验证，官方打包机制在后续里程碑提供）。
- 需要放置的文件：

| 文件 | 位置 | 作用 |
|---|---|---|
| `ext_<name>.conf` | 与 `common_relay.conf` 同目录（设备上为 `/oem/usr/etc/nginx/`，对应源码 deploy 树 `deploy/etc/nginx/`） | nginx location 定义 |
| 静态前端 | `/oem/usr/www/extension/<name>/` | SPA 构建产物（root 为 `/oem/usr/www`，见下方模板） |
| 后端进程（可选） | 任意；监听 unix socket（在用实例约定 `/dev/shm/<name>-api.sock`） | API/WS 服务 |
| init 脚本（可选） | `/etc/init.d/SNNxxx` | 启动后端（SysVinit；实例：`S40acousticslabd`） |

- 改完 nginx 配置后 reload：`nginx -s reload`（未上机验证具体 init 封装）。

## 模板：ext_acousticslabd.conf 逐段解读

以下为官方 acousticslab 扩展的 conf 全文（源码原样），三种典型 location 都在里面。

```nginx
# ① 未登录时的跳转目标：302 到官方登录页，登录后回跳原地址
location @acousticslabd_login_redirect {
    absolute_redirect off;
    add_header Cache-Control "no-store" always;
    return 302 /login?redirect_uri=$safe_redirect_uri;   # $safe_redirect_uri 由 nginx.conf url_encode 生成
}

# ② 静态 SPA：JWT 保护 + history 路由回退
location /extension/acousticslab/ {
    auth_request /_jwt_verify;                            # 复用官方 JWT 会话（机制见下节）
    error_page 401 403 = @acousticslabd_login_redirect;   # 未登录 → 跳登录页
    add_header Cache-Control "no-cache" always;
    root /oem/usr/www;                                    # 实际文件在 /oem/usr/www/extension/acousticslab/
    try_files $uri $uri.html $uri/ /extension/acousticslab/index.html;  # SPA 回退

    location /extension/acousticslab/_app/immutable/ {    # 带 hash 的构建产物走强缓存
        add_header Cache-Control "public, max-age=31536000, immutable" always;
        try_files $uri =404;
    }
}

# ③ 反代后端 API：JWT 保护 + unix socket 上游
location /extension/acousticslab/api/ {
    auth_request /_jwt_verify;
    add_header Access-Control-Allow-Origin   '*' always;   # 该后端自身不做 CORS，由 nginx 补
    add_header Access-Control-Expose-Headers '*' always;
    if ($request_method = OPTIONS) {                       # 预检直接在 nginx 应答
        add_header Access-Control-Allow-Origin  '*' always;
        add_header Access-Control-Allow-Methods 'GET, POST, PUT, PATCH, DELETE, OPTIONS' always;
        add_header Access-Control-Allow-Headers $http_access_control_request_headers always;
        add_header Access-Control-Max-Age 86400 always;
        return 204;
    }
    proxy_pass http://unix:/dev/shm/acousticslabd-api.sock:/api/;  # 前缀重写：/extension/.../api/x → /api/x
    proxy_http_version 1.1;
    proxy_set_header Host              $host;
    proxy_set_header X-Real-IP         $remote_addr;
    proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    client_max_body_size    0;                             # 大文件上传不限
    proxy_request_buffering off;
}

# ④ WebSocket：Upgrade 头 + 长超时
location /extension/acousticslab/stream/ {
    auth_request /_jwt_verify;
    proxy_pass http://unix:/dev/shm/acousticslabd-api.sock:/stream/;
    proxy_http_version 1.1;
    proxy_set_header Upgrade    $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_set_header Host       $host;
    proxy_read_timeout 1h;
    proxy_send_timeout 1h;
    proxy_buffering    off;
}
```

对照最小实例 `ext_alpkg.conf`：只有一个静态页 + 登录跳转，没有后端——纯静态扩展只需 ②①两段：

```nginx
location @alpkg_login_redirect {
    absolute_redirect off;
    add_header Cache-Control "no-store" always;
    return 302 /login?redirect_uri=$safe_redirect_uri;
}
location = /extension/alpkg/import {
    auth_request /_jwt_verify;
    error_page 401 403 = @alpkg_login_redirect;
    alias /oem/usr/www/extension/alpkg/import.html;
    add_header Cache-Control "no-store" always;
    default_type text/html;
}
```

## 接入步骤（挂一个 `/extension/myapp/`）

1. 前端构建产物放 `/oem/usr/www/extension/myapp/`。
2. 后端（如有）监听 `/dev/shm/myapp-api.sock`，写一个 `/etc/init.d/SNNmyapp` 启动它。
3. 照上方模板写 `ext_myapp.conf`（把 `acousticslab` 全部替换为 `myapp`），放到 `/oem/usr/etc/nginx/`。
4. `nginx -s reload`，浏览器访问 `http://<设备IP>/extension/myapp/`——未登录会被 302 到官方登录页。

## 鉴权机制：`auth_request /_jwt_verify`

- 每个受保护 location 的请求，nginx 先向内部 location `/_jwt_verify`（`common_relay.conf:88-107`）发一次子请求，经 FastCGI 调 `entry.cgi` 的 `/auth_verify` 路径，转发原请求的 `Authorization` 头与 Cookie。
- entry.cgi 侧校验逻辑在 `recamera_web_backend/src/rest_api.cpp:67-81`（`auth_verify()`）：依次尝试 ①localhost 直通 ②内部 API key ③JWT 校验；`/auth_verify` 命中后按结果返回 200/401（`rest_api.cpp:255-260` 附近的分派）。
- 返回 200 → nginx 放行你的 location；401/403 → 走你配置的 `error_page` 跳登录。
- **你的后端因此不需要实现任何鉴权**——到达 unix socket 的请求都已通过官方会话校验。但注意：socket 文件本身是本机任意进程可连的，鉴权只覆盖"经 nginx 进来"的路径。

### 本机进程调 entry.cgi 免鉴权

`nginx.conf:38-41` 定义 `geo $is_local_request`（仅源地址 `127.0.0.1` 为 1），并在 `/cgi-bin/` location 注入 `HTTP_X_INTERNAL_FROM_LOCALHOST $is_local_request`（`common_relay.conf:130`）；entry.cgi 见到该变量为 `"1"` 直接跳过鉴权（`rest_api.cpp:69-73`）。

含义：**设备上的进程 `curl http://127.0.0.1/cgi-bin/entry.cgi/<域>/...` 无需 token 即可调用官方全部 HTTP API。** 边界：仅限本机回环源地址；从外部 IP 访问必须带 JWT。此直通行为在后续引入 app token 的里程碑中会被替代（老路径保留过渡期），不要把它当成永久契约写死在产品逻辑里。

## 边界与限制

- `/extension/<name>/` 是路径约定而非隔离机制：conf 里写别的路径 nginx 一样加载。请自律只占用自己的前缀，避免与官方 location 冲突（冲突时 nginx 起不来，影响整机 Web）。
- conf 写错会导致 `nginx -s reload` 失败甚至 nginx 无法启动——上线前先 `nginx -t` 验证。
- 官方前端（React dashboard）目前没有把扩展入口显示到主界面的机制；用户需直接访问 URL，或你在自己的分发渠道给入口。
- `/oem` 分区可写性、打包安装流程属于后续打包分发里程碑；当前文档只覆盖"文件就位后如何生效"。

## 故障排查

| 现象 | 排查 |
|---|---|
| 访问 404 | conf 未被加载：确认文件名匹配 `ext_*.conf` 且在 common_relay.conf 同目录；`nginx -T \| grep extension` 查看生效配置 |
| 访问直接 401 而非跳登录 | 缺 `error_page 401 403 = @<name>_login_redirect` 段 |
| 登录后仍 401 | JWT 在 Cookie/Authorization 中未随请求带上；对照官方 dashboard 同浏览器会话测试 |
| API 502 | 后端 socket 不存在或进程未启动：`ls -l /dev/shm/<name>-api.sock`；`proxy_pass` 的 URI 重写段（`:/api/`）是否写对 |
| WS 连上即断 | 缺 `Upgrade/Connection` 头或 `proxy_http_version 1.1` |
