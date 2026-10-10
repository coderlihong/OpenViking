# 企业知识库实施记录与当前使用方法

> 当前执行状态：I051（2026-10-05）实际持久D2提交成员共同发布及published/backoff SIGKILL恢复通过：单MD 3/3回执，目录3 URI/9回执含L0/L1，恢复未重复建代；完整7门禁仍pending、不标ready。修复已注册空间显式/自动祖先摘要边界。MinerU原始产物丢图已确认并补真实PDF页码/位置保全，客户端冻结上传副本并校验来源指纹；同XLS D1/W1复测各保留1 PNG，D1真实重建11条/0失败、图文向量回执齐全且图片URI可检索，W1四MD/三私有候选到draft_ready。模型图片语义与章节归属质量未验收。最终受影响164 PASS，初测1项兼容失败保留；服务/解析health200、active durable jobs=0。15 Feature/四FLOW仍PARTIAL、33任务32 PARTIAL+1 OPEN、0 DONE，完整验收NOT_RUN。 [本轮报告](evidence/implementation-I051-2026-10-05.json)。

> 历史执行状态：I034（2026-10-05）联合回归1575通过、1跳过（真实模型探针需单独启用，已另跑通过）；前端356项、完整类型检查和生产构建通过。所有15个Feature与四条完整FLOW仍NOT_RUN，完整仓库测试未执行。 [I034证据](evidence/implementation-I034-2026-10-05.json)。

> 历史记录对齐spec v0.10（2026-10-04）；最新代码证据I034（1575项局部联合回归通过，1项真实模型探针独立运行）；I030作为本轮历史基线。下文按时间保留历史记录，I001对应当时v0.6。当前剩余任务见[任务台账](openviking-enterprise-kb-backlog.md)，局部通过不等于完整验收。

## I001：PDF 章节解析与自动回退

已接入现有资源入库入口，不需要另开一套任务队列。默认旧行为保持兼容；本阶段须显式设置 `structure_policy="bookmarks_preferred"`。

流程：选用现有 PDF 转换器 → 读取 PDF 书签元数据 → 将书签标题与解析正文的合法标题/独立编号行做有序全局对齐 → 生成章节树 → 校验全文覆盖 → 暂存章节和图片 → 现有持久化/索引流程。

- 页码仅保存在元数据中，不用于匹配或切正文。新模式关闭旧的按页注入书签标题，避免用合成标题假装模型正文对齐成功。
- 对齐失败整本按 Markdown AST 标题切分，再按完整内容块规则分组；代码、表格、引用中的伪标题不当章节。任务 info 记录失败原因和实际策略，无人工等待。
- 父章节只保存自己的正文，子正文不重复；短章节保留，缺失正文的结构父节点可标 `structural_only`；真正歧义退回规则，不强行命中。
- 章节名消毒并防止文件覆盖；分支为 `index.md`、叶子为 `.md`，`toc.md` 链接真实路径。
- 图片复制到引用章节同级 `images/`，正文保留相对引用。缺图/无效图使解析失败并清理临时树；回退 info 不能掩盖真实失败。本阶段支持已下载的本地模型资产；产物协议已校验引用式图片，但章节重写/同级复制对引用式图片仍待完整适配验证。
- `_toc.json` / `_manifest.json` 保存实际结构、书签、匹配状态和 info，可读但不进入语义摘要、正文向量或 reindex 正文目标。
- `TaskRecord.info/alignment_status/structure_source` 持久化，任务列表/详情可见，Web 任务详情显示“解析说明”。对齐失败不会单独将任务标为 failed；最终状态仍由现有任务处理结果决定。

### Python SDK

使用本仓库更新后的 `openviking-sdk`。本地 PDF 自动上传，原文件名用于解析目录名；`parent` 表示父目录。

```python
from openviking_sdk import SyncHTTPClient

client = SyncHTTPClient(url="http://localhost:1933", api_key="<API_KEY>")
try:
    result = client.add_resource(
        "/path/维修手册.pdf",
        parent="viking://resources/raw",
        wait=False,
        options={
            "create_parent": True,
            "structure_policy": "bookmarks_preferred",
            "parse_backend": "local",
            "section_target_tokens": 4096,
        },
    )
    print(result)  # 获取现有资源任务返回值，随后查询 task_id
finally:
    client.close()
```

异步客户端 `AsyncHTTPClient.add_resource` 接受相同 options。目录落点为 `viking://resources/raw/维修手册`；同名覆盖/更新仍使用现有资源策略，独立 namespace 注册与代际发布不在此阶段完成。

### HTTP

当前可用入口：`POST /api/v1/resources`。下面的 `path` 是服务端可读取的路径；客户端本地文件先使用现有 `temp_upload` 入口，或直接用 SDK 上传。

```json
{
  "path": "/server/manual.pdf",
  "parent": "viking://resources/raw",
  "create_parent": true,
  "wait": false,
  "structure_policy": "bookmarks_preferred",
  "parse_backend": "local",
  "section_target_tokens": 4096
}
```

任务查询沿用 `GET /api/v1/tasks/{task_id}`。对齐失败示例字段：

```json
{
  "alignment_status": "failed",
  "structure_source": "markdown_headings",
  "info": [{
    "code": "BOOKMARK_ALIGNMENT_FAILED",
    "message": "书签对齐未成功，已按解析结果自动切分",
    "fallback_strategy": "markdown_headings"
  }]
}
```

以实际返回的 message/reason 为准。`alignment_status` 是结构质量说明，与任务 status 分开；本阶段不会产生 spec 完整新 job API 的 `written/ready` 状态契约。

参数也可放入现有 `args`，同一参数不能同时放顶层和 args。`section_target_tokens` 必须是 1–100000 的整数，且只在章节模式使用；这是**规则章节分组的估算预算**，不是 F4 child embedding 的 tokenizer 上限。章节模式不能与 `no_split` 组合，也不能传给非 PDF 文件。

`parse_backend` 当前接受 `local/mineru/auto/teleocr/logics`。三后端走统一 Docker 工件协议（`openviking/parse/model_backend.py` + worker，见 `deploy/parser-models/`）：流式上传、任务轮询、产物包下载，OpenViking 校验后端/版本/选项/页数覆盖/图片哈希后才入库；未注册 endpoint 时仍走 local 并拒绝 `model_mode/backend_options`。MinerU 走该协议的 worker 封装，权重改用 ModelScope 全参 safetensors（OpenDataLab/MinerU2.5-Pro-2605-1.2B，rev 7aa8e299），推理经 10.147.254.3 上常驻 `mineru-kit api-server`（vllm-async-engine、standard tier、`--allow-local-source`）完成，worker 仅作 HTTP 编排与工件校验。TeleOCR/Logics 已接入并完成远程部署验收（权重锁定 ModelScope 快照哈希）。**三后端远程 E2E 已通过**（12 页样例 12/12 页覆盖、页数/块引用/资产哈希校验全过；证据见 `evidence/deployment-I002-2026-10-03.json`）；三模型在全量 114 TC 上的质量验收与全卷 1072 页手册跑批仍未执行，spec §9 的 `/kb` 企业接口、解析能力注册仍待实现。

## 达成情况

| 子项 | 本次实际交付 | 已运行证据 | 仍未达成 |
|---|---|---|---|
| F1.6 | HTTP + async/sync SDK 新参数（`parse_backend/model_mode/backend_options`）；三后端统一工件协议接入与选项校验；非法/重复参数拒绝 | 113 项 enterprise_kb/parse/service 测试 PASS；三后端远程 Docker 部署 E2E PASS（TeleOCR/Logics/MinerU 各 12/12 页覆盖，`evidence/deployment-I002-2026-10-03.json`） | 三模型 114 TC 质量验收、错误码统一复核 |
| F2.1 | 新模式 PyMuPDF 读取完整层级；旧路径修复命名目标 bytes 误作页码 | 合成真实 PDF、对象/命名/动作目标测试 | 完整手册独立金标、多样真实 PDF |
| F2.2 | 标题归一化、顺序约束、全局歧义检测和结构空父节点 | U 测试 | 原目录识别、OCR 更复杂差异、三模型正文对齐质量 |
| F2.3/2.4/2.6 | 无损章节规划、TOC/manifest、标题→内容块自动回退 | U 测试 + local 实际解析/存储集成 | 生产服务完整入库 E2E |
| F2.5/2.7 | 本地图片同级存储、缺图失败、临时树清理、控制文件不入索引 | 模型输出模拟测试 + 实际文件系统 | 真实后端资产包、引用式图片、多来源授权 |
| F3.1/3.4 | 新增结构说明的持久化与前端展示 | 持久任务存储重载、Web 测试 | 多 worker 串行租约、恢复/取消、发布屏障完整验收 |
| F4/F5/F7/F8/F9/F10/F11/F13/F14 | I001 阶段未完成这些目标；I004 文本分块进展见下文 | 不将现有基建当通过 | 父子 chunk、图文联合 embedding、混合检索、模糊 meta、Office、原 URI、Wiki、统一 IndexPlan/隔离/发布 |
| F6.4 | THINKING 全局 L2 召回不再依赖 ACL 开关，仍使用原 tenant/scope 过滤 | 37 项检索相关回归 PASS，含导航为空、rerank 不可用、ACL on/off | 真实授权边界与 BM25/RRF/MMR 完整检索验收 |
| F15 | 小型合成 PDF、章节反例与可运行回归测试 | 当前代码运行报告 | 真实手册金标与模型/检索评测集 |

目标 Feature 的完整验收仍 NOT_RUN；上述是部分实现及对应测试的 PASS，不提升整个 Feature 为 PASS。

## 运行边界与后续实施顺序

本次真实存储集成使用本地 AGFS 测试实现，直接执行资源 ingestion，未运行真实生产 AddResource worker。队列字段测试验证序列化保留，不证明多 worker 串行互斥。图片测试中的外部模型响应被模拟；本地 pdfplumber 转换使用真实自建 PDF。

I001 历史运行时 WeWork 路径无法读取；I003 已通过 Finder 获取授权临时测试副本，重新核对 1072 页/491 书签及完整 SHA，并上传一致的远端输入。三个模型服务已运行且健康，I002 各 12 页实际产物联调通过；I003 的 MinerU HTTP→PDFParser→章节目录磁盘写入再次通过。该样例无书签，触发 markdown_headings，不能证明完整手册书签对齐。

1072 页 MinerU 完整解析已完成，产物声明 1072/1072 页覆盖，8,366 个内容块、2,288 个图片资产，366.56 秒；产物校验通过，见 I003 证据。完整手册尚未取得章节/正文识别质量与检索验收结论；无须再次处理原路径权限。原件仍只作为有界评测临时输入保留，非永久知识资产。

一次扩展回归包含的既有 `tests/service/test_reindex_file_lock.py` 因缺少 `agfs_client/test_dir` fixture 无法初始化；单列该限制，不把它写作 PASS。其余本次选定后端回归通过。

后续按 spec M0–M2：完成解析后端契约与实际部署、namespace/发布可靠性 → 共享父子 chunk IndexPlan（首次入库与 reindex 复用）→ 图片上下文联合 embedding → 混合召回/URI 引用；之后 Office 与 Wiki。章节规则分组不能代替父子 chunk，章节落盘也不能代替企业生产验收。


## I003 本轮修订与真实验证

- 实现模型取消传播、原子完成发布，取消后迟到结果不得写 completed；队列取消清理输入，SQLite 连接按操作关闭。
- 产物图片校验按 Markdown AST 识别真实 HTML，代码围栏里的 img 示例不当缺图；拒绝 ZIP 中非普通文件。
- 配置默认 PDF 模型时不被 UnderstandingAPI 绕过；模型选项是 `model_mode`，与旧 `args.parse_mode` 分开。
- [I003 证据](evidence/implementation-I003-2026-10-04.json)记录实际运行和边界；服务健康、12 页模型链路通过均不代表 F4 父子 embedding、F5 图文联合向量或生产 ready 已完成。

I003 另修复 THINKING 在 ACL 关闭时缺少全局叶子兜底的逻辑，保持同一授权代理和过滤范围。该修复只推进 F6.4；BM25/RRF/MMR、父子 chunk 候选聚合及完整检索仍待实现。

完整手册的可重复产物验证脚本为 `deploy/parser-models/evaluate_manual.py`（在 mounted `/models` 的 MinerU worker 内运行，读取 secret 文件，不输出凭据）。脚本写入 `state/manual-full-evaluation.json`，明确区分 RUNNING/PASS/FAIL；PASS 仅限完整页数/产物协议校验，不能代替人工独立 gold、章节质量或检索验收。


