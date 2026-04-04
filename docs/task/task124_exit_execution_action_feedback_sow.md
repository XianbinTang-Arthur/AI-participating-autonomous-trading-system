## Task 124 - Exit Execution Action Feedback

### Objective
- Surface the latest operator action outcome for each parent exit review item.
- Reduce ambiguity after operators click `refresh / retry_limit_lookup / safe_cancel` by leaving an auditable action summary in the recovery UI.

### Current Behavior
- Parent exit review items expose actionable buttons, but the card itself does not show the latest action taken against that parent.
- Startup-snapshot-backed review items lose operator action context once the initial toast disappears.

### Planned Changes
1. Enrich parent exit review items with the latest matching operator action summary from `system.operator_actions`.
2. Apply the same enrichment to startup snapshot review items returned through the recovery view.
3. Render `最近动作 / 结果 / 时间` on the risk workspace exit-execution card.
4. Add tests covering:
   - recovery API returns latest operator action summary on parent exit review items,
   - risk view displays the enriched action summary.

### Boundaries
- No event schema change.
- No new API endpoint.
- No change to operator action execution logic.

### Acceptance Criteria
- After a parent-exit operator action runs, the recovery payload includes the latest action summary for the affected parent.
- The risk workspace shows the latest action label, result, and timestamp for that parent review item.
