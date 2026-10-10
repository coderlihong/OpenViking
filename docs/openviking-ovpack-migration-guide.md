# OpenViking OVPack 迁移手册（带向量，不重算）

把一个子树（含文件、L0/L1 语义文件、向量）从一个租户/实例搬到另一个租户/实例，**不重新计算向量和 L0/L1**。操作基本用 `ov` CLI，个别探针用 `curl`。

能不能"不重算"，前提只有一个：**两端的向量配置必须一致**。所以本手册先讲这个前置条件（第 1 节），再讲怎么配连接、导出、导入。

```
        源实例                                目标实例
   ┌──────────────┐                     ┌──────────────┐
   │ embedding    │  ① 前置：两端配置必须一致  │ embedding    │
   │ .dense       │ ◀───── 逐项对齐 ─────▶ │ .dense       │
   └──────┬───────┘                     └──────▲───────┘
          │                                     │
   ② ov export --include-vectors          ④ ov import --vector-mode require
          │                                     │
          ▼                                     │
   simplewiki.ovpack ──── ③ 自动上传 ────────────┘
                        （还原向量，不重算）
```

---

## 0. 核心概念

导入参数 `--vector-mode` 决定向量怎么来：

| 值 | 行为 |
|---|---|
| `auto`（默认） | 包里向量完整且与目标端 embedding 配置兼容就复用，否则**重算** |
| `require` | 必须复用包里向量。包里没有、不完整、或与目标端配置不兼容 → **直接报错**，绝不偷偷重算 |
| `recompute` | 忽略包里向量，重新算一遍 |

迁移要保证「没重算」，就用 `require`——它会因为任何不兼容而失败，失败即说明有问题，比 `auto` 静默重算更可靠。

`--on-conflict` 决定目标端已存在同名资源时怎么办：`fail`（默认，报冲突）/ `overwrite`（覆盖）/ `skip`（跳过）。

---

## 1. 前置条件：两端向量配置必须一致

**这是整件事成立的基础，先做这一步。** 向量是源端 embedding 模型算出来的数值，目标端只有用**同一套**配置去检索，这些数值才有意义。配置不同则向量语义不可复用，`require` 会直接拒绝。

### 1.1 要比对的 6 个字段

`require` 会逐项比对下面 6 项，**差一项就报 `embedding <字段> mismatch`**：

| 字段 | 含义 | 本环境取值 |
|---|---|---|
| `provider` | 提供方类型 | `openai` |
| `model` | 模型名 | `qwen3-vl-embed` |
| `input` | 输入类型 | `multimodal` |
| `query_param` | 查询侧参数（非对称模型用） | 未设（对称模型） |
| `document_param` | 文档侧参数（非对称模型用） | 未设（对称模型） |
| `dimensions` | 向量维度 | `4096` |

**只有这 6 项参与比对。** `api_base`、`api_key`、`batch_size`、`wire_format`、`backend` 不参与比对，但 `api_base` 必须两端都可达——否则目标端做检索时，query 侧嵌入算不出来。

### 1.2 要改的配置：embedding（必改）+ VLM（建议）

`ov.conf` 里跟这套部署相关的模型配置就两块：**`embedding`（向量，迁移成败的关键）** 和 **`vlm`（生成 L0/L1 摘要的模型）**。下面是我们这套环境的真实值（本机 `10.147.254.3`）：

**embedding —— 两端 6 个字段必须一致：**

```json
"embedding": {
  "dense": {
    "provider": "openai",
    "model": "qwen3-vl-embed",
    "api_base": "http://10.147.254.3:8020/v1",
    "api_key": "sk-embed",
    "dimension": 4096,
    "input": "multimodal",
    "wire_format": "messages",
    "batch_size": 32,
    "backend": "volcengine"
  },
  "text_source": "content_only",
  "max_input_tokens": 4096
}
```

**vlm —— 目标端配好、服务可达即可（取值不必与源端一致）：**

