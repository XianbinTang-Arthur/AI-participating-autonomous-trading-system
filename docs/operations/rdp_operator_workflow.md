# RDP Operator 工作流 SOP

> 文档状态：现行操作说明
> 最后核对：2026-08-28（起始 HEAD `c15ccd2d`，含参数内容身份与 Phase 3/4 子产物语义 LF-A 候选；以本文档所在 HEAD 为准）
> 核对范围：当前 Operator API/UI、Run/attempt queue、发布资格和回滚风险收敛代码；不证明现场状态

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
- apply-capable recommendation 精确引用成功的 Phase 6 round，且当前 qualification policy、combo candidate、显式时区完成时间与 168 小时有效期均通过；系统会将合法时区偏移归一为 UTC，naive/未来/不一致时间失败关闭；
- Phase 6 combo candidate 携带可复算的参数值指纹，且与当前 registry 及锁内 DB values 一致；
  缺指纹的旧 round 或同 ID 内容漂移必须重新研究，不能手工补 hash 或借新 round 替旧证据背书；
- approve-and-release 内部签发的晋级 capability 最长只存活 5 分钟，并受上述证据剩余寿命进一步限制；即使对象被序列化/反序列化，进程内 issuer identity 也无法恢复为有效授权，因此不能跨请求长期复用；
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

当前可执行入口：

- release + apply：`POST /rdp/releases/create`，需要 write access、integrity/gate 和 `action=apply` token；
- approve + release + apply：`POST /rdp/recommendations/{id}/approve-and-release`，需要同一组控制，token 校验先于审批写入。

`POST /rdp/parameters/apply` 是无写入迁移失败入口，固定返回
`code=release_required`；不得把它写成发布或故障重试通道。

UI 会自动从当前 session 申请短时 token；直接调用 API 时必须显式携带。`skip_apply=true` 不要求 apply token，生产仍不得使用 `skip_gate=true`。

发布后：

1. 核对 active set、apply history 和 release；
2. 通过标准部署重建 runtime；
3. 核对 Settings Provenance；
4. 检查主系统 health/recovery/reconciliation；
5. 进入 observation window。

## 4. 回滚

本节是 Operator 人工入口。另有启用中的 `observation_cycle` 内部风险收敛路径：它可在
精确 release/provenance、combo lock、clean attempt 和数据库终态证明全部满足时自动回滚、
取消旧意图或 soft pause。legacy/缺证明/中断记录进入 `reconciliation_required`，不会自动
重放；这条内部路径不使用浏览器 rollback token，也不等于允许自动 release。

1. 运行/查看 `POST /rdp/rollback-recommendation/evaluate` 的建议；
2. 确认合法 previous/explicit target；
3. 当前 Operator session 申请 `action=rollback` token；
4. 携带 token 调用 `POST /rdp/parameters/rollback`；
5. 核对 active/history/release/provenance 和主交易事实；若该 release 已有 pending
   effectiveness 风险义务，等待/触发受控 observation cycle 依据精确 operation、target、
   history、active 与时间事实将其收口为 `enforced`。只见 release=`rolled_back` 不算完成。

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
- `research_cycle` 的完整 RDP 默认执行 Phase 3 live attribution。后台必须已注入只读 `RDP_LIVE_DATABASE_URL`；缺失或不可查询时任务失败关闭，不会隐式改成 replay-only。纯回放只允许维护者在受控 CLI 中显式使用 `--replay-only`，其结果不能通过发布 readiness。
- full pipeline 只要包含 Step 2、Step 3、Phase 3 或 Phase 4，就必须在启动前提供同一非空
  `--start/--end`（或 `--lookback-days`）；仅刷新既有治理数据并运行 Decision 时才可不带研究窗口。
  Step 2/Step 3 日志应实时可见，后续阶段必须显示并消费本次运行 marker 绑定的精确产物；Phase 3/4
  也必须分别返回绑定其 immutable manifest 与 Step 3 lineage 的唯一 `round_result.json` marker。
  在 managed DB 环境，Step 2、Step 3、Phase 3、Phase 4 均须先成功写入对应的 exact research-round
  snapshot，随后才能写入并输出本轮 `round_result.json` marker；snapshot 写入失败时阶段以非零退出，
  不得短暂显示成功，也不得以文件自洽或 lazy bootstrap 继续。Step 3 snapshot 还须逐字锚定
  candidate/manifest 及其 Step 2 父 snapshot。
  Decision 还必须显式携带 `--expected-step2-round-id`，从治理 DB 读取该 exact snapshot 并把其
  typed fingerprint 写入 Phase 2 evidence；promotion 校验会要求它与目标 Step 3 managed snapshot
  固化的父 Step 2 ID/fingerprint 完全一致。运行期间出现更新的 Step 2 不能以 `latest` 替换本轮父证据。
  出现 marker 缺失/重复、digest 不符、四 combo topology 不完整或 `step3_result_not_bound` 时停止，
  不手工改为某个 latest 目录。仅续跑 Governance/Decision 时必须显式提供 Step 3、Phase 3、Phase 4
  三份精确 result refs；Decision 只接受与 Step 3 父 Step 2 及这两个 Phase round ID 精确一致的
  managed DB snapshot。
