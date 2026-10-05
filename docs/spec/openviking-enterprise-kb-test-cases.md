# OpenViking 企业知识库测试与达成追踪

> 当前执行状态：I051（2026-10-05）实际持久D2提交成员共同发布及published/backoff SIGKILL恢复通过：单MD 3/3回执，目录3 URI/9回执含L0/L1，恢复未重复建代；完整7门禁仍pending、不标ready。修复已注册空间显式/自动祖先摘要边界。MinerU原始产物丢图已确认并补真实PDF页码/位置保全，客户端冻结上传副本并校验来源指纹；同XLS D1/W1复测各保留1 PNG，D1真实重建11条/0失败、图文向量回执齐全且图片URI可检索，W1四MD/三私有候选到draft_ready。模型图片语义与章节归属质量未验收。最终受影响164 PASS，初测1项兼容失败保留；服务/解析health200、active durable jobs=0。15 Feature/四FLOW仍PARTIAL、33任务32 PARTIAL+1 OPEN、0 DONE，完整验收NOT_RUN。 [本轮报告](evidence/implementation-I051-2026-10-05.json)。

> I050执行状态：I050（2026-10-05）持久D1/D2新增整次reindex批次，真实member回执观测/崩溃恢复；封口在authority同一事务校验job owner/fence/lease，过期worker不得切head。提交成员可共同切换，但完整7类覆盖门禁仍pending，不放ready。旧DOC/XLS/PPT六项实际D1/W1文本/索引/溯源合同通过，私有候选3/2/4；确认XLS可见图在MinerU worker产物阶段缺失（源与PDF有1图，产物0asset/0引用），本轮Office证据整体不通过，复现资产已冻结。联合1061 PASS/1 SKIP，最后封口修改后85定向PASS（与40封口回归重叠）；新batch命令尚未加载线上，15 Feature/四FLOW均PARTIAL，完整验收不提升。 [本轮报告](evidence/implementation-I050-2026-10-05.json)。

> I049执行状态：I049（2026-10-05）新增冻结三一路径适配包与实际执行器：八种path_layout校验目录形状和本地字段对齐；候选验证及合并来源重新执行，回执绑定schema/声明/正文hash和collection root。新适配包未执行标记118→110，仅消除8个实际执行的路径标记，全部正文规则保留。七个原fewshot结构通过，DTC原kb://来源仍拒绝，未猜测映射。受影响191专项通过；当前服务/旧注册schema未改，原包仍118，完整strict诊断/发布/gold/FLOW未验收。 [本轮证据](evidence/implementation-I049-2026-10-05.json)。

> I048执行状态：I048（2026-10-05）修复图片引用深度超过8层时静默漏收，以及误用 embedding tokenizer 作为 VLM tokenizer 的问题。新增 VLM 专属固定 tokenizer 配置、主/备模型覆盖检查和文件指纹校验，截断后重新核验实际字符串预算；引用记录区分 measured/estimated，context 升级 v3。72 项受影响测试通过（含现有真实 VLM 非空摘要协议探针；非质量 gold）。代码已在本地验证，当前服务仍为 I047，未声明新部署链路通过。三个并行子任务因模型服务403中断，未增加其完成证据；共同发布、完整schema和legacy Office仍待实现/实测。15个Feature、四条FLOW及任务整体状态不提升。 [本轮报告](evidence/implementation-I048-2026-10-05.json)。
> I047执行状态：I047（2026-10-05）接通显式 SSH→冻结 Docker Office 转换，默认字体门禁开启；六格式转换合同通过，实际 D1 Word 上传→PDF→规则回退→7 条索引重建/0 失败→限定来源检索通过；W1 DOCX/XLSX/PPTX 三族最终均到私有 draft_ready（2/2/5 个候选），逐字引文核验通过。PowerPoint 原输出错误改写标题符号，v3 提示修复后通过；此前失败记录保留。上传新增原文件指纹，幂等重放不重解析/改源 epoch；已有树替换明确拒绝，整树提交仍待实现。持久 D2 保存真实索引计数并等待共同发布回执；ACL 更新过滤历史代并检测操作期间 head 变化。完整三一 schema 的 118 个待执行规则在实际 strict API 中明确拒绝、无作业创建。联合回归 1008 PASS/1 SKIP；最后修改后受影响范围 141 PASS、Office 59 PASS、上传重放 60 PASS（范围重叠，不相加）。这些是实现/合成合同证据；15 个 Feature 与四条 FLOW 仍 PARTIAL，32 项任务 PARTIAL + KB-032 OPEN，完整产品验收均未通过。 [本轮报告](evidence/implementation-I047-2026-10-05.json) · [Office W1 实测](evidence/flow-w1-office-fontchecked-I047-2026-10-05.json) · [完整 schema 拒绝门禁](evidence/wiki-strict-gate-I047-2026-10-05.json)。

> 历史执行记录：I046（2026-10-05）复核真实 W1 PDF 作业：job `1e73c293bb924638a3f9c4d027ee9fc0` 已正常到 `draft_ready`，耗时 479 秒，捕获 8 篇非空 MD，44 次模型调用、44 份私有草稿、42 个候选。通过实际 SDK/API 逐份读取，全部证据 URI 和引文逐字核验通过，未发布诊断知识。原验收脚本约 300 秒观察窗口过短；现支持继续验收已有作业、默认 1800 秒观察窗口、全部草稿计数/来源/私有回执/逐字引文校验。PDF/Office 路由与章节/Wiki 回归 70 PASS，验收脚本 8 PASS。简化 business/Thing schema 下的切片执行证据不能替代完整三一 schema、全卷质量或发布验收。15 个 Feature 均 PARTIAL，33 项任务为 32 PARTIAL + 1 OPEN，四条 FLOW 均 PARTIAL；完整产品验收尚未通过。 [W1 PDF 实测](evidence/flow-w1-pdf-2026-10-05.json) · [继续验收报告](evidence/flow-w1-pdf-resumed-I046-2026-10-05.json)。

> 历史执行记录：I045（2026-10-05）真实服务+SDK 跑通 FLOW-W2 递归目录源路径：`viking://resources/raw/w2rec`（两层 MD + 同级 jpg）recursive 捕获两篇嵌套 MD、排除非 MD 图片，draft_ready（kind W2），候选溯源目录内 URI 且落 target 树内，`tests/enterprise_kb/flows/flow_w2_recursive.py` 七项断言全 PASS，FLOW-W2 记为 **PARTIAL（强化）**。I044（2026-10-05）真实服务+SDK 验证 reindex 语义分层（目标3）：目录递归 `semantic_and_vectors` 重建 L0（`.abstract.md` 179 字符）/L1（`.overview.md` 794 字符，真实 VLM 生成），改文后非递归 `vectors_only` 重建向量 1 条且已发布 L0 逐字节不变，harness `tests/enterprise_kb/flows/flow_layers.py` 五项断言全 PASS，F11 记为 **PARTIAL（强化）**（全树 L0/L1 与 agent 定位质量评测仍 NOT_RUN）。I043（2026-10-05）真实服务+SDK 跑通实验性 Wiki 的 W1（上传）与 W2（URI）两条构建执行链路，均到 `draft_ready`：W1 `temp_upload` → 物化 `viking://resources/raw/w1-manual.md/w1-manual.md` → prepared 计划 → execute → draft_ready（`tests/enterprise_kb/flows/flow_w1.py` 八项断言全 PASS）；W2 选择既有 `viking://resources/raw/manual.md` → prepared 计划 → execute → draft_ready（`tests/enterprise_kb/flows/flow_w2.py` 六项断言全 PASS）。两条均校验 job kind（W1/W2）、终态 draft_ready、候选 `evidence.source_uri` 回指原始 Viking URI、`quote` 逐字一致、候选 URI 落在 collection target 树内，并先经 namespace/restricted-ACL 与显式 source→target export 授权；FLOW-W1/W2 记为 **PARTIAL**（PDF/Office 物化上传、递归目录源、strict_diagnostic、草稿发布与 COMMON-1 一致性仍 NOT_RUN）。I042（2026-10-05）真实服务+SDK 跑通 FLOW-D2 的 Viking MD-URI reindex 路径（`viking://resources/mdflow/manual.md` 文件非递归 1 rebuilt/0 failed → 目录递归 3 rebuilt/0 failed；正文 read_raw 前后逐字节一致；搜索命中全部为可溯源 viking:// URI 且包含源 MD URI 本身），harness `tests/enterprise_kb/flows/flow_d2.py` 五项断言全 PASS，FLOW-D2 记为 **PARTIAL**；I041（2026-10-05）真实服务+SDK 跑通 FLOW-D1 的 PDF/async-SDK 路径（上传→书签章节树 matched→章节/同级图落盘→reindex 17 rebuilt/0 failed→搜索命中全部为可溯源 viking:// URI），harness `tests/enterprise_kb/flows/flow_d1.py` 四项断言全 PASS，FLOW-D1 记为 **PARTIAL**；I038（2026-10-05）补录 KB-029 来源撤回/重建为 PARTIAL（44项通过）；I037 定向验证 reindex 语义分层契约（17项）；I036补录 KB-025 为 PARTIAL（44项）；I034联合回归1575通过、1跳过。仅 KB-032 保持 OPEN；所有15个Feature与四条完整FLOW仍NOT_RUN，完整仓库测试未执行。 [I038证据](evidence/implementation-I038-2026-10-05.json)。

> v0.15 · 2026-10-05 · 配套：[开发规格](openviking-enterprise-kb-spec.md)、[审查与证据](openviking-enterprise-kb-review.md)。
> 本文是**待落地的验收测试设计**，不是已实现测试代码。完整目标验收当前仍 NOT_RUN；I031最终925项局部回归通过，旧5项SDK基线失败已解决（4项实现修复、1项过期安全预期纠正），完整仓库测试未执行；I030的692项保留为历史证据；见当前任务台账与下方历史记录；历史源码核查、合成缺陷复现和本轮文档检查均不计产品通过。无运行记录时不写“0/N 失败”，不估算完成百分比。

## 0. 测试组织与状态

执行分层：U=离线单元/属性测试；C=HTTP/SDK/后端契约；I=真实服务集成与故障注入；Q=人工金标质量；E=端到端。测试通过需绑定 `run_id + git revision/worktree digest + spec version + fixture hashes + model/adapter/index versions + config + environment + report`。

