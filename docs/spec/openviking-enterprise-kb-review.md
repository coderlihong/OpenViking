# 企业知识库规格审查记录

> 当前版本：v0.8 · 2026-10-04 · CR-008。已按用户最新决定同步规格、验收用例与实施阶段。
> 当前已有部分实现和三后端部署联调；完整产品验收仍未完成。下文 CR-006 及更早章节是历史文档审查，不能覆盖当前实施证据。

## CR-008：四主入口与分层索引实现边界

最新用户需求确认为 D1 上传文件指定 URI 前缀、D2 指定 URI 构建分层索引、W1 上传文件指定 collection、W2 指定 URI 构建 Wiki。已同步 spec §0.1、§2.1、§8/§9 与 FLOW-* 验收。

修复设计歧义：Wiki collection 绑定 schema 和目标 namespace，用户不重复指定冲突配置；两个 Wiki 入口共享构建器；已有 MD URI 不重跑 PDF 解析或改写原目录；来源只引用真实 Viking URI。章节树与检索父子 chunk 分别验收。I004 为文本父子索引的部分实现，不能据此宣称四入口、图文联合索引或线上高可用完成。

## CR-007：部署证据与实现逻辑复核

| 问题 | 修订 | 证据边界 |
|---|---|---|
| parse_mode 同时承担模型模式和旧 no_split 策略 | 模型统一命名 model_mode；API/SDK/worker 对齐 | 定向参数/路由测试；完整企业 API 待实现 |
| 取消只改包装任务，底层 MinerU 仍运行；迟到结果重新成功 | cancel 传播到底层；running→completed 原子发布，取消不被异常覆盖 | 六项 worker 生命周期测试；真实 GPU 排空仍待故障注入 |
| HTML 全文正则把代码示例当图片 | 从 Markdown AST HTML 节点检查图片 | 真 HTML 缺图反例和围栏示例测试 |
| ACL off 时 THINKING 全局叶子召回缺失 | 移除开关条件，仍用 tenant/scope 代理 | 37 项定向检索回归；生产授权/混合检索仍待验收 |
| “未部署、无法读取手册”的历史句子与当前状态冲突 | 三服务健康和有限联调单列；重新记录手册 SHA/规模 | I002/I003；不提升整体 Feature 为 PASS |

[当前 I003 实施证据](evidence/implementation-I003-2026-10-04.json)；完整手册与生产入库、父子索引、联合 embedding、混合检索、Wiki 仍需继续。

## 0. 历史 CR-006 修订：自动回退、URI溯源与可靠性

章节对齐失败不等待人工：先使用Markdown的 `#` 标题结构，无法形成有效结构时按内容块规则分组。整本文档统一回退以避免混用两棵树；任务 `info` 持久记录“书签对齐未成功”和实际策略。内容覆盖、图片、索引合格后正常 `ready`。离线质量金标仍需独立标注，与线上处理流程分开。

| ID | 发现/用户决定 | v0.6修订 | 验收映射 |
|---|---|---|---|
| RV-24 | 对齐失败人工等待会阻断串行队列，非written分支也缺释放规则 | 自动回退；written释放默认解析槽，失败/取消/退避释放，重试重新排队；对齐与回退有时间预算 | TC-2.6/2.12/2.14–2.16、TC-3.6/3.8/3.9、E2E-7 |
| RV-25 | generation范围不明确，单文档重建可能隐藏其他文档，一次查询可能混代 | 文档级代次、namespace不可变快照映射；A重建继承B；查询固定快照；并发发布CAS冲突重合并 | TC-11.7–11.9 |
| RV-26 | GC只考虑读租约与TTL，失败重建可能失去仍在使用的旧正文/图片 | active索引、查询、有效任务及Wiki构建依赖全部保护；无引用且过保留期才回收，清理前复核并发发布 | TC-13.9–13.11 |
| RV-27 | 单span无法覆盖重复表头/标题前缀，且用户只要求URI溯源 | 对外只要求可读Viking URI，不新增位置映射或版本读回协议；内部revision保留一致性用途，多chunk保留各命中内容 | TC-4.4/4.6/4.7、TC-6.5、TC-9.2、TC-10.2、E2E-2/7 |

