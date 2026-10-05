# 企业知识库规格入口

> 当前执行状态：I051（2026-10-05）实际持久D2提交成员共同发布及published/backoff SIGKILL恢复通过：单MD 3/3回执，目录3 URI/9回执含L0/L1，恢复未重复建代；完整7门禁仍pending、不标ready。修复已注册空间显式/自动祖先摘要边界。MinerU原始产物丢图已确认并补真实PDF页码/位置保全，客户端冻结上传副本并校验来源指纹；同XLS D1/W1复测各保留1 PNG，D1真实重建11条/0失败、图文向量回执齐全且图片URI可检索，W1四MD/三私有候选到draft_ready。模型图片语义与章节归属质量未验收。最终受影响164 PASS，初测1项兼容失败保留；服务/解析health200、active durable jobs=0。15 Feature/四FLOW仍PARTIAL、33任务32 PARTIAL+1 OPEN、0 DONE，完整验收NOT_RUN。 [本轮报告](evidence/implementation-I051-2026-10-05.json)。

> I050执行状态：I050（2026-10-05）持久D1/D2新增整次reindex批次，真实member回执观测/崩溃恢复；封口在authority同一事务校验job owner/fence/lease，过期worker不得切head。提交成员可共同切换，但完整7类覆盖门禁仍pending，不放ready。旧DOC/XLS/PPT六项实际D1/W1文本/索引/溯源合同通过，私有候选3/2/4；确认XLS可见图在MinerU worker产物阶段缺失（源与PDF有1图，产物0asset/0引用），本轮Office证据整体不通过，复现资产已冻结。联合1061 PASS/1 SKIP，最后封口修改后85定向PASS（与40封口回归重叠）；新batch命令尚未加载线上，15 Feature/四FLOW均PARTIAL，完整验收不提升。 [本轮报告](evidence/implementation-I050-2026-10-05.json)。

> I049执行状态：I049（2026-10-05）新增冻结三一路径适配包与实际执行器：八种path_layout校验目录形状和本地字段对齐；候选验证及合并来源重新执行，回执绑定schema/声明/正文hash和collection root。新适配包未执行标记118→110，仅消除8个实际执行的路径标记，全部正文规则保留。七个原fewshot结构通过，DTC原kb://来源仍拒绝，未猜测映射。受影响191专项通过；当前服务/旧注册schema未改，原包仍118，完整strict诊断/发布/gold/FLOW未验收。 [本轮证据](evidence/implementation-I049-2026-10-05.json)。

> I048执行状态：I048（2026-10-05）修复图片引用深度超过8层时静默漏收，以及误用 embedding tokenizer 作为 VLM tokenizer 的问题。新增 VLM 专属固定 tokenizer 配置、主/备模型覆盖检查和文件指纹校验，截断后重新核验实际字符串预算；引用记录区分 measured/estimated，context 升级 v3。72 项受影响测试通过（含现有真实 VLM 非空摘要协议探针；非质量 gold）。代码已在本地验证，当前服务仍为 I047，未声明新部署链路通过。三个并行子任务因模型服务403中断，未增加其完成证据；共同发布、完整schema和legacy Office仍待实现/实测。15个Feature、四条FLOW及任务整体状态不提升。 [本轮报告](evidence/implementation-I048-2026-10-05.json)。
> I047执行状态：I047（2026-10-05）接通显式 SSH→冻结 Docker Office 转换，默认字体门禁开启；六格式转换合同通过，实际 D1 Word 上传→PDF→规则回退→7 条索引重建/0 失败→限定来源检索通过；W1 DOCX/XLSX/PPTX 三族最终均到私有 draft_ready（2/2/5 个候选），逐字引文核验通过。PowerPoint 原输出错误改写标题符号，v3 提示修复后通过；此前失败记录保留。上传新增原文件指纹，幂等重放不重解析/改源 epoch；已有树替换明确拒绝，整树提交仍待实现。持久 D2 保存真实索引计数并等待共同发布回执；ACL 更新过滤历史代并检测操作期间 head 变化。完整三一 schema 的 118 个待执行规则在实际 strict API 中明确拒绝、无作业创建。联合回归 1008 PASS/1 SKIP；最后修改后受影响范围 141 PASS、Office 59 PASS、上传重放 60 PASS（范围重叠，不相加）。这些是实现/合成合同证据；15 个 Feature 与四条 FLOW 仍 PARTIAL，32 项任务 PARTIAL + KB-032 OPEN，完整产品验收均未通过。 [本轮报告](evidence/implementation-I047-2026-10-05.json) · [Office W1 实测](evidence/flow-w1-office-fontchecked-I047-2026-10-05.json) · [完整 schema 拒绝门禁](evidence/wiki-strict-gate-I047-2026-10-05.json)。

