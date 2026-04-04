## Business objectives and boundaries

- 一次性补齐 `independent` 任务书剩余主线工作：
  - adaptive 专门 operator/postmortem 摘要
  - README 11.3 scenario tests
  - README 11.4 dedicated replay/recovery integration tests
- 同时补齐 future target 数据模型：
  - `IndependentBookStateSnapshot`
  - `IndependentDecisionSnapshot`
  - `IndependentRecoverySnapshot`
- 同时以默认关闭方式落受控语义升级：
  - adaptive rollout
  - health kernel enforcement
  - sizing / long-short asymmetry / size-down
- 不改 public API 路径名；只做加性字段、加性摘要和默认关闭配置。

## Module responsibilities and domain model

- `aats/services/strategy_engines/independent/`
  - 继续作为 `independent` 领域主实现
  - 增强 state / decision / recovery snapshots
  - 增强 adaptive summary
- `aats/services/strategy_engines/families/independent_family.py`
  - 保持 adapter 入口
  - 继续向外暴露兼容 candidate/runtime 数据
- `aats/services/operator/query_service.py`
  - 增强 operator/postmortem summary
- `aats/api/static/modules/views/*`
  - 增强 adaptive postmortem 展示
- `tests/scenario/`
  - 新增任务书要求的场景测试
- `tests/integration/`
  - 新增 replay/recovery/bundle_recovery 专门集成测试

## Input/output interfaces

- 保持 `IndependentFamilyEngine.evaluate(...)` 外部调用接口不变
- 保持 `StrategyCandidate` / `StrategyBookRuntimeState` / `StrategyBookExpectancyEntry` 兼容
- 通过 additive 字段扩展：
  - state snapshot
  - decision snapshot
  - recovery snapshot
  - adaptive operator/postmortem summary

## Database schema / tables / indexes / constraints

- 本次不新增数据库表
- 仅使用现有 runtime / recovery / replay payload 和事件链路

## Transactions, Consistency, Concurrency

- 不引入新的跨仓储事务
- 语义升级默认关闭，避免影响现有 live 一致性

## Authorization, Authentication, Data Security

- 不改鉴权和权限边界

## Error Handling and Idempotency

- adaptive/operator 摘要缺字段时继续兼容旧 payload
- future snapshot 缺历史字段时允许部分回填，不抛新异常

## State Transition and Lifecycle

- 补齐 `IndependentBookStateSnapshot` 生命周期字段
- replay/recovery 摘要继续围绕 state transition / decision / threshold / health / execution posture
- 语义升级开关默认关闭，避免直接改变现有状态机行为

## Caching and Performance

- 复用现有 operator/runtime 查询缓存
- 避免新增重型历史扫描；优先复用现有 payload

## Logging, Monitoring, Auditing

- adaptive postmortem 进入 operator/replay 摘要
- decision/recovery snapshot 字段增强后继续随现有审计链路持久化

## Testing Strategy

- 新增 README 11.3 的 5 条 scenario tests
- 新增 README 11.4 的 3 条 dedicated integration tests
- 更新必要 unit tests 覆盖 snapshot 字段和 rollout 开关
- 运行 lint、相关 unit、最窄 integration

## Migration, Rollback, Compatibility

- 全部采取 additive 迁移
- 新配置默认关闭，回滚只需关闭开关或忽略新字段
- 不删除旧字段和兼容 wrapper

## Configuration and Environment Isolation

- 新增独立开关：
  - adaptive rollout
  - health enforcement
  - sizing asymmetry / size-down
- 默认值保持保守，live profile 不被无声改变

## Code Organization and Dependencies

- 优先在 `independent/` 子域内收敛逻辑
- 避免把新语义散回 `families/independent_family.py`

## Documentation and Operations Manual

- 本文档作为本轮收口 SOW
- 代码改动完成后补充交付文档说明新增开关、摘要和测试

## Deployment and Acceptance Criteria

- lint 通过
- 相关 unit tests 通过
- README 11.3 和 11.4 对应测试文件存在并通过
- operator/runtime/replay 能看到 dedicated adaptive/postmortem summary
- future target snapshots 字段达到 README 建议字段级别或明确兼容回填
