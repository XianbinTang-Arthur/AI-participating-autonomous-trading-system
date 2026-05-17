# Research Factory PreApply Evidence Package SOW

## Business objectives and boundaries

Add an evidence-only package after observation review so `eligible_for_preapply` can be converted into a self-contained review bundle. This work must not mutate active parameters, runtime configuration, live orders, OKX adapters, ledger, reconciliation, or production deployment paths.

## Module responsibilities and domain model

`aats.data_platform.research_factory.preapply` owns `PreApplyEvidencePackage`, `PreApplyEvidenceRecorder`, and `build_preapply_evidence_package()`. The package summarizes candidate, recommendation, evidence bundle, observation gate, and review outcome state for future governance review only.

## Input/output interfaces

Inputs are `CandidateArtifact`, `ResearchRecommendation`, `EvidenceBundle`, `ObservationGateResult`, `ReviewOutcome`, evidence refs, and gate refs. Outputs are stable JSON artifacts under `artifacts/research/research_factory/preapply/<package_id>/`: `preapply_evidence_package.json` and `preapply_manifest.json`.

## Database schema / tables / indexes / constraints

No database schema changes. This phase is file-artifact only.

## Transactions, Consistency, Concurrency

Artifacts are written atomically. Existing package directories are not overwritten. The package builder validates cross-object identity before writing.

## Authorization, Authentication, Data Security

No environment files, credentials, exchange APIs, live runtime state, or production configs are read. Package roots must stay under `artifacts/research`. Text fields and ref names reject runtime promotion terms.

## Error Handling and Idempotency

Invalid ids, missing required refs, mismatched candidate/recommendation/observation identity, failed ready prerequisites, runtime mutation flags, and unsafe paths fail closed with `ValueError`.

## State Transition and Lifecycle

`eligible_for_preapply` with passing candidate gate, evidence bundle, and observation gate becomes `preapply_ready`. `keep_reviewing` becomes `needs_more_observation`. `reject` becomes `preapply_rejected`.

## Caching and Performance

No caching is introduced. The package is a small deterministic JSON artifact.

## Logging, Monitoring, Auditing

The manifest captures package identity and output refs. The package captures ids, review decision, gate states, required refs, failure reasons, and immutable research-only flags.

## Testing Strategy

Unit tests cover ready, keep-reviewing, rejected, failed ready prerequisites, missing refs, forbidden terms, recorder output, duplicate writes, and research artifact root enforcement.

## Migration, Rollback, Compatibility

No migration is required. Existing recommendation and observation artifacts remain compatible. Rollback is a normal git revert because no runtime or schema state changes.

## Configuration and Environment Isolation

Default package output stays under `artifacts/research`. No environment variable or deployment configuration is added.

## Code Organization and Dependencies

The implementation uses only existing Research Factory contracts and the standard library. No Qlib, RD-Agent, database, exchange, or runtime service dependency is introduced.

## Documentation and Operations Manual

Operators should treat `preapply_ready` as evidence for a separate operator/governance review. It does not authorize apply or live runtime mutation.

## Deployment and Acceptance Criteria

Acceptance requires focused preapply unit tests, `ruff check`, the Research Factory unit subset, the full unit suite, and the existing WSL2 RDP integration sanity check. No production deployment is required.
