# Task 96: 合约 Hedge Mode 第五阶段交付说明

## 1. 业务目标与边界

本阶段只处理 `Phase 5`：让合约 `hedge mode` 的对账、恢复和 operator 恢复视图真正理解双腿仓位。

边界保持收敛：

- 不改策略层目标模型
- 不改执行器下单语义
- 不改四口径风控主逻辑
- 只修腿级对账归因、恢复归因和 operator 恢复展示

## 2. 当前问题

虽然系统已经能在快照和执行链上保留 `long / short` 两条腿，但恢复链里还残留两类历史问题：

1. `without_local_execution_chain` 的判定仍带有按 `symbol` 粗归因的旧习惯。
2. operator 恢复页没有把“哪一条腿异常、哪一条腿来源不明”直接收口给用户。

## 3. 模块职责与领域模型

### `aats/services/reconciliation_service/comparator.py`

- 继续负责生成 `ReconciliationReport`
- 把合约持仓差异拆成：
  - 腿级数量差异
  - 腿级来源不明
  - instrument 级 `long / short / net / gross` 摘要差异

### `aats/services/operator/query_service.py`

- 把原始对账报告整理成 operator 可直接消费的腿级摘要

### `aats/services/operator/recovery_queries.py`

- 把腿级对账摘要挂到 recovery view

### `aats/api/static/modules/views/risk-view.js`

- 在“风险与恢复”页直接显示腿级异常摘要

## 4. 输入 / 输出接口

输入：

- 本地 `PortfolioSnapshot.positions`
- 本地 `OrderState` / `FillEvent`
- 交易所 `ExchangeAccountSnapshot.positions`

输出：

- `ReconciliationReport.position_diff.exchange_leg_mismatches`
- `ReconciliationReport.position_diff.exchange_instrument_mismatches`
- `reconciliationLatest.mismatch_summary.leg_mismatch_summary`
- `systemRecovery.recovery.latest_reconciliation_summary`

## 5. 数据模型变化

新增非破坏性字段：

- `position_diff.exchange_leg_mismatches`
- `position_diff.stored_instrument_states`
- `position_diff.exchange_instrument_states`
- `position_diff.exchange_instrument_mismatches`
- recovery/query 层的 `latest_reconciliation_summary`
- mismatch summary 里的 `leg_mismatch_summary`

不删除旧字段，保持向后兼容。

## 6. 事务、一致性与并发

- 对账仍然是只读比较，不新增写路径
- 自动修复逻辑保持原样：只在纯本地重建场景下工作
- 腿级缺失只改变分类和恢复归因，不自动改仓

## 7. 错误处理与幂等

- `long_short_mode` 下按 `position_key` 精确判断哪一条腿缺失
- 本地已有对应腿，或本地已有该腿执行链时，不再误判成 `without_local_execution_chain`
- `report -> classifier -> recovery` 幂等链路保持不变

## 8. 状态流转

- 合法双腿并存：保持 `CLEAN` 或普通腿级差异，不进入“来源不明”
- 某一腿数量漂移：进入腿级 mismatch
- 某一腿交易所存在，但本地既无该腿快照也无该腿执行链：进入 `derivatives_exchange_position_without_local_execution_chain`

## 9. 安全、审计与运维

- 不改变权限模型
- 不新增高风险 operator 动作
- operator 恢复页只增加只读异常摘要，便于人工判读

## 10. 测试策略

本阶段至少覆盖：

- 合法双腿数量差异不会被误判为未知来源
- 只缺某一条腿时，能精确标出 `long / short`
- recovery view 能直接吐出腿级摘要
- 风险页能显示腿级异常文案

## 11. 迁移、回滚与兼容性

- 这次是纯增量字段，不需要数据迁移
- 回滚时只需回退本次代码改动，不涉及表结构或配置切换

## 12. 验收标准

- 合法 `long + short` 并存不再被误判成未知仓位
- `without_local_execution_chain` 能精确落到具体腿
- recovery/operator 视图可以明确看到哪一条腿异常
- 所有新增测试通过