完整产物章节回放也已完成：读取 491 条书签后，正文候选出现 `heading_order_conflict`，按契约自动以 Markdown 标题切分；`alignment_status=failed`、info 为“书签对齐未成功”。磁盘测试存储写入 967 个章节、2,612 个文件，其中 1,642 个引用图片副本。2,288 是模型资产包中的图片数，1,642 是实际章节图片写入数，两者口径不同，不据此宣称逐图质量全通过。该验证没有生产入库/embedding；不能将回退结构当成书签对齐成功。后续需独立核验模型内容顺序与书签候选来改善质量，同时保持自动回退。

F15 已从全卷真实章节生成 50 条候选（见 evaluation/），保留内容/工件 hash 和支持摘录。它们仅是待复核的标题查询起点，未冻结 gold、未验证生产 URI、未跑检索，需继续增加真实问法和困难反例。

## I004：共享文本父子索引与四个主入口（2026-10-04）

规范 v0.8 将 D1 上传文件+URI 前缀、D2 指定 URI 索引、W1 上传文件+collection、W2 指定 URI+collection 定为四个主入口，并增加 FLOW-* 验收。collection 绑定 schema 版本、目标 Wiki namespace/URI、来源目录和授权；`/kb` Wiki/collection 接口及统一 WikiBuildPlan 仍是待实现契约。

### 已实现的文本部分

- `openviking/indexing/plan.py` 用锁定 SHA 的模型 tokenizer 构建完整父子计划，AST 分块、标题祖先上下文、token 预算与重叠，内部覆盖检查确保首尾正文不丢失；parent 保存较大上下文，child 用于 embedding。
- 初次入库 `vectorize_file` 与 URI reindex 的文本文件分支共用 `enqueue_text_plan`，计划全部校验后才排队；复用现有 embedding worker 和任务计数，不新增第二套消费队列。现有 reindex 计数按来源文件统计，不把它宣传为 child 进度。
- 子块记录使用独立 ID，writer 存储 chunk 正文、父上下文、revision/plan/space 元数据，避免同 URI 子块互相覆盖；内容字段使用子块文本供后续词法索引读取。模型 failover 不允许切换声明不同的模型空间。
- QUICK/THINKING 候选保留 child 身份，再按原 URI 聚合 `matches[]`；各命中保留相关片段和 parent 文本，外部 URI 不添加 `#chunk`。URI/level 清理分页删除所有子块，拒绝部分删除假成功；复制/移动向量保留 child 独立身份。

### 配置与迁移边界

`embedding.chunking.enabled` 默认 false，保持旧配置兼容。测试环境启用须提供**该 embedding 模型实际 tokenizer.json 路径与 SHA256**，并使用 `text_source=content_only`；默认 child target/max=512/768、parent max=2048、overlap=64、special reserve=32。预算校验失败或 tokenizer hash 不符直接拒绝，不静默截断。新配置会改变 collection 模型元数据指纹；远程旧 schema 缺少 chunk 字段时拒绝启用，须新建测试 collection 或完成字段/索引迁移。这里是当前底层配置用法，不能写成尚未存在的 `client.kb` 可运行示例。

**不能直接按本轮部分实现宣布线上 ready。** 原子代次切换、旧 revision/legacy 向量 GC、删除/复制后的全量一致性、namespace 安全注册与发布屏障仍待实现/验收。复制后的 document/plan 身份重建仍有迁移缺口。长代码/表格目前连续片段可保留全部正文，但超长表格尚未实现目标的按行组重复表头，质量验收待补。图片上下文与联合 VL embedding、真正 BM25/RRF/MMR、四入口完整 HTTP/SDK E2E、Wiki 构建尚未通过。新文本开关仅用于隔离测试 collection；不是生产功能默认上线。

### 运行证据

96 项定向测试通过（`tests-I004-index.xml`）：分块稳定 ID、首尾覆盖、token 边界、标题祖先/围栏伪标题、Unicode/长块、真实 embedding 消费端的多子块写入（fixture embedder/内存 DB）、同 URI QUICK/THINKING 多命中、分页/部分删除及 collection 回归。fixture 向量测试不证明真实生产 Viking 数据库与检索。

另外从 I003 的完整 MinerU 手册输出选择最大 Markdown（91,298 字符），使用部署中的 Qwen tokenizer：构建 98 child / 25 parent，source coverage 完整，最大 child 550 tokens（另预留 32）。抽取头/中/尾三个 child 调用远端 Qwen embedding 服务，实际返回三个有限值的 2560 维向量。此证据仅证明模型输入及 embedding 输出；来源 URI 是评测计划地址，未在生产 Viking 持久化，也没有完成检索命中验收。原始 tokenizer SHA、模型名和源内容 SHA 见 I004 JSON。

## I005：表格行组、共享调用链与重建标签（2026-10-04）

在 I004 文本实现上补齐以下部分，chunker 升级为 `markdown-parents-v2`：

- 超长 Markdown 表格按完整行分组，每组 embedding 输入重复原表头，owned source 正文只出现一次。单行超过预算时保留表头、按 Unicode 连续分片并标 continuation，禁止丢弃尾部。表头本身过大无法装入预算时，计划明确失败，排队前拒绝，不静默截断或宣称索引就绪。
- 实际 `vectorize_file` 与 `_reindex_resource_vectors_from_entries` 调用链测试确认，同一 URI/内容/配置输出一致 child ID、parent ID、plan ID 和模型输入；读取既有正文，不调用 OCR。文件系统/队列由测试替身提供，这不是四主入口完整 HTTP/SDK E2E。
- URI reindex 将已有 source vector 的 search_tags 显式带入每个新 child；append 先合并旧标签，replace（包括空列表）尊重显式替换。新 chunk ID 不能依赖 writer 在旧 ID 上自动继承标签。尚未有文档级权威元数据 manifest；存在旧代标签不一致时仍需后续发布/registry 解决，本次不宣称所有业务 meta 已支持。
- collection metadata 指纹增加 chunker_version，规则更新也触发迁移校验。禁止通过 metadata override 作为正常生产迁移方式，完整新代构建/原子切换仍待实现。

当前回归 125 项 PASS，覆盖索引计划、实际共享调用链、标签传播、检索多命中、collection 元数据、迁移/语义选项/reindex 占位语义。报告 `evidence/tests-I005-combined.xml`。

另外对完整 MinerU 手册回退输出的**全部 968 份 Markdown**执行真实 Qwen tokenizer 分块计划回放：968/968 通过，无正文丢失或预算超限，生成 1884 child / 1066 parent；最大 child 561 tokens（另预留 32），最大 parent 1965 tokens，15.517 秒。该数字含目录 Markdown；967 为 I003 的章节数，不可混为同一计数。源 URI 仍是评测计划地址，未进行全量真实 embedding 写入或生产检索。原书签对齐的 heading_order_conflict 未因分块通过而得到修复，章节质量仍待独立 gold。

远端 TeleOCR、Logics、MinerU `/health` 本轮再次均返回 ready_for_jobs/model_verified=true；这是健康检查，不替代三模型全卷质量验收。I004 的真实三个 embedding 结果保持历史证据，本轮全卷验证仅执行 tokenizer/计划。

完整 Feature 和 FLOW-D1/D2/W1/W2 仍未验收；下一个关键依赖是发布屏障与旧代清理，再继续图文/混合检索和 Wiki 构建。所有历史报告 hash 仅代表当时文件，不覆盖当前代码。

## I006：文本向量代次发布与查询快照基础（2026-10-04）

本轮接入 `openviking/indexing/publication.py`，使用实际 SQLite 事务记录 expected child、写入 receipt、生产者 seal、desired attempt、不可变 namespace 快照、活动头及查询租约。`enqueue_text_plan` 建立新代次，全部消息排队成功才 seal；embedding worker 每个 child 确认向量 upsert 返回 ID 后 ack。unique receipt 防止重复消息提前满足计数。任一终止失败/取消阻止发布，旧 active 文本向量继续可见。较早尝试不能覆盖较晚尝试；同 account/namespace 两文档并发发布会合并最新映射，不覆盖未修改文档。

物理 vector ID 加入 `index_generation`，同一内容再次构建也不会在发布前覆盖旧 active row；对外来源 URI 和稳定 chunk ID 不变。公共 find/search 捕获查询视图，后续向量路和结果组装使用同一 manifest；租约在查询中续期、返回前复核，结束时释放。视图 TTL 当前 120 秒，过期未能续期时明确失败需重试；尚未有独立长查询心跳。后台 maintenance query/filter 可看到 staged/旧代记录以便恢复和清理。

公开 vector search 和无 query 的 tenant lookup 在排名/limit 前附加 URI+active generation 条件，拒绝 staged、旧代、同 URI legacy row 与复制后未注册的新地址；保留原 tenant/scope/ACL 过滤。查询 wrapper 的只读配置探测不会初始化 singleton，避免影响既有 live rerank 设置。独立底层调用只保证单次操作视图；完整多路查询需经过已接入的公开 find/search wrapper。

启用隔离测试模式需在 `embedding.chunking` 添加 `publication_path`，指向所有 producer/consumer/reader 共用的持久 SQLite 文件；空值保持旧配置兼容。只验证了同主机 SQLite 基础，不支持把各 worker 私有文件当共享注册表，也未验收跨主机/NFS部署。新 schema 需有 index_generation 字段及 scalar index；启用状态纳入 collection metadata 兼容指纹，必须走新 collection/明确迁移。该配置不是企业 API 的正式部署说明；不能默认打开并接管现有生产 collection。

### 本轮运行证据与未完成项

131 项定向回归 PASS，新增十项 publication 用例验证实际 SQLite 持久状态与重启可读、seal/expected 计数、重复 ack、失败/较早尝试 fencing、并发保留其他文档、account 隔离、读租约保护、真实 TextEmbeddingHandler ack/取消/false-write、生产者排队中途失败、跨切代固定视图，以及实际 adapter 编译谓词在 top-k 前排除 staged。模型和 vector DB 使用 fixture，不是远端全量向量或生产索引验收。报告：`evidence/tests-I006-publication.xml`。

GC 当前仅提供 `retired_candidates` 只读计划：active snapshot、有效查询租约保护的代次不进入候选，保留期可配。**尚不执行物理 GC**，因为完整 spec 还要求任务/Wiki、正文版本、图片、目录语义及共享依赖保护，并在删除前再校验。SQL 未发布 orphan/building 的恢复、租约过期后任务重排、registry 丢失恢复/备份、规模性能也待完成。

该实现只切换**文本文件向量**。公开正文仍由原解析/文件写入路径处理，尚未做到正文+L0/L1+图片+BM25+meta 的统一快照投影；旧索引结果对应的公开 URI 可能已被修改，需继续内容版本发布，不能宣称 §2.3/ready 屏障整体达成。当前 namespace 缺少业务授权注册时内部使用 resources 槽，不能将其当作已实现事业部 namespace 隔离。任务 ready/UI 与 publication manifest 的完整联动、URI 删除/移动/复制的 manifest 重映射仍待实现。F13、TC-4.5 和四入口完整 FLOW 继续 NOT_RUN。

## I007：派生正文版本与文本向量共同发布（2026-10-04）

在 I006 基础上补充 `openviking/indexing/content.py`。正文版本资产存放在 account 的 AGFS 私有 `_system/index_blobs/<SHA>.bin`，沿用现有 AGFS 栈及账号路径，SQL/队列只携带 SHA、大小、内部写入 epoch 和规范文本摘要，不复制全文进 SQL。这是解析后的/已有文本的版本，不是原始上传 PDF；仍不永久保留原件。版本资产没有面向普通用户的公开 URI，来源引用不改变。

`vectorize_file` 在正文发布模式下授权读取工作正文，保存并读回校验不可变版本，再将其与分块正文 hash 绑定。producer begin 固定 source reference；publish 事务一次更新正文 pointer 与该 URI 的 generation，其他文档映射继续继承。公共 `read/read_file/read_file_bytes` 原 URI 读取按 active/query snapshot 投影，保留行/字节切片语义。新向量未完整写入或失败时读到旧正文；成功后读到新正文。读取前执行当前来源权限检查，来源已删除/变成目录则不通过保留版本复活，版本缺失或 hash/size 校验失败不回落到工作正文冒充一致结果。

