# P0 Profitability Batch2 Bugfix SoW

## Business objectives and boundaries
- 修复 lifecycle attribution 在同 symbol / 同方向窗口内串入弱关联决策的问题。
- 修复 independent trial guard 对 residual exit 样本宇宙前后不一致的问题。
- 不改主视图默认账单口径，不改 public API 路径，只做向后兼容的字段补充。

## Module responsibilities and domain model
- `aats/services/operator/lifecycle_attribution.py`
  负责 lifecycle 诊断归因，主 trace 只接收强关联决策；弱关联决策仅作为候选证据暴露。
- `aats/services/strategy_execution_health.py`
  保持纯计算 helper，只消费 fills / snapshots / guard exclusion 结果，不直接查审计存储。
- `aats/services/strategy_execution_guard_filters.py`
  负责从 decision audit + runtime payload 中提取 residual-exit guard exclusion。
- `aats/services/decision_engine/context_builder.py`
  在 live 决策链上游生成 `guard_excluded_fill_ids` 并传给 health 计算。
- `aats/services/operator/query_service.py`
  在 operator 查询链复用相同 exclusion 逻辑，避免诊断和 live 判定口径分裂。

## Input/output interfaces
- `compute_strategy_execution_health()` / `compute_leg_strategy_execution_health()`
  新增可选输入 `guard_excluded_fill_ids`。
- `StrategyExecutionHealthSnapshot.as_payload()`
  新增 `recent_guard_eligible_net_realized_pnl` 输出。
- `/reports/position-lifecycle-attribution/{lifecycle_id}`
  新增 `candidate_decisions`、`trace_completeness`、`unmatched_actionable_decision_count`。

## Database schema / tables / indexes / constraints
- 无 schema 变更。

## Transactions, consistency, concurrency
- 不新增写路径。
- live 决策链与 operator 查询链都基于既有审计事件 / 审计仓储做只读分类。

## Authorization, authentication, data security
- 不新增权限面。
- 不读取或暴露凭证。

## Error handling and idempotency
- 审计缺失时回退为“不排除 fill / trace 不完整”，不抛出新异常。
- lifecycle detail 保持增量字段兼容。

## State transition and lifecycle
- lifecycle decision trace:
  - 强匹配进入主 trace。
  - 窗口内弱匹配进入 candidate，不参与主归因统计。
- trial guard:
  - fee/churn/win-rate/net-pnl 统一使用 guard-eligible sample universe。

## Caching and performance
- guard exclusion 只针对当前 fills 的 `decision_id / execution_chain_id` 解析审计记录。
- lifecycle attribution 继续复用现有 TTL cache。

## Logging, monitoring, auditing
- 不新增日志事件。
- 继续依赖既有 decision audit 事件链。

## Testing strategy
- 单测覆盖：
  - lifecycle 强/弱匹配分离与完整性字段；
  - residual cleanup 仅在审计显式信号下排除；
  - trial guard 使用 guard-eligible net pnl。
- 运行 lint、unit、WSL2 窄集成。

## Migration, rollback, compatibility
- 新字段均为向后兼容追加。
- 回滚时只需回退代码，无数据迁移。

## Configuration and environment isolation
- 不新增配置项。

## Code organization and dependencies
- 复用现有 `normalize_independent_runtime_state_payloads()`，避免在 health helper 内引入 repo/event-store 依赖。

## Documentation and operations manual
- 本 SoW 作为本次修复边界说明。

## Deployment and acceptance criteria
- lifecycle detail 不再串入弱关联决策，且能显式提示 trace 完整性。
- residual cleanup 不再用宽松启发式判定。
- engine / gates / operator trial guard 摘要统一读 guard-eligible net pnl。
