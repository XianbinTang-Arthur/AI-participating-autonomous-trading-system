# RDP Workflow 调度日历

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。


## 周调度视图

```
┌──────────┬────────┬──────────────────────────────────┐
│   Day    │  Time  │         Workflow                 │
├──────────┼────────┼──────────────────────────────────┤
│ Mon-Sat  │ 06:00  │ data_maintenance                 │
│ Mon-Sat  │ 07:00  │ governance_cycle                 │
│          │        │                                  │
│ Sunday   │ 06:00  │ data_maintenance                 │
│ Sunday   │ 07:00  │ governance_cycle                 │
│ Sunday   │ 08:00  │ research_cycle                   │
│ Sunday   │ ~10:00 │ decision_cycle (research 完成后) │
│          │        │                                  │
│ 按需     │ 随时   │ decision_cycle (ad-hoc)          │
└──────────┴────────┴──────────────────────────────────┘
```

所有时间为 UTC。

## 日视图

### 工作日 (Mon-Sat)

```
06:00 ┤ ▓▓▓ data_maintenance (~5min)
      │   ├─ gap_detection
      │   ├─ gold_build
      │   └─ artifact_index_rebuild
06:10 ┤
      │
07:00 ┤ ▓▓▓ governance_cycle (~3min)
      │   ├─ quality_monitor
      │   ├─ artifact_validation
      │   └─ active_rounds_refresh
07:05 ┤
      │
08:00 ┤ (可选) 手动可靠性检查
      │
```

### 周日 (Sunday)

```
06:00 ┤ ▓▓▓ data_maintenance (~5min)
06:10 ┤
      │
07:00 ┤ ▓▓▓ governance_cycle (~3min)
07:05 ┤
      │
08:00 ┤ ▓▓▓▓▓▓▓ research_cycle (~15-20min)
      │   ├─ research_round (最耗时)
      │   ├─ attribution_round
      │   └─ execution_realism_round
08:20 ┤
      │
~10:00┤ ▓▓▓ decision_cycle (~5min)
      │   ├─ decision_round
      │   ├─ reliability_check
      │   └─ observation_check
10:05 ┤
```

## Workflow 详细信息

### data_maintenance (每日 06:00 UTC)

| 任务 | 超时 | 关键性 | 预计耗时 |
|------|------|--------|---------|
| gap_detection | 120s | 非关键 (allow_failure) | ~30s |
| gold_build | 300s | 关键 | ~2min |
| artifact_index_rebuild | 60s | 非关键 (allow_failure) | ~15s |

**依赖**: 无上游依赖
**下游**: governance_cycle 依赖 Gold 层数据

### governance_cycle (每日 07:00 UTC)

| 任务 | 超时 | 关键性 | 预计耗时 |
|------|------|--------|---------|
| quality_monitor | 120s | 关键 | ~30s |
| artifact_validation | 60s | 非关键 (allow_failure) | ~15s |
| active_rounds_refresh | 60s | 非关键 (allow_failure) | ~10s |

**依赖**: data_maintenance 完成
**下游**: research_cycle 使用质量监控结果

### research_cycle (每周日 08:00 UTC)

| 任务 | 超时 | 关键性 | 预计耗时 |
|------|------|--------|---------|
| research_round | 600s | 关键 | ~10min |
| attribution_round | 300s | 非关键 (allow_failure) | ~3min |
| execution_realism_round | 300s | 非关键 (allow_failure) | ~3min |

**依赖**: governance_cycle 完成（质量监控数据）
**下游**: decision_cycle 使用研究结果

### decision_cycle (每周日 ~10:00 UTC 或按需)

| 任务 | 超时 | 关键性 | 预计耗时 |
|------|------|--------|---------|
| decision_round | 300s | 关键 | ~2min |
| reliability_check | 120s | 非关键 (allow_failure) | ~30s |
| observation_check | 60s | 非关键 (allow_failure) | ~15s |

**依赖**: research_cycle 完成（研究轮次结果）
**下游**: 无（产出 recommendation 供 operator 审核）

## 依赖链

```
data_maintenance ──→ governance_cycle ──→ research_cycle ──→ decision_cycle
   (daily 06:00)      (daily 07:00)      (weekly Sun 08:00)  (weekly ~10:00)
```

## 假日调度

- **公共假日**: data_maintenance 和 governance_cycle 照常执行（自动调度）
- **研究周期**: research_cycle 周日照常执行
- **决策周期**: 可跳过，待下周执行

## 调度冲突处理

1. **前一个 workflow 超时**: 后续 workflow 延后执行，不并行
2. **前一个 workflow 失败**: 后续 workflow 仍按计划执行（独立调度）
3. **手动触发与自动调度冲突**: 以先到的为准，避免同一 workflow 并行

## 维护窗口

建议在以下时段进行系统维护:
- **数据库维护**: 05:00-06:00 UTC (所有 workflow 之前)
- **代码部署**: 11:00-13:00 UTC (所有 workflow 之后)
- **配置变更**: 任一 workflow 间隔期
