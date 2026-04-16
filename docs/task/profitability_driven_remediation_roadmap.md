# 盈利导向整改路线图

## 1. 文档目标

本文档把最近一轮围绕 `independent / 15m` 实盘表现的 8 项分析，整理成一份可执行的整改路线图。

目标不是“让系统看起来更稳”，而是按 **对净期望的影响** 排序，优先修复最直接影响盈利能力的问题：

- 预测边际是否被系统性高估
- 开仓成本是否被系统性低估
- 退出链路是否把本来就很薄的利润切碎
- 执行健康保护是否在残仓阶段过度放大坏 churn
- 前端是否混淆了账单口径，影响判断

本文档只定义整改顺序、执行动作、验收标准和阶段门槛；不直接修改代码，不替代后续具体设计文档。

## 1.1 与其他文档的关系

本文档是三份材料中的第二层：

- [D:\文件\project\AIParticipatingAutonomousTradingSystem\docs\task\profitability_driven_priority_list.md](D:/文件/project/AIParticipatingAutonomousTradingSystem/docs/task/profitability_driven_priority_list.md)
  - 回答：**先看什么问题**
- 本文档
  - 回答：**先做什么、后做什么**
- [D:\文件\project\AIParticipatingAutonomousTradingSystem\docs\task\parameter_change_evidence_thresholds.md](D:/文件/project/AIParticipatingAutonomousTradingSystem/docs/task/parameter_change_evidence_thresholds.md)
  - 回答：**哪些 live 参数现在能不能动**

必须明确：

- 本路线图允许先做 **逻辑修复、语义收口、保护性收紧**
- 本路线图 **不授权** 在样本不足时做长期参数优化
- 任何涉及 live 参数的动作，都必须同时满足参数证据门槛文档
- 如果某项工作只增强 **后端 attribution / decision trace 证据能力**
- 且不修改交易逻辑、不修改 live 参数、不修改主视图默认口径
- 可以作为“为 P0/P1 提供证据底座的诊断例外”前置执行
- 但这不改变 lifecycle 归因与前端账单口径统一在一般语义上仍归属 P2

## 2. 当前状态摘要

基于最近 24 小时实盘数据和对应 decision / fill / lifecycle / runtime 分析，当前最重要的已知事实如下：

- 最近 24 小时实际成交几乎全部来自 `independent / 15m`
- 最近 24 小时 fills 全部为 `taker`
- 经济上可还原成 2 笔真实仓位生命周期（1 笔 long，1 笔 short）
- 最近 24 小时整体结果：
  - 毛收益：`+0.90754 USDT`
  - 手续费：`-0.94164805 USDT`
  - 交易净收益：`-0.03410805 USDT`
  - 资金费：`+0.000773178438847 USDT`
  - 综合净收益：`-0.033334871561153 USDT`
- 开仓时 `expected_net_edge_bps` 明显高于实际可兑现毛边际：
  - 开仓时预期约 `22~36 bps`
  - 实际只兑现出 `7~12 bps` 毛边际
- 当前 `expected_cost_bps` 更像 entry-side 单边成本，不是 round-trip 成本
- 最近真实退出链并非正常 scale-out，而是：
  - `failed_thesis`
  - `de_risk`
  - `execution_health_degraded`
  - `liquidity_degraded`
 共同主导
- 真实 thesis 半衰期约 `5~7 分钟`
- 最终 `17~18 分钟` 的生命周期大部分是在退出，不是在持有有效 thesis
- `execution health` 既有真实依据，也会在残仓阶段被小额退出进一步喂坏
- 前端存在 `gross / net / combined net / account realized` 口径混用，但这不是负收益主因

一句话概括当前状态：

> 当前系统已经具备完整交易工程链路，但 `independent / 15m` 这条实盘路径的真实可兑现净边际不足以覆盖真实全生命周期成本，因此目前更像“工程完整但经济性不足”的系统，而不是稳定盈利系统。

## 3. 整改原则

整改过程必须遵守以下原则：

