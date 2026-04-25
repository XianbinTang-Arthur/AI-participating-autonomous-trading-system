# Task239 Decision Orderbook Context Truth Surface SOW

## Business Objective And Boundaries
- Add a machine-readable execution-science summary to the existing decision truth chain.
- The objective is to expose whether covered decisions have pre/post local orderbook context linked through lifecycle refs, or whether that evidence is explicitly missing.
- This task does not capture new orderbook data, change strategy, modify risk gates, alter execution behavior, change AI provider behavior, add symbols/venues/families, tune, promote, release, or change schema.

## Module Responsibilities And Domain Model
- `aats.services.operator.query_service.OperatorQueryService` owns the read-side projection.
- Existing lifecycle payloads remain the source of `pre_event_orderbook_snapshot_ref` and `post_event_orderbook_snapshot_ref`.
- Existing execution repositories remain the source of decision-scoped order/fill records.
- The new summary is nested under the decision truth chain and is advisory evidence only.

## Input / Output Interfaces
- Input:
  - Existing `decision_view(...).truth_chain` order/fill/lifecycle evidence.
  - Existing `lifecycle_snapshot_refs` payloads on order/fill records.
  - Existing lifecycle market-context completeness helper.
- Output:
  - `truth_chain.execution_science.orderbook_context`:
    - status: `linked`, `partial`, `missing_after_lifecycle_record`, `absent_no_lifecycle_record`, or `absent_no_execution_record`.
    - per-stage evidence with record kind/id, stage, status, and missing refs.
    - summary counts for total/complete/incomplete stages.
  - `truth_chain.execution_science.sequence_validation` explicitly reports `missing_not_implemented` until local book diff sequencing is available.

## Database Schema / Tables / Indexes / Constraints
- No schema changes.
- No migrations.
- No new indexes.

## Transactions, Consistency, Concurrency
- Pure read-side projection.
- No write path, transaction, lock, retry, or idempotency change.

## Authorization, Authentication, Data Security
- No credential handling.
- No secret, token, API key, DB password, full connection string, or raw environment value is exposed.
- Output contains internal snapshot refs and missing-evidence reason codes only.

## Error Handling And Idempotency
- Missing lifecycle refs are represented as explicit absent/missing states.
- Missing pre/post orderbook refs are represented as explicit missing refs.
- Malformed lifecycle payloads continue to degrade through existing normalization helpers.

## State Transition And Lifecycle
- Does not alter order/fill state machines.
- Does not create new lifecycle stages.

## Caching And Performance
- Reuses the existing decision truth-chain payload construction.
- Adds in-memory aggregation over already loaded decision-scoped order/fill payloads.
- No additional database queries beyond the deployed truth-chain lookup path.

## Logging, Monitoring, Auditing
- No new logs.
- Audit value comes from operator/API payloads that distinguish linked evidence from missing evidence.

## Testing Strategy
- Unit tests cover:
  - Complete pre/post orderbook context.
  - Missing pre/post orderbook context after lifecycle evidence exists.
  - Clean no-execution decisions.
  - Existing provenance/order/fill truth-chain behavior remains intact.

## Migration, Rollback, Compatibility
- Backward-compatible additive API field under `truth_chain`.
- Rollback by reverting this read-side change and tests.

## Configuration And Environment Isolation
- No config or environment changes.

## Code Organization And Dependencies
- Reuses `OperatorQueryService` lifecycle helper methods.
- No new dependencies.

## Documentation And Operations Manual
- This SOW is the operational reference for the bounded task.
- Future P2 work should replace `sequence_validation.missing_not_implemented` with real local book diff sequence evidence.

## Deployment And Acceptance Criteria
- `decision_view(...).truth_chain.execution_science.orderbook_context` distinguishes linked local-book evidence from explicit missing evidence.
- Focused tests pass.
- Full unit tests pass.
- Deployment via `scripts/deploy.sh` passes if using safe-readonly fast lane.
- Runtime truth report returns no blockers after deployment.
