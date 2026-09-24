# wolf-viking 启动与配置指南

> 目标：把本仓库（`deploy/wolf-viking` 分支，含 `processing_mode=none` 等 fork 特性）
> 用 Docker 跑起来，并讲清楚 `ov.conf` 里每一段要配什么。
> 文中所有 key/地址都是**模拟值**，照抄能启动（网络类功能要真实值才能用）。

---

## 一、启动

### 1. 构建镜像（首次，约 10–30 分钟）

```bash
cd /path/to/OpenViking

docker build \
  --build-arg OPENVIKING_VERSION=0.4.15.dev35 \
  -t openviking:wolf .
```

- 直连 GitHub / Docker Hub / npm / PyPI 不稳时，加上代理 build-arg：
  `--build-arg http_proxy=http://host.docker.internal:7890 --build-arg https_proxy=http://host.docker.internal:7890 --build-arg no_proxy=localhost,127.0.0.1`
- 构建过程：Rust 工具链 → web-studio（npm）→ Python 环境（uv sync + 编译 Rust CLI / C++
  扩展 / ragfs）→ 精简运行时。编译阶段日志可能静默几分钟，属正常。

### 2. 启动容器

```bash
docker run -d --name openviking-local -p 1933:1933 \
  -v ~/.openviking:/app/.openviking \
  -e OPENVIKING_WITH_BOT=0 \
  openviking:wolf
```

前提：宿主 `~/.openviking/ov.conf` 已按第二节写好。若不想挂载，可用环境变量直灌
（见 2.1），或用 compose（见 1.3）。

- `OPENVIKING_WITH_BOT=0`：关掉内置 Bot（Bot 需要额外的 LLM 配置，先不用）。
- 数据（配置、workspace、`ovcli.conf`）都在 `/app/.openviking`，挂载后持久化。

### 3. 用 compose（推荐，等价于上面两步）

```bash
cd deploy/wolf-local
mkdir -p ov-data && cp ov.conf.example ov-data/ov.conf   # 编辑 ov.conf
docker compose up -d --build

# 走代理构建：
export BUILD_HTTP_PROXY=http://host.docker.internal:7890
docker compose up -d --build
```

### 4. 验证

```bash
curl -s http://localhost:1933/health   # {"status":"ok","healthy":true,...}
curl -s http://localhost:1933/ready    # 各项状态
```

`/ready` 各项含义：`agfs` / `vectordb` / `api_key_manager` / `embedding` 应为 `ok`；
`ollama` 为 `not_configured` 属正常。`embedding` 为 `error` 时服务器还能跑，但写入
（非 none 模式）会失败。

Studio：http://localhost:1933/studio/ ，用 `root_api_key` 登录。

---

## 二、配置：`ov.conf` 逐段说明

配置文件位置（容器内）：`/app/.openviking/ov.conf`，即宿主挂载的
`~/.openviking/ov.conf`。支持 `$VAR` 环境变量展开。

### 2.1 最简可用配置（可直接抄）

```json
{
  "server": {
    "host": "0.0.0.0",
    "port": 1933,
    "root_api_key": "test-key-123"
  },
  "storage": {
    "workspace": "./data",
    "agfs": { "backend": "local" },
    "vectordb": { "backend": "local" }
  },
  "embedding": {
    "dense": {
      "provider": "volcengine",
      "model": "doubao-embedding-vision-251215",
      "api_base": "https://ark.cn-beijing.volces.com/api/v3",
      "api_key": "sk-mock-embedding-key",
      "dimension": 1024,
      "input": "multimodal"
    }
  },
  "vlm": {
    "provider": "volcengine",
    "model": "doubao-seed-2-0-lite-260428",
    "api_base": "https://ark.cn-beijing.volces.com/api/v3",
    "api_key": "sk-mock-vlm-key"
  },
  "log": { "level": "INFO", "output": "stdout" }
}
```

不想挂载文件时，把这段 JSON 压成一行放进 `OPENVIKING_CONF_CONTENT` 环境变量即可；
容器首次启动会写入 `/app/.openviking/ov.conf`（**文件已存在则不再覆盖**）。

### 2.2 `server` — 必填

