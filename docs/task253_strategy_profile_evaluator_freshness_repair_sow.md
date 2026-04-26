# Task253: Strategy Profile Evaluator Freshness Repair

## Business Objectives and Boundaries

目标是修复 `strategy_profile_evaluations` / `strategy_profile_recommendations` 在 operator 手动固定档位后停止更新的问题，使 readiness/active-core 预检继续获得新鲜证据。

边界：

- 不开启自动切档。
- 不开启 release、promotion、tuning。
- 不改策略、风险门、执行门、下单行为、symbol、venue、strategy family 或 timeframe plumbing。
- 手动固定档位时不调用 AI provider。

## Module Responsibilities and Domain Model

- `ApplicationRuntime._run_profile_auto_switch_loop`：负责每 30 分钟调度一次 profile freshness evaluation。
- `StrategyProfileControlService.evaluate_now`：负责生成 evaluation、comparison、optimization、recommendation 与 selection decision。
- `StrategyProfileRepository`：负责持久化 evaluations/recommendations，并清理过期 pending recommendations。

修复后的语义：

- `strategy_profile_auto_control_enabled=false`：完全跳过调度。
- `strategy_profile_auto_control_enabled=true` 且 activation state `auto_switch_enabled=true`：做正常评估，允许 AI recommendation，允许自动激活通过 gate 后执行。
- `strategy_profile_auto_control_enabled=true` 且 activation state `auto_switch_enabled=false/manual`：只做 freshness 评估与规则 fallback recommendation；不调用 provider，不自动激活。

## Input/Output Interfaces

输入：

- `strategy_profile_auto_control_enabled`
- `StrategyProfileActivationState.auto_switch_enabled`
- profile revisions、runtime context、performance/execution/safety summary

输出：

- `strategy_profile_evaluations`
- `strategy_profile_recommendations`
- strategy profile event_store events
- scheduled tick log with `allow_auto_activation` and `use_ai_recommendation`

## Database Schema / Tables / Indexes / Constraints

不新增 schema、表、索引或约束。

写入表：

- `strategy_profile_evaluations`
- `strategy_profile_recommendations`
- `event_store`

新增 repository 行为：

- `expire_pending_recommendations(...)` 将过期 pending recommendation 标为 `expired`，并同步更新 JSON payload 的 `decision_status` 与 reason 字段。

## Transactions, Consistency, Concurrency

- 每个 repository save/expire 操作沿用现有 session/commit 边界。
- `evaluate_now` 仍按现有 phase1 / AI-or-fallback / phase3 顺序执行。
- manual 模式下禁用 AI provider，避免 provider timeout 阻塞 freshness。
- 过期清理在保存新 recommendation 前执行，避免 UI/API 把旧 pending 当作当前候选。

## Authorization, Authentication, Data Security

本任务不新增 API，不读取或输出凭证，不改变认证/授权。DB 查询和 runtime smoke 只输出非敏感聚合。

## Error Handling and Idempotency

- `auto_switch_effective_enabled()` 读取失败时，本轮调度跳过并记录 background failure。
- pending recommendation 过期清理失败时记录 warning，不阻断新的 freshness evaluation。
- 多次执行清理是幂等的：已 `expired` / `accepted` / `rejected` 的 recommendation 不会再次改变。

## State Transition and Lifecycle

recommendation lifecycle 新增自动清理路径：

- `pending` + `expires_at <= now` -> `expired`
- `accepted` / `rejected` 不受影响
- 新 recommendation 仍默认 `pending`

manual profile lifecycle 不变：manual 固定档位不会被 scheduled evaluation 自动覆盖。

## Caching and Performance

manual 模式下跳过 provider 调用，减少延迟和超时风险。每 30 分钟最多一次只读 evaluation，额外 DB 写入量与原 auto-switch 评估一致。

## Logging, Monitoring, Auditing

- scheduled tick log 增加 `allow_auto_activation` 和 `use_ai_recommendation` 字段。
- recommendation expire failure 会记录 `strategy_profile_recommendation_expire_failed` warning。
- recommendation payload 保留 fallback reason：`strategy_profile_scheduled_manual_mode`。

## Testing Strategy

Focused tests：

- 调度 loop 在 effective auto-switch disabled 时仍调用 `evaluate_now(allow_auto_activation=False, use_ai_recommendation=False)`。
- 调度 loop 在 enabled 时保持 `allow_auto_activation=True, use_ai_recommendation=True`。
- 全局 `strategy_profile_auto_control_enabled=false` 仍跳过。
- In-memory repository 只把匹配 scope 的过期 pending recommendation 标为 expired。

## Migration, Rollback, Compatibility

无 migration。`evaluate_now` 新增关键字参数 `use_ai_recommendation`，默认 `True`，保持现有调用兼容。

回滚：`git revert` 本任务 commit 并按 `scripts/deploy.sh` 标准部署。

## Configuration and Environment Isolation

不新增配置项。现有 `strategy_profile_auto_control_enabled` 继续是 scheduler 总开关，activation state 的 `auto_switch_enabled` 只控制 provider/auto activation，而不再阻断 freshness evaluation。

## Code Organization and Dependencies

修改范围：

- `aats/bootstrap/config.py`
- `aats/services/operator/strategy_profiles.py`
- `aats/storage/strategy_profile_repo.py`
- `aats/storage/strategy_profile_repo_postgres.py`
- `tests/unit/test_profile_auto_switch_schedule.py`
- `tests/unit/test_strategy_profile_repository.py`

不新增依赖。

## Documentation and Operations Manual

若 live DB 仍未刷新，应检查 decision container 是否运行、`strategy_profile_auto_control_enabled` 是否为 true、以及下一次 :00 / :30 boundary 后是否出现新 `strategy_profile_evaluations`。

manual 固定档位下看到新 recommendation 不代表自动切档已开启；应检查 payload 的 `fallback_reason_code=strategy_profile_scheduled_manual_mode` 和 activation state `auto_switch_enabled=false`。

## Deployment and Acceptance Criteria

部署入口：`bash scripts/deploy.sh --profile derivatives-live --skip-commit --timeout 180`

验收标准：

- Focused unit tests pass。
- `ruff check` pass。
- 标准部署成功。
- 部署后下一个半小时 boundary 或手动安全 smoke 后，`strategy_profile_evaluations` 最新时间推进，且 activation state 仍为 manual / `auto_switch_enabled=false`。
