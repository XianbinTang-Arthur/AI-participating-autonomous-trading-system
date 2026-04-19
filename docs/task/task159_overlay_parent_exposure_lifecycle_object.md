## Task 159 - Overlay Parent Exposure Lifecycle Object

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。


### Business objectives and boundaries
- 把 `protective / opportunistic` 从“运行时各自读取 directional target 再局部 fallback inventory”升级成共享的 parent exposure lifecycle object。
- 由 coordinator 统一解析 parent exposure，再下发到 family context。
- 保持 allocator / apply / execution 主链不变，不把 overlay family 改造成独立 alpha family。

### Current behavior summary
- 之前 `protective_family.py` 自己定义并解析 `OverlayParentExposureContract`。
- `opportunistic` 复用这套 helper，但主链里仍是 family 自己从 `directional_target` 派生 parent exposure。
- 结果是 parent exposure 还不是 strategy engine input contract 的一等对象。

### Module responsibilities and domain model
- `overlay_parent_exposure.py`
  - 共享定义 `OverlayParentExposureLifecycle`
  - 统一解析 target / inventory / effective signal / lifecycle state
- `coordinator.py`
  - 在 family registry evaluate 前统一生成 `overlay_parent_exposures_by_family`
- `base.py`
  - 把 overlay parent exposure 接入 `StrategyEngineInput` / `StrategyEvaluationContext`
- `protective_family.py` / `opportunistic_family.py`
  - 优先消费 coordinator 已解析好的 lifecycle object
  - 仅在 legacy / unit helper 路径缺对象时才本地 fallback

### Input/output interfaces
- 新增 `StrategyEngineInput.overlay_parent_exposures_by_family`
- 新增 `StrategyEvaluationContext.overlay_parent_exposure`
- 保留旧 helper 名称，兼容现有测试和调用面

### Database schema / tables / indexes / constraints
- 本轮不改数据库 schema。

### Transactions, Consistency, Concurrency
- parent exposure 由 coordinator 单点解析，避免 protective / opportunistic 各自重复派生导致的不一致。

### Authorization, Authentication, Data Security
- 本轮不改鉴权与安全模型。

### Error Handling and Idempotency
- family context 中缺少预计算 parent exposure 时，仍保留本地解析 fallback，避免打断 legacy 测试与单点调用。

### State Transition and Lifecycle
- 新对象显式记录：
  - `target_signal`
  - `current_signal`
  - `effective_signal`
  - `signal_source`
  - `lifecycle_state`
  - `target_active`
  - `inventory_active`
- 这让 residual inventory、target-only、target+inventory 三类阶段都有统一表达。

### Caching and Performance
- coordinator 统一解析一次 parent exposure，减少 overlay family 重复解析。

### Logging, Monitoring, Auditing
- candidate metrics 继续保留并新增：
  - `parent_family`
  - `parent_lifecycle_state`
  - `parent_target_active`
  - `parent_inventory_active`

### Testing Strategy
- unit
  - protective candidate 优先消费 precomputed parent exposure
  - opportunistic candidate 优先消费 precomputed parent exposure
  - coordinator family context 能拿到 overlay parent exposure
- integration
  - protective / opportunistic family cutover 主链继续可运行
  - strategy runtime snapshot 继续暴露真实 overlay family candidate

### Migration, Rollback, Compatibility
- 通过 wrapper 保留旧 helper 入口，兼容现有 unit 测试和单点调用。
- 若需回滚，只需回退 shared module 与 context 接线，不影响数据库。

### Configuration and Environment Isolation
- 本轮不新增配置项。

### Code Organization and Dependencies
- 新增共享模块：`aats/services/strategy_engines/overlay_parent_exposure.py`
- 避免继续由 `opportunistic` 直接依赖 `protective` 的内部实现细节。

### Documentation and Operations Manual
- 本文档即本轮结构升级说明。

### Deployment and Acceptance Criteria
- lint 通过
- 相关 unit tests 通过
- 最窄 integration tests 通过
- protective / opportunistic 主链优先消费 coordinator 下发的 parent exposure lifecycle object
