# 企业知识库未达成任务清单

> 当前执行状态：I057（2026-10-06）TC-16.19 全通道派生权限交集闭环：BM25 召回前预过滤+rerank 前复检双门禁、FS 读取通道 `_can_access_many` 单点谓词覆盖 read/stat/grep/download；语义失败传播三修——reindex REQUEUED 不再静默放行（FAILED/REQUEUED 统一中止）、semantic_dag 目录生成失败 re-raise 经 fail() 传播不吞不挂、图片再生失败计 semantics_required 不再崩批。I056（2026-10-06）D1 图片向量缺口根因闭环：semantic_dag 单图 VLM 超时/空回复曾整批 re-raise 毁掉兄弟摘要、REQUEUED 静默落空 → reindex 向量阶段 933 图全撞 SEMANTICS_REQUIRED 但任务 completed。修复两层：(1) semantic_dag contextual 图片失败改为 defer 不再清空兄弟（Failed to generate summary→Deferred image summary after generation failure）；(2) reindex process_file 缓存失效(SEMANTICS_REQUIRED)时定点重生成 VLM 语义（共享 vlm.max_concurrent 信号量），重生成再失败才计 semantics_required、绝不降级为过期摘要。同步落地：TeleOCR/Logics 真机图片保全 I053 直连证据（17 页切片 19/19 图位保全、RMS 1.50 MATCH、绑定提交 PDF sha256）+端到端 HTTP 探针 backend-probe_8/_9 双腿通过，目标文本"TeleOCR、Logics 尚未接入这套保全"已被推翻。I055（2026-10-06）D1 卡死链第三层修复：checkpoint 钉住的上一 attempt failed 批与 building 态同路 reset（封口后重开新批），job_claim 的 _record 对人工维护非 JSON info/checkpoint 容错不再 wedge worker；SY215C 1072 页真实作业解除 attempt 322 秒败循环。模型图片语义质量、全通道 FLOW 与混合代外部编辑场景未验收。15 Feature/四FLOW仍PARTIAL、33任务32 PARTIAL+1 OPEN、0 DONE，完整验收NOT_RUN。 [本轮报告](evidence/implementation-I052-2026-10-05.json) · [I051 报告](evidence/implementation-I051-2026-10-05.json)。

当前规格版本v0.15，CR-019；任务拆解最初基线I030（2026-10-04）。这是当前任务台账；历史实施证据保留原范围。机器数据为[feature-status.json](feature-status.json)，目标见[主spec](openviking-enterprise-kb-spec.md)，验收断言见[测试文档](openviking-enterprise-kb-test-cases.md)。

OPEN=未完成目标实现；PARTIAL=有局部实现与证据；DONE=该任务完成条件全部通过且附报告。I034（2026-10-05）联合回归1575通过、1跳过（真实模型探针需单独启用，已另跑通过）；前端356项、完整类型检查和生产构建通过。所有15个Feature与四条完整FLOW仍NOT_RUN，完整仓库测试未执行。 I031的925项和I030的692项保留为历史证据，没有总体完成百分比。

P0先完成可靠性/授权/提交底座，P1闭环文档/检索与评测，P2完成Wiki，P3做JEV。同一优先级内按依赖推进。KB-031金标与KB-009/011解析适配可以独立推进；核心文档里程碑不等待Wiki/JEV。KB-032是汇总验收任务，D1/D2可先单独验收；其Wiki依赖只影响W1/W2和全目标结论。

当前进度：I047（2026-10-05）接通显式 SSH→冻结 Docker Office 转换，默认字体门禁开启；六格式转换合同通过，实际 D1 Word 上传→PDF→规则回退→7 条索引重建/0 失败→限定来源检索通过；W1 DOCX/XLSX/PPTX 三族最终均到私有 draft_ready（2/2/5 个候选），逐字引文核验通过。PowerPoint 原输出错误改写标题符号，v3 提示修复后通过；此前失败记录保留。上传新增原文件指纹，幂等重放不重解析/改源 epoch；已有树替换明确拒绝，整树提交仍待实现。持久 D2 保存真实索引计数并等待共同发布回执；ACL 更新过滤历史代并检测操作期间 head 变化。完整三一 schema 的 118 个待执行规则在实际 strict API 中明确拒绝、无作业创建。联合回归 1008 PASS/1 SKIP；最后修改后受影响范围 141 PASS、Office 59 PASS、上传重放 60 PASS（范围重叠，不相加）。这些是实现/合成合同证据；15 个 Feature 与四条 FLOW 仍 PARTIAL，32 项任务 PARTIAL + KB-032 OPEN，完整产品验收均未通过。 [本轮报告](evidence/implementation-I047-2026-10-05.json) · [Office W1 实测](evidence/flow-w1-office-fontchecked-I047-2026-10-05.json) · [完整 schema 拒绝门禁](evidence/wiki-strict-gate-I047-2026-10-05.json)。

## 当前Feature状态

|Feature|实现状态|完整验收|下一任务|
|---|---|---|---|
|F1|PARTIAL|NOT_RUN|KB-001,KB-009,KB-010|
|F2|PARTIAL|NOT_RUN|KB-006,KB-010,KB-011|
|F3|PARTIAL|NOT_RUN|KB-004,KB-021|
|F4|PARTIAL|NOT_RUN|KB-006,KB-013|
|F5|PARTIAL|NOT_RUN|KB-006,KB-014,KB-015|
|F6|PARTIAL|NOT_RUN|KB-016,KB-017|
|F7|PARTIAL|NOT_RUN|KB-018,KB-019|
|F8|PARTIAL|NOT_RUN|KB-012|
|F9|PARTIAL|NOT_RUN|KB-013,KB-020|
|F10|PARTIAL|NOT_RUN|KB-022,KB-023,KB-024,KB-025,KB-026,KB-027,KB-028,KB-029,KB-030|
|F11|PARTIAL|NOT_RUN|KB-006,KB-020|
|F12|PARTIAL|NOT_RUN|KB-033|
|F13|PARTIAL|NOT_RUN|KB-002,KB-004,KB-005,KB-006,KB-007,KB-008,KB-029|
|F14|PARTIAL|NOT_RUN|KB-003,KB-022,KB-027|
|F15|PARTIAL|NOT_RUN|KB-010,KB-031,KB-032|

## 任务依赖与验收

### KB-001 统一 API、SDK 与兼容契约

