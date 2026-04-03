# Task 196 - Auto Parallel Phase 1 Split

## Business objectives and boundaries
- Split `auto_parallel` into explicit permission, budget, and composition layers.
- Preserve existing allocator/execution interfaces while making “permission denied” and “budget zero suppressed” distinguishable.
- Keep phase 1 focused on control semantics, observability, and tests.
- Do not rename config keys, redesign allocator route actions, or change execution order models in this phase.

## Module responsibilities and domain model
- `sleeve_execution_permission.py`: decide whether a sleeve intent may enter automatic execution, including protective exceptions.
- `sleeve_budget_controller.py`: calculate contraction multipliers and scaled quantities without deciding route semantics.
- `sleeve_routing_composer.py`: compose final route action, delta, legs, and composition reasons from permission + budget outputs.
- `auto_parallel.py`: orchestration shell that extracts raw inputs, calls the three modules, and writes compatibility payloads.
- `strategy_runtime.py`: persist the layered traces on automation decisions and sleeve intents.

## Input/output interfaces
- Inputs:
  - `StrategyCandidate`
  - `StrategySleeveIntent`
  - baseline regime/volatility
  - reconciliation state
  - recent sleeve PnL
  - runtime setting `strategy_sleeve_auto_parallel_enabled`
- Outputs:
  - `ExecutionPermissionDecision`
  - `BudgetControlDecision`
  - `ComposedSleeveRoutingDecision`
  - enriched `StrategySleeveAutomationDecision`
  - enriched `StrategySleeveIntent.control_trace`

## Database schema / tables / indexes / constraints
- No new tables or indexes.
- Persist new fields through existing `StrategySleeveAutomationDecision` / `StrategySleeveIntent` payloads only.

## Transactions, Consistency, Concurrency
- No transactional changes.
- Decisions remain deterministic within a single coordinator evaluation cycle.

## Authorization, Authentication, Data Security
- No auth changes.
- New runtime fields contain control/audit metadata only.

## Error Handling and Idempotency
- Layered decisions are pure functions over current inputs.
- Re-running the same cycle with identical inputs should produce the same permission/budget/composition traces.

## State Transition and Lifecycle
- Permission deny and budget-zero suppression are recorded separately.
- Final route action remains compatible with existing allocator semantics in phase 1.

## Caching and Performance
- No new storage lookups beyond current reconciliation/PnL inputs.
- Added control traces are computed in-memory and attached to existing payloads.

## Logging, Monitoring, Auditing
- Startup guard remains explicit when non-protective auto execution is disabled.
- Runtime/operator summaries expose counts for permission-driven advisory-only and budget-zero suppression.
- Sleeve intent traces contain permission/budget/composition sections.

## Testing Strategy
- Add dedicated unit tests for permission, budget, and composer modules.
- Extend coordinator tests to verify permission-denied vs budget-zero semantics.
- Extend runtime/operator/UI integration tests to expose the new control traces and counters.

## Migration, Rollback, Compatibility
- Backward compatibility is preserved by keeping current route-action semantics.
- Rollback is straightforward: revert new modules and restore old auto-parallel logic.

## Configuration and Environment Isolation
- Continue using `strategy_sleeve_auto_parallel_enabled` in phase 1.
- Live/paper behavior differences remain controlled by existing runtime settings.

## Code Organization and Dependencies
- New modules live under `aats/services/strategy_engines/`.
- No new external dependencies.

## Documentation and Operations Manual
- Operators should be able to answer whether a no-order sample was denied by permission or suppressed by budget from runtime payloads alone.

## Deployment and Acceptance Criteria
- `auto_parallel.py` becomes a thin orchestration shell.
- `permission denied` and `budget zero suppressed` are distinguishable in runtime payloads, sleeve intents, and coordinator tests.
- Targeted unit/integration tests pass.
