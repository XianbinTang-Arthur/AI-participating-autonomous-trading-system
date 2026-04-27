# Directional Impulse Chase Entry Admission SOW

## Business objectives and boundaries
- Objective: prevent directional impulse override from opening or scaling into BTC-USDT-SWAP after a sharp spike/pullback pattern that looks like late chase execution rather than stable continuation.
- Boundary: OKX derivatives directional live carrier only. No new symbol, venue, strategy family, provider path, release/promotion/tuning path, or risk limit expansion.

## Module responsibilities and domain model
- `BaselineAssessment.direction_rule` identifies impulse override intent via `baseline_impulse_override_long` / `baseline_impulse_override_short`.
- `TargetPositionEngine` is responsible for target admission before order intent generation.
- `MarketSnapshot.recent_trades` and `kline_15m` provide local microstructure evidence for spike and pullback classification.

## Input/output interfaces
- Inputs: `DecisionContext.market_snapshot`, `DecisionContext.market_last_price`, baseline direction/rule, current and desired target quantity.
- Output: unchanged target quantity when microstructure is acceptable; current position quantity with guardrail flag when impulse-chase risk is detected.
- Guardrail flags are persisted through existing `PositionTarget.guardrail_flags` and blocker-chain surfaces.

## Database schema / tables / indexes / constraints
- No schema changes.
- No table or index changes.

## Transactions, Consistency, Concurrency
- The guard runs in deterministic target construction before execution intent creation.
- It does not rely on mutable external state and does not introduce new transaction boundaries.
- It complements, rather than replaces, the deployed duplicate directional risk-increase convergence guard.

## Authorization, Authentication, Data Security
- No credential access.
- No operator/auth changes.
- No secret-bearing output.

## Error Handling and Idempotency
- Missing or malformed market microstructure evidence degrades to no additional block; existing cost, cooldown, and performance gates still apply.
- Decimal math is used for boundary checks to preserve deterministic behavior.
- Repeated identical inputs produce identical guardrail flags and targets.

## State Transition and Lifecycle
- Only risk-increasing impulse entry/scale-in targets can be blocked.
- Exit, reduce, flat-hold, emergency protective exit, alpha decay, and risk contraction lifecycle transitions are unchanged.

## Caching and Performance
- Uses the in-memory market snapshot already present in the decision context.
- Recent trade scan is bounded to the latest 32 trade prices.

## Logging, Monitoring, Auditing
- Guardrail flags expose the blocker reason in the target gate and decision blocker chain.
- No new metrics are required for this bounded task.

## Testing Strategy
- Unit tests cover:
  - sharp impulse spike followed by immediate pullback blocks a new directional risk increase;
  - ordinary non-spike trend continuation remains admissible.
- Existing cost/cooldown/risk tests remain authoritative for other gates.

## Migration, Rollback, Compatibility
- Backward compatible: no public API or schema change.
- Rollback: revert the impulse admission commit, push, and redeploy through `bash scripts/deploy.sh --skip-commit`.

## Configuration and Environment Isolation
- No new environment variables.
- The guard uses conservative internal thresholds to avoid widening runtime configuration surface during live operation.

## Code Organization and Dependencies
- Implementation stays in `aats/services/decision_engine/target_position.py`.
- Tests stay in `tests/unit/test_target_position_engine.py`.
- No new third-party dependency.

## Documentation and Operations Manual
- This SOW documents scope and rollback.
- Runtime operators should treat the new guardrail reason as a trading-quality admission block, not as infrastructure failure.

## Deployment and Acceptance Criteria
- Acceptance:
  - a sharp one-minute spike followed by immediate pullback cannot open a new directional risk increase solely via impulse override;
  - normal trend continuation with stable microstructure is not blanket-blocked;
  - focused tests, ruff, full unit tests, and affected integration validation pass;
  - post-deploy runtime truth has no blocking findings.
