## Task 136 - Exit Execution Action History

### Objective
- Fix the stale cached recovery payload returned by exit-execution operator actions.
- Add a short operator action history to each parent-exit review item so operators can review the recent handling sequence without leaving the risk workspace.

### Current Behavior
- `refresh / retry_limit_lookup / safe_cancel` append an operator action event, but their direct response can still reuse a cached `recovery` payload from before the event append.
- Parent-exit review items only show the latest operator action, not the recent sequence of actions.

### Planned Changes
1. Invalidate operator query caches after writing exit-execution operator action events, before returning `recovery`.
2. Enrich parent-exit review items with `recent_operator_actions`.
3. Render the recent action history on the risk workspace parent-exit card.
4. Add regression coverage for:
   - action response `recovery` includes the just-recorded action,
   - risk view displays recent action history.

### Boundaries
- No new API route.
- No schema change.
- No change to execution semantics.

### Acceptance Criteria
- Exit-execution operator action responses no longer return stale cached `recovery` payloads.
- Risk workspace parent-exit cards show a short recent action history with status, time, summary, and post-action blocker.
