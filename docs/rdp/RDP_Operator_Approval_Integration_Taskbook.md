# RDP Operator / Approval 集成任务书（正式版）

## 1. 任务定位

在《RDP 整合 MVP 开工任务书（正式版）》之后，下一阶段不再继续补底层连接，而是进入：

> **让人真正“看得到、批得动、用得起、退得回”的 operator / approval 集成阶段。**

这一阶段的目标是把 RDP 的研究、归因、execution realism、治理、决策建议，正式接到主交易系统的 operator 观察面与受控应用流程中。

它解决的问题不是：

- 数据能不能读
- 参数能不能加载

而是：

- 谁来看这些结论
- 谁批准 recommendation
- 批准后怎么 apply
- apply 后怎么看效果
- 出问题怎么 rollback

---

## 2. 为什么这是下一阶段

在 MVP 阶段完成后，你已经具备：

1. RDP 可稳定读取主系统 live facts
2. 主系统可加载 active parameter set
3. family/timeframe 参数可被 active set 覆盖

这时系统已经有“技术上的整合能力”，但还缺“运营上的可用能力”。

也就是说，当前最缺的不是下一层分析，而是：

> **从 recommendation 到 operator 决策再到参数生效的受控工作流。**

如果这一步不做，RDP 仍然只是“研发会用”的系统，不是“运营可用”的系统。

---

## 3. 本阶段目标

本阶段完成后，系统应具备以下能力：

1. operator 可以在主系统里直接看到 RDP 关键结论
2. recommendation 可以被人工审批
3. approved recommendation 可以被显式 apply 成 active parameter set
4. active parameter set 的应用有审计记录
5. active parameter set 可以 rollback
6. operator 有最小 SOP，知道何时看、如何批、如何回滚

---

## 4. 本阶段不做什么

### 4.1 本阶段必须做
- operator 只读观察面
- recommendation 审批状态流转
- apply active parameter set
- rollback active parameter set
- apply / rollback 审计记录
- operator SOP

### 4.2 本阶段不做
- 自动批准 recommendation
- 自动 apply 参数
- 自动重启/热更新生产系统
- Phase 6 自动 pause / resume family
- 更复杂的 UI workflow engine
- 企业级权限审批系统

---

## 5. 本阶段拆成 4 个工作包

---

## 工作包 A：Operator 观察面集成

### A.1 目标
让 operator 不进入 artifacts 目录，也能直接看到 RDP 的关键结论。

### A.2 第一版必须展示的内容

#### 1. Active Parameter Sets
展示：
- family
- timeframe
- active parameter set id
- active values
- source round
- source phase
- frozen / candidate 状态
- 最近 apply 时间

#### 2. Latest Recommendations
展示：
- recommendation id
- recommendation type
- target family/timeframe
- target parameter set id
- confidence
- status（draft / approved / rejected / superseded）
- created_at

#### 3. Latest Attribution Summary
展示：
- latest attribution round id
- top failure modes
- family/timeframe 维度的关键失败类别
- latest round status

#### 4. Latest Execution Realism Summary
展示：
- full_fill_ratio
- partial_fill_ratio
- mean_total_execution_cost_bps
- positive_adjusted_edge_ratio
- latest execution round id

#### 5. Latest Family/Timeframe Decisions
展示：
- keep_active / lower_priority / pause / require_review
- readiness
- evidence freshness
- last recommendation id

### A.3 整合位置
建议接入：

- `aats/services/operator`
- API gateway
- operator UI summary 区域

### A.4 建议新增 API

```text
GET /rdp/parameters/active
GET /rdp/recommendations/latest
GET /rdp/recommendations/history
GET /rdp/attribution/latest
GET /rdp/execution/latest
GET /rdp/decisions/latest
GET /rdp/readiness/latest
```

### A.5 建议新增 UI 模块

- Active Parameter Sets 卡片
- Latest Recommendations 列表
- Attribution Summary 卡片
- Execution Realism Summary 卡片
- Family/Timeframe Decisions 表格

### A.6 输出

- operator 只读 API
- UI summary 组件
- `docs/operations/operator_rdp_console.md`

### A.7 验收标准

