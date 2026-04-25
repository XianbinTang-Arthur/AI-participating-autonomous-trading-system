# Task240: Orderbook Snapshot-Ref Sequence Truth

## Business Objectives And Boundaries

Current execution-science truth exposes whether lifecycle stages have pre/post
orderbook snapshot refs, but it does not validate the ordering of those refs.
This task adds a minimal read-only truth primitive: for each decision lifecycle
stage with orderbook refs, parse the persisted snapshot ref timestamps and
report whether the pre-event ref is ordered before the post-event ref.

This is not full orderbook diff reconstruction. It must still expose missing
diff payload/checksum evidence until the collector or persistence layer stores
verifiable diff sequence data.

## Module Responsibilities And Domain Model

- `OperatorQueryService` owns the read-only operator truth surface.
- `lifecycle_snapshot_refs` remains the source of pre/post orderbook refs.
- Bronze orderbook tables remain the source referenced by refs:
  `bronze.market_orderbook_books5` and `bronze.market_orderbook_bbo`.
- The domain distinction is explicit:
  - snapshot-ref ordering truth: pre timestamp <= post timestamp.
  - diff sequence truth: not yet available in the current ref format.

## Input / Output Interfaces

Input:
- Decision-scoped order/fill payloads.
- `lifecycle_snapshot_refs.<stage>.market_context_snapshot_refs`.
- Existing ref format:
  `<optional_source.>bronze.market_orderbook_books5:<symbol>:<iso_ts>` or
  `<optional_source.>bronze.market_orderbook_bbo:<symbol>:<iso_ts>`.

Output:
- `truth_chain.execution_science.sequence_validation.status`.
- Per-stage `stage_evidence` with parsed pre/post ref metadata, ordering
  result, and missing evidence.

## Database Schema / Tables / Indexes / Constraints

No schema changes. The task only parses persisted ref strings already stored in
execution payload JSON.

## Transactions, Consistency, Concurrency

No transactions are opened by this task. It runs in the existing read path and
does not mutate state.

## Authorization, Authentication, Data Security

No credentials, DB URLs, API keys, or tokens are read or printed. The output
contains only existing snapshot ref identifiers and parsed metadata.

## Error Handling And Idempotency

Malformed refs are reported as explicit `unparseable_ref` /
`unsupported_table` / `unparseable_ts` evidence. The read path stays fail-soft
and deterministic.

## State Transition And Lifecycle

No order state transitions are changed. Submit/ack/fill lifecycle stages are
only inspected.

## Caching And Performance

No additional database query is added. The parser works on payloads already
loaded for the decision truth chain.

## Logging, Monitoring, Auditing

No new logs are required. The operator API response is the audit surface.

## Testing Strategy

Focused unit tests cover:
- Complete orderbook context with valid pre/post ref ordering.
- Missing orderbook refs.
- Invalid pre/post timestamp order.
- Clean no-execution decisions.

## Migration, Rollback, Compatibility

No migration. Rollback is `git revert` of the code and test change, then
standard deploy.

## Configuration And Environment Isolation

No config or environment changes.

## Code Organization And Dependencies

The parser stays local to `OperatorQueryService` to avoid introducing a
shared dependency for a read-only truth-surface concern.

## Documentation And Operations Manual

Operators should read `sequence_validation.status` as:
- `snapshot_ref_sequence_validated_diff_missing`: refs are ordered, but diff
  payload/checksum truth is still missing.
- `invalid_snapshot_ref_sequence`: pre/post refs violate expected time order.
- `missing_orderbook_refs`: lifecycle exists but pre/post orderbook refs are
  absent.

## Deployment And Acceptance Criteria

Acceptance:
- Focused lint and unit tests pass.
- Full lint and unit tests pass before deployment.
- Deploy uses `scripts/deploy.sh`.
- Post-deploy runtime truth has no blocking findings.
- No strategy, risk, execution, provider, schema, symbol, venue, release,
  promotion, tuning, or live order behavior changes.
