# Baseline Strategy Rework Plan

The repository currently runs a conservative `baseline_only` strategy chain:

`MarketSnapshot -> FeatureSnapshot -> BaselineAssessment -> PositionTarget -> PolicyDecision -> RiskDecision -> ExecutionPlan -> OrderIntent`

This strategy is structurally coherent, but the live/demo behavior observed on `BTC-USDT-SWAP` shows a gap between:

- market-state classification quality
- and tradable edge quality after fees, slippage, and execution friction

The most important problem is not that the system cannot infer direction at all. The problem is that the current strategy semantics convert weak or neutral evidence into unnecessary trading activity.

Current pain points confirmed from code and runtime logs:

- `baseline_only` maps `direction_bias="flat"` to `target_position_qty=0`, which often means immediate flattening of an existing derivatives position
- entry and exit thresholds are not separated, so the strategy oscillates around threshold boundaries
- there is no explicit cost gate that requires estimated edge to exceed fees and expected slippage
- transient exchange failures can cause repeated medium-urgency close attempts on the same position
- the feature stack behaves more like a regime and eligibility filter than a fully production-grade short-horizon alpha engine

This document defines a practical rework plan. It is intentionally staged. The first stage is designed to be safe enough to merge and deploy without rewriting the architecture.

## 1. Objectives

Primary objectives:

- reduce churn and unnecessary flattening
- require stronger evidence before opening or reversing risk
- preserve genuine risk exits and safety blockers
- keep the strategy explainable and testable
- avoid mixing strategy optimization with unrelated architecture changes

Non-objectives in this pass:

- redesigning the execution architecture
- replacing the feature engine with a new alpha model
- enabling autonomous real-money live trading
- weakening reconciliation, kill-switch, or operator review controls

## 2. Current Strategy Map

### 2.1 Feature Construction

`FeatureCalculator` combines:

- momentum alpha
- trend alpha
- regime alpha
- multi-timeframe alignment alpha
- microstructure alpha
- liquidity scaling
- volatility targeting

Observed weighting in code:

- momentum alpha: `0.34`
- trend alpha: `0.22`
- regime alpha: `0.17`
- multi-timeframe alpha: `0.12`
- microstructure alpha: `0.15`

Strengths:

- more robust than a single-indicator strategy
- explicitly penalizes poor liquidity and poor execution quality
- already produces sensible `suggested_position_scale` and `volatility_target_scale`

Weaknesses:

- the final score is still a generic directional score, not an explicit estimate of tradable post-cost edge
- feature composition is optimized for state inference, not necessarily for short-horizon execution alpha

### 2.2 Baseline Direction Logic

`BaselineStrategy` converts the feature state into `long / short / flat`.

Current behavior:

- `breakout` regime uses the loosest threshold
- `trend` uses a medium threshold
- `range` is stricter
- `uncertain` is the strictest
- supportive microstructure lowers thresholds
- conflicting microstructure raises thresholds

Strengths:

- regime-sensitive thresholds are correct in principle
- microstructure confirmation is a good safety feature

Weaknesses:

- the baseline layer does not distinguish:
  - entry threshold
  - hold threshold
  - exit threshold
  - reverse threshold
- therefore `flat` means both "not enough edge to add" and "enough reason to fully flatten"

### 2.3 Target Position Logic

`TargetPositionEngine` converts baseline output into a target quantity and leverage.

Current strengths:

- staged scale-in
- reduced aggression under high volatility
- leverage scaling based on confidence and microstructure
- partial reduction before weak reversals

Current weakness with highest production impact:

- in `baseline_only` and `ai_advisory`, `flat` becomes a zero target
- for derivatives, this makes the system behave as if "weak signal" means "close now"

### 2.4 Execution and Retry Behavior

Execution is already guarded by:

- policy checks
- risk checks
- local obligations
- execution outbox
- exchange health and reconciliation blockers

However, the strategy layer can still emit repeated medium-urgency close targets when:

- the current position remains open
- the signal remains weak
- the exchange returns transient busy errors

That is a strategy-to-execution coupling problem, not only an exchange reliability problem.

