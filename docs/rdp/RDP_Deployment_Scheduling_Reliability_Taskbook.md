# RDP Deployment / Scheduling / Reliability 集成任务书（正式版）

## 1. 任务定位

在《RDP Production Workflow / Policy Gate 集成任务书（正式版）》之后，下一阶段不再只是定义：

- recommendation
- approval
- apply
- gate
- observation
- rollback recommendation

而是进入：

> **让 RDP 及其与主系统的整合链条可以稳定长期运行的部署、调度与可靠性阶段。**

这一阶段的重点不是再扩新的研究能力，也不是再补新的 operator 流程，而是回答：

- 这些流程谁来定时跑？
- 失败了谁来发现？
- 失败了怎么补跑？
- 不同环境怎么隔离？
- 产物、配置、调度、告警如何长期稳定运转？
- 如何避免“只能靠你自己盯着看”？

---

## 2. 为什么这是下一阶段

在完成前一阶段后，你已经具备：

1. recommendation -> approval -> apply 流程
2. pre-apply gate
3. release record
4. observation window
5. rollback recommendation

但如果没有这一阶段，系统仍然停留在：

> **流程存在，但需要人工持续驱动。**

也就是说，平台仍然缺：

- 调度
- 可靠性
- 告警
- 环境隔离
- 运维级 runbook
- 持续运行能力

因此，这一阶段的目标是：

> **把 RDP 与主系统的整合，从“流程上成立”推进到“生产上能持续可靠运行”。**

---

## 3. 本阶段目标

本阶段完成后，系统应具备以下能力：

1. RDP 各关键任务可以按计划调度执行
2. 关键任务失败时可自动记录、告警、补跑
3. production / staging / research 环境隔离明确
4. artifacts / registries / configs 的目录与生命周期可长期维护
5. operator / maintainer 有可靠性 runbook
6. 整个 RDP integration workflow 可以在无人盯守下持续运行

---

## 4. 本阶段不做什么

### 4.1 本阶段必须做
- 调度策略
- 失败恢复
- 可靠性与告警
- 环境隔离
- registry/artifact 生命周期
- 运维 runbook

### 4.2 本阶段不做
- 新策略 family
- 新 execution model
- 自动改 live 参数
- 企业级分布式任务系统
- 多租户权限体系
- 全自动无人值守上线

---

## 5. 本阶段拆分为 5 个工作包

---

## 工作包 A：RDP Workflow 调度设计

### A.1 目标
将目前需要人工触发的关键流程，正式纳入调度体系。

### A.2 需要纳入调度的任务

至少包括以下类别：

#### 1. 数据层
- historical daemon
- realtime daemon
- Gold build
- gap detection / repair

#### 2. 研究层
- Step 1 calibration（按需）
- Step 2 research round（按周期 / 按数据窗口）
- Phase 3 attribution round
- Phase 4 execution realism round

#### 3. 治理层
- artifact validation
- artifact index rebuild
- quality monitor
- active rounds refresh

#### 4. 决策层
- Phase 6 decision round
- pre-apply gate（事件驱动）
- post-apply observation（按 release 触发）
- rollback recommendation evaluation

### A.3 调度模型建议

第一版不要求企业级任务编排器。  
建议采用：

- cron / Windows Task Scheduler / systemd timer / orchestrator wrapper
- 加一层统一脚本或 manifest 驱动

建议新增统一调度入口：

```text
scripts/rdp_run_scheduled_workflow.py
```

职责：
- 根据 `--workflow` 运行某一类流程
- 统一日志
- 统一退出码
- 统一错误记录

### A.4 建议新增配置

```text
configs/rdp_workflows/
  data_maintenance.json
  research_cycle.json
  governance_cycle.json
  decision_cycle.json
```

### A.5 输出

- workflow 配置文件
- 调度入口脚本
- `docs/operations/rdp_scheduling_strategy.md`

### A.6 验收标准

1. 至少 3 类 workflow 可通过统一入口调度
2. 调度配置清晰，不靠人工拼命令
3. 任务退出码与日志统一

---

## 工作包 B：失败恢复与补跑机制

