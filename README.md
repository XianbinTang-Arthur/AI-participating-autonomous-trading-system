# AI Autonomous Trading System

Current state: event-driven AATS prototype with:
- local demo market + paper execution
- durable event/audit/replay path
- OKX real market data integration
- OKX read-only account integration
- guarded OKX execution adapter with dry-run-by-default blocking and demo-environment simulated submit support
- full-loop `real_market_paper` execution using local paper fills under real market conditions
- governed AI integration with strict structured-output validation and fallback modes

This is not production-ready real-money live trading.

Current real-money live status:
- supported: `local_demo`, `real_market_paper`, guarded OKX demo simulated submit
- intentionally unsupported: any real-money OKX live trading path
- default behavior: exchange submission blocked unless guarded demo-submit gates are explicitly enabled

The code currently blocks real-money live trading in policy and mode handling:
- `autonomous_live` is rejected by the runtime mode controller
- non-simulated guarded live submit is blocked with `real_money_live_not_supported`

## Modes

The repository now supports six config profiles:
- `local_demo`: demo market data + paper execution
- `real_market_paper`: OKX real market data + read-only account + paper execution
- `guarded_simulated_submit_dry_run`: OKX real market data + read-only demo account + OKX simulated submit path selected, but exchange submission blocked and rendered as dry-run
- `guarded_simulated_submit_enabled`: OKX real market data + read-only demo account + explicitly enabled OKX simulated submit
- `guarded_live_blocked`: OKX real market data + read-only account + OKX order payload/signing path, but submission blocked by default
- `guarded_live_enabled`: reserved future real-live profile; real-money submit remains blocked in this prototype

The effective operating state is exposed by `GET /system/mode` and `GET /system/health` as one of:
- `local_demo`
- `real_market_paper`
- `guarded_simulated_submit_dry_run`
- `guarded_simulated_submit_enabled`
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
psql postgresql://aats:aats@localhost:5432/aats -f migrations/0003_audit_execution_plan_refs.sql
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

Default OKX endpoint behavior:
- defaults target the OKX global endpoints:
  - `AATS_OKX_REST_URL=https://www.okx.com`
  - `AATS_OKX_PUBLIC_WS_URL=wss://ws.okx.com:8443/ws/v5/public`
  - `AATS_OKX_BUSINESS_WS_URL=wss://ws.okx.com:8443/ws/v5/business`
- if you use a different regional OKX environment, override these three values explicitly in `.env`

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

## Guarded Simulated Dry-Run Mode

This enables the OKX execution adapter against the OKX demo environment, request signing, instrument validation, and exact dry-run payload generation, but exchange submission remains blocked.

```powershell
$env:AATS_CONFIG_PROFILE="guarded_simulated_submit_dry_run"
.\.venv\Scripts\python.exe -m uvicorn apps.api_gateway.main:app --reload
```

Behavior:
- policy/risk/health still gate every execution path
- order intents can produce `DRY_RUN` order states showing the exact demo-order payload that would be sent
- no OKX order is submitted

This differs from `real_market_paper`:
- `real_market_paper` generates local paper fills and mutates the local portfolio
- guarded simulated dry-run produces dry-run OKX order states only and does not create fills

## Guarded Simulated Enabled Mode

This is the only mode in the current repository that can submit orders to OKX, and it submits only to the OKX demo trading environment with `x-simulated-trading: 1`.

Submission is allowed only when all of the following are true:
- profile or env selects `guarded_simulated_submit_enabled`
- runtime mode is `guarded_live`
- `AATS_EXECUTION_BACKEND=okx`
- `AATS_OKX_SIMULATED_TRADING=true`
- `AATS_LIVE_SUBMIT_ENABLED=true`
- `AATS_GUARDED_EXECUTION_DRY_RUN=false`
- OKX demo credentials are configured
- symbol is allowlisted
- per-order notional and open-order caps pass
- health, reconciliation, and halt gates pass

Example:

```powershell
$env:AATS_CONFIG_PROFILE="guarded_simulated_submit_enabled"
.\.venv\Scripts\python.exe -m uvicorn apps.api_gateway.main:app --reload
```

