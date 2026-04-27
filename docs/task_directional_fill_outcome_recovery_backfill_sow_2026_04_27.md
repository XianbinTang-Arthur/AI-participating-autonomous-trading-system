# Directional Fill Outcome Recovery Backfill SOW - 2026-04-27

## Task

- task_type: runtime-reliability-fix
- input: Runtime truth reports the latest filled directional episode has a fill with no `fill_outcomes.realized_pnl_delta`; detailed evidence shows the fill also lacks lot projection evidence, so the prior "closed lifecycle" label over-relied on close intent.
- output: Deterministic recovery backfill for missing fill outcomes, plus stricter truth classification that separates close intent from actual lot lifecycle evidence.

## Scope

- Include: recovery-time fill outcome compensation from immutable execution fills; runtime truth lifecycle classification; focused unit tests.
- Exclude: strategy tuning, live order behavior, risk-gate bypasses, schema changes, provider changes, symbol/venue/family expansion, release/promotion/tuning.

## Current Behavior

- `ExecutionRecoveryService._detect_and_compensate_fill_gaps()` detects fills without `FillOutcomeRecord` and logs them, but does not actually compensate.
- `scripts/runtime_truth_report.py` treats close/reduce position intent as enough evidence for `closed_lifecycle_missing_fill_outcome`, even when `lot_events` and `position_lots` evidence are absent.

## Planned Behavior

- Recovery replays scoped fills in deterministic fill-processing order through a fresh `PortfolioState`.
- For every replayed fill missing a `FillOutcomeRecord`, recovery persists a `FillOutcomeRecord` derived from replayed balance deltas and position before/after state.
- Runtime truth reports `close_intent_missing_portfolio_projection` when close/reduce intent exists but no lot close event exists.
- Runtime truth keeps `closed_lifecycle_missing_fill_outcome` only when actual close lifecycle evidence exists.

## Impact

- Data repair impact: writes missing `fill_outcomes` during recovery from existing execution fills.
- No live order behavior impact.
- No database schema impact.
- No strategy or risk parameter impact.

## Validation

- Focused unit tests for recovery backfill and lifecycle classification.
- Ruff on touched AATS code and required `ruff check aats/ --fix`.
- Full unit suite: `pytest tests/unit/ -x -q`.

## Rollback

- Revert the recovery backfill code and truth classification test/code changes.
- Existing persisted fill outcomes are deterministic derivations from execution fills; if needed, remove only records created after the reverted deployment timestamp by fill_id audit, not by blanket truncation.

## Acceptance Criteria

- AC1: A recovery run with scoped fills missing outcomes creates `FillOutcomeRecord` rows for replayed fills, including realized PnL for closing fills.
- AC2: Recovery notes report actual compensated count, not detection-only count.
- AC3: A close/reduce fill with no lot close event is classified as missing portfolio projection, not as closed lifecycle.
- AC4: Tests covering AC1-AC3 pass.