> 历史执行记录：I046（2026-10-05）复核真实 W1 PDF 作业：job `1e73c293bb924638a3f9c4d027ee9fc0` 已正常到 `draft_ready`，耗时 479 秒，捕获 8 篇非空 MD，44 次模型调用、44 份私有草稿、42 个候选。通过实际 SDK/API 逐份读取，全部证据 URI 和引文逐字核验通过，未发布诊断知识。原验收脚本约 300 秒观察窗口过短；现支持继续验收已有作业、默认 1800 秒观察窗口、全部草稿计数/来源/私有回执/逐字引文校验。PDF/Office 路由与章节/Wiki 回归 70 PASS，验收脚本 8 PASS。简化 business/Thing schema 下的切片执行证据不能替代完整三一 schema、全卷质量或发布验收。15 个 Feature 均 PARTIAL，33 项任务为 32 PARTIAL + 1 OPEN，四条 FLOW 均 PARTIAL；完整产品验收尚未通过。 [W1 PDF 实测](evidence/flow-w1-pdf-2026-10-05.json) · [继续验收报告](evidence/flow-w1-pdf-resumed-I046-2026-10-05.json)。

> 历史执行记录：I045（2026-10-05）真实服务+SDK 跑通 FLOW-W2 递归目录源路径：以 `viking://resources/raw/w2rec`（含 `index.md` 与 `chapter/overview.md` 两层、另含同级 jpg）为目录源，`build_wiki(recursive=True)` 捕获两篇嵌套 MD、排除非 MD 图片，execute 到 `draft_ready`（kind W2），候选 `evidence.source_uri` 回指目录内 Viking URI 且候选落在 collection target 树内；harness `tests/enterprise_kb/flows/flow_w2_recursive.py` 七项断言全 PASS，FLOW-W2 记为 **PARTIAL（强化）**（草稿发布、strict_diagnostic、COMMON-1 一致性仍 NOT_RUN）。 [FLOW-W2递归证据](evidence/flow-w2-recursive-2026-10-05.json)。I044（2026-10-05）真实服务+SDK 验证 reindex 语义分层（目标3）：对 `viking://resources/layerflow` 目录递归 `semantic_and_vectors` reindex 重建目录 L0（`.abstract.md` 179 字符）/L1（`.overview.md` 794 字符，真实 VLM 生成）；改动文档后非递归 `vectors_only` reindex 重建向量 1 条且已发布 L0 逐字节不变；harness `tests/enterprise_kb/flows/flow_layers.py` 五项断言全 PASS，F11 记为 **PARTIAL（强化）**（全树 L0/L1 与 agent 定位质量评测仍 NOT_RUN）。 [分层证据](evidence/flow-layers-2026-10-05.json)。I043（2026-10-05）真实服务+SDK 跑通实验性 Wiki 的 W1/W2 两条构建执行链路，均到 `draft_ready`：W1（上传）`temp_upload` → 物化到 `viking://resources/raw/w1-manual.md/w1-manual.md` → prepared 计划 → execute → draft_ready（harness `tests/enterprise_kb/flows/flow_w1.py` 八项断言全 PASS）；W2（URI）选择既有 `viking://resources/raw/manual.md` → prepared 计划 → execute → draft_ready（harness `tests/enterprise_kb/flows/flow_w2.py` 六项断言全 PASS）。两条链路均校验 job kind（W1/W2）、终态 draft_ready、候选实体 `evidence.source_uri` 回指原始 Viking URI、`quote` 与源文逐字一致、候选 URI 落在 collection target 树内；并先经 namespace/restricted-ACL 注册与显式 source→target export 授权。部署侧新增 `embedding.chunking`（enabled / Qwen3-VL-Embedding-8B tokenizer sha256 / publish_content / lexical_enabled / publication_path）并为 flowtest 账户开启 ACL。FLOW-W1/W2 记为 **PARTIAL**（PDF/Office 物化上传、递归目录源、strict_diagnostic、草稿发布与 COMMON-1 一致性仍 NOT_RUN）。 [FLOW-W1证据](evidence/flow-w1-2026-10-05.json) · [FLOW-W2证据](evidence/flow-w2-2026-10-05.json)。I042（2026-10-05）真实服务+SDK 跑通 FLOW-D2 的 Viking MD-URI reindex 路径：`viking://resources/mdflow/manual.md` 文件非递归 reindex（1 rebuilt/0 failed）→ 目录递归 reindex（3 rebuilt/0 failed），正文 `read_raw` 前后逐字节一致，搜索命中全部为可溯源 `viking://` URI 且包含源 MD URI 本身（多 chunk 指向同一来源）；harness `tests/enterprise_kb/flows/flow_d2.py` 五项断言全 PASS，FLOW-D2 记为 **PARTIAL**（L0/L1 层、相对图片共享语义链、父上下文有界、COMMON-1 一致性仍 NOT_RUN）。 [FLOW-D2证据](evidence/flow-d2-2026-10-05.json)。I041（2026-10-05）真实服务+SDK 跑通 FLOW-D1 的 PDF/async-SDK 路径：上传 `slice_flowd1.pdf` → `viking://resources/raw/slice_flowd1`（`structure_source=pdf_bookmarks`、`alignment_status=matched`）→ 章节/同级图落盘 → `semantic_and_vectors` reindex 17 rebuilt/0 failed → 搜索命中全部为可溯源 `viking://` URI（含章节同级图片）；harness `tests/enterprise_kb/flows/flow_d1.py` 四项断言全 PASS，FLOW-D1 记为 **PARTIAL**（MD/Office、sync SDK、对齐回退、头中尾分布仍 NOT_RUN）。 [FLOW-D1证据](evidence/flow-d1-2026-10-05.json)。I040（2026-10-05）修复 reindex 命名空间路径丢失 `image_summaries` 的缺陷，企业分块路径经真实 `qwen3-vl-embed`（messages，4096）实现图文联合 embedding：17 文本 chunk + 22 图文向量全部落库、`failed_records` 22→0、搜索命中携带 chunk_id/parent_chunk_id；部署配置已按用户决定切到 :8020 VL 模型（4096/messages，存量 2560 维向量需 reindex 重建）；I039 真实 SANY 手册切片三后端全页解析通过（mineru 25/25、teleocr 22/22、logics 15/15 图片签名有效）；I038 补录 KB-029 来源撤回/重建为 PARTIAL（44项通过）；I037 定向验证 reindex 语义分层契约（17项）；I034联合回归1575通过、1跳过。仅 KB-032 保持 OPEN；所有15个Feature与四条完整FLOW仍NOT_RUN，完整仓库测试未执行。 [I040证据](evidence/implementation-I040-2026-10-05.json) · [I039证据](evidence/parse-real-manual-I039-2026-10-05.json)。

