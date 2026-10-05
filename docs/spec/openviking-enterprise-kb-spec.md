# OpenViking 企业知识库开发规格

> 当前执行状态：I051（2026-10-05）实际持久D2提交成员共同发布及published/backoff SIGKILL恢复通过：单MD 3/3回执，目录3 URI/9回执含L0/L1，恢复未重复建代；完整7门禁仍pending、不标ready。修复已注册空间显式/自动祖先摘要边界。MinerU原始产物丢图已确认并补真实PDF页码/位置保全，客户端冻结上传副本并校验来源指纹；同XLS D1/W1复测各保留1 PNG，D1真实重建11条/0失败、图文向量回执齐全且图片URI可检索，W1四MD/三私有候选到draft_ready。模型图片语义与章节归属质量未验收。最终受影响164 PASS，初测1项兼容失败保留；服务/解析health200、active durable jobs=0。15 Feature/四FLOW仍PARTIAL、33任务32 PARTIAL+1 OPEN、0 DONE，完整验收NOT_RUN。 [本轮报告](evidence/implementation-I051-2026-10-05.json)。

> I050执行状态：I050（2026-10-05）持久D1/D2新增整次reindex批次，真实member回执观测/崩溃恢复；封口在authority同一事务校验job owner/fence/lease，过期worker不得切head。提交成员可共同切换，但完整7类覆盖门禁仍pending，不放ready。旧DOC/XLS/PPT六项实际D1/W1文本/索引/溯源合同通过，私有候选3/2/4；确认XLS可见图在MinerU worker产物阶段缺失（源与PDF有1图，产物0asset/0引用），本轮Office证据整体不通过，复现资产已冻结。联合1061 PASS/1 SKIP，最后封口修改后85定向PASS（与40封口回归重叠）；新batch命令尚未加载线上，15 Feature/四FLOW均PARTIAL，完整验收不提升。 [本轮报告](evidence/implementation-I050-2026-10-05.json)。

> I049执行状态：I049（2026-10-05）新增冻结三一路径适配包与实际执行器：八种path_layout校验目录形状和本地字段对齐；候选验证及合并来源重新执行，回执绑定schema/声明/正文hash和collection root。新适配包未执行标记118→110，仅消除8个实际执行的路径标记，全部正文规则保留。七个原fewshot结构通过，DTC原kb://来源仍拒绝，未猜测映射。受影响191专项通过；当前服务/旧注册schema未改，原包仍118，完整strict诊断/发布/gold/FLOW未验收。 [本轮证据](evidence/implementation-I049-2026-10-05.json)。

> I048执行状态：I048（2026-10-05）修复图片引用深度超过8层时静默漏收，以及误用 embedding tokenizer 作为 VLM tokenizer 的问题。新增 VLM 专属固定 tokenizer 配置、主/备模型覆盖检查和文件指纹校验，截断后重新核验实际字符串预算；引用记录区分 measured/estimated，context 升级 v3。72 项受影响测试通过（含现有真实 VLM 非空摘要协议探针；非质量 gold）。代码已在本地验证，当前服务仍为 I047，未声明新部署链路通过。三个并行子任务因模型服务403中断，未增加其完成证据；共同发布、完整schema和legacy Office仍待实现/实测。15个Feature、四条FLOW及任务整体状态不提升。 [本轮报告](evidence/implementation-I048-2026-10-05.json)。
> I047执行状态：I047（2026-10-05）接通显式 SSH→冻结 Docker Office 转换，默认字体门禁开启；六格式转换合同通过，实际 D1 Word 上传→PDF→规则回退→7 条索引重建/0 失败→限定来源检索通过；W1 DOCX/XLSX/PPTX 三族最终均到私有 draft_ready（2/2/5 个候选），逐字引文核验通过。PowerPoint 原输出错误改写标题符号，v3 提示修复后通过；此前失败记录保留。上传新增原文件指纹，幂等重放不重解析/改源 epoch；已有树替换明确拒绝，整树提交仍待实现。持久 D2 保存真实索引计数并等待共同发布回执；ACL 更新过滤历史代并检测操作期间 head 变化。完整三一 schema 的 118 个待执行规则在实际 strict API 中明确拒绝、无作业创建。联合回归 1008 PASS/1 SKIP；最后修改后受影响范围 141 PASS、Office 59 PASS、上传重放 60 PASS（范围重叠，不相加）。这些是实现/合成合同证据；15 个 Feature 与四条 FLOW 仍 PARTIAL，32 项任务 PARTIAL + KB-032 OPEN，完整产品验收均未通过。 [本轮报告](evidence/implementation-I047-2026-10-05.json) · [Office W1 实测](evidence/flow-w1-office-fontchecked-I047-2026-10-05.json) · [完整 schema 拒绝门禁](evidence/wiki-strict-gate-I047-2026-10-05.json)。

> 历史执行记录：I046（2026-10-05）复核真实 W1 PDF 作业：job `1e73c293bb924638a3f9c4d027ee9fc0` 已正常到 `draft_ready`，耗时 479 秒，捕获 8 篇非空 MD，44 次模型调用、44 份私有草稿、42 个候选。通过实际 SDK/API 逐份读取，全部证据 URI 和引文逐字核验通过，未发布诊断知识。原验收脚本约 300 秒观察窗口过短；现支持继续验收已有作业、默认 1800 秒观察窗口、全部草稿计数/来源/私有回执/逐字引文校验。PDF/Office 路由与章节/Wiki 回归 70 PASS，验收脚本 8 PASS。简化 business/Thing schema 下的切片执行证据不能替代完整三一 schema、全卷质量或发布验收。15 个 Feature 均 PARTIAL，33 项任务为 32 PARTIAL + 1 OPEN，四条 FLOW 均 PARTIAL；完整产品验收尚未通过。 [W1 PDF 实测](evidence/flow-w1-pdf-2026-10-05.json) · [继续验收报告](evidence/flow-w1-pdf-resumed-I046-2026-10-05.json)。

> 历史执行记录：I038（2026-10-05）补录 KB-029（多源增量/证据撤回/投影重建）实现为 PARTIAL（44项通过）：来源撤权即拒绝读取、仍有合法来源时同身份重建并丢弃撤回贡献、跨域合并拒绝、人工值CAS保护；零证据stale/权威边投影重建/迁移回滚仍待实现。I037 定向验证 reindex 语义分层契约（17项通过）；I036补录 KB-025 为 PARTIAL（44项）。仅 KB-032（四 FLOW 上线验收）保持 OPEN；15个Feature与四条完整FLOW仍NOT_RUN，完整仓库测试未执行。 [I038证据](evidence/implementation-I038-2026-10-05.json)。

> 版本：v0.15 · 2026-10-05 · 状态：规格持续迭代，功能实现与验收进行中。
> 本文定义目标与接口契约；[测试与达成追踪](openviking-enterprise-kb-test-cases.md)定义验收；[审查记录](openviking-enterprise-kb-review.md)记录证据与修订原因。
> v0.6 / CR-006：按用户决定，对齐失败自动走 Markdown 标题与规则切分并记录任务 info；溯源只要求 Viking URI。补齐文档级索引代次、查询快照、GC依赖保护和队列释放。新增字段、接口、状态机均为**拟实现契约**，不是当前 SDK 的可运行说明。已有能力按源码核对，不能据此宣称端到端通过。

> v0.7 / CR-007：解析模型参数统一命名为 `model_mode`，避免与现有 `args.parse_mode=default/no_split` 的正文组织策略冲突。记录三后端部署及有限 E2E，补充取消传播/迟到结果发布约束；完整企业验收仍待执行。

> v0.9 / CR-010：I026增加显式结构门禁与模板/引用对象编译，补共享政策和路径未执行台账；能力读取复核当前编译覆盖。结构通过不等于Wiki诊断发布通过。

> v0.10 / CR-011：统一当前实现与目标契约，建立[未达成任务清单](openviking-enterprise-kb-backlog.md)和机器可读台账；15个Feature完整验收仍NOT_RUN。配置/来源计划可用不等于企业知识构建已上线。

> v0.12 / CR-013：修正台账陈旧状态——KB-025（实体身份、保守合并和人工编辑）此前记为 OPEN 且无证据，实际 `openviking/wiki/identity.py` 已实现
> namespace/type/schema 身份元组、canonical ID 跨重建复用、collision-aware 别名归属、expected_revision 人工编辑保护与默认不近义强并，14项身份测试通过；本轮补录证据并置为 PARTIAL。语义身份验证/本体政策/可信实体目录仍 pending。

> v0.11 / CR-012：固定来源抽取器由“整篇单次调用”改为“拆解任务→分发抽取”（按Markdown标题分段、超长段再切分、逐段词法kind短名单筛选、每段一次有界结构化调用），每次调用不再携带原始schema包，避免上下文打爆；新增独立`wiki`模型段与`get_wiki_model()`（未设置回退`vlm`），远端`svc/deepseek-v4-flash`真实模型探针CONTRACT_PASS。完整Wiki诊断发布仍NOT_RUN。

> v0.15 / CR-016：为目标第1项补录 I039 真实手册三后端解析证据。取真实 SANY 维修手册（1072 页、379MB、491 条书签）第 21–31 页切片（11 页、含 6 条 TOC）经 `ParseModelBackend.parse` 依次送 mineru/teleocr/logics，三者均 11/11 页完整、后端 markdown 互异、图片资产签名全部有效（mineru 25/25、teleocr 22/22、logics 15/15），KB-009/KB-010 追加证据。全卷 1072 页、章节目录落盘、解析后 reindex 与 D1 产品验收仍 NOT_RUN，F1 保持 PARTIAL。

> v0.14 / CR-015：修正台账陈旧状态——KB-029（多源增量、证据撤回和投影重建）此前记为 OPEN 且无证据，实际 `openviking/wiki/merge.py` 已实现逐候选来源依赖溯源（source_uri/sha256/epoch/build/plan）、`read_merge` 重校来源 ACL/export 与 epoch（撤权即拒绝读取，不回退剩余来源）、仍有合法来源时用同一 canonical_id 重建并丢弃被撤回来源贡献、跨域合并拒绝、人工确认值受 expected_revision CAS 保护；44项测试通过。本轮补录证据并置为 PARTIAL。零证据 stale/移出热路径、权威边投影重建、迁移回滚仍 pending。

> v0.13 / CR-014：为目标第3项（语义分层须在 reindex 中重建）补录 I037 定向证据。D2 `reindex` 两模式契约：`vectors_only` 只重建 chunk/BM25/图片向量，绝不重写已发布目录 L0/L1；`semantic_and_vectors` 自底向上重建目录 L0/L1 与图片摘要，`planned_semantic_levels` 语义模式计 2×目录数；两模式均不把联合图片记录降级为纯文本摘要向量，缺有效图片摘要返回 `SEMANTICS_REQUIRED`；原 MD/目录不改写、parser 调用 0 次、不读 `_toc.json` 重切。`tests/enterprise_kb/test_reindex_contract.py` 17 项通过。完整 D2 端到端产品验收与四 FLOW 仍 NOT_RUN。

## 0. 目标、范围与决策

目标：形成可用于诊断维修的多源企业知识库。PDF/Office/已有 Viking Markdown → 串行解析与持久化 → 可重建的文本分层索引、图片上下文摘要与联合向量 → 混合检索、可读回的引用；在同一 VikingFS 上实验性构建可维护的实体 Wiki。

| ID | 必须遵守的目标 | Feature |
|---|---|---|
| R01 | MinerU、**TeleOCR**、Logics-Parsing-V3 三后端均有适配、Docker 部署和独立质量验收；TextIn 不是 TeleOCR | F1 |
| R02 | 优先书签树与模型内容对齐；失败自动按 Markdown 的 # 标题和规则切分，任务 info 注明对齐未成功；页码仅 meta | F2 |
| R03 | 用户指定父 URI，追加文件名目录；图片按引用章节就近放置；先写入再 reindex | F2,F3 |
| R04 | 一本一本解析、队列/日志/前端可观测；失败能恢复，不无限保留原件 | F3,F13 |
| R05 | 长文本子母 chunk，使用配置的 embedding；溯源到可读 Viking URI，不要求精确位置；已有 md 多 chunk 均溯源原 URI | F4,F9 |
| R06 | 图片摘要结合章节与邻近正文，**摘要+原图联合 VL embedding** | F5 |
| R07 | 真正 BM25 + dense、rerank 模型、MMR；导航失效仍保留全局叶子召回 | F6 |
| R08 | 可选业务 meta；支持不准确表达的规范化、别名、前缀/子串/错字候选匹配 | F7 |
| R09 | Word/Excel/PowerPoint 先转 PDF，再走统一解析链路 | F8 |
| R10 | 指定源 URI 和目标 Wiki namespace；用户定义 Markdown 实体模板；同一套检索与 VikingFS | F10,F14 |
| R11 | reindex 能重建 L0/L1 和 L2 chunk、图片语义，初次索引与重建契约一致 | F11 |
| R12 | JEV 式状态与类型化决策预研，不能把检索分数冒充故障概率 | F12 |
| R13 | 需求、设计、实施步骤、测试、达成证据随变更同步更新；构建期生成评测候选 | F15 |

保留已明确的选择：不引入独立图库；原件不作永久知识资产；图片按章就近；Office 转 PDF；长 md 必须分层 chunk；`_toc.json` 为解析时产生的元数据存档，reindex 不用它重切章。

范围分两层：M1 优先验收**带书签 PDF，并同时具备对齐失败的自动回退**；回退器也处理无书签的模型输出，无书签 PDF 的全面质量评测和 Office 放 M1b，不能宣称已支持所有 PDF。Wiki 与 JEV 为 experimental，不阻塞核心入库检索交付。人工标注仅用于离线质量评测，不是章节入库的等待步骤。

## 0.1 四个主入口与共享处理链（CR-008）

以下四条是产品的主验收链路。文档解析入库负责来源组织和索引，Wiki ingest 负责按实体模板抽取、归一与关系构建。章节树是可读文件结构；父子 chunk 是该结构上建立的检索索引，两者不能互相替代。

| 入口 | 用户输入 | 处理链 | 结果与来源 |
|---|---|---|---|
| D1 文件上传入库 | 文件、可写 `parent` URI 前缀、解析后端/模式、可选 meta | 暂存 → Office 转 PDF（如适用）→ 解析 → PDF meta 书签与正文对齐或自动回退 → 写入结构树 → 共享 IndexPlan → 索引发布 | `parent/<文件名>/` 下章节 Markdown、同级图片；匹配引用对应章节 Viking URI |
| D2 已有 URI 索引 | 文件/目录 Viking URI、索引策略、递归范围 | 读取已有 Markdown/图片 → 共享 IndexPlan → 分层 chunk 与 embedding、L0/L1 语义层 → 索引发布 | 保持原文和原目录；每个 Markdown 的多个 chunk 均引用其原 Viking URI |
| W1 文件上传构建 Wiki | 文件、`collection_id`、可选构建参数 | 查询 collection 绑定的 schema → 暂存/统一来源入库 → 共享 WikiBuildPlan → 实体归一、关系构建与校验 → 写入实体 MD → 同一索引体系发布 | collection 指定目录下实体 Markdown 与关系；事实来源按 schema 字段存储，统一响应 `sources[]` 指向已持久化来源 Viking URI |
| W2 已有 URI 构建 Wiki | Viking URI/URI 列表（含目录）、`collection_id` | 校验读源/写目标权限 → 读取来源 → 共享 WikiBuildPlan → 同 W1 的构建、存储、索引与发布 | 同 W1 的实体和关系格式；来源引用保留输入文件 Viking URI |

D2 对已有 Markdown 不再次调用 PDF/OCR、不重新组织章节、不把每个 chunk 写成替代源文件，也不依赖 `_toc.json` 重切分。二进制 PDF/Office URI 不是 Markdown 索引输入；需显式走解析入库路径，不能把其字节当文本 index。W2 对已有 Markdown 同样跳过文件上传和 OCR。两个 Wiki 入口必须调用同一个构建器，不新增第二套向量检索、存储或实体归一规则。

**collection 是业务知识库标识**，不是向量引擎的物理 collection。注册项至少绑定 `(account_id, collection_id)`、`target_namespace_id`、`target_uri`、`raw_sources_prefix`、`schema_id/schema_version`、成员/权限与索引策略。用户先注册/选择 collection 并上传 schema Markdown 模板，再用 collection 启动 W1/W2；服务端从注册项确定 schema 与目标隔离域。一个 Wiki collection 对应一个独立 Wiki namespace，多个来源可进入同一 collection。不同事业部默认各自 collection/namespace；共享读取或跨域导出必须显式授权，不能仅靠路径隔离。原文域和 Wiki 域可以分别注册，不能用 collection 名绕过来源 ACL。

