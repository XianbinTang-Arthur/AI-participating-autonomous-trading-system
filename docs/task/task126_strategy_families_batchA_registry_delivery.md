# Task126：Strategy Families Batch A Registry / Coordinator 骨架交付

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。


## 1. 本批次目标

本批次只完成 Step 1 设计文档后的第一批代码骨架，不迁移三条 family 的业务逻辑。

目标是：

- 扩展 `StrategyFamily`
- 新增 family registry / protocol / evaluation context
- 改造 coordinator 支持注册式 family evaluation
- 接入 `protective / opportunistic / independent` 3 个 skeleton family engine
- 让 snapshot / audit 先能识别新 family identity

本批次明确不做：

- protective 业务迁移
- opportunistic 业务迁移
- independent 业务迁移
- legacy hedge path 切流

## 2. 代码修改概览

### 2.1 family 类型与开关

扩展：

- [settings.py](/D:/文件/project/AIParticipatingAutonomousTradingSystem/aats/bootstrap/settings.py)
- [strategy_runtime.py](/D:/文件/project/AIParticipatingAutonomousTradingSystem/aats/schemas/strategy_runtime.py)

新增 family：

- `protective`
- `opportunistic`
- `independent`

新增 Batch A 需要的骨架开关：

- `strategy_family_protective_enabled`
- `strategy_family_opportunistic_enabled`
- `strategy_family_independent_enabled`
- `strategy_family_protective_shadow_mode_enabled`
- `strategy_family_opportunistic_shadow_mode_enabled`
- `strategy_family_independent_shadow_mode_enabled`
- `strategy_family_protective_live_execution_enabled`
- `strategy_family_opportunistic_live_execution_enabled`
- `strategy_family_independent_live_execution_enabled`

默认全部为 `false`，避免改变当前 live 行为。

### 2.2 新 registry / protocol / evaluation context

新增：

- [base.py](/D:/文件/project/AIParticipatingAutonomousTradingSystem/aats/services/strategy_engines/base.py)
  - `StrategyFamilyRuntimeControl`
  - `StrategyEvaluationContext`
  - `StrategyFamilyEngine`
- [registry.py](/D:/文件/project/AIParticipatingAutonomousTradingSystem/aats/services/strategy_engines/families/registry.py)
  - `StrategyFamilyRegistry`

### 2.3 family skeleton engines

新增目录：

- [families](/D:/文件/project/AIParticipatingAutonomousTradingSystem/aats/services/strategy_engines/families)

新增文件：

- [legacy_adapters.py](/D:/文件/project/AIParticipatingAutonomousTradingSystem/aats/services/strategy_engines/families/legacy_adapters.py)
- [protective_family.py](/D:/文件/project/AIParticipatingAutonomousTradingSystem/aats/services/strategy_engines/families/protective_family.py)
- [opportunistic_family.py](/D:/文件/project/AIParticipatingAutonomousTradingSystem/aats/services/strategy_engines/families/opportunistic_family.py)
- [independent_family.py](/D:/文件/project/AIParticipatingAutonomousTradingSystem/aats/services/strategy_engines/families/independent_family.py)

当前 skeleton family 的行为：

- 已注册
- 已参与 coordinator snapshot
- 仅写 identity / disabled 或 inactive 状态
- 不可 selectable
- 不可 execution_compatible

### 2.4 coordinator 改造

修改：

- [coordinator.py](/D:/文件/project/AIParticipatingAutonomousTradingSystem/aats/services/strategy_engines/coordinator.py)

本批次变化：

- coordinator 改为通过 registry 做 family evaluation
- 旧 `directional / smart_arbitrage / spot_grid / dca` 先通过 adapter 挂入 registry
- 新 `protective / opportunistic / independent` skeleton 也同时进入 snapshot candidates
- allocator 仍只消费旧的 4 个 allocatable families

### 2.5 snapshot 保留新 family

修改：

- [auto_parallel.py](/D:/文件/project/AIParticipatingAutonomousTradingSystem/aats/services/strategy_engines/auto_parallel.py)

目的：

- 保证没有 sleeve intent 的新 skeleton family 不会在 auto-control 阶段被静默丢掉
- 让 snapshot / audit 能真实看到新 family identity

## 3. 行为结果

本批次之后：

- 新 family 已经进入 coordinator 的候选视图
- snapshot / audit 能看到：
  - `protective`
  - `opportunistic`
  - `independent`
- 但它们当前只会以：
  - `disabled`
  - 或 future batch 的 `inactive`
  skeleton 形式出现

同时：

- 旧 directional internal hedge path 仍然保持不变
- allocator 仍只消费：
  - `directional`
  - `smart_arbitrage`
  - `spot_grid`
  - `dca`

## 4. 测试

新增/更新：

- [test_strategy_coordinator.py](/D:/文件/project/AIParticipatingAutonomousTradingSystem/tests/unit/test_strategy_coordinator.py)
  - 验证 snapshot candidates 已包含 3 个新 family skeleton
- [test_strategy_runtime_integration.py](/D:/文件/project/AIParticipatingAutonomousTradingSystem/tests/integration/test_strategy_runtime_integration.py)
  - 验证 `/strategy/runtime` 的 `latest_snapshot.candidates` 已暴露 3 个新 family skeleton

## 5. 剩余风险

本批次还没有解决这些核心问题：

- `protective / opportunistic / independent` 仍未迁出 `target_position.py`
- `independent` 还没有 expectancy gating / hysteresis / execution gating
- allocator 还不能消费新 family 的真实 candidate
- control plane / execution plane 语义分裂尚未修复

这些问题留给下一批：

- Batch B：protective family
- Batch C：opportunistic family
- Batch D：independent family

## 6. 回滚

如需关闭本批次新骨架：

- 保持 `strategy_family_*_enabled = false`
- coordinator 将继续只用旧 family 参与实际选择与执行
- 新 family 只是不再出现在 snapshot candidates 中

