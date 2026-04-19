# Task72-E 合约优先版第五批具体开发任务（自动调参二期）

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../../docs/project_positioning.md)。


## 1. 文档定位

这份文档承接 `Task72-D` 已完成的自动阻断 / 停机规则、启盘前自检和小资金运行包。

当前目标不是继续堆新的静态档位，而是把现有的策略档位体系升级成一套更像真钱运行系统的“自动调参二期”能力：系统不仅能根据市场状态切换信号阈值，还能在风险、执行质量和持仓节奏层面做结构化、可审计、可回滚的自动收缩与自动解释。

本批次只讨论“在既有现货 / 合约 live 架构上，如何把自动换档从信号层扩展到风险层和执行层”。  
不讨论多交易所、多账户、多 runtime，也不讨论高频型动态做市。

## 2. 当前批次范围

本批次包含五个高优先级任务：

1. `Task72-B11` 紧急安全换档快速通道 v1
2. `Task72-B12` 风险预算 multiplier 与自动收缩 v1
3. `Task72-B13` 执行侵略性 multiplier 与自动收缩 v1
4. `Task72-B14` 持有 / 冷却参数纳入策略档位 v1
5. `Task72-B15` 档位解释、阻断证据与 operator 面板收口 v1

这五个任务的共同目标是把“系统为什么更激进 / 更保守、为什么现在不该开仓、为什么切到了安全档”从隐含推断升级成代码里明确的自动控制层。

## 3. Task72-B11 紧急安全换档快速通道 v1

### 3.1 目标

- 把“常规优化换档”和“紧急风险收缩换档”分成两条不同通路
- 当执行质量、账户快照、对账状态或运行时安全明显恶化时，允许系统快速切入更保守档位
- 快速通道只能向更保守方向收缩，不能借机自动切到更激进档位

### 3.2 代码级开发子项

#### B11.1 紧急安全换档分类器

主要改动：

- 增加 `emergency_safety_transition` 判定
- 区分 `normal_optimization`、`safety_contraction`、`manual_only`
- 根据运行时安全状态、最近执行错误、对账状态、快照陈旧和 runtime guard 结论生成稳定 reason code

当前落点：

- `aats/services/operator/strategy_profiles.py`
- `aats/services/operator/strategy_profile_activation.py`

验收标准：

- 只有“更保守”候选档位可以走快速通道
- `trend_aggressive` 不允许通过快速通道自动激活
- 快速通道必须输出结构化 reason code，不能只靠日志字符串

#### B11.2 紧急安全换档门槛与常规门槛拆分

主要改动：

- 为 `execution_degraded_safe`、`high_volatility_defensive`、`range_defensive` 定义快速通道最小门槛
- 对紧急收缩路径放宽“最少平仓笔数 / replay 次数 / 连续胜出次数 / 最短活跃分钟数”要求
- 对常规优化换档保留现有慢门槛，避免频繁抖动

当前落点：

- `aats/services/operator/strategy_profile_activation.py`
- `aats/bootstrap/settings.py`
- `.env.spot.live`
- `.env.derivatives.live`

验收标准：

- 紧急收缩路径不能要求先积累完整实盘样本后才能收缩
- 常规优化换档逻辑必须保持现有保守门槛，不允许一起变松
- 自动激活记录里必须能区分“正常优化”还是“紧急收缩”

#### B11.3 紧急安全换档审计与 operator 可见性

主要改动：

- 在 selection decision、activation record、runtime summary 里显式写出 `transition_class`
- 新增“为什么立即切到安全档”的中文解释
- 风险页和策略档位页显示“当前处于紧急收缩态”与“解除条件”

当前落点：

- `aats/services/operator/query_service.py`
- `aats/api/static/modules/views/risk-view.js`
- `aats/api/static/modules/terms.js`

验收标准：

- operator 能明确分辨“系统主动优化”和“系统被迫收缩”
- 中文解释必须是干净 UTF-8 文案

## 4. Task72-B12 风险预算 multiplier 与自动收缩 v1

### 4.1 目标

- 把自动调参从“只改信号阈值”扩展到“同步收缩风险预算”
- 让系统在高波动、回撤、执行抖动、对账不稳时自动缩小可下单风险
- 风险预算只能自动收紧；自动放宽必须保守、渐进且可关闭

### 4.2 代码级开发子项

#### B12.1 风险预算状态模型

主要改动：

- 新增 `risk_budget_multiplier`
- 新增 `risk_budget_state / target / floor / ceiling / last_adjusted_at / adjustment_reason_codes`
- 区分 `steady`、`contracting`、`recovery_observing`

当前落点：

- `aats/schemas/governance.py`
- `aats/schemas/system.py`
- `aats/services/governance_engine/risk.py`
- `aats/services/operator/query_service.py`

