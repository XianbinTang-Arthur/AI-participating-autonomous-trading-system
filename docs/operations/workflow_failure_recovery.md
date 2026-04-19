# Workflow 失败恢复指南

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。


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

---

## 与交易系统安全相关的失败

RDP workflow 失败通常只影响研究和参数治理，不应直接修改 live 交易状态。但以下失败需要按生产事件处理：

| 失败 | 风险 | 处理 |
|------|------|------|
| pre-apply gate 失败或缺失 | 未验证参数进入 live | 阻止 apply，记录 failure，重新跑 gate |
| apply history 写入失败 | 无法审计参数变更 | 停止后续 apply，修复 DB/文件双写后补录 |
| active parameter DB 写入失败并 fallback 文件 | DB/JSON 可能漂移 | 恢复 DB 后运行 `seed-db` 并比对 active registry |
| observation/rollback workflow 失败 | 异常参数可能继续生效 | 人工评估是否立即 rollback |
| 生产 apply 缺少 gate 记录 | 绕过门控 | 视为流程违规，立即审计 active parameter 和 release history |

如果上述失败发生在 `spot_live` 或 `derivatives_live` 期间，Operator 应同时检查主交易系统：

1. `/system/health`
2. kill switch 状态
3. reconciliation 最新报告
4. active parameter version
5. 最近 decision / order intent 是否显著变化
