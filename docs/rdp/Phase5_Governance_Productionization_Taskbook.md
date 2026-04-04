# Phase 5 任务书（Governance / Productionization）

## 1. 目标

在 Phase 1 ~ Phase 4 已经具备功能骨架之后，进入 **Phase 5：Governance / Productionization**。

Phase 5 的目标不是继续扩研究能力，而是把现有平台从：

- 能跑
- 能研究
- 能归因
- 能做 execution proxy realism

推进到：

> **可长期运行、可版本治理、可追溯、可交接、可稳定运营的平台。**

Phase 5 要解决的问题包括：

1. 哪一份参数结论才是当前有效版本
2. 哪一份 round / batch / scan / attribution / execution 结果可以被信任
3. dataset version、parameter version、artifact version 如何关联
4. 失败任务如何被发现、记录、重跑
5. 新人接手时如何知道应该运行什么、看什么、冻结什么
6. 研究结论如何从“结果文件”升级成“受治理的候选结论”

---

## 2. Phase 5 的定位

### 2.1 它不是新功能 phase
Phase 5 不继续扩：
- 新策略
- 新市场
- 新 scanner
- 新 execution model

### 2.2 它解决的是“平台能否长期存活”
Phase 5 关注的是：

- artifact 规范
- 命名规范
- manifest 完整性
- 参数版本治理
- 结果状态管理
- 数据质量监控
- 调度与重跑
- operator runbook
- README / handbook / SOP

---

## 3. Phase 5 的核心目标

Phase 5 结束时，平台应具备以下能力：

1. 任意一个研究结果都能追溯到：
   - dataset version
   - parameter set
   - code phase
   - artifact path
2. 任意一个 round 失败后，都能快速知道：
   - 哪个步骤失败
   - 是否可重跑
   - 重跑命令是什么
3. 任意一个参数推荐，都能知道：
   - 来源于哪个 round
   - 信心等级
   - 是否已冻结
   - 是否已过期
4. 任意一个新接手的人，都能通过文档和 runbook 独立操作平台

---

## 4. Phase 5 的核心交付物

Phase 5 至少产出：

```text
docs/operations/
  platform_runbook.md
  artifact_conventions.md
  parameter_governance.md
  round_lifecycle.md
  operator_checklist.md

artifacts/governance/
  current_parameter_registry.json
  active_round_index.json
  artifact_index.json
  quality_monitor_summary.json
```

以及必要的治理脚本：

```text
scripts/rdp_validate_artifacts.py
scripts/rdp_build_artifact_index.py
scripts/rdp_freeze_parameter_set.py
scripts/rdp_list_active_rounds.py
scripts/rdp_run_quality_monitor.py
scripts/rdp_retry_failed_round.py
```

---

## 5. Phase 5 分成 5 个子阶段

### Phase 5-A：Artifact / 命名 / Manifest 规范化
目标：
- 统一所有 phase 的 artifact 结构与 manifest

### Phase 5-B：参数与结论治理
目标：
- 让 parameter candidates / recommendations 成为可版本化对象

### Phase 5-C：运行状态与失败治理
目标：
- 让 round / batch / scan / attribution / execution 都有标准生命周期

### Phase 5-D：质量监控与巡检
目标：
- 对数据质量、artifact 完整性、关键结果异常做巡检

### Phase 5-E：运行手册与交接文档
目标：
- 让平台脱离“只有你自己会用”的状态

---

## 6. Phase 5-A：Artifact / 命名 / Manifest 规范化

### 6.1 目标
为所有 Phase 的 artifact 建立统一规范。

### 6.2 要覆盖的对象
至少包括：

- Step 1 calibration round
- Step 2 research round
- Phase 3 attribution round
- Phase 4 execution realism round
- 单实验 replay artifact
- parameter scan artifact

### 6.3 统一要求
每个 round 目录都必须至少包含：

- `round_manifest.json`
- `status`
- `started_at`
- `finished_at`
- `scope`
- `input_refs`
- `output_refs`
- `code_version`（如果可获得）
- `notes`（可选）

### 6.4 新增脚本
```text
scripts/rdp_validate_artifacts.py
scripts/rdp_build_artifact_index.py
```

### 6.5 产物
- `artifact_conventions.md`
- `artifact_index.json`

---

## 7. Phase 5-B：参数与结论治理

### 7.1 目标
把目前分散在：
- `parameter_recommendations.json`
- `parameter_candidates.json`
- round conclusion 文档
里的参数结论，收口成受治理对象。

### 7.2 要解决的问题
- 哪一套参数是当前 active candidate
- 哪一套参数只是历史实验结果
- 哪些参数已经 freeze
- 哪些参数仍 pending validation

### 7.3 建议新增对象
```text
artifacts/governance/current_parameter_registry.json
```

### 7.4 建议结构
每个 parameter set 至少包含：

- `parameter_set_id`
- `family`
- `symbol`
- `timeframe`
- `source_round_id`
- `source_phase`
- `dataset_version`
- `values`
- `confidence`
- `status` (`draft` / `candidate` / `frozen` / `deprecated`)
- `frozen_at`
- `notes`

### 7.5 新增脚本
```text
scripts/rdp_freeze_parameter_set.py
scripts/rdp_show_parameter_registry.py
```

### 7.6 产物
- `parameter_governance.md`
- `current_parameter_registry.json`

---

## 8. Phase 5-C：运行状态与失败治理

### 8.1 目标
让所有 runner 都有统一生命周期。

### 8.2 生命周期建议
每个 round / run 至少统一成：

- `pending`
- `running`
- `succeeded`
- `partial_success`
- `failed`
- `deprecated`

