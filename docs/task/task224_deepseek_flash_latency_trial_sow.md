# Task224 - DeepSeek Flash Live Latency Trial

## Business Objectives And Boundaries
Switch only the derivatives live AI model from `deepseek-v4-pro` to `deepseek-v4-flash` to test whether live decision cycles stop timing out under the existing AI-enabled trading microscope scope.

Out of scope: symbol, venue, strategy family, timeframe plumbing, risk gates, kill switch, execution gates, promotion gates, release gates, and tuning logic.

## Module Responsibilities And Domain Model
`configs/strategy_profiles/derivatives_live.yaml` owns the managed derivatives live AI model selection. `tests/unit/test_env_profiles.py` guards the managed profile contract.

## Input/Output Interfaces
Input is the managed profile configuration. Output is `AATSSettings.ai_model_name == "deepseek-v4-flash"` when the `derivatives_live` profile is loaded.

## Database Schema / Tables / Indexes / Constraints
No database schema, table, index, or constraint changes.

## Transactions, Consistency, Concurrency
No transactional behavior changes. Runtime consistency depends on normal deploy synchronization to WSL2 and container restart.

## Authorization, Authentication, Data Security
No credential or authentication change. Runtime checks must not print secrets.

## Error Handling And Idempotency
The profile load remains deterministic and idempotent. If DeepSeek Flash is unavailable or still too slow, the existing AI degradation and decision timeout behavior remains the fallback.

## State Transition And Lifecycle
No order, fill, or portfolio lifecycle state transition changes.

## Caching And Performance
This task is a latency experiment. Timeout stays at 30 seconds to isolate model selection as the only variable.

## Logging, Monitoring, Auditing
Post-deploy observation should check `/ai/runtime` and recent `aats-decision` logs for `decision_cycle_timeout`.

## Testing Strategy
Run the profile unit test and full unit suite after the configuration change.

## Migration, Rollback, Compatibility
Rollback is restoring `ai_model_name: deepseek-v4-pro` and redeploying through `scripts/deploy.sh`.

## Configuration And Environment Isolation
Only `derivatives_live` changes. Other profiles remain on their current configured models.

## Code Organization And Dependencies
No dependency or code organization change.

## Documentation And Operations Manual
This SOW is the operational record for the model trial.

## Deployment And Acceptance Criteria
Acceptance:
- `derivatives_live` managed profile loads `deepseek-v4-flash`.
- Validation passes.
- Deploy succeeds through `scripts/deploy.sh`.
- Required app containers are healthy.
- `/ai/runtime` remains `provider=deepseek` and `effective_operating_mode=ai_decision_maker`.
- Recent decision logs show whether `decision_cycle_timeout` persists after the switch.
