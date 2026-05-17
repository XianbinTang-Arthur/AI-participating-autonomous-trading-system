# Research Factory Evidence Reference Integrity SOW

## Business Objectives and Boundaries

Add a research-only evidence reference integrity report for pre-apply evidence packages. The report checks whether referenced evidence artifacts exist, remain under `artifacts/research`, and carry matching candidate, recommendation, observation, experiment, and review identities.

This work must not trigger apply, active parameter mutation, live orders, OKX writes, or production deployment.

## Module Responsibilities and Domain Model

- `integrity.py`: validates artifact references and builds `EvidenceReferenceIntegrityReport`.
- `PreApplyEvidencePackage`: remains the source object being checked.
- Existing recorders remain responsible for writing package and review artifacts.

## Input/Output Interfaces

Input:

```text
PreApplyEvidencePackage
artifact root directory containing referenced JSON artifacts
```

Output:

```text
EvidenceReferenceIntegrityReport
```

## Database Schema / Tables / Indexes / Constraints

No database changes. The validator reads JSON artifacts from the research artifact tree only.

## Transactions, Consistency, Concurrency

No transaction changes. The report is deterministic for a fixed artifact tree.

## Authorization, Authentication, Data Security

The validator rejects paths outside `artifacts/research`, absolute paths, traversal, and runtime-promotion terms already blocked by package refs. It does not read `.env` or credential files.

## Error Handling and Idempotency

Missing files, invalid JSON, unsafe refs, and identity mismatches are reported as failures inside the integrity report. Invalid root or package type fails closed.

## State Transition and Lifecycle

No lifecycle transition. This report is evidence input for future pre-apply review workflows.

## Caching and Performance

No caching. The validator reads only referenced JSON files.

## Logging, Monitoring, Auditing

The report includes checked refs, missing refs, unsafe refs, identity mismatches, failures, and timestamp.

## Testing Strategy

Add unit tests for passing pre-apply refs, missing files, identity mismatch, and unsafe roots.

## Migration, Rollback, Compatibility

Additive module. Existing package and review behavior remains compatible.

## Configuration and Environment Isolation

No environment variables. Files must stay under `artifacts/research`.

## Code Organization and Dependencies

Use standard library and existing Research Factory artifact/path helpers only.

## Documentation and Operations Manual

Operators should treat a passing report as evidence-link integrity, not as approval to apply.

## Deployment and Acceptance Criteria

Acceptance requires lint, focused tests, full unit tests, and narrow WSL2 integration sanity.
