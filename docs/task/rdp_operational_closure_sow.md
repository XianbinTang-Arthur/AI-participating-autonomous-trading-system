# RDP 收尾工作 SOW

## 1. 背景与当前状态

主证据链已经闭合，并完成了一轮真实 RDP round 验证：

- `Step2 -> Step3 -> Phase3 -> Phase4 -> Phase6` 的文件 artifacts 已与回测事实一致。
- Phase 6 不再把 Phase 2 证据读成全局空值。
- recommendation / decision / governance 文件侧已经能正确落盘。

当前剩余问题不再是“研究结果失真”，而是“运营闭环和自动化闭环还没有完全收口”：

1. 治理 DB 同步仍有断点，导致部分 registry / snapshot 只写文件不写 DB。
2. approved recommendation 之后仍缺少自动进入 `gate -> release -> apply` 的受控链路。
3. `schedule_hint` 仍只是说明文字，没有真正的内部调度入队器。
4. 策略调优仍主要依赖人工读 artifacts；需要自动生成调优提案，但仍保留人工审核边界。

## 2. 实施边界

- 不取消人工审批。
- 不做“研究完成即自动改 live 参数”。
- 不绕过 `gate / release / observation / rollback` 生产约束。
- 策略调优只自动生成“待审核提案”，不直接改 live 配置。
- 尽量复用现有 recommendation registry、release registry、task queue、daemon、environment guard。

## 3. 优先级

1. DB 同步
2. 自动应用链
3. 调度能力
4. 自动调优待审核

## 4. 详细目标与方案

### 4.1 DB 同步

#### 目标

- recommendation registry、active decision registry、decision round snapshot 统一复用项目真实的治理 DB 解析链。
- 保持 DB-first + 文件 fallback，不因为 DB 故障阻断文件 artifacts。

#### 备选方案评审

方案 A：继续只读 `AATS_ACTIVE_PARAMETER_DB_URL` / `RDP_DATABASE_URL`

- 优点：改动最小。
- 缺点：与 `aats.data_platform.db.get_engine()` 的真实解析链不一致，环境稍有差异就会继续分叉。
- 结论：拒绝。

方案 B：`try_governance_db()` 复用 `get_settings().database_url`

- 优点：与项目内部 DB 连接逻辑一致，兼容 `AATS_ACTIVE_PARAMETER_DB_URL -> RDP_DATABASE_URL -> settings.database_url`。
- 缺点：需要补单元测试，避免引入隐式副作用。
- 结论：采用。

#### 实施

- 在 `aats/data_platform/governance/_db_util.py` 增加统一的治理 DB URL 解析 helper。
- `try_governance_db()` 改为：
  - 优先显式治理 URL
  - 再退回 RDP URL
  - 最后复用 `get_settings().database_url`
- DB 失败时保持 best-effort 行为：记录 warning，但不阻断文件写入。

#### 验收

- recommendation / decision registry 的 DB 同步恢复。
- `decision_round_snapshot` 能成功写入 `governance.decision_round_snapshots`。
- 单测覆盖显式 env 与 settings fallback 两条路径。

### 4.2 自动应用链

#### 目标

- 在不自动审批的前提下，让“已批准 recommendation”自动进入现有 `gate -> release -> apply` 受控链。
- 保持 apply 的生产边界：必须经过 approved recommendation、gate、release。

#### 备选方案评审

方案 A：Decision round 直接写 `active_parameter_sets`

- 优点：链路最短。
- 缺点：破坏人工审批与 gate 边界，风险不可接受。
- 结论：拒绝。

方案 B：自动审批后立即 apply

- 优点：表面上一键闭环。
- 缺点：把“批准”从人工控制变成系统动作，和当前治理设计冲突。
- 结论：拒绝。

方案 C：新增 approved-only `release_cycle`

- 优点：复用现有 `create_parameter_release()`，不改变审批边界，只自动推进已批准项。
- 缺点：需要处理重复 release、同 combo 多条 approved recommendation 的选择策略。
- 结论：采用。

#### 实施

- 新增 `release_cycle`：
  - 只处理 `status=approved` 且带 `target_parameter_set_id` 的 `parameter_upgrade` recommendation。
  - 同一 combo 只取最新 approved recommendation。
  - 若该 recommendation 已有 release 记录，则跳过。
  - 调用现有 `create_parameter_release()` 执行 `gate + release + apply`。
