# Task219 Lifecycle Snapshot Refs Completeness SOW

## Business Objectives And Boundaries
- Add a machine-readable completeness summary for existing `lifecycle_snapshot_refs` in operator execution record payloads.
- Make lifecycle truth density auditable without requiring callers to infer missing refs manually.
- Keep the task read-side only: no strategy, symbol, venue, AI autonomy, tuning, release, promotion, execution behavior, or runtime timeframe changes.

## Module Responsibilities And Domain Model
- `aats.services.operator.query_service.OperatorQueryService` remains the control-plane read-side normalization layer.
- Existing lifecycle stages remain owned by prior write-side work:
  - `submit`
  - `ack`
  - `fill`
- This task only derives completeness from already persisted lifecycle payloads.

## Input / Output Interfaces
- Input: normalized `lifecycle_snapshot_refs` dict from execution order/fill raw payloads.
- Output: `lifecycle_snapshot_refs_completeness` in execution record payloads.
- Output shape:
  - `has_lifecycle_snapshot_refs`
  - `present_stages`
  - `complete_stages`
  - `incomplete_stages`
  - `missing_snapshot_refs_by_stage`
  - `all_present_stages_complete`

## Database Schema / Tables / Indexes / Constraints
- No schema changes.
- No migrations.
- No new indexes.

## Transactions, Consistency, Concurrency
- Read-only in-memory transformation.
- No transaction or concurrency changes.

## Authorization, Authentication, Data Security
- No credential handling.
- No secret output.
- Exposes only completeness of existing snapshot reference ids.

## Error Handling And Idempotency
- Missing or malformed lifecycle payload returns an empty completeness summary instead of raising.
- Non-dict lifecycle stages are ignored by the existing normalizer and therefore do not enter completeness.
- Empty string refs are treated as missing.

## State Transition And Lifecycle
- Does not create or alter lifecycle stages.
- Does not mutate order or fill state machines.

## Caching And Performance
- Constant-time per execution record relative to stage count.
- No additional database calls or cache writes.

## Logging, Monitoring, Auditing
- No new logs.
- Audit improvement is a deterministic per-record lifecycle completeness payload suitable for UI/API review and future truth-density aggregation.

## Testing Strategy
- Unit tests cover:
  - fully complete lifecycle refs.
  - incomplete stage with a missing snapshot ref.
  - absent lifecycle refs.
  - stage ordering remains stable for `submit`, `ack`, and `fill`.

## Migration, Rollback, Compatibility
- Backward-compatible payload field addition.
- Rollback by reverting the query-service helper and tests.

## Configuration And Environment Isolation
- No configuration changes.
- No environment variable changes.

## Code Organization And Dependencies
- Reuse `SNAPSHOT_REF_KEYS` from `aats.services.execution_engine.lifecycle_snapshot_refs`.
- No new third-party dependencies.

## Documentation And Operations Manual
- This SOW is the operational record for the bounded task.
- Downstream reviewers should treat `lifecycle_snapshot_refs_completeness` as read-side evidence quality metadata, not as fill feasibility evidence.

## Deployment And Acceptance Criteria
- `tests/unit/test_operator_execution_record_payload_truth_exposure.py` passes.
- Ruff passes for touched Python files.
- `lifecycle_snapshot_refs_completeness` is present for dict execution record payloads.
- No release, promotion, tuning, symbol, venue, strategy-family, AI autonomy, or timeframe changes.
