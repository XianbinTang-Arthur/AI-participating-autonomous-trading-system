# Task127：Strategy Families 重构 Batch B 交付说明

## 1. 本批目标

本批只迁移 `protective family`，不提前切换 allocator 或主执行路径。

目标：

- 把 `protective` 的业务评估从 `target_position.py` 迁到独立 family 模块
- 保留原有 protective 的 gating / cooldown / min_hold 语义
- 让 coordinator / snapshot / audit 首次出现真实 `family="protective"` 业务候选
- 保持当前主执行仍由 `directional` 承接，避免在 Batch B 提前切流

## 2. 实际改动

### 2.1 Protective 评估逻辑迁出

新增 [protective_family.py](/D:/文件/project/AIParticipatingAutonomousTradingSystem/aats/services/strategy_engines/families/protective_family.py)：

- `evaluate_protective_overlay_decision(...)`
- `protective_candidate_from_directional_target(...)`
- `build_protective_candidate_leg(...)`

这里承接了原先 `target_position.py` 中 protective 的核心逻辑：

- protective 专属 gating
- pressure score
- min hold
- rebalance cooldown
- protective overlay decision 组装

### 2.2 TargetPositionEngine 改成调用迁出后的 protective helper

[target_position.py](/D:/文件/project/AIParticipatingAutonomousTradingSystem/aats/services/decision_engine/target_position.py) 不再内嵌 `_protective_overlay_decision()` 和 `_protective_pressure_score()` 实现，而是直接调用 family 模块里的 helper。

这一步的目的不是切流，而是先完成“protective 业务逻辑的归属迁移”。

### 2.3 Coordinator 引入 ai_assessment 并注册真实 ProtectiveFamilyEngine

为了让 protective family 自己完成评估：

- [base.py](/D:/文件/project/AIParticipatingAutonomousTradingSystem/aats/services/strategy_engines/base.py)
  - `StrategyEngineInput`
  - `StrategyEvaluationContext`
  新增 `ai_assessment`
- [orchestrator.py](/D:/文件/project/AIParticipatingAutonomousTradingSystem/aats/services/decision_engine/orchestrator.py)
  把 `ai_assessment` 传进 coordinator
- [coordinator.py](/D:/文件/project/AIParticipatingAutonomousTradingSystem/aats/services/strategy_engines/coordinator.py)
  注册真实 `ProtectiveFamilyEngine(settings=...)`

### 2.4 当前阶段的 protective candidate 语义

protective candidate 现在：

- `family = "protective"`
- 会带真实 `state`
- 会带真实 `execution_mode = "protective_overlay"`
- 会带真实 protective metrics / blocking reasons / cooldown 信息
- 会带 protective 自己的 hedge leg

但当前仍然：

- `selectable = false`
- allocator 不参与选择它
- 真正 applied target 仍走 legacy directional 主线

这符合 Batch B 约束：先让 family identity 成立，不提前切执行主路径。

## 3. 验收结果

本批验收通过的点：

- 当 `strategy_family_protective_enabled = true` 且 protective 模式激活时
  - snapshot 中出现真实 protective candidate
  - 不再是 placeholder skeleton
- protective 旧的 target_position protective 行为未回归
- 当前 selected family 仍保持 `directional`
  - 说明没有提前切 allocator / apply path

## 4. 已知边界

本批没有完成：

- protective family 的 allocator 接管
- protective family 的 selected/apply 切流
- opportunistic family 业务迁移
- independent family 业务迁移

因此当前系统状态是：

- `protective` 已经完成“业务评估归属迁移 + snapshot/audit 身份成立”
- 但执行主路径仍是 legacy directional 承接

## 5. 回滚方式

如果要回滚 Batch B：

1. 关闭 `strategy_family_protective_enabled`
2. coordinator 恢复看到 disabled protective skeleton
3. legacy directional protective path 仍可继续工作
