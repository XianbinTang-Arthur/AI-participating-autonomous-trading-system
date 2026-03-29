# Task96 公式口径修复与回归测试
## 1. 业务目标与边界
- 目标：修复 4 个已经确认的公式/口径问题，避免成本模型、试盘守护、运营报表和保证金 helper 出现系统性误判。
- 范围：
  - 修复 smart arbitrage funding account proxy 的 per-event / total 口径混淆。
  - 修复 maker rebate 被错误当成正成本或被直接抹掉的问题。
  - 修复 fee-to-notional ratio 未统一换算到 quote 口径的问题。
  - 修复 `derivatives_initial_margin_requirement()` 对字符串数量输入会抛异常的问题。
- 非目标：
  - 不重构整套成本模型。
  - 不改数据库 schema。
  - 不改交易所接入公共接口的入参格式。

## 2. 模块职责与领域模型
- `aats/services/execution_engine/okx_account.py`
  - 负责把 OKX 账户、费率、recent bills、funding schedule 映射到本地快照与辅助 summary。
- `aats/services/fee_resolver.py`
  - 负责统一解析 taker / maker / funding / settlement 的有效费率口径。
- `aats/services/trade_drag.py`
  - 负责把 fee、spread、slippage、funding、borrow 等组件聚合成 drag / edge。
- `aats/services/strategy_engines/smart_arbitrage/cost_model.py`
  - 负责套利场景的 funding event 投影与成本估算。
- `aats/services/operator/query_service.py`
  - 负责 execution-quality、profitability、trial review 等运营查询口径。
- `aats/services/governance_engine/trial_guard.py`
  - 负责前向试盘守护的 fee drag、slippage、慢成交阈值判断。
- `aats/services/accounting.py`
  - 提供 spot / derivatives 预估资金占用、手续费换算等基础 helper。

## 3. 输入 / 输出接口
- 输入：
  - OKX fee schedule 中的 `maker` / `taker`
  - OKX recent funding fee bills
  - fill outcome 中的 `fee_amount` / `fee_currency` / `fee_delta` / `fill_notional`
  - `derivatives_initial_margin_requirement()` 的 `quantity` / `reference_price` / `target_leverage`
- 输出：
  - `funding_fee_bps_proxy`
  - `funding_fee_bps_proxy_per_event`
  - `effective_maker_fee_bps`
  - `fee_ratio` / `fee_to_notional_ratio`
  - `ideal_total_cost_bps` / `executable_total_drag_bps`
  - `derivatives_initial_margin_requirement()`

## 4. 数据库 Schema / 表 / 索引 / 约束
- 本次不修改 schema。
- 相关只读链路涉及：
  - `execution_fills`
  - `fill_outcomes`
  - `funding_fee_records`
  - `portfolio_snapshots`
  - `event_store`

## 5. 事务、一致性与并发
- 本次改动主要是纯计算逻辑与查询口径，不引入新的事务边界。
- 同一条 fill / funding bill 在不同视图中应维持同一 quote 口径，避免 query 与 guard 结果不一致。

## 6. 授权、认证与数据安全
- 不修改认证授权逻辑。
- 不新增外部写操作。
- 所有修复仅影响内存计算与 API 视图返回值。

## 7. 错误处理与幂等性
- fee 口径换算优先使用已存在的 quote 成本字段；缺失时再按原始 fee 推导；仍缺失时保守回退。
- `derivatives_initial_margin_requirement()` 对字符串、数值、Decimal 输入都应保持幂等可计算。

## 8. 状态迁移与生命周期
- 修复前：
  - funding account proxy 可能被重复乘 funding events。
  - maker rebate 被转换成正成本或直接被钳成 0。
  - fee ratio 可能混用 base / quote 量纲。
  - 字符串数量输入可能触发 TypeError。
- 修复后：
  - funding total 与 per-event 口径分离。
  - maker rebate 允许以负值进入 drag 计算。
  - fee ratio 统一优先按 quote 成本计算。
  - 保证金 helper 对输入类型更稳健。

## 9. 缓存与性能
- 不新增持久缓存。
- 仅增加少量本地字段推导与 helper 调用，性能影响可忽略。

## 10. 日志、监控与审计
- 不新增日志字段。
- 修复后 operator / trial guard / cost model 的数值应能相互对齐，便于后续审计。

## 11. 测试策略
- 单元测试：
  - OKX maker rebate 保持负值。
  - smart arbitrage funding account proxy 不再对累计 proxy 重复乘次数。
  - trade drag 允许负 fee 降低总 drag。
  - trial guard fee ratio 优先使用 quote 口径 `fee_delta`。
  - derivatives initial margin helper 接受字符串数量。
- 集成测试：
  - 运行最窄的 operator API / strategy runtime 相关测试，确认 API 结果链路未被破坏。

## 12. 迁移、回滚与兼容性
- 无 schema migration。
- 回滚仅需回退本次 Python 代码与测试改动。
- 对外接口保持兼容；新增字段仅为补充字段，不删除旧字段。

## 13. 配置与环境隔离
- 不修改 `.env` 或 managed profile。
- 继续使用现有 `spot` / `derivatives` / `*_live` 配置。

## 14. 代码组织与依赖
- 变更限定在：
  - `aats/services/accounting.py`
  - `aats/services/execution_engine/okx_account.py`
  - `aats/services/fee_resolver.py`
  - `aats/services/trade_drag.py`
  - `aats/services/strategy_engines/smart_arbitrage/cost_model.py`
  - `aats/services/governance_engine/trial_guard.py`
  - `aats/services/operator/query_service.py`
  - `aats/services/operator/report_queries.py`
  - `aats/services/operator/strategy_queries.py`
  - `aats/services/operator/strategy_profile_context.py`
  - 相关 unit / integration tests

## 15. 文档与运维手册
- 本文档记录本次公式修复的口径边界与验收标准。
- 运维侧需要注意：
  - `funding_fee_bps_proxy` 保留历史 total 口径。
  - 新增 `funding_fee_bps_proxy_per_event` 供 event-projection 场景使用。

## 16. 部署与验收标准
- 验收标准：
  - account proxy funding 在多次 funding 预测下不再重复放大。
  - maker rebate 能反映为更低甚至负的 fee/drag。
  - fee-to-notional ratio 在 quote 口径下计算。
  - `derivatives_initial_margin_requirement(quantity="0.1", ...)` 不再抛异常。
  - 相关 lint、unit、narrow integration 测试完成并报告结果。
