# Task111：全仓库 lint 清理

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。


## Business objectives and boundaries

- 目标：清理当前 `ruff check .` 报出的全仓库 lint 问题，让仓库重新达到可接受的静态检查状态。
- 边界：仅修复当前 lint 问题，不做业务逻辑重构、不扩大战略或执行行为变更。

## Module responsibilities and domain model

- 业务模块中的修复仅限：
  - 未使用导入/变量删除
  - 局部变量改名，避免遮蔽导入
- 脚本模块中的修复仅限：
  - 调整导入位置或改成惰性导入，满足 `E402`
  - 保持现有 CLI 参数和运行入口不变

## Input/output interfaces

- 不改变任何公共 API。
- 不改变脚本参数、返回码和输出结构。

## Database schema / tables / indexes / constraints

- 本次不涉及数据库 schema 变更。

## Transactions, Consistency, Concurrency

- 本次不引入新的事务或并发行为。
- 脚本导入调整不应改变运行时状态顺序。

## Authorization, Authentication, Data Security

- 不新增密钥、凭据或数据库连接信息。
- 不改变认证与授权逻辑。

## Error Handling and Idempotency

- 保持现有错误处理分支。
- 命令脚本仍按原方式抛出 `SystemExit` 或打印结果。

## State Transition and Lifecycle

- 不改变任何状态机。
- 仅确保 lint 清理不会影响启动、回放、种子脚本的生命周期顺序。

## Caching and Performance

- 本次不调整缓存与性能策略。
- 惰性导入只发生在脚本入口，性能影响可以忽略。

## Logging, Monitoring, Auditing

- 不改变日志事件和审计模型。

## Testing Strategy

- 先跑全仓库 `ruff check .` 收集错误。
- 修复后重跑全仓库 `ruff check .`。
- 跑受影响的单测。
- 跑最窄的集成测试验证脚本/运行链未受影响。

## Migration, Rollback, Compatibility

- 无迁移步骤。
- 回滚方式为回退本次 lint 清理提交。

## Configuration and Environment Isolation

- 不改 profile、dotenv 或运行模式配置。
- 脚本仍使用各自原有的环境加载方式。

## Code Organization and Dependencies

- 保持现有目录结构。
- 脚本中的仓库内模块导入改为惰性导入，避免 `E402`。

## Documentation and Operations Manual

- 本文档作为本次 lint 清理的范围说明。

## Deployment and Acceptance Criteria

- `ruff check .` 通过。
- 相关单测通过。
- 最窄集成测试通过或明确说明环境性 skip。
