# Task 144 SOW - Independent Score Stability Display Language Cleanup

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。


## Goal

Remove the remaining read-side “max drawdown / 回撤阈值 / 分数回撤” wording from operator/dashboard display paths for independent score stability, and replace it with explicit Chinese wording:

- 上行抬升幅度
- 向下回撤幅度

## Scope

- Update the independent strategy dashboard copy in `aats/api/static/modules/views/strategy-view.js`.
- Add dashboard integration coverage to lock the new wording in place.

## Non-Goals

- Do not rename settings keys such as `*_drawdown_bps`.
- Do not change scoring logic, replay payload keys, or query API structure.

## Validation

- `.\.venv\Scripts\python.exe -m ruff check tests\integration\test_dashboard_ui.py`
- `node --check aats\api\static\modules\views\strategy-view.js`
- `.\.venv\Scripts\python.exe -m pytest tests\integration\test_dashboard_ui.py -q -k "test_strategy_view_surfaces_independent_overlay_config_and_state"`
