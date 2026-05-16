# Research Factory Evidence Quality Gate SOW - 2026-05-16

## Business Objectives and Boundaries

Move the Research Factory real-data runner from "can produce a recommendation" to "can produce a recommendation backed by data-quality and source-integrity evidence."

This remains research-only. It does not mutate active parameters, modify runtime config, submit orders, call OKX write APIs, deploy services, or introduce Qlib/RD-Agent production dependencies.

## Module Responsibilities and Domain Model

Responsibilities:

- Dataset quality report: verify bar count, segment coverage, bar-gap ratio, and funding alignment completeness.
- Source integrity report: verify source candle/funding dataset versions and build-run traceability.
- Execution evidence report: verify execution summary identity and window match the experiment dataset.
- Evidence bundle: combine reports and fail closed before candidate/recommendation generation.
- Real-data runner: write evidence artifacts and include them in recommendation evidence refs.

Domain model:

- `DatasetQualityThresholds`
- `DatasetQualityReport`
- `SourceIntegrityReport`
- `ExecutionEvidenceReport`
- `EvidenceBundle`

## Input/Output Interfaces

Inputs:

- Gold replay `GoldBarRecord` rows.
- `DatasetSpec` and dataset fingerprint.
- Optional execution cost summary JSON.

Outputs under each experiment directory:

- `dataset_quality_report.json`
- `source_integrity_report.json`
- `execution_evidence_report.json` when execution evidence is supplied.
- `evidence_bundle.json`

Recommendation evidence refs include the new reports.

## Database Schema / Tables / Indexes / Constraints

No schema changes. The existing Gold replay read-only query remains unchanged except source watermark metadata is enriched with source funding dataset versions and timestamp timezone assumptions.

## Transactions, Consistency, Concurrency

Evidence artifacts are written through the existing atomic artifact writer. Concurrent writes to the same experiment id remain unsupported.

## Authorization, Authentication, Data Security

No secret files are read. Execution summaries remain constrained to the configured `artifacts/research` root. Evidence artifacts contain source versions, counts, fingerprints, and refs only.

## Error Handling and Idempotency

Evidence failures write report artifacts and then fail the experiment before candidate generation. With `overwrite=True`, reruns replace the experiment directory and produce deterministic evidence for deterministic inputs.

## State Transition and Lifecycle

Lifecycle becomes:

`start -> dataset/source/execution evidence -> evidence bundle gate -> metrics -> candidate -> recommendation -> registry -> finish`

Evidence gate failure writes `failure.json` and a failed registry entry.

## Caching and Performance

Evidence checks are in-memory scans over already-loaded Gold bars and small execution summary JSON payloads.

## Logging, Monitoring, Auditing

The manifest records all evidence report refs. Recommendation evidence refs also point to the reports.

## Testing Strategy

Add tests covering:

- successful real-data run includes evidence artifacts and recommendation evidence refs.
- mixed source candle dataset versions fail before candidate generation.
- funding missing ratio above threshold fails.
- execution evidence window mismatch fails.

## Migration, Rollback, Compatibility

Existing artifacts without evidence reports remain readable. Rollback removes the evidence module, runner integration, and tests.

## Configuration and Environment Isolation

No new environment variables. Thresholds are supplied through `ResearchFactoryExperimentConfig` with conservative defaults.

## Code Organization and Dependencies

No new third-party dependencies. Evidence contracts live under `aats.data_platform.research_factory`.

## Documentation and Operations Manual

Operators should treat recommendations without evidence bundle refs as pre-quality-gate legacy artifacts.

## Deployment and Acceptance Criteria

Acceptance requires:

- focused Research Factory tests pass.
- `ruff check aats/ --fix` passes.
- full unit suite passes.
- WSL2 CLI no-DB safety path remains fail-closed.
- WSL2 RDP production workflow integration smoke remains green.
