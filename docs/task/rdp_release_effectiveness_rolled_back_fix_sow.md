# RDP Release Effectiveness `rolled_back` 语义修复 SOW

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。


## Business objectives and boundaries

- 目标：修复 RDP 在引入 release `rolled_back` 终态后，指标链仍将其判为 `unknown` 的问题。
- 边界：只修复 release effectiveness 及其直接下游语义，不改动审批、发布、观察、回滚主流程接口。

## Module responsibilities and domain model

- `release_registry`：记录 release 生命周期状态。
- `active_parameter_apply`：执行 apply / rollback，并更新 release 终态。
- `release_effectiveness`：把 release 结果归类为 `effective / mixed / ineffective / rollback_triggered / insufficient_evidence`。
- `periodic_review` / `backlog_builder`：消费 effectiveness 结论，生成运营复盘与改进任务。

## Input/output interfaces

- 输入：
  - `artifacts/production_workflow/parameter_release_history.json` 或 DB 中的 release history
  - observation / rollback recommendation 结果
- 输出：
  - `release_effectiveness_registry`
  - 后续 periodic review / improvement backlog 的统计与建议

## Database schema / tables / indexes / constraints

- 本次不新增 schema。
- 继续复用现有 release history / effectiveness registry 的 DB-first 读写链。

## Transactions, Consistency, Concurrency

- 本次只调整纯读判定逻辑，不新增跨表事务。
- 要求保持文件 fallback 与 DB-first 结果语义一致。

## Authorization, Authentication, Data Security

- 不涉及新权限模型。
- 不读取、不暴露任何凭证。

## Error Handling and Idempotency

- 对未知 observation status 继续保守返回 `unknown`。
- 对 `rolled_back` 改为显式负面结果，且 detail 中保留 `rollback` 关键词，保证既有派生规则兼容。

## State Transition and Lifecycle

- 新增/确认语义：
  - `rolled_back` = 已经发生回滚，属于运营负面终态
  - 不应再被视为 “insufficient” 或 “unknown”

## Caching and Performance

- 无新增缓存。
- 仅调整单次 evaluation 的状态分支判断，性能影响可忽略。

## Logging, Monitoring, Auditing

- 不新增日志。
- 通过测试确保 review/backlog 对回滚事件计数正确。

## Testing Strategy

- 新增单元测试，覆盖：
  - `observation_status=rolled_back` 时 operations 维度应为 `negative`
  - effectiveness 结论应为 `rollback_triggered`
  - detail 应保留 rollback 语义，兼容下游派生逻辑

## Migration, Rollback, Compatibility

- 无 schema 迁移。
- 向后兼容既有 `completed / observing / rollback_recommended` 语义。

## Configuration and Environment Isolation

- 不新增配置项。
- Windows 单测 + WSL2 最窄集成测试验证。

## Code Organization and Dependencies

- 仅修改：
  - `aats/data_platform/metrics/release_effectiveness.py`
  - 对应 tests

## Documentation and Operations Manual

- 本 SOW 即本轮修复说明。

## Deployment and Acceptance Criteria

- `rolled_back` release 不再被归类为 `unknown`
- periodic review / backlog 能通过既有 `rollback_triggered` 结论正确识别失败 release
- 通过 lint、全量单测、最窄 WSL2 集成测试
