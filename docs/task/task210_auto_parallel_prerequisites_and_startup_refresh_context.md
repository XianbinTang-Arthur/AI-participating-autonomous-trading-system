# Task 210 - 自动执行前置条件显式化与 startup refresh 诊断增强

## Business objectives and boundaries

- 目标 1：避免 `runtime_supported` 被误解为“完整自动执行前置条件已满足”。
- 目标 2：统一腿级 note 前缀体系，方便后续按腿排障与 dashboard 解析。
- 目标 3：增强 startup refresh failure 的上下文，让 review item 能直接说明失败发生在哪一步、针对哪个 runtime scope。
- 非目标：
  - 不做数据库字段 rename / migration。
  - 不删除 `automation_state` 顶层兼容字段。
  - 不改 allocator / execution 主行为。

## Module responsibilities and domain model

- `StrategySleeveAutomationDecision.runtime_supported`
  - 保留兼容，继续只表示 candidate state 层是否不是 `incompatible`。
- 新增 `execution_prerequisites_supported`
  - 明确表示自动入链前置条件中的执行兼容层是否已满足。
  - 当前由 `candidate_state_runtime_supported && candidate_execution_compatible` 计算。
- 腿级 note 统一使用：
  - `execution_permission:*`
  - `budget_control:*`
  - `composition:*`

## Input/output interfaces

- 输入：`RawSleeveCandidateInputs`、permission/budget/composer 决策结果、startup refresh exception。
- 输出：
  - `StrategySleeveAutomationDecision.execution_prerequisites_supported`
  - `StrategySleeveIntent.execution_prerequisites_supported`
  - `control_trace.permission.execution_prerequisites_supported`
  - 更结构化的腿级 note
  - 更丰富的 startup refresh failure notes / review items

## Database schema / tables / indexes / constraints

- 不改 SQL 表结构。
- 新字段仅进入 JSON payload，不增加列。
- 历史 `automatic_enabled` 列名保留，继续依赖文档语义解释。

## Transactions, Consistency, Concurrency

- 无事务边界变化。
- JSON payload 扩展保持向后兼容，旧记录仍可反序列化。

## Authorization, Authentication, Data Security

- 无认证授权改动。
- startup refresh 诊断只记录截断后的异常消息和 runtime scope 摘要，不引入敏感凭据。

## Error Handling and Idempotency

- startup refresh 失败仍然 fail-closed。
- 新增 note / review item 字段不影响恢复流程幂等性。

## State Transition and Lifecycle

- 不改自动控制状态机。
- 只增强：
  - 前置条件语义表达
  - 腿级行为说明
  - startup review failure 诊断

## Caching and Performance

- 新增字段为常量时间拼装，无显著性能影响。

## Logging, Monitoring, Auditing

- startup refresh failure 会额外记录：
  - refresh stage
  - runtime scope 摘要
  - 截断后的异常消息
- 这些信息会进入 overlay review items / persisted state snapshot。

## Testing Strategy

- 单测覆盖：
  - automation decision / intent 暴露新的 execution prerequisites 字段
  - 腿级 note 前缀统一
  - startup refresh failure notes 和 review item 诊断增强
- 最窄 integration 覆盖：
  - strategy runtime endpoint 暴露新的 decision 字段

## Migration, Rollback, Compatibility

- 向后兼容：
  - 保留 `runtime_supported`
  - 保留 `automation_state`
  - 新字段为增量补充
- 回滚成本低，只需回退 payload 扩展与 note 规范化。

## Configuration and Environment Isolation

- 无新配置。

## Code Organization and Dependencies

- 仅修改：
  - `auto_parallel.py`
  - `strategy_runtime.py`
  - `sleeve_routing_composer.py`
  - `startup_recovery.py`
  - 相关测试

## Documentation and Operations Manual

- 本文档记录本轮新增语义：
  - `runtime_supported` 是 state 层兼容字段
  - `execution_prerequisites_supported` 是更准确的新诊断口径

## Deployment and Acceptance Criteria

- runtime/operator 不再需要把 `runtime_supported=true` 误解成“完整自动执行前置条件已满足”。
- 腿级 note 能稳定区分 permission / budget / composition。
- startup refresh failure review item 带上 stage / scope / message 摘要。
