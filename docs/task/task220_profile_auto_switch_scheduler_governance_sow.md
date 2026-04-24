# Task 220 - Profile Auto-Switch Scheduler Governance

## Business Objectives And Boundaries

Reduce AI provider cost by keeping strategy profile auto-switch recommendation generation off the per-decision path, while preserving operator pause/restore semantics and existing risk/profile activation gates.

In scope:

- Fix the committed profile auto-switch scheduler lint blocker.
- Ensure the scheduler respects the effective auto-control state, including operator pause-auto state.
- Add focused tests for the scheduler gate.

Out of scope:

- Strategy family, symbol, venue, timeframe, tuning, release, or promotion changes.
- Any bypass of risk engine, kill switch, execution gates, truth chain, or profile activation gates.
- Deployment.

## Module Responsibilities And Domain Model

- `ApplicationRuntime` owns background task scheduling.
- `StrategyProfileControlService` owns strategy profile activation state and effective auto-control semantics.
- `StrategyProfileActivationFacade.evaluate_mainline_profile_control()` remains a per-decision read-side profile view and must not generate AI recommendations.

## Input/Output Interfaces

Input:

- Runtime settings: `strategy_profile_auto_control_enabled`.
- Activation state: `auto_switch_enabled`, updated by operator pause/restore and manual activation flows.

Output:

- Scheduler calls `evaluate_now(allow_auto_activation=True)` only when both configured and effective auto-control are enabled.
- Scheduler skips AI evaluation when the effective auto-control flag is false.

## Database Schema / Tables / Indexes / Constraints

No schema change.

## Transactions, Consistency, Concurrency

The scheduler reads activation state at each boundary before dispatching `evaluate_now()`. The read is sent through `asyncio.to_thread()` because the repository path is synchronous. Existing `evaluate_now()` concurrency guarantees remain unchanged.

## Authorization, Authentication, Data Security

No new API endpoint or credential path. Operator pause/restore authority remains unchanged.

## Error Handling And Idempotency

If effective-state loading fails, the scheduler records a background failure and skips that tick. This avoids generating AI calls when the scheduler cannot confirm auto-control is enabled.

## State Transition And Lifecycle

No new activation states. The existing `auto_switch_enabled` state now gates scheduled AI recommendation generation, not only auto-apply.

## Caching And Performance

At most one activation-state read per half-hour boundary per decision process. AI recommendation generation remains capped at two scheduled attempts per hour when effective auto-control is enabled.

## Logging, Monitoring, Auditing

Existing scheduled tick logging remains. Effective-state load failures are recorded via `_record_background_failure`.

## Testing Strategy

- Unit tests for half-hour boundary math.
- Unit tests for scheduler dispatch when effective auto-control is enabled.
- Unit tests for scheduler skip when configured or effective auto-control is disabled.
- Existing strategy profile activation switch tests remain valid.

## Migration, Rollback, Compatibility

No migration. Roll back by reverting this task commit.

## Configuration And Environment Isolation

No new environment variable. Existing `AATS_STRATEGY_PROFILE_AUTO_CONTROL_ENABLED` and activation-state pause/restore behavior remain authoritative.

## Code Organization And Dependencies

No new dependency.

## Documentation And Operations Manual

This SOW records the behavior contract. No operator runbook change is required.

## Deployment And Acceptance Criteria

Acceptance:

- Targeted ruff passes.
- Targeted unit tests pass.
- The scheduler does not call `evaluate_now()` when effective auto-control is disabled.