内部 `draft_sources` 仅供构建任务读取工作文件。初次入库/reindex 的文本 vectorize 分支明确读取工作正文，SemanticProcessor 的队列任务同样读工作数据，避免把已发布旧正文再次索引。普通客户端的 `raw` 读取参数不等于内部 draft 模式。公开 URI/stat/list/目录语义的完整一致投影尚未全部实现。

### 来源修改检测与运行边界

标准 VikingFS `write/write_file/write_file_bytes/append_file` 在底层写入前推进已跟踪来源的 draft epoch。archive 捕获 epoch，producer begin 校验其未变，发布事务再校验 epoch/源 hash；索引过程中编辑会使旧计划失败，保留旧 active 正文与向量，下次 reindex 读取最终工作内容。写入失败也可能保守地使等待计划失效，不能把失败写入当成成功发布。通过底层 AGFS、目录同步/mv、外部 WebDAV 等绕过这些写入钩子的变化，尚未完成统一 CAS/事件整合；不能宣称所有修改入口已覆盖。

`protected_content_revisions` 提供只读派生正文保护根：active、有效查询 snapshot 和 building/queued 的来源版本都被保护，共享 SHA 在新旧代之间正确保留。仍未执行物理 GC；Wiki、图片、有效重试和完整生命周期根还需进入统一依赖登记，不能仅根据当前正文根删除文件。

启用隔离测试模式：在 I006 已配置的 `embedding.chunking.publication_path` 基础上增加 `publish_content=true`（默认 false，要求前者存在）。新配置参与 collection metadata 兼容校验，需新建测试 collection 或明确迁移/重建；老发布记录没有正文 reference 时，正文投影明确报错要求重建，不静默读工作正文。直接调用底层 enqueue 必须提供已验证、且与 IndexPlan 对应的 source reference；无 reference、错误 hash 和无子块的空正文不发布。现阶段空文件需明确 clear/delete 生命周期处理，自动空内容代次尚未实现。

### 实际验证

220 项定向回归 PASS：七项新增正文测试使用真实磁盘与 SQLite、实际 VikingFS 读取方法和 TextEmbeddingHandler；AGFS 传输与 embedding/vector DB 是测试替身。覆盖待发布/部分写入/失败后原 URI 保持旧正文、发布后切换、文本行范围/字节范围、查询组装期间切代仍读同一版本、当前撤权/删除、私有正文损坏、不在 SQL/队列存全文、工作正文编辑后发布失败与最终内容 reindex、入队前修改检测、active/read/pending 正文 GC 保护。权限撤销使用控制测试替身，未证明生产 ACL 或加密有效性；本轮没有远端生产部署或完整手册向量检索。相关 SemanticProcessor 回归一并通过。报告：`evidence/tests-I007-content.xml`。

此模式实现的是**单个文本文件的正文 pointer＋文本向量共同发布**。PDF 多章节根的整本发布屏障、L0/L1、图片、BM25、完整 meta 与任务 ready/UI 仍需共同发布；copy/move/delete 的 manifest 更新、统一修改入口 CAS、跨主机持久化/故障恢复和物理 GC 尚未验收。F13、§2.3/ready、四入口完整 FLOW 的目标保持不变，整体 NOT_RUN。I006 的“正文未纳入”是历史阶段描述，以 I007 当前范围为准。


## I008：多文本章节共同发布（2026-10-04）

新增共享 `text_publication_batch` 编排与 SQL batches/batch_members。producer 在第一份文本计划时建立持久批次，后续并行子任务加入同一 account/namespace 的批次；单文件 seal 仅关闭该文件 producer，整个遍历成功后才 batch seal。消费端使用持久成员与 receipt，不依赖 producer 进程存活。最终事务重新核对每个成员的唯一 child 集合、desired attempt 和来源 epoch，合并最新 head 后一次切换所有成员，保留无关文档。

D2 `_reindex_resource_vectors_from_entries`、D1 `_vectorize_resource_files` 和 SemanticProcessor 的递归任务共用此屏障。producer 正常返回（含 reindex 已捕获并计入 failed_records 的错误、ProcessResult.failed/requeued）即封口批次；producer 异常/取消才整批失败。批次封口时逐个成员复核：失败或被取代的成员从本次发布中剔除并保留其旧版本，其余健康成员照常发布；仅当批次已无可发布成员时才整批失败。终止 embedding 错误不再连坐同批尚未发布成员，重排由新的 producer 批次重新构建；没有自动 orphan 复活。

共享 SemanticNodeScheduler 的 worker 会服务多份文档，不能依赖创建 worker 时继承的 ContextVar。SemanticDagExecutor 在创建时保存自己的批次，并在每次节点执行时重新绑定；同时显式读取工作正文，避免拿到已发布旧正文。空批次不切换 head，重复 URI 或跨 account/namespace/registry 加入被拒绝。每个 child 使用原 generation 防止物理向量覆盖旧代。

### 验证与边界

326 项定向回归 PASS，其中新增 12 项覆盖早完成章节等候全批、失败/较新任务/来源编辑导致整批保留旧版、持久状态重开、重复与隔离拒绝、实际 TextEmbeddingHandler 在 producer 完成前全部写完仍不可发布、counter/retry/cancel 失败、共享 worker 的批次绑定，以及实际 URI reindex 两份文本的正文/向量共同切换与坏表格失败。报告 `evidence/tests-I008-batch.xml`。SQL 和磁盘正文实现真实运行，AGFS transport、embedding/vector DB 为 fixture；不是生产 ACL、远端部署或完整手册检索验收。

仍仅为**同一 producer 调用中实际计划的文本集合**：未将目录 L0/L1、目录布局/list/stat、图片、BM25、完整 metadata、删除与任务 ready 放入同一批次，也未提供完整版本回收/跨主机注册表或崩溃恢复。新增来源的工作正文在首个 active 之前仍可读取，不能称完整根快照已实现。更早完成的 embedding 请求统计也不等于整个 job ready；任务 UI 联动待完成。W1/W2 尚未实现。四条完整 FLOW、TC-4.5/F13/整本发布验收仍 NOT_RUN。I007 单文本范围描述是历史阶段，以此处当前文本集合范围为准。


## I009：目录语义向量加入共同发布（2026-10-04）

新增 `indexing/semantic.py`，将一个资源目录的 L0/L1 消息作为完整语义计划注册。版本 hash 来自模型配置、URI/account/namespace 与语义正文，SQL expected 使用 semantic:0/semantic:1；这些不是 L2 child chunk，也没有把目录摘要伪装成 chunk。shared TextEmbeddingHandler 使用统一 publication_member_id 校验/ack，物理 vector ID 对非 chunk 的受管记录同样加入 generation。目录的新写入不会覆盖旧 active L0/L1，同一 manifest/查询 prefilter 对目录和文件通用。

`vectorize_directory_meta` 保留旧配置的语义层/标签/scalar/timestamp 与队列契约；显式 publication 模式下改为注册完整目录 generation。只改 L0 或 L1 时，读取另一已有 sidecar 重建该层，防止完整目录切代后误丢另一层。memory 的既有专用维护路径保持原模式。URI reindex 的目录循环改为同一共享生产者，与文件 chunk 共用批次；单 marker 重建也调用同一目录计划。空语义的 marker clear 明确拒绝，待统一删除事务实现，避免破坏 active 版本。

启用时 Semantic DAG 的目录生成或 sidecar 写入异常会向上失败，不再静默跳过后让文本批次发布；reindex 检查 SemanticProcessor 的 ProcessResult，FAILED/REQUEUED 不再继续向量阶段。异步 consumer 仍以真正 upsert 返回 ID 为 receipt，部分 L0 写入、L1 取消不会发布目录或关联批次。

### 本轮验证

404 项定向回归 PASS，新增 10 项验证真实 directory producer/consumer、目录先完成仍等待文本、L0/L1 各自有独立物理 ID 且不覆盖旧版、部分 receipt/取消保留旧目录、部分层更新保留另一已有层、实际 URI reindex 目录+文件同批、语义阶段失败/重排中止、受管 DAG 不吞目录错误、marker 重建不绕过 generation、非法层身份拒绝。SQLite 实现实际运行；AGFS transport、模型与 vector DB 为 fixture。报告 `evidence/tests-I009-semantic.xml`。

扩展回归发现既有文件锁测试依赖其他目录的缺失 fixture，且其 FakeVikingFS 未跟随现有异步 pathlock API。已将此测试改为独立临时文件和受控 pathlock 替身，明确断言文件 URI 使用 exact 锁、未使用 tree 锁并释放 lease；不声称验证了 AGFS 服务本身。初始失败报告保留供审计，最终完整本轮回归已通过。

### 未完成边界

目录向量记录现在有共同发布屏障，但 `.abstract.md/.overview.md` 的公开文件内容尚未按 manifest 投影，也没有与文本 epoch 等价的完整 sidecar 修改检测。直接目录/sidecar读取可能先看到工作语义版本；必须继续版本资产/引用/CAS 才能通过整本一致可读验收。目录 layout/list/stat、图片、BM25、权威 meta、任务 ready、删除/移动/复制、崩溃恢复与物理 GC 仍未完成；四个完整 FLOW 和 TC-4.5/F13 继续 NOT_RUN。I008 的目录向量未纳入是历史状态，以 I009 当前范围为准。没有新增默认生产开关或本轮远端部署声明。


## I010：目录摘要内容快照与修改检测（2026-10-04）

扩展 PublicationStore 的 semantic_sources 与目录 manifest 的 semantic_contents。目录 producer 归档每个实际 sidecar，验证归档 raw 解析出的 body 与向量输入一致，再以 semantic:0/semantic:1 expected 成员、对应 sidecar epoch/sha 注册。发布事务再次逐层核对修改 epoch/sha，与文本正文引用和各层向量 generation 一起切换。SQL 只存版本 reference；含 metadata 的完整文件保存在私有 AGFS blob，与 I007 资产共享去重，并非永久保存原件 PDF。

原 sidecar URI 的 read/read_file/read_file_bytes 沿用内容投影。abstract/overview（包含批量目录摘要读取）在当前来源访问检查后按同一 active/query reference 读取、校验 raw 大小与 SHA，然后按原格式返回 visible body。部分 receipt、失败或取消时读取旧版；新层尚未发布时不能绕过旧 manifest 读工作文件。没有 immutable sidecar 的旧发布记录明确要求重建；私有版本损坏不回退到工作正文。父 URI/sidecar 当前访问与存在性仍检查，不通过历史版本复活已删除文件。

URI reindex 的 shared entries、单 marker 重建和直接 index_resource 明确使用 draft_sources。直接目录索引也加入统一批次，让目录与其文本成员等待共同发布。内部 sidecar metadata/freshness 读写使用工作版本，避免用公开旧快照覆盖未发布 metadata。标准 VikingFS 写入钩子复用 I007 epoch；begin 前与发布前两个阶段检测修改，失败后重新索引读取最终工作文件。

### 运行证据与当前边界

420 项定向回归 PASS，新增 12 项覆盖侧文件和导航接口旧/新投影、两层 receipt、跨切代查询保持旧语义、构建中编辑及最终内容重建、begin 前编辑拒绝、向量输入与归档 body 不一致拒绝、版本损坏不回退、active/read/pending 侧文件保护根、实际 URI reindex 读取最新工作摘要、直接目录索引目录+文本共同发布、metadata 写回读取最新工作版本、未发布新层不泄漏、旧目录无 reference 的迁移门禁。SQL、磁盘内容及实际 VikingFS 方法被执行；AGFS transport、embedder/vector DB 为 fixture。报告 `evidence/tests-I010-semantic-content.xml`。

GC 仅增加可达性根，不物理删除。目录根 layout/list/stat 和初次工作文件未发布的可见性仍需根 manifest；图片、BM25、完整 metadata、job ready/UI、Wiki、删除/移动/清除、任务恢复/跨主机持久化仍未完成。标准写入以外直接 AGFS/WebDAV/同步/mv 的统一修改 CAS 尚未覆盖，当前 sidecar epoch 不能被称为全入口修改检测。I009 的 sidecar 未纳入是历史状态，以 I010 本文为准；完整 FLOW/TC-4.5/F13 继续 NOT_RUN。本轮没有远端生产部署或全卷检索声明。


## I011：图片引用上下文与联合输入保护（2026-10-04）

已实现 `indexing/image_context.py`：读取授权章节 Markdown，识别真实 AST/HTML 图片引用、跳过代码围栏，提取标题/图注/附近正文及引用 hash。同图不同引用分别生成 VLM 摘要；不可据邻近正文凭空断言机型或配置。企业模式摘要失败向 DAG 传播；向量 producer/worker 都拒绝缺图、空摘要或文本服务降级，内部保护字段不写入数据库、重试消息保留保护。资源 URI 图片重建复用初次入库 vectorize_file 联合输入。

