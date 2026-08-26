# RDP Platform V3 架构重构设计

> 文档状态：已批准实施的设计基线
> 最后核对：2026-08-25（起始 HEAD `70f1a581a81f55697c9b68167539ad0db86fc06a`，包含同一工作树内已完成的 RDP 业务逻辑整改）
> 核对范围：当前代码、RDP workflow JSON、ORM/迁移、Operator API、Dashboard 前端与相关测试
> 运行边界：本文档不证明当前容器、数据、候选、参数或交易状态；这些必须在发布后现场复核

## 1. 目标与非目标

本次重构的产品目标是把 RDP 从“能触发一组脚本并查看分散结果”改造为“可理解、可操作、可审计、失败关闭的研究运营控制面”。Operator 应能在一个页面回答：

1. 当前数据、研究、治理、发布和观察分别处于什么阶段；
2. 现在可以执行哪个动作，为什么其他动作被阻断；
3. 点击运行后是“已接收、等待执行槽、运行中”还是“被门禁拒绝”；
4. 一个研究结论的数据、实验、回放、执行可行性、Gate、发布和 active parameter 证据如何串联；
5. 当系统没有符合门禁的候选时，为什么必须停止而不是“挑一个最不差的”应用。

非目标：

- 不重写已通过业务逻辑审查的 replay、attribution、execution-realism 数学引擎；
- 不引入 MLflow、Optuna、Airflow 或新的外部编排基础设施；
- 不解除 `release_cycle` 冻结或 live profile NO-GO；
- 不把“最新”、“置信度高”或“回测收益最高”单独定义为可应用的最优结果。

## 2. 当前系统真实流程

### 2.1 数据与研究主链

```text
OKX REST / 历史 ZIP / 主交易只读事实
  -> staging -> validate/normalize -> bronze -> silver -> gold
  -> Phase 2 参数研究
  -> Step 3 扩展扫描
  -> candidate import
  -> Phase 3 live/replay attribution
  -> Phase 4 execution realism
  -> Phase 5 governance refresh
  -> decision round / recommendation
  -> Operator approval -> pre-apply gate -> release/apply
  -> governance.active_parameter_sets
  -> 主交易 runtime 在重建时读取
  -> observation / rollback recommendation
```

`research_cycle` 只执行“数据刷新 + full pipeline”；不会自动批准或发布。`release_cycle` 同时被 workflow 配置和入队 allowlist 冻结。手工运行通过 `/rdp/v2/runs` 立即创建逻辑 Run，但 daemon 是单执行槽；如果已有长任务，新 Run 会合法等待。

### 2.2 现有调度流程

```text
10 份 configs/rdp_workflows/*.json
  -> workflow_scheduler 计算 UTC slot / 合并 missed slots
  -> governance.rdp_task_queue
  -> daemon 按 operator_recovery > operator > retry > scheduled 排序
  -> FOR UPDATE SKIP LOCKED claim
  -> workflow_dispatcher shell=False 子进程执行
  -> Run / Attempt / Step / Event 单调投影
  -> 仅 transient_infrastructure 自动重试一次
```

### 2.3 现有控制面问题

1. RDP 页面依赖 `rdpRuns`、`rdpControl`、`rdpWorkbenchOverview`、`rdpWorkbenchItems`、`rdpWorkbenchAlerts`、`rdpTuningOverview`、`rdpTuningProposals` 七份快照；不同生成时间可能导致页面短暂自相矛盾。
2. `rdp_control_summary.py` 同时承担数据读取、业务归纳、中文文案和 UI action 生成，超过 2,000 行，责任边界不清。
3. `rdp-control-panel.js` 超过 1,100 行，运行、研究、发布、观察和 tuning 互相穿插；前端还保留 legacy fallback，难以证明每个状态来自同一快照。
4. Run 已有稳定 ID 和优先级，但 API/UI 没有把“单执行槽”、“前方任务”和“daemon 待领取”建模为一等状态，使 queued 容易被理解成“点击没生效”。
5. 研究结论与发布候选分在不同卡片；没有一个稳定的 lifecycle stage 模型。
6. “最优候选”缺少统一可比较目标。现有 confidence、recommendation type、Gate 和执行证据是门禁，不是一个可随意加权的收益排名。

## 3. 市场成熟实践对标

