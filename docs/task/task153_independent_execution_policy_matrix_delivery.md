# Task 153: Independent Execution Policy Matrix Delivery

## 交付范围

本次只实现 `independent_family_optimization_README.md` 中的 Deliverable 3：

- execution policy matrix
- `build_independent_leg()` 重构
- execution-related metrics

不包含 Deliverable 4 的 book-native runtime state objects，也不包含 Deliverable 5 的 replay / paper diagnostics。

## 本次改动

### 1. 新增统一 execution policy

文件：

- `aats/services/strategy_engines/families/independent_family.py`

主要变化：

- 新增 `IndependentExecutionPolicy`
- 新增 `_independent_execution_policy(...)`
- 新增 `_independent_edge_strength(...)`

当前 policy matrix 至少区分：

- `open`
- `scale_in`
- `de_risk`
- `close_failed_thesis`
- `close_stale_thesis`

并综合考虑：

- edge strength
- liquidity quality
- execution health
- weak-edge / passive-first 配置
- action type

### 2. 重构 `build_independent_leg(...)`

文件：

- `aats/services/strategy_engines/families/independent_family.py`

重构后：

- 不再在 `build_independent_leg(...)` 内散落执行偏好判断
- 改为直接消费 `IndependentBookEvaluation.execution_policy`
- 每条腿会明确写入：
  - `book_action`
  - `close_reason`
  - `policy_reason`
  - `execution_policy_urgency`
  - `expected_leg_cost_bps`
  - `expected_net_edge_bps`
  - `liquidity_quality_score`
  - `execution_health_state`
  - `max_acceptable_cost_bps`

### 3. planner / runtime / operator 串联

文件：

- `aats/services/execution_engine/planner.py`
- `aats/bootstrap/config.py`
- `aats/services/operator/query_service.py`
- `aats/schemas/strategy_runtime.py`
- `aats/api/static/modules/terms.js`
- `aats/api/static/modules/detail-drawers.js`

主要变化：

- planner 现在显式支持 `market` 型腿级 preference，不再只识别 `limit`
- strategy leg 可以带 `execution_policy_urgency`，并优先覆盖 `base_target.urgency`
- `book_expectancy_summary` 补充 execution-related metrics
- operator / decision drawer 可以直接看到：
  - policy reason
  - urgency
  - execution style / order type / tif
  - liquidity quality
  - execution health

## 验证

已通过：

- Python `ruff`
- `test_independent_family.py`
- `test_operator_position_states.py`
- `test_execution_planner.py`
- `test_strategy_coordinator.py` 中的 independent 用例
- `test_strategy_runtime_integration.py` 中的 independent runtime 用例
- `test_dashboard_ui.py` 中的 independent / execution discipline 用例
- `test_mainline_chain.py` 中的 independent mainline 用例

## 后续建议

下一步进入 Deliverable 4 时，建议只推进：

- book-native runtime state objects
- family summary / audit / replay 配套
- 避免在 Deliverable 4 之前继续把 realized diagnostics 提前混进来
