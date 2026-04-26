# Directional Cost and Flat Hold Fix SoW

## Business Objectives and Boundaries

Fix two live directional strategy defects without changing strategy family routing: size-aware cost estimation must account for the actual target delta, and flat-signal hold must not bypass active position-management exits or reductions. Independent dual-book behavior is out of scope.

## Module Responsibilities and Domain Model

`DecisionContextBuilder` owns construction of the per-cycle context. `DecisionContext` may carry a non-persisted market snapshot for local cost estimation. `TargetPositionEngine` owns target sizing, entry qualification, position management, and expected cost/edge metadata.

## Input/Output Interfaces

Input remains the same for external callers. `DecisionContext` gets an optional `market_snapshot` field excluded from serialization. `PositionTarget` output fields stay compatible; `expected_cost_bps` and `expected_net_edge_bps` become based on the actionable target delta or the blocked candidate delta.

## Database Schema / Tables / Indexes / Constraints

No database schema, table, index, or constraint changes.

## Transactions, Consistency, Concurrency

The change is synchronous and per-decision-cycle. No shared mutable state, no new transactions, and no new concurrency behavior.

## Authorization, Authentication, Data Security

No credential access and no authorization changes. The local market snapshot field is excluded from event serialization to avoid publishing larger raw market payloads through decision-context events.

## Error Handling and Idempotency

Cost estimation retains existing fallback behavior when market depth is absent. No exception should escape the decision hot path from cost metadata enrichment.

## State Transition and Lifecycle

Directional target lifecycle is unchanged. Flat-signal hold is applied after position management and only when no active position-management guardrail requested exit/reduce.

## Caching and Performance

The market snapshot already exists in context building. Passing it through as a non-serialized in-memory field avoids additional reads. Cost estimation remains O(1) except for existing orderbook-depth calculations in `TradeCostService`.

## Logging, Monitoring, Auditing

No new log stream is required. `expected_cost_bps`, `expected_net_edge_bps`, and guardrail flags become more audit-correct for size-aware directional decisions.

## Testing Strategy

Add focused unit tests in `tests/unit/test_target_position_engine.py` for size-aware cost-gated entry and flat hold yielding to alpha-decay exit. Run narrow unit tests, ruff, then the full unit suite if feasible.

## Migration, Rollback, Compatibility

Backward compatible. Optional context field defaults to `None` and is excluded from serialized payloads. Rollback is a code revert only.

## Configuration and Environment Isolation

No live env files are read or changed. Existing strategy cost and flat-hold settings remain authoritative.

## Code Organization and Dependencies

Reuse existing `TradeCostService` and market schema. No new third-party dependencies.

## Documentation and Operations Manual

This SoW documents scope and acceptance. No operator manual changes are required because behavior follows existing config names.

## Deployment and Acceptance Criteria

Deploy through the standard repository deployment path only after tests pass. Acceptance criteria: directional entry cost gating receives side/quantity/notional/snapshot when available, cost-blocked entries remain blocked with meaningful cost metadata, and flat-signal hold no longer suppresses alpha-decay exits.
