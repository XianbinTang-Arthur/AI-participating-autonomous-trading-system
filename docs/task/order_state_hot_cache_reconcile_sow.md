## OrderStateHotCache Reconcile SoW

### Business objective and boundary
- Eliminate stale `DecisionContext.current_open_orders` caused by Redis-resurrected `OrderStateHotCache` entries.
- Preserve existing source-of-truth ordering: Postgres remains authoritative, Redis/NATS remain best-effort acceleration layers.
- Align operator leg trial-guard summaries with runtime/target fallback semantics when guard-eligible windows are empty.

### Current behavior summary
- `OrderStateHotCache.fire_and_forget_publish()` eagerly applies local state, but skips Redis/index persistence when the same state has already been applied locally via self-subscribed `ORDER_UPDATES`.
- `OrderStateHotCache.bootstrap()` hydrates Redis state without reconciling hydrated non-terminal orders against Postgres truth.
- `persist_fill()` can refresh synthetic `OrderState` rows in Postgres via fill settlement without pushing the resulting `OrderState` to `OrderStateHotCache`.
- `OperatorQueryService._leg_trial_guard_audit_summary()` can prefer guard-eligible zero values over raw metrics when the guard-eligible window is empty.

### Module responsibilities and domain model
- `aats/services/execution_engine/order_state_cache.py`
  - Maintains per-process `OrderState` hot cache.
  - Hydrates from Redis, listens to NATS `ORDER_UPDATES`, exposes scoped open orders to decision context.
- `aats/storage/execution_repo_converged_postgres.py`
  - Persists authoritative `OrderState` and fill rows.
  - Synthesizes terminal/partial order state from fills when needed.
- `aats/services/execution_engine/outbox.py`
  - Persists order/fill events and performs post-commit hot-cache publication.
- `aats/services/operator/query_service.py`
  - Builds operator-visible summaries from decision context payloads.

### Input/output interfaces
- No public API changes.
- `OrderStateHotCache.bootstrap()` may accept an optional reconciliation callback/provider.
- `persist_fill()` may return or derive the latest persisted `OrderState` for cache synchronization, but must preserve existing caller behavior.

### Database schema / tables / indexes / constraints
- No schema changes.
- Reads from `execution_orders` / `order_states` remain scoped by existing repository methods.

### Transactions, consistency, concurrency
- Cache writes remain post-commit only.
- Bootstrap reconciliation must treat Postgres as the terminal truth for hydrated non-terminal Redis entries.
- Fire-and-forget remote persistence must publish the latest in-memory state, not a stale captured object, to avoid async task reordering regressions.

### Authorization, authentication, data security
- No credential or auth changes.
- No direct credential file reads or logging.

### Error handling and idempotency
- Redis/NATS remain fail-soft.
- Reconciliation must not block cache bootstrap on best-effort failures.
- Duplicate or stale order updates must remain idempotent.

### State transition and lifecycle
- Terminal order states must not be resurrected as open orders after process restart.
- Fill-driven synthetic `OrderState` transitions must be reflected in hot cache.

### Caching and performance
- Bootstrap reconciliation should only query Postgres for hydrated non-terminal client order ids.
- No change to cache read path complexity during steady state.

### Logging, monitoring, auditing
- Log reconciliation summary and best-effort failures without exposing secrets.
- Keep event-store and audit semantics unchanged.

### Testing strategy
- Unit tests for:
  - self-subscribed local apply no longer suppresses Redis/index persistence
  - bootstrap reconciliation drops or overwrites stale non-terminal Redis entries using Postgres truth
  - fill-driven synthetic `OrderState` refresh also synchronizes order-state cache
  - operator trial-guard summary falls back to raw metrics when guard-eligible window is empty
- Run existing affected unit suites plus the narrowest runtime integration test.

### Migration, rollback, compatibility
- No migration required.
- Rollback by reverting modified files only.

### Configuration and environment isolation
- No new runtime configuration required.
- Behavior remains profile-agnostic.

### Code organization and dependencies
- Keep fixes local to execution cache, execution outbox/repo, and operator query summary code.
- Avoid unrelated refactors or protocol changes.

### Documentation and operations manual
- This SoW is the implementation record for the cache reconciliation fix.

### Deployment and acceptance criteria
- `DecisionContext.current_open_orders` no longer includes historical `FILLED` orders after restart when PG shows terminal truth.
- Post-commit order-state cache persistence continues to refresh Redis/index even when self-subscription updated local memory first.
- Operator trial-guard view matches runtime/target fallback semantics for empty guard-eligible windows.