状态：NOT_RUN（未执行）、PASS、FAIL、BLOCKED（已有运行受外部条件阻断）、STALE（实现/需求变化后过期）。设计文档中的“具备基建”独立于验收状态；只在已有 run_id 的证据上更新结果。真实模型不可用不能用 mock 的 PASS 替代其 I/Q/E 验收。

### Fixture 计划

| ID | 内容与独立 oracle | 用途 |
|---|---|---|
| FX-PDF-S | 自建可公开小 PDF：命名目标/页对象目标/动作目标、三级书签、同页兄弟、空分支、重复标题、无书签；生成时记录独立预期书签与正文 block IDs | 不用被测 fitz 输出反过来当正确答案 |
| FX-MD-A | 手工标注 Markdown/块流，含原书 TOC、漏 `#` 标题、相同编号、代码/表格、首尾正文、跨页表、图片及引用式链接 | 对齐、无损覆盖、图片定位 |
| FX-PARSE-{M,T,L} | 固定官方版本三模型对同组样本的原始响应及人工核对的标准化结果；失败/截断/缺图响应另存 | Adapter 回放与格式漂移；不能强求三模型图片张数完全相同 |
| FX-OFFICE | doc/docx、xls/xlsx、ppt/pptx，每类含中文字体、合并单元格/长表、隐藏页/打印范围、跨页图文、加密/损坏样本 | 转 PDF 内容覆盖与失败语义 |
| FX-LONG-MD | 超过配置 token 上限的 md；答案分别在头/中/尾，parent 下有多个相关 child；表格/代码/公式/相对图 | 禁止尾部截断和同 URI 粗暴去重 |
| FX-META | SY215、SY215C、SY215H、SY21、别名/空白/错字、主体码相差一位、缺值；人工指定可接受候选 | 模糊召回与诊断配置严格确认 |
| FX-ACL | 同 account 两个 namespace、另一个 account 同名 URI；reader/writer/admin/无权限用户；越域图片/关系/缓存 | 真正的授权隔离 |
| FX-WIKI | 八实体最小有效图及无 Profile/无来源/悬空 Fault/双锚 Profile/错误别名合并/多来源冲突的反例 | 引用门禁与三态 |
| FX-FAIL | 可控 429/5xx/超时/断连、worker kill、lease 过期、对象写成功索引失败、取消竞争、磁盘配额 | 状态、恢复与幂等 |
| FX-SY215C | 用户真实手册；按人工核验书签/正文边界/图用途/维修问答建立冻结 gold | 完整 I/Q/E 验收 |

真实手册路径：`/Users/wh/Library/Containers/com.tencent.WeWorkMac/Data/Documents/Profiles/F2CEDF129A304910ABC861ACC052578C/Caches/Files/2026-08/c8ffb0db8421da2e381984f63349747d/SY215C_SY021NCB62788_维修手册_zh.pdf`。

2026-10-04 已重新验证授权临时副本，**1072 页、491 书签**（L1=11/L2=76/L3=330/L4=74），大小 379,089,082 字节，SHA256 `99a3a0d37696c7f76f8364a641e06b6a100d8e57f7030c1715beae0487196534`。原 WeWork 路径受应用数据访问权限限制，使用 Finder 复制的临时测试副本及远端 SHA 一致输入。此结果证明可读和规模，**不是章节边界 gold**；TC-2.13 仍待独立正文核对。

模型服务器 `10.147.254.3:/home/t-wanghai/models/openviking-parsers` 上三后端已部署，各完成 12 页产物契约联调；I003 再次确认三个健康状态及 MinerU 真实远程解析→章节磁盘写入。12 页样例无书签且以封面/目录为主，仅证明链路，不抵消完整正文/图片/章节质量验收。完整 1072 页 MinerU 产物覆盖校验已通过（1072/1072 页、8366 块、2288 图片资产、366.56 秒），章节/正文 gold 和检索仍待验收；其他后端全卷、质量评测、生产入库和检索仍未完成。凭据不得进入 fixture/report。

建议测试代码位置为 `tests/enterprise_kb/`，前端测试就近放 `web-studio/src/routes/tasks/`。本轮不创建空测试桩冒充通过。

## 0.1 四入口主验收（CR-008；完整 E2E 均 NOT_RUN）

| ID | 输入与操作 | 必须断言 | 依赖 |
|---|---|---|---|
| FLOW-D1 | HTTP 和 async/sync SDK 上传 PDF、MD、Office 并指定 parent 前缀 | 输出根追加文件名；PDF meta 树与模型正文结合，对齐失败自动规则回退且 info 可见；章节/同级图已写入才索引；头中尾检索可命中；URI 可读 | F1–F8,F13,F14 |
| FLOW-D2 | 指定现有长 MD URI 与目录 URI，递归/非递归分别 reindex | 不调用 OCR，不修改正文/目录/重切章；子块完整覆盖、父上下文有界；多个命中引用同一原 URI；L0/L1 重建；相对图片走共享语义链 | F4–F7,F9,F11,F13,F14 |
| FLOW-W1 | 上传 PDF/Office/MD，指定已注册 collection | schema 为该 collection 固定版本；来源落配置 raw_sources_prefix；按固定schema仅构建有证据的实体及合法关系，不强求八类齐全；实体 MD 和来源 URI 可读；无第二套检索 | F1–F5,F8,F10,F13,F14 |
| FLOW-W2 | 指定文件/目录 URI 与 collection | 已有 MD 不重复 OCR；沿用同一 WikiBuildPlan、归一/关系/存储格式；来源引用输入 URI；遵守源读/目标写/跨域 export 权限 | F9–F11,F13,F14 |

- FLOW-COMMON-1：用相同 Markdown 内容与固定模型响应分别走 D1/D2，扣除预期来源 URI 不同后，分块正文、模型输入预算、父子关联与索引策略一致。相同 URI/revision/config 重试得到稳定 chunk ID；不同来源 URI 不能要求 ID 相同。
- FLOW-COMMON-2：同一 schema 版本与固定抽取响应分别走 W1/W2，实体归一、关系与冲突策略一致；各自 sources 必须正确，不能要求不同来源 URI 的产物 hash 完全相同。
- FLOW-COLLECTION-1：同 account 两事业部同名文件/实体，在不同 collection 中独立构建和查询；实体、关系、图片、来源、任务日志、缓存均无越域暴露。collection 名不是物理向量库名。
- FLOW-COLLECTION-2：未注册 collection、未配置/无效 schema、无读源/写目标/export 权限，在模型调度前失败；兼容参数与注册配置冲突返回 409；任务运行期间 schema 更新不改变该任务固定版本。
- FLOW-INPUT-1：文件 ID 和 source_uris 混传拒绝；D1 prefix 和旧 to 不混用；D2 的二进制 PDF/Office 明确拒绝或引导显式解析，不能字节转文本。W1 上传模板必须走 schema 注册，不当知识来源。
- FLOW-PUBLISH-1：写入成功、embedding 失败、取消或 schema 校验失败，不返回 ready；恢复后不产生重复实体/向量；新索引未完整发布前保持旧代次可查询。

以上是验收定义，不能把单元测试或解析工件通过直接提升四入口为 PASS。现阶段具体证据见实施记录与 I004 报告。

## 1. Feature 达成矩阵

| Feature | 现状 | 目标验收 | 对应测试 | 完成条件 |
|---|---|---|---|---|
| F1 | I001–I003/I018：三模型12页及MinerU全卷；全后端质量/正式job链未验收 | NOT_RUN | TC-1.*；KB-001,KB-009,KB-010 | 三模型分别满足适配、模式、HTTP/SDK、Docker 运行与质量 |
| F2 | I001/I003：书签meta、自动回退及全卷章节产物；gold结构与完整发布待验收 | NOT_RUN | TC-2.*；KB-006,KB-010,KB-011 | 优先书签对齐，失败自动标题/规则回退并写info；完整覆盖、图片与元信息一致 |
| F3 | 任务/UI/worker健康基建；企业持久job、共同租约与恢复未闭环 | NOT_RUN | TC-3.*；KB-004,KB-021 | 多 worker 仍串行、写入/索引屏障和恢复可观测 |
| F4 | I004–I008：tokenizer父子计划、多chunk与代次屏障；真实完整搜索待验收 | NOT_RUN | TC-4.*；KB-006,KB-013 | 文本尾部覆盖、子母块与多片段引用 |
| F5 | I011–I015：引用上下文/缓存/joint实际向量及共同发布；真实VLM质量、多空间搜索与引用分页未完 | NOT_RUN | TC-5.*；KB-006,KB-014,KB-015 | 章节上下文、摘要+图片联合输入和图文空间一致 |
| F6 | I003叶子兜底修复；BM25倒排、完整RRF/rerank/MMR与质量消融未闭环 | NOT_RUN | TC-6.*；KB-016,KB-017 | 真 BM25、融合、rerank+MMR、导航失败兜底、可验证引用 |
| F7 | 现有标签/过滤算子不能替代权威meta、模糊候选字典与性能验收 | NOT_RUN | TC-7.*；KB-018,KB-019 | 模糊候选精度/性能达标且不放宽硬约束 |
| F8 | 目标Office→PDF→统一解析未接通；AnyDoc→md不计完成 | NOT_RUN | TC-8.*；KB-012 | 六扩展名转 PDF、无书签回退及内容质量 |
| F9 | 已有URI入口与共享计划；原URI完整图文/搜索/授权E2E待验收 | NOT_RUN | TC-9.*；KB-013,KB-020 | 原 md 不改写，分层索引、图片与溯源可用 |
| F10 | I020–I030：模板/结构门禁/catalog、W1/W2准备与查询；完整规则、抽取、关系/实体发布和增量未完 | NOT_RUN | TC-10.*；KB-022,KB-023,KB-024,KB-025,KB-026,KB-027,KB-028,KB-029,KB-030 | 发布有效图无悬空，配置锚与来源正确，增量不覆盖人工知识 |
| F11 | I009–I016：目录/图文语义与版本保护；根可见性、词法/权威meta及job屏障待完成 | NOT_RUN | TC-11.*；KB-006,KB-020 | 首次入库/重建同契约、语义传播、原子切代 |
| F12 | 只完成设计启发和概率边界；独立PoC未运行 | NOT_RUN | TC-12.*；KB-033 | 类型化状态、拒判、独立评测与校准结论 |
| F13 | 持久发布/依赖根/写入保护与部分幂等；恢复、删除/物理GC/配额/备份未闭环 | NOT_RUN | TC-13.*；KB-002,KB-004,KB-005,KB-006,KB-007,KB-008,KB-029 | 幂等/版本/删除/TTL/恢复/配额 |
| F14 | I027知识根/export配置及局部门禁；全入口预过滤、身份刷新与提交fencing未完成 | NOT_RUN | TC-14.*；KB-003,KB-022,KB-027 | 所有读写与派生通道授权，零越权样本 |
| F15 | 候选及局部测试/证据已保存；独立gold、生产性能和完整FLOW验收未完 | NOT_RUN | TC-15.*；KB-010,KB-031,KB-032 | gold 与合成样本分离、版本证据及变更一致 |

