# Profitability Diagnostic Evidence Foundation SoW

## Scope

本批次属于 **为 P0/P1 提供证据底座的前置诊断批次**，不属于一般意义上的 P0 止血批次，也不改变路线图中 lifecycle 归因与前端口径统一整体仍归属 P2 的事实。

本批次只实现两项诊断与可解释性增强，不做参数优化，不改变交易决策逻辑：

1. lifecycle 级净收益归因
2. `failed_thesis / de_risk / execution_health` 退出链可解释化

## Relationship To Existing Documents

本文档是以下材料的实施化落地补充：

- [D:\文件\project\AIParticipatingAutonomousTradingSystem\docs\task\profitability_driven_priority_list.md](D:/文件/project/AIParticipatingAutonomousTradingSystem/docs/task/profitability_driven_priority_list.md)
  - 定义净期望导向的优先级
- [D:\文件\project\AIParticipatingAutonomousTradingSystem\docs\task\profitability_driven_remediation_roadmap.md](D:/文件/project/AIParticipatingAutonomousTradingSystem/docs/task/profitability_driven_remediation_roadmap.md)
  - 定义 P0/P1/P2 的整改顺序
- [D:\文件\project\AIParticipatingAutonomousTradingSystem\docs\task\parameter_change_evidence_thresholds.md](D:/文件/project/AIParticipatingAutonomousTradingSystem/docs/task/parameter_change_evidence_thresholds.md)
  - 定义哪些 live 参数当前不能动、只能保护性收紧、或必须满足证据门槛后才能改

本批次的作用不是直接提高收益，而是为后续：

- P0 第一批逻辑修复的 live 验证
- 参数修改证据沉淀
- 生命周期级盈利归因

提供统一、可复核、可对照账单的证据底座。

本文档的前置实施资格仅限于：

- 不修改交易逻辑
- 不修改 live 参数
- 不修改主视图默认账单口径
- 不把展示统一伪装成 P0 止血动作

## Objectives

- 让系统可以直接回答：一笔真实仓位生命周期最终赚了多少钱，钱损失在什么环节。
- 让系统可以直接回答：一笔仓位为什么会从 `opening` 走到 `failed_thesis`，再走到 `de_risk / execution_health_degraded`。
- 让后端和诊断详情能够围绕“整笔仓位最终综合净收益”组织，而不是继续让用户从 fills 和 child orders 手工拼接。
- 为后续任何参数调整提供 lifecycle 级可解释证据，而不是继续依赖局部 realised、单笔委托盈利或印象判断。

## Non-goals

- 不修改开仓/平仓/减仓阈值
- 不修改 `min_hold / cooldown / thesis_age`
- 不修改 signal / expectancy / health guard 公式
- 不修改 live 配置
- 不改变当前交易执行逻辑

## Design

### 1. Lifecycle 级净收益归因

#### 1.1 目标语义

系统中的“盈利分析最小单位”统一为 **真实仓位生命周期**，而不是：

- fill
- child order
- close outcome
- 单个 decision

每个 lifecycle 必须直接给出：

- 毛收益
- 开仓手续费
- 平仓手续费
- funding
- 交易净收益
- 综合净收益
- child order 数量
- 退出原因分布

#### 1.2 后端范围

优先在现有 operator 查询层扩展，不优先新造孤立模块：

- [D:\文件\project\AIParticipatingAutonomousTradingSystem\aats\services\operator\report_queries.py](D:/文件/project/AIParticipatingAutonomousTradingSystem/aats/services/operator/report_queries.py)
- [D:\文件\project\AIParticipatingAutonomousTradingSystem\aats\services\operator\query_service.py](D:/文件/project/AIParticipatingAutonomousTradingSystem/aats/services/operator/query_service.py)

如现有函数已明显超载，可新增一个 attribution helper，例如：

- `aats/services/operator/lifecycle_attribution.py`

#### 1.3 必须新增或统一的 lifecycle 字段

每个 lifecycle 至少返回：

- `lifecycle_id`
- `family`
- `timeframe`
- `symbol`
- `direction`
- `opened_at`
- `closed_at`
- `hold_seconds`
- `entry_fill_count`
- `exit_fill_count`
- `child_order_count`
- `entry_notional_quote`
- `exit_notional_quote`
- `gross_realized_pnl`
- `entry_fee_quote`
- `exit_fee_quote`
- `total_fee_quote`
- `funding_fee_quote`
- `net_realized_pnl`
- `combined_net_realized_pnl`
- `gross_to_net_capture_ratio`
- `exit_reason_breakdown`
- `exit_intent_breakdown`

#### 1.4 计算原则

- lifecycle 必须按真实仓位归并，不允许前端自行拼 fill。
- funding 必须并入最终 `combined_net_realized_pnl`。
- 开仓费和平仓费必须拆开。
- `combined_net_realized_pnl` 作为主诊断口径。
- 如果某个数据源只能提供近似值，必须明确在 payload 中标识其语义，不允许静默混入主口径。

### 2. 退出链可解释化

#### 2.1 目标语义

系统必须能直接解释一笔 lifecycle 的退出链：

- 第一次进入退出链是什么原因
- 后续每次 `reduce / close` 的驱动因素是什么
- 当时关键阈值和运行态指标是多少
- 这是策略性退出，还是保护性退出

#### 2.2 后端范围

优先扩现有 decision / operator 查询面：

