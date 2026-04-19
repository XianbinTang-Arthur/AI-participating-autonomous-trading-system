# Task 135 - Strategy Families Batch I Delivery

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。


## 范围

Batch I 的目标是完成 family refactor 的测试与清理收口：

- 把 `protective / opportunistic / independent` 的旧 `target_position` overlay 单测迁到 family helper 层
- 让 `tests/unit/test_target_position_engine.py` 只保留：
  - directional 本体行为
  - family cutover 旁路断言
- 清理 `target_position.py` 相关的旧入口测试残留
- 最后做一轮更完整的 family refactor 回归验证

## 本轮改动

### 1. 新增 family helper 单测

新增共享测试支持文件：

- [tests/support/strategy_family.py](/D:/文件/project/AIParticipatingAutonomousTradingSystem/tests/support/strategy_family.py)

新增 3 组 family helper 单测：

- [tests/unit/test_protective_family.py](/D:/文件/project/AIParticipatingAutonomousTradingSystem/tests/unit/test_protective_family.py)
- [tests/unit/test_opportunistic_family.py](/D:/文件/project/AIParticipatingAutonomousTradingSystem/tests/unit/test_opportunistic_family.py)
- [tests/unit/test_independent_family.py](/D:/文件/project/AIParticipatingAutonomousTradingSystem/tests/unit/test_independent_family.py)

覆盖的旧场景包括：

- protective
  - 开保护腿
  - `min_hold`
  - dedicated enabled switch
  - rebalance cooldown
  - `context.as_of_ts`
- opportunistic
  - 开机会腿
  - fee drag guard
  - `min_hold`
  - rebalance cooldown
  - reversal handover 时无旧主腿库存
  - rollout gate
- independent
  - 长短腿独立 cooldown
  - close hysteresis
  - expected net edge gating
  - weak-edge passive-first 偏好
  - execution cost gating
  - trial guard
  - rollout gate
  - disabled fallback

### 2. 精简 `test_target_position_engine.py`

[tests/unit/test_target_position_engine.py](/D:/文件/project/AIParticipatingAutonomousTradingSystem/tests/unit/test_target_position_engine.py) 已删除旧的 overlay unit tests，文件现在只保留：

- directional / net / hedge 基础行为
- 3 条 family cutover 旁路断言

这意味着 `target_position` 单测不再继续依赖已经移出的 legacy overlay helper 语义。

## 当前状态

完成 Batch I 后，family refactor 的测试职责已清晰分层：

- `target_position.py`
  - directional 目标与 hedge primary legs
  - family cutover bypass 验证
- family helper tests
  - `protective / opportunistic / independent` 评估逻辑
- coordinator / mainline integration
  - 真实 family cutover 与最终 applied target

## 验证

本轮回归覆盖：

- 全仓库 lint
- `target_position` 全量 unit test
- 新增 family helper unit test
- `strategy_coordinator` unit test
- `mainline_chain` 的 family cutover integration
- `strategy_runtime_integration` 的 family snapshot/runtime integration

`verify.sh` 仍无法运行，原因是当前环境缺少可访问的 `bash.exe`，不是本轮代码逻辑失败。
