# AI Autonomous Trading System

Current state: event-driven AATS prototype with:
- local demo market + paper execution
- durable event/audit/replay path
- OKX real market data integration
- OKX read-only account integration
- guarded OKX execution adapter with dry-run-by-default blocking and demo-environment simulated submit support
- explicit spot vs derivatives runtime layering on one shared trading mainline
- product-aware execution, portfolio, risk, and recovery schemas
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
- `guarded_simulated_submit_spot_dry_run`
- `guarded_simulated_submit_spot_enabled`
- `guarded_simulated_submit_derivatives_dry_run`
- `guarded_simulated_submit_derivatives_enabled`
- `guarded_live_blocked`
- `guarded_live_enabled`

## Unified Mainline And Runtime Layering

The repository now keeps one shared trading mainline across simulated paths:

1. market data ingress
2. feature calculation
3. decision creation
4. policy evaluation
5. risk evaluation
6. execution planning
7. order intent emission
8. adapter submission or synchronization
9. portfolio update
10. reconciliation and recovery
11. audit, replay, and operator controls

Environment differences are now resolved once at startup and exposed through four explicit layers:

- `runtime_profile`
  - `paper_local`
  - `exchange_simulated_spot`
  - `exchange_simulated_derivatives`
  - `exchange_live_reserved`
- `environment_capabilities`
  - product type
  - market-data source kind
  - account-state source kind
  - execution adapter kind
  - exchange submission target and enablement
  - position directionality
  - leverage support
  - margin model
  - persistent-storage requirement
- `policy_profile`
  - whether exchange submission is allowed in principle
  - whether the current posture is dry-run only
  - whether health and review blockers gate execution
  - whether shorting and leverage are allowed
  - maximum target leverage
- `recovery_policy`
  - whether startup baseline import is supported
  - whether operator rebaseline is supported
  - whether review-required state blocks resume
  - whether derivatives position comparison is required

`GET /system/mode`, `GET /system/runtime`, and `GET /system/health` now expose these resolved structures directly. The dashboard renders from that resolved posture instead of reconstructing semantics from raw flags.

Behavior by profile:

- `paper_local`
  - shared decision, policy, risk, and portfolio pipeline
  - paper execution only
  - exchange submission unavailable by design
  - may still observe exchange market or account state, but operator rebaseline is not part of the normal paper-control surface
- `exchange_simulated_spot`
  - shared business mainline
  - OKX demo/simulated execution adapter
  - reconciliation, account sync, baseline import, rebaseline, and resume workflows remain available
  - dry-run vs enabled submit is expressed through the resolved environment and policy layers
- `exchange_simulated_derivatives`
  - shared business mainline
  - derivatives-capable guarded simulated posture
  - shorting, explicit reversal, and leverage become first-class runtime and risk concepts
  - exchange submission remains simulated only; real-money live is still blocked
- `exchange_live_reserved`
  - keeps the future exchange-live structural shape explicit
  - real-money submission remains blocked
  - the profile is useful only as a reserved architecture boundary, not as an enabled trading mode

Strategy behavior is now product-aware:
- spot cash stays long-only by default
- bearish spot signals stop scaling in; they do not force impossible short reversals
- flat spot signals decay existing exposure instead of immediately churning to zero
- derivatives mode can express short targets, explicit reversals, and leverage-aware intents

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
psql postgresql://aats:aats@localhost:5432/aats -f migrations/0004_operator_users.sql
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
- `AATS_STORAGE_MODE` is persistent; guarded simulated submit is rejected when `storage_mode=memory`
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

Product posture in this mode:
- `AATS_TRADING_PRODUCT_TYPE=spot` resolves to `exchange_simulated_spot`
- `AATS_TRADING_PRODUCT_TYPE=derivatives` resolves to `exchange_simulated_derivatives`
- derivatives posture enables short-side and leverage-aware risk checks while keeping submit inside the simulated environment only

## Operational Product Switch

