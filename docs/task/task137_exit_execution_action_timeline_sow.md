## Task 137 - Exit Execution Action Timeline

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。


### Objective
- Fix parent-exit refresh actions so their operator audit record always carries a stable parent reference.
- Add a standalone parent-exit operator action timeline to the recovery view and risk workspace.

### Current Behavior
- `retry_limit_lookup` and `safe_cancel` action records carry explicit `parent_intent_id` / parent snapshots, but `refresh_exchange_state` mainly relies on startup snapshot context and post-action review state.
- Parent-exit recent actions are only shown inside the current review card; operators cannot scan a cross-parent recent handling timeline in one place.

### Planned Changes
1. Record stable `parent_intent_id`, `parent_before`, and `parent_after` payloads for parent-exit `refresh_exchange_state`.
2. Add `exit_execution_action_history` to `recovery_view`, scoped to in-scope parent-exit actions.
3. Render a dedicated “退出任务处理时间线” card on the risk workspace.
4. Add regression coverage for:
   - refresh actions keep stable parent references,
   - recovery payload includes parent-exit action history,
   - risk view renders the standalone timeline.

### Business objectives and boundaries
- Improve operator traceability and reduce ambiguity during incident handling.
- No change to execution semantics, order routing, or venue behavior.
- No new REST route and no schema migration.

### Module responsibilities and domain model
- `OperatorQueryService`: normalize and expose parent-exit operator action history.
- `RecoveryQueryFacade`: include the new timeline in recovery payloads.
- `risk-view.js`: render the timeline in Chinese for operators.

### Input/output interfaces
- Input: existing `topics.OPERATOR_ACTIONS` events.
- Output: `recovery.exit_execution_action_history` with recent normalized entries.

### Database schema / tables / indexes / constraints
- No database changes.

### Transactions, Consistency, Concurrency
- Reuse existing event append order.
- Read-side timeline is cache-backed and rebuilt after action cache invalidation.

### Authorization, Authentication, Data Security
- Timeline is read-only and follows the same recovery-view access model.
- No broadened write permissions.

### Error Handling and Idempotency
- Missing parent metadata should degrade to snapshot-derived context when available.
- Timeline building must ignore malformed action payloads rather than fail the whole recovery view.

### State Transition and Lifecycle
- No change to parent-exit execution lifecycle.
- Adds read-side observability for operator actions only.

### Caching and Performance
- Build from cached operator action event stream.
- Keep the history bounded by a small limit.

### Logging, Monitoring, Auditing
- Leverage existing operator action events; no new log channel.

### Testing Strategy
- Update operator API integration tests.
- Update dashboard UI integration tests.
- Run existing narrow unit/integration suites for parent-exit handling.

### Migration, Rollback, Compatibility
- Backward compatible; old action records remain readable.
- Rollback is code-only.

### Configuration and Environment Isolation
- No new config keys.

### Code Organization and Dependencies
- Keep changes limited to operator query/recovery and risk view rendering.

### Documentation and Operations Manual
- This SOW is the operator-facing implementation note for the new timeline.

### Deployment and Acceptance Criteria
- Refresh action records keep a stable parent reference.
- Recovery payload exposes `exit_execution_action_history`.
- Risk workspace shows a separate recent parent-exit handling timeline.
