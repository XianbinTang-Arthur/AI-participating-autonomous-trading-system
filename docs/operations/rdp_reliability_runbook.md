# RDP 长期可靠性运行手册

## 概述

本手册面向 RDP 系统的长期运行维护，覆盖日常运维、异常处理、周期性维护任务。

## 日常运维清单

### 每日检查 (工作日)

| 时间 | 任务 | 命令 | 预期 |
|------|------|------|------|
| 06:30 UTC | 确认 data_maintenance 完成 | 检查 `artifacts/operations/workflow_runs/` 最新报告 | overall_status = success |
| 07:30 UTC | 确认 governance_cycle 完成 | 同上 | overall_status = success |
| 08:00 UTC | 运行可靠性检查 | `python scripts/rdp_run_reliability_check.py` | 退出码 0 |
| 08:05 UTC | 检查告警摘要 | `python scripts/rdp_build_alert_summary.py --current` | 无 critical |
| 随时 | 检查 open 失败 | `python scripts/rdp_record_workflow_failure.py --list-open` | 无 open 失败 |

### 每周检查 (周一)

| 任务 | 命令 | 预期 |
|------|------|------|
| 确认 research_cycle 完成 | 检查周日运行报告 | overall_status = success |
| 确认 decision_cycle 完成 | 检查周日运行报告 | overall_status = success |
| 审核告警历史 | 查看 `artifacts/operations/alerts/history/` | 趋势稳定 |
| 审核失败历史 | 查看 `artifacts/operations/workflow_failures.json` | 无长期 open |
| 检查 release 观察状态 | 查看 release history | 无异常 observing |

## 异常处理 SOP

### Scenario 1: data_maintenance 失败

```
1. 查看运行报告确定失败任务
2. 检查常见原因:
   - 数据库连接? → 检查 PostgreSQL 状态
   - 磁盘空间? → 检查 artifacts 目录大小
   - 超时? → 考虑增加 timeout_seconds
3. 记录失败:
   python scripts/rdp_record_workflow_failure.py \
     --workflow data_maintenance --run-id <RUN_ID> \
     --task <TASK> --error "<ERROR>"
4. 修复后补跑:
   python scripts/rdp_retry_workflow_failure.py \
     --failure-id <FAILURE_ID> --mode task
5. 验证: 检查 governance_cycle 是否能正常运行
```

### Scenario 2: 可靠性检查 critical 告警

```
1. 查看告警详情:
   python scripts/rdp_build_alert_summary.py --json
2. 根据 check_name 定位问题:
   - quality_monitor_exists → 重跑 governance_cycle
   - workflow_configs_exist → 检查 configs 目录
3. 修复后重新检查:
   python scripts/rdp_run_reliability_check.py
4. 确认 overall_status 恢复为 healthy
```

### Scenario 3: 参数 apply 后异常

```
1. 检查 release 状态:
   查看 artifacts/production_workflow/parameter_release_history.json
2. 运行 observation:
   python scripts/rdp_run_post_apply_observation.py --release-id <ID>
3. 评估 rollback:
   python scripts/rdp_evaluate_rollback_recommendation.py --release-id <ID>
4. 如果建议 rollback:
   python scripts/rdp_rollback_active_parameter_set.py \
     --family <FAMILY> --timeframe <TF> --actor operator
```

### Scenario 4: 治理层 DB 连接失败

```
日志中出现: "parameter_registry: DB 写入失败" 或 "DB 读取失败，fallback 到文件"
```

**影响**: 数据仍然写入 JSON 文件，系统正常运行，但 DB 数据会滞后。

**处理**:
```
1. 检查 PostgreSQL 容器状态:
   docker ps | grep aats-postgres
2. 检查环境变量:
   echo $AATS_ACTIVE_PARAMETER_DB_URL
3. 测试 DB 连接:
   psql $AATS_ACTIVE_PARAMETER_DB_URL -c "SELECT 1"
4. 检查 governance schema 是否存在:
   psql ... -c "\dt governance.*"
5. 如果 schema 丢失，重新建表:
   python -c "from aats.data_platform.rdp_models import create_rdp_schema; create_rdp_schema()"
6. DB 恢复后，从 JSON 文件重新种子:
   python scripts/apply_active_parameter_set.py --action seed-db
7. 验证 DB 数据完整性:
   psql ... -c "SELECT count(*) FROM governance.parameter_sets"
```

### Scenario 5: 长时间无 workflow 执行

```
1. 检查调度器状态 (cron / Task Scheduler)
2. 手动执行一轮:
   python scripts/rdp_run_scheduled_workflow.py --workflow governance_cycle --dry-run
3. 确认 dry-run 正常后实际执行:
   python scripts/rdp_run_scheduled_workflow.py --workflow governance_cycle
4. 恢复调度器配置
```

## 周期性维护

### 月度维护

| 任务 | 说明 |
|------|------|
| 清理旧运行报告 | 保留最近 90 天的 workflow_runs |
| 清理告警历史 | 保留最近 90 天的 alerts/history |
| 审核失败记录 | 关闭过期的 open 失败 |
| 检查磁盘使用 | 确保 artifacts 目录未过度增长 |
| 审核 workflow 配置 | 确认 timeout/enabled 设置合理 |

### 季度维护

| 任务 | 说明 |
|------|------|
| 审核可靠性检查规则 | 确认检查项覆盖当前需求 |
| 审核环境策略 | 确认 dev/staging/prod 策略合理 |
| 审核操作文档 | 确保所有 runbook 与现实一致 |
| 性能基准 | 检查各 workflow 执行时间趋势 |

## 容量规划

### Artifact 增长预估

| 产物 | 增长频率 | 单条大小 | 月增长 |
|------|---------|---------|--------|
| workflow_runs | 4/天 + 2/周 | ~5KB | ~700KB |
| alerts/history | 1-2/天 | ~3KB | ~100KB |
| workflow_failures | 按需 | ~1KB | ~10KB |
| gate results | 按需 | ~5KB | ~50KB |
| release records | 按需 | ~3KB | ~30KB |

### 建议清理策略

- 90 天前的 workflow_runs: 可压缩归档
- 180 天前的 alerts/history: 可删除
- resolved/ignored 的 failures: 可定期清理

## 升级注意事项

### 添加新 Workflow

1. 在 `configs/rdp_workflows/` 创建新 JSON 配置
2. 更新调度策略文档
3. 添加对应的可靠性检查项（如需要）
4. 测试: `python scripts/rdp_run_scheduled_workflow.py --workflow <name> --dry-run`

### 修改现有 Workflow

1. 修改对应 JSON 配置
2. 先在 dev 环境测试
3. 在 staging 验证
4. 部署到 prod

### 添加可靠性检查

1. 在 `reliability_checks.py` 添加新检查函数
2. 添加到 `DEFAULT_RELIABILITY_CHECKS` 列表
3. 更新 `reliability_alerting.md` 文档
4. 运行验证: `python scripts/rdp_run_reliability_check.py`

## 联系信息

| 角色 | 职责 |
|------|------|
| RDP Operator | 日常运维、告警响应、参数审批 |
| Data Engineer | 数据层问题、数据库维护 |
| Quant Researcher | 研究层问题、参数质量 |
| System Admin | 调度器配置、环境管理 |
