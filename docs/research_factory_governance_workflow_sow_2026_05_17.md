# Research Factory End-to-End Governance Workflow SOW

## Business Objectives and Boundaries

Build a one-command, research-only workflow that stitches existing Research Factory evidence artifacts into an auditable governance chain:

```text
real-data experiment
  -> observation source
  -> observation gate
  -> review outcome
  -> preapply evidence package
  -> evidence reference integrity
  -> preapply review pending
  -> registry memory
```

This workflow must not mutate active parameters, write runtime config, send orders, call OKX write APIs, deploy, or imply production approval.

## Module Responsibilities and Domain Model

- `workflow.py`: orchestrates the evidence chain and writes a workflow summary artifact.
- `preapply.py`: enforces that manual-apply-design approval requires an explicit passing reference-integrity report.
- `scripts/rdp_run_research_governance_review.py`: CLI wrapper for DB-backed real-data research plus read-only observation summary input.
- Existing modules remain authoritative for their own artifacts: `real_data.py`, `observations.py`, `preapply.py`, `integrity.py`, and `registry.py`.

## Input/Output Interfaces

Inputs:

- `ResearchFactoryExperimentConfig` with an explicit `research_profile`.
- Read-only observation summary JSON under `artifacts/research`.
- Gold replay data source.

Outputs:

- observation artifacts under `artifacts/research/research_factory/observations/`
- preapply package under `artifacts/research/research_factory/preapply/`
- preapply review under `artifacts/research/research_factory/preapply_reviews/`
- workflow summary under `artifacts/research/research_factory/workflows/`
- registry memory entries for observation and preapply outcomes

## Database Schema / Tables / Indexes / Constraints

No schema changes. The workflow reads Gold replay data through the existing read-only real-data runner and does not write DB state.

## Transactions, Consistency, Concurrency

Artifact writes use existing atomic JSON helpers. The workflow fails closed when an upstream step fails. Registry writes reuse atomic JSONL upsert behavior.

## Authorization, Authentication, Data Security

No env files are read by the workflow module. The CLI reads only the named DB URL environment variable and never prints secrets. Observation summaries must be under `artifacts/research`.

## Error Handling and Idempotency

Missing profile, failed real-data experiment, missing recommendation artifacts, unsafe observation summaries, failed identity checks, and failed reference integrity produce explicit workflow failures. Existing artifact directories are not overwritten unless the real-data experiment config explicitly allows overwrite.

## State Transition and Lifecycle

Successful ready path:

```text
recommendation_ready
  -> observation_eligible_for_preapply
  -> preapply_ready
  -> preapply_review_pending
```

Failure/continue paths produce `observation_keep_reviewing`, `observation_rejected`, `needs_more_observation`, or `preapply_rejected`; none authorize apply.

## Caching and Performance

No caching changes. Summary reads and artifact validation are small JSON operations.

## Logging, Monitoring, Auditing

The final `workflow_summary.json` captures IDs, refs, profile, gate status, integrity status, registry path, and next step.

## Testing Strategy

Add unit tests for:

- preapply review approval requiring explicit reference integrity ref and passed flag
- successful governance workflow
- missing profile failure
- failed observation gate producing non-ready preapply status

Run focused tests, lint, full unit tests, and the narrow WSL2 RDP/data-platform integration sanity test.

## Migration, Rollback, Compatibility

Additive workflow and CLI. Existing real-data runner behavior remains compatible. The review approval hardening is intentionally stricter and preserves the research/apply boundary.

## Configuration and Environment Isolation

Workflow runner requires explicit `research_profile`; no implicit real-data governance flow is allowed.

## Code Organization and Dependencies

Use only existing Research Factory modules and standard library. Do not add Qlib/RD-Agent runtime dependencies.

## Documentation and Operations Manual

Operators should treat `preapply_review_pending` as evidence ready for human review, not apply approval.

## Deployment and Acceptance Criteria

Acceptance requires:

- no profile -> fail
- missing or non-passing reference integrity -> no manual apply design approval
- failed observation gate -> no `preapply_ready`
- all artifacts under `artifacts/research`
- no live runtime writes
- tests and WSL2 integration sanity pass
