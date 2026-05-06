# BTCUSDT 错过行情回放报告 - 2026-05-06 20:30 至 2026-05-07 00:30 +08

## 结论摘要

这段行情不是系统完全没有识别趋势。live 事件显示，`baseline` 和 `directional` sleeve 在主跌段持续识别到 short，但所有非零 directional intent 都在预算层被压成 0，没有进入订单层。

本次反事实结果不支持直接关闭 `directional_loss_blocks_risk_increase`。如果只把预算压零拿掉，并让系统按每个 directional target 高频调整仓位，毛收益仍不能覆盖预估交易成本，净结果明显变差。更合理的修复方向是降低无意义 churn，做 episode 级别的趋势确认和持仓，而不是简单放开亏损后的新增方向风险。

## 数据范围

- 固定窗口: `2026-05-06 20:30:00+08` 到 `2026-05-07 00:30:00+08`
- 标的: `BTC-USDT-SWAP`
- 数据库: `aats_live_derivatives`
- 主要来源: `event_store`
- 使用事件:
  - `MarketSnapshot`: 663 条
  - `BaselineAssessment`: 663 条
  - `StrategySleeveIntent`: 4633 条，其中 directional intent 662 条
  - `PositionTarget`: 661 条
  - `DecisionOutcome`: 664 条
- 订单侧事实: 同窗口 `execution_orders=0`、`execution_commands=0`、`execution_fills=0`

窗口内市场快照从 `82359.0` 到 `81488.5`，下跌 `870.5 USDT`。这解释了肉眼看起来趋势很明显，但系统实际没有任何真实订单承接这个方向。

## 决策时间线

| 时间桶 +08 | 首价 | 末价 | 价格变化 | baseline long/short/flat | intent long/short/zero | suppressed |
|---|---:|---:|---:|---:|---:|---:|
| 20:30 | 82359.0 | 82324.4 | -34.6 | 19 / 0 / 3 | 18 / 0 / 3 | 18 |
| 20:45 | 82329.9 | 82195.3 | -134.6 | 31 / 0 / 0 | 29 / 0 / 3 | 29 |
| 21:00 | 82189.2 | 82089.4 | -99.8 | 1 / 18 / 31 | 1 / 16 / 33 | 17 |
| 21:15 | 82070.0 | 81909.4 | -160.6 | 0 / 27 / 14 | 0 / 23 / 18 | 23 |
| 21:30 | 81847.8 | 81868.8 | +21.0 | 0 / 58 / 0 | 0 / 57 / 1 | 57 |
| 21:45 | 81873.0 | 81633.3 | -239.7 | 0 / 52 / 0 | 0 / 45 / 6 | 45 |
| 22:00 | 81631.7 | 81427.3 | -204.4 | 0 / 62 / 0 | 0 / 56 / 6 | 56 |
| 22:15 | 81415.8 | 81580.0 | +164.2 | 0 / 55 / 0 | 0 / 55 / 0 | 55 |
| 22:30 | 81585.7 | 81840.6 | +254.9 | 0 / 39 / 0 | 0 / 32 / 7 | 32 |
| 22:45 | 81845.0 | 81472.7 | -372.3 | 0 / 48 / 1 | 0 / 37 / 12 | 37 |
| 23:00 | 81466.4 | 81362.9 | -103.5 | 0 / 36 / 0 | 0 / 36 / 0 | 36 |
| 23:15 | 81378.9 | 81623.1 | +244.2 | 0 / 4 / 30 | 0 / 4 / 30 | 4 |
| 23:30 | 81616.0 | 81619.7 | +3.7 | 0 / 27 / 7 | 0 / 23 / 10 | 23 |
| 23:45 | 81642.2 | 81669.9 | +27.7 | 0 / 31 / 0 | 0 / 32 / 0 | 32 |
| 00:00 | 81658.3 | 81724.1 | +65.8 | 0 / 10 / 27 | 0 / 9 / 28 | 9 |
| 00:15 | 81708.6 | 81488.5 | -220.1 | 0 / 9 / 23 | 0 / 4 / 28 | 4 |