分层chunk与embedding设计保留：child召回、parent上下文、L0/L1语义导航分开；章节规则切分与embedding分块分开；文本使用配置模型，图片走“上下文摘要+原图”联合输入，初次入库和reindex共享IndexPlan。

当前检查证据见 [v0.6文档校验](evidence/review-v0.6-2026-10-03.json)。新增12条功能验收契约及1条E2E，累计114条功能用例、7条E2E；均待产品实现后执行。未重新运行三模型、真实手册或生产故障注入，不将下方历史检查当成本轮新执行结果。

下列第1–4节保留CR-005历史审查。与当前决定冲突的条目已标明被CR-006替代；当前规范以v0.6主规格和测试文档为准。

## 1. 历史发现与修订（CR-005）

P1：会造成目标偏离、数据错误/泄漏或假成功；P2：影响可实施性与验收可信度。此处“已修订”均指文档层面。

| ID | 级别 | v0.4 问题与后果 | v0.5 修订 | 验收映射 |
|---|---|---|---|---|
| RV-01 | P1 | 用户要求 TeleOCR，却全篇写 TextIn；实现后仍不满足需求 | 恢复 TeleOCR，查官方代码确认输入/输出；三模型分别验收 | TC-1.1–1.3 |
| RV-02 | P1 | 不同 URI 被当成天然互不可见 | namespace 注册+ACL；检索、读回、缓存、日志、Wiki派生内容全部覆盖 | TC-14.* |
| RV-03 | P1 | 页码不参与切分，却强制前置内容 p1–20 和父子区间全不重叠 | 内容区间依 blocks；页码提示可重叠/为空；父子own-body与子树范围分开 | TC-2.2/2.7/2.9 |
| RV-04 | P1 | 书签编号唯一被当成模型标题全匹配；强制491个文件可被空文件满足 | 全局对齐、容器/alias分类与覆盖金标；原歧义待审已被CR-006自动回退替代 | TC-2.3–2.8/2.13 |
| RV-05 | P1 | 只允许 Markdown 标题行，OCR不带 `#` 时无法匹配；未定义分支正文边界 | 适配器规范候选；分支只存own-body；CR-006统一回退保留全部内容 | TC-2.4–2.8 |
| RV-06 | P1 | 无书签本期不做，却承诺所有Office转PDF可走同链路 | CR-006调整：M1带书签及基础自动回退；M1b扩展无书签质量评测与Office验收 | TC-2.12、TC-8.* |
| RV-07 | P1 | “永久不保留原件”和“一律断点续解析”同时承诺 | 活跃暂存/TTL、后端恢复粒度、持久产物重建与SOURCE_REQUIRED分开 | TC-3.4、TC-13.3/13.4 |
| RV-08 | P1 | 降队列并发到1即称串行；多进程/内置路径与索引完成未定义 | 全局解析资源租约，written/ready屏障，失败/取消/前端契约 | TC-3.* |
| RV-09 | P1 | 将图片联合embedding列为“待拍板”，允许摘要文本代替完整通过 | 联合输入为主验收，显式降级不计F5达成；独立空间正确查询融合 | TC-5.* |
| RV-10 | P1 | 误称无图片embedding链路，忽略已有实现；reindex仍只取文本 | 复用已有多模态输入；补VLM章节上下文，共享IndexPlan修复重建差异 | TC-5.1/5.2、TC-11.1/11.3 |
| RV-11 | P1 | sparse等于BM25、MMR只接THINKING，完整顺序不明确 | 真词法BM25+RRF→rerank→MMR；统一混合profile和消融验收 | TC-6.1–6.3/6.7 |
| RV-12 | P1 | 宣称导航总有叶子兜底，但当前L2召回有ACL条件 | 授权范围内L2独立兜底，测试ACL/导航/rerank组合 | TC-6.4 |
| RV-13 | P1 | Contains被当成快速模糊业务匹配；全局否认prefix/regex | 区分适配器差异、字典候选/alias/错字、硬filter与软hint、性能 | TC-7.* |
| RV-14 | P1 | 按source URI强制只保留一条结果，会丢长md多个证据 | CR-006调整：parent/child独立身份，聚合保留matches，仅按URI读回，不要求span/版本参数 | TC-4.3–4.6、TC-9.* |
| RV-15 | P1 | Wiki悬空标open_gap即可通过，和现有Profile schema冲突 | 有效发布边悬空0；区分缺知识、坏引用、规则违反、冲突；无证据不建Profile | TC-10.4–10.6 |
| RV-16 | P1 | Profile只认ConfigSpec，忽略合法Device锚；诊断链简化遗漏 | Profile恰一锚、direct_component、有序Fault；DTC合法直达边单独处理 | TC-10.3/10.4/10.11 |
| RV-17 | P1 | 缺幂等、同名处理、更新/删除传播与索引原子切代 | document/revision/generation、CAS/outbox、GC、源到Wiki的证据失效 | TC-13.*、TC-10.8/10.9 |
| RV-18 | P2 | API/SDK只有“加参数”，parent/to含义混淆，角色与extra=forbid未处理 | 具体请求/响应/错误/兼容契约，企业域授权，sync/async与前端参数透传测试 | TC-1.6/1.7、TC-14.4 |
| RV-19 | P2 | 相对图片链接与既有rewrite转绝对URI同时要求；按目录无法区分叶子上下文 | 持久相对、API规范URI；occurrence/source_uri上下文，显式控制rewrite | TC-2.10、TC-5.1/5.5 |
| RV-20 | P2 | 13%完成度、模型图片数一致、30分钟完成和循环金标缺依据 | 全部产品验收NOT_RUN；独立gold、逐后端质量和实测性能；证据驱动状态 | TC-15.* |
| RV-21 | P2 | 测试代码示意用os.listdir(viking://)，且从symptom读possible_faults | 改为行为契约，用Viking API+Profile关系测试；不再冒充可运行测试 | TC-4.6、TC-10.4 |
| RV-22 | P2 | 测试文档称真实PDF已清除；旧实测当本轮证据 | 文件存在且在主工作区可读（1072页/491书签，sha256留档）；审查沙箱曾拒绝读取；人工gold仍待建立 | FX-SY215C |
| RV-23 | P2 | JEV排序等于Profile顺序就是正确，检索分可冒充概率 | 确认根因/检查成本金标、拒判与校准；实验功能不改写权威知识 | TC-12.* |

## 2. 历史源码与参照核查（CR-005）

代码基线 HEAD：`e8d634c88fa96ca589009355c54dca98295d04fd`，含现有未提交工作。精确文件hash记录于[审查证据](evidence/review-2026-10-03.json)。本轮未修改产品实现文件。

| 观察 | 证据 |
|---|---|
| 命名目标取下标后误当整数页码 | `openviking/parse/parsers/pdf.py::_extract_bookmarks/_resolve_bookmark_page` |
| 入库图+摘要进入向量输入已有 | `openviking/utils/embedding_utils.py` IMAGE分支；`storage/queuefs/embedding_msg_converter.py` |
| VLM未获得章节上下文 | `openviking/parse/parsers/media/utils.py::generate_image_summary` 传 `No additional context` |
| reindex资源文件走文本/summary | `openviking/service/reindex_executor.py::_best_resource_file_vector_text` 及 `process_file` |
| 全局L2受ACL条件控制 | `openviking/retrieve/hierarchical_retriever.py` THINKING分支 `leaf_results` |
| 底层已有prefix/contains/regex | `src/index/detail/scalar/filter/filter_ops.cpp` |
| Contains语义不可直接跨后端等价 | `storage/vectordb_adapters/qdrant_adapter.py`、`storage/vectordb/index/cuvs_index.py` |
| reindex URI入口、角色和dry_run限制已有 | `openviking/server/routers/content.py`；当前dry_run仅允许prune_orphans，扩展IndexPlan dry_run仍需实现 |
| parent与to并非同义 | `openviking/server/routers/resources.py::AddResourceRequest`；`service/resource_service.py` |
| WeKnora parent不参与向量、MMR用token/Jaccard | `/Users/wh/code/WeKnora/internal/types/chunk.go`、`internal/reranking/mmr.go` |
| WeKnora归一保留语义标点 | `/Users/wh/code/WeKnora/internal/application/service/wiki_ingest_dedup.go::normalizeWikiIdentityTitle` |
| Profile锚点恰一、有序非空Fault、目标必须存在 | `/Users/wh/code/iroot-llm/packages/xeno-adp-agentic/config/agentic-front/entities/Profile.schema.yaml` |

参考目录 `datas/menus` 只用于理解用户要的可读组织形态，未把它的编号/页码/图片数量当成规范。用户截图中提及的 HTML 本轮未获得完整可核对原件，因此没有沿用其具体审计数字作为当前事实；诊断约束以可读取的 schema 与用户目标为依据。

官方研究来源见主 spec §3.2、§8.3。TeleOCR已核对README/infer.py/engine.py；Logics V3已核对README的启动与输出；MinerU各版本输出/启动有差异，部署必须锁版本。未下载模型或测得GPU性能，不能从README基准外推1072页手册耗时。

## 3. 历史执行记录（CR-005，v0.6未重跑）

1. 读取两份旧文档并逐项对照用户需求、源码、WeKnora与iroot实体schema。
2. 从当前PDFParser源码AST抽取三个书签方法，使用合成目标执行：命名bytes得到错误p113；直接对象引用对照得到p21。该复现说明类型分支缺陷，**没有运行真实手册，也没有修复产品代码**。
3. 真实文件stat显示379,089,082字节；审查会话沙箱内PyMuPDF打开返回Operation not permitted。**另一个AI随后记录主工作区复核成功：1072页、491书签（sha256见历史evidence JSON）。** 此处保留该记录及其环境差异判断；v0.6本轮未重新打开手册核验。
4. 检查规格之间的R/F/TC覆盖、用例ID唯一、Markdown链接/表格/JSON示例，以及最终修改范围。结果保存在审查证据中。

没有执行的事项：SSH部署、三模型推理、Office转换、向量/检索性能、完整PDF入库、Wiki抽取、JEV PoC、所有新增产品验收测试。原因是本轮交付目标为审查修订spec；这些已经作为后续实施和验收任务明确列出。

## 4. 剩余风险与开工入口

规格不再以隐含假设掩盖以下实施风险：真实手册访问权限；服务器GPU/驱动/容量；三后端的锁版输出兼容性及图片质量；多模态服务联合输入；VikingFS快照发布/内部版本依赖回收；全文索引中文分词；schema与人工编辑迁移成本。均有对应阶段和测试，不需要为了写完spec先凭空判定成功。

开工按M0→M1：先定义授权/身份/任务/发布契约与小fixture，再打通单模型完整链路，随后逐个扩展模型与Office。核心检索M2达成后再做experimental Wiki/M4；功能状态以真实验收逐项推进。


## I001 实施复核（2026-10-03）

已授权实施 PDF 章节/自动回退首批代码。修复命名目标页码误读，新的章节路径不按页码注入标题；标题歧义走整本回退；真实错误不伪装为成功。新增控制文件不参与摘要/向量/reindex，补充任务说明持久化和中英文 UI。局部测试证据与未实现项见 [实施记录](openviking-enterprise-kb-implementation.md)。此前 v0.6 文档检查的 hash 属于实施记录加入前版本；新工作树 hash 单独保存在 I001 证据中。

I003 全卷验证发现真实质量差距：1072 页产物完整，但书签候选无法按顺序全局对齐，触发 heading_order_conflict。自动标题回退正常，标记对齐未成功；后续复核模型阅读顺序/标题候选，不能凭页覆盖将章节质量标为通过。

## I005 实现审查补记

修正长表格只保留字节但失去表头语义的问题；增加完整行组重复表头和单行连续分片。发现新 revision 的 child ID 没有旧 row 可继承标签，reindex 改为显式读取授权 source vector 并传递 search_tags；仍需权威文档元数据 registry。chunker_version 纳入 collection metadata 迁移指纹，避免规则变更在旧 collection 静默混写。首次入库/URI 重建真实底层调用链对齐；全卷回放与生产检索验收分开记录。

## I006 实现审查补记

新 revision/相同内容尝试必须使用独立物理 generation，否则标签更新也会在发布前覆盖旧 active row。已经扩展物理 ID，并把 producer seal 和 child receipt 分离；公开排名前固定可见代次，URI 绑定防止复制/移动未注册地址借用旧 generation。配置探测不触发 singleton 初始化，修复验证时发现的 live rerank 干扰。GC 为只读候选，不能在依赖注册未完整之前执行删除。文本向量基础不等于正文、语义目录、图片、BM25、namespace 安全及 ready 的完整原子发布，原目标继续保留。

## I007 实现审查补记

修复文本向量切代而原 URI 正文先被覆盖的阶段缺口：派生正文只存 AGFS 私有版本，SQL 仅保存 reference；正文 hash 与 chunk plan 绑定，在同一发布事务更新正文 pointer/向量代次。公共来源读取按 query/active snapshot 投影并检查当前源权限/存在性；内部构建明确读工作数据。标准文件写入 epoch 阻止过期计划，跨入口 CAS/整本/L0/L1/图文/BM25 仍待完成。正文版本属于派生知识资产，不变成保留原始 PDF 的要求；物理 GC 必须等待统一依赖保护，当前只有只读根计划。


## I008 实现审查补记

修复递归文档按单章节提前切代的问题：批次 producer 成功结束与全部文本 child receipt 构成双屏障，最终事务逐成员再验证版本有效性。reindex 的已捕获 counter 错误也使批次失败。共享 Semantic DAG worker 显式绑定所属批次与工作正文，避免跨任务 ContextVar 串用。单文本/多文本已发布映射保留无关文档；重复 URI、作用域冲突拒绝。目录 L0/L1、图文、BM25、首次根可见性、删除与 job ready 仍未纳入，不能把局部测试当成整个企业发布完成。原 URI 溯源与四入口契约不变。


## I009 实现审查补记

修复目录 L0/L1 固定物理 ID 导致发布前原位覆盖：目录整体 generation 和统一 member 身份加入文本批次。部分层/marker 重建也重建完整已存在语义层，避免 legacy 更新被 active 过滤隐藏。受管 DAG 的目录错误和 reindex 语义 ProcessResult 不再被忽略。空 marker clear 暂拒绝，不能绕过生命周期删 active row。目录向量与正文批次共同发布得到局部证据；sidecar 内容与修改 CAS 未完成，不能宣称根快照或整本发布通过。初始文件锁测试因缺失 fixture/旧 FakeVikingFS 出错，已修复独立测试契约并保留历史报告。


## I010 实现审查补记

修复目录向量已有 generation，而 public sidecar/abstract/overview 提前读工作文件的阶段缺口。真实侧文件归档后与向量 body 校验，sidecar epoch/SHA 在 begin/publish 复核；查询按语义 reference 投影，未发布新层不可泄漏，旧记录缺引用需重建。shared reindex/marker/direct index 与内部 metadata/freshness 处理使用工作版本，防止旧快照再次入库。GC 增加侧文件根但不删除。根结构/首次可见性、外部绕过标准写入 CAS、图文/BM25/job/Wiki 等依旧未完成，不据局部一致性宣称生产可用。


## I011 实现审查补记

发现URI图片vectors_only仍用纯文本_upsert_context，改用首次入库相同vectorize_file。企业模式在producer与consumer拒绝静默文本降级，摘要失败不再吞成空成功。引入AST逐引用上下文与分别VLM请求，避免按图片物理目录猜语义。438项回归通过；真实远端联合请求另有有效向量证据，但不能据此宣称图文召回质量或共同发布已通过。逐引用独立索引/缓存、图片快照/代次发布仍缺，16引用和估算预算是明确临时限制。四入口/API/schema目标不缩减，Wiki仍拟实现。


## I012 实现审查补记

发现图片固定ID仍可提前覆盖旧结果；修复为独立generation+member+原图reference，共用文本/目录发布屏障。归档原图与模型输入同一次读取，consumer内容按queued摘要物化，标准写入epoch阻止过期原图发布；held query按旧reference读图片。新增图片索引版本指纹迁移保护。8项新故障/一致性用例和446项回归通过；没有据此声明引用级索引或整本发布通过。原图快照属于解析知识资产，原始PDF仍不保留；初次根可见性、多空间、BM25和上下文失效依赖均待完成。


## I013 实现审查补记

原始HTTP探测的messages成功不能覆盖实际adapter input请求，现已修显式wire_format、sync/async/query协议和模型/计划身份迁移。真实adapter远端结果与fixture测试分别记录。保留旧input默认、不自动猜测协议或降级；base64由低层POST路径自行解码。发现本机系统proxy影响连接池测试，只设测试loopback NO_PROXY并维持原连接断言，不改变production网络策略。472项回归通过，不将协议结果提升为VLM摘要/真实检索/四入口验收。


## I014 实现审查补记

修复逐引用VLM结果被合并为一row、URI语义阶段丢引用信息及增量图片仅按字节判断语义可复用三个缺口。独立成员、同asset generation、SHA拒绝过期图摘要与matches上下文URI已有局部证据。新版vectors_only无缓存暂显式拒绝，不降级压回合并row。新增schema/version迁移门禁。正文更改依赖和跨ACL/撤权前置过滤仍缺，不能用fixture通过证明企业安全或真实召回；完整F5/四FLOW未验收。


## I015 实现审查补记

修复仅vectors_only无法复用引用语义：缓存归档私有AGFS而非SQLite plaintext，绑定原图/context/epoch/prompt/model并随asset generation发布。完整引用cache不合并、不重新调VLM，失效要求semantics重建；重开/损坏/权限/编辑/GC有局部证据。新增来源epoch依赖在begin/publish复核。进一步发现标准write预先invalidate而未标记in-flight的窗口，属于尚需修复的fence/CAS缺口，不声明完整并发安全。目录新引用及ACL改变事务、事件传播仍缺。488项回归不等于真实企业验收。


## I016 实现审查补记

修复预先 invalidate 后尚未写完的旧body捕获窗口：持久 source_writes token 覆盖四个标准存储 await，capture/begin/publish及图片/sidecar依赖均检查。每个writer独立，成功结束再次推进epoch；取消/传输未知不自动清除，避免后台继续写入却已允许发布。498项局部回归通过。残留 uncertain 的 verified recovery 尚缺，不能把拒绝错误发布等同可用性/恢复完成；外部与跨主机 writer、目录/ACL变化、四FLOW/Wiki依然待验收。


## I017 恢复与证据审查

发现仅清除uncertain会允许迟到写覆盖新索引，故定义可信后端terminal/fencing而非用户proof/TTL/hash稳定，并要求精确token集合及epoch CAS。现有API能力不足，恢复代码未落地，新增验收保持NOT_RUN。远端运行/健康/hash匹配证据独立记录，历史model_verified不作为当前推理质量证明。主文§2.2实施状态更新至I016，避免仅追加历史补记而留下过期当前摘要。


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


## I027–I030：配置、输入准备与实际构建边界复核

I027实现知识域根与单向导出配置，I028/I029接入W2/W1准备入口；均不能把prepared当ready。W1暂限单Markdown，W2使用已有Markdown/目录；PDF/Office物化、域门禁执行器、抽取/归一/关系与统一发布仍未接通。当前查询要求来源可读、目标可写；查询历史计划不是获准跨域执行。

I030修复上传先整块读入再校验大小的问题，并修复已绑定幂等键replace先改变原文再冲突的问题。新增GET计划查询供客户端取回已保存计划，SDK与前端生成客户端同契约。692项局部PASS；实际AclManager在I027用例验证，I030 HTTP权限为替身，不构成生产认证验证。shared临时下载磁盘配额、首次并发物化原子事务、持久任务状态和导出提交fence仍需完成。初始失败报告保留，旧SDK五项基线失败未消除。


## CR-011 / v0.10：当前规格和未达成目标统一

修正前文过期状态、namespace SDK命名、Wiki路由当前prepared与最终job响应混用、W2当前不接受update_policy、key长度和400/412/413语义差异，显式保留旧来源URI/上传布局与replacement CAS演进。补33项任务的依赖与完成条件、15Feature当前状态和TC-16交叉断言，全部完整验收NOT_RUN。

明确首版单协调者本机SQLite/WAL、多worker不直接各建权威库；多主HA需共享事务repository与实际fencing/切换验证。把根layout可见性、物理GC、未确认writer恢复、真实BM25/meta、图片17+引用、Wiki可信catalog/规则coverage/增量证据和生产gold列入阻断任务。当前没有33项完成或任何Feature PASS的证据；本轮是规格修订，不是生产验收。
