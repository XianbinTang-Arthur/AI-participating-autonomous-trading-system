# Task 152: Independent Thesis-Aware Exit Delivery

## 交付范围

本次只实现 `independent_family_optimization_README.md` 中的 Deliverable 2：

- 拆分 `close_failed_thesis / close_stale_thesis / de_risk`
- 新增统一 `close_reason`
- 将原本偏阈值式退出升级为 thesis-aware state machine

不包含 Deliverable 3 的 execution policy matrix，也不包含更深的 realized diagnostics/replay 扩展。

## 本次改动

### 1. 独立双书退出状态机升级

文件：

- `aats/services/strategy_engines/families/independent_family.py`

主要变化：

- `IndependentBookEvaluation` 新增：
  - `book_action`
  - `close_reason`
  - `thesis_age_seconds`
- 当前持仓 book 的退出逻辑不再只有 “低于 close 阈值就退出”，而是按 thesis-aware 顺序判断：
  - `failed_thesis`
  - `stale_thesis`
  - `execution_health_degraded`
  - `liquidity_degraded`
  - `weak_edge_de_risk`
- family-level action 新增：
  - `de_risk_independent_book`
  - `close_failed_thesis_independent_book`
  - `close_stale_thesis_independent_book`

### 2. schema / summary / operator 串联

文件：

- `aats/schemas/strategy_runtime.py`
- `aats/schemas/decision.py`
- `aats/services/strategy_engines/coordinator.py`
- `aats/services/strategy_engines/allocator.py`
- `aats/services/operator/query_service.py`

主要变化：

- `StrategyExecutionSummary` 新增 `close_reason`
- `HedgeOverlayDecision` 新增：
  - `close_reason`
  - `long_leg_close_reason`
  - `short_leg_close_reason`
- coordinator / allocator / operator summary 已识别新的 thesis-aware family action

### 3. settings / profile / UI 暴露

文件：

- `aats/bootstrap/settings.py`
- `configs/strategy_profiles/derivatives.yaml`
- `configs/strategy_profiles/derivatives_live.yaml`
- `aats/api/static/modules/terms.js`
- `aats/api/static/modules/views/strategy-view.js`

新增显式配置：

- `strategy_hedge_independent_max_thesis_age_seconds`
- `strategy_hedge_independent_de_risk_net_edge_bps`
- `strategy_hedge_independent_failed_thesis_net_edge_bps`
- `strategy_hedge_independent_execution_health_de_risk_enabled`
- `strategy_hedge_independent_liquidity_de_risk_enabled`

前端独立双书配置卡已展示以上配置。

## 验证

已通过：

- Python `ruff`
- `test_independent_family.py`
- `test_strategy_coordinator.py`
- `test_operator_position_states.py`
- `test_settings.py`
- `test_env_profiles.py`
- `test_strategy_runtime_integration.py` 中的 independent runtime 用例
- `test_dashboard_ui.py` 中的 independent 配置展示用例
- `test_mainline_chain.py` 中的 independent mainline 用例

## 后续建议

下一步进入 Deliverable 3 时，建议只处理：

- execution policy matrix
- thesis-aware close action 对应的更细执行策略
- replay / realized diagnostics 的扩展
