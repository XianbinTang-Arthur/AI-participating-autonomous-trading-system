# Task 173 - Independent 后续模块加性收口

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。


## 背景

`independent` 已完成 phase-1 抽取，但 phase-2 到 phase-6 的新增模块此前仍主要停留在 helper 或测试层：

- `state_machine.py`
- `health.py`
- `sizing.py`
- `adaptive.py`
- `replay.py`

本轮目标不是引入新的 live 语义，而是把这些模块以加性方式接入现有运行时与诊断链路。

## 本轮改动

1. `engine.py`
   - `score_adjusted` 在无自适应修正时显式回填为 `score`
   - 生成并挂接：
     - `threshold_snapshot`
     - `state_snapshot`
     - `health_snapshot`
     - `replay_snapshot`
   - `build_independent_family_candidate(...)` 现在保留 `family_health`

2. `models.py`
   - `IndependentBookDecision` 新增后续阶段的加性快照字段
   - `IndependentFamilyEvaluation` 新增 `family_health`

3. `strategy_runtime.py`
   - 新增：
     - `StrategyAdaptiveThresholdSnapshot`
     - `StrategyIndependentLegHealthSummary`
   - `StrategyBookRuntimeState` 新增：
     - `threshold_snapshot`
     - `leg_health_summary`

4. `diagnostics.py`
   - `runtime_state_from_decision(...)` 可选接收 threshold / health 快照
   - 运行时状态可携带新的加性摘要

5. `families/independent_family.py`
   - candidate metrics 现在显式暴露：
     - family health
     - threshold snapshot
     - replay snapshot
     - 细化后的 `book_state / holding_phase / health_state`

## 兼容性

- 保留旧 `state` 语义，不删除旧字段
- 不改变当前 independent 的 live gating
- 新字段全部为加性字段

## 验证

- `ruff check`
- `compileall`
- independent 相关 unit tests
- `test_strategy_runtime_integration.py` 最窄 integration
