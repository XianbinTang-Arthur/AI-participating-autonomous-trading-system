# Task107 业务线公式审计与修复 SOW

## 1. 业务目标与边界
- 目标：按当前仓库实际业务线梳理关键计算链路，修复与此前“手续费口径 / 保证金占用 / 风控比率”同类的可复现 bug。
- 本次覆盖：
  - 方向性交易主线（target / execution health / 风控收缩）
  - 策略控制与运营画像主线（strategy profile / operator safety summary）
  - 执行事实与回放主线（converged execution truth）
  - 资金成本与仓位费用相关口径
- 非目标：
  - 不重构策略协调器、风控引擎或执行存储架构
  - 不修改交易所费率默认值或引入外部最新费率表
  - 不改数据库 schema

## 2. 当前业务线归纳
- 业务线 1：方向性交易主线
  - baseline / AI -> target_position -> policy / risk -> execution
- 业务线 2：智能套利主线
  - smart_arbitrage，包含 `spot_carry` / `margin_reverse_carry`
- 业务线 3：现货网格主线
  - `spot_grid`
- 业务线 4：定投主线
  - `dca`
- 业务线 5：合约保护性对冲主线
  - `strategy_hedge_overlay_mode=protective`
- 业务线 6：合约机会型对冲主线
  - `strategy_hedge_opportunistic_*`
- 业务线 7：合约独立双账本主线
  - `strategy_hedge_independent_*`
- 支撑链路：
  - portfolio / ledger / reconciliation / recovery
  - operator / strategy profile / AI shadow

## 3. 模块职责与领域模型
- `aats/services/strategy_execution_health.py`
  - 负责按成交与快照回放最近平仓表现，生成 `fee_drag_ratio / churn_ratio / low_edge_streak`
- `aats/services/operator/strategy_profile_context.py`
  - 负责策略画像控制所需的 performance summary
- `aats/services/operator/strategy_profiles.py`
  - 负责把 safety_state/live_guard 汇总为策略画像侧的 adaptive controls
- `aats/services/governance_engine/risk.py`
  - 负责交易运行时 adaptive controls 与风险预算收缩
- `aats/storage/execution_repo_converged_postgres.py`
  - 负责在 converged execution truth 下用 fills 回填 synthetic order state

## 4. 输入 / 输出接口
- 输入：
  - fill 级手续费：`fee_amount` / `fee_currency`
  - fill 级 quote 手续费：`fill_fee_cost_in_quote(...)`
  - 已实现盈亏：`realized_pnl_delta` / snapshots
  - 实盘风控快照：`current_initial_margin_usage_fraction` / `nearest_liquidation_gap_ratio`
- 输出：
  - `recent_fee_drag_ratio`
  - `fee_to_gross_pnl_ratio`
  - `risk_budget.multiplier`
  - `execution_aggressiveness.multiplier`
  - synthetic `OrderState.fees`

## 5. 数据库 schema / 表 / 索引 / 约束
- 本次不改 schema。
- 涉及只读/写入链路：
  - `execution_orders`
  - `execution_fills`
  - `portfolio_snapshots`
  - `strategy_profile_*`

## 6. 事务、一致性与并发
- 修改均为纯计算或回填汇总逻辑，不新增事务边界。
- 需要保证同一笔成交在：
  - execution truth
  - strategy health
  - strategy profile
  - operator 安全视图
  之间保持一致口径。

## 7. 授权、认证与数据安全
- 不改认证授权。
- 不新增外部写操作。
- 不引入联网费率抓取。

## 8. 错误处理与幂等性
- 对缺失的 projected margin 字段保持保守处理：缺失时传 `None`，不伪造为 current。
- 对 fee/gross 比率继续保持零分母保护。

## 9. 状态迁移与生命周期
- 修复前：
  - 负 gross realized 会把 fee ratio / fee drag ratio 直接压成 `1.0`
  - 只有 current margin usage 时，adaptive controls 会把同一值同时当作 projected 与 current 使用
  - converged fill backfill 会把原始 `fee_amount` 直接写入 `OrderState.fees`
- 修复后：
  - fee ratio / fee drag ratio 统一按 `abs(gross)` 计量
  - projected margin usage 仅在真实存在时参与 projected 惩罚
  - synthetic order state 的 `fees` 统一按 quote 成本汇总

## 10. 缓存与性能
- 不新增缓存。
- 仅增加少量本地 helper/字段解析，性能影响可忽略。

## 11. 日志、监控与审计
- 不新增日志字段。
- 修复后 operator / risk / execution truth 的费用与收缩原因会更一致，便于后续审计。

## 12. 测试策略
- 单测：
  - strategy execution health 在 gross realized 为负时仍按绝对值计算 fee drag
  - strategy profile context 在 gross realized 为负时仍返回真实 fee ratio
  - risk engine / strategy profile adaptive summary 不再把 current margin 当 projected 重复惩罚
  - converged execution repo 回填 order state 时按 quote 手续费汇总
- 集成测试：
  - 运行最窄 operator API / persistence 相关测试确认链路未破坏

## 13. 迁移、回滚与兼容性
- 无 schema migration。
- 回滚仅需回退 Python 代码与测试。
- 对外 API 保持兼容，仅修正内部数值口径。

## 14. 配置与环境隔离
- 继续使用现有 `settings.py` 与 `configs/strategy_profiles/*.yaml`
- 不改 `.env` 与 managed profile

## 15. 代码组织与依赖
- 变更限定在：
  - `aats/services/strategy_execution_health.py`
  - `aats/services/operator/strategy_profile_context.py`
  - `aats/services/operator/strategy_profiles.py`
  - `aats/services/governance_engine/risk.py`
  - `aats/storage/execution_repo_converged_postgres.py`
  - 相关 unit / integration tests

## 16. 文档与运维手册
- 本文档记录这次业务线级公式审计的边界与验收标准。

## 17. 部署与验收标准
- gross realized 为负时，fee ratio / fee drag ratio 不得被错误钳成 `1.0`
- 仅有 current margin usage 时，不得出现伪造的 `projected_margin_usage_*` 收缩原因
- spot buy 基础币手续费回填 synthetic order state 时，`fees` 必须按 quote 成本展示
- 相关 lint、unit、narrow integration 命令完成并报告结果
