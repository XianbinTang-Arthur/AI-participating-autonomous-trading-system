# Operator execution truth scoped counts SOW

## Context

This review covers the execution truth modules touched by the May 18 recovery
and reconciliation work:

- `execution_orders` / `execution_fills` read paths used by operator pages.
- Phase 1 shadow backlog checks.
- Startup recovery Phase 4 order checks.

The previous fixes moved local reconciliation and operator reads toward the
current execution ledger. A follow-up review found that some list pages and
health checks read scoped rows but still used global Phase 5 counts.

## Problem

When more than one runtime scope exists in the same database, global
`count_orders()` or `count_fills()` can disagree with the current scoped page.
That can make pagination, backlog, or recovery evidence look larger or smaller
than the current live workspace actually is.

## Scope

Implement scope-aware count/read helpers for Phase 5 execution truth repos and
route the affected consumers through those helpers:

- operator recent orders and fills;
- operator order/fill details by id;
- Phase 5 fill page reads;
- Phase 1 shadow backlog;
- startup recovery Phase 4 open order/count checks.

## Non-goals

- No schema migration.
- No change to public operator response shape.
- No relaxation of recovery blockers.
- No Research Factory runtime/apply behavior changes.

## Validation Plan

- Ruff on `aats/`.
- Targeted unit tests for operator execution read paths, storage scoped counts,
  Phase 1 shadow monitor, startup recovery, reconciliation, and Research
  Factory modules touched in this session.
- Full `tests/unit/`.
- Narrow WSL2 integration tests for Phase 4/Phase 5 recovery/runtime paths.
- Deploy through `scripts/deploy.sh --profile derivatives-live --skip-commit`
  only after commit.