```json
"vlm": {
  "provider": "openai",
  "model": "qwen3.8-next-flash",
  "api_base": "http://10.147.254.3:8003/v1",
  "api_key": "sk-qwen3-vl-32b",
  "thinking": false,
  "extra_request_body": { "chat_template_kwargs": { "enable_thinking": false } },
  "max_concurrent": 8
}
```

> VLM 为什么也要配：导入本身不重算 L0/L1（随包带来），但导入后**新增/改动**的文件要靠 VLM 生成摘要，检索时也要用到。不配的话迁移能成，但目标端后续入库会缺摘要。`thinking: false` 是这套 `qwen3.8-next-flash` 的要求（须关思考模式）。

### 1.3 三种改法（选一种）

**改法 A：直接编辑 `ov.conf`（最直接，`ov` 启动时读取）**

`ov.conf` 的解析顺序：`--config` 显式路径 → `OPENVIKING_CONFIG_FILE` 环境变量 → `~/.openviking/ov.conf` → `/etc/openviking/ov.conf`。

```bash
# 1) 备份（含密钥，先收紧权限）
cp ~/.openviking/ov.conf /tmp/ov.conf.bak && chmod 600 /tmp/ov.conf.bak

# 2) 编辑 embedding.dense 与 vlm 两块（用编辑器改，别用 sed 硬替换）
$EDITOR ~/.openviking/ov.conf

# 3) 重启服务让它生效（embedding 改动必须重启）
openviking-server --config ~/.openviking/ov.conf
```

Docker（`deploy/wolf-local`）部署的，改挂载进去的 `ov-data/ov.conf`，然后 `docker compose restart`：

```bash
cd deploy/wolf-local
$EDITOR ov-data/ov.conf
docker compose restart
```

**改法 B：Studio 页面（图形化，适合核对字段）**

浏览器打开实例的 `/studio/`，用 root 或 admin key 登录，进**「服务配置」（Server Configuration，路径 `/model-config`）**。页面里有 **Embedding** 和 **VLM（多模态模型）** 两个卡片，可直接填 `模型 / Provider / API 地址 / 密钥 / 维度`，也支持 JSON 模式填全部字段。

- 保存会**写回 `ov.conf`**。
- 页面明确提示：`vlm` 保存后**立即生效**；**`embedding` 改了要重启服务**才生效（向量维度在服务启动时固定）。
- 这个入口需要 root 权限（导航项对 root 才显示）。

**改法 C：命令行（HTTP API，可脚本化）**

`ov` CLI 目前**没有**改模型配置的子命令（`ov config` 只管 CLI 连哪个实例，不管服务端模型）。要命令行改就走服务端 admin API（需 root key）：

```bash
# 读当前模型配置（核对两端 6 字段）
curl -s -H "X-API-Key: $ROOT_KEY" http://<实例地址>/api/v1/admin/model-config

# 改 embedding（局部更新，未给的键保持原值）
curl -s -X PUT -H "X-API-Key: $ROOT_KEY" -H "Content-Type: application/json" \
  -d '{"embedding":{"dense":{"provider":"openai","model":"qwen3-vl-embed","api_base":"http://10.147.254.3:8020/v1","dimension":4096,"input":"multimodal","wire_format":"messages"}}}' \
  http://<实例地址>/api/v1/admin/model-config

# 改 vlm（保存即生效）
curl -s -X PUT -H "X-API-Key: $ROOT_KEY" -H "Content-Type: application/json" \
  -d '{"vlm":{"provider":"openai","model":"qwen3.8-next-flash","api_base":"http://10.147.254.3:8003/v1","thinking":false}}' \
  http://<实例地址>/api/v1/admin/model-config
```

这个接口是**字段级合并**：patch 里没给的键保留原值，不会把整段清空。`embedding` 改完响应里会标 `restart_required: ["embedding"]`，重启后生效。

