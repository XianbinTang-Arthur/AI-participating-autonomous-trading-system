# Task238 Decision Truth Chain Join Keys SOW

## Business Objective And Boundaries
- Expose a machine-readable decision truth chain in the operator read API so each decision can state whether its provenance, order, fill, and lifecycle evidence is linked, absent, or missing.
- Increase P1 truth-chain density for OKX + BTC-USDT-SWAP without changing strategy logic, risk gates, execution behavior, AI provider behavior, schema, symbol, venue, strategy family, release, promotion, or tuning.

## Module Responsibilities And Domain Model
- `aats.services.operator.query_service.OperatorQueryService` owns the read-side truth-chain projection.
- Decision audit records remain the source of provenance refs.
- Execution repositories remain the source of order/fill join-key evidence.
- Lifecycle refs remain payload metadata surfaced from existing order/fill payloads.

## Input / Output Interfaces
- Input:
  - `DecisionAuditRecord` refs.
  - Scoped execution order states matching `decision_id`.
  - Scoped fill events matching `decision_id`.
  - Existing lifecycle snapshot refs in order/fill payloads.
- Output:
  - `decision_view(...).truth_chain` with:
    - `overall_status` and `complete`.
    - `missing_evidence`.
    - `provenance`, `order`, `fill`, and `lifecycle` sections.
    - Explicit `absent_*` states for clean no-execution decisions.
    - Explicit `missing_*` states for expected links that are not present.

## Database Schema / Tables / Indexes / Constraints
- No schema change.
- No migration.
- Uses existing indexed decision/order/fill data through existing repositories.

## Transactions, Consistency, Concurrency
- Read-only query projection.
- No transaction boundary, lock, or idempotency change.

## Authorization, Authentication, Data Security
- No credential handling.
- No secret, token, API key, DB password, or full connection string output.
- Output contains join-key ids and internal payload reference ids only.

## Error Handling And Idempotency
- Missing execution repo returns `unknown_lookup_failed` with a bounded reason code.
- Lookup exceptions are reduced to exception class names only; raw error text is not exposed.
- Missing order/fill/lifecycle evidence is encoded as machine-readable status, not inferred as success.

## Caching And Performance
- Adds one decision-scoped order lookup and one decision-scoped fill lookup when building a decision detail.
- Uses indexed `decision_id` repository paths where available; no full order/fill table scan on the production Postgres path.
- No new polling loop or write path.

## Testing Strategy
- Unit tests cover:
  - Linked order/fill/lifecycle evidence.
  - Clean no-execution decisions.
  - Order intent without order state.
  - Provenance gaps.
  - In-memory order lookup by `decision_id`.

## Rollback
- Revert the read-side query change and its tests.
- No persisted data or schema requires rollback.

## Acceptance Criteria
- `decision_view` includes `truth_chain`.
- `truth_chain` distinguishes `linked`, `absent_*`, `missing_*`, and provenance `partial`.
- Focused unit tests pass.
- Ruff passes for touched Python files.
