# RDP Workflow 调度策略

## 概述

RDP 使用 JSON 配置驱动的 Workflow 调度系统，通过统一入口脚本 `rdp_run_scheduled_workflow.py` 运行。
每个 Workflow 包含一组有序任务，按顺序执行，支持超时控制、失败停止策略和 dry-run 模式。

## 调度架构

```
configs/rdp_workflows/
  ├── data_maintenance.json    # 数据层维护
  ├── research_cycle.json      # 研究层周期
  ├── governance_cycle.json    # 治理层周期
  └── decision_cycle.json      # 决策层周期

scripts/rdp_run_scheduled_workflow.py   # 统一调度入口
aats/data_platform/operations/
  ├── workflow_dispatcher.py             # 核心调度器
  ├── failure_registry.py                # 失败记录
  └── retry_manager.py                   # 补跑管理
```

## Workflow 目录

| Workflow | 调度建议 | 说明 |
|----------|---------|------|
| `data_maintenance` | 每日 06:00 UTC | 数据缺口检测、Gold 层构建、索引重建 |
| `governance_cycle` | 每日 07:00 UTC | 质量监控、产物验证、轮次索引刷新 |
| `research_cycle` | 每周日 08:00 UTC | 研究轮次、归因分析、执行真实性评估 |
| `decision_cycle` | 每周（研究后）或按需 | 决策轮次、可靠性检查、观察检查 |

## 调度时序

```
Day N 06:00 UTC ─ data_maintenance
         │
Day N 07:00 UTC ─ governance_cycle
         │
Sunday  08:00 UTC ─ research_cycle
         │
Sunday  ~10:00 UTC ─ decision_cycle (research 完成后)
```

### 依赖关系

- `governance_cycle` 依赖 `data_maintenance` 的产物（Gold 层数据、artifact 索引）
- `research_cycle` 依赖 `governance_cycle` 的质量监控结果
- `decision_cycle` 依赖 `research_cycle` 的研究结果

### 推荐执行方式

**方式 1: cron / Task Scheduler（推荐生产环境）**

```bash
# Linux crontab 示例
0 6 * * * cd /path/to/project && python scripts/rdp_run_scheduled_workflow.py --workflow data_maintenance >> /var/log/rdp/data_maintenance.log 2>&1
0 7 * * * cd /path/to/project && python scripts/rdp_run_scheduled_workflow.py --workflow governance_cycle >> /var/log/rdp/governance_cycle.log 2>&1
0 8 * * 0 cd /path/to/project && python scripts/rdp_run_scheduled_workflow.py --workflow research_cycle >> /var/log/rdp/research_cycle.log 2>&1
0 10 * * 0 cd /path/to/project && python scripts/rdp_run_scheduled_workflow.py --workflow decision_cycle >> /var/log/rdp/decision_cycle.log 2>&1
```

```powershell
# Windows Task Scheduler (schtasks) 示例
schtasks /create /tn "RDP_DataMaintenance" /tr "python scripts/rdp_run_scheduled_workflow.py --workflow data_maintenance" /sc daily /st 06:00
schtasks /create /tn "RDP_GovernanceCycle" /tr "python scripts/rdp_run_scheduled_workflow.py --workflow governance_cycle" /sc daily /st 07:00
schtasks /create /tn "RDP_ResearchCycle" /tr "python scripts/rdp_run_scheduled_workflow.py --workflow research_cycle" /sc weekly /d SUN /st 08:00
schtasks /create /tn "RDP_DecisionCycle" /tr "python scripts/rdp_run_scheduled_workflow.py --workflow decision_cycle" /sc weekly /d SUN /st 10:00
```

**方式 2: 手动触发**

```bash
# 列出可用 workflows
python scripts/rdp_run_scheduled_workflow.py --list

# 预览（不执行）
python scripts/rdp_run_scheduled_workflow.py --workflow governance_cycle --dry-run

# 执行
python scripts/rdp_run_scheduled_workflow.py --workflow governance_cycle

# 失败后继续
python scripts/rdp_run_scheduled_workflow.py --workflow research_cycle --no-stop-on-failure
```

## Workflow 配置格式

每个 workflow JSON 文件结构：

```json
{
  "workflow": "workflow_name",
  "description": "描述",
  "schedule_hint": "调度建议",
  "tasks": [
    {
      "name": "task_name",
      "description": "任务描述",
      "command": "python -m module.path --flag",
      "timeout_seconds": 120,
      "enabled": true,
      "allow_failure": false
    }
  ]
}
```

### 字段说明

| 字段 | 类型 | 说明 |
|------|------|------|
| `name` | string | 任务唯一标识 |
| `command` | string | 子进程执行命令 |
| `timeout_seconds` | int | 超时限制（秒） |
| `enabled` | bool | 是否启用（false 则跳过） |
| `allow_failure` | bool | 失败是否继续（true = 非关键任务） |

## 退出码规范

| 退出码 | 含义 |
|--------|------|
| 0 | 全部成功 |
| 1 | 有任务失败 |
| 2 | 配置或参数错误 |

## 运行报告

每次执行生成报告保存至 `artifacts/operations/workflow_runs/<run_id>.json`：

```json
{
  "run_id": "wf_20260404_060000_abc123",
  "workflow": "data_maintenance",
  "started_at": "2026-04-04T06:00:00Z",
  "completed_at": "2026-04-04T06:05:30Z",
  "overall_status": "success",
  "succeeded": 3,
  "failed": 0,
  "skipped": 0,
  "tasks": [...]
}
```

## 失败处理策略

1. **stop_on_failure（默认）**: 关键任务失败后停止后续任务
2. **no-stop-on-failure**: 失败后继续执行，最终报告汇总所有结果
3. **allow_failure**: 单个任务级别，标记为 true 的任务失败不影响后续

### 失败恢复流程

1. 检查失败报告：`artifacts/operations/workflow_runs/`
2. 记录失败：`python scripts/rdp_record_workflow_failure.py`
3. 修复问题后补跑：`python scripts/rdp_retry_workflow_failure.py`
4. 查看失败历史：`artifacts/operations/workflow_failures.json`

## 监控与告警

- 可靠性检查：`python scripts/rdp_run_reliability_check.py`
- 告警摘要：`python scripts/rdp_build_alert_summary.py`
- 当前告警：`artifacts/operations/alerts/current_alerts.json`

详见 [可靠性告警文档](reliability_alerting.md)
