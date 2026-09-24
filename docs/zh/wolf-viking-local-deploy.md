# OpenViking (wolf-viking fork) 本地部署

本仓库分支 `deploy/wolf-viking`（`Leoyzen/OpenViking`）的 Docker 本地部署说明。
镜像从本地源码构建（**不是**官方 `ghcr.io/volcengine/openviking` 镜像），包含该 fork 的
`processing_mode=none`、Qdrant、S3/OSS 等特性。

## TL;DR

```bash
# 1. 构建镜像（约 10-30 分钟，需要能访问 Docker Hub / npm / PyPI / crates.io）
docker build \
  --build-arg OPENVIKING_VERSION=0.4.15.dev35 \
  --build-arg http_proxy=http://host.docker.internal:7890 \
  --build-arg https_proxy=http://host.docker.internal:7890 \
  --build-arg no_proxy=localhost,127.0.0.1 \
  -t openviking:wolf .

# 2. 启动（embedding 配置见下文，必须提供，否则启动即崩）
docker run -d --name openviking-local -p 1933:1933 \
  -e OPENVIKING_WITH_BOT=0 \
  -e OPENVIKING_CONF_CONTENT='{
    "server": {"host": "0.0.0.0", "port": 1933, "root_api_key": "test-key-123"},
    "storage": {"workspace": "./data", "agfs": {"backend": "local"}, "vectordb": {"backend": "local"}},
    "embedding": {"dense": {"provider": "volcengine", "model": "doubao-embedding-vision-251215",
      "api_base": "https://ark.cn-beijing.volces.com/api/v3", "api_key": "YOUR_KEY", "dimension": 1024}},
    "log": {"level": "INFO", "output": "stdout"}
  }' \
  openviking:wolf

# 3. 验证
curl -s http://localhost:1933/health   # {"status":"ok","healthy":true,...}
curl -s http://localhost:1933/ready    # 五项全 ok 才算完全就绪
```

Studio 入口：http://localhost:1933/studio/ ，用 `root_api_key` 登录。

## 两个必须注意的坑

### 1. embedding 配置是硬性要求

`embedding` 段缺失时，配置默认回落到**本地 embedding**（`bge-small-zh-v1.5-f16`，需要
`llama-cpp-python` + GGUF 模型）。而镜像里**没有**装 `llama-cpp-python`（Dockerfile 的
`uv sync` 只带 `--extra bot --extra gemini`），结果是容器启动直接崩：

```
EmbeddingConfigurationError: Local embedding is enabled but 'llama-cpp-python' is not installed
Application startup failed. Exiting.
```

所以启动时必须提供 `embedding.dense`，指向一个可用的远端 provider（构造是惰性的，不会
在启动时联网校验；但 `/ready` 里的 embedding 探针会真实调用一次，失败则 `/ready` 返回 503）。

可选 provider：`volcengine` / `openai` / `ollama` / `jina` / `gemini` / `voyage` /
`dashscope` / `minimax` / `cohere` / `litellm` / `local`。
完整示例见 `examples/ov.conf.example`。

### 2. 容器内绑 0.0.0.0，必须设 `root_api_key`

否则服务拒绝启动（安全设计）。本地随便写一个即可，例如 `test-key-123`。

## 配置怎么给

三种方式，按需选一：

1. **`OPENVIKING_CONF_CONTENT` 环境变量**（上面的 TL;DR 用的方式）：完整 ov.conf JSON
   字符串，容器首次启动时写入 `/app/.openviking/ov.conf`。注意一旦文件存在，后续启动
   **不会**再用环境变量覆盖它。
2. **挂载宿主配置目录**：`-v ~/.openviking:/app/.openviking`。宿主的 `ov.conf` 里必须有
   `embedding` 段，否则同上崩溃。数据（workspace、`ovcli.conf` 等）也会持久化在这里。
3. **容器内跑交互向导**：
   ```bash
   docker exec -it openviking-local openviking-server init
   ```
   配置已存在时会给出菜单，选 `3) Update embedding` 只改 embedding。
   改完 `docker restart openviking-local` 生效。

## 用 compose 启动