1. operator 可直接看到 active parameter sets
2. operator 可直接看到 latest recommendation / readiness
3. operator 可看到 attribution / execution 摘要
4. 不需要进入 artifacts 目录翻文件

---

## 工作包 B：Recommendation 审批流

### B.1 目标
把 recommendation 从“结果文件”升级成“受控可审批对象”。

### B.2 当前基础
你已经有：

- `recommendation_registry.json`
- `active_decision_registry.json`
- `evidence_bundle_index.json`

这一阶段要在此基础上补“审批流”。

### B.3 recommendation 生命周期

建议明确以下状态：

- `draft`
- `approved`
- `rejected`
- `superseded`

### B.4 审批动作

第一版至少支持：

#### 1. approve
将 recommendation 从 `draft` 改为 `approved`

#### 2. reject
将 recommendation 从 `draft` 改为 `rejected`

#### 3. supersede
当新 recommendation 替代旧 recommendation 时，将旧的标记为 `superseded`

### B.5 审批元信息

每条 recommendation 应至少补充：

- `approved_by`
- `approved_at`
- `rejected_by`
- `rejected_at`
- `approval_notes`

### B.6 建议新增脚本

```text
scripts/rdp_approve_recommendation.py
```

支持：

- `--recommendation-id`
- `--action approve|reject|supersede`
- `--actor`
- `--notes`
- `--dry-run`

### B.7 建议新增 operator 写接口

```text
POST /rdp/recommendations/{id}/approve
POST /rdp/recommendations/{id}/reject
POST /rdp/recommendations/{id}/supersede
```

### B.8 输出

- recommendation 审批脚本
- recommendation registry 扩展字段
- 审批 API
- `docs/operations/recommendation_approval_workflow.md`

### B.9 验收标准

1. recommendation 可被 approve / reject / supersede
2. 有审批人和审批时间记录
3. registry 状态流转正确
4. operator 可通过 API 或脚本完成审批

---

## 工作包 C：Parameter Apply / Rollback

### C.1 目标
将已批准 recommendation 受控地应用为 active parameter set，并支持回滚。

### C.2 设计原则
apply 必须是：
- 显式动作
- 可审计
- 可回滚

不能是：
- recommendation 自动生效

### C.3 apply 行为

第一版 apply 的行为是：

1. 选定一个 approved recommendation
2. 解析其 `target_parameter_set_id`
3. 写入 `configs/active_parameter_sets/active_parameter_registry.json`
4. 更新 active set 的生效记录
5. 写入 apply history

### C.4 rollback 行为

第一版 rollback 的行为是：

1. 查找某 family/timeframe 的上一个 active parameter set
2. 将其重新写为 active
3. 写入 rollback history
4. 输出 rollback 结果

### C.5 建议新增 registry

```text
artifacts/decision_system/parameter_apply_history.json
```

每条记录至少包含：

- `operation_id`
- `operation_type` (`apply` / `rollback`)
- `family`
- `timeframe`
- `from_parameter_set_id`
- `to_parameter_set_id`
- `recommendation_id`
- `actor`
- `created_at`
- `notes`

### C.6 建议新增脚本

```text
scripts/rdp_apply_approved_recommendation.py
scripts/rdp_rollback_active_parameter_set.py
```

#### `rdp_apply_approved_recommendation.py`
支持：
- `--recommendation-id`
- `--family`
- `--timeframe`
- `--actor`
- `--dry-run`

#### `rdp_rollback_active_parameter_set.py`
支持：
- `--family`
- `--timeframe`
- `--to-parameter-set-id`（可选）
- `--actor`
- `--dry-run`

### C.7 建议新增 operator 写接口

```text
POST /rdp/parameters/apply
POST /rdp/parameters/rollback
GET  /rdp/parameters/apply-history
```

### C.8 文档

新增：

```text
docs/operations/parameter_apply_and_rollback.md
```

内容包括：
- apply 流程
- rollback 流程
- active registry 更新逻辑
- 注意事项
- 回滚触发条件

### C.9 验收标准

1. 可从 approved recommendation apply 到 active parameter set
2. apply history 写入成功
3. rollback 成功
4. rollback 后 active registry 正确更新

---

## 工作包 D：Operator SOP / 运行流程

