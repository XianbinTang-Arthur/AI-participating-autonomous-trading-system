# Microstructure Silver Funding Received-At Fallback SOW

## Business Objectives and Boundaries

Fix the microstructure Silver 15m ETL so `funding_rate_current` reflects the funding state known during a bar. The change is limited to RDP research data quality. It does not affect live order placement, strategy decisions, risk checks, exchange adapters, or runtime execution.

## Module Responsibilities and Domain Model

`scripts/microstructure_ws_daemon.py` and the collector persist funding ticks into `staging.market_oi_funding_ticks`. `aats.data_platform.merge.microstructure_silver_merger` owns the Bronze/Staging to Silver 15m aggregation. Funding ticks can carry an exchange `ts` that points to a future funding timestamp while `received_at` records when the system learned that funding state.

## Input/Output Interfaces

Input remains `staging.market_oi_funding_ticks` with `tick_type='funding'`, `funding_rate`, `next_funding_rate`, `next_funding_time`, `ts`, and `received_at`. Output remains one row per `(symbol, ts)` in `silver.market_oi_funding_metrics_15m`.

## Database Schema / Tables / Indexes / Constraints

No schema migration is required. The query continues to use existing tables. The fallback query filters `tick_type='funding'`, so the existing `ix_staging_market_oif_type_ts` index narrows the scan to low-cardinality funding rows.

## Transactions, Consistency, Concurrency

The ETL remains read-only against staging and upserts one deterministic Silver row inside the caller-managed transaction. Multiple runs for the same bar keep the existing `ON CONFLICT (symbol, ts) DO UPDATE` idempotency.

## Authorization, Authentication, Data Security

No credentials or private account data are read. The source data is public market data already persisted in the RDP database.

## Error Handling and Idempotency

If no recent funding state exists, the ETL preserves the existing `funding_no_data` flag and writes null funding fields. If a funding state is found by `received_at` or recent carry-forward, it fills `funding_rate_current` and does not emit `funding_no_data`.

## State Transition and Lifecycle

No lifecycle or task-queue state changes are introduced. `microstructure_silver_15m` continues to run every 15 minutes through the existing scheduler.

## Caching and Performance

No cache changes. The fallback only checks funding rows, which are much lower volume than mark and OI ticks. A 12-hour carry window prevents indefinite stale funding carry-forward.

## Logging, Monitoring, Auditing

Existing `COMMITTED` / `COMMITTED_BUT_EMPTY` logging remains unchanged. Removing false `funding_no_data` flags prevents healthy bars from being labeled empty solely because funding `ts` points to the next funding event.

## Testing Strategy

Add unit coverage for preserving bar-local funding by exchange `ts`, using a funding tick received in the bar when its exchange `ts` is future-dated, carrying forward the latest recent funding state before the bar closes, and keeping `funding_no_data` when the last funding state is beyond the carry window.

## Migration, Rollback, Compatibility

Rollback is a normal git revert. There is no migration. Public function signatures and workflow configuration remain compatible.

## Configuration and Environment Isolation

No configuration changes. The fixed carry window is code-local to the Silver ETL and independent of live trading environment variables.

## Code Organization and Dependencies

The implementation stays inside `aats/data_platform/merge/microstructure_silver_merger.py` with test fixture support in `tests/unit/data_platform/_silver_test_helpers.py`.

## Documentation and Operations Manual

This SOW documents the operational intent. Existing runbooks for `microstructure_silver_15m` remain valid.

## Deployment and Acceptance Criteria

Acceptance requires lint success, targeted OI/funding unit tests, full unit tests, and the narrowest WSL2 microstructure integration test. In production, new bars with received funding ticks should no longer be flagged `funding_no_data` solely due to future exchange funding timestamps.
