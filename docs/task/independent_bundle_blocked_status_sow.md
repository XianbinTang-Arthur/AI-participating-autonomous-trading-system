# Independent 合约实盘 bundle blocked 状态修复 SOW

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。


## 背景

对 Independent 合约实盘链路复审时发现，`StrategyExecutionBundleStatus` 已定义 `blocked`，
但 `aats/services/execution_engine/bundle_status.py` 的状态机从未返回该状态。

当前逻辑会把以下场景错误升级为 `review_required`：

- bundle 内所有腿都在本地 rollout / risk gate 被 `BLOCKED`
- 或所有腿都已终态失败（`BLOCKED/FAILED/REJECTED`），且没有任何 open/fill

这类场景没有部分成交，也没有恢复中的半开状态，本质上是“未执行成功、但也无需恢复”的
受控阻断，不应进入 `review_required` 恢复链。

## 目标

1. 让全终态失败且无 open/fill 的策略 bundle 正确落到 `blocked`。
2. 保持混合态失败（存在 open 或 fill）的 bundle 继续进入 `review_required`。
3. 为 Independent dry-run rollout 拦截增加 bundle 级回归保护。

## 边界

- 不改 Independent 策略建模、allocator 或 recovery 主流程。
- 只修 bundle 状态机与受影响测试。
- 不改变已有 `partial_fill_recovery / recovered / review_required` 的既有语义边界。

## 方案

1. 在 `bundle_status.py` 中增加 `blocked` 分支：
   - `has_failure=True`
   - `has_open=False`
   - `has_filled_or_partially_filled=False`
2. 保留原有混合失败场景：
   - 有 open 或有 fill 时，仍返回 `review_required`
3. 增加状态原因码 `strategy_bundle_blocked`，并补前端术语翻译。
4. 增加：
   - 纯单元测试：直接锁住状态机分支
   - WSL2 集成测试：Independent family mode 在 dry-run rollout 被阻断时，bundle.status 应为 `blocked`

## 验收标准

1. 全终态失败、无 open/fill 的 bundle 返回 `blocked`。
2. 混合 open/failure 的 bundle 仍返回 `review_required`。
3. Independent rollout guard 集成测试通过。
