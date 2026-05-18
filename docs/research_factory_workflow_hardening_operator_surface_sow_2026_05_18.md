# Research Factory Workflow Hardening And Operator Review Surface SOW

Date: 2026-05-18

## Business Objectives and Boundaries

Make the Research Factory governance workflow safer to rerun, easier to diagnose, and usable by an operator review process. The workflow remains evidence-only and must not mutate active parameters, runtime configuration, live orders, OKX state, or production deployment state.

## Module Responsibilities and Domain Model

- `workflow.py` orchestrates research evidence, observation evidence, pre-apply evidence, reference integrity, registry memory, and workflow summaries.
- `observation_sources.py` treats shadow/paper observation summaries as fact inputs.
- `preapply.py` persists pre-apply review artifacts and associated reference-integrity evidence.
- Operator-facing files summarize the already-created evidence chain; they do not authorize runtime changes.

## Input and Output Interfaces

Inputs:

- Explicit `research_profile`.
- Real-data experiment config.
- Read-only observation summary under `artifacts/research`.
- Execution evidence summary under `artifacts/research`.

Outputs:

- `workflow_summary.json`
- `preapply_review_summary.md`
- `operator_review_checklist.json`
- Existing experiment, observation, preapply, review, integrity, and registry artifacts.

## Database Schema / Tables / Indexes / Constraints

No database schema changes. The workflow still uses read-only Gold replay access through the existing real-data runner.

## Transactions, Consistency, Concurrency

Reference integrity is written into the preapply review directory before the review manifest is finalized, reducing the chance of a manifest pointing to missing integrity evidence. Workflow failure ids include timestamp and content-derived entropy to avoid repeated default failure directory collisions.

## Authorization, Authentication, Data Security

The workflow writes only under `artifacts/research`. It does not read `.env` files, does not write runtime config, and does not call OKX write APIs.

## Error Handling and Idempotency

Smoke profile usage is rejected unless explicitly allowed. Failed workflows write a diagnostic summary with research-only status. Existing workflow directories are still protected from accidental overwrite.

## State Transition and Lifecycle

The lifecycle remains:

```text
Gold replay evidence -> Recommendation -> Observation -> Gate -> ReviewOutcome
  -> PreApplyEvidencePackage -> ReferenceIntegrity -> PreApplyReview
  -> Operator Review Surface
```

`preapply_ready` and `review_approved_for_manual_apply_design` remain evidence/review states, not apply authority.

## Caching and Performance

No cache changes. Operator summaries are small local artifacts.

## Logging, Monitoring, Auditing

`workflow_summary.json` now includes artifact refs, risk flags, and blocking failures. The operator checklist provides explicit evidence status and a no-runtime-mutation statement.

## Testing Strategy

Add unit coverage for smoke-profile rejection, operator summary/checklist artifacts, artifact refs, and preapply review manifest integrity output refs. Re-run focused Research Factory workflow/preapply tests, full unit tests, and the narrow WSL2 RDP integration sanity test.

## Migration, Rollback, Compatibility

Existing workflow callers using non-smoke profiles are compatible. Smoke-profile workflow callers must opt in with `allow_smoke_profile=True` or `--allow-smoke-profile`.

## Configuration and Environment Isolation

No new environment variables. The CLI exposes an explicit smoke-profile escape hatch for tests/smoke only.

## Code Organization and Dependencies

No new dependency is introduced. Operator review surface generation stays inside `research_factory.workflow`.

## Documentation and Operations Manual

Operators should read `preapply_review_summary.md` and `operator_review_checklist.json` before any separate manual apply design discussion. These files explicitly state that no runtime mutation is authorized.

## Deployment and Acceptance Criteria

Acceptance criteria:

- Missing or unintended smoke profile use fails closed.
- Workflow summary includes all major artifact refs.
- Operator summary and checklist are produced for successful governance workflows.
- Reference integrity report is present in the preapply review manifest outputs.
- All outputs stay under `artifacts/research`.
- No live runtime writes, active parameter writes, or OKX writes are introduced.