子项状态继承 Feature 的 NOT_RUN，直到分别附运行证据；主 spec §10 的每个 F 子项必须映射至本表测试族。部分子项通过可单独记账，不能提前将整个 Feature 标 PASS。

## 2. 详细验收用例

每条均给出输入/操作与可观察结果。除显式标注目标值外，结构正确性是零容忍不变量。

### TC-1 解析后端与 API/SDK

| ID | 层 | 输入/操作 | 验收结果 |
|---|---|---|---|
| TC-1.1 | C/I/Q | FX-PARSE-M 与真实 MinerU 锁定版本解析 | 标准化内容/图引用匹配人工 gold；完整处理指定页范围，无默认分页截断 |
| TC-1.2 | C/I/Q | TeleOCR Detection/Segmentation 实测及回放 | effective backend 为 teleocr；中间块、md、图片可关联，TextIn 不能通过此项 |
| TC-1.3 | C/I/Q | Logics V3 多窗口文档 | 顺序/跨页结构状态保留，md/hierarchy/metadata 适配；图片必须可引用或明确缺失失败 |
| TC-1.4 | C | 请求不支持的 mode/option，或未指定 backend | 使用 model_mode（不混用 args.parse_mode）；422 表明能力缺失；默认模型由配置决定且响应可见；Understanding 不旁路已注册默认模型 |
| TC-1.5 | I | 后端断连/超时/空输出/截断 | job failed 或明确 retryable；未允许 fallback 时不能静默换模型，空结果不成功；取消传播到底层引擎，迟到产物/异常不覆盖 cancelled |
| TC-1.6 | C | HTTP 提交 parser、parent、namespace、meta、index options | 服务到队列再到 adapter 的 effective options 全一致，额外非法字段拒绝 |
| TC-1.7 | C | Python async/sync SDK 各提交同请求并等待 | 序列化与 HTTP 一致；等待超时后同 job 可查询，错误码不丢失 |
| TC-1.8 | I | 三容器逐一冷启动/重启；记录 digest/资源配置 | 健康检查、模型版本、恢复和产物均可复现；仅启动 HTTP 服务不等于模型 ready |

### TC-2 书签与内容对齐

| ID | 层 | 输入/操作 | 验收结果 |
|---|---|---|---|
| TC-2.1 | U/I | FX-PDF-S 命名目标/对象引用/动作目标 | 与生成时 gold 的标题/层级/页目标一致；不存在 bytes 首字节变 p113 |
| TC-2.2 | U | 对同一内容提供带页码、无页码和乱序 page meta | 有效内容块的章节归属一致；page meta 错误仅影响展示告警，不影响切分 |
| TC-2.3 | U/Q | FX-MD-A 原目录与正文同名、编号在表格和代码中 | 切分点只落正文合法候选，目录内容保留且不误切 |
| TC-2.4 | U/Q | 标题无 `#`、OCR 空格/全半角、重复标题 | 适配候选与全局路径/顺序匹配符合人工边界；不能只用 string contains |
| TC-2.5 | U | 结构空父节点、相同位置别名书签 | 标记 structural_only/validated_alias；不复制子正文、不假报标题命中 |
| TC-2.6 | U/I | 缺标题、两个同分候选、内容与 TOC 顺序冲突 | 自动整本回退到Markdown标题/规则切分，无人工等待；任务info记录BOOKMARK_ALIGNMENT_FAILED和实际策略，完整覆盖后继续入库 |
| TC-2.7 | U | 任意含首尾内容的 block 流拆分再还原 | 去掉系统生成头部后，各源 block 恰好出现一次，顺序与正文 hash 可验证 |
| TC-2.8 | U/I | 成功书签路径中小节很短、分支own-body和多个子节 | 不合并书签小节；叶子.md、分支index.md；分支不重复收录子树；回退路径不受书签节点数约束 |
| TC-2.9 | U | 同页兄弟、父子重叠、倒序/远程目标页码 | 允许重叠，end_hint 不小于 start，非法则 null+warning；前置内容不强行标 p1–20 |
| TC-2.10 | I | 多叶共享目录、同图跨两章、前置图 | images 就近，无全书总池；每 occurrence 归属 source_uri，所有持久相对引用可读回 |
| TC-2.11 | U/C | 同名/去编号碰撞、`../`、编码斜线、超长标题 | 安全稳定目录名，不越域、不覆盖；toc.md 指向真实 .md，不泄露宿主路径 |
| TC-2.12 | C/I | 默认bookmarks_preferred输入无书签PDF或Office转换结果 | 自动标题/规则切分，info记录BOOKMARKS_UNAVAILABLE；结构来源真实，完整内容可入库，无书签不单独导致失败 |
| TC-2.13 | Q/E | 完整SY215C，逐后端跑结构匹配与回退 | 独立gold统计对齐质量、回退率和实际章节质量；对齐不成功可自动完成但不算对齐命中；正文无丢失、不凑空节点 |
| TC-2.14 | U/I | 对齐失败但md含#标题、跳级标题、前置正文、围栏内伪标题 | 只按合法标题AST建树，跳级挂最近浅层；前后内容不丢，不混用未提交书签树；toc.md描述实际树 |
| TC-2.15 | U/I | 对齐失败且无可用标题，含长表/代码/图；另测单段短文 | 自动rule_blocks顺序成章，表图归属有效；大不可分块留后续chunk处理，短文一章也能ready |
| TC-2.16 | I | 注入空输出、丢图/丢正文或规则处理异常 | 不可用产物仍失败且可观测；不能以对齐回退info掩盖真正解析/覆盖错误 |

### TC-3 队列与前端

| ID | 层 | 输入/操作 | 验收结果 |
|---|---|---|---|
| TC-3.1 | I | 两 worker 同时提交 3 本、覆盖内置与外部路径 | 解析资源池实际最大活跃文档数=1；以事件时间/租约审计验证，不只看配置 |
| TC-3.2 | I | 第一本文档写入后让 embedding 暂停 | 下一本可解析；第一本 written 可读但未 ready，队列空不误报成功 |
| TC-3.3 | I | 第二本失败，其余成功 | FIFO/公平策略下其他任务继续，失败原因/重试次数可见且内容不串 |
| TC-3.4 | I | kill worker、租约过期、旧 worker 恢复 | 新 owner 接管，旧 fencing token 不得提交；后端按能力从页/窗口/文档恢复 |
| TC-3.5 | I/C | 取消、客户端 wait 超时、重复 retry 并发 | 取消有确定终态并清理；wait 超时不取消；同阶段幂等无多写 |
| TC-3.6 | C/I | 前端tasks/monitoring与HTTP、sync/async SDK对照 | 阶段/进度/终态一致，info[]和alignment_status/structure_source透传且重启后保留；回退成功显示ready及对齐未成功说明；无权者不可见 |
| TC-3.7 | I | 多 namespace 长队列与优先级变化 | 有界等待/公平调度；日志 trace 可追全链路且不输出凭据 |
| TC-3.8 | I/C | A对齐失败自动回退，B/C排队；A写入后暂停embedding | A自动写入并释放槽，B/C继续；A完成后ready+info，无人工阻塞；默认不会等A的embedding才解析B |
| TC-3.9 | I | 对齐超时→回退；回退异常、取消、重试退避、worker崩溃 | 时间预算有限；正常退出均释放解析槽，崩溃后租约回收；重试重新排队，无永久占槽或旧token写入 |

### TC-4 文本子母 chunk

| ID | 层 | 输入/操作 | 验收结果 |
|---|---|---|---|
| TC-4.1 | U/I | FX-LONG-MD 分块、索引、查询尾部专有事实 | 所有正文覆盖；除规定 overlap 不重复；尾部可以召回，无全文截断 |
| TC-4.2 | U | 中文长表/代码/公式跨 token 上限 | child 输入含标题也不超模型上限；表头/语法/连续关系完整 |
| TC-4.3 | U/I | 多 child 关联同 parent | parent 只作上下文；召回 child 时扩展正确 parent，预算不超限 |
| TC-4.4 | I | 同URI两个远距chunk分别支持答案 | 聚合展示保留两条命中内容，共同指向原URI；不要求原文位置，不因URI相同丢掉第二条内容 |
| TC-4.5 | I | 同 revision/配置重建，再改正文/分块配置 | 前者稳定 ID/幂等；后者新代替换，旧 chunk 不参与 active 搜索 |
| TC-4.6 | U/I | 回填每child来源，不提供位置或版本参数 | 只用source_uri经现有Viking读取接口可打开来源；来源文档正确，不要求span/hash精确匹配 |
| TC-4.7 | U/I | 跨块长表重复表头、添加标题前缀后检索 | 完整chunk可召回并返回正确章节/原md URI；不要求加工后的embedding文本对应连续原文位置 |

### TC-5 图片上下文与联合向量

| ID | 层 | 输入/操作 | 验收结果 |
|---|---|---|---|
| TC-5.1 | C/Q | 同一图片放不同章节及图注/前后文 | VLM 请求实际含图和该 occurrence 上下文；人工核对用途，不只断言含章节名 |
| TC-5.2 | C/I | 捕获真实多模态 embedding 请求 | **同一输入同时有摘要和原图**；返回向量的 space ID/维度吻合 |
| TC-5.3 | C/I | 文本-only embedder 执行 required joint | 显式失败；允许降级时 ready_degraded 且报告 text_only，不能将 F5 标 PASS |
| TC-5.4 | I/Q | 文本搜索图，文本/图向量来自不同空间 | query 分别正确编码，排名融合；不可直接混算 cosine，相关图片命中 gold |
| TC-5.5 | I | 图片相同但上下文改变，prompt/model 改变 | 旧摘要缓存失效、引用语境隔离；无上下文明确 unknown，不伪造用途 |
| TC-5.6 | I | images 丢失、跨域图片、重复图、VLM 超时 | 无权拒绝，缺图/超时可见并按必需项阻止 ready；引用归属无误 |

### TC-6 混合检索、重排与溯源

