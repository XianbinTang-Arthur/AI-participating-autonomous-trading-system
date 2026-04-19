# Task 201 - Auto Parallel Permission Alignment and Controller Orchestration Coverage

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。


## Business objectives and boundaries
- Eliminate the remaining semantic split where `approved_for_execution=True` could coexist with `execution_compatible=False`.
- Reduce the diagnostic weight of legacy `automation_state` so `budget_zero_suppressed` no longer looks like a plain pause.
- Add controller-level tests that validate the orchestrated behavior of `StrategySleeveAutoController`, not just the individual subcomponents.
- Keep allocator route semantics unchanged; this task focuses on permission alignment, runtime interpretation, and test coverage.

## Module responsibilities and domain model
- `sleeve_execution_permission.py`: execution permission must consider execution compatibility as a hard prerequisite.
- `auto_parallel.py`: controller keeps orchestration responsibility, but the emitted automation state and operator summary should no longer blur permission denial with approved-but-suppressed behavior.
- `strategy-view.js`: UI should prioritize `execution_behavior` and `execution_control_mode` over legacy `automation_state` labels.
- `query_service.py`: runtime summary should prefer `entry_auto_execution_enabled` over the deprecated `auto_parallel_enabled` name.

## Input/output interfaces
- Inputs:
  - `RawSleeveCandidateInputs.candidate_execution_compatible`
  - permission/budget/composition decisions
  - recent runtime summary payloads
- Outputs:
  - `ExecutionPermissionDecision.candidate_execution_compatible`
  - explicit permission denial reason `candidate_execution_incompatible`
  - `automation_state="contracted"` for budget-zero and budget-contracted paths
  - controller-level tests that lock end-to-end orchestration semantics

## State transition and lifecycle
- If candidate execution compatibility fails, permission is denied before composition, even if runtime support remains true.
- `budget_zero_suppressed` remains an approved path, but its legacy `automation_state` is now mapped to `contracted` rather than `paused`/`protective_only`.
- `protective_override` remains the only path that should surface as `protective_only`.

## Logging, monitoring, auditing
- `control_trace.permission` now records `candidate_execution_compatible`.
- Controller/operator summaries explicitly explain execution-compatibility denial.
- Strategy UI surfaces execution behavior as the primary runtime signal and uses legacy automation state only as secondary context.

## Testing strategy
- Extend permission unit tests with an execution-compatibility denial case.
- Add a dedicated `test_auto_parallel_orchestration.py` that covers:
  - permission denied
  - approved and directly executable
  - approved but budget suppressed
  - protective override
  - execution-incompatible candidate
- Update strategy coordinator expectations where legacy `automation_state` changes from `paused`/`protective_only` to `contracted`.
- Update runtime integration tests so `entry_auto_execution_enabled` replaces `auto_parallel_enabled` in the primary summary contract.

## Compatibility and rollback
- `automation_state` is retained for compatibility, but it is no longer treated as the primary diagnostic surface.
- UI keeps a fallback read of historical `auto_parallel_enabled` payloads so older snapshots can still render during the migration window.
