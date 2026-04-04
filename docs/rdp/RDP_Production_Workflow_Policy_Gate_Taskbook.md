# RDP Production Workflow / Policy Gate 集成任务书（正式版）

## 1. 任务定位

在《RDP Operator / Approval 集成任务书（正式版）》之后，下一阶段不再只解决“人能看到、能审批、能 apply”，而是进入：

> **让 RDP 结论正式进入主交易系统的生产工作流与 policy gate。**

这一阶段的目标是把已经具备的：

- recommendation
- approval
- active parameter set
- rollback
- operator SOP

进一步推进为：

- 上线前 gate
- 发布窗口控制
- 参数生效前检查
- 参数生效后观察窗口
- 自动生成 release checklist
- 受控 rollback policy

也就是说，这一阶段解决的问题是：

- recommendation 批准了，什么时候允许 apply？
- apply 前要检查哪些 gate？
- apply 后多久算观察窗口？
- 观察窗口内哪些指标异常必须 rollback？
- 如何把这一套流程固化成 production workflow，而不是靠人记忆？

---

## 2. 为什么这是下一阶段

当前完成 Operator / Approval 集成后，你已经具备：

1. operator 能看到 RDP 结论
2. recommendation 可以 approve / reject
3. approved recommendation 可以 apply
4. active parameter set 可以 rollback

但系统仍然缺少：

> **参数变更进入生产的“制度化门禁”。**

如果没有这一层，系统仍会依赖人工经验去判断：

- 这次 apply 是否应该现在做
- 这次是否需要先看 quality monitor
- 是否必须先检查 attribution / execution realism
- 应用后观察多久
- 触发 rollback 的条件是什么

所以这一步不是新分析能力，而是：

> **把 recommendation -> approval -> apply -> observe -> rollback 变成标准生产流程。**

---

## 3. 本阶段目标

本阶段完成后，系统应具备以下能力：

1. parameter apply 前必须经过 pre-apply policy gate
2. 主系统可以识别当前是否处于允许发布的窗口
3. 每次 parameter apply 都会生成 release record
4. 每次 apply 都会自动绑定观察窗口与观察清单
5. 系统可以根据 policy 自动提示 rollback 建议
6. operator 可以看到当前 parameter release 状态与观察状态

---

## 4. 本阶段不做什么

### 4.1 本阶段必须做
- pre-apply policy gate
- release record
- observation window
- rollback recommendation policy
- release/rollback runbook

### 4.2 本阶段不做
- 自动 approve recommendation
- 自动 apply 参数
- 自动暂停/恢复 family
- 自动重启所有服务
- 全自动无人值守上线

---

## 5. 本阶段拆分为 4 个工作包

---

## 工作包 A：Pre-Apply Policy Gate

### A.1 目标
在 apply active parameter set 之前，增加一个统一的上线前门禁检查。

### A.2 为什么必须有 gate
即使 recommendation 已经 approved，也不代表它一定应该“现在立刻 apply”。

必须在 apply 前检查：

- governance 状态是否健康
- quality monitor 是否健康
- attribution / execution realism 是否存在明显冲突
- 当前是否存在未处理的 failed / partial round
- active family/timeframe 是否已处于需要 review 的状态

### A.3 gate 检查项（第一版建议）

#### 1. Governance Health
- `quality_monitor_summary.json` 是否 healthy / degraded / unhealthy
- 是否存在 critical failures

#### 2. Artifact Freshness
- recommendation 引用的 evidence 是否 stale
- source round 是否过旧
- evidence completeness 是否足够

#### 3. Decision Consistency
- recommendation 是否与 Phase 6 decision 冲突
- 当前 family/timeframe 是否为 `pause` / `require_review`

#### 4. Active Round Health
- 是否存在最近 failed / partial_success 的关键 round
- 是否存在未处理的 retry-required round

### A.4 输出结果
每次 gate 检查至少输出：

- `allow_apply`
- `gate_status` (`pass` / `warn` / `block`)
- `checks`
- `blocking_reasons`
- `warnings`

### A.5 建议新增脚本

```text
scripts/rdp_run_pre_apply_gate.py
```

支持：
- `--recommendation-id`
- `--family`
- `--timeframe`
- `--output`
- `--dry-run`

### A.6 建议新增模块

```text
aats/data_platform/production_workflow/
  pre_apply_gate.py
  gate_rules.py
```

### A.7 输出文件

```text
artifacts/production_workflow/gates/<gate_run_id>/
  pre_apply_gate_result.json
  pre_apply_gate_report.md
```

### A.8 验收标准

1. apply 前可以运行 gate
2. gate 能给出 pass / warn / block
3. block 时不能进入 apply 流程（至少在 workflow 里应阻断）

---

## 工作包 B：Parameter Release Record

### B.1 目标
让每一次 parameter apply 都成为一个可追踪、可审计的“release”。

### B.2 为什么要做
当前 apply 本质上还是“改 active parameter set”。  
但生产环境里，这应该被记录成：

> **一次受控 release 事件。**

### B.3 release record 至少应记录

- `release_id`
- `created_at`
- `family`
- `timeframe`
- `recommendation_id`
- `parameter_set_id`
- `actor`
- `gate_result_ref`
- `apply_result`
- `previous_parameter_set_id`
- `notes`

### B.4 建议新增 registry

```text
artifacts/production_workflow/parameter_release_history.json
```

### B.5 建议新增脚本

```text
scripts/rdp_create_parameter_release.py
```

职责：
1. 读取 approved recommendation
2. 运行或引用 gate
3. 生成 release record
4. 写入 release history
5. 可选调用 apply 逻辑

### B.6 建议新增 operator API

```text
POST /rdp/releases/create
GET  /rdp/releases/latest
GET  /rdp/releases/history
```

