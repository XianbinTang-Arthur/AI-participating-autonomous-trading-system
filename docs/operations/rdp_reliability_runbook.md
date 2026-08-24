# RDP 长期可靠性运行手册

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。

> 最后核对：2026-08-22（代码基线 `be9179e`）。当前 scheduler/daemon 在容器内运行，workflow 共 10 个；`decision_cycle`、`release_cycle` disabled，旧 active JSON seed 和直写 rollback CLI 不可用。


## 概述

本手册面向 RDP 系统的长期运行维护，覆盖日常运维、异常处理、周期性维护任务。

## 日常运维清单

### 每日检查 (工作日)

| 时间 | 任务 | 命令 | 预期 |
|------|------|------|------|
| 06:30 UTC | 确认 data_maintenance 完成 | 检查 `artifacts/operations/workflow_runs/` 最新报告 | overall_status = success |
| 07:30 UTC | 确认 governance_cycle 完成 | 同上 | overall_status = success |
| 每小时 :15 后 | 确认 reliability_cycle | `/rdp/tasks/status` + reliability output | 最近 slot 完成 |
| 08:05 UTC | 检查告警摘要 | `python scripts/rdp_build_alert_summary.py --current` | 无 critical |
| 08:10 UTC | 检查 daemon 心跳 | 查看 `/rdp/health` 中 `rdp-daemon` 项 | `status=ok` 且 heartbeat < 45s |
| 随时 | 检查 open 失败 | `python scripts/rdp_record_workflow_failure.py --list-open` | 无 open 失败 |

### 每周检查 (周一)

| 任务 | 命令 | 预期 |
|------|------|------|
| 确认 research_cycle 完成 | 检查周日运行报告 | overall_status = success |
| 确认 decision_cycle 保持禁用 | 检查 workflow 定义/调度状态 | 不应自动入队 |
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
   由当前 Operator session 申请 action=rollback token，
   调用 POST /rdp/parameters/rollback，并核对 active/history/provenance
```

### Scenario 4: 治理层 DB 连接失败

```
日志中出现: "parameter_registry: DB 写入失败" 或 "DB 读取失败，fallback 到文件"
```

**影响**: 不同治理模块的降级语义不同。主交易 runtime active parameter loader 不读 JSON fallback，会退化到 profile 参数并记录 error；不能声称“系统正常运行”。

**处理**:
```
1. 停止新的参数发布，查看 /rdp/health 和数据库容器健康/日志。
2. 不输出连接串或环境文件内容；使用项目健康检查确认 research/governance DB。
3. 核对 governance migrations 和 78 表 schema，不用临时 Python 建表替代迁移。
4. 恢复数据库真源；不得从 active JSON 人工 seed runtime 状态。
5. 核对 active parameter sets、apply history、release、scheduler state 和 runtime provenance。
```

### Scenario 5: 长时间无 workflow 执行

```
1. 检查 rdp-daemon heartbeat、scheduler state 和 /rdp/tasks/status。
2. 用 scripts/rdp_schedule_workflows.py --dry-run --json 查看应到期 slot。
3. 需要补跑时通过 POST /rdp/tasks/trigger 入队，不绕开任务队列直接执行。
4. 确认同 workflow active 唯一约束和 earliest_start_at。
```

### Scenario 6: `rdp-daemon` 心跳陈旧或丢失

```
1. 查看 /rdp/health，确认阻断项是否为 rdp_daemon_status_missing / rdp_daemon_unhealthy
2. 检查 governance.rdp_runtime_status:
   psql ... -c "SELECT component, status, heartbeat_at FROM governance.rdp_runtime_status"
3. 检查 daemon 容器日志:
   docker logs aats-rdp-daemon --tail 200
4. 如 daemon 已停止或报错，修复原因后通过 bash scripts/deploy.sh --skip-commit 重建/恢复，禁止手工 restart 单个 Compose 服务。
5. 再次确认 /rdp/health 中 heartbeat 已恢复 < 45s
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
