# Phase 1 验收清单

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../../docs/project_positioning.md)。


## 兼容层写入
- [ ] `OrderManager` 会 shadow write 到 `execution_orders`
- [ ] fill 会 shadow write 到 `execution_fills`
- [ ] obligation 会 mirror write 到 `reservations / settlements / ledger_*`

## 可观测性
- [ ] `/system/shadow` 返回当前状态、lag、execution shadow、ledger shadow
- [ ] `/system/health` 暴露 `phase1_shadow_*` 摘要
- [ ] `/system/metrics` 暴露 shadow backlog / failure / alert / recovery 指标
- [ ] 持续 lagging / degraded 会进入 health blocker

## Operator 闭环
- [ ] blocker control 能显示 `phase1_shadow_lagging / phase1_shadow_degraded`
- [ ] operator 可执行“查看影子详情”
- [ ] operator 可执行“已核查，继续阻断”
- [ ] 人工核查会落 `system.operator_actions`
- [ ] `/system/shadow/history` 能看到 review / alert / failure 时间线

## 测试
- [ ] unit 覆盖 execution shadow / ledger mirror / shadow gate / shadow alerting
- [ ] integration 覆盖 operator API 的 `phase1_shadow` 读写路径
- [ ] dashboard UI smoke 覆盖 shadow detail 入口
- [ ] postgres integration 覆盖 shadow route / review history 基础链路

## 不在本阶段
- [ ] 不切资金主真相
- [ ] 不切执行主读路径
- [ ] 不切恢复主链
