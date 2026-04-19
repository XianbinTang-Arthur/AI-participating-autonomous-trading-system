# Task 154 - Independent Book Runtime State Delivery

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。


## Business objective and boundary

- Deliverable 4 only upgrades `independent` into a book-native family on the control plane.
- This task adds first-class long/short book runtime state objects and threads them through runtime, operator, and replay.
- This task does not change allocator selection rules, risk approval logic, or realized diagnostics.

## Module responsibilities and domain model

- `independent_family.py`
  - owns long/short book state evaluation
  - emits native per-book runtime state alongside legs and expectancy
- `independent_models.py`
  - owns the service-layer dataclass for independent book runtime state
- `decision.py` / `strategy_runtime.py`
  - expose additive schema fields so runtime, operator, and replay can consume native per-book state
- `coordinator.py`
  - copies selected family book runtime state into the applied target and decision outcome
- `query_service.py`
  - backfills top-level payload fields for operator and external consumers
- `replay.py`
  - validates that independent decision chains carry complete long/short book runtime state

## Input/output interfaces

- Inputs
  - current long/short inventory from `DecisionContext`
  - latest fill/open/close timestamps from `DecisionContext`
  - per-book expectancy and policy evaluation from `independent_family`
- Outputs
  - `StrategyCandidate.book_runtime_states`
  - `StrategyExecutionSummary.book_runtime_states`
  - `PositionTarget.book_runtime_states`
  - `DecisionOutcome.book_runtime_states`

## State transition and lifecycle

- Each independent book state carries:
  - current qty
  - target qty
  - state
  - book action
  - close reason
  - policy reason
  - thesis timestamps and age
  - cooldown horizon
  - expectancy snapshot
  - execution health
- Family action remains an aggregation; long/short book runtime state becomes the native lifecycle record.

## Replay, audit, and operator requirements

- Operator payloads must expose top-level `book_runtime_states`.
- Decision audit must include the same state objects without forcing consumers to infer from `book_expectancy_summary`.
- Replay must flag independent chains that are missing long/short runtime state when family execution summary exists.

## Testing strategy

- unit
  - independent family emits two runtime state objects
  - coordinator copies them into summary/target/outcome
  - operator payload backfills top-level fields
- integration
  - runtime/operator API exposes top-level book runtime states
  - replay remains consistent for independent bundle flow

## Compatibility and rollback

- change is additive only
- existing `book_expectancy_summary` remains unchanged
- other families keep an empty runtime-state list until they adopt the same contract