| ID | 层 | 输入/操作 | 验收结果 |
|---|---|---|---|
| TC-6.1 | I | 禁用 dense 跑 DTC/型号/中文精确术语查询 | 真 BM25 命中，trace 有词法分数与索引版本；不能用 sparse 模型冒充 |
| TC-6.2 | I/Q | 禁用 BM25 跑语义改写，再开启融合 | dense 与混合对各自 gold 有召回；RRF 可复算，不直接相加异空间原始分数 |
| TC-6.3 | C/I | 固定候选，切换 rerank/MMR；lambda=0/1/默认 | 顺序为融合→rerank→MMR；归一化与稳定 tie-break，极值行为符合定义 |
| TC-6.4 | I | 目录摘要为空/错误、导航零结果；ACL on/off，rerank 有/无 | 每种合法配置仍召回授权 L2；关闭 ACL 不得使叶子兜底消失 |
| TC-6.5 | I | 正文、图片、同URI多chunk；点击前源被删除/撤权 | 正常结果来源URI可读且相关；不提供精确位置/版本号也能打开；删除/撤权按现有错误语义处理，无虚构chunk文件地址 |
| TC-6.6 | I | rerank 超时或返回数量不符 | 严格策略失败；允许策略回退融合分并标记；不能错位关联候选 |
| TC-6.7 | Q | 冻结评测集消融：dense/BM25/hybrid/+rerank/+MMR | 报 Recall/NDCG/多样性与置信区间；不能用单条排名上升代替总体提升 |

### TC-7 业务 meta

| ID | 层 | 输入/操作 | 验收结果 |
|---|---|---|---|
| TC-7.1 | C/I | 传可选 meta 或省略；尝试覆盖 account/namespace | 原值/规范值/来源正确，省略不清空已有 meta；系统字段覆盖拒绝 |
| TC-7.2 | U/Q | FX-META：大小写、空白、别名、前缀 sy21、错字 | canonical 候选符合人工允许集合；返回解析方式/歧义，非任意正则放宽 |
| TC-7.3 | C/I | 同字段多候选，modelid 与 subject_code 联合 | 同字段 OR、跨字段 AND；BM25/向量一致过滤 |
| TC-7.4 | C/I | 显式 filter 无命中与低置信 query hint | 硬过滤不静默删除；hint 默认 boost；缺 meta 仅显式 include_unknown 可进入约束结果 |
| TC-7.5 | U/I | 型号相似但配置标准不同 | 可辅助找资料，但诊断不能自动选错误 ConfigSpec/Profile |
| TC-7.6 | I | 对本期支持的各存储 adapter 执行同一 filter 集 | contains/prefix/fuzzy 结果符合统一语义；后端无能力明确拒绝或走声明的字典层 |
| TC-7.7 | I | 10万 chunk/1万值、并发5、冷暖分开测 | 元数据候选 p95≤100ms 的目标有硬件/样本记录；同时报候选膨胀/精度和扫描量 |

### TC-8 Office

| ID | 层 | 输入/操作 | 验收结果 |
|---|---|---|---|
| TC-8.1 | I/Q | doc/docx 转 PDF 再解析 | 中文字体/图文完整，书签能用则用；无书签走 content_headings |
| TC-8.2 | I/Q | xls/xlsx 多 sheet/长表/合并单元格/打印范围 | 可读单元格值、表头、单位不丢，sheet 来源可定位；不把公式执行语义算入承诺 |
| TC-8.3 | I/Q | ppt/pptx 图文、多页同标题 | 保留顺序和图用途，不混章；无书签仍可完成 |
| TC-8.4 | I | 字体缺失、密码、坏文件、超时、宏文档 | 清晰转换错误，禁宏，不能 AnyDoc→md 静默绕过已选管线 |
| TC-8.5 | I | 转换后/解析后/索引失败时重试 | 原 Office/中间 PDF 按 TTL 清理；已持久结果无需原件重新解析 |

### TC-9 原 URI Markdown

| ID | 层 | 输入/操作 | 验收结果 |
|---|---|---|---|
| TC-9.1 | I | 指定现有 md URI reindex，记录调用和前后 hash | parser 调用次数=0，正文与 URI 不变；只更新派生索引 |
| TC-9.2 | I/Q | FX-LONG-MD头/中/尾查询 | 全部gold事实可召回，多个chunk的source_uri均为原URI；可保留命中正文，但无需精确位置 |
| TC-9.3 | I | 原 md 相对图、Viking 图、缺失/越域图 | 正确基于源 URI 解析，权限与 F5 一致，无额外文档副本 |
| TC-9.4 | C/I | recursive true/false、已有 meta 与目录祖先 | 范围正确，未传 meta 不丢失；语义模式更新受影响祖先，范围不越域 |

### TC-10 Wiki

| ID | 层 | 输入/操作 | 验收结果 |
|---|---|---|---|
| TC-10.1 | C/I | 注册 md 模板+YAML/fewshot，构建中更新 schema | 可读模板得到应用；job 锁定版本，升级生成新版本和迁移计划 |
| TC-10.2 | I/Q | 只含部分实体证据的源文 | 仅抽有证据的类型，不凑8类；每个已发布事实带可读来源Viking URI，不要求revision/span作为对外溯源参数 |
| TC-10.3 | U/I | 配置Profile、无配置asserted_faults及禁直连schema反例 | 当前三一schema有证据fault_uri合法且目标可读才通过；旧related_fault(s)拒绝，Profile-only包直连拒绝；DTC合法边不误拒绝 |
| TC-10.4 | U/I | Profile config_spec_uri/device_uri 恰一、皆无、皆有 | 仅恰一有效锚通过；direct_component 和有序非空 possible_faults 同时校验 |
| TC-10.5 | U/Q | 无配置差异证据、空候选、无部件 | 不虚构 Profile/Fault；缺口记录在适当父页，不发布伪完整诊断链 |
| TC-10.6 | U/I | 可归一别名、真正缺知识、URI 拼错、跨域 URI | 分类 gap/reference/schema/conflict；已发布有效边悬空为0；不因 open_gap 放行坏边 |
| TC-10.7 | U/Q | 同名不同组件层级、不同类型、跨 namespace | 按身份与证据归一，不能“空调≈空调系统”就无条件合并 |
| TC-10.8 | I | 两源同实体有冲突，人工修正文档后重跑 | 多源证据保留；expected_revision 检测冲突，人工确认值不被覆盖 |
| TC-10.9 | I | 删除/替换一个源，再删最后来源 | 保留仍有证据的事实；无证据事实 stale/撤出诊断热路径，反向索引可重建 |
| TC-10.10 | U/I | BOM 不全且查不到部件；来源明确不适用 | 前者 undetermined；后者可 not_applicable，不能从缺失推出否定 |
| TC-10.11 | I | 模糊症状归一后禁语义模型调用，执行遍历 | 仍按固定schema精确展开配置Profile或合法asserted_faults路径，输出路径依据/未知配置；缺合法关系返回缺口，不造边 |
| TC-10.12 | C/I | schema同ID/version不同内容hash；构建中修改template/vocab | 注册409要求新版本；任务固定完整包hash，W1/W2不随工作文件漂移 |
| TC-10.13 | C/I | 现行Profile冲突fewshot、嵌套fault_uri和body变体 | 冲突在抽取前可见SCHEMA_CONFLICT；默认适配包修正后通过；示例不覆盖权威声明 |
| TC-10.14 | U/I | Device仅raw_refs来源及其他实体sources来源 | 按schema验证并保存，统一结果sources[]正确；不向Device补写禁止字段，URI仍可读 |
| TC-10.15 | U/I | 跨部件possible_faults、Procedure缺fault_refs及投影更改 | 前者按各Fault归属合法，入口件不错误限制；孤儿Procedure拒绝；反向投影从权威边再生，不能独立改事实 |
| TC-10.16 | U/I/Q | Profile配置不适用且有通用asserted_faults；无关系Symptom | 不把通用断言变成配置适用/概率或覆盖明确不适用；未知配置保持undetermined；无关系候选不发布有效诊断图 |

### TC-11 reindex 语义与原子性

| ID | 层 | 输入/操作 | 验收结果 |
|---|---|---|---|
| TC-11.1 | I | 同 fixture 初次入库与 reindex，同版本配置 | chunk、meta、图片 space、来源覆盖等语义一致；非确定模型输出按质量 gold 验证 |
| TC-11.2 | I | vectors_only 与 semantic_and_vectors 分别重建 | 前者不重写 L0/L1；后者图片摘要和祖先语义更新；均不重切 PDF/调用原 parser |
| TC-11.3 | I | 图片只有联合向量，新 reindex 重建 | 重建后仍联合输入，不退化到只有文本；无有效摘要按 SEMANTICS_REQUIRED 处理 |
| TC-11.4 | I | 受管理替换重建中修改源/kill worker/部分embedding失败；另测外部md直接编辑 | 受管理的新代不发布，旧内容/索引保持可用，版本冲突不提交；外部md被改则排除不匹配旧块并标index_pending，重建后恢复 |
| TC-11.5 | C/I | dry_run 各模式与旧请求回归 | 新 IndexPlan dry_run 零写入；旧 mode（含 prune_orphans）保留契约；输出计划可对照真实计数 |
| TC-11.6 | I/Q | 子章节正文更新、目录层级深、摘要缺失 | L0/L1 自底向上更新并体现变化；遍历定位与叶子检索都有效 |
| TC-11.7 | I | 同namespace含A/B，仅reindex A | 新snapshot只更新A与受影响摘要，B映射和召回不变；BM25/dense/meta不得用namespace单一generation过滤丢B |
| TC-11.8 | I | BM25发出后切代，再执行dense、导航和parent扩展 | 同一查询全部使用捕获快照，新查询用新快照；未授权/已删除项仍按最新权限/tombstone剔除 |
| TC-11.9 | I | A/B并发重建，B先发布；A在旧snapshot上CAS失败 | A重合并保留B已发布映射；祖先依赖变化则重算，不丢更新；禁止长期持namespace写锁阻塞构建 |

### TC-12 JEV 式预研

| ID | 层 | 输入/操作 | 验收结果 |
|---|---|---|---|
| TC-12.1 | C/U | 合法 state、未知配置、证据不足 | 返回类型化候选/下一检查/证据和 abstain，不自动补造状态 |
| TC-12.2 | Q | 独立标注的诊断案例与 Profile 初始顺序对照 | 按真实根因/检查成本评价；不要求模型排序机械等于原顺序 |
| TC-12.3 | Q | 声称输出 probability 的模型 | 报校准分割、Brier/ECE/可靠性曲线；未校准仅 score，单故障/多故障语义不同 |
| TC-12.4 | I | 预研服务关闭/不可用 | 核心入库、检索与确定性 Wiki 遍历仍可用，权威知识不被改写 |

