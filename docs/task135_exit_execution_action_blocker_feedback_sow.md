## Task 135 - Exit Execution Action Blocker Feedback

### Objective
- Fix the action-feedback correctness gap where the latest action card can incorrectly reuse the current blocker as if it were the blocker immediately after the action.
- Push the operator-action loop one step further so `refresh / retry_limit_lookup / safe_cancel` responses directly carry the post-action parent blocker.

### Current Behavior
- Parent-exit review items expose `current_blocker`.
- `latest_operator_action.remaining_blocker` is synthesized from the current review item, not from the action-time result, so later reconciliation can make the card wording historically inaccurate.
- The three exit-execution operator action APIs do not return the post-action blocker explicitly.

### Planned Changes
1. Persist the post-action parent review snapshot and blocker into operator action `details`.
2. Make `OperatorQueryService` read `remaining_blocker` from operator action details instead of reconstructing it from the current review item.
3. Extend the three exit-execution operator action responses with:
   - `details.parent_review_after`
   - `details.current_blocker_after_action`
4. Show the returned blocker in the dashboard success banner so operators can see the result before the next full refresh.
5. Add regression tests for:
   - operator API response payloads,
   - risk view action-feedback wording.

### Boundaries
- No new route.
- No exit-execution state machine change.
- No schema migration.

### Acceptance Criteria
- Latest-action blocker wording is based on action-time details, not current-state reconstruction.
- Exit-execution action responses directly tell operators what blocker remains after the action.
