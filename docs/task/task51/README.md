# Task51 Phase 1 收口

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../../docs/project_positioning.md)。


## 目标
本任务把 Task38 到 Task50 形成的 Phase 1 兼容层收成一条明确的工程边界，重点不是切换主真相源，而是把新旧执行链之间的影子兼容能力补齐为“可写、可观测、可阻断、可留痕、可查询”的一套闭环。

## 本次收口包含的内容
- `phase1_shadow` 有统一子系统对象，不再只靠 runtime 上的散装 repo / service / monitor 字段。
- operator 侧可以直接查看 `phase1_shadow` 当前状态、近期告警、处理失败和人工核查记录。
- `phase1_shadow` 有专门历史接口，便于后续接 dashboard 历史视图或 operator 追踪页。
- blocker control 对 `phase1_shadow` 提供“查看影子详情”“刷新当前状态”“已核查，继续阻断”三个动作。
- 人工核查会落 `system.operator_actions`，不会自动放行系统，但能保留恢复前人工判断证据。
- postgres 路径补了 `phase1_shadow` 路由与人工核查历史的集成测试骨架。

## Phase 1 现在的边界
Phase 1 到此为止，系统已经具备：
- Phase 1 execution shadow 写入
- Phase 1 ledger mirror 写入
- shadow lag / failure 监控
- shadow blocker / alert / operator visibility
- shadow review audit trail

Phase 1 仍然**没有**做的事：
- 不把 `portfolio_snapshot` 替换为账本真相源
- 不把执行主读路径切到 `execution_orders / execution_fills / reservations / ledger_*`
- 不把恢复主链完全切到新执行表和新账本表

## 验收口径
Phase 1 收口后，至少应该满足：
1. 旧链路产生的订单、成交、保留金，能镜像到新表。
2. 新旧链路一旦掉队，健康面和恢复门禁能看见并阻断。
3. operator 能查看影子兼容层详情和最近历史。
4. operator 的人工核查动作会留痕，但不会绕过阻断。
5. postgres migration-path 下，`/system/shadow` 和 `/system/shadow/history` 行为可验证。

## 下一阶段建议
Phase 2 应该开始做“主真相切换前的双轨验证”：
- 让 `execution_orders / execution_fills / reservations / ledger_journals / ledger_entries` 成为可比对的准真相层。
- 用新账本投影重建 `balance / position / equity` 视图。
- 把恢复与对账逐步迁到新执行表和新账本表。
