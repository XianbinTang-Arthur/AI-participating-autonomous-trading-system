# Live Data Read Path Optimization SOW - 2026-05-07

## Business Objectives and Boundaries

The live operator UI and gateway read model must stop using unbounded historical reads for short-frequency operational views. The goal is to reduce slow queries and stale execution health noise without changing trading decision, risk, order submission, or recovery semantics.

This work is limited to read-side APIs, dashboard presentation, SQL indexes, and tests. It does not change baseline bias, sleeve intent generation, budget gates, execution order state transitions, or exchange adapters.

## Module Responsibilities and Domain Model

- `event_store` remains the durable event/audit source for replay and compliance.
- `event_store_archive` remains the cold historical extension of `event_store`.
- `reconciliation_reports` remains the durable reconciliation audit record.
- Operator dashboard endpoints are online read models and should present bounded recent state, not full audit history.
- Audit/replay/RDP code may still use full history when explicitly invoked outside the live dashboard path.

## Input/Output Interfaces

- Existing REST/UI contracts remain compatible.
- Recent list endpoints must continue returning `limit`, `offset`, `total_available`, and `has_more`.
- Metrics that were previously full-history operational counters may become bounded online counters only where the UI already frames them as current/recent execution health.
- New repository parameters must be optional and backward compatible.

## Database Schema / Tables / Indexes / Constraints

Current live data support observed on `aats_live_derivatives`:

- `event_store`: about 17 GB, about 2.37M estimated live rows.
- `event_store_archive`: about 1.1 GB.
- `decision_audit_records`: about 1.4 GB.
- `strategy_sleeve_intents`: about 1.1 GB.
- `portfolio_allocation_decisions`: about 728 MB.
- `reconciliation_findings`: about 652 MB.
- `reconciliation_reports`: about 148 MB.

Existing useful indexes:

- `event_store(topic, product_type, margin_mode, sequence_id)`
- `event_store_archive(topic, product_type, margin_mode, source_sequence_id)`
- `event_store(event_timestamp)` and archive timestamp indexes
- `reconciliation_reports(product_type, margin_mode, as_of_ts)`

Known gaps:

- No expression index for `reconciliation_reports.payload ->> 'portfolio_snapshot_ref'`.
- Hot `event_store` retention is not currently meeting the 14-day intent; more than 800k rows are older than 14 days.
- Some live queries still hydrate JSON payloads before counting or paginating.
- Several Postgres repository `limit` paths returned the oldest N rows while in-memory repositories returned the latest N rows. Online bounded reads must use the latest window and keep chronological return order.

## Transactions, Consistency, Concurrency

Read-side changes must use ordinary read transactions only. Index creation should be idempotent and safe to re-run. Startup migration execution must be serialized by a Postgres advisory transaction lock because multiple process roles can boot at the same time. No code path should hold long transactions while scanning historical JSON payloads.

## Authorization, Authentication, Data Security

No credentials or environment values are printed. Existing operator authentication and session behavior remain unchanged. The UI continues to expose only existing operator-facing fields.

## Error Handling and Idempotency

Repository APIs must remain tolerant of empty stores and legacy in-memory test repositories. New migrations use `IF NOT EXISTS` where possible and are recorded by `schema_migrations`.

## State Transition and Lifecycle

No trading state transitions are changed. OrderState three-layer persistence is untouched.

## Caching and Performance

Online dashboard paths should use bounded `limit`/recent reads. Full-history APIs remain available only for explicit audit/replay flows. Query code should avoid Python pagination after full DB hydration.

Target fixes:

- Bound portfolio snapshot event reads in operator metrics.
- Bound reconciliation snapshot-ref reads for online metrics.
- Use a smaller reconciliation-ref window for dashboard metrics because the UI only compares against the latest bounded snapshot event window, and the live DISTINCT query is otherwise the dominant refresh cost.
- Cache expensive exact audit record counts behind a short operator TTL instead of running `count(distinct decision_id)` on every runtime panel refresh.
- Align Postgres `limit` semantics with in-memory repositories: fetch the latest N rows, then return them in chronological order.
- Replace recent policy/risk/AI history full loads with bounded DB reads.
- Avoid phase 5 fill list full loads in gateway.
- Make execution UI distinguish current runtime orderless subsystem failures from stale failed orders/fills.
- Keep dashboard `latestDecision` on a lightweight summary path; full decision chains,
  orders, fills, reconciliation refs, and dedicated overlay event history stay available
  behind explicit detail endpoints instead of blocking first-screen refresh.

## Logging, Monitoring, Auditing

Existing slow-query logging remains the operational signal. Deployment monitoring should verify container health, `/healthz`, active DB slow queries, and recent execution error summaries.

## Testing Strategy

- Unit tests for bounded event-store and reconciliation reference APIs.
- Unit tests for execution latest/UI status semantics.
- Existing targeted dashboard/operator tests.
- Required project validation: ruff and unit tests.
- Narrow integration test where feasible after unit validation.

## Migration, Rollback, Compatibility

Forward migration adds read-performance indexes only. Rollback is dropping the added indexes; data remains unchanged. Code remains backward compatible with old repositories by using optional parameters and fallbacks.

## Configuration and Environment Isolation

No new runtime secrets or environment variables. Deployment target remains the current WSL2 `derivatives-live` profile unless a more specific profile is discovered before deployment.

## Code Organization and Dependencies

Use existing repository classes and operator query facades. Do not add new third-party dependencies.

## Documentation and Operations Manual

This SOW is the operator-facing implementation record. Post-deploy monitoring should report table health, active long queries, gateway health, and container status.

## Deployment and Acceptance Criteria

Acceptance criteria:

- No live dashboard path introduced by this change calls unbounded `by_topic()` or `by_topic_scoped(limit=None)` for recent state.
- Slow `reconciliation_reports.payload ->> 'portfolio_snapshot_ref'` reads are bounded and index-supported.
- UI does not imply a stale failed order/fill is the current execution blocker when current failures are orderless subsystem errors.
- Tests pass, commit is created, standard deploy script completes, and post-deploy monitoring shows healthy core containers.
