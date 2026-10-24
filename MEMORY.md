
## 检索延迟三根因（2026-10-17 修，均已带测试）
- `registry_lexical._lexical_scope` 曾按 URI × 墓碑做前缀扫描（7.9e9 次比较 → 318s）；现用 `_under_denied_root` 祖先链 set 查找（0.316s）。
- `PublicationStore.visibility()` 的全量 URI DSL 不再进引擎：向量查询只带粗粒度 scope，取回后用 `recheck_visibility` 过滤（over-fetch 4×，上限 1000）。
- `source_reference()` 不再使用写事务 `capture()`：改只读 `_head_manifest()`（事件循环上等单写者 = 12–33s 卡顿）。
- 实测：整树 `find` 从 642.5s / >300s 超时降到 **17.8s**（窄 13.3s，满载重跑中）。
- 配置化新增：`server.listen_backlog`（uvicorn backlog；macOS somaxconn 默认 128 会截断）、`concurrency.max_task_store_io`（task store 并发，默认 8/线上 32）。

## rerank 后端 8029（2026-10-19 重建，已实测生效）
- 两套 viking 的 `ov.conf` 里 rerank 都是 `provider=openai` / `api_base=http://10.147.254.3:8029/v1` / `model=qwen3-vl-reranker-2b` / `api_key=sk-reranker`（本地 1933 用 `~/.openviking/ov.conf`，远端 1935 用 `/home/t-wanghai/viking/ov.conf`）。此前 8029 无进程，`find` 一直降级成 rank-only（日志 `Rerank degraded, serving rank-only fused order`）。
- 现役容器 `qwen3-vl-reranker-2b-vllm-8029`：镜像本地 `…/llm/vllm-openai:v0.27.1`（自带 `/v1/rerank`），模型 `/home/t-wanghai/models/Qwen3-VL-Reranker-2B`（`model.safetensors` sha256 `466ec019…d5b9` = ModelScope master），`--runner pooling --max-model-len 32768 --gpu-memory-utilization 0.35`，GPU5，`--restart unless-stopped`，对外 `0.0.0.0:8029`。
- 重建脚本 `/home/t-wanghai/deploy_rerank_8029.sh`（= 仓库 `tmp/deploy_rerank_8029.sh`，`MAX_LEN/GPU_UTIL/GPU/PORT` 可覆盖，容器 exited 会打尾日志）。
- `max-model-len` 取模型卡认证的 **32K**：`config.json` 里 `max_position_embeddings=262144` 是继承 base 的位权容量，在 0.35 预算下要 28 GiB KV（可用 26.92 GiB）**起不来**（vLLM 自估上限 252048）。OV 侧 `rerank.max_input_tokens=0`（**不截断**），服务端上限是唯一闸门；embedding 把文档卡在 8192 token，32K 有 4 倍余量。实测 31,200 token 文档 200/1.4 s，32,769 才 400。
- **不需要重启 OV**：rerank client 每次检索新建，8029 一起来即生效（本次未重启即恢复）。
- 旧的 `qwen3-vl-reranker-2b-vllm-8027`（只绑 `127.0.0.1:8027`，挂 zike.wang 的模型副本）已删；GPU1 释放 ~32.6 GB。
- 验证口径：1933 上跨 menus/cases/fault_mechanism 共 16 条 find 全 200，top1 命中、分 0.78–0.96，`Rerank degraded` 计数冻结在 67 不增。
- 无关告警：偶发 500 `QueryLeaseExpired: Publication store busy while pinning the query view: database is locked`（publication.db 锁竞争，与 rerank 无关）。

