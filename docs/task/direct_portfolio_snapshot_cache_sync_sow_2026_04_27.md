# Direct Portfolio Snapshot Cache Sync SOW (2026-04-27)

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../project_positioning.md)。

## Business Objectives And Boundaries

修复 `recovery_auto_healed` / 直接 `portfolio_repo.save_snapshot(...)` 写入后，gateway 仍从旧 `PortfolioSnapshotCache` / Redis 热状态读取过期持仓的问题。范围只覆盖直接 `save_snapshot()` 路径；`save_snapshot_in_session()` 仍归 outbox publisher 事务路径管理。

## Module Responsibilities And Domain Model

- `PostgresPortfolioRepository` / `InMemoryPortfolioRepository`: 持久化 `PortfolioSnapshot`，commit 后调用 listener。
- `PortfolioSnapshotCache`: latest snapshot 的跨进程热状态 sidecar。
- `OperatorQueryService`: 继续 cache 优先、Postgres fallback，不改公开 API。

## Input/Output Interfaces

输入是已构造并成功写入 repo 的 `PortfolioSnapshot`。输出是本进程 cache 更新、Redis latest key 更新，以及 best-effort `portfolio.snapshots` cache-only 广播。

## Database Schema / Tables / Indexes / Constraints

不改 schema。仍写 `portfolio_snapshots` 表；本修复不新增表、索引或约束。

## Transactions, Consistency, Concurrency

listener 只在 `save_snapshot()` commit 成功后触发，避免未提交快照进入 cache。`snapshot_ts <= existing.snapshot_ts` 仍作为幂等和防退化规则。异步 Redis/NATS 写失败不回滚已提交的快照。

## Authorization, Authentication, Data Security

不读取或打印凭证；广播内容仅为已有 `PortfolioSnapshot` 业务数据。

## Error Handling And Idempotency

直接写同步走 fire-and-forget：本地 apply 立即生效，Redis/NATS best-effort。调度失败只记录 warning，不拖垮 repo 写路径。重复或旧 snapshot 不推进远端状态。cache-only 广播使用独立 `source_component`，reconciliation 不把它当作新对账输入，避免恢复启动期误触发对账。

## State Transition And Lifecycle

`recovery_auto_healed` 空仓快照应从 Postgres 推进到 Redis 和其他进程 cache，覆盖旧持仓快照。outbox publisher 路径保持原有 lifecycle，不经 listener。

## Caching And Performance

只在直接 `save_snapshot()` 后增加一次异步 Redis SET 和一次 NATS 广播。读路径仍命中本地 dict，cache miss 才回落 Postgres。

## Logging, Monitoring, Auditing

新增 direct sync 相关日志事件，用于区分 stale noop、无 event loop、调度失败和 NATS 广播失败。reconciliation 对 cache-only 来源显式跳过，避免把热状态同步事件误作为对账触发事件。

## Testing Strategy

补单元测试覆盖直接 `save_snapshot()` 后本地 cache、Redis hot state、远端 cache 都推进到 recovery 空仓快照，并保留 `save_snapshot_in_session()` 不触发 listener 的测试。

## Migration, Rollback, Compatibility

无迁移。回滚本改动后直接写路径退回本进程 local-only listener 行为；公开 API 不变。

## Configuration And Environment Isolation

不新增配置。Windows 单元测试使用 in-memory bus/store；实盘仍使用现有 Redis/NATS 配线。

## Code Organization And Dependencies

改动集中在 `portfolio_service/snapshot_cache.py`、`bootstrap/config.py` 和对应单元测试。不新增第三方依赖。

## Documentation And Operations Manual

本 SOW 记录修复范围。运维验证可检查 Redis key `aats:hot:portfolio:latest:derivatives:cross` 是否跟随最新 `portfolio_snapshots`。

## Deployment And Acceptance Criteria

验收标准：直接 `save_snapshot()` 写入较新的空仓 `PortfolioSnapshot` 后，gateway 读路径不再显示旧持仓；相关单元测试、lint 和 unit suite 按仓库要求通过。
