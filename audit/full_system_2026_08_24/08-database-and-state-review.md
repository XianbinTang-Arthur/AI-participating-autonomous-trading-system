# 08 数据库、事务与状态审查

## FS-008 — 连接池上限与 PostgreSQL 容量不一致

- 严重度：P1；置信度：高；类别：capacity / availability
- 状态：VERIFIED（静态容量）；运行时峰值未压测
- 位置：`aats/storage/session.py:209-258`；`deploy/wsl2-dev/docker-compose.yml:52-58`
- 证据：每个主 runtime pool 为 15 + overflow 45 = 60；四进程理论 240，已超过 PostgreSQL `max_connections=200`。RDP 5+10、governance、live query 等额外 pools 未计入。源码注释仍写“PG 200 充裕”。
- Phase 2 现行裁定：**P2 / DOWNGRADED**。补算稳态理论上限约 317、瞬时 321；但 overflow 按需创建，按角色估计可信生产峰值约 142–160，当前模拟采样 40。首轮证明了缺少全局预算，却未证明可信峰值必然超过普通连接上限约 197；仍需全 live 拓扑容量/故障压测。完整计算见 `17-p1-adversarial-verification.md`。
- 运行采样：当前模拟栈只读采样为 40 个连接；这不代表峰值安全。
- 触发：Gateway fan-out、慢查询/idle transaction、对账和 RDP 并发峰值。
- 后果：pool timeout 或 PostgreSQL 拒绝连接，控制面和 execution 可能同时失效；`work_mem=64MB` 还放大内存压力。
- 建议：做全进程连接预算，Gateway 专用较大池、其他 slice 小池；引入 PgBouncer 或严格并发门；压测并监控 active/idle/queue wait/rejected。连接预算之和必须留管理与恢复余量。

## FS-009 — schema 由启动时 create_all 与手工迁移共同管理

- 严重度：P1；置信度：高；类别：schema governance / deploy safety
- 状态：原始 finding VERIFIED；Phase 3E `PARTIALLY REMEDIATED / CLONE MANIFEST & ROLLBACK OPEN`
- 位置：`aats/storage/session.py:261-290`；`aats/data_platform/db.py:53-63`；`rdp_models.py`；`migrations/_batch_b.py`
- 证据：四个主进程默认都会 `Base.metadata.create_all()` 后再应用根迁移；DDL 前没有覆盖 create_all 的全局 migration lock。RDP `run_migrations()` 只做 schema + ORM create_all；Batch B 的 ALTER、VIEW、CHECK、精度扩展和 rollback 通过独立脚本手工执行。Gateway 遇到失败仍继续。
- 触发：新环境、并行进程冷启动、模型变更、漏跑 Batch B、旧库升级。
- 后果：schema 看似有表但缺约束/视图/ALTER；并行 DDL 竞态；应用版本与数据库版本无法单值证明。
- 建议：建立单一版本化 migration tool/ledger；部署前独占 migrate job，应用只校验版本且禁止 runtime DDL；前滚/回滚在克隆库演练。RDP Batch B 纳入相同 ledger。

### Phase 3E 当前工作区补充

上述证据保留原始基线。当前工作区已实施：部署期一次性 root+RDP schema job；root 和 RDP 分别使用 exact version/checksum ledger；RDP 13 个 Batch B stage 有 advisory lock、canonical predecessor、checksum、同事务 DDL+ledger 和 rollback-suffix contract；四个 managed profile 禁止 runtime DDL；Gateway/daemon 启动只读校验且 Gateway 失败不 ready。

尚未在真 Postgres 执行 fresh/upgrade/partial failure/rollback，也未比较完整 schema manifest 或证明 app+schema 一致回退，故不能从单元测试标为 CLOSED。权威证据见 `25`。

## 状态一致性评价

- 订单/成交：PostgreSQL 是核心事实源，outbox 支持事务后传播；恢复对模糊 command fail-closed。
- 组合：snapshot 是加速投影，不是唯一事实；fill/ledger 可重建。事务内写 fill outcome、snapshot、outbox 是强控制。
- Redis：kill switch、portfolio/account/obligation/stream snapshot 是跨进程热副本；必须携带 scope/version/as_of，拒绝旧事件覆盖新值。已审 cache 有部分 stale guard。
- NATS：传播层不能替代 DB。命令与 observer 分 stream 是正确方向，但 topic 级恢复证明未完成。
- RDP：governance active set 应为参数真相源；文档/artifact 只能作为证据，不能证明当前激活状态。

## 事务与查询风险

- 同步 SQLAlchemy 被 async API 和高并发 ThreadPool fan-out 调用，源码历史注释已记录 idle-in-transaction 与 137 秒查询；60 秒数据库 timeout 是安全网，不是根治。
- 数据库 context manager 多数正确 commit/rollback/close；但多个 repository/service 组合操作是否共享同一事务需逐用例证明。
- ORM `create_all` 不会可靠处理列删除、类型变更、约束和 view；表数一致不等于 schema 一致。
- 当前未检查真实 live 数据完整性、索引命中、bloat、replication、backup 或 restore；均为 UNKNOWN。
