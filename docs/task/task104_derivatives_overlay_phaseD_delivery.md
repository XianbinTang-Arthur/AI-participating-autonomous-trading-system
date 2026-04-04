# Task 104：合约 Overlay Phase D 交付说明

## 1. 背景

`Task 100 / Phase D` 的目标不是继续扩张交易语义，而是把已经能跑的 overlay 能力收进一套可灰度、可回滚、可上线前验收的控制面。

## 2. 本轮改动

- 新增 `strategy_hedge_opportunistic_rollout_stage`
- 新增 `strategy_hedge_independent_rollout_stage`
- 在决策层按 `replay_only / dry_run / live` 做运行时门禁
- live 运行线下，如果阶段只到 `dry_run`，会明确阻断 opportunistic / independent
- `/strategy/runtime` 现在会返回 `configured_parameters.directional.hedge_rollout`
- 策略页现在会展示：
  - 当前运行线阶段
  - opportunistic / independent 的 rollout stage
  - 当前模式为什么被阶段阻断
  - 标准回滚顺序
- 补了运行手册和样本报告模板：
  - `docs/derivatives_overlay_rollout_runbook.md`
  - `docs/derivatives_overlay_sample_report_template.md`

## 3. 当前默认策略

- `derivatives`：`opportunistic=dry_run`，`independent=dry_run`
- `derivatives_live`：`opportunistic=live`，`independent=dry_run`
- 两条线都默认保持 `*_enabled=false`，不会自动放开

## 4. 风险边界

- 这轮没有新增真实交易逻辑，只新增 rollout gate 和 operator 可见性。
- `independent` 仍不允许直接配置成 `live`，这是当前阶段的硬限制。
- 运行时如果阶段不匹配，会保留主路径，但不会放开对应 overlay。

## 5. 验收结果

- 设置校验、managed profile 默认值、决策门禁、strategy runtime 暴露和策略页展示都已覆盖测试。
- 仓库级 `ruff check .` 仍有历史存量问题，不属于本轮新增。