W1/W2 不要求用户重复提交可能冲突的 namespace、target_uri 和 schema；若为兼容接口允许这些字段，必须与 collection 注册项一致，否则返回 409。未配置 schema、schema 模板无效、源不可读、目标不可写，在调度昂贵模型之前拒绝；`SCHEMA_NOT_CONFIGURED` 等属于拟实现错误码。任务创建时固定 schema 版本及模板摘要，运行中注册项更新只影响后续任务。schema 是实体格式/约束模板，不是待抽取的知识来源。

W1 的来源持久化是 Wiki ingest 内部复用的文档入库阶段：PDF 和 Office 使用同一解析器，上传 MD 直接保留内容与相对图片。来源固定到 collection 的 `raw_sources_prefix/<文件名>/`；原件仍只作有界临时输入。事实必须有可读来源 URI。实体/关系发布前复用 §8 引用校验、别名归一、冲突策略与八实体诊断链；缺失当前 schema 所需的关系承载、悬空引用或解析异常不能冒充已验证诊断事实；无配置档时是否允许来源断言边，由固定 schema 决定，见 §8.2。

四条最终执行链统一返回job，遵守既定写入/索引阶段与发布屏障；当前Wiki来源准备接口仍返回prepared计划，未来独立执行接口见§9.2。文档已写入但索引未就绪不能宣称 ready。章节对齐失败自动按 Markdown AST 标题→块规则切分，记录 `info/alignment_status/structure_source`，不阻塞人工审核。外部溯源只要求 Viking URI，不要求页码、字符位置或 `#chunk` 地址。

### 主入口 API 与 SDK 契约

下表列最终四入口契约。当前实现：I033新增`POST /api/v1/kb/ingestion-jobs`实验执行接口，显式namespace+kind(D1/D2)+payload+options，SDK为`create_ingestion_job/get_ingestion_job/cancel_ingestion_job`，与下表最终入口仍需统一；已接资源写入/reindex但无完整ready屏障。W1/W2接口已接入来源准备，当前HTTP 200返回prepared计划而非202 job。当前可调用面与迁移要求见§9.1；不能将目标参数直接用于当前SDK。

| 主入口 | HTTP | Python async/sync SDK |
|---|---|---|
| D1 | `POST /api/v1/kb/ingestions`：`temp_file_id, parent, parse_backend, model_mode, meta`；namespace 由授权注册根解析，显式值必须一致 | `client.kb.ingest(temp_file_id=..., parent=...)` |
| D2 | 扩展现有 `POST /api/v1/content/reindex`：`uri, recursive, mode=semantic_and_vectors, index_profile` | 扩展现有 `client.reindex(uri=..., ...)` |
| W1执行目标 | `POST /api/v1/kb/wiki/ingestion-jobs`：文件/collection及固定执行profile；当前`/ingestions`只准备单MD | `client.kb.start_wiki_ingestion(...)`（待实现）；当前`ingest_wiki(...)`只准备 |
| W2执行目标 | 当前`POST /api/v1/kb/wiki/builds`先准备；待实现`POST /api/v1/kb/wiki/builds/{build_id}/execute` | 当前`build_wiki(...)`准备；待实现`start_wiki_build(...)`执行 |
| collection 配置 | `POST /api/v1/kb/wiki/collections`；schema 注册沿用 §9 | `client.kb.create_wiki_collection(...)` |

上传文件 ID 和 source_uris 不能同时出现在同一入口请求；批量上传由同一批任务管理逐文件处理，不改变文档串行解析要求。D1 的 parent 是路径前缀，沿用旧接口 to 为精确目标的语义。W1 的 collection 决定输出位置，W2 的 source_uris 决定输入位置，两者不能混为一个参数。

### 四入口使用约定（CR-009）

1. 文档上传：上传文件，指定可写 parent URI 前缀，生成文件名目录和章节结构树，再建立索引。
2. 文档 URI：指定已有 Markdown 文件或目录 URI，直接建立分层 chunk、文本/图片 embedding 和语义层；源 URI 保持不变。
3. Wiki 上传：上传知识文件并选择 collection；服务端读取该库固定版本的 Markdown schema，将来源写入 raw_sources_prefix 后按模板构建实体和关系。
4. Wiki URI：选择 collection 并指定来源 URI，按同一 schema 和同一构建器生成 Wiki；不要求重新上传来源。

D1/D2 共用 IndexPlan；W1/W2 共用 WikiBuildPlan，并将 Wiki 实体交给同一索引体系。用户无需另外选择第二套检索。source_uri 是来源，parent 是文档输出前缀，collection 是 Wiki 配置标识；三种参数不能混用。结构树、实体文件和检索 chunk 分别承担来源组织、知识表示和检索，不应在 UI 上把已配置 collection 显示为已构建 Wiki。

### 当前达成情况

D1 已有解析路由、章节/回退、HTTP/SDK 参数及远程模型有限验证；D2 已有 URI reindex，I004 新增显式启用的共享文本父子分块、子块向量写入与同 URI 多命中保留。两条已部分实现共享父子 chunk、文本/目录/图片代次共同发布、引用语义缓存和标准写入保护（I004–I016）；完整企业发布、图文质量及混合检索仍未验收。I023/I024 已实现内部 collection/schema 持久化、配置 HTTP/SDK 及两入口共用的不可变选择；I025 已接入两个 Wiki 输入共用的持久来源准备，固定 schema 和 Markdown 版本；I027已实现知识根/export配置与内部来源使用检查，I028–I030已实现W1/W2准备HTTP/SDK和计划查询；全入口/执行提交授权、持久任务执行、抽取/关系发布及完整W1/W2仍待实现。四条完整 E2E 均 NOT_RUN；本规范定义完整目标，不宣称线上高度可用。

## 1. 已有能力与真实差距

核对基线：OpenViking HEAD `e8d634c88fa96ca589009355c54dca98295d04fd` 加当前工作区。存在其他未提交修改；当前工作区含 I001–I030 部分实现；本节历史基线与下述最新实施状态一起阅读。路径相对仓库根，函数名优先于易变行号。

以下为截至I030的当前实现摘要，替代早期差距表；历史I编号只证明对应局部范围。完整产品验收均NOT_RUN，具体交付与阻塞任务见台账。

| Feature | 实现状态 | 当前证据与剩余缺口 | 剩余任务 | 完整验收 |
|---|---|---|---|---|
| F1 | PARTIAL | I001–I003/I018：三模型12页及MinerU全卷；全后端质量/正式job链未验收 | KB-001,KB-009,KB-010 | NOT_RUN |
| F2 | PARTIAL | I001/I003：书签meta、自动回退及全卷章节产物；gold结构与完整发布待验收 | KB-006,KB-010,KB-011 | NOT_RUN |
| F3 | PARTIAL | 任务/UI/worker健康基建；企业持久job、共同租约与恢复未闭环 | KB-004,KB-021 | NOT_RUN |
| F4 | PARTIAL | I004–I008：tokenizer父子计划、多chunk与代次屏障；真实完整搜索待验收 | KB-006,KB-013 | NOT_RUN |
| F5 | PARTIAL | I011–I015：引用上下文/缓存/joint实际向量及共同发布；真实VLM质量、多空间搜索与引用分页未完 | KB-006,KB-014,KB-015 | NOT_RUN |
| F6 | PARTIAL | I003/I032：BM25词法索引（FTS5 local_fts5）与RRF→rerank→MMR融合单测通过；全量质量消融未闭环 | KB-016,KB-017 | NOT_RUN |
| F7 | PARTIAL | I032：权威业务meta与模糊字典/别名（模糊匹配非强匹配）单测通过；跨后端语义与性能验收未完成 | KB-018,KB-019 | NOT_RUN |
| F8 | PARTIAL | I032：Office→PDF→统一解析链路契约单测通过；真实LibreOffice容器全类型质量待验收 | KB-012 | NOT_RUN |
| F9 | PARTIAL | 已有URI入口与共享计划；原URI完整图文/搜索/授权E2E待验收 | KB-013,KB-020 | NOT_RUN |
| F10 | PARTIAL | I020–I030：模板/结构门禁/catalog、W1/W2准备与查询；完整规则、抽取、关系/实体发布和增量未完 | KB-022,KB-023,KB-024,KB-025,KB-026,KB-027,KB-028,KB-029,KB-030 | NOT_RUN |
| F11 | PARTIAL | I009–I016：目录/图文语义与版本保护；根可见性、词法/权威meta及job屏障待完成 | KB-006,KB-020 | NOT_RUN |
| F12 | PARTIAL | I032：JEV式状态/类型化决策PoC（openviking/jev）状态机与决策路径单测通过；独立校准PoC未运行 | KB-033 | NOT_RUN |
| F13 | PARTIAL | 持久发布/依赖根/写入保护与部分幂等；恢复、删除/物理GC/配额/备份未闭环 | KB-002,KB-004,KB-005,KB-006,KB-007,KB-008,KB-029 | NOT_RUN |
| F14 | PARTIAL | I027知识根/export配置及局部门禁；全入口预过滤、身份刷新与提交fencing未完成 | KB-003,KB-022,KB-027 | NOT_RUN |
| F15 | PARTIAL | 候选及局部测试/证据已保存；独立gold、生产性能和完整FLOW验收未完 | KB-010,KB-031,KB-032 | NOT_RUN |

重要边界：

- `pdf.py` 在调用点先取 `dest[0]`；若 `dest` 为命名目标 bytes，首字节被当页索引。`b'pdflink…'[0]=112` 会返回 113。属于应用代码类型误判，不能称 pdfplumber 普遍失效。采用 PyMuPDF 只读书签并保留兼容性测试；不因此替换内容模型。
- `generate_image_summary` 当前传入 `No additional context`；图片向量输入已存在，缺的是章节上下文和整条重建链路，不能写“从零做图片 embedding”。I011/I013已验证实际joint/query请求得到2560维有限向量，只适用于记录的模型profile；VLM真实上下文质量和完整检索仍未验收。
- I003 已移除 THINKING 全局 L2 `leaf_results` 的 ACL 开关条件，仍通过同一 tenant proxy 和 scope 过滤；导航无结果时保留叶子候选。ACL on/off 定向回归通过，不代表真实授权域/混合检索端到端验收。
- sparse embedding 不等于 BM25；有远程 grep 的 BM25 能力不等于混合搜索已满足本目标。
- `Contains` 在不同存储适配器下实现不同，例如 cuVS 做 substring 判断，Qdrant 转 text 条件。C++ 已有 prefix/regex，但存在算子不证明通用高效模糊检索。不能把此项写成“引擎完全不支持前缀/正则”。
- 资源 reindex 有角色限制：当前 USER 只可重建其个人私有域。企业知识域管理员/写入者的授权契约需扩展，不能由 SDK 绕过检查。

## 2. 身份、namespace 与数据生命周期（F13/F14）

### 2.1 知识域和 URI

`account_id` 来自认证上下文；在 account 内增加显式 `namespace_id` 注册项，绑定用户提供的 `root_uri`、成员/角色、schema 与索引策略。**目录分开只是组织结构，互不可见由授权保证。** 不强制在用户 URI 中拼接事业部字符串。

示例：`namespace_id=excavator-manuals` 注册到 `viking://resources/raw`；`namespace_id=excavator-wiki` 注册到用户指定的 `viking://resources/wiki/excavator`。同 account 下隔离 namespace 的根不允许嵌套重叠；共享读通过显式 grant 表达。跨 account 的同名 URI 仍不同。

新企业入库 API 要求 `parent`；服务端从唯一授权注册根解析 namespace，可显式提交 `namespace_id` 并校验一致性，且 `parent` 规范化后属于注册 root；最终根为 `parent/<safe_filename_stem>/`。沿用现有 API 的语义：`parent` 是父目录，`to` 是精确目的地，两者互斥；禁止把旧 `to` 悄悄改成父目录。只允许受支持、可写的资源域，不能把任意 `viking://` URI 当可写目标。

授权范围必须进入读取/list/grep/下载、BM25/dense、导航、图片读回、关系遍历、队列、日志、缓存、导出与 reindex。相似匹配只针对业务 meta，不能用于 namespace/account/ACL。缓存键包含授权版本和知识域。路径按规范化后的段边界比较（`/a` 不包含 `/ab`），拒绝 `..`、编码穿越与跨域图片引用。

### 2.2 文档与发布版本

- `document_id`：服务端稳定 ID，不等于文件名；`content_revision` 为内部内容版本，`index_generation` 为**文档级**索引代次，唯一键为 `(account_id, namespace_id, document_id, index_generation)`。文件名用于可读目录；这些版本字段不构成用户溯源参数要求。
- 同名不同内容默认 `409 TARGET_EXISTS`；显式 `replace` 必须提供 `document_id` + `expected_revision`。不静默混入原目录、不自动覆盖人工编辑。
- 幂等键作用域是 `(account_id, namespace_id, idempotency_key)`；相同键与不同请求摘要冲突返回 409。相同内容/参数/版本重复请求返回原 job；换后端/模型/分块版本视为新任务。
- 章节标题标准化需防路径注入、Unicode 等价碰撞、过长名、保留名。冲突加稳定节点 ID 后缀，节点 ID 不仅由标题生成。文档更新保留旧章节 URI 映射，不能悄悄让旧引用指向另一章。
- 正文/图片/元信息在 staging 完成覆盖校验后持久化，先落盘再建索引。新文档 `written` 时可通过 URI 读取但尚未进入搜索；替换文档则持久保存新 revision，公开 URI 继续读旧的已发布内容，待新索引完成后一起发布。旧数据可用时重建失败不影响原有查询。
- VikingFS 内部保留有界 revision 目录，公开 URI 映射到当前发布的内容；用户直接按 URI 读取，无需传版本、位置或哈希。URI 表示来源文档/章节，不承诺长期固定在某历史片段。已存在的外部 md 不改写，重建时捕获内部一致性版本；检测到外部编辑后排除不匹配的旧索引并标 `index_pending`，直到新代发布。
- namespace 使用不可变 `snapshot_id` manifest，内容包含 `documents[document_id] = {content_revision, index_generation, embedding_spaces}` 和目录摘要版本映射。重建 A 只替换 A 及受影响的祖先摘要，**继承 B 等未修改文档的映射**。索引记录携带 document_id/generation；不得用单个 namespace 最新 generation 等值过滤整库。BM25、向量和 meta 索引按此映射限定可见记录，再计算候选排名。
- 新文档级索引全部校验后，CAS 切换 namespace 的 active snapshot，并通过同一 manifest 更新受管理文档的公开内容投影。构建在锁外执行，发布只持短租约；CAS 冲突要在最新快照上重合并，保留其他任务已发布的文档；受影响祖先依赖改变则重算摘要，不覆盖并发更新。
- 一次查询开始捕获 snapshot 并取得读租约，BM25、dense、导航、parent扩展和结果组装都使用该快照。跨 namespace 查询固定各自 snapshot 组成的查询视图；权限撤销和删除 tombstone 在返回前按最新状态复核，快照不能绕过撤权。查询期间切代不得混用前后两代；租约直到响应组装完成才释放。

I006–I016 已部分实现可配置的正文/图片 reference、L0/L1 sidecar、generation 与 SQLite 查询快照及多来源批次屏障；producer seal 且全部 member receipt 后共同发布。标准写入覆盖存储 await 的持久 token 与 epoch 在捕获、begin、publication 复核。完整文档根 layout/list/stat、BM25、业务 namespace、其他修改入口、跨主机与异常恢复、物理 GC 和任务 ready 联动仍待实现，不能代替本节完整快照契约；详见最新实施记录。

- 文件系统与向量库非同一事务：持久 outbox + 幂等 upsert + 文档代次可见性过滤 + 恢复扫描实现发布屏障。解析暂存也必须服从授权。版本控制用于内部一致性和回滚，不新增面向用户的精确位置溯源协议。

### 2.3 原件、恢复与删除

不永久保留原 PDF/Office。上传临时件仅活跃任务及有界重试期可用，默认失败保留 TTL 24 小时、完成解析结果持久化后清除，实际 TTL 可配置且在任务显示。保留的知识资产为章节 md、必要图片、书签 sidecar、来源摘要/哈希与版本 manifest；不保留整份 OCR 调试文件、整页渲染副本或另一份永久全文。

恢复分能力：后端支持 checkpoint 才承诺页/窗口续算；Logics 状态化窗口恢复必须保存兼容的结构状态。无 checkpoint 后端允许在暂存 TTL 内重新执行解析，明确 `resume_granularity=document`。解析结果已持久化后，embedding/reindex 失败直接读 Viking 产物重试。原件已清理且需要换解析模型时返回 `SOURCE_REQUIRED`，不承诺无原件重解析。完成或取消后清理临时件；TTL worker 处理崩溃遗留。

