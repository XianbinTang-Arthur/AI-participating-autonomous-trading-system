# RDP Operator 工作流 SOP

> 最后核对：2026-08-25（起始代码基线 `70f1a581`，含本轮 RDP 业务逻辑整改工作区）。本 SOP 只使用当前 Operator API/UI 和 Run/attempt queue；旧直写 CLI、4-workflow 清单和 active JSON fallback 已移除。

## 1. 每日观察

登录后依次查看：

| 目标 | API/页面 | 判断 |
| --- | --- | --- |
| RDP health | `GET /rdp/health` | DB、artifact、workflow、daemon 无不可接受错误 |
| Run center | `GET /rdp/v2/runs`、`GET /rdp/v2/runs/{run_id}` | queued 有明确 eligible time；running 有 heartbeat/current step；retry 仍属于同一 run |
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
- release + apply：`POST /rdp/releases/create`，需要 write access、integrity/gate 和 `action=apply` token；
- approve + release + apply：`POST /rdp/recommendations/{id}/approve-and-release`，需要同一组控制，token 校验先于审批写入。

UI 会自动从当前 session 申请短时 token；直接调用 API 时必须显式携带。`skip_apply=true` 不要求 apply token，生产仍不得使用 `skip_gate=true`。

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

新 UI 通过 `POST /rdp/v2/runs` 触发，以 `GET /rdp/v2/runs/{run_id}` 跟踪；
旧 `/rdp/tasks/trigger` 与 `/rdp/tasks/status` 仅作兼容入口。

- 可识别 workflow 共 10 个；
- `release_cycle` disabled 且禁止入队；
- 同 workflow 已有 pending/running 时不会重复创建；
- 手动 Run 会立即取得稳定 `run_id`，并优先于尚未启动的定时任务；单 daemon
  正在执行时仍需等待执行槽，UI 会显示真实等待原因；
- daemon 通过数据库 claim 执行，不能手工把状态改为 running；
- 自动重试仅针对已识别的临时基础设施故障，复用同一 `run_id`、递增 attempt，并遵循 `earliest_start_at`；TypeError/ValueError、数据不足、gate 阻断和未知错误不会自动重跑；
- queued Run 可立即取消；running Run 的取消先登记，再由 daemon 终止当前子进程并写 `cancelled` 终态。
- `succeeded_with_warnings` 表示任务完成但存在 allow-failure 或研究批次部分成功；`partially_succeeded` 表示硬失败前后仍有可用阶段产物，两者都不能写成完整成功。
- 运行详情优先看 `run.error_summary` 和首个失败步骤；日志 tail 只是诊断补充，最后一个成功阶段不能覆盖前面的失败。

完整 schedule 见 [平台运行手册](platform_runbook.md)。

## 6. 常见故障

| 症状 | 处理 |
| --- | --- |
| Recommendation 看不到 | 检查 DB-first registry、过滤条件和 source artifact；不要用禁用 CLI show |
| Apply integrity blocked | 修复 Step2 evidence，不继续前向变更 |
| Gate blocked | 根据 failed checks 修复后重跑；不跳 gate |
| Active set 与预期不符 | 停止发布，查 DB active set/history/release/runtime provenance |
| DB active loader 失败 | runtime 已退化到 profile 参数；恢复数据库，不能靠 JSON fallback |
| Run queued | 看 `eligible_at`、trigger kind、当前执行槽和 daemon heartbeat；不要把合法等待写成 DB 故障 |
| Run running 无 heartbeat | daemon 只回收超过 30 秒无 task heartbeat 的 running attempt；旧 attempt 应变 failed/-3，Run 错误码为 `worker_orphan_recovered`，新鲜心跳不得被另一 daemon 误杀 |
| Run 失败后未自动排队 | 先看 failure class；只有 `transient_infrastructure` 会自动重试一次，其余需要修复根因后由 `POST /rdp/v2/runs/{run_id}/retry` 人工重试 |
| Rollback 无 target | 不猜测版本；人工审查合法 parameter set |

## 7. 禁止事项

- 不执行 `apply_active_parameter_set.py`、`approve_recommendation_and_apply.py`、`rdp_rollback_active_parameter_set.py`、`rdp_freeze_parameter_set.py`、`rdp_run_release_cycle.py`；它们均为禁用桩。
- 不直接修改 active parameter JSON 或数据库表绕过 API。
- 不展示 session cookie、apply token、数据库连接串或环境文件内容。
- 不把 `/healthz` 或 HTTP 200 当成发布成功；必须检查 response `ok` 和业务状态。
- 不把 historical Stage/Phase runbook 当作当前操作指南。
