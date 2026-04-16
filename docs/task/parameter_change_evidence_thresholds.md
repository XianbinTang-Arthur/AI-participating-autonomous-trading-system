# 参数修改证据门槛清单

## 1. 文档目标

本文档定义当前盈利整改阶段下，哪些参数：

1. **现在不能动**
2. **只能做保护性收紧**
3. **必须等满足样本条件后才能改**

本清单的核心目的不是限制优化，而是避免两类错误：

- 在逻辑语义仍然错误时，用参数硬压出“表面改善”
- 在真实样本不足时，拿最近几笔交易结果过拟合 live 参数

## 1.1 与其他文档的关系

本文档是三份材料中的第三层：

- [D:\文件\project\AIParticipatingAutonomousTradingSystem\docs\task\profitability_driven_priority_list.md](D:/文件/project/AIParticipatingAutonomousTradingSystem/docs/task/profitability_driven_priority_list.md)
  - 定义净期望优先级
- [D:\文件\project\AIParticipatingAutonomousTradingSystem\docs\task\profitability_driven_remediation_roadmap.md](D:/文件/project/AIParticipatingAutonomousTradingSystem/docs/task/profitability_driven_remediation_roadmap.md)
  - 定义阶段执行顺序
- 本文档
  - 定义 live 参数的允许动作边界

本文件不决定修复顺序，只决定：

- 这个参数当前能不能动
- 能动的话，只能保护性收紧，还是可以在样本达标后做长期校准

如果路线图与本文件冲突，以本文件为准。

## 2. 基本原则

### 2.1 先修逻辑，再调长期参数

以下问题在当前阶段已被确认属于 **逻辑或语义问题**，不是“参数最优值”问题：

- entry expectancy 明显高估
- `expected_cost_bps` 更像单边成本，不是全生命周期成本
- 退出链会把残仓切成很多低质量小额 taker 子单
- execution health 会把残仓退出继续算成坏 churn
- 前端盈利口径混用

在这些问题未修前，继续大幅修改长期盈利参数，会把逻辑错误误伪装成“参数优化成功”。

### 2.2 样本单位必须是“真实生命周期”，不是 fill

后续任何参数调整，默认都要用：

- **真实仓位生命周期**
- 不是 `fill`
- 不是 `child order`
- 不是单个 `close outcome`

否则会把残仓碎单误当成独立交易样本，污染阈值判断。

### 2.3 主验收口径必须是账单口径

参数是否值得改，最终只能看：

- `combined_net_realized_pnl`
- `fee_drag_ratio`
- `churn_ratio`
- `gross-to-net capture ratio`

不能只看：

- 胜率
- 毛收益
- 单个 fill 的 realized
- 某个页面上“看起来盈利”的局部值

## 3. A 类：现在不能动

这类参数在当前阶段 **禁止调整**。原因不是它们永远不能调，而是：

- 当前逻辑缺陷尚未修复
- 现在改只会掩盖问题
- 改完后得到的“改善”没有证据价值

### A1. Execution health guard 阈值

**现在不能动的参数**

- `strategy_max_fee_drag_ratio`
- `strategy_max_churn_ratio`
- `strategy_low_edge_threshold_bps`
- `strategy_low_edge_streak_limit`

**为什么现在不能动**

当前已经确认：

- health guard 既有真实依据
- 也会把“退出中的残仓小额子单”继续算成坏 churn

所以这组参数目前处在“指标本身和退出语义缠在一起”的状态。  
此时直接放宽阈值，只会得到一个假象：

- 不是系统更健康了
- 而是 guard 被调钝了

**解锁条件**

- 已完成残仓退出降敏
- 已把“新开仓后的坏 churn”和“退出中的残仓清理”分离记账

### A2. 失败 thesis / 去风险阈值

**现在不能动的参数**

- `strategy_hedge_independent_failed_thesis_net_edge_bps`
- `strategy_hedge_independent_de_risk_net_edge_bps`

**为什么现在不能动**

当前确认：

- 前半段退出主要是 thesis/edge 衰减
- 后半段很多残仓退出已被 execution health 接管

也就是说，这两个阈值现在并不是在一个“干净的纯策略退出环境”里工作。  
直接调它们，很容易把：

- 真实的 thesis failure
- 和被 health guard 放大的残仓退出

混成同一个问题。

**解锁条件**

- 已拆开 thesis failure 与 protection exit 语义
- 已确认 execution health 不再在残仓阶段自强化

### A3. 持有/冷却时间参数

**现在不能动的参数**

- `strategy_hedge_independent_long_min_hold_seconds`
- `strategy_hedge_independent_short_min_hold_seconds`
- `strategy_hedge_independent_rebalance_cooldown_seconds`
- `strategy_hedge_independent_max_thesis_age_seconds`