1. 只按 **净期望提升优先级** 排序，不按实现难度排序。
2. 优先修正 **entry expectancy / lifecycle cost / exit economics**，而不是先修 UI。
3. 不牺牲现有 risk / reconciliation / recovery / kill switch 语义。
4. 不把“减少交易次数”误当成成功，必须看 `combined_net_realized_pnl` 是否改善。
5. 不把“有盈利单”误当成成功，必须看：
   - fee drag
   - churn
   - gross-to-net capture ratio
   - 每笔真实生命周期的综合净收益
6. 所有阶段都需要有明确的“继续 / 暂停 / 回滚 / 放开”条件。

## 4. 阶段划分总览

### P0：立刻处理

目标：先阻止负期望路径继续放大，并把 entry 经济判断收紧到接近真实实盘条件。

说明：

- P0 中允许出现 **保护性收紧**
- P0 中不允许把 live 参数调优伪装成“长期最优校准”

### P1：紧接着处理

目标：修复退出链和 health guard 的自强化问题，防止本来就薄的边际被进一步切碎。

### P2：后续处理

目标：统一账单口径、补全归因和运营可解释性，减少误判，提高持续调参效率。

例外说明：

- P2 中的“lifecycle 级归因”和“退出链可解释化”默认仍属后续治理工作
- 但若某个批次只做后端 attribution / trace 能力，用于支撑 P0/P1 验证
- 且明确不改主视图默认账单口径
- 则允许以前置诊断批次方式先行实施

## 5. P0 路线图

### P0-1 收紧或临时冻结 `independent / 15m` 实盘开仓

**目标**

在 expectancy 和 full lifecycle cost 没有校准前，避免继续积累低质量实盘样本和手续费磨损。

**动作**

1. 先把 `independent / 15m` 的实盘开仓能力切到保守模式。
2. 如果无法只收紧而不引入新的不可解释行为，则直接临时冻结新开仓。
3. 保留已有持仓的正常退出与回收路径，不允许因为冻结新开仓而损害风控安全。

**验收标准**

- `independent / 15m` 新开仓显著下降或暂时归零
- 不影响既有持仓的 close / reduce / recovery 链路
- 风控、对账、恢复链路无回归

**放开条件**

- P0-2 和 P0-3 完成
- 小样本验证中 `combined_net_realized_pnl` 明显高于 0

### P0-2 重做 entry expectancy 校准

**目标**

让 `expected_net_edge_bps` 更接近真实可兑现毛边际，而不是继续系统性高估。

**动作**

1. 建立最近 7 天实盘样本的：
   - `expected_signal_edge_bps`
   - `expected_net_edge_bps`
   - 实际 `gross_realized_bps`
2. 统计不同方向、不同 regime 下的预期-实际偏差。
3. 明确是：
   - 信号预测过高
   - 成本扣减不足
   - 还是两者同时存在
4. 在修公式前，先定义一个保守校准层，避免继续把 `22~36 bps` 错当成真实可兑现边际。

**验收标准**

- 能给出一份预期 vs 实际的偏差分布
- 明确偏差主要来自哪一层
- 新校准后，最近样本里“预期与实际 gross bps 的偏差”明显收敛

### P0-3 把开仓 gate 升级为 full lifecycle cost gate

**目标**

开仓前就按全生命周期成本判断，而不是只按 entry-side 成本放行。

**动作**

1. 统一定义一个 conservative lifecycle cost floor，至少覆盖：
   - 开仓 fee
   - 平仓 fee
   - 现实滑点
   - 噪音 buffer
2. 把 open eligibility 的判断从“单边成本 + safe edge”改成“全生命周期成本 + 安全垫”。
3. 保留 size-aware 成本模型，但把它放到更保守的 lifecycle 语义下。

**验收标准**

- 新开仓判断不再只依赖 single-leg cost
- 实盘最近样本中“fee floor 附近的微弱边际单”明显减少
- 允许开仓的样本，其实际 gross bps 更明显高于 fee floor

