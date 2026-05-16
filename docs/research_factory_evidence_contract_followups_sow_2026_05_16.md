# Research Factory Evidence Contract Followups SOW - 2026-05-16

## Business Objectives and Boundaries

Close the remaining audit and evidence-contract gaps in the Research Factory real-data runner before it is used by scheduled research automation.

This remains research-only. It does not mutate active parameters, change runtime config, submit orders, call OKX write APIs, deploy services, or introduce Qlib/RD-Agent as production runtime dependencies.

## Module Responsibilities and Domain Model

Responsibilities:

- Real-data runner: create an auditable experiment attempt before DB/Gold replay load so load failures produce manifest, failure artifact, and registry entry.
- Source integrity gate: fail mixed or missing `build_run_id`, not just missing traceability.
- Execution evidence contract: require schema version, source run id, symbol/timeframe/window match, and exact dataset fingerprint or explicit compatibility marker.
- Experiment recorder: reject output ref names that encode runtime/apply semantics.
- Path helper: require destination directories for copied research artifacts to stay under the configured research artifact root.

## Input/Output Interfaces

Inputs:

- `execution_cost_summary.json` with `schema_version`, `source_run_id`, `symbol`, `timeframe`, `window_start`, `window_end`, and either `dataset_fingerprint` or explicit `dataset_fingerprint_compatibility = "compatible"`.
- Gold replay load source metadata.

Outputs:

- Load failures write `experiment_manifest.json`, `failure.json`, and registry failed entries.
- Execution evidence reports include schema/source-run/dataset-fingerprint compatibility state.

## Database Schema / Tables / Indexes / Constraints

No database schema changes.

## Transactions, Consistency, Concurrency

Experiment start occurs before DB load. Successful loads replace the pending experiment spec with the resolved Gold replay dataset version and source refs before evidence generation. Concurrent writes to the same experiment id remain unsupported.

## Authorization, Authentication, Data Security

No secret files are read or printed. Path validation still rejects `.env`, credential, secret, token, password, live, and production config tokens.

## Error Handling and Idempotency

If DB load fails after a valid experiment id and artifact root, the runner fails closed and leaves auditable artifacts. With `overwrite=True`, reruns replace the experiment directory.

## State Transition and Lifecycle

Lifecycle becomes:

`start pending attempt -> load Gold replay -> replace resolved spec -> evidence contract gate -> metrics -> candidate -> recommendation -> registry -> terminal`

Load failures stop at `failed`.

## Caching and Performance

No new heavy runtime work. Contract checks are JSON and metadata validation over already loaded artifacts.

## Logging, Monitoring, Auditing

Manifest and registry now capture load failure attempts and stricter execution/source evidence failures.

## Testing Strategy

Add tests covering:

- DB/load failure writes failure artifact and registry entry.
- mixed build run ids fail source integrity.
- missing execution evidence schema/source-run/fingerprint compatibility fails.
- `record_output_ref()` rejects runtime/apply ref names.
- `copy_research_artifact_file()` rejects destination outside `artifacts/research`.

## Migration, Rollback, Compatibility

Existing legacy execution summary artifacts without the new contract fields will fail for real-data `ready_for_review`. They can be regenerated or marked explicitly compatible if justified.

## Configuration and Environment Isolation

No new environment variables.

## Code Organization and Dependencies

No new third-party dependencies.

## Documentation and Operations Manual

Operators should treat old execution summaries without contract metadata as draft evidence only.

## Deployment and Acceptance Criteria

Acceptance requires:

- focused Research Factory tests pass.
- `ruff check aats/ --fix` passes.
- full unit suite passes.
- WSL2 real-data CLI no-DB safety path remains fail-closed.
- WSL2 RDP production workflow integration smoke remains green.
