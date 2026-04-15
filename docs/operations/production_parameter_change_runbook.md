# Production Parameter Change Runbook

> 本文档定义参数变更进入生产的完整受控流程。
> 涵盖从 recommendation 到 gate、release、observation、rollback 的全链路。

---

## 1. 流程总览

```
recommendation (approved)
    ↓
Pre-Apply Gate
    ↓ pass / warn → 继续
    ↓ block → 停止，修复后重试
Parameter Release
    ↓
Apply Active Parameter Set
    ↓
Observation Window (默认 24h)
    ↓
Post-Apply Assessment
    ↓
    ├── keep → 结束观察
    ├── review → 人工审查
    └── rollback_recommended → 执行回滚
```

---

## 2. Pre-Apply Gate

### 2.1 何时运行

- recommendation 被 approved 后、apply 前
- 每次 apply 必须通过 gate
- 生产环境不允许跳过 gate

### 2.2 检查项

| 检查 | 类别 | 级别 | 说明 |
|------|------|------|------|
| recommendation_status | approval | block | 必须为 approved |
| parameter_set_exists | approval | block | target PS 必须在 registry 中 |
| quality_monitor_health | governance | block/warn | unhealthy=block, degraded=warn |
| evidence_freshness | freshness | block/warn | >7d=block, >3d=warn |
| evidence_completeness | freshness | block/warn | <0.25=block, <0.5=warn |
| decision_consistency | decision | block/warn | pause=block, require_review=warn |
| latest_round_health | round | block/warn | failed=block, partial=warn |
| current_alerts | operations | block/warn | critical=block, warning=warn |
| live_db_health | production | block/warn | staging/prod 下 live DB 不健康直接 block |
| workflow_freshness | operations | block/warn | 关键 workflow 缺失/过旧 |

### 2.3 Gate 输出

| 状态 | 含义 | 操作 |
|------|------|------|
| **pass** | 所有检查通过 | 继续 apply |
| **warn** | 有警告但无阻断 | 可继续，需关注 |
| **block** | 有阻断条件 | 不允许 apply |

### 2.4 运行方式

```bash
# 脚本
python scripts/rdp_run_pre_apply_gate.py --recommendation-id rec_xxx

# API
POST /rdp/gates/run {"recommendation_id": "rec_xxx"}
```

### 2.5 Gate 结果存储

```
artifacts/production_workflow/gates/<gate_run_id>/
  pre_apply_gate_result.json
  pre_apply_gate_report.md
```

---

## 3. Parameter Release

### 3.1 何时创建

- gate pass 或 warn 后
- 每次 apply 都应通过 release 流程

### 3.2 Release 记录字段

| 字段 | 说明 |
|------|------|
| release_id | 唯一标识 |
| created_at | 创建时间 |
| family / timeframe | 目标 combo |
| recommendation_id | 关联 recommendation |
| parameter_set_id | 要应用的参数集 |
| previous_parameter_set_id | 上一个参数集 |
| actor | 操作人 |
| gate_result_ref | 关联 gate 结果 |
| gate_status | gate 状态 |
| apply_result | pending / success / failed / blocked_by_gate |
| observation_status | pending / observing / completed / rollback_recommended |
| observation_window_hours | 观察窗口时长 |

### 3.3 运行方式

```bash
# 完整流程: gate + release + apply
python scripts/rdp_create_parameter_release.py \
    --recommendation-id rec_xxx --actor operator_name

# 生产不提供跳过 gate 的标准流程
# 如需真正执行 prod apply，必须额外显式设置:
#   export RDP_PRODUCTION_APPLY_ENABLED=true

# API
POST /rdp/releases/create {
    "recommendation_id": "rec_xxx",
    "actor": "operator_name",
    "observation_window_hours": 72
}
```

### 3.4 Release 历史

```
artifacts/production_workflow/parameter_release_history.json
```

查看: `GET /rdp/releases/latest` 或 `GET /rdp/releases/history`

---

## 4. Observation Window

### 4.0 生产硬约束

active parameter apply 会改变 live 策略行为，必须按生产变更处理：

- 必须有 approved recommendation。
- 必须有 pre-apply gate run id。
- 必须有 release id、actor、notes。
- 必须写入 DB + 文件 apply history。
- 必须具备 rollback 目标。
- 禁止在生产环境跳过 gate。
- `prod` 观察窗口不得短于 72h，`staging` 不得短于 24h。

### 4.1 何时运行

- apply 成功后自动进入 observing 状态
- 在观察窗口内定期运行观察检查

### 4.2 建议观察时间表

| 时间点 | 操作 |
|--------|------|
| Apply 后 1h | 首次观察检查 |
| Apply 后 4h | 第二次观察检查 |
| Apply 后 24h | 正式评估（默认窗口结束） |
| Apply 后 48h | 延长观察（如需要） |

### 4.3 观察指标

| 类别 | 指标 | 触发条件 |
|------|------|----------|
| 治理层 | quality_monitor health | unhealthy → regression |
| 决策层 | family/tf decision status | pause → regression |
| 归因层 | attribution failure modes | 数据异常 → warn |
| 执行层 | execution realism metrics | 数据异常 → warn |

### 4.4 观察状态