- 优先级/状态：P0 / PARTIAL；Feature：F1, F10, F13。
- 前置任务：无；可独立启动。当前局部证据：I024, I028, I029, I030。
- 具体工作：冻结当前配置/准备响应与未来job响应，补真实参数默认值、错误码和旧SDK兼容；定义版本迁移，不静默改变现有prepared响应。
- 完成条件：实际OpenAPI、同步/异步SDK、CLI及前端字段一致；修复已知5项SDK基线失败并回归。
- I031本轮局部进展：接口/旧Python兼容底座完成本轮增量：4项修复+1项安全预期纠正，prepare key统一1–128，实际400错误码及前端知识域client同步；CLI、未来job和完整契约待验收。 [独立报告](evidence/parallel-KB-001-2026-10-04.json)。任务仍PARTIAL，完整验收NOT_RUN。
- 验收：TC-1.6, TC-1.7, TC-13.1, TC-16.1, TC-16.18。当前完整验收：NOT_RUN。

- I033当前进展：新增D1/D2持久job提交/查询/取消与同步异步SDK；Wiki仍200 prepared。管理员旧collection检视/CAS迁移已加入API与SDK；CLI及最终入口契约仍待对齐。 [本轮证据](evidence/implementation-I033-2026-10-05.json)。完整验收仍NOT_RUN。
- I034进展：新增Wiki execute202与私有draft读取API、同步/异步SDK及严格OpenAPI契约；旧prepare200不变，完整CLI便利入口仍待对齐。

### KB-002 权威 registry 部署与进程边界

- 优先级/状态：P0 / PARTIAL；Feature：F3, F13。
- 前置任务：无；可独立启动。当前局部证据：I006, I016。
- 具体工作：首版单协调服务拥有本机SQLite/WAL和发布事务，其他worker经内部接口报告；禁止各主机独立SQLite或网络文件共享充当一致性保证。设计统一repository接口与备份。
- 完成条件：多worker使用同一权威协调者；进程重启恢复状态；协调者不可用时停止新提交，已发布数据按缓存/租约策略服务且不产生双主。
- I031本轮局部进展：同机Unix socket单权威repository、SQLite/WAL所有权、断连拒绝、重启、备份及实际producer发布已验证；跨主机、Wiki/ACL事务迁移和真实后端联合恢复待完成。 [独立报告](evidence/parallel-KB-002-2026-10-04.json)。任务仍PARTIAL，完整验收NOT_RUN。
- 验收：TC-13.16, TC-16.2。当前完整验收：NOT_RUN。

- I033当前进展：Wiki catalog、namespace和build plan改用协调者领域命令；独立进程authority下HTTP/SDK已验证。跨主机、真实AGFS恢复和联合备份待完成。 [本轮证据](evidence/implementation-I033-2026-10-05.json)。完整验收仍NOT_RUN。

### KB-003 所有入口的知识域授权

- 优先级/状态：P0 / PARTIAL；Feature：F14。
- 前置任务：KB-002。当前局部证据：I027。
- 具体工作：复用现有AclManager与知识根注册；source/member/current-role检查覆盖read/list/stat/grep/download/reindex/search/image/cache/job/log。任务执行刷新当前身份，不复用创建时旧组。
- 完成条件：生产身份访问矩阵零越权；撤权后候选预过滤、缓存与URI读回一致拒绝；管理员路径也有审计。
- 验收：TC-14.1, TC-14.2, TC-14.3, TC-14.4, TC-14.6, TC-16.3, TC-16.19。当前完整验收：NOT_RUN。

- I033当前进展：注册根权限约束VikingFS读/list/stat/grep/download和向量候选；宽子ACL、撤组、关闭ACL均不绕过私有根。job执行刷新当前身份；派生多源权限交集、审计及完整通道仍待完成。 [本轮证据](evidence/implementation-I033-2026-10-05.json)。完整验收仍NOT_RUN。

- I047进展：ACL 原始扫描误回写历史代已修复；当前 head 租约、子树 generation map 前后检测与实际 reindex 后 ACL 更新通过。检测不等于事务 CAS，仍须将权限 revision 与发布事务绑定。 [证据](evidence/implementation-I047-2026-10-05.json)。

- I057进展（2026-10-06）：TC-16.19 全通道派生权限交集闭环。BM25 通道：lexical_recall 召回前 denied 预过滤 + rerank 前 denied 复检双门禁，真实 lexical 行派生 fixture（test_authority_lexical 10 passed）；FS 读取通道：`_can_access_many` READ 单点接入 denied_derived_uris，read_file/stat/grep/download 继承同一谓词（test_derived_image_access 4 passed）。语义失败传播三修：(1) reindex REQUEUED 不再静默返回 None，统一抛 OpenVikingError（spec TC-13 FAILED/REQUEUED 中止口径）；(2) semantic_dag `_overview_task` 生成失败 re-raise 经 worker fail()→root_done→run() 传播，不吞 VLM 错误也不挂 DAG；(3) reindex 图片再生失败计 semantics_required_records 不再 raise 崩批（一条坏图不再中止整轮 reindex）。enterprise_kb 全量回归 1087 passed / 2 failed→0（修复后）。完整验收仍 NOT_RUN。


### KB-004 持久 job 与串行解析租约

- 优先级/状态：P0 / PARTIAL；Feature：F3, F13。
- 前置任务：KB-001, KB-002, KB-003。当前局部证据：I018。
- 具体工作：把D1/D2/W1/W2阶段、attempt、owner/fence、checkpoint、有效配置与info写入权威job记录。三后端及内置共用parse_write槽；进入written/失败/取消/退避均释放。
- 完成条件：真实两个worker+三文档最大解析活跃数1；embedding暂停不堵下一本；kill/迟到结果/取消均不让旧owner发布。
- 验收：TC-3.1, TC-3.2, TC-3.4, TC-3.5, TC-3.8, TC-3.9, TC-16.4。当前完整验收：NOT_RUN。

- I033当前进展：持久job、owner/fence/checkpoint、全局parse_write槽与崩溃隔离已实现；D1桥接实际ResourceService，D2桥接reindex。未知写入保留隔离，不凭超时释放；共同发布receipt未齐，不返回ready。 [本轮证据](evidence/implementation-I033-2026-10-05.json)。完整验收仍NOT_RUN。
- I034进展：已有持久D1/D2运行器新增已准备W1/W2实际抽取；逐片段checkpoint/resume验证通过，完整ready屏障仍明确拒绝。

- I047进展：持久作业保存有界 index_result；请求 completed 但 failed_records/semantics_required_records 非零不能标记索引成功。真实 D2 1 rebuilt/0 failed 后 backoff/index，显式等待 7 类共同回执。 [证据](evidence/implementation-I047-2026-10-05.json)。