### B.1 目标
让 workflow 失败后可被发现、分类、补跑，而不是靠人工记忆。

### B.2 需要覆盖的失败类型

#### 1. 任务级失败
- 脚本异常退出
- 关键输入缺失
- 依赖文件不存在
- DB 连接失败

#### 2. 结果级失败
- round 成功启动但产物不完整
- manifest 缺失
- summary/report 缺失
- registry 未更新

#### 3. 质量级失败
- quality monitor 报 unhealthy
- active round stale
- parameter registry 不一致

### B.3 建议新增统一失败记录

```text
artifacts/operations/workflow_failures.json
```

每条记录至少包含：

- `failure_id`
- `workflow`
- `phase`
- `task_name`
- `status`
- `error_type`
- `error_message`
- `created_at`
- `retryable`
- `retry_command`
- `notes`

### B.4 建议新增脚本

```text
scripts/rdp_record_workflow_failure.py
scripts/rdp_retry_workflow_failure.py
```

### B.5 与现有失败逻辑的关系
这一层应复用：
- Phase 5 的 retry / failed round 逻辑
- 各 phase 的 partial_success / failed 退出码

不要重写一套平行系统。

### B.6 输出

- workflow failure registry
- retry 脚本
- `docs/operations/workflow_failure_recovery.md`

### B.7 验收标准

1. 任务失败可记录
2. 失败可判断是否 retryable
3. 至少能对 1 类失败生成 retry command
4. 运维人员可根据 runbook 补跑

---

## 工作包 C：告警与可靠性观察

### C.1 目标
让关键 workflow 问题可被及时发现，而不是等人偶然看到。

### C.2 第一版告警范围

#### 1. 数据层
- historical / realtime daemon 失败
- gold build 长时间未更新
- gap repair 连续失败

#### 2. 治理层
- quality monitor unhealthy
- artifact index 缺失
- active round stale
- parameter registry 损坏

#### 3. 决策层
- decision round 长时间未跑
- latest recommendation stale
- release observation overdue
- rollback recommendation 已出现但未处理

### C.3 第一版告警方式
第一版不要求复杂外部告警系统。  
建议从最简单可落地的方式开始：

- 告警 JSON / markdown summary
- 控制台 / 日志告警
- 可选本地邮件 / webhook wrapper（后续）

### C.4 建议新增输出

```text
artifacts/operations/alerts/
  current_alerts.json
  alert_history.json
```

### C.5 建议新增脚本

```text
scripts/rdp_run_reliability_check.py
scripts/rdp_build_alert_summary.py
```

### C.6 输出

- alert summary
- reliability check 脚本
- `docs/operations/reliability_alerting.md`

### C.7 验收标准

1. 至少能识别 5 类关键异常
2. 当前 alerts 可生成汇总文件
3. operator 可通过 summary 看出当前主要风险

---

## 工作包 D：环境隔离与部署规范

### D.1 目标
明确 RDP integration 在不同环境中的部署方式，避免 production/staging/research 混淆。

### D.2 需要明确的环境

至少区分：

- local / dev
- staging
- production

### D.3 每个环境必须明确的内容

#### 1. 数据库
- production DB
- research DB
- live readonly DB

#### 2. artifact root
- 不同环境必须使用不同路径

#### 3. active parameter registry
- 不同环境必须隔离

#### 4. release / recommendation registry
- 不同环境必须隔离

### D.4 建议新增配置项

例如：

- `AATS_ENVIRONMENT=dev|staging|prod`
- `RDP_ENVIRONMENT=dev|staging|prod`
- `RDP_ARTIFACT_ROOT`
- `RDP_WORKFLOW_ROOT`
- `AATS_ACTIVE_PARAMETER_REGISTRY_PATH`

### D.5 建议新增文档

```text
docs/operations/environment_isolation_for_rdp.md
```

内容包括：
- 哪些目录必须隔离
- 哪些 DB 必须隔离
- 哪些 registry 绝不能跨环境共用
- 如何切换环境

### D.6 验收标准

1. dev/staging/prod 的 registry 与 artifact root 明确隔离
2. production 环境不读取 dev/staging 产物
3. 环境说明文档完整

---

