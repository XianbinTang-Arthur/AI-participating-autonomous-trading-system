# 平台运行手册 (Platform Runbook)

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。


## 1. 平台概览

AATS Research Data Platform (RDP) 是一个多阶段研究平台，用于评估自主交易策略。

### 1.1 Phase 架构

| Phase | 名称 | 目标 | 入口脚本 |
|-------|------|------|----------|
| Phase 2 Step 1 | 校准 | 单组合单参数校准 replay | `scripts/rdp_run_step1_calibration.py` |
| Phase 2 Step 2 | 研究 | 跨参数扫描+推荐 | `scripts/rdp_run_step2_research.py` |
| Phase 3 | 归因 | Replay vs Live 对照归因 | `scripts/rdp_run_phase3_round.py` |
| Phase 4 | 执行评估 | 执行代理 realism | `scripts/rdp_run_phase4_round.py` |
| Phase 5 | 治理 | Governance / 产品化 | 见下方治理脚本 |

### 1.2 固定范围

- **Symbol**: BTC-USDT-SWAP
- **Families**: independent, directional
- **Timeframes**: 15m, 1h
- **Combos**: 4 (independent_15m, independent_1h, directional_15m, directional_1h)

### 1.3 关键目录与 DB 表

```
artifacts/
  research/
    experiments/          # Step 1/2 单实验和参数扫描
    calibration_batches/  # Step 1 批量校准
    calibration_rounds/   # Step 2 研究 round
    attribution_rounds/   # Phase 3 归因 round
    execution_rounds/     # Phase 4 执行评估 round
  governance/
    artifact_index.json              # 全局 artifact 索引
    active_round_index.json          # 当前 active round 索引
    current_parameter_registry.json  # 参数注册表（文件备份）
    quality_monitor_summary.json     # 最近巡检结果
```

**governance schema DB 表**（DB-first + 文件 fallback 双写，`AATS_ACTIVE_PARAMETER_DB_URL` 控制）:

| DB 表 | 对应 JSON 文件 | 说明 |
|------|------|------|
| `governance.parameter_sets` | `current_parameter_registry.json` | 参数集候选池 |
| `governance.recommendations` | `recommendation_registry.json` | 建议审批记录 |
| `governance.active_decisions` | `active_decision_registry.json` | combo 决策状态 |
| `governance.active_parameter_sets` | `active_parameter_registry.json` | 当前生效参数 |
| `governance.parameter_apply_history` | `parameter_apply_history.json` | 应用审计日志 |

全量种子: `python scripts/apply_active_parameter_set.py --action seed-db`

---

## 1.4 与主交易系统的边界

RDP 是离线研究与参数治理平台，不是实时交易链路的一部分。主交易系统不会读取 RDP Bronze/Silver/Gold 行情表；RDP 对主交易系统的影响只发生在 active parameter set 被 apply 并由 runtime 加载之后。

因此，RDP 运维需要遵守以下边界：

- RDP workflow 失败不会自动停止主交易；需要通过 Operator/reliability 告警和人工流程处理。
- active parameter apply 是生产行为变更，必须经过 recommendation approval、pre-apply gate、release 记录、apply history 和 rollback plan。
- 生产环境不得跳过 pre-apply gate。
- 如果 active parameter DB 写入失败而 fallback 到文件，必须在恢复 DB 后运行 `seed-db` 并验证 DB/JSON 一致性。

## 2. 日常操作

### 2.1 运行 Step 1 单实验

```bash
python scripts/rdp_run_step1_calibration.py \
    --family independent \
    --timeframe 15m \
    --symbol BTC-USDT-SWAP \
    --start 2026-03-31 --end 2026-04-02
```

### 2.2 运行 Step 2 参数扫描

```bash
python scripts/rdp_run_step2_research.py \
    --family independent \
    --timeframe 15m \
    --symbol BTC-USDT-SWAP \
    --start 2026-03-31 --end 2026-04-02
```

### 2.3 运行 Phase 3 归因 Round

```bash
python scripts/rdp_run_phase3_round.py \
    --start 2026-03-31 --end 2026-04-02

# 使用 Phase 2 推荐参数
python scripts/rdp_run_phase3_round.py \
    --start 2026-03-31 --end 2026-04-02 \
    --params-json artifacts/research/experiments/.../parameter_candidates.json
```

### 2.4 运行 Phase 4 执行评估 Round

```bash
python scripts/rdp_run_phase4_round.py \
    --start 2026-03-31 --end 2026-04-02

# 使用自定义 taker fee
python scripts/rdp_run_phase4_round.py \
    --start 2026-03-31 --end 2026-04-02 \
    --taker-fee-bps 3.0
```

### 2.5 退出码含义

