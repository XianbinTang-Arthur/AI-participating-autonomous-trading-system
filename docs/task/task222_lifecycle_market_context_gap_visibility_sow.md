# Task222: Lifecycle Market Context Gap Visibility

## Business Objectives And Boundaries

Expose submit / ack / fill lifecycle market-context gaps as machine-readable truth.
The objective is not to capture local order book snapshots in this task. The objective is to make the missing pre/post orderbook linkage explicit in persisted lifecycle payloads and operator query responses, so PM Loop can route the next P1/P2 work from facts rather than absent fields.

In scope:
- Normalize lifecycle market-context refs for each lifecycle stage.
- Surface per-stage completeness for pre/post orderbook context.
- Preserve existing decision-time snapshot-ref completeness semantics.
- Keep the fixed live scope: OKX + BTC-USDT-SWAP, independent carrier.

Out of scope:
- No new strategy family, symbol, venue, or timeframe plumbing.
- No local order book reconstruction.
- No new database columns, tables, migrations, or indexes.
- No release, promotion, tuning, or AI authority expansion.

## Module Responsibilities And Domain Model

- `aats.services.execution_engine.lifecycle_snapshot_refs` owns lifecycle payload normalization.
- `aats.services.operator.query_service` owns control-plane exposure for order/fill records.
- Existing lifecycle stages remain `submit`, `ack`, `fill`, plus backward-compatible support for any future stage names.
- New market-context refs are treated separately from existing decision-time refs:
  - `pre_event_orderbook_snapshot_ref`
  - `post_event_orderbook_snapshot_ref`

## Input / Output Interfaces

Input:
- Existing raw payloads containing `lifecycle_snapshot_refs`.
- New raw payloads generated through `lifecycle_snapshot_ref_payload`.

Output:
- Each normalized lifecycle stage includes `market_context_snapshot_refs`.
- Operator payloads include `lifecycle_market_context_completeness`.
- Existing `lifecycle_snapshot_refs_completeness` remains unchanged and still describes the four decision-time refs.

## Database Schema / Tables / Indexes / Constraints

No schema change. Data remains in existing JSON payloads.

## Transactions, Consistency, Concurrency

The change is pure payload normalization and query projection. It does not alter transaction boundaries, locking, idempotency keys, or order-state transitions.

## Authorization, Authentication, Data Security

No secret, credential, or raw environment value is exposed. The new fields are internal snapshot references only.

## Error Handling And Idempotency

Malformed or non-dict lifecycle payloads remain tolerated and return `None` / empty completeness. Missing market-context refs are reported as `missing`, not treated as runtime errors.

## State Transition And Lifecycle

Existing stage semantics are preserved. The new completeness layer distinguishes:
- decision-time refs complete/incomplete
- event-time orderbook context complete/incomplete

## Caching And Performance

The change adds small dictionary normalization only. No DB query count or index requirement changes.

## Logging, Monitoring, Auditing

No new logs are required. The audit value comes from control-plane payload fields and future artifacts reading these fields.

## Testing Strategy

Unit coverage:
- Lifecycle helper defaults market-context refs to explicit missing.
- Operator payload exposes missing market-context refs per stage.
- Operator payload marks a stage complete when both pre/post orderbook refs are present.
- Existing decision-time completeness behavior remains unchanged.

## Migration, Rollback, Compatibility

Backward compatible:
- Old records without market-context refs continue to parse.
- Existing response fields are preserved.
- New fields are additive.

Rollback:
- Revert this task's code and tests.
- Existing persisted JSON containing `market_context_snapshot_refs` is harmless to older readers because it is nested additive data.

## Configuration And Environment Isolation

No environment variable or runtime config change.

## Code Organization And Dependencies

No new dependency. Reuse the existing lifecycle snapshot helper module.

## Documentation And Operations Manual

This SOW documents the scope. Operationally, use the new `lifecycle_market_context_completeness` payload to route the next P1/P2 task.

## Deployment And Acceptance Criteria

Acceptance criteria:
- New lifecycle stage payloads include `market_context_snapshot_refs` with explicit pre/post orderbook refs set to `None` when absent.
- Operator query payloads include `lifecycle_market_context_completeness`.
- Existing `lifecycle_snapshot_refs_completeness` tests still pass.
- Relevant unit tests pass.
