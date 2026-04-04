# Task95 合约实盘风控 short 仓强平距离误判修复

## 1. 业务目标与边界
- 目标：修复 `DerivativesLiveGuardService` 对 OKX 双向持仓空仓的强平距离误判，避免把远离强平的 `short` 仓错误判定为 `derivatives_liquidation_proximity_auto_halt`。
- 边界：仅修复合约实盘运行时风控的强平距离方向判断，不改订单语义、不改账户解析公共接口、不改 UI 文案。

## 2. 模块职责与领域模型
- `aats/services/execution_engine/okx_account.py`
  - 负责把 OKX 账户、仓位、风险快照映射到 `ExchangeAccountSnapshot`。
  - 当前仓位契约是：`ExchangePosition.side` 表示方向；`ExchangePosition.quantity` 表示内部数量，不保证通过正负号表达方向。
- `aats/services/governance_engine/derivatives_live_guard.py`
  - 负责根据账户快照与风险快照生成 `only_reduce` / `auto_halt` 结论。
  - 本次修复点在 `_liquidation_summary()`。
- `aats/services/operator/query_service.py`
  - 已存在正确的方向判断逻辑，可作为本次修复的对齐参考。

## 3. 输入 / 输出接口
- 输入：
  - `ExchangeAccountSnapshot.positions[*].side`
  - `ExchangeAccountSnapshot.positions[*].quantity`
  - `ExchangeAccountSnapshot.positions[*].mark_price`
  - `ExchangeAccountSnapshot.positions[*].liquidation_price`
- 输出：
  - `nearest_liquidation_gap_ratio`
  - `closest_position`
  - `only_reduce_reasons`
  - `auto_halt_reasons`

## 4. 数据库 Schema / 表 / 索引 / 约束
- 本次不修改数据库 schema。
- 排障涉及但不变更的表：
  - `execution_orders`
  - `execution_fills`
  - `event_store`
  - `reconciliation_reports`

## 5. 事务、一致性与并发
- 本次变更仅影响内存中的风险判断，不新增事务边界。
- 风险判断应与 operator 侧强平距离计算保持一致，避免同一持仓在不同视图中出现互相矛盾的结论。

## 6. 授权、认证与数据安全
- 不修改鉴权和凭证读取逻辑。
- 验证期间允许使用现有只读账户查询；不新增外部写操作。

## 7. 错误处理与幂等性
- 当 `side` 缺失时，允许回退到 `quantity` 的正负号判断，保持对旧快照或净持仓场景的兼容。
- 修复后重复计算同一快照应得到稳定一致的风险结论。

## 8. 状态迁移与生命周期
- 修复前：
  - OKX `short` 仓可能以 `side="short"` 且 `quantity>0` 进入风控。
  - `live_guard` 用 `quantity > 0` 误判为多仓，得到负的 gap，误触发 `auto_halt`。
- 修复后：
  - 优先按 `side/pos_side` 判定多空方向。
  - 仅在方向缺失时回退到 `quantity` 正负号。

## 9. 缓存与性能
- 仅修改单次风控计算的方向分支，不引入新缓存。
- 性能影响可忽略。

## 10. 日志、监控与审计
- 不新增日志字段。
- 修复后 `system.processing_failures` 中的 `nearest_liquidation_gap_ratio` 应与实际方向一致。

## 11. 测试策略
- 单元测试：
  - 为 `DerivativesLiveGuardService` 增加 `short + 正数量 + liqPx > markPx` 回归用例。
  - 为 `OKXAccountService` 增加 OKX `posSide=short` 且 `pos>0` 的映射契约回归用例。
- 集成测试：
  - 运行最窄 operator API 风险包测试，确认控制面链路未被破坏。

## 12. 迁移、回滚与兼容性
- 无 schema 迁移。
- 如需回滚，仅回退本次 Python 代码变更。
- 兼容旧快照：`side` 缺失时仍保留数量符号回退逻辑。

## 13. 配置与环境隔离
- 不修改 `.env` 和 managed profile。
- 继续使用 `derivatives_live` 现有阈值：
  - `AATS_DERIVATIVES_AUTO_HALT_LIQUIDATION_GAP_FRACTION`
  - `AATS_LIQUIDATION_BUFFER_FRACTION`

## 14. 代码组织与依赖
- 变更限定在：
  - `aats/services/governance_engine/derivatives_live_guard.py`
  - `tests/unit/test_task72_derivatives_live_guard.py`
  - `tests/unit/test_okx_account.py`

## 15. 文档与运维手册
- 本文档记录本次 bugfix 的契约与验证范围。
- 运维结论：后续若再出现类似误停机，应优先核查 `side` 与 `quantity` 的语义是否被混用。

## 16. 部署与验收标准
- 验收标准：
  - `short` 仓且 `liqPx > markPx` 时，`nearest_liquidation_gap_ratio` 为正值。
  - 不再因 `short + 正数量` 误触发 `derivatives_liquidation_proximity_auto_halt`。
  - 相关 unit / integration tests 通过。
