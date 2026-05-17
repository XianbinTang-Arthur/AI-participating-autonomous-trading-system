# Research Factory PreApply Memory SOW

## Business Objectives and Boundaries

Extend Research Factory memory so pre-apply package and pre-apply review outcomes become future research decision inputs. This helps NoveltyGate and later allocation policy understand candidates that passed research but failed or stalled at pre-apply governance.

The work is research-only. It does not create apply commands, active parameter changes, live orders, OKX writes, or production deployment behavior.

## Module Responsibilities and Domain Model

- `registry.py`: add preapply memory statuses and a builder for preapply package/review entries.
- `PreApplyEvidencePackage`: supplies preapply status and failure reasons.
- `PreApplyReviewDecision`: optionally supplies review-level decision and followups.

## Input/Output Interfaces

Input:

```text
CandidateArtifact
PreApplyEvidencePackage
optional PreApplyReviewDecision
artifact refs
```

Output:

```text
ResearchMemoryEntry
```

## Database Schema / Tables / Indexes / Constraints

No database changes. Registry remains JSONL under `artifacts/research`.

## Transactions, Consistency, Concurrency

Registry writes keep existing atomic JSONL behavior.

## Authorization, Authentication, Data Security

No credentials are read. Existing registry redaction and relative artifact ref constraints remain in force.

## Error Handling and Idempotency

Identity mismatches between candidate, package, and review decision fail closed. Registry upsert remains idempotent by entry id.

## State Transition and Lifecycle

New memory statuses:

```text
preapply_ready
preapply_rejected
needs_more_observation
preapply_review_approved_for_manual_apply_design
preapply_review_rejected
preapply_review_needs_more_evidence
```

## Caching and Performance

No caching changes.

## Logging, Monitoring, Auditing

Entries preserve package id, review id, review decision, failure/followup reasons, factor signature, dataset fingerprint, and artifact refs.

## Testing Strategy

Add unit tests for ready package memory, review followups, and identity mismatch rejection.

## Migration, Rollback, Compatibility

Additive registry schema fields remain optional. Existing JSONL entries still deserialize.

## Configuration and Environment Isolation

No configuration or environment changes.

## Code Organization and Dependencies

Use existing registry and preapply modules. No new dependencies.

## Documentation and Operations Manual

Preapply memory is advisory research memory. It must not be treated as apply approval.

## Deployment and Acceptance Criteria

Acceptance requires lint, focused tests, full unit tests, and narrow WSL2 integration sanity.
