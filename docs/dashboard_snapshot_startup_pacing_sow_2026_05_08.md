# Dashboard Snapshot 启动预热节流 SOW - 2026-05-08

## Business objectives and boundaries

目标是降低 gateway 重启后 dashboard snapshot 首轮冷缓存预热对 DB 和线程池的瞬时压力，避免 P0/P1 面板在同一秒内集中软超时。

边界限定在操作台 dashboard snapshot 后台读模型：不改变交易决策、下单、风控、恢复、RDP 审批语义、数据库 schema、API payload 结构或前端交互。

## Current behavior

`DashboardSnapshotPlane.start()` 当前直接调用 `enqueue_all(reason="startup")`。这会把 P0/P1/P2/P3 以及默认变体一次性放入 inflight。虽然每个 priority 有独立 semaphore，但不同 priority 会同时运行，启动冷缓存时可能形成 P0、P1、P2、P3 的叠加并发。

## Target behavior

启动预热改为按 priority 和 panel 顺序分批投放：

- P0 优先，随后 P1、P2、P3。
- 同一 priority 内按现有 registry 顺序逐个投放。
- scheduler 在启动预热尚未轮到某个 snapshot key 时，不再用 `scheduler_missing` 抢先补发。
- 请求侧读取尚未轮到的 startup pending key 时返回 loading/refreshing 语义，不抢先 enqueue；已轮到或非 startup pending 的 missing/stale panel 仍可触发 read-side enqueue。

## Transactions, consistency, concurrency

不新增写入，不改变 snapshot singleflight。已有 `_inflight` 仍保证同一 snapshot key 同时只有一个刷新任务。新增的 pending-startup 集合只用于防止 scheduler 在冷启动阶段把未轮到的 key 全量补发。

## Error handling and idempotency

如果启动预热任务异常退出，会释放 pending-startup 集合，让常规 scheduler 恢复 missing panel 刷新，避免永久跳过。`stop()` 会取消启动预热任务并清理 pending 状态。

## Caching and performance

该变更不延长任何 panel 的 TTL、stale-after 或 hard-expire。性能收益来自降低启动瞬时并发，而不是隐藏慢查询或增加超时预算。

## Logging, monitoring, auditing

保留原有 `dashboard_snapshot_refresh_*` 日志。新增启动预热 enqueue 开始、enqueue 完成、enqueue 失败日志，用于部署后确认冷启动是否按分批节奏投放；具体 snapshot 完成状态仍以各 panel 的 `dashboard_snapshot_refresh_success/failed/timeout` 为准。

## Testing strategy

新增单元测试覆盖：

1. `start()` 只按节奏投放 startup refresh。
2. scheduler 和 request-time read 不会在 startup pending 阶段抢先 enqueue 所有 missing panel。
3. startup 最终仍会覆盖所有注册 panel。

现有 snapshot singleflight、软超时、default variants、bundle snapshot 读取测试继续保持。

## Migration, rollback, compatibility

无需迁移。回滚为恢复 `start()` 直接 `enqueue_all(reason="startup")`。外部 API 与前端 payload 兼容。

## Deployment and acceptance criteria

部署后验收：

1. gateway 健康。
2. derivatives-live 关键容器健康。
3. DB 无长时间 active query backlog。
4. 重启后首轮 gateway 日志中 dashboard snapshot timeout 数量低于上一轮冷启动，稳定窗口无持续 timeout。
