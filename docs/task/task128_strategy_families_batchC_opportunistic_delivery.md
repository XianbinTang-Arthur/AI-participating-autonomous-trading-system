# Task 128：Strategy Families 重构 Batch C 交付

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。


## 范围

本批次只迁移 `opportunistic family` 的业务评估归属：

- 把 opportunistic overlay 的评估、rollout gate、fee/churn guard、min hold、rebalance cooldown 迁入独立 family 模块
- 让 coordinator / snapshot / audit 首次出现真实 `family="opportunistic"` 候选
- 不提前切 allocator / apply / execution 主路径

## 本次变更

### 1. 新增 opportunistic family 真实评估引擎

文件：

- `aats/services/strategy_engines/families/opportunistic_family.py`

内容：

- 新增 `OpportunisticFamilyEngine(settings=...)`
- 新增 `opportunistic_candidate_from_directional_target(...)`
- 新增 `evaluate_opportunistic_overlay_decision(...)`
- 新增 `opportunistic_overlay_score(...)`
- 新增 candidate leg 构建与 headline helper

### 2. target_position 缩成薄封装

文件：

- `aats/services/decision_engine/target_position.py`

内容：

- `_opportunistic_overlay_decision()` 改为委托 family helper
- `_opportunistic_overlay_score()` 改为薄封装，继续保留现有 patch 点

### 3. coordinator / runtime 可见性

文件：

- `aats/services/strategy_engines/coordinator.py`
- `tests/unit/test_strategy_coordinator.py`
- `tests/integration/test_strategy_runtime_integration.py`

结果：

- snapshot / audit 可看到真实 opportunistic candidate
- allocator 仍只消费旧 allocatable families
- selected family 仍保持旧主线，不提前切流

## 验证目标

- 现有 opportunistic target_position 行为不回归
- snapshot / audit 不再显示 opportunistic placeholder skeleton
- 主执行路径仍由 directional 承接