## 3. Confirmed Strategy Problems

### Problem A: `flat` Is Too Close to `exit`

Current semantics:

- `long` -> open or hold long
- `short` -> open or hold short
- `flat` -> target zero

For derivatives, this is too aggressive.

Desired semantics:

- `long` -> open/add/hold long
- `short` -> open/add/hold short
- `flat` -> do not add new risk; usually hold existing risk unless explicit exit evidence exists

### Problem B: No Hysteresis

The strategy lacks a stable neutral band.

Consequences:

- opens can happen shortly above threshold
- closes can happen shortly below threshold
- threshold-boundary noise becomes turnover

### Problem C: No Net-Edge Gate

There is no explicit control requiring:

`expected edge > fee + expected slippage + desired safety margin`

Consequences:

- many small trades look theoretically directionally justified
- but are economically poor after taker fees and real execution friction

### Problem D: Position Management Is Overloaded

`TargetPositionEngine` currently mixes:

- direction interpretation
- risk posture
- position management
- reversal staging
- leverage setting

This is workable for a small system, but it hides important invariants.

### Problem E: Repeated Close Intents After Transient Exchange Failures

The current stack can repeatedly attempt the same close behavior after transient exchange errors such as:

- `50013`
- `Systems are busy`

This creates operational churn and can distort strategy evaluation.

## 4. Design Principles for the Rework

The rework must preserve these invariants:

- safety blockers remain authoritative
- kill-switch behavior must not be weakened
- reconciliation review requirements must still block submission
- strong adverse signals must still be able to reduce or exit risk
- changes must be explainable from the decision payload alone
- new behavior must be covered by unit and runtime tests

## 5. Proposed Rework

## 5.1 Phase 1: Safe Behavioral Corrections

This phase is intentionally narrow and is suitable for immediate implementation.

### Change 1: Flat-Signal Hold for Existing Derivatives Positions

New rule:

- when `baseline_only` or `ai_advisory`
- and current product type is `derivatives`
- and current position is non-zero
- and the derived target is zero because `direction_bias == "flat"`
- default behavior becomes `hold current position`

Exception:

- exit is still allowed if explicit adverse evidence exists

Explicit adverse evidence can be based on:

- adverse microstructure
- adverse momentum factor
- adverse trend factor
- strong adverse AI edge if present

Recommended logic:

- exit if at least two adverse signals align
- or if a particularly strong adverse combination is present

What this fixes:

- weak signal no longer automatically becomes churn
- the baseline strategy becomes less trigger-happy without weakening hard safety exits

### Change 2: Cost Guard for Entry / Add / Reverse

New rule:

- before allowing a new opening, scale-in, or reversal target
- estimate a conservative one-way cost budget:
  - taker fee
  - expected slippage as a fraction of max tolerated slippage
- require the signal-strength proxy to exceed:
  - expected cost
  - plus a configurable net-edge buffer

Recommended initial approximation:

- estimated cost bps = `paper_taker_fee_bps + max_slippage_tolerance_bps * expected_slippage_fraction`
- signal edge proxy = function of:
  - `abs(composite_alpha_score)`
  - plus bonus for strong supportive microstructure
  - plus bonus for strong non-fallback AI edge when available

Important constraint:

- this gate should not block risk-reducing moves
- it should only apply to:
  - open
  - add
  - reverse

What this fixes:

- weak alpha no longer results in frequent uneconomic entries

### Change 3: Transient Close Retry Cooldown

New rule:

- if a recent close attempt failed with a known transient exchange error
- and the next close attempt is medium/low urgency with essentially the same size and intent
- block the retry locally for a short cooldown window

Safety constraint:

- do not apply cooldown to `high` urgency intents

What this fixes:

- repeated noisy close attempts after transient exchange congestion

### Phase 1 Status

This implementation pass should include:

- flat-signal hold behavior
- explicit flat-exit conditions
- entry cost guard
- transient close retry cooldown
- unit tests for each new behavior
- runtime validation after deployment

## 5.2 Phase 2: Proper Entry / Hold / Exit / Reverse Bands

