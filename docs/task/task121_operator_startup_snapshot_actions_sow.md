## Task 121 - Operator Startup Snapshot Action Loop

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。


### Objective
- Wire `startup_exit_execution_review` state snapshots into the operator action loop.
- Let operator actions `refresh_exchange_state`, `retry_limit_lookup`, and `safe_cancel` reference the latest startup snapshot directly.
- Keep the current recovery / reconciliation model unchanged and add only the minimum control-plane glue.

### Current Behavior
- Startup recovery can persist an auditable `ReconciliationStateSnapshot` with `details_json.source = "startup_exit_execution_review"`.
- Recovery views expose the latest snapshot and current parent-exit review items.
- Operator actions can refresh exchange state, but they do not reference the startup snapshot context.
- There is no operator API action for retrying parent exit resume limit lookup or for safe-canceling a parent exit intent.

### Planned Changes
1. Add operator-side helpers to:
   - read the latest startup exit-execution snapshot,
   - extract `details_json.review_items`,
   - resolve a target `parent_intent_id` from the snapshot when possible,
   - attach the snapshot context to action results and audit records.
2. Extend `OrderManager` with parent-level control methods:
   - `retry_exit_execution_limit_lookup(parent_intent_id)`
   - `safe_cancel_exit_intent(parent_intent_id)`
3. Add operator API routes for:
   - `POST /system/exit-execution/retry-limit-lookup`
   - `POST /system/exit-execution/safe-cancel`
4. Enrich startup snapshot review items with explicit available operator actions so the snapshot remains self-describing.
5. Add integration tests covering:
   - refresh action audit details include startup snapshot context,
   - retry-limit-lookup can resolve a parent directly from the startup snapshot,
   - safe-cancel can resolve a parent directly from the startup snapshot.

### Boundaries
- No schema change to `OrderState`.
- No new reconciliation report type.
- No UI refactor.
- No change to auto-splitting logic beyond exposing existing resume/cancel controls to operators.

### Acceptance Criteria
- Operator action details include a `startup_snapshot_context` when the latest startup snapshot exists.
- Retry-limit-lookup can continue a resumable parent exit flow using the snapshot-selected parent.
- Safe-cancel can cancel a parent exit flow using the snapshot-selected parent.
- Lint, targeted unit tests, and narrow integration tests pass.
