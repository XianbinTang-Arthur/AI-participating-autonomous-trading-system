# Execution Science Orderbook Runtime Truth SOW - 2026-04-26

## Business objectives and boundaries
- Objective: expose read-only execution-science evidence for directional order book freshness, payload sequence continuity, silver orderbook bar availability, and fill-feasibility preconditions in the runtime truth report.
- Boundary: no strategy, risk, provider, schema, venue, symbol, release, promotion, tuning, timeframe, or live order behavior changes.

## Module responsibilities and domain model
- `scripts/runtime_truth_report.py` owns the no-secret runtime truth projection used by automation and PM loops.
- `aats_research` microstructure tables are the source of orderbook and execution-science evidence.
- Bronze BBO/books5 rows prove live orderbook ingestion freshness.
- `bronze.market_orderbook_payloads` proves payload persistence and collector sequence continuity.
- `silver.market_orderbook_metrics_15m` proves aggregated orderbook features are available for fill-feasibility and slippage analysis.

## Input/output interfaces
- Input: gateway container environment-loaded RDP database URL, queried inside the running container without printing credentials.
- Output: runtime truth gains `execution_science_truth` and live facts for execution-science status, smallest missing field, and orderbook sequence validation.
- Existing runtime truth fields remain backward compatible.

## Database schema / tables / indexes / constraints
- No schema, table, index, or constraint changes.
- The read-only probe touches existing `bronze.market_orderbook_bbo`, `bronze.market_orderbook_books5`, `bronze.market_orderbook_payloads`, `silver.market_orderbook_metrics_15m`, and `governance.rdp_task_queue`.

## Transactions, Consistency, Concurrency
- No transactions are introduced beyond read-only SQL statements.
- Probe results are point-in-time aggregates and tolerate concurrent collector writes.

## Authorization, Authentication, Data Security
- Credentials are read only inside the running gateway container process environment.
- The report never emits database URLs, passwords, tokens, or raw payloads.

## Error Handling and Idempotency
- Missing RDP configuration or tables degrade to explicit unavailable/missing status.
- Re-running the report is idempotent for the same runtime state.

## State Transition and Lifecycle
- No live trading state transitions are changed.
- Runtime truth only classifies execution-science evidence availability.

## Caching and Performance
- No hot-path cache is introduced.
- Queries use bounded aggregates and latest-row lookups on indexed time columns.

## Logging, Monitoring, Auditing
- Automation can now distinguish healthy orderbook/fill-feasibility evidence from missing or stale microstructure facts.
- Payload sequence gap status is surfaced for operator audit.

## Testing Strategy
- Unit tests cover no-secret command generation, probe parsing, execution-science summary status, and live facts projection.
- Runtime smoke verifies the deployed report against live data.

## Migration, Rollback, Compatibility
- No migration required.
- Rollback is a normal code revert and redeploy through `scripts/deploy.sh --skip-commit`.

## Configuration and Environment Isolation
- No new configuration is required.
- The symbol remains fixed to BTC-USDT-SWAP for the current stage.

## Code Organization and Dependencies
- Changes stay in `scripts/runtime_truth_report.py` and `tests/unit/scripts/test_runtime_truth_report.py`.
- No new dependency.

## Documentation and Operations Manual
- This SOW documents the read-only execution-science truth surface and acceptance criteria.

## Deployment and Acceptance Criteria
- Acceptance: focused runtime truth tests pass, lint passes, full unit tests pass, deployment via standard script succeeds, and post-deploy runtime truth reports verified orderbook sequence/fill-feasibility evidence or an explicit smallest missing field.