After Phase 1 is stable, the next step is to split the current threshold model.

Required changes:

- separate `entry_threshold`
- separate `hold_threshold`
- separate `exit_threshold`
- separate `reverse_threshold`

Target semantics:

- entry requires the strongest evidence
- hold requires less evidence than entry
- exit requires stronger adverse evidence than "not enough to add"
- reverse requires stronger evidence than simple exit

This phase should be done in `BaselineStrategy`, not only in the target engine.

## 5.3 Phase 3: Position Manager Separation

Refactor target generation into two steps:

- `SignalDecision`
- `PositionManager`

`SignalDecision` should answer:

- preferred side
- confidence
- whether trading is justified

`PositionManager` should answer:

- hold
- add
- reduce
- close
- reverse
- target leverage

Benefits:

- clearer invariants
- easier testing
- easier future addition of holding-time logic and PnL-aware exits

## 5.4 Phase 4: Holding-Time and PnL-Aware Exits

Add state-aware exit rules:

- maximum holding duration
- time-decay of conviction
- minimum unrealized improvement expectation
- optional stop-loss / time-stop / stale-signal cleanup

This phase should only happen after the simpler semantic fixes are proven stable.

## 5.5 Phase 5: Better Alpha Calibration

The current feature stack may continue to be useful as a regime and risk filter, even if it is not the final trading alpha.

Potential next steps:

- calibrate `composite_alpha_score` against realized forward returns
- derive empirical edge curves by regime
- separate "eligibility model" from "execution alpha"
- learn or fit alpha-to-edge translation instead of using a static proxy

## 6. Configuration Additions

Recommended strategy settings:

- `strategy_flat_signal_hold_enabled`
- `strategy_flat_exit_microstructure_threshold`
- `strategy_flat_exit_factor_threshold`
- `strategy_flat_exit_ai_edge_threshold`
- `strategy_cost_guard_enabled`
- `strategy_alpha_edge_bps_scale`
- `strategy_expected_slippage_bps_fraction`
- `strategy_min_net_edge_bps`
- `strategy_transient_close_retry_cooldown_seconds`

These settings should stay conservative by default.

## 7. Testing Plan

### Unit tests required

- derivatives flat signal holds existing long when adverse evidence is weak
- derivatives flat signal exits when multiple adverse factors align
- weak entry signal is blocked by cost guard
- strong entry signal survives cost guard
- transient close failure places the next similar close into cooldown
- high-urgency close is not blocked by cooldown

### Integration tests required

- no regression in guarded simulated mode
- no regression in risk-blocking behavior
- no regression in reconciliation-required blocking
- no regression in execution outbox flow

### Runtime checks required

- strategy still produces decisions
- decision rate does not collapse unexpectedly
- repeated target flips reduce meaningfully
- execution error volume from repeated close attempts decreases
- health and reconciliation remain green

## 8. Risk Review

Potential risks introduced by the rework:

- holding too long on weak signals
- blocking a useful retry after an exchange transient error
- over-constraining entries and reducing opportunity capture

Mitigations:

- keep flat-hold logic limited to derivatives and neutral baseline states
- keep retry cooldown limited to transient failures and non-high urgency close intents
- keep cost guard only on open/add/reverse paths
- add tests that confirm risk-reducing actions are not suppressed incorrectly

## 9. Implementation Order

1. Add configuration knobs
2. Add flat-signal hold logic
3. Add explicit flat-exit rule
4. Add cost guard for open/add/reverse
5. Add transient close retry cooldown
6. Update unit tests
7. Run guarded-live and guarded-simulated regression tests
8. Start server and validate decision/execution behavior from runtime logs and APIs
9. Iterate on bugs before deployment

## 10. Expected Outcome

After Phase 1, the strategy should behave more like:

- "open only when the edge appears worth paying for"
- "hold through weak neutral noise"
- "exit only when adverse evidence is explicit"
- "do not spam the exchange with repeated medium-urgency close retries after transient failures"

That is still a conservative baseline strategy, but it is materially better aligned with live trading economics.
