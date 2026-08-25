# Bifrost

Bifrost 是一个只使用公网服务器做 WebRTC 信令的内网 HTTP 访问器：业务 HTTP 数据走浏览器与本地 client 之间的 WebRTC DataChannel，公网 server 不做业务中继。

## 组件

- `bifrost.server`：公网 HTTPS/WSS 信令服务器、room 入口和登录页面。
- `bifrost.client`：内网端 agent，将 DataChannel 请求转换成普通 HTTP 请求。
- `bifrost.protocol`：两端共用的消息格式与配置加载。
- `examples/local-http`：纯 aiohttp HTTP 示例，不包含任何 Bifrost 代码。

## 运行

```bash
python3 -m pip install -r requirements.txt
python3 -m bifrost.server --config config/server.toml
python3 -m bifrost.client --config config/client.toml
```

server 默认按 `[tls]` 配置使用 HTTPS/WSS。如果 `tls.cert` 和 `tls.key` 同时配置为空字符串，server 会改用 HTTP/WS；两者只能同时为空或同时非空。纯 HTTP 不会加密密码、Cookie 或业务流量，只适合可信内网、反向代理已在前面终止 TLS 的部署，或临时调试环境。

内网 client 会使用 `config/client.toml` 中的 `[[services]]` 注册一个或多个 room。client 启动后，可以在 `https://<server>:8443/` 输入 room 名称，也可以直接打开 `https://<server>:8443/<room>`；未注册的 room 会被 server 拒绝。`/<room>/some/path?x=1` 这样的深链接会在登录成功后回到原地址。

首次 demo 使用自签名证书，浏览器需手动信任证书。页面内的导航栏会拦截内网页面的站内超链接，通过同一条 DataChannel 请求新 URI；浏览器真实地址保持在 Bifrost 页面，连接 ID/建立时间用于确认是否重连。

浏览器 iframe 中的超链接、表单提交、`fetch`、`XMLHttpRequest` 和 `sendBeacon` 会被桥接到同一条 DataChannel。请求方法、请求头和请求体会传给内网 client，因此 `GET`、`POST`、`PUT`、`PATCH`、`DELETE`、`HEAD` 及其他 aiohttp 接受的方法都可以转发；二进制请求体和响应体使用 Base64 保留原始字节。client 会为同一 WebRTC peer 复用 HTTP session，因此连接池和 Cookie 可以跨请求保留。HTML 解析器自动加载的图片、样式表、脚本等子资源，以及 WebSocket、同步 XHR 和流式请求，目前不经过该桥接。

## Room 浏览器访问密码

密码按 `[[services]]` 配置，因为 room 及其本地端口都由内网 client 管理。已有自身用户认证的服务可以明确配置为空，Bifrost 不再增加一层登录：

```toml
[[services]]
room = "public-app"
local_port = 10080
password_hash = ""
```

对于原本没有认证的内网页面，推荐在部署机器上交互生成 scrypt 哈希（不会把密码留在 shell history）：

```bash
bifrost-hash-password
# 或：python -m bifrost hash-password
```

将输出完整复制到对应 service：

```toml
[[services]]
room = "home"
local_port = 10081
password_hash = "$scrypt$v=1$n=32768,r=8,p=1$<salt>$<derived-key>"
```

`password_hash = ""` 表示免密 room；字段省略时也按空哈希处理。明文 `password` 配置已不再接受，使用 `bifrost-hash-password` 生成哈希后再写入 TOML。

server 端可以调整会话时长和密码尝试限制：

```toml
[browser_auth]
session_ttl = 43200       # 12 小时，范围 60 秒到 30 天
max_attempts = 5          # 每个来源地址、每个 room 的失败次数
attempt_window = 60       # 限制窗口，秒
password_workers = 2      # 并发 scrypt 校验数，限制内存/CPU 消耗
```

认证设计如下：

- 人类口令使用带 128-bit 随机盐的 scrypt（`N=32768, r=8, p=1`），不是可逆的对称加密。server 只在 agent 在线时将哈希保存在内存，不把明文或哈希写入 server TOML。
- 已通过 Ed25519 认证的 agent 会把 room、随机 challenge 和密码哈希作为一个整体签名，公网 server 不能接受未认证连接伪造的“免密”策略。
- 浏览器通过 HTTPS 提交密码。验证成功后得到只绑定当前 room、当前密码哈希版本和过期时间的 HMAC 签名 Cookie；Cookie 使用 `Secure`、`HttpOnly`、`SameSite=Lax` 和 `__Host-` 约束。不同 room 使用不同 Cookie。
- 页面路由和 `/signal` WebSocket 握手都会校验会话，不能跳过登录页直接连接信令。会话到期时 server 会关闭信令，并通知 agent 拆掉对应 WebRTC peer connection。
- 内网页面运行在无 `allow-same-origin` 的 sandbox iframe 中，浏览器信令还会检查 WebSocket `Origin`，避免一个 room 的页面借用浏览器中另一个 room 的登录 Cookie。
- 密码校验在线程中执行并限制并发，失败登录在昂贵的 scrypt 运算之前按来源地址限速。登录回跳只允许当前 room 下的相对路径，避免开放重定向。

