# RDP Platform V3 代码审查报告

> 文档状态：现行审查快照（本地静态与测试复核完成，待标准部署和现场验收）
> 审查日期：2026-08-25
> 起始基线：`70f1a581a81f55697c9b68167539ad0db86fc06a`（`main`）
> 设计真源：[`../design/rdp_platform_v3_architecture_2026_08_25.md`](../design/rdp_platform_v3_architecture_2026_08_25.md)
> 范围边界：本文不证明当前容器、交易所、数据新鲜度、候选盈利性或参数现场状态

## 1. 审查结论

RDP V3 已把七份前端读快照收敛为版本化 workspace，并保留现有 Run、Task、recommendation、Gate、release、active parameter 和 observation 真源。本轮复核发现并修复了队列投影、发布资格、状态分级、证据可追溯性和 workspace 内部一致性问题。最终本地复核没有发现仍未修复的 P0/P1 代码缺陷。

“没有发现”仅适用于本报告列出的代码范围和已执行测试。真实 Postgres 并发、容器进程、模拟市场数据和一次完整 RDP 的运行结果，必须在提交、推送和标准部署后重新验证。

## 2. 审查范围

- `/rdp/v2/runs` 与新增 `/rdp/v3/workspace` 的认证、错误和列表语义；
- `rdp_workspace.py` 的 lifecycle、execution lane、workflow capability、candidate eligibility 与 next action；
- Dashboard snapshot plane、Gateway 写后失效和 active Run 定向刷新；
- RDP V3 前端布局、中文状态、Run drawer、研究证据、发布、观察、回滚与 tuning；
- 业务逻辑审查已修改的 execution realism、dispatcher、scheduler、retry、daemon 和 Run/Task/Step/Event 单调状态；
- apply/release/rollback 的 session、短时 action token、Gate 与 stale history 失败关闭；
- 当前 Operations/RDP 文档与实现的一致性。

## 3. 本轮发现与修复

| 编号 | 级别 | 审查发现 | 风险 | 修复与验证 |
| --- | --- | --- | --- | --- |
| V3-CR-001 | P1 | workspace 仅取最近 N 条 Run，长时间 queued/running Run 可能被大量新终态挤出 | UI 显示执行槽空闲，Operator 重复触发 | recent 与全部 active status 分别查询、按 Run ID 合并；增加“旧 active 不得分页丢失”测试 |
| V3-CR-002 | P1 | UI 队列最初按 Run 返回顺序展示，未完全复刻 daemon 的 priority/backoff 领取规则 | 队列位次和等待原因失真 | 使用 task truth，按 eligible、operator recovery、operator、retry、scheduled 投影；冷却任务排在可领取任务之后 |
| V3-CR-003 | P1 | `partially_succeeded` / `succeeded_with_warnings` 在 lifecycle 中可能被当作完整成功 | 带缺口证据进入下一阶段 | 映射为 `action_required`，Run drawer 和卡片使用警告语义 |
| V3-CR-004 | P1 | Gate 未通过或治理历史来源未知/stale 时，已有候选动作仍可能显示可创建发布 | 在不完整治理证据上诱导 apply | 后端统一禁用 create release；只有 approved + Gate pass + DB 真源新鲜候选进入 eligible 集合 |
| V3-CR-005 | P1 | 回滚按钮最初把展示型 `combo_key` 直接传给 slash 分隔的 API action | family/timeframe 解析失败或回滚错对象 | action value 只使用受控 `family/timeframe`；缺失映射时按钮禁用 |
| V3-CR-006 | P1 | workspace HTTP 已统一，但组装器内部仍重复读取多次 control summary 和 Phase 3/4 证据 | 同一响应可拼接不同时间点状态，且产生重复 DB/文件读取 | 新增 workbench bundle；一次读取 control summary、一次读取 Phase 3/4 证据，再派生 overview/items/alerts/release/tuning；增加单次真源复用测试 |
| V3-CR-007 | P2 | 空的认证 snapshot 默认值曾被塑造成“有效空 workspace” | 首屏短暂显示正常空状态而非加载状态 | 默认值改为 `{}`，由 RDP view 显示明确加载/错误状态 |
| V3-CR-008 | P2 | 研究重构只保留结论，遗漏 evidence metrics、source round 与风险摘要 | 审批可追溯性下降 | 在折叠证据区恢复中文关键指标、单位、来源轮次和主要风险，同时保持紧凑布局 |
| V3-CR-009 | P2 | observation 重构只显示状态，遗漏 recommendation/effectiveness；stale history 仍可能开放动作 | Operator 无法解释回滚建议，或基于副本执行动作 | 恢复中文观察建议和有效性摘要；stale release history 禁用观察与回滚 |
| V3-CR-010 | P2 | queued Run 同时进入“最近运行”，造成重复展示 | 页面噪声和状态误解 | recent 区只展示终态，active/queued 分区保持互斥 |

## 4. 不变式复核

1. 前端动作只能收紧后端 capability，不能把后端禁用动作重新启用。
2. Run 创建立即返回 logical Run；真正执行仍由单槽 daemon 领取。
3. `release_cycle` 的 golden-path freeze 没有解除。
4. 所有会执行 apply 的直接或组合入口要求当前 session 签发的 `action=apply` token；rollback 使用独立 token。
5. Gate 未通过、历史来源未知/stale、证据不完整或没有 eligible candidate 时不应用参数。
6. Run/Task/Step terminal 状态保持单调；迟到 worker 结果不会覆盖已终结状态。
7. 本轮只允许 derivatives 模拟部署；没有引入 live profile 例外。

## 5. 验证证据

- `ruff check aats/ --fix`：通过；
- 全量 unit：`4656 passed, 30 skipped, 94 subtests passed`；
- RDP production workflow、apply token 与 Dashboard UI 集成集：`139 passed`；
- 最终前端/Gateway/snapshot 定向集：`35 passed`；
- workspace/control-summary/v2/UI 定向复核：`146 passed`；
- 48 份 Dashboard JavaScript 文件语法检查：通过；
- 改动文档相对链接检查：14 份、0 断链；
- `git diff --check`、敏感凭证模式扫描、UTF-8 replacement-character 扫描：通过。

测试产生的 SQLAlchemy/SQLite Python 3.12 deprecation warning 为既有兼容性提醒，本轮没有把 warning 当作失败；它不改变上述 RDP 测试结果，但应在后续依赖升级任务中处理。

## 6. 仍需现场验证

1. 标准 WSL2 derivatives 部署后，验证 required containers、Gateway、`/system/health`、`/system/recovery`、RDP daemon heartbeat 和治理数据库真源；
2. 在目标浏览器核对桌面与窄视口布局、中文指标、队列位次、disabled reason 和 Run drawer；
3. 人工触发 `research_cycle`，记录 Run ID，监控 Attempt/Step/Event 到终态；
4. 若结果为 warning/partial/failed，必须按首个失败证据处理，不继续发布；
5. 只有 workspace 返回真实 eligible candidate，且发布时重新通过 token、Gate、映射和 rollback 校验，才允许执行模拟参数应用；否则以 `no_eligible_candidate` 安全停止。
