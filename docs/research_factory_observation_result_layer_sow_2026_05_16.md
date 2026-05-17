# Research Factory Observation Result Layer SOW

## Business objectives and boundaries

Add a research-only observation artifact layer after `ResearchRecommendation` so a candidate can be evaluated in shadow or paper mode before any future pre-apply review. This work must not mutate active parameters, runtime configuration, live orders, OKX adapters, ledger, reconciliation, or production deployment paths.

## Module responsibilities and domain model

`aats.data_platform.research_factory.observations` owns `ObservationRun`, `ObservationResult`, `ReviewOutcome`, and `ObservationRecorder`. `ResearchRecommendation` remains the input evidence package. `ReviewOutcome` may mark a recommendation `eligible_for_preapply`, but it never authorizes apply.

## Input/output interfaces

Inputs are a ready-for-review `ResearchRecommendation` and later a validated shadow/paper `ObservationResult`. Outputs are stable JSON artifacts under `artifacts/research/research_factory/observations/<observation_id>/`: `observation_run.json`, `observation_result.json`, `review_outcome.json`, and `observation_manifest.json`.

## Database schema / tables / indexes / constraints

No database schema changes. This phase is file-artifact only.

## Transactions, consistency, concurrency

Artifacts are written with atomic JSON replacement. An observation cannot be started twice, cannot record a result before `running`, and cannot record a review outcome before a completed result.

## Authorization, authentication, data security

The layer does not read environment files, secrets, credentials, exchange APIs, or live runtime state. Identifiers and artifact refs reject path traversal and absolute paths. Review rationale and next-step text reject direct runtime/apply command terms.

## Error handling and idempotency

Invalid lifecycle transitions fail closed with `ValueError`. Existing observation directories are not overwritten. JSON payloads require timezone-aware timestamps and finite numeric values.

## State transition and lifecycle

The lifecycle is `planned -> running -> completed -> review outcome`. Manifest status maps to Research Factory status values: `pending`, `running`, and terminal `succeeded`.

## Caching and performance

No caching is introduced. Payloads are small JSON artifacts.

## Logging, monitoring, auditing

The observation manifest provides audit lineage through recommendation, candidate, experiment, mode, and output refs. No runtime monitoring hooks are added in this phase.

## Testing strategy

Add focused unit coverage for valid artifact creation, lifecycle ordering, runtime mutation rejection, `eligible_for_preapply` safety, and research artifact root enforcement.

## Migration, rollback, compatibility

No migration is required. Existing recommendation, real-data runner, registry, and experiment artifact contracts remain compatible.

## Configuration and environment isolation

Default output root stays under `artifacts/research`. The module has no environment variable dependency.

## Code organization and dependencies

The implementation uses only existing Research Factory helpers and the standard library. No Qlib, RD-Agent, exchange, database, or runtime service dependency is introduced.

## Documentation and operations manual

Operators should treat observation artifacts as evidence for review only. `eligible_for_preapply` means a separate pre-apply package may be prepared; it does not grant apply permission.

## Deployment and acceptance criteria

Acceptance requires unit tests for the observation lifecycle, `ruff check`, the Research Factory unit subset, the full unit suite, and the existing WSL2 integration sanity check. No production deployment is required for this artifact-only change.
