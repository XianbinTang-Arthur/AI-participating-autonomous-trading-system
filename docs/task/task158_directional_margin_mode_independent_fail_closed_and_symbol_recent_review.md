## Task 158 - Directional Margin Mode, Independent Fail-Closed, and Symbol-Scoped Recent Review

### Business objectives and boundaries
- 修复 directional 在 derivatives 主链里对 `settings.margin_mode` 的硬编码，改为使用本轮 resolved runtime margin mode。
- 修复 independent 在 expectancy 计算异常时仍可继续新开仓/加仓的问题，改为 fail-closed。
- 修复 coordinator 拉取 recent market history 时的全 topic recent 后过滤采样偏差，改为按 symbol 定向 recent。
- 收紧 protective / opportunistic 的 parent exposure contract，使其优先使用 directional primary leg contract，而不是只看净 target qty。
- 不改变 public API 语义，不扩大为新的 family 重构。

### Module responsibilities and domain model
- `target_position.py` 负责 directional base target、cost estimate、primary leg metadata。
- `independent_family.py` 负责独立双书的 expectancy、entry/scale-in gating、book action。
- `coordinator.py` 负责 family market history request 和 recent snapshot 分发。
- `protective_family.py` / `opportunistic_family.py` 负责 overlay family 的 parent exposure resolution。

### Input/output interfaces
- 保持现有 `TargetPositionEngine.build(...)`、family evaluator 和 coordinator 接口不变。
- 仅新增 event store 的 `(topic, key)` 定向 recent 能力。

### Database schema / tables / indexes / constraints
- 本轮不改数据库 schema。

### Transactions, Consistency, Concurrency
- recent snapshot 读取从 topic-level recent 改为 symbol-scoped recent，避免高频 symbol 挤占低频 symbol 窗口。

### Authorization, Authentication, Data Security
- 本轮不改鉴权或安全模型。

### Error Handling and Idempotency
- independent expectancy 解析失败时，对新开仓/加仓 fail-closed，并显式记录 blocked reason。

### State Transition and Lifecycle
- directional `margin_mode` 记录、primary leg metadata、cost estimate 统一使用 runtime resolved margin mode。
- overlay parent exposure 优先来自 directional primary leg contract，fallback 才使用 target qty / inventory。

### Caching and Performance
- symbol-scoped recent 可能引入多次查询，但可避免错误采样；当前优先正确性。

### Logging, Monitoring, Auditing
- independent blocked reason 新增 `independent_{leg}_book_expectancy_resolution_failed`，便于 operator/audit 追踪。

### Testing Strategy
- unit:
  - directional margin mode resolved from runtime context
  - independent expectancy failure blocks new risk
  - overlay parent exposure prefers directional primary leg contract
  - recent market snapshots fetch per symbol independently
- integration:
  - independent mainline chain
  - family runtime history consumers
  - protective/opportunistic cutover chain

### Migration, Rollback, Compatibility
- event store 新增方法为向后兼容扩展。
- 老调用仍可继续使用 `recent_by_topic(...)`。

### Configuration and Environment Isolation
- 不新增配置项。

### Code Organization and Dependencies
- 仅修改现有 decision engine、strategy engines、event store 和对应测试。

### Documentation and Operations Manual
- 本文档即本轮修复范围说明。

### Deployment and Acceptance Criteria
- lint 通过
- 相关 unit tests 通过
- 最窄 integration tests 通过
- directional / independent / coordinator / overlay 的 4 个问题都有对应回归