- Phase 3 必须同时证明 live 查询成功、至少一个 `family + symbol + timeframe + signal_bar_start` 精确对齐，并且没有缺 lineage 的 live intent；旧 intent 不按 `created_at` 猜测或回填。

完整 schedule 见 [平台运行手册](platform_runbook.md)。

## 6. 常见故障

| 症状 | 处理 |
| --- | --- |
| Recommendation 看不到 | 检查 DB-first registry、过滤条件和 source artifact；不要用禁用 CLI show |
| Apply integrity blocked | 修复 Step2 evidence，不继续前向变更 |
| Promotion qualification blocked / audit-only | 核对 recommendation 的精确 evidence round；不能用较新的健康 round 替代，也不能原地补写历史证据 |
| Target combo Phase 2 hard gate blocked | 目标 combo 必须同时满足 opening 实验数 ≥ 1、最大 opening count ≥ 1、平均 positive-edge ratio ≥ 0.20；不得借用其他 combo 的 edge 或靠 Phase 3/4/治理高分覆盖 |
| `derivatives_phase2_promotion_evidence_unavailable` | 当前 legacy Step 2 只有探索/审计证据，尚无与 SWAP instrument/参数身份绑定的资格轮；保持 hold，等待独立 derivatives qualification producer，禁止借用 SPOT bundle 或手工补 metrics |
| Parameter fingerprint invalid / mismatch | 停止发布；确认 parameter set 未被同 ID 改写，用新 ID 重跑 Phase 6 与审批，不手改历史 snapshot |
| Step 3 import `round_metadata_invalid` / `round_content_conflict` | 停止该轮导入；核对 current schema、round/symbol/dataset、manifest status、candidate size/SHA-256，以及 exact `phase2_step3`/Step 2 managed snapshot；不以文件回灌 DB，不覆盖或手改历史 artifact |
| Step 3 import `import_lock_busy` | 已有 importer 持有 DB 全局锁；等待该次终态后重试，不并行启动第二条导入链 |
| Step 3 import `supersession_deferred` | 新参数可能已安全入库，但旧候选缺少可证明的更早 manifest；保留双方，补齐真实 provenance 或人工审查，不能按目录名猜顺序 |
| Phase 3/4 `child_result_invalid` / `child_artifact_invalid` | 停止该 combo；核对唯一 result marker、sidecar scope、UTC 时间、参数指纹及输出 SHA/size；若为 artifact invalid，再核对完整 CSV 表头、lineage、有限数值和明细/汇总重算结果。不得手动指定、复制“最新”子目录或修改历史产物 |
| Gate blocked | 根据 failed checks 修复后重跑；不跳 gate |
| Active set 与预期不符 | 停止发布，查 DB active set/history/release/runtime provenance |
| DB active loader 失败 | runtime 已退化到 profile 参数；恢复数据库，不能靠 JSON fallback |
| Phase 3 显示 zero exact alignment / unattributable lineage | 先确认标准部署已应用 root migration、新 intent 已写入 timeframe/bar/parameter/generation/snapshot lineage；旧记录不猜测回填。历史市场数据覆盖度另行处理 |
| 数据治理显示覆盖缺口、collector unknown 或归档积压 | 查看 `GET /rdp/v3/data-governance` 的只读快照，并按 [`rdp_historical_data_recovery_runbook.md`](rdp_historical_data_recovery_runbook.md) 分类为官方回填、确定性重建、只能重新采集或不可恢复；不得伪造 gap/heartbeat |
| Run queued | 看 `eligible_at`、trigger kind、当前执行槽和 daemon heartbeat；不要把合法等待写成 DB 故障 |
| Run running 无 heartbeat | daemon 只回收超过 30 秒无 task heartbeat 的 running attempt；旧 attempt 应变 failed/-3，Run 错误码为 `worker_orphan_recovered`，新鲜心跳不得被另一 daemon 误杀 |
| Run 失败后未自动排队 | 先看 failure class；只有 `transient_infrastructure` 会自动重试一次，其余需要修复根因后由 `POST /rdp/v2/runs/{run_id}/retry` 人工重试 |
| Rollback 无 target | 不猜测版本；人工审查合法 parameter set |
| Rollback `in_progress` / `reconciliation_required` | 保持 combo 前向变更阻断；核对 exact attempt、active/history/release 与 action proof，禁止手改状态或重放 |
| Recommendation 写入返回 mirror degraded | DB canonical 迁移可能已成功；先读取权威状态并修复审计镜像，不要盲目重复 approve/reject/supersede |

## 7. 禁止事项

- 不执行 `apply_active_parameter_set.py`、`approve_recommendation_and_apply.py`、`rdp_rollback_active_parameter_set.py`、`rdp_freeze_parameter_set.py`、`rdp_run_release_cycle.py`；它们均为禁用桩。
- 不直接修改 active parameter JSON 或数据库表绕过 API。
- 不展示 session cookie、apply token、数据库连接串或环境文件内容。
- 不把 `/healthz` 或 HTTP 200 当成发布成功；必须检查 response `ok` 和业务状态。
- 不把 historical Stage/Phase runbook 当作当前操作指南。