| 字段 | 说明 | 模拟值 |
| --- | --- | --- |
| `host` | 监听地址。容器里必须 `0.0.0.0` | `"0.0.0.0"` |
| `port` | 服务端口，与 `-p` 映射一致 | `1933` |
| `root_api_key` | **必填**。绑定非 localhost 时，不设它服务拒绝启动 | `"test-key-123"` |

`root_api_key` 用途：Studio 登录、管理接口（建账号/用户）认证。**它不能直接调数据
API**（读写内容），数据 API 需要下面建的 user key。

> 绑定 localhost 时可设 `auth_mode: "dev"` 免鉴权（`root_api_key` 可省），但容器场景
> 用不上。

### 2.3 `storage` — 必填

```json
"storage": {
  "workspace": "./data",
  "agfs": { "backend": "local" },
  "vectordb": { "backend": "local" }
}
```

| 字段 | 说明 |
| --- | --- |
| `workspace` | 数据根目录。容器内相对路径基于 `/app/.openviking`，所以默认落在挂载卷里 |
| `agfs.backend` | 文件存储：`local`（默认）/ `s3`（fork 支持阿里云 OSS） |
| `vectordb.backend` | 向量库：`local`（默认）/ `qdrant`（fork 支持）/ `http` / `cuvs` |

单机跑用 `local` 就行，零依赖。要接 Qdrant：

```json
"vectordb": {
  "backend": "qdrant",
  "qdrant": {
    "url": "http://qdrant:6333",
    "api_key": "mock-qdrant-key",
    "dense_vector_name": "vector",
    "sparse_vector_name": "sparse_vector",
    "meta_collection_name": "__openviking_meta",
    "enable_text_index": true
  }
}
```

### 2.4 `embedding` — 必填（否则启动崩）

不写这一段时，默认回落到**本地 embedding**（`bge-small-zh-v1.5-f16`），需要
`llama-cpp-python` + GGUF 模型文件；镜像里**没有**装，启动直接报：

```
EmbeddingConfigurationError: Local embedding is enabled but 'llama-cpp-python' is not installed
```

所以必须指向一个远端 provider：

```json
"embedding": {
  "dense": {
    "provider": "volcengine",
    "model": "doubao-embedding-vision-251215",
    "api_base": "https://ark.cn-beijing.volces.com/api/v3",
    "api_key": "sk-mock-embedding-key",
    "dimension": 1024,
    "input": "multimodal"
  }
}
```

| 字段 | 说明 |
| --- | --- |
| `provider` | `volcengine` / `openai` / `ollama` / `jina` / `gemini` / `voyage` / `dashscope` / `minimax` / `cohere` / `litellm` / `local` |
| `model` | 模型名（openai 例：`text-embedding-3-small`；ollama 例：`nomic-embed-text`） |
| `api_base` | API 端点；ollama 是 `http://<host>:11434/v1` |
| `api_key` | 云端 provider 必填；openai 兼容端点可写任意非空值 |
| `dimension` | 向量维度，**必须与模型真实输出一致**（bge 系 512/768、doubao 1024、openai-3-small 1536） |
| `input` | `multimodal`（图+文）或 `text`，按模型能力选 |

注意：

- 构造是**惰性**的，启动时不联网校验 —— 所以填假 key 也能启动；但 `/ready` 的
  embedding 探针会真实调用一次，失败则该项报 `error`。
- 维度不一致会导致后续检索/写入报错，第一次配置时就要定好。

### 2.5 `vlm` — 建议配（影响语义处理）

用于 L0/L1 摘要生成、记忆提取、图片理解。**不配也能启动**，但普通写入
（非 `processing_mode=none`）的后台任务会失败。

```json
"vlm": {
  "provider": "volcengine",
  "model": "doubao-seed-2-0-lite-260428",
  "api_base": "https://ark.cn-beijing.volces.com/api/v3",
  "api_key": "sk-mock-vlm-key",
  "temperature": 0.0,
  "max_retries": 2
}
```

- 字段结构与 embedding 类似（`provider`/`model`/`api_base`/`api_key`）。
- 备选 provider：`openai`（如 `gpt-5.4`）、`kimi`、`glm`、`openai-codex`（OAuth）等。
- 只想用 none 模式纯占位存储（不触发语义处理）时，VLM 可以不配。

