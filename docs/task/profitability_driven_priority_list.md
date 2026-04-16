# 盈利导向整改优先级清单

## 1. 文档目标

本文档只回答一个问题：

> 如果目标是尽快把系统拉回到“可验证的正净期望”，当前最应该先修什么，后修什么？

本清单只按 **对净期望的影响** 排序，不按实现难度、UI 体验或工程完备度排序。

## 2. 与其他文档的关系

本文档是三份材料中的第一层：

- 本文档回答：**什么问题最先处理**
- [D:\文件\project\AIParticipatingAutonomousTradingSystem\docs\task\profitability_driven_remediation_roadmap.md](D:/文件/project/AIParticipatingAutonomousTradingSystem/docs/task/profitability_driven_remediation_roadmap.md) 回答：**按什么阶段和顺序执行**
- [D:\文件\project\AIParticipatingAutonomousTradingSystem\docs\task\parameter_change_evidence_thresholds.md](D:/文件/project/AIParticipatingAutonomousTradingSystem/docs/task/parameter_change_evidence_thresholds.md) 回答：**哪些 live 参数现在不能动、哪些只能保护性收紧、哪些必须等样本达标后再改**

约束关系如下：

1. **优先级清单决定“先看什么”**
2. **整改路线图决定“先做什么、后做什么”**
3. **参数证据门槛决定“现在能不能动 live 参数”**

如果三者出现冲突，以以下顺序为准：

1. 参数证据门槛
2. 整改路线图
3. 优先级清单

原因很简单：即使某件事优先级再高，如果证据门槛不允许直接动 live 参数，就必须先修逻辑、限流、补样本，再调参。

## 3. 当前高层结论

围绕最近一轮 `independent / 15m` 实盘分析，当前最重要的事实如下：

- 最近 24 小时实际成交几乎全部来自 `independent / 15m`
- 真实执行几乎全是 `taker`
- 毛收益为正，但手续费更大，综合净收益接近零或略为负
- entry 侧 `expected_net_edge_bps` 明显高估
- `expected_cost_bps` 更像 entry-side 单边成本，不是全生命周期成本
- 真实 thesis 半衰期很短，后半段生命周期主要是在退出，不是在持有有效 thesis
- 退出链并非正常 scale-out，而是 `failed_thesis / de_risk / execution_health_degraded` 主导
- execution health 既有真实依据，也会在残仓阶段被小额退出进一步喂坏
- 前端确实存在盈利口径混用，但这不是净亏损主因

一句话总结：

> 当前主问题不是“系统不能交易”，也不是“账单算错”，而是 `independent / 15m` 的可兑现边际不足以覆盖真实全生命周期成本。

## 4. 排序原则

任何整改动作按以下顺序排序：

1. 是否直接影响真实 `combined_net_realized_pnl`
2. 是否会系统性污染后续样本
3. 是否会放大 fee drag / churn
4. 是否只是改善解释性或页面体验

因此：

- **先修逻辑语义和交易经济性**
- **再修状态机/执行联动**
- **最后再修展示口径和可解释性**

例外说明：

- 如果某项工作 **不修改交易逻辑、不修改 live 参数、也不修改主视图默认口径**
- 且它的唯一作用是为 P0/P1 提供 lifecycle attribution / decision trace 级证据底座
- 那么这类纯诊断能力可以前置实施
- 但这不改变“lifecycle 归因”和“前端账单口径统一”在一般语义上仍属于 P2 的定位

## 5. 优先级排序

### P0.1 先收紧或冻结当前负期望路径

**为什么排第一**

如果当前路径已经被确认接近负期望，继续放量只会继续烧手续费，并积累被错误逻辑污染的样本。

**对应动作类型**

- live 开仓限流
- 临时冻结新风险
- 不影响已有持仓安全退出

### P0.2 先修 entry expectancy 与实际毛边际的错配

**为什么排第二**

这是当前最核心的经济性问题。  
如果开仓时系统持续把 `22~36 bps` 错当成真实可兑现边际，而实际只拿到 `7~12 bps`，那么后面所有优化都只是补丁。

**对应动作类型**

- expectancy 校准
- 预期 vs 实际 gross bps 偏差审计

### P0.3 把开仓 gate 升级为 full lifecycle cost gate

**为什么排第三**

现在成本判断更像单边 entry cost，不是 round-trip cost。  
只要这层不修，系统就会持续放进“理论可做、实盘不赚钱”的单。

**对应动作类型**

- lifecycle-aware cost floor
- safe edge 重定义

### P0.4 提高最小净利润门槛

**为什么排第四**

当前门槛太贴近 fee floor。  
在真实 taker 执行环境下，边际不够厚的单，哪怕方向对，也很难在账单上留下正净收益。

**对应动作类型**

- 保护性收紧 entry / scale-in / safe-edge

### P0.5 给退出链加“最小经济动作门槛”

**为什么排第五**

主仓位 `~470 USDT` 不是根因，但残仓被切成 `7~22 USDT` 继续 taker 退出，会持续制造坏 churn 和 fee drag。

**对应动作类型**

- 最小残仓退出经济阈值
- 合并退出 / 一次性收敛 / 低频退出

### P1.1 拆开 thesis failure 与 protection exit

**为什么排在 P1 开头**

当前很多退出链已经把：

- thesis 坏了
- execution/liquidity 在自保

混在一起。  
不拆开，就很难知道接下来该修 signal、execution 还是 health guard。

### P1.2 让 execution health 对退出中的残仓降敏

**为什么重要**

当前 health guard 会把很多残仓退出继续算成坏 churn，形成自强化回路。  
这不是当前净亏损的第一根因，但确实在放大后半段损耗。

### P1.3 重审 entry timing

**为什么在 health guard 后**

必须先把退出链和 health guard 清干净，才能判断：

- 这条信号天然半衰期短
- 还是我们总在行情后段才进

否则 timing 分析会被碎片化退出污染。

### P1.4 让 execution mode 与策略半衰期匹配

**为什么在 timing 之后**

先搞清楚这条边际到底有多短，再决定它适不适合：

- passive-first
- maker 优先
- 还是应该降级到 shadow / 纸面验证

### P2.1 补 lifecycle 级盈利归因

**为什么放在 P2**

这对长期治理非常重要，但它不直接创造 alpha。  
它的作用是让调参、复盘和上线决策更可靠。

补充说明：

- 如果只是后端 attribution / trace 能力，用于给 P0/P1 提供证据底座
- 且不牵涉主视图默认口径调整
- 可以作为狭义例外前置执行
- 但其长期归属仍然是 P2

### P2.2 统一前端账单口径

**为什么不是更高优先级**

它能减少误判，但不能把负期望变成正期望。  
所以它属于重要的“认知矫正”，不是盈利根因修复。

### P2.3 统一策略 / 风控 / 执行的盈利看板语义

**为什么放最后**

这是把前面已经修好的交易经济性、状态机和归因结果，统一投影到运营层。  
它很重要，但前提是前面的经济问题已经开始收敛。

## 6. 当前不允许跳过的顺序

以下顺序当前不允许跳过：

1. 不能先做 UI 统一再去修 entry expectancy
2. 不能先放宽 health guard 再去修残仓退出语义
3. 不能在 lifecycle cost gate 未修前，直接做长期 threshold 优化
4. 不能在样本不足时，根据最近几笔交易直接改长期参数

## 7. 当前阶段的一句话指令

当前阶段最重要的不是“让系统看起来更顺”，而是：

> **先把负期望路径关小，先把交易经济性语义修对，再用更干净的真实样本去做长期参数优化。**
