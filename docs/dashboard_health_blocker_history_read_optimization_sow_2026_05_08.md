# Dashboard Health / Blocker History Read Optimization SOW - 2026-05-08

## Objective

Reduce remaining `health` and `blockers` dashboard snapshot latency after the RDP snapshot summary cache deploy. The change is read-side only and must not alter trading decisions, risk gates, order submission, recovery commands, or public write semantics.

## Current Behavior

- The dashboard `health` panel uses `system_health_dashboard()` for the main health payload, but then immediately calls full `query.metrics()` to populate `execution_summary`.
- Full metrics can hydrate recent event windows, order/fill state, execution errors, reconciliation refs, and strategy execution health when its TTL is cold.
- The derived `blockers` panel reuses `blockerControl`, but still queries blocker history on every panel refresh.

## Scope

- For dashboard health payloads, populate `execution_summary` from:
  - cached full operator metrics when already fresh;
  - otherwise cheap runtime counters plus the phase1 shadow summary already present in health.
- Mark omitted DB-derived fields as deferred instead of triggering heavy metrics reads from the P0 health panel.
- Cache blocker history for a short 10 second TTL through the existing shared `OperatorQueryService` cache.
- Keep direct health, blocker-control, recovery, decision, execution, and trade logic unchanged.

## Acceptance

- Unit tests prove dashboard health does not rebuild full metrics on cache miss.
- Unit tests prove cached metrics are reused when available.
- Unit tests prove blocker history is cached for repeated dashboard reads.
- Deployment validation confirms all required containers healthy, `/healthz` returns 200, no active Postgres query older than 5 seconds, and gateway logs do not show recurring snapshot refresh timeouts.
