# Task 183 - BTC Live Replay 成本熔断校准测试

## Business objectives and boundaries

- 将 `2026-04-01` 美国东部时间 `13:23`、`13:26`、`13:28`、`14:09` 这 4 个 BTC live short 候选窗口固化为本地可重复运行的校准测试。
- 用真实窗口样本验证 `depth_penalty / size_penalty / confidence_penalty` 不会再把这些本应放行的 short 机会错误熔断。
- 不让测试依赖 live 数据库；一旦样本提取完成，测试只使用本地固化夹具。
- 本轮不重构成本模型，只在必要时微调动态熔断权重。

## Module responsibilities and domain model

- `tests/unit/test_independent_live_replay_calibration.py`
  - 保存真实窗口样本夹具，并回放到 `TradeCostService + anomaly_cost_fuse_threshold_bps + evaluate_open_eligibility`。
- `aats/services/strategy_engines/independent/gates.py`
  - 接受 replay 校准测试对动态熔断权重的约束。
- `aats/services/trade_costs.py`
  - 用样本中的 top-of-book 价格与数量重建 size-aware 诊断字段。

## Input/output interfaces

- 输入：
  - BTC live 样本时间点
  - `best_bid / best_ask / last_price / bid_size / ask_size`
  - live 记录中的 `short_expected_signal_edge_bps / short_expected_cost_bps / short_expected_net_edge_bps`
  - 当前 `default_order_qty = 0.01`
- 输出：
  - `depth_consumption_ratio`
  - `size_impact_bps`
  - `cost_confidence`
  - `effective_max_cost_bps`
  - 是否触发 `independent_short_book_expected_cost_above_max_acceptable`

## Database schema / tables / indexes / constraints

- 无 schema 变更。
- 样本来源于 live PostgreSQL：
  - `strategy_sleeve_intents`
  - `event_store`

## Transactions, Consistency, Concurrency

- 仅新增离线回放测试，无事务语义变化。

## Authorization, Authentication, Data Security

- 测试文件中不保存数据库连接串、密钥或其他敏感信息。
- 仅固化必要行情与策略数值样本。

## Error Handling and Idempotency

- 测试不访问数据库，避免因为环境不可用导致不稳定。
- 如果未来 live 事件结构变化，测试样本仍可独立复现当前语义。

## State Transition and Lifecycle

- 真实样本目标：
  - 这些 short 候选在新的成本熔断语义下，不应再因为普通成本超 nominal 上限而被熔断。
- 压力样本目标：
  - 用同一真实最薄盘口样本做更大订单的 stress replay，验证熔断仍会在明显放大冲击时 fail-closed。

## Caching and Performance

- 无新增 I/O。
- 测试只做轻量级数值计算。

## Logging, Monitoring, Auditing

- 通过测试名称和时间戳明确标记样本来源窗口。
- 不向仓库写入任何敏感原始 payload。

## Testing Strategy

- unit:
  - 4 个真实 BTC live short 样本不再触发成本熔断。
  - 最薄真实样本会让 fuse 比“无深度惩罚”版本更紧，但仍放行实际 `0.01` 下单量。
  - 同一最薄真实样本在 `0.02` stress size 下会触发熔断。

## Migration, Rollback, Compatibility

- 无 migration。
- 回滚方式：
  - 删除 replay 校准测试，恢复对应权重。

## Configuration and Environment Isolation

- 测试使用与 live 语义一致的关键参数：
  - `default_order_qty = 0.01`
  - `strategy_edge_noise_buffer_bps = 4.5`
  - `max_acceptable_cost_bps = 7.5`
  - `min_safe_net_edge_bps = 3.0`
  - `expected_slippage_buffer_bps = 1.0`
  - `expected_execution_buffer_bps = 2.0`

## Code Organization and Dependencies

- 仅新增测试文件与文档。
- 不新增第三方依赖。

## Documentation and Operations Manual

- 本文档记录样本来源和校准目标。

## Deployment and Acceptance Criteria

- 真实窗口样本在本地测试中稳定复现。
- 新测试能保护动态熔断权重不回退到再次误拦这 4 次 short 候选。
- lint、相关 unit tests、全量 unit、最窄 integration test 通过。

## Extension Status

- 2026-04-01 增补了 1 组真实 `BTC long` 回放样本，覆盖 `buy/long` 方向，验证成本名义超限时不会再被旧的硬成本门误拦。
- 回放夹具现在保留每个样本自己的 `exchange / product_type / margin_mode / expected_slippage_bps / orderbook_depth`，后续接入真实 non-BTC 样本时不需要再把 helper 改回去。
- 当前仓库可访问的 AATS 数据库与本地日志里没有任何真实 non-BTC live 决策窗口或盘口快照；因此本任务没有伪造“非 BTC 真回放”样本。
- 非 BTC 回放样本要继续补齐，必须先接入真实来源之一：另一套运行数据库、保留了非 BTC 决策窗口的日志，或单独导出的非 BTC 决策/盘口快照。
