# OpenViking e2etest 迁移复盘（2026-10-22）

## 一、迁移概况

| 项 | 数值 |
|----|------|
| 源 | 本地 `127.0.0.1:1933`（账户 e2etest，MAC） |
| 目标 | `10.147.254.3:1933`（t-wanghai，`~/viking/e2e`，Linux） |
| 数据量 | 389,518 向量 / 120,141 jpg / 80,899 md / 词法 228,607 行 |
| 导出包 | 74 个（1 个全量 + 73 个 menus 叶子），拆分后 146 个（md/images 各 73） |
| 包总大小 | ~5GB 原始导出 + 4.4GB 拆分 |
| 传输 | VPN ~1.5MB/s，全程 gzip -1 管道 |
| 耗时 | 11:00 侦察 → 16:22 数据完成 → 18:17 前端修完（含修复约 7.5h） |
| 结果 | 372,410 向量 / 120,141 jpg（与本地严格一致）/ 词法 225,024 / 磁盘存在文档 100% 可读 |

代码补丁（全部在 gitlab `deploy/wolf-viking`）：
- `852b1b82` 批量子树记录获取 + dense 导出内存（25GB→3.2GB）
- `4dd147da` 分组扫描上限 25k 行
- `abf5f9b5` 归档预读流水线 + jpg STORED + 文本 level 1
- `d1b88802` restore 批量 upsert_many + bulk_ingest

---

## 二、遇到的问题（按性质分类）

### A. 性能问题（导出/恢复太慢，原始工具链不具备大租户能力）
1. **逐 URI 向量查询**：每个文件一次 `Eq("uri")` 查询，20 万次 → 改为 PathScope 子树批量查询（~460 次），10-20x。
2. **dense 内存爆炸**：Python `list[float]` 全租户约 25GB（首次备份被 OOM kill）→ `array('f')` 3.2GB + 逐批释放。
3. **单次全树抓取内存尖峰**：整个用例树（~10 万行）一次抓取 7-8GB → 分组扫描上限 25k 行。
4. **归档阶段串行**：逐文件 read→deflate，无流水线；jpg 也走 deflate level 6（纯浪费）→ 预读流水线 + ZIP_STORED + 文本 level 1（数据无差异，端到端收益有限，因瓶颈在别处）。
5. **restore 逐条 upsert**：~100 条/秒（6.3 万条写 9m39s）→ `upsert_many` 500/批 + `begin/end_bulk_ingest`（延迟后台索引重建）。
6. **单包导出病态慢**：menus 单包 15MB/min（预估 4-6h）→ 按 73 个子目录并行导出（12 并发，~50 包/h）。
7. **CLI 默认 60s 超时** → 配置 timeout 21600。
8. **VPN 带宽 1.5MB/s**：文本包 gzip -1 管道传输，双方 sha256 校验。

### B. 格式/语义问题（OVPack 的坑）
1. **两种包形状**：export（`ov import`）vs backup（`ov restore`），混用直接失败。
2. **`import --on-conflict overwrite` 会递归删除目标根目录**：md/images 两阶段拆分时，第二阶段会把第一阶段的数据删掉 → 必须用 backup 形状 + `ov restore`（合并语义，从不删除）。
3. **`ov import pack-user-md.ovpack viking://user/` 被 ACL 拒绝**（要求 `viking://~/...` 或显式 `viking://user/{user_id}/...`）→ 改用 backup 形状 restore。
4. **backup 包的硬约束**：`scopes` 必须声明；scope 根目录 + 祖先目录条目必须都在包内；根条目 path="" 与 `<base>/files/` zip 成员的特殊对应关系。
5. **空包**（无 dense/无文件）恢复会失败或产生空记录 → 跳过。
6. **PUBLIC_SCOPES 限制**：OVPack 只覆盖 `resources`、`user`；`agent` scope 天然不在包内 → 需 API/文件级补齐。
7. **export 形状包恢复后路径可能错位**：fault_mechanism 包恢复到了 `resources/` 而非 `resources/raw/`（导入目标解析与向量 URI 不一致）→ 需 `ov mv` 修复并验证。

### C. 账本缺失（最严重，导致"导入完成 ≠ 可用"）
OVPack 只搬**文件树 + 向量快照**，不搬 **publication 账本 + 内容 blob**，restore 后又没有后置补齐步骤：

