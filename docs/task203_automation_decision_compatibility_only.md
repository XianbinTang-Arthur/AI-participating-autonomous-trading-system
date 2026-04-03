# Task 203 - Mark Per-Decision Automation State as Compatibility-Only

## Business objectives and boundaries
- Keep `automation_state` readable for historical consumers.
- Make `execution_control_mode` and `execution_behavior` the only primary diagnostic signals on single automation decisions.
- Stop using `automation_state` as a first-class explanation field in runtime UI details.
- Do not change allocator routing or execution behavior in this task.

## Module responsibilities and domain model
- `strategy_runtime.py`
  - Marks `StrategySleeveAutomationDecision.automation_state` as compatibility-only.
  - Defines `compatibility.legacy_automation_state` as the preferred legacy mirror.
- `auto_parallel.py`
  - Continues writing the legacy mirror for each automation decision.
- `query_service.py`
  - Reads legacy automation state from `compatibility` first, then falls back only for historical payloads.
- `strategy-view.js`
  - Keeps execution behavior and execution control mode as the primary displayed semantics.

## Input/output interfaces
- Inputs:
  - per-decision automation payloads
  - `execution_control_mode`
  - `execution_behavior`
  - `compatibility.legacy_automation_state`
- Outputs:
  - runtime payloads where single decisions still expose `automation_state` for compatibility
  - tests asserting primary diagnostics do not depend on `automation_state`

## Database schema / tables / indexes / constraints
- No database schema changes.
- Event payload shape remains backward compatible.

## Transactions, consistency, concurrency
- No transaction or concurrency model changes.

## Authorization, authentication, data security
- No auth or security behavior changes.

## Error handling and idempotency
- Historical payloads without `compatibility.legacy_automation_state` still fall back to `automation_state`.
- New payloads should always emit the compatibility mirror.

## State transition and lifecycle
- No strategy state machine changes.
- Only the diagnostic interpretation layer changes.

## Caching and performance
- No meaningful runtime cost impact.

## Logging, monitoring, auditing
- Runtime compatibility summaries continue to aggregate legacy automation states from the compatibility mirror.
- Operator/runtime diagnostics remain driven by canonical control mode and execution behavior.

## Testing strategy
- Update controller-level tests to assert `automation_state == compatibility.legacy_automation_state`.
- Update runtime integration tests to assert the compatibility mirror on single automation decisions.
- Update dashboard UI tests to confirm legacy automation state labels are not rendered in primary UI copy.

## Migration, rollback, compatibility
- Backward compatible.
- Rollback is safe because the top-level `automation_state` field remains present.

## Configuration and environment isolation
- No config changes.

## Code organization and dependencies
- Minimal change set limited to schema docs and tests.

## Documentation and operations manual
- This task closes the remaining gap between runtime compatibility summaries and per-decision compatibility semantics.

## Deployment and acceptance criteria
- Single automation decisions continue exposing `automation_state`, but it is explicitly documented as compatibility-only.
- New tests prove the legacy mirror exists and is the preferred compatibility source.
- Strategy UI no longer surfaces raw legacy automation state labels in primary detail copy.
