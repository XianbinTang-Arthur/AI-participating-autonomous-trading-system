# Task 143 - Independent Score Stability 下游读侧语义收口
## 业务目标与边界
- 将 `max_drawdown_bps` 从 independent 的下游展示与回放出口中逐步退掉。
- 保留底层 `ScoreStabilityMetrics.max_drawdown_bps` 兼容字段，避免直接破坏内部与历史调用。
- 本轮只处理 family metrics、replay snapshot、以及相关测试；不修改 gate 判定与底层打分计算。

## 模块职责与领域模型
- `aats/services/strategy_engines/families/independent_family.py`
  - 不再向 candidate family metrics 暴露 `*_score_stability_max_drawdown_bps` 与 compat source。
  - 只保留 `*_score_stability_upward_excursion_bps` 与 `*_score_stability_downward_drawdown_bps`。
- `aats/services/strategy_engines/independent/replay.py`
  - replay snapshot 的 `score_stability_metrics` 不再写出 `max_drawdown_bps` 与 compat source。
- `aats/services/strategy_engines/independent/models.py`
  - 继续保留 `ScoreStabilityMetrics.max_drawdown_bps` 兼容字段，供内部与历史对象使用。

## 输入 / 输出接口
- 输入不变。
- 输出变化：
  - family metrics 不再包含：
    - `long_score_stability_max_drawdown_bps`
    - `short_score_stability_max_drawdown_bps`
    - `long_score_stability_max_drawdown_bps_compat_source`
    - `short_score_stability_max_drawdown_bps_compat_source`
  - replay snapshot 的 `score_stability_metrics` 不再包含：
    - `max_drawdown_bps`
    - `max_drawdown_bps_compat_source`

## 兼容性与回滚
- 底层 dataclass 兼容字段保留。
- 只收口下游读侧出口，属于显式展示层变更。
- 回滚为代码级回滚，无 schema / migration。

## 测试策略
- 更新 independent family unit tests，断言新字段仍存在、旧字段不再暴露。
- 新增 replay snapshot unit test，断言只输出 upward/downward。
- 运行最窄 integration，确保 independent 主路径未回归。

## 配置与环境隔离
- 本任务不新增 `.env.*` 配置项。

## 验收标准
- family metrics 与 replay snapshot 不再向下游继续扩散 `max_drawdown_bps`。
- 调参/分析入口仍能从 `upward_excursion_bps / downward_drawdown_bps` 获得所需信息。
- lint、相关 unit tests、最窄 integration tests 通过。
