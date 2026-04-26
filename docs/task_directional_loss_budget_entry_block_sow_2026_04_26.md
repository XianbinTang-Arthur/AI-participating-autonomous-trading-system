# Directional Loss Budget Entry Block SOW - 2026-04-26

## Context

Live directional trading on OKX BTC-USDT-SWAP produced repeated net losses while budget control only soft-contracted directional exposure. The observed failure mode allowed new entries or scale-ins while `pnl_contraction_active` was already present, including a new long entry after recent realized loss.

## Bounded Task

Task type: `runtime-reliability-fix`.

Implement a narrow budget-layer guard so directional sleeves with recent negative PnL cannot increase exposure while PnL contraction is active. The guard must preserve reduce/close behavior and protective overrides.

## In Scope

- `SleeveBudgetController` risk-increase classification for directional sleeves.
- A dedicated reason code for directional loss entry/scale-in suppression.
- Focused unit tests covering entry, scale-in, and reduce cases.
- Standard lint and unit validation.

## Out Of Scope

- No strategy family, symbol, venue, timeframe, provider, release, promotion, or tuning changes.
- No execution adapter or exchange order behavior changes.
- No schema migration.
- No cost-model round-trip calibration in this bounded task.
- No post-close cooldown context reconstruction in this bounded task.

## Acceptance Criteria

- A directional flat-to-nonflat target under recent negative PnL is budget-suppressed to zero.
- A directional same-side scale-in under recent negative PnL is budget-suppressed to zero.
- A directional reduce/close target under recent negative PnL remains eligible to reduce exposure.
- Existing non-directional behavior remains unchanged.
- Focused tests pass, followed by ruff and full unit tests.

## Rollback

Revert the implementation commit and redeploy with `bash scripts/deploy.sh --skip-commit`. No manual compose operations.