验收标准：

- 风险预算状态必须可审计、可查询
- 收缩与恢复都要有明确原因和时间戳

#### B12.2 风险预算联动到真实下单边界

主要改动：

- 用 multiplier 联动：
  - `default_order_qty`
  - `max_abs_position_qty`
  - `max_notional_per_symbol`
  - `max_pending_notional_per_symbol`
  - `max_total_open_notional`
  - 合约侧 `default_target_leverage`
- 只允许在配置上限之内自动收紧，不允许突破人工设定的硬上限

当前落点：

- `aats/services/execution_engine/planner.py`
- `aats/services/governance_engine/risk.py`
- `aats/bootstrap/config.py`

验收标准：

- 自动收缩后，下单计划里的数量 / 名义金额 / 杠杆要与 operator 可见值一致
- 不能出现“控制面显示已收缩，但实际 planner 仍按旧上限下单”的分裂
- 对现货与合约都要保持单位语义正确

#### B12.3 风险预算恢复策略

主要改动：

- 引入逐步恢复逻辑，而不是一次性恢复到满额
- 恢复前要求：
  - 风险缓冲回到安全区
  - 对账状态持续干净
  - 最近执行错误回落
  - 最近收益 / churn / fee 拖累未恶化
- 支持一键关闭自动恢复，只保留自动收缩

当前落点：

- `aats/services/governance_engine/derivatives_live_guard.py`
- `aats/services/governance_engine/trial_guard.py`
- `aats/bootstrap/settings.py`

验收标准：

- 自动恢复必须比自动收缩慢
- 恢复过程必须可观察，不允许瞬间放大回原始仓位预算

## 5. Task72-B13 执行侵略性 multiplier 与自动收缩 v1

### 5.1 目标

- 根据 spread、盘口深度、滑点、提交失败、撤单重试和执行链健康度，自动调节执行侵略性
- 当执行质量变差时，优先降低执行 aggressiveness，而不是一上来直接停交易
- 保持现有订单生命周期和幂等语义不变

### 5.2 代码级开发子项

#### B13.1 执行侵略性状态模型

主要改动：

- 新增 `execution_aggressiveness_multiplier`
- 新增 `execution_quality_state / quality_score / degradation_reason_codes`
- 为最近滑点、挂单存活时间、submit->fill 延迟、cancel-replace 抖动建立统一摘要

当前落点：

- `aats/schemas/execution.py`
- `aats/services/governance_engine/health.py`
- `aats/services/operator/query_service.py`

验收标准：

- 侵略性状态必须能回答“为什么当前执行更保守”
- 质量评分和 reason code 必须来自系统已有指标，不允许拍脑袋生成

#### B13.2 执行参数自动收缩

主要改动：

- 用 multiplier 联动：
  - `ai_execution_max_passive_bias`
  - `ai_execution_max_maker_taker_bias`
  - `ai_execution_max_cross_spread_bps`
  - `ai_execution_max_slice_count`
  - `ai_execution_max_participation_rate`
  - `ai_execution_max_cancel_replace_patience_ms`
- 为不使用 AI 执行建议的场景提供统一 fallback，不让该逻辑只在 AI 打开时生效

当前落点：

- `aats/services/execution_engine/planner.py`
- `aats/services/execution_engine/order_manager.py`
- `aats/services/governance_engine/risk.py`

验收标准：

- 即使 `ai_execution_suggestion_mode=diagnostic_only`，执行侵略性收缩也能生效
- 自动收缩不能破坏价格单位、数量单位和现有提交前一致性校验

#### B13.3 执行侵略性与运行时停机边界

主要改动：

- 明确“先收缩执行侵略性”与“直接 only_reduce / halt”的边界
- 当执行质量进一步恶化时，把执行侵略性收缩升级为安全档切换或停机

当前落点：

- `aats/services/governance_engine/derivatives_live_guard.py`
- `aats/services/operator/strategy_profiles.py`
- `aats/services/blocker_control/service.py`

验收标准：

- operator 能看懂当前是“只降低侵略性”还是“已经必须停机”
- 不允许出现执行质量恶化时仍自动切到更激进档位

## 6. Task72-B14 持有 / 冷却参数纳入策略档位 v1

### 6.1 目标

- 让不同档位不仅影响开仓阈值，也影响持仓与再入场节奏
- 让“趋势激进”和“安全防御”在交易节奏上的差异真正闭环
- 保持平仓优先级和只减仓逻辑不被意外削弱

### 6.2 代码级开发子项

#### B14.1 扩展策略档位可管理字段

主要改动：

- 把以下字段纳入策略档位 payload：
  - `strategy_min_hold_seconds`
  - `strategy_post_close_cooldown_seconds`
  - `strategy_low_edge_threshold_bps`
  - `strategy_low_edge_streak_limit`
  - `strategy_low_edge_cooldown_seconds`

