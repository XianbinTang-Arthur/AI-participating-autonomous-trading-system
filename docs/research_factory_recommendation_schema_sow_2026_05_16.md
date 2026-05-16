# Research Factory Recommendation Schema SOW - 2026-05-16

## Business Objectives and Boundaries

Move Research Factory from candidate-only artifact generation to a research-only recommendation evidence package. A recommendation must summarize why a passing `CandidateArtifact` deserves review, what evidence supports it, how it should be observed in shadow or paper mode, and how it can be rolled back if it later advances outside Research Factory.

This work does not apply parameters, mutate active runtime configuration, submit orders, call OKX, deploy services, or introduce Qlib/RD-Agent runtime dependencies.

## Module Responsibilities and Domain Model

Research Factory remains responsible for deterministic research artifacts:

- `CandidateArtifact`: passing research candidate produced by candidate gate.
- `PreApplyEvidence`: evidence bundle linking metrics, gate result, dataset fingerprint, and artifact refs.
- `ObservationPlan`: shadow or paper observation expectations before any runtime promotion is considered.
- `RollbackPlan`: required rollback evidence and operator actions for a future promotion workflow.
- `ResearchRecommendation`: research-only review package that binds candidate, evidence, observation plan, rollback plan, and approval requirements.

The recommendation is not a runtime command and cannot be consumed as an active parameter set.

## Input/Output Interfaces

Inputs:

- Existing `CandidateArtifact`.
- Existing `MetricsSnapshot`.
- Existing `CandidateGateResult`.
- Relative artifact refs for metrics, candidate, experiment manifest, and optional execution realism summaries.

Outputs:

- `research_recommendation.json` under the experiment artifact directory.
- Manifest output ref `research_recommendation`.
- Smoke runner result field `recommendation_ref` when a recommendation is produced.

## Database Schema / Tables / Indexes / Constraints

No database schema changes. Recommendation artifacts are stable JSON files under `artifacts/research`.

## Transactions, Consistency, Concurrency

Artifact writes use the existing recorder atomic JSON writer. `record_recommendation()` must only run before the experiment reaches a terminal status and after metrics and candidate artifacts have been recorded.

Concurrent writes to the same experiment directory remain unsupported, matching the current recorder contract.

## Authorization, Authentication, Data Security

No credentials are read. No `.env` files are accessed. No live exchange, account, order, or private websocket APIs are imported or called.

Recommendation refs must be relative, must not contain path traversal, and must not point at absolute filesystem paths.

## Error Handling and Idempotency

Invalid schema inputs raise `ValueError` before writing artifacts. Re-running the smoke runner with `overwrite=True` remains deterministic and replaces the prior experiment directory after verifying the path is still inside the research artifact root.

## State Transition and Lifecycle

Lifecycle:

`metrics_snapshot -> candidate_artifact -> research_recommendation -> terminal experiment manifest`

The recommendation can be `draft`, `ready_for_review`, or `rejected`. It cannot mark itself as approved for runtime mutation.

## Caching and Performance

No caching changes. The schema validation is in-memory and bounded by the small recommendation payload size.

## Logging, Monitoring, Auditing

The recommendation artifact is audit evidence. It includes schema version, created timestamp, candidate id, experiment id, gate status, artifact refs, observation plan, rollback plan, and explicit research-only flags.

## Testing Strategy

Add unit tests covering:

- Recommendation schema accepts a passing candidate and writes stable JSON.
- Recommendation rejects runtime mutation flags.
- Evidence refs reject absolute paths and traversal.
- Recorder writes recommendation after candidate and before terminal status only.
- Smoke runner emits `research_recommendation.json` and manifest ref.

## Migration, Rollback, Compatibility

This is additive. Existing candidate artifacts and experiment manifests remain compatible. Rollback is deleting the new recommendation module, recorder method, smoke runner hook, and associated tests.

## Configuration and Environment Isolation

No new configuration files. No environment variables are read. Generated artifacts remain below `artifacts/research`.

## Code Organization and Dependencies

Add a Research Factory recommendation schema module and extend the existing experiment recorder. No third-party dependencies are introduced.

## Documentation and Operations Manual

This SOW is the operational reference for Work Package 3. Operators should treat recommendations as review evidence only; a separate governance workflow is required before any shadow, paper, or active runtime promotion.

## Deployment and Acceptance Criteria

Acceptance requires:

- Unit tests for the recommendation schema, recorder integration, and smoke runner pass.
- `ruff check aats/ --fix` passes.
- Full unit suite passes.
- Narrow WSL2 integration smoke remains green.
- No imports or writes touch live runtime, OKX, ledger, reconciliation, or active parameter apply paths.