当前规范版本：**v0.15（2026-10-05）**。

- [未达成任务清单](openviking-enterprise-kb-backlog.md)：33项任务、依赖、完成条件及TC映射，区分局部实现与完整验收。
- [机器可读状态](feature-status.json)：15个Feature和四FLOW当前状态。
- [开发规格](openviking-enterprise-kb-spec.md)：需求R01–R13、F1–F15拆解、存储/解析/检索/Wiki/API契约与M0–M4实施路径。
- [测试与达成追踪](openviking-enterprise-kb-test-cases.md)：fixture计划、逐Feature验收、端到端路径与评测基线。完整产品验收均NOT_RUN；最新验证详见上方I034；I031仅为历史局部证据。
- [审查记录](openviking-enterprise-kb-review.md)：CR-007模式命名/取消/部署证据及CR-006自动回退、URI溯源、队列/快照/GC修订，以及历史审查记录。
- [v0.6评审时文档校验证据](evidence/review-v0.6-2026-10-03.json)：v0.6文件hash、用例映射与一致性检查；不是产品通过报告。
- [历史审查证据](evidence/review-2026-10-03.json)：v0.5源码核查、合成缺陷复现与其他会话的PDF记录；其中的文档hash仅对应旧版。

当前决定：章节对齐失败自动按Markdown标题→内容块规则切分，任务info注明对齐未成功；覆盖与索引合格后正常ready。对外溯源只需Viking URI。分层chunk、图文联合embedding和语义reindex的设计继续保留。