当前落点：

- `aats/schemas/strategy_profiles.py`
- `aats/services/operator/strategy_profile_seed.py`

验收标准：

- 档位切换后，这些字段必须跟随 active profile 生效
- 不允许出现“页面显示已切档，但 target_position 仍用旧值”的问题

#### B14.2 档位差异设计

主要改动：

- `trend_aggressive` 缩短最小持仓与平仓后冷却
- `trend_strict / range_defensive / high_volatility_defensive / execution_degraded_safe` 逐级拉长
- 保持“更保守档位不会比更激进档位更容易频繁进出”的单调性

当前落点：

- `aats/services/operator/strategy_profile_seed.py`
- `aats/services/decision_engine/target_position.py`

验收标准：

- 档位顺序与参数方向必须单调一致
- 更保守档位不能出现更短冷却、更短最小持仓这类反直觉配置

#### B14.3 节奏切换回归测试

主要改动：

- 补“切到 aggressive 更早允许再次入场”
- 补“切到 defensive 后降低 churn”
- 补“only_reduce / close-only 场景不被最小持仓错误挡住”

当前落点：

- `tests/unit/test_target_position.py`
- `tests/integration/test_mainline_chain.py`

验收标准：

- 节奏参数变化不会破坏正确平仓与风险收缩

## 7. Task72-B15 档位解释、阻断证据与 operator 面板收口 v1

### 7.1 目标

- 让 operator 一眼看懂当前档位、候选档位、切换阻断原因和下一次可能切换的条件
- 避免“系统自动换档了，但人不知道为什么”的黑盒体验
- 为后续 automation / 日报提供稳定接口

### 7.2 代码级开发子项

#### B15.1 档位控制解释接口

主要改动：

- 新增统一的 `profile_control_explanation`
- 输出：
  - 当前 active profile
  - 候选 profile
  - `transition_class`
  - blocked reasons
  - evidence counters
  - `next_eligible_switch_at`
  - `required_remaining_closed_trades`
  - `required_remaining_replay_validations`

当前落点：

- `aats/services/operator/query_service.py`

验收标准：

- operator 不需要反推多个字段就能看懂为什么没切档
- 必须区分“证据不足”“人工冻结”“运行不安全”“打开订单未清”四类原因

#### B15.2 前端档位面板和中文解释

主要改动：

- 风险页 / 策略页增加“当前档位说明”“候选档位说明”“为何未切换”
- 中文文案统一收口，避免直接显示内部 reason code

当前落点：

- `aats/api/static/modules/views/risk-view.js`
- `aats/api/static/modules/views/ai-config-view.js`
- `aats/api/static/modules/terms.js`

验收标准：

- 前端新增文本必须是干净 UTF-8 中文
- 同一条阻断原因不能在前后端出现互相矛盾的解释

#### B15.3 自动化消费接口

主要改动：

- 为日报 / 自动巡检保留稳定摘要接口
- 提供最小可消费结构：`status / headline / key_reasons / operator_action`

当前落点：

- `aats/services/operator/query_service.py`
- `aats/api/routes.py`

验收标准：

- 后续如果接 automation，不需要再重新拼字段

## 8. 当前执行顺序

本批次默认顺序如下：

1. 先做 `Task72-B11`，把紧急安全换档从常规优化换档里拆出来
2. 再做 `Task72-B12`，让风险预算能自动收缩
3. 再做 `Task72-B13`，让执行侵略性能自动收缩
4. 然后做 `Task72-B14`，把持有 / 冷却节奏纳入档位
5. 最后做 `Task72-B15`，把解释、证据和 operator 面板收口

## 9. 本批次边界与设计红线

- 不允许自动修改 `trading_product_type`、`margin_mode`、`allowed_symbols`
- 不允许自动突破人工配置的仓位 / 杠杆 / 名义金额硬上限
- 不允许因为自动调参而绕开已有 pre-trade 风控、对账阻断或恢复阻断
- 不允许把“紧急安全收缩”和“常规优化换档”混成一套门槛
- 不允许只在 AI 模式下才生效；自动收缩逻辑必须在 `baseline_only` / `diagnostic_only` 口径下也成立

## 10. 完成标志

当以下条件同时满足时，可以认为本批次完成：

- 系统能在运行不稳时快速切到更保守档，而不是卡在慢门槛上
- 风险预算会随着风险状态自动收缩，并可逐步恢复
- 执行侵略性会随着执行质量自动收缩，而不是只靠人工盯盘
- 持有 / 冷却节奏能跟随不同档位生效
- operator 可以直接看懂当前档位、候选档位、阻断原因和解除条件