What this mode does:
- submits the built order payload to OKX demo trading only
- ingests exchange-backed order state on submit and during sync polling
- ingests exchange fills when present
- updates local portfolio from exchange fills
- runs exchange-aware reconciliation
- persists audit, replay, and reconciliation artifacts for the decision
- maintains an explicit local lifecycle across `CREATED`, `SUBMITTING`, `SUBMITTED`, `PARTIALLY_FILLED`, `FILLED`, `CANCEL_PENDING`, `CANCELED`, `REJECTED`, `FAILED`, and `EXPIRED`
- merges repeated order-state updates idempotently so exchange polling cannot regress terminal states
- supports multi-fill partial-fill ingestion with duplicate-fill protection
- restores persisted order/fill/portfolio state on restart
- rebuilds the latest portfolio snapshot from persisted fills if the snapshot is missing
- restores the latest reconciliation context when available
- enters a safe halted startup state if recovery finds divergence, missing reconciliation context for recovered execution state, stale reconciliation context, or recovered open orders that require operator review

Environment note:
- guarded simulated submit expects OKX demo-trading credentials
- the runtime adds `x-simulated-trading: 1` automatically when `AATS_OKX_SIMULATED_TRADING=true`
- if you use the OKX global account environment, keep the default `www.okx.com` and `ws.okx.com` endpoints
- if you use a regional OKX environment, override the REST and websocket URLs in `.env`

What this mode still does not do:
- only OKX Spot
- current MVP symbol universe only
- no real-money trading
- no cancel-all / flatten automation
- no unrestricted autonomous live trading

## Reserved Guarded Live Profiles

`guarded_live_blocked` and `guarded_live_enabled` remain in the repo to preserve the profile layout, but real-money live trading is still intentionally unsupported here.

If `AATS_OKX_SIMULATED_TRADING=false`, policy and adapter safeguards will block actual submission.

## Replay

Replay remains deterministic and validates:
- portfolio reconstruction integrity
- decision-chain completeness
- execution-chain integrity
- audit-reference integrity
- explicit `HealthSnapshot`, `ExecutionPlan`, `OrderState`, and `FillEvent` references inside the audit chain

```powershell
.\.venv\Scripts\python.exe scripts/replay_session.py --bootstrap-local-loop --iterations 4
```

Replay filters are available for forensic use:

```powershell
.\.venv\Scripts\python.exe scripts/replay_session.py --decision-id decision_xxx
.\.venv\Scripts\python.exe scripts/replay_session.py --start-at 2026-03-14T12:00:00Z --end-at 2026-03-14T12:15:00Z
```

Replay output includes:
- `portfolio_issues`
- `decision_chain_issues`
- `execution_chain_issues`
- `audit_issues`

## Operator API Surface

The repository now exposes a practical operator-facing JSON control surface. These endpoints are intended to be usable without reading source code.

## Lightweight Operator Frontend

The API gateway now also serves a lightweight operator frontend:
- `GET /`
- `GET /ui`

Frontend characteristics:
- no separate Node build step
- static HTML + CSS + vanilla JavaScript, served directly by FastAPI
- works against the existing operator APIs
- supports optional `X-AATS-API-Key` entry and local browser storage
- graceful partial refresh: one failing panel does not blank the whole page
- richer operator layout:
  - top-level navigation split into `System`, `Trading`, `Execution`, and `Diagnostics`
  - per-page secondary navigation for quick section jumps
  - system overview and runtime metrics separated from trading and execution views
  - sticky side inspector for decision/order/fill drill-down across pages
- safe manual actions:
  - halt
  - resume
  - active reconciliation validation

The frontend is intentionally lightweight. It is meant to be operator-usable and resilient, not a large SPA.

System and control:
- `GET /system/health`
- `GET /system/mode`
- `GET /system/runtime`
- `GET /system/blockers`
- `GET /system/metrics`
- `GET /system/recovery`
- `POST /system/mode`
- `POST /system/halt`
- `POST /system/resume`

Decision, policy, and risk visibility:
- `GET /decision/latest`
- `GET /decision/recent`
- `GET /decision/{decision_id}`
- `GET /policy/latest`
- `GET /policy/recent`
- `GET /risk/latest`
- `GET /risk/recent`

Portfolio and account visibility:
- `GET /portfolio`
- `GET /portfolio/latest`
- `GET /portfolio/history`
- `GET /balances`
- `GET /positions`
- `GET /account/state`
- `GET /account/open-orders`
- `GET /account/recent-fills`