> 注意：该接口保存时会把**整个 `ov.conf` 重新序列化**（按 schema 规范化字段、去掉默认值/空值），不是只改你给的那几行。所以手改过的注释、非标准键可能被规整掉——**命令行改之前先备份 `ov.conf`**。纯手操场景更推荐改法 A。

> 三种改法都落到同一个 `ov.conf`，效果等价。手操迁移建议用 **A**（清楚、可 diff）；核对字段用 **B**；批量/脚本用 **C**。

### 1.4 怎么核验两端确实一致

**看包里记的配置**（导出后执行，读 manifest 里固化的 embedding 指纹）：

```bash
unzip -p ./simplewiki.ovpack simplewiki/_ovpack/manifest.json | python3 -c "import sys,json;print(json.dumps(json.load(sys.stdin)['index']['dense']['embedding'],indent=2,ensure_ascii=False))"
```

**看两端 ov.conf 的 6 个字段**（各自执行，把 `$OVCONF` 换成该实例实际的 ov.conf 路径，然后肉眼比一遍）：

```bash
OVCONF=~/.openviking/ov.conf   # 或 --config 指定的那份，如 ~/.openviking-replica/ov.conf
python3 -c "import json;e=json.load(open('$OVCONF'))['embedding'];m=e.get('hybrid') or e.get('dense');print({k:m.get(k) for k in ('provider','model','input','query_param','document_param')},'dimension=',m.get('dimension'))"
```

两边打印出来的字典必须完全相同。若不同，先对齐再往下走，别用 `require` 硬上。

> 注意：manifest 里的指纹会**省略值为空的键**（本例 `query_param` / `document_param` 未设，所以只显示 4 项），这不影响比对——空值按 `None` 参与比较，与 ov.conf 里"未设"等价。

### 1.5 目标端嵌入服务要通

配置对齐了还不够，目标端必须**真能连上**同一套嵌入服务（检索时要用它算 query 向量）。

先确认嵌入模型已配置：

```bash
ov config switch dst
ov observer models
```

输出里应能看到 `Embedding Models` 表，Model 是 `qwen3-vl-embed`。看不到说明目标端没配嵌入模型。

再做一次**在线探针**（`ov` 没有直接命令，用 HTTP 探一下目标端地址）：

```bash
curl -s http://<目标端地址>/ready | python3 -c "import sys,json;print(json.load(sys.stdin)['checks']['embedding'])"
```

打印 `ok` 才算通。为 `error: ...` 或 `not_configured`，先解决嵌入服务可达性再往下走。

> 一句话：**6 个字段一致 + 目标端嵌入可达 → 才能用 `require` 不重算。** 做不到就退而求其次用 `auto`（可能静默重算）或 `recompute`（明确重算）。

---

## 2. 配置连接

`ov` 的连接信息存在 `~/.openviking/ovcli.conf`，通过保存的配置切换实例/租户。每个实例+租户存一份，用 `ov config switch` 切。

### 连接本地自建实例

本地实例（如 `http://127.0.0.1:1933`）用 `custom` 类型，`--url` 指定地址：

```bash
# 源实例（示例：本机 1933，replicasrc 租户）
printf '%s' "$SRC_KEY" | ov config add custom --name src --url http://127.0.0.1:1933 --api-key-stdin --activate

# 目标实例（示例：本机 1934，replicadst 租户）
printf '%s' "$DST_KEY" | ov config add custom --name dst --url http://127.0.0.1:1934 --api-key-stdin --activate
```

- key 从 stdin 传，**不落命令行历史、不进日志**。
- `--activate` 让这份配置立即生效。
- `--account` / `--user` 可选，用于显式指定 X-OpenViking-Account/User 头。

### 连接云端服务

官方托管服务用 `ov-service` 类型（地址固定为 `https://api.vikingdb.cn-beijing.volces.com/openviking`，不接 `--url`）：

```bash
printf '%s' "$SRC_KEY" | ov config add ov-service --name src-cloud --api-key-stdin --activate
```