438项定向回归PASS（I010的420项+11项新图片测试+7项已有媒体测试），报告 `evidence/tests-I011-regression.xml`；另185项受影响子集PASS。测试覆盖 AST/HTML/重复引用/路径编码/外链忽略、上下文预算、授权失败/引用超限、VLM逐引用及严格失败、实际producer/worker联合输入与URI重建分支。VLM/embedder/vector DB/AGFS transport仍为fixture，真实服务请求单独记录，不混算完整E2E通过。

远端 `local/wemm-embedding-4b-v1` 在8026接受真实手册图片95628字节和文本联合messages，返回2560维有限向量，记录 `evidence/image-joint-I011-2026-10-04.json`。未进行真实VLM摘要质量、图片改变对向量的影响、检索质量或全卷图片索引验证。三解析容器存在不是重新验证完整健康及全卷结果。

限制：只扫描当前章节目录、16引用硬上限、VLM token估算；权威meta/引用缓存/分页未完成。引用摘要尚未持久化为独立索引行，仍汇总到既有图片行；图片共同发布/版本快照与多空间隔离、BM25/MMR/模糊meta性能、job ready/恢复/生命周期、Office转换、Wiki collection/schema APIs均仍待完成。D1/D2部分实现；W1/W2拟实现；四完整FLOW、TC-4.5/F13/F5不提升为PASS。新增配置改变计划指纹，需要新代重建。


## I012：图片资产与向量共同发布（2026-10-04）

新增indexing/image.py，通过现有chunk member、generation fencing、SQL batch与consumer receipt接入图片。工作原图由archive_source授权归档，模型数据URI使用同次raw bytes；原图reference与向量统一发布。每次尝试不同物理ID，不提前覆盖active图片行；摘要作为对应row内容。content投影/查询lease/GC保护复用现有机制，公开来源仍为图片Viking URI。配置指纹加入image_index_version以触发企业collection迁移门禁。

446项定向回归PASS（I011的438项+8项图片发布用例）；测试真实SQLite、磁盘资产、VikingFS读取及queue consumer，AGFS transport/embedding/vector DB为fixture。新增覆盖原图+向量切代、文本+L0/L1+图片整批receipt、图片编辑/worker失败/被新任务替代、持有旧query/GC保护、资产损坏无fallback、enqueue拒绝。报告evidence/tests-I012-regression.xml。

当前为每图片文件一个索引member，未逐引用独立持久化，未完成上下文改动依赖/缓存、多空间查询/融合、BM25共同发布、初次根可见性、生命周期恢复/job ready/Wiki等。原图历史属解析知识资产，不保留原始PDF；物理GC仍未执行。本轮未远端部署新代码或声明真实检索通过，四完整FLOW/F5/F13仍待验收。


## I013：真实OpenViking图文适配器联调（2026-10-04）

修复原始HTTP探测与实际adapter协议不同的缺口。OpenAIDenseEmbedder新增wire_format=messages，同步/异步经现有SDK客户端POST /embeddings，query也包装user/content；保留重试/并发/连接池/telemetry，默认float，base64手动解码为float32。配置工厂传递协议并拒绝非兼容provider；额外body不得覆盖身份/联合输入，批次不得静默拼接。collection metadata/text space指纹绑定协议，新旧索引需迁移重建。

此服务的配置片段（不是已部署生产配置）：
```json
{"embedding":{"dense":{"provider":"openai","model":"local/wemm-embedding-4b-v1","api_base":"http://10.147.254.3:8026/v1","dimension":2560,"input":"multimodal","wire_format":"messages","encoding_format":"float"}}}
```
实际本轮经临时SSH回环转发访问，验证后转发已关闭；生产网络/认证配置需按部署环境。不能据示例认定与其他文本模型同维度就属于同一空间。

472项回归PASS，新增9项真实SDK+mock transport协议测试，覆盖sync/async、联合输入、query/document参数、base64、body覆盖拒绝、factory和批次拒绝。真实远端配置factory+async adapter也分别获得joint/query的2560维有限向量，记录evidence/image-adapter-I013-2026-10-04.json。不是实际worker/发布/检索或VLM质量验收。

初始471PASS/1FAIL：连接复用测试被本机macOS代理转发，代理响应Connection: close；实际客户端保持同一实例。只修fixture的loopback NO_PROXY，不改变生产代理策略或削弱断言，保留tests-I013-regression-initial.xml；最终报告tests-I013-regression.xml为472PASS。其余I012缺口不变，四完整FLOW仍NOT_RUN。


## I014：按图片引用建立独立向量（2026-10-04）

shared image plan从一图一row扩为一图多occurrence row，各摘要+原图单独embedding，context URI/hash/ordinal、parent上下文与独立chunk_id写入现有索引。共用图片generation/asset reference及文档批次；单引用失败保留整批旧代，所有receipt齐全才发布。结果聚合仍为图片URI，matches保留每引用和context URI。原图在VLM完成后变化会因asset SHA不一致在enqueue前拒绝。

DAG在企业图片语义任务中重新提取上下文，内部ProcessResult把image_summaries传给URI语义重建的向量阶段，避免只读取overview合并摘要。旧文本与legacy路径保留。image-occurrences-v2和新增schema字段要求企业collection迁移；新版图片vectors_only没有引用缓存时显式SEMANTICS_REQUIRED并保留旧索引，待补持久缓存后恢复该模式，不偷偷重复调用VLM或合并多个引用。

478项回归PASS，新增6项覆盖distinct vectors/receipt/聚合matches、stale image SHA、单occurrence worker失败、URI相同plan/语义结果交接、vectors_only保护、actual DAG增量图片路径重新取摘要。SQL、磁盘、producer/consumer和转换器实际执行；模型/vector DB/AGFS transport为fixture，语义阶段完整E2E未验证。本轮未远端部署或真实质量测试。

缺口：VLM中的正文改动与发布依赖、增量事件自动传播到引用图片、持久缓存、跨ACL来源交集/撤权前置过滤、16引用分页及跨目录发现、多空间和混合检索/生命周期/ready/Wiki。引用row已持久到向量库，但不等于完整F5验收通过。原始PDF仍不保留，解析图片及其发布版本属于知识资产。


## I015：私有图片引用缓存与vectors_only复用（2026-10-04）

新增image_cache.py，将语义payload存account AGFS私有内容寻址blob，随source reference共同发布，不把摘要/正文写进SQLite。cache signature绑定VLM配置、实际活跃prompt renderer和context规则；图片、引用列表/上下文/epoch复核通过时，URI vectors_only复用occurrences走shared image plan，维持稳定成员ID。payload经大小/SHA检验，来源当前ACL读取失败、模型/上下文/图片改变、缓存损坏均拒绝，不降级合并摘要。旧版无缓存需semantic_and_vectors建立缓存。

collect_image_contexts捕获各来源标准写入epoch；registry.begin及publish检查对应epoch，缓存/图片generation共用失败屏障。私有缓存加入active/reader/pending GC roots。生成时捕获的model/prompt签名在cache归档前检查，避免配置变化后错误标记缓存为新模型结果。cache恢复再次绑定原图SHA，load之后变图也拒绝。原始PDF不保留；缓存属于派生知识资产。

488项回归PASS（新增10cache测试）；重开SQLite、实际磁盘payload及VikingFS/queue consumer执行，AGFS transport、VLM/embedder/vector DB为fixture，生产加密/全E2E未验证。I014 vectors_only保护用例现在验证合法缓存能复用，而非保持旧限制。报告tests-I015-regression.xml。本轮无远端部署/质量测试。

未完成：发现标准write在落盘前更新epoch，写入未完成时的捕获窗口仍需fence/CAS；完成写入编辑测试不能证明全并发保护。直接AGFS/WebDAV/sync/move、目录新引用/ACL变化的事务依赖、自动事件传播、跨ACL预过滤、引用分页、真实质量/多空间/BM25/ready/lifecycle/Wiki仍待完成，四完整FLOW/F5/F13不提升PASS。


## I016：标准文件写入保护

配置 registry 后，四个标准 VikingFS 写入入口从开始至实际存储 await 成功返回持有持久 token；开始/结束推进来源 epoch。并发 token 独立，正文捕获、generation begin、最终 publication 对来源与图片上下文/sidecar 依赖检查 writing/uncertain。取消/异常保留待确认标记，重开后仍阻止新索引而保留旧已发布内容。正常 finish 不能清除 uncertain；verified recovery、外部写入和跨主机协调仍待实现。部署时需排空旧未受保护 writer。

498项回归PASS（I015的488+10），报告 evidence/tests-I016-regression.xml。新测试调用实际 VikingFS write/write_file/write_file_bytes/append_file，暂停磁盘 transport 注入并发窗口；覆盖多writer、重开、账号/URI隔离、取消/异常、旧引用拒绝、sidecar/image context发布拒绝。不是远端 AGFS 或生产取消验收。追加写入 fixture 初始使用了错误 lease 字段，修正为现有 lease_ref 契约后通过，未削弱路径锁断言。

四个完整FLOW/Wiki接口、BM25、多空间、根可见性、ACL事务及任务ready/恢复继续未完成。I015写入窗口缺口由本轮标准入口保护补齐；不能据498项局部回归声明企业高可用。


## I017：运行复核与恢复前置能力

远端三服务health/鉴权capabilities均200，三个worker/引擎运行，SQL检查时无queued/running，三个部署文件与本地hash一致；证据runtime-I017-2026-10-04.json。当前存储API不能确认未知写已停止，尚未加入自动解除标记代码。已补spec§2.3.1后端terminal/fencing能力→授权读取→token/epoch CAS→审计→新代重建的路径及TC-13.12–16。能力/API/执行器未实现，不以hash稳定/TTL/普通锁当确认。当前498项回归为I016历史运行，未跑新线上推理/四FLOW。


## I018：解析队列就绪状态修复（2026-10-04）

发现HTTP仍运行但work()已退出时，旧health仅看依赖/权重文件会继续ready。health现在返回dependencies_ready与worker_state(stopped/recovering/running/failed)，ready_for_jobs同时要求依赖就绪、后台任务存活及启动恢复完成；submit因此拒绝未就绪请求，不消费上传内容。lifespan以finally确保异常退出仍取消后台任务；健康结果不暴露后台异常内容。

新增4项测试覆盖无worker、异常队列拒绝上传、真实OS共享锁下启动恢复阻塞/解锁及生命周期异常清理。10项worker定向测试、502项回归PASS。远端取得共享解析gate并确认队列空闲后更新worker.py，保留I017脚本备份，重启三个worker；三服务health与鉴权capabilities均报告running/ready，文件hash匹配。首次部署脚本以append方式开root创建的lock遭权限拒绝，未发生部署变更；改为只读文件描述符取得同一flock，无权限放宽，然后成功更新。

此项不是模型新推理/引擎探测、完整解析入库、四FLOW、Wiki或存储未知写恢复验收。队列失败时自动watchdog/重启仍缺；HTTP可用但ready=false可供监控判断，现有docker restart策略不因后台任务退出自动触发。证据evidence/runtime-I018-2026-10-04.json；I017运行记录保留为历史版本。


## I019：实际Wiki模板对齐

读用户指定entities包，8实体/schema/fewshot/vocab内容hash记录wiki-schema-I019-2026-10-04.json，可用tools/audit_wiki_schema.py重跑。修正过期Profile-only绝对规则，按绑定schema定义asserted_faults、Device.raw_refs、Procedure.fault_refs与Profile多部件关系。发现Profile fewshot与YAML冲突，未修改上游，默认适配包修正及注册预检未实现。工具仅清点，非注册器或Wiki验证器；W1/W2及新增TC均未验收。


## I020：不可变Wiki schema基础与三一适配产物

新增openviking/wiki/schema.py。调用者传入已授权读取的Markdown/YAML文件，SchemaBundle复制为不可变tuple并对完整成员hash；限制成员路径/数量/大小，拒绝执行文件、重复YAML键及aliases。compile_bundle检查结构字段类型/required、共享字段豁免、实体重复/引用目标和基础body小节，列出所有未编译自然语言规则。它不是完整实体校验器；不执行规则、enum/path/变体/身份/嵌套引用内容或fewshot语义一致性。Markdown-only包可以固定内容，但不能冒称可执行声明。