### KB-005 未知写入的后端确认恢复

- 优先级/状态：P0 / PARTIAL；Feature：F13。
- 前置任务：KB-002, KB-004。当前局部证据：I016, I017。
- 具体工作：实现server验证的terminal receipt或后端writer fencing、授权恢复与token/epoch CAS审计；客户端超时/稳定hash不解除保护。
- 完成条件：跨重启真实延迟writer不会覆盖新代；伪造proof/审计失败/并发新writer均不能解锁；合法恢复可幂等。
- 验收：TC-13.12, TC-13.13, TC-13.14, TC-13.15, TC-13.16。当前完整验收：NOT_RUN。
- I034进展：受控后端恢复14专项通过，source/account/actor/迟到writer/策略CAS/审计回滚已验证；普通AGFS无可信终态及fence继续拒绝。

### KB-006 正文树及全部索引共同发布

- 优先级/状态：P0 / PARTIAL；Feature：F2, F4, F5, F11, F13。
- 前置任务：KB-002, KB-003, KB-004, KB-005。当前局部证据：I006, I007, I008, I009, I010, I012, I014, I015。
- 具体工作：扩展既有PublicationStore，纳入根目录成员/layout/list/stat、正文/图片/L0/L1/parent与词法meta索引receipt；发布事务固定成员版本和授权依赖。
- 完成条件：各必需索引未完成不ready；新文档written整树可读但不可搜索，替换期间公开旧树；故障和外部编辑不出现混合代。
- 验收：TC-2.7, TC-11.4, TC-13.2, TC-16.5。当前完整验收：NOT_RUN。
- I034进展：真实authority BM25文本回执与向量共同提交，固定query视图和ACL前后检查；正文树/所有图片/meta/整本任务ready门禁仍不完整。

- I050进展：持久整次批次及authority内owner/fence/lease原子封口；提交成员共同切换/真实receipt恢复通过，完整7类覆盖门禁仍缺。 [证据](evidence/implementation-I050-2026-10-05.json)。

- I052进展：七类覆盖门禁（body_tree/images/parent_child/semantic_levels/bm25/metadata/version_switch）改为读取已提交权威证据并在 job_complete ready 路径强制执行；证据齐备的 published 批次到达 ready 并标记 verified，未验证一律 PUBLICATION_GATE_REQUIRED 且保持可续跑。门禁 semantic_levels 改按批次自身声明的 semantic:* 成员核验（仅 L0 的目录无需伪造 .overview.md 即可发布）；reindex 托管库分支 include 标志改为按 sidecar 实际存在判定。真机探针复验：仅 L0 目录 D2 到达 ready、PUBLICATION_VERIFIED、3/3 重建 0 失败。Inflight 写入崩溃恢复（write_recovery 租约封口）同轮通过；TeleOCR/Logics 源 PDF 丢图保全三后端测试齐备。I057补验：混合代外部编辑场景已闭环——成员回执收齐但联合发布事务未落（seal_batch 前）的三种带外编辑（desired 换代/invalidate_draft 变 epoch/begin_write 占用写栅栏）均使 seal_batch 失败、批次 failed、ready 判定 PUBLICATION_GATE_REQUIRED 且 job 保持 running 可续跑；发布后换代不撤回已公开旧树（替换期间公开旧树语义正向断言）。仍待：全通道 FLOW 真实后端验收。 [证据](evidence/implementation-I052-2026-10-05.json)。

- I055进展：D1 卡死链第三层修复——checkpoint 钉住的上一 attempt failed 批不再 raise ProcessingError，与 building 态同样 reset（fail_batch 封口后清 publication_batch_id 重开新批）；SY215C 1072 页真实作业 attempt 322 秒败循环解除，attempt 323 已换新批进入 reindex 长跑。job_claim 的 _record 对人工维护留下的非 JSON info/checkpoint 列容错（{'raw': text}），不再 wedge 整个 worker；17/17 durable_jobs 回归含两条新测试通过。

### KB-007 删除传播与物理 GC

- 优先级/状态：P0 / PARTIAL；Feature：F13。
- 前置任务：KB-002, KB-003, KB-006。当前局部证据：I006, I012, I015, I025。
- 具体工作：实现tombstone即时过滤与依赖标记/二次CAS清理；保护active/query租约/pending/retry/Wiki引用，补计划退役和失败未注册blob回收。
- 完成条件：删除立即所有召回及读回拒绝；被重新引用或旧active依赖资产不删；引用解除、租约/保留期满足后实际释放空间并审计。
- 验收：TC-13.5, TC-13.9, TC-13.10, TC-13.11。当前完整验收：NOT_RUN。

- 现有局部进展：tombstone 即时过滤（读/recall/glob）+ 删除传播（_tombstone_scope 先行）+ 引用计数 blob 物理回收（plan_gc/reclaim）；修复 AuthorityRepository COMMANDS 缺失 tombstone 面导致 7 个 authority 测试回归；不提升完整验收状态。

### KB-008 配额、临时输入 TTL 和恢复备份

- 优先级/状态：P0 / PARTIAL；Feature：F13。
- 前置任务：KB-002, KB-004, KB-007。当前局部证据：I030。
- 具体工作：在上传接收/shared下载、解析渲染、队列、模型tokens/重试和派生版本设置有界配额；原件不永久保留。实现一致registry+AGFS+ACL+模型配置备份与空环境恢复。
- 完成条件：OOM/满盘/超额可观察且无半树；临时原件及Office PDF清理；原件清理后仅能reindex持久产物，换parser明确SOURCE_REQUIRED；恢复后授权和gold一致。
- 验收：TC-13.3, TC-13.4, TC-13.7, TC-13.8, TC-16.6。当前完整验收：NOT_RUN。
- I034进展：离线联合bundle与隔离恢复14专项通过；registry/受控正文journal/身份/配置/向量ACL目录哈希校验，RESTORE_PENDING阻止启动；生产引擎验证及activate未达成。

### KB-009 三模型能力与部署契约

- 优先级/状态：P1 / PARTIAL；Feature：F1。
- 前置任务：KB-001, KB-004。当前局部证据：I001, I002, I003, I018。
- 具体工作：锁三模型官方版本/image digest与模式矩阵，完善native产物→标准block/Markdown/asset适配、截断/缺图/取消/引擎ready校验和可复现Docker配置。
- 完成条件：逐后端真实请求证明effective backend/mode；冷启动/重启/断连不假ready，禁止未经授权换模型或默认12页截断。
- 验收：TC-1.1, TC-1.2, TC-1.3, TC-1.4, TC-1.5, TC-1.8。当前完整验收：NOT_RUN。

