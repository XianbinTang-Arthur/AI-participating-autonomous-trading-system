# Task 147 SOW - Independent Replay Version Surface And Compat Group Cleanup

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。


## Goal

- Continue shrinking residual `max_drawdown_bps` alias usage so ordinary unit tests stop treating it as a normal field.
- Keep only a small dedicated compatibility test surface for the alias.
- Surface independent `state_version` and score-stability `semantics_version` in replay/operator read paths so historical replay diagnostics do not rely on implicit code-generation knowledge.

## Scope

- `aats/services/operator/query_service.py`
- `aats/services/operator/audit_replay_queries.py`
- `aats/schemas/operator.py`
- `aats/api/static/modules/views/replay-view.js`
- focused unit/integration tests for replay/operator/dashboard read paths

## Non-Goals

- Do not remove the compatibility alias from `ScoreStabilityMetrics` itself.
- Do not change score-stability decision logic.
- Do not add new configuration keys.

## Validation

- `.\.venv\Scripts\python.exe -m ruff check aats\services\operator\query_service.py aats\services\operator\audit_replay_queries.py aats\schemas\operator.py tests\unit\test_audit_replay_queries.py tests\unit\test_independent_scoring.py tests\unit\test_independent_replay.py tests\unit\test_independent_score_stability_compat.py tests\integration\test_dashboard_ui.py`
- `.\.venv\Scripts\python.exe -m pytest tests\unit\test_audit_replay_queries.py tests\unit\test_independent_scoring.py tests\unit\test_independent_replay.py tests\unit\test_independent_score_stability_compat.py tests\unit\test_independent_family.py tests\unit\test_independent_engine.py tests\unit\test_independent_gates.py -q`
- `.\.venv\Scripts\python.exe -m pytest tests\integration\test_dashboard_ui.py -q -k "test_replay_view_surfaces_independent_version_diagnostics or test_risk_view_surfaces_independent_recovery_snapshot_versions"`
