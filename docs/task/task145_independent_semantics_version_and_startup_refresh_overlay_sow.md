# Task 145 SOW - Independent Semantics Versioning And Startup Refresh Overlay Hardening

## Goal

Tighten three remaining semantic/observability gaps:

- stop treating `max_drawdown_bps` as a primary score-stability output while preserving a compatibility alias;
- promote startup exit-execution parent refresh failures into explicit startup recovery blockers and auditable snapshots;
- start using an explicit independent state-machine generation for new snapshots/runtime payloads.

## Scope

- `aats/services/strategy_engines/independent/models.py`
- `aats/services/strategy_engines/independent/scoring.py`
- `aats/services/strategy_engines/families/independent_family.py`
- `aats/services/strategy_engines/independent/replay.py`
- `aats/services/strategy_engines/independent/state_machine.py`
- `aats/services/recovery_control/startup_recovery.py`
- affected runtime/state schema defaults and focused tests

## Non-Goals

- Do not remove compatibility config keys such as `*_drawdown_bps`.
- Do not change order/execution public APIs unrelated to startup parent refresh overlay.

## Validation

- `.\.venv\Scripts\python.exe -m ruff check aats\services\strategy_engines\independent\models.py aats\services\strategy_engines\independent\scoring.py aats\services\strategy_engines\families\independent_family.py aats\services\strategy_engines\independent\replay.py aats\services\strategy_engines\independent\state_machine.py aats\services\recovery_control\startup_recovery.py aats\bootstrap\config.py tests\unit\test_independent_scoring.py tests\unit\test_independent_family.py tests\unit\test_independent_replay.py tests\unit\test_independent_state_machine.py tests\unit\test_startup_recovery.py tests\integration\test_recovery.py`
- `.\.venv\Scripts\python.exe -m pytest tests\unit\test_independent_scoring.py tests\unit\test_independent_family.py tests\unit\test_independent_replay.py tests\unit\test_independent_state_machine.py tests\unit\test_startup_recovery.py -q`
- `.\.venv\Scripts\python.exe -m pytest tests\integration\test_recovery.py -q -k "postgres_startup_recovery_promotes_parent_resume_issue_into_recovery_status or postgres_startup_recovery_blocks_resume_for_truth_pending_parent"`