| 状态 | 含义 |
|------|------|
| observing | 窗口内，正在观察 |
| completed | 窗口结束，无异常 |
| rollback_recommended | 发现异常，建议回滚 |

### 4.5 运行方式

```bash
# 脚本
python scripts/rdp_run_post_apply_observation.py --release-id rel_xxx

# API
POST /rdp/observations/run {"release_id": "rel_xxx"}
```

### 4.6 观察结果存储

```
artifacts/production_workflow/observations/<release_id>/
  observation_summary.json
  observation_report.md
```

---

## 5. Rollback Recommendation

### 5.1 触发条件

| 条件 | 严重度 | 说明 |
|------|--------|------|
| Attribution 总失败率 > 80% | high | strategy + risk + execution 失败率过高 |
| Execution fill_ratio < 0.5 | high/medium | 成交率过低 |
| Execution cost > 10bps | high/medium | 执行成本过高 |
| Execution edge_ratio < 0.3 | high/medium | 正向 edge 过低 |
| QM unhealthy / critical > 0 | high | 治理层严重退化 |
| QM degraded | medium | 治理层退化 |

### 5.2 评估结果

| 字段 | 说明 |
|------|------|
| rollback_recommended | 是否建议回滚 |
| severity | none / medium / high |
| reasons | 具体原因列表 |
| suggested_target_parameter_set_id | 建议回滚到哪个版本 |

### 5.3 运行方式

```bash
# 脚本
python scripts/rdp_evaluate_rollback_recommendation.py --release-id rel_xxx

# API
POST /rdp/rollback-recommendation/evaluate {"release_id": "rel_xxx"}
```

### 5.4 结果存储

```
artifacts/production_workflow/rollback_recommendations/<release_id>/
  rollback_recommendation.json
  rollback_recommendation_report.md
```

---

## 6. 完整操作流程

### Step 1: 审批 Recommendation

```bash
python scripts/rdp_approve_recommendation.py \
    --recommendation-id rec_xxx --action approve --actor operator
```

### Step 2: 创建 Release（含 Gate + Apply）

```bash
python scripts/rdp_create_parameter_release.py \
    --recommendation-id rec_xxx --actor operator
```

### Step 3: 重启系统

重启 API gateway 使新参数生效。

### Step 4: 观察检查（1h / 4h / 24h）

```bash
python scripts/rdp_run_post_apply_observation.py --release-id rel_xxx
```

### Step 5: Rollback 评估

```bash
python scripts/rdp_evaluate_rollback_recommendation.py --release-id rel_xxx
```

### Step 6: 如需回滚

```bash
python scripts/rdp_rollback_active_parameter_set.py \
    --family independent --timeframe 15m --actor operator
```

---

## 7. 紧急操作

### 7.1 禁止紧急跳过 Gate

生产环境没有“紧急跳过 Gate”的标准路径。紧急场景只能做两类操作：

1. 不 apply 新参数，先保持当前 active parameter。
2. 对已经生效且表现异常的参数执行 rollback。

如果本地开发/测试需要演练 gate 异常，应在隔离环境执行，不得使用 `spot_live` / `derivatives_live`。

### 7.2 紧急回滚

```bash
python scripts/rdp_rollback_active_parameter_set.py \
    --family independent --timeframe 15m --actor operator \
    --notes "emergency rollback"
```

---

## 8. API 端点汇总

### 只读（require_read_access）

| 端点 | 说明 |
|------|------|
| `GET /rdp/releases/latest` | 最近 releases |
| `GET /rdp/releases/history` | 完整 release 历史 |
| `POST /rdp/gates/run` | 运行 gate 检查 |
| `POST /rdp/observations/run` | 运行观察检查 |
| `POST /rdp/rollback-recommendation/evaluate` | 评估回滚建议 |

### 写入（require_write_access）

| 端点 | 说明 |
|------|------|
| `POST /rdp/releases/create` | 创建 release（含 gate + apply） |

---

## 9. 存储结构

### 9.1 文件存储

```
artifacts/production_workflow/
  parameter_release_history.json          # release 历史
  gates/<gate_run_id>/                    # gate 结果
    pre_apply_gate_result.json
    pre_apply_gate_report.md
  observations/<release_id>/              # 观察结果
    observation_summary.json
    observation_report.md
  rollback_recommendations/<release_id>/  # 回滚建议
    rollback_recommendation.json
    rollback_recommendation_report.md
```

### 9.2 DB 存储（governance schema）

> 设置 `AATS_ACTIVE_PARAMETER_DB_URL` 后，apply/rollback 操作同时写入 DB。

| DB 表 | 对应文件 | 说明 |
|------|---------|------|
| `governance.active_parameter_sets` | `configs/active_parameter_sets/*.json` | 当前生效参数 |
| `governance.parameter_apply_history` | `parameter_apply_history.json` | 操作审计日志 |
| `governance.recommendations` | `recommendation_registry.json` | 建议审批状态 |
| `governance.active_decisions` | `active_decision_registry.json` | combo 决策状态 |
| `governance.parameter_sets` | `current_parameter_registry.json` | 参数集候选池 |

DB 与文件始终双写同步，DB 不可用时静默回退到纯文件模式。