### 切换与查看

```bash
ov config list          # 列出所有保存的配置
ov config show          # 查看当前生效的配置
ov config switch dst    # 切到目标配置
```

> 切换配置是全局生效的。一次迁移里要跨源、目标两个实例操作，**每条命令前先 `ov config switch`**，或在两条终端里各切各的。别在源实例上误跑导入。

---

## 3. 源端自检（必做）

导出时服务端会做一致性校验，盘面记录和索引对不上会直接报错。先自己查一遍：

```bash
ov config switch src
ov system consistency viking://resources/simplewiki
```

看返回：`missing_record_count` 必须为 0，`ok` 为 true 才能继续。

- `expected_count` 是**盘面该有的记录数**（L0 目录 + L1 目录 + L2 文本文件）。
- 若 `missing_record_count` > 0，说明有文件没进索引。等处理完成再查：

```bash
ov system wait --timeout 600
ov system consistency viking://resources/simplewiki
```

---

## 4. 导出（带向量）

```bash
ov config switch src
ov export viking://resources/simplewiki ./simplewiki.ovpack --include-vectors
```

- 第一个参数是源 URI，第二个是输出的本地文件路径。
- `--include-vectors` 必须带上，否则包里没有向量，导入时 `require` 会报错。
- 大库传得慢就耐心等，CLI 会流式下载。

导出后核对包里确实带了向量：

```bash
ls -lh ./simplewiki.ovpack
unzip -l ./simplewiki.ovpack | grep -E 'dense\.f32|index_records\.jsonl|manifest\.json'
```

要能看到 `dense.f32`（向量数据）和 `index_records.jsonl`（索引记录）两项。**没有 `dense.f32` 就说明没带向量。** 再用第 1.4 节第一条命令核一下 manifest 里的 embedding 指纹。

包里内容：

```
simplewiki/
├── files/...              文件树（含 .abstract.md / .overview.md）
└── _ovpack/
    ├── manifest.json      元信息（含 embedding 配置指纹）
    ├── index_records.jsonl 索引记录（每条含向量偏移）
    └── dense.f32          向量本体
```

---

## 5. 导入（不重算）

`ov import` 会**自动把本地包上传**到目标实例，再发起导入，一步到位，不用手动上传。

```bash
ov config switch dst
ov import ./simplewiki.ovpack viking://resources --on-conflict overwrite --vector-mode require
```

- 第一个参数是本地包路径，第二个是**目标父目录**。导入后会变成 `<父目录>/<包名>`，即 `viking://resources/simplewiki`。
- `--on-conflict overwrite` 让重复导入可覆盖；首次导入用 `fail` 也行。
- `--vector-mode require` 强制复用包里向量。若报 `embedding ... mismatch`，就是第 1 节的 6 个字段没对齐，回去对齐后重试。

成功会返回目标 URI。

**大包会看到「请求超时」，别慌，也别说失败。** CLI 对 `/api/v1/pack/import` 这一步用的是固定 60 秒超时，不随包大小调整。百 MB 级的包还原向量往往超过 60 秒，CLI 会打印 `请求超时` 退出——但**服务端仍在后台继续导入**。等它跑完再验收即可：

```bash
ov system wait --timeout 600
ov system consistency viking://resources/simplewiki
```

`missing_record_count` 归零就说明导入其实成功了。只有 `wait` 之后仍缺失，才是真失败。

---

## 6. 验收

**一致性**（盘面 vs 索引）：

```bash
ov config switch dst
ov system consistency viking://resources/simplewiki
```

`missing_record_count` 必须为 0。

**检索**：源、目标各跑一遍同样的 query，对比返回的 uri 和排序：

```bash
ov find "发动机冒黑烟" -u viking://resources/simplewiki -n 5
```

两边结果应一致（uri 和 score 都对得上）。这一步同时也证明了第 1 节的配置确实对齐——向量复用成功且检索可用。