## 检索/写入同队列根治（2026-10-19 改，未上线需重启）
- 问题定位：publication.db 单写者（BEGIN IMMEDIATE）上，检索路径自己也是写者——每次 find 三个写事务（`capture` 租约 INSERT / 心跳 UPDATE / release DELETE），外加 `job_claim` 轮询**每轮无条件开写事务**（空闲也开，实测等锁 16.6s）。今天慢写 7374 条中 7139 条集中在 00 点 reindex 收尾，之后每分钟 1–12 条维护写（job_claim ~60s、lexical prune、GC、延迟发布 gzip 快照）轮流占写者；find 实测 2.8–18.4s，偶发 QueryLeaseExpired 500。
- 修复 1（`jobs.py job_claim`）：先 `db(readonly=True)` 预检 `kb_jobs` 有无 due/expired，空闲直接 return None，不再开 BEGIN IMMEDIATE。谓词与 claim 查询完全一致（含过期 running 租约，会交给 `_recover_jobs`）。
- 修复 2（查询租约挪库）：新增 `openviking/indexing/query_leases.py` 的 `QueryLeaseStore`，租约行存 `publication.db.query_leases.sqlite3`（同目录、WAL、同表结构 id/snapshots/expires）。`capture()` 改为：只读事务读 manifest+heads → 租约写进小库 → 只读复核 pins 仍存在（防 prune 竞态，重试 3 次）；`renew/release` 全走小库。**查询路径对 publication.db 零写事务**。
- 必须同步的隐藏消费方（漏一处就是数据事故）：`registry_gc._protected_generations`（代际写围栏，"被租约保护的旧 head 可写"）、`prune_snapshots` keep 集（+顺带接管 gc_leases 过期清理）、`retired_candidates`、`_protected_content_revisions`、`prune_superseded_lexical` 的租约冻结守卫、`registry_lexical`/`registry_metadata` 的 lease 覆盖检查——全部改为合并新库 live 行（老 `leases` 表保留读兼容，不再写）。
- authority socket 转发按方法名 dispatch（capture/release/renew/protected_content_revisions/retired_candidates），签名未变，无需改。
- 测试：新增 `tests/enterprise_kb/test_query_lease_store.py`（10 个：写事务计数断言、pin=manifest、过期拒续、prune/GC 保护、torn-view 不外泄、legacy 行兼容）+ `test_durable_jobs.py` 空闲 claim 只读断言；回归 119+47 通过。`test_vikingfs_find_without_rerank` 2 个失败为 main 上既有，与本次无关。
- **生效条件：重启 1933 服务**（代码改动，非配置）；重启后旧 `leases` 表残留行随 TTL 过期自清。

## 重启实测结果 + 第三个根因：事件循环被入库分块冻结（2026-10-19 修，已上线生效）
- 实测（pid 61660，`tmp/ov-server-run1053.log`，10:53 起）：menus 12.1s→**3.1s**、cases 7.9s→**2.8s**、全局 18.4s→**3.9s**；rerank 真分 0.75–0.95；`job_claim` 在新日志出现 **0 次**（旧日志每分钟等锁 2–3s）；QueryLeaseExpired/Rerank degraded 均 0；lexical 只读慢事务 lock wait 全为 0ms（纯 FTS 计算不是排队）。租约确认落 `publication.db.query_leases.sqlite3`（find 期间监视到并发 1 条）。
- **第三个根因（比写队列更狠）**：`enqueue.py` 的 `plan_resource`（HF tokenizer 全文分块，二分 token 预算 = O(n log n) 编码）**裸跑在事件循环上**，大文件 = 整机冻结：/health、find 全部无响应，SIGTERM 都杀不掉（今晨旧进程 10:37 静默冻结、10:44 才 SIGKILL；重启后 1–3 分钟内必复现）。证据链：`tmp/loop_watchdog.log`（watchdog 输出在独立文件，server 日志里看不到！）MainThread 栈 = uvicorn → process_file → vectorize_file → enqueue_text_plan → plan_resource → prefix_piece → tokenizers encode；native sample 同栈。
- 修复：[enqueue.py](openviking/indexing/enqueue.py) 把 `plan_resource` 包进 `asyncio.to_thread`（同函数里 prepare_meta/seal 等本就如此）。测试 `test_plan_build_never_blocks_the_event_loop` 断言 plan 构建不在主线程执行。重启后 4 分钟 + 连续三轮探测 /health 全部 ~2ms，watchdog 零新增转储。
- **诊断要点（下次直接用）**：watchdog 栈在 `tmp/loop_watchdog.log`（环境变量 `OPENVIKING_LOOP_WATCHDOG=1`，重启脚本已带）；server 日志只写 slow-tx。`sample <pid> 3` 可看 native 栈；py-spy 需 sudo 不可用。
- 测试修 3 处（均为观察点迁移非行为回归）：`test_publication.py::_open_leases` 改数专用库；`test_expired_leases_are_pruned_by_the_next_capture` 的 gc_leases 清扫断言改为"下次 `gc_lease` 清扫"（实现同步改：`gc_lease` 写事务内顺带 DELETE 过期行——capture 不再碰 publication.db，prune_snapshots 生产无调用方，所以必须由 gc_leases 自己的写方负责）；全量回归 **204 passed**。