This workspace is now set up for one primary exchange-coupled runtime, not two concurrent runtimes on one account.

Recommended operating model:
- keep one main instance
- use the derivatives posture as the primary runtime when you want automatic long/short and leverage-aware posture
- switch the same runtime back to spot by replacing `.env` with the spot preset when needed
- switch back to derivatives by replacing `.env` with the derivatives preset
- restart the API after each posture change

Env preset flow:
- keep these two local preset files next to `.env`:
  - `.env.spot.backup`
  - `.env.derivatives.primary`
- switch to spot with:

```powershell
Copy-Item .env.spot.backup .env -Force
```

- switch to derivatives with:

```powershell
Copy-Item .env.derivatives.primary .env -Force
```

- restart the API after the copy completes
- open `/ui`
- run `Reconciliation Validate`
- if the posture switch changed how the account is interpreted, perform `Rebaseline` and then `Resume` only after the runtime is clean again

Boot-critical values stay in `.env` and are not editable from the browser:
- `AATS_DATABASE_URL`
- OKX API credentials
- operator session secret
- host / port binding

Important:
- do not expect clean isolation from two exchange-coupled instances sharing one OKX account and one database
- prefer one primary runtime and switch posture through the env preset files
- do not rely on the browser to change trading posture
- real-money live trading remains blocked even in derivatives posture

Environment note:
- guarded simulated submit expects OKX demo-trading credentials
- the runtime adds `x-simulated-trading: 1` automatically when `AATS_OKX_SIMULATED_TRADING=true`
- if you use the OKX global account environment, keep the default `www.okx.com` and `ws.okx.com` endpoints
- if you use a regional OKX environment, override the REST and websocket URLs in `.env`

What this mode still does not do:
- real-money derivatives trading
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
- supports operator login and authenticated browser sessions
- includes an admin-only Operators view for database-backed account creation, role changes, password resets, disable/enable, and deletion
- preserves API-key compatibility for automation and non-browser tooling
- graceful partial refresh: one failing panel does not blank the whole page
- richer operator layout:
  - one minimal control page instead of many always-open panels
  - compact summary strip for system, portfolio, decision, and execution state
  - on-demand inspector drawer for decision/order/fill/reconciliation detail
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
- `POST /system/rebaseline`

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
- reconciliation now exposes explicit mismatch categories such as:
  - `historical_state_only`
  - `external_manual_activity_detected`
  - `local_fill_missing`
  - `local_balance_divergence`
  - `local_position_divergence`
  - `local_open_order_divergence`
  - `unsafe_unknown_state`
- reconciliation severity is graded as:
  - `CLEAN`
  - `INFO`
  - `SOFT_MISMATCH`
  - `REVIEW_REQUIRED`
  - `HARD_MISMATCH`
- `REVIEW_REQUIRED` remains conservative and operator-visible, but is distinct from `HARD_MISMATCH`
- truly unsafe unknown divergence and open-order divergence still trigger `halt_required`
- `/system/recovery` now exposes explicit recovery states:
  - `normal_operation`
  - `review_required`
  - `rebaseline_pending`
  - `rebaseline_completed`
  - `resume_blocked`
- `POST /system/rebaseline` accepts the current exchange state as a new trusted baseline, persists that switch, and rebuilds the local portfolio baseline before any later resume
- `POST /system/resume` now re-runs reconciliation and re-checks recovery blockers before clearing the halt; if the runtime is still unsafe it returns `resume_blocked`

Operator API authentication:
- browser users should sign in through `/login`; the operator console uses authenticated session cookies instead of manual API-key entry
- when `AATS_OPERATOR_AUTH_ENABLED=true`, visiting `/` or `/ui` without a valid session redirects to `/login`
- session auth supports explicit operator roles:
  - `viewer`
  - `operator`
  - `admin`
