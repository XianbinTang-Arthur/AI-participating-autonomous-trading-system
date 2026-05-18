# Research Factory Manual Design Workflow SOW

Date: 2026-05-18

## Business objectives and boundaries

Add a second, independent Research Factory workflow that starts after a
`PreApplyReviewDecision` approves a package for manual design consideration and
ends at a dry-run plan evidence package.

This workflow does not execute dry-runs, does not write active parameters, does
not mutate runtime config, does not call OKX write APIs, and does not authorize
apply.

## Module responsibilities and domain model

The workflow coordinates existing research-only modules:

- `ManualApplyDesignPackage`
- `ManualApplyDesignValidationReport`
- `ManualApplyDesignReview`
- `ManualApplyDesignReviewDecision`
- `DryRunPlanPackage`
- `DryRunPlanValidationReport`

The workflow is separate from the Gold replay governance workflow. It consumes
pre-apply review evidence and produces design/planning artifacts only.

## Input/output interfaces

Inputs:

- `PreApplyEvidencePackage`
- `PreApplyReview`
- `PreApplyReviewDecision`
- manual design draft fields
- manual design policy profile or policy
- dry-run plan fields

Outputs:

- `manual_apply_design_package.json`
- `manual_apply_design_validation_report.json`
- `manual_apply_design_review.json`
- `manual_apply_design_review_decision.json`
- `dry_run_plan_package.json`
- `dry_run_plan_validation_report.json`
- `manual_design_workflow_summary.json`
- `manual_design_workflow_manifest.json`

## Database schema / tables / indexes / constraints

No database changes.

## Transactions, consistency, concurrency

Artifacts are written atomically by existing recorders. Duplicate workflow,
design, review, or dry-run plan ids fail closed.

## Authorization, authentication, data security

All artifacts are written under `artifacts/research`. The workflow reads no
`.env` files and does not require credentials. Every artifact explicitly keeps
`runtime_mutation_allowed = false` and `operator_approval_required = true`.

## Error handling and idempotency

Invalid pre-apply decisions, failed manual design validation, rejected manual
design review, invalid dry-run plan fields, and duplicate downstream artifact
ids fail closed with a workflow summary that identifies the failed stage.
Duplicate workflow ids fail closed without overwriting the existing workflow
directory.

## State transition and lifecycle

Allowed positive chain:

```text
review_approved_for_manual_apply_design
  -> ManualApplyDesignPackage
  -> ManualApplyDesignValidationReport(passed=true)
  -> ManualApplyDesignReview
  -> ManualApplyDesignReviewDecision(design_ready_for_dry_run_planning)
  -> DryRunPlanPackage
```

The terminal workflow status is a planning status, not an execution status.

## Caching and performance

No caching changes. Artifacts are small JSON/manifest files.

## Logging, monitoring, auditing

Auditability is provided by stable JSON artifacts, manifests, and
`manual_design_workflow_summary.json`.

## Testing strategy

Add focused unit tests for:

- successful manual design workflow writes all expected artifacts
- non-approved pre-apply review decision cannot enter the workflow
- failed manual design validation stops before dry-run plan creation
- rejected manual design review decision stops before dry-run plan creation
- workflow root must stay under `artifacts/research`

## Migration, rollback, compatibility

No migrations. Rollback is removing the new workflow module, SOW, and tests.

## Configuration and environment isolation

No new environment variables. The workflow only uses explicit function inputs
and artifact roots.

## Code organization and dependencies

Add `manual_design_workflow.py` under `aats/data_platform/research_factory/`.
Reuse existing manual-design and dry-run planning builders/recorders. Do not add
dependencies.

## Documentation and operations manual

This SOW is the operations note: the workflow prepares dry-run planning
evidence only. A separate future SOW is required for any dry-run execution path.

## Deployment and acceptance criteria

Acceptance requires lint and focused Research Factory tests. Full unit suite and
WSL2 sanity should be run before a push that claims release readiness.
