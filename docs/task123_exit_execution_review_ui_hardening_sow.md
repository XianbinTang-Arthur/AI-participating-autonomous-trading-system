## Task 123 - Exit Execution Review UI Hardening

### Objective
- Fix the current-stage UI correctness issues in the exit-execution operator loop.
- Harden the risk workspace so operators see one coherent parent-exit review card and only the actions they are actually allowed to run.

### Current Behavior
- Parent-exit review items from runtime state and startup snapshots are merged in the risk workspace.
- The merge key currently allows the same parent exit task to render twice if the review kind changed over time.
- The UI treats `retry_limit_lookup` like an ordinary write action even though the backend route requires admin access.

### Planned Changes
1. Merge parent-exit review items by `parent_intent_id`, not by `parent + kind`, while preserving runtime state as the primary truth source.
2. Add role-aware action gating for exit-execution actions:
   - `refresh_exchange_state` and `safe_cancel` follow normal write access.
   - `retry_limit_lookup` requires admin access, or local unsafe-write mode when auth is disabled.
3. Update dashboard UI tests to cover:
   - admin-only action gating,
   - runtime-preferred deduplication over startup snapshot entries.

### Boundaries
- No backend API change.
- No schema change.
- No new UI route or page.
- No change to exit execution domain semantics.

### Acceptance Criteria
- One parent exit task renders as at most one review card in the risk workspace.
- Runtime review state overrides older startup snapshot state for the same parent.
- Non-admin operators do not get an active `重试拆单上限查询` button.
- Existing startup-snapshot-backed action rendering still works for admin-capable sessions.
