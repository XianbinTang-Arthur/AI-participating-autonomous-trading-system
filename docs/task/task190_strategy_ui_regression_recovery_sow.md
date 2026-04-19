# Task 190 - 策略页 UI 回归恢复与 dataclass 收集修复

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。


## Business objectives and boundaries

- 修复 `aats/bootstrap/config.py` 的 dataclass 字段顺序错误，恢复 `pytest` 基本收集能力。
- 修复补跑回归时暴露出的同类 dataclass 收集错误，范围只限阻断本次回归的 `independent/replay.py`。
- 将策略页 UI 相关集成测试对齐到当前产品边界：
  - 策略页隐藏已删除的候选、诊断、配置参考与调度明细。
  - 决策抽屉、首页、回放页、风险页继续保留对应的深度诊断展示。
- 不修改本次策略页 UI 的展示边界，不恢复已明确删除的前端内容。

## Current behavior summary

- `config.py` 中 dataclass 的默认字段位于必填字段之前，导致 Python 3.14 在导入阶段抛出 `TypeError`，`pytest` 无法收集。
- 修完第一处后，`independent/replay.py` 暴露出第二处同类 dataclass 顺序问题，继续阻断集成测试收集。
- 策略页 UI 已经按要求收口，但部分旧集成测试仍断言策略页展示已删除的候选、机会腿、智能套利理由、自适应与迁移异常卡片，造成假回归。

## Module responsibilities and domain model

- `aats/bootstrap/config.py`
  - 定义应用运行时与存储后端 dataclass。
- `aats/services/strategy_engines/independent/replay.py`
  - 定义独立双书回放快照 dataclass。
- `tests/integration/test_dashboard_ui.py`
  - 锁定策略页、抽屉、首页、回放页、风险页之间的信息分层边界。

## Input/output interfaces

- 输入：
  - `pytest` 导入 `ApplicationRuntime`、`StorageBackends`、独立双书 replay 快照类。
  - Node 渲染 `renderStrategyView` / `buildDecisionDrawer` / `renderHomeView` / `renderReplayView` / `renderRiskView`。
- 输出：
  - `pytest` 可正常收集并执行。
  - 策略页测试断言“隐藏旧细节”。
  - 其他工作区测试继续断言“保留深度信息”。

## Database schema / tables / indexes / constraints

- 无数据库变更。

## Transactions, Consistency, Concurrency

- 仅涉及 dataclass 定义与前端渲染测试，无事务语义变化。

## Authorization, Authentication, Data Security

- 不新增权限点，不修改认证授权流程。

## Error Handling and Idempotency

- dataclass 定义在导入期应保持稳定，避免因为字段顺序错误导致整个应用或测试入口不可用。
- 测试应稳定表达当前 UI 分层，不再把已删除区域误判成回归。

## State Transition and Lifecycle

- 无状态机逻辑变更，只修正回放快照 dataclass 的字段声明顺序。

## Caching and Performance

- 无运行时性能回退；测试新增的辅助方法仅在集成测试内使用。

## Logging, Monitoring, Auditing

- 不新增日志与审计事件。

## Testing Strategy

- 运行 `ruff check .`。
- 运行 `pytest tests/unit -q`。
- 运行 `pytest tests/integration/test_dashboard_ui.py -q`。
- 运行 `pytest tests/integration/test_strategy_runtime_integration.py -q`。

## Migration, Rollback, Compatibility

- 无 migration。
- 若需回滚，只需撤回 dataclass 字段顺序调整与测试契约调整。

## Configuration and Environment Isolation

- 沿用项目现有 `.venv\\Scripts\\python.exe`。
- 不新增环境变量。

## Code Organization and Dependencies

- 仅修改已有运行时 dataclass、已有集成测试与文档。
- 不引入第三方依赖。

## Documentation and Operations Manual

- 本文档记录这次回归恢复的边界、验证范围与验收条件。

## Deployment and Acceptance Criteria

- `pytest` 不再被 dataclass 顺序错误阻断收集。
- 策略页 UI 集成测试通过，并明确锁定“策略页隐藏深度细节”的当前行为。
- 决策抽屉、首页、回放页、风险页相关正向展示断言保持通过。
- `ruff check .` 与目标测试通过。