### P0-4 提高 `independent / 15m` 的最小净利润门槛

**目标**

避免系统继续交易“刚刚过线但不够赚钱”的机会。

**动作**

1. 基于 P0-2 / P0-3 的结论重新定义最小净利润门槛。
2. 不再按“理论上能覆盖 entry cost”设门槛，而要按“明显高于 full lifecycle fee floor”设门槛。
3. 区分：
   - entry
   - scale-in
   - reversal
   的最小净利润要求。

**参数约束**

- 当前阶段只允许做 **保护性收紧**
- 不允许在样本不足时宣称这是长期最优值
- 最终长期取值必须回到参数证据门槛文档约束下重做

**验收标准**

- 新门槛显著高于当前 fee floor
- 样本中 `gross bps < fee bps` 的开仓减少
- 小样本验证中毛边际与综合净收益分布向上移动

### P0-5 给退出链加“最小经济动作门槛”

**目标**

阻止残仓被切成很多 `7~22 USDT` 的低质量 taker 子单。

**动作**

1. 明确残仓退出的最小经济动作阈值。
2. 对低于阈值的残仓，定义统一处理策略：
   - 一次性收敛
   - 合并处理
   - 进入低频退出模式
3. 使该规则只针对残仓阶段，不影响正常大额止损/强平/紧急风控。

**验收标准**

- `small_churn` 占比下降
- 平均 exit child order 数下降
- 残仓阶段 fee drag 明显下降

## 6. P1 路线图

### P1-1 拆开“信号失效”与“保护性退出”状态机

**目标**

避免 `failed_thesis` 与 `de_risk / execution_health_degraded` 混成一条自强化退出链。

**动作**

1. 显式区分：
   - thesis failure
   - execution protection
   - liquidity protection
2. 让退出原因和动作语义一一对应。
3. 在审计/运行态中能明确看出：
   - 这次退出是“信号坏了”
   - 还是“系统出于健康保护在收缩”

**验收标准**

- 决策链中 `failed_thesis` 与 `de_risk` 语义边界清晰
- 同一条退出链不再在多个不透明原因之间来回切换

### P1-2 让 execution health 对“退出中的残仓”降敏

**目标**

避免 health guard 把残仓清理继续算成坏 churn，形成自强化回路。

**动作**

1. 重新定义 `small_churn` 在退出链中的适用范围。
2. 区分：
   - 新开仓后的坏交易
   - 已进入退出链的残仓清理
3. 对后者降低惩罚强度或单独记账。

**验收标准**

- `fee_drag_ratio / churn_ratio` 不再因残仓小单继续被快速拉爆
- `execution_health_state` 不再在退出尾段持续恶化

### P1-3 重审 `independent / 15m` 的 entry timing

**目标**

判断问题到底是信号天然短半衰期，还是当前总在行情后段才进场。

**动作**

1. 对最近样本做：
   - 开仓后 1m / 3m / 5m / 10m 的毛边际演化
2. 判断真实 alpha 半衰期。
3. 识别当前 entry 是否总发生在：
   - alpha 已展开大半之后
   - 或 execution 条件已不适配的时候

**验收标准**

- 明确得到“半衰期短”还是“entry 晚”的主因
- 给出后续是修 signal 还是修 timing 的决定依据

### P1-4 让 execution mode 与策略半衰期重新匹配

**目标**

判断当前全 taker 执行是否天然不适合这条短半衰期路径。

**动作**

1. 分析当前 `passive_first` 与现实成交结果之间的偏差。
2. 明确：
   - 是不是实际总落到 taker fallback
   - maker/passive 是否在现实里根本没有兑现空间
3. 决定这条策略未来的执行定位：
   - 继续 live
   - 改执行模式
   - 只留作 shadow / 纸面验证

**验收标准**

- 明确这条路径是否适合当前实盘 execution style
- 不再继续把“短半衰期 + taker-only”当成默认可盈利组合

## 7. P2 路线图

### P2-1 补 lifecycle 级盈利归因

**目标**

