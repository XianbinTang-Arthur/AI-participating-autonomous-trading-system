# Task 223 - DeepSeek Timeout Budget

## Context

The derivatives live runtime is configured for `ai_decision_maker` and `enabled_live` execution suggestions with DeepSeek as the provider. Runtime evidence showed DeepSeek HTTP 200 responses followed by `ai_assessment_fallback` with `fallback_reason=ai_timeout`, leaving `effective_operating_mode=baseline_only`.

## Scope

- Increase only the `derivatives_live` AI request timeout budget.
- Keep provider, model, symbol, venue, strategy family, execution gates, risk gates, release gates, and promotion gates unchanged.
- Add a profile regression assertion so future profile edits do not silently restore the too-tight budget.

## Acceptance

- `configs/strategy_profiles/derivatives_live.yaml` has `ai_timeout_seconds: 30.0`.
- Unit profile tests confirm the managed derivatives live profile exposes the 30 second timeout.
- No strategy surface, timeframe, promotion, release, or execution gate is changed.

## Rollback

Restore `configs/strategy_profiles/derivatives_live.yaml` `ai_timeout_seconds` to `10.0` and redeploy through `scripts/deploy.sh`.