### B.7 验收标准

1. 每次 apply 都有 release record
2. release record 可追溯到 recommendation 和 gate
3. 可查询历史 releases

---

## 工作包 C：Observation Window / Post-Apply Monitoring

### C.1 目标
让参数生效后，不是“改完就结束”，而是自动进入观察窗口。

### C.2 设计原则
每次 parameter release 后，都应该产生一段观察窗口：

```text
apply -> observe -> assess -> keep / rollback
```

### C.3 第一版观察窗口需要记录的内容

- `release_id`
- `family`
- `timeframe`
- `started_at`
- `observation_window_hours`
- `status` (`observing` / `completed` / `rollback_recommended`)
- `checklist`

### C.4 观察指标（第一版建议）

#### 1. Live 行为层
- 是否出现 opening 完全归零
- attribution failure mode 是否结构性恶化
- family/timeframe decision 是否退化为 `require_review`

#### 2. 执行层
- execution realism proxy 是否显著变差
- slippage / cost-adjusted edge 是否显著恶化

#### 3. 治理层
- quality monitor 是否从 healthy 变 degraded / unhealthy
- 是否出现新的 critical failure

### C.5 建议新增脚本

```text
scripts/rdp_run_post_apply_observation.py
```

支持：
- `--release-id`
- `--window-hours`
- `--family`
- `--timeframe`

### C.6 建议新增模块

```text
aats/data_platform/production_workflow/
  observation_window.py
  post_apply_monitor.py
```

### C.7 输出文件

```text
artifacts/production_workflow/observations/<release_id>/
  observation_summary.json
  observation_report.md
```

### C.8 验收标准

1. 每个 release 可生成 observation 结果
2. operator 可看到当前 release 是否 still observing
3. observation 可输出 keep / review / rollback_recommended 倾向

---

## 工作包 D：Rollback Recommendation Policy

### D.1 目标
把 rollback 从“人工临时决定”变成有规则支撑的推荐流程。

### D.2 设计原则
第一版 rollback 仍然由人执行，但系统应能给出：

- 是否建议 rollback
- 为什么建议 rollback
- rollback 到哪个 parameter set

### D.3 rollback recommendation 触发条件（第一版建议）

#### 1. Attribution Regression
- 主要 failure mode 明显恶化
- strategy / risk / execution failure 大幅上升

#### 2. Execution Regression
- full_fill_ratio 明显下降
- total_execution_cost_bps 明显上升
- positive_adjusted_edge_ratio 明显下降

#### 3. Governance Regression
- quality monitor degraded / unhealthy
- evidence freshness 严重退化
- 关键 round 失败

### D.4 输出结果
每次评估至少输出：

- `rollback_recommended`
- `severity`
- `reasons`
- `suggested_target_parameter_set_id`

### D.5 建议新增脚本

```text
scripts/rdp_evaluate_rollback_recommendation.py
```

### D.6 建议新增模块

```text
aats/data_platform/production_workflow/
  rollback_policy.py
```

### D.7 输出文件

```text
artifacts/production_workflow/rollback_recommendations/<release_id>/
  rollback_recommendation.json
  rollback_recommendation_report.md
```

### D.8 验收标准

1. release 后可生成 rollback recommendation
2. recommendation 有明确理由
3. recommendation 可指向具体 rollback target

---

## 6. 本阶段建议新增/修改的文件

### 6.1 脚本
```text
scripts/rdp_run_pre_apply_gate.py
scripts/rdp_create_parameter_release.py
scripts/rdp_run_post_apply_observation.py
scripts/rdp_evaluate_rollback_recommendation.py
```

### 6.2 新模块
```text
aats/data_platform/production_workflow/
  pre_apply_gate.py
  gate_rules.py
  release_registry.py
  observation_window.py
  post_apply_monitor.py
  rollback_policy.py
```

### 6.3 文档
```text
docs/operations/pre_apply_gate_workflow.md
docs/operations/parameter_release_workflow.md
docs/operations/post_apply_observation_workflow.md
docs/operations/rollback_recommendation_policy.md
docs/operations/production_parameter_change_runbook.md
```

### 6.4 Registry / 历史记录
```text
artifacts/production_workflow/parameter_release_history.json
```

---

## 7. 最小实现范围（MVP）

第一版只要求：

1. apply 前能跑 pre-apply gate
2. apply 时能生成 release record
3. release 后能生成 observation summary
4. 能输出 rollback recommendation
5. 有对应 runbook

第一版不要求：

- 自动执行 rollback
- 自动热重载参数
- 自动根据 observation 改写 decision registry
- 多级审批流

---

## 8. 建议实施顺序

### 第 1 步
先做 pre-apply gate

### 第 2 步
再做 release record

### 第 3 步
再做 observation window

### 第 4 步
最后做 rollback recommendation policy

---

## 9. 风险与注意事项

### 9.1 不要把 gate 做成阻塞实时交易主链
它只作用于参数发布流程，不进入每笔交易。

### 9.2 observation window 要区分“建议”与“执行”
第一版只能推荐 keep / review / rollback，不要自动操作。

### 9.3 rollback recommendation 不等于自动 rollback
必须保留 operator 决策空间。

### 9.4 release history 一定要完整
否则后续无法审计“哪次参数变更带来了什么效果”。

---

## 10. 验收标准

本阶段通过条件：

1. recommendation approved 后，apply 前能跑 gate
2. 每次 apply 都有 release record
3. 每次 release 都有 observation summary
4. 可生成 rollback recommendation
5. 有 production parameter change runbook

---

## 11. 一句话总结

这个阶段的职责是：

> **把 recommendation / approval / apply 进一步推进为正式的 production parameter change workflow：有 gate、有 release、有 observation、有 rollback recommendation。**