让每一笔真实仓位生命周期都能直接回答：为什么最后只赚了这点钱，或者为什么亏了。

**动作**

1. 每笔 lifecycle 统一归因：
   - 毛收益
   - 开仓 fee
   - 平仓 fee
   - funding
   - child order 数
   - 退出原因分布
2. 建立可对照交易所账单的 operator 视图。

**验收标准**

- 任意一笔 lifecycle 都能解释净收益构成
- 不再依赖 fill 明细手工拼接才能看懂

### P2-2 统一前端主视图的“账单口径”

**目标**

减少“系统看着在盈利，交易所账单却在亏”的口径错觉。

**动作**

1. 定义主视图统一默认用：
   - `combined_net_realized_pnl`
2. 将：
   - `gross_realized_pnl`
   - `net_realized_pnl`
   - `funding_fee_net_pnl`
   降级为辅助信息
3. 明确标注哪些视图是：
   - per-fill
   - per-lifecycle
   - account-level

**验收标准**

- 主视图不再混合多个 PnL 口径
- 用户从 UI 得到的“最终是否赚钱”结论能与账单更一致

### P2-3 统一策略 / 风控 / 执行的盈利看板语义

**目标**

让全系统都围绕净期望工作，而不是不同模块各看一套“局部正确”的指标。

**动作**

1. 统一主要运营指标：
   - `combined_net_realized_pnl`
   - fee drag
   - churn
   - gross-to-net capture ratio
2. 让策略、风控、执行主看板口径一致。

**验收标准**

- 运营判断和策略优化基于同一套盈利指标
- 不再出现“某个局部页面看起来很好，但最终账单不赚钱”的治理错位

## 8. 阶段门槛

### 进入 P1 前

必须完成：

- P0-1 到 P0-5
- 最近一段验证中，`independent / 15m` 的综合净收益不再稳定为负
- 退出 child orders 数与 `small_churn` 占比出现可见下降
- 已明确 P0 中所有参数动作仅属保护性收紧，不作为长期定值结论

### 进入 P2 前

必须完成：

- P1-1 到 P1-4
- 已明确：
  - 这条策略是否适合继续 live
  - 还是应该降级到 shadow / 纸面验证
- 已确认后续若要改长期参数，必须切换到“参数证据门槛”流程，而不是继续按 P0/P1 的止血逻辑推进

## 9. 暂停 / 放开条件

### 需要立刻暂停继续放量的条件

- `combined_net_realized_pnl` 持续为负
- `fee_drag_ratio` 明显高于 guard 阈值
- `small_churn` 占比继续上升
- 退出链继续呈现大量微型残仓子单

### 可以考虑逐步放开的条件

- `expected vs actual gross bps` 偏差明显收敛
- 开仓判断已切换到 lifecycle-aware cost
- 最近样本中 `gross bps` 稳定高于 fee floor
- `execution health` 不再因残仓退出持续恶化
- 小样本 / 扩大样本都显示综合净收益稳定为正

## 10. 建议的执行顺序

建议严格按以下顺序推进：

1. 先关小坏路径：P0-1
2. 再修 entry 认知：P0-2、P0-3、P0-4
3. 再修退出经济性：P0-5
4. 然后修状态机和 health guard：P1-1、P1-2
5. 再判断这条策略还能不能 live：P1-3、P1-4
6. 最后统一归因和展示：P2-1、P2-2、P2-3

这条顺序不能倒过来。原因很简单：

> 如果 entry expectancy 和 lifecycle cost 没修，先做 UI 统一和归因，只会让我们更清楚地看到亏损；不会让系统更接近盈利。

## 11. 验收总标准

这份路线图最终的成功标准不是“系统更稳定”或“页面更清楚”，而是：

- 在真实 live execution 条件下，
- 对最近一段 `independent / 15m` 样本，
- `combined_net_realized_pnl` 稳定转正，
- 且不依赖偶然的大单或极小样本。

如果达不到这个标准，就不应把这条路径视为“已盈利”。