## 工作包 E：长期运行 Runbook / 运维交接

### E.1 目标
把前面所有 workflow、失败恢复、告警、环境隔离整合成长期运行手册。

### E.2 文档必须回答的问题

#### 1. 每天 / 每周要看什么？
- quality monitor
- latest recommendation
- latest release observation
- current alerts

#### 2. 哪些任务是定时跑的？
- 什么时候跑 research
- 什么时候跑 decision
- 什么时候跑 quality/reliability check

#### 3. 失败了怎么处理？
- retry
- rerun
- escalate
- rollback

#### 4. 环境怎么隔离？
- 哪些路径不能混用
- 哪些 DB 不能混用

#### 5. operator / maintainer 交接时要交什么？
- 当前 active parameter sets
- 当前 open alerts
- 当前 release observation 状态
- 当前 pending rollback recommendations

### E.3 必写文档

新增：

```text
docs/operations/rdp_reliability_runbook.md
docs/operations/rdp_workflow_calendar.md
docs/operations/rdp_environment_matrix.md
```

### E.4 验收标准

1. maintainer 能依据 runbook 独立完成日常操作
2. 当前 workflow、告警、环境关系有清晰说明
3. 交接不依赖口头知识

---

## 6. 本阶段建议新增/修改的文件

### 6.1 脚本
```text
scripts/rdp_run_scheduled_workflow.py
scripts/rdp_record_workflow_failure.py
scripts/rdp_retry_workflow_failure.py
scripts/rdp_run_reliability_check.py
scripts/rdp_build_alert_summary.py
```

### 6.2 新模块
```text
aats/data_platform/operations/
  scheduler.py
  workflow_dispatcher.py
  failure_registry.py
  retry_manager.py
  alerting.py
  reliability_checks.py
  environment_guard.py
```

### 6.3 配置
```text
configs/rdp_workflows/
  data_maintenance.json
  research_cycle.json
  governance_cycle.json
  decision_cycle.json
```

### 6.4 Registry / Artifact
```text
artifacts/operations/workflow_failures.json
artifacts/operations/alerts/current_alerts.json
artifacts/operations/alerts/alert_history.json
```

### 6.5 文档
```text
docs/operations/rdp_scheduling_strategy.md
docs/operations/workflow_failure_recovery.md
docs/operations/reliability_alerting.md
docs/operations/environment_isolation_for_rdp.md
docs/operations/rdp_reliability_runbook.md
docs/operations/rdp_workflow_calendar.md
docs/operations/rdp_environment_matrix.md
```

---

## 7. 最小实现范围（MVP）

第一版只要求：

1. 至少 3 类 workflow 可调度
2. workflow failure 可记录
3. reliability check 可生成 alerts
4. dev/staging/prod 隔离策略明确
5. 有长期运行 runbook

第一版不要求：

- 外部监控平台深度集成
- 多机分布式调度
- 自动化自愈
- 复杂 on-call 系统

---

## 8. 建议实施顺序

### 第 1 步
先做 workflow 调度入口

### 第 2 步
再做 failure registry + retry

### 第 3 步
再做 reliability checks + alerts

### 第 4 步
最后补环境隔离与长期运行 runbook

---

## 9. 风险与注意事项

### 9.1 不要让调度直接控制实时交易主链
调度的是 RDP integration workflow，不是每笔交易。

### 9.2 不要在没有环境隔离前就共享 artifacts / registries
否则后续很难清理。

### 9.3 alerts 第一版追求可落地，不追求花哨
先让问题能被发现，比先接复杂外部告警更重要。

### 9.4 失败恢复一定要与已有 phase 退出码兼容
不要重写新的状态语义。

---

## 10. 验收标准

本阶段通过条件：

1. 至少 3 类 workflow 可按计划执行
2. workflow failure 有记录和 retry 支持
3. reliability check 可生成 alert summary
4. dev/staging/prod 隔离清晰
5. 有长期运行 runbook

---

## 11. 一句话总结

这个阶段的职责是：

> **把 RDP integration 从“流程上成立”推进到“长期可运行”：有调度、有失败恢复、有告警、有环境隔离、有可靠性 runbook。**
