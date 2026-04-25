# Pre-Order Feasibility Truth Surface SOW

## Business Objectives And Boundaries

Expose a read-only pre-order feasibility truth surface for no-trade decisions. The operator should see which available evidence explains the absence of an order before submission: signal threshold, net edge, cost, book/liquidity, policy gate, and risk gate.

This task does not change strategy logic, thresholds, risk gates, order routing, AI provider behavior, symbols, venues, strategy families, release gates, promotion gates, or tuning paths.

## Module Responsibilities And Domain Model

- `aats.services.operator.query_service.OperatorQueryService` owns the API read model.
- `no_trade_classification.pre_order_feasibility` is a derived read-only payload based only on already recorded decision outcome, position target, policy decision, risk decision, sleeve intents, and independent book runtime states.
- `aats/api/static/modules/no-trade-display.js` owns Chinese operator copy and compact rendering helpers.
- `aats/api/static/modules/detail-drawers.js` consumes the helper through the existing no-trade card.

Domain model: each feasibility dimension has a stable key, status, evidence availability, per-leg evidence when applicable, and reason codes. Missing evidence is explicitly labeled unavailable.

## Input/Output Interfaces

Input: existing `/decision/latest`, `/decision/recent`, and `/decision/{id}` source payloads.

Output: additive `pre_order_feasibility` under `no_trade_classification`; existing fields remain backward compatible.

## Database Schema / Tables / Indexes / Constraints

No schema, table, index, or constraint changes.

## Transactions, Consistency, Concurrency

No writes are introduced. The payload is computed during existing read-model construction, so it follows current read consistency.

## Authorization, Authentication, Data Security

No auth changes. No credentials, tokens, database URLs, or secrets are read or emitted.

## Error Handling And Idempotency

Malformed or sparse evidence produces `unavailable` dimensions instead of inferred conclusions. The computation is deterministic for the same payload.

## State Transition And Lifecycle

No state transitions change. Order, fill, portfolio, policy, and risk lifecycle behavior is untouched.

## Caching And Performance

The surface is derived from small in-memory payload dictionaries already loaded by the operator query service. No new repository calls are introduced.

## Logging, Monitoring, Auditing

No new logs. The output improves operator auditability by exposing the evidence used for no-trade/pre-order attribution.

## Testing Strategy

- Unit tests for backend feasibility payload construction.
- Node-render UI tests for the decision drawer.
- Run focused tests first, then ruff and unit tests if time allows.

## Migration, Rollback, Compatibility

The API change is additive. Rollback is a normal git revert of this commit.

## Configuration And Environment Isolation

No config or environment changes.

## Code Organization And Dependencies

No new third-party dependencies. Keep helpers local to the existing query service and no-trade display module.

## Documentation And Operations Manual

This document is the task record. Operator behavior is visible through the existing decision drawer.

## Deployment And Acceptance Criteria

Acceptance:

- API read model exposes signal threshold, net edge, cost, book/liquidity, policy gate, and risk gate dimensions when evidence exists.
- Missing dimensions are marked unavailable, not inferred.
- Decision drawer renders the feasibility surface in UTF-8 Chinese.
- No live order behavior changes.
- Narrow validation passes.
