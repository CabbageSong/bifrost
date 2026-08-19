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
