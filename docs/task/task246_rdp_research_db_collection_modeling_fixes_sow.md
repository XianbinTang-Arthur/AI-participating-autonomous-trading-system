# Task246: RDP research DB collection and modeling fixes

## Business Objectives And Boundaries

Fix the research database issues exposed by the `aats_research` audit without changing live trading behavior. The task is limited to RDP collection governance, schema/modeling hygiene, and observable data semantics.

Out of scope: strategy logic, risk gates, order execution, AI provider behavior, symbol/venue/family expansion, release/promotion/tuning, and runtime timeframe plumbing.

## Module Responsibilities And Domain Model

- `meta.ingest_runs`: lifecycle truth for long-running collectors and batch jobs.
- `staging.raw_liquidations`: OKX liquidation raw stream; now explicitly marks whether a row is the fixed trading instrument or broad-market context.
- `meta.*` RDP metadata models: must admit `microstructure` as a first-class data domain where migrations already do so.
- `bronze.market_orderbook_payloads`: schema-only sidecar until runtime collector payload writes are implemented.

## Input/Output Interfaces

Inputs:
- Existing SQLAlchemy sessions and RDP migrations.
- Existing OKX liquidation and microstructure collectors.
- Existing Batch B migration runner.

Outputs:
- New/updated ORM columns and constraints.
- A Batch B migration stage for RDP modeling hygiene.
- Collector lifecycle reconciliation before new microstructure daemon run creation.
- Unit tests covering SQL emission and source-level lifecycle contracts.

## Database Schema / Tables / Indexes / Constraints

- Add `staging.raw_liquidations.source_scope`.
- Add source-scope check/index for `raw_liquidations`.
- Backfill existing BTC-USDT-SWAP liquidation rows as `fixed_trading_scope`; other rows remain `broad_market_context`.
- Widen RDP metadata domain constraints to include `microstructure` consistently.
- Add future-row FK enforcement for `bronze.market_orderbook_payloads.ingest_run_id`.
- Add table comments documenting dormant/currently-unwired collection surfaces.

## Transactions, Consistency, Concurrency

Migrations are idempotent and run in a single transaction. Collector lifecycle reconciliation updates only stale `running` microstructure daemon runs before creating a new run; it does not touch active order state or trading tables.

## Authorization, Authentication, Data Security

No secrets are read or printed. DB validation uses existing project/container entry points only.

## Error Handling And Idempotency

- Migration uses `DROP CONSTRAINT IF EXISTS`, `ADD COLUMN IF NOT EXISTS`, `CREATE INDEX IF NOT EXISTS`, and guarded `DO` blocks.
- Microstructure orphan reconciliation returns a row count and logs failures via existing startup error flow.
- Liquidation writes remain idempotent through the existing natural-key conflict clause.

## State Transition And Lifecycle

Before a new microstructure daemon ingest run is created, older `running` daemon rows for the same domain are closed as `failed` with an explicit orphan reason. Normal shutdown still derives `succeeded`, `retrying`, or `failed` from write/error/drop counters.

## Caching And Performance

The new liquidation scope index supports scoped queries without changing write semantics. The migration backfill is bounded to the existing liquidation table size and is not on the order path.

## Logging, Monitoring, Auditing

Startup logs include the number of orphaned microstructure runs reconciled. Table comments document dormant/unwired collection surfaces so audit output can distinguish expected empty tables from collection failures.

## Testing Strategy

- Unit tests for microstructure ingest lifecycle source contracts.
- Unit tests for liquidation insert SQL and source-scope parameter.
- Unit tests for Batch B stage registration and migration SQL content.
- Narrow lint and pytest validation.

## Migration, Rollback, Compatibility

New Batch B stage: `batch_b_13_rdp_collection_modeling_hygiene`.
Rollback drops the new liquidation source-scope column, removes the orderbook payload FK, and restores previous metadata checks.

## Configuration And Environment Isolation

No new environment variables. No `.env` content is printed.

## Code Organization And Dependencies

Changes stay within existing RDP modules:
- `aats/data_platform/jobs/run_registry.py`
- `aats/data_platform/collectors/*`
- `aats/data_platform/rdp_models.py`
- `aats/data_platform/migrations/*`
- focused unit tests

## Documentation And Operations Manual

This SOW records the operational boundary. Runtime payload sidecar writes and hard FK validation across high-volume historical tables remain separate runtime-affecting tasks.

## Deployment And Acceptance Criteria

Acceptance is binary:
- Unit tests pass for changed RDP collector/schema contracts.
- Batch B stage 13 is registered and SQL/rollback files exist.
- No code path modifies trading decisions, risk gates, or order execution.