源删除/替换必须传播到 chunk、BM25、图片向量、摘要、meta 候选索引、缓存和 Wiki 来源引用。Wiki 被多源共同支持的事实保留剩余证据；无证据事实标为 stale 待审，禁止继续在诊断热路径当有效知识。删除先写 tombstone，阻止新查询和旧快照继续返回该来源，再执行异步清理。

GC 的保留依据是**依赖可达性＋保留期**：active snapshot/索引、未结束查询租约、有效重试任务、未完成 Wiki 构建所引用的内部正文版本、图片、parent块、L0/L1与索引段都必须保留。重建持续失败时旧 active 代及其依赖不因TTL到期而清理；读取租约不是唯一保护条件。发布新快照后，旧依赖不再被上述根引用，且保留期到期，才允许回收；共享图片按实际引用回收。GC 标记/清理与发布采用互斥或删除前CAS复核，不能清理刚被重新引用的数据。查询/任务租约有超时回收机制；受保护资产占用超限时停止接收入库并告警，不能强删 active 数据。永久保留的是当前有效知识资产，不是原始上传文件。

备份覆盖 md、图片、manifest、namespace/ACL/schema；索引可重建，必须做恢复演练。

### 2.3.1 写入结果未知的恢复契约（拟实现，F13.3）

对取消、传输超时和进程中断的写入，文件当前存在、连续两次 hash 相同、客户端任务已结束、等待 TTL、取得普通路径锁或容器已重启，都不能单独证明旧后端请求不会继续执行。禁止用这些条件自动清除 writing/uncertain。当前实现只保留保护标记，恢复执行器/API 尚未实现。

实施顺序：存储适配器先提供可验证的写操作身份和 terminal receipt，或提供服务端执行的 writer fencing（旧 writer 即使恢复也不能再修改目标）。能力不满足时保持 WRITE_RECOVERY_REQUIRED，并显示受影响来源 URI、原因和恢复步骤；旧发布版本可继续读取，不无限自动重试。外部存储恢复不得把用户提交的 completed=true 或任意 proof 字符串视为后端证据。原件不永久保留的政策不因此改变。

具备上述能力后，恢复器在来源写权限及域管理授权下执行：固定 account/URI/目标 token 集合和 epoch → 调用可信后端适配器确认目标操作 terminal 或已被 fence → 授权读取最新工作内容，校验内容 hash → 同一 SQL 事务 CAS 复核 token 集合、epoch 与后端身份并记录审计、推进 epoch、仅解除已确认的 token。CAS 期间新增 writer 或内容变化则放弃并重新验证；不得解除其他 writer，也不得直接发布失败构建或复活取消任务。随后从最新持久内容建立新 generation、重新走全部 receipt 和发布门禁。授权/后端验证/读取/审计任一步失败都保留标记。跨主机必须共享同一权威 writer registry，不能靠各自 SQLite 猜测。

恢复任务应有幂等键、操作者/可信适配器/操作 ID/确认时间/内容 hash/旧新 epoch 和结果审计，不保存凭据。只读诊断与真正执行恢复分开，接口命名在具备后端能力后确定；不得把未实现契约写成现有 SDK 使用方法。TC-13.12–13.16 对上述安全与可恢复行为独立验收。

## 3. 解析模型与 Office 适配（F1/F8）

### 3.1 统一适配结果

新增 `ParseModelBackend`，避免与现有 `ParserBackend(internal/understanding)` 重名。输入为受控临时文件句柄、解析选项和取消/超时上下文；不要要求把 379MB 文档一次性读入内存或 JSON base64。

输出 `ParsedDocument`：

| 字段 | 契约 |
|---|---|
| `backend/version/model_revision/options_digest` | 可复现来源，包含模式与模型版本 |
| `blocks[]` | 有序 `block_id/type/text_or_markdown/heading_level/asset_refs`；位置可作内部调试元信息，不要求对外暴露；table、formula、code 不随意打断 |
| `assets[]` | 图片 ID、字节/受控路径、MIME、hash、引用 block；bbox/page 元数据可空 |
| `markdown` | 模型内容的标准化视图；不得用书签标题补造缺失正文 |
| `warnings/coverage` | 缺页、空输出、截断、图像缺失、能力降级；失败不可伪装为空成功 |
| `checkpoint` | 可选后端页/窗口/结构状态；声明支持级别 |

渲染 PDF 页、裁图属于内容适配器的图像 I/O，可以使用 PDF 工具；**不能将书签页码变成章节内容的强匹配条件**。块输出和 Markdown 无页码也必须能完成章节对齐。

### 3.2 三后端的真实适配边界

官方来源已核对于 2026-10-03；部署时锁 commit、权重 revision、镜像 digest 和依赖，不使用浮动 latest。以下是集成要求，不表示服务器已部署。

