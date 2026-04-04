## Task 134 - Exit Execution Blocker Feedback And Env Sync

### Objective
- Surface the current parent-exit blocker directly on the operator recovery card, so operators can see what still prevents progress after a manual action.
- Sync the recent unknown-write review threshold settings into the real root `.env.*` files, templates, and configuration reference.

### Current Behavior
- Parent-exit review items currently expose `latest_operator_action`, but they do not explicitly say what blocker still remains after that action.
- `query_service` only enriches exit review items when a matching operator action exists, which makes the enrichment path action-dependent.
- `execution_unknown_submit_review_after_seconds` and `execution_unknown_cancel_review_after_seconds` exist in `settings.py`, but they are not documented in managed config docs or present in the real root `.env.*` files.

### Planned Changes
1. Update `OperatorQueryService` exit-review enrichment so it always enriches review items and derives a stable `current_blocker` payload.
2. Render the current blocker on the risk workspace exit-execution card, with wording that changes depending on whether a recent operator action exists.
3. Add missing Chinese copy for parent-exit blocker/review codes used by the risk workspace.
4. Sync the unknown-write review threshold settings into:
   - root `.env.derivatives`
   - root `.env.derivatives.live`
   - root `.env.spot`
   - root `.env.spot.live`
   - `configs/templates/.env.derivatives.example`
   - `configs/templates/.env.spot.example`
   - `configs/README.md`
   - `docs/configuration/managed-config-reference.md`
5. Add regression tests for:
   - recovery API enrichment returning the current blocker,
   - risk view showing the blocker after the latest action.

### Boundaries
- No new operator API route.
- No change to the exit-execution state machine itself.
- No schema change for parent-exit review items outside the additive enrichment fields.

### Acceptance Criteria
- Recovery payloads expose a stable `current_blocker` structure for parent-exit review items.
- Risk workspace cards show operators what the parent is still blocked on after the latest action, without requiring number-by-number comparison.
- Root `.env.*` files and config templates/reference include the unknown-write review threshold settings.
