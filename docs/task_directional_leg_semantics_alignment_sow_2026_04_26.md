# Directional Leg Semantics Alignment SOW - 2026-04-26

## Business Objectives And Boundaries

Objective: eliminate the derivatives hedge-mode semantic mismatch where valid directional `scale_in_*` intent is blocked as `okx_leg_action_mismatch_with_position_intent`.

This is a runtime-reliability fix. It does not change alpha generation, risk budgets, position sizing, symbol, venue, provider, strategy family, timeframe plumbing, release, promotion, or tuning.

## Module Responsibilities And Domain Model

- `OrderIntent.position_intent`: high-level lifecycle intent such as `open_long`, `scale_in_long`, or `reverse_to_short`.
- `leg_action`: exchange leg action required by OKX hedge mode: `open`, `reduce`, or `close`.
- `OKXExecutionAdapter`: verifies that high-level lifecycle intent is compatible with the explicit hedge-mode leg.
- `ExecutionPlanner`: preserves high-level execution action for audit while generating exchange-compatible legs.

## Input / Output Interfaces

Input: `OrderIntent` / `LegOrderIntent` for OKX derivatives long-short mode.

Output: unchanged schema; compatibility is resolved in code so valid scale-in and reversal legs are not blocked solely because their lifecycle intent is more specific than the exchange leg action.

## Database Schema / Tables / Indexes / Constraints

No schema, table, index, or constraint change.

## Transactions, Consistency, Concurrency

No transaction boundary change. This task does not solve duplicate concurrent decision convergence; it prevents false rejection at execution semantic validation.

## Authorization, Authentication, Data Security

No auth or credential handling change. No secrets are read or printed.

## Error Handling And Idempotency

Invalid side/posSide/action combinations remain rejected. Idempotency keys and command flow behavior are unchanged.

## State Transition And Lifecycle

`scale_in_long` and `scale_in_short` remain risk-increasing lifecycle states while mapping to OKX `open` legs. `reverse_to_*` remains compatible with the corresponding close/reduce old-side leg and open new-side leg.

## Caching And Performance

No cache behavior change. Compatibility checks are constant-time string checks.

## Logging, Monitoring, Auditing

Execution action now preserves the high-level position intent where available, improving audit classification for scale-in and reverse legs.

## Testing Strategy

Add unit coverage for:

- scale-in intent preserving `execution_action=scale_in` through leg planning and order intent conversion;
- compatibility helper accepting scale-in open legs and reverse close/open legs;
- incompatible open/side/posSide combinations still failing.

## Migration, Rollback, Compatibility

Backward compatible. Rollback by reverting the commit and redeploying via `bash scripts/deploy.sh --skip-commit`.

## Configuration And Environment Isolation

No config or environment change.

## Code Organization And Dependencies

Reuse `aats.schemas.execution` for shared semantics so planner and adapter do not encode divergent compatibility rules.

## Documentation And Operations Manual

This SOW is the operational record for the bounded task.

## Deployment And Acceptance Criteria

Acceptance:

- focused execution planner tests pass;
- `ruff check aats/ --fix` passes;
- full unit test suite passes;
- deploy through `scripts/deploy.sh --skip-commit`;
- post-deploy runtime truth reports no blocking findings.
