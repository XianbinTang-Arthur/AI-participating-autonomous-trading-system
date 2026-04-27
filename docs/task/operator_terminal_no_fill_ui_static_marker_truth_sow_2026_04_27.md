# Operator Terminal No-Fill UI Static Marker Truth SOW

## Business objectives and boundaries

固化上一轮已经部署的 terminal no-fill UI 可见性验证，让 `runtime_truth_report.static_truth_surface` 自动检查策略判断页和总览驾驶舱是否仍包含 terminal no-fill 解释标记。边界是只读 smoke/truth report，不改变策略、风险、执行、AI provider、交易品种、schema、release/promotion/tuning 或 live order 行为。

## Module responsibilities and domain model

- `scripts/runtime_truth_report.py`：定义并检查静态 UI marker。
- `tests/unit/scripts/test_runtime_truth_report.py`：覆盖 marker 集合和静态 surface 判定。
- Operator UI marker 代表用户能从已部署静态资源中看到 terminal no-fill 解释入口，不代表绕过 operator auth 查询受保护 API 数据。

## Input/output interfaces

输入是 gateway 静态资源 URL：

- `/ui/modules/views/strategy-view.js`
- `/ui/modules/views/overview-view.js`
- `/ui/modules/no-trade-display.js`

输出是 `static_truth_surface` 中每个资源的 `ok/http_status/markers/error`。

## Database schema / tables / indexes / constraints

不涉及数据库 schema、表、索引或约束。

## Transactions, Consistency, Concurrency

不参与交易事务，也不改变并发控制。该检查只读取已部署静态文件。

## Authorization, Authentication, Data Security

不读取凭证，不打印 token/API key/DB password/完整连接串。静态资源检查不需要 operator session；受保护 API 的字段级验证仍由单元测试覆盖。

## Error Handling and Idempotency

静态资源获取失败或 marker 缺失时只在报告中返回 `ok=false` 和 `error/http_status`，不抛出影响交易进程。重复执行幂等。

## State Transition and Lifecycle

无订单、仓位、策略、risk gate 或 lifecycle 状态迁移。

## Caching and Performance

仅在 runtime truth report 执行时发起少量静态资源读取，不引入长期缓存或后台任务。

## Logging, Monitoring, Auditing

审计入口是 `static_truth_surface` 结构化输出，以及本 SOW 和 automation state。

## Testing Strategy

新增 focused unit test，模拟静态资源响应并断言 strategy/overview terminal no-fill marker 被纳入检查。

## Migration, Rollback, Compatibility

无迁移。回滚方式是 revert 本次 runtime truth marker 变更；UI 渲染可独立保留。

## Configuration and Environment Isolation

沿用 `runtime_truth_report.py --api-base`，默认 `https://127.0.0.1:8011`。不新增配置。

## Code Organization and Dependencies

只修改现有常量 `STATIC_MARKERS` 和对应单测，不新增依赖。

## Documentation and Operations Manual

运行：

```powershell
.venv\Scripts\python.exe scripts\runtime_truth_report.py --pretty
```

检查 `static_truth_surface` 中 strategy/overview 的 terminal no-fill marker。

## Deployment and Acceptance Criteria

Acceptance criteria：

- `static_truth_surface` 自动检查 terminal no-fill UI marker。
- focused unit test 通过。
- 不改变 live order behavior。
- 若 worktree dirty 阻止部署，必须记录 exact blocker。
