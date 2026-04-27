# Directional Episode Pretrade Microstructure Attribution SOW

## Business Objectives And Boundaries
- Objective: extend the read-only runtime truth report so recent BTC-USDT-SWAP directional episodes include pretrade microstructure context around decision and fill time.
- Boundary: this is an evidence-density improvement only. It does not change strategy logic, risk gates, execution behavior, provider behavior, symbols, venues, strategy families, release gates, promotion gates, or timeframe plumbing.

## Module Responsibilities And Domain Model
- `scripts/runtime_truth_report.py`: query existing live DB episode attribution and RDP silver microstructure bars, then project a sanitized attribution object.
- `tests/unit/scripts/test_runtime_truth_report.py`: verify summary and live-fact projections.
- Domain model: a directional episode decision can have order/fill/PnL evidence and now an attached `pretrade_microstructure` context with silver orderbook/trade-flow bars at or before decision and latest fill timestamps.

## Input/Output Interfaces
- Input: `portfolio_allocation_decisions`, `execution_orders`, `execution_fills`, `fill_outcomes`, RDP `silver.market_orderbook_metrics_15m`, and RDP `silver.market_trade_flow_15m`.
- Output: runtime truth `directional_episode_attribution_truth.pretrade_microstructure`, per-decision `pretrade_microstructure`, and live runtime facts for microstructure coverage.
- No credentials or connection strings are emitted.

## Database Schema / Tables / Indexes / Constraints
- No schema change.
- Reads existing live Postgres tables and existing RDP silver tables.
- No migrations, indexes, or constraints are added.

## Transactions, Consistency, Concurrency
- Read-only probes run against current runtime databases.
- The report is a point-in-time diagnostic and does not participate in trading-state mutation.
- No transaction semantics are changed.

## Authorization, Authentication, Data Security
- Uses existing container runtime environment for database access.
- Does not print secrets, API keys, tokens, passwords, or connection strings.
- Dashboard auth-required status remains treated as unknown runtime truth rather than inferred effective mode.

## Error Handling And Idempotency
- Missing RDP evidence returns explicit missing fields instead of raising.
- The report is idempotent for the same database state.
- Unavailable live DB or RDP DB is represented as structured missing evidence.

## State Transition And Lifecycle
- No trading lifecycle transition is changed.
- Episode lifecycle evidence is read and summarized only.

## Caching And Performance
- RDP probe returns a bounded recent silver-bar window.
- Matching is in-memory over a small list of recent 15m bars.
- No new cache layer is introduced.

## Logging, Monitoring, Auditing
- Runtime truth exposes coverage counts and smallest missing field for operator audit.
- No new logs are required.

## Testing Strategy
- Focused unit tests cover verified microstructure attachment and missing-evidence classification.
- Runtime truth focused tests validate live-fact projection fields.
- Full unit test and ruff must pass before commit.

## Migration, Rollback, Compatibility
- No migration.
- Rollback is reverting the runtime truth/reporting commit and redeploying through `bash scripts/deploy.sh --skip-commit`.
- Existing runtime truth fields remain backward compatible.

## Configuration And Environment Isolation
- No new config.
- Uses current live stack and RDP database wiring from the running gateway container.

## Code Organization And Dependencies
- No new dependencies.
- Changes stay in runtime truth script and its unit tests.

## Documentation And Operations Manual
- Operators should inspect `directional_episode_attribution_truth.recent_decisions[].pretrade_microstructure` to classify losses by signal, cost, guard, execution, and microstructure.
- `directional_1h` remains `none_verified` unless separately validated.

## Deployment And Acceptance Criteria
- Acceptance:
  - latest filled directional episodes expose pretrade orderbook/trade-flow status or exact missing field;
  - live runtime facts project microstructure coverage;
  - focused tests, ruff, full unit tests, deploy, and post-deploy runtime truth smoke pass;
  - no runtime behavior is changed.
