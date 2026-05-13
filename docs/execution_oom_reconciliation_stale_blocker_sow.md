# Execution OOM and stale reconciliation blocker SOW

## Business objectives and boundaries
Restore the derivatives-live execution process from an OOM restart loop so it can refresh reconciliation and clear fail-closed stale-reconciliation blockers through normal recovery logic. Do not change strategy decisions, risk thresholds, RDP parameters, order submission semantics, or manual resume policy.

## Module responsibilities and domain model
`aats-execution` owns order execution, recovery, and reconciliation. `ReconciliationRepository` owns report lookup. The domain state remains: fresh nonblocking reconciliation can clear `recovery_reconciliation_stale`; stale or blocking reconciliation keeps execution halted.

## Input/output interfaces
Inputs are portfolio snapshot events, reconciliation reports, execution order/fill rows, and container resource limits. Outputs are fresh reconciliation reports, recovery state snapshots, and unchanged kill-switch events.

## Database schema / tables / indexes / constraints
No schema change. The new repository API uses existing `reconciliation_reports.product_type`, `margin_mode`, and `payload ->> 'portfolio_snapshot_ref'` data without loading report payloads into Python. Production Postgres keeps the literal JSON expression for expression-index matching; SQLite tests use the SQLAlchemy JSON accessor for compatibility.

## Transactions, consistency, concurrency
The existence check is read-only and transaction-local. It does not write report state and preserves idempotent duplicate-snapshot suppression.

## Authorization, authentication, data security
No credentials are read or displayed. No public API authorization changes.

## Error handling and idempotency
Empty snapshot refs return false. If a repository lacks the optimized API, the service falls back to the existing refs API or legacy history scan for compatibility.

## State transition and lifecycle
No direct kill-switch mutation is added. Existing recovery code remains responsible for resuming only after a fresh nonblocking reconciliation report exists.

## Caching and performance
The report existence check becomes SQL `SELECT 1 ... LIMIT 1` instead of full `history_for_scope()` payload hydration. Execution memory limit is raised to 3GiB to absorb live recovery spikes while the read path is narrowed.

## Logging, monitoring, auditing
No new log surface. Existing Docker health, kill-switch, recovery, and reconciliation events remain the audit path.

## Testing strategy
Unit tests cover scoped existence semantics for in-memory and Postgres repository implementations. Live acceptance checks inspect container memory, health, latest reconciliation age, and kill-switch state after deploy.

## Migration, rollback, compatibility
Rollback is the inverse code/config change. Repository protocol is additive and backward-compatible.

## Configuration and environment isolation
Only the WSL2 AATS execution service memory limit changes. No `.env` values or live credentials are modified.

## Code organization and dependencies
Changes stay in storage repository APIs, reconciliation service read path, compose service resources, tests, and this SOW.

## Documentation and operations manual
Operators should treat `recovery_reconciliation_stale` as a fail-closed symptom. If execution is also OOMKilled, stabilize the execution process first; do not force RDP parameter drafts or clear Redis manually.

## Deployment and acceptance criteria
Deploy via `bash scripts/deploy.sh --profile derivatives-live --skip-commit` after commit. Acceptance requires execution healthy without OOM churn, a fresh reconciliation report, and no stale-reconciliation kill switch unless a real blocking report is present.
