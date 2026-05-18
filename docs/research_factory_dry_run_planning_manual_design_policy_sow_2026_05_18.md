# Research Factory Dry-Run Planning And Manual Design Policy SOW

Date: 2026-05-18

## Business objectives and boundaries

Add a research-only dry-run planning layer after `design_ready_for_dry_run_planning`
and upgrade manual apply design validation from heuristic checks to an explicit
policy profile.

This work does not execute dry-runs and does not authorize apply. It must not
write active parameters, runtime config, OKX orders, or deployment state.

## Module responsibilities and domain model

Manual design policy:

- `ManualApplyDesignPolicy`
- `manual_apply_design_policy_for_profile()`
- policy-backed `validate_manual_apply_design_domain()`

Dry-run planning:

- `DryRunPlanPackage`
- `DryRunPlanValidationReport`
- `DryRunPlanReviewDecision`
- `DryRunPlanRecorder`
- `build_dry_run_plan_package()`
- `validate_dry_run_plan()`

## Input/output interfaces

Inputs:

- `ManualApplyDesignPackage`
- `ManualApplyDesignValidationReport`
- `ManualApplyDesignReview`
- `ManualApplyDesignReviewDecision`
- dry-run target environment, scope, inputs, success criteria, abort conditions

Outputs:

- `dry_run_plan_package.json`
- `dry_run_plan_validation_report.json`
- `dry_run_plan_manifest.json`

## Database schema / tables / indexes / constraints

No database changes.

## Transactions, consistency, concurrency

Artifacts are written atomically. Duplicate dry-run plan ids are rejected.

## Authorization, authentication, data security

Outputs are under `artifacts/research`. Artifacts require operator approval and
forbid runtime mutation. No `.env` files are read.

## Error handling and idempotency

Unknown policy profiles, unknown runtime components, unknown parameter paths,
missing risk guards, missing dry-run checks, missing rollback plan, missing
success criteria, or missing abort conditions fail closed.

## State transition and lifecycle

`design_ready_for_dry_run_planning` can create a `dry_run_plan_draft`. A dry-run
plan may become `dry_run_plan_ready_for_review` only as a plan artifact. It does
not execute dry-run steps.

## Caching and performance

No caching changes. Plan artifacts are small JSON files.

## Logging, monitoring, auditing

Auditability is provided by stable JSON artifacts and manifests.

## Testing strategy

Add focused tests for:

- policy rejects unknown runtime components
- policy rejects unknown parameter paths
- policy rejects forbidden delta keys
- dry-run plan builds only from passing manual design review decision
- dry-run plan validation rejects missing success criteria or abort conditions
- recorder writes package, validation report, and manifest

## Migration, rollback, compatibility

No migrations. Rollback is removing the new dry-run planning module and policy
additions.

## Configuration and environment isolation

No new environment configuration. No runtime writes.

## Code organization and dependencies

Keep policy in `manual_apply_design.py`; put dry-run plan artifacts in
`dry_run_planning.py`. Reuse existing Research Factory artifact helpers. Do not
add dependencies.

## Documentation and operations manual

This SOW documents the boundary: dry-run planning readiness is still not dry-run
execution and not apply authority.

## Deployment and acceptance criteria

Acceptance requires lint, focused Research Factory tests, full unit tests, and
WSL2 RDP production workflow sanity passing. No deployment is required.