Execution visibility and manual intervention:
- `GET /orders/open`
- `GET /orders/latest`
- `GET /orders/recent`
- `GET /orders/partial`
- `GET /orders/canceled`
- `GET /orders/{client_order_id}`
- `POST /orders/{client_order_id}/cancel`
- `GET /fills/latest`
- `GET /fills/recent`
- `GET /fills/{fill_id}`
- `GET /execution/latest`
- `GET /execution/result/latest`
- `GET /execution/errors`

Reconciliation and audit:
- `GET /reconciliation/latest`
- `GET /reconciliation/recent`
- `GET /reconciliation/mismatches`
- `GET /reconciliation/{reconciliation_id}`
- `POST /reconciliation/validate`
- `GET /audit/latest`
- `GET /audit/{decision_id}`
- `GET /replay/status`
- `GET /replay/recent-validations`
- `POST /replay/validate/{decision_id}`

Key operator semantics:
- `/system/health` exposes overall status, runtime state, blockers, warnings, subsystem freshness, execution summary, and recovery status
- `/system/mode` exposes the effective mode contract, including:
  - config profile
  - current mode
  - operating state
  - execution, account, and market-data backends
  - AI operating mode
  - whether exchange submit is allowed
  - whether submit is blocked only for safety or by mode design
- `/system/blockers` classifies blockers into:
  - execution-affecting blockers
  - account-sync blockers
  - submit-only blockers
  - recommended operator action
  - recent persisted blocker history
- `/decision/{decision_id}` returns an operator-usable linked chain for:
  - health snapshot
  - decision context
  - baseline and AI assessments
  - target, policy, and risk decisions
  - execution plan
  - order intents and order-state updates
  - fills
  - portfolio snapshot
  - reconciliations
- `/audit/{decision_id}` returns the latest `DecisionAuditRecord` plus linked objects for the full decision chain
- `/execution/errors` returns structured recent execution failures and rejections, backed by persisted error summaries
- `/replay/status` and `/replay/validate/{decision_id}` expose the currently implemented replay-validation capability only; they do not claim recovery features that do not exist
- `/reconciliation/validate` triggers an explicit reconciliation validation pass after startup or operator review, instead of relying only on the last stored report

Operator API authentication:
- header: `X-AATS-API-Key`
- when `AATS_OPERATOR_AUTH_ENABLED=true`:
  - read endpoints accept either the read key or write key
  - write endpoints require the write key
- config:
  - `AATS_OPERATOR_READ_API_KEY`
  - `AATS_OPERATOR_WRITE_API_KEY`
- when auth is disabled, the API remains open for local development

Blocked vs degraded vs halted:
- `healthy`: no active execution blockers and no warning-only subsystem degradation
- `degraded`: warning-level subsystem issues are present, but execution is not blocked
- `blocked`: execution blockers are present
- `halted`: the operator kill switch is active

Mode semantics are explicit:
- `local_demo`: local demo market plus local paper execution; exchange submit intentionally unavailable
- `real_market_paper`: real OKX market data plus read-only account and local paper execution; exchange submit intentionally unavailable
- `guarded_simulated_submit_dry_run`: OKX demo submit path selected, but submit intentionally blocked and rendered as dry-run
- `guarded_simulated_submit_enabled`: OKX demo submit allowed only when all configured gates pass
- `guarded_live_blocked` and `guarded_live_enabled`: reserved future real-money profile shapes; real-money submit remains unsupported

Structured logs now standardize the main correlation fields when available:
- `decision_id`
- `intent_id`
- `order_id`
- `fill_id`

Startup now also creates a local rotating file-log layout under `logs/`:
- `logs/runtime/aats.log`: full runtime stream at or above `AATS_LOG_LEVEL`
- `logs/debug/debug.log`: debug-only events when `AATS_LOG_LEVEL=DEBUG`
- `logs/info/info.log`: info-level events
- `logs/warning/warning.log`: warning-level events
- `logs/error/error.log`: error and critical events

Logging config:
- `AATS_LOG_LEVEL`
- `AATS_LOG_DIR`
- `AATS_LOG_ROTATE_MAX_BYTES`
- `AATS_LOG_BACKUP_COUNT`

This keeps console logs for active debugging while making backend service logs inspectable after restart.

## AI Modes

AI remains inside the existing decision chain and never bypasses policy, risk, or execution controls.