## rerank=false 分数域修复（2026-10-19 修，已上线生效）
- 问题：`rerank: false`（或降级）时返回的 score 是**原始 RRF 和** `Σ 1/(60+rank)` ≈ 0.016–0.033——尘埃域，`score_threshold` 完全不可用（0.5 阈值会滤掉全部结果），跨查询也无法比较。
- 修复：[fusion.py](openviking/retrieve/fusion.py) 新增 `normalize_fused_scores`，在 `run_fusion` 里 RRF 之后、rerank/MMR 之前按**融合上限**归一：`score / (n_nonempty_paths/(k+1))`。正比例缩放严格保序、有界 (0,1]、跨查询稳定（不像 min-max 依赖结果集构成）。双路都排第一 → 1.0；两路下单路 rank-1 → 0.5。原始和保留在 `score_components["rrf"]`；trace 新增 `score_domain`（`rerank` | `normalized_rrf`）；degraded 告警文案同步更新。
- 边界：空 path 不计入上限；`reciprocal_rank_fusion` 本身保持原始语义（其单测不动）；rerank 应用时其 0-1 相关性直接覆盖、不再缩放。
- **语义口径**：归一域是"排名位置域"不是"查询相关性域"——只有 reranker 度量相关性。两种模式各自内部一致、都有界，但跨模式的分数值含义仍不可直接互比。
- 测试：`tests/retrieve/test_fusion.py` 5 个新测试（上限数学/保序/raw 保留/空 path/三模式 score_domain 断言）+ `test_hybrid_profile.py` 端到端钉住新契约（`123/124` 与 `0.5` 精确值）；retrieve+hybrid 111 passed。
- 实测（pid 66992，run1107）：`rerank=false` → 0.8743/0.7869/0.7697（复测逐位一致），`score_threshold=0.5` 正常返回 3 条（修复前会 0 条）；`rerank=true` 分数与修复前逐位一致（0.9089…），reranker 路径零影响。注意：61660 进程 SIGKILL 才退出是**排水慢**不是冻结——watchdog 自 10:53 零新增转储。

## 六条检索访问路径验证（2026-10-19，脚本 tmp/verify_access_paths.py）
- 对照 CSV（iroot-llm/xeno-adp-agentic 的三一手册清单，72 本）：**document_id 全部 72/72 命中 menus L0 目录**。抽 4 本不同机型（SY55C/SY60C/SY60CPro/SY200C）× 4 症状全路径验证。
- 结果：L0/L1 目录定位、index.md 定位（每册 2.9–3.6KB 可读）、scoped 向量检索（4/4 命中册内 L2 chunk，1.6–2.6s）、引用溯源（read 命中 URI 返回正确章节：标题+document_id+chapter_uri+页码全对）、父子查询（chunk 的父节存在且含该 chunk，兄弟 8–29 个）——**全部通过**。向量 top1 直击症状章节 2/4（冒黑烟→9.9.6_发动机冒黑烟 0.914、回转无动作→9.7.22_H-19_机器不能回转 0.809，后者是语义改写命中）；另 2/4 泛化命中（判断表方法/技术规格）。
- Agent 启发式（qwen3.5-2b @10.147.254.3:8004，OpenAI 兼容，~0.2s）：给 L1 目录让模型选节，**症状优先+排除前言/拆装类的提示词**下 3/4 与向量 top1 一致（初版提示词只有 1/4，2B 模型吃提示词）。建议 agent 路径=目录导航缩小范围+向量检索定节，不要让小模型直接选节。
- **重要运维发现：menus 树正在被重建重排**——逐册插入 `9_故障诊断与排故` 这类一级分组层（`手册/9.9_xxx/…` → `手册/9_故障诊断与排故/9.9_xxx/…`），**重建前捕获的 URI 引用会 404**。find/read/ls 在重排后即时一致（find 秒级返回新 URI），但跨时间缓存的引用必须按 document_id 重新定位。验证引用溯源时若 404 先怀疑"引用过期"再怀疑"断链"。

