# Recovery Reconciliation Stale Clearance

## Business objectives and boundaries
Runtime truth showed the live decision cycle halted by `recovery_reconciliation_stale` even after a fresh non-blocking reconciliation existed. The objective is to clear only that automatic stale-reconciliation halt when the stale condition is proven resolved. This change does not alter strategy logic, risk thresholds, provider behavior, symbols, venues, or order submission behavior.

## Module responsibilities and domain model
`ExecutionRecoveryService` owns startup recovery side effects, including recovery-driven kill switch halts. It now also owns the matching recovery-driven clearance for the same automatic halt reason. `KillSwitch` remains the cross-process halt/resume state holder.

## Input/output interfaces
Input is the latest scoped `ReconciliationReport`, current recovery flags, open-order count, bundle recovery state, and current kill switch reason. Output is either no-op or `kill_switch.resume()` plus an audit note on the recovery status.

## Database schema / tables / indexes / constraints
No schema, table, index, or constraint changes.

## Transactions, consistency, concurrency
The existing `KillSwitch.resume()` path performs local update plus best-effort Redis/NATS propagation when bootstrapped. The new path is idempotent and only runs after current recovery checks in the same recovery cycle.

## Authorization, authentication, data security
No new operator or external API surface is added. No credentials are read or logged.

## Error handling and idempotency
If the kill switch is not halted, has a different reason, lacks a reconciliation report, still has a stale report, or has any blocking recovery condition, the path returns without side effects.

## State transition and lifecycle
`recovery_reconciliation_stale` can move from halted to resumed only when the latest reconciliation is fresh and non-blocking, startup is otherwise safe, no open orders exist, and no strategy bundle recovery remains active or blocking.

## Caching and performance
No additional repository queries are introduced. The check uses data already loaded by the recovery cycle.

## Logging, monitoring, auditing
The recovery status notes include `recovery_reconciliation_stale_halt_cleared_after_fresh_nonblocking_reconciliation` when the automatic clearance runs.

## Testing strategy
Unit coverage verifies that a fresh non-blocking reconciliation clears the stale-reconciliation halt, a manual halt is not cleared, and a halt-required reconciliation remains halted.

## Migration, rollback, compatibility
Rollback is a normal git revert of the code and test changes. The public API is unchanged.

## Configuration and environment isolation
No configuration changes. The existing `reconciliation_stale_after_seconds` threshold remains authoritative.

## Code organization and dependencies
The helper is local to `ExecutionRecoveryService`; no new dependencies are introduced.

## Documentation and operations manual
Operators should treat this as a protected automatic recovery for a stale evidence condition, not as a general kill switch bypass.

## Deployment and acceptance criteria
Deploy separately through `scripts/deploy.sh` after review because the change affects runtime recovery behavior. Acceptance requires focused unit tests and lint to pass, and a follow-up runtime truth report after deployment showing the stale reconciliation kill switch no longer remains active when a fresh non-blocking reconciliation is present.
