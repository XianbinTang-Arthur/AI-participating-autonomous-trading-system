# Task116 Protective 独立开关补齐

## 目标

- 补齐 `protective` 缺失的独立开关。
- 保持现有 `strategy_hedge_overlay_enabled` 继续作为 overlay 总开关。
- 不改变 opportunistic / independent 的既有行为。

## 变更范围

- `aats/bootstrap/settings.py`
  - 新增 `strategy_hedge_protective_enabled`
- `aats/services/decision_engine/target_position.py`
  - protective 模式下接入独立开关
- `aats/services/execution_engine/order_manager.py`
  - 执行层二次防线补齐 protective 禁用拦截
- `aats/services/operator/query_service.py`
  - runtime 暴露 `hedge_protective_enabled`
- `aats/api/static/modules/views/strategy-view.js`
  - 策略页展示 protective 单独开关
- `configs/strategy_profiles/derivatives.yaml`
- `configs/strategy_profiles/derivatives_live.yaml`
  - 增加显式配置，避免 protective 继续成为“隐式总是启用”

## 兼容性

- 新开关默认值为 `true`，避免旧配置在未补字段时被意外关掉 protective。
- 关闭逻辑为：
  - 总开关 `strategy_hedge_overlay_enabled=false`：三种 overlay 全部停用
  - `strategy_hedge_protective_enabled=false`：仅 protective 停用

## 验证

- settings / managed profile 单测
- protective 决策层单测
- protective 执行层阻断单测
- runtime 配置展示集成测试
