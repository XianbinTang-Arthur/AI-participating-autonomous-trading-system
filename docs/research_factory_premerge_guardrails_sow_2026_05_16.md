# Research Factory Pre-Merge Guardrails SOW - 2026-05-16

## Business objectives and boundaries

Add blocking guardrails required before merging the Research Factory first phase as research-only infrastructure. The work must not introduce Qlib or RD-Agent runtime dependencies, must not mutate active parameters, and must not touch live trading runtime, OKX adapters, deployment scripts, credentials, or production configuration.

## Module responsibilities and domain model

The Research Factory remains a candidate-only research substrate. Workflow specs define research stages, experiment recorders persist audit artifacts, sandbox guardrails validate generated proposal boundaries, numeric validators reject non-finite research inputs, and the factor baseline reports deterministic research metrics.

## Input/output interfaces

Inputs are in-memory specs, factor values, label values, sandbox proposals, and JSON-compatible research policy config. Outputs remain stable JSON artifacts under `artifacts/research`, validation errors, and `MetricsSnapshot` objects.

## Database schema / tables / indexes / constraints

No database schema, table, index, or constraint changes.

## Transactions, Consistency, Concurrency

Experiment artifacts continue to use atomic JSON writes. The recorder root validation must fail before any directory creation when a caller points outside the research artifact tree.

## Authorization, Authentication, Data Security

No authentication changes. Guardrails must keep generated candidate code and artifacts away from credentials, live config, deployment files, and Research Factory framework guardrail code.

## Error Handling and Idempotency

Invalid workflow actions, recorder roots, sandbox write paths, non-finite numbers, and invalid OHLCV bars must raise `ValueError` before artifacts are written or metrics are generated.

## State Transition and Lifecycle

Experiment lifecycle semantics are unchanged: start, record metrics, record candidate, finish, and fail. Terminal experiment protection remains intact.

## Caching and Performance

No caching changes. Numeric validation is local and constant time per value.

## Logging, Monitoring, Auditing

No new logging. The existing manifest and candidate artifact audit path is preserved.

## Testing Strategy

Add focused unit tests for blocked workflow actions, recorder root constraints, narrowed sandbox write roots, finite numeric validation, OHLCV invariants, and turnover-based baseline costs. Run the Research Factory unit subset, then the required broader validation commands.

## Migration, Rollback, Compatibility

No data migration. Existing broad sandbox write policies are intentionally narrowed; generated candidate outputs must move to explicit `generated` or research artifact paths.

## Configuration and Environment Isolation

Update `configs/research_factory/sandbox_policy.json` to match code defaults. Do not read or print `.env` files.

## Code Organization and Dependencies

Add a small Research Factory numeric validation helper. Do not add external dependencies.

## Documentation and Operations Manual

This SOW documents the pre-merge safety scope. Existing operation manuals remain unchanged.

## Deployment and Acceptance Criteria

No deployment for this research-only patch. Acceptance requires lint, unit tests, and the narrowest relevant WSL2 integration command to be run or clearly reported if blocked.