核心观察:

- 20:30 到 20:59 系统偏 long，但价格已经下跌。这说明早段并不是“没有执行就错过赚钱”，如果放开，早段会先承受错误方向损失。
- 21:15 到 23:00 是真正的主 short 段，baseline short 和 directional short 均较密集。
- 23:15 以后 baseline 明显转 flat/弱 short，说明截图附近的反弹并未被稳定判定为可做多趋势。

## 预算层阻塞链

directional intent 汇总:

- 总 directional intent: 662
- 非零请求: 477
- long 请求: 48
- short 请求: 429
- zero/hold 请求: 185
- `suppressed_after_approval`: 477

477 个非零请求全部带有以下预算阻塞原因:

- `reconciliation_contraction_active`
- `pnl_contraction_active`
- `directional_loss_blocks_risk_increase`
- `budget_contracted_to_zero`
- `approved_but_budget_zero_suppressed`
- `composed_as_advisory_only`

对应源码:

- `aats/services/strategy_engines/sleeve_budget_controller.py:83-99` 在 recent net PnL 为负且 directional 请求增加风险时，把 `pnl_multiplier` 置 0。
- `aats/services/strategy_engines/sleeve_budget_controller.py:111-127` 把 `effective_scale <= 0` 落成 `budget_zero_suppressed`。
- `aats/services/strategy_engines/sleeve_routing_composer.py:85-113` 把被预算压零的 approved intent 转成 `execution_behavior="suppressed_after_approval"`，实际 composed delta 为 0。

这就是“系统看到了，但不下单”的直接链路。

## 反事实假设和结果

### A. 不压零，逐 target 高频跟随

假设:

- 使用 directional intent 的 `requested_target_position_qty` 作为目标仓位。
- 每次 target 变化都按最近 market snapshot 成交。
- 成本使用事件中的 `expected_cost_bps`。
- 窗口末按市场价 mark，并保留最后仓位。

结果:

| 指标 | 数值 |
|---|---:|
| target changes | 533 |
| turnover | 159,720.53 USDT |
| 最大仓位 | 0.01724443 BTC |
| gross PnL | -1.136584 USDT |
| estimated cost | 130.501304 USDT |
| net PnL | -131.637888 USDT |

解释: 这不是一个可接受的修复方向。即便取消预算压零，按当前 target 频率高频调整，交易成本会远大于方向收益。

### B. 不压零，但按 15 分钟多数方向降 churn

假设:

- 每 15 分钟只看该桶 directional intent 的多数方向。
- 使用同窗口非零 directional target notional 的中位数 `880.2078 USDT` 作为目标名义本金。
- 每个桶最多调整一次仓位。
- 成本统一使用 `11.5 bps` 近似。

结果:

| 指标 | 数值 |
|---|---:|
| target changes | 15 |
| turnover | 5,295.85 USDT |
| gross PnL | +4.727142 USDT |
| estimated cost | 6.090224 USDT |
| net PnL | -1.363081 USDT |

这个结果比逐 target 好很多，但仍未覆盖成本。原因是:

- 20:30-20:59 的 long 在下跌中亏损。
- 22:15-22:30 的 short 遇到反弹亏损。
- 23:30-23:45 后续 re-short 贡献不稳定。

如果只截取 `21:15-23:15` 的主 short episode，按同样 `880.2078 USDT` 名义本金、`21:15` 首价 `82070.0` 入场、`23:15` 首价 `81378.9` 出场、`11.5 bps` 进出成本估算，结果约为 `gross +7.412107 USDT`、`cost 2.015954 USDT`、`net +5.396153 USDT`。但全窗口加入早段错误 long、回撤反弹和后段 re-entry 后，优势被成本和 churn 抹平。

### C. 只放宽 cost buffer

事实:

- 同窗口 `expected_edge_below_cost_buffer` cost gate candidate 共 26 个。
- 其中 buy 3 个，sell 23 个。
- 平均 signal edge: `13.080 bps`
- 平均 expected cost: `11.665 bps`
- 平均 required edge: `15.665 bps`

