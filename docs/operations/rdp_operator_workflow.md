# RDP Operator 工作流 SOP

> 最后核对：2026-08-22（代码基线 `be9179e`）。本 SOP 只使用当前 Operator API/UI 和 task queue；旧直写 CLI、4-workflow 清单和 active JSON fallback 已移除。

## 1. 每日观察

登录后依次查看：

| 目标 | API/页面 | 判断 |
| --- | --- | --- |
| RDP health | `GET /rdp/health` | DB、artifact、workflow、daemon 无不可接受错误 |
| Task queue | `GET /rdp/tasks/status` | 无长期 pending/running；失败有 exit/log tail |
| Active parameters | `GET /rdp/parameters/active` | combo/version/actor 完整，无异常 missing |
| Recommendations | `GET /rdp/recommendations/latest` | 新 draft、已批准、被替代状态合理 |
| Decision/readiness | `GET /rdp/decisions/latest`、`GET /rdp/readiness` | pause/review 与证据一致 |
| Attribution/execution | `GET /rdp/attribution/latest`、`GET /rdp/execution/latest` | failure/cost/fill 指标无退化 |
| Releases/history | `GET /rdp/releases/latest`、`GET /rdp/parameters/apply-history` | 无未知 actor 或半完成发布 |
| 主交易系统 | `/system/health`、recovery、reconciliation | 无 critical blocker 或资金事实不一致 |

## 2. Recommendation 审阅

审批前确认：

- source parameter set 和 evidence lineage 完整；
- Step2 integrity 正常；
- replay、attribution、execution realism 和 readiness 支持；
- 结论不是只靠单次收益或短窗口；
- 变更字段在 active parameter 映射 allowlist 中；
- 影响范围、observation 和 rollback target 清晰。

当前动作：

- approve：`POST /rdp/recommendations/{id}/approve`
- reject：`POST /rdp/recommendations/{id}/reject`
- supersede：`POST /rdp/recommendations/{id}/supersede`

认证开启时 actor 由 session principal 决定；request body 的 actor 不能伪造审计身份。

## 3. 发布

生产前向变更必须按 [Production Parameter Change Runbook](production_parameter_change_runbook.md) 执行。

当前入口：

- 单独 apply：`POST /rdp/parameters/apply`，需要 `action=apply` 的短时 `X-Rdp-Apply-Token`；
- release + apply：`POST /rdp/releases/create`，当前只要求 write access + integrity/gate；
- approve + release + apply：`POST /rdp/recommendations/{id}/approve-and-release`，策略与组合 release 相同。

组合端点没有 token 依赖是当前代码事实，不等于可绕过 gate。生产不使用 `skip_gate=true`。

发布后：

1. 核对 active set、apply history 和 release；
2. 通过标准部署重建 runtime；
3. 核对 Settings Provenance；
4. 检查主系统 health/recovery/reconciliation；
5. 进入 observation window。

## 4. 回滚

1. 运行/查看 `POST /rdp/rollback-recommendation/evaluate` 的建议；
2. 确认合法 previous/explicit target；
3. 当前 Operator session 申请 `action=rollback` token；
4. 携带 token 调用 `POST /rdp/parameters/rollback`；
5. 核对 active/history/release/provenance 和主交易事实。

旧的 `scripts/rdp_rollback_active_parameter_set.py` 已禁用，不能作为紧急通道。

## 5. 手工触发 Workflow

通过 UI 或 `POST /rdp/tasks/trigger` 触发，`GET /rdp/tasks/status` 跟踪。

- 可识别 workflow 共 10 个；
- `release_cycle` disabled 且禁止入队；
- 同 workflow 已有 pending/running 时不会重复创建；
- daemon 通过数据库 claim 执行，不能手工把状态改为 running；
- 重试遵循 `earliest_start_at` 和 failure history。

完整 schedule 见 [平台运行手册](platform_runbook.md)。

## 6. 常见故障

| 症状 | 处理 |
| --- | --- |
| Recommendation 看不到 | 检查 DB-first registry、过滤条件和 source artifact；不要用禁用 CLI show |
| Apply integrity blocked | 修复 Step2 evidence，不继续前向变更 |
| Gate blocked | 根据 failed checks 修复后重跑；不跳 gate |
| Active set 与预期不符 | 停止发布，查 DB active set/history/release/runtime provenance |
| DB active loader 失败 | runtime 已退化到 profile 参数；恢复数据库，不能靠 JSON fallback |
| Task pending | 查 daemon heartbeat、earliest_start_at、同 workflow active task |
| Task running 无 heartbeat | 查 orphan recovery；旧 task 应变 failed/-3，再走 retry |
| Rollback 无 target | 不猜测版本；人工审查合法 parameter set |

## 7. 禁止事项

- 不执行 `apply_active_parameter_set.py`、`approve_recommendation_and_apply.py`、`rdp_rollback_active_parameter_set.py`、`rdp_freeze_parameter_set.py`、`rdp_run_release_cycle.py`；它们均为禁用桩。
- 不直接修改 active parameter JSON 或数据库表绕过 API。
- 不展示 session cookie、apply token、数据库连接串或环境文件内容。
- 不把 `/healthz` 或 HTTP 200 当成发布成功；必须检查 response `ok` 和业务状态。
- 不把 historical Stage/Phase runbook 当作当前操作指南。
