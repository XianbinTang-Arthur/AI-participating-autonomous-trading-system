# Dashboard Snapshot Scheduler 稳态分批 SOW - 2026-05-08

## Business objectives and boundaries

目标是降低 dashboard snapshot 稳态刷新时同一秒内多个 P0/P1 panel 同时进入 DB 读路径的概率，减少 `health`、`blockerControl`、`blockers` 这类共享面板的偶发软超时。

边界限定在 snapshot scheduler 的 enqueue 策略，不改变交易决策、下单、风控、恢复、RDP 审批语义、数据库 schema、API payload 结构或具体 panel loader 结果。

## Current behavior

`_enqueue_due_panels()` 每秒扫描所有 snapshot key，发现 missing 或 TTL due 就立即 enqueue。多个 TTL 相同的 P0 panel 会在同一个 scheduler tick 同时进入 `_inflight`，再由 P0 semaphore 并发执行。部署后日志显示 `blockerControl` 和 `blockers` 经常同秒启动，放大重复读压力。

## Target behavior

为 scheduler 增加每 priority、每 tick 的 enqueue batch limit：

- P0 默认每 tick 最多新增 2 个 refresh。
- P1/P2/P3 默认每 tick 最多新增 1 个 refresh。
- singleflight 仍然防止同一 snapshot key 重复刷新。
- request-time read 的非 startup pending 路径仍可按原逻辑触发 missing/stale refresh，避免用户交互被 scheduler batch 完全阻塞。

## Transactions, consistency, concurrency

不新增写入，不改变 snapshot 数据结构。该策略只改变“什么时候把 due key 放入 inflight”，不改变 loader 的读语义、timeout 预算或已有 semaphore。

## Error handling and idempotency

如果某个 due key 本 tick 未被 enqueue，下一个 scheduler tick 会重新评估。没有持久队列状态，因此失败回滚为恢复原扫描即发行为。

## Caching and performance

该变更不延长 TTL/stale-after/hard-expire，也不提高 timeout。收益来自平滑同一秒的刷新 fan-out。P0 有 9 个 key，默认 2/tick 能在约 5 秒内覆盖一轮，仍贴近 P0 stale-after。

## Logging, monitoring, auditing

沿用 `dashboard_snapshot_refresh_start/success/timeout` 日志观察效果。验收重点是 timeout 是否继续成组出现在同一秒。

## Testing strategy

新增单元测试覆盖：同一个 scheduler tick 内，多个 due P0 key 只会按 batch limit 新增 enqueue；后续 tick 会继续补齐。

## Migration, rollback, compatibility

无需迁移。回滚为删除 batch limit 并恢复 `_enqueue_due_panels()` 对 due key 的逐项立即 enqueue。

## Deployment and acceptance criteria

部署后验收：

1. derivatives-live 关键容器健康。
2. DB 无长时间 active query backlog。
3. gateway 日志中 dashboard snapshot timeout 不再持续成组出现。
4. 受影响单元和集成测试通过。
