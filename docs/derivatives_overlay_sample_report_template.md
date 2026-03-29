# 合约 Overlay 回放 / Dry-Run 样本报告模板

## 1. 样本信息

- 样本类型：回放 / dry-run
- Overlay 模式：opportunistic / independent
- 运行日期：
- 交易对：
- 运行阶段：replay_only / dry_run / live

## 2. 结论摘要

- 是否建议继续放开：
- 主要观察结论：
- 是否出现恢复 / 对账异常：

## 3. 关键指标

- 主腿成交次数：
- overlay 腿成交次数：
- 主腿净收益：
- overlay 腿净收益：
- 费用拖累：
- churn：
- 最大 gross 敞口：
- 最大 net 敞口：

## 4. 关键事件

1. 第一次开腿原因：
2. 第一次被阻断原因：
3. 是否出现 only-reduce / review_required：
4. 是否触发腿级 trial guard：

## 5. 是否满足继续放开条件

- 至少 2 组回放样本：是 / 否
- 至少 1 组 dry-run 样本：是 / 否
- operator 审计可解释：是 / 否
- recovery / reconciliation 无误导：是 / 否

## 6. 回滚建议

- 是否需要立刻回滚：
- 若回滚，执行顺序：
  - 关闭 `strategy_hedge_opportunistic_enabled`
  - 关闭 `strategy_hedge_independent_enabled`
  - 保留 `protective`
  - 必要时把 `strategy_hedge_overlay_mode` 切回 `protective`
