# Task 97：合约对冲模式 Phase 6 交付说明

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。


## 本轮目标

按 `task91_derivatives_hedge_mode_phase_breakdown.md` 的 `Phase 6`，把 operator / dashboard / audit 视图补成真正理解合约 `hedge mode` 的控制面，而不是只在底层代码里支持。

## 本轮完成项

1. Dashboard bundle 正式接入 `positions` 面板。
2. `交易总览` 的 `当前持仓` 表在合约双腿场景下改为显示：
   - 持仓模式
   - 多头腿 / 空头腿
   - 净敞口
   - 毛敞口
3. `风险与恢复` 页面新增：
   - 持仓模式契约
   - 合约敞口（long / short / gross / net）
   - 保留已有腿级对账异常摘要
4. `decision_view` / `/audit/{decision_id}` 新增 `hedge_mode_audit`：
   - `position_mode`
   - `leg_orders`
   - `leg_reconciliation`
5. 决策抽屉新增 3 张审计卡片：
   - 对冲模式审计
   - 腿级订单审计
   - 腿级对账审计

## 这轮没做的事

1. 没有新增独立的 operator 审计存储模型。
   - 这轮是基于现有 audit record、order intent、fill、reconciliation 派生可读摘要。
2. 没有进入 `Phase 7` 的策略 overlay。
3. 没有改 blocker action 的控制语义。

## 风险说明

1. 当前 `hedge_mode_audit.position_mode` 里的交易所模式来自“当前账户快照”，不是“该次历史决策当时的冻结快照”。
2. `leg_orders` 的主来源是 `order_intents`；如果历史记录缺少 intent，只会退回 `order_updates`。
3. 仓库里仍有既有 lint 存量问题，这轮没有顺手清掉。

## 验收重点

1. 概览页不再只展示净仓。
2. 风险页能直接看到：
   - 本地要求的模式
   - 交易所当前模式
   - long / short / gross / net
   - 腿级对账异常
3. 审计接口能直接看到：
   - posMode 相关摘要
   - 腿级订单摘要
   - 腿级对账摘要
