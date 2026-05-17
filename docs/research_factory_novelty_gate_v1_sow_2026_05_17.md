# Research Factory Novelty Gate V1 SOW

## Business objectives and boundaries

Use Research Factory memory as a first-class research decision input so repeated factors, cross-dataset retests, failed observation families, and risky dataset contexts are identified before spending another experiment run. This remains research-only and must not mutate active parameters, runtime configuration, live orders, OKX adapters, ledger, reconciliation, or production deployment paths.

## Module responsibilities and domain model

`aats.data_platform.research_factory.registry` owns `NoveltyGateResult` and `evaluate_novelty_gate()`. The gate reads existing `ResearchMemoryEntry` records and returns one of `allow`, `duplicate`, `retest`, `warn`, or `suppress`.

## Input/output interfaces

Inputs are a factor DSL expression, a dataset fingerprint, prior registry entries or a `ResearchMemoryRegistry`, and optional failure suppression thresholds. Output is a deterministic `NoveltyGateResult` with decision, should-run flag, reasons, matched memory entries, failure match count, and evaluation timestamp.

## Database schema / tables / indexes / constraints

No database schema changes. The registry remains a JSONL artifact under `artifacts/research`.

## Transactions, Consistency, Concurrency

No new transaction path is introduced. The gate is read-only. Existing registry writes keep their atomic JSONL replacement behavior.

## Authorization, Authentication, Data Security

No environment files, credentials, exchange APIs, live runtime state, or production configs are read. The novelty gate only reads research registry artifacts already constrained to `artifacts/research`.

## Error Handling and Idempotency

Invalid factor expressions still produce deterministic factor signatures. Empty dataset fingerprints, invalid entry sequences, invalid limits, and invalid result decisions fail closed. Re-running the same novelty evaluation against the same registry produces the same decision.

## State Transition and Lifecycle

The gate does not create or mutate lifecycle state. It informs whether a proposal should run: exact duplicates and suppressed failed families should not run; retests, warnings, and allowed proposals may run under research-only controls.

## Caching and Performance

No caching is introduced. The scan is linear over existing registry entries and bounded output matches by limit.

## Logging, Monitoring, Auditing

`NoveltyGateResult` includes matched entries, reasons, and failure counts so later experiment artifacts can record why a proposal was run, retested, warned, suppressed, or treated as duplicate.

## Testing Strategy

Unit tests cover exact duplicate detection, same-factor retest, repeated failed-family suppression, same-dataset warning, new proposal allow, and result validation.

## Migration, Rollback, Compatibility

Existing registry entries remain readable and existing callers are unchanged. Rollback is a normal git revert because no runtime, schema, or deployment state changes are introduced.

## Configuration and Environment Isolation

No new environment variable, YAML setting, or deployment configuration is added. Suppression threshold is an explicit function argument with a conservative default.

## Code Organization and Dependencies

The implementation stays in `registry.py` and uses existing Research Factory registry contracts plus the standard library. No Qlib, RD-Agent, database, exchange, or runtime service dependency is introduced.

## Documentation and Operations Manual

Use `duplicate` to skip exact same factor and dataset, `retest` for same factor on a different dataset, `warn` when the dataset has prior failed or unresolved memory, and `suppress` when the same factor family repeatedly failed. None of these decisions authorize apply.

## Deployment and Acceptance Criteria

Acceptance requires focused registry unit tests, `ruff check`, the Research Factory unit subset, the full unit suite, and the existing WSL2 RDP integration sanity check. No production deployment is required.
