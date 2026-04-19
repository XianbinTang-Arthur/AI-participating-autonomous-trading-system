# Task 156 - Independent Deliverables 1-5 Review Bugfix SOW

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。


## Business objectives and boundaries
- Review the existing Deliverable 1-5 implementation for the independent family.
- Fix only real defects found in the current implementation.
- Preserve existing public payload shapes and avoid unrelated refactors.

## Module responsibilities and domain model
- `independent_family.py`: book-level state machine, thesis-aware exits, execution policy.
- `settings.py` and managed profiles: runtime-safe configuration and validation.
- `query_service.py`: operator/runtime configuration surface.
- `strategy-view.js`: operator UI configuration visibility.

## Input/output interfaces
- Inputs:
  - independent family settings from managed profiles and env-derived settings
  - `DecisionContext`, `BaselineAssessment`, `AIMarketAssessment`
- Outputs:
  - `StrategyCandidate`
  - book-native runtime state
  - operator/runtime configured parameters

## State transition and lifecycle
- Existing independent positions must not remain in `hold` when execution health is already classified as blocked.
- Action-specific execution policy settings must be explicit and observable without changing legacy defaults unless profiles opt in.

## Configuration and environment isolation
- Add independent execution-policy settings with safe `adaptive` defaults.
- Explicitly wire recommended values into managed derivatives profiles.

## Testing strategy
- Unit tests:
  - blocked execution health on an existing book forces `de_risk`
  - explicit configured execution-policy modes and offsets are honored
  - settings/profile validation for the new execution-policy fields
- Integration tests:
  - runtime payload exposes the new execution-policy settings
  - dashboard strategy view surfaces the new configuration row

## Migration, rollback, compatibility
- Backward compatible:
  - new settings default to `adaptive`
  - existing consumers keep working
  - profiles can opt into explicit action-specific execution modes
