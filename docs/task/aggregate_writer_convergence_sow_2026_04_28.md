# Aggregate writer convergence SOW (2026-04-28)

## 1. Business objectives / boundaries

目标：把 execution 侧仍存在的多 writer 伏笔收敛为每类 aggregate 一个生产 writer service，避免实盘订单、资金预留、portfolio projection、exit execution 父子聚合在跨进程/恢复/人工 operator 路径中出现一半提交、一半不可见或被覆盖的状态。

本次边界：

- `order/fill/obligation`：Postgres 生产路径必须经 `PostgresExecutionOutboxPublisher` 的事务写入或 obligation writer 入口写入；service 层只负责 preview / domain 计算。
- `portfolio snapshot + fill outcome + balance delta`：恢复补偿必须经 `PostgresPortfolioOutboxPublisher` 的 fill projection 路径；无 publisher 的非 Postgres legacy 单测路径才允许直接 repo 写。
- `exit_execution`：新增 writer service，order manager、refresh、startup recovery 只能经 writer 写 parent intent / child ref。
- operator clear cache 不再制造空 authoritative cache，清理 Redis 后必须从 DB active obligations 重建本地/Redis 可见状态。

不做：本次不改表结构，不引入新的外部 broker，不改变 operator API 入参/返回主形状。

## 2. Module responsibilities / domain model

- `ExecutionObligationService`：负责 obligation 预留、成交消耗、终态释放的纯 domain 计算；生产持久化委托 writer。
- `PostgresExecutionOutboxPublisher`：负责 Postgres execution aggregate 的同事务写入、durable outbox 与 hot cache post-commit 发布；新增 standalone obligation persist 入口用于 recovery cleanup 等无 order/fill envelope 的路径。
- `PostgresPortfolioOutboxPublisher`：负责 snapshot、balance delta、fill outcome 的原子 projection；新增 sync fill projection 入口给 startup/recovery 使用。
- `ExitExecutionWriter`：负责 `ExitExecutionIntent` 与 `ChildExitOrderRef` 的唯一写入口；aggregator/order manager/startup recovery 只读 repo、写 writer。

## 3. Input / output

输入保持现有模型：

- `OrderObligation`
- `OrderState`
- `FillEvent`
- `PortfolioSnapshot`
- `PortfolioBalanceDelta`
- `FillOutcomeRecord`
- `ExitExecutionIntent`
- `ChildExitOrderRef`

输出保持现有返回值语义，新增返回字段仅限 operator action details 内的重建计数/失败计数。

## 4. DB schema / tables

不改 schema。涉及现有表：

- `execution_obligations`
- `execution_order_states` / `execution_fills`
- `portfolio_snapshots`
- `fill_outcomes`
- event store / outbox tables
- `exit_execution_intents`
- `exit_execution_child_refs`

## 5. Transactions / concurrency

- obligation 与 order/fill 同事务路径继续由 `PostgresExecutionOutboxPublisher` 完成。
- orphan obligation cleanup 走 execution outbox publisher 的 standalone obligation writer，commit 后发布 hot cache。
- recovery fill outcome backfill 走 portfolio outbox publisher 的 sync projection，单事务写 snapshot + outcome + balance delta outbox。
- exit_execution 先收敛入口，不在本次引入 OCC；writer method 保留 source/reason 参数，为后续 history/OCC 加载点预留。

## 6. Auth / security

不读取、不打印任何凭证。operator clear cache 保持现有权限上游校验，不扩大可操作范围。

## 7. Error handling / idempotency

- cache publish 仍 fail-soft，DB commit 成功后发布失败只记录 warning。
- missing fill outcome backfill 对已有 outcome 幂等跳过；projection 失败计入 notes 并继续后续 fill。
- direct Postgres legacy writer 触发明确异常，避免静默绕过 outbox。

## 8. State lifecycle

- obligation：ACTIVE/PARTIALLY_CONSUMED -> RELEASED/CANCELED/FAILED 只能通过 writer 持久化并 post-commit 发布。
- fill outcome：缺失 -> projection 补偿，补偿同时推进 snapshot/balance delta outbox。
- exit_execution：parent/child ref 写入统一进 writer；refresh/retry/safe_cancel 不直接保存 repo。

## 9. Caching / performance

- clear obligation cache 清 Redis index 后从 DB active obligations 重建本地 cache 与 Redis，避免 risk/gateway 看到空 cache。
- recovery sync projection 只在启动/恢复补偿路径执行，不影响热路径成交延迟。

## 10. Logging / auditing

- direct writer violation 记录 source_component、operation、aggregate key。
- operator clear cache action details 记录清理和重建计数。
- recovery backfill 保留失败日志并在 notes 中暴露失败计数。

## 11. Testing strategy

- 单元测试：obligation clear cache rebuild、obligation production direct-write guard、recovery fill outcome 走 portfolio outbox、exit_execution writer routing。
- 契约扫描：生产 services 中 direct `save_obligation` / `fill_outcome_repo.save_outcome` / `exit_execution_repo.save_*` 只能出现在 writer 或 legacy fallback 白名单。
- 集成测试：跑最窄 Postgres outbox / recovery 相关测试。

## 12. Migration / rollback

无 schema 迁移。回滚为代码回滚；已有数据模型兼容。

## 13. Config / env

不新增配置项。Postgres writer 是否可用仍由 bootstrap 中已有 `database_runtime`、repo 类型、outbox repo 条件决定。

## 14. Code organization / dependencies

新增文件放在 `aats/services/execution_engine/`：

- `obligation_writer.py`
- `exit_execution_writer.py`

现有 outbox publisher 增加 writer 入口，不增加第三方依赖。

## 15. Docs / ops

本 SOW 是本次变更的操作记录。operator runbook 后续可补“clear obligation cache 会 rebuild，不会清成空 authoritative state”。

## 16. Deployment / acceptance

验收条件：

- recovery/orphan/operator 路径不再直接写 Postgres obligation / fill outcome projection / exit_execution aggregate。
- clear obligation cache 后 active obligation 仍可被 risk/gateway 读到。
- `ruff`、unit tests、窄 integration tests、`git diff --check` 通过或明确记录失败原因。
