# Task 216 Lifecycle Snapshot Linkage SoW

## Objective
- Add the minimal machine-readable lifecycle snapshot linkage for live execution truth.
- Persist `submit`, `ack`, and `fill` snapshot refs in execution raw payloads without adding strategy surface or changing runtime trading behavior.

## Scope
- In scope:
  - `execution_order.raw_payload.lifecycle_snapshot_refs.submit` from outbox submit seed.
  - `execution_order.raw_payload.lifecycle_snapshot_refs.ack` from converged order-state updates.
  - `execution_fill.raw_payload.lifecycle_snapshot_refs.fill` from fill persistence.
  - Preserve prior lifecycle stages when later stages update the same order row.
- Out of scope:
  - New DB columns or migrations.
  - New market-data capture, local order book reconstruction, or fill feasibility.
  - Strategy tuning, promotion, release, or timeframe changes.

## Acceptance
- Submit, ack, and fill payloads expose the same four refs: `market_snapshot_ref`, `feature_snapshot_ref`, `portfolio_snapshot_ref`, and `health_snapshot_ref`.
- Updating an order at ack time keeps existing `submit` refs and adds `ack` refs.
- Missing refs remain backward-compatible as `null`.
- Narrow unit tests cover the payload shape and preservation behavior.

## Rollback
- Revert this task's helper, writer-path payload additions, and unit assertions.
