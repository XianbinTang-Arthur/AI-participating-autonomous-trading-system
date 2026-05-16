# Research Factory Real Data Runner P1 Followups SOW - 2026-05-16

## Business Objectives and Boundaries

Fix three P1 audit and artifact-boundary issues before the real-data Research Factory runner is used by scheduled automation:

- Real-data runs must use actual UTC run time by default, not the deterministic smoke timestamp.
- Execution realism summary inputs must be constrained to research artifacts.
- Execution realism evidence must be self-contained or unambiguous inside the experiment artifact tree.

This work remains research-only. It does not mutate active parameters, change runtime config, submit orders, call OKX, deploy services, or read secret files.

## Module Responsibilities and Domain Model

Responsibilities:

- Path boundary helper: validate research artifact JSON files and reject traversal, `.env`, credential, live, and production config path tokens.
- Experiment recorder: attach copied execution realism summary refs to the manifest.
- Real-data runner: default timestamp to `datetime.now(UTC)` and copy execution summaries into the experiment directory.
- Smoke runner: apply the same execution summary file boundary and copy behavior for consistency.

## Input/Output Interfaces

Inputs:

- `execution_cost_summary_path`: must resolve under `artifacts/research` and have `.json` suffix.

Outputs:

- `<experiment_dir>/execution_cost_summary.json`
- Manifest `output_refs.execution_cost_summary = "execution_cost_summary.json"`
- Candidate payload and recommendation evidence refs point to `"execution_cost_summary.json"`.

## Database Schema / Tables / Indexes / Constraints

No database schema changes.

## Transactions, Consistency, Concurrency

The summary file is copied after metrics are loaded and before candidate/recommendation generation. The manifest is updated through the existing atomic artifact writer. Concurrent writes to the same experiment id remain unsupported.

## Authorization, Authentication, Data Security

No `.env` files are read. The execution summary path validator rejects:

- absolute paths outside `artifacts/research`
- path traversal
- `~`
- `.env*`
- credential/secret/token/password/key path tokens
- live/production_config path tokens

## Error Handling and Idempotency

Invalid execution summary paths fail closed before reading the file. With `overwrite=True`, deterministic smoke runs remain idempotent because the copied summary and manifest refs are stable.

## State Transition and Lifecycle

Lifecycle remains:

`metrics -> execution_cost_summary copy -> candidate -> recommendation -> registry -> terminal manifest`

If execution summary validation fails after recorder start, a failure artifact is written.

## Caching and Performance

Execution summaries are small JSON artifacts. Copying them into experiment dirs has negligible overhead.

## Logging, Monitoring, Auditing

Manifest output refs now include the execution summary. Recommendation evidence is self-contained inside the experiment artifact directory.

## Testing Strategy

Add/update tests covering:

- real-data config timestamp defaults to current UTC at construction time.
- unsafe execution summary paths are rejected before read.
- copied execution summary exists under the experiment dir.
- manifest output refs include `execution_cost_summary`.
- smoke runner uses the same self-contained evidence behavior.

## Migration, Rollback, Compatibility

This is additive. Existing artifacts without `execution_cost_summary` output refs remain readable. Rollback removes the path helper, recorder method, copy hooks, and test updates.

## Configuration and Environment Isolation

No new environment variables. CLI behavior is unchanged except invalid execution summary paths now fail closed.

## Code Organization and Dependencies

No new third-party dependencies. Shared path validation lives inside Research Factory.

## Documentation and Operations Manual

Operators should pass execution summaries produced under `artifacts/research`. External paths must be copied into a research artifact directory before use.

## Deployment and Acceptance Criteria

Acceptance requires:

- focused Research Factory tests pass.
- `ruff check aats/ --fix` passes.
- full unit suite passes.
- WSL2 CLI no-DB safety path still passes.
- existing WSL2 data-platform integration smoke remains green.
