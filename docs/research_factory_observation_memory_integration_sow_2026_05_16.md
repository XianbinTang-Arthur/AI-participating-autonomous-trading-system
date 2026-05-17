# Research Factory Observation Memory Integration SOW

## Business objectives and boundaries

Record observation-stage outcomes in Research Factory memory so future research can learn from candidates that passed research but failed, drifted, or succeeded during shadow/paper observation. This is research-only memory and must not mutate active parameters, runtime configuration, live orders, OKX adapters, ledger, reconciliation, or production deployment paths.

## Module responsibilities and domain model

`aats.data_platform.research_factory.registry` owns observation memory status extensions and `build_observation_memory_entry()`. Observation memory links candidate identity, recommendation id, observation id, review decision, observation metrics, observation gate result, failure reasons, and artifact refs.

## Input/output interfaces

Inputs are `CandidateArtifact`, `ObservationResult`, `ObservationGateResult`, `ReviewOutcome`, creator metadata, and optional artifact refs. Output remains the existing JSONL registry under `artifacts/research/.../registry/research_memory.jsonl`.

## Database schema / tables / indexes / constraints

No database schema changes. This phase is file-artifact only.

## Transactions, Consistency, Concurrency

Registry writes keep the existing atomic JSONL replacement behavior. Entry ids are deterministic for candidate, observation id, and observation status, so repeated writes are idempotent.

## Authorization, Authentication, Data Security

No environment files, credentials, exchange APIs, live runtime state, or production configs are read. Registry paths remain constrained to `artifacts/research`; artifact refs must be relative and traversal-free. Failure text continues through existing redaction.

## Error Handling and Idempotency

Mismatched candidate, observation, gate, or review outcome ids fail closed. Invalid statuses and unsafe artifact refs fail closed.

## State Transition and Lifecycle

Observation decisions map to memory statuses: `keep_reviewing` -> `observation_keep_reviewing`, `reject` -> `observation_rejected`, and `eligible_for_preapply` -> `observation_eligible_for_preapply`.

## Caching and Performance

No caching is introduced. Registry similarity remains bounded by the existing registry search path.

## Logging, Monitoring, Auditing

The registry stores observation metrics, gate result details, failure reasons, created metadata, and artifact refs for auditability.

## Testing Strategy

Unit tests cover eligible observation memory, failed-gate failure reasons, identity mismatch rejection, similarity across observation statuses, JSONL persistence, and artifact path safety.

## Migration, Rollback, Compatibility

Existing registry entries remain readable because new fields default to empty values. Rollback is a normal git revert because no runtime or schema state changes.

## Configuration and Environment Isolation

No new environment variable or deployment configuration is added.

## Code Organization and Dependencies

The implementation stays in `registry.py` and uses existing Research Factory observation contracts plus the standard library. No Qlib, RD-Agent, database, exchange, or runtime service dependency is introduced.

## Documentation and Operations Manual

Operators can use observation memory to identify candidates that failed because of insufficient samples, negative executable edge, fillability, partial fills, drawdown, metric drift, or aborts. The memory entry does not authorize apply.

## Deployment and Acceptance Criteria

Acceptance requires focused registry unit tests, `ruff check`, the Research Factory unit subset, the full unit suite, and the existing WSL2 RDP integration sanity check. No production deployment is required.