- 输出 release cycle summary/report artifacts。
- `dry-run` 必须真实无副作用，只评估不创建 release。

#### 验收

- 新的 approved recommendation 在 `release_cycle` 运行后生成 release history。
- apply 成功时写入 `active_parameter_sets` 与 apply history。
- 同一 recommendation 不会重复 release。
- `dry-run` 不会创建 release 或 apply。

### 4.3 调度能力

#### 目标

- 让 workflow 从“带 `schedule_hint` 的文档配置”变成“真正可入队执行的调度配置”。
- 保持现有 `rdp_task_daemon + rdp_task_queue` 架构，不新增旁路执行器。

#### 备选方案评审

方案 A：继续依赖外部 cron / Task Scheduler

- 优点：实现成本最低。
- 缺点：项目内部仍然没有真正的调度能力，`schedule_hint` 仍只是注释。
- 结论：不满足本次目标。

方案 B：直接解析 `schedule_hint` 自然语言

- 优点：不用改现有 JSON。
- 缺点：字符串语义脆弱、难测试、难维护。
- 结论：拒绝。

方案 C：为 workflow 增加结构化 `schedule`，由内部 scheduler 只负责“入队”

- 优点：与现有 task queue / daemon 自然拼接，调度与执行解耦，容易测试。
- 缺点：需要为 workflow JSON 增加新字段，并维护调度状态文件。
- 结论：采用。

#### 实施

- 在 workflow JSON 中新增可选 `schedule` 字段，保留 `schedule_hint` 作为展示文案。
- 新增 `workflow_scheduler`：
  - 按 UTC 解析 `daily / weekly / hourly` 三类结构化计划。
  - 只判断“是否到点、是否需要入队、是否已有 active task”。
  - 将调度状态落到 `artifacts/operations/workflow_scheduler_state.json`，避免重复入队。
- 新增 `rdp_schedule_workflows.py` 独立入口。
- `rdp_task_daemon.py` 增加 `--enable-scheduler`，允许同一 daemon 一边调度一边执行。
- `dry-run` 必须真实无副作用，不写队列也不写状态。

#### 验收

- 配置了 `schedule` 的 workflow 能被自动入队。
- 同一时间窗口不会重复入队。
- 仍通过现有 task queue / daemon 执行，不直接旁路运行命令。
- `dry-run` 不会创建任务或修改状态。

### 4.4 自动调优待审核

#### 目标

- 把“是否该调 `max_acceptable_cost_bps` / `min_safe_net_edge_bps` / `score_stability_threshold`”从人工读日志，收口成结构化调优审查。
- 自动生成调优提案，但状态默认 `pending_review`，仍由人工审核。
- 不直接修改 live 参数，不绕过人工边界。

#### 备选方案评审

方案 A：发现 blocker 就自动修改阈值

- 优点：省人工。
- 缺点：实盘风险过高，也会掩盖真实策略问题。
- 结论：拒绝。

方案 B：只输出诊断报告，不生成结构化提案

- 优点：安全。
- 缺点：仍需要人工从报告里二次提炼行动，自动化闭环不完整。
- 结论：拒绝。

方案 C：自动生成“待审核调优提案”注册表

- 优点：兼顾自动化、可追踪、人工审核边界。
- 缺点：需要定义去重、 supersede、审核状态流转。
- 结论：采用。

#### 实施

- 新增 `strategy_tuning_review`：
  - 读取最新 Step2 `scan_comparison_summary.json`
  - 结合最新 Phase4 execution realism 结果
  - 输出每个 combo 的 blocker 主因、成本压力、安全边际压力、稳定性压力
  - 自动生成结构化调优提案
- 新增 `strategy_tuning_proposals.json` 治理注册表：
  - proposal_id
  - combo_key / parameter / current_value / proposed_value
  - status=`pending_review`
  - review_required=true
  - rationale / confidence / reviewed_by / reviewed_at
- 新增审核脚本，对提案执行 approve / reject。
- 规则上明确：
  - 只有 `cost_exceeds_max_acceptable` 成为主阻断且平均成本贴近阈值时，才建议调高 `max_acceptable_cost_bps`
  - 主阻断为 `net_edge_below_safe_minimum` 且 Phase4 成本后边际为正时，才建议下调 `min_safe_net_edge_bps`
  - 主阻断为 `score_not_stable` 且执行后边际为正时，才建议下调 `score_stability_threshold`
