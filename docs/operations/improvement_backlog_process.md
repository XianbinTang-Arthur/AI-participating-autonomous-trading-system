# Improvement Backlog 流程

## 概述

Improvement Backlog 是 RDP 持续优化的核心工具，将 metrics 分析和复盘结果转化为可执行的改进任务。

## Backlog 来源

| 来源 | 检测逻辑 | 优先级 |
|------|---------|--------|
| 高失败率 Workflow | 失败率 > 30% | high |
| 高频 Rollback | rollback 比率 > 30% | high |
| Stale Recommendations | draft 比率 > 50% | medium |
| Ineffective Releases | 同 combo ≥ 2 次 ineffective | high |
| 低 Evidence Completeness | completeness < 50% | medium |
| 未处理 Critical Alerts | critical alert > 0 | high |
| Periodic Review 建议 | review 中的 suggestions | 按建议 |

## Backlog Item 格式

```json
{
  "backlog_id": "bl_20260404_120000_workflow",
  "created_at": "2026-04-04T12:00:00Z",
  "source": "workflow_failure_analysis",
  "category": "reliability",
  "family": null,
  "timeframe": null,
  "priority": "high",
  "problem_statement": "Workflow 'governance_cycle' 失败率 3/5 (60%)",
  "suggested_action": "审查 governance_cycle 的失败原因，优化配置或修复根因",
  "status": "open"
}
```

### 状态流转

```
open ──→ in_progress ──→ resolved
  │
  └──→ ignored
```

## 使用方式

### 生成 Backlog

```bash
# 自动检测并生成
python scripts/rdp_generate_improvement_backlog.py

# JSON 输出
python scripts/rdp_generate_improvement_backlog.py --json
```

### 查看 Backlog

```bash
python scripts/rdp_generate_improvement_backlog.py --list
```

### 更新状态

```bash
# 标记开始处理
python scripts/rdp_generate_improvement_backlog.py \
    --update bl_20260404_120000_workflow \
    --status in_progress

# 标记已解决
python scripts/rdp_generate_improvement_backlog.py \
    --update bl_20260404_120000_workflow \
    --status resolved \
    --notes "已优化 timeout 配置"
```

## 合并逻辑

每次运行 `generate_improvement_backlog()`:
1. 扫描所有来源，生成新的 open items
2. 保留已有的 non-open items (in_progress / resolved / ignored)
3. 替换所有旧的 open items 为新生成的

这意味着：
- 已解决的问题不会再出现
- 新发现的问题自动加入
- 持续存在的问题会保持

## 优先级定义

| 优先级 | 含义 | 建议响应时间 |
|--------|------|-------------|
| high | 严重影响系统价值 | 下一工作日 |
| medium | 需要关注但不紧急 | 一周内 |
| low | 改进建议 | 下次维护窗口 |

## 输出位置

```
artifacts/metrics/improvement_backlog.json
```

## 与 Periodic Review 集成

周期复盘的 improvement suggestions 可以自动转成 backlog items:

```python
from aats.data_platform.metrics.backlog_builder import backlog_from_review

# 从 review 生成额外 items
items = backlog_from_review(root, review_data)
```

## 消费 Backlog

### Operator 日常

1. 每日检查 high priority items
2. 认领并标记 in_progress
3. 完成后标记 resolved

### 周复盘

1. 运行 `rdp_generate_improvement_backlog.py` 刷新
2. 在周复盘中讨论 open items
3. 分配责任人

### 月复盘

1. 审查所有 resolved items — 确认效果
2. 审查 ignored items — 是否需要重新评估
3. 分析 backlog 趋势 — 问题是否在减少