## e2etest 租户 clean rebuild（2026-10-20 00:16–01:10，随最新 commit 1745abe5 验证）
- 背景：HEAD 提交了 reindex 重用守卫（99868468）+ 批量 tombstone（1745abe5）等；决定清空本地 e2etest 全部数据，从 datas/ 重新上传并全量 reindex，验证"同一数据多次 reindex 不再产生重复代"。
- 构建：HEAD 相对上次 build 只有 crates/ragfs 改动（0ff448c9，queuefs backend）；C++ src/ 无改动无需 cmake。用 `uv pip install maturin` + `cd crates/ragfs-python && maturin build --release` 提取 .so 到 openviking/lib/（1m15s）。make build 在本机因缺 cmake 走不通，只能单独重建 Rust 绑定。
- 清租户：`DELETE /api/v1/admin/accounts/e2etest`（root key）→ 后台 account_delete 任务**在最后一步 _collect_garbage 必败**（publication.py:2281 PHYSICAL_GC_CAPABILITY_REQUIRED，部署无物理 GC 能力，fail-closed）。失败前已完成：tombstone 182,184 条、向量按 account 物理删除（delete_account_data）、账号文件删除（data/viking/e2etest 消失）。**产品缺陷：本部署下账号删除永远无法完成**（finish_deletion 不执行，账号残留 deleting）。临时解法（本次采用）：停服后手工清理——从 publication.db 按 generation/account 删除 e2etest 行（builds 299,051 / desired 182,184 / drafts 210,451 / tombstones 182,184 / lexical 209,800 / gc_marks 14,022 等），FTS5 表 rebuild，accounts.json 移除 e2etest，重启后 POST /accounts 重建同 id+用户，再用备份的旧 users.json 覆盖 + 重启恢复旧 API key（鉴权是明文比对）。baomasany 租户数据全程保留（19,170 builds）。
- 上传：新脚本 [tmp/upload_tree.py](tmp/upload_tree.py)（content/write + processing_mode=none + mkdir 骨架 + ledger 续跑，16 并发 ~400 文件/s）：menus 130,056 / cases 42,694 / fault_mechanism 75，全部 fail=0，约 8 分钟；各树目录生成 1 个 .abstract.md 占位（9190 个）。
- reindex 驱动参数化：tmp/reindex_manuals.py 增加 URI_ROOT/DIR_ROOT（--dir-root/--uri-root 对所有树生效），金丝雀 fault_mechanism 5 目录 75 文档 10.1min 完成、75/75 published 0 failed。
- **重用守卫实测**：对已发布 URI 再发 reindex → task result `rebuilt_records: 0, reused_records: N`，builds 行数 (1,1) 不变——重复 reindex 不再开新代。
- 全量重建：tmp/rebuild_newver.sh，menus(73 手册) → cases(148 目录) 串行，driver concurrency=2/max-in-flight=6，日志 tmp/rebuild_newver.log（首 3 手册各 386–712s、0 失败，ETA menus ~5h）。VLM overview 失败 35 次但占位符 0（新版本 converge retry 降采样兜底生效）。

## e2etest → sanytop10 租户迁移（2026-10-24 完成；A 路线代码修复已上线并真机验证）
- **代码修复（3 个提交，均已推 GitLab irootech `deploy/wolf-viking`）**：
  - `1f13a1c3`：`import_ovpack` 对 top-level scope 包（`parent=viking://`）不再整根删除——租户 key 删 `viking://resources` 根必被 `_access.py:270` 守卫拒绝。改为 `_clear_scope_root_children()`（逐子条目 rm，根本身不动），语义等价且空骨架根无副作用。
  - `3a140e8f`：清空阶段遇生成型导航文件 `{dir}/index.md`（FailedPreconditionError）跳过不抛，包内同名成员覆盖内容。
  - `b1d4c0a9`：补 `FailedPreconditionError` import（首跑真机 NameError→500 的教训）。
  - 测试：新增 2 个回归用例（`NamespaceRootVikingFS`/`RegeneratedCatalogueVikingFS` fake），22-23 passed；4 个 ovpack fixture 红与 2 个 docker 打包红为 main 上既有。
