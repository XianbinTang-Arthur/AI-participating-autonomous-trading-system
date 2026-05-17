# Research Factory Real Data Novelty Preflight SOW

## Business objectives and boundaries

Connect the Research Memory novelty gate to the real-data Research Factory runner so exact duplicates and repeatedly failed factor families are stopped before expensive evidence, metric, candidate, and recommendation generation. This remains research-only and must not mutate active parameters, runtime configuration, live orders, OKX adapters, ledger, reconciliation, or production deployment paths.

## Module responsibilities and domain model

`aats.data_platform.research_factory.real_data` owns the real-data runner lifecycle. It now resolves the actual dataset fingerprint from Gold replay data, evaluates `ResearchMemoryRegistry.evaluate_novelty()`, records `novelty_gate_result.json`, and continues or fails closed based on the gate decision.

## Input/output interfaces

Inputs are unchanged real-data experiment config fields plus the existing registry path. Output adds an optional `novelty_gate_ref` in `ResearchFactoryExperimentResult` and an experiment artifact `novelty_gate_result.json`.

## Database schema / tables / indexes / constraints

No database schema changes. Gold replay access remains read-only. Novelty memory stays in the JSONL registry under `artifacts/research`.

## Transactions, Consistency, Concurrency

No new transaction path is introduced. Registry reads are preflight-only; failure/skip memory continues to use existing atomic registry upsert behavior.

## Authorization, Authentication, Data Security

No environment files, credentials, exchange APIs, live runtime state, or production configs are read. The novelty gate only reads research registry artifacts and writes experiment artifacts under `artifacts/research`.

## Error Handling and Idempotency

`duplicate` and `suppress` decisions fail closed with a failure artifact, manifest output ref, and registry memory entry. `allow`, `warn`, and `retest` continue into the normal evidence loop. Re-running the same duplicate attempt is deterministic and leaves an auditable failure artifact.

## State Transition and Lifecycle

The runner lifecycle becomes: pending manifest, Gold replay load, resolved dataset fingerprint, novelty gate, evidence gate, metrics, candidate, recommendation, registry. Novelty rejection remains terminal failure for the experiment attempt, not runtime apply.

## Caching and Performance

No caching is introduced. The gate avoids downstream work for duplicates and suppressed factor families after the minimal data load needed to compute the exact dataset fingerprint.

## Logging, Monitoring, Auditing

`novelty_gate_result.json` records decision, should-run flag, reasons, matched entries, failure count, and timestamp. Manifest output refs and registry entries keep the decision auditable.

## Testing Strategy

Unit tests cover duplicate skip, repeated failed-family suppression, warning continuation, manifest refs, registry status, and continued recommendation generation when novelty warns rather than blocks.

## Migration, Rollback, Compatibility

Existing successful runs remain compatible. `novelty_gate_ref` is optional in CLI JSON. Rollback is a normal git revert because no runtime, schema, or deployment state changes are introduced.

## Configuration and Environment Isolation

No new environment variable or YAML setting is added. Runner config exposes `enable_novelty_gate` and `novelty_suppress_after_failures` as explicit in-process controls.

## Code Organization and Dependencies

The implementation stays in `real_data.py`, `registry.py`, and focused Research Factory tests. No Qlib, RD-Agent, database write, exchange, or runtime service dependency is introduced.

## Documentation and Operations Manual

Operators should treat `duplicate` and `suppress` as research attempt stops, not live safety actions. `warn` and `retest` are evidence context for the resulting recommendation and do not authorize apply.

## Deployment and Acceptance Criteria

Acceptance requires focused real-data runner tests, registry tests, `ruff check`, the Research Factory unit subset, the full unit suite, and the existing WSL2 RDP integration sanity check. No production deployment is required.