### TC-13 生命周期与企业可靠性

| ID | 层 | 输入/操作 | 验收结果 |
|---|---|---|---|
| TC-13.1 | C/I | 并发相同幂等键；同键改 body；同文件名不同内容 | 原 job 复用或409冲突；不双写、不自动覆盖 |
| TC-13.2 | I | 写树成功但 manifest 提交前崩溃 | 正式 URI 不出现半棵树；重试恢复或回收 staging |
| TC-13.3 | I | 原件清理后 embedding 失败；再请求换 parser | 前者从持久产物重建；后者 SOURCE_REQUIRED，不能承诺无原件恢复解析 |
| TC-13.4 | I | 失败TTL到期、取消、进程崩溃遗留 | 所有临时原件/渲染/调试结果按策略清理，有审计计数 |
| TC-13.5 | I | 删除文档/图/namespace，立即各路搜索与引用 | tombstone立即阻止新请求及旧快照返回删除内容；物理GC同时满足依赖保护、租约和保留期，不能仅按TTL清理 |
| TC-13.6 | I | 升级 embedding 维度/模型与分块版本 | 新空间/代隔离，切换前后可用，失败回退旧代，旧向量不混入 |
| TC-13.7 | I | 备份后重建到空环境 | md/图片/schema/ACL/manifest 完整，重建后 gold 召回与授权一致 |
| TC-13.8 | I | 上传大小/队列/存储/token 限额触发 | 提前拒绝或可见失败，释放临时资源，无无限重试和成本放大 |
| TC-13.9 | I | 新索引失败超过旧版保留期，执行GC；再成功切代 | 旧active依赖的正文/图片/parent/L0/L1及索引均保留，查询可用；失去所有保护引用且过期后才回收 |
| TC-13.10 | I | 旧查询租约/重试任务/未完成Wiki构建引用旧代，共享图片仍在用 | GC不删依赖；正常结束或租约超时并满足保留期后回收；配额不足告警/限流而非强删active |
| TC-13.11 | I | GC标记后并发发布重新引用同一资产 | 清理前锁/CAS复核能看到新引用，资产保留；删除tombstone仍优先阻止内容返回 |
| TC-13.12 | I | 未知写入仅客户端取消/超时、hash稳定、TTL或锁可取得 | 不清除保护；旧已发布版本可读；可观察WRITE_RECOVERY_REQUIRED，禁止无限重试 |
| TC-13.13 | I | 可信后端terminal或writer fencing确认，授权恢复 | 读取最新内容及CAS后只解除确认token、推进epoch、审计落盘；新generation重建后才发布，不复活取消job |
| TC-13.14 | I | 后端确认后另一个writer开始/原token或epoch变化 | CAS拒绝；其他writer保护保留；重新确认前不得恢复/发布 |
| TC-13.15 | C/I | 越权、伪造completed/proof、后端验证失败、审计失败 | 不能解除保护或泄漏其他域诊断；失败可观察，后续合法恢复可幂等重试 |
| TC-13.16 | I | 崩溃重开、跨主机writer及恢复重试 | 共享权威registry保留在途状态，确认后仅一次恢复；各自SQLite不算通过，真实后端延迟写不覆盖重建内容 |

### TC-14 namespace 与隔离

| ID | 层 | 输入/操作 | 验收结果 |
|---|---|---|---|
| TC-14.1 | C/I | 注册重叠 root、不属 root 的 parent、`/a` vs `/ab` | 重叠/越界拒绝；按段边界与规范化判定 |
| TC-14.2 | I | FX-ACL 全入口访问矩阵 | read/list/grep/download/search/navigation/image/traverse/export 均无越权内容 |
| TC-14.3 | I | 同一 query 先由高权限人预热缓存，再由低权限人调用 | 无缓存泄漏；ACL变更后旧缓存/向量候选不绕过授权 |
| TC-14.4 | C/I | 无写权限者 ingest/reindex/wiki；业务 writer 重建企业域 | 非法拒绝；合法 namespace writer 按新权限契约成功，不要求伪装 ROOT |
| TC-14.5 | I | 私密源构建权限更宽 Wiki，无/有 export grant | 无权拒绝；有权明确审计，不靠目标目录不同就允许复制 |
| TC-14.6 | I | 跨域 job ID、日志、错误消息、图片链接 | 读不到详情且不泄露存在性；合法管理员可审计 |

### TC-15 评测与变更追踪

| ID | 层 | 输入/操作 | 验收结果 |
|---|---|---|---|
| TC-15.1 | C/Q | 入库阶段生成 query/来源候选，Wiki生成案例候选 | 标明 synthetic 与 generator version；人工审定前不得 gold |
| TC-15.2 | U/Q | 数据划分及重复/标题泄漏扫描 | 按文档/机型分离，同片段改写不跨测试边界，泄漏项剔除并记录 |
| TC-15.3 | I/Q | 固定硬件/版本跑冷暖性能与质量 | 报样本数、并发、p50/p95、置信区间与失败率；未测不得宣称30分钟完成 |
| TC-15.4 | C | 改 R/F/模型/分块/schema 再更新状态 | 对应 TC 标 STALE，变更记录有影响/迁移/证据；不能只改主文档 |

## 3. 端到端验收

| ID | 路径 | 通过条件 |
|---|---|---|
| E2E-1 | 完整SY215C → 每个指定模型 → 对齐或自动回退 → 章节/图片 → shared IndexPlan → 混合检索 | 三模型分别报告对齐质量/回退率；正文覆盖、联合图片embedding、gold内容命中、URI可读和权限满足；结构回退可ready且带info，能力缺失的degraded不算完整通过 |
| E2E-2 | 已有长md URI → semantic_and_vectors → 多chunk搜索 | 原文URI/hash不变；头中尾可召回；parent扩展和命中内容完整；仅凭原URI可打开来源，L0/L1可导航 |
| E2E-3 | 多源内容 → schema锁版 → Wiki → 配置诊断 | 不虚构实体；有效边无悬空；Profile恰一锚；来源可读；人工编辑重跑不被覆盖 |
| E2E-4 | 三本排队 → kill/重试 → written后索引失败 → 恢复 → 更新/删除 | 串行解析不破坏；成功屏障真实；重试不重复；删除/版本传播正确 |
| E2E-5 | Office六扩展名 → PDF（含无书签）→ 入库检索 | 转换质量gold通过；不因无书签整体失败；未静默跳过PDF步骤 |
| E2E-6 | 两知识域两角色跨读写/缓存/检索/Wiki/日志 | 零越权样本；URI目录分开不是验收替代；合法显式共享可用 |
| E2E-7 | 缺标题/无书签PDF → 标题或规则自动回退 → 落盘/索引 → 检索 → URI读回，同时处理后续文档 | 全程无需人工确认；info持久显示实际策略；正文和图片完整，正常ready；只传Viking URI可打开来源，后续文档不被阻塞 |

## 4. 质量、性能与评测集

### 4.1 样本与来源

构建时自动生成 `query + expected_source_uri + generator/version + split + label_status`（评测内部可记录源revision固定样本，不要求产品返回位置）；Wiki候选增加 `symptom/config/gt_faults/evidence`，`gt_*` 只有经独立标注才生效。未审核样本用于 smoke，不能当独立质量证据。

初始最低 gold 规模：至少 100 条检索查询（精确术语、口语改写、模糊机型、图片、负例各至少15条，类别可交叉）；至少30处图文用途标注；至少50个章节边界（覆盖分支/叶子/重复标题/首尾/表格）且加入完整手册自动覆盖检查；Wiki至少20条人工核验诊断路径与所有规则反例。预研概率校准样本不足时明确结论“不足以校准”，不声称模型概率可靠。

离线章节 golden 必须由原件/人工边界独立建立；这不构成线上对齐失败时的人工审核流程。不用新 splitter 产物给自己打分。业务实体/诊断真值不能直接取同一待测 LLM 的抽取结果。原件不得永久保留的生产策略不禁止用户授权的评测语料；本仓库只存许可允许的小样或 hash/标注，敏感完整手册留受控测试环境。

### 4.2 初始门槛（待执行，不是性能保证）

| 指标 | 门槛/判定 |
|---|---|
| 规范内容块覆盖 | 100%；书签成功和自动回退两条路径均无内容丢失/额外复制（允许显式chunk overlap） |
| 已发布章节结构 | 成功书签路径节点分类完整；回退路径实际标题/规则章完整且info真实，不能要求其复刻书签数。两条路径分别对实际结构gold评估边界准确率≥95%；对齐命中率与回退率另报，不把回退计为对齐成功 |
| 图片引用 | 100%有效且在授权域；人工必需图保留率≥95%，各后端分别计数，不要求总张数一致 |
| 图片上下文摘要 | ≥90% gold评分≥4/5，评分含可见对象、上下文用途、引用、无臆造；具体机型/维修数值的无依据断言为严重失败 |
| 文本 Recall@5 | ≥0.80；按查询类别分别报告，不能被标题同义生成题淹没 |
| 引用支持率 | ≥0.95的来源文档包含支持内容；正常授权状态下URI技术可读率100%；不要求精确位置、对外revision或hash匹配 |
| rerank/MMR | 以验证集调参后冻结；NDCG@10 相对 hybrid 基线退化不超过0.02，重复证据率下降；不承诺每个query都提升5% |
| Wiki 发布门禁 | 有效边悬空0、无证据事实0、身份冲突未处理0、配置锚无效0 |
| 隔离/生命周期 | 全部相关功能用例通过；零越权/静默覆盖/旧代混入 |
| meta候选/检索延迟 | spec §11 初始目标；固定10万chunk、1万meta值、并发5、至少1000次查询，冷暖独立，报告p50/p95及错误率 |
| 完整文档吞吐 | 三模型各测 parse/write/semantic/embed/index耗时、页/秒、峰值内存/显存；测后锁定SLO，不使用未经测量的30分钟承诺 |

掩码：诊断输入去除已确认根因答案、标题泄漏和处置结果；GT 保留在独立标签侧，因果层区分根因/诱发/表现。外部 HTML 中的 rubric 在未拿到本轮可复核版本前不作为隐藏验收标准。

## 5. 执行证据与变更台账

