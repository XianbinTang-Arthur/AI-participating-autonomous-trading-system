# Research Factory Observation Source, Profile Policy, and PreApply-aware Novelty SOW

## Business Objectives and Boundaries

Add three research-only capabilities:

1. Read-only Shadow/Paper observation data sources that turn existing observation summaries into `ObservationResult`.
2. A unified `ResearchProfile` policy surface for dataset quality, candidate gates, observation thresholds, and execution evidence policy.
3. Deeper NoveltyGate reasoning over preapply and preapply-review memory outcomes.

The work must not write runtime config, mutate active parameters, submit orders, call OKX write APIs, deploy, or introduce production Qlib/RD-Agent dependencies.

## Module Responsibilities and Domain Model

- `profiles.py`: owns named research profiles and stage thresholds.
- `observation_sources.py`: reads research artifact JSON summaries and builds observation facts.
- `registry.py`: uses preapply memory states in novelty decisions and reasons.
- `real_data.py`: optionally consumes a named research profile for dataset quality thresholds and candidate gates.

## Input/Output Interfaces

Observation summary input is a JSON research artifact under `artifacts/research` with mode, observation window, counts, fillability, cost, drawdown, and drift metrics.

Output is:

```text
ObservationResult
ResearchProfile
NoveltyGateResult with preapply-aware reasons
```

## Database Schema / Tables / Indexes / Constraints

No schema changes. Observation data sources are artifact-read-only.

## Transactions, Consistency, Concurrency

No transaction changes. Registry writes keep existing atomic JSONL behavior.

## Authorization, Authentication, Data Security

No env files or credentials are read. Observation source paths must stay under `artifacts/research`; source adapters are read-only and do not import runtime execution clients.

## Error Handling and Idempotency

Invalid profile names, unsafe observation source paths, identity mismatches, missing summary metrics, and unsupported mode mismatches fail closed.

## State Transition and Lifecycle

No live lifecycle transition. The artifact lifecycle remains:

```text
Recommendation -> ObservationResult -> ObservationGate -> ReviewOutcome -> PreApplyEvidence
```

## Caching and Performance

No caching changes. Summary reads are small JSON reads.

## Logging, Monitoring, Auditing

All produced objects carry schema/profile names and source refs for audit.

## Testing Strategy

Add focused unit tests for profiles, shadow/paper source parsing and read-only path rejection, and preapply-aware novelty reasons/suppression.

## Migration, Rollback, Compatibility

Additive contracts. Existing configs and runner defaults remain compatible unless a `research_profile` is explicitly provided.

## Configuration and Environment Isolation

No environment variables. Profiles are code-defined deterministic policy presets.

## Code Organization and Dependencies

Use existing Research Factory modules and standard library only.

## Documentation and Operations Manual

Profiles prevent smoke thresholds from drifting into review/preapply. Observation sources must be treated as evidence readers only.

## Deployment and Acceptance Criteria

Acceptance requires lint, focused tests, full unit tests, and narrow WSL2 RDP integration sanity. No deployment is required.
