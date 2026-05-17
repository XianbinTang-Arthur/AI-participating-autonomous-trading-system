# Research Factory Observation Gate SOW

## Business objectives and boundaries

Add a deterministic gate between `ObservationResult` and `ReviewOutcome` so a recommendation cannot become `eligible_for_preapply` from observed metrics alone. This remains research-only evidence generation and must not mutate active parameters, runtime configuration, live orders, OKX adapters, ledger, reconciliation, or production deployment paths.

## Module responsibilities and domain model

`aats.data_platform.research_factory.observations` owns `ObservationThresholds`, `ObservationGateResult`, `evaluate_observation_gate()`, and `ObservationRecorder.record_gate_result()`. `ObservationResult` remains the observed fact payload. `ReviewOutcome` remains the governance judgment, but `eligible_for_preapply` must be backed by a passing observation gate.

## Input/output interfaces

Inputs are a completed or running `ObservationRun`, an `ObservationResult`, and optional `ObservationThresholds`. Outputs are stable JSON artifacts under the observation directory: `observation_gate_result.json` plus the existing observation manifest output ref.

## Database schema / tables / indexes / constraints

No database schema changes. This phase is file-artifact only.

## Transactions, Consistency, Concurrency

Artifacts are written atomically. A gate result can only be recorded after `observation_result.json` exists. A review outcome can only be recorded after `observation_gate_result.json` exists.

## Authorization, Authentication, Data Security

No environment files, credentials, exchange APIs, live runtime state, or production configs are read. Gate artifacts stay under `artifacts/research`.

## Error Handling and Idempotency

Invalid threshold values, non-finite metrics, mismatched ids, missing observation result, missing gate result, or `eligible_for_preapply` without a passing gate fail closed.

## State Transition and Lifecycle

The lifecycle becomes `planned -> running -> completed result -> gate result -> review outcome`. Failed gates can support `reject` or `keep_reviewing`, but not `eligible_for_preapply`.

## Caching and Performance

No caching is introduced. The gate is an in-memory deterministic check over a small metrics payload.

## Logging, Monitoring, Auditing

The observation manifest records `observation_gate_result` as an output ref. The gate result stores thresholds, failures, critical metrics, and evaluation time for auditability.

## Testing Strategy

Unit tests cover passing gates, insufficient sample keep-reviewing behavior, negative edge rejection, aborted observation rejection, recorder ordering, and the hard rule that `eligible_for_preapply` requires a passing gate.

## Migration, Rollback, Compatibility

Existing observation run/result artifacts remain valid. New review outcomes should include gate refs. Rollback is a normal git revert because no schema or runtime state is changed.

## Configuration and Environment Isolation

Default thresholds are code-level defaults for Research Factory observation review. No environment variable or deployment configuration is added.

## Code Organization and Dependencies

The implementation stays in `observations.py` and uses existing Research Factory helpers plus the standard library. No Qlib, RD-Agent, database, exchange, or runtime service dependency is introduced.

## Documentation and Operations Manual

Operators should treat `eligible_for_preapply` as permission to prepare a separate pre-apply evidence package only. It does not authorize apply.

## Deployment and Acceptance Criteria

Acceptance requires focused observation unit tests, `ruff check`, the Research Factory unit subset, the full unit suite, and the existing WSL2 RDP integration sanity check. No production deployment is required.