- 对同一 combo + parameter 的未审核旧提案自动 supersede，避免审核队列膨胀。

#### 验收

- 能生成结构化 tuning review artifacts。
- 能自动生成 `pending_review` 调优提案注册表。
- 对当前最新 round，应得出“不建议优先单独重估 `max_acceptable_cost_bps`”的结论。
- 审核脚本能把提案状态从 `pending_review` 流转为 `approved/rejected`。

## 5. 模块职责与接口

- `governance/_db_util.py`
  - 负责治理 DB URL 解析与连通性探测。
- `production_workflow/release_cycle.py`
  - 负责 `approved recommendation -> release/apply` 批处理。
- `operations/workflow_scheduler.py`
  - 负责读取 workflow schedule、判断到点、入队任务。
- `operations/strategy_tuning_review.py`
  - 负责读取最新 research/execution artifacts 并生成调优审查。
- `operations/strategy_tuning_registry.py`
  - 负责调优提案注册表的去重、supersede、审核状态流转。
- `scripts/rdp_run_release_cycle.py`
  - release cycle CLI。
- `scripts/rdp_schedule_workflows.py`
  - scheduler CLI。
- `scripts/rdp_run_strategy_tuning_review.py`
  - tuning review CLI。
- `scripts/rdp_review_strategy_tuning_proposal.py`
  - 调优提案审核 CLI。

## 6. 数据库、事务与一致性

- 不新增表结构。
- 继续复用：
  - `governance.recommendations`
  - `governance.active_decisions`
  - `governance.active_parameter_sets`
  - `governance.parameter_apply_history`
  - `governance.decision_round_snapshots`
  - `governance.rdp_task_queue`
- release/apply 仍使用现有 DB 事务边界。
- scheduler 只创建 queue task，不直接执行 workflow。
- 调优提案注册表使用文件治理，不直接落 DB。

## 7. 错误处理与幂等

- DB 不可用时：
  - registry / snapshot 继续落文件
  - 明确 warning，不静默伪成功
- release cycle：
  - 已有 release 的 recommendation 直接跳过
  - gate block 记为已处理但未 apply，不自动无限重试
  - dry-run 不产生副作用
- scheduler：
  - 同一 schedule window 只处理一次
  - active task 存在时不重复入队
  - dry-run 不写队列、不写状态
- tuning review：
  - 缺少 round 时返回可读的“无可用数据”结果，而不是异常退出
  - 同一 combo + parameter 的重复未审核提案自动 supersede 或复用

## 8. 监控与运维

- workflow scheduler state 落盘到 `artifacts/operations/workflow_scheduler_state.json`
- release cycle summary 落盘到 `artifacts/production_workflow/release_cycles/<run_id>/`
- tuning review 落盘到 `artifacts/strategy_tuning_reviews/<run_id>/`
- tuning proposal registry 落盘到 `artifacts/governance/strategy_tuning_proposals.json`
- `decision_cycle` 可附带 tuning review task
- `release_cycle` 作为独立 workflow，可被 daemon / task queue 调度

## 9. 测试策略

- 单元测试：
  - governance DB URL fallback
  - release cycle 的筛选 / 去重 / 幂等 / dry-run
  - scheduler 的到点判断 / 去重入队 / active task 跳过 / dry-run
  - tuning review 的 blocker 判定、提案生成、pending_review 注册表、审核流转
- 回归：
  - `ruff check aats/ --fix`
  - `pytest tests/unit/ -x -q`
  - 受影响的 WSL2 集成测试

## 10. 验收标准

1. Phase 6 写 recommendation / decision / snapshot 时，DB 与文件都能同步。
2. 人工 approve 后，无需再手工 freeze/apply，`release_cycle` 能走通既有 `gate -> release -> apply`。
3. daemon 在启用 scheduler 时，能按照 workflow `schedule` 自动入队。
4. tuning review 能对最新 round 产出结构化提案，并默认置为 `pending_review`。
5. `dry-run` 在 release cycle 和 scheduler 上都是真正无副作用。
