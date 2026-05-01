# Runtime Truth Kill Switch Attribution SOW

## Business objectives and boundaries
- Objective: distinguish stale decision output caused by an active kill switch from stale market/feature transport.
- Boundary: read-only runtime truth reporting only. No strategy, risk, execution, provider, symbol, venue, schema, or order behavior changes.

## Module responsibilities and domain model
- `scripts/runtime_truth_report.py` owns aggregate runtime facts and blocker classification.
- `event_store.system.kill_switch_state` is the durable source for the latest kill-switch state.
- `recent_directional_no_order_freshness_truth` remains the bounded truth surface for the current no-order decision freshness check.

## Input/output interfaces
- Input: latest `system.kill_switch_state` event from Postgres `event_store`.
- Output: sanitized `database_truth.latest_kill_switch_state`, live fact keys, and a specific stale-decision attribution status when the kill switch is halted.

## Database schema / tables / indexes / constraints
- No schema changes.
- Read-only query against `event_store` using existing columns: `topic`, `event_key`, `created_at`, `event_timestamp`, `payload`.

## Transactions, consistency, concurrency
- Single read-only query in the existing database truth probe.
- No writes, locks, or transactional behavior changes.

## Authorization, authentication, data security
- No credentials are read or printed.
- Only non-sensitive kill-switch fields are surfaced: halted, reason, source role, timestamps.

## Error handling and idempotency
- Missing kill-switch state produces `missing_latest_kill_switch_state`.
- Invalid halted payload produces `invalid_latest_kill_switch_state`.
- Report generation remains idempotent.

## State transition and lifecycle
- No runtime state transition is performed.
- The report only explains that stale decisions are expected while `kill_switch.halted=true`.

## Caching and performance
- One latest-row query by indexed/event-store ordering pattern already used elsewhere.
- No cache behavior changes.

## Logging, monitoring, auditing
- Adds blocker classification `decision_cycle_halted_by_kill_switch`.
- Adds live facts for kill-switch status and stale-decision attribution.

## Testing strategy
- Unit tests cover stale decision attribution with active kill switch and blocking finding selection.
- Existing stale-decision behavior remains covered when no kill-switch halt is present.

## Migration, rollback, compatibility
- No migration.
- Rollback is a normal git revert of this reporting-only change.
- Existing JSON consumers remain compatible because fields are additive, except the blocker label becomes more precise when kill switch explains the stale decision.

## Configuration and environment isolation
- Uses the existing gateway-container DB environment through the existing runtime truth path.
- No new environment variables.

## Code organization and dependencies
- No new dependencies.
- Changes stay in the runtime truth script and its unit tests.

## Documentation and operations manual
- Operators should interpret `decision_cycle_halted_by_kill_switch` as a protected halt, not as market-data transport failure.
- Clearing the blocker requires the existing protected recovery / reconciliation path, not bypassing the kill switch.

## Deployment and acceptance criteria
- Acceptance criteria:
  - Runtime truth includes latest kill-switch state without exposing secrets.
  - When latest decision is stale and kill switch is halted, freshness status is `decision_cycle_halted_by_kill_switch_for_recent_no_order_freshness`.
  - Blocking findings contain `decision_cycle_halted_by_kill_switch` instead of the generic stale-decision blocker for that case.
  - Focused unit tests pass.
