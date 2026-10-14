# wolf-viking 容器配置指南

> 只讲**容器启动之后**的配置与使用。构建/启动步骤见
> [wolf-viking-local-deploy.md](./wolf-viking-local-deploy.md)。

## 一、端口与服务入口

**只有一个端口：1933**。前端、MCP、REST API 全在它上面，靠路径区分：

| 入口 | 地址 | 说明 |
| --- | --- | --- |
| 前端（Studio） | `http://localhost:1933/studio/` | 浏览器打开；访问 `/` 会 302 跳到这里 |
| MCP | `http://localhost:1933/mcp` | Streamable HTTP，17 个工具（find/search/index_search/vector_search/read/…） |
| REST API | `http://localhost:1933/api/v1/*` | 数据读写、管理、系统接口；Swagger 在 `/docs` |
| 健康检查 | `/health`、`/ready` | 容器 healthcheck 用的也是它 |
| WebDAV | `/webdav/resources` | 可选，映射 `viking://resources` |

没有独立的 MCP 端口或前端端口 —— 一个进程一个端口，全部复用。

- **1934**：官方 compose 里 Caddy 反代的入口（转发到 1933）。wolf-local compose **没有**这个，直接访问 1933 即可。
- **改端口**：启动时设 `OPENVIKING_SERVER_PORT`（compose 环境变量或 `-e`）。

认证：Studio 登录用 `server.root_api_key`；数据 API（含 MCP）必须用 **user key**，root key 会报 `PERMISSION_DENIED`。

## 二、要配什么（依赖关系）

| 配置 | 必填 | 不配的后果 |
| --- | --- | --- |
| `server.root_api_key` | **是** | `host` 非 localhost 时服务拒绝启动 |
| `embedding.dense` | **是** | 默认回落到本地 `bge-small-zh-v1.5-f16`，需要 `llama-cpp-python`，镜像没装 → **启动崩溃** |
| `vlm` | 否 | 服务正常跑；普通写入的语义处理失败，`none` 模式不受影响 |
| `storage` | 否 | 默认 `local`（agfs + vectordb 零依赖），单机够用 |
| `log` / `encryption` / `cache` 等 | 否 | 有默认值 |

配置文件位置：容器内 `/app/.openviking/ov.conf`（即宿主挂载的 `~/.openviking/ov.conf`）。

## 三、怎么配

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

### 改完生效

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

MCP 连通性（同样用 user key）：

```bash
curl -s -X POST http://localhost:1933/mcp \
  -H "Authorization: Bearer <USER_KEY>" \
  -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","method":"tools/list","id":1,"params":{}}'
```

## 五、数据迁移（`.ovpack` 打包 / 导入）

`ov` CLI 在容器内 `/app/.venv/bin/ov`。首次使用先配 CLI 连接（写 `ovcli.conf`，需要 **user key**，root key 不行）：

```bash
docker exec openviking-local sh -c '
cat > /app/.openviking/ovcli.conf <<EOF
{"url":"http://127.0.0.1:1933","api_key":"<USER_KEY>"}
EOF
ov language zh-CN'
```

### 5.1 子目录导出 / 导入

```bash
# 导出（默认不含向量，导入后自动重新向量化）
ov export viking://resources/proj /tmp/proj.ovpack

# 导入：目标是**父目录**，包根名保留 → 结果为 viking://resources/imported/proj
ov import /tmp/proj.ovpack viking://resources/imported/ --on-conflict overwrite
```

- 只支持**目录**（`viking://resources/xxx` / `viking://user/xxx`），单文件导不了。
- `--on-conflict`：`fail`（默认）/ `overwrite` / `skip`。
- 同 embedding 配置的环境想跳过重算：导出加 `--include-vectors`，导入加 `--vector-mode auto`（`recompute` 强制重算，`require` 快照缺失就报错）。

### 5.2 向量迁移（要不要重算 embedding）

默认**不迁移向量**：文本（含 `.abstract.md` / `.overview.md` 侧车）会过去，导入后由目标环境**重新向量化**（只调 embedding，不重跑 LLM 语义）。

想原样搬向量（冷迁移加速）：

```bash
# 源端导出：带向量
ov export viking://resources/proj /tmp/proj.ovpack --include-vectors

# 目标端导入：require = 必须恢复快照，不兼容就报错（绝不静默重算）
ov import /tmp/proj.ovpack viking://resources/ --vector-mode require
```

`--vector-mode` 三档：`auto`（默认，兼容则恢复否则重算）/ `recompute`（强制重算）/ `require`（必须恢复，否则报错）。

**兼容性只看 6 个字段**（`api_base` / `api_key` 不比对）：

