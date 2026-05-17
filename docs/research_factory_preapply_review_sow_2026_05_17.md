# Research Factory PreApply Review SOW

## Business Objectives and Boundaries

Add a research-only review workflow after `PreApplyEvidencePackage`. The review may decide whether a package is worth entering a manually designed governance plan, but it must not mutate runtime configuration, active parameters, live orders, OKX state, or production deployment.

Target chain:

```text
PreApplyEvidencePackage -> PreApplyReview -> PreApplyReviewDecision
```

The chain stops at review evidence. It does not apply.

## Module Responsibilities and Domain Model

- `PreApplyEvidencePackage`: existing evidence package, with direct-constructor status and review decision consistency enforced.
- `PreApplyReview`: pending review artifact for a package.
- `PreApplyReviewDecision`: final review decision artifact.
- `PreApplyReviewRecorder`: writes review and decision artifacts under `artifacts/research`.

## Input/Output Interfaces

Input:

```text
PreApplyEvidencePackage
review rationale
reviewer identity
required followups
```

Outputs:

```text
preapply_review.json
preapply_review_decision.json
preapply_review_manifest.json
```

Allowed decision values:

```text
review_approved_for_manual_apply_design
review_rejected
needs_more_evidence
```

## Database Schema / Tables / Indexes / Constraints

No database changes. This is artifact-only research governance.

## Transactions, Consistency, Concurrency

Artifacts are written atomically. A review directory may only be created once, and a recorded decision must match the review/package identity.

## Authorization, Authentication, Data Security

No credentials are read. Review text rejects runtime promotion terms such as active parameter mutation, live order, OKX write, runtime mutation, direct apply, and auto apply.

## Error Handling and Idempotency

Invalid identity links, invalid decisions, missing followups for `needs_more_evidence`, and attempts to approve non-ready packages fail closed.

## State Transition and Lifecycle

```text
review_pending -> review_approved_for_manual_apply_design | review_rejected | needs_more_evidence
```

Manifest lifecycle:

```text
running -> succeeded
```

## Caching and Performance

No caching changes.

## Logging, Monitoring, Auditing

Review artifacts include package, candidate, recommendation, observation, experiment, reviewer, rationale, and followup identity.

## Testing Strategy

Add unit coverage for status/decision consistency, approval restrictions, runtime text rejection, pending review artifact writes, decision writes, duplicate rejection, and mismatched decision rejection.

## Migration, Rollback, Compatibility

No public runtime API change. Existing preapply package builder behavior remains compatible, with stricter direct-constructor validation.

## Configuration and Environment Isolation

No new environment variables. Outputs remain under `artifacts/research`.

## Code Organization and Dependencies

Use the existing `preapply.py` module and artifact helpers. No new dependencies.

## Documentation and Operations Manual

Operators should interpret `review_approved_for_manual_apply_design` only as permission to prepare a manual governance design package. It is not approval to apply.

## Deployment and Acceptance Criteria

Acceptance requires lint, unit tests, and a narrow WSL2 integration sanity check. No deployment is required for this artifact-only Research Factory change.
