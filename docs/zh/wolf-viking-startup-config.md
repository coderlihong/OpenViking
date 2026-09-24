# wolf-viking 容器配置指南

> 只讲**容器启动之后**怎么配模型和参数。构建/启动步骤见
> [wolf-viking-local-deploy.md](./wolf-viking-local-deploy.md)。

## 一、要配什么（依赖关系）

| 配置 | 必填 | 不配的后果 |
| --- | --- | --- |
| `server.root_api_key` | **是** | `host` 非 localhost 时服务拒绝启动 |
| `embedding.dense` | **是** | 默认回落到本地 `bge-small-zh-v1.5-f16`，需要 `llama-cpp-python`，镜像没装 → **启动崩溃** |
| `vlm` | 否 | 服务正常跑；普通写入的语义处理失败，`none` 模式不受影响 |
| `storage` | 否 | 默认 `local`（agfs + vectordb 零依赖），单机够用 |
| `log` / `encryption` / `cache` 等 | 否 | 有默认值 |

配置文件位置：容器内 `/app/.openviking/ov.conf`（即宿主挂载的 `~/.openviking/ov.conf`）。

## 二、怎么配

### 方式 A：容器内向导（推荐）

```bash
docker exec -it openviking-local openviking-server init
```

配置已存在，先出菜单 —— 只改 embedding 选 **3**：

```
1) Start over
2) Update VLM
3) Update embedding
4) Update server & auth
5) Cancel
```

按提示选 provider、填 `api_base` / `api_key` / `model` / `dimension`，自动写回 ov.conf。

⚠️ 容器内没有 ollama，向导里的 "local Ollama" 选项不可用；端点必须容器内可达。

### 方式 B：直接编辑 ov.conf

宿主上编辑 `~/.openviking/ov.conf`，核心两段：

```json
{
  "server": { "host": "0.0.0.0", "port": 1933, "root_api_key": "change-me" },
  "embedding": {
    "dense": {
      "provider": "volcengine",
      "model": "doubao-embedding-vision-251215",
      "api_base": "https://ark.cn-beijing.volces.com/api/v3",
      "api_key": "sk-真实key",
      "dimension": 1024,
      "input": "multimodal"
    }
  },
  "vlm": {
    "provider": "volcengine",
    "model": "doubao-seed-2-0-lite-260428",
    "api_base": "https://ark.cn-beijing.volces.com/api/v3",
    "api_key": "sk-真实key"
  }
}
```

要点：

- `dimension` 必须和模型真实输出一致（doubao 1024、openai-3-small 1536、bge 512/768），配错后续写入/检索报错。
- 可用 provider：`volcengine` / `openai` / `ollama` / `jina` / `gemini` / `voyage` / `dashscope` / `minimax` / `cohere` / `litellm`。任意 OpenAI 兼容端点用 `provider: "openai"` + 自定义 `api_base`。
- 构造是惰性的，填假 key 也能启动；但 `/ready` 探针会真实调用一次，失败则 embedding 项报 `error`。
- 值里支持 `$VAR` 环境变量展开。

### 方式 C：环境变量直灌（仅首次启动）

ov.conf **不存在**时，用 `OPENVIKING_CONF_CONTENT` 传 JSON 字符串，容器启动时写入。文件已存在后不再覆盖。

## 三、生效 + 验证

```bash
docker restart openviking-local
curl -s http://localhost:1933/ready | python3 -m json.tool
```

`agfs` / `vectordb` / `api_key_manager` / `embedding` 都应为 `ok`；`ollama` 为 `not_configured` 属正常。`embedding` 为 `error` 时服务能跑，但非 none 写入会失败。

离线体检（不启动服务）：

```bash
docker exec openviking-local openviking-server doctor
```

## 四、冒烟测试

```bash
# 1. 建 user key（数据 API 不能用 root key）
curl -s -X POST http://localhost:1933/api/v1/admin/accounts/default/users \
  -H "Authorization: Bearer <ROOT_KEY>" -H "Content-Type: application/json" \
  -d '{"user_id": "admin", "role": "admin"}'      # 返回的 key 字段即 user key

# 2. none 模式写入（fork 特性，不调 embedding/VLM）
curl -s -X POST http://localhost:1933/api/v1/content/write \
  -H "Authorization: Bearer <USER_KEY>" -H "Content-Type: application/json" \
  -d '{"uri": "viking://resources/test.md", "content": "hello", "processing_mode": "none"}'
# → semantic_status:"skipped", vector_status:"skipped"

# 3. 读回 / 列目录
curl -s "http://localhost:1933/api/v1/content/read?uri=viking://resources/test.md" -H "Authorization: Bearer <USER_KEY>"
curl -s "http://localhost:1933/api/v1/fs/ls?uri=viking://resources" -H "Authorization: Bearer <USER_KEY>"
```

## 五、常见报错

| 现象 | 原因 |
| --- | --- |
| 启动崩 `llama-cpp-python is not installed` | 没配 `embedding.dense` |
| 启动报 root_api_key 错误 | `host` 非 localhost 时必须设非空 `root_api_key` |
| 数据 API `PERMISSION_DENIED` | 用了 root key；数据 API 要 user key |
| 改 ov.conf 不生效 | 改完要 `docker restart`；`OPENVIKING_CONF_CONTENT` 只在文件不存在时写入 |
| `/ready` embedding `error` | key/端点不对或网络不通 |
| 写入一直 `queued` / 检索不到 | embedding 或 vlm 不可用，后台任务在重试；`docker logs` 看详情 |