- I033当前进展：三后端2页synthetic auto契约探针通过，支持模式/引擎ready/产物身份校验已加强。Logics删除缺失图引用只能视为降级，完整图文适配未达成；全卷及实际新版本部署验收待完成。 [本轮证据](evidence/implementation-I033-2026-10-05.json)。完整验收仍NOT_RUN。

- I053进展：TeleOCR与Logics生产worker真机实跑17页图密集切片（19嵌入图对象），两后端validate_document全过：19/19可见图位保全、绑定提交PDF字节sha256、落图页码与源一致（2/4/6/7/14/16页）；像素级抽检worker PNG vs 本地源PDF同区域重渲RMS 1.50 MATCH；19条均走restored(source_pdf_visible_region_render)兜底渲染路径，两引擎仍未提供精确像素模型抽图。此前"TeleOCR、Logics尚未接入这套保全"的自述已不成立。 [证据](evidence/teleocr-logics-image-preservation-I053-2026-10-05.json)。17页切片不替代KB-010的1072页全卷验收。

### KB-010 三模型全卷质量验收

- 优先级/状态：P1 / PARTIAL；Feature：F1, F2, F15。
- 前置任务：KB-009, KB-011, KB-031。当前局部证据：I002, I003。
- 具体工作：对1072页SY215C分别执行三后端，另冻结无书签/扫描/表格图纸样本；记录质量与parse/write资源时间。已有12页和MinerU全卷不替代另外两模型。
- 完成条件：三份独立完整输入/版本/hash/产物/覆盖/图引用/质量报告；不要求图数量相同；不合格后端不进入可用模式。
- 验收：TC-1.1, TC-1.2, TC-1.3, TC-2.13, TC-15.3。当前完整验收：NOT_RUN。

### KB-011 书签对齐与回退结构质量

- 优先级/状态：P1 / PARTIAL；Feature：F2。
- 前置任务：KB-009。当前局部证据：I001, I003。
- 具体工作：完善命名书签meta、内容候选映射/own-body、相对同级图片、目录安全/稳定URI及全部块覆盖；失败按合法#标题再rule_blocks整本回退。
- 完成条件：无人工等待、无页码硬匹配；持久info明确失败原因/实际策略；原文块恰一次、图可读、无凑空书签；两种结构独立质量达标。
- 验收：TC-2.1, TC-2.2, TC-2.3, TC-2.6, TC-2.7, TC-2.10, TC-2.11, TC-2.14, TC-2.15, TC-2.16。当前完整验收：NOT_RUN。

- I033当前进展：修复index章节文件覆盖、跨章节引用式图片定义、编码路径与HTML图片、重复/乱序page_idx；本地client/deploy校验器已同步。独立结构gold与完整图文质量仍待验收。 [本轮证据](evidence/implementation-I033-2026-10-05.json)。完整验收仍NOT_RUN。

### KB-012 Office 转 PDF 后复用解析

- 优先级/状态：P1 / PARTIAL；Feature：F8。
- 前置任务：KB-008, KB-009, KB-011。当前局部证据：无目标实现证据。
- 具体工作：锁转换容器/字体/输出profile，六扩展名先PDF再同一解析器；Excel全部有效sheet/使用区按可读分页策略，PPT按页序；禁宏/限时/密码明确失败。
- 完成条件：三个文件族各有图文表格gold与坏文件；不使用AnyDoc→md冒充该链路；中间PDF按TTL清理，D1/W1共用转换模块。
- 验收：TC-8.1, TC-8.2, TC-8.3, TC-8.4, TC-8.5。当前完整验收：NOT_RUN。

- I033当前进展：现有Office转换和统一路由已进入真实验证；发现无效LibreOffice参数、宏profile/取消清理等缺陷，正在修复。六格式实际转换及生产容器质量尚未验收，D1当前仍明确拒绝Office。 [本轮证据](evidence/implementation-I033-2026-10-05.json)。完整验收仍NOT_RUN。
- I034进展：Office45专项通过，服务器Docker六真实格式CONTRACT_PASS，取消/超时/宏profile/used-sheet/原件保留验证；D1可用本地转换器时进入统一PDF路由。远端容器CLI未接常驻worker，gold及完整FLOW待验收。

- I047进展：显式环境配置接远端冻结 Docker、真实版本/字体库存/宏禁用与隔离清理；六扩展名转换合同通过；D1 Word 和 W1 三现代 Office 族实际链路通过。仍缺 legacy HTTP/SDK 链路、独立 Office gold、规模/取消终态及完整发布验收。 [证据](evidence/implementation-I047-2026-10-05.json)。

- I050进展：legacy DOC/XLS/PPT六文本链路实测通过；XLS可见图片在MinerU worker artifact中缺失，已冻结源/PDF/原artifact，图保留断言FAIL；CFB字体与gold未验收。 [证据](evidence/implementation-I050-2026-10-05.json)。

- I051进展：同XLS真实PDF保全asset 0→1，D1/W1各恰好1 PNG，真实摘要/图文向量回执及图片URI检索通过；原模型遗漏保留，不等于图意/章节质量、CFB字体或全Office完成。 [证据](evidence/office-image-preservation-I051-2026-10-05.json)。

### KB-013 父子 chunk 的真实写入与召回

- 优先级/状态：P1 / PARTIAL；Feature：F4, F9。
- 前置任务：KB-006。当前局部证据：I004, I005, I006, I008。
- 具体工作：复用已实现真实tokenizer/AST表格计划；验证实际向量后端多child主键、parent扩展预算、不同远距命中不按URI丢弃和旧代退出。
- 完成条件：已知头/中/尾事实均可真实召回；含标题输入不超限；多个chunk共同引用原Viking URI，不新增位置/版本用户负担。
- 验收：TC-4.1, TC-4.2, TC-4.3, TC-4.4, TC-4.5, TC-4.6, TC-4.7, TC-9.2。当前完整验收：NOT_RUN。

- 现有局部进展：父子 chunk 的真实写入与召回（plan.py parent budget + recall）；单测通过；不提升完整验收状态。

### KB-014 图片引用语义与实际 VLM 质量