1. **read/get 全面 500**：head manifest 缺 `content`/`semantic_contents` 引用 → `Published source has no immutable content`。
2. **read/get 全面 NOT_FOUND**：restore 只写文件树，没写内容寻址 blob（`_system/index_blobs/<sha>.bin`）→ 投影读取找不到 blob。
3. **BM25 词法层 0 覆盖**：`publication_lexical` 只有早期测试的 3,426 行 → 混合检索退化为纯向量。
4. **level 0/1（目录摘要层）检索不到**：包里有 308 条 L0 + 154 条 L1/包，restore 后检索为空（原因未完全定位，restore 路径的 payload/账本差异）。
5. **内容版本错位 12 条**：账本 ref sha 指向旧版本，磁盘是另一版本 → read NOT_FOUND（改 ref 修复）。

修复手段（都是后置补偿，非工具原生）：
- 重建 head manifest（join sources/semantic_sources）
- 从磁盘重建 144,385 个 blob（2.53GB）
- 从源库移植 publication 账本（lexical 225,533 行 + builds/sources/metadata/receipts/desired + 对齐 head snapshot）
- ref 对账（ref sha vs 磁盘 sha）

### D. 部署/构建问题
1. `Makefile` 的 `PYTHON?=python3` 指向 linuxbrew 3.14（缺 setuptools）→ 直接用 `.venv/bin/python setup.py build_ext --inplace`。
2. `aws-lc-sys` 拒绝 gcc 10.2 → 用 clang 22 构建。
3. `apscheduler 4.0.0a6` 预发布版启动崩溃 → 锁 `apscheduler==3.11.2`。
4. 版本字符串不一致（0.1.dev2430 vs 0.4.15.dev415）是 setuptools_scm 无 tag 的回退，代码 commit 一致即可。
5. **Web Studio 前端未构建**：`/studio` 404；直接用本地 dist 也错（本地构建 `base=/`，资源引用 `/assets/*`，服务器只挂 `/studio/*` → 全 404）。正确做法（与 Dockerfile 一致）：服务器上 `npm ci && npm run build -- --base=/studio/`。
6. `temp_upload.shared_max_size_bytes` 需临时调大（2GB→12GB），完成后还原。
7. 服务优雅关闭偶发挂起（SIGTERM 后 8 分钟无响应）→ WAL/LevelDB 崩溃安全，SIGKILL 安全。
8. macOS 无 `setsid`。

### E. 基线数据质量问题（不是迁移造成的，但会误导验收）
1. **203 条历史死链**（0.38%）：vikingdb/词法里有旧命名路径（`raw/fault_mechanism/*`、`fault_mechanism_v2/*/故障机理/发动机冒白烟/发动机冒白烟.md`），磁盘文件早已重命名/删除。**本地基线同样存在**。
2. **内容版本错位**：12 条账本 ref 指向旧版本（与磁盘不一致）。
3. 本地 find 排序不返回这些死链，服务器因词法融合权重不同会推出几条 → 容易被误判为迁移缺陷。

### F. 协作/运维问题
1. 共享服务器：1935/1936 是其他人的服务 → 全程不触碰；端口必须与本地对齐（1933）。
2. 重启窗口误杀进行中的导出：runner 日志出现 60 个 FAIL（实际是 kill 导致的瞬时失败，非数据问题）。
3. 备份/回滚点管理：publication.db 三份备份，验收后保留 `pre-manifest-fix`。

---

## 三、什么阻碍了"一次性导入导出成功"

1. **OVPack 的迁移语义不完整（根本原因）**
   它只是"文件树 + 向量快照"的快照，不是"知识库完整状态"。OpenViking 的**读取、BM25、目录摘要、内容投影**都依赖 publication 账本（builds/sources/lexical/snapshots/drafts）和内容 blob（index_blobs）。恢复后是"半成品"：dense 检索可用，但 read/BM25/level0-1 全缺。这是设计缺口，不是操作失误。

2. **导入导出没有为"大租户全量"设计**
   原始实现逐 URI 查询、逐条 upsert、全量内存、单线程归档 → 39 万向量/15 万文件体量下会 OOM 或耗时数小时到一天。必须打批量/内存/并行补丁。

3. **import 与 restore 的删除语义差异** + md/images 两阶段拆包需求 → 必须选 backup 形状，选错会互相覆盖。

4. **服务部署链不完整**：前端未构建、依赖版本冲突、编译器不兼容、上传限额 → 部署本身就要 1-2 小时排障。

