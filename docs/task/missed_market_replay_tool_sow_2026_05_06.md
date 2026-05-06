# Missed Market Replay Tool SOW - 2026-05-06

## Business Objectives And Boundaries

Build a read-only operator replay tool for "obvious trend was missed" incidents. The tool reconstructs live market snapshots, baseline direction, directional sleeve intents, budget/cost gates, and counterfactual PnL for a fixed time window.

This change does not modify live trading decisions, risk gates, order submission, position sizing, or deployment configuration.

## Module Responsibilities And Domain Model

- `aats.services.operator.missed_market_replay`: pure data model, aggregation, and counterfactual simulation.
- `scripts/missed_market_replay.py`: CLI wrapper that reads a live DB URL from an environment variable, queries `event_store` and execution tables, and emits Markdown or JSON.

Domain objects:

- market tick: timestamp and last price
- baseline assessment: timestamp and direction bias
- directional intent: requested target, target notional, behavior, cost estimate, budget reason codes
- cost candidate: target rejected by `expected_edge_below_cost_buffer`
- simulation result: turnover, gross PnL, estimated cost, net PnL, max quantity, target changes

## Input / Output Interfaces

Input:

- `--symbol`, default `BTC-USDT-SWAP`
- `--start`, timezone-aware timestamp
- `--end`, timezone-aware timestamp
- `--database-url-env`, default `AATS_DATABASE_URL`
- `--format markdown|json`
- optional `--output`

Output:

- Markdown report for operator review
- JSON summary for automation or later comparison

The CLI must not print the database URL or any credential-bearing environment value.

## Database Schema / Tables / Indexes / Constraints

Read-only tables:

- `event_store`
- `execution_orders`
- `execution_commands`
- `execution_fills`

Relevant `event_store` event types:

- `MarketSnapshot`
- `BaselineAssessment`
- `StrategySleeveIntent`
- `PositionTarget`
- `DecisionOutcome`

No schema changes, migrations, indexes, or constraints are introduced.

## Transactions, Consistency, Concurrency

The tool uses read-only SQL queries against a fixed timestamp window. It does not write rows or hold long transactions beyond the query duration. Results are window-stable as long as the selected time range is in the past and event ingestion has completed.

## Authorization, Authentication, Data Security

Credentials are supplied only by environment variable. The CLI validates that the environment variable exists but never prints it. Reports include event-derived diagnostics and no secrets.

## Error Handling And Idempotency

Missing database URL exits with code `2`. Query or report failures exit with code `1`. Re-running with the same window and output path overwrites only the selected report artifact.

## State Transition And Lifecycle

There is no runtime state transition. This is an offline/operator diagnostic tool.

## Caching And Performance

Queries are bounded by symbol and time window and use existing `event_store` timestamp/symbol/type indexes. No cache is added.

## Logging, Monitoring, Auditing

The generated report is the audit artifact. CLI output is intentionally concise and avoids sensitive configuration.

## Testing Strategy

Unit tests cover:

- bucket aggregation
- target-following PnL and turnover calculation
- 15-minute majority-direction counterfactual
- budget reason counting
- cost candidate simulation

Live DB access is not required for unit tests.

## Migration, Rollback, Compatibility

No migration. Rollback is removing the script, module, tests, and this SOW. Public runtime APIs are unchanged.

## Configuration And Environment Isolation

The CLI reads only the named database URL environment variable. It does not parse `.env.*.live` or `.env.wsl2`.

## Code Organization And Dependencies

Use existing project dependencies: Python standard library and SQLAlchemy. No new third-party dependency is introduced.

## Documentation And Operations Manual

Operator usage:

```powershell
.venv\Scripts\python.exe scripts\missed_market_replay.py `
  --start "2026-05-06T20:30:00+08:00" `
  --end "2026-05-07T00:30:00+08:00" `
  --database-url-env AATS_DATABASE_URL `
  --output docs/audit/missed_market_replay_2026_05_06_btcusdt.md
```

## Deployment And Acceptance Criteria

Acceptance criteria:

- CLI can generate a report from a fixed window using a DB URL supplied by environment.
- Unit tests pass without live DB access.
- Ruff passes for the touched `aats/` module.
- No trading behavior changes.