**为什么现在不能动**

当前我们已经确认：

- 真实 thesis 半衰期更像 `5~7 分钟`
- 但最终生命周期常常是 `17~18 分钟`
- 后半段大部分是在退出，不是在持有有效 thesis

这意味着当前看到的 holding time 是“假长”。  
此时调这些时间参数，风险很大：

- 调短，可能只是让退出更快，但没有改善开仓质量
- 调长，可能只是把错误仓位拖更久

**解锁条件**

- 已完成 entry timing 复盘
- 已确认当前 15m 路径到底是“天然短半衰期”还是“entry 偏晚”

## 4. B 类：现在只能做保护性收紧

这类参数当前可以改，但只允许朝 **更保守、更少亏损** 的方向改，不能宣称“已调到最优”。

### B1. `independent / 15m` 开仓相关门槛

**现在只能保护性收紧的参数**

- `strategy_hedge_independent_long_entry_threshold`
- `strategy_hedge_independent_short_entry_threshold`
- `strategy_hedge_independent_long_scale_in_threshold`
- `strategy_hedge_independent_short_scale_in_threshold`
- `hedge_independent_min_safe_net_edge_bps`

**为什么可以收紧**

我们已经有足够证据证明：

- 当前 entry expectancy 高估
- 当前成本判断低估 full lifecycle cost
- 最近实际综合净收益接近零或为负

所以：

- 提高 entry / scale-in / safe-edge 门槛
- 会减少低质量新风险进入
- 属于止血，不属于过拟合

**当前不允许的动作**

- 放宽这些门槛
- 因为当前没有任何证据支持“应该做更多单”

**与路线图的对应关系**

- 对应路线图中的：
  - `P0-1`
  - `P0-4`
- 这些动作在当前阶段都只能解释为“止血”和“保护性收紧”，不能解释为“长期最优参数已经确定”

### B2. 独立策略实时暴露开关

**现在只能保护性收紧的参数/控制**

- `independent / 15m` 的 live enablement
- 对应 family / timeframe 的开仓放开程度

**为什么可以收紧**

当前这条路径已经被确认是最主要的负期望来源之一。  
在 expectancy 和 lifecycle cost 没修完前，关小暴露是为了停止继续产生坏样本。

**当前不允许的动作**

- 增加该路径实盘暴露
- 放开更多 symbol / timeframe / leverage

### B3. 残仓最小经济动作门槛（若以参数形式落地）

**现在只能保护性收紧的参数**

- 未来若引入“最小残仓退出名义金额”
- 或“最小经济动作阈值”
- 只允许设得更保守，不允许放宽

**为什么**

当前已经确认：

- 真正的 size 问题不在主仓位 `~470 USDT`
- 而在残仓被切成 `7~22 USDT` 的小额 taker 退出

如果这个门槛以后做成参数，当前阶段只允许往“减少碎片化退出”的方向调。

## 5. C 类：必须等满足样本条件后才能改

这类参数不是“现在不能动”，而是 **必须先满足证据门槛，才允许调最终值**。

### C1. Entry / scale-in / reversal 的最终长期取值

**参数**

- `strategy_hedge_independent_long_entry_threshold`
- `strategy_hedge_independent_short_entry_threshold`
- `strategy_hedge_independent_long_scale_in_threshold`
- `strategy_hedge_independent_short_scale_in_threshold`
- 任何未来引入的 reversal threshold

**必须满足的样本条件**

至少同时满足：

1. 逻辑前提已满足
   - entry expectancy 已校准
   - lifecycle cost gate 已上线
2. 样本量
   - 至少 `60` 个真实完整生命周期
3. 方向覆盖
   - 至少 `20` 个 long lifecycle
   - 至少 `20` 个 short lifecycle
4. 时间覆盖
   - 至少覆盖 `7` 个自然日
5. 市场状态覆盖
   - 至少覆盖 `2` 种明显不同 regime
6. 验收口径
   - `combined_net_realized_pnl`
   - `gross-to-net capture ratio`
   - `expected_net_edge_bps vs actual gross bps`

### C2. Close / failed thesis / de-risk 阈值

**参数**

- `strategy_hedge_independent_long_close_threshold`
- `strategy_hedge_independent_short_close_threshold`
- `strategy_hedge_independent_failed_thesis_net_edge_bps`
- `strategy_hedge_independent_de_risk_net_edge_bps`

**必须满足的样本条件**

至少同时满足：

1. 已完成 thesis failure 与 protection exit 拆分
2. 已完成 execution health 对残仓退出降敏
3. 至少 `40` 个完整 lifecycle，且退出链不再主要由残仓碎片噪音主导
4. 已能对最近样本清楚分类：
   - 正常 thesis exit
   - failed thesis
   - de-risk
   - execution/liquidity protection
