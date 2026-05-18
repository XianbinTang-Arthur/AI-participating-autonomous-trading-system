# Research Factory Workflow Operationalization SOW

Date: 2026-05-18

## Business Objectives and Boundaries

Turn the existing Research Factory governance workflow into a more operable evidence-review product. The scope is strictly research-only: no active parameter mutation, no runtime config write, no live order, no OKX write, and no production deployment behavior is introduced.

## Module Responsibilities and Domain Model

- `workflow.py` remains the orchestrator for real-data research, observation, pre-apply evidence, reference integrity, pre-apply review, registry memory, and operator-facing summaries.
- `ResearchGovernanceStageResult` provides a machine-readable result for each workflow stage.
- Operator checklist v2 provides readiness booleans and explicit allowed/forbidden next actions.

## Input and Output Interfaces

Inputs are unchanged:

- Explicit `research_profile`
- Gold replay experiment config
- Execution evidence summary
- Read-only shadow/paper observation summary

Outputs are additive:

- `workflow_summary.json` includes `stage_results`
- `operator_review_checklist.json` includes `readiness`, `allowed_next_actions`, and `forbidden_next_actions`
- `preapply_review_summary.md` uses operator-facing sections for candidate, data, execution, gates, pre-apply, novelty, followups, and non-authorization

## Database Schema / Tables / Indexes / Constraints

No database schema changes.

## Transactions, Consistency, Concurrency

No transaction semantics change. Workflow artifacts remain local atomic writes under `artifacts/research`.

## Authorization, Authentication, Data Security

The workflow continues to read only configured research inputs and write only research artifacts. It does not read `.env` files and does not touch live runtime or exchange write paths.

## Error Handling and Idempotency

Stage-level results preserve blocking failures and next debug actions so failed or blocked runs can be diagnosed without scanning every artifact. Existing workflow directory collision protection remains.

## State Transition and Lifecycle

Lifecycle remains:

```text
real_data_experiment -> observation -> observation_gate -> preapply_package
  -> reference_integrity -> preapply_review -> registry_memory -> workflow_summary
```

All stages explicitly carry `runtime_mutation_allowed=false`.

## Caching and Performance

No caching changes. Added outputs are small JSON/Markdown fields.

## Logging, Monitoring, Auditing

Stage results improve auditability by making stage status and blocking artifacts explicit. Checklist v2 makes allowed and forbidden next actions machine-readable.

## Testing Strategy

Unit tests verify:

- Successful workflows include stage results.
- Blocked observation workflows mark the observation gate stage as blocked.
- Failed experiment workflows record a failed stage result.
- Operator checklist v2 contains readiness booleans and forbidden runtime/apply actions.

## Migration, Rollback, Compatibility

Changes are additive to workflow summary and checklist payloads. Existing callers remain compatible.

## Configuration and Environment Isolation

No new configuration or environment variables.

## Code Organization and Dependencies

No new dependencies. Stage result modeling is kept in `workflow.py` to avoid broad refactors while preserving the current public workflow API.

## Documentation and Operations Manual

Operators should use `stage_results`, `readiness`, and `allowed_next_actions` / `forbidden_next_actions` as the first review surface before any separate manual apply design discussion.

## Deployment and Acceptance Criteria

Acceptance criteria:

- Workflow summary includes typed stage results.
- Operator checklist v2 includes all readiness booleans and explicit forbidden actions.
- Operator summary contains separate evidence/review sections and an explicit non-authorization statement.
- All outputs remain under `artifacts/research`.
- No active parameter write, live runtime write, OKX write, or auto-apply behavior exists.
