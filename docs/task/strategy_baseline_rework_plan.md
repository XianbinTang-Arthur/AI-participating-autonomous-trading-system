# 策略改造方案书

## 1. 文档目标

本文档面向当前 `AIParticipatingAutonomousTradingSystem` 中实际运行的 `baseline_only` 策略，目标是把“能跑通”与“能交易”区分开，围绕以下问题建立一套可执行的改造与验证标准：

- 当前策略到底在做什么
- 当前决策链中哪些环节决定了高频磨损
- 哪些问题属于真实缺陷，哪些属于策略语义不合理
- 应当如何收紧开仓、加仓、反手、减仓、平仓逻辑
- 如何在不削弱风控阻断的前提下，提高交易经济性
- 如何验证改造确实减少了手续费型亏损，而不是仅仅减少交易次数

本文档既是策略评估结论，也是本轮代码改造与运行验证的执行依据。

## 2. 当前策略的真实决策链

当前系统实际运行的主链路是：

`MarketSnapshot -> FeatureSnapshot -> BaselineAssessment -> PositionTarget -> PolicyDecision -> RiskDecision -> ExecutionPlan -> OrderIntent -> OrderState -> FillEvent -> PortfolioSnapshot`

关键模块如下：

- 特征计算：
  [calculator.py](D:/文件/project/AIParticipatingAutonomousTradingSystem/aats/services/feature_engine/calculator.py)
- 基线方向判断：
  [baseline.py](D:/文件/project/AIParticipatingAutonomousTradingSystem/aats/services/decision_engine/baseline.py)
- 目标仓位与杠杆：
  [target_position.py](D:/文件/project/AIParticipatingAutonomousTradingSystem/aats/services/decision_engine/target_position.py)
- 决策触发节奏：
  [trigger_policy.py](D:/文件/project/AIParticipatingAutonomousTradingSystem/aats/services/decision_engine/trigger_policy.py)
- 决策编排：
  [orchestrator.py](D:/文件/project/AIParticipatingAutonomousTradingSystem/aats/services/decision_engine/orchestrator.py)
- 风控阻断：
  [risk.py](D:/文件/project/AIParticipatingAutonomousTradingSystem/aats/services/governance_engine/risk.py)
- 执行与同步：
  [order_manager.py](D:/文件/project/AIParticipatingAutonomousTradingSystem/aats/services/execution_engine/order_manager.py)
  [okx_adapter.py](D:/文件/project/AIParticipatingAutonomousTradingSystem/aats/services/execution_engine/okx_adapter.py)

### 2.1 特征层做了什么

`FeatureCalculator` 把多个方向相关因子压成一个 `composite_alpha_score`，同时给出：

- `regime_indicator`
- `regime_confidence`
- `trend_strength`
- `volatility_state`
- `suggested_position_scale`
- `volatility_target_scale`
- `microstructure_alpha`
- `multi_timeframe_alpha`

从代码看，它更像一个“市场状态分类器 + 风险过滤器”，而不是一个已经被严格校准过的短周期交易 alpha。

### 2.2 基线策略做了什么

`BaselineStrategy` 按 `regime` 采用不同阈值：

- `breakout` 最宽松
- `trend` 次之
- `range` 更严格
- `uncertain` 最保守

并使用 `microstructure_alpha` 做支持/冲突校正。

输出是：

- `direction_bias = long / short / flat`
- `confidence`
- `composite_alpha_score`
- `factor_scores`

这一步的优点是解释性好，缺点是语义比较粗。它只回答“现在更偏多、偏空还是中性”，并不回答“是否值得支付手续费和滑点去交易”。

### 2.3 目标仓位层做了什么

`TargetPositionEngine` 把 `direction_bias` 翻译成：

- 目标方向
- 目标仓位
- 目标杠杆
- 仓位意图，如 `open_long` / `reduce_long` / `close_short`

这里历史上存在一个最关键的问题：

- `flat` 直接映射为 `target_position_qty = 0`

对于衍生品，这意味着：

- “信号不够强”
- 被解释成
- “把已有仓位直接平掉”

