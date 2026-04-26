# Directional Lifecycle Cost And Cooldown SOW - 2026-04-26

## Context

The live directional incident showed that entry decisions treated cost as a single execution leg while realized PnL depends on the full open-close lifecycle. The same incident also showed a new entry shortly after a close, meaning post-close cooldown must use the freshest available lifecycle close timestamp.

## Bounded Task

Task type: `runtime-reliability-fix`.

Strengthen the risk-increase admission path for directional derivatives:

- estimate entry, scale-in, and reversal candidates with lifecycle cost: entry fee + expected close fee + spread + slippage/funding;
- keep reduce/close cost as a single execution leg;
- make post-close cooldown read aggregate and leg-level close timestamps.

## In Scope

- `TradeCostService.estimate_single_leg_entry` optional close-fee component.
- `TargetPositionEngine` lifecycle cost usage for derivatives risk-increase candidates.
- `TargetPositionEngine` post-close cooldown timestamp resolution.
- Focused unit tests for lifecycle cost and leg-level cooldown.

## Out Of Scope

- No strategy signal formula tuning.
- No execution adapter behavior change.
- No schema migration.
- No symbol, venue, provider, strategy family, release, promotion, or timeframe changes.
- No duplicate decision atomicity fix in this bounded task.

## Acceptance Criteria

- Directional derivatives entry cost includes expected close fee and spread.
- Directional derivatives reduce/close remains single-leg and is not overcharged as a round trip.
- Post-close cooldown blocks entry when only a leg close timestamp is available.
- Focused tests, ruff, and full unit tests pass.

## Rollback

Revert the implementation commit and redeploy with `bash scripts/deploy.sh --skip-commit`. No manual compose operations.