5. **基线数据自身有质量问题**（203 死链 + 12 版本错位），且没有现成的"数据体检"工具 → 迁移会继承问题，且难以区分"迁移缺失"与"基线残留"。

---

## 四、下次最佳实践（Checklist）

### 阶段 0：准备（0.5h）
- [ ] 明确范围：哪些 scope（resources / user / agent / memory）、是否要词法层、是否要 level 0/1 摘要层
- [ ] **基线体检**：`ov status` + 文件计数 + 全量死链扫描（vikingdb uri vs 磁盘）+ 词法行数，记录基线指标
- [ ] 目标环境：端口对齐、账户/用户、config diff 逐项确认、`temp_upload` 限额临时调大
- [ ] 代码基线：确认目标服务器与源端同一 commit（tag 可不同）

### 阶段 1：导出（1-2h，并行）
- [ ] 使用带批量优化的分支（852b1b82/4dd147da/abf5f9b5）
- [ ] **按子树并行导出**（每个叶子一个包），不要单包全量
- [ ] `--include-vectors`（快照恢复比 recompute 快 10-20x；recompute 37 万向量≈9-12h）
- [ ] CLI timeout 设大（21600）
- [ ] 每包记录 sha256 + 向量数 + 文件数（对账用）
- [ ] 两阶段需求（md 先行）→ 拆分器输出 **backup 形状**（scopes 声明 + 根/祖先目录条目齐全）

### 阶段 2：导入（1-2h）
- [ ] **优先 backup 形状 + `ov restore`**（合并语义，可 md→images 两阶段）
- [ ] export 形状 import 只能导入**全新不存在的目标**（否则会删根）
- [ ] 逐包恢复 + 每包对账向量增量（预期 = 包 dense 数 − 跨代折叠）
- [ ] md 全部完成并验证后再恢复 images

### 阶段 3：补齐账本与内容（本次缺失的关键步骤，1-2h）
- [ ] **词法层**：从源库导出 `publication_lexical`/`scope`/`sources`/`semantic_sources`/`desired` + 对齐的 head snapshot，装载后触发 `rebuild_lexical_index`
- [ ] **内容 blob**：遍历恢复的文件树生成 `_system/index_blobs/<sha>.bin`
- [ ] **head manifest**：按 generation join sources/semantic_sources 补 `content`/`semantic_contents`
- [ ] **ref 对账**：manifest ref sha vs 磁盘 sha，不一致的改 ref（不要 re-embed）
- [ ] **非 PUBLIC_SCOPES 数据**（agent scope 等）：API 写入（`.abstract.md` 类被禁写 → 直接文件系统写入即可被 AGFS 读取）
- [ ] 词法 prune 会删除不在 desired/head 中的旧代 → 确认 head 快照引用齐全

### 阶段 4：服务部署（1h）
- [ ] Python 扩展：`.venv/bin/python setup.py build_ext --inplace`（绕开 Makefile 的 python3）
- [ ] 依赖锁定：`apscheduler==3.11.2`；编译器用 clang（aws-lc-sys 拒绝老 gcc）
- [ ] **前端必须构建**：`npm ci && npm run build -- --base=/studio/`，产物放 `openviking/web_studio/dist`
- [ ] 临时调大 `temp_upload.shared_max_size_bytes`，完成后还原
- [ ] 启动：setsid + nohup；验证 `/health`、`ov health`、`ov status`

### 阶段 5：验收（0.5h）
- [ ] 计数对账：向量数、jpg/md 文件数、词法行数 vs 基线
- [ ] 检索对照：本地 vs 远端 3-5 组查询 top-k 一致率
- [ ] read/get 抽样 100+ 文件：0 个 500/NOT_FOUND（排除已知基线死链）
- [ ] `ov get` 下载 sha256 == manifest content ref
- [ ] Web Studio：`/studio` + 全部 assets 200
- [ ] 死链清单：区分"基线自带"与"迁移缺失"
- [ ] 回滚点保留

---

## 五、建议的产品/工具改进