| 后端 | 官方入口与产物 | 必须完成的适配与验收 |
|---|---|---|
| MinerU | [官方仓库](https://github.com/opendatalab/MinerU)、[输出说明](https://opendatalab.github.io/MinerU/reference/output_files/)；存在 md、结构化内容与图片产物 | 锁定版本的 HTTP/CLI 参数；显式处理全页范围/分页续读，避免默认只解析部分页面；按锁定输出 schema 归一 blocks、表格、公式与图片，不能混用不同大版本字段 |
| TeleOCR | [官方仓库及用法](https://github.com/caipeng328/TeleOCR)、[infer.py](https://github.com/caipeng328/TeleOCR/blob/main/infer.py)、[engine.py](https://github.com/caipeng328/TeleOCR/blob/main/TeleOCR/engine.py)；CLI 可读图/PDF；engine 写 md、middle JSON 和 images | `Detection`/`Segmentation` 能力映射，提取阅读顺序、标题候选、图引用；部署服务包装 CLI/engine，不能假设已有兼容 OpenAI 的“文档解析 API” |
| Logics-Parsing-V3 | [官方仓库与 Quick Start](https://github.com/alibaba/Logics-Parsing)、[inference_v3.py](https://github.com/alibaba/Logics-Parsing/blob/main/inference_v3.py)；`markdown.md/hierarchy.md/raw_output.dsl/result.json` | 按顺序保留跨页结构状态；hierarchy 作为内容侧辅助，不覆盖 PDF 书签树；用真实输出确认 bbox/图片是否完整，必要时在适配器补裁图与引用关联，缺图不能算全能力通过 |

模式不伪装成三个模型具有相同参数：对外 `model_mode=auto/text/ocr/layout`，后端 capability 描述支持集合和实际映射；TeleOCR 原生 layout_mode、Logics window_size 等放有 schema 的 `backend_options`。不支持的组合返回 `422 UNSUPPORTED_PARSE_MODE`。不指定后端使用 namespace/服务配置默认值，返回 `effective_backend`；外部后端失败默认失败，只有显式 `fallback_policy` 才能降级并记录原因。内置 pdfplumber 可保留兼容路径，不抵消三模型验收要求。

`model_mode` 是解析模型能力选项；现有资源入口的 `args.parse_mode` 仅指 `default/no_split`，不得混用。已注册 PDF 模型作为配置默认时，UnderstandingAPI 不得旁路统一产物校验与章节流程。

### 3.3 Docker 实施与模型验收

部署目标按用户指定：`10.147.254.3`，账号 `t-wanghai`，根目录 `/home/t-wanghai/models`。凭据通过受控环境配置，不写入 spec、镜像、日志或仓库。

实施顺序：只读检查 GPU/驱动/CUDA/显存/磁盘与已有容器 → 三个独立镜像及模型卷 → parser gateway/worker → 健康检查 → 小样 → 完整 SY215C → 输出适配/恢复/性能评测。不同后端依赖不共用同一 Python 环境。GPU 不足时可顺序加载，不能臆定三模型同时驻留。

交付 `compose.yaml`、Dockerfile/锁依赖、权重清单、capabilities JSON、启动/健康/重启方法、端口与资源限制、fixture 输出和测试报告。官方无现成 Docker/API 的部分由我们封装；不能用“模型能推理”代替“可恢复的文档服务”。

运行取消须传播到模型子进程/常驻引擎；不能仅更新包装层 DB。完成发布必须以 running 状态为前提原子执行，取消后到达的产物不可把任务改回 completed；异常也不得覆盖 cancelled。失败/取消输入按生命周期清理，运行中资源需等实际执行退出后释放。包装层已发送 cancel 或引擎返回 canceled，不单独证明 GPU 请求全部排空；此项仍需故障注入和资源观察。

实施证据：[I002 三后端 12 页部署联调](evidence/deployment-I002-2026-10-03.json)、[I003 当前验证](evidence/implementation-I003-2026-10-04.json)。I003 的真实 MinerU→章节落盘验证使用磁盘测试存储，不等于生产 AGFS/向量/发布验收；完整手册任务结果需单独记录。

### 3.4 Office 转 PDF

Word/Excel/PowerPoint 的 `.doc/.docx/.xls/.xlsx/.ppt/.pptx` 列入支持矩阵。LibreOffice headless 在隔离容器、独立用户配置目录运行，固定版本/字体、禁宏、超时与资源限制。转换后复用 F1，不走 AnyDoc→md 静默替代。

Word 优先保留标题导出的书签；PPT 无书签时用 slide 标题/顺序辅助模型结构；Excel 每张工作表记录名称和打印范围，长表分页需重复表头、保留单元格值和单位，禁止“缩成一页”造成不可读。不能保证 PDF 保留公式计算逻辑，因此目标是已渲染内容的知识检索，非电子表格执行语义。

转换后无书签自动采用 §4.1 的 Markdown 标题→规则切分，标明实际结构来源；不存在强制书签或等待人工模式。统一 `structure_policy=bookmarks_preferred`：有书签先尝试对齐，失败或无书签都自动继续。密码/转换失败/模型空输出/内容截断等真实解析错误仍需报告，不能伪装成结构回退成功。

## 4. PDF 章节拆分与图片落位（F2）

### 4.1 书签 meta 与内容对齐

流程：提取 PDF meta 书签树 → 外部解析模型输出标准化 blocks/md → 尝试对齐 → 成功按书签树、失败按 Markdown 标题与规则切分 → 检查覆盖 → 落盘。`_toc.json` 保存原始书签与对齐状态；`toc.md` 和 `_manifest.json` 记录**实际落盘结构**。书签匹配位置只用于内部算法，未匹配书签不虚构产物 URI。无页码填 null，远程目标标明类型；不填猜测值。

对齐规则：

1. 从结构化 heading block 或 Markdown AST 的候选标题提取编号/标题；原始 OCR 若标题只是普通文本行，适配器可依据版面角色、编号形态及局部内容明确晋升候选并记录依据。不能仅要求 `#`，也不能对任意正文做模糊命中。
2. 排除原书目录段、页眉页脚、表格/代码/公式内部的编号。目录识别须使用内容/结构证据，不依赖固定页数或“首标题之前一律目录”。目录内容保留，仅不作切分锚点。
3. 规范化空白/全半角/编号格式，先匹配编号与标题，再做标题相似度；重复标题要联合父路径和前后锚点。全局序列对齐保持阅读顺序，禁止贪心把后续章节抢走。
4. 每个有正文锚点的节点位置单调；章节 own-body 是本标题后到**下一已定位节点标题前**的块序列。分支 `index.md` 只存自己的引言，不把所有子章节正文再复制一次。
5. 仅结构父节点（模型可能不输出其正文标题）可用子节点位置建立虚拟容器，`match_status=structural_only`，own-body 为空；不能伪称标题精确命中。纯重复书签保留 separate bookmark ID，标为 alias，不复制正文。
6. 缺失、歧义、顺序冲突或达到配置的对齐时间预算时，不强行映射、不等待人工。在完整规范化内容上**整本文档统一回退**，丢弃未提交的部分章节树，避免书签切分与规则切分混用造成重复/遗漏。成功书签路径的节点分类为 matched/structural_only/validated_alias；回退路径无需强制生成书签数量的文件。

**自动回退规则（F2.6，同时进入M1）**：

- 首选 Markdown AST 的 `#`～`######` 标题（并支持规范化后的Setext标题），按标题级别栈建树；跳级接到最近存在的更浅父级，不伪造正文。忽略代码围栏、表格和已识别的原书目录中的伪标题；标题前的正文保留为前置内容。
- 未形成可用标题结构时，按规范化段落、列表、表格/代码等完整块顺序分组，得到 `section_0001.md` 等稳定规则章节，`section_target_tokens` 初始为4096。不能只按总字符数硬切图文；超大表格/代码块在此阶段可独立成章，后续F4再分块。短文即使只有一章也继续入库。
- 生成 `structure_source=pdf_bookmarks/markdown_headings/rule_blocks` 和 `alignment_status=matched/failed/not_available`；回退图片按实际所属内容位置就近落位，不按未匹配书签猜章节。页码仅在存在可靠对应时展示，规则章不继承猜测页码。
- 对齐未成功时追加持久 `job.info[]`：`code=BOOKMARK_ALIGNMENT_FAILED`、`message=书签对齐未成功，已按Markdown标题或规则切分继续入库`、`reason`、`fallback_strategy`、`unmatched_count`；无书签则用 `BOOKMARKS_UNAVAILABLE`。记录贯穿HTTP/SDK、任务列表/详情和前端，重试/重启后保留；最终info更新为实际使用的策略。
- 自动回退是正式支持的成功路径，覆盖/图片/索引均合格后状态为 `ready`，不因书签对齐失败单独标 `failed` 或 `ready_degraded`。只有内容为空、损坏、丢失、权限/持久化失败等才是真失败。覆盖校验对两条路径相同，不能以回退为由容忍内容丢失。

“书签编号唯一”只说明原始目录没重复，不证明 OCR 结果也 491/491 命中。质量分两项：映射分类覆盖率与对人工金标的边界准确率。不能靠造 491 个空文件通过验收。

### 4.2 完整覆盖与页码展示

正文归属使用半开区间 `[block_start, block_end)`；忽略章节元信息和明确标注的结构化容器，块必须**恰好一次**进入某章节 own-body。首个正文锚点之前的块存 `00_<filename>.md`（有内容才生成），最后节点延续到内容末端；未匹配块不能丢弃。书签对齐成功时，源自模型的额外子标题留在对应章内供 chunk 使用，不随意新增 PDF 书签节点；自动回退时则按实际 Markdown 标题或规则块建章。

页码仅展示：`page_start` 为书签目标；可选 `page_end_hint` 为估计的子树覆盖末页，不表示 own-body 精确页范围。父/子与同页兄弟允许重叠；同页兄弟结束不得计算成小于开始。遇倒序/非法目标省略 end_hint 并告警。`00_` 不能因首书签 p21 就声称内容精确等于 p1–20；对齐边界是内容位置，不是页码。

### 4.3 持久布局

保持旧版叶子 `.md` / 分支 `index.md` 的选择，不套用参考目录的具体名字或页码。目录只是结构示意。

```text
<parent>/<safe_filename_stem>/
  toc.md
  _toc.json
  _manifest.json
  00_<filename>.md                 # 仅当前置内容非空
  images/                          # 只供根级正文引用，不是全书图片池
  前言/
    index.md                       # 本分支 own-body，允许结构性空正文
    1.1 阅读方法.md
    images/
      <asset_sha256>.png
  故障诊断与排故/
    index.md
    9.9 故障类型及排故方式/
      index.md
      9.9.12 空调不制冷.md
      images/<asset_sha256>.png
```

成功书签路径一级可去章节编号，二级及以下保留；重名按 §2.2 处理。书签每个节点有稳定的 md 入口，alias/容器明示，不作为正文覆盖数量或搜索正文命中计数。回退路径按实际Markdown标题/规则章生成同样的叶子/分支布局，节点数由实际切分决定，不能补空文件凑书签数。关闭现有 MarkdownParser 对书签模式的“小节合并”，但不能全局破坏其他 md 导入行为。

章节 md 保留易读 bullet 元信息（document_id、chapter_id、level、uri、page_start），机器权威信息写 `_manifest.json`，避免编辑正文造成安全标签变更。`toc.md` 每一项链接到实际 `.md`（分支指向 `index.md`），页码未知显示未知；不输出服务器本地绝对路径。

图片存引用 md 所在目录的 `images/`；同目录内 hash 去重，跨目录允许复制。同一目录下多个叶子共享 images，但**摘要按引用 occurrence 的章节上下文区分**，不是按物理目录猜归属。记录 `asset_id + source_uri + block_id + context_hash`。没有整本图片池也不意味着必须给每个叶子新建目录。

持久 md 统一保留相对链接 `images/<hash>.<ext>`；读取/检索 API 返回规范化 Viking URI。现有 `rewrite_image_uris` 会转换链接，新增章节模式需显式控制并测试，不能同时要求“持久相对链接”又无条件复用其重写行为。发布前校验引用存在、在授权域内、无路径穿越；不受控远程图片不能留作已完成资产。

## 5. 队列、顺序与状态机（F3）

```text
queued → validating → converting(Office) → parsing → aligning
       → fallback_splitting(仅对齐未成功或无书签)
       → persisting → written → reindexing → ready
```

对齐成功直接进入 persisting；不成功自动经 fallback_splitting 后继续。章节解析流程没有人工等待状态。真实错误/取消转 `failed`/`cancelled`；显式允许缺少联合图片embedding等能力时才用 `ready_degraded`，不用于正常结构回退。`persisting` 之前用户不读到半棵树；written表示产物已持久化（新文档公开可读，替换文档仍待联合发布）。

默认 `serial_scope=parse_write`：同一文档完成对齐或有界自动回退并到 written 后，释放解析槽，下一本可解析，前一本独立reindex。真实失败、取消、转入等待重试的退避期也必须释放槽；重试重新排队并获取租约，旧fencing token不能继续写。对齐和回退各有有限时间预算，规则处理异常终止为可观察失败，不反复循环解析。若配置 `serial_scope=full_ingest`，则完成/失败/取消后释放；所有分支由同一释放逻辑保障，崩溃靠租约超时接管。

默认解析资源池一次只处理一本（所有后端共同约束），包括内部解析路径，不能仅把某个 Python 进程 `external_parse=1` 就算达成。部署多 worker 时以持久租约/锁保证互斥、超时回收与 fencing；namespace 内 FIFO，跨 namespace 做公平调度，优先级不得造成永久饥饿。单本文档内部并发只由后端声明允许，Logics 跨页状态不能乱序。

`ready` 屏障：正文/图片已提交；所需 chunk、BM25、L0/L1、图片联合向量计数符合 manifest；无失败必需项；namespace active snapshot 已包含该文档的新内容版本和索引代次。任务队列空不等于成功，API `wait` 到达此屏障才返回成功。客户端等待超时不取消 job。

每个 job 返回：`job_id/task_id/namespace_id/document_id/revision/state/stage/attempt/retryable/queue_position/queued_total/processed_units/total_units/heartbeat/checkpoint/resume_granularity/info/alignment_status/structure_source/error/warnings/artifact_uri/trace_id`。没有后端页进度时返回未知及阶段，不能伪造百分比。前端复用 tasks/monitoring，显示第几本、内容已写入/索引未就绪、失败原因、重试/取消/清理状态；回退成功显示“已完成，书签对齐未成功，已按标题/规则切分”。日志按 job 关联且权限过滤。

## 6. 文本、图片与语义层级（F4/F5/F9/F11）

### 6.1 三种层次不可混用

| 层次 | 用途 | 存储/索引 |
|---|---|---|
| 书签章节树 | 内容组织和章节 URI | VikingFS 章节 md |
| parent/child chunk | 小片段召回、较大上下文返回 | 派生 chunk 索引和 parent 引用 |
| L0/L1/L2 | Agent 由摘要到细节逐步定位 | `.abstract.md` / `.overview.md` / 正文与叶子 chunk |

借鉴 WeKnora `internal/types/chunk.go`：parent_text 为上下文，不必再做一份冗余向量；子块用于召回。不能直接把 memory 字符切片复制过来就算分层。

分块按 Markdown AST 的章节/段落/表格边界，child 默认目标 512 tokens、上限 768、重叠 64；parent 上限 2048 tokens，均为初始可配置值，实际不得超过 embedding 模型输入上限（含标题前缀）。超长表格按行组切并带表头，保留公式/代码语法，超大不可分块需分片并标明连续关系。全文覆盖到末尾，不能静默截断。阈值以 tokenizer 为准，不按“5000 字”猜测。

每 child 内部记录：`chunk_id/document_id/source_uri/content_revision/chapter_id/parent_chunk_id/ordinal/content_hash/chunker_version/embedding_space_id/namespace_id/meta`。hash与revision用于幂等、缓存和索引一致性；**对外溯源只要求可读的 source_uri**，不要求字符offset、页码、bbox、span或片段hash校验。分块器可内部保留块序号/边界做覆盖检查，但不成为API必填字段。表头重复、标题前缀等embedding输入加工只需保留正确来源URI，不新增精确位置映射系统。L0/L1继承安全范围；chunk ID稳定于同一revision和配置，旧块须按快照依赖和GC规则清理。

实施时必须同时改写写入和召回身份：当前 `collection_schemas.py` embedding 消费端以 account+uri+level 重算 ID，同 URI 多 child 会相互覆盖；`vectorize_file` 当前单文件输入截断，QUICK/递归收集按 URI 去重也会抹掉不同 child。F4 必须在 shared IndexPlan 接入时将记录主键扩展为 namespace/document/revision/chunk/embedding-space，保留 source_uri 为引用字段；writer、URI 统计/删除/迁移和检索证据集合共同迁移。禁止仅生成 chunk 文件、仅新增 chunk_id 或仅修改 vector_record_id 就宣称父子索引完成。旧 L0/L1 及 legacy 文件向量以明确索引版本迁移。

I004/I005 已完成写入/候选身份、长表格重复表头行组和首次索引/URI 重建共享调用链的文本部分改造；完整手册 968 份 Markdown 的 tokenizer/计划回放通过。原子发布、旧向量清理、复制后身份重建及真实检索质量仍待完成。完整边界见实施记录，不能据当前分块实现提升 F4/F9/F11 为 PASS。

### 6.2 原 URI Markdown

对已有 `viking://…/xxx.md` 重建只读正文，不调用 PDF/Office parser，不另造公开资源副本。长 md 在派生索引中按标题分层，所有 chunk 的 `source_uri` 始终是原 URI。返回可保留多个命中正文片段，但不要求片段在原文中的精确位置；不能因为同URI就只保留一个chunk而丢掉不同内容。

相对图片从该 URI 的目录解析，现有 Viking 图片复用；图片缺失进入告警/失败规则，不越权读取。meta 未显式提供时保留已有权威 meta，不凭空清空或把 LLM 猜测写成事实。

### 6.3 图片语义与向量空间

VLM 输入必须包含：原图、章节路径/标题、图注、引用前后段落（按 token 预算截取）、可确认的机型/配置 meta；输出描述所见对象、图类型、上下文中的用途与相关部件，推测标为不确定。缓存键含图片 hash、引用上下文 hash、prompt/model 版本。相同图片在不同章节的语义可能不同。

用户要求的主路径是**一个多模态 embedding 输入中同时提供摘要文本与原图**。发送两次独立 embedding 或只发摘要均不满足。能力探测与真实请求证据验证联合输入；文本 embedder 的自动摘要降级必须在企业模式被识别。`require_joint_image_embedding=true` 默认开启，服务不支持返回可见失败；显式允许降级时标 `ready_degraded`，F5 不计达成。

文本和图像如果同模型同空间可共享索引；不同模型/维度使用独立物理 collection/索引，逻辑检索统一。每个空间记录 model、version、dimension、normalization、query/document 协议。查询文本分别经各空间配套 query encoder，按排名融合，不直接比较不同空间 cosine；同维度也不能假定同空间。不同模型迁移必须重建新代后切换。

### 6.4 reindex 的一致性

采用共享 `IndexPlan`（初次入库与重建同一实现）：章节/原 md → child/parent 与图片上下文摘要 → 自底向上 L0/L1 → 正文/图片/L0/L1 embedding、BM25和meta索引 → 发布屏障。无依赖阶段可并行，但摘要必须先于其对应向量。叶子失效要传播祖先，外部源不重解析；`_toc.json` 不触发重切章节。

保留现有模式：`vectors_only` 不重写 L0/L1；按已提交内容与配置重建 chunk/BM25/图片向量，联合模式缺少当前有效图片摘要时返回 `SEMANTICS_REQUIRED` 或由调用方选语义模式。`semantic_and_vectors` 重新生成图片摘要和目录 L0/L1，再建对应索引。二者均不静默缩成文本摘要向量。`dry_run` 返回计划和预估计数，无写入。

reindex 中途内容被改时用 revision CAS 检测，取消当前提交并重排，不能将新正文与旧向量混合。对源 URI 重建影响的目录祖先可计算到 namespace 根；不自动越域重算。

现有 `prune_orphans` 等维护模式保持兼容，不被上述两个企业构建模式替代。当前路由只允许 `prune_orphans` 使用 dry_run；企业 IndexPlan 的 dry_run 是需要新增的能力，不能按现成接口宣传。

## 7. 混合检索与业务 meta（F6/F7）

### 7.1 统一检索流程

```text
认证/namespace/ACL → 业务 meta 候选解释 → 授权候选约束
 → 并行 BM25、dense 文本、dense 图片摘要+原图空间、可选 L0/L1 导航
 → RRF 融合与 chunk 去重 → rerank 精排 → MMR 选集
 → parent 上下文扩展（预算内）→ 授权复核 → Viking URI 引用与相关片段
```

默认候选预算：各召回路 100、融合后 100、rerank 50、MMR top 10；可调并记录 trace。RRF 常数默认 60；rerank 失败按配置回退到融合排序并标明 degraded；若要求严格精排则失败。MMR 在最终选择阶段，防止随后精排把多样性再次消除。相关性分与冗余分先归一化到可比范围，默认 λ=0.7；跨向量空间使用统一文本/摘要 token 相似度计算冗余。借鉴 WeKnora `internal/reranking/mmr.go` 的 token/Jaccard 实现，不误称其跨空间向量算法。

BM25 必须使用 child 文本与图片摘要的全文倒排、中文分词/型号与 DTC token、文档频率和长度归一化。通过统一检索适配器封装；后端无 BM25 就增设同 namespace 的词法索引组件，并参与代际发布，不引入面向用户的第二套检索 API。sparse 模型可作可选增益，不能替代 BM25 验收。

首版词法提供者选择固定为：已具备全文能力且通过契约测试的部署可用 `native`；本地后端使用 `local_fts5`（[SQLite FTS5 BM25](https://www.sqlite.org/fts5.html#the_bm25_function)），索引和查询都先经锁版本的中文分词及型号/DTC token 保留规则。词法索引只存派生内容、按 namespace/document_id/index_generation 组织，并按查询snapshot映射和授权过滤；服务化部署通过统一词法服务访问，不让各 worker 持有不一致副本。FTS分数方向由适配层统一，再用RRF融合。若性能/分词基线不达标，换 provider 要记录ADR并重跑TC-6/7/14，不能悄悄改需求。

全局 L2 召回在授权候选范围内始终可执行，独立于 reranker/ACL 是否启用；导航为空、摘要缺失、入口选错时仍保留这些候选。这里“全局”不跨用户授权域。新增统一 `retrieval_profile=hybrid`；现有 quick/thinking 保持兼容，新 profile 不由是否配置 reranker 来隐式决定。

每条结果必含 `source_uri/namespace_id`，可附 `chunk_id/parent_chunk_id/snippet/score_components`；图片带 `image_uri/context_uri`，Wiki事实带 `sources[]` Viking URI。以原URI聚合展示时 `matches[]` 保留相关chunk内容，不要求位置。客户端直接用现有Viking内容读取接口打开URI；无须版本号、span或hash，也不要求打开后定位到某行。章节文件可直接指向章节URI，已有md多个chunk都指向同一原URI。内部版本/快照用于一致性，不能额外加给用户作为溯源负担。`viking://…#chunk_N` 不作为不存在的文件地址返回；来源被删除/撤权按现有404/403语义处理，不伪造可读引用。

### 7.2 meta 模糊匹配

入库 `meta` 可选，原始值、规范值、来源与规范化版本分别保存；例如 `modelid=SY215-C`、`subject_code=SY021NCB62788`。额外 key 有类型/长度限制，不允许覆盖系统字段。

建立 namespace 内业务值字典与倒排：精确规范化、人工/业务别名、prefix、trigram 子串、受限编辑距离候选；解析 query 中的机型表达，映射到有限 canonical ID 集合，再用精确 ID 过滤各召回路。优先索引字典值而非在每个向量行执行 regex 全扫。不是所有字段都适合相同模糊规则：主体码、BOM、DTC 的一位差异可能表示不同配置。

查询支持两类输入：`meta_filters` 为用户明确约束，可指定 `exact/prefix/contains/fuzzy`；同字段候选 OR、不同字段 AND。无候选返回空或歧义提示，不能静默取消硬约束。query 推断出的 `meta_hints` 默认用于 boost；只有高置信唯一映射才可按配置收紧候选，低置信返回候选与解释。命中相近机型仅用于找资料，诊断 Profile/ConfigSpec 仍须精确确认，不混用相似配置标准值。

缺失 meta 的文档：无约束可参与检索；显式字段过滤默认不命中，支持显式 `include_unknown` 并在结果标明。ACL、namespace、适用性三态不作模糊解释。性能需同时报告字典基数、文档/chunk 数、候选膨胀与准确率；字符串算子可用不等于快速。

## 8. Wiki 与诊断状态空间（F10/F12）

### 8.1 Schema 与构建

用户注册 `schema_id/version`，包含可读 Markdown 模板、可选 YAML 字段/身份/关系约束、fewshot 和迁移规则。不能强制用户只写一个严格图数据库 schema。三一重机默认包从 iroot 的 `config/agentic-front/entities/` 适配；每批构建锁定完整 bundle hash（模板、约束、fewshot、共享字段及词表），禁止中途模板漂移。仅声明 version 相同不代表内容相同；发现同 ID/version 不同 hash 时注册返回409，要求显式新版本，旧任务仍使用旧包。

主入口输入 `source_uris[]`（可目录）+ `collection_id`；目标 namespace/URI/schema 从 collection 注册项确定，源 namespace 从授权根解析。底层构建计划保留解析后的 `source_namespace_ids[]/target_namespace_id/target_uri/schema_id/schema_version`。构建者须同时能读源和写目标；目标落在注册 root 内。跨域复制到权限更宽目标须有单独的 export/share 权限，否则拒绝；不能通过 Wiki 摘要泄露原文。

流程：内部固定源revision → LLM按模板抽取候选/来源URI → 身份归一/别名候选 → 对现有实体做保守合并 → 关系与适用性验证 → staging → 发布md和可重建关系投影 → 原检索IndexPlan。抽取只产原文支持的事实，不要求每本文档凑齐8类实体；证据按来源URI关联，无精确位置要求。抽取错误、无证据、模型超时分别记录，不能一律 `open_gap`。

节点即 md，关系引用是 md 中的权威字段；反向边、邻接表、全文/向量都是派生索引，可从 md 重建。关系遍历是同一 VikingFS 服务内的确定性链接展开，不另建向量问答检索系统。用户手改 md 使用 expected_revision，人工已确认字段受保护；模型重跑形成变更集，冲突保留证据并待审，不能覆盖。

归一化借鉴 WeKnora 的大小写/空白处理和保守同名去重；但本体身份按 `(namespace, entity_type, schema_identity)`，不能跨实体类型/配置凭词近义强行合并。“空调”和“空调系统”可能是别名，也可能是不同部件层级；必须有 schema 和证据支持。每条抽取事实附来源Viking URI，内部记录构建版本用于失效传播，合并保留多源出处、冲突值与优先级来源。

### WikiBuildPlan 来源准备约定（I025 部分实现）

W1 在上传解析/来源写入后、W2 在 URI 选择后，共用固定来源版本的准备逻辑。准备计划保存 collection/schema、输入范围、逐文件原 URI、私有正文引用、来源 epoch 和目录成员；任务执行须使用固定版本，不回退当前正文。相同幂等键不同来源版本/参数返回冲突。计划自身状态 prepared 不表示排队或构建完成。

当前W1单Markdown上传及W2已有Markdown来源均经HTTP/SDK接入共享准备；Office/PDF物化和持久执行任务尚未接通。I027已有内部namespace/export使用检查，但准备路由未调用执行器门禁，所以计划仍明确namespace_export_pending。未来执行器必须完成注册域与跨域导出校验、严格schema/domain门禁才调度模型并发布；最终事务还要复核全部来源/成员/ACL依赖。

捕获后及使用前复核内容/成员，不把二次遍历误称目录事务。目录不包含目标Wiki树；超上限失败不截断。批量正文流式读取，每个来源仍检查当前读权限；修订/撤权/删除影响未来任务与最终发布。保存的解析正文版本只按依赖保留；计划租约/退役/物理GC与未注册临时版本清理仍待完成。详见I025实施证据。

### 8.1.1 Schema 包预检与示例一致性（F10/F14，部分实现）

collection 固定的 schema 包是抽取和发布规则的数据输入，不执行包中的脚本或工具指令。Markdown 是用户编辑的实体模板；可选 YAML 负责可执行身份/字段/关系/布局约束。通用来源、授权、URI存在性门禁始终执行；未编译成可执行声明的自然语言规则不能冒称已验证，严格诊断 profile 需显式列出支持与不支持的规则并拒绝不足的配置。不能要求所有业务都使用这八个实体，它们只属于三一默认包。

注册时读取授权的完整包，固定文件清单/hash与规则编译版本；验证共享字段豁免、字段类型、嵌套关系对象、模板小节与变体、引用方向、派生字段和fewshot兼容性。fewshot只供示例，不能覆盖schema规则或被抽取为业务事实。模板与约束冲突时返回SCHEMA_CONFLICT并定位包内文件/规则，在昂贵知识抽取前拒绝；修订形成新schema版本。源文件更新不影响已固定的任务，W1/W2使用同一个经过预检的不可变包。

已核对的现行 iroot 包 declared version=2，但实际内容含9月28/29日的 asserted_faults 规则。Profile.fewshot.md仍要求每个Fault属于direct_component_uri，而Profile.schema.yaml明确允许跨部件；这项默认包冲突必须通过OpenViking适配包修正后才能注册。保留上游文件不动，记录适配文件hash和差异；不得默默把冲突fewshot送进模型。Device来源豁免、Procedure.fault_refs及Symptom嵌套fault_uri均需适配并单独验收。

### 8.1.2 显式结构门禁与未执行政策（I026）

结构约束必须声明为支持的纯数据类型，不能将任意文本、脚本或规则ID视为已执行：

| 声明 | 检查 | 范围限制 |
|---|---|---|
| structural_gates.exactly_one | 多个可选字符串字段恰好一个有值 | 不证明配置差异有事实证据 |
| nonempty / distinct / forbid | 非空列表/字符串、无重复条目、字段禁止出现 | 不证明实体身份唯一或关系正确 |
| parent_profiles | 本页位于引用父index.md的直接profiles目录 | 不替代父实体实际解析和完整path规则 |
| body_reference_order | 指定小节的候选URI首次出现顺序与字段一致 | 不证明推荐诊断顺序的质量 |
| class_name.enum_families / body_sections.templates | 枚举与按分类选择小节/次序 | 不执行正文语义/数值/工况门禁 |
| reference_key / reference_attributes | 声明的对象URI键及必需附属属性类型 | 引用仍需实际内容、同account、注册域与类型/版本校验 |

_shared.sources必须显式编译为来源引用；Device通过显式forbid来源字段豁免，不能从shared_not_required推断禁止。ConfigSpec的BOM不能因它不是纯URI字符串而丢弃spec，也不能丢失组件引用。所有shared_rules按实体纳入未执行台账，path_pattern/path_regex尚未执行时保留schema:path_layout标记；任意regex不自动运行。包含未执行检查的严格构建仍拒绝。

I026三一新适配包保留原包和变更manifest，固定摘要。七类示例仅通过明确结构检查，DTC旧kb来源未映射而拒绝；fewshot不是业务知识，不能自动当作引用实体或构建来源。38条实体规则、9条共享政策及完整路径/身份/证据语义仍待执行。旧schema能力缓存须按当前编译器覆盖复核；后续持久任务必须同时固定编译/抽取/归一版本与模型profile，不能仅锁输入模板hash后信任旧ready标记。

### 8.2 八实体和硬门禁

| 实体 | 身份/关键约束 |
|---|---|
| Symptom | 规范现象与功能域；按 schema 使用 Profile 配置路径，或无配置档时的有来源证据 asserted_faults 路径；禁止已废弃 related_fault(s) 字段 |
| DTC | `(code, scope_ref)`；可关联 Symptom/Fault，不能误套“所有路径必经 Profile” |
| Device | 营销机型页；配置关系由 ConfigSpec.device_refs 权威承载 |
| ConfigSpec | 主体码；BOM、配置标准与能力声明；device_refs 可解析 |
| Profile | `(parent_symptom, config_spec_uri 或 device_uri)`；两种锚点**恰好一个**，direct_component_uri 必须有效 |
| Fault | `(component_class, failure_mode)`；不把 Symptom 拼进身份；affected_components 是权威边 |
| Component | 依据 schema 的部件身份与规格/BOM；机理归属清楚，不重复可写反向边 |
| Procedure | `(target_components, procedure_scope, applicable_configs)`；fault_refs 必填非空，是 Fault→Procedure 的权威边载体；Fault 页规程链接为派生投影 |

Profile 仅在来源有配置差异证据时创建，`possible_faults` 有序且非空，且所有 URI 指向已存在或同批实际生成的有效实体。不能为了完成链路虚构 ConfigSpec/Device/Fault。缺 Profile 记录父 Symptom 的 `missing_profile`；候选为空则不发布 Profile。

实体引用必须指向当前内容与构建固定schema相符的已验证目标，证据来自服务端可信catalog（内部内容hash/schema hash），不能仅凭文件存在、预留身份、路径前缀或实体MD自称validation_state来认定有效。同批目标也需实际生成并通过闭合引用/完整规则门禁；暂时计划地址不放行。源资料引用不要求先成为Wiki实体，但仍须授权可读及固定版本；提交时重新检查依赖版本与权限。

区分：`knowledge_gap` 为确实缺少知识；`unresolved_reference` 为引用解析问题；`schema_violation` 为规则违反；`conflict` 为来源冲突。别名修正后仍无目标的边不发布，缺口可以单独保存；**已发布有效图的悬空边必须为 0**。允许 `open_gap` 不代表损坏图可通过验收。适用性三态 applicable/not_applicable/undetermined，BOM 不完整时不能仅以没查到部件判 not_applicable。

诊断热路径按固定 schema 确定性展开：有确切配置及有效匹配 Profile 时，规范 Symptom → 配置锚 → Profile → 有序 Fault → Component/Procedure。当前 iroot Symptom schema 允许无配置档的来源断言路径：Symptom.asserted_faults[{fault_uri}] → Fault；不把它解释为特定配置的概率/优先级，也不因此取消缺失配置的 undetermined。选择已确定的配置路径时，不得用无配置断言替代明确不适用的 Profile/Fault；无法确定时返回路径依据与未知项。DTC 按合法边进入。

这修正旧版“所有 Symptom→Fault 直连一律拒绝”的过期断言，不允许根据语义相似临时造边。严格 Profile-only 的其他 schema 仍应拒绝直连。当前三一默认 schema 中，无有效 Profile 且 asserted_faults 为空的 Symptom 保持 draft candidate；有明确来源并通过目标引用校验的 asserted_faults 可成为关系承载。通用资料检索仍用统一混合搜索，P10 只约束诊断热路径。

持久来源字段也按 schema：Device 使用 raw_refs，不重复写通用 sources；其他类型按声明来源字段。统一API结果可投影为 sources[]，不是要求向实体MD硬加字段。Device.config_specs 从 ConfigSpec.device_refs 派生，Fault→Procedure 从 Procedure.fault_refs 派生；生成/刷新这些投影，不保存可独立修改的第二套权威关系。Profile.direct_component_uri 是诊断入口，不限制 possible_faults 必须来自同一部件，各 Fault 的 affected_components 决定归属。

### 8.3 JEV 预研

参考用户提供的 [Eigent 介绍](https://www.eigent.ai/zh-CN/blog/typesafe-ai-jev-system-one-models) 与 [TypeSafe 官方说明](https://docs.typesafe.ai/introduction)。采用的设计启发是“状态 + 类型化问题 → 结构化判断”，不是承诺已接入 JEV 服务或有本地权重。

PoC state 包含规范症状、设备/配置、观测证据、已完成检查、候选 Fault、适用性与未知项；decision 包含候选排序、下一检查、证据 URI、置信信息与 `abstain/needs_more_info`。单故障互斥分类与多故障独立事件的概率语义必须分开，不能强行都归一和为 1。

排序分数默认叫 score；只有在独立诊断金标集做校准、可靠性曲线/Brier/ECE 后才称概率。与 Profile 初始顺序不一致不自动判错，应以确认根因和检查成本评估。PoC 不自动改写权威知识与配置适用性，不替代核心检索验收。

## 9. HTTP API 与 Python SDK 契约

以下为新增/扩展设计；复用现有 `/api/v1/resources`、content、search 和 tasks 基建。企业接口置于 `/api/v1/kb`，路由只编排现有服务与新 IndexPlan，不重复实现一套存储或检索。所有请求/响应定义到 OpenAPI，并同步 Python async/sync SDK 与前端生成客户端。

| 操作 | HTTP（拟新增，除注明者） | SDK（拟新增/扩展） |
|---|---|---|
| 后端能力 | `GET /api/v1/kb/parse-backends` | `client.kb.parse_backends()` |
| 注册知识域（I027配置已实现） | `POST /api/v1/kb/namespaces` | `client.kb.register_namespace(...)` |
| 提交入库 | `POST /api/v1/kb/ingestions` | `client.kb.ingest(...)` |
| job 状态/列表 | `GET /api/v1/kb/jobs/{id}`、`GET /api/v1/kb/jobs` | `client.kb.get_job/list_jobs(...)` |
| 重试/取消 | `POST /api/v1/kb/jobs/{id}/retry`、`/cancel` | `client.kb.retry_job/cancel_job(...)` |
| URI reindex | 现有 `POST /api/v1/content/reindex` 扩展选项 | 现有 `client.reindex(...)` 扩展类型与调用链 |
| 来源读回 | 复用现有 Viking 内容读取接口，传 `uri` | 复用现有 `client.read(uri)` |
| 混合搜索 | 现有 `/api/v1/search/find`、`/search` 扩展 profile/options | 现有 `find/search` 扩展类型 |
| 注册 schema（I024 配置已实现） | `POST /api/v1/kb/wiki/schemas` | `client.kb.register_schema(...)` |
| 读取 schema 元数据（I024） | `GET /api/v1/kb/wiki/schemas/{schema_id}/{version}` | `client.kb.get_wiki_schema(schema_id, version)` |
| Wiki collection 注册（I024 配置已实现） | `POST /api/v1/kb/wiki/collections` | `client.kb.create_wiki_collection(...)` |
| Wiki collection 读取（I024） | `GET /api/v1/kb/wiki/collections/{collection_id}` | `client.kb.get_wiki_collection(collection_id)` |
| Wiki 文件上传（I029仅Markdown准备） | `POST /api/v1/kb/wiki/ingestions` | `client.kb.ingest_wiki(...)` |
| Wiki 构建（I028仅来源准备） | `POST /api/v1/kb/wiki/builds` | `client.kb.build_wiki(...)` |
| Wiki 展开/诊断链 | `POST /api/v1/kb/wiki/traverse` | `client.kb.traverse_wiki(...)` |
| 文档删除 | `DELETE /api/v1/kb/documents/{id}?expected_revision=...` | `client.kb.delete_document(...)` |

入库请求示意（上传走现有上传协议，使用 temp_file_id）：

```json
{
  "temp_file_id": "upload_123",
  "namespace_id": "excavator-manuals",
  "parent": "viking://resources/raw",
  "parse_backend": "teleocr",
  "model_mode": "layout",
  "backend_options": {"layout_mode": "Detection"},
  "structure_policy": "bookmarks_preferred",
  "meta": {"modelid": "sy215", "subject_code": "SY021NCB62788"},
  "index_profile": "enterprise-v1",
  "require_joint_image_embedding": true,
  "idempotency_key": "ingest-20261003-001",
  "on_conflict": "error",
  "wait": false
}
```

202 响应含 `job_id`、现有 `task_id`、最终 `root_uri`、effective_backend/mode、namespace、状态；客户端超时通过 idempotency_key/job_id 恢复查询。`source_name` 用于文件名映射，服务端以规范安全名计算 root，不信任上传路径。

来源读回再次验证调用者的当前授权，按URI读取当前发布的文档/图片，不新增精确位置或历史版本读回要求。内部revision存储路径不作为公开URI。job详情与列表返回持久 `info[]`、alignment_status与structure_source，SDK和前端均需透传；自动回退不需要额外调用对齐修正接口。

自动回退成功后的job字段示意（省略其他字段；`failed`仅描述对齐结果，不是任务状态）：

```json
{
  "state": "ready",
  "alignment_status": "failed",
  "structure_source": "markdown_headings",
  "info": [{
    "code": "BOOKMARK_ALIGNMENT_FAILED",
    "message": "书签对齐未成功，已按Markdown标题切分并完成入库",
    "reason": "ambiguous_heading_matches",
    "fallback_strategy": "markdown_headings",
    "unmatched_count": 3
  }],
  "error": null
}
```

以下字段仍为最终目标，未经当前OpenAPI/SDK确认不能视为已支持。URI reindex 扩展请求：`uri/namespace_id/mode/recursive/wait/dry_run/index_profile/expected_revision/require_joint_image_embedding`；未传新选项保持旧语义，企业 namespace 配置默认策略。Wiki 主请求明确 `source_uris/collection_id/update_policy/idempotency_key`；底层固定 `source_namespace_ids/target_namespace_id/target_uri/schema_id/schema_version`，不让用户重复填互相冲突的目标配置；目标 Wiki 是精确目录，不默认加文件名以支持多源汇入同库。

错误统一 `code/message/job_id/stage/retryable/details/trace_id`：400 参数/路径错误；403 未授权；404 不可见或不存在；409 冲突/版本不符；422 模式不支持；413 超限；503 服务不可用。异步真实失败写入job.error（提交HTTP 202不能冒充完成）；`SOURCE_REQUIRED`、`SEMANTICS_REQUIRED` 为业务码。`BOOKMARK_ALIGNMENT_FAILED`/`BOOKMARKS_UNAVAILABLE` 属成功回退的job.info，不是HTTP错误。未经授权的job/source不泄露存在性。

兼容工作必须覆盖 `server/routers/*` 请求模型的 extra=forbid、服务参数、队列序列化、parser/IndexPlan、`sdk/python/openviking_sdk/options.py` 与 client、CLI/旧 HTTP client（若其入口开放）、前端 OpenAPI 生成文件。新增未知字段不能只在 SDK 被吞进 kwargs 而服务器实际拒绝。契约测试分别检查 sync/async HTTP 请求与持久 job 的 effective options。

### 9.1 当前可调用接口与演进边界（v0.10）

| 范围 | 当前实现 | 尚不能据此承诺 |
|---|---|---|
| namespace配置 | register_namespace/get_namespace/set_export；使用已存在目录和restricted资源ACL | 自动创建成员、全通道隔离、执行提交授权 |
| schema/collection | register_schema/get_wiki_schema/create_wiki_collection/get_wiki_collection | 注册collection自动完成域绑定门禁或知识抽取 |
| W1 | ingest_wiki(temp_file_id, collection_id, update_policy=fail或replace, idempotency_key)；单MD→prepared | PDF/Office/多文件附件、持久job、实体和关系ready |
| W2 | build_wiki(source_uris, collection_id, recursive, idempotency_key)→prepared | 当前请求不接受update_policy；prepared不是完成构建 |
| 来源计划查询 | get_wiki_build(build_id)，GET /api/v1/kb/wiki/builds/{build_id} | job进度、实际抽取结果或当前export授权凭据 |
| D1/D2 | 旧resources/content入口含局部企业能力，详见实施记录 | 新kb.ingest、统一企业job和全部目标选项已经可用 |

当前返回结构是build_id/digest/manifest/state=prepared（HTTP 200）；不能给它补一个假的job_id或queued状态。执行能力开放时采用明确的独立job接口（§9.2）：保留当前准备语义与原响应，不把现行HTTP 200悄悄改成202；OpenAPI/SDK/前端同批发布。准备计划与持久job为不同身份，一个固定计划可创建受限的重试attempt，job最终必须通过ready屏障。执行期重新检查当前schema规则覆盖、身份/导出和来源有效性，不能信任准备时快照状态。

最终replacement需expected_revision保护。当前W1仅fail/replace，未实现该CAS，不应当作安全的人工编辑覆盖协议；新增版本以显式revision参数引入并单独迁移。现行W1目标使用raw_sources_prefix/<filename含.md>/<filename含.md>，与最终统一safe_filename_stem布局存在差异；既有来源URI必须继续可读，新版本才启用统一布局并记录映射，不原地重命名来源。

当前准备路由超限返回400 INVALID_ARGUMENT，能力前置条件常返回412 FAILED_PRECONDITION，Pydantic校验返回422。§9的413和专用业务码是目标；不得在客户端硬编码不存在的码。KB-001负责统一版本化错误语义、重试/取消行为及已知5项旧SDK兼容缺陷。I031已将当前准备请求idempotency_key统一为内部catalog实际支持的ASCII标识符、长度1–128，并验证边界；旧256字符广告约束已收紧，同步/异步SDK及前端生成客户端同步。I033已新增D1/D2持久job扩展；最终入口、CLI和完整发布契约仍待完成。

### 9.2 最终 Wiki 执行接口与共同 job（待实现）

选择两阶段兼容协议，不再保留“准备接口何时直接变job”的模糊选项：现有W1 ingest_wiki、W2 build_wiki和get_wiki_build保持原语义。新增执行接口如下，当前代码未提供；UI/SDK只在服务声明对应capability后开放。

|接口|请求|返回|
|---|---|---|
|POST /api/v1/kb/wiki/ingestion-jobs|temp_file_id、collection_id、build_profile、idempotency_key、update_policy；replace还需expected_revision；可选parse_backend/model_mode/backend_options/index_profile|202 job_id/task_id；SDK start_wiki_ingestion。PDF/Office/MD及受限资产包统一走W1内部物化→prepared→共同执行|
|POST /api/v1/kb/wiki/builds/{build_id}/execute|expected_plan_digest、build_profile、idempotency_key；可选expected_collection_revision/index_profile|202 job_id/task_id；SDK start_wiki_build。使用当前固定计划，不重取不同版本正文|
|GET /api/v1/kb/jobs/{job_id}|当前调用身份|持久执行阶段、计数、产物、info/error和有效配置；prepared计划另用get_wiki_build查询|

build_profile为固定注册项引用：wiki_draft只写账号私有候选/变更集，不进入正式Wiki、统一搜索或诊断；diagnostic要求该schema所有必需规则可执行、可信实体catalog/来源授权和发布屏障通过。draft任务终态为draft_ready，不使用正式ready，不自动变成诊断事实。profile绑定schema许可规则集、抽取/归一/编译/prompt版本、模型profile、索引策略及成本预算；服务端固定这些有效值而非接受任意脚本或prompt代码。

执行提交时先验证当前读源/写目标/域导出、来源计划digest和成员/版本、schema capability，再持久固定job。idempotency作用域为account/target namespace/key：计划digest、profile及有效配置相同返回原job；不同则409，重试为同job新attempt。目录来源发生变化时明确SOURCE_CHANGED并要求新prepare，不能悄悄把新正文拼进旧计划。创建时校验不替代逐源使用及最终提交校验。已准备计划没有有效执行授权时只能继续查询，不启动模型。

W1原件读取/解析之前固定collection完整绑定；转换/解析产物首先进入对应raw_sources_prefix，随后用同一个WikiBuildPlanner捕获，最后进入与W2完全相同的执行器。job记录input_kind及派生build_id；PDF解析失败属于job失败，不返回伪prepared；上传MD的相对附件按受限manifest解析并验证路径/数量/hash/权限，裸MD引用缺图按声明策略失败，不假装已保存附件。上传原件及中间PDF仍按TTL清理。

## 10. Feature 拆解与实施路径

当前已实施子项按§1及[任务台账](openviking-enterprise-kb-backlog.md)记录；未附完整范围证据的子项默认“待实现/待验收”；“已有基建”不计目标完成。完成定义：实现合入/可定位 + 对应测试通过 + 可复现运行证据 + 无阻断缺陷。状态详情见测试文档，不使用无依据的总体百分比。

| Feature | 子项（稳定 ID） | 主要修改位置 | 验收里程碑 |
|---|---|---|---|
| F1 | F1.1 适配契约；F1.2 MinerU；F1.3 TeleOCR；F1.4 Logics；F1.5 能力/失败策略；F1.6 HTTP/SDK；F1.7 Docker | parse、server/resources、SDK、部署目录 | M1/M1b |
| F2 | F2.1 书签；F2.2 序列对齐；F2.3 覆盖和目录；F2.4 TOC/meta；F2.5 图片关联；F2.6 标题/规则自动回退；F2.7 发布校验 | parse/parsers、image_rewrite、持久 manifest | M1；M1b扩展质量评测 |
| F3 | F3.1 持久状态；F3.2 串行租约；F3.3 屏障；F3.4 日志/前端；F3.5 恢复/取消 | service/tasks、queuefs、web-studio | M0/M1 |
| F4 | F4.1 AST child；F4.2 parent；F4.3 片段引用；F4.4 全文覆盖；F4.5 代际索引 | embedding_utils、reindex_executor、vector schema | M2 |
| F5 | F5.1 上下文；F5.2 摘要缓存；F5.3 联合 embedding；F5.4 多空间查询 | media/utils、embedder、IndexPlan | M2 |
| F6 | F6.1 BM25；F6.2 RRF；F6.3 rerank/MMR；F6.4 叶子兜底；F6.5 可读回引用 | retrieve、vector adapters、搜索响应 | M2 |
| F7 | F7.1 meta 契约；F7.2 字典索引；F7.3 hints/filters；F7.4 跨后端语义；F7.5 性能 | schema、storage filter、search | M2 |
| F8 | F8.1 转换；F8.2 无书签结构；F8.3 三类文件质量 | converter 容器、parse | M1b |
| F9 | F9.1 原 URI 保持；F9.2 分层；F9.3 图片引用；F9.4 URI溯源 | content/reindex、IndexPlan | M2 |
| F10 | F10.1 schema；F10.2 抽取；F10.3 身份/合并；F10.4 关系门禁；F10.5 发布/溯源；F10.6 增量/人工编辑；F10.7 API；F10.8 诊断遍历 | Wiki 编排 + VikingFS 派生索引 | M3 |
| F11 | F11.1 共享计划；F11.2 L0/L1 传播；F11.3 图文一致性；F11.4 原子发布 | reindex_executor、semantic_processor | M2 |
| F12 | F12.1 可行性；F12.2 状态/问题契约；F12.3 校准 PoC | experimental/diagnosis | M4 |
| F13 | F13.1 身份/幂等；F13.2 revision/发布；F13.3 TTL/恢复；F13.4 删除/GC；F13.5 备份/限额 | service、storage、jobs | M0/M2 |
| F14 | F14.1 注册/权限；F14.2 全入口过滤；F14.3 跨域构建；F14.4 缓存/日志隔离 | authorization、namespace registry | M0/M3 |
| F15 | F15.1 fixture；F15.2 金标/防泄漏；F15.3 性能基线；F15.4 状态证据/变更追踪 | tests/enterprise_kb、eval、docs/spec | 持续 |

实施顺序：

1. **M0 契约与可靠性底座**：F14/F13 的身份/授权/发布，F3 状态与租约，F1.1 adapter 和 F15 fixture。队列与隔离不能放到功能完成后再补。
2. **M1 首条解析闭环**：MinerU → 书签对齐或自动标题/规则回退 → md/图片持久化 → API/SDK/job/info/UI；完整SY215C在模型服务具备后验收。对齐不成功也能自动完成入库是M1门禁。再适配TeleOCR、Logics，不能以单后端成功宣称三模型达成。
3. **M1b 多模型与 Office**：全部适配器能力矩阵、Docker 恢复测试、无书签与回退结构的扩展质量评测、Office 三件套；基础自动回退已在M1交付。
4. **M2 检索闭环**：F4/F5/F11 共享计划 → F7 metadata → F6 混合检索 → F9 原 URI；删除/更新/恢复纳入同批验收。
5. **M3 Wiki experimental**：已发布来源 + schema 包 + 权限 → 抽取/归一/合并 → 关系校验 → 同一检索和确定性遍历。
6. **M4 JEV 预研**：独立诊断金标、状态/决策、校准与检查成本评估，不影响前序交付。

## 11. 性能、评测与持续变更

性能不先承诺“1072 页 30 分钟”：远程硬件、模型/精度/并发未知。首轮分别记录各后端冷启动/热启动、页数、输入大小、GPU/显存/峰值内存、parse/write/semantic/embed/index 的耗时与失败率；定标后将 SLO 数值和版本锁入 F15。初始性能目标：10 万 chunk、1 万 metadata 值的暖查询元数据候选阶段 p95≤100ms、端到端检索 p95≤2s（含 embedding/rerank，固定并发 5）；均为待测目标，不是事实。

质量初始门槛见测试文档；全部输入规范块归属、有效引用、隔离、幂等和删除传播为硬门禁。生成评测候选与正文同时进行，人工核验后才为 gold；生产 Wiki 自己抽出的 Fault 不能直接作为诊断真值。样本按文档/机型切分，避免同段改写 query 在训练和测试中交叉泄漏。

需求变化流程：记录 CR-ID、用户需求来源、影响 R/F/TC、设计修改、迁移/兼容成本、验收证据；同一改动更新 spec、测试矩阵、feature 状态。F2/F4/F5 任何算法或模型变化都更新版本与评测；旧验收标记 stale，重新运行受影响用例。评审通过不等于产品实现完成。


## 12. 实施记录 I001（2026-10-03）

用户已授权开始实现。首批交付为 PDF 章节模式及自动回退，复用当前资源 API/SDK/任务存储。实际代码参数、使用方法、子项达成情况和限制见 [实施记录](openviking-enterprise-kb-implementation.md)；运行证据见 `evidence/implementation-I001-2026-10-03.json`。

F2 与 F1.6/F3.4 有部分可运行实现，但没有提升整个 M1 或 Feature 为通过。特别是：章节分组不是 F4 父子 chunk，现有文件向量不是目标分层索引；三模型、真实手册、namespace 发布及图文联合 embedding 尚未验收。本阶段的现有 `/api/v1/resources` 扩展是兼容入口，§9 的企业契约仍是后续目标。


## 13. 当前实施补记 I008（2026-10-04）

四主入口仍遵守 §0.1。D1 上传后的递归 Semantic DAG、vectors_only 文本遍历和 D2 URI reindex 已接入同一文本批次发布屏障：所有该轮计划内的文本文件 producer 成功结束、所有预期 child 写入成功，且各来源 epoch/desired attempt 仍有效，才在一次 SQLite 事务中切换其正文引用（开启 publish_content 时）与文本向量。任何文本成员失败、取消、重排、编辑或被较新尝试替代均不发布整批；旧已发布文本继续可见。已完成章节不能提前单独发布，引用仍是原 Viking URI。

这只覆盖本轮实际入计划的文本文件；不表示目录结构与 L0/L1、图片、BM25、删除/移动及任务 ready 已原子切换。首次入库工作文件的 list/read 也尚未完整受根快照投影约束。W1/W2 的 collection/schema、统一 WikiBuildPlan 与 API/SDK 仍待实现。不得把文本批次封口解释为 job ready；完整 FLOW 与 §2.3 继续 NOT_RUN。配置沿用显式启用的 chunking/publication_path/publish_content，不默认接管已有生产库。实现与 326 项定向回归证据见 I008。


## 14. 当前实施补记 I009（2026-10-04）

D1 递归 Semantic DAG 和 D2 URI reindex 的目录 L0/L1 **向量记录**现已加入 I008 发布批次。一个目录的全部计划语义层构成同一 generation，与本轮文本文件一起满足 producer seal 与全部 receipt 后才切换；目录物理向量 ID 包含 generation，重建不原位覆盖旧层。单独 marker 重建也调用完整目录 generation，避免写入 legacy ID 后被 active 过滤隐藏。部分层更新会读取并重新加入另一已有层；不存在的层不凭空继承旧 row。

启用 publication 后，目录语义生成/写入失败不能吞掉错误再发布文本；reindex 的语义阶段 FAILED/REQUEUED 同样明确终止后续向量阶段。空目录语义 generation 的 clear/delete 仍需统一生命周期，在 marker 重建中明确拒绝，不能直接删除 active row 冒充已完成发布。

当前只共同切换**目录导航向量和文本索引/正文引用**；目录 `.abstract.md/.overview.md` 文件的公开读取仍是工作版本，尚未纳入内容快照与完整修改 CAS。目录 layout/list/stat、图片、BM25、meta 权威注册、任务 ready、删除与 Wiki 均仍待推进。因此整本根的一致可读发布、四个 FLOW 和 §2.3 未完成。404 项定向回归与详细边界见 I009 实施证据；不代表远端生产发布或全卷检索。


## 15. 当前实施补记 I010（2026-10-04）

显式开启 publish_content 时，目录 `.abstract.md/.overview.md` 的实际文件字节现在保存为 AGFS 私有不可变版本，与 L0/L1 generation 一起在同一 manifest 更新 `semantic_contents`。完整文件（含规范 metadata）保留原 URI 的读回契约，public abstract/overview 仍只输出可见 body；正文文件、语义 sidecar 与对应向量按同一查询快照读取。两层未完成或失败时保持旧版，新层未发布不能透过工作文件泄漏。

写入/发布前绑定 sidecar epoch 与 raw SHA，校验归档 body 等于该轮向量输入；标准 VikingFS 写入推进 epoch，期间修改会使目录及其同批成员不发布。URI reindex/marker/direct index 和内部 metadata 写回明确读工作源，防止重建已发布旧摘要。保护根增加 sidecar active/read/pending 资产；仍不执行物理 GC。旧目录发布记录缺少版本引用时明确要求重建，不静默退回工作文件。

此处补齐的是**文本与目录 sidecar 内容及向量**的局部发布；原件 PDF 不成为永久资产。根 layout/list/stat、初次入库完全可见性、图片、BM25、权威 metadata、任务 ready/Wiki、清除/移动/删除、外部 AGFS/WebDAV/同步写入 CAS 与生产恢复仍未完成。四完整 FLOW、§2.3 和 TC-4.5/F13 继续 NOT_RUN。420 项定向回归及实现 hash 见 I010，不等于完整企业验收。


## 16. I011 图片上下文与四入口一致性实施状态（2026-10-04）

四条主入口仍以 §0.1 为准：D1 文件上传+parent 生成章节结构，D2 原 Viking URI 执行分层索引；W1 文件上传+collection 与 W2 来源 URI+collection 共用 schema Markdown 驱动的 Wiki 构建器。D2 不重新调用 OCR 或用 TOC 重排既有 Markdown；chunk 与图片摘要均是派生索引，公开引用保持来源 Viking URI。Wiki schema 用于约束实体模板和关系，不作为被抽取的知识。

I011 已增加 Markdown AST 图片引用上下文：章节标题路径、图注、引用前后正文，同一图片的多个引用分别请求 VLM；外部图片链接不自动抓取，读取沿用来源权限。企业 chunking 模式下摘要失败、空摘要、缺少原图或 embedder 仅支持文本会失败，producer 与 consumer 均校验联合输入。URI 图片 vectors_only 重建使用同一 vectorize_file 图片路径，不再落到纯文本 _upsert_context。semantic_and_vectors 沿用同一摘要生成器。当前 require_joint_image_embedding=false 仍显式拒绝，ready_degraded 尚未实现。

I011当时的上下文实现只扫描图片所属章节目录 Markdown（images/ 则扫描其上一级），每图最多16个引用，超限明确失败；尚需分页处理和跨目录引用注册。2048-token上下文预算是估算，不是已绑定 VLM tokenizer；机型/配置权威 meta、引用级缓存和独立索引持久化尚未接入。摘要虽逐引用生成，目前仍汇总送入既有图片向量行，不能宣称 occurrence 索引完成。图片 generation、原图快照、共同发布、多个 embedding 空间及真实召回质量仍待完成。新增 joint 策略进入 chunking 配置指纹，已有企业索引需按新配置重建后发布，不可静默混用旧计划。

远端8026服务接受真实手册图片+文本的一次联合请求，返回2560维有限向量；此证据只验证协议请求与输出，不能证明图片贡献、语义质量或 OpenViking 线上完整链路。协议参考 [vLLM Embedding Usages](https://docs.vllm.ai/en/latest/models/pooling_models/embed/)。438项回归PASS，具体边界见 I011 实施/证据；完整四FLOW和F5/F13继续未验收。


## 17. I012 图片资产与向量发布状态（2026-10-04）

I012新增image-joint-v1索引计划：每图片文件独立generation与member，摘要/联合输入/模型配置进入计划指纹，原图来自同一次授权归档读取，consumer物化内容使用对应摘要，避免读到后续工作资产。开启publish_content时，原图reference随图片向量参加文本、L0/L1相同批次；所有producer封口及receipt完成才切换，任一图片编辑/失败/被新任务替代使批次失败并保留旧版。公共图片字节读取和查询使用既有版本投影，私有资产进入active/read/pending GC保护根，仅生成保护计划、不物理删除。保留的是解析图片及其派生历史，不要求保留原始PDF。

图片计划使用既有L2 chunk字段承载一个图片member，不是独立引用occurrence索引完成；引用摘要仍汇总，引用级持久缓存与上下文修改依赖尚缺。collection模型指纹加入image_index_version，旧企业collection需新代迁移/重建，不能直接混写。只验证同一配置空间内发布，独立文本/图片空间的路由、query encoder和融合尚未实现。最初未发布根布局/图片可见性、BM25共同发布、跨入口写入CAS、job ready/UI/恢复、Wiki仍缺。446项fixture回归PASS；真实生产图文E2E/质量未验收，原完整四FLOW保持NOT_RUN。I011未加入图片代次/资产发布的描述属于当时状态，以本节为最新边界。


## 18. I013 真实图文embedding协议接入（2026-10-04）

发现I011远端探测发送messages，而OpenViking OpenAIDenseEmbedder始终发送input：旧证据不能证明适配器可用。已新增embedding.dense.wire_format=input|messages，默认input保留既有接口；messages使用SDK底层POST /embeddings并包装user/content，明确保留联合内容，同步/异步及文本query共用协议，默认float并支持base64解码。禁止extra_body覆盖模型/输入/维度/编码；不将文本批次拼成一个文档。messages仅适用于OpenAI-compatible dense实现，其他provider拒绝此配置。显式input=multimodal仍是图文能力配置，不仅凭wire_format推定模型具备能力。collection metadata及文本space identity纳入messages协议变化；修改协议需按新索引配置重建/迁移。

真实OpenViking配置工厂+异步适配器经SSH转发访问8026 local/wemm-embedding-4b-v1，手册原图95628字节+文本及文本query均返回2560维有限向量。证据image-adapter-I013-2026-10-04.json，未运行完整queue/发布/检索E2E或真实VLM摘要质量。472项回归PASS（含9项新增协议测试）。本机系统代理造成旧连接复用测试经代理关闭连接，已只在loopback测试fixture指定NO_PROXY并保留连接数断言，初始失败报告仍保留。使用协议参照[vLLM Embedding Usages](https://docs.vllm.ai/en/latest/models/pooling_models/embed/)。完整四FLOW与F5/F13仍未验收。


## 19. I014 图片引用独立索引（2026-10-04）

图片索引版本更新为image-occurrences-v2。已将generate_image_summary逐引用结果传入shared image plan：每引用独立chunk_id/ordinal、摘要+同一原图联合输入、context URI/hash和上下文；所有引用共用图片generation，receipt全部完成才可参与整批发布。公开聚合仍按图片Viking URI返回，matches保留独立引用及image_context_uri，溯源不要求精确位置。VLM asset SHA与当前归档原图不一致时，在enqueue前拒绝旧摘要。新增image_context_uri/hash字段与版本指纹，企业collection需迁移/新代重建。

Semantic DAG企业图片不再仅凭图片字节未变而复用目录合并摘要；语义阶段保留引用结果，skip_vectorization的资源ProcessResult内部value把image_summaries传给URI向量阶段，复用首次入库image plan。该数据只在单次语义重建中传递，不是持久缓存。新版本图片仅vectors_only且没有引用结果时，明确SEMANTICS_REQUIRED、保留旧版，不能静默压回合并向量；完整vectors_only引用缓存/复用仍待完成，不删除此目标。

478项局部回归PASS，其中新增6项验证独立row/joint input/receipt/结果matches、VLM后图变化、单引用失败、URI相同计划及阶段传递、vectors_only保护、增量语义图片重新提取。真实数据库为SQLite及磁盘fixture，VLM/embedder/vector DB/AGFS transport仍为测试替身；不是实际检索/全FLOW验收。上下文在VLM过程中修改的版本依赖、跨ACL引用交集与撤权预过滤、持久缓存、引用分页/跨目录发现、多空间/BM25、根可见性/job/Wiki等仍需完成。F5/F9/F11/F13与四完整FLOW保持未验收。


## 20. I015 图片引用缓存与正文版本依赖（2026-10-04）

image-occurrences-v3在私有account AGFS内容版本中保存occurrence语义缓存，SQL仅引用cache blob SHA/size及来源epoch。缓存随图片generation共同发布，active/read/pending roots保护缓存版本，不物理GC。缓存绑定原图SHA、当前引用列表/context hash/epoch、上下文规则版本、实际活跃prompt renderer及VLM配置签名；签名在VLM调用前捕获，enqueue时重核。仅embedding配置变化不要求重调VLM，但必须重建新向量代次。公开引用仍为Viking URI。

URI vectors_only在授权读取当前工作原图与引用正文后恢复已发布缓存，同一图多个引用保留独立ID；原图/引用/模型/prompt变化返回SEMANTICS_REQUIRED，缓存损坏或来源不可读明确失败，无合并摘要/空摘要fallback。旧image版本没有缓存仍需一次semantic_and_vectors，之后才能复用。缓存恢复与实时语义构建复用同一image plan。标准写入的context epoch在registry.begin及publication复核，使已完成的来源编辑阻止过期图片发布；缺乏epoch的legacy/manual语义尚不能认定具备完整正文依赖。

488项局部回归PASS，新增10项覆盖private cache重开/稳定plan、模型/asset/context失效、生成后模型改变、enqueue前/embedding中正文编辑、授权/缓存损坏、cache读取后原图修改、held reader/pending GC roots；原vectors_only保护测试更新为复用成功，不削弱独立引用断言。I014临时vectors_only拒绝是历史状态，当前有合法缓存即可复用。

并发审查发现标准write当前只在落盘前invalidate，读到写入尚未完成的旧body却捕获新epoch的窗口仍需写入fence/CAS，不能据上述完成写入测试宣称全并发安全。外部AGFS/WebDAV/sync/move、目录中新引用与权限变化的事务屏障、增量事件传播、跨ACL/撤权预过滤、分页/跨目录、多空间/BM25/root/job/lifecycle/Wiki及真实E2E仍未完成。完整F5/F9/F11/F13和四FLOW保持未验收。


## 21. I016 标准写入期间的发布保护（2026-10-04）

修复 I015 发现的窗口：仅在写入前推进 epoch，可能让索引捕获尚未落盘的旧内容却记下新 epoch。配置 publication registry 时，标准 VikingFS write/write_file/write_file_bytes/append_file 现在以持久 source_writes 标记覆盖实际存储 await；开始和成功结束均推进 epoch。每个并发写入有独立 token，结束一个不能解除其他写入。observe_draft、generation begin 和最终 publication 都检查正文、图片上下文及目录 sidecar 的写入状态；构建期间发生相关写入则拒绝发布，保留已有版本。

取消、传输异常或进程中断不能证明远端写入已停止。结果未知的标记保留为 uncertain（进程中断时可能仍为 writing），重开 SQLite 后继续阻止新的捕获/发布；不按超时自动解除，也不能用正常 finish_write 清除 uncertain。已有已发布内容仍可读取。经后端确认完成后的恢复接口/任务仍待实现，因此这项是安全保护的局部完成，不是生产恢复与高度可用验收。

498 项回归 PASS，新增10项覆盖四个实际写入入口的暂停窗口、多写入/重开/隔离、取消与传输失败、捕获后修改、目录摘要和图片上下文依赖。执行真实 SQLite、磁盘及 VikingFS 方法，AGFS transport/embedding/vector DB 为 fixture；未进行远端生产并发写入验证。发布 registry 未配置、外部 AGFS/WebDAV/sync/move/delete、不同主机独立 registry、目录引用集合及 ACL 事务均不由本次保护覆盖。部署前须排空旧版未受保护写入，不能把新增表当成兼容所有在途任务的迁移完成。

四入口契约仍以 §0.1 为准：D1 上传+parent 解析并建结构，D2 URI 复用分层索引；W1 上传+collection 与 W2 URI+collection 共用 schema 驱动 Wiki 构建。Wiki 接口、collection/schema 注册和构建器仍待实现；四完整 FLOW、F13 恢复及真实线上质量继续未验收。本节替代 I015 关于标准写入 in-flight 窗口尚未修复的当时描述，其他边界不变。


## 22. I017 远端运行复核与恢复设计（2026-10-04）

本轮远端只读复核：TeleOCR/Logics/MinerU 的 /health 和鉴权 capabilities 均200，声明 ready_for_jobs/model_verified=true；三个worker与MinerU引擎容器运行。共享SQL队列在检查时没有queued/running。worker.py/model_artifact.py/serve.sh hash与本地逐一一致。/home挂载可用约271GiB（98%占用），仍须配额/容量管理；不是存储配额验收。详情 evidence/runtime-I017-2026-10-04.json。

health当前检查依赖/权重文件；model_verified来自历史成功，不能证明此刻模型推理、引擎健康或全文质量。此轮未提交新解析任务、未部署OpenViking线上索引、未跑四完整FLOW。异常写入缺少后端terminal/fencing证据，新增§2.3.1恢复契约及TC-13.12–16，执行器/API仍待实现；不能用TTL/hash稳定/客户端取消直接解锁。上一轮498项代码回归证据不因本轮文档修改而提升范围。


## 23. I018 解析队列运行与部署状态

解析服务健康契约新增dependencies_ready/worker_state，ready必须包含后台队列存活和启动恢复完成；失败队列不再接收上传。502项回归PASS及远端三个worker更新后running/ready证据见I018实施记录。仍没有fresh模型推理/引擎健康、自动队列watchdog或四完整FLOW验收。I017描述的health只检查文件已由本次修复扩展；历史model_verified仍不是当前质量证明。


## 24. I019 实际Wiki schema对齐审查

只读清点用户指定iroot/entities得到8实体、完整Markdown/YAML/词表文件hash及bundle摘要，工具tools/audit_wiki_schema.py可重跑，证据evidence/wiki-schema-I019-2026-10-04.json。此工具仅清点，不是运行时schema注册/校验器。修正默认schema的asserted_faults路径、Device.raw_refs例外、Procedure.fault_refs权威方向及Profile多部件语义。现行Profile fewshot与schema冲突，适配包修正/注册门禁待实现；没有修改上游或宣称W1/W2已运行。新增验收TC-10.12–16和修订TC-10.3/11均NOT_RUN，历史审查中的绝对Profile-only描述仅代表当时假设。


## 25. I020 Wiki schema基础实现状态

已加入不可变完整schema包和结构声明编译函数、三一Profile示例适配的新版本产物，516项回归PASS。actual默认包八实体结构可编译；38条自然语言规则仍未执行，strict模式拒绝包进入严格使用。只修已定位的Profile示例句，不宣称完整模板/enum/path/身份/内容规则校验通过。适配zip是本机可审查产物，未注册到远端collection；API/SDK/WikiBuildPlan及W1/W2仍待实现。详见I020实施及证据。


## 26. I021 Wiki实体检查实现状态

共享schema基础增加字段enum与实体Markdown结构检查、声明来源/嵌套关系和版本引用解析接口。540项回归PASS；真实Profile适配示例通过结构检查，其9引用尚未授权存储解析，5规则未执行，不能认为实际诊断图通过。检查结果明确为draft并记录未编译规则；正式构建器仍需真实VikingFS resolver、跨域export/ACL及提交CAS，再执行完整domain门禁。两Wiki入口/API/持久注册仍未接入，四FLOW保持NOT_RUN。详见I021实施/证据。


## 27. I022 Wiki引用读取实现状态

已有VikingFS工作内容授权读取适配器、前后epoch/写入保护及内容+schema绑定的实体验证标记检查，552项回归PASS。trusted binding catalog与ACL在测试中为mock，未实现持久namespace/实体catalog、完整domain验证、export/提交CAS与W1/W2编排。I021只有抽象resolver的当时描述由本次读取适配器扩展，但生产Wiki门禁/四FLOW仍未验收。


## 28. I023 collection/schema服务实现状态

内部WikiCatalog加入账号隔离的schema与collection保存、AGFS私有包/GC根及不可变选择，567项回归PASS。注册权限为账号管理员，选用collection沿用目标读写ACL；upload另检查raw写权限。不同collection默认target/raw写根均不重叠，授权跨库来源由W2读取；这是绑定组织检查，不替代namespace授权注册。draft_rules_pending/structural_ready均不代表正式诊断发布通过。HTTP/SDK、更新CAS、成员授权、实体catalog/WikiBuildPlan及完整W1/W2仍未接入；详见I023实施证据。


## 29. I024 四入口边界与配置接口实现状态

四入口的输入/输出约定在 §0.1 再次明确。schema/collection 四个 HTTP 配置操作已接入同步/异步 SDK 和共享 transport 的前端生成客户端，573项本功能回归及独立 TypeScript 检查 PASS。五个已有 SDK 兼容失败通过 HEAD 基线复测确认，扩展回归并非全通过。Wiki ingest/build 任务入口、完整域授权和 W1/W2 编排仍未完成；配置状态不能作为构建或发布 ready。完整四 FLOW 仍 NOT_RUN，详见 I024 实施与测试证据。


## 30. I025 Wiki共享来源准备实现状态

内部 prepared WikiBuildPlan 已保存固定 schema/collection 和 Markdown 来源版本、范围/成员/epoch，两个输入类型共用准备器。600项本功能回归PASS；真实手册968 Markdown与三一适配包的数据回放通过来源等价及流式hash校验。此结果不包含模型抽取、38条domain规则、namespace/export授权、任务HTTP/队列、实体/关系原子发布和完整检索。四FLOW仍NOT_RUN，GC物理回收未开启。


## 31. I026 schema结构执行与覆盖纠正

15个结构门禁、分类模板与引用对象属性已接入共享检查器；schema/collection/pin读取复核能力状态。新三一包6a6f66c8…保留原版，七类示例结构PASS、DTC旧来源拒绝；严格编译仍拒绝118个按实体记录的未执行条目。647项局部功能回归PASS，完整F10/W1/W2和四FLOW未验收。见I026实施、测试及适配包审计。


## 32. I027 知识域与导出配置实现状态

知识域根注册、读取和定向导出授权 API/同步异步 SDK 已实现，使用既有资源 ACL；根要求 ACL 启用、实际目录、显式 restricted 且互不重叠。读取来源和写入目标不自动允许跨域导出。Wiki 执行来源授权内部接口复核主体、组、当前根 ACL 与策略 revision；全局检索域预过滤、提交期间 ACL fencing、任务身份刷新尚未完成。669项局部回归，见 [I027证据](evidence/implementation-I027-2026-10-04.json)。

## 33. I028 W2 URI 入口实现状态

POST `/api/v1/kb/wiki/builds` 与 `client.kb.build_wiki` 接收 source_uris、collection_id、recursive、idempotency_key，固定来源和 collection/schema。返回不可变来源计划 `prepared`，未执行抽取或发布。namespace/export 仍需执行期门禁；不是 ready 任务。675项局部回归，见 [I028证据](evidence/implementation-I028-2026-10-04.json)。

## 34. I029 W1 上传入口实现状态

POST `/api/v1/kb/wiki/ingestions` 与 `client.kb.ingest_wiki` 接收 temp_file_id、collection_id、update_policy=fail|replace、idempotency_key。当前仅接受单个 Markdown，物化到 collection.raw_sources_prefix 后共用 WikiBuildPlanner；PDF/Office 解析接入和附件批上传未完成。682项局部回归，见 [I029证据](evidence/implementation-I029-2026-10-04.json)。

## 35. I030 来源计划查询、上传限额与幂等保护

新增 GET `/api/v1/kb/wiki/builds/{build_id}` 与同步/异步 `client.kb.get_wiki_build(build_id)`，返回同一不可变来源计划。账号从请求身份获取；不存在或他账号计划返回404，当前来源不可读或 collection 目标不可写时拒绝返回。读取会校验已存 manifest 摘要和当前 collection 绑定。返回的 schema_capability_state 与 authorization_state 是准备时记录；不作为当前可执行资格或跨域导出凭据。来源计划查询也不是持久 job 状态接口，不更新为 queued/ready。

上传路由按 planner 的 document_bytes 上限读取最多 limit+1 字节，超限在物化/落库前返回400；不依赖 stat 大小避免读取期间增长绕过限额。resolve_for_consume 之后的所有退出都执行 cleanup（具体是否删临时副本沿用现有 upload store 语义）。这只限制 Wiki 路由正文读取，shared upload store 下载到磁盘的流量/磁盘配额仍需另外实施。

已绑定幂等键在物化前检查 collection 绑定、输入类型、来源路径及字节摘要。不同内容/文件名，即使 replace 也返回409且不改写来源；相同内容但当前来源已经被编辑/删除，也不恢复旧原文，需新幂等键。最终计划持久化仍复核来源 epoch/绑定及幂等冲突。此改动处理已有键的顺序重放；首次并发上传的完整物化事务/目录写入 fencing 仍待实现，不宣称并发原子性。

前端 Wiki 客户端片段由实际 server OpenAPI 重建，包含配置、W1/W2 准备及查询七条路径，共用原 transport；独立 TypeScript 类型检查通过。未新增 UI，未覆盖其他已有生成文件。

```python
plan = client.kb.build_wiki(
    source_uris=["viking://resources/raw/manual"],
    collection_id="sany-maintenance", recursive=True, idempotency_key="manual-v1",
)
prepared = client.kb.get_wiki_build(plan["build_id"])
assert prepared["state"] == "prepared"  # 来源准备阶段；尚未执行知识构建
```

本轮692项局部回归（I029的682+新增10）通过；旧SDK五项兼容基线失败仍未修复。完整四FLOW、Wiki抽取与关系/统一索引发布、真实混合检索、持久任务恢复/GC及线上高度可用继续未验收。见 [I030证据](evidence/implementation-I030-2026-10-04.json)。


## 36. CR-011 未完成交付与运营边界

全部剩余任务、依赖、实现状态、TC映射和完成条件见[任务台账](openviking-enterprise-kb-backlog.md)，机器数据见feature-status.json。每轮变更先更新当前Feature摘要与台账，再追加历史证据，不能只在文档末尾追记。台账中的OPEN/PARTIAL表示实现程度；product_acceptance=NOT_RUN表示完整验收未执行，两者不能合并成“已完成”。

首版部署是单权威协调服务加多worker；SQLite位于协调服务本机，不通过NFS并发共享，也不允许每个worker维护独立权威库。单协调服务故障期间新写入/发布暂停，不能宣称写入高可用。后续多协调者HA需支持事务/CAS的共享repository与选主/写入fence，并有实际切换测试；保持同一API和VikingFS知识形态，不另造用户检索系统。

上线门禁按M0/M1/M1b/M2/M3/M4分别出报告。核心文档入库检索可在M2验收后独立开放，Wiki标experimental且只有M3完整门禁通过才能发布诊断事实，JEV仅PoC。RTO/RPO、容量、水位告警和端到端延迟必须在固定环境首次测量后锁定数值，缺实测时写未定标，不宣传线上高度可用。现有原件不永久保留；要重新选择parser仍需重新提供原件，不通过备份承诺恢复已经清理的PDF。


### 36.1 共同发布与派生权限的具体协议（待实现完整门禁）

协调服务先持久记录job attempt与发布intent，写账号私有不可变正文树及资产，所有向量/词法/meta/目录producer以该intent和fence报告receipt；确认内容hash、成员、全部必需receipt及当前授权/版本后，在同一registry事务切换active manifest并标记job可完成。AGFS写与SQLite不能跨系统原子提交，事务只切换已经确认持久的引用；崩溃留下的未引用资产由依赖GC处理，不向公开layout投影半完成树。不可确认的后端写入保留保护token并进入明确恢复状态。

查询一次捕获namespace active manifest，BM25、文本/图片dense、L0/L1与正文扩展均按同一manifest过滤。删除/撤权优先使用当前tombstone和当前授权，不能让旧query租约继续返回已禁止内容。list/stat/read同样取可见根成员；新文档written的整体URI读取与搜索未就绪区分，替换文档继续投影旧published版本。物理GC二次事务/CAS复核活动/租约/pending/Wiki依赖，无权强删以腾空间。

权限事实继续使用既有资源ACL/member服务；协调registry保存单调授权revision与ACL写入intent用于fencing，不保存一套互相矛盾的成员权限。受管理ACL更新先登记intent，在可信后端确认投影完成后推进revision；未知结果不能提前解除fence。最终发布与授权变更按同一协调者序列化，读取亦校验当前授权。外部或跨主机直接修改正文/ACL无法受本机epoch保证时，部署必须禁用该绕过入口或接入共同变更协议；不能依靠hash稳定推断“不会再写”。KB-003/005/006负责真实后端故障测试。

L0/L1、图片摘要、Wiki事实及parent上下文的权限是所有贡献来源可读权限的交集，并要求目标可写/跨域export允许；目录较宽ACL不能使私密子文档内容进入公共摘要。返回候选、parent扩展、日志与缓存都检查这些依赖，不能只校验图片物理路径或摘要所在目录。撤权使旧摘要/候选不再可见，并产生受限重建任务；不把授权失效解释为知识缺失。

## 37. I031 首批独立并行实现（2026-10-04）

按用户授权，三个agent分别负责无前置依赖的KB-001、KB-002、KB-031，主agent审查集成、同步前端并回写证据。共享工作区按代码范围分工，保留全部已有未提交改动。

|任务|本轮实际增量|仍需完成|
|---|---|---|
|KB-001|旧Python bridge find/search兼容；prepare key统一1–128；实际HTTP400/SDK异常契约；前端补知识域配置client|Rust CLI字段一致、未来job/execute及完整任务契约|
|KB-002|同机Unix socket认证repository；SQLite/WAL单权威所有权、断连拒绝、重启、registry备份；真实producer经协调进程发布|跨主机内部接口、Wiki/ACL旧事务迁移、真实后端恢复、联合灾备|
|KB-031|gold人工审定绑定、数据冻结/验hash、文档/机型/片段防泄漏、类别和分组CI报告|独立人工gold、全部类别覆盖、真实质量和性能|

最终联合回归925项全部通过，覆盖I030原测试模块、所有enterprise_kb、旧客户端配置及完整Python SDK；前端7个Wiki路径+3个namespace路径从实际OpenAPI生成并严格TypeScript检查通过。SDK旧5项失败中4项修复实现、1项更新过期凭据转发断言，保留现行主机绑定和challenge规则。第一轮924项通过后评测输入校验增加1项，因此最终重跑925项，历史首次报告保留。

评测实际产物为50条synthetic、0条gold，全部unassigned；单文档/单机型不拆成独立训练与测试。没有检索/模型性能数据，质量与性能仍NOT_RUN，CI为null。协调服务验证为同机独立进程，AGFS/embedding/vector仍有fixture替代；旧Wiki/ACL直接SQL在authority worker上明确拒绝，尚未完成可部署的全通道迁移。

## 38. I035 Wiki 抽取拆解/分发与独立模型段（2026-10-05）

固定来源抽取器不再对整篇来源做一次调用，改为“拆解任务→分发抽取”：先按 Markdown 标题边界切分来源文本（超长段再硬切并限制 `source_chars`），对每个片段做词法 kind 短名单筛选（**所有声明 kind 始终有资格，相关者前置**，筛选只影响排序不影响可见性），再对每个片段单独发起一次有界结构化调用。每次调用只携带 schema 的 id/version/digest、声明列表、目标 URI 与单段来源文本，**不再携带原始 schema 包成员正文**（旧实现的上下文爆炸源）。抽取器版本 `wiki-fixed-source-extractor-v2`，续跑校验同一 configuration 与分段确定性。

Wiki 抽取目标模型与图像摘要模型解耦：新增 `OpenVikingConfig.wiki`（`Optional[VLMConfig]`）与 `get_wiki_model()`，未设置或为空时回退 `vlm`；`runtime_config` 的运行时模型段、可热重载段、可选 VLM 段集合均已纳入 `wiki`。本部署将 `wiki` 指向远端 `svc/deepseek-v4-flash`，本地 `vlm` 继续服务图像摘要。

|任务|本轮实际增量|仍需完成|
|---|---|---|
|KB-024|拆解/分发结构、每次调用紧凑 schema 负载、逐段 checkpoint 续跑、独立 wiki 模型段与真实模型探针 CONTRACT_PASS|真实企业语料质量、可信身份关系、完整诊断发布|

受影响范围 94 项通过（`wiki-decompose-dispatch-I035-2026-10-05.xml`）；真实模型探针见 `wiki-extraction-real-model-2026-10-05.json`（synthetic，非企业质量证据）。完整 Feature 与四 FLOW 仍 NOT_RUN。

三个任务保持PARTIAL，15个Feature和四条完整FLOW均NOT_RUN。完整仓库测试未执行，不以925项局部通过替代企业验收。总报告见[I031](evidence/implementation-I031-2026-10-04.json)，各任务报告见[KB-001](evidence/parallel-KB-001-2026-10-04.json)、[KB-002](evidence/parallel-KB-002-2026-10-04.json)、[KB-031](evidence/parallel-KB-031-2026-10-04.json)。权威服务使用方法见[部署说明](../deployment/registry-authority.md)，评测使用方法见[评测说明](evaluation/independent-gold/README.md)。

## 38. I033 当前执行与证据边界（2026-10-05）

D1/D2当前实验接口是`POST /api/v1/kb/ingestion-jobs`，提交`namespace_id, kind, idempotency_key, payload, options`；D1 payload使用temp_file_id及互斥的parent/to，D2仅uri且拒绝二进制PDF/Office直接索引。GET `/api/v1/kb/jobs/{job_id}`查询，POST `/api/v1/kb/jobs/{job_id}/cancel`取消。这是当前扩展面，§0.1最终便捷入口与CLI尚未对齐，不以实验接口替代最终任务完成条件。

新collection要求target位于指定注册域、raw_sources_prefix位于独立注册域；缺域、越界、同域均拒绝。旧绑定持久标为legacy_domain_pending；账号管理员通过migration-inspection读取binding digest，再migrate-domains提交expected_binding_digest做CAS迁移。合法迁移继续返回prepared，不授权诊断发布。

当前job在written后释放解析槽，再执行index；没有正文树/图片/父子/L0L1/BM25/meta的完整共同receipt，不允许ready。取消或崩溃时存在未知写入则保留write_recovery_required，服务端确认恢复正在实施。三模型小样本通过仅说明契约，Logics删除未声明图引用不能证明完整图像覆盖；真实Office转换缺陷与全卷/gold质量均仍需完成。


## 39. I034 当前接入与验证（2026-10-05）

联合回归1575 PASS、1 SKIP；跳过项为显式启用的真实Wiki模型探针，已在独立报告实际运行通过。前端356项、完整TypeScript和生产构建通过。初轮4项旧契约失败与前端环境/超时失败保留原报告，修复后重跑。该范围不等于完整仓库或产品验收。

文本BM25已加入权威发布回执，现有find/search复用固定manifest和当前权限；不含业务meta、图片摘要与完整物理GC。Office本地45项和服务器Docker六格式合成契约通过；生产worker与远端转换容器传输未完。受控后端未知写入恢复与离线隔离恢复已验证，普通AGFS能力不足时明确拒绝，恢复库不得自动启动。

Wiki旧接口继续200 prepared；POST `/api/v1/kb/wiki/builds/{build_id}/execute`固定plan digest与profile，返回202持久W1/W2 job。已准备MD抽取共用一个执行器，支持逐片段恢复；私有draft使用 `/api/v1/kb/wiki/jobs/{job_id}/drafts/{draft_index}`读取，复核所有来源ACL/export及当前输入。完成事务绑定真实候选回执和plan，并再次复核source epoch/export revision。候选blob纳入GC保护；没有诊断知识发布。

默认包仍有118个未执行规则实例；严格诊断模式拒绝。真实配置模型合成探针生成1候选，不能证明语义证据质量。用户明确暂时没有人工gold；质量与性能门禁继续保留。KB-024从OPEN更新为PARTIAL，所有完整Feature与FLOW仍NOT_RUN。报告见[I034](evidence/implementation-I034-2026-10-05.json)。

## 40. I037 reindex 语义分层契约（2026-10-05）

目标第3项要求语义分层在 reindex 中也能重建，以利用 Viking agent 的定位特性（L0 摘要→L1 概览→L2 正文/叶子 chunk 的逐步定位）。D2 `POST /api/v1/content/reindex` 的 `mode` 两值契约由 `ReindexExecutor.effective_mode_config` 显式区分：

- `vectors_only`：只重建 chunk/BM25/图片向量，`writes_semantics=False`、`write_directory_levels=False`、`regenerate_image_summaries=False`——已发布的目录 L0/L1 不被改写。
- `semantic_and_vectors`：`writes_semantics=True`、`write_directory_levels=True`、`regenerate_image_summaries=True`——先重建图片摘要与目录 L0/L1（祖先自底向上传播、叶子失效传播祖先、递归不重复刷新），再建对应向量。

两模式共用同一 `IndexPlan`，与 D1 初次入库等价：原 MD/目录不被改写、parser 调用 0 次、不读 `_toc.json` 重切章节。`dry_run` 预览按计划目录数给出 `planned_semantic_levels`（语义模式为 2×目录数，即 L0+L1）。两模式均不得把联合图片记录降级为纯文本摘要向量，缺当前有效图片摘要时返回业务码 `SEMANTICS_REQUIRED`，绝不静默退化。

定向证据：`tests/enterprise_kb/test_reindex_contract.py` 17 项通过，其中语义分层相关含 `test_effective_mode_config_distinguishes_two_modes`、`test_vectors_only_does_not_touch_published_directory_levels`、`test_semantic_mode_refreshes_ancestors_bottom_up`、`test_recursive_semantic_mode_does_not_double_refresh_ancestors`、`test_dry_run_preview_counts_directory_levels_semantic_mode`。KB-020/F11 追加 I037 证据。完整 D2 端到端产品验收（真实后端、真实 VLM 摘要质量、跨空间检索）与四条完整 FLOW 仍 NOT_RUN，报告见 [I037](evidence/implementation-I037-2026-10-05.json)。

## 41. I038 KB-029 多源撤回与投影重建（2026-10-05）

KB-029 此前在台账中为 OPEN 且无证据。实际 `openviking/wiki/merge.py`（`MERGE_VERSION = 'wiki-private-candidate-merge-v2'`）已实现来源依赖与撤回重建的主体：

- **逐候选来源依赖溯源**：每个候选的 `provenance` 记录 `source_uri/source_sha256/source_epoch/build_id/plan_digest/draft_reference/candidate_uri/candidate_sha256`；`field_observations/body_variants/evidence` 均带逐来源 provenance，`payload['inputs']` 保存本次 merge 的全部来源依赖。
- **来源撤权即拒绝读取**：`read_merge()` 对每个 build 重新 `authorize_inputs` 并重读 receipt；来源 ACL/export 撤权或 source epoch 变化即抛 `PermissionDeniedError`/`ConflictError`，不静默回退到剩余来源。
- **仍有合法来源时同身份重建**：撤权一个来源后，对仍有合法来源的 build 重新 merge，得到同一 `canonical_id`；`contributors`/`evidence` 只保留剩余合法来源，被撤回来源的贡献被丢弃。
- **不跨 collection 乱写**：merge 强制同 namespace/target_uri/schema 绑定，跨域直接 `SchemaError('cannot cross target namespace')`。
- **人工知识保护**：`human_confirmed` 覆盖层受 `expected_revision` CAS 保护，模型重跑不覆盖人工值，身份定义字段不得被手改。

定向证据：`tests/enterprise_kb/test_wiki_candidate_merge.py` + `tests/enterprise_kb/test_wiki_identity_conservative_merge.py` 共 44 项通过，其中撤回/投影相关含 `test_explicit_rebuild_with_authorized_remaining_source_keeps_identity_but_drops_revoked_contribution`、`test_private_read_rechecks_all_sources_and_does_not_fall_back_to_remaining_source`、`test_standard_same_bytes_write_invalidates_merged_source_epoch`、`test_revoke_export_or_disable_acl_refuses_private_merge_read`、`test_cross_namespace_merge_is_rejected_even_when_both_inputs_authorized`。KB-029 置为 PARTIAL。

仍 pending（故不能 DONE）：零证据事实置 stale/移出诊断热路径、权威边反向投影重建、迁移回滚不覆盖人工知识；诊断发布 `diagnostic_publishable=False`，TC-10.8/10.9/TC-16.16 完整端到端仍未运行。报告见 [I038](evidence/implementation-I038-2026-10-05.json)。


## I048 图片引用发现与 VLM 文本预算实施状态

章节树内采用迭代遍历及分页，不再因深度超过8层漏掉引用；同一来源去重，异常或跨树目录项明确拒绝。当前仍以图片所在章节树为发现范围，不能声称已完成全部外部章节/注册域引用。

`vlm.context_tokenizers` 显式绑定 VLM 模型名、tokenizer.json路径与SHA256；必须覆盖已配置的主/备和credential模型，取各tokenizer的最大文本token数。没有配置时沿用估算，但记录estimated；显式配置损坏或缺失模型则失败，不降级冒充实测。每条引用记录context_token_budget/context_token_count/context_budget_kind。此预算只覆盖引用上下文文本，完整提示词、图像token与输出预留仍需补齐。

context升级为image-context-v3，旧语义缓存需重新执行semantic_and_vectors，不能把旧缓存视作新上下文。72项定向回归通过不代表全文图片语义质量或共同发布完成。