实际iroot八实体包经适配生成新version=2+openviking.1：只修Profile示例中“所有机理来自入口件”的旧句，保留所有其他成员并附上游digest/变更原因。产物evidence/sany-schema-adapter-I020.zip及implementation-I020-2026-10-04.json可复核；不修改上游。该包还有38条自然语言规则未执行，strict编译明确拒绝，不能称默认包已可上线注册。

14项新增schema测试+此前502项=516项回归PASS。覆盖caller修改不漂移、完整文件/hash、路径控制字符、重复key/alias拒绝、声明引用与来源豁免、未知规则严格拒绝、adapter不改上游/不重复套用、错误声明形状。实际包结构编译是本机真实输入，不是Wiki抽取/授权/存储E2E。collection/schema注册与持久化、模板冲突通用检查、38条规则适配、共享WikiBuildPlan和W1/W2仍未完成。


## I021：Wiki实体Markdown结构与引用依赖检查

新增wiki/validation.py，inspect_entity按固定EntitySchema检查YAML frontmatter、未知/缺失字段、类型和声明enum，AST检查required H2小节恰一次及顺序（围栏内伪标题不算）。嵌套关系对象须显式reference_keys声明，例如asserted_faults→fault_uri，不能猜字段或仅按名称造边；至少一条schema声明的来源引用，空sources不能靠正文链接冒充来源。Viking资源URI不含query/fragment，拒绝路径穿越/编码分隔符/控制字符，Unicode编码与原URI归一。draft保留来源内容hash及尚未执行规则，不作为published/validated状态。

resolve_references要求可信adapter提供authorized、实际内容和revision。实体关系的类型/account/namespace匹配collection；跨域来源必须由adapter验证读取权限，不由路径名称放行。重复目标验证期间变代明确失败，返回revision依赖用于后续提交CAS。当前resolver仅接口+测试替身，未接入真实VikingFS授权/ACL撤销或最终publication，不声明零悬空线上门禁已完成。

24项新增用例+此前516=540项回归PASS。实际三一适配包Profile示例经结构检查通过，9条引用待实际存储解析、5条Profile规则未执行；全包38条自然语言规则仍未执行。原schema测试扩展enum编译，保持strict拒绝未编译规则。完整默认包各实体示例/正文变体、身份/path/配置规则与跨ACL/export事务、schema/collection API、共享WikiBuildPlan、W1/W2仍待完成。本轮无远端Wiki部署或抽取E2E。


## I022：VikingFS引用读取与已验证实体门禁

新增wiki/resolver.py。VikingReferenceResolver使用原始RequestContext及真实fs.read_file_bytes，前后调用现有访问检查；拒绝bypass_acl上下文，要求持久source registry。由可信binding lookup确定注册namespace/root及实体类型，不能让模型提供这些值。读工作内容不读旧public投影，capture前后epoch检查及标准in-flight/uncertain标记拒绝过期捕获；内部revision=内容SHA+epoch，不增加用户URI溯源负担。

ResolvedReference新增entity_validated/validation_schema_hash。实体目标只有可信catalog的validated_sha256等于本次实际读取内容，且schema hash有效，才有验证标记；resolve_references另要求该hash匹配构建固定schema，类型/account/namespace一致。源和正文资料引用仍按授权来源读取，不误套实体验证要求。model frontmatter的validation_state本身不能当trusted catalog证明。

10项actual VikingFS/SQLite/磁盘测试+2项未验证/错schema用例，总552项回归PASS。覆盖最新工作内容与旧发布投影、内容绑定验证失效、读期间完成写入、in-flight/uncertain、读前及读后拒绝、root边界、空白内容、bypass拒绝。AGFS transport为磁盘替身、访问检查及binding catalog为mock；没有生产ACL/export或持久实体catalog验收。

读取适配器已具备，但namespace/实体可信catalog存储/API、全domain验证生成catalog、collection变化/撤权与提交CAS、源版本资产保留/WikiGC保护和共享WikiBuildPlan/W1/W2仍未完成。不以读取后检查替代最终事务，也不把全包38条未执行规则视为完成。本轮无远端Wiki部署或完整FLOW验收。


## I023：collection与schema内部持久化服务

新增wiki/catalog.py与共享PublicationStore的wiki_schemas/wiki_collections表。schema成员正文通过现有account私有AGFS blob保存，SQLite只保存身份/digest/ref/state；服务验证冻结包完整性，账号管理员才能注册。同ID/version相同内容幂等，不同内容或并发抢占返回冲突；新版本不改变现有collection。所有注册schema资产加入既有GC保护根，物理GC仍关闭，版本退役/配额/待注册资产保护仍待完成。

collection固定namespace_id、target_uri、raw_sources_prefix、schema_id/version/digest；同account不同collection的目标与raw写入根须相互独立，组件边界判重而非误把wiki2当wiki子目录，已存在文件不能充当目录prefix。显式跨collection共用写根尚未实现；共享来源通过W2读取授权URI，不默认共用上传写根。仅元数据绑定，不自动创建namespace成员/ACL授权。

get/pin沿用VikingFS访问检查，upload需raw+目标写权限，uri入口不要求写raw；读取包前后复核目标写权限。pin返回不可变bundle/binding_digest/能力状态供两入口共享，不是已完成WikiBuildPlan或持久job。私有包损坏明确失败，无live模板fallback；含未执行规则的schema可注册为draft_rules_pending用于编辑，structural_ready也不表示完整诊断发布门禁通过。

15项新增测试+此前552=567项回归PASS。覆盖真实SQLite重开/私有文件/GC根、幂等冲突/并发复核、账号同名隔离、版本固定、根交叉/目录检查、管理员与bypass拒绝、损坏、读权限/撤权及草稿能力。AGFS为磁盘替身、ACL为mock，不代表生产域授权或加密验收。初始报告保留；发现首次schema无account父目录时归档失败，服务增加现有ensure_parent_dirs；磁盘fixture同时补足AGFS挂载根初始化，未改断言。

内部服务完成部分持久化；HTTP/SDK注册、collection更新revision CAS、独立namespace/member/ACL注册、持久实体验证catalog、源export与提交事务、default38规则、Wiki抽取/关系/发布/W1/W2完整流程仍未完成。无远端OpenViking/Wiki部署。


## I024：Wiki 配置 HTTP、Python SDK 与前端客户端

四个配置接口已接入 server/app：POST /api/v1/kb/wiki/schemas、GET /api/v1/kb/wiki/schemas/{schema_id}/{version}、POST /api/v1/kb/wiki/collections、GET /api/v1/kb/wiki/collections/{collection_id}。同步/异步 client.kb 提供 register_schema、get_wiki_schema、create_wiki_collection、get_wiki_collection；沿用现有连接、认证和错误类型。schema 注册/读取限账号管理员；collection 继续使用现有 VikingFS 权限。请求不接受 account_id，账号来自原 RequestContext，非法额外字段或非字符串版本拒绝；缺少 VikingFS 或持久 publication_path 时明确 412。

配置使用示例（客户端已连接，模板文件由调用者读取）：

```python
client.kb.register_schema(schema_id="sany", version="2+openviking.1", files=schema_files)
client.kb.create_wiki_collection(
    collection_id="sany-maintenance", namespace_id="sany-wiki",
    target_uri="viking://resources/wiki/sany",
    raw_sources_prefix="viking://resources/raw/sany",
    schema_id="sany", schema_version="2+openviking.1",
)
configuration = client.kb.get_wiki_collection("sany-maintenance")
```

示例只注册配置，不执行知识抽取。schema_files 是完整模板包的相对文件名到 UTF-8 正文映射，包含 Markdown 模板及所需结构声明；模板不是知识来源。含未编译规则会记录 draft_rules_pending；structural_ready 也不代表诊断 domain 门禁通过。target_uri 是 Wiki 输出，raw_sources_prefix 是 W1 上传后的来源输出，W2 source_uris 是输入；不能互相替代。配置绑定 namespace_id 不等于创建成员/事业部 ACL。

前端 src/gen/wiki-client 仅生成这四个配置接口，复用 ov-client 的同一个 client 实例，避免丢失认证或服务器连接设置。gen-wiki-client.mjs 可从保存的实际 server OpenAPI 重建片段，不覆盖其他已有生成文件。本轮只完成客户端，未新增配置 UI 或构建任务 UI。

新增6项真实 HTTP 路由与同步/异步 SDK 测试，真实 SQLite/磁盘 catalog，认证 RequestContext 和 ACL 为替身；不能宣称真实 API key/OAuth 生产授权通过。I023 的567项加本轮6项，共573项本功能回归 PASS；生成客户端的独立严格 TypeScript 检查 PASS，未跑完整前端构建。扩展加入旧 SDK 配置/搜索用例的报告为604通过、5失败；用本轮变更前 HEAD 的 client.py 在临时 SDK 包复测，同样5失败、31通过，分别涉及 gateway_token 加载、find/search 默认 telemetry 及 context_type 旧参数兼容。保留两份失败报告，本轮未改这些兼容行为，不能宣称全仓回归通过。

D1/D2 已有部分实现；W1 文件上传+collection 与 W2 URI+collection 仍未接入共同 WikiBuildPlan。注册配置不是构建完成；四个完整 FLOW、持久任务恢复、完整 namespace/member 授权、域规则/实体 catalog/CAS、真实检索质量及线上高度可用验收仍 NOT_RUN。


## I025：两种 Wiki 输入共用的持久来源准备

新增 WikiBuildPlanner / WikiBuildPlan：W1 调用方先将上传解析产物写到 collection.raw_sources_prefix，W2 直接选择已有 Markdown/目录；之后两种入口共用 prepare_sources。固定 collection/schema 摘要与来源 URI、SHA、字节数和标准写入 epoch，解析后的正文保存到账号私有 AGFS 版本，SQLite 只存引用和计划。没有保留 PDF/Office 原件，没有替换来源 URI，也没有把 schema 当知识来源。现有 collection 绑定不可变；将来若开放更新，W1 在解析开始前还须由持久 job 固定同一绑定并传递到此阶段，不能在解析后重新选版本。

计划状态为 prepared，不是 queued/ready；namespace/export 授权状态明确 namespace_export_pending。现阶段校验的是现有 VikingFS 来源读/目标写权限，不具备业务 namespace/export 注册，也不执行 LLM、domain 规则、关系发布。未编译规则的 schema 保持 draft_rules_pending；不得以 prepared 当可执行或诊断发布授权。

目录递归/非递归走实际 VikingFS 列表，保留逐文件原 URI，跳过派生隐藏 sidecar、图片和控制 JSON；直接 PDF/Office URI 显式要求先解析。默认上限：2048文档、16384列表条目、64层目录、单文件8MiB、总Markdown64MiB，超限明确失败不截断。拒绝输入包含本 collection 输出树，防止 Wiki 将自身生成结果当新事实反复抽取。W2 可以准备其他授权来源的 Markdown，但跨域发布仍要独立 export 门禁。

来源读前后检查权限和写入 epoch，目录在捕获后重新遍历；持久事务再检查全部标准写入保护/epoch、collection绑定。幂等作用域为账号+目标namespace+key：同来源版本/输入参数返回原计划，不同版本或参数返回冲突；两个实际重叠的准备请求在保存时复核。源文件修改后仍能读已捕获版本用于检查；check_current_inputs 发现当前正文、目录成员或epoch变化时拒绝继续采用旧计划。私有内容损坏不回退实时源。

批处理使用 iter_source_texts：只加载一次固定计划，再逐文档检查读前/读后权限并读取私有版本，避免每个文档重复扫描整个计划；下一文档撤权时，不读取其私有正文。公开来源 URI 不需要用户提供版本号或精确位置。

共享 PublicationStore 新增 wiki_build_plans 表；所有已保存计划引用加入 GC 根。当前没有计划退出/退役/租约和物理回收，失败准备的未注册 blob 也未具备回收事务；这些仍属于 F13 未完成项。目录二次遍历和写入 epoch 只是准备/预检查，未覆盖目录增删、ACL与外部写入在最终提交期间的完整原子保护。没有新增生产开关或远端 Wiki 部署。

27项新用例加此前573项，共600项本功能回归PASS。覆盖两入口等价、实际SQLite重开/私有正文/GC根、幂等/并发保存、递归/来源URI、成员变动/来源写入/未知写入、权限、损坏、限制、自输入，以及预检查和流式撤权。首轮23PASS/1FAIL是裸 resources URI 测试期待错误分类不符已有canonical URI契约，修正该断言并保留初始报告；未放宽校验。后续新增预检查/流式用例全部包含最终回归。I024五项旧SDK基线失败仍未修复，600项不是全仓测试。

