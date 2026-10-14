# 检索接口参考

OpenViking 对外暴露的检索操作一共 11 个：语义检索（`find` / `search` / `vector_search`）、清单定位（`index_search`）、词面定位（`grep` / `glob`）、目录下探（`ls` / `tree`）、内容读取（`read` / `abstract` / `overview` / `download`）。HTTP 与 MCP 同端口同参数，本文两面一起写。

调用方进来时手里没有 URI——起点永远是库的根 `viking://resources`。先看三条调用链路怎么从根走到全文，再看后面的接口参数。

## 六条能力 → 三条链路

| 能力 | 走哪条 |
|------|--------|
| 1.1 L0/L1 导航检索 + L2 精读 | 链路 A |
| 1.2 目录 list 启发式披露、逐层下探 | 链路 A |
| 1.3 index.md / grep / BM25 关键词定位（目录过大时） | 链路 B |
| 2.1 向量 image + text 检索 | 链路 C |
| 2.2 meta 信息过滤检索 | 链路 C |
| 2.3 子块命中 → 父块/全文溯源 | 链路 C |

## 链路 A：结构导航，从根逐层下探（能力 1.1 + 1.2）

OpenViking 的内容分三层：L0 目录摘要、L1 目录概览、L2 正文全文。从根出发，先用 L0/L1 看「哪些目录相关」，再逐层下钻拿 L2 全文，避免一上来把大量全文塞给模型。

```text
第 1 步  L0/L1 导航（1.1）
         vector_search(query="液压泵 结构", level=[0,1], limit=5)
         # 不传 target_uri 就是从根搜全库；回来的是 .abstract.md / .overview.md
         # 目录摘要行，看哪些目录名相关
第 2 步  启发式披露下探（1.2）
         ls(uri=<第 1 步的相关目录>)
         # 列一层子目录与文件（带摘要），决定往哪个方向继续走
第 3 步  L2 精读（1.1 后半）
         read(uri=<定位到的文件>)                # L2 全文
         或 vector_search(query=..., level=[2])  # 直接拿正文行
```

实测（menus 手册树）：level=[0,1] 5 条命中 0.4s；list 三层下探 11→12→文件；read 拿到全文。

当前目录下文件数量过大、逐层 ls 翻不过来时，第 2 步改走链路 B 的关键词披露。

## 链路 B：关键词披露（能力 1.3）

把「翻目录」变成「问关键词」：先在当前目录的清单和正文里做关键词定位，命中相关子目录/文件后回到链路 A 继续下钻或精读。

```text
第 1 步  清单定位
         index_search(uri=<当前目录>, query="Pump")
         # 只扫该目录子树各级的 index.md 清单条目，毫秒级，
         # 命中回条目 URI 与所在 index.md 行号
第 2 步  清单没这个词不代表正文没有，转词面或向量兜底：
         grep(uri=<当前目录>, pattern="先导泵")                    # 正则行号证据
         vector_search(query="先导泵 故障", target_uri=<当前目录>)  # BM25+向量语义
第 3 步  拿到子目录/文件后 → 链路 A 第 2、3 步（ls 下探或 read 精读）
```

实测：index_search 扫 121 份清单 1.5s 命中；grep 3 处正文命中；向量命中后 read 全文。

## 链路 C：向量检索——图文、meta 过滤、父子溯源（能力 2.1 + 2.2 + 2.3）

```text
第 1 步  发查询（2.1：text 和 image 都走这里）
         文本：vector_search(query="液压泵 标准值")            # 不传 target_uri = 从根搜全库
         图片：vector_search(image_url="viking://…/xxx.jpg")   # 或外链 / base64
第 2 步  meta 过滤收窄（2.2）
         meta_filters=[{"field":"category","values":["text"]}]    # 只要文本行
         meta_filters=[{"field":"category","values":["images"]}]  # 只要图片行
第 3 步  父子链溯源（2.3：命中是子块，向上两跳是现成数据）
         matches[].text        子块正文（命中点，够用就直接用）
         matches[].parent_text 父块正文（随行已带，更大的上下文）
         uri（= meta.source_uri）→ read(uri) 拿源 md 全文
```

实测（menus 手册树）：图查图 top1 相关度 0.82；category=text/images 过滤严格互斥 10/10；chunk_text 1401 字 < parent_text 4027 字 < 全文 10829 字，同 `parent_chunk_id` 的多行 parent_text 逐字相同取一份即可。

MCP 文本输出不打印 parent_text，第 3 步的父块与全文都用 `read` 按 URI 打开。子块正文带合成标题前缀，要逐字原文以 `content/read` 为准。

## 接口总表

