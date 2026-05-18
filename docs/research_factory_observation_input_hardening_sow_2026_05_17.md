# Research Factory Observation Input Hardening SOW

Date: 2026-05-17

## Business Objectives and Boundaries

Harden the read-only observation summary input used by Research Factory governance workflows. The work remains research-only: it may produce evidence, observation results, preapply packages, reviews, and registry memory, but it must not mutate live runtime state, active parameters, orders, or OKX state.

## Module Responsibilities and Domain Model

- `observation_sources.py` reads shadow/paper observation summary artifacts and converts them into `ObservationResult`.
- Observation summaries are fact inputs, not governance decision inputs.
- `ReviewOutcome` and preapply review logic remain responsible for governance decisions.

## Input and Output Interfaces

- Input: `research_observation_summary_v1` JSON under `artifacts/research`.
- Required identity fields: `recommendation_id`, `candidate_id`, and `experiment_id`.
- Output: `ObservationResult` with a neutral default `review_decision="keep_reviewing"` unless the workflow explicitly provides a decision.

## Database Schema / Tables / Indexes / Constraints

No database schema changes.

## Transactions, Consistency, Concurrency

No transactional writes are introduced. The adapter remains a local artifact reader.

## Authorization, Authentication, Data Security

Observation summary paths remain constrained to `artifacts/research`. No env files, credentials, runtime configs, or live trading paths are read.

## Error Handling and Idempotency

Missing or mismatched identity fields fail closed before an `ObservationResult` is created. Re-reading the same valid summary produces deterministic observation facts except for caller-provided timestamps.

## State Transition and Lifecycle

The change preserves the existing lifecycle:

```text
ResearchRecommendation -> ObservationResult -> ObservationGateResult -> ReviewOutcome
```

Observation summaries no longer supply governance decisions by default.

## Caching and Performance

No caching or performance-sensitive paths are changed.

## Logging, Monitoring, Auditing

Identity validation improves auditability by preventing an observation summary from being attached to the wrong recommendation.

## Testing Strategy

Add unit coverage for missing summary identity and for ignoring `suggested_review_decision` unless a caller explicitly supplies `review_decision`.

## Migration, Rollback, Compatibility

Existing summaries used by governance workflows must include `recommendation_id`, `candidate_id`, and `experiment_id`. A compatibility CLI alias is added at `scripts/rdp_run_research_governance_workflow.py` while preserving the existing review runner.

## Configuration and Environment Isolation

No new configuration or environment reads are introduced.

## Code Organization and Dependencies

No new runtime dependency is introduced.

## Documentation and Operations Manual

Operators should treat observation summaries as fact-only inputs. Gate and review artifacts remain the source of governance decisions.

## Deployment and Acceptance Criteria

Acceptance requires focused Research Factory tests, full unit tests, and the narrow WSL2 RDP integration sanity check to pass.
