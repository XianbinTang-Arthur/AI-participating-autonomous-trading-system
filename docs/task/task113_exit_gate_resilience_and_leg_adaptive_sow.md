# Task113 Exit Gate Resilience And Leg Adaptive SOW

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。


## Business Objectives And Boundaries

- 修复 OKX 提交前 gate 对平仓、减仓、止损类订单的错误硬阻断，保证系统在需要降风险时仍具备退出能力。
- 保持对开仓、加仓类订单的现有保守门禁，不扩大真实提交通道的风险面。
- 统一腿单风控路径与主风控路径的 adaptive multiplier 来源，避免同类风险在不同执行路径下表现不一致。
- 仅修复 `okx_adapter`、`okx_rest`、`risk`、`README` 以及对应测试，不做无关重构。

## Module Responsibilities And Domain Model

- `aats/services/execution_engine/okx_adapter.py`
  - 负责本地下单前 gate、OKX 语义映射、提交流程与阻断原因输出。
  - 本次新增“新增风险订单”和“降风险订单”的区分逻辑。
- `aats/services/execution_engine/okx_rest.py`
  - 负责 OKX HTTP 请求、鉴权、响应解析与错误抛出。
  - 本次补充查询链路的短重试、退避和错误分类。
- `aats/services/governance_engine/risk.py`
  - 负责主路径、腿单路径、腿单 bundle 路径的风险评估。
  - 本次统一 adaptive risk budget / execution aggressiveness 的输入来源。
- `README.md`
  - 负责声明仓库支持边界与托管 profile 的真实能力。

## Input Output Interfaces

- 输入：
  - `OrderIntent` / `LegOrderIntent`
  - OKX REST 查询响应与错误
  - 风控 runtime guard / trial guard / reconciliation 状态
- 输出：
  - 更准确的 `OrderState.execution_error`
  - 更稳健的 OKX 请求异常分类
  - 包含 adaptive multiplier 的 `RiskDecision`
  - 与实际 live profile 能力一致的 README 边界说明

## Database Schema / Tables / Indexes / Constraints

- 本任务不修改数据库 schema、索引或迁移。

## Transactions, Consistency, Concurrency

- 不引入新的事务边界。
- 保持本地 gate 与外部查询失败之间的 fail-closed / fail-open 分流：
  - 新增风险订单：保守阻断。
  - 降风险订单：允许降级放行。

## Authorization, Authentication, Data Security

- 不改变 OKX 鉴权协议。
- 不新增凭证读取路径。
- 文档修正时避免误导操作者对真实资金 profile 的理解。

## Error Handling And Idempotency

- `okx_rest.request()` 对 GET 查询增加短重试与退避。
- transport timeout / network / 429 / 5xx 归类为可重试查询错误。
- 业务拒绝、参数错误、权限错误保持不可重试。
- 写请求默认不自动重试，避免重复下单风险。
- `max-size` 预检查失败时，仅对新增风险订单保守阻断。

## State Transition And Lifecycle

- `submit()` 仍维持 `payload build -> submission gate -> semantic gate -> max-size gate -> submit` 的顺序。
- 变化点仅在 gate 语义：
  - reduce / close / exit 类订单不再被新增风险门禁抢先拦截。
  - 查询链路瞬时失败不再直接掐断退出路径。

## Caching And Performance

- GET 查询重试次数保持很小，仅用于恢复瞬时抖动。
- 不改变持久化缓存或快照结构。

## Logging, Monitoring, Auditing

- 保留现有阻断 reason 输出。
- 新错误分类应使 `execution_error` 更容易区分“业务拒绝”和“外部临时异常”。

## Testing Strategy

- 单测覆盖：
  - reduce/close 订单绕过 `max_open_orders` / `max_notional_per_symbol`
  - `max-size` 预检查失败时的新增风险阻断与降风险放行
  - OKX GET 查询重试与 POST 不自动重试
  - leg / leg bundle 路径继承 adaptive multiplier
- 集成测试覆盖：
  - live submit 路径在 `max-size` 预检查异常时仍允许 close leg 提交

## Migration, Rollback, Compatibility

- 无 schema migration。
- 回滚方式为恢复上述文件改动。
- 保持现有 public model 字段兼容；仅增强内部行为与文档说明。

## Configuration And Environment Isolation

- 沿用现有 `.venv\Scripts\python.exe`。
- 不改动 `.env.*` 文件结构。
- 文档明确区分模拟盘、guarded live 实盘与 autonomous live。

## Code Organization And Dependencies

- 不新增第三方依赖。
- 在现有模块内补充最小 helper 与测试。

## Documentation And Operations Manual

- README 需明确：
  - 不支持的是“无保护/自治型真实资金自动交易”
  - 现有 `spot_live` / `derivatives_live` 为 guarded live 实盘 profile
  - 使用前提是严格风控、恢复、审计和小资金验证

## Deployment And Acceptance Criteria

- reduce/close/exit 订单在名义金额或挂单数量上限已触发时仍可通过本地 gate。
- `get_max_order_quantity()` 查询异常不会阻断退出单。
- leg 风控返回的 multiplier 与主路径来自同一 adaptive control 源。
- README 与 `managed_profiles.py` 的 live 能力表述一致。
- lint、相关单测和最窄集成测试通过。
