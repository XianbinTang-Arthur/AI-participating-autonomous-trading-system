## Task 138 - Exit Execution Timeline Scope and State

### Objective
- Fix parent-exit action timeline scope leakage across runtimes that share the same symbol.
- Surface each timeline entry's resulting parent aggregate status for faster operator interpretation.

### Current Behavior
- Standalone parent-exit action history falls back to symbol-only scope checks when building the timeline.
- Timeline entries show action/status/blocker, but not the resulting parent aggregate status.

### Planned Changes
1. Tighten `exit_execution_action_history` scope checks by resolving the referenced parent from `exit_execution_repo` when available.
2. Carry `aggregate_status` through normalized timeline entries.
3. Render parent aggregate status in the risk workspace timeline card.
4. Add regression coverage for:
   - out-of-scope parent actions are excluded,
   - timeline entries expose the resulting parent aggregate status.

### Boundaries
- No new route.
- No schema change.
- No execution semantics change.

### Acceptance Criteria
- Cross-scope parent-exit actions do not appear in the current runtime timeline.
- Risk workspace timeline entries show the resulting parent status after each operator action.