5. 有 1m / 3m / 5m / 10m 的 edge decay 轨迹

### C3. Min hold / cooldown / thesis age

**参数**

- `strategy_hedge_independent_long_min_hold_seconds`
- `strategy_hedge_independent_short_min_hold_seconds`
- `strategy_hedge_independent_rebalance_cooldown_seconds`
- `strategy_hedge_independent_max_thesis_age_seconds`

**必须满足的样本条件**

至少同时满足：

1. 已完成 entry timing 分析
2. 已明确：
   - 信号天然短半衰期
   - 或 entry 时点偏晚
3. 至少 `50` 个完整 lifecycle，可观测：
   - 开仓到首次 thesis failure 的时间
   - 开仓到完整退出的时间
   - 不同持仓时间下的 `combined_net_realized_pnl`
4. 已确认调整时间参数不会只是把“错误仓位持有更久”或“更快切碎退出”

### C4. Execution health 阈值

**参数**

- `strategy_max_fee_drag_ratio`
- `strategy_max_churn_ratio`
- `strategy_low_edge_threshold_bps`
- `strategy_low_edge_streak_limit`

**必须满足的样本条件**

至少同时满足：

1. health 公式已经把“退出中的残仓”与“新开仓后的坏 churn”区分开
2. 至少 `80` 个 closed outcomes 的新口径样本
3. 至少 `20` 个明确被 health guard 触发的 case
4. 能证明：
   - 放宽不会重新放出明显负期望路径
   - 收紧不会把正常退出误杀成 blocked/degraded

### C5. Execution mode / maker-taker 相关参数

**参数**

- `hedge_independent_entry_execution_mode`
- 任何 passive / taker fallback 相关参数
- 任何 maker/taker 选择相关阈值

**必须满足的样本条件**

至少同时满足：

1. 已完成 entry timing 与执行模式匹配分析
2. 已明确当前 passive-first 在 live 里是否只是名义存在、实际总落到 taker fallback
3. 至少 `30` 个真实入场样本，可对比：
   - passive 尝试
   - 实际成交角色
   - 最终 gross/net capture
4. 能证明改动后不会只是降低成交率，而是会改善 `combined_net_realized_pnl`

## 6. 证据门槛的统一格式

以后任何 live 参数调整，都建议在变更单里强制写清楚以下字段：

1. **参数名**
2. **当前值**
3. **目标值**
4. **参数类别**
   - A：现在不能动
   - B：只能保护性收紧
   - C：需满足样本条件后才能改
5. **样本单位**
   - lifecycle / fill / outcome
6. **样本窗口**
   - 最近几天
   - 覆盖几个 regime
7. **核心证据**
   - expectancy 偏差
   - fee drag
   - churn
   - combined net PnL
8. **风险说明**
   - 这次调整是在修逻辑污染
   - 还是在做长期最优校准

没有这 8 项，就不应该改 live 参数。

## 7. 当前阶段的执行建议

基于目前的证据，当前建议如下：

- **现在不要动**
  - execution health 阈值
  - failed thesis / de-risk 阈值
  - min hold / cooldown / max thesis age

- **现在可以做的，且只允许保护性收紧**
  - `independent / 15m` 的开仓与加仓门槛
  - `min_safe_net_edge_bps`
  - 该路径的实时暴露强度
  - 未来若引入残仓最小经济动作参数，只允许先往更保守方向设

- **必须等样本条件满足后再改**
  - entry / scale-in / reversal 的最终长期取值
  - close / failed thesis / de-risk 的最终阈值
  - hold / cooldown / thesis age
  - execution health 阈值
  - maker/taker / passive-first 相关执行参数

## 8. 当前阶段的操作解释规则

为避免后续误解，当前阶段统一按以下规则解释参数动作：

1. **逻辑修复**
   - 不属于“调参”
   - 例如：全生命周期成本认知、残仓退出降敏、盈利口径统一

2. **保护性收紧**
   - 允许在样本不足时执行
   - 目标是止血，不是宣布长期最优
   - 只能往更保守方向移动

3. **长期参数校准**
   - 必须满足本文样本门槛
   - 必须使用真实 lifecycle 样本
   - 必须给出预期 vs 实际的稳定统计证据

## 9. 最终判断标准

我们修改参数的目标，不是“让系统看起来更稳”，而是：

- 在真实 live execution 条件下
- 用真实 lifecycle 样本
- 让 `combined_net_realized_pnl`、`fee_drag_ratio`、`churn_ratio`、`gross-to-net capture ratio`
- 向稳定正期望的方向移动

如果改参数后只是：

- 交易变少了
- 页面更好看了
- 但真实综合净收益没有改善

那这次参数变更就不应被视为成功。