- 优先级/状态：P1 / PARTIAL；Feature：F5。
- 前置任务：KB-003, KB-006, KB-011。当前局部证据：I011, I012, I013, I014, I015。
- 具体工作：移除每图最多16引用的截断边界，分页发现跨目录引用；用实际VLM tokenizer约束上下文/图预算，真实调用生成用途摘要并按引用缓存。
- 完成条件：17+引用和跨目录均不丢失；context变化不复用错摘要；每个joint输入含对应摘要+原图；人工摘要质量达标且无无依据维修结论。
- 验收：TC-5.1, TC-5.2, TC-5.3, TC-5.5, TC-5.6, TC-16.7。当前完整验收：NOT_RUN。

- 现有局部进展：图片引用语义与实际 VLM 质量（image_context/image_cache）；引用与摘要单测通过；不提升完整验收状态。

- I048进展：章节树内取消8层静默截断；VLM专属tokenizer配置、主备覆盖/指纹及最终预算核验，引用记录区分实测/估算。72专项通过；章节树外引用、完整图像/提示预算和人工质量仍待验收。 [证据](evidence/implementation-I048-2026-10-05.json)。

- I053e进展：章节布局图片质量门——书册解析落盘阶段（`_apply_chapter_layout`）对每张本地图片先过 `is_valid_image` 质量门（最小边/像素/长宽比/大小），不达标图**跳过引用并写入结构化 info 条目 `CHAPTER_IMAGE_QUALITY_SKIPPED`（count+明细）**，解析整体照常成功；缺文件、`viking://` 与远程引用仍为硬 ValueError，写入后的 token 级校验拒绝任何未物化的图片语法（含被剔除图留下的残句）。修复了部署侧保全模块把 undersized 源 PDF 裁片写进 `__source_pdf_images` 导致整本 1072 页手册中止的阻塞。章节管线+契约硬化 63 项回归通过。仍待：完整 FLOW-D1 真册验收与图片语义质量。

### KB-015 多空间 query 编码和融合

- 优先级/状态：P1 / PARTIAL；Feature：F5, F6。
- 前置任务：KB-003, KB-013, KB-014。当前局部证据：I013。
- 具体工作：绑定每空间query/document协议、模型版本与归一化；文本/图片不同空间分别query encode，融合排名并保留引用上下文权限交集。
- 完成条件：相同维度不同模型也不直接比较cosine；真实图文gold搜索命中，空间迁移新代失败保留旧版。
- 验收：TC-5.4, TC-6.2。当前完整验收：NOT_RUN。

### KB-016 真实 BM25 派生索引

- 优先级/状态：P1 / PARTIAL；Feature：F6。
- 前置任务：KB-002, KB-003, KB-006, KB-013。当前局部证据：无目标实现证据。
- 具体工作：实现统一词法provider，首版单协调服务local_fts5或已验收native；锁中文分词/型号/DTC保留规则，用child及图片摘要建倒排并纳入发布receipt。
- 完成条件：关闭dense仍可词法命中并显示BM25索引代；授权和snapshot在取候选前过滤；不以grep/sparse模型当完成。
- 验收：TC-6.1, TC-6.2, TC-16.8。当前完整验收：NOT_RUN。

- 现有局部进展：真实 BM25 词法派生索引（SQLite FTS5 local_fts5）；写入/召回单测通过；不提升完整验收状态。
- I034进展：BM25文本写入真实协调服务发布事务，现有检索器在无dense时实际返回原URI/分块；授权语料隔离词频，tokenizer v2保留真实词频。图片摘要/meta/GC/大语料性能仍待完成。

### KB-017 RRF → rerank → MMR 统一检索

- 优先级/状态：P1 / PARTIAL；Feature：F6。
- 前置任务：KB-013, KB-015, KB-016, KB-019。当前局部证据：I003。
- 具体工作：同一find/search profile实现预算、stable ties、RRF、模型数量校验/降级、最后MMR、parent扩展和URI聚合；叶子全局召回独立于导航/rerank开关。
- 完成条件：dense/BM25/hybrid/+rerank/+MMR消融与故障注入，引用URI可读；明确rerank失败策略、trace有效配置，不增加另一套搜索API。
- 验收：TC-6.2, TC-6.3, TC-6.4, TC-6.5, TC-6.6, TC-6.7。当前完整验收：NOT_RUN。

- 现有局部进展：RRF → rerank → MMR 统一检索（retrieve/fusion.py）；融合单测通过；不提升完整验收状态。
- I034进展：现有hybrid入口已接真实authority词法召回；融合专项通过，显式meta尚未接词法分支且不丢弃限制，全量消融/模型质量未验收。

### KB-018 权威业务 meta 与版本传播

- 优先级/状态：P1 / PARTIAL；Feature：F7。
- 前置任务：KB-003, KB-006。当前局部证据：无目标实现证据。
- 具体工作：保存原值/规范值/来源/normalizer版本，与document revision绑定；chunk/图片/语义继承权威meta，重建未指定不清空，系统字段不可覆盖。
- 完成条件：BM25/dense/图片同字段OR跨字段AND一致；修改meta与正文同代可见；缺meta策略明确。
- 验收：TC-7.1, TC-7.3, TC-7.4。当前完整验收：NOT_RUN。

- 现有局部进展：权威业务 meta 与版本传播（business_meta）；写入/过滤单测通过；不提升完整验收状态。

### KB-019 模糊字典、别名与性能

- 优先级/状态：P1 / PARTIAL；Feature：F7。
- 前置任务：KB-018。当前局部证据：无目标实现证据。
- 具体工作：namespace内精确/别名/prefix/trigram/受限编辑距离词典，显式filters与推断hints分离，有限候选集合交统一检索provider。
- 完成条件：机型模糊可找资料但不替用户确认主体码/配置；硬过滤无候选不撤销；精度/候选膨胀/扫描量/p95真实报告。
- 验收：TC-7.2, TC-7.4, TC-7.5, TC-7.6, TC-7.7。当前完整验收：NOT_RUN。

- 现有局部进展：模糊字典/别名（meta_dictionary）；模糊匹配而非字符串强匹配；不提升完整验收状态。

### KB-020 D2 reindex 的完整契约

- 优先级/状态：P1 / PARTIAL；Feature：F9, F11。
- 前置任务：KB-001, KB-003, KB-006, KB-013, KB-014, KB-016, KB-018。当前局部证据：I004, I009, I010, I014, I015。
- 具体工作：扩展业务writer授权、dry_run零写入、祖先语义传播和两模式有效配置；复用IndexPlan并在job屏障等候全部receipt。
- 完成条件：原MD/目录不改写，parser调用0，不读toc重切章；vectors_only保留L0/L1且有效图片缓存缺失需语义模式；与D1等价。
- 验收：TC-9.1, TC-9.3, TC-9.4, TC-11.1, TC-11.2, TC-11.3, TC-11.5, TC-11.6。当前完整验收：NOT_RUN。

