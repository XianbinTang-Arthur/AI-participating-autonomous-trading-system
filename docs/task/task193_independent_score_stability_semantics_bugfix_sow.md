## Task 193: Independent Score-Stability 语义修复

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。


### Business objectives and boundaries
- 修复 `independent` 的 `score_stability` 语义错误，避免把强动量行情中的“分数增强”误判为“不稳定”。
- 范围限定在 `independent` 的打分稳定性计算、gate 判定、诊断输出与相关测试。
- 本轮不直接修改 live profile 阈值，不做 allocator / execution policy / operator UI 重构。

### Module responsibilities and domain model
- `aats/services/strategy_engines/independent/scoring.py`
  - 保留现有 score 计算流程，修正稳定性指标的定义。
- `aats/bootstrap/settings.py`
  - 新增 additive 配置 `strategy_hedge_independent_min_score_drawdown_bps`
  - 未配置时继续 fallback 到旧字段 `strategy_hedge_independent_min_score_stability_bps`
- `aats/services/strategy_engines/independent/models.py`
  - 在 `ScoreStabilityMetrics` 中新增更精确的诊断字段，兼容旧字段。
- `aats/services/strategy_engines/independent/gates.py`
  - entry quality gate 继续依赖 `stable` 布尔，不直接耦合旧字段名。
- `aats/services/strategy_engines/families/independent_family.py`
  - 输出 additive 诊断指标，方便 query/replay/operator 后续消费。
- `aats/services/strategy_engines/independent/replay.py`
  - replay / recovery 决策快照补充新稳定性字段。

### Input/output interfaces
- 输入
  - `recent_score_history`
  - `score`
  - `entry_threshold`
  - `strategy_hedge_independent_min_score_stability_bps`
- 输出
- 继续保留 `max_drawdown_bps`
- 新增 `upward_excursion_bps`
- 新增 `downward_drawdown_bps`
- `stable` 改为由真正的 `downward_drawdown_bps` 决定
- drawdown 阈值优先读取 `strategy_hedge_independent_min_score_drawdown_bps`
- 若新字段未配置，则兼容回退到 `strategy_hedge_independent_min_score_stability_bps`

### Database schema / tables / indexes / constraints
- 无数据库 schema 变更。
- 变更只发生在内存计算和已有 payload 的 additive 诊断字段。

### Transactions, Consistency, Concurrency
- 无新事务。
- 同一输入应产生确定性的稳定性指标和 gate 结果。

### Authorization, Authentication, Data Security
- 无认证、授权或数据安全变更。

### Error Handling and Idempotency
- 稳定性计算保持纯函数。
- 旧字段保留，避免下游读取报错。

### State Transition and Lifecycle
- 不修改状态机。
- 本轮目标是让 entry quality gate 对强动量增强不再误伤，同时仍能拦截“已塌掉”的信号。

### Caching and Performance
- 只增加轻量数值计算，不增加 IO。

### Logging, Monitoring, Auditing
- additive 输出新的稳定性诊断字段，便于后续排查。
- 不移除旧诊断字段，避免历史对比失真。

### Testing Strategy
- 单元测试覆盖：
  - 强动量增强场景不再因“current - recent min”被误挡
  - 真正回撤场景仍会被判定 unstable
  - gate / engine / family diagnostics 与新语义保持一致
- 运行最窄 integration，确保 `independent` 主路径未回归。

### Migration, Rollback, Compatibility
- 无 migration。
- 通过 additive 字段兼容旧消费面。
- 回滚是代码级回滚。

### Configuration and Environment Isolation
- 不修改 `.env` 或 live 配置。
- 继续使用 `.venv\\Scripts\\python.exe` 执行验证。

### Code Organization and Dependencies
- 复用现有 `ScoreStabilityMetrics` 和 `replay` 输出结构，不引入并行模型。
- 不在本轮改变 `strategy_hedge_independent_min_score_stability_bps` 配置值。

### Documentation and Operations Manual
- 本文档记录语义修复边界。
- live 阈值 replay / 调参属于下一小步，不在本任务内。

### Deployment and Acceptance Criteria
- 强动量增强样本在相同阈值下不再被稳定性 gate 误伤。
- 真正的 downward drawdown 仍能触发稳定性阻断。
- 旧字段继续可读，新字段可用于后续观测。
- lint / unit / 最窄 integration 通过。
