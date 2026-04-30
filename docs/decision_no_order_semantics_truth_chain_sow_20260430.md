# Decision No-Order Semantics Truth Chain SOW - 2026-04-30

## Business Objectives and Boundaries

Extend the deployed no-order semantic equivalence truth surface from latest decision only to recent directional decisions. The objective is to prevent repeated no-order root oscillation from being treated as material progress unless an order, fill, health, or deploy condition actually changes.

This is read-only truth/reporting work. It must not change strategy selection, risk gates, execution behavior, provider behavior, symbols, venues, schemas, release gates, or runtime timeframe plumbing.

## Module Responsibilities and Domain Model

`scripts/runtime_truth_report.py` owns the runtime truth projection. The domain model remains:

- `portfolio_allocation_decisions`: decision and route-action truth.
- `execution_orders`: order surface truth.
- `execution_fills`: fill surface truth.
- Runtime truth artifacts: sanitized evidence for automation and operator review.

The new no-order semantic coverage belongs to `directional_episode_attribution_truth`.

## Input/Output Interfaces

Input remains the existing runtime truth DB probe and RDP microstructure probe. Output adds read-only fields:

- Per recent directional decision: `no_order_semantics`.
- Summary coverage: `directional_episode_attribution_truth.no_order_semantics`.
- Live facts projection: `directional_episode_no_order_*`.

No public API contract or database write interface changes.

## Database Schema / Tables / Indexes / Constraints

No schema, table, index, or constraint changes. The implementation reads existing decision/order/fill evidence only.

## Transactions, Consistency, Concurrency

No transactions are introduced. The report remains a point-in-time read-only snapshot. Consistency follows the existing runtime truth report behavior.

## Authorization, Authentication, Data Security

No credentials are read or printed directly. Runtime truth uses existing project entry points and sanitized output. Raw payloads remain excluded from emitted report surfaces.

## Error Handling and Idempotency

Classification is deterministic and idempotent for the same runtime truth input. Missing or non-no-order evidence returns explicit not-applicable or not-verified statuses instead of raising.

## State Transition and Lifecycle

No trading state transition changes. The lifecycle impact is automation state only: a recent no-order sequence can be classified as a stable non-executable regime rather than a material root change.

## Caching and Performance

No caching changes. The extra work is O(n) over the existing recent directional decision list, currently capped by the report query.

## Logging, Monitoring, Auditing

Runtime truth gains audit fields for recent no-order semantic coverage, stable equivalence-class count, and materiality flags.

## Testing Strategy

Focused unit tests cover:

- Advisory/hold-current recent decisions classified as stable no-order semantics.
- Executable decisions without order surface remain material truth-chain gaps.
- Live runtime facts project the no-order semantic summary.

Full unit validation remains required before deployment.

## Migration, Rollback, Compatibility

No migration is required. Rollback is a normal git revert of the report/test/doc change plus redeploy. Existing consumers remain compatible because the change only adds fields.

## Configuration and Environment Isolation

No configuration changes. The scope remains OKX BTC-USDT-SWAP directional canary with `shadow_benchmark=none_verified`.

## Code Organization and Dependencies

Changes stay in `scripts/runtime_truth_report.py` and `tests/unit/scripts/test_runtime_truth_report.py`. No new dependencies.

## Documentation and Operations Manual

This SOW documents the bounded scope. Runtime truth artifacts remain the operational evidence surface.

## Deployment and Acceptance Criteria

Acceptance criteria:

1. Fresh runtime truth is generated before and after the change.
2. Recent no-order decisions expose per-decision semantics and summary coverage.
3. Executable decisions without orders remain flagged as missing order surface.
4. Focused and full unit tests pass.
5. Deployment, if performed, uses `scripts/deploy.sh --profile derivatives-live --skip-commit`.
