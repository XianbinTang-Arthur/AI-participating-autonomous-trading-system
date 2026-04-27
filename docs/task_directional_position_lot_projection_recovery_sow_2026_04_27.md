# Directional Position Lot Projection Recovery SOW - 2026-04-27

## Objective

Restore deterministic lifecycle attribution for OKX `BTC-USDT-SWAP` directional trading when startup recovery finds fills that already have `fill_outcomes` but lack `position_lots` / `lot_events` projection rows.

## Scope

- Runtime scope remains the configured live derivatives scope: OKX, `BTC-USDT-SWAP`, current strategy family `directional`.
- No strategy signal, risk gate, execution submission, provider, symbol, venue, timeframe plumbing, release, promotion, or tuning behavior is changed.
- The recovery service may rebuild read-side lot projections from persisted scoped fills.

## Current Behavior

Normal fill handling persists a snapshot, `fill_outcomes`, sleeve PnL projection, and persistent lot book projection. Startup recovery already replays scoped fills to backfill missing `fill_outcomes`, but it did not rebuild the persistent lot book. After a previous recovery fill-outcome repair, a fill can therefore have realized PnL while `lot_events` and `position_lots` remain absent.

## Proposed Behavior

Startup recovery injects `PersistentLotBookService` when the persistent lot repositories are available. After fill outcome gap compensation, it idempotently rebuilds the persistent lot book from the same scoped fills used for recovery.

## Data Model

- Source of truth: persisted scoped `FillEvent` rows.
- Rebuilt projections:
  - `position_lots`
  - `lot_events`
- Existing deterministic `LotBasedProjectionBuilder` remains the only projection builder.
- Existing repository `replace_scope` semantics remain unchanged: the scoped lot rows are replaced atomically by repository transaction when backed by the same session factory.

## Consistency And Concurrency

- Recovery rebuild is deterministic and idempotent.
- It runs during startup recovery, before normal trading resumes.
- It uses the runtime scope product type and margin mode and lets `PersistentLotBookService` group by symbol/product/margin.
- It does not create ad hoc SQL updates and does not mutate order state.

## Failure Handling

- Repository failure is logged and recorded in recovery notes as `persistent_lot_book_rebuild_failed:1`.
- Recovery does not fabricate lifecycle evidence; if rebuild fails, runtime truth can still surface missing lot projection as a blocker.

## Observability

Recovery status notes include `persistent_lot_book_rebuilt:<fill_count>` when the rebuild runs.
Structured logs include the fill count, product type, and margin mode without secrets.

## Validation

- Focused unit coverage verifies:
  - Missing `fill_outcomes` are still backfilled from replay.
  - Persistent lot book rebuild still runs when `fill_outcomes` already exist, covering the live gap shape.
- Standard validation:
  - `ruff check aats/ --fix`
  - `pytest tests/unit/ -x -q`
  - Narrow integration test if runtime recovery wiring requires it.

## Rollback

Revert the recovery service injection and rebuild call. Existing hot-path fill processing and `PersistentLotBookService` behavior remain unchanged by this rollback.