- header: `X-AATS-API-Key`
- API keys remain as a compatibility path for automation and controlled non-browser access
- when `AATS_OPERATOR_AUTH_ENABLED=false`, read endpoints remain open but write endpoints are denied unless `AATS_OPERATOR_UNSAFE_WRITE_WITHOUT_AUTH=true`
- when `AATS_OPERATOR_AUTH_ENABLED=true`:
  - browser users authenticate against the stored `operator_users` account table plus `AATS_OPERATOR_SESSION_SECRET`
  - read API keys map to viewer access
  - write API keys map to admin-equivalent access
  - session-authenticated `viewer` users can read but cannot execute control actions
  - session-authenticated `operator` and `admin` users can execute control actions
  - persistent account storage:
    - migration `migrations/0004_operator_users.sql`
    - table: `operator_users`
  - bootstrap config:
    - `AATS_OPERATOR_SESSION_SECRET`
    - `AATS_OPERATOR_BOOTSTRAP_ENABLED`
    - bootstrap seed file: `docs/user.txt`
  - bootstrap behavior:
    - when `operator_users` is empty and bootstrap is enabled, the runtime reads `docs/user.txt`
    - expected file format is one account per line:
      - `viewer:<password>`
      - `operator:<password>`
      - `admin:<password>`
    - normal browser login checks the stored password hash in the database, not the raw env values
    - after first sign-in, an admin can manage stored accounts directly in `/ui` -> `Operators`
    - once the operator user table is initialized, `docs/user.txt` can be deleted if you do not want to keep it around
  - admin account management:
    - endpoint family: `/auth/users`
    - requires admin-equivalent access (session admin or write API key)
    - blocks self-delete, self-disable, and removing the last enabled admin account
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
- `guarded_simulated_submit_spot_dry_run`: OKX demo submit path for spot is selected, but submit is intentionally blocked and rendered as dry-run
- `guarded_simulated_submit_spot_enabled`: OKX demo submit is allowed for spot when all configured gates pass
- `guarded_simulated_submit_derivatives_dry_run`: derivatives-capable simulated submit path is selected, but submit is intentionally blocked and rendered as dry-run
- `guarded_simulated_submit_derivatives_enabled`: derivatives-capable simulated submit is allowed when all configured gates pass
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
- startup account baseline imports

## Startup Baseline Takeover

The runtime now supports conservative startup baseline takeover for previously used exchange accounts.

When all of the following are true:
- `bootstrap_portfolio_from_exchange=true`
- `account_backend=okx`
- `account_read_enabled=true`
- no trusted local portfolio snapshot is already present

the runtime can:
- read the current exchange balances, fills, positions/holdings, and open orders
- persist an `account.baselines` startup event
- initialize the local portfolio baseline from the imported exchange state
- expose baseline takeover state through:
  - `GET /system/health`
  - `GET /system/runtime`
  - `GET /account/state`
  - `GET /system/recovery`

Important semantics:
- imported exchange baseline state is not treated as if it were produced by the current local decision chain
- clean or non-empty accounts can both be imported as a startup baseline
- imported baselines with open orders are treated conservatively and can require operator review before normal execution resumes

## Operator-Confirmed Re-Baselining

The runtime now supports explicit operator-confirmed re-baselining after manual intervention,
historical-account divergence, or interrupted recovery.

Operator flow:
- detect divergence or `REVIEW_REQUIRED` / `HARD_MISMATCH`
- keep the runtime halted
- call `POST /system/rebaseline`
- persist a new `account.baselines` switch point with `baseline_kind=operator_rebaseline`
- rebuild the local portfolio baseline from the accepted exchange state
- run reconciliation against that new trusted baseline
- inspect `GET /system/recovery`
- call `POST /system/resume` only after the runtime is actually resume-eligible

Important semantics:
- re-baselining is explicit and auditable; it is not an automatic repair
- `rebaseline_completed` means the trusted baseline has been rebuilt, not that the runtime is already trading again
- `resume_blocked` means a resume attempt re-checked freshness, reconciliation, and recovery blockers and kept the halt in place
- baseline switch points remain visible through `GET /system/recovery`, `GET /system/runtime`, and replay validation summaries

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