### 8.3 要做的事
1. 统一 manifest 中的 `status`
2. 统一退出码语义
3. 统一失败记录格式
4. 提供失败重跑入口

### 8.4 新增脚本
```text
scripts/rdp_list_active_rounds.py
scripts/rdp_retry_failed_round.py
```

### 8.5 产物
- `round_lifecycle.md`
- `active_round_index.json`

---

## 9. Phase 5-D：质量监控与巡检

### 9.1 目标
让平台具备最基础的自检能力。

### 9.2 要监控的内容
至少包括：

#### 数据层
- silver / gold 表是否空表
- funding 是否缺失
- 数据时间覆盖是否断层
- row count 异常变化

#### artifact 层
- round_manifest 是否缺失
- 关键文件是否缺失
- summary / report 是否为空
- parameter file 是否不可解析

#### 结果层
- opening_count 全 0
- full_fill_ratio 全 0
- positive_edge_ratio 全 0
- 所有 round 都 partial / failed

### 9.3 新增脚本
```text
scripts/rdp_run_quality_monitor.py
```

### 9.4 输出
```text
artifacts/governance/quality_monitor_summary.json
```

### 9.5 文档
- `operator_checklist.md`

---

## 10. Phase 5-E：运行手册与交接文档

### 10.1 目标
让别人能接手，不依赖你的口头知识。

### 10.2 必写文档
```text
docs/operations/platform_runbook.md
docs/operations/operator_checklist.md
docs/operations/artifact_conventions.md
docs/operations/parameter_governance.md
docs/operations/round_lifecycle.md
```

### 10.3 文档必须回答的问题
- 平台有哪些 phase
- 每个 phase 用哪个脚本启动
- 主要 artifact 在哪里
- 结果怎么判断是否成功
- 参数冻结怎么做
- 失败怎么重跑
- 数据异常先看哪里
- 哪些文件是“当前有效结论”

---

## 11. 建议新增的模块目录

```text
aats/data_platform/governance/
  artifact_index.py
  parameter_registry.py
  round_status.py
  retry_logic.py
  quality_monitor.py
  manifest_validation.py
```

---

## 12. 需要复用的现有能力

Phase 5 不应重写前面 phase 的主逻辑。

必须复用：

- Step 1 / Step 2 runners
- Phase 3 / 4 round runners
- 现有 manifest / summary / report
- parameter candidates / recommendations
- diagnostics / attribution / execution summaries

---

## 13. 建议新增的统一 manifest 字段规范

所有 round manifest 建议统一至少包含：

```json
{
  "round_id": "...",
  "phase": "phase2_step1 | step2 | phase3 | phase4",
  "status": "succeeded",
  "started_at": "...",
  "finished_at": "...",
  "scope": {
    "family": "...",
    "symbol": "...",
    "timeframe": "..."
  },
  "input_refs": {
    "dataset_version": "v1.0",
    "parameter_set_id": "..."
  },
  "output_refs": {
    "summary_path": "...",
    "report_path": "..."
  },
  "code_version": null,
  "notes": null
}
```

---

## 14. Phase 5 的最小实现范围

第一版只要求：

1. 建立 artifact index
2. 建立 parameter registry
3. 建立统一 round status 规范
4. 支持失败 round 重跑
5. 支持基础质量巡检
6. 写完 operator 文档

第一版不要求：

- Web UI
- 权限系统
- 数据库化 governance backend
- 实时告警系统
- 企业级审计

---

## 15. 需要新增的 CLI 行为

建议所有治理脚本都支持统一风格：

- `--artifact-root`
- `--output`
- `--phase`
- `--round-id`
- `--status`
- `--dry-run`

---

## 16. Phase 5 的输出文件定义

### 16.1 `artifact_index.json`
索引所有已知 round / run / report / summary。

### 16.2 `active_round_index.json`
只记录当前 active / latest 的 round。

### 16.3 `current_parameter_registry.json`
记录当前有效参数候选与冻结参数。

### 16.4 `quality_monitor_summary.json`
输出最近一次巡检结果。

---

## 17. Phase 5 结论文档（建议）

建议新增：

```text
docs/operations/phase5_governance_conclusion.md
```

内容包括：

- 已治理的对象
- 当前 active parameter sets
- 当前 active rounds
- 当前已知平台风险
- 当前仍待治理的问题
- 平台是否进入可交接状态

---

## 18. 实现约束

### 18.1 不允许继续扩策略功能
Phase 5 专注治理与运营化。

### 18.2 不允许为了治理重写 Phase 2~4 主逻辑
优先做 wrapper / index / validation / registry。

### 18.3 不允许只写文档不写工具
Phase 5 必须有：
- index
- registry
- validation
- retry
- monitor

### 18.4 不允许只写工具不写 runbook
Phase 5 的价值很大一部分在可交接性。

---

## 19. 验收标准

Phase 5 第一版通过条件：

1. 能生成 `artifact_index.json`
2. 能生成 `current_parameter_registry.json`
3. 能对至少一个 parameter set 执行 freeze
4. 能列出 active rounds
5. 能对至少一个 failed / partial round 生成 retry plan
6. 能生成 `quality_monitor_summary.json`
7. docs/operations 下的 5 份文档齐全
8. 一个不了解项目的人能根据 runbook 找到：
   - 当前有效参数
   - 最近有效 round
   - 失败 round 如何处理

---

## 20. 一句话总结

Phase 5 的职责是：

> **把已经具备功能闭环的研究平台，推进到可追溯、可冻结、可重跑、可巡检、可交接的治理与运营化阶段。**
