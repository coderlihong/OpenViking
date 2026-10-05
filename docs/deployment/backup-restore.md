# 联合备份与隔离恢复（受控本地能力）

2026-10-05 实现位于 `openviking.indexing.backup_restore`，属于 operator-local 能力，没有公开 HTTP/RPC 的强制恢复接口。`KB-008` 仍为 PARTIAL。

`export_bundle` 只支持 `ControlledLocalWriteBackend` 和离线本地 vector/ACL 目录。operator 必须停止协调者和所有身份、ACL、向量、模型配置写入，并传入覆盖这些组件的可信 `quiescence` context factory。活协调者持有的 lifetime lock 会阻止导出；导出额外持有 registry 写事务和受控后端 operation journal 写事务，禁止源 writer 在快照过程中变更。通用 AGFS/远程 AGFS 没有一致快照适配器时直接拒绝。

bundle 包含：

- registry 的事务一致 SQLite 快照，包括 namespace/export、Wiki schema/catalog/build plan、发布 head/receipt、source fence 和持久 jobs；
- 受控 backend 的持久 operation journal/identity、正文树和私有 immutable blobs；所有当前受保护 blob 引用均核对实际字节 hash，包含 Wiki draft/extraction 引用；
- 当前身份配置 JSON、模型配置 JSON、本地向量和 ACL 存储目录；
- 绑定完整路径、大小、SHA-256、backend identity 和必需恢复验证项的 manifest。

存在未知 source writer、未清空 parse/write slot、backend pending operation、不可读/损坏引用或缺失依赖时拒绝导出。未物化 D1/W1 上传 job 依赖临时原件，当前 bundle 不支持恢复该输入，因而同样拒绝。`viking://upload` 对应的 shared 临时原件排除在永久备份之外，本地上传临时目录也不是备份输入。它们仍由现有 TTL 流程清理，导出本身不删除原件。

导出、恢复均有文件数量和字节配额；单个 identity/model JSON 上限 1 MiB，manifest 上限 32 MiB，单个组件 inventory 硬上限 100,000 个文件/10 GiB。文件按流计算 digest，拒绝 symlink、别名和特殊文件。复制前预估及复制后校验配额；中途失败删除 staging 目录。发布前 fsync 文件和目录，再原子切换目标目录。operator 要求目标父目录由自己管理，且目标不存在。

`restore_bundle` 要求来自可信独立渠道的 `expected_manifest_sha256`，拒绝额外文件、缺文件、坏 hash、manifest 降低验证要求及未解决 writer。恢复只输出 `registry.quarantined.sqlite` 和 `RESTORE_PENDING.json`，返回 `ready=False`。同目录 marker 存在时，`PublicationStore` 和 `RegistryAuthority` 启动明确失败，防止绕过隔离直接使用恢复后的发布 head。

当前没有自动删除 marker、释放 registry 或启动服务的步骤。必须先用实际向量引擎/ACL、当前身份、模型配置以及所有发布 receipt 进行验证，并获得恢复后授权和独立 gold 一致性的证据。局部测试对真实 SQLite、受控文件后端和独立源 writer 验证了字节一致性和停写排斥；vector/ACL/identity 配置使用模拟 fixture，尚未证明生产向量引擎恢复或企业灾备验收。

下一步需补通用 AGFS/vendor 快照能力、真实身份/ACL/vector engine restore validation、独立 gold 的前后对照、Office/模型 token/临时磁盘配额和故障恢复演练。本轮隔离恢复不会提前标记这些要求达成。