| 操作 | HTTP | MCP | 用途一句话 |
|------|------|-----|-----------|
| find | `POST /api/v1/search/find` | `find` | 语义检索主入口 |
| search | `POST /api/v1/search/search` | `search` | find + 会话意图分析 |
| vector_search | `POST /api/v1/vector/search` | `vector_search` | 语义检索解耦面，阈值真实生效 |
| index_search | `POST /api/v1/index/search` | `index_search` | 目录清单关键词定位，毫秒级 |
| grep | `POST /api/v1/search/grep` | `grep` | 正则找原文行号 |
| glob | `POST /api/v1/search/glob` | `glob` | 文件名模式找 URI |
| ls | `GET /api/v1/fs/ls` | `list` | 列一层目录 |
| tree | `GET /api/v1/fs/tree` | `tree` | 看多层目录树 |
| read | `GET /api/v1/content/read` | `read` | 读命中条目全文 |
| abstract / overview | `GET /api/v1/content/abstract` `/overview` | — | 读目录摘要/概览（L0/L1） |
| download | `GET /api/v1/content/download` | — | 取图片/二进制原始字节 |

## 通用约定

| 项 | 值 |
|----|----|
| 基址 | `http://<host>:1933` |
| MCP 端点 | `http://<host>:1933/mcp` |
| 鉴权 | `Authorization: Bearer <KEY>` |
| 成功响应 | `{"status":"ok","result":{...}}` |
| 失败响应 | `{"status":"error","error":{"code":...,"message":...}}` |
| 自动文档 | `GET /docs`、`GET /openapi.json` |

判断成败只看顶层 `status`。请求带未知字段一律 400 报错，不静默忽略。

## rerank 开关

三个语义检索接口（find / search / vector_search）共用一个开关：

| 参数 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `rerank` | bool | `true` | `false` 跳过 rerank 模型调用，按 RRF 融合序返回，快约三成 |

两档区别：

- `rerank=true`（默认）：召回后过 rerank 模型重打分，score 是 0–1 的相关度，排序质量最好，耗时大头在这里。
- `rerank=false`：跳过模型调用，score 是 RRF 融合分（量级 ~0.0x），只用于排序，不是相关度含义；要快、或自己做重排时用。

注意：`rerank=false` 时 score 不在 0–1 域，按分数截断（`score_threshold` / `min_score`）的常用阈值不适用，要么不传阈值要么按融合分另定。

实测同一查询（limit=15）：

| 面 | rerank | 耗时 | 分数域 |
|----|--------|------|--------|
| vector_search | true | 4.2s | 0–1 相关度 |
| vector_search | false | 2.9s | RRF ~0.016 |
| find | true | 3.8s | 0–1 相关度 |
| find | false | 2.5s | RRF ~0.016 |

```bash
# 关闭 rerank
-d '{"query":"阀","target_uri":"viking://resources/raw/menus/820676206347751","limit":15,"rerank":false}'
```

MCP 三个工具同样有 `rerank` 参数。

## find

语义检索统一入口：文本、图片、meta 过滤、时间窗都从这进。

`POST /api/v1/search/find`；MCP 工具 `find`。

| 参数 | 类型 | 必填 | 默认 | 说明 |
|------|------|------|------|------|
| `query` | string | 条件必填 | `""` | 无 `image_url` 且无 `filter` 时必填 |
| `image_url` | string | 否 | — | 图片查询：`viking://`、http(s)、base64 |
| `target_uri` | string \| string[] | 否 | `""` | 限定检索范围到该 URI 前缀 |
| `context_type` | string \| string[] | 否 | — | `memory` / `resource` / `skill` |
| `level` | int \| int[] | 否 | — | `0`=摘要、`1`=概览、`2`=正文 |
| `limit` | int | 否 | `10` | 最大返回条数 |
| `score_threshold` | number | 否 | 配置值 | find 混合路径不消费此参数，要用阈值用 vector_search |
| `filter` | object | 否 | — | 结构化元数据过滤表达式 |
| `meta_filters` | object[] | 否 | — | meta 硬过滤，最多 16 条，见「meta 过滤」 |
| `meta_hints` | object[] | 否 | — | meta 软提示，默认只加权不过滤 |
| `tags` | string[] | 否 | — | 显式标签，`k=v` 形式，多值 AND |
| `since` / `until` | string | 否 | — | 时间窗：`2h`、`7d`、日期、ISO 8601 |
| `time_field` | string | 否 | `updated_at` | 时间窗作用字段 |
| `include_provenance` | bool | 否 | `false` | 附带 provenance |
| `read_content` | bool | 否 | `false` | 内联返回全文 |
| `rerank` | bool | 否 | `true` | 见「rerank 开关」 |
| `telemetry` | bool \| object | 否 | `false` | 附带遥测（仅 HTTP） |

```bash
curl -X POST http://127.0.0.1:1933/api/v1/search/find \
  -H "Authorization: Bearer <KEY>" -H "Content-Type: application/json" \
  -d '{"query":"液压泵 标准值","limit":5}'
```