| 平台 | 官方实践 | AATS 可借鉴 | 不直接照搬的原因 |
| --- | --- | --- | --- |
| QuantConnect Research Pipeline | 把 Idea、Research、Backtest、Paper Trading、Live Trading 建模为显式阶段 | 工作台以研究生命周期而不是后端文件名组织 | AATS 是单系统、强风控、本地运行，不需要复制云 IDE/计费调度 |
| QuantConnect Research Guide | 强调先验假设、限制反复回测和参数数量、警惕过拟合 | UI 显示 hypothesis/试验计数/参数复杂度/封存状态，不只显示收益 | 官方经验阈值只能作警示，不应取代 AATS 现有统计门 |
| MLflow Tracking / Model Registry | Experiment 容纳 Runs，Run 绑定参数、指标、代码版本和 artifact；元数据与大 artifact 分存 | 保持 Run 为一等资源，统一呈现 lineage 和 artifact 指针 | AATS 已有 Postgres + artifact registry；再引入 MLflow 会形成第二真源 |
| Microsoft Qlib Recorder | Experiment Manager -> Experiment -> Recorder/Run，Run 记录 metrics、params、tags 和 artifacts | 把研究对象、执行记录和产物层次分开 | 不在本次把 84 张表迁往新 Recorder 实现 |
| NautilusTrader | 回测与 live 共享执行语义和时间模型 | 在 UI 中显示 execution model version、因果时间线和 paper/live parity 边界 | AATS 不会在本次替换主交易引擎 |
| Optuna | Study -> Trials，支持剪枝、并行和可视化 | 将候选扫描显示为一个研究组和可追踪试验 | 本次不用自动调参绕过预注册、holdout 和多重检验门 |

官方来源：

- <https://www.quantconnect.com/docs/v2/cloud-platform/research-pipeline>
- <https://www.quantconnect.com/docs/v2/cloud-platform/backtesting/research-guide>
- <https://mlflow.org/docs/latest/ml/tracking/>
- <https://github.com/microsoft/qlib/blob/main/docs/component/recorder.rst>
- <https://nautilustrader.io/docs/>
- <https://optuna.readthedocs.io/en/stable/>

## 4. 第一版方案

第一版方案计划引入完全新的 Project / Experiment / Trial / Candidate / Release 聚合，把现有 Run、workflow、recommendation 和 artifact 迁移到新表，并在 Gateway 中引入多 worker 并发研究。

优点：领域语言干净，长期可扩展。

问题：

1. 需要为 84 张表之上的多套 registry 进行高风险数据迁移，无法在一次发布中证明所有 lineage 不丢失；
2. 新聚合会与旧 recommendation/release/active parameter 真源重叠；
3. 并发运行 data maintenance 和 research pipeline 会争用 artifact 目录、checkpoint 和数据库连接预算；
4. 一次替换 API、执行器、数据模型和 UI 不具备可逆发布路径；
5. 它优化了抽象，却不能直接解决 Operator 当下最强的问题：页面状态矛盾、等待原因不透明、下一步动作不明确。

因此第一版方案被否决。

## 5. 优化后的最终方案

### 5.1 架构原则

1. **保留真源，重构控制面**：保留现有 ORM、Run/Task、workflow、recommendation、release 和 active parameter 存储语义。
2. **单一读模型**：增加版本化 `RDP Workspace Snapshot V3`，一次读取返回同一生成时间的健康、阶段、Run、研究项、发布、观察、tuning 和告警。
3. **动作来自后端 capability**：前端只渲染已经计算好的 `enabled/disabled_reason`，不在浏览器里重新发明门禁。
4. **立即接收与立即执行分开**：点击必须立即返回 Run ID；如果唯一执行槽忙，明确返回前方 Run、queue position 和不可安全并发的原因。
5. **最优必须先合格**：只能从 integrity 完整、recommendation 已批准、Gate 通过、参数可映射且具备 rollback target 的候选中选择；无合格候选时结果是 `no_eligible_candidate`，不应用任何参数。
6. **旧 API 保持兼容**：V3 先成为当前 UI 唯一读入口，旧 read API 保留给测试和外部脚本；写 API 仍使用现有授权、token、Gate 和审计实现。

### 5.2 目标组件

```text
Postgres + artifact registries + workflow JSON
              |
              v
  RdpWorkspaceAssembler (application read model)
    - lifecycle projector
    - execution-lane projector
    - workflow capability catalog
    - candidate eligibility projector
              |
              v
GET /rdp/v3/workspace  ----> Dashboard snapshot plane (one panel)
              |
              v
  RDP V3 UI
    - 运营总览
    - 运行与执行队列
    - 研究证据与审批
    - 发布、观察与回滚
```

