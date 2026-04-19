# Task114 - Independent 同 Bundle Partial Fill 提交防误拦

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。


## 目标

- 修复 `independent` 双书在同一 bundle 内按腿顺序提交时，第一条腿已成交后把当前 bundle 自己误判成 `bundle_recovery_in_progress`，进而拦掉后续腿的问题。

## 边界

- 只放宽“当前同一 bundle 且仍可恢复”的 `strategy_bundle_recovery_in_progress` 豁免。
- 不放宽其他 bundle 的恢复阻断。
- 不放宽 `review_required` 或不可恢复 bundle。

## 修复思路

- 调整 `RiskEngine._structured_open_orders_belong_to_current_bundle()` 的判定。
- 当前实现只接受 `structured_open_orders`，但同一 bundle 第一条腿先成交时，会自然进入 `partial_fill_recovery`。
- 对当前 bundle 来说，这仍属于“本轮正在继续完成同一 bundle”，不应把后续腿当成外部 only-reduce 场景拦掉。
- 因此把允许状态扩展为：
  - `structured_open_orders`
  - `partial_fill_recovery`

## 验证

- 单测覆盖：同 bundle `partial_fill_recovery` 不拦后续腿。
- Postgres 集成测试覆盖：`independent_overlay_bundle_consistent` 不再落成 `review_required`。
