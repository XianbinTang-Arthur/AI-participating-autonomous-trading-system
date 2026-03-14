# AI Autonomous Trading System

Current state: event-driven AATS prototype with:
- local demo market + paper execution
- durable event/audit/replay path
- OKX real market data integration
- OKX read-only account integration
- guarded OKX execution adapter with dry-run-by-default blocking
- full-loop `real_market_paper` execution using local paper fills under real market conditions

This is not production-ready live trading. Real order submission remains disabled by default.

## Modes

The repository now supports four config profiles:
- `local_demo`: demo market data + paper execution
- `real_market_paper`: OKX real market data + read-only account + paper execution
- `guarded_live_blocked`: OKX real market data + read-only account + OKX order payload/signing path, but submission blocked by default
- `guarded_live_enabled`: same guarded-live path with explicit live submit enabled

The effective operating state is exposed by `GET /system/mode` and `GET /system/health` as one of:
- `local_demo`
- `real_market_paper`
- `guarded_live_blocked`
- `guarded_live_enabled`

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
```

If you use PostgreSQL-backed storage:

```powershell
psql postgresql://aats:aats@localhost:5432/aats -f migrations/0001_postgres_storage.sql
psql postgresql://aats:aats@localhost:5432/aats -f migrations/0002_execution_and_audit_correlation.sql
```

## Local Demo Mode

Default profile is safe local demo mode.

```powershell
.\.venv\Scripts\python.exe scripts/run_local.py --iterations 6 --interval-seconds 0
```

Recommended env:

```powershell
$env:AATS_CONFIG_PROFILE="local_demo"
```

## Real-Market Paper Mode

This uses OKX public websocket market data and OKX read-only account REST calls, but keeps execution on the local paper adapter.

`real_market_paper` now means:
- real OKX market data
- real OKX demo-account read
- local paper execution
- local `OrderIntent -> FillEvent -> PortfolioSnapshot -> ReconciliationReport`
- no OKX order submission
- no OKX simulated submit

Mode semantics are runtime-driven:
- `mode=paper_live` always routes eligible intents to the local `PaperExecutionAdapter`
- `mode=guarded_live` is required before the guarded OKX adapter is even selected
- `live_submit_enabled=false` remains the safe default and still blocks all real submit paths

Required env:
- `AATS_CONFIG_PROFILE=real_market_paper`
- `AATS_MARKET_DATA_BACKEND=okx`
- `AATS_ACCOUNT_BACKEND=okx`
- `AATS_ACCOUNT_READ_ENABLED=true`
- `AATS_OKX_API_KEY`
- `AATS_OKX_API_SECRET`
- `AATS_OKX_API_PASSPHRASE`

Run the API runtime:

```powershell
$env:AATS_CONFIG_PROFILE="real_market_paper"
.\.venv\Scripts\python.exe -m uvicorn apps.api_gateway.main:app --reload
```

The API lifespan starts:
- OKX public websocket market stream
- periodic OKX account refresh loop
- event-driven feature and decision flow
- local paper execution and portfolio mutation after eligible decisions
- reconciliation after fill-driven portfolio updates

## Guarded Live Blocked Mode

This enables the OKX execution adapter, request signing, instrument validation, and exact dry-run payload generation, but submission remains blocked.

```powershell
$env:AATS_CONFIG_PROFILE="guarded_live_blocked"
.\.venv\Scripts\python.exe -m uvicorn apps.api_gateway.main:app --reload
```

Behavior:
- policy/risk/health still gate every execution path
- order intents can produce `DRY_RUN` order states showing the exact payload that would be sent
- no real order is submitted

This differs from `real_market_paper`:
- `real_market_paper` generates local paper fills and mutates the local portfolio
- guarded-live blocked produces dry-run OKX order states only and does not create fills

## Guarded Live Enabled Mode

Real submission is only allowed when all of the following are true:
- profile or env selects `guarded_live_enabled`
- runtime mode is `guarded_live`
- `AATS_EXECUTION_BACKEND=okx`
- `AATS_LIVE_SUBMIT_ENABLED=true`
- `AATS_GUARDED_EXECUTION_DRY_RUN=false`
- OKX credentials are configured
- policy, risk, and health checks pass

Example:

```powershell
$env:AATS_CONFIG_PROFILE="guarded_live_enabled"
.\.venv\Scripts\python.exe -m uvicorn apps.api_gateway.main:app --reload
```

This path is still intentionally constrained:
- only OKX Spot
- current MVP symbol universe only
- no cancel-all / flatten automation
- no unrestricted autonomous live trading

## Replay

Replay remains deterministic and validates:
- portfolio reconstruction integrity
- decision-chain completeness
- execution-chain integrity
- audit-reference integrity

```powershell
.\.venv\Scripts\python.exe scripts/replay_session.py --bootstrap-local-loop --iterations 4
```

Replay output includes:
- `portfolio_issues`
- `decision_chain_issues`
- `execution_chain_issues`
- `audit_issues`

## API Surface

Current operator-facing endpoints:
- `GET /system/health`
- `GET /system/mode`
- `POST /system/mode`
- `POST /system/halt`
- `POST /system/resume`
- `GET /portfolio`
- `GET /positions`
- `GET /orders/open`
- `GET /reconciliation/latest`
- `GET /decision/latest`
- `GET /audit/latest`
- `GET /audit/{decision_id}`

`/system/health` now reports:
- market data connectivity and freshness
- account connectivity and freshness
- execution adapter readiness
- reconciliation freshness
- current blockers
- execution summary counts for orders and fills

`GET /decision/latest` now reports:
- one internally consistent `decision_id` chain
- latest health snapshot for that decision
- latest decision context
- latest policy decision
- latest risk decision
- latest execution plan
- latest audit record
- latest order intent and fill event for the same decision when they exist
- execution summary counts

## Important Safety Defaults

Defaults remain conservative:
- `market_data_backend=demo`
- `execution_backend=paper`
- `account_read_enabled=false`
- `live_submit_enabled=false`
- `guarded_execution_dry_run=true`

Event persistence defaults to visibility-first behavior:
- `AATS_EVENT_PERSISTENCE_MODE=strict`

## What Is Implemented

- OKX public websocket integration for `tickers`, `candle15m`, and `candle1H`
- reconnecting market stream with liveness/freshness status
- event-driven feature generation from real market snapshots
- decision trigger gating with duplicate suppression and material-change checks
- persisted `HealthSnapshot` events referenced by `DecisionContext.health_snapshot_ref`
- OKX read-only REST integration for balances, positions, open orders, account config, and instruments
- full-loop `real_market_paper` path with `RiskDecision`, `OrderIntent`, paper fills, portfolio mutation, and reconciliation
- explicit `ExecutionPlan` events between approved target positions and order intents
- guarded OKX execution adapter with signing, payload building, validation, dry-run, and explicit live-submit gate
- stronger mode-aware policy/risk/health blocking
- expanded operator API surface

## What Remains Intentionally Limited

- AI inference is still a deterministic stub
- reconciliation repair remains a TODO
- no Kafka or distributed runtime
- no multi-exchange support
- no production-grade live order lifecycle management beyond guarded submission
- no autonomous live rollout
- paper execution still uses local fill simulation rather than exchange-side simulated order placement
- `DecisionAuditRecord` still does not store a dedicated `execution_plan_ref`; execution plans are currently recovered from the event store by `decision_id`

## Reference

Implementation aligned to current OKX API V5 documentation:
- Public websocket channels and websocket URLs: https://app.okx.com/docs-v5/en/
- REST authentication and signing headers: https://app.okx.com/docs-v5/en/
- Account balance / positions / open orders / instruments / place order endpoints: https://app.okx.com/docs-v5/en/