反事实结果:

| 场景 | gross PnL | estimated cost | net PnL | 说明 |
|---|---:|---:|---:|---|
| 只放宽 cost buffer，但预算压零仍开启 | 0.000000 | 0.000000 | 0.000000 | 预算层仍是硬约束，放宽 cost 不会产生可执行 delta |
| 单独执行这些 cost-gated candidates | +4.741190 | 6.113250 | -1.372060 | 毛收益为正，但不覆盖成本 |

结论: 这段窗口里 cost buffer 并不是错过利润的主因。它拦下的是边际 edge 不够厚的交易；贸然放宽会增加成交，但不一定改善净收益。

对应源码:

- `aats/services/decision_engine/target_position.py:1302-1309` 用 expected cost、noise buffer、min net edge 计算 required edge。
- `aats/services/decision_engine/target_position.py:2505-2512` 把失败候选记录到 `expected_edge_below_cost_buffer`。

### D. 短周期趋势更敏感

诊断假设:

- 用 market snapshot 构造 5 分钟 momentum 信号。
- 阈值测试: 10 / 20 / 30 / 40 / 50 / 75 bps。
- 目标名义本金同样使用 `880.2078 USDT`。
- 每 5 分钟最多切换一次方向，成本按 `11.5 bps`。

结果:

| 5m 阈值 | changes | gross PnL | estimated cost | net PnL |
|---:|---:|---:|---:|---:|
| 10 bps | 38 | -2.467746 | 42.512879 | -44.980626 |
| 20 bps | 28 | -5.680701 | 30.367390 | -36.048090 |
| 30 bps | 14 | -0.204345 | 14.174474 | -14.378820 |
| 40 bps | 6 | -2.725090 | 6.077358 | -8.802448 |
| 50 bps | 2 | -1.874387 | 2.026633 | -3.901021 |
| 75 bps | 0 | 0.000000 | 0.000000 | 0.000000 |

结论: “更敏感”本身不是答案。这个窗口里简单 5m momentum 会更频繁地追涨杀跌，成本和反弹段亏损会吞掉收益。短周期模块如果要做，必须加入 episode 级别的趋势确认、反弹退出、最小持仓时间和再入场冷却。

## 风险判断

1. 不能直接关闭 `directional_loss_blocks_risk_increase`。它确实错过了主 short 段，但也阻止了早段错误 long 和后续高 churn re-entry。
2. 不能只放宽 cost buffer。该窗口 cost-gated candidates 的毛收益为正但净收益为负，说明 buffer 在保护边际交易。
3. 应优先修“信号到执行的 churn 结构”，而不是简单放大风险预算。
4. 当前策略缺少一个明确的 source-level episode 控制: 何时确认趋势、何时只持有不微调、何时冷却、何时允许反向。

## 建议下一步

1. 加一个只读 replay 脚本，把本报告的 SQL 和反事实计算固化，支持任意时间窗复跑。
2. 设计 directional episode gate: 连续 N 分钟/多桶方向一致后才开仓，中途不因小 target 抖动反复调仓。
3. 保留 `directional_loss_blocks_risk_increase`，但研究一个“受限恢复额度”: 在亏损收缩状态下，只允许低 churn、低 notional、episode-confirmed 的方向试单。
4. 对 cost buffer 做分层: 不放宽全局阈值，只允许在强趋势 episode 内降低 noise buffer，并要求 replay/backtest 净收益覆盖成本。

## 复核命令摘要

本报告使用只读查询和本地反事实计算，没有读取或输出任何凭证。

关键核对项:

- 事件计数: `MarketSnapshot=663`, `BaselineAssessment=663`, `StrategySleeveIntent=4633`, `PositionTarget=661`, `DecisionOutcome=664`
- directional intent: `662`
- 非零 directional intent: `477`
- `suppressed_after_approval`: `477`
- 同窗口订单/命令/成交: 全部 `0`
- cost gate candidates: `26`
