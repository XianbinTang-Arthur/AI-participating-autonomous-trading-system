# Dashboard Snapshot 全局 Loader 并发阀 SOW - 2026-05-08

## Business objectives and boundaries

目标是限制 dashboard snapshot 后台 loader 同时进入同步 DB/threadpool 读路径的总数量，避免轻量 P0 panel 因跨 priority 后台任务排队而出现软超时。

边界限定在 snapshot plane 的后台 loader 并发控制，不改变交易决策、下单、风控、恢复、RDP 审批语义、数据库 schema、API payload 结构或具体 panel loader 结果。

## Current behavior

当前只有 per-priority semaphore：P0、P1、P2、P3 可以同时各自启动 loader。即使 scheduler 已经按 priority 分批，同一时刻仍可能存在多个 priority 的 `asyncio.to_thread` 任务占用 DB 连接或线程池。部署后日志显示 `mode` 这种轻量 panel 也偶发超过 1 秒软预算，说明存在全局排队压力。

## Target behavior

在 priority semaphore 外再增加全局 loader semaphore：

- 默认全局同时运行的 loader 上限为 3。
- per-priority 上限继续保留，避免单一 priority 独占。
- singleflight 继续保证同一 snapshot key 不重复刷新。
- 被全局阀拦住的任务只等待，不会启动额外 DB 查询。

## Transactions, consistency, concurrency

不新增写入，不改变 snapshot 数据结构。该变更只减少同步 loader 的并发 fan-out，不改变 refresh 成功、失败、软超时或快照写入语义。

## Error handling and idempotency

全局 semaphore 使用 async context manager，取消和异常都会释放 slot。`stop()` 继续取消 inflight tasks。

## Caching and performance

该变更可能让低优先级重面板更晚刷新，但会降低 DB/threadpool 瞬时压力。P0/P1 仍通过 priority 顺序、scheduler batch 和较短 TTL 保持较高刷新频率。

## Logging, monitoring, auditing

沿用 `dashboard_snapshot_refresh_start/success/timeout` 日志观察效果。验收重点是轻量 `mode`、`runtime` 不再因全局排队出现持续 timeout。

## Testing strategy

新增单元测试覆盖：跨 P0/P1/P2 同时 enqueue 时，实际进入 loader 的最大并发不超过全局上限。

## Migration, rollback, compatibility

无需迁移。回滚为删除全局 loader semaphore，恢复仅 per-priority 并发控制。

## Deployment and acceptance criteria

部署后验收：

1. derivatives-live 关键容器健康。
2. DB 无长时间 active query backlog。
3. gateway 日志稳定窗口中不再持续出现 dashboard snapshot timeout。
4. 受影响单元和集成测试通过。
