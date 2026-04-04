## Task 143 - Opportunistic Execution Discipline And Overlay Main-Leg Contract Cleanup

### Business objectives and boundaries
- 为 `opportunistic` family 补齐独立的 execution-cost discipline，避免机会腿继续无条件沿默认激进执行链下发。
- 收敛 `protective / opportunistic` 对 `directional_target` 的分散直接依赖，改成统一解析后的主腿 contract。
- 保持 overlay family 仍然依附 directional 主腿目标的业务边界，不改 allocator / risk / execution 主链。
- 把新配置显式写入 managed profile，并暴露到 runtime / operator / UI。

### Module responsibilities and domain model
- `protective_family.py`
  - 提供共享的 overlay 主腿 contract 解析 helper。
- `opportunistic_family.py`
  - 基于共享主腿 contract评估 overlay，并在 opening / expanding 路径增加 execution-cost discipline。
- `settings.py`
  - 定义 opportunistic 的 safe-net-edge / slippage / execution buffer / max acceptable cost / passive-first 配置。
- `query_service.py` / `strategy-view.js`
  - 暴露并展示 opportunistic 新配置。

### Input/output interfaces
- 输入：
  - `StrategyEvaluationContext.directional_target`
  - `DecisionContext`
  - opportunistic execution-discipline settings
- 输出：
  - `StrategyCandidate.metrics` 中 opportunistic 的 expected gross/cost/net 与 weak-edge 执行偏好
  - `StrategyLegIntent` 的执行偏好字段
  - runtime/operator 配置摘要中的 opportunistic 显式参数

### Database schema / tables / indexes / constraints
- 无数据库 schema 变更。

### Transactions, consistency, concurrency
- 纯内存策略评估与配置变更，无事务或并发模型调整。

### Authorization, authentication, data security
- 不涉及鉴权或凭证处理变更。

### Error handling and idempotency
- 如果 opportunistic 的 execution-cost gate 认为边际不足：
  - `block` 模式下阻止新机会腿开仓/扩仓
  - `report_only` 模式下保留机会腿，但要求 planner 走更保守的 passive-first 偏好
- overlay 主腿 contract 解析失败时，回退到 context / settings 的安全默认值，而不是抛异常中断整轮评估。

### State transition and lifecycle
- `protective / opportunistic` 继续遵守原有 opening / holding / closing / blocked 生命周期。
- opportunistic 仅在 `opening_or_expanding` 路径额外加入 expected-net-edge / expected-cost 校验。

### Caching and performance
- 新增一次 opportunistic 单腿成本估算，属于常量级开销。
- 不新增额外外部 IO。

### Logging, monitoring, auditing
- candidate metrics 增加 opportunistic 的 expected edge / cost / weak-edge 执行偏好，便于 operator 审计。
- UI 配置卡显式展示 opportunistic 的 safe-net-edge / buffer / passive-first 参数。

### Testing strategy
- unit
  - opportunistic 弱边际 report-only 时生成 passive-first 执行偏好
  - opportunistic expected cost 超过上限时阻止 opening
  - managed profiles 显式包含 opportunistic 新参数
- integration
  - runtime payload 暴露 opportunistic 新参数
  - dashboard UI 显示 opportunistic execution-discipline 配置

### Migration, rollback, compatibility
- 向后兼容：
  - 新配置都有默认值
  - 未开启 opportunistic family 的运行线行为保持不变
- rollback：
  - 可独立回退 family 文件、profile 配置与前端配置展示

### Configuration and environment isolation
- 更新：
  - `configs/strategy_profiles/derivatives.yaml`
  - `configs/strategy_profiles/derivatives_live.yaml`
- 不改 `.env.derivatives.live`

### Code organization and dependencies
- 变更范围限制在：
  - `aats/services/strategy_engines/families`
  - `aats/bootstrap`
  - `aats/services/operator`
  - `aats/api/static/modules/views`
  - `configs/strategy_profiles`
  - `tests`
  - `docs`

### Documentation and operations manual
- 本文档记录本次 execution-discipline / overlay contract 收敛的边界与验证要求。

### Deployment and acceptance criteria
- opportunistic opening / expanding 不再缺少 execution-cost discipline。
- `protective / opportunistic` 通过共享主腿 contract 获取 symbol / leverage / margin / target qty，不再散落直接依赖 `directional_target`。
- managed profile 与 runtime/operator/UI 都能看到 opportunistic 新配置。
- lint、unit、最窄 integration 通过。
