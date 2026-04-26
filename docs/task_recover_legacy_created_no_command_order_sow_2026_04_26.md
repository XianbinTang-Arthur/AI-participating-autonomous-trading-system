# Recover Legacy Created No-Command Order SOW

## Business objectives and boundaries
- Objective: safely resolve the historical OKX BTC-USDT-SWAP pre-submit order that is locally `CREATED`/`SUBMITTING` but has no execution command and no venue order truth.
- Boundary: no order replay, no manual submit, no strategy tuning, no new venue, no new symbol, no release or promotion change.

## Module responsibilities and domain model
- `OperatorQueryService` owns eligibility checks for operator-controlled stuck submission recovery.
- `ReconciliationSystemQueryFacade.resolve_stuck_submission` owns the audited transition to `FAILED` and reconciliation refresh.
- The historical order remains a local pre-submit order until the audited operator recovery path marks it terminal.

## Input/output interfaces
- Input: local `OrderState`, scoped local fills, private websocket probes, fresh exchange account snapshot, latest scoped reconciliation report.
- Output: eligibility payload and, when eligible, a terminal `FAILED` order state with operator action and reconciliation evidence.

## Database schema / tables / indexes / constraints
- No schema, index, or table changes.
- Existing order state persistence and execution order truth sync are reused.

## Transactions, consistency, concurrency
- The change does not introduce a new transaction boundary.
- Recovery remains single-order and idempotent after the order leaves pre-submit statuses.

## Authorization, authentication, data security
- Existing protected operator command path is reused.
- No credentials are read or printed by this task.

## Error handling and idempotency
- Existing blockers remain: venue mismatch, non-pre-submit status, exchange order id present, local fills, current-runtime order, private WS evidence, unavailable exchange confirmation, visible exchange order, and exchange fills.
- New allowance applies only when dirty reconciliation evidence is self-blocking for the same missing local order.

## State transition and lifecycle
- Allowed recovery transition remains `CREATED/SUBMITTING -> FAILED`.
- No transition to submitted, canceled, or filled is introduced.

## Caching and performance
- No new cache keys or hot-path queries.
- Existing operator cache invalidation remains in the recovery facade.

## Logging, monitoring, auditing
- Existing operator action, order update, execution error summary, and reconciliation report are retained.
- Eligibility payload now records whether latest reconciliation was self-blocking.

## Testing strategy
- Unit tests cover self-blocking acceptance and rejection of unexpected exchange orders, other unknown orders, and unbooked exchange fills.
- Integration test covers the protected operator recovery endpoint when latest reconciliation is self-blocking.

## Migration, rollback, compatibility
- No migration required.
- Rollback: revert the implementation/test/doc commit and redeploy with `scripts/deploy.sh --skip-commit`.

## Configuration and environment isolation
- No configuration change.
- No AI provider, risk, execution submit, symbol, venue, or strategy family change.

## Code organization and dependencies
- Code change is limited to `aats/services/operator/query_service.py`.
- No new third-party dependency.

## Documentation and operations manual
- Operator procedure remains: use the existing stuck-submission recovery command after exchange/private WS absence is confirmed.

## Deployment and acceptance criteria
- Acceptance: focused tests pass, full unit tests pass, deploy uses `scripts/deploy.sh --skip-commit`, and runtime truth no longer reports `execution_command_missing_for_created_order` for the historical order.
