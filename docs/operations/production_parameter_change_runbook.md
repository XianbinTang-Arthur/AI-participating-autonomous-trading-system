# Production Parameter Change Runbook

> 最后核对：2026-08-24（起始 HEAD `00b6df0` + 未提交 Phase 3F 覆盖层）。这是 future production 变更门禁；当前全系统 `REAL-MONEY PRODUCTION: NO-GO`，标准 deploy/prewarm/wrapper 硬禁用所有 live profile。本页当前只可用于准备和审阅证据，不得执行 release/apply/runtime rebuild。API payload 以运行时 `/openapi.json` 为准，基础 apply/rollback 语义见 [Parameter Apply 与 Rollback](parameter_apply_and_rollback.md)。

## 1. 流程

```text
research evidence
  -> recommendation draft
  -> Operator approve
  -> Step2 integrity + pre-apply gate
  -> release + active parameter DB write
  -> runtime rebuild/load
  -> observation
  -> keep / review / rollback recommendation
  -> rollback（如需）
```

生产环境不允许自动 release。`release_cycle` 的 JSON schedule 为 disabled，任务队列也阻止它入队；所有前向变更必须是可归因的 Operator 动作。

## 2. 变更前硬门

- [ ] recommendation 已批准且未被 supersede。
- [ ] evidence lineage、研究数据版本、replay、attribution、execution realism 完整。
- [ ] Step2 integrity 无 blocking reason。
- [ ] pre-apply gate 为 pass，或 warn 已由 Operator 明确接受；block 不得继续。
- [ ] `/rdp/health` 无不可接受降级。
- [ ] `/system/health` 无 critical blocker。
- [ ] recovery 无 ambiguous/stuck submit，reconciliation 无 unresolved high/critical finding。
- [ ] 当前 active parameter set、previous target、actor、release id、notes 可追踪。
- [ ] observation window、成功/失败指标和回滚触发条件已定义。
- [ ] 代码版本、profile、交易数据库、research/governance 数据库边界已记录。

## 3. Gate

使用 `POST /rdp/gates/run`。服务端检查 recommendation/parameter set、quality、evidence freshness/completeness、decision、round、alerts、live DB health 和 workflow freshness 等条件。

| 结果 | 行为 |
| --- | --- |
| `pass` | 可继续 |
| `warn` | Operator 记录接受理由后才可继续 |
| `block` | 停止，修复后重新运行 gate |

生产流程不使用 `skip_gate=true`。即使请求模型保留该字段，也不代表它是受支持的生产操作。

## 4. Release 与 Apply

当前组合入口：

- `POST /rdp/releases/create`：对已批准 recommendation 执行 gate + release + apply；
- `POST /rdp/recommendations/{id}/approve-and-release`：approve + gate + release + apply。

两者与直接 `POST /rdp/parameters/apply` 一样，在实际执行 apply 时依赖 Operator write access、Step2 integrity gate 和当前 session 签发的 `action=apply` 短时 `X-Rdp-Apply-Token`。token 校验先于 approve/release 写入；`skip_apply=true` 的纯治理记录操作不要求该 token。

发布响应必须检查：

- `ok`；
- release id；
- gate result；
- apply result（success/failed/blocked_by_gate）；
- recommendation 权威状态；
- previous/target parameter set。

HTTP 200 不等于参数必然已生效：组合端点会用 `ok=false` 表达 integrity blocked、gate blocked 或 apply failed。

## 5. Runtime 生效验证

active parameter 写入数据库后，主交易 runtime 需要重新构建/启动才会通过 `build_runtime()` 注入；不要只重启一个错误的 slice 并假设四个进程一致。

标准 live 发布/重建入口当前失败关闭，没有 override；不得直接 Compose 绕过。若只验证部署脚本和模拟 runtime，使用 `bash scripts/deploy.sh --profile derivatives --skip-commit`，但该模拟结果不能证明参数已在 production 生效。

验证：

1. 所有主交易进程和 rdp-daemon 健康；两个 derivatives-live collector 单独验证。
2. Settings Provenance 显示预期 active parameter 字段和来源。
3. `GET /rdp/parameters/active` 与数据库 target 一致。
4. `GET /rdp/parameters/apply-history` 与 release actor/gate/history 一致。
5. `/system/health`、recovery、reconciliation、account freshness 和 kill switch 正常。

runtime loader 数据库失败时会退化到 profile 参数并记录 error，不读取 JSON fallback。此时停止变更并恢复数据库真源。

## 6. Observation

使用 `POST /rdp/observations/run` 或启用中的 `observation_cycle`。默认请求模型窗口为 24 小时；具体生产窗口必须在变更记录中明确，不依赖旧文档中的固定 72 小时说法。

至少观察：

- 策略决策数量、entry/exit/reversal/scale-in 分布；
- net edge、fee drag、slippage、fill ratio；
- realized/unrealized PnL、drawdown、margin/liquidation buffer；
- order/fill/obligation/ledger/reconciliation 一致性；
- blocker、kill switch、recovery 和 alert；
- RDP quality、attribution 和 execution realism 是否退化。

| 观察结论 | 后续 |
| --- | --- |
| `completed` / keep | 完成记录，保留参数 |
| review | 冻结进一步变更，人工审查 |
| `rollback_recommended` | 评估 target 后走受保护回滚 |

## 7. Rollback

1. 通过 `POST /rdp/rollback-recommendation/evaluate` 获取建议和 target。
2. 当前 Operator session 调用 `POST /rdp/operator-tokens`，action 为 `rollback`。
3. 携带 token 调用 `POST /rdp/parameters/rollback`。
4. 核对 active set、apply history、release/observation 和主交易 runtime。
5. 当前不得重建 live runtime；记录待执行动作。只有全系统 gate、克隆回滚演练和独立复核均通过并由后续变更重新开放 live 后，才可通过唯一标准入口重建并重复 trading-ready 检查。

Rollback 是安全动作，代码不会因为 Step2 integrity 降级而阻断；但仍会拒绝非法 target、无 active set、无 previous target 或环境不允许的请求。

旧的 `scripts/rdp_rollback_active_parameter_set.py` 已禁用并退出 2，不得使用。

## 8. 失败与恢复

| 故障 | 操作 |
| --- | --- |
| recommendation CAS race/409 | 刷新权威状态，不盲目重发 |
| integrity blocked | 修复 evidence/Step2，不创建前向变更 |
| gate blocked | 保留 gate/release 证据，修复后新一轮评估 |
| apply failed | 核对 active DB/history/release，确认是否发生部分写入 |
| runtime provenance 不匹配 | 保持/触发 halt，恢复 DB 真源并完整重建 |
| observation 严重退化 | 停止新决策，评估并执行 rollback |
| rollback 无合法 target | 不从 JSON 猜测；人工审查合法 parameter set |

任何 ambiguous 状态都先保护资金、保留证据，不能用重复 apply 掩盖。

## 9. 审计记录

每次生产变更至少保留：

- commit/profile/actor/time；
- recommendation、parameter set、gate run、release id；
- from/to 参数差异；
- apply/rollback history；
- runtime provenance；
- observation 数据与结论；
- 异常、处置和回滚结果。

数据库是 runtime active parameter 的唯一真源。artifact/JSON 是证据或审计副本，不保证能在 DB 故障时驱动 runtime。
