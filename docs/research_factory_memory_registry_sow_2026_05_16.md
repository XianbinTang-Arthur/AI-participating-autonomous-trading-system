# Research Factory Memory Registry SOW - 2026-05-16

## Business Objectives and Boundaries

Add a lightweight Research Factory memory layer so repeated research runs can retain what was tried, why it passed or failed, and whether a new candidate duplicates prior work. This moves Research Factory from single-run artifacts toward an auditable research loop that can avoid repeatedly generating the same low-value factor or parameter candidate.

This work remains research-only. It does not write active parameters, mutate runtime config, call OKX, submit orders, run generated code, deploy services, or reuse the decision-system recommendation registry.

## Module Responsibilities and Domain Model

Research Factory owns a local memory registry with these concepts:

- `ResearchMemoryEntry`: one attempted research result or failure.
- `factor_signature`: deterministic signature for a factor expression or candidate payload.
- `dataset_fingerprint`: deterministic dataset identity when available.
- `metric_snapshot`: flattened metrics and missing reasons captured from the run.
- `gate_result`: candidate gate decision evidence when available.
- `failure_reason`: redacted failure reason for failed attempts.
- `similarity_to_existing`: exact or near-duplicate matches against previous memory entries.
- `artifact_refs`: relative refs to experiment artifacts.

The registry is separate from production, governance, and pre-apply registries.

## Input/Output Interfaces

Inputs:

- `CandidateArtifact`, `MetricsSnapshot`, and `CandidateGateResult` for successful candidate runs.
- Factor expression, dataset fingerprint, failure reason, and artifact refs for failed runs.
- Research artifact root or explicit registry path under `artifacts/research`.

Outputs:

- `artifacts/research/research_factory/registry/research_memory.jsonl` by default.
- Smoke runner JSON includes `registry_ref`.

## Database Schema / Tables / Indexes / Constraints

No database schema changes. V1 uses an atomic JSONL file under `artifacts/research`. A later phase may mirror it to SQLite/Postgres after the file contract stabilizes.

## Transactions, Consistency, Concurrency

The registry writer loads existing JSONL entries, upserts by deterministic `entry_id`, and rewrites the full file atomically. This keeps smoke reruns idempotent and prevents duplicate entries for the same deterministic experiment.

Concurrent writers to the same registry file are not supported in V1 and should be serialized by the caller.

## Authorization, Authentication, Data Security

No credentials or `.env` files are read. Failure reasons are redacted when they contain secret-like markers. Artifact refs must be relative and must not contain path traversal.

## Error Handling and Idempotency

Invalid paths, ids, refs, status values, datetimes, non-JSON payloads, and non-finite numeric values raise `ValueError` before writing. Re-running a deterministic smoke experiment replaces its registry entry instead of appending duplicates.

## State Transition and Lifecycle

Registry statuses:

- `recommendation_ready`
- `gate_failed`
- `failed`
- `rejected`
- `duplicate`

The registry records lifecycle evidence only; it does not transition candidates into runtime.

## Caching and Performance

V1 reads and rewrites a small JSONL file. This is acceptable for the first research loop. If registry size becomes large, the next phase should add an indexed SQLite/Postgres backing store.

## Logging, Monitoring, Auditing

Each line is stable JSON with schema version, created timestamp, entry id, experiment id, candidate id, factor signature, dataset fingerprint, metrics, gate result, failure reason, similarity matches, creator, status, and artifact refs.

## Testing Strategy

Add unit tests covering:

- Upsert idempotency.
- Similarity detection for repeated factor/dataset combinations.
- Redacted failure reasons.
- Path and artifact ref validation.
- Smoke runner writes registry entries for both success and failure.

## Migration, Rollback, Compatibility

This is additive. Existing experiment artifacts remain compatible. Rollback removes the registry module, smoke runner registry hook, CLI flag, SOW, and tests.

## Configuration and Environment Isolation

Default registry path is derived from the Research Factory artifact root:

`artifacts/research/research_factory/registry/research_memory.jsonl`

An explicit `--registry-path` can be passed, but it must still be under `artifacts/research`.

## Code Organization and Dependencies

Add a Research Factory registry module and tests. No third-party dependencies are introduced.

## Documentation and Operations Manual

Operators can inspect `research_memory.jsonl` to answer:

- Which factors were tried?
- Which experiments passed the candidate gate?
- Which failed and why?
- Which new result duplicates or resembles prior attempts?
- Which dataset fingerprint and artifacts support the record?

## Deployment and Acceptance Criteria

Acceptance requires:

- Focused registry and smoke tests pass.
- `ruff check aats/ --fix` passes.
- Full unit suite passes.
- WSL2 smoke CLI writes a registry entry.
- Existing WSL2 data-platform integration smoke remains green.
