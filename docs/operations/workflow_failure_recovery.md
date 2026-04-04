# Workflow 失败恢复指南

## 概述

当 RDP workflow 任务失败时，系统提供标准化的失败记录和补跑流程。
所有失败记录保存在 `artifacts/operations/workflow_failures.json`。

## 失败记录流程

### 自动记录

Workflow dispatcher 执行完毕后，可通过 `auto_record_failures_from_report()` 自动提取失败任务：

```python
from aats.data_platform.operations.retry_manager import auto_record_failures_from_report

failures = auto_record_failures_from_report(root, report)
```

### 手动记录

```bash
python scripts/rdp_record_workflow_failure.py \
    --workflow governance_cycle \
    --run-id wf_20260404_070000_abc \
    --task quality_monitor \
    --error "Connection timeout" \
    --exit-code 1
```

### 查看 open 失败

```bash
python scripts/rdp_record_workflow_failure.py --list-open
```

## 失败记录格式

```json
{
  "failure_id": "fail_governance_cycle_quality_monitor_20260404_070000",
  "workflow": "governance_cycle",
  "run_id": "wf_20260404_070000_abc",
  "task_name": "quality_monitor",
  "error_message": "Connection timeout to PostgreSQL",
  "exit_code": 1,
  "recorded_at": "2026-04-04T07:05:00Z",
  "status": "open",
  "retry_count": 0,
  "last_retry_at": null,
  "last_retry_result": null,
  "resolution_notes": ""
}
```

### 状态流转

```
open ──→ retried (补跑成功)
  │
  ├──→ resolved (手动解决)
  │
  └──→ ignored (标记忽略)
```

## 补跑流程

### 补跑单个任务

```bash
python scripts/rdp_retry_workflow_failure.py \
    --failure-id fail_governance_cycle_quality_monitor_20260404_070000 \
    --mode task
```

### 补跑整个 workflow

```bash
python scripts/rdp_retry_workflow_failure.py \
    --failure-id fail_governance_cycle_quality_monitor_20260404_070000 \
    --mode workflow
```

### 预览补跑

```bash
python scripts/rdp_retry_workflow_failure.py \
    --failure-id fail_governance_cycle_quality_monitor_20260404_070000 \
    --dry-run
```

### 自定义超时

```bash
python scripts/rdp_retry_workflow_failure.py \
    --failure-id fail_research_cycle_research_round_20260404_080000 \
    --mode task \
    --timeout 900
```

## 常见失败场景

### 1. 数据库连接超时

**症状**: `Connection timeout to PostgreSQL`
**处理**:
1. 检查数据库状态
2. 确认连接参数（环境变量）
3. 补跑失败任务

### 2. 子进程超时

**症状**: `timeout after Ns`
**处理**:
1. 检查任务是否正常但耗时过长
2. 如果数据量增长导致超时，调整 workflow config 中的 `timeout_seconds`
3. 补跑任务，可使用 `--timeout` 覆盖

### 3. 依赖产物缺失

**症状**: `FileNotFoundError` 或 `artifact not found`
**处理**:
1. 检查上游 workflow 是否执行成功
2. 先补跑上游 workflow
3. 再补跑当前失败任务

### 4. 配置错误

**症状**: 退出码 2
**处理**:
1. 检查对应 workflow JSON 配置
2. 修复配置后重新执行

## 补跑决策指南

| 失败类型 | 建议操作 |
|---------|---------|
| 临时网络/连接问题 | 直接补跑单任务 |
| 上游依赖缺失 | 先补跑上游，再补跑当前 |
| 配置错误 | 修复配置后补跑整 workflow |
| 数据异常 | 调查根因，手动解决后标记 resolved |
| 已知非关键失败 | 标记 ignored |

## API 集成

失败记录也可通过 RDP API 访问：

```
GET /rdp/operations/failures          # 列出所有失败
GET /rdp/operations/failures/open     # 列出 open 失败
POST /rdp/operations/failures/retry   # 补跑失败任务
```