Supported operating modes:
- `baseline_only`: never calls an external model; emits an audit-friendly fallback `AIMarketAssessment`
- `ai_advisory`: calls the configured provider when available, but target generation still follows baseline
- `ai_blended`: AI must align with baseline before target generation uses it
- `ai_primary`: AI can drive target direction when valid and above the configured confidence threshold; otherwise it falls back to baseline

Current provider support:
- `disabled`
- `openai`

Safety and reliability behavior:
- strict structured output schema enforcement
- timeout fallback
- invalid output fallback
- degrade after repeated failures
- recover after configured successful calls
- assessment metadata remains auditable through `AIMarketAssessment`

Required env for OpenAI provider:
- `AATS_AI_PROVIDER=openai`
- `AATS_AI_OPERATING_MODE=ai_advisory|ai_blended|ai_primary`
- `AATS_OPENAI_API_KEY`
- optional `AATS_OPENAI_BASE_URL`

Safe defaults remain:
- `AATS_AI_PROVIDER=disabled`
- `AATS_AI_OPERATING_MODE=baseline_only`

`GET /system/mode` now reports the explicit mode contract:
- `market_data_source`
- `account_read_source`
- `execution_route`
- `exchange_submit_target`
- `exchange_submit_allowed`
- `submit_blocked_reasons`

## Important Safety Defaults

Defaults remain conservative:
- `market_data_backend=demo`
- `execution_backend=paper`
- `account_read_enabled=false`
- `live_submit_enabled=false`
- `guarded_execution_dry_run=true`
- `okx_simulated_trading=false`
- `operator_auth_enabled=false`

Event persistence defaults to visibility-first behavior:
- `AATS_EVENT_PERSISTENCE_MODE=strict`

Operator persistence now records compact summaries into the existing event store for:
- blocker snapshots
- operator halt/resume actions
- execution errors
- replay validations
- explicit reconciliation validations

## What Is Implemented

- OKX public websocket integration for `tickers`, `candle15m`, and `candle1H`
- reconnecting market stream with liveness/freshness status
- event-driven feature generation from real market snapshots
- decision trigger gating with duplicate suppression and material-change checks
- persisted `HealthSnapshot` events referenced by `DecisionContext.health_snapshot_ref`
- OKX read-only REST integration for balances, positions, open orders, account config, and instruments
- full-loop `real_market_paper` path with `RiskDecision`, `OrderIntent`, paper fills, portfolio mutation, and reconciliation
- explicit `ExecutionPlan` events between approved target positions and order intents
- explicit `execution_plan_ref` and `order_state_refs` in `DecisionAuditRecord`
- guarded OKX execution adapter with signing, payload building, validation, dry-run, explicit simulated-submit gates, and exchange lifecycle sync
- hardened order lifecycle handling for partial fills, cancellations, idempotent order updates, and restart recovery
- exchange-aware reconciliation for OKX simulated submit mode
- stronger mode-aware policy/risk/health blocking
- expanded operator API surface
- explicit recovery status visibility via `GET /system/health`, `GET /execution/latest`, and `GET /system/recovery`
- structured operator blocker summaries, runtime metrics, and replay validation status
- read/write operator API auth hooks and persisted operator-history summaries

## What Remains Intentionally Limited

- AI integration is provider-backed and audited, but still intentionally conservative and governance-bound
- reconciliation repair remains a TODO
- no Kafka or distributed runtime
- no multi-exchange support
- no production-grade live order lifecycle management beyond guarded submission
- no autonomous live rollout
- no real-money live trading support
- `real_market_paper` still uses local fill simulation rather than exchange-side simulated order placement
- exchange reconciliation against balances/positions is only meaningful when `bootstrap_portfolio_from_exchange=true`
- amend workflows and private websocket execution streams are not implemented yet
- restart recovery rebuilds local state safely, but does not yet perform automated exchange-side remediation
- recovery prefers safe degraded startup over ambiguous resume; operator review is still required for recovered open orders

## Reference

Implementation aligned to current OKX API V5 documentation:
- Public websocket channels and websocket URLs: https://app.okx.com/docs-v5/en/
- REST authentication and signing headers: https://app.okx.com/docs-v5/en/
- Account balance / positions / open orders / instruments / place order endpoints: https://app.okx.com/docs-v5/en/