另用真实全卷 MinerU 衍生手册及三一适配 schema 回放来源准备：968个Markdown、1,205,623字节、3个目录，upload/uri两种计划使用完全相同的schema绑定和来源版本；全部流式读回摘要匹配，当前来源预检查通过（1项数据回放PASS）。schema含38条未编译规则，状态draft_rules_pending；授权状态namespace_export_pending。磁盘AGFS/ACL替身，不证明真实生产授权、章节对齐质量或实体抽取。完整 FLOW-W1/W2、Wiki HTTP 构建任务和统一发布/索引仍NOT_RUN。


## I026：执行明确结构门禁，补齐 schema 覆盖台账

新增纯数据 structural_gates：exactly_one、nonempty、distinct、forbid、parent_profiles、body_reference_order；声明字段/类型/小节不一致或未知门禁明确拒绝，不执行包内脚本/自然语言指令。class_name.enum_families 编译为枚举，Procedure body_sections.templates 按分类选择完整小节并检查次序；模板不能重叠、遗漏基础小节或留下未覆盖枚举。引用列表可声明 reference_key 和有类型的 reference_attributes，精确校验对象结构、枚举和有限数值，不丢失 BOM 的 spec。

针对已审计 I020 完整包生成新不可变适配版本 2+openviking.1+structural.1，摘要6a6f66c8…；原 iroot 与 I020 包保持不动。15个明确门禁：Profile配置锚二选一、possible_faults非空/去重/正文引用顺序和父目录一致；Procedure fault_refs/target_components非空/去重；Fault affected_components非空/去重；Device禁sources且raw_refs非空；ConfigSpec device_refs非空/去重。公共sources补ref_kind=source，Symptom asserted_faults补fault_uri对象声明，ConfigSpec BOM补component_uri+spec声明。配置入口继续只注册配置，没有自动启用或部署该包。

当前直接 compile I020 原包再 inspect 实际 Profile 示例会因缺少已声明来源引用而拒绝。I021 的单例结构局部PASS不能替代完整包的引用编译/fewshot一致性证据；历史报告保留，I026提供可重现的新适配重放。新包七类示例通过当前结构门禁；DTC示例仍有kb://chapters/813927429744789，明确拒绝，需要已知实际来源的迁移/映射，不能猜造一个Viking来源。示例检查未解析到真实实体/来源，也未校验path布局，不能作为业务事实或正式发布证据。

发现_shared的9条shared_rules此前未进入覆盖清单，本轮按shared:ID在各实体保留。自然语言只作为政策数据；38条实体规则、9条共享规则及8个实体路径布局声明仍待实现（118个按实体记录的待验证条目）。明确结构门禁不代表同名自然语言规则的全部语义已实现，不移除原uncompiled规则。require_executable_rules继续拒绝；新包仍draft_rules_pending。

schema/collection读取和pin从校验过的私有包重新计算覆盖状态并刷新缓存，不信任旧structural_ready；数据绑定与包hash不变。来源与正文链接引用统一要求当前account，namespace必须实际非空；同账号其他授权来源域可以继续读取，跨域export门禁仍未实现。原始Role/FS ACL不变，测试仍是ACL替身。

647项本功能回归PASS，相对I025新增47项：结构门禁/模板/对象字段/真实示例，来源账号/namespace、catalog与HTTP旧覆盖缓存、畸形声明返回400而非500。首轮结构测试失败是fixture把同一个Python列表交给YAML序列化形成alias，修正为独立列表、不放宽别名拒绝；引用账号拒绝消息兼容既有mismatch断言，保留两份初始失败报告。编译与diff检查通过；I024五项旧SDK兼容基线失败仍未修复，此647项不是全仓验收。

剩余：自然语言/身份/证据/适用性/路径门禁、DTC示例来源映射、生产namespace/export授权、LLM抽取/保守归一、实体与关系事务、Wiki两个HTTP任务入口和共同索引发布、计划执行指纹/恢复/GC。F10、四FLOW和线上高度可用继续NOT_RUN。


## I027：知识 namespace 根与导出策略 HTTP/SDK 配置面（2026-10-04）

新增 `openviking/wiki/namespaces.py` 的 `NamespaceCatalog`：在既有资源 ACL 之上登记独立知识 namespace 根，并提供显式跨 namespace 导出边。不引入独立的成员授权、ACL 投影或向量检索。根必须已启用 ACL、具备 restricted 根 ACL、且是实际存在的目录；同账号各根必须互不重叠。`register` 的门禁顺序为 ACL 启用 → 目录门禁 → 根重叠冲突 → restricted-ACL 门禁：文件 URI 当根按前置条件失败（FAILED_PRECONDITION），嵌套目录根按冲突失败（CONFLICT）。同 ID 同根重注册幂等，同 ID 不同根冲突。

`authorize_sources`/`check_source_use`/`recheck` 依据导出边与根 ACL 授权跨 namespace 来源使用；读取/写入权限不隐含导出授权，需显式导出边；组上下文与 actor 变化、导出策略变更、源/目标 ACL 撤权都会使已发凭据失效。策略 revision 随每次变更递增。

HTTP 面：`POST /api/v1/kb/namespaces`（注册）、`GET /api/v1/kb/namespaces/{namespace_id}`（读取）、`POST /api/v1/kb/namespaces/{source_namespace}/exports`（设置导出边）。请求模型 `extra='forbid'`，不接受 account_id（账号来自 RequestContext）；`allowed` 必须严格布尔。router 已注册进 `openviking/server/routers/__init__.py` 并在 `server/app.py` include。SDK `client.kb` 同步/异步新增 `register_namespace`/`get_namespace`/`set_export`，沿用同一连接、认证与错误类型；`set_export` 返回 `{source_namespace,target_namespace,principal,allowed}`。

669 项本功能回归 PASS（I026 的 647 + 15 项 namespace catalog + 7 项 namespace HTTP/SDK），报告 `evidence/tests-I027-regression.xml`；namespace 聚焦报告 `evidence/tests-I027-namespaces.xml`（22/22）。覆盖持久注册重开与规范 URI 解析、读写权限不隐含导出、组/单向导出、凭据 actor/组/账号复用拒绝、源/目标撤权、更宽松子 ACL 不绕过根成员、注册门禁与顺序、HTTP 严格模型与状态码映射（409/412/422）、异步/同步 SDK 契约。ACL 上下文存储与 embedding 为替身，磁盘 AGFS；不是生产授权或完整 namespace 检索/提交验收。I024 五项旧 SDK 兼容基线失败仍未修复，此 669 项不是全仓验收。

剩余：生产业务 namespace 成员/ACL 注册与隔离、全局 namespace 预过滤与 ACL 变更 fencing、任务身份刷新、跨域导出事务提交/CAS、持久实体验证 catalog、Wiki 两个 HTTP 构建入口与共同索引发布、自然语言/身份/证据/适用性/路径门禁、DTC 示例来源映射、物理 GC/计划退役/恢复。F10、四 FLOW 和线上高度可用继续 NOT_RUN。证据 `evidence/implementation-I027-2026-10-04.json`。


## I028：W2 已有 URI 构建 Wiki 的 HTTP/SDK 入口（2026-10-04）

按 spec §0.1 的 W2 契约接入 `POST /api/v1/kb/wiki/builds`：`source_uris`、`collection_id`、`recursive`、`idempotency_key`。请求模型 `extra='forbid'`，不接受 namespace/target_uri/schema 等冲突字段（这些由 collection 注册项决定）；非 Markdown 来源、超限、不可读在昂贵模型工作前拒绝。路由复用 I025 的 `WikiBuildPlanner.prepare_sources(input_kind='uri')`，把来源固定为不可变版本并返回 `{build_id, digest, manifest, state='prepared'}`。SDK `client.kb.build_wiki` 同步/异步同契约。

本入口只准备来源计划，不执行抽取、domain 规则、实体/关系发布或索引，也不返回 queued/ready；`prepared` 不是构建完成，`authorization_state` 仍为 `namespace_export_pending`。W1（`POST /api/v1/kb/wiki/ingestions`，上传物化 + 同一 planner）与共享 WikiBuildPlan 的执行/发布仍未实现。

675 项本功能回归 PASS（I027 的 669 + 6 项 W2 入口 HTTP/SDK），报告 `evidence/tests-I028-regression.xml`。覆盖异步/同步 SDK 计划准备、幂等重放同 build_id、严格请求模型与 422、非 Markdown 来源 400、OpenAPI 路径集含 builds。ACL 与 embedding 为替身、磁盘 AGFS；不是生产授权、真实抽取或完整 W2 E2E。I024 五项旧 SDK 兼容基线失败仍未修复。证据 `evidence/implementation-I028-2026-10-04.json`。


## I029：W1 上传+collection 构建 Wiki 的 HTTP/SDK 入口（2026-10-04）

按 spec §0.1 的 W1 契约接入 `POST /api/v1/kb/wiki/ingestions`：`temp_file_id`、`collection_id`、`update_policy`、`idempotency_key`。请求模型 `extra='forbid'`，`update_policy` 仅接受 `fail|replace`。

`WikiBuildPlanner.prepare_upload` 是 W1 的来源物化阶段：先校验纯文件名、`.md` 后缀、UTF-8 非空非二进制正文与文档大小上限，再把上传正文写到 `collection.raw_sources_prefix/<文件名>/<文件名>`（同名子目录承载解析器产生的相对图片兄弟文件），随后调用与 W2 完全相同的 `prepare_sources(input_kind='upload')`，因此两入口共享同一 collection/schema 绑定、摘要与计划持久化路径（本轮把计划落库抽为 `_persist_plan`）。目标已存在且字节不同时返回 409，须显式 `update_policy='replace'`；字节相同的重放跳过重写，避免无谓写入推进来源 epoch 而把同一幂等键误判为不同修订集。`prepare_sources` 仍要求来源位于 collection 的 `raw_sources_prefix` 之下。

HTTP 路由经 `TempUploadStore.resolve_for_consume` 解析临时上传、读取本地副本，并在 `finally` 中清理，随后交给 planner；不执行抽取、domain 规则、实体/关系发布或索引。SDK `client.kb.ingest_wiki` 同步/异步同契约。

本入口只准备来源计划，`state='prepared'` 不是 queued/ready，`authorization_state` 仍为 `namespace_export_pending`；PDF/Office 解析物化、共享 WikiBuildPlan 的执行/发布、执行期 namespace/export 强制仍未实现，完整 FLOW-W1 继续 NOT_RUN。

682 项本功能回归 PASS（I028 的 675 + 7 项 W1 入口 HTTP/SDK），报告 `evidence/tests-I029-regression.xml`；W1 聚焦报告 `evidence/tests-I029-w1-ingestions.xml`（7/7）。覆盖异步/同步 SDK 物化并准备计划、物化 URI 与读回正文、幂等重放同 build_id、同名不同内容 409 与 replace 覆盖、严格请求模型与 422、非 Markdown 上传 400、OpenAPI 路径集含 ingestions。临时上传存储、ACL 与 embedding 为替身、磁盘 AGFS；不是生产授权、真实解析或完整 W1 E2E。I024 五项旧 SDK 兼容基线失败仍未修复。证据 `evidence/implementation-I029-2026-10-04.json`。


## I030：来源计划查询与失败请求的写入保护（2026-10-04）

新增 GET /api/v1/kb/wiki/builds/{build_id} 与 kb.get_wiki_build 同步/异步方法，沿用 planner.get 的账号隔离、manifest完整性、collection绑定及当前来源读/目标写权限检查。返回 prepared 来源计划，不返回正文、不执行任务、不刷新历史能力/导出状态。

上传本地副本读取改为 document_bytes+1 的有界读取，超限请求在任何来源物化/计划落库前失败并触发临时副本清理。shared upload store 下载到磁盘阶段的配额未在本轮实现。修复已绑定幂等键复用时先写入再报冲突的问题：参数/字节不匹配提前409；来源后来改动或删除也拒绝恢复旧原文。最终保存仍有原epoch检查。未覆盖首次并发上传物化的完整事务。

新增10项 HTTP/SDK、权限、账号、边界读取、清理和失败不写入用例；I029的682项加本轮10项，共692项局部回归PASS。首轮新增测试的3个失败为错误引用server异常类而SDK映射为SDK异常，修正测试导入；首轮整组691通过/1失败是旧OpenAPI路径清单未含新增GET，更新严格清单后重跑通过，报告均保留。前端从实际create_app OpenAPI生成Wiki客户端并独立类型检查通过，未跑全前端构建。旧SDK五项基线失败仍存在。

