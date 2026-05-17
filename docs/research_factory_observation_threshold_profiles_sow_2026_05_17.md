# Research Factory Observation Threshold Profiles SOW

## Business objectives and boundaries

Make observation review gates stage-aware so smoke, shadow review, paper review, and pre-apply review cannot accidentally share one threshold set. This remains research-only and must not mutate active parameters, runtime configuration, live orders, OKX adapters, ledger, reconciliation, or production deployment paths.

## Module responsibilities and domain model

`aats.data_platform.research_factory.observations` owns `ObservationThresholdProfile`, profile name validation, and conversion from a profile name into `ObservationThresholds`. The observation gate still produces `ObservationGateResult`; profiles only select deterministic threshold values.

## Input/output interfaces

Inputs are an observation result, observation run, and either explicit `ObservationThresholds` or a named threshold profile. Output remains `ObservationGateResult` with resolved threshold values and the profile name recorded in its threshold mapping.

## Database schema / tables / indexes / constraints

No database schema changes. This phase is file-artifact and in-memory contract only.

## Transactions, Consistency, Concurrency

No new transaction path is introduced. Observation artifact writes keep the existing atomic JSON write behavior.

## Authorization, Authentication, Data Security

No environment files, credentials, exchange APIs, live runtime state, or production configs are read. Profile selection is static code configuration and does not authorize runtime promotion.

## Error Handling and Idempotency

Unknown profile names fail closed. Passing both explicit thresholds and a named profile fails closed to avoid ambiguous gate semantics. Re-running the same observation gate with the same profile is deterministic.

## State Transition and Lifecycle

Profiles do not create new lifecycle states. They only parameterize the existing `ObservationResult -> ObservationGateResult -> ReviewOutcome` flow.

## Caching and Performance

No caching is introduced. Threshold profile resolution is constant-time static mapping lookup.

## Logging, Monitoring, Auditing

`ObservationGateResult.thresholds` records the resolved `threshold_profile` so gate artifacts remain auditable.

## Testing Strategy

Unit tests cover stage-specific threshold ordering, named profile evaluation, profile object evaluation, invalid profile rejection, and mutual exclusion between explicit thresholds and named profiles.

## Migration, Rollback, Compatibility

Existing callers that pass explicit thresholds or no thresholds keep working. Rollback is a normal git revert because no schema or runtime state changes are introduced.

## Configuration and Environment Isolation

No new environment variable, YAML setting, or deployment configuration is added. Profile definitions are static research code contracts.

## Code Organization and Dependencies

The implementation stays in `observations.py` and uses existing Research Factory observation contracts plus the standard library. No Qlib, RD-Agent, database, exchange, or runtime service dependency is introduced.

## Documentation and Operations Manual

Use `smoke` only for artifact lifecycle tests, `shadow_review` for shadow observation review, `paper_review` for paper intent observation, and `preapply` for the strictest pre-apply evidence review gate. A passing preapply gate still does not authorize apply.

## Deployment and Acceptance Criteria

Acceptance requires focused observation unit tests, `ruff check`, the Research Factory unit subset, the full unit suite, and the existing WSL2 RDP integration sanity check. No production deployment is required.
