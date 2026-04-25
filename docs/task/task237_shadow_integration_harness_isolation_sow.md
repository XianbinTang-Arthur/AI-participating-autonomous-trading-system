# Task237 Shadow Integration Harness Isolation SOW

## Business Objectives And Boundaries
Validate Task236 without allowing integration-test background decision triggers to distort the result. The goal is clean evidence for AI shadow fail-soft behavior, not a production behavior change.

## Module Responsibilities And Domain Model
- `tests/integration/test_operator_api.py`: isolate targeted tests that manually call `DecisionEngine.run_cycle`.
- `DecisionCycleTrigger`: production background trigger remains unchanged.
- `EventStore`: remains the evidence source for `POSITION_TARGETS`, `AI_SHADOW_EVALUATIONS`, and `AI_PERFORMANCE_REPORTS`.

## Input/Output Interfaces
Input is the existing operator integration runtime fixture. Output is a deterministic test result for the two shadow fail-soft cases.

## Database Schema / Tables / Indexes / Constraints
No database schema, table, index, or constraint changes.

## Transactions, Consistency, Concurrency
The fix addresses test concurrency only: local market snapshot publication can enqueue background decision cycles before the test manually calls `run_cycle`.

## Authorization, Authentication, Data Security
No auth, secret, token, or credential handling changes. Do not read or print runtime env files.

## Error Handling And Idempotency
Stopping the decision trigger dispatcher in the test is idempotent and scoped to the per-test in-memory runtime.

## State Transition And Lifecycle
The test runtime still builds normally, publishes local market data, then disables the background dispatcher before manual cycle assertions.

## Caching And Performance
No cache changes. Test runtime becomes less flaky because it avoids extra background AI/provider calls.

## Logging, Monitoring, Auditing
No production logging/auditing changes. Test evidence remains EventStore-based.

## Testing Strategy
Run the two targeted WSL integration cases first, then run focused AI inference unit coverage and ruff.

## Migration, Rollback, Compatibility
No migration. Roll back by reverting this doc and the targeted test helper/assertion changes.

## Configuration And Environment Isolation
No runtime configuration change. The isolation is test-local and does not alter production `DecisionCycleTrigger` semantics.

## Code Organization And Dependencies
Keep changes inside the existing integration test file. Add no dependencies.

## Documentation And Operations Manual
This SOW documents why the integration harness is isolated before Task236 commit/deploy readiness can be judged.

## Deployment And Acceptance Criteria
No deployment in this bounded task. Acceptance is binary: the two targeted WSL integration tests pass without weakening production guardrails or changing strategy/risk/execution behavior.
