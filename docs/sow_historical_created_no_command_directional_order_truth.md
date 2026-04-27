# Historical CREATED/no-command Directional Order Truth SOW

## Business objectives and boundaries

固化一个只读运行态事实面，用于判断 OKX `BTC-USDT-SWAP` directional 历史遗留的 `CREATED`/no-command 订单是否仍然存在。目标是让 PM Loop 在没有当前待恢复订单时能够把队首恢复任务判定为过期，而不是反复进入恢复路径。本任务不创建、提交、取消、恢复或终结任何订单。

## Module responsibilities and domain model

- `scripts/runtime_truth_report.py`: 在既有 gateway 容器环境内读取数据库聚合事实，输出无敏感信息的 runtime truth。
- `execution_orders`: 当前命令流订单事实源，用于识别 `CREATED`/`SUBMITTING`、`venue_order_id is null` 且无 `execution_commands.command_type='submit'` 的 directional 订单。
- `order_states`: 旧订单状态事实源，用于交叉识别 legacy `CREATED`/`SUBMITTING`、`exchange_order_id is null` 且无 submit command 覆盖的订单状态。

## Input/output interfaces

输入：运行中 gateway 容器隐式环境、live 数据库、现有 runtime truth report 参数。

输出：

- `created_no_command_directional_order_truth.status`
- `created_no_command_directional_order_truth.root_cause`
- `created_no_command_directional_order_truth.coverage`
- `runtime.live_runtime_facts.created_no_command_directional_order_*`

## Database schema / tables / indexes / constraints

不修改 schema。只读访问：

- `execution_orders`
- `execution_commands`
- `order_states`

查询条件固定为 `symbol='BTC-USDT-SWAP'`、`strategy_family='directional'`、pre-submit 状态且无交易所订单号。

## Transactions, consistency, concurrency

不启用写事务。报告是时间点聚合快照，用于 operator truth 和 automation state 判断；不能替代执行层恢复事务。

## Authorization, authentication, data security

不读取或打印连接串、凭证、token、API key。数据库连接由现有容器环境提供，输出仅包含聚合计数和非敏感订单标识。

## Error handling and idempotency

数据库探针失败时返回 `missing_database_truth`。缺少该探针时返回 `missing_created_no_command_directional_order_probe`。只读报告可重复运行。

## State transition and lifecycle

不触发订单状态变更。状态分类：

- `active_created_no_command_directional_order`: 最近 1 小时存在当前 pre-submit/no-command directional 订单。
- `historical_created_no_command_directional_order_still_present`: 仍存在历史 pre-submit/no-command directional 订单。
- `verified_no_created_no_command_directional_orders`: 当前无可恢复/可终结的 matching 订单，历史任务可判定过期。

## Caching and performance

聚合查询限定 symbol、strategy family 和状态集合，并只返回最多 5 条样本行。该探针随 runtime truth report 按需运行，不引入常驻缓存。

## Logging, monitoring, auditing

runtime truth JSON 是审计输出。自动化状态应引用该事实面，而不是历史叙述。

## Testing strategy

新增单元测试覆盖：

- DB probe 包含 created/no-command root cause 和 truth key。
- 缺口为 0 时分类为 verified absence。
- 最近缺口存在时分类为 active gap。
- live runtime facts 投影暴露该事实面。

## Migration, rollback, compatibility

无 migration。回滚方式是撤销本报告和测试改动；不会影响 live order behavior。

## Configuration and environment isolation

无新增配置。固定沿用 `AATS_EXECUTION_SCIENCE_SYMBOL`，默认 `BTC-USDT-SWAP`。

## Code organization and dependencies

仅修改 `scripts/runtime_truth_report.py` 与对应单元测试，不新增依赖。

## Documentation and operations manual

Operator 若看到 `verified_no_created_no_command_directional_orders`，应停止把历史 CREATED/no-command directional order 当作当前恢复阻断；若状态转为 active/historical present，再进入受控恢复或终结流程。

## Deployment and acceptance criteria

Acceptance criteria:

- runtime truth report 新增 `created_no_command_directional_order_truth`。
- live runtime facts 暴露对应状态和计数。
- 聚焦测试、ruff、完整单元测试通过。
- 部署后 smoke report 显示无当前 matching 订单，且部署 head 与本地 head 一致。