产品运行记录模板（待实现后填写）：

```json
{
  "run_id": "<真实执行ID>",
  "spec_version": "0.6",
  "test_ids": ["TC-1.1"],
  "status": "NOT_RUN",
  "commit": "<commit+worktree digest>",
  "fixtures": [{"id": "FX-PARSE-M", "sha256": "<fixture hash>"}],
  "environment": {"gpu": "<实测>", "model_revision": "<固定版本>"},
  "artifacts": [],
  "failures": [],
  "limitations": []
}
```

v0.5源码/合成缺陷与外部会话PDF记录保留于 `evidence/review-2026-10-03.json`；v0.6文档一致性检查见 `evidence/review-v0.6-2026-10-03.json`。两者均不能当E2E报告。

| CR | 日期 | 需求/问题 | 影响 | 状态 |
|---|---|---|---|---|
| CR-005 | 2026-10-03 | v0.5基础设计审查 | R01–R13，F1–F15 | 历史设计记录；被CR-006修改项按新规则验收 |
| CR-006 | 2026-10-03 | 用户要求章节对齐失败自动#+规则切分、任务info注明失败；URI级溯源；修复队列、局部重建/查询快照和GC依赖 | R02/R04/R05，F2/F3/F4/F6/F8/F9/F10/F11/F13/F15；TC-2/3/4/6/9/10/11/13及E2E-7 | 设计与测试已同步；产品测试NOT_RUN |

后续每次 CR 必须补：变更依据、设计段落、受影响子项、用例新增/修改、迁移方案、证据链接和负责人。禁止以“review了多轮”当完成证据。


## 6. I001 部分实现测试（2026-10-03）

与上方完整目标验收分开记账。详见 [实现与使用方法](openviking-enterprise-kb-implementation.md) 和 `evidence/implementation-I001-2026-10-03.json`。

| 目标族 | 实际覆盖 | 状态与范围 |
|---|---|---|
| TC-1.4/1.6/1.7 | 参数验证、HTTP/SDK、原文件名、队列序列化 | 部分 U/C PASS；完整企业契约仍 NOT_RUN |
| TC-2.1/2.2/2.5/2.6/2.7/2.8/2.11/2.12/2.14/2.15 | 命名/动作目标、页码不参与切分、重复标题、无损首尾、规则回退、安全命名、短文 | 合成 U/本地存储子路径 PASS；真实模型/质量层仍 NOT_RUN |
| TC-2.10/2.16 | 同级图片、缺图失败和清理 | 模拟外部输出 PASS；真实模型资产待验收 |
| TC-3.3/3.4 | alignment/info 与 task status 分离、重载后 info 保留、前端说明 | 子契约 PASS；worker故障/多机状态机 NOT_RUN |
| TC-11.* | 新章节控制文件不参与摘要/正文向量/reindex目标 | 限定回归 PASS；统一 IndexPlan 与原子发布仍 NOT_RUN |

完整 Feature 状态保持 NOT_RUN，不能用这张局部测试表代替 I/Q/E 验收。已有 reindex 单文件锁测试缺少其所需 fixture 的初始化错误单列在证据中。


## I003 实施验证（2026-10-04）

见 [I003 证据](evidence/implementation-I003-2026-10-04.json) 与 [定向代码测试报告](evidence/implementation-I003-backend.xml)。新增取消竞争、FIFO 跨后端、数据库连接释放、底层 MinerU cancel 传播、Markdown 代码围栏 HTML 反例、默认模型路由覆盖测试。仅支持对应部分 U/C PASS；TC-1.5/F3 故障注入和实际 GPU 排空、完整 I/Q/E 仍 NOT_RUN。

I003 检索定向回归共 37 项 PASS（[报告](evidence/implementation-I003-retrieval.xml)），覆盖 ACL on/off、目录递归无结果时保留全局叶子，rerank 不可用回退向量分数；仅对应 TC-6.4 部分 U，真实授权服务 I 与完整检索 E 仍 NOT_RUN。

I003 全卷真实产物回放已验证自动回退与磁盘章节写入：491 书签触发 heading_order_conflict，info 明确失败，自动切成 967 章节，写入 1642 个引用图片副本。对齐质量不是 PASS；TC-2.6 部分集成路径通过，TC-2.13 独立 gold 和生产 E 仍未执行。

构建期已同步生成 [50 条真实章节评测候选](evaluation/sy215c-candidates-I003.jsonl)，全部 GENERATED_UNREVIEWED、非 gold；[候选边界与复核要求](evaluation/README.md)明确标题衍生查询偏差、待发布 URI 和负例缺口。不据此宣称完成 TC-15 或检索效果通过。

## I005 局部实施证据补记

TC-4.1/4.2/4.7 的全文覆盖、预算与表格行组 U 部分已运行；全卷 968 Markdown 的模型 tokenizer/计划回放通过，不证明真实 embedding/检索或章节 quality。FLOW-COMMON-1 的底层首次 vectorize_file 与 URI reindex 计划等价性测试通过，不提升完整 API/SDK FLOW 状态。新增重建标签 preserve/append/replace→全部 child 传播测试；chunker 版本变化会改变 collection metadata 指纹。125 项回归见 I005 报告。权威元数据 registry、新代发布和旧代淘汰仍待实现，TC-4.5 整体继续 NOT_RUN。

## I006 局部发布证据补记

TC-4.5 的文本代次可见性 U 部分已运行：producer seal、unique ack、false upsert/取消/排队失败不发布，较早任务晚到不覆盖较新任务，查询跨切代保持 manifest，筛选在 top-k 前执行。131 项回归通过。GC 计划保护活动快照/查询租约的 U 用例已运行，但物理 GC、任务/Wiki/正文/图片依赖保护未实现；TC-13/14 与 ready 屏障保持完整验收 NOT_RUN。不能用内存 vector DB fixture 证明生产原子发布。I006 报告提供实际 SQLite 状态验证与 adapter 谓词编译证据。

## I007 正文发布局部证据补记

TC-4.5/4.6 与生命周期的 U/I 部分新增：原 URI 在新代排队/部分写入/失败时读旧正文、全部写入后切换；行/字节读取仍按原契约；同一查询组装过程中切代不改变正文；来源当前撤权/删除或私有版本损坏不返回工作正文。工作文件写入 epoch 在计划入队和发布两个阶段检测，修改后旧计划不发布，重新 index 读取最终工作正文。正文保护根覆盖 active/read/pending，但 Wiki/图片/重试和物理删除未验收。

220 项回归报告与源码 hash 见 I007。磁盘/SQLite 为实际实现，AGFS transport、embedder/vector DB/撤权为测试替身；未替代生产 ACL/加密、全卷检索、整本章节/所有修改入口原子发布验收。§2.3/ready/TC-13 与四主入口仍整体 NOT_RUN。


## I008 多文本批次局部证据

新增 12 项 U/I 定向用例（`tests/enterprise_kb/test_batch_publication.py`），映射 TC-4.5、TC-4.6、F3/F13 和 FLOW-COMMON-1 的部分发布契约。使用真实 SQLite、实际 enqueue/consumer、实际 URI reindex 和磁盘正文读取，验证全批 producer seal + 全 child receipt，早完成章节不提前发布，错误/取消/重排/编辑/较新尝试阻止整批发布，重复 URI 和作用域不一致被拒绝，共享 worker 不串批。全量本轮定向报告 326 PASS。

| 场景 | 局部结果 | 尚未证明 |
|---|---|---|
| 两份 Markdown URI reindex 共用文本批次，完成第一份仍读两份旧正文，第二份完成后同时切换 | PASS（真实编排，模型/向量 fixture） | HTTP/SDK、生产存储、完整 PDF 根 E2E |
| 第二份表格超预算导致 reindex failed_records，第一份完整排队仍不发布 | PASS | 任意 Office/图片/目录阶段的整体恢复 |
| 队列重排/取消、embedding 失败、编辑/较新任务 | PASS（局部故障注入） | 跨主机故障排空、任务 UI ready |
| 各文本 producer 在 seal 前完成所有 embedding | PASS，仍等待批次封口 | 目录/图片/BM25 同屏障 |

不提升完整 FLOW-D1/D2/W1/W2、TC-4.5 或 TC-13 的验收状态。部分未受根快照投影约束的初次正文/list、旧章节删除、目录语义与图文联合发布必须在后续完整验收覆盖。


## I009 目录语义局部证据

新增 10 项 `test_semantic_publication.py` 用例，对应 F11/TC-4.5/TC-13 与 FLOW-COMMON-1 的部分要求：L0/L1 generation 与文件 chunk 共用批次，早完成目录等待文本，单层写入/L1 取消不发布，部分层变更保留已有另一层，真实 URI reindex 目录和文件统一编排，marker 路径不写 legacy row，语义阶段 FAILED/REQUEUED 中止，受管 DAG 不吞目录生成失败，非法非 chunk 身份拒绝。最终 404 项回归 PASS；目录/scalar/标签与既有 Semantic DAG 回归包含在报告中。

这些证明目录**向量**版本屏障，不证明 `.abstract.md/.overview.md` 内容快照、目录树原子投影、线上 AGFS/ACL、BM25 或图文。文件锁既有测试完成 fixture/API 修复后作为 exact 锁调用契约测试通过，不能当成实际 AGFS 锁排空证据。首轮失败报告保留，最终报告以 `tests-I009-semantic.xml` 为准。完整 FLOW/TC-4.5/TC-13 仍 NOT_RUN。


## I010 目录内容版本局部证据

新增 12 项 `test_semantic_content_publication.py`，映射 TC-4.5/4.6、F11/F13 与 FLOW-COMMON-1 的部分内容一致性要求。验证原 sidecar URI/public abstract/overview 与目录 generation 一起切换；单层完成仍读旧版；快照跨切代固定两层；标准写入修改会阻止过期计划，begin 前修改或 body 不匹配拒绝；损坏版本不读工作文件；新版新增层不泄漏；旧目录无 reference 需重建。实际 URI reindex/direct directory index 与内部 metadata 读写验证了工作源使用，active/read/pending sidecar GC 根也被覆盖。

本轮 420 项定向回归 PASS；完整文件 metadata fixture 按实际 source(kind,uri) schema 与原 newline 格式校验。模型和向量库为 fixture、AGFS transport 为磁盘替身，未替代生产隔离/加密/故障恢复。根结构与初次入库可见性、图片/BM25/任务 ready/Wiki/删除传播仍不在此局部验收内；完整 FLOW/TC-4.5/TC-13 均 NOT_RUN。


