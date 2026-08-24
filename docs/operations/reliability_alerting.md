# RDP 可靠性与告警

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。

> 最后核对：2026-08-22（代码基线 `be9179e`）。当前 reliability checker 仍有两项 legacy 覆盖缺口：workflow check 只检查 4 个核心 JSON，active parameter check 只检查 artifact 文件，不能代替 10-workflow allowlist 或 runtime DB active set 验证。


## 概述

RDP 可靠性检查系统定期扫描各层健康状态，生成告警摘要。
检查结果保存到 `artifacts/operations/alerts/current_alerts.json`。

## 可靠性检查项

| 检查名 | 分类 | 严重级别 | 说明 |
|--------|------|---------|------|
| `quality_monitor_exists` | governance | critical | quality_monitor_summary.json 存在且有效 |
| `active_decisions_exists` | decision | warning | active_decisions.json 存在 |
| `workflow_configs_exist` | operations | critical | **仅**检查 data/research/governance/decision 4 个核心 JSON；当前实际共有 10 个 workflow |
| `artifact_directories` | operations | warning | 关键 artifact 目录存在 |
| `open_failures` | operations | warning | 无未处理的 workflow 失败 |
| `release_history_exists` | decision | info/warning | release 历史可读，检测 observing 状态 |
| `active_parameters` | decision | info | 检查 legacy `active_parameters_registry.json` artifact；不验证 `governance.active_parameter_sets` |

## 严重级别

| 级别 | 含义 | 建议操作 |
|------|------|---------|
| `critical` | 核心功能受损 | 立即处理 |
| `warning` | 需要关注 | 下次维护窗口处理 |
| `info` | 信息性 | 无需操作 |

## 使用方式

### 运行可靠性检查

```bash
# 文本输出
python scripts/rdp_run_reliability_check.py

# JSON 输出
python scripts/rdp_run_reliability_check.py --json
```

退出码：
- 0 = 全部通过 (healthy)
- 1 = 有 critical 告警
- 2 = 有 warning 告警

### 构建告警摘要

```bash
# 运行检查并生成告警
python scripts/rdp_build_alert_summary.py

# 查看当前告警（不重新检查）
python scripts/rdp_build_alert_summary.py --current

# 确认告警
python scripts/rdp_build_alert_summary.py --acknowledge ALERT_ID
```

## 告警摘要格式

```json
{
  "generated_at": "2026-04-04T07:00:00Z",
  "overall_status": "warning",
  "total_checks": 7,
  "passed": 5,
  "failed": 2,
  "critical_alerts": 0,
  "warning_alerts": 2,
  "alerts": [
    {
      "alert_id": "alert_open_failures_20260404_070000",
      "check_name": "open_failures",
      "category": "operations",
      "severity": "warning",
      "detail": "2 open failure(s) need attention",
      "timestamp": "2026-04-04T07:00:00Z",
      "acknowledged": false
    }
  ],
  "check_results": [...]
}
```

## Overall Status 判定

```
any critical failed → overall = "critical"
any warning failed  → overall = "warning"
all passed          → overall = "healthy"
```

## 告警历史

每次运行检查的告警快照保存在：
```
artifacts/operations/alerts/history/alerts_YYYYMMDD_HHMMSS.json
```

## 集成建议

### 与 Workflow 调度集成

独立且启用的 `reliability_cycle` 每小时第 15 分钟运行可靠性检查。disabled 的 `decision_cycle` 也保留同名任务，但不应依赖它提供周期检查：

```json
{
  "name": "reliability_check",
  "command": "python scripts/rdp_run_reliability_check.py",
  "allow_failure": true
}
```

### 与外部监控集成

可通过解析退出码与外部监控系统集成；通知目标必须来自安全配置，不把 webhook 写进仓库或文档：

```bash
python scripts/rdp_run_reliability_check.py
exit_code=$?
if [ $exit_code -eq 1 ]; then
    # 调用组织批准的通知适配器；不要在脚本中硬编码 webhook/token
    notify_rdp_critical
fi
```

### 添加自定义检查

在 `reliability_checks.py` 中添加新的检查函数：

```python
def check_custom(root: Path) -> ReliabilityCheckResult:
    # 自定义检查逻辑
    return ReliabilityCheckResult(
        name="custom_check",
        category="custom",
        passed=True,
        severity="info",
        detail="all good",
    )
```

然后添加到 `DEFAULT_RELIABILITY_CHECKS` 列表。