- [D:\文件\project\AIParticipatingAutonomousTradingSystem\aats\services\strategy_engines\independent\lifecycle.py](D:/文件/project/AIParticipatingAutonomousTradingSystem/aats/services/strategy_engines/independent/lifecycle.py)
- [D:\文件\project\AIParticipatingAutonomousTradingSystem\aats\services\strategy_engines\independent\engine.py](D:/文件/project/AIParticipatingAutonomousTradingSystem/aats/services/strategy_engines/independent/engine.py)
- [D:\文件\project\AIParticipatingAutonomousTradingSystem\aats\services\operator\query_service.py](D:/文件/project/AIParticipatingAutonomousTradingSystem/aats/services/operator/query_service.py)

#### 2.3 decision trace 必须包含的字段

每个退出相关 decision 至少返回：

- `decision_id`
- `timestamp`
- `book_state`
- `book_action`
- `close_reason`
- `transition_category`
- `expected_signal_edge_bps`
- `expected_cost_bps`
- `expected_lifecycle_cost_bps`
- `expected_net_edge_bps`
- `expected_lifecycle_net_edge_bps`
- `liquidity_quality_score`
- `execution_health_state`
- `fee_drag_ratio`
- `guard_eligible_fee_drag_ratio`
- `churn_ratio`
- `guard_eligible_churn_ratio`
- `low_edge_streak`
- `guard_eligible_low_edge_streak`
- `position_qty_before`
- `position_qty_after`
- `close_notional_quote`
- `residual_notional_quote`

#### 2.4 transition_category 约束

`transition_category` 至少支持以下语义：

- `strategy_exit`
- `protective_exit`
- `execution_guard_exit`

并满足：

- `failed_thesis` 不能再只是一个字符串，而要能被稳定归入某一类退出语义
- `weak_edge_de_risk`
- `execution_health_degraded`
- `liquidity_degraded`

必须能够在 trace 中被区分

### 3. API 形态

#### 3.1 优先方案

优先扩现有 lifecycle/report 查询，不急于拆很多新接口。

建议：

- 扩展现有 lifecycle profitability/report payload
- 补一个单 lifecycle 详情接口，例如：
  - `GET /reports/position-lifecycle-attribution`
  - `GET /reports/position-lifecycle-attribution/{lifecycle_id}`

#### 3.2 单 lifecycle 详情返回

单 lifecycle 详情至少包含：

- lifecycle summary
- child fills
- decision trace
- exit reason breakdown
- 关键指标时间线

### 4. 前端诊断承接

#### 4.1 目标

让用户在诊断详情里一眼能看到：

- 这笔仓位最后到底赚没赚
- 钱死在哪
- 为什么会被切成这么多单
- 谁在主导退出

#### 4.2 建议落点

优先使用诊断详情承接，不要求修改主策略/执行主视图的默认口径。

如确有前端承接需要，优先考虑：

- [D:\文件\project\AIParticipatingAutonomousTradingSystem\aats\api\static\modules\views\execution-view.js](D:/文件/project/AIParticipatingAutonomousTradingSystem/aats/api/static/modules/views/execution-view.js)
- [D:\文件\project\AIParticipatingAutonomousTradingSystem\aats\api\static\modules\trade-display.js](D:/文件/project/AIParticipatingAutonomousTradingSystem/aats/api/static/modules/trade-display.js)

只有在不改变主视图默认账单口径的前提下，才允许局部详情视图接入。

#### 4.3 展示层次

先做两层，不一次性铺太大：

1. lifecycle summary 详情卡片
   - 综合净收益
   - 毛收益
   - 手续费
   - funding
   - 持仓时长
   - child order 数
   - 退出原因分布

2. exit chain 诊断时间线
   - 开仓
   - 首次 `failed_thesis`
   - 后续 `de_risk`
   - `execution_health_degraded`
   - 最终 `close`

#### 4.4 文案与口径要求

- 诊断详情口径统一使用“综合净收益”
- 必须明确标注：
  - 这是整笔仓位口径
  - 不是单笔委托口径
- 所有前端文字必须为干净 UTF-8 中文
- 本批次不要求修改主策略视图、主风险视图、主执行视图的默认主值口径

## Testing

### 1. 后端单测

至少补齐：

- lifecycle attribution 聚合正确性
- `entry_fee / exit_fee / funding / combined_net` 分拆正确性
- `exit_reason_breakdown` 正确性
- `transition_category` 正确性

### 2. 集成测试

最窄路径至少覆盖：

- 一笔包含 `open + 多次 reduce + close` 的 lifecycle
- attribution 接口返回完整字段
- decision trace 顺序正确
- 退出原因 breakdown 正确

### 3. 前端测试

最窄 UI 测试至少覆盖：

- lifecycle summary 详情主值显示综合净收益
- exit chain 时间线文案为 UTF-8 中文
- 明确标示“整笔仓位口径”而非“单笔委托口径”

## Validation

- `.\.venv\Scripts\python.exe -m ruff check aats/ --fix`
- `.\.venv\Scripts\python.exe -m pytest tests/unit/ -x -q`
- WSL2 中运行本批次影响到的最窄 integration tests

## Acceptance

- 任意一笔 lifecycle 都能直接解释其 `combined_net_realized_pnl` 由哪些部分构成
- 用户无需手工拼 fills，也能看懂这笔仓位为什么最后只赚了几美分，或为什么亏损
- `failed_thesis / de_risk / execution_health_degraded / liquidity_degraded` 在退出链中具备明确可解释语义
- 若提供前端诊断承接，详情视图不再把单笔委托收益误导成整笔仓位最终结果

## Boundary

本批次严格限制为“诊断与可解释化增强”，不包含：

- 交易阈值调整
- 策略参数优化
- live 配置修改
- execution mode 调整
- signal / gate / health guard 公式修改
- 主视图默认账单口径统一

如果实施过程中发现必须改上述任一逻辑，必须先单独开设计/SoW，不允许在本批次中静默带入。
