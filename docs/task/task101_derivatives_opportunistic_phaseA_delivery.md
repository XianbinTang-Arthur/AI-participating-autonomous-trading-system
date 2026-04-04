# Task 101：合约 Opportunistic Overlay Phase A 交付说明

## 1. 交付目标

本阶段落实 [`task100_derivatives_opportunistic_independent_overlay_sow.md`](/D:/文件/project/AIParticipatingAutonomousTradingSystem/docs/task100_derivatives_opportunistic_independent_overlay_sow.md) 中的 `Phase A：Opportunistic 决策与配置`。

本阶段只做 4 件事：

- 新增 opportunistic overlay 的配置项
- 让 directional 在 `hedge mode + overlay_mode=opportunistic` 时产出机会腿
- 给机会腿单独定义 reason code / blocked reason，不复用 protective 的命名
- 让 `/strategy/runtime` 能明确看到 `effective_mode=opportunistic` 与对应参数

本阶段不做：

- `independent` 双书状态机
- operator 审计页面改造
- 回放样本与灰度上线

## 2. 业务边界

- 只支持合约 `hedge mode`
- 只支持 directional
- `opportunistic` 仍然要求先有主腿库存，再允许机会腿存在
- 机会腿默认受独立的最小持有、重平衡冷却、费耗和 churn 限制

## 3. 模块范围

- `aats/bootstrap/settings.py`
- `aats/services/decision_engine/target_position.py`
- `aats/schemas/decision.py`
- `aats/services/operator/query_service.py`
- `configs/strategy_profiles/derivatives.yaml`
- `configs/strategy_profiles/derivatives_live.yaml`
- `tests/unit/test_target_position_engine.py`
- `tests/integration/test_strategy_runtime_integration.py`

## 4. 运行语义

- `strategy_hedge_overlay_mode=protective`
  - 继续走现有保护性对冲评分
- `strategy_hedge_overlay_mode=opportunistic`
  - 当主腿存在库存且对侧机会分数达到开仓阈值时，生成机会腿
  - 当机会分数跌回关闭阈值以下时，优先收口机会腿
  - 当机会腿已经存在时，最小持有和重平衡冷却依然生效

## 5. 验收标准

- settings 能成功加载 opportunistic 参数
- 单测能覆盖：
  - 开机会腿
  - 因费耗比过高阻断机会腿
  - 最小持有阻断提前收口
  - 重平衡冷却阻断再次打开
- `/strategy/runtime` 能看到：
  - `hedge_overlay_mode=opportunistic`
  - `hedge_opportunistic_enabled`
  - `hedge_overlay_decision.effective_mode=opportunistic`

## 6. 回滚

- 关闭 `strategy_hedge_opportunistic_enabled`
- 把 `strategy_hedge_overlay_mode` 改回 `protective`
- 如需彻底退回旧行为，可继续把 `strategy_hedge_overlay_enabled=false`
