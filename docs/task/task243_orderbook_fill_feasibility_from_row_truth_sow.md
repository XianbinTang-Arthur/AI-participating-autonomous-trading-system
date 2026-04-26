# Task243 - Orderbook Fill Feasibility From Row Truth

## Business Objectives And Boundaries

Turn resolved local orderbook row truth into a read-only execution-science signal: for each decision/order/fill lifecycle stage with resolved books5/bbo rows, report whether the observed fill had enough level-1 quote evidence to support a top-of-book feasibility and slippage basis.

This task is not a strategy, risk, execution, provider, schema, collector, release, promotion, or tuning change. It must not affect live order behavior.

## Module Responsibilities And Domain Model

- `aats.services.execution_engine.orderbook_snapshot_refs`: expose a minimal top-of-book projection from already-resolved bronze bbo/books5 rows.
- `aats.services.operator.query_service`: consume sequence row evidence and fill records to expose read-only `execution_science.fill_feasibility`.
- Unit tests: prove feasible, no-fill, depth-limited, and missing-row paths remain explicit.

Domain terms:
- Snapshot row truth: exact referenced row exists and has checksum/sequence metadata.
- Top-of-book projection: bid/ask px/sz derived from persisted row level 1 fields.
- Fill feasibility: observational comparison between actual fill facts and resolved local book facts.

## Input / Output Interfaces

Input:
- Existing decision/order/fill lifecycle truth chain.
- Existing `sequence_validation.stage_evidence`.
- Resolved `pre` / `post` orderbook row payloads.
- Fill facts: side, price, quantity, optional notional.

Output:
- `execution_science.fill_feasibility.status`.
- Per-stage `fill_evidence` with side, expected price source, top-of-book quote, top-level size coverage, adverse slippage bps, and optional adverse cost estimate.
- Explicit missing evidence for absent rows, absent fills, unsupported sides, or incomplete fill/book facts.

## Database Schema / Tables / Indexes / Constraints

No schema change.

Read-only evidence ultimately references existing bronze orderbook tables:
- `bronze.market_orderbook_books5`
- `bronze.market_orderbook_bbo`

No indexes, constraints, migrations, or writes are introduced.

## Transactions, Consistency, Concurrency

The resolver continues to use existing read-only session behavior. The operator surface computes derived facts from immutable payloads and does not open write transactions.

No new locks, background jobs, or concurrent writers are introduced.

## Authorization, Authentication, Data Security

No credentials, tokens, database URLs, or API keys are logged or returned.

The exposed payload contains only non-secret market microstructure facts already present in persisted bronze rows and execution/fill records.

## Error Handling And Idempotency

All missing inputs fail soft:
- missing row truth -> `orderbook_row_truth_missing`
- missing fill -> `fill_absent_for_orderbook_stage`
- missing side/price/qty -> precise missing fact code
- missing top-of-book projection -> `top_of_book_projection_missing`

Repeated calls are idempotent because the payload is derived from existing persisted facts.

## State Transition And Lifecycle

This task does not change order, fill, portfolio, strategy, or runtime lifecycle state. It only adds another read-only truth surface nested under existing decision truth chain output.

## Caching And Performance

No new broad query is introduced. Feasibility uses already-collected sequence evidence and existing fill payloads within the current decision payload.

The top-of-book projection is computed in-process while resolving exact row refs.

## Logging, Monitoring, Auditing

No new log stream is required. The operator truth chain becomes the audit surface.

Reviewers should inspect `execution_science.fill_feasibility` for explicit missing evidence rather than assuming absent metrics are zero.

## Testing Strategy

Focused unit tests cover:
- resolved rows and fill where level-1 size covers the fill
- resolved rows without a fill
- resolved rows where level-1 size does not cover the fill
- missing orderbook row evidence
- checksum/top-of-book projection stability through existing row resolver tests

## Migration, Rollback, Compatibility

No migration is required.

Rollback:
1. Revert the query surface and resolver projection commit.
2. Redeploy with `scripts/deploy.sh --profile derivatives-live --skip-commit`.

Existing `sequence_validation` fields remain backward compatible.

## Configuration And Environment Isolation

No new configuration is introduced. Existing runtime scope remains OKX + BTC-USDT-SWAP.

## Code Organization And Dependencies

No new dependency is introduced.

Implementation stays in:
- `aats/services/execution_engine/orderbook_snapshot_refs.py`
- `aats/services/operator/query_service.py`
- `tests/unit/test_operator_decision_truth_chain.py`

## Documentation And Operations Manual

Operators should read `fill_feasibility.status` as execution evidence only. It is not promotion, profitability, or scale-up evidence.

When status is `fill_requires_depth_or_maker_context`, the result means level-1 evidence is insufficient; it does not prove the fill was impossible because deeper book or maker/passive context may explain it.

## Deployment And Acceptance Criteria

Acceptance:
- Focused lint and tests pass.
- Full unit suite passes.
- Deploy succeeds through `scripts/deploy.sh`.
- Runtime truth report returns no blocking findings.
- No strategy/risk/provider/scope/schema/live-order drift.
