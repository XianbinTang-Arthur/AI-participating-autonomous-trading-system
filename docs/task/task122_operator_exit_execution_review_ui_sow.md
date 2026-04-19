## Task 122 - Exit Execution Review UI Wiring

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。


### Business Objective
- Close the current-stage operator workflow gap: startup parent-exit review items and their operator actions already exist in the backend, but the dashboard risk workspace does not surface or trigger them.
- Deliver the next-phase minimum UI so operators can act on parent-exit review items without leaving the dashboard.

### Current Behavior
- Startup recovery can persist `startup_exit_execution_review` snapshots and operator query APIs can resolve `refresh / retry_limit_lookup / safe_cancel` against that snapshot context.
- The risk workspace does not render parent-exit review items from `recovery.exit_execution_review_items` or from `latest_state_snapshot.details_json.review_items`.
- `app.js` cannot dispatch dedicated parent-exit operator actions, so the new backend control loop is effectively hidden from operators.

### Scope
- Add a risk-page card that surfaces parent-exit review items from the current recovery payload and startup snapshot.
- Add dedicated dashboard actions for:
  - `POST /system/exit-execution/refresh`
  - `POST /system/exit-execution/retry-limit-lookup`
  - `POST /system/exit-execution/safe-cancel`
- Keep existing reconciliation, blocker-control, and backend operator APIs unchanged.

### Boundaries
- No schema change.
- No new backend operator action type.
- No UI framework refactor.
- No change to automatic split execution policy.

### Module Responsibilities
- `aats/api/static/modules/views/risk-view.js`
  - Render parent-exit review items.
  - Merge live review items with startup snapshot review items without duplicating the same parent review record.
  - Attach the correct operator action IDs and parent intent IDs to buttons.
- `aats/api/static/app.js`
  - Dispatch the new parent-exit operator actions to the dedicated backend routes.
  - Reuse the existing action lifecycle, banner, and refresh flow.

### Input / Output Interfaces
- Input:
  - `systemRecovery.recovery.exit_execution_review_items`
  - `systemRecovery.recovery.latest_state_snapshot.details_json.review_items`
- Output:
  - Risk workspace HTML containing parent-exit review cards and action buttons.
  - Operator requests to the three `/system/exit-execution/*` endpoints.

### Consistency and Concurrency
- UI actions continue to rely on the existing `state.actionInFlight` guard and post-action manual refresh.
- Parent selection remains explicit via `parent_intent_id`; backend snapshot resolution remains the fallback when the ID is omitted.

### Error Handling and Idempotency
- Route invocation continues through existing `runAction` / `runDangerousAction` wrappers.
- Action failures remain banner-visible and do not mutate local UI state optimistically.

### Logging, Monitoring, Auditing
- Backend operator audit records are unchanged and continue to include `startup_snapshot_context`.
- UI changes only expose those audited actions; no new audit format is introduced.

### Testing Strategy
- Add / update dashboard UI integration tests to verify:
  - the risk view renders parent-exit review actions from startup snapshot data,
  - the dashboard app bundle contains dedicated action dispatch wiring for the new parent-exit routes.

### Migration, Rollback, Compatibility
- No migration required.
- Safe rollback by removing the risk-page card and action dispatch helpers.

### Documentation and Acceptance Criteria
- Operators can see startup parent-exit review items on the risk page.
- Operators can click `刷新交易所状态` / `重试拆单上限查询` / `安全取消退出任务`.
- The UI continues to work when only live review items exist, only startup snapshot review items exist, or both exist.