规范优先级：用户明确需求 → 当前开发规格 → 当前验收契约；历史回复或示例目录不覆盖规范。需求变化同时更新需求映射、Feature/子项、测试、实施/迁移路径与证据状态。不要用审查次数或主观百分比表示完成。

- [I001 实施与使用方法](openviking-enterprise-kb-implementation.md)：已实现章节/回退/API/SDK/info及尚未达成项。
- [I001 运行证据](evidence/implementation-I001-2026-10-03.json)：定向代码测试、实现hash与环境限制。

- [I002 三解析后端部署联调](evidence/deployment-I002-2026-10-03.json)：三模型各 12 页产物校验。
- [I003 当前实施证据](evidence/implementation-I003-2026-10-04.json)：取消/产物/默认路由修复、真实 MinerU 章节落盘及全卷任务状态；不等于全部验收通过。

当前主验收为四入口：D1 文件上传+URI 前缀入库、D2 Viking URI 分层索引、W1 文件上传+collection 构建 Wiki、W2 Viking URI+collection 构建 Wiki。详情见规格 §0.1 和测试 FLOW-*；四条完整 E2E 尚未验收。

I004 已加入显式启用的共享文本父子 IndexPlan 和同 URI 多命中保留。完整发布、图文质量及混合检索仍待验收；Wiki配置和来源准备/查询API已接入，抽取/关系发布未完，详见当前任务台账。历史证据 hash 只对应当时版本。

- [I004 文本父子索引与真实模型证据](evidence/implementation-I004-2026-10-04.json)：96 项定向测试、长 Markdown 完整覆盖、三个真实 embedding 输出；非生产检索验收。

- [I005 当前实现证据](evidence/implementation-I005-2026-10-04.json)：表格行组、重建标签、共享调用链、125 项回归与全卷 968 Markdown 分块回放；生产发布与检索仍待验收。

- [I006 当前发布基础证据](evidence/implementation-I006-2026-10-04.json)：131 项回归，文本 generation/快照与向量查询可见性、GC 只读计划；完整企业发布仍待完成。

- [I007 当前正文发布证据](evidence/implementation-I007-2026-10-04.json)：220 项回归，单文本正文与向量共同发布、来源 URI 读取和标准写入 epoch；整本、目录、图文及混合索引发布仍待完成。

- [I008 多文本发布证据](evidence/implementation-I008-2026-10-04.json)：326 项回归，D1/D2 共享文本批次、所有章节等候共同发布与失败保留旧版；目录/图文/BM25/任务 ready/Wiki 仍待完成。

- [I009 目录语义向量发布证据](evidence/implementation-I009-2026-10-04.json)：404 项回归，L0/L1 与文本批次共同发布、部分层/marker 维护和语义失败中止；sidecar 正文快照、图文/BM25/Wiki 仍待完成。

- [I010 目录摘要内容发布证据](evidence/implementation-I010-2026-10-04.json)：420 项回归，sidecar 原 URI/导航读取快照、标准写入修改检测及工作源重建；根结构/图文/BM25/任务 ready/Wiki 仍待完成。