返回：`result.resources[]`，每条含 `uri`、`level`、`score`、`abstract`、`matches[]`（子块/父块）、`meta`；顶层另有 `memories`、`skills`、`total`。

## search

在 find 之上加会话上下文与意图分析。参数 = find 全表 + 下表（`rerank` 同样适用）。

| 参数 | 类型 | 必填 | 默认 | 说明 |
|------|------|------|------|------|
| `session_id` | string | 否 | — | 会话 ID，提供时读历史做意图分析 |
| `mode` | string | 否 | `list` | `list`=排名列表（形态同 find）/ `context`=直接给组装好的上下文块 |
| `query_expansion` | string | 否 | `auto` | 查询扩展，仅 `mode=context` |
| `max_tokens` | int | 否 | `1600` | 组装预算，64–32000，仅 `mode=context` |
| `quotas` | object | 否 | — | 按分类条数配额，仅 `mode=context` |
| `purpose` | string | 否 | — | `chat` / `coding`，仅 `mode=context` |
| `detail` | string \| object | 否 | `auto` | 档位，仅 `mode=context` |
| `dedup_turns` | int | 否 | `0` | 跨轮去重窗口 |

约束：`mode=list` 不收 context 专属参数；`mode=context` 不支持 `target_uri` 和 `read_content`；传了 `image_url` 跳过意图分析直接走图片检索。

## vector_search

解耦的向量检索面：与 find 同一召回管线，但不带会话意图和查询扩展。比 find 多两点：`score_threshold` 真实生效（按 rerank 后最终分过滤），参数校验更严（未知字段直接 400）。MCP 侧阈值名为 `min_score`。

参数表与 find 一致（端点 `POST /api/v1/vector/search`，MCP 工具 `vector_search`），`node_limit` 不支持，`rerank` 同样适用。

## index_search

在目录子树各级的 `index.md` 清单条目上做关键词匹配，毫秒级。不碰向量索引——清单没有的词不代表正文没有，转 `vector_search` 或 `grep`。

`POST /api/v1/index/search`；MCP 工具 `index_search`。

| 参数 | 类型 | 必填 | 默认 | 说明 |
|------|------|------|------|------|
| `uri` | string | 是 | — | 起点目录 URI |
| `query` | string | 是 | — | 关键词，竖线 `\|` 分隔表示「或」 |
| `limit` | int | 否 | `20` | 条目上限，1–200 |
| `case_insensitive` | bool | 否 | `false` | 忽略大小写 |
| `level_limit` | int | 否 | `10` | 子树深度上限，0–64；0=只扫自身 |
| `include_content` | bool | 否 | `false` | 返回命中的清单行原文 |

打分：条目名命中权重 2、描述命中权重 1，归一化后只用于 top-n 排序。

```bash
curl -X POST http://127.0.0.1:1933/api/v1/index/search \
  -H "Authorization: Bearer <KEY>" -H "Content-Type: application/json" \
  -d '{"uri":"viking://resources/raw/menus/820676206347751","query":"Pump","limit":5}'
```

返回 `result.matches[]`（`name`/`uri`/`score`/`line`/`content?`）+ `count` + `index_files_scanned`。校验：空 query 400、uri 不存在 404、uri 是文件 400、无清单空结果不报错。

## grep

正则找原文，行号级证据。

`POST /api/v1/search/grep`；MCP 工具 `grep`。

| 参数 | 类型 | 必填 | 默认 | 说明 |
|------|------|------|------|------|
| `uri` | string | 是 | — | 起点 URI，扫全子树 |
| `pattern` | string（HTTP）/ string[]（MCP） | 是 | — | 正则；MCP 可传数组并发多模式 |
| `exclude_uri` | string | 否 | — | 排除该前缀 |
| `case_insensitive` | bool | 否 | `false` | 忽略大小写 |
| `node_limit` | int | 否 | `256` | 最大返回条数 |
| `level_limit` | int | 否 | `10` | 最大遍历深度 |

```bash
-d '{"uri":"viking://resources/raw/menus/820676206347751","pattern":"先导泵","node_limit":5}'
# → {"matches":[{"line":18,"uri":"…","content":"…"}],"count":2}
```

## glob

文件名模式找 URI。

`POST /api/v1/search/glob`；MCP 工具 `glob`。

| 参数 | 类型 | 必填 | 默认 | 说明 |
|------|------|------|------|------|
| `pattern` | string | 是 | — | glob 模式，如 `**/*.md` |
| `uri` | string | 否 | `viking://` | 起点 |
| `node_limit` | int | 否 | `256` | 最大返回条数 |

```bash
-d '{"pattern":"**/4.4.3*.md","uri":"viking://resources/raw/menus/820676206347751"}'
# → {"matches":["viking://…/4.4.3_Hydraulic_Pump.md"],"count":1}
```

