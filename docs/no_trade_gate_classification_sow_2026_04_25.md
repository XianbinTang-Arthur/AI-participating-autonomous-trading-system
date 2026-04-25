# No-Trade Gate Classification SoW

## Business objectives and boundaries
- Objective: make no-fill / no-order windows explainable without ad hoc database queries.
- Boundary: read-only operator/truth-chain diagnostics only; no strategy tuning, risk gate weakening, new symbol, venue, or family.

## Module responsibilities and domain model
- `OperatorQueryService` owns operator-facing decision detail and recent decision summaries.
- New domain concept: `no_trade_classification`, a machine-readable explanation for a decision that produced no order/fill.
- Classifications distinguish policy/risk blocks, independent signal/net-edge blocks, actionable decisions, and unknown no-trade states.

## Input/output interfaces
- Inputs: existing `decision_outcome`, `position_target`, `policy_decision`, `risk_decision`, `strategy_sleeve_intents`, and order/fill counts.
- Outputs: JSON payload attached to decision detail and recent decision summary.

## Database schema / tables / indexes / constraints
- No schema change. Uses existing event/audit payloads already loaded by the operator query layer.

## Transactions, consistency, concurrency
- No writes and no transactions. Classification is computed at query time from already persisted facts.

## Authorization, authentication, data security
- Exposed only through existing operator APIs and existing authorization boundaries.
- No secrets, tokens, database URLs, or credentials are read or emitted.

## Error handling and idempotency
- Missing or malformed payloads classify as `unknown_no_trade_blocker` instead of raising.
- Repeated calls are deterministic for the same input payloads.

## State transition and lifecycle
- No lifecycle mutation. The classifier explains a point-in-time decision.

## Caching and performance
- Uses payloads already fetched by `decision_view` / recent decision queries.
- No new database query is introduced.

## Logging, monitoring, auditing
- The emitted classification becomes part of operator/truth-chain observable evidence.

## Testing strategy
- Unit tests cover policy block, independent signal/net-edge block, actionable decision, and unknown no-trade fallback.

## Migration, rollback, compatibility
- Backward compatible additive JSON field.
- Rollback: revert this patch.

## Configuration and environment isolation
- No configuration or environment changes.

## Code organization and dependencies
- No new dependency. Helper remains in `aats/services/operator/query_service.py`.

## Documentation and operations manual
- This SoW records the behavior and boundaries for review.

## Deployment and acceptance criteria
- Acceptance: focused unit tests pass and the latest no-fill class can be represented as `no_executable_independent_legs_due_signal_and_net_edge_gates`.