这会让策略在阈值附近发生高频来回调整。

### 2.4 触发策略做了什么

当前系统不是等到 K 线收盘才唯一触发一次决策，它会基于：

- 最小时间间隔
- 每分钟最大决策次数
- 价格变动门槛
- 动量变化门槛
- 关键状态变更

进行决策。因此，若同时允许多个 timeframe 直接参与触发，就容易出现多频率叠加带来的重复决策。

## 3. 当前策略的优点

当前策略并非毫无价值，它具备几项真实优点：

- 不是单一指标策略，而是多因子组合
- 明确区分 `trend / breakout / range / uncertain`
- 有盘口微结构冲突修正
- 有波动率缩放与流动性缩放
- 有 staged scale-in 和 staged reversal 机制
- 已被 execution / reconciliation / risk / recovery 安全链路包住，不是裸奔策略

如果只从“是否具备基本量化策略结构”判断，它是及格的。

## 4. 当前策略的主要不足

### 4.1 `flat` 语义过于激进

这是当前第一大问题。

在 `baseline_only` 下，`flat` 被解释为“把仓位打回 0”，结果是：

- 阈值刚刚跌回中性带时就开始减仓/平仓
- 方向没有显著反转，只是信号减弱，也会触发退出
- 费用和滑点被动放大

这对高费率、以 taker 为主的永续合约尤其伤。

### 4.2 entry / hold / exit / reverse 没有分层阈值

当前 `BaselineStrategy` 只决定 `long / short / flat`，没有显式区分：

- 开仓需要多强证据
- 加仓需要多强证据
- 持仓需要多强证据
- 平仓需要多强反证据
- 反手需要多强反向证据

因此它天然缺乏迟滞带，容易出现：

- 刚刚进场
- 很快又减仓或平仓

### 4.3 缺少“净优势覆盖成本”的交易资格层

方向成立不等于值得交易。

当前策略历史上没有单独要求：

- 预估 alpha 边际
- 必须超过 taker fee
- 必须超过预期滑点
- 必须超过一个净边际缓冲

所以它会做很多“方向看起来有点道理，但经济上不值得付费”的交易。

### 4.4 决策节奏偏快，且多周期直接参与触发

如果 `15m` 和 `1h` 都直接触发决策，而不是让 `1h` 只作为辅助上下文，那么系统会在短时间内得到多个近似重复的 target。

这会放大：

- 重复决策
- 仓位微调
- 手续费型亏损

### 4.5 执行失败后的重复操作会污染策略表现

虽然 execution 层已经做了不少保护，但若策略层持续发出相同 close/reduce 意图，就会：

- 让日志看起来像策略很激进
- 实际上一部分只是执行失败后的重复操作噪声
- 干扰策略本身的收益评估

### 4.6 当前 alpha 更像资格过滤器，不像成熟的短线 alpha

`composite_alpha_score` 更适合回答：

- 市场现在是否允许做多/做空
- 风险是否足够低

不太适合直接回答：

- 这笔单现在值得用 taker 成本去做吗
- 这笔单的期望收益能否覆盖摩擦成本

## 5. 运行期观测到的核心问题

这部分不是理论推演，而是来自当前仓库和运行日志的真实现象。

### 5.1 手续费磨损明显

运行结果显示，单笔成交的已实现盈亏常常只有几美分到几毛美元，但手续费本身就已经很接近甚至超过这一级别。  
这说明策略的真实问题不是“总是完全错方向”，而是：

- 边际不够厚
- 交易太碎
- 成本门槛不够严

### 5.2 高频弱调整依然存在

即使已有“flat 持仓默认 hold”的第一轮修正，历史日志仍然表明策略会做这类行为：

- 先按较高 target 加到某个多头仓位
- 接着把 target 下修一点点
- 然后发出小幅 `reduce_long`

从交易角度看，这种仓位微调若没有显著优势，往往只是手续费再分配。

### 5.3 旧进程下仍有 `1h` 决策直接触发

在旧进程日志中仍可见：