- [I011 图片上下文与联合输入证据](evidence/implementation-I011-2026-10-04.json)：438项回归、首次/URI图片共享producer、严格联合输入保护，以及远端真实图片请求的2560维输出；引用级持久索引/图片共同发布/Wiki仍待完成。


- [I012 图片共同发布证据](evidence/implementation-I012-2026-10-04.json)：446项回归，图片generation/原图reference参加文本+目录同批发布，查询读取旧图和GC保护；引用级索引/多空间/BM25/根可见性/Wiki仍待完成。


- [I013 实际图文适配器证据](evidence/implementation-I013-2026-10-04.json)：472项回归，显式messages协议及真实OpenViking adapter远端joint/query 2560维输出；完整发布/检索与Wiki仍待完成。


- [I014 图片引用独立索引证据](evidence/implementation-I014-2026-10-04.json)：478项回归，逐引用joint vector/member、URI语义结果交接、过期原图摘要拒绝；vectors_only持久缓存、正文依赖/ACL/完整检索/Wiki仍待完成。


- [I015 图片缓存与正文依赖证据](evidence/implementation-I015-2026-10-04.json)：488项回归，private cache/vectors_only复用、标准来源epoch复核与GC roots；in-flight写入fence、目录/ACL依赖与完整E2E/Wiki仍待完成。


- [I016 标准写入期间保护证据](evidence/implementation-I016-2026-10-04.json)：498项回归，四个标准写入入口的持久token、来源及图片/sidecar依赖保护；待确认写入恢复、外部/跨主机修改和四入口完整E2E仍待完成。


- [I017 远端运行复核](evidence/runtime-I017-2026-10-04.json)：三服务健康/capabilities及部署hash匹配，检查时无queued/running；不是新解析E2E。恢复契约见spec§2.3.1与TC-13.12–16，后端确认能力/执行器仍待实现。


- [I018 队列健康修复与远端部署](evidence/runtime-I018-2026-10-04.json)：502项回归、三个worker更新后的running/ready状态；模型质量、自动watchdog及完整四FLOW继续未验收。


- [I019 Wiki schema清点](evidence/wiki-schema-I019-2026-10-04.json)：实际八实体完整包hash、当前关系/来源规则；修正规格过期断言，默认包fewshot冲突仍待适配。清点工具非运行时Wiki构建。


- [I020 Wiki schema实现证据](evidence/implementation-I020-2026-10-04.json)及[三一适配包](evidence/sany-schema-adapter-I020.zip)：516项回归、完整包固定/结构编译；38条自然语言规则及注册/构建API仍待实现，非可上线默认包。


- [I021 Wiki实体检查证据](evidence/implementation-I021-2026-10-04.json)：540项回归、实际Profile结构检查；授权resolver/发布CAS/完整domain规则及两个Wiki入口仍待实现。


- [I022 VikingFS引用适配证据](evidence/implementation-I022-2026-10-04.json)：552项回归、工作内容/epoch读取及固定schema实体验证门禁；可信catalog持久化、生产ACL/export/提交CAS与Wiki入口仍待实现。


- [I023 collection/schema持久化证据](evidence/implementation-I023-2026-10-04.json)：567项回归、内部注册/不可变选择、私有包与GC根；HTTP/SDK、完整域授权和W1/W2仍待完成。


- [I024 配置接口与四入口约定](evidence/implementation-I024-2026-10-04.json)：schema/collection HTTP、同步/异步 SDK、前端生成客户端；573项功能回归通过，5项旧SDK基线失败保留；Wiki上传/URI构建任务仍待实现。


- [I025 Wiki共享来源准备](evidence/implementation-I025-2026-10-04.json)：600项功能回归通过；两输入固定schema/来源版本、幂等/权限/GC根与流式读取；[真实手册968来源回放](evidence/wiki-plan-full-I025-2026-10-04.json)。prepared非构建ready，执行/域授权/抽取与关系发布仍待完成。