### D.1 目标
让 operator 真正知道这套机制怎么用。

### D.2 必须回答的问题

#### 1. 什么时候该看 recommendation？
例如：
- 新一轮 Step 2 / Phase 6 完成后
- live 行为出现明显偏差后
- attribution 显示 failure mode 结构变化后

#### 2. 什么时候能 approve？
例如：
- recommendation confidence >= medium
- evidence completeness 足够
- quality monitor 健康
- attribution / execution realism 未出现明显冲突

#### 3. apply 后看什么？
例如：
- active parameter registry 是否更新
- live 行为是否符合预期
- attribution failure mode 是否改善
- readiness 是否变化

#### 4. 什么时候 rollback？
例如：
- live 行为明显恶化
- attribution 出现新的主要失败类型
- execution realism 与预期偏差过大
- operator / reviewer 明确要求回滚

### D.3 必写文档

新增：

```text
docs/operations/rdp_operator_workflow.md
```

建议章节：

1. 背景与目标
2. daily/weekly operator 检查项
3. recommendation review checklist
4. approval checklist
5. apply checklist
6. rollback checklist
7. 常见异常处理
8. 与 artifacts / registry 的对应关系

### D.4 验收标准

1. operator 不依赖口头知识即可执行流程
2. SOP 可指导：
   - 看 recommendation
   - approve / reject
   - apply
   - rollback

---

## 6. 本阶段需要新增/修改的文件建议

### 6.1 脚本
```text
scripts/rdp_approve_recommendation.py
scripts/rdp_apply_approved_recommendation.py
scripts/rdp_rollback_active_parameter_set.py
```

### 6.2 RDP / decision system
可能需要新增/修改：

```text
aats/data_platform/decision_system/
  recommendation_registry.py      # 扩审批字段与流转
  active_parameter_apply.py       # apply / rollback 逻辑
  operator_views.py               # 给 operator API 的聚合读取（可选）
```

### 6.3 主系统 operator 层
需要新增/修改：

```text
aats/services/operator/...
api gateway routes ...
UI summary / tables ...
```

### 6.4 文档
```text
docs/operations/operator_rdp_console.md
docs/operations/recommendation_approval_workflow.md
docs/operations/parameter_apply_and_rollback.md
docs/operations/rdp_operator_workflow.md
```

### 6.5 Registry / 历史记录
```text
artifacts/decision_system/parameter_apply_history.json
```

---

## 7. 最小实现范围（MVP）

第一版只要求：

1. operator 能看 latest RDP summary
2. recommendation 能 approve / reject
3. approved recommendation 能 apply
4. active parameter set 能 rollback
5. 有 apply / rollback history
6. 有最小 SOP

第一版不要求：

- 复杂审批权限模型
- 多级 reviewer
- 自动热加载参数
- 自动重启服务
- UI 上的复杂 workflow designer

---

## 8. 建议实施顺序

### 第 1 步
先做 recommendation approval 脚本 + registry 扩展

### 第 2 步
再做 parameter apply / rollback 脚本 + history

### 第 3 步
把 latest summary 和 active parameters 接到 operator 只读 API

### 第 4 步
最后补 operator 写接口和 SOP

---

## 9. 风险与注意事项

### 9.1 不要跳过 approval 直接 apply
第一版必须保留人工审批。

### 9.2 不要在这一阶段做自动参数生效
主系统读 active parameter set 没问题，但 apply 仍应显式触发。

### 9.3 rollback 一定要有历史记录
否则出了问题无法审计。

### 9.4 operator 可见性与 operator 写权限要分开
只读 summary 可以先广泛开放；approve/apply/rollback 必须更谨慎。

---

## 10. 验收标准

本阶段通过条件：

1. operator 可看到 latest RDP summary
2. recommendation 可被 approve / reject
3. approved recommendation 可 apply 为 active parameter set
4. active parameter set 可 rollback
5. `parameter_apply_history.json` 正常写入
6. `rdp_operator_workflow.md` 完成

---

## 11. 一句话总结

这个阶段的职责是：

> **把 RDP 从“技术上已整合”推进到“operator 真正可用”：能看结论、能批 recommendation、能 apply 参数、能 rollback、能按 SOP 运行。**
