# Research Factory Manual Apply Design Review And Domain Validation SOW

Date: 2026-05-18

## Business objectives and boundaries

Add a research-only review layer for `ManualApplyDesignPackage` and validate
manual design drafts by candidate type before they can be considered ready for
dry-run planning.

This work does not implement apply, auto-apply, runtime configuration writes,
OKX writes, active parameter mutation, or deployment.

## Module responsibilities and domain model

Extend the manual apply design module with:

- `ManualApplyDesignValidationReport`
- `validate_manual_apply_design_domain()`
- `ManualApplyDesignReview`
- `ManualApplyDesignReviewDecision`
- `ManualApplyDesignReviewRecorder`

Allowed review decisions:

- `design_ready_for_dry_run_planning`
- `design_rejected`
- `needs_design_revision`

## Input/output interfaces

Inputs:

- `ManualApplyDesignPackage`
- candidate-type-specific delta fields
- risk guard requirements
- dry-run check requirements
- rollback references

Outputs:

- `manual_apply_design_validation_report.json`
- `manual_apply_design_review.json`
- `manual_apply_design_review_decision.json`
- `manual_apply_design_review_manifest.json`

## Database schema / tables / indexes / constraints

No database changes.

## Transactions, consistency, concurrency

Artifact writes are atomic. Duplicate review ids are rejected. Review decisions
must match the stored review identity.

## Authorization, authentication, data security

All outputs remain under `artifacts/research`. Review and decision artifacts
must keep `runtime_mutation_allowed = false` and
`operator_approval_required = true`.

## Error handling and idempotency

Unsupported candidate types, missing rollback evidence, missing dry-run checks,
missing risk guards, non-passing validation, or runtime-promotion text fail
closed.

## State transition and lifecycle

`ManualApplyDesignPackage` can start a `ManualApplyDesignReview`. A passing
domain validation report is required before a review decision can become
`design_ready_for_dry_run_planning`.

## Caching and performance

No caching changes. Reports are small JSON artifacts.

## Logging, monitoring, auditing

Auditability is provided through stable JSON artifacts and Research Factory
artifact manifests.

## Testing strategy

Add unit tests for:

- factor design validation
- parameter design validation
- risk budget validation
- execution policy validation
- missing dry-run / risk / rollback checks
- review decision cannot become dry-run planning ready without passing validation
- recorder writes validation, review, decision, and manifest

## Migration, rollback, compatibility

No migrations. Rollback is removing the review/domain validation module changes
and tests.

## Configuration and environment isolation

No new environment configuration. No `.env` reads.

## Code organization and dependencies

Keep the implementation in `aats/data_platform/research_factory/manual_apply_design.py`
to avoid introducing a fragmented design-review module too early. Reuse existing
artifact manifest helpers. Do not add dependencies.

## Documentation and operations manual

This SOW documents that dry-run planning readiness is still not apply authority.

## Deployment and acceptance criteria

Acceptance requires lint, focused manual design tests, Research Factory unit
tests, and the existing WSL2 RDP production workflow integration sanity check.
