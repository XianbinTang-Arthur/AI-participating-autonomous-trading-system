# Research Factory Smoke Runner SOW - 2026-05-16

## Business objectives and boundaries

Build a one-command research-only smoke runner that proves the Research Factory modules work as a complete candidate generation loop. This work must not connect to RD-Agent, Qlib, OKX, live runtime, active parameter apply paths, deployment, credentials, or production databases.

## Module responsibilities and domain model

The smoke runner owns deterministic fixture data and orchestration only. Existing Research Factory modules remain responsible for dataset specs, Gold bar preparation, safe factor evaluation, baseline metrics, candidate gates, candidate artifacts, and experiment manifests.

## Input/output interfaces

Inputs are CLI options for artifact root, experiment id, factor expression, overwrite behavior, and cost settings. Outputs are JSON artifacts under `artifacts/research/research_factory/experiments/<experiment_id>/`, plus a concise JSON summary on stdout.

## Database schema / tables / indexes / constraints

No database schema, table, index, or constraint changes.

## Transactions, Consistency, Concurrency

Artifact writes continue to use atomic JSON writes through `ExperimentRecorder`. Optional overwrite is limited to the target experiment directory under the validated research artifact root.

## Authorization, Authentication, Data Security

No authentication changes. The runner must not read `.env` files, import OKX clients, call live runtime APIs, or write outside the research artifact tree.

## Error Handling and Idempotency

Successful runs write experiment spec, metrics snapshot, candidate artifact, and final manifest. Failures after experiment start write a redacted `failure.json` and mark the manifest failed. Deterministic inputs should produce stable metrics and artifact payloads.

## State Transition and Lifecycle

The lifecycle is `running -> succeeded` on a passing candidate gate. If the factor expression, dataset preparation, metrics, gate, or candidate creation fails, the lifecycle becomes `running -> failed`.

## Caching and Performance

No cache integration. The smoke dataset is small and in-memory.

## Logging, Monitoring, Auditing

No new monitoring. The experiment manifest and artifact tree are the audit surface.

## Testing Strategy

Add unit tests for successful smoke output, stable metrics across repeated deterministic runs, failure artifact writing, root confinement, and artifact path shape. Run the Research Factory tests, the script tests, lint, full unit suite, and a WSL2 narrow integration test.

## Migration, Rollback, Compatibility

No migration. The runner is additive and does not change runtime behavior.

## Configuration and Environment Isolation

The runner uses deterministic in-memory Gold bars and CLI-supplied constants. It does not read `.env`, `.env.wsl2`, `.env.derivatives.live`, or runtime templates.

## Code Organization and Dependencies

Add a small orchestration module under `aats/data_platform/research_factory/` and a CLI wrapper under `scripts/`. Do not add dependencies.

## Documentation and Operations Manual

The executable command is `.venv\Scripts\python.exe scripts\rdp_run_research_factory_smoke.py --overwrite`.

## Deployment and Acceptance Criteria

No deployment. Acceptance requires successful artifact creation, candidate generation, metrics stability, and validation commands passing.
