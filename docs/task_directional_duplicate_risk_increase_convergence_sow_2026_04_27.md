# Directional Duplicate Risk-Increase Convergence SOW - 2026-04-27

## Current Behavior

Directional live execution can receive multiple risk-increasing bundles before the newest order/fill has been reflected back into the next portfolio exposure snapshot. Existing guards block duplicate orders from the same `portfolio_snapshot_ref`, but they do not cover a newer snapshot that still carries stale `current=0` exposure while a previous directional open/scale-in is already in flight or recently filled.

## Objective

Prevent stale directional risk-increase decisions from increasing live exposure more than the risk/budget layer intended, without weakening protective reduce/close behavior or expanding strategy scope.

## In Scope

- OKX `BTC-USDT-SWAP` directional derivatives path.
- Carry current/projected derivatives exposure evidence from risk evaluation into execution intents.
- Add an order-boundary convergence guard for directional risk-increase intents.
- Focused tests for stale-zero duplicate open, fresh exposure evidence, and protective reduce behavior.

## Out Of Scope

- Strategy alpha tuning, impulse signal tuning, timeframe plumbing, release/promotion/tuning.
- New symbols, venues, strategy families, or provider behavior.
- Manual order bypasses or risk-limit expansion.

## Acceptance Criteria

- A second directional risk-increase intent is blocked when recent same-family increase exposure is in flight or recently filled but not reflected in current exposure evidence.
- A follow-up risk-increase is allowed once the current exposure evidence reflects the prior fill.
- Protective reduce/close remains allowed.
- Focused and full unit tests pass, followed by deploy through `scripts/deploy.sh`.

## Rollback

Revert the convergence-guard commit and redeploy through `bash scripts/deploy.sh --skip-commit`. No manual `docker compose` actions.