### 2.6 `log` — 可选

```json
"log": { "level": "INFO", "output": "stdout" }
```

容器里建议 `output: "stdout"`，方便 `docker logs` 看。

### 2.7 `encryption` — 可选

默认**关闭**。需要落盘加密（多租户 API key 等）时再开：

```json
"encryption": {
  "enabled": true,
  "provider": "local",
  "local": { "key_file": "~/.openviking/master.key" }
}
```

容器内 `~` = `/app`，即挂载卷内，可持久化。

### 2.8 其余段落（可全部省略）

`cache`（Redis）、`rerank`（重排序）、`parsers`（PDF/图片解析）、`bot`（内置 Bot）、
`queue_workers`、`retrieval` 等都有合理默认值，单机起步不用管。
完整参考：`examples/ov.conf.example`（404 行，含各 provider 示例）。

---

## 三、首次使用：拿 key + 冒烟测试

### 3.1 建 user key

root key 只能管账号，数据 API 要用 user key：

```bash
curl -s -X POST http://localhost:1933/api/v1/admin/accounts/default/users \
  -H "Authorization: Bearer test-key-123" \
  -H "Content-Type: application/json" \
  -d '{"user_id": "admin", "role": "admin"}'
```

返回里的 `key` 就是 user key（形如 `ZGVmYXVsdA.YWRtaW4.xxxx`）。

### 3.2 none 模式写入（fork 特性）

```bash
curl -s -X POST http://localhost:1933/api/v1/content/write \
  -H "Authorization: Bearer <USER_KEY>" \
  -H "Content-Type: application/json" \
  -d '{"uri": "viking://resources/test.md", "content": "hello", "processing_mode": "none"}'
# → {"mode":"create","written_bytes":5,"content_updated":true,"semantic_status":"skipped","vector_status":"skipped"}
```

`semantic_status` / `vector_status` 都是 `skipped` 即生效：纯占位存储，不调
embedding/VLM。

### 3.3 普通写入 + 读取

```bash
# 普通写入（会排队做语义处理，需要 embedding + vlm 可用）
curl -s -X POST http://localhost:1933/api/v1/content/write \
  -H "Authorization: Bearer <USER_KEY>" -H "Content-Type: application/json" \
  -d '{"uri": "viking://resources/normal.md", "content": "hello"}'
# → semantic_status:"queued", vector_status:"queued"

# 读回
curl -s "http://localhost:1933/api/v1/content/read?uri=viking://resources/normal.md" \
  -H "Authorization: Bearer <USER_KEY>"

# 列目录（注意：ls 在 /api/v1/fs，不在 content）
curl -s "http://localhost:1933/api/v1/fs/ls?uri=viking://resources" \
  -H "Authorization: Bearer <USER_KEY>"
```

---

## 四、改配置后怎么生效

- **改 `ov.conf`** → `docker restart openviking-local`。
- **容器内交互改**：`docker exec -it openviking-local openviking-server init`
  （配置已存在时菜单选 `3) Update embedding` 只改 embedding 段）。
- **体检**：`docker exec openviking-local openviking-server doctor`（离线检查配置
  + 探活 embedding/VLM）。

---

## 五、常见问题

| 现象 | 原因 / 处理 |
| --- | --- |
| 启动即崩，报 `llama-cpp-python is not installed` | 没配 `embedding.dense`，见 2.4 |
| 启动报 root_api_key 相关错误 | `server.host` 非 localhost 时必须设 `root_api_key` |
| `/ready` 里 embedding 为 `error` | key/端点不对或网络不通；启动不受影响但写入会失败 |
| 容器起来后配置不生效 | `OPENVIKING_CONF_CONTENT` 只在 ov.conf **不存在**时写入；文件已存在则改文件本身 |
| 数据 API 返回 `PERMISSION_DENIED` | 用了 root key；数据 API 要用 user key（3.1） |
| 写入后一直 `queued` / 检索不到 | embedding/vlm 不可用，后台任务在重试；查 `docker logs` |
| 构建卡在拉基础镜像 | 先手动 `docker pull` 那几个基础镜像，或加代理 build-arg |