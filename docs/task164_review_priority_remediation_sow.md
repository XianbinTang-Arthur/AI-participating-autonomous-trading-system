# Task 164: Review Priority Remediation For Fees, Submit Idempotency, And Operator Auth Hardening

## Business objectives and boundaries
- Fail closed when a fill fee currency cannot be mapped into the symbol's base/quote accounting model.
- Align submit-command idempotency with the stable order/business key instead of the transient `intent_id`.
- Harden exchange-coupled operator session startup so browser auth cannot run with insecure cookies.
- Tighten write API key compatibility so it remains a local-development escape hatch instead of a broad non-prod admin path.
- Keep the patch scoped to accounting, execution command flow, startup validation, auth gating, and directly related tests.

## Module responsibilities and domain model
- `aats/services/accounting.py`
  - Canonical fee-currency resolution and quote-cost conversion for fills.
- `aats/services/portfolio_service/positions.py`
  - Applies fills to the in-memory portfolio projection and emits processing failures on fill pipeline errors.
- `aats/services/execution_control/order_service.py`
  - Seeds persistent submit/cancel commands and defines command idempotency semantics.
- `aats/services/execution_engine/order_manager.py`
  - Persists created orders, handles pre-submit cancellation, and must understand both new and legacy submit-command keys.
- `aats/services/recovery_control/startup_recovery.py`
  - Detects stranded created orders and must not miss legacy or new submit-command rows.
- `aats/bootstrap/config.py`
  - Enforces exchange-runtime hardening requirements at startup.
- `aats/api/auth.py`
  - Resolves operator principals and compatibility access paths.

## Input/output interfaces
- Inputs:
  - Fill events with `fee_amount`, `fee_currency`, `symbol`, and venue defaults.
  - Submit intents carrying `intent_id`, `idempotency_key`, and derived `client_order_id`.
  - Runtime settings for operator auth, session cookies, storage, and environment capabilities.
- Outputs:
  - Unknown fee currencies raise explicit errors instead of silently becoming zero quote cost.
  - Submit commands use `submit:{client_order_id}` as the primary idempotency key.
  - Legacy `submit:{intent_id}` rows remain readable for cancellation and recovery.
  - Exchange-coupled session auth fails startup when `Secure` cookies are disabled.
  - Write API key compatibility is enabled only for local/memory-style runtimes.

## Database schema / tables / indexes / constraints
- Reuse the existing `execution_commands`, `execution_orders`, `order_states`, `execution_fills`, and operator tables.
- No schema migration is required.
- Existing uniqueness on `execution_commands.idempotency_key` continues to enforce submit dedupe.

## Transactions, Consistency, Concurrency
- Preserve existing transactional persistence boundaries for order state plus command enqueue.
- Treat unsupported fee currencies as non-retriable processing errors so replay/recovery stops on bad inputs instead of masking them.
- Maintain backward compatibility by checking both new and legacy submit-command keys during cancel-before-submit and startup recovery.

## Authorization, Authentication, Data Security
- Exchange-coupled browser session auth must require `operator_session_cookie_secure=true`.
- Write API key compatibility is restricted to local or memory-backed runtimes.
- No public auth API shape changes.

## Error Handling and Idempotency
- Replace fee fail-open behavior with explicit exceptions containing the unsupported currency context.
- Emit `processing_failure` events when fill application fails before snapshot persistence.
- Promote submit idempotency to the stable client order identity while retaining legacy lookup compatibility.

## State Transition and Lifecycle
- Order lifecycle remains unchanged.
- Pre-submit cancel still abandons pending submit commands, but now recognizes both key generations.
- Portfolio/fill lifecycle becomes fail-closed on unsupported fee data.

## Caching and Performance
- No cache changes.
- Added lookup work is O(1) with at most two submit-command key probes.

## Logging, Monitoring, Auditing
- Processing failures now surface unsupported fee currency errors through the existing failure event channel.
- Recovery continues to flag stranded created orders if neither the new nor legacy submit command exists.

## Testing Strategy
- Unit:
  - unsupported fee currency raises and does not mutate portfolio state
  - unsupported fee currency during fill handling emits a processing failure
  - submit commands use `client_order_id` idempotency keys
  - legacy submit-command keys still cancel correctly
  - startup guard enforces secure operator session cookies for exchange runtimes
  - write API key compatibility is disabled outside local/memory runtimes
- Integration:
  - phase2 command flow runtime persists and drains submit commands with the new key
  - phase4 recovery still sees pending submit commands with the new key

## Migration, Rollback, Compatibility
- No migration required.
- New submits write `submit:{client_order_id}`.
- Runtime logic still recognizes legacy `submit:{intent_id}` rows for rollback/restart compatibility.

## Configuration and Environment Isolation
- Use the repository-local `.venv\Scripts\python.exe`.
- PostgreSQL-backed integration tests use `AATS_DATABASE_URL`; the current repo points to `.env.derivatives.live`.

## Code Organization and Dependencies
- Limit code changes to the existing modules above plus focused tests.
- Do not introduce new runtime dependencies.

## Documentation and Operations Manual
- This document records the new fee fail-closed behavior, submit-command idempotency contract, and operator auth hardening boundary.

## Deployment and Acceptance Criteria
- `ruff` lint passes for touched files.
- Targeted unit tests covering accounting, auth, startup guards, and command flow pass.
- Targeted PostgreSQL-backed integration tests for phase2 command flow and phase4 recovery pass.
- Remaining behavior stays backward compatible except for the intentional fail-closed handling of unsupported fee currencies.