## I011 图片链路局部证据

新增11项 `test_image_context.py` 用例，映射F5/F9/F11与FLOW-D1/D2部分要求：真实图片AST引用、章节标题和邻近上下文；HTML/重复引用/编码路径；不抓外链/忽略围栏；预算与权限/超限失败；逐引用VLM及严格失败；actual vectorize_file与worker联合输入；内部字段不存数据库且重试保留；URI图片reindex复用联合producer。438项回归PASS，报告 `evidence/tests-I011-regression.xml`。局部AGFS/VLM/embedding为fixture。

真实8026联合请求返回2560维有限向量，仅协议局部PASS（`image-joint-I011-2026-10-04.json`）。F5完整验收仍NOT_RUN：需引用级独立资产/索引、缓存、同代发布、真实上下文摘要及检索评测、多空间查询编码。超16引用分页、跨目录引用、绑定VLM tokenizer、权威meta输入、ready_degraded均未完成。四主FLOW维持NOT_RUN。


## I012 图片共同发布局部证据

新增8项test_image_publication.py，对应F5/TC-4.5/F13和FLOW-COMMON部分：图片原图/向量同代切换、文本/目录/图片一起等候receipt、编辑/worker失败/新任务替代保留旧批次、跨切代query读旧图及GC根、损坏资产拒绝、enqueue失败中止。446项回归PASS，evidence/tests-I012-regression.xml。SQLite和磁盘资产实际执行；AGFS transport/model/vector DB为fixture。逐引用独立索引、上下文修改失效、首次根可见性、独立空间融合/BM25/生命周期/任务状态与真实图文检索继续未验收，不提升完整FLOW/F5/F13。


## I013 协议与真实服务局部证据

新增9项test_openai_embedding_messages.py：实际OpenAI SDK经过mock HTTP transport，验证sync/async messages联合body、文本query/document参数、float/base64解码、额外body覆盖身份/输入拒绝、factory传递和文本批次拒绝。472项回归PASS（I012的446+9新增+17已有embedder/pool）。本机proxy影响loopback复用测试已修fixture并保留原断言，初始471PASS/1FAIL报告另存。

真实远端模型通过OpenViking factory/async adapter返回joint和query 2560维有限向量，evidence/image-adapter-I013-2026-10-04.json。F5协议局部PASS；实际worker共同发布/图文质量与召回未验证，完整FLOW/F5/F13仍NOT_RUN。


## I014 图片引用局部证据

新增6项image_publication测试：同图两引用各自摘要/joint input/独立ID，共用generation并等待两receipt；result.matches保留context URI；VLM后原图变化enqueue前拒绝；一个引用失败保留旧代；D1/URI共享plan及语义结果交接；新版vectors_only无引用缓存显式失败不压回一row；actual DAG增量图片重新生成引用摘要。478项回归PASS，tests-I014-regression.xml。VLM、vector DB和AGFS transport为fixture；阶段交接测试不代表完整HTTP/SDK E2E。持久缓存/正文版本依赖/跨ACL与撤权预过滤、真实召回/图文质量等仍NOT_RUN，完整FLOW和F5/F13不提升PASS。


## I015 图片缓存局部证据

新增10项test_image_cache.py：重开SQL可恢复private payload并保留stable occurrence IDs；asset/context/model失效；生成后model/prompt变化；enqueue前/embedding中正文完成编辑；授权拒绝/损坏cache；恢复后变图；pending/held-reader cache GC保护。更新旧vectors_only测试为合法缓存复用成功。488项回归PASS，tests-I015-regression.xml；AGFS transport/model/vector DB为fixture。

新增待验收项：写入未完成但epoch已invalidate期间捕获旧body的fence/CAS；目录增加/删除引用和撤权事务；旧无epoch数据迁移。当前epoch测试是完成写入的故障注入，不证明这些并发窗口。跨ACL/自动依赖传播、真实图文E2E/四FLOW继续NOT_RUN。


## I016 标准写入局部证据

新增10项 test_source_write_fence.py：四入口 storage await 暂停时禁止归档；多个写入 token 独立及重开/账号/URI隔离；取消与传输失败保留 uncertain、旧发布内容仍可读；捕获后写入拒绝 begin/最终 publication；sidecar 与图片 context 在途写入保留旧代。498项回归PASS，evidence/tests-I016-regression.xml。SQLite/磁盘/VikingFS 实际执行，AGFS transport/model/vector DB 为 fixture。

I015标准 in-flight 窗口在上述入口已补齐。待测/待实现：后端确认后的持久恢复、进程崩溃恢复E2E、外部写入/目录引用集合/ACL与跨主机事务、迁移排空、真实服务并发。四个完整FLOW与完整F13仍NOT_RUN。


## I017 运行复核与新增恢复验收

远端三个服务健康/capabilities 200、部署脚本hash匹配、检查时SQL队列无queued/running，记录runtime-I017-2026-10-04.json。health/历史verified不等于新推理或完整FLOW通过。新增TC-13.12–16属于未实现恢复契约，全为NOT_RUN；需真实后端terminal/fencing确认、延迟写入及跨主机故障注入，不能以mock断言代替。


## I018 队列健康局部证据

新增4项生命周期用例：依赖齐全但无worker不ready；worker异常退出503拒绝任务且不消费上传；真实flock阻塞启动恢复至解锁才ready；lifespan消费者异常仍停止worker。10项worker/502项回归PASS，报告tests-I018-worker.xml和tests-I018-regression.xml。远端更新后三worker running/ready及脚本hash匹配；不是fresh推理、watchdog、完整F3/四FLOW验收。


## I019 Wiki schema审查证据

实际8实体包只读清点/hash完成，非Wiki执行测试。TC-10.3/11按collection固定schema修正配置与无配置路径，新增TC-10.12–16；全部运行时验收NOT_RUN。发现Profile fewshot部件限制与YAML冲突、Device来源例外、Procedure权威边方向，适配包及注册预检仍待实现。


## I020 schema基础局部证据

14项test_wiki_schema.py与此前502项共516项回归PASS，tests-I020-regression.xml。实际八实体结构编译及Profile示例适配zip生成，保留上游digest；严格模式对38条未编译自然语言规则拒绝。不提升TC-10.1/12/13或W1/W2整体状态：注册持久化、权限、通用模板一致性及规则运行尚未完成。


## I021 实体检查局部证据

新增24项test_wiki_validation.py，检查frontmatter/enum/类型/空来源、嵌套关系严格对象形状、AST章节次序/伪标题、URI规范及Unicode归一、来源授权/实际内容/版本、关系类型/collection边界和重复引用变代。与此前516项共540项回归PASS，tests-I021-regression.xml。引用resolver是AsyncMock，不代表实际ACL/export/存储存在性门禁；真实Profile示例仅结构PASS。TC-10及W1/W2完整验收继续NOT_RUN。


## I022 引用读取局部证据

10项test_wiki_resolver.py调用actual VikingFS/SQLite+磁盘AGFS，2项test_wiki_validation.py新增未验证/错schema目标拒绝。552项回归PASS，tests-I022-regression.xml。权限前后检查由mock模拟、注册namespace/验证catalog为fixture，非生产授权/跨域export/最终CAS验收。TC-10.6的版本读取部分有局部证据；完整TC-10、W1/W2仍NOT_RUN。


## I023 collection/schema持久化局部证据

15项test_wiki_catalog.py加此前552项，总567项回归PASS，tests-I023-regression.xml。实际SQLite/私有磁盘包/GC根，访问检查为mock；不存在完整HTTP/SDK、namespace授权或Wiki构建验收。初始失败报告保存catalog-initial/parent-initial，服务补首次schema父目录创建，fixture补正常AGFS mount根。TC-10.1/12与FLOW-COLLECTION-1仅部分内部服务证据，完整状态仍NOT_RUN。


## I024 Wiki配置 HTTP/SDK 局部证据

新增6项 test_wiki_http_sdk.py：同步/异步4方法、固定schema摘要、版本冲突/权限错误类型、账号额外字段/严格版本/非法文件路径拒绝、读取权限、配置范围 OpenAPI。567+6=573项本功能回归PASS（tests-I024-feature-regression.xml）；实际 FastAPI/SQLite/磁盘，RequestContext/ACL替身。前端仅生成客户端且独立TypeScript检查PASS，无完整UI验收。

扩展SDK回归 tests-I024-regression.xml：604 PASS/5 FAIL；HEAD client.py临时包基线 tests-I024-sdk-baseline.xml：31 PASS/同5 FAIL。旧gateway_token/telemetry/context_type兼容问题未在本轮修复；不得写全仓测试通过。配置API并非W1/W2任务API，完整FLOW-COLLECTION-1、FLOW-W1/W2和domain授权验收继续NOT_RUN。


## I025 Wiki来源准备局部证据

新增27项 test_wiki_build_plan.py，映射FLOW-W1/W2的共享输入阶段、TC-10.1/6/12和F13依赖保护部分：固定来源/schema、SQLite重开/私有版本GC根、实际重叠幂等保存、目录范围/新增成员、写入期间/未知结果/撤权、损坏、限额、自输入及流式检查。600项本功能回归PASS（tests-I025-regression.xml）；首轮失败报告保留，不以此替代I024五项旧SDK基线失败。

真实全卷数据回放（tests-I025-full-plan.xml、wiki-plan-full-I025-2026-10-04.json）：968个Markdown、1,205,623字节、3目录；三一schema摘要83816ecc…、两输入来源版本相同、每个流式读回hash相符。独立1项回放PASS；磁盘AGFS/ACL替身、draft_rules_pending与namespace_export_pending。不证明实体抽取/38规则/生产授权或章节quality。

后续完整验收必须增加：W1任务创建时先固定schema、upload→解析→prepared→抽取→发布→索引；W2目录成员增删在commit期间的事务保护；namespace/export门禁拒绝未授权来源；计划退役/租约/临时版本回收；编辑/删除/撤权对实际实体发布的影响。以上完整TC及FLOW继续NOT_RUN。


## I026 显式结构门禁与覆盖台账证据

647项本功能回归PASS，新增47项：实际三一适配示例/锚点恰一/非空和重复关系列表/Device来源限制/父目录和故障顺序/Procedure分类模板/BOM对象属性/Symptom嵌套URI键、共享及布局待验证记录、当前schema能力重算、来源account/namespace、畸形声明拒绝。tests-I026-regression.xml为最终报告；structure-initial与account-contract-initial保留，未通过放宽业务断言消除失败。catalog/HTTP权限仍为替身。