- `timeframe=15m`
- `timeframe=1h`

都在直接触发 decision cycle。  
这说明配置未生效前，系统仍处于多周期直接下单状态。

## 6. 策略改造原则

改造必须满足以下约束：

- 不削弱原有风控阻断
- 不绕过 reconciliation / halt / kill switch
- 不把“减少交易次数”误当成“提升策略”
- 不把真实需要减仓/止损的情况挡掉
- 每一条新规则都应能从 decision payload 解释出来
- 优先修正交易语义，不先大改架构

## 7. 本轮改造方案

### 7.1 第一层：平掉最明显的经济性错误

#### 方案 A：`flat != 立即平仓`

规则：

- 仅对 `derivatives` 生效
- 仅对 `baseline_only / ai_advisory` 的 neutral 状态生效
- 若已有仓位且 `direction_bias == flat`
- 默认保持当前仓位
- 只有明确不利证据同时出现时才允许退出

显式退出证据使用：

- `microstructure_alpha`
- `momentum_alpha`
- `trend_alpha`
- `ai directional edge`

要求至少两个不利信号同时成立，或特别强的冲突组合成立。

#### 方案 B：交易资格门

对以下行为增加更严格资格要求：

- 开新仓
- 同向加仓
- 真正反手

新增限制：

- 只允许在 `trend / breakout` 开新方向
- `entry`、`scale_in`、`reversal` 使用不同 `alpha` / `confidence` 最小门槛
- 若强度不足，保持现有仓位，不生成新的增风险 target

这一步的目的不是“预测更准”，而是把噪声交易挡在交易层之前。

#### 方案 C：成本门槛

要求开仓/加仓/反手的边际至少覆盖：

- taker fee
- 预期滑点
- 额外净收益缓冲

本轮采用保守代理：

- `signal_edge_proxy`
- `expected_cost_bps`
- `required_net_edge_bps`

只要边际不够，就不加新风险。

#### 方案 D：相同 close 重试冷却

若近期同一 close intent 因已知瞬时交易所错误失败，则在短时间内阻止同类、同尺寸、非高紧急度 close 重试，降低执行噪声。

### 7.2 第二层：把决策节奏收紧成“主周期驱动”

交易层只允许 `15m` 直接触发下单。  
`1h` 只保留为上下文和趋势辅助，不再直接变成高频补刀。

同时收紧：

- 每分钟最大决策数
- 最小决策间隔
- 最小价格变动
- 最小动量变化

目标是减少“没有新信息却重复做同一决策”。

### 7.3 第三层：限制无意义的仓位微调

这是本轮之后如果仍不达预期的下一步重点。

需要进一步评估是否增加：

- 最小减仓变动阈值
- 持仓最小保持时间
- 成交后再入场冷却
- 同方向加仓冷却

如果第一轮重启后仍然出现频繁 `reduce_long / reduce_short`，下一轮就优先做这一层。

## 8. 参数设计建议

本轮建议的衍生品默认策略参数如下：

- `AATS_ENABLED_DECISION_TIMEFRAMES=["15m"]`
- `AATS_MAX_DECISIONS_PER_MINUTE=3`
- `AATS_DECISION_MIN_INTERVAL_SECONDS_15M=45`
- `AATS_DECISION_MIN_INTERVAL_SECONDS_1H=180`
- `AATS_DECISION_MIN_PRICE_MOVE_BPS=4`
- `AATS_DECISION_MIN_MOMENTUM_DELTA=0.0004`
- `AATS_STRATEGY_MIN_NET_EDGE_BPS=8`
- `AATS_STRATEGY_EXPECTED_SLIPPAGE_BPS_FRACTION=0.35`
- `AATS_STRATEGY_ENTRY_ALLOWED_REGIMES=["trend","breakout"]`
- `AATS_STRATEGY_ENTRY_ALPHA_MIN=0.18`
- `AATS_STRATEGY_ENTRY_CONFIDENCE_MIN=0.62`
- `AATS_STRATEGY_SCALE_IN_ALPHA_MIN=0.24`
- `AATS_STRATEGY_SCALE_IN_CONFIDENCE_MIN=0.68`
- `AATS_STRATEGY_REVERSAL_ALPHA_MIN=0.30`
- `AATS_STRATEGY_REVERSAL_CONFIDENCE_MIN=0.75`
- `AATS_STRATEGY_TRANSIENT_CLOSE_RETRY_COOLDOWN_SECONDS=120`