文档现同步补充I027知识域、I028 URI入口、I029上传入口的实际进度，避免主spec只停留I026。本轮不修改远端部署，也不宣称Wiki执行、持久队列或四FLOW完成。证据：evidence/implementation-I030-2026-10-04.json。


## CR-011 / v0.10：未达成实施路径重新对齐

新增openviking-enterprise-kb-backlog.md与feature-status.json，33项剩余任务明确优先级/依赖/完成条件，保持四FLOW及15Feature完整验收NOT_RUN。主spec前面的状态表与API边界已同步I030，不再仅靠末尾历史实施记录解释现状。新增TC-16.1–19待验收断言。

本轮只修改规格/状态记录与文档一致性校验，不新增或部署产品能力；最新产品代码范围仍为I030的692项局部PASS，旧SDK五项兼容失败仍存在。任务不能因文档已完成而标DONE；后续依赖顺序先可靠性/权限与核心文档检索，再Wiki及JEV。

## I031 首批独立并行实现（2026-10-04）

按用户授权，三个agent分别负责无前置依赖的KB-001、KB-002、KB-031，主agent审查集成、同步前端并回写证据。共享工作区按代码范围分工，保留全部已有未提交改动。

|任务|本轮实际增量|仍需完成|
|---|---|---|
|KB-001|旧Python bridge find/search兼容；prepare key统一1–128；实际HTTP400/SDK异常契约；前端补知识域配置client|Rust CLI字段一致、未来job/execute及完整任务契约|
|KB-002|同机Unix socket认证repository；SQLite/WAL单权威所有权、断连拒绝、重启、registry备份；真实producer经协调进程发布|跨主机内部接口、Wiki/ACL旧事务迁移、真实后端恢复、联合灾备|
|KB-031|gold人工审定绑定、数据冻结/验hash、文档/机型/片段防泄漏、类别和分组CI报告|独立人工gold、全部类别覆盖、真实质量和性能|

最终联合回归925项全部通过，覆盖I030原测试模块、所有enterprise_kb、旧客户端配置及完整Python SDK；前端7个Wiki路径+3个namespace路径从实际OpenAPI生成并严格TypeScript检查通过。SDK旧5项失败中4项修复实现、1项更新过期凭据转发断言，保留现行主机绑定和challenge规则。第一轮924项通过后评测输入校验增加1项，因此最终重跑925项，历史首次报告保留。

评测实际产物为50条synthetic、0条gold，全部unassigned；单文档/单机型不拆成独立训练与测试。没有检索/模型性能数据，质量与性能仍NOT_RUN，CI为null。协调服务验证为同机独立进程，AGFS/embedding/vector仍有fixture替代；旧Wiki/ACL直接SQL在authority worker上明确拒绝，尚未完成可部署的全通道迁移。

三个任务保持PARTIAL，15个Feature和四条完整FLOW均NOT_RUN。完整仓库测试未执行，不以925项局部通过替代企业验收。总报告见[I031](evidence/implementation-I031-2026-10-04.json)，各任务报告见[KB-001](evidence/parallel-KB-001-2026-10-04.json)、[KB-002](evidence/parallel-KB-002-2026-10-04.json)、[KB-031](evidence/parallel-KB-031-2026-10-04.json)。权威服务使用方法见[部署说明](../deployment/registry-authority.md)，评测使用方法见[评测说明](evaluation/independent-gold/README.md)。

## I033：持久执行、注册域门禁与扩大回归

已接入同机协调者领域命令、D1/D2持久执行桥接、当前身份刷新、VikingFS/向量注册域保护及旧collection显式迁移。生成客户端覆盖Wiki9条、namespace3条及文档jobs3条路径并通过独立TypeScript检查；随后队列列表迭代需再次生成。

扩大回归发现SDK导入测试破坏全进程模块身份；现改为独立解释器执行。修正旧FakeAGFS树/分页协议与PermissionDeniedError断言，Qdrant日志断言直接接收实际logger记录。最后一项本地client/deploy副本漂移已修复并79项专项通过；远端部署版本单独记录，不能用本地副本一致代替线上验证。

剩余工作继续按33项台账推进，当前Office真实转换、服务端未知写入恢复、持久队列展示由三个agent独立处理，完整发布/检索集成与Wiki执行仍待闭环。


## 39. I034 当前接入与验证（2026-10-05）

联合回归1575 PASS、1 SKIP；跳过项为显式启用的真实Wiki模型探针，已在独立报告实际运行通过。前端356项、完整TypeScript和生产构建通过。初轮4项旧契约失败与前端环境/超时失败保留原报告，修复后重跑。该范围不等于完整仓库或产品验收。

文本BM25已加入权威发布回执，现有find/search复用固定manifest和当前权限；不含业务meta、图片摘要与完整物理GC。Office本地45项和服务器Docker六格式合成契约通过；生产worker与远端转换容器传输未完。受控后端未知写入恢复与离线隔离恢复已验证，普通AGFS能力不足时明确拒绝，恢复库不得自动启动。

Wiki旧接口继续200 prepared；POST `/api/v1/kb/wiki/builds/{build_id}/execute`固定plan digest与profile，返回202持久W1/W2 job。已准备MD抽取共用一个执行器，支持逐片段恢复；私有draft使用 `/api/v1/kb/wiki/jobs/{job_id}/drafts/{draft_index}`读取，复核所有来源ACL/export及当前输入。完成事务绑定真实候选回执和plan，并再次复核source epoch/export revision。候选blob纳入GC保护；没有诊断知识发布。

默认包仍有118个未执行规则实例；严格诊断模式拒绝。真实配置模型合成探针生成1候选，不能证明语义证据质量。用户明确暂时没有人工gold；质量与性能门禁继续保留。KB-024从OPEN更新为PARTIAL，所有完整Feature与FLOW仍NOT_RUN。报告见[I034](evidence/implementation-I034-2026-10-05.json)。


## I046 W1 PDF 执行状态与验收修正（2026-10-05）

I046（2026-10-05）复核真实 W1 PDF 作业：job `1e73c293bb924638a3f9c4d027ee9fc0` 已正常到 `draft_ready`，耗时 479 秒，捕获 8 篇非空 MD，44 次模型调用、44 份私有草稿、42 个候选。通过实际 SDK/API 逐份读取，全部证据 URI 和引文逐字核验通过，未发布诊断知识。原验收脚本约 300 秒观察窗口过短；现支持继续验收已有作业、默认 1800 秒观察窗口、全部草稿计数/来源/私有回执/逐字引文校验。PDF/Office 路由与章节/Wiki 回归 70 PASS，验收脚本 8 PASS。简化 business/Thing schema 下的切片执行证据不能替代完整三一 schema、全卷质量或发布验收。15 个 Feature 均 PARTIAL，33 项任务为 32 PARTIAL + 1 OPEN，四条 FLOW 均 PARTIAL；完整产品验收尚未通过。

该作业第 1 次执行正常结束并释放租约，无执行错误。之前的 `running` 快照取于完成之前；固定 100×3 秒轮询使脚本先结束。现在 `--job-id`（可选 `--build-id`）在原计划上继续验收，避免重复上传使固定输入 epoch 失效；观察超时保留实际状态和可操作原因。空首片允许后续片段产生候选，但所有片段回执数量、来源、固定计划及私有状态均须通过。

代码回归初轮 61 PASS/1 FAIL 为旧 PDF 拒绝断言；新增路由测试初轮 63 PASS/7 FAIL 为测试目录与已有 fixture 重叠。修正不支持类型为 ZIP、使用独立测试目录后 70 PASS。验收脚本独立 8 PASS，含续查不上传、空首片、非法溯源、伪造引文和观察超时。完整仓库回归未执行。

证据：[实际 API 全部草稿核验](evidence/flow-w1-pdf-2026-10-05.json)、[正式 harness 继续验收](evidence/flow-w1-pdf-resumed-I046-2026-10-05.json)、[70 项回归](evidence/w1-pdf-regression-I046-2026-10-05.xml)、[8 项脚本回归](evidence/w1-harness-I046-2026-10-05.xml)。


## I047 Office 实际链路、上传重放、索引与 ACL 门禁（2026-10-05）

I047（2026-10-05）接通显式 SSH→冻结 Docker Office 转换，默认字体门禁开启；六格式转换合同通过，实际 D1 Word 上传→PDF→规则回退→7 条索引重建/0 失败→限定来源检索通过；W1 DOCX/XLSX/PPTX 三族最终均到私有 draft_ready（2/2/5 个候选），逐字引文核验通过。PowerPoint 原输出错误改写标题符号，v3 提示修复后通过；此前失败记录保留。上传新增原文件指纹，幂等重放不重解析/改源 epoch；已有树替换明确拒绝，整树提交仍待实现。持久 D2 保存真实索引计数并等待共同发布回执；ACL 更新过滤历史代并检测操作期间 head 变化。完整三一 schema 的 118 个待执行规则在实际 strict API 中明确拒绝、无作业创建。联合回归 1008 PASS/1 SKIP；最后修改后受影响范围 141 PASS、Office 59 PASS、上传重放 60 PASS（范围重叠，不相加）。这些是实现/合成合同证据；15 个 Feature 与四条 FLOW 仍 PARTIAL，32 项任务 PARTIAL + KB-032 OPEN，完整产品验收均未通过。 [本轮报告](evidence/implementation-I047-2026-10-05.json) · [Office W1 实测](evidence/flow-w1-office-fontchecked-I047-2026-10-05.json) · [完整 schema 拒绝门禁](evidence/wiki-strict-gate-I047-2026-10-05.json)。

当前服务在 1933，使用 deploy/office-converter/ssh-soffice 和冻结 LibreOffice 7.4.7.2，字体检查保持 true，库存取实际远端容器。现代 OOXML 合同夹具的字体声明明确改为已安装的 Noto Sans CJK SC；Excel 正向抽取夹具另加显式合成维修工作表。样本与字体变化/hash 均保存，不称独立 gold。字体检测覆盖 Word/font table、Excel/styles 与 DrawingML/theme，语言代码不当字体；仍是保守声明检查，未做字形与实际样式可达性质量评估。

上传固定 upload_source(filename/sha256/size)，同键重复上传验证当前来源/成员/权限/epoch并返回原计划，不重复模型解析。遗留计划未保存原文件指纹时拒绝猜测输入。已有 PDF/Office 树无安全整树提交时，replace 返回 WIKI_REPLACEMENT_GATE_PENDING，保留旧源；这是尚未实现的替换能力，目标契约不变。

索引有失败或语义缺口时不设置 index_dispatched；无失败时保存真实结果并等待正文树、图片、父子、语义层、BM25、meta 与版本切换共同回执。实际 D2 门禁合同核验后取消测试等待作业，避免永久后台轮询；不把 backoff 当 ready。

ACL 修复避免退休/未发布/删除代回写；持当前head租约，并在子树更新前后比较受影响 generation map。并发 head 切换明确 ACL_PUBLICATION_CHANGED；这不是原子事务 CAS，最终检查至返回仍有窗口。

PPT 错误 quote 将 # 改成 ##，未放宽验证；v3 提示要求直接从来源复制每个字符，成功后全部 quote 再核验。抽取器/政策版本现在绑定提交配置、worker和权威完成。旧私有草稿继续可读；未带版本的旧执行不能静默用新提示继续，应以原 prepared plan 和新 execution key 重建任务。原 Excel 纯表格产出0候选、测试期间 export revision 变更使旧任务拒绝、PPT 两次错误引文均保留报告；最终样本通过不意味着模型质量/性能达标。

- [D1 Office 字体门禁开启的实测](evidence/flow-d1-office-fontchecked-I047-2026-10-05.json)
- [W1 三族最终及失败历史](evidence/flow-w1-office-fontchecked-I047-2026-10-05.json)
- [同输入同键真实重放](evidence/w1-office-replay-I047-2026-10-05.json)
- [真实 D2 索引/发布门禁](evidence/durable-d2-index-gate-I047-2026-10-05.json)
- [三一 schema strict 拒绝及 ACL 重放](evidence/wiki-strict-gate-I047-2026-10-05.json)


## I048 深目录图片引用与 VLM tokenizer 修正（2026-10-05）

