# Task 93: 合约 Hedge Mode Phase 2 双腿仓位状态与快照交付说明

## 1. 业务目标与边界

本阶段目标是把“合约双腿仓位”从底层可存储状态，提升为上层可消费状态：

- 本地持仓真相继续保留 `symbol + pos_side`
- `DecisionContext` 显式暴露 long/short 双腿、净仓、毛仓
- operator 查询显式暴露 instrument 级双腿状态

本阶段明确不包含：

- 腿级下单语义
- planner / order manager 改造
- 风控四口径改造
- 对账 / 恢复腿级改造
- UI 渲染重构

## 2. 当前行为简述

在本次改造前：

- `PortfolioState`、`PortfolioSnapshot.positions` 已经能保留 `BTC-USDT-SWAP:long` 与 `BTC-USDT-SWAP:short`
- 但 `DecisionContextBuilder` 会先把同 symbol 仓位求和，只输出 `current_position_qty`
- operator `/positions` 主要暴露 `local_net_positions` / `exchange_net_positions`

这意味着底层已经是双腿，上一层仍然主要以净仓视角消费。

## 3. 模块职责与领域模型

新增领域对象：

- `PositionLegState`
- `InstrumentPositionState`

职责划分：

- `aats/schemas/portfolio.py`
  - 定义腿级状态与 instrument 级状态 schema
- `aats/services/portfolio_service/instrument_states.py`
  - 负责把本地 `Position` 或交易所 `ExchangePosition` 聚合成 `InstrumentPositionState`
- `aats/services/decision_engine/context_builder.py`
  - 负责把双腿状态注入 `DecisionContext`
- `aats/services/operator/query_service.py`
  - 负责把双腿状态转换成 operator 可读 payload
- `aats/services/operator/account_queries.py`
  - 负责在 `/positions` 输出中显式暴露 instrument 级双腿状态

## 4. 输入 / 输出接口

输入：

- `PortfolioSnapshot.positions`
- `ExchangeAccountSnapshot.positions`

输出：

- `DecisionContext.current_position_state`
- `DecisionContext.current_position_legs`
- `DecisionContext.current_net_position_qty`
- `DecisionContext.current_gross_position_qty`
- `DecisionContext.current_long_position_qty`
- `DecisionContext.current_short_position_qty`
- `DecisionContext.current_net_position_notional`
- `DecisionContext.current_gross_position_notional`
- `DecisionContext.current_long_position_notional`
- `DecisionContext.current_short_position_notional`
- `/positions.local_instrument_positions`
- `/positions.exchange_instrument_positions`

兼容性约束：

- 旧字段 `current_position_qty` 继续保留，语义仍为净仓
- 旧字段 `local_net_positions` / `exchange_net_positions` 继续保留，但内容升级为 richer instrument state

## 5. 数据库 / 表 / 索引 / 约束

本阶段无数据库 schema 变更。

原因：

- 双腿真相已存在于持仓快照与执行链路的 `position_key`
- 本阶段只增加上层聚合与暴露，不改持久化结构

## 6. 事务、一致性与并发

- 本阶段只读聚合，不新增事务边界
- `InstrumentPositionState` 为快照派生对象，不作为独立持久化真相
- 同一快照内的 long/short 腿按单次读取聚合，避免跨快照拼接

## 7. 授权、认证与数据安全

- 不新增写接口
- 不新增敏感凭证读取
- 双腿状态仅通过现有 operator 查询面暴露，沿用现有鉴权

## 8. 错误处理与幂等

- 当本地快照不存在 symbol 对应 legs 时，context/query 返回空或零值，不抛出新异常
- 现货无持仓但有 base balance 时，context 仍可回退到 spot balance synthetic state
- 交易所 side / notional 缺失时，exchange instrument state 退回到已知 quantity/price 推导

## 9. 状态转换与生命周期

本阶段不改变持仓生命周期，只改变表达方式：

- 底层仍由 `PortfolioState` 维护 position lifecycle
- 上层新增 `InstrumentPositionState` 作为派生视图

## 10. 缓存与性能

- 聚合逻辑基于当前快照内存对象执行
- 不引入额外 repository 查询
- `/positions` 仍复用现有 query cache / ttl cache

## 11. 日志、监控与审计

- 本阶段不新增 audit 事件
- 不新增额外日志噪音
- 后续 Phase 5 / 6 再补腿级 mismatch / operator audit

## 12. 测试策略

单测：

- `tests/unit/test_decision_context_builder.py`
  - 验证 long/short legs 能生成正确的 net/gross state
  - 验证 build 后的 `DecisionContext` 暴露双腿字段
- `tests/unit/test_operator_position_states.py`
  - 验证本地与交易所双腿聚合 payload

最窄集成：

- `tests/integration/test_operator_api.py`
  - 验证 `/positions` 暴露 `local_instrument_positions`

## 13. 迁移、回滚与兼容性

迁移：

- 无数据库迁移
- 无环境变量迁移

回滚：

- 可直接回滚本轮代码改动

兼容性：

- 旧净仓字段保留
- 新字段为 additive change

## 14. 配置与环境隔离

本阶段不新增配置项。

双腿状态能力仅在已有合约 runtime 中生效，现货路径保持兼容。

## 15. 代码组织与依赖

- 新增共用聚合 helper：`aats/services/portfolio_service/instrument_states.py`
- 避免把双腿聚合散落在 `context_builder`、`query_service` 各自手写

## 16. 文档与运维手册

本文件即为本阶段交付说明。

运维含义：

- 现在可以从 `/positions` 和 `DecisionContext` 明确看到双腿状态
- 但这还不是“hedge mode 可下单”，只是 Phase 2 完成

## 17. 部署与验收标准

验收通过条件：

- `DecisionContext` 显式包含双腿字段
- `/positions` 显式包含 `local_instrument_positions` / `exchange_instrument_positions`
- 旧净仓字段仍可工作
- 单测与最窄集成测试通过

## 18. 剩余风险

- 当前 target/risk/planner 仍以净仓语义为主，这属于后续 Phase 3-5 范围
- `local_net_positions` 字段名仍带历史语义，但内容已经升级为 richer state；后续 Phase 6 再考虑 UI 与字段命名治理