这些值不是“理论最优”，而是“先显著减少噪声交易，再保留真正强信号”的安全初始值。

## 9. 代码落地方向

### 9.1 已落地项

本轮已经或准备落地以下修改：

- `TargetPositionEngine` 增加衍生品 entry/scale-in/reversal 资格门
- `TargetPositionEngine` 保留原有风控阻断语义，只拦增风险路径
- `.env / .env.derivatives / .env.spot` 统一按分组整理与同步
- 运行配置调整为 `15m-only` 直接决策

### 9.2 视重启结果决定的候选二次改造

若重启后仍然表现出明显 fee churn，则优先追加：

- 成交后再入场冷却
- 同方向再次加仓冷却
- 最小有效减仓阈值
- 持仓时间感知的 exit 逻辑

## 10. 风控语义要求

下列语义必须在任何策略改造后继续保持：

- `halted` 仍然阻止提交
- `review_required` 仍然阻止提交
- `kill_switch_active` 仍然阻止提交
- reconciliation 异常不能被策略层“优化”掩盖
- 高紧急度减仓/平仓不能被成本门槛错误挡住
- 明确 adverse signal 的风险退出不能被“hold”语义吞掉

换句话说：

- 可以减少噪声交易
- 不能削弱真实风险退出

## 11. 测试计划

### 11.1 单元测试

至少覆盖：

- 非允许 regime 下不得新开仓
- 同向加仓要求比开仓更强
- 反手要求比开仓更强
- `flat` 持仓在弱 adverse 下应 hold
- `flat` 持仓在强 adverse 下应允许退出
- 成本门槛不能误挡风险减仓

### 11.2 集成测试

至少覆盖：

- guarded live 不回归
- guarded simulated 不回归
- risk blocking 不回归
- outbox / recovery / reconciliation 不回归

### 11.3 运行验证

上线后至少观察：

- 决策频率是否明显下降
- 是否只剩 `15m` 直接决策
- 同方向微幅开平是否显著减少
- close retry busy 错误是否下降
- `reconciliation` 是否仍保持 `CLEAN`
- 是否出现新的 `halted` 假阳性

## 12. 收益验证方法

不能只看“有没有盈利单”，必须看：

- fill 数量
- 手续费总额
- 净 realized PnL
- gross before fees
- 各 `position_intent` 的收益分布
- 平均持仓时长
- 单位边际收益是否提升

如果交易数少了但 gross alpha 也一起塌掉，需要重新判断是不是过度保守。

## 13. 失败标准

以下任一情况出现，都视为本轮策略改造未达预期：

- fill 数量明显下降，但净收益仍持续为负且 fee 占比未改善
- `reduce_long / reduce_short` 仍然大量出现且绝大多数为小额负收益
- 仍有多个 timeframe 直接驱动下单
- 风控阻断被意外放松
- `halted / reconciliation` 出现新的假阳性

## 14. 下一阶段路线

若第一轮仍未达到预期，后续应按以下顺序推进：

1. 成交后再入场冷却
2. 持仓最小保持时间
3. entry / hold / exit / reverse 四段阈值下沉到 `BaselineStrategy`
4. `SignalDecision` 与 `PositionManager` 分层
5. alpha-to-edge 经验校准

## 15. 最终预期

完成本轮改造后，策略不应再表现为：

- “只要有点想法就交易”
- “信号一弱就马上平仓”
- “多个周期叠加重复出手”

而应表现为：

- “只有强信号才开新风险”
- “中性噪声下默认持有，不做无意义仓位微调”
- “高成本环境下宁可少做，也不做净边际不足的交易”
- “保持现有风控和对账语义不被削弱”