非对称密钥仍用于 agent 身份认证，适合机器长期持有私钥；没有把它用于浏览器登录，是因为那会要求每个临时浏览器预先安全分发私钥，明显降低使用便利性。也没有让浏览器直接提交“哈希后的密码”，因为那会把可读取的哈希变成可重放的 bearer credential。普通表单认证意味着公网 server 在验证瞬间能看到密码，因此生产环境必须使用可信的 HTTPS 证书；scrypt 主要保护配置、内存快照或哈希泄露后的离线破解风险，不能弥补弱密码。

## Agent 身份认证与权限

Agent 注册到信令服务器前会完成一次 Ed25519 challenge-response 验证。私钥只保存在 agent，server 直接在 TOML 配置中保存多个 OpenSSH `.pub` / `authorized_keys` 风格的公钥文本；随机 challenge、协议域、room 和浏览器访问策略会一起签名，因此签名不能跨连接、跨 room 重放或被改成免密策略。

本版本的 server 仍接受旧 agent 的 v1 签名，但只能把这种连接注册为免密 room。滚动升级时应先升级 server，再升级 client；启用 `password_hash` 必须使用新版 client。

生成密钥并部署公钥：

```bash
ssh-keygen -t ed25519 -f /opt/bifrost/keys/agent_ed25519
install -m 600 /opt/bifrost/keys/agent_ed25519 /path/on/agent/agent_ed25519
cat /opt/bifrost/keys/agent_ed25519.pub
```

server 配置：

```toml
[auth]
public_keys = [
  "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAA... home-agent",
  "room=\"office\" ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAA... office-agent",
]
timeout = 10
```

client 配置：

```toml
[auth]
private_key = "/path/on/agent/agent_ed25519"
public_key = "/path/on/agent/agent_ed25519.pub"
timeout = 10
```


访问某个 room 时，公网 URL 直接使用 room 名称，例如：

```text
https://v.phenix.my/home
https://v.phenix.my/office
```

页面打开后，room 只用于建立信令连接，内嵌导航栏只显示内网 URI。比如导航栏显示 `/healthz`，发送给对应 room 的 client 后，client 会请求自己配置的本地目标：

```text
http://127.0.0.1:10080/healthz
```

`/home/healthz` 也支持作为首次打开页面的深链接，但建立页面后后续导航不会改变公网地址；实际请求路径不会包含 `/home`。本地目标地址由 client 的 `[[services]]` room/port 映射决定。

普通的 `ssh-ed25519 ...` 行允许该 key 注册任意 room。需要按 room 授权时，在公钥行前增加自定义的 `room` 选项；同一个 key 可重复多行来授权多个 room：

```text
room="home" ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAA... home-agent
room="office" ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAA... office-agent
```

server 会在启动时读取并校验 `public_keys` 数组，只接受 `ssh-ed25519` 公钥；每个元素可以直接粘贴完整 `.pub` 行（包括末尾 comment），也可以使用 `room=...` 选项。空数组、非 Ed25519 key 或格式错误都会让服务拒绝启动。

## 一个 client 注册多个 room

信令地址、TLS 和 Ed25519 密钥由所有服务共用，每个 `[[services]]` 条目配置一个 room 和本地端口：

```toml
[signal]
url = "wss://v.phenix.my:8443/signal"
verify_tls = true

[local_http]
host = "127.0.0.1"
scheme = "http"

[[services]]
room = "home"
local_port = 10080
password_hash = "$scrypt$v=1$n=32768,r=8,p=1$...$..."

[[services]]
room = "office"
local_port = 10081
password_hash = ""

[auth]
private_key = "/opt/bifrost/keys/agent_ed25519"
public_key = "/opt/bifrost/keys/agent_ed25519.pub"
timeout = 10
```

同一个 client 进程会并行注册 `home` 和 `office`，分别转发到 `127.0.0.1:10080` 和 `127.0.0.1:10081`。room 不允许重复，名称必须是 1–64 个 ASCII 字母、数字、点、下划线或连字符并以字母/数字开头；端口范围必须是 1 到 65535。server 的 `public_keys` 必须允许该公钥进入对应 room。

## 安装与打包

项目采用 `src` 布局，元数据和依赖定义在 `pyproject.toml` 中。安装运行依赖并构建 wheel：

```bash
python -m pip install .
python -m pip install build
python -m build
```

构建产物位于 `dist/`，也可以直接使用安装后的命令：

```bash
bifrost-server --config /path/to/server.toml
bifrost-client --config /path/to/client.toml
bifrost-hash-password
```

配置文件和证书是部署文件，不会被打进 wheel；`src/bifrost/static/` 下的访问器、room 入口和登录页面会作为包数据随 wheel 安装。