- [I026 显式schema门禁与覆盖纠正](evidence/implementation-I026-2026-10-04.json)：647项功能回归通过；[新三一适配包](evidence/sany-schema-adapter-I026.zip)与[八实体审计](evidence/wiki-schema-I026-2026-10-04.json)。七类示例结构通过、DTC旧来源拒绝；共享/实体政策与路径未执行项完整保留，完整Wiki仍待实现。


- [I027实施证据](evidence/implementation-I027-2026-10-04.json)：669项局部PASS；知识域根与单向导出HTTP/SDK配置，执行/提交隔离未完成。


- [I028实施证据](evidence/implementation-I028-2026-10-04.json)：675项局部PASS；W2已有URI准备来源计划，未执行Wiki。


- [I029实施证据](evidence/implementation-I029-2026-10-04.json)：682项局部PASS；W1单Markdown上传物化并准备，PDF/Office与执行未接通。


- [I030实施证据](evidence/implementation-I030-2026-10-04.json)：692项局部PASS；计划查询、上传有界读取与已有幂等键失败不改写保护；四FLOW仍未验收。


- [v0.10规格一致性校验](evidence/review-v0.10-2026-10-04.json)：15个Feature、33项任务、19条新增断言、四FLOW与依赖/引用/版本校验；仅文档检查，不计产品验收。

持续更新时运行 `python docs/spec/tools/check_spec_status.py`。任务标DONE或Feature标PASS必须附相应完整范围报告，不能只修改状态。

- [I031 三agent独立开发与联合验证](evidence/implementation-I031-2026-10-04.json)：KB-001接口兼容、KB-002同机权威registry、KB-031评测冻结底座，三任务均PARTIAL；925项回归和前端生成类型检查通过。

- [I034 联合回归与真实模型探针](evidence/implementation-I034-2026-10-05.json)：1575 PASS/1 SKIP、前端356 PASS；Wiki私有候选抽取、逐片段恢复、execute持久job。

- [I035 Wiki抽取拆解/分发与独立模型段](evidence/implementation-I035-2026-10-05.json)：固定来源抽取器改为“拆解→分发”，每次调用不再携带原始schema包；新增`wiki`模型段(`get_wiki_model`回退`vlm`)；受影响范围94 PASS，远端`svc/deepseek-v4-flash`真实模型探针CONTRACT_PASS。

- [I036 KB-025 身份/保守合并补录](evidence/implementation-I036-2026-10-05.json)：identity.py 实现与14项身份测试通过；台账 OPEN→PARTIAL。
- [I037 reindex 语义分层契约](evidence/implementation-I037-2026-10-05.json)：D2两模式(`vectors_only`不重写已发布L0/L1；`semantic_and_vectors`自底向上重建目录L0/L1与图片摘要)；17项通过；KB-020/F11追加证据。
- [I038 KB-029 来源撤回/重建补录](evidence/implementation-I038-2026-10-05.json)：来源撤权即拒绝读取、仍有合法来源时同身份重建并丢弃撤回贡献、跨域拒绝、人工值CAS保护；44项通过；台账 OPEN→PARTIAL。
- [I039 真实 SANY 手册切片三后端解析](evidence/parse-real-manual-I039-2026-10-05.json)：真实手册 11 页切片（含 TOC 元数据）经 mineru/teleocr/logics 各 11/11 页通过，图片资产签名全部有效，后端 markdown 互异；KB-009/KB-010 追加证据，F1 仍 PARTIAL（全卷与 D1 未验收）。

- [I040 reindex 图文联合闭环](evidence/implementation-I040-2026-10-05.json)：修复 `reindex_executor` 命名空间路径丢弃 `image_summaries`；企业分块路径经真实 `qwen3-vl-embed`（messages 协议，4096 维）联合 embedding，17 文本 chunk + 22 图文联合向量全部落库，`failed_records` 22→0，搜索命中携带 `chunk_id/parent_chunk_id`；KB-013/014/020 追加证据，F5/F9 仍 PARTIAL。

- [v0.11规格一致性校验](evidence/review-v0.11-2026-10-05.json)：15个Feature、33项任务、19条断言、四FLOW与依赖/引用/版本校验；仅文档检查，不计产品验收。
