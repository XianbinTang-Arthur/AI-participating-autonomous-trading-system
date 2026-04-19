# Task 94: 合约 Hedge Mode Phase 3 交付说明

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。


## 1. 变更范围

- 新增显式腿订单语义：`LegOrderIntent`
- 新增腿级执行规划：`LegExecutionPlan`
- 合约 `hedge` 模式下，禁止继续使用 `signed target -> position_intent` 的旧路径
- 新增统一执行入口：`submit_leg_order()`
- 让 `OrderManager -> Adapter -> OrderState/FillEvent` 全链路保留 `leg_action / leg_intent_id`

## 2. 影响模块

- `aats/schemas/execution.py`
- `aats/services/execution_engine/planner.py`
- `aats/services/execution_engine/exchange_adapter.py`
- `aats/services/execution_engine/order_manager.py`
- `aats/services/execution_engine/okx_adapter.py`
- `aats/services/execution_engine/paper_adapter.py`
- `aats/services/execution_control/order_service.py`
- `aats/services/execution_control/shadow.py`

## 3. 数据模型变化

### 新增

- `LegOrderAction = open | reduce | close`
- `LegOrderIntent`
- `LegExecutionPlan`

### 扩展

- `OrderIntent`
  - `leg_intent_id`
  - `leg_action`
- `ExecutionPlan`
  - `leg_action`
- `OrderState`
  - `leg_intent_id`
  - `leg_action`
- `FillEvent`
  - `leg_intent_id`
  - `leg_action`

### 语义变化

- `position_intent` 仍保留用于兼容旧链路和存量持久化
- 但在合约 `long_short_mode` 下，它不再是主语义来源
- 主语义改成：`side + pos_side + action + position_mode`

## 4. 配置变化

- 无新增配置项
- 继续沿用 Phase 0/1 已有配置：
  - `derivatives_position_mode`
  - `derivatives_hedge_transition_mode`
  - `derivatives_require_exchange_pos_mode_match`

## 5. 测试清单

### 单元测试

- `tests/unit/test_execution_planner.py`
  - 覆盖显式腿计划/腿意图构建
  - 覆盖 hedge 模式下旧 signed 路径被拒绝
- `tests/unit/test_guarded_simulated.py`
  - 覆盖 `submit_leg_order()` 的显式 `posSide` 行为
  - 覆盖 `long_short_mode` 下 legacy `OrderIntent` 被阻断

### 集成测试

- `tests/integration/test_okx_live_submit_path.py`
  - 覆盖 `OrderManager.submit_leg_order() -> OKXExecutionAdapter`
  - 验证 `posSide=long`
  - 验证 `reduceOnly=true`
  - 验证 `leg_action=close` 会落到状态与成交事件

## 6. 回滚方式

- 回滚本次 schema / planner / adapter / order manager 改动
- 删除 `LegOrderIntent` 和 `LegExecutionPlan`
- 恢复 `long_short_mode` 下继续允许 `build_plan/build_intent` 的旧 signed 提交流程

注意：

- 如果已经有上层代码开始调用 `submit_leg_order()`，回滚前必须先同步回退调用方
- 当前数据库 schema 未新增列，本次变更主要通过兼容字段和 `submission_payload` 承载，不涉及数据库迁移

## 7. 尚未覆盖的风险

- Phase 3 只解决“腿级下单语义”，还没有完成：
  - Phase 4 腿级风控
  - Phase 5 腿级对账与恢复
  - Phase 6 控制面完整双腿运维
  - Phase 7 hedge overlay 策略
- 当前 `bootstrap/config.py` 的主策略执行链仍走净仓位 `PositionTarget -> ExecutionPlan`
  - 在交易所已处于 `long_short_mode` 时，这条旧链现在会停止生成订单
  - 这是刻意的保护，不是最终产品形态
- `position_intent` 仍然存在于持久化和若干兼容路径里
  - 这有利于渐进迁移
  - 也意味着后续 Phase 4/5 还需要继续清理“净仓意图残留”

## 8. 本阶段结论

- Gate C 的一半基础已经到位：
  - hedge 模式下旧 signed 提交路径被切断
  - execution layer 已具备显式腿订单入口
- 但还不能宣称“hedge mode 可真实运行”
  - 下一步必须进入 Phase 4，把风控改成 `long / short / gross / net` 四口径
