# Task 175: Independent Shadow Adaptive Layer

## Business objectives and boundaries
- 将 `independent/adaptive.py` 从静态 settings 快照升级成 shadow-only 动态层。
- 只做诊断/解释增强，不改变当前 `independent` 的真实开平仓 gating 语义。
- replay / recovery 必须能看到新的 adaptive 阈值与资本倍率快照。

## Module responsibilities and domain model
- `independent/adaptive.py`
  - 生成 base thresholds
  - 生成 shadow-only adaptive thresholds
  - 生成 capital multiplier 和 adjustment reason codes
- `independent/engine.py`
  - 将 adaptive snapshot 接入 book decision / replay snapshot
- `independent/diagnostics.py`
  - 将 adaptive snapshot 映射到 runtime state schema
- `replay / recovery`
  - 透传 adaptive threshold snapshot，作为 postmortem / recovery 解释的一部分

## Input/output interfaces
- 输入
  - `BaselineAssessment`
  - `AIMarketAssessment`
  - `DecisionContext`
  - `IndependentLegHealthSnapshot`
  - `IndependentBookDecision`
- 输出
  - `IndependentAdaptiveSnapshot`
  - `StrategyAdaptiveThresholdSnapshot`
  - recovery / replay summary 中的 adaptive threshold details

## State transition and lifecycle
- 不改变当前 book state machine。
- adaptive snapshot 只描述“shadow 阈值/资本倍率”，不驱动真实状态切换。

## Error handling and compatibility
- 保持 `entry_threshold / close_threshold / scale_in_threshold` 继续表示静态基线。
- 新增字段全部为 additive：
  - `adaptive_*`
  - multiplier fields
  - `reason_codes`

## Testing strategy
- 新增 `test_independent_adaptive.py`
- 更新 `test_independent_engine.py`
- 更新 `test_independent_replay.py`
- 更新 `test_execution_recovery.py`
- 更新最窄 runtime/recovery integration