I048（2026-10-05）修复图片引用深度超过8层时静默漏收，以及误用 embedding tokenizer 作为 VLM tokenizer 的问题。新增 VLM 专属固定 tokenizer 配置、主/备模型覆盖检查和文件指纹校验，截断后重新核验实际字符串预算；引用记录区分 measured/estimated，context 升级 v3。72 项受影响测试通过（含现有真实 VLM 非空摘要协议探针；非质量 gold）。代码已在本地验证，当前服务仍为 I047，未声明新部署链路通过。三个并行子任务因模型服务403中断，未增加其完成证据；共同发布、完整schema和legacy Office仍待实现/实测。15个Feature、四条FLOW及任务整体状态不提升。

新增配置示例（model须与实际部署名称相符，hash须从实际模型tokenizer产物获得；示例不是已审定模型配置）：

```json
{"vlm":{"context_tokenizers":[{"model":"your-vision-model","path":"/absolute/path/to/tokenizer.json","sha256":"<64 lowercase hex characters>"}]}}
```

配置须覆盖备模型与credential覆盖模型；此版本计量范围只包含每条引用上下文文本。模型完整上下文窗口和图像预算、跨章节引用注册、质量gold仍缺失；image-context-v3使旧缓存需要语义重建。当前API进程未重启，I047实际链路证据仍保留。三个agent因403中断，无可验证新代码成果，不计为任务完成。

- [I048范围、限制和源码hash](evidence/implementation-I048-2026-10-05.json)
- [72项回归原始结果](evidence/image-context-I048-2026-10-05.xml)


## I049 三一八类型路径政策执行（2026-10-05）

I049（2026-10-05）新增冻结三一路径适配包与实际执行器：八种path_layout校验目录形状和本地字段对齐；候选验证及合并来源重新执行，回执绑定schema/声明/正文hash和collection root。新适配包未执行标记118→110，仅消除8个实际执行的路径标记，全部正文规则保留。七个原fewshot结构通过，DTC原kb://来源仍拒绝，未猜测映射。受影响191专项通过；当前服务/旧注册schema未改，原包仍118，完整strict诊断/发布/gold/FLOW未验收。

新增 adapter 版本 `2+openviking.1+structural.1+paths.1`，digest `c7119de4cb142ca61ad5b9b78cb72c5a6b064a89437dff6f3b657ad9ffa078ef`；输入冻结 I026 未变。不是执行任意正则：声明必须匹配已审定hash，实际由有限路径程序约束。Device 系列/型号、ConfigSpec host_code、DTC机型目录/代码文件名以及相关class_name与目录对齐。路径必须属于显式collection root，不能从URI中猜根。原文/模板规则没有自动标记为已执行，身份唯一性与关系一致性仍pending。

抽取器和合并验证都调用路径程序；抽取器保存server-generated PASS回执，合并重新计算后将回执放入来源provenance。回执不能作为诊断发布证明。policy版本升级v2，旧未完成v1任务不能混用新政策，应按原prepared plan与新execution key提交；旧私有草稿可读。未重启服务，未声称新包实际线上执行。DTC原fewshot的kb://chapters来源仍缺权威迁移，完整结构拒绝；仅路径执行器通过。

- [完整范围、测试及源码hash](evidence/implementation-I049-2026-10-05.json)
- [八类型原示例重放与未执行规则](evidence/wiki-schema-I049-2026-10-05.json)
- [冻结新适配包](evidence/sany-schema-adapter-I049.zip)
- [191项定向测试](evidence/wiki-path-I049-2026-10-05.xml)


## I050 持久发布批次、封口fence与legacy Office复现（2026-10-05）

I050（2026-10-05）持久D1/D2新增整次reindex批次，真实member回执观测/崩溃恢复；封口在authority同一事务校验job owner/fence/lease，过期worker不得切head。提交成员可共同切换，但完整7类覆盖门禁仍pending，不放ready。旧DOC/XLS/PPT六项实际D1/W1文本/索引/溯源合同通过，私有候选3/2/4；确认XLS可见图在MinerU worker产物阶段缺失（源与PDF有1图，产物0asset/0引用），本轮Office证据整体不通过，复现资产已冻结。联合1061 PASS/1 SKIP，最后封口修改后85定向PASS（与40封口回归重叠）；新batch命令尚未加载线上，15 Feature/四FLOW均PARTIAL，完整验收不提升。

producer写入/排队前记录publication_batch_id。所有实际加入的text/image/semantic成员在外层结束后封口；job_publication_status从authority读取真实expected/received和成员hash，不接受调用者宣称的receipt。queued/published重试仅观察；building中断批次先失败隔离再新尝试。job_publication_seal在同一SQLite事务中检查原owner/fence/lease、绑定batch/account/namespace，再封口和发布，修复客户端assert_active到seal的竞争窗口。Unix socket合同及旧worker拒绝测试通过。真实代码生产者/消费者测试使用测试embedding/向量后端，不替代线上部署；最终85定向回归覆盖最后fence改动，1061/1skip联合运行在该最后改动收集前，范围重叠不相加。

共同批次仅证明已提交成员，不证明成员清单穷尽完整正文树、全部图片、父子、L0/L1、BM25、meta与版本。完整ready依旧禁止。新authority命令和runtime尚未加载当前服务，不能只重启API而遗漏实际协调进程的协议更新。

旧Office合成实测：DOC/XLS/PPT D1分别重建8/4/9条，均0失败且限定来源URI可追；W1分别3/2/4个私有候选，逐字证据通过。修正过同stem导致的harness命名断言以及本地路径错误，保留历史，没有覆盖DOC。源OOXML字体已归一、冻结LO导出legacy、转换器库存门禁开启；CFB全部声明字体识别仍NOT_RUN。

XLS图片是已复现失败：OOXML绘图锚点包含1个PNG，legacy→中间PDF第1页实际绘制1图；相同MinerU/auto/options{}的5页产物覆盖校验通过，但没有资产/MD图片引用，最终树没有images。确认早于materializer，尚未分清模型原始输出或worker内部归一化；不能把文本合同PASS当Office完整PASS。所有输入/中间产物/原artifact已保留。

- [范围、测试和源码hash](evidence/implementation-I050-2026-10-05.json)
- [legacy六链路与图片FAIL](evidence/flow-office-legacy-I050-2026-10-05.json)
- [完整图片缺失复现材料](evidence/office-legacy-I050-artifacts/)
- [最终85项批次/执行/语义回归](evidence/document-batch-I050-2026-10-05.xml)


## I051 实际共同发布恢复与空间边界（2026-10-05）

实际 API 的协调状态使用进程内 PublicationStore；已受控加载 I050 的 status/seal 与本轮客户端、空间边界代码。单 MD D2 收齐 3/3 回执，目录 D2 两个 MD 与根目录收齐 9/9 回执（2 text、2 BM25、3 meta、2 L0/L1），均切换同一提交成员批次，原文 SHA 不变。目录模型处理 17.6 秒、失败 0，L0 126 字符、L1 3470 字符。

在 published/backoff、无后台写入的边界 SIGKILL API 后，作业继续读取同一 batch/membership，构建数 150、回执数 483 不增加。证据：[实际批次恢复](evidence/live-durable-joint-I051-2026-10-05.json)、[目录分层批次](evidence/live-durable-semantic-I051-2026-10-05.json)。这不证明 inflight 写入崩溃恢复；七类完整覆盖门禁仍 pending、不标 ready。实测作业已通过 SDK 取消，取消没有撤回 published batch。

`reindex_executor` 的非递归祖先摘要与 `semantic_processor` 的自动父级传播均读取实际注册空间边界：可刷新空间根 L0/L1，但不可越过该根汇总兄弟空间。重叠注册根拒绝，读取 registry 失败不静默放行；未注册旧资源保留旧边界。该修复不替代全通道派生权限验收。

解析客户端把实际上传字节复制到私有临时文件，流式 SHA 绑定上传副本；产物 source_image_preservation 的源 PDF 指纹错配则拒绝并取消任务。真实来源保全不等于模型抽取、图语义或章节归属通过。MinerU 源产物丢图与保全部署实测详见[本轮报告](evidence/implementation-I051-2026-10-05.json)，最终XLS D1/W1与图文索引复测PASS，证明本次保全合同。

受影响联合 164 PASS，初测 PyMuPDF image_profile 兼容失败保留（158 PASS/1 FAIL），改用固定 Pillow 头部尺寸探测后修复；45/92 专项与联合范围重叠，不相加。规格 v0.15，15 Feature、33 任务、四条完整 FLOW 的全目标验收状态不提升。


I051 图片保全最终实测：[XLS入库/Wiki/图文索引](evidence/office-image-preservation-I051-2026-10-05.json)。同一I050冻结XLS，原MinerU engine zip只有正文/元数据、0图片；新的PDF视觉保全记录绑定源PDF SHA、实际page/draw/bbox与PNG SHA，真实asset 0→1。保全来自PDF可见绘制区域，不伪造图意/章节归属，记录 `restored=true`、`model_extracted=false`、`model_quality_accepted=false`。正常模型资产仅在完整像素相等时去重，真实MD图片引用判定不被代码中的路径提及压制；源model body为空仍拒绝，不能靠补图绕过空产物门禁。

D1与W1分别物化恰好1张PNG。D1实际重建11条、0失败；按flowtest账户读取真实authority，image回执齐全且已published，真实VLM摘要/来源上下文与图文向量可检索到图片URI。W1捕获4篇MD、3候选到私有draft_ready，逐字引文与来源核验通过。这是合成用例的保全/索引合同，不能提升完整Office、三一诊断或gold质量状态。TeleOCR/Logics尚未启用该保全门禁；legacy CFB字体自省、完整章节归属与全卷图片质量仍待验收。

部署使用现有冻结Docker+挂载worker/隔离HTTP runtime。远端依赖下载两次挂起，最终从本机下载官方Pillow12.1.1目标Linux wheel，按官方SHA核验后传入离线安装；没有改pin。API启动快照曾因错误源路径失败，已把全部路径/hash验证前移到停止旧API之前并恢复成功，过程记录保留于图片部署证据。最终API pid99130，API/MinerU health均200，无active durable job。

本次live Office转换PDF的书签对齐失败，已明确回退Markdown标题并保留失败原因；不能据本次保全通过宣布章节gold通过，纯矢量图识别仍未验收。

## I053 FLOW-D1 语义分层修复、旧代码重启恢复与长跑判定（2026-10-06）

FLOW-D1（SY215C 1072页手册，685 章节 MD，root viking://resources/raw/SY215C_SY021NCB62788_维修手册_zh_1）在 index 阶段 attempt 攒到 320 后，两处定点修复 semantic_dag：移除 require_semantics 重抛门——configured_store() 非 None 即把目录侧车写入的任何异常当致命错误重抛，瞬态 LockAcquisitionError 会中止整个 reindex attempt；HEAD 契约是瞬态锁争用吞掉跳过向量化，AbstractOverviewFormatError 的无条件 re-raise 分支保留。文件摘要路径与 _vectorize_directory 对已消失源 NotFoundError 跳过不中止（重启日志实证：legacy 树 `维修手册_zh` 无 `_1` 后缀与 `履带式液压挖掘机/images` 的上百个幽灵图片引用被逐个跳过）。修复后 tests/storage 全组 42 passed（修复前 test_directory_vectorization_retries_after_matching_sidecar_write 1 failed）。

attempt=320 判定为合法长跑而非空转：8s/20s 双采样 attempt 冻结、lease 30s 窗口持续续约、根 .overview.md 与根锁文件 mtime 新鲜、ClashX 代理持有 ~100 条到 VLM 端点 10.147.254.3:36688 的 ESTABLISHED 连接、vectordb context 版本文件 5 分钟内有更新；回执表 654 恒定属预期（回执只在批次封口时落账，遍历中不增长）。

crash-recovery 实证：SIGKILL 运行中的旧代码服务器（PID 34476，启动于 01:56:50 早于 02:09:28 的代码修改），重启到修复后代码（PID 39698），D1 从 checkpoint 自动恢复——新发布批 f5b15712 分配、attempt 321、新日志 0 个 Traceback/ERROR 行。旧 building 批 a4d7ee06（0 成员）按 building→fail_batch 路径处理或留为孤儿，无重复代成员混入。 [证据](evidence/live-durable-d1-semantics-I053-2026-10-06.json)。

后台采样器每 120s 记录 D1 state/log 行数/回执数到 /tmp/d1-watch.log 至终态。D1 到 ready 前不做检索探针，头/中/尾分布断言与 FLOW-D1 完整验收仍未通过；不提升 Feature/FLOW 状态。
