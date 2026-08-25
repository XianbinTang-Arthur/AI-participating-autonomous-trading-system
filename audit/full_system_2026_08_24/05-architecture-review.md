# 05 架构审查

> 状态边界：本文件主体保存 Phase 1/2 原始审查快照。Phase 3D 当前工作区已实现
> FS-006 的 task-exit supervisor（关键 task 结束触发 nonzero/503），但永久
> hang/lag、依赖断连和生产等价容器验证仍 OPEN；当前证据以 `24` 为准。

> Phase 3E 更新：下文 schema 真相源与 Gateway `schema ensure` 耦合为
> Phase 1/2 快照。当前工作区已收口部署期 root+RDP 显式 job，managed
> runtime 只读校验，Gateway 在 readiness/background 前失败关闭。真 PG
> clone manifest 和 app+schema rollback 仍 OPEN；详见 `25`。

> Phase 3J 更新：下文 FS-016 的 INTEREST + fail-open 是 Phase 1/2 原始快照。当前工作区已实施 generation-scoped strict barrier，但真 Redis/NATS/Compose 启动重启故障矩阵仍 OPEN；详见 `30`。

> Phase 3K 更新：FS-006 已为七条固定周期资金关键循环增加成功进度 deadline；永久 await 或连续无成功周期会分类为 `stalled` 并触发 nonzero/503。事件驱动任务、整体 event-loop stall 和目标依赖/容器验证仍 OPEN；详见 `31`。

## 总体评价

AATS 已从单体演进为 gateway/market/decision/execution 四个主切片，并把 live 数据采集、RDP、监控基础设施拆为支持进程。领域边界基本清晰，资金事实集中在 execution/ledger/portfolio/reconciliation，消息总线承担传播而非唯一持久化，这一方向正确。主要架构风险来自：进程“活着”与业务“正确工作”没有统一 supervisor/readiness contract；部分状态以 Redis/NATS 副本传播但启动 gate fail-open；迁移和配置的真相源不够单一。

## FS-006 — 业务后台任务可死亡而容器持续 healthy

- 严重度：P1；置信度：高；类别：process supervision
- 状态：原始 finding VERIFIED；Phase 3K `PARTIALLY REMEDIATED / EVENT-DRIVEN AND TARGET RUNTIME VERIFICATION OPEN`
- 位置：`aats/bootstrap/config.py:561-684`；`process_lifecycle.py:411-467`；`docker-compose.aats.yml:161-216`
- 证据：业务循环通过 `asyncio.create_task` 加入列表，没有 done callback/supervisor；`run_process` 启动独立 heartbeat 后只等待 OS stop signal。某业务 task 异常结束不会触发进程退出或停止心跳。Compose health 仅检查心跳文件 mtime。
- 触发：private WS、对账、执行同步、command processor、outbox flush 等 task 出现未捕获异常或意外正常返回。
- 后果：进程与容器绿色，但核心能力静默停止；execution 假绿直接影响订单、成交、账户和恢复安全。
- 建议：为关键任务定义 owner、criticality、restart policy、freshness SLO；critical task 退出应让进程 non-zero 或进入显式 fail-closed degraded/halt。health/readiness 必须聚合 task generation 与最后成功时间。
- Phase 3K 证据：账户刷新、执行同步、对账、execution outbox/command、Phase 1 shadow 和 trial guard 已声明成功进度预算，pending/连续失败超时映射为 `stalled`。19 focused、118 related 与 4,296 full unit 通过；WebSocket/dispatcher 等事件驱动任务和目标运行仍未证。

## FS-016 — peer readiness 与 INTEREST stream 的故障语义不闭合

- 严重度：P2；置信度：高；类别：messaging / startup ordering
- 状态：原始 finding VERIFIED；Phase 3J `CODE REMEDIATED / TARGET NATS STARTUP-RESTART VERIFICATION OPEN`
- 位置：`process_lifecycle.py:19-29,149-222`；`aats/bus/nats_bus.py:614-645`
- 证据：一般 events stream 已使用 INTEREST；readiness 在 Redis get 异常或 60 秒超时后返回并继续。命令 topic 已拆到 LIMITS stream，这是重要保护，但一般 events 中仍包含订单更新、fills、reconciliation、kill switch 等状态传播。
- 触发：Redis 在部署启动窗口不可用、peer 初始化超过 60 秒、ready key 过期/残留时序异常。
- 后果：INTEREST topic 在无 consumer 时可能不保留；各进程依赖 DB/Redis hydrate 的覆盖不一致，UI/状态副本可能缺事件。
- 建议：按 topic 建立 delivery contract；资金命令/事实必须 outbox + durable storage，快照类必须可 hydrate；INTEREST 仅用于可证明可丢的 observer 数据。live profile 的必需 peer readiness 应 fail-closed。
- Phase 3J 证据：四主进程 hybrid/nats 对 generation/hot-state/announce/poll/timeout 失败关闭；标准 deploy 生成同代次，Compose 必填注入，key/payload/evidence 同时绑定。129 项扩大相关与 4,286 项全量 unit 通过。未运行真 NATS/Redis，因此只能裁定代码路径已收口。

## 边界与耦合

- `ApplicationRuntime` 是大型 composition root，字段超过百项，承担所有 slice 的可选依赖；优点是 wiring 集中，缺点是 gateway/market/decision/execution 的编译期边界弱，大量 `None`/`getattr` 让错误延后到运行期。
- Gateway 同时承载 operator API、RDP API 和 dashboard 聚合，控制面与研究查询故障域仍耦合。Phase 3E 已移除 Gateway 的 schema DDL 所有权，仅保留启动前只读校验。
- NATS handler fan-out 可能在一个 handler 失败时重投整个消息；所有 handler 必须逐一证明幂等。已审 portfolio/outbox 有较强幂等证据，其他 observer 尚未逐项证明。
- 多套“active parameter / strategy profile / runtime bundle / managed YAML”语义重叠。应以治理 DB + 版本化 runtime truth 为单一当前事实，YAML 只做 bootstrap/default。

## 架构层建议顺序

先建立统一 readiness/supervision contract，再修资金路径 fencing；然后收敛 schema migration 与配置校验；最后拆分 composition root 和 query plane。不要在 live 前做与风险闭环无关的大规模重构。