- I051进展：实际单MD/目录D2提交成员批次共同切换，published/backoff SIGKILL恢复不重复建代；注册空间祖先摘要边界修复，完整7门禁及inflight崩溃仍未验收。 [证据](evidence/implementation-I051-2026-10-05.json)。

### KB-021 前端队列与 API/SDK 状态一致

- 优先级/状态：P1 / PARTIAL；Feature：F3。
- 前置任务：KB-001, KB-004, KB-006。当前局部证据：I001, I018。
- 具体工作：tasks/monitoring接持久job，展示排队/第N本、未知进度、written/索引中/ready、info/失败/取消/重试，日志按权限与trace过滤。
- 完成条件：重启后状态不丢；队列空不冒充ready；自动回退成功说明持久且清晰；同步/异步等待超时可接同job。
- 验收：TC-3.3, TC-3.6, TC-3.7。当前完整验收：NOT_RUN。

- I033当前进展：已有持久队列面板/API投影，正在修正列表错误误报为空、未知写入恢复状态、draft_ready与ready区分及当前用户列表授权；局部代码不代表完整UI验收。 [本轮证据](evidence/implementation-I033-2026-10-05.json)。完整验收仍NOT_RUN。
- I034进展：前端完整356项、tsc与生产构建通过，持久队列状态/错误/取消面板已接真实API；未知写入和私有draft独立展示，未用假进度或空列表替代错误。完整身份/四FLOW UI验收待完成。

### KB-022 collection 与注册域绑定门禁

- 优先级/状态：P2 / PARTIAL；Feature：F10, F14。
- 前置任务：KB-001, KB-003。当前局部证据：I023, I024, I027。
- 具体工作：注册/读取collection强制target属于registered namespace、raw来源有独立根，落实一Wiki collection一namespace与目标唯一性；旧不合法绑定迁移或禁执行。
- 完成条件：不能仅写namespace_id绕过成员/域登记；跨域来源可读不代表获导出；原材料域可被多个目标按显式grant使用。
- 验收：TC-14.1, TC-14.5, TC-16.9。当前完整验收：NOT_RUN。

- I033当前进展：新collection必须绑定独立已注册target/raw域；旧记录标legacy_domain_pending，管理员检视摘要并CAS迁移。真实HTTP/SDK及多目标export撤回局部验证通过，完整执行/发布仍待完成。 [本轮证据](evidence/implementation-I033-2026-10-05.json)。完整验收仍NOT_RUN。

### KB-023 三一完整 schema 政策执行

- 优先级/状态：P2 / PARTIAL；Feature：F10。
- 前置任务：KB-022。当前局部证据：I019, I020, I026。
- 具体工作：逐条把38实体+9共享政策及8类型path标记转支持声明/受约束执行器并保存逐规则coverage receipt；核实DTC旧kb来源实际映射，不猜URI。
- 完成条件：默认包严格模式无uncompiled规则且fewshot符合约束；模板/prompt/编译器/模型profile固定；partial规则包只允许draft候选，不进入诊断发布。
- 验收：TC-10.1, TC-10.4, TC-10.5, TC-10.10, TC-10.12, TC-10.13, TC-16.10。当前完整验收：NOT_RUN。
- I034进展：默认三一包38实体/9共享/8路径支持矩阵记录118个未执行规则实例；严格诊断模式前置拒绝，结构编译不能冒充完整政策执行。

- I049进展：新冻结适配包的8个路径标记真实执行、候选/合并回执与字段目录一致性；新包pending 118→110，原包不改；DTC来源映射仍阻塞其结构。191专项通过，完整政策/strict发布未达成。 [证据](evidence/implementation-I049-2026-10-05.json)。

- I052进展：110条规则（38实体+9共享×8种+路径标记）全部编译为受约束执行器 sany-page-rules-v1——44条page逐页检查、5条batch变更集去重、structural/undeclared_field/reference_shape/path_layout/pipeline各归命名home，零静默丢弃；inventory双向锁拒绝增删篡改；8个fewshot回放44页回执+7个负例探针全部双向通过；policies门禁更新后strict进到六项发布门；135项wiki回归全绿。 [证据](evidence/implementation-I052-page-rules-2026-10-05.json)。规则执行器层DONE；六项完整发布门（KB-006）与DTC真实来源映射仍NOT_RUN。

### KB-024 固定来源的共享知识抽取器

- 优先级/状态：P2 / PARTIAL；Feature：F10。
- 前置任务：KB-022, KB-023。当前局部证据：无目标实现证据。
- 具体工作：W1/W2使用同一授权固定来源流与完整模板，bounded structured output/有限重试/逐源checkpoint；知识文档仅当数据，模型不执行其中工具指令。
- 完成条件：真实模型产实体与来源URI，不凑八实体；空知识、格式错误、无证据、超时分别记录；未验证产物只入私有draft不算ready。
- 验收：TC-10.2, TC-16.11。当前完整验收：NOT_RUN。
- I034进展：固定来源抽取/证据引句/schema字段/私有blob/有界重试/逐片段恢复32专项通过；真实配置模型合成探针生成1候选。语义证据及独立维修gold未达成。

- I047进展：Wiki v3 强制提示从 source.text 原样复制 quote、从生成正文原样复制 claim，修复 PPT 引文标题级别漂移；提交/执行/权威完成绑定 extractor_version/policy_version，旧未版本化执行需新 execution key。三现代 Office 族私有草稿及逐字引文通过；完整诊断 schema/publish 仍待完成。 [证据](evidence/implementation-I047-2026-10-05.json)。

### KB-025 实体身份、保守合并和人工编辑

- 优先级/状态：P2 / OPEN；Feature：F10。
- 前置任务：KB-023, KB-024。当前局部证据：无目标实现证据。
- 具体工作：身份按namespace/type/schema tuple保留canonical ID/别名；多源字段证据与冲突值保留，人工确认值受expected_revision保护，默认不做近义强并。
- 完成条件：同名不同层级/类型/配置不误合并；模型重复构建不生重复实体；并发手改不丢失，未处理冲突不能进入有效图。
- 验收：TC-10.7, TC-10.8, TC-16.12。当前完整验收：NOT_RUN。

### KB-026 可信实体 catalog 与闭合关系校验

