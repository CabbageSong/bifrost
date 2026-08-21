# Bifrost

Bifrost 是一个只使用公网服务器做 WebRTC 信令的内网 HTTP 访问器：业务 HTTP 数据走浏览器与本地 client 之间的 WebRTC DataChannel，公网 server 不做业务中继。

## 组件

- `bifrost.server`：公网 HTTPS/WSS 信令服务器和调试页面。
- `bifrost.client`：内网端 agent，将 DataChannel 请求转换成普通 HTTP 请求。
- `bifrost.protocol`：两端共用的消息格式与配置加载。
- `examples/local-http`：纯 aiohttp HTTP 示例，不包含任何 Bifrost 代码。

## 运行

```bash
python3 -m pip install -r requirements.txt
python3 -m bifrost.server --config config/server.toml
python3 -m bifrost.client --config config/client.toml
```

首次 demo 使用自签名证书，浏览器需手动信任证书。页面内的导航栏会拦截内网页面的站内超链接，通过同一条 DataChannel 请求新 URI；浏览器真实地址保持在 Bifrost 页面，连接 ID/建立时间用于确认是否重连。

## Agent 身份认证与权限

Agent 注册到信令服务器前会完成一次 Ed25519 challenge-response 验证。私钥只保存在 agent，server 直接在 TOML 配置中保存多个 OpenSSH `.pub` / `authorized_keys` 风格的公钥文本；随机 challenge、协议域和 room 会一起签名，因此签名不能跨连接或跨 room 重放。

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

[[services]]
room = "office"
local_port = 10081

[auth]
private_key = "/opt/bifrost/keys/agent_ed25519"
public_key = "/opt/bifrost/keys/agent_ed25519.pub"
timeout = 10
```

同一个 client 进程会并行注册 `home` 和 `office`，分别转发到 `127.0.0.1:10080` 和 `127.0.0.1:10081`。room 不允许重复，端口范围必须是 1 到 65535。server 的 `public_keys` 必须允许该公钥进入对应 room。

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
```

配置文件和证书是部署文件，不会被打进 wheel；`src/bifrost/static/index.html` 已作为包数据随 wheel 安装。

