# Task 172 - Independent Phase-1 Refactor PR1 to PR5

## Label

- `EXTRACTION-ONLY`

## Business objectives and boundaries

- Refactor the current `independent` strategy into a dedicated submodule package.
- Preserve current phase-1 behavior as much as possible.
- Keep `aats/services/strategy_engines/families/independent_family.py` as the external adapter entrypoint.
- Keep runtime/output compatibility; new fields must be additive only.
- Do not introduce state-machine semantics changes, health-kernel enforcement changes, adaptive thresholds, or replay/recovery redesign in this task.

## Module responsibilities and domain model

- `independent/models.py`
  - shared dataclasses and compatibility aliases
- `independent/scoring.py`
  - book score, signal edge, stability, candidate confidence
- `independent/lifecycle.py`
  - thesis age, cooldowns, close reasons, de-risk target qty
- `independent/execution_policy.py`
  - execution policy resolution
- `independent/gates.py`
  - open gate and entry quality gate helpers
- `independent/engine.py`
  - book-level orchestration
- `independent/diagnostics.py`
  - runtime state shaping
- `families/independent_family.py`
  - thin compatibility adapter and family-level wiring

## Input/output interfaces

- Preserve:
  - `IndependentFamilyEngine.evaluate(...)`
  - `StrategyCandidate`
  - `StrategyBookRuntimeState`
  - `StrategyBookExpectancyEntry`
- Keep legacy helper names in `families/independent_family.py` as wrappers during phase 1.
- Preserve legacy outward fields:
  - `state`
  - `score`
  - `expectancy`
  - `execution_policy`
  - `reason_codes`
  - `blocked_reasons`
  - `book_action`
  - `close_reason`
  - `policy_reason`

## Database schema / tables / indexes / constraints

- No database schema changes in this task.

## Transactions, Consistency, Concurrency

- No transaction semantics changes in this task.
- This refactor is code movement and adapter preservation only.

## Authorization, Authentication, Data Security

- No auth or security model changes.

## Error Handling and Idempotency

- Preserve existing `independent` fail-closed semantics.
- Custom expectancy resolver failure remains `None -> blocked`.

## State Transition and Lifecycle

- Preserve existing state strings and close reason behavior.
- Do not introduce explicit state-machine semantics yet.

## Caching and Performance

- No intended performance changes.
- Avoid repeated recomputation where helper extraction naturally centralizes logic.

## Logging, Monitoring, Auditing

- Preserve existing reason codes, blocked reasons, close reasons, and policy reasons.
- New runtime schema fields are additive only.

## Testing Strategy

- Add phase-1 extraction freeze tests:
  - `tests/unit/test_independent_scoring.py`
  - `tests/unit/test_independent_lifecycle.py`
  - `tests/unit/test_independent_execution_policy.py`
  - `tests/unit/test_independent_gates.py`
  - `tests/unit/test_independent_engine.py`
- Keep and adapt `tests/unit/test_independent_family.py`.
- Run:
  - `ruff check`
  - `python -m compileall` for touched packages
  - touched unit tests
  - the narrowest integration test for independent runtime output

## Migration, Rollback, Compatibility

- Compatibility wrappers remain in `families/independent_family.py`.
- New runtime fields are optional/additive.
- Rollback is low risk because external entrypoints remain unchanged.

## Configuration and Environment Isolation

- No config semantics changes in this task.

## Code Organization and Dependencies

- New package:
  - `aats/services/strategy_engines/independent/`
- Avoid circular imports by keeping `families/independent_family.py` as adapter only.

## Documentation and Operations Manual

- This document is the execution record for PR1 to PR5 completion in one local change set.

## Deployment and Acceptance Criteria

- Independent submodule skeleton exists and imports cleanly.
- Dataclasses are imported from `independent/models.py`.
- Scoring, lifecycle, execution policy, gates, engine, and diagnostics modules exist.
- `_evaluate_independent_book(...)` delegates to the new engine.
- New runtime schema fields are additive and optional.
- Tests and compile checks pass.
