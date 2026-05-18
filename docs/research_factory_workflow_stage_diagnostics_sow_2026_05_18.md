# Research Factory Workflow Stage Diagnostics SOW

Date: 2026-05-18

## Business Objectives and Boundaries

Make the Research Factory governance workflow easier to diagnose and rerun by recording stage-level failure context. The work remains research-only: it does not mutate active parameters, runtime configuration, live orders, OKX state, or deployment state.

## Module Responsibilities and Domain Model

- `workflow.py` owns end-to-end research governance orchestration.
- Each workflow attempt records the current stage when failures occur.
- Workflow summaries identify blocking artifacts and next debug actions for operators.

## Input and Output Interfaces

Inputs are unchanged: real-data experiment configuration, explicit research profile, execution evidence summary, and read-only observation summary.

Outputs extend `workflow_summary.json`, `operator_review_checklist.json`, and `preapply_review_summary.md` with:

- `failed_stage`
- `blocking_artifact`
- `blocking_failures`
- `next_debug_action`

## Database Schema / Tables / Indexes / Constraints

No schema changes.

## Transactions, Consistency, Concurrency

No new database transactions. Workflow artifact writes remain local atomic writes under `artifacts/research`.

## Authorization, Authentication, Data Security

No new credential access. No `.env` reads. No runtime or exchange write path is introduced.

## Error Handling and Idempotency

Failures are categorized by workflow stage. Failed workflows write a summary and manifest with enough context to inspect the most relevant artifact first. Existing workflow directory overwrite protection is preserved.

## State Transition and Lifecycle

The workflow lifecycle remains:

```text
real_data_experiment -> load_research_artifacts -> observation -> observation_gate
  -> preapply_package -> reference_integrity -> preapply_review
  -> registry_memory -> workflow_summary
```

`preapply_ready` and `review_approved_for_manual_apply_design` remain evidence/review states, not apply authority.

## Caching and Performance

No caching changes. Added summary fields are small JSON/Markdown additions.

## Logging, Monitoring, Auditing

Stage diagnostics improve auditability by making failure location, blocking artifact, and debug action explicit.

## Testing Strategy

Add unit coverage for:

- Ready workflow summaries having no failed stage.
- Observation-gate-blocked workflows pointing at `observation_gate_result.json`.
- Failed experiment workflows recording `failed_stage=real_data_experiment`.

## Migration, Rollback, Compatibility

Existing workflow callers remain compatible. New summary fields are additive.

## Configuration and Environment Isolation

No new configuration.

## Code Organization and Dependencies

No new dependency is introduced.

## Documentation and Operations Manual

Operators should use `next_debug_action` and `blocking_artifact` as the first diagnostic pointer when a workflow is not `preapply_review_pending`.

## Deployment and Acceptance Criteria

Acceptance criteria:

- Failed workflows include `failed_stage`, `blocking_artifact`, `blocking_failures`, and `next_debug_action`.
- Non-ready workflows point to the stage artifact that blocked promotion.
- Operator summary/checklist surface the same diagnostic fields.
- All outputs remain under `artifacts/research`.
- No runtime mutation or active parameter write is introduced.