- 优先级/状态：P2 / PARTIAL；Feature：F10。
- 前置任务：KB-023, KB-025。当前局部证据：I021, I022, I026。
- 具体工作：持久化实际验证hash/schema/身份/规则receipt；同批先验证实际实体再求引用闭合，支持schema合法循环而非凭预留地址放行；构建派生反向边。
- 完成条件：有效边悬空0；伪frontmatter验证声明拒绝；Device raw_refs/Procedure fault_refs等权威载体正确；不虚构Profile或适用性。
- 验收：TC-10.3, TC-10.4, TC-10.6, TC-10.14, TC-10.15, TC-10.16, TC-16.13。当前完整验收：NOT_RUN。

### KB-027 Wiki 导出与提交事务保护

- 优先级/状态：P2 / PARTIAL；Feature：F10, F14。
- 前置任务：KB-003, KB-006, KB-022, KB-026。当前局部证据：I027。
- 具体工作：执行前/逐源使用/最终commit都验证source read、target write、单向export及当前主体；提交事务校验来源成员/hash/epoch、实体revision和ACL/policy fence。
- 完成条件：抽取后撤权/改源/新增目录成员/目标变化拒绝发布；所有事实+关系+索引同批，旧Wiki可用；不要把prepare授权当永久receipt。
- 验收：TC-14.5, TC-16.14。当前完整验收：NOT_RUN。
- I053本轮局部进展：私有候选树发布落地——`PrivateWikiMerger.publish` 将 valid_graph 内 admitted 组的每个 body variant 按 candidate_uri 落为真实 md 节点（一节点一md），同 sha 变体重联合并、跨源哈希冲突拒绝、已被人改写的页面拒绝覆盖、pending_identity/conflict 组永不落盘；HTTP 入口 `POST /api/v1/kb/wiki/merges/publish`（reference{sha256,size} 定型模型 + blob 摘要作真 revision、调用方 revision 作 CAS 声明，错配映射 409 而非 400）。39项回归通过含9项发布测试（5项新 HTTP 测试覆盖成功发布/409 CAS/400 坏回执/422 缺 revision/OpenAPI 契约），证据 [wiki-private-publish-I053](evidence/wiki-private-publish-I053-2026-10-05.xml)。
- I053b进展：publish 改为整批预检后才落盘——`_plan_pages` 在首次写前完成 URI 冲突检测、非 source 关系目标闭包（同批页按编译后 kind 匹配；磁盘页用当前 schema 重验内容，伪 frontmatter 验证声明因边重验内容而被拒）与全批并发人改检查；任一门失败写 0 页。新增2测试（悬空目标整批拒发+同批闭包接受），36项回归通过，证据 [wiki-private-publish-closure-I053b](evidence/wiki-private-publish-closure-I053b-2026-10-05.xml)。
- I053c进展：发布页接入共享 D1 索引管线——`_index_pages` 在全批落盘后逐页走 `enqueue_text_plan`（`vectorize_file` 同款校验路径：磁盘重读+SHA256 核验 `_source_snapshot`、business-meta sidecar 捆绑），chunk 引用页面 Viking URI（无第二套检索）；注册表逐页钉 generation，publish 结果新增 `indexed_chunks`。新测试覆盖消息契约（URI/level=2/每页独立 plan、registry pin、content_revision==磁盘 SHA）+ 消费端逐条成功回收且 uri 全可溯源，37项回归，证据 [wiki-private-index-wiring-I053c](evidence/wiki-private-index-wiring-I053c-2026-10-05.xml)。仍缺：与旧 Wiki 数据共存的全量回归、全量 diagnostic 图发布。
- I053d进展：写入途中崩溃恢复——publish 重放改为把**全部 planned 页**送索引（`_plan_pages` 预检已钉死每页内容且拒绝人改页，对崩溃前已落盘页重投幂等：registry 每页开新 generation、desired 原位覆盖），崩溃中途落盘的页不再滞留索引之外；此前旧逻辑跳过已存在页导致这些页永不入索引。新增2测试：崩溃后重放治愈整批（1 页崩溃前落盘、队列空、重放后全批 URI 入队且 content_revision 对齐磁盘 sha）+ 发布→索引全链（见 I053c）。65项回归通过（含 HTTP 发布与 D1 管线套件），证据 [wiki-replay-index-I053c](evidence/wiki-replay-index-I053c-2026-10-06.xml)。仍缺：事实+关系+索引同批原子提交并与旧 Wiki 共存、全量 diagnostic 图发布。

### KB-028 两个 Wiki 入口接共同持久执行器

- 优先级/状态：P2 / PARTIAL；Feature：F10。
- 前置任务：KB-004, KB-006, KB-009, KB-012, KB-023, KB-024, KB-025, KB-026, KB-027。当前局部证据：I025, I028, I029, I030。
- 具体工作：W1 PDF/Office/MD及资产目录先复用D1物化，再与W2同一构建job；固定模板/执行profile，抽取→合并→关系→IndexPlan→发布，支持恢复与取消。
- 完成条件：上传和URI同输入得语义等价结果与可读来源；prepared与job分开可查，失败不把半图标ready，不新增第二套检索。
- 验收：TC-16.15, TC-16.18, FLOW-W1, FLOW-W2。当前完整验收：NOT_RUN。
- I034进展：旧prepare保持200；新增execute202持久W1/W2 job，已捕获MD同一抽取流程/私有draft读取/plan及权限revision完成CAS通过。PDF/Office完整W1调度、可信归并/关系/共同发布仍待完成。

### KB-029 多源增量、证据撤回和投影重建

- 优先级/状态：P2 / OPEN；Feature：F10, F13。
- 前置任务：KB-007, KB-025, KB-026, KB-027, KB-028。当前局部证据：无目标实现证据。
- 具体工作：建立source revision→事实/关系依赖；替换/删除/撤权撤回对应证据，仍有合法来源事实保留，零证据状态stale或移出热路径，反向投影重建。
- 完成条件：多源汇入不乱写collection；删除最后来源与手改并发可解释；旧source版本不支持新事实；迁移可回滚且不覆盖人工知识。
- 验收：TC-10.8, TC-10.9, TC-16.16。当前完整验收：NOT_RUN。

### KB-030 确定性 Wiki 诊断展开

