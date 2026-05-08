# Live Visibility Panel Error SOW - 2026-05-08

## Business Objectives And Boundaries
- Prevent operator UI panels from showing "暂无记录" when the underlying protected panel read failed.
- Keep this change limited to dashboard visibility/read-state semantics and test fixtures.
- Do not change trading decisions, risk gates, execution behavior, database schemas, or authentication policy.

## Module Responsibilities And Domain Model
- `strategy-view.js` owns strategy decision history rendering.
- `execution-view.js` owns recent order/fill rendering.
- Operator API panels continue to return either data or per-panel errors; the frontend must distinguish empty successful reads from failed reads.

## Input/Output Interfaces
- Input: existing `data.errors.recentDecisions`, `data.errors.recentOrders`, and `data.errors.recentFills` fields supplied by the dashboard store.
- Output: panel-local Chinese callouts for read failures instead of empty-table placeholders.
- API response schemas remain unchanged.

## Database Schema / Tables / Indexes / Constraints
- No schema change.
- No new indexes.

## Transactions, Consistency, Concurrency
- No transactional writes.
- Rendering is deterministic from the current dashboard state snapshot.

## Authorization, Authentication, Data Security
- Do not expose credentials or tokens.
- Auth failures should be visible as localized UI read failures, not misrepresented as empty data.

## Error Handling And Idempotency
- Panel errors are rendered as stable callouts.
- Successful empty reads still render normal empty states.

## State Transition And Lifecycle
- No new persisted states.
- Existing dashboard refresh lifecycle and `state.errors` ownership remain unchanged.

## Caching And Performance
- No new network calls.
- Rendering change is local and does not increase backend load.

## Logging, Monitoring, Auditing
- No logging changes.
- Acceptance is via unit rendering tests and operator API integration coverage.

## Testing Strategy
- Add Node-backed unit tests for strategy and execution panel error rendering.
- Fix the operator visibility integration fixture to seed explicit order/fill records before exercising order/fill endpoints.

## Migration, Rollback, Compatibility
- Backward compatible frontend behavior.
- Rollback is a normal git revert of the UI/test change.

## Configuration And Environment Isolation
- No configuration changes.
- Live deployment must use the standard `scripts/deploy.sh` entrypoint.

## Code Organization And Dependencies
- Keep changes in existing static view modules and the affected integration test.
- No new dependencies.

## Documentation And Operations Manual
- This SOW documents the scope and acceptance criteria for this optimization wave.

## Deployment And Acceptance Criteria
- `ruff check aats/ --fix` passes.
- Relevant unit and integration tests pass.
- Full unit suite passes before deploy.
- `derivatives-live` deploy succeeds and post-deploy health/log/DB checks are clean.