### 5.3 Workspace V3 合同

```json
{
  "schema_version": "rdp.workspace.v3",
  "generated_at": "ISO-8601",
  "environment": {},
  "health": {},
  "lifecycle": {"stages": [], "current_stage": "research"},
  "execution": {
    "capacity": 1,
    "active_run": null,
    "queued_runs": [],
    "daemon": {},
    "queue_explanation": ""
  },
  "workflows": [],
  "research": {"overview": {}, "items": [], "alerts": {}},
  "release": {
    "candidates": [],
    "eligible_candidate": null,
    "selection_status": "no_eligible_candidate",
    "observations": [],
    "active_parameters": {}
  },
  "tuning": {"overview": {}, "proposals": []},
  "compatibility": {"legacy_read_api_supported": true}
}
```

合同不携带数据库连接串、session cookie、HMAC token 或环境文件内容。

### 5.4 前端信息架构

- 顶部：环境、RDP/daemon/DB 健康、当前阶段和单一“建议下一步”。
- 第一行：六阶段 lifecycle rail，每阶段只显示状态、证据计数和阻断理由。
- 第二行：workflow 快速动作条，明确点击后立即创建 Run。
- 第三行：左侧运行与队列（正在执行 + 队列 + 最近结果），右侧研究审阅列表；证据详情在当前卡片内按需展开。
- 第四行：发布候选、观察/回滚、tuning 三列；没有项时使用紧凑空状态，不留大块空白。

## 6. 交易正确性与安全不变式

1. 前端不能根据本地状态启用后端已禁用的动作。
2. 所有实际 apply 路径继续要求 session-bound `action=apply` 短时 token；rollback 使用独立 token。
3. `release_cycle` 继续 disabled + enqueue blocked。
4. 不允许因为候选为空而退化到 JSON active parameter fallback。
5. Run/Attempt/Step/Event 终态单调，迟到 worker 不能覆盖终态。
6. 最优选择不得在 Gate 前合并不可比指标，不得在没有合格候选时强制应用。
7. 本次发布只允许 `derivatives` 模拟 profile；不启动任何 live profile。

## 7. 性能、可用性与可观测目标

- 页面首次 RDP 数据由 7 个 panel 收敛为 1 个 workspace panel；
- workspace 快照是单次组装的一致视图；任一子数据源失败必须以 blocker/stale 显示，不得静默变成空数组；
- active Run 时 3–5 秒定向刷新 workspace；空闲时使用快照 TTL；
- Run 创建 API 在数据库提交后立即返回，不等待 workflow 执行；
- 执行槽容量、queue position、等待原因和 daemon heartbeat 必须可见；
- 所有写动作继续进入现有审计记录。

## 8. 兼容、迁移与回滚

- 本次不修改 RDP 数据库 schema；
- `/rdp/v3/workspace` 是向后兼容新增；旧 `/rdp/control-summary`、`/rdp/workbench/*`、`/rdp/tuning/*` 和 `/rdp/v2/runs` 保留；
- Dashboard 改用单一 `rdpWorkspace` panel；回滚前端时旧 panel/API 仍可使用；
- 发布失败时回滚 Git commit 并走标准 derivatives 模拟部署，不对 active parameter 表做直接数据库修改。

## 9. 验收标准

1. 前端 RDP 视图仅依赖 `rdpWorkspace` 业务快照；
2. workspace 合同包含 schema version、generated time、lifecycle、execution lane、workflow capabilities、research/release/tuning/alerts；
3. 队列 Run 显示位次和真实等待原因；点击运行立即返回 Run ID；
4. 无合格候选时显式返回 `no_eligible_candidate`，不执行 apply；
5. 现有 auth/token/gate/freeze/单调状态测试继续通过；
6. 通过 Ruff、全量 unit、最窄 integration、JS 语法和目标浏览器人工测试；
7. 提交推送 main 后只发布 derivatives 模拟栈，全容器和多层健康验证通过；
8. 完整 RDP 实跑后，只在存在通过全部门禁的候选时执行模拟参数应用，否则以安全停止作为正确验收结果。

## 10. 未来增长时重新评估的决策

仅当单执行槽成为经实测的主要瓶颈，且 artifact/checkpoint 已有显式 concurrency key 时，再评估多 worker；仅当试验规模和团队协作需求超过当前 Postgres/artifact registry 能力时，再评估 MLflow/Optuna 类组件。
