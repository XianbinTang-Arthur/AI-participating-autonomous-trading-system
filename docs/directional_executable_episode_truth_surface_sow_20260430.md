# Directional Executable Episode Truth Surface SOW - 2026-04-30

## Business Objectives and Boundaries
Expose the latest executable directional episode as a structured runtime-truth surface for the OKX BTC-USDT-SWAP trading microscope. This is read-only reporting only and must not change strategy, risk, execution, provider, venue, symbol, schema, release, tuning, or live order behavior.

## Module Responsibilities and Domain Model
`scripts/runtime_truth_report.py` owns no-secret runtime truth generation. The new surface summarizes the existing `database_truth.latest_executable_directional_decision` object and its `execution_truth_chain` into a first-class `directional_executable_episode_truth` section.

## Input/Output Interfaces
Input is the existing DB probe output already loaded through the gateway container environment. Output is JSON-only: status, smallest missing field, latest executable decision identity, execution chain coverage, terminal no-fill evidence, and route/order/fill expectations.

## Database Schema / Tables / Indexes / Constraints
No schema changes. The surface only consumes existing decision, execution order, order state, command, and fill counts already queried by the runtime truth probe.

## Transactions, Consistency, Concurrency
No writes, no transactions, and no locks beyond existing read-only probe behavior.

## Authorization, Authentication, Data Security
No credentials are printed. DB access remains through existing container/env-loaded runtime entrypoints.

## Error Handling and Idempotency
Missing or partial executable-decision evidence reports a deterministic `smallest_missing_field`; repeated runs are idempotent.

## State Transition and Lifecycle
The surface distinguishes no executable decision, executable order/fill surface present, terminal order no-fill expected, and missing executable truth-chain evidence.

## Caching and Performance
No new DB queries. The surface derives from already-probed payloads.

## Logging, Monitoring, Auditing
Runtime truth artifact gains a dedicated top-level section and live runtime fact fields for automation gating.

## Testing Strategy
Add focused unit coverage for terminal order no-fill executable episodes and live fact projection. Run focused pytest, ruff on touched files, and `ruff check aats/ --fix` per repo policy.

## Migration, Rollback, Compatibility
No migration. Rollback is reverting the reporting code and generated automation state. Existing fields stay backward-compatible.

## Configuration and Environment Isolation
No configuration changes. Scope remains OKX BTC-USDT-SWAP with `none_verified` shadow benchmark.

## Code Organization and Dependencies
No new dependencies. Keep helper functions in `scripts/runtime_truth_report.py` near related truth summaries.

## Documentation and Operations Manual
This SOW documents the operational boundary for the change.

## Deployment and Acceptance Criteria
Safe-readonly fast lane applies only after focused/full validation. Deployment, if done, must use `bash scripts/deploy.sh --profile derivatives-live --skip-commit`.

Acceptance criteria:
- AC1: Runtime truth exposes `directional_executable_episode_truth`.
- AC2: Terminal order no-fill executable episodes are classified without creating a false blocker.
- AC3: Live runtime facts project the structured executable episode status and latest decision id.
- AC4: Existing latest-decision and no-order semantics surfaces remain unchanged.