| 退出码 | 含义 |
|--------|------|
| 0 | 全部成功 |
| 2 | 部分成功（有 combo 失败） |
| 3 | 全部失败 |

---

## 3. 治理操作

### 3.1 构建 Artifact 索引

```bash
python scripts/rdp_build_artifact_index.py
# 输出: artifacts/governance/artifact_index.json
```

### 3.2 校验 Manifest 规范

```bash
# 全部校验
python scripts/rdp_validate_artifacts.py

# 只校验 Phase 3
python scripts/rdp_validate_artifacts.py --phase phase3

# 自动补全缺失字段
python scripts/rdp_validate_artifacts.py --fix
```

### 3.3 查看参数注册表

```bash
python scripts/rdp_freeze_parameter_set.py --action show --verbose

# 按条件筛选
python scripts/rdp_freeze_parameter_set.py --action show --family independent --status frozen
```

### 3.4 导入参数到注册表

```bash
# 从 parameter_candidates.json 导入
python scripts/rdp_freeze_parameter_set.py --action import \
    --source artifacts/research/experiments/.../parameter_candidates.json

# 从 parameter_recommendations.json 导入
python scripts/rdp_freeze_parameter_set.py --action import \
    --source artifacts/research/experiments/.../parameter_recommendations.json \
    --family independent --timeframe 15m
```

### 3.5 冻结参数

```bash
python scripts/rdp_freeze_parameter_set.py --action freeze \
    --parameter-set-id ps_20260403_123456_abc123
```

### 3.6 列出 Active Rounds

```bash
python scripts/rdp_list_active_rounds.py

# 包含 experiments
python scripts/rdp_list_active_rounds.py --include-experiments

# 只看失败的
python scripts/rdp_list_active_rounds.py --status failed
```

### 3.7 失败 Round 重跑

```bash
# 查看可重跑列表
python scripts/rdp_retry_failed_round.py --action list

# 生成重跑计划
python scripts/rdp_retry_failed_round.py --action plan \
    --round-dir artifacts/research/attribution_rounds/<round_id> \
    --phase phase3

# 执行重跑
python scripts/rdp_retry_failed_round.py --action rerun \
    --round-dir artifacts/research/attribution_rounds/<round_id> \
    --phase phase3
```

### 3.8 运行质量巡检

```bash
python scripts/rdp_run_quality_monitor.py
# 输出: artifacts/governance/quality_monitor_summary.json

# 退出码: 0=healthy, 1=unhealthy(critical), 2=degraded(warning)
```

---

## 4. 判断结果是否成功

### 4.1 单实验（Step 1）

查看 `diagnostics.json`:
- `opening_count > 0`：有开仓信号
- `positive_edge_ratio > 0`：有正 edge
- `execution_compatible_ratio > 0.05`：有足够的执行兼容 bar

### 4.2 Round（Phase 3/4）

查看 `round_manifest.json`:
- `combos[].status` 逐个检查
- 全部 `succeeded` = 成功
- 有 `partial_success` = 需要调查
- 有 `failed` = 需要重跑或排查

### 4.3 质量巡检

运行 `rdp_run_quality_monitor.py`，检查:
- `health: "healthy"` = 平台正常
- `health: "degraded"` = 有 warning
- `health: "unhealthy"` = 有 critical 问题

---

## 5. 数据异常排查

### 5.1 先看哪里

1. 运行 `rdp_run_quality_monitor.py`，看有哪些 check 失败
2. 检查 `quality_monitor_summary.json` 中 `passed: false` 的项
3. 按 category 分类排查:
   - `artifact`: 文件缺失/目录问题
   - `result`: 结果异常（全 0、全失败）
   - `parameter`: 参数文件不可解析
   - `governance`: 治理文件缺失

### 5.2 常见问题

| 现象 | 可能原因 | 处理 |
|------|---------|------|
| opening_count 全 0 | 参数过严 | 放宽 min_safe_net_edge_bps |
| 全部 combo failed | 数据库连接问题 | 检查 DB URL 和网络 |
| no_bar_data | Gold 表为空 | 运行 backfill |
| manifest 缺失 | 脚本异常退出 | 查 stderr，重跑 |

---

## 6. 当前有效结论

要找到当前有效结论，按以下顺序检查:

1. **参数注册表**: `artifacts/governance/current_parameter_registry.json`
   - 找 `status: "frozen"` 的 parameter set
2. **Active rounds**: `artifacts/governance/active_round_index.json`
   - 找 `latest_by_phase` 中各 phase 的最近成功 round
3. **质量报告**: `artifacts/governance/quality_monitor_summary.json`
   - 确认 health 状态

---

## 7. 参考文档

- [Artifact 规范](artifact_conventions.md)
- [参数治理](parameter_governance.md)
- [Round 生命周期](round_lifecycle.md)
- [运维检查清单](operator_checklist.md)