- 优先级/状态：P2 / PARTIAL；Feature：F10。
- 前置任务：KB-003, KB-026, KB-027。当前局部证据：无目标实现证据。
- 具体工作：按固定schema/profile执行有界链接遍历，配置Profile与无配置asserted_faults分别显示依据；仅模糊症状归一用统一检索。
- 完成条件：禁止模型调用后仍精确展开；配置未知undetermined，不把通用断言变成特定配置概率，不跨域、不因缺边造边。
- 验收：TC-10.11, TC-10.16。当前完整验收：NOT_RUN。

- 现有局部进展：确定性有界诊断展开 DiagnosticExpander（无模型调用）：配置 Profile 路径与无配置 asserted_faults 分离展示；缺失边/越 namespace 记 gap；模糊症状归一仅走注入的统一检索；不提升完整验收状态。

### KB-031 独立 gold 与构建期候选评测

- 优先级/状态：P1 / PARTIAL；Feature：F15。
- 前置任务：无；可独立启动。当前局部证据：I003, I005, I025。
- 具体工作：继续生成synthetic候选，人工核验来源支持/结构边界/图摘要及诊断案例；按文档/机型分组防同段改写泄漏，冻结数据和generator版本。
- 完成条件：gold/synthetic严格分离；覆盖型号/DTC/语义/表格/图片/尾部/模糊meta；报告类别样本量与CI，小数据结论明确限制。
- I031本轮局部进展：独立gold审定、冻结hash、分组泄漏拒绝与分类/CI报告工具已实现；50 synthetic、0 gold、单文档单机型，真实质量/性能NOT_RUN。 [独立报告](evidence/parallel-KB-031-2026-10-04.json)。任务仍PARTIAL，完整验收NOT_RUN。
- 验收：TC-15.1, TC-15.2, TC-15.3, TC-15.4。当前完整验收：NOT_RUN。
- I034进展：用户2026-10-05确认暂时没有人工审定数据/审核安排；保持50 synthetic、0 gold，真实模型probe不计gold，质量门禁保留。

### KB-032 四 FLOW 与上线验收

- 优先级/状态：P1 / OPEN；Feature：F1, F2, F3, F4, F5, F6, F7, F8, F9, F10, F11, F13, F14, F15。
- 前置任务：KB-001, KB-002, KB-003, KB-004, KB-005, KB-006, KB-007, KB-008, KB-010, KB-012, KB-013, KB-014, KB-015, KB-016, KB-017, KB-018, KB-019, KB-020, KB-021, KB-028, KB-029, KB-030, KB-031。当前局部证据：无目标实现证据。
- 具体工作：生产同配置跑四入口、故障矩阵/质量消融/并发5基线、迁移/备份恢复与权限审计；核心D1/D2先可独立验收，Wiki只有M3门禁通过才开放experimental构建。
- 完成条件：分里程碑分别出报告，四FLOW全通过才称全目标达成；不拿692局部PASS作生产验收；无实测RTO/RPO/容量不称高度可用。
- 验收：FLOW-D1, FLOW-D2, FLOW-W1, FLOW-W2, TC-16.17。当前完整验收：NOT_RUN。

- I047进展：D1 Word、W1 三现代 Office 族、完整 schema strict 拒绝、真实 D2 index gate 新增实际证据；仍为 OPEN，不以合同 PASS 替代完整四 FLOW。 [证据](evidence/implementation-I047-2026-10-05.json)。
- I052进展：真机探针（flowtest 用户键、本地 1933 服务）单 MD 与目录两种 D2 入口均到达 ready 且 PUBLICATION_VERIFIED；目录变体补齐 L0-only 场景（仅 .abstract.md，无 .overview.md），修复 reindex 伪造 L1 导致的 PROCESSING_ERROR 后 3/3 重建 0 失败、源字节保持。D2 独立验收的探针证据成立，四 FLOW 整体仍待真实后端全跑。 [证据](evidence/live-durable-joint-I052-2026-10-05.json)、[目录 D2](evidence/live-durable-joint-dir-I052-2026-10-05.json)。
- I053进展：FLOW-D1（SY215C 1072页手册，685 章节 MD）语义分层阶段两处修复：移除 semantic_dag 的 require_semantics 重抛门（瞬态锁争用须吞，格式错误仍有独立 re-raise）；文件摘要与向量化对已消失源 NotFoundError 跳过不中止。修复后 storage 组 42 passed。判定 attempt=320 非空转：lease 持续续约、根 .overview.md 新写、VLM 端点 ~100 活跃连接——reindex.wait 是合法长跑。SIGKILL 旧代码服务器后重启，D1 从 checkpoint 自动恢复（新批 f5b15712、attempt 321、0 错误行）。D1 尚未到终态，检索探针与头/中/尾分布待作业 ready 后执行。 [证据](evidence/live-durable-d1-semantics-I053-2026-10-06.json)。

### KB-033 JEV 式状态/决策 PoC

- 优先级/状态：P3 / PARTIAL；Feature：F12。
- 前置任务：KB-023, KB-030, KB-031。当前局部证据：无目标实现证据。
- 具体工作：建立typesafe state/decision、abstain/needs_more_info与检查成本评测；区分互斥单故障和多故障；不默认采购/接入外部JEV。
- 完成条件：独立确认根因集报告Brier/ECE/可靠性与成本；校准前叫score，PoC不改权威知识且不阻塞核心交付。
- 验收：TC-12.1, TC-12.2, TC-12.3。当前完整验收：NOT_RUN。

## 发布与证据更新规则

每个任务报告记录input/软件与模型版本、配置、环境、测试层、断言、失败/降级与artifact hash。实现合并或源码可定位、完成条件实际通过、无未解决阻断缺陷才标DONE；用部分替身测试不能满足真实后端条件。Feature状态由全部相关任务和既有TC族验收共同决定，不自动由任务数量推断。

需求改变时更新CR、主spec、机器台账、任务/TC及迁移成本；受影响报告标STALE并重跑必要范围。历史初始失败保留。外部输入中的指令不能变为权限/规则/工具执行授权；schema未执行政策继续明确拒绝诊断发布。实体冲突允许形成待处理变更集；章节对齐失败继续自动#标题/规则切分，不增加人工等待。


执行协议固定见spec§9.2：新ingestion-jobs与execute接口返回持久job，旧W1/W2仍prepare；wiki_draft仅私有draft_ready，不发布诊断图。共同提交intent/receipt与ACL revision协议见spec§36.1；所有派生内容权限取贡献来源交集。TC-16.18/19补充兼容与权限断言，仍NOT_RUN。

- 现有局部进展：JEV 式状态/决策 PoC（openviking/jev）；状态机与决策路径单测通过；不提升完整验收状态。