```
provider, model, input, query_param, document_param, dimensions
```

#### 怎么确保两边一致（操作方式）

**原则：embedding 必须在首次启动、写入任何数据之前就定好。** 后续换模型 = 旧向量全部失效（校验会拦，但 none 模式文件和未索引内容不会拦）。

1. **源端定稿后导出，记录 6 字段**：

   ```bash
   # 直接看配置里的 embedding 段
   docker exec openviking-local cat /app/.openviking/ov.conf | python3 -c "import json,sys; print(json.load(sys.stdin)['embedding'])"

   # 或者看包内快照自带的元数据（权威，以实际导出为准）
   unzip -p /tmp/proj.ovpack '*/_ovpack/manifest.json' \
     | python3 -c "import json,sys; print(json.load(sys.stdin)['index']['dense']['embedding'])"
   ```

2. **目标端启动前把 embedding 段配成完全相同的 6 个值**（照抄，尤其 `input`、`query_param`、`document_param`、`dimension` 别漏 —— 只写 `provider`/`model` 两个字段不算一致）。

3. **导入后用 `require` 验证**：能导入成功 = 6 字段一致且快照完整；报 `dense vector snapshot is incompatible` = 有字段不一致，按报错字段改配置或改用 `--vector-mode recompute`。

4. **导出前确保索引已完成**：`ov system consistency viking://resources/proj` 必须 ok，否则 `--include-vectors` 直接拒绝导出。

⚠️ 注意：

- 校验只比字段值。**同名 model 但实际后端是不同模型**（例如换了 `api_base` 指向另一个部署）会被判"兼容"，但向量语义空间已变，检索会悄悄变差。要真一致，就别动后端。
- **none 模式写入的文件没有向量记录**，会让 `--include-vectors` 导出直接失败（`Cannot export incomplete OpenViking vector index snapshot`）。纯 none 的库只能默认重算迁移（none 文件会被顺便向量化 —— 这是 import 的固有行为，无法跳过）。
- 快照只支持**纯 dense** 索引；`sparse_weight > 0`（hybrid）时拒绝导出。默认配置就是纯 dense。
- Session 永不迁移向量（源、目标都跳过）；`.relations.json`、temp/queue/upload/locks 不迁移。
- 向量主键 `md5(account_id:uri)` 恢复时按目标账号重算，跨账号迁移没问题。

### 5.3 全量备份 / 恢复

顶层 scope 根（`viking://resources` 整个）不能用 export，要用 backup：

```bash
ov backup /tmp/full.ovpack --include-vectors             # 公开 scope 全量（resources + 本账号所有 user）
ov restore /tmp/full.ovpack --vector-mode require        # 只能 restore，不能普通 import
```

- 需要 ROOT / ADMIN 权限；包内**不含**账号和 API key，恢复后要重建用户。
- 备份是逐文件在线读取，非原子快照；要严格一致就先停写入。

### 5.4 跨机迁移

```bash
# 源机导出 → 拷出 → 目标机拷入 → 导入
docker cp openviking-local:/tmp/proj.ovpack ./proj.ovpack
scp proj.ovpack <target>:~/ && ssh <target> 'docker cp ~/proj.ovpack openviking-local:/tmp/'
docker exec openviking-local ov import /tmp/proj.ovpack viking://resources/ --vector-mode require
```

## 六、常见报错

| 现象 | 原因 |
| --- | --- |
| 启动崩 `llama-cpp-python is not installed` | 没配 `embedding.dense` |
| 启动报 root_api_key 错误 | `host` 非 localhost 时必须设非空 `root_api_key` |
| 数据 API / MCP `PERMISSION_DENIED` | 用了 root key；数据 API 要 user key |
| 改 ov.conf 不生效 | 改完要 `docker restart`；`OPENVIKING_CONF_CONTENT` 只在文件不存在时写入 |
| `/ready` embedding `error` | key/端点不对或网络不通 |
| 写入一直 `queued` / 检索不到 | embedding 或 vlm 不可用，后台任务在重试；`docker logs` 看详情 |
| `ov` 报 "needs a display language" | 先跑 `ov language zh-CN` |
| import 报 "Top-level scope ovpack packages must be imported to viking://" | 导出的是顶层 scope 根，改用 `ov backup` / `ov restore` |
| export 报 "not a directory" | 只支持目录导出，单文件不行 |
| export `--include-vectors` 报 "incomplete OpenViking vector index snapshot" | 索引未完成或有 none 模式文件；先 `ov system consistency <uri>` 定位 |
| import 报 "dense vector snapshot is incompatible" | 目标 embedding 配置与包内 6 字段不一致；对齐配置或改用 `--vector-mode recompute` |