# Task252: Readiness Gate 与 Active-Core 收缩预检

## 结论

本轮结论是 **NO-GO**。当前不能执行 readiness gate 动作，也不能收缩 active core。

原因不是代码存在立即 hard stop，而是证据链不足：

- 最近真实订单与 fills 已停止在 `2026-04-17 17:52 +08:00` 左右，当前只有历史 25 笔 fills，无法证明最新执行科学链路在真实成交上仍然有效。
- `strategy_profile_evaluations` 最新记录停在 `2026-04-25 08:30 +08:00`，距本轮运行态检查约 31 小时；`strategy_profile_recommendations` 有 4409 条 pending 且最新建议已经过期。
- dashboard 受认证保护，runtime truth 只能确认 `effective_operating_mode=unknown_auth_required`；不能把配置目标 `ai_decision_maker` 当成当前有效运行态。
- DB 可确认策略档位为人工固定：`active_profile_id=trend_normal`、`activation_mode=manual`、`auto_switch_enabled=false`，所以自动切档当前不是有效运行态。
- 自动化 artifact 三个状态文件带 UTF-8 BOM，导致 `scripts/runtime_truth_report.py` 把 artifact 状态归为 `missing_artifact`；这不是交易 hard stop，但会污染后续自动化判断。
- `parameter_release_history.json` 内存在 `rec_dev_1 / ps_dev_1 / gate_1` 的开发痕迹，不能作为生产 release/readiness 证据。

## 运行态事实

- Git/部署：`main` 与 `origin/main` 同步，HEAD 为 `eb9a77918bc798559ef791d60094a430f712447d`，deployed head 与 Windows head 一致。
- Runtime truth：`ok=true`，`blocking_findings=[]`，AI timeout 当前不是 active blocker。
- Dashboard：受认证保护，AI effective mode 只能标记为 `unknown_auth_required`。
- 决策链：`decision_audit_records` 512519 条，最新 `updated_at=2026-04-26 15:46 +08:00`；`event_store` 与 reconciliation 仍在更新。
- 执行链：`execution_orders=28`、`execution_fills=25`、`fill_events=25`、`fill_outcomes=25`；最新真实执行证据均停在 `2026-04-17 17:52 +08:00` 前后。
- 档位链：`strategy_profile_activation` 显示 `trend_normal`、manual、auto switch disabled；最近人工切换时间 `2026-04-25T00:59:43Z`。
- 档位评估链：`strategy_profile_evaluations=26538`，最新 `2026-04-25 08:30 +08:00`；`strategy_profile_recommendations=4410`，最新 recommendation 已过期。
- 组合/成本链：`portfolio_snapshots=30`，最新 `2026-04-21 02:35 +08:00`；`funding_fee_records=4`，最新 `2026-04-23 00:00 +08:00`。

## 预检矩阵

| 前置条件 | 状态 | 证据 | 处理 |
| --- | --- | --- | --- |
| 固定范围 OKX + BTC-USDT-SWAP | 满足 | DB orders/fills 与 runtime truth 均只指向 `BTC-USDT-SWAP` | 保持 |
| Git/deploy 一致 | 满足 | HEAD/deployed 均为 `eb9a779` | 保持 |
| release/promotion/tuning 冻结 | 满足 | `ENQUEUE_BLOCKED_WORKFLOWS={release_cycle}`；daemon 对 blocked workflow 直接返回 | 不解冻 |
| AI timeout 分类 | 满足 | runtime truth 未显示 active blocker；effective mode 未认证确认 | 保持 latent risk，不升级为当前 blocker |
| 自动切档有效态 | 满足/人工态 | DB 显示 manual + auto_switch_enabled=false | 不按自动切档判断 readiness |
| 最新真实 fills | 阻断 | fills/orders 最新停在 2026-04-17 | 需要新真实 fills 或明确无成交原因 |
| 档位评估 freshness | 阻断 | profile evaluations 最新停在 2026-04-25 08:30 +08:00 | 需要修复/确认定时任务 |
| 组合与成本 freshness | 缺证 | portfolio/funding 较旧；可能由交易停止导致，也可能是采集任务缺口 | 需要单独 freshness 审计 |
| artifact 状态可信度 | 阻断 | 三个 automation JSON 文件存在 UTF-8 BOM，runtime report 误判 missing_artifact | 需要去 BOM 并增强读取容错 |
| active-core 收缩依据 | 阻断 | independent 仍只是 live carrier；directional_1h 仍是 `none_verified` | 不收缩 |

## Bounded Task 定义

- task_type：`state-audit`
- lineage：`readiness_gate_active_core_precheck`
- input：runtime truth report、live DB 非敏感聚合、readiness/release/frozen governance 文档、RDP queue/release freeze 代码、automation state。
- output：本预检文档、automation state 更新、下一轮唯一 bounded task。
- 影响范围：只读审计与 automation 状态；不改变策略、风险、执行、AI provider、symbol、venue、strategy family、release/promotion/tuning、schema 或 live order behavior。
- 验证方式：runtime truth smoke、DB 非敏感聚合查询、代码/文档交叉检查、git diff check。
- 回滚方式：删除本文件并恢复 automation state 到上一轮；无运行时行为变更。

## 验收标准

- 每个 readiness/active-core 前置条件都标记为 satisfied、blocked 或 missing-evidence。
- release/promotion/tuning 路径没有被打开。
- 下一轮任务是单一 allowed bounded task。

验收结果：**通过审计任务本身，readiness/active-core 为 NO-GO**。

## 下一轮队首任务

`runtime-reliability-fix / strategy_profile_evaluator_freshness_repair`

目标：调查并修复 `strategy_profile_evaluations` / `strategy_profile_recommendations` 停止更新或过期堆积的问题，同时确认是否影响 `portfolio_snapshots`、`funding_fee_records` 等低频采集表。不得开启自动切档、release、promotion 或 tuning。
