# RDP 手动完整流程触发契约修复 SOW

## Business objectives and boundaries
- 目标：修复 RDP 工作台“运行完整 RDP”按钮显示已排队，但实际 `full_pipeline` 未执行的问题。
- 目标：保持按钮语义真实——操作员点击“运行完整 RDP”时，应运行完整 RDP pipeline。
- 边界：只开放 `research_cycle.full_pipeline` 的人工触发；`research_cycle.schedule.enabled` 继续为 false，不恢复 weekly 自动调度。
- 边界：不开放 `release_cycle` queue boundary freeze，不改 live trading 主链路、不改参数发布语义。

## Module responsibilities and domain model
- `configs/rdp_workflows/research_cycle.json` 仍是 research workflow 的配置真源。
- `aats.data_platform.operations.workflow_dispatcher` 负责解释 workflow 配置与手动触发可用性。
- `aats.api.rdp_routes` 负责在写入 `governance.rdp_task_queue` 前拒绝配置不可用的手动触发。
- `aats.api.rdp_control_summary` 负责把 workflow 可用性暴露给 RDP 工作台按钮。

## Input/output interfaces
- 输入：`POST /rdp/tasks/trigger` 的 `workflow` 字段。
- 输出：当前配置下 `research_cycle` 可手动入队，daemon 会执行 `full_pipeline`。
- 输出：若未来配置再次禁用 `full_pipeline`，接口返回 `ok=false`、`blocked_by_config=true` 和中文原因，避免假成功。
- UI 输出：当前配置下“运行完整 RDP”按钮可用。

## Database schema / tables / indexes / constraints
- 不改 schema。
- 不改 `governance.rdp_task_queue` 索引或状态字段。
- 当前配置下完整 RDP 手动触发会写入 pending 任务。
- 未来若配置禁用，手动触发不会写入 pending 任务。

## Transactions, consistency, concurrency
- 可用性检查发生在入队事务之前。
- 入队并发控制仍由 `db_create_task_if_idle` 和 partial unique index 负责。
- 不改变 daemon claim、running、done/failed 生命周期。

## Authorization, authentication, data security
- 保持 `require_write_access` 不变。
- 不新增凭证读取或输出。
- 禁用原因只包含配置状态，不包含路径、DSN 或内部异常堆栈。

## Error handling and idempotency
- 配置禁用时返回业务失败，不抛 500。
- 配置缺失或不可读按 fail-closed 处理，避免 UI 提交一个后续必然不执行的任务。
- 重复点击仍由已有 pending/running 检查处理。

## State transition and lifecycle
- 修复前：`research_cycle` 可入队，daemon 执行后 `refresh_recent_data` 成功、`full_pipeline` skipped，任务仍标记 done。
- 修复后：当前 `full_pipeline` 人工触发可执行；若配置再次禁用，手动触发不会进入 pending/running 生命周期。
- 已存在的历史任务不迁移、不回写。

## Caching and performance
- 可用性检查只读取本地 JSON workflow 配置，成本低。
- 不新增跨请求缓存，避免配置变更后 UI/API 状态滞后。

## Logging, monitoring, auditing
- 本次不新增日志格式。
- 修复后减少“done 但核心任务 skipped”的误导性审计记录。
- 现有 daemon heartbeat 和 task queue 监控继续保留。

## Testing strategy
- 单元测试覆盖 workflow 手动触发可用性。
- 单元测试覆盖当前 `research_cycle.full_pipeline` 为人工可触发。
- API 集成测试覆盖 `research_cycle.full_pipeline=false` 时拒绝入队。
- UI/workbench payload 测试覆盖当前“运行完整 RDP”按钮可用。

## Migration, rollback, compatibility
- 无 DB migration。
- 回滚方式：撤销本次代码改动即可恢复旧的“入队但 full_pipeline skipped”行为。
- 对调度器、daemon、已有 workflow JSON 兼容。

## Configuration and environment isolation
- 继续以 repo 内 `configs/rdp_workflows/*.json` 为配置真源。
- 不读取或打印 `.env.*` 凭证。
- Windows 与 WSL2 行为一致。

## Code organization and dependencies
- 不新增第三方依赖。
- 新逻辑放在 workflow dispatcher 附近，避免 API 和 UI 各自复制配置解释规则。

## Documentation and operations manual
- 本 SOW 记录“按钮名称必须匹配实际会执行的 workflow 内容”的操作契约。
- 当前已恢复 `full_pipeline.enabled=true` 的人工触发语义。
- 若未来再次冻结，应改配置并依赖 UI/API 可用性检查阻止假成功。

## Deployment and acceptance criteria
- `POST /rdp/tasks/trigger` 对当前 `research_cycle` 返回 `ok=true` 并入队。
- `POST /rdp/tasks/trigger` 对配置禁用的 `research_cycle` 返回 `ok=false`。
- RDP 工作台“运行完整 RDP”在当前配置下可点击。
- `data_maintenance` 仍可手动触发。
- 相关单元和集成测试通过。
