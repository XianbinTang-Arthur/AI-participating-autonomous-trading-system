# Task61 策略分层验证

## 目标

在 [Task60](D:/文件/project/AIParticipatingAutonomousTradingSystem/docs/task60/README.md) 已有执行质量与收益归因报表的基础上，把近期成交按策略维度分层，识别：

- 哪些市场状态在赚钱
- 哪些方向在亏钱
- 哪类动作成本最高
- 哪些切片样本量不足

## 本轮实现

- 新增 `/reports/strategy-segments`
- 默认按以下维度分组：
  - `symbol`
  - `market_regime`
  - `side`
  - `execution_action`
- 支持自定义 `group_by`

## 输出字段

每个 segment 输出：

- `fill_count`
- `winning_fill_count`
- `losing_fill_count`
- `win_rate`
- `gross_realized_pnl`
- `net_realized_pnl`
- `total_fees`
- `total_notional`
- `fee_to_notional_ratio`
- `avg_adverse_slippage_bps`

## 数据来源

- `fill_outcomes`
- `decision_context`
- `execution_action / position_intent`

## 目的

这一步的重点不是做复杂策略研究，而是先让系统具备一个基本能力：

可以明确看到，近期收益究竟集中在什么条件下产生，亏损又集中在什么条件下发生。