1. **ovpack 支持完整状态迁移**：新增 `ov migrate`（或 `restore --with-publication`），一次性搬运 文件树 + 向量 + publication 账本 + 内容 blob + head 快照，并对齐 generation/sha。
2. **restore 后置自动补齐**：blob 重建、manifest ref join、lexical 镜像重建、ref 对账，做成 restore 的内置步骤（幂等）。
3. **导出/导入批量化与并行**：子树批量查询、`upsert_many`、并行导出、断点续传（本次补丁应合入主线）。
4. **`ov migrate verify`**：全量死链扫描 + 计数对账 + 检索一致性抽样，输出可读报告。
5. **部署脚本自动构建 web-studio**（`--base=/studio/`），避免手工遗漏。
6. **文档明确**：import vs restore 的删除语义、PUBLIC_SCOPES 边界、level 0/1 与词法层的迁移覆盖范围。
7. **基线体检工具**：`ov doctor`（孤儿向量、死链、版本错位、lexical 覆盖缺口），迁移前先跑一遍。

---

## 六、本次遗留事项（可选项）

1. 清理 203 条基线历史死链（让服务器比基线更干净，但与本地不一致）。
2. 重建 level 0/1 摘要层（服务器 VLM qwen3.8-27b 可用，重新生成目录摘要，内容与本地原文会有差异）。
3. 将本次 5 个性能补丁 + 账本补齐脚本合入主线（gitlab `deploy/wolf-viking` 已有 4 个，账本脚本在 `tmp/migration/lexical_transplant/`）。

---

## 七、离线环境（无 embedding 服务）迁移方案

**前提**：目标机无法访问 embedding/VLM 服务，只能搬数据。

### 方案 A：整库物理拷贝（已验证 ✅ 100% 完整）

```bash
# 源机（停服后）
tar czf ov-data.tar.gz -C /path/to/install data/
# 目标机（同版本 OpenViking）
tar xzf ov-data.tar.gz -C /path/to/install/
rm -f /path/to/install/data/.openviking.pid        # 清掉源机的进程锁
# 改配置：server.port、embedding.chunking.publication_path
/path/to/venv/bin/openviking-server --config ov.conf
```
- 实测（本地完整数据副本 → 独立实例）：L0/L1/L2 检索全部正常、read 正常、向量数一致
- 体积：约 60GB（可用 `ov` 的 GC 先清理 history 减小）
- 优点：**100% 完整**（index 文件一起带走）；缺点：体积大

### 方案 B：ovpack + 索引文件（工具已实现，受引擎缺陷限制）

```bash
# 源机
ov backup --include-vectors --include-index /tmp/openviking-backup.ovpack

# 目标机（同版本，空库）
# 1) 启动服务 → 导入数据（文件树/向量/词法/blob/账本）
ov restore /tmp/openviking-backup.ovpack --on-conflict overwrite --vector-mode auto
# 2) 停服
kill $(cat data/.openviking.pid)
# 3) 应用随包携带的向量索引
python -m openviking.storage.ovpack.apply_index /tmp/openviking-backup.ovpack \
    --data-dir /path/to/install/data --force
# 4) 启动服务
openviking-server --config ov.conf
```
- 包体积：数据 ~3.8GB + 索引 ~0.8-2.4GB（压缩后）
- **实测限制**：L2 内容检索 / read / 词法全部正常；**目录 L0/L1 检索在加载源索引后仍不可用**（引擎的 index↔store 一致性缺陷，见下）

### L0/L1 检索丢失的真正根因（已修复，commit 43c26b6f）

**不是引擎缺陷，是 ovpack restore 丢了两个字段**：

- restore 时 `_build_vector_snapshot_payload` 移除了 `index_generation` / `index_plan_id`（曾以为"restore 不是 managed generation write"）。
- 但检索后的 `recheck_visibility` 用 `row["index_generation"]` 与 head manifest 比对：**generation 为空且 manifest 认识该 URI 的行一律丢弃**。
- 而 bundle 迁移了源 head 快照 → 目标 manifest 认识所有迁移 URI 且 generation 非空 → **所有恢复的行都被可见性检查过滤**（payload/向量都正常，扫描可见、检索不可见）。
- 目录 L0/L1 受影响最明显，因为文件级 L2 命中常来自词法旁路。

**修复**：保留 `index_generation` 与 `index_plan_id`（后者是 managed-write 的 `(account, uri, plan)` 校验所需，bundle 已迁移 builds 行）。

**验证**（小批量 `menus/797105495448853`，903 md）：导入后 L0 0.396/0.384、L1 0.591/0.449、L2 正常、重启后全部保留；本地与远端服务器均通过。

**结论**：离线部署**不需要**整库拷贝，也不需要 embedding 服务——纯 ovpack 数据迁移即可完整恢复 L0/L1/L2/词法/读取。