机器可读输出用 `-o json`，注意它会在 JSON 前多打一行 `cmd: ...`，解析时先剥掉第一行：

```bash
ov find "发动机冒黑烟" -u viking://resources/simplewiki -n 5 -o json | tail -n +2 | python3 -c "import sys,json;[print(r['score'],r['uri']) for r in json.load(sys.stdin)['result']['resources']]"
```

---

## 7. 创建新租户（可选）

目标实例上还没有租户时，用 root key 建（root 操作要加 `--sudo`）：

```bash
ov config switch dst-root
ov admin create-account newtenant --admin admin
```

返回里带 `user_key`（api_key 认证模式下），就是该租户的管理员 key，拿它配一份新的 custom 配置当 `dst` 用：

```bash
printf '%s' "$NEW_KEY" | ov config add custom --name dst --url http://127.0.0.1:1934 --api-key-stdin --activate
```

给已存在的租户加用户：

```bash
ov admin register-user newtenant u1 --role admin
```

查看账户与用户：

```bash
ov admin list-accounts
ov admin list-users newtenant
```

---

## 8. 坑与排查

| 报错 | 原因 | 处理 |
|---|---|---|
| `embedding ... mismatch` | 两端 embedding 配置不一致（第 1 节的 6 个字段） | 对齐两端 `ov.conf` 的 `embedding.dense`，重启后重试 |
| `Cannot export incomplete OpenViking vector index snapshot` | 源端盘面与索引不一致 | 回到第 3 步，补齐索引再导出 |
| `package does not contain a dense vector snapshot` | 导出时漏了 `--include-vectors` | 重新导出 |
| `dense vector snapshot is incomplete` | 包不完整（传输截断） | 重新导出并核对包大小 |
| `snapshot cannot be restored into a sparse or hybrid index` | 目标端索引类型不支持稠密向量还原 | 确认目标端用同样的 dense 索引 |
| `Resource already exists` | 目标已有同名资源且 `--on-conflict fail` | 改 `--on-conflict overwrite` |

**导出不剪枝陈旧记录。** 导出的是「索引里现有的记录」，不是「盘面该有的记录」。若之前删过文件但索引没清干净，那些陈旧向量会一起进包。验收时以 `consistency` 的 `expected_count`（盘面）为准，别拿包里的记录行数当基准。需要清理可以跑：

```bash
ov reindex viking://resources/simplewiki --mode prune_orphans --dry-run   # 先预览
ov reindex viking://resources/simplewiki --mode prune_orphans            # 确认后执行
```

**大小写折叠。** macOS 的 APFS 大小写不敏感，仅大小写不同的同名文件会被折叠成一个。若源端是 Linux、目标端是 macOS，这类文件会丢；反过来不会。迁移前先确认两端文件系统是否一致，必要时把这类文件名规范化。

**验收不要用 scroll 做集合对比。** `/api/v1/debug/vector/scroll` 是 offset 分页，在无序数据上会重复 id、漏项，得出假差异。权威信号是「盘面文件 sha256 + 每子树 vector/count + consistency」。

**大包慢。** 几百 MB 的包传输要几分钟，CLI 会流式处理，中断了直接重跑，不用等。

---

## 9. 回滚

导入是覆盖式落盘，目标端原数据在导入时已被替换。要回滚：

- 导入前先把目标端原资源导出留底：把第 4 步的源换成目标跑一遍，得到一份目标端的备份包。

```bash
ov config switch dst
ov export viking://resources/simplewiki ./dst-backup.ovpack --include-vectors
```

- 回滚时用同样的 `ov import` 流程，把备份包导回目标端。

`ov backup` 可以一次性导出全部公共 scope，但那是 restore-only 格式，只能用 `ov restore` 还原，不能用来做单子树迁移：

```bash
ov backup ./openviking-backup.ovpack --include-vectors
ov restore ./openviking-backup.ovpack
```