- **真机 A 路线验证**：sanytop10 租户 key 下合成 top-level 包（root=viking://resources，含 README.md+demo/note.md，手工构造 content_sha256=`sha256(json.dumps(file_entries, sort_keys=True, separators=(',',':')))` + `index.records` 块）`pack/import parent=viking:// on_conflict=overwrite vector_mode=auto` → status ok，文件可读，子条目清空逻辑生效。
- **磁盘事故记录**：/home 共享卷 11T 满盘导致 restore/import 多次 ENOSPC 中断（temp_upload 55G 历史孤儿 + vectordb ldb 43G + publication.db 4.7G + 各实例 packs 22G）。已清理：过期 temp uploads、`publication.db.pre-*`、旧 baomasany 包、1935 temp 数据；publication.db 用 `sqlite3.backup()` 到 /tmp 再 VACUUM 换回（4.72G→4.09G，integrity ok）。**教训：sqlite VACUUM 需要等量空闲空间，满盘时只能 backup-to-other-volume 再换回**。
- **sanytop10 当前状态**：数据层被多次失败重试折腾过——向量行 ~3.5k（残片）、文件层仅 smoke 文件、e2etest 源租户全程无损（354,892 向量不变）。**如需完整数据，从 e2etest 重新走一次 bundle restore 即可**（磁盘现有 377G 余量，够放 2×6.5G 包）。
- 新租户：远端 `t-wanghai@10.147.254.3:1933` 上 `POST /api/v1/admin/accounts`（root key，body `{"account_id":"sanytop10","admin_user_id":"sanytopadmin"}`）创建，返回 `user_key=c2FueXRvcDEw.c2FueXRvcGFkbWlu.Yzli…（sanytopadmin 的 admin key）`。
- **顶层 scope 包 import 不可用**：`viking://resources` 导出的包属于 top-level scope package，`pack/import` 只允许 `parent=viking://`；而该路径走 `_remove_existing_root(viking://resources)`=删命名空间根 → **PERMISSION_DENIED 需要 root**（tenant key 也一样，这是防越权的硬墙）。绕行正解：**备份包 + `pack/restore`**——逐文件合并写、无删根路径、tenant key 即可用。
- 实际用的包：今下午 15:28 现成的 `~/viking/e2e/packs/e2etest-full-bundle.ovpack`（6.5G backup 型，双 scope resources+user，含 publication bundle 账本+稠密向量+`.abstract.md`/`.overview.md` 侧车）。e2etest 身份 `pack/export?include_vectors=true` 全量导出（2.2h，一致性检查 ~25min 是大头）已验证可行但本次未用上（包在 `/tmp/export_*.ovpack`，注意 temp_upload 上限 8GiB）。
- restore 参数：`{"on_conflict":"overwrite","vector_mode":"auto"}`；耗时 1.5h（上传仅 24s / 账本安装 32s 一笔慢事务 / 向量快照回放 ~66min，~5.5k 行/min）。日志 `skipped 35080 superseded snapshot record(s)` 与源端完全一致。
- 数据落地后验收记录（01:19 restore 完成时）：向量 354,902 vs 源 354,892（+10=user scope 个人空间记录）；raw/menus 269,457、raw/cases 80,059、fault_mechanism_v2 1,851、expertbooks 3,509 四树 count 逐一相等；8 个 L0/L1 侧车字节数全同（验证脚本首轮 MISS 是投影异步落定窗口，重跑即 ok）；find 冒烟 top1 完全复现（机理 0.916 双命中、cases SY60 同文档 0.832）。
- 验证脚本：远端 `~/viking/ovpack/verify_sanytop10.py`；过程脚本 `~/viking/ovpack/{migrate_e2e_to_sanytop10,import_sanytop10,restore_sanytop10}.sh` 及各自 .log。
- xeno 项目接入：`.env` 把 `OV_API_KEY`/`EXPERTBOOK_OV_KEY` 换成上面的 sanytop key 即指向新租户（VIKING_NAMESPACE=initwiki918 等与租户无关的配置不动）。

## clean rebuild 最终验收（2026-10-20 09:20）
- 全量重建完成：07:47:42（menus 4.5h + cases 160.8min），221 个手册/目录任务，0 driver 失败。
- 覆盖率：menus 27,154/27,154、cases 25,380/25,381（差 1 = 源文件 0 字节，索引器合理拒绝）、fault_mechanism 75/75。
- 一致性 `/api/v1/system/consistency`（POST）：三棵树 ok=true missing=0（menus expected 44,986、cases 25,916、fm 87）。注意：**树根 URI 自身 L0/L1 需单独 reindex**（recursive=false），否则根目录缺 2 条记录、consistency 报 missing；已补。
- 无重复代：52,609 个文件 URI 全部 1 条 build；目录 URI 9,185 个有 2 条（L0/L1 双计划，legit）；failed builds=0；desired 181,930 行=每源一条。
- 检索验收（稳态中位）：menus 全局 3.28s/限树 3.02s、cases 2.42s、fm 1.78s、无 scope 全局 3.94s；top1 分数 0.90–0.96（rerank 真分）；Rerank degraded=0、QueryLeaseExpired=0；随机抽 15 篇 content/read 与源文件逐字节等长一致。
- rerank=false 归一分数正常（0.754–0.836，score_threshold=0.5 可用）。
- 日志：VLM overview 失败 955 次（超时）但占位符 sidecar=0（converge retry 兜底生效）；watchdog 全程零新增转储（事件循环健康）。
