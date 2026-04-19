# 周期复盘工作流

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。


## 概述

RDP 支持按周/月生成长周期复盘报告，帮助 operator 和 owner 理解系统的长期趋势。

## 复盘内容

1. **Metrics Snapshot 汇总** — 生成最新 metrics 快照
2. **Release History 汇总** — 周期内的 release 统计
3. **Rollback History 汇总** — rollback 频率和比率
4. **Workflow 运行统计** — 成功率、失败率
5. **Family/Timeframe Ranking** — 按 combo 统计排名
6. **Improvement Suggestions** — 自动生成改进建议

## 使用方式

### 周复盘

```bash
python scripts/rdp_run_periodic_review.py --window weekly
```

### 月复盘

```bash
python scripts/rdp_run_periodic_review.py --window monthly
```

### 按维度筛选

```bash
python scripts/rdp_run_periodic_review.py --window weekly --family independent --timeframe 15m
```

## 输出

复盘报告保存到:
```
artifacts/reviews/weekly/<review_id>/
  review_summary.json    # 结构化数据
  review_report.md       # 可读报告

artifacts/reviews/monthly/<review_id>/
  review_summary.json
  review_report.md
```

## 复盘报告内容

### Summary 概览

| 字段 | 说明 |
|------|------|
| total_releases | 周期内 release 总数 |
| successful_releases | 成功 release 数 |
| total_applies | 参数 apply 次数 |
| total_rollbacks | rollback 次数 |
| rollback_ratio | rollback 比率 |
| workflow_runs / workflow_success | workflow 运行和成功次数 |
| open_failures | 未处理的失败数 |

### Effectiveness 统计

| 结论 | 说明 |
|------|------|
| effective | 被评为有效的 release |
| mixed | 结果混合 |
| ineffective | 被评为无效 |
| insufficient_evidence | 证据不足 |

### Family/Timeframe Ranking

按 combo_key 聚合：
- release 数量
- apply 成功数
- effective 数
- ineffective 数

### Improvement Suggestions

自动生成的改进建议，基于:
- 高 rollback 率 → 审查 recommendation 质量
- 高 workflow 失败率 → 检查配置和依赖
- 未处理的失败 → 处理 open failures
- ineffective releases → 审查研究质量
- 无 release → 检查 decision cycle

## 建议复盘节奏

| 频率 | 谁做 | 目标 |
|------|------|------|
| 每周一 | Operator | 检查上周 workflow/release 状态 |
| 每月初 | Owner + Operator | 长期趋势分析，调整策略 |
| 季度 | 全团队 | 系统性回顾和规划 |

## 将 Review 转成 Backlog

复盘中的改进建议可以直接转成 improvement backlog:

```python
from aats.data_platform.metrics.backlog_builder import backlog_from_review
items = backlog_from_review(root, review)
```

或在生成 backlog 时自动包含:
```bash
python scripts/rdp_generate_improvement_backlog.py
```
