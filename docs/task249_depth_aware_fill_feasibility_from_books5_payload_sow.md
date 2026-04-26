# Task249: books5 深度感知成交可行性归因 SOW

## Business objectives and boundaries
- 目标：在 operator execution-science truth surface 中，用已持久化的 books5 sidecar 证据判断真实 fill 数量是否能被本地盘口深度覆盖。
- 边界：只读归因增强；不改变策略、风控、执行门、provider、symbol、venue、strategy family、release、promotion、tuning、schema 或 live order behavior。

## Module responsibilities and domain model
- `orderbook_snapshot_refs`：从已持久化的 books5/bbo 行投影非敏感 `depth_ladder`。
- `OperatorQueryService`：在 fill feasibility 中消费 `payload_evidence + depth_ladder`，输出 depth coverage、已用档位、可成交量、未成交量、深度均价和成本归因。
- 域模型：top-of-book 仍是一级快速证据；books5 sidecar 完整时，深度投影补充一级不足的 fill feasibility。

## Input/output interfaces
- 输入：resolved pre/post orderbook refs、`payload_evidence`、`depth_ladder`、fill payload。
- 输出：`depth_book_size_covers_fill` / `depth_book_size_insufficient` / existing top-of-book statuses；附带 `depth_levels_used`、`depth_feasible_qty`、`depth_unfilled_qty`、`depth_missing_evidence`。

## Database schema / tables / indexes / constraints
- 不改 schema。
- 读取既有 `bronze.market_orderbook_books5` / `bronze.market_orderbook_bbo` 与 `bronze.market_orderbook_payloads` sidecar evidence。

## Transactions, Consistency, Concurrency
- 只读查询，不新增事务写入。
- 保持 sidecar checksum 和 row truth 的既有一致性门。

## Authorization, Authentication, Data Security
- 不读取或输出凭证。
- 不暴露 raw payload，只输出行投影出的价格/数量档位和 sidecar 元数据。

## Error Handling and Idempotency
- 缺少 sidecar、payload hash、depth ladder 或 fill facts 时输出显式 `missing_evidence`。
- 只读计算幂等。

## State Transition and Lifecycle
- 不改变订单、fill、strategy profile 或 runtime 状态。
- lifecycle ref sequence 仍由原有 resolver 控制。

## Caching and Performance
- 使用已解析 row payload 内的 `depth_ladder`，不增加额外 DB 查询。
- 每次最多消费 books5 的 5 档深度。

## Logging, Monitoring, Auditing
- 新 truth fields 可用于 UI/API 审计。
- 不新增日志噪声。

## Testing Strategy
- 单测覆盖：
  - 无 sidecar 时大 fill 仍保持 unknown depth。
  - 有 books5 sidecar 时一级不足但多档深度可覆盖。
  - raw payload 不暴露。

## Migration, Rollback, Compatibility
- 无迁移。
- 回滚：revert 本任务 commit 并通过 `scripts/deploy.sh` redeploy。
- 向后兼容既有 top-of-book fields。

## Configuration and Environment Isolation
- 不新增配置。
- 不改变 live/staging 环境行为。

## Code Organization and Dependencies
- 只修改 resolver、operator query service、相关单测和本 SOW。
- 不新增依赖。

## Documentation and Operations Manual
- 本文件记录范围、验收和回滚方式。
- Operator 看到 depth status 时，应按“只读成交归因”理解，不作为策略放行条件。

## Deployment and Acceptance Criteria
- 验收：
  - books5 sidecar 完整时可从深度档位计算 fill coverage。
  - sidecar 缺失时不伪造深度结论。
  - raw payload 不出现在 truth surface。
  - 单测、lint、部署和 runtime smoke 通过。