## ls 与 tree

目录下探，纯结构浏览不打分。

`GET /api/v1/fs/ls`、`GET /api/v1/fs/tree`；MCP 工具 `list`、`tree`。

ls 主要参数（query string）：`uri`（必填）、`recursive`（递归列全部）、`simple=true`（只回 URI 数组）、`sort_by`（`name`/`mtime`）、`node_limit`（默认 1000）。tree 少 `simple`/`recursive`，多 `level_limit`（默认 3）。

```bash
curl -G -H "Authorization: Bearer <KEY>" \
  --data-urlencode "uri=viking://resources/raw/menus/820676206347751" \
  --data-urlencode "limit=3" \
  "http://127.0.0.1:1933/api/v1/fs/ls"
```

## read 与分层内容

| 端点 | 层 | 参数 | 说明 |
|------|----|------|------|
| `GET /api/v1/content/read` | L2 | `uri`、`offset`、`limit`（-1 到底） | 正文全文 |
| `GET /api/v1/content/abstract` | L0 | `uri` | 目录聚合摘要 |
| `GET /api/v1/content/overview` | L1 | `uri` | 目录概览 |
| `GET /api/v1/content/download` | — | `uri` | 原始字节落盘用 |

MCP `read` 收 `uris` 数组一次多读；读图片/音频返回原生块，无需二次下载。

## meta 过滤

`meta_filters` 是硬过滤（命中必须满足），`meta_hints` 是软提示（默认只加权）。每条形如：

| 键 | 说明 |
|----|------|
| `field` | 字段名，支持中英文别名 |
| `values` | 1–32 个字符串，同字段多值 OR，跨字段 AND |
| `op` | `exact`（默认）/ `prefix` / `contains` / `fuzzy` |
| `include_unknown` | `true` 时纳入该字段为空的记录 |

`category` 取 `images`/`text` 两值，是图片行与文本行的判别键；文本查询也能命中图片行，要纯文本就加 `meta_filters category=text`。

严格字段（`category`、`source_document_id`、主体码/BOM/DTC）只接受 `exact`，用 `fuzzy` 报 400。

## 图片检索

向量检索同时支持文本和图片：传了 `image_url` 就走图片路径，无需额外声明。也可文本 query 命中图片行（图注直喂 embedding）。

| 维度 | 文本 query | 图片 query |
|------|-----------|-----------|
| 召回 | 向量 + BM25 双路融合 | 纯向量，不走 BM25 |
| 默认层级 | L2 为主 | 强制 `level=[2]` |
| 召回量 | `max(limit, 10)` | `max(limit*5, 50)` |

`image_url` 收三种形态，都是合法入参：

| 形态 | 适用场景 |
|------|----------|
| `data:image/<mime>;base64,<…>` | 原生形态：本地图片编码后直接给 |
| `http(s)://…` | 外部图片链接 |
| `viking://…` | 图已在库里的便利形态：服务端读文件字节转 base64 再进 embedding，结果与直接给 base64 一致 |

其他形态（如本地路径）400 拒绝。文本+图片联合查询 `query` 与 `image_url` 同传，embedding 输入拼成 `[text, image]` 两段。出参与文本检索完全一致（`result.resources[]`）。

实测（menus 手册树）：同一张图 viking:// 与 base64 两形态 top3 命中与分数完全一致；图查图 top1 相关度 0.82；文本查询命中图文混召，要纯文本腿用 `meta_filters category=text`。

## 命中体与父子链

见「调用链路 3」。补充两个边界：L0/L1 行没有 `matches`；图片行 `parent_chunk_id` 为空，`meta.image_uri` 指向图片本体、`meta.source_uri` 指向引用它的正文文件。

## HTTP 与 MCP 差异

能力完全对齐，只有命名与形态差异：

| 差异点 | HTTP | MCP |
|--------|------|-----|
| 评分阈值名 | `score_threshold` | `min_score` |
| peer 降权 | `other_peer_penalty` | `other_peer_penalties` |
| read 入参 | 单 `uri` | `uris` 数组，可多读 |
| grep 模式 | 字符串 | 数组并发多模式 |
| 返回形态 | JSON | 文本段落 |

## 选面

| 问题形态 | 用哪个 |
|----------|--------|
| 自然语言找内容 | `find` / `vector_search` |
| 要按分数截断 | `vector_search`（阈值真实生效） |
| 要会话意图或组装上下文 | `search` |
| 已知范围+关键词，要快 | `index_search` |
| 记得原文词面 | `grep` |
| 知道文件名片段 | `glob` |
| 先看看有什么 | `ls` / `tree` |
| 拿到 URI 之后 | `read` |

## MCP 接入

```json
{"mcpServers":{"openviking":{"type":"http","url":"http://<host>:1933/mcp","headers":{"Authorization":"Bearer <KEY>"}}}}
```