wiki-schema-I026-2026-10-04.json + 新适配ZIP：当前直接重放I020 Profile因shared来源未声明而拒绝；I026七类示例结构PASS、DTC旧kb来源拒绝。38实体规则+9共享政策+8路径标记共有118条按实体记录的pending，严格编译拒绝；不是域门禁或事实真实性验收。完整TC-10.1/3/4/6/11–16与四FLOW保持NOT_RUN。

尚需验证完整构建对同批实体引用/循环关系、实际配置差异证据、Fault机理身份、Procedure适用性/分支/回检闭环、路径命名及别名冲突的处理，并固定执行器/编译器版本。I024五项旧SDK兼容基线失败仍存在，不能用647局部PASS替代全仓回归。


## I027–I030 输入准备与查询的局部证据

I027：669项（新增22），知识域注册/导出、actor/组/撤权与HTTP/SDK；I028：675项（新增6），W2 URI准备入口；I029：682项（新增7），W1 Markdown物化准备入口。对应报告见evidence/tests-I027/28/29-regression.xml，均为本功能范围，非四FLOW完整验收。

I030：692项（新增10），GET取回同一计划（同步/异步）、来源撤权拒绝、账号隔离404、无效ID/不存在ID、超限拒绝且无源/计划写入并清理、边界limit与有界读，以及旧幂等键的不同内容/不同文件名/来源已编辑三种replace失败均不改变来源字节或epoch。最终报告tests-I030-regression.xml；聚焦报告tests-I030-plan-read.xml。初始报告保留：SDK异常类型测试导入错误3失败、旧OpenAPI清单1失败；修正测试后通过。

Wiki配置/准备/查询前端客户端独立严格类型检查PASS。ACL/embedding/temp store为fixture、磁盘AGFS，不证明生产认证、抽取、批任务和发布。完整TC-10、F13/F14、四FLOW继续NOT_RUN；共享下载磁盘配额、并发首次物化事务、导出执行与提交期间撤权仍须端到端测试。旧SDK五项兼容失败仍未修复。


## 7. CR-011 剩余交付交叉验收（全部NOT_RUN）

以下是新增断言族TC-16，补充既有TC而不替代。局部回归与完整产品测试仍分别记录；所有条目尚未取得本范围生产证据。

|ID|输入/故障|必须观察到的结果|
|---|---|---|
|TC-16.1|读取实际OpenAPI；当前prepare与未来execute分别调用；旧SDK兼容样本|未知参数不吞；当前200 prepared与执行202 job分开；5项旧失败修复；key长度/错误码边界一致|
|TC-16.2|协调进程崩溃、多个独立worker、协调者不可用|一份权威状态；不独立SQLite双主；新发布暂停且状态可恢复；单节点模式不宣称HA|
|TC-16.3|job创建后改变组/角色/ACL，高权缓存预热后低权查询|执行刷新身份；所有路预过滤和读回/缓存拒绝越权；无跨域job存在性泄漏|
|TC-16.4|全部后端及内部路径同时三文档、两worker、embedding暂停/取消/迟到产物|parse_write互斥且written释放，旧fence不能持久化或ready；wait超时不取消|
|TC-16.5|首次发布/替换时目录成员增加或删除，list/stat/read/search并发|只看到允许阶段的完整树；替换公开旧树到同批切代；BM25/meta/图文/导航一致，无半树|
|TC-16.6|shared临时下载大文件、满盘、render爆炸、job/token限额，随后空环境恢复|在有界资源内失败并回收；active不强删；备份恢复ACL/manifest/来源及gold，不恢复已清理原件|
|TC-16.7|同图17+引用、跨目录引用、超token上下文、一个引用撤权|全部合法引用分页处理且语境隔离；actual tokenizer预算有效；无权上下文不泄露|
|TC-16.8|禁用dense、词法版本未完成/旧代查询、中文型号/DTC|真实BM25命中且可复算词法分；授权snapshot在候选阶段执行；receipt不齐不切代|
|TC-16.9|collection指定未注册/不一致namespace、两个Wiki collection复用域、旧非法绑定|拒绝新非法绑定并明确旧项不可执行；来源域和目标域/export分别检查|
|TC-16.10|逐规则coverage receipt、默认八实体样例、DTC旧kb引用|未编译/未验证项阻止诊断发布；规则数与ID可审计；仅真实映射可迁移DTC来源|
|TC-16.11|源资料含工具指令/伪schema、无事实/无证据、模型超时/畸形输出|资料仅数据；不执行工具；有限重试/独立错误；没有证据不发布实体，合法URI来源关联|
|TC-16.12|同名不同类型/组件层级；两源冲突；模型和人工并发修改|身份不误并；来源/冲突完整；expected_revision拒绝旧写，不覆盖人工确认字段|
|TC-16.13|同批合法循环、只有预留地址、伪verified metadata、一个目标结构失败|循环不被无理由拒绝；实际完整验证后的闭合子图才可发布；无悬空/自报认证|
|TC-16.14|抽取后撤export/来源ACL/改正文/新增目录成员；提交时目标变更|提交fence/CAS拒绝该批，旧图可用；不能用创建时grant覆盖当前撤权|
|TC-16.15|同文件分别W1/W2，MD含相对图片，另PDF/Office上传；重启/取消|共用实际抽取与IndexPlan，URI证据有效，持久job恢复正确；prepared不冒充ready|
|TC-16.16|事实两来源→删一→删最后；投影损坏；人工字段仍在|按证据撤回/保留，零证据退出诊断；权威边可重建投影，人工字段保护不丢|
|TC-16.17|四FLOW及固定环境质量/负载/恢复/迁移验证|分别报告PASS/FAIL/NOT_RUN；容量/RTO/RPO和依赖故障可解释；单节点协调模式不称写入HA|

任务与Feature映射见feature-status.json。TC-16均是目标断言，不伪装为已存在可运行测试。章节回退无人工等待；来源只要求Viking URI，内部epoch/hash/fence不变为用户定位参数。


### TC-16 执行兼容与派生权限补充（NOT_RUN）

|ID|操作|验收|
|---|---|---|
|TC-16.18|旧W1/W2 prepare、execute job、W1 PDF/Office job，分别重放同key和修改profile/digest|旧200 prepared不变；新202 job同输入复用，不同409；当前来源已变拒绝；wiki_draft仅私有draft_ready，diagnostic未验证规则拒绝|
|TC-16.19|私密子文档与公共子文档共同生成目录摘要/图片上下文/parent，撤权后重查|派生可读权限按贡献来源交集；BM25/dense/导航/扩展/缓存均无泄漏；不凭目录ACL或旧query租约放行|

## I031 局部证据映射

KB-001：当前prepare/旧Python兼容覆盖TC-16.1/16.18局部；KB-002：同机协调边界覆盖TC-13.16/16.2局部；KB-031：冻结/人工审定与泄漏拒绝覆盖TC-15.1/15.2/15.4局部，TC-15.3真实质量/性能未执行。最终925项回归通过仅覆盖上述源码范围，各完整TC族和FLOW状态不提升。详见[I031联合报告](evidence/implementation-I031-2026-10-04.json)。

## I033 局部验收映射

KB-001/004：D1/D2持久HTTP/SDK、owner/fence、双worker三job、kill及未知写入隔离；TC-3/16.4只有局部证据。KB-003/022：当前身份、注册根候选预过滤、严格域绑定、旧配置检视/CAS迁移；TC-14/16.3/16.9/16.19仍需生产全通道矩阵。KB-009/011：模式、产物及图片章节边界契约；TC-1/2完整gold未执行。SDK导入测试改独立解释器，避免污染后续解析mock；旧权限/分页测试替身同步当前协议，未放宽访问拒绝。历史失败XML保留，本轮不是四FLOW通过报告。


## 39. I034 当前接入与验证（2026-10-05）

联合回归1575 PASS、1 SKIP；跳过项为显式启用的真实Wiki模型探针，已在独立报告实际运行通过。前端356项、完整TypeScript和生产构建通过。初轮4项旧契约失败与前端环境/超时失败保留原报告，修复后重跑。该范围不等于完整仓库或产品验收。

文本BM25已加入权威发布回执，现有find/search复用固定manifest和当前权限；不含业务meta、图片摘要与完整物理GC。Office本地45项和服务器Docker六格式合成契约通过；生产worker与远端转换容器传输未完。受控后端未知写入恢复与离线隔离恢复已验证，普通AGFS能力不足时明确拒绝，恢复库不得自动启动。

Wiki旧接口继续200 prepared；POST `/api/v1/kb/wiki/builds/{build_id}/execute`固定plan digest与profile，返回202持久W1/W2 job。已准备MD抽取共用一个执行器，支持逐片段恢复；私有draft使用 `/api/v1/kb/wiki/jobs/{job_id}/drafts/{draft_index}`读取，复核所有来源ACL/export及当前输入。完成事务绑定真实候选回执和plan，并再次复核source epoch/export revision。候选blob纳入GC保护；没有诊断知识发布。

默认包仍有118个未执行规则实例；严格诊断模式拒绝。真实配置模型合成探针生成1候选，不能证明语义证据质量。用户明确暂时没有人工gold；质量与性能门禁继续保留。KB-024从OPEN更新为PARTIAL，所有完整Feature与FLOW仍NOT_RUN。报告见[I034](evidence/implementation-I034-2026-10-05.json)。


### I049 路径政策局部验收证据

TC-10.1/10.4/10.10/10.12 的路径部分由 `test_wiki_path_policy.py`覆盖：八种合法路径、错误目录深度、Device型号/ConfigSpec主体码/DTC代码及class_name错配、跨collection root、声明hash改动、候选实际执行/服务生成回执和strict继续拒绝。DTC原来源未迁移时仍拒绝；不以机械路径通过冒充来源验证。191项联合定向通过；完整用例验收状态保持NOT_RUN。


### I050 局部回执与失败证据

KB-006/TC-4.5/TC-16.8 的提交成员共同切换、部分receipt不可见、producer崩溃恢复和旧owner/fence拒绝已有确定性代码/authority测试；未覆盖穷尽正文树及所有派生索引，不提升完整用例。TC-8.*实际legacy三族D1/W1文本合同通过，但可见XLS图保留断言FAIL：源与中间PDF存在1图而worker产物/最终树无图。模型原输出与归一化内部分界仍需定位。synthetic不是人工维修gold。1061/1skip整合与最终85专项范围重叠，不相加。
