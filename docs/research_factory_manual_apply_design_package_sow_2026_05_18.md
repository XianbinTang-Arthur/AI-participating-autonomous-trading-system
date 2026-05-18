# Research Factory Manual Apply Design Package SOW

Date: 2026-05-18

## Business objectives and boundaries

Add an evidence-only ManualApplyDesignPackage after a positive PreApplyReviewDecision.
The package describes what a separate manual apply design review would need to
consider. It does not authorize, generate, or execute runtime changes.

Hard boundaries:

- No active parameter mutation.
- No runtime configuration writes.
- No OKX writes or order instructions.
- No production deployment.
- All generated artifacts remain under `artifacts/research`.

## Module responsibilities and domain model

Add a Research Factory module for:

- `ManualApplyDesignPackage`: immutable evidence/design artifact.
- `ManualApplyDesignRecorder`: writes the package and manifest.
- `build_manual_apply_design_package()`: creates a design draft only from an
  approved `PreApplyReviewDecision`.

Allowed package statuses:

- `design_draft`
- `design_ready_for_review`
- `design_rejected`

## Input/output interfaces

Inputs:

- `PreApplyEvidencePackage`
- `PreApplyReview`
- `PreApplyReviewDecision`
- proposed change summary
- proposed parameter/config delta
- affected runtime component names
- required risk guards
- required dry-run checks
- rollback plan reference

Outputs:

- `manual_apply_design_package.json`
- `manual_apply_design_manifest.json`

## Database schema / tables / indexes / constraints

No database schema changes.

## Transactions, consistency, concurrency

The recorder writes artifacts atomically and rejects duplicate design ids.

## Authorization, authentication, data security

The package is research-only. It must require operator approval, must not allow
runtime mutation, and must reject promotion text such as auto-apply, OKX write,
or active parameter apply language.

## Error handling and idempotency

Invalid references, unsafe identifiers, non-JSON payloads, missing required
sections, or non-approved review decisions fail closed.

## State transition and lifecycle

`review_approved_for_manual_apply_design` can create a `design_draft` package.
Rejected or needs-more-evidence review decisions cannot create a design package.

## Caching and performance

No caching changes. Package payloads are small JSON artifacts.

## Logging, monitoring, auditing

Auditability is provided by stable JSON artifacts and an artifact manifest.

## Testing strategy

Add unit tests for:

- successful design draft build from an approved preapply review decision
- rejection of non-approved review decisions
- rejection of runtime/apply/OKX promotion text
- JSON-safe delta validation
- recorder artifact and manifest output
- research artifact root enforcement

## Migration, rollback, compatibility

No migrations. Rollback is removing the new module, tests, and artifact type.

## Configuration and environment isolation

No environment variables. Outputs must stay under `artifacts/research`.

## Code organization and dependencies

Add the module under `aats/data_platform/research_factory/` and reuse the
existing artifact manifest helpers. Do not add runtime dependencies.

## Documentation and operations manual

This SOW documents the operational boundary: the package is design evidence, not
apply authority.

## Deployment and acceptance criteria

Acceptance requires focused unit tests and Research Factory unit tests passing.
No deployment is required for this research-only artifact model.