仓库自带的根目录 `docker-compose.yml` 指向**官方镜像** `ghcr.io/volcengine/openviking:latest`，
不是本 fork（而且默认还会起一个 caddy 反代 1934）。本 fork 的 compose 在
`deploy/wolf-local/`，直接从源码构建：

```bash
cd deploy/wolf-local
mkdir -p ov-data && cp ov.conf.example ov-data/ov.conf   # 编辑 ov.conf：填 embedding + root_api_key
docker compose up -d --build

# 走代理构建：
export BUILD_HTTP_PROXY=http://host.docker.internal:7890
docker compose up -d --build
```

配置放在 `ov-data/ov.conf`（挂载为 `/app/.openviking`），数据同目录持久化。

## 验证 none 模式（fork 特性）

root key 不能访问数据 API（api_key 模式下设计如此），需要先建用户 key：

```bash
# 建用户（root key 认证）
curl -s -X POST http://localhost:1933/api/v1/admin/accounts/default/users \
  -H "Authorization: Bearer test-key-123" -H "Content-Type: application/json" \
  -d '{"user_id": "admin", "role": "admin"}'
# 返回里的 key 即 user key，用它调数据 API

# none 模式写入：不触发 embedding / 语义处理，只存占位
curl -s -X POST http://localhost:1933/api/v1/content/write \
  -H "Authorization: Bearer <USER_KEY>" -H "Content-Type: application/json" \
  -d '{"uri": "viking://resources/test-none-mode.md", "content": "hello", "processing_mode": "none"}'
# → {"mode":"create","written_bytes":5,"content_updated":true,"semantic_status":"skipped","vector_status":"skipped"}
```

其他常用端点：`GET /api/v1/content/read?uri=...`、`GET /api/v1/fs/ls?uri=viking://resources`。

## 构建细节 / 排错

- **代理**：Dockerfile 里没有任何 proxy ARG，但 Docker 原生支持 `http_proxy` /
  `https_proxy` / `no_proxy` build-arg。直连 GitHub/Docker Hub 不稳时务必加上（见 TL;DR）。
- **基础镜像预拉**：构建卡在 `failed to fetch oauth token` / `dial tcp ... i/o timeout`
  时，先逐个 `docker pull` 基础镜像：`rust:1.91.1-trixie`、`node:24-trixie-slim`、
  `python:3.13-slim-trixie`、`ghcr.io/astral-sh/uv:python3.13-trixie-slim`、
  `docker/dockerfile:1.9`。
- **版本号**：`OPENVIKING_VERSION` 从 `openviking/_version.py` 读取（当前 `0.4.15.dev35`）；
  显式传 build-arg 更稳。
- **构建阶段**：rust 工具链 → web-studio（npm ci + build）→ py-builder（uv sync + 编译
  Rust ov CLI + C++ 扩展 + ragfs-python，日志可能静默数分钟，属正常）→ 精简 runtime。
- **runtime 内没有编译器**，也没有 pip 可用网络装包的能力，别指望运行中容器里装
  `llama-cpp-python`；要用本地 embedding 得改 Dockerfile 加 `local-embed` extra 后重建。

## 相关文件

| 文件 | 说明 |
| --- | --- |
| `Dockerfile` | 4 阶段构建：rust-toolchain → web-studio → py-builder → runtime |
| `docker/openviking-entrypoint.sh` | 入口：配置缺失时起 503 pending 服务等待配置；健康检查后启动 server |
| `docker-compose.yml` | 官方 compose（caddy 反代 1934 + 直连 1933），image 需换成自建 |
| `examples/ov.conf.example` | 完整配置示例（404 行，含各 embedding provider） |
| `deploy/wolf-local/docker-compose.yml` | 本 fork 的 compose（源码构建 + 配置/数据挂载） |
| `deploy/wolf-local/ov.conf.example` | 可直接用的最小 ov.conf 模板 |
| `docs/zh/wolf-viking-startup-config.md` | 启动步骤 + ov.conf 逐段配置说明（模拟参数可直接抄） |
| `deploy/helm/openviking` | K8s Helm chart |