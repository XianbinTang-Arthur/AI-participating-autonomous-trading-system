# RDP Workflow 调度策略

## 概述

RDP 使用 JSON 配置驱动的 Workflow 调度系统，通过统一入口脚本 `rdp_run_scheduled_workflow.py` 运行。
每个 Workflow 包含一组有序任务，按顺序执行，支持超时控制、失败停止策略和 dry-run 模式。

> **2026-04-07 重要变更**: 数据采集已从常驻 daemon 模式切换为日批模式。
> 原 `rdp_realtime_daemon.py` 的 60s tick 已废弃, 新增 `rdp_run_daily_ingest.py`
> 由 `data_maintenance` workflow 每天 04:00 UTC 调用一次。详见
> [Section "数据采集迁移到日批"](#数据采集迁移到日批) 章节。

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
| `data_maintenance` | 每日 04:00 UTC | **日批 OKX 增量采集** + 数据缺口检测 + Gold 层构建 + 索引重建 |
| `governance_cycle` | 每日 07:00 UTC | 质量监控、产物验证、轮次索引刷新 |
| `research_cycle` | 每周日 08:00 UTC | 研究轮次、归因分析、执行真实性评估 |
| `decision_cycle` | 每周（研究后）或按需 | 决策轮次、可靠性检查、观察检查 |

## 调度时序

```
Day N 04:00 UTC ─ data_maintenance  (日批 OKX → Bronze/Silver/Gold)
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
0 4 * * * cd /path/to/project && python scripts/rdp_run_scheduled_workflow.py --workflow data_maintenance >> /var/log/rdp/data_maintenance.log 2>&1
0 7 * * * cd /path/to/project && python scripts/rdp_run_scheduled_workflow.py --workflow governance_cycle >> /var/log/rdp/governance_cycle.log 2>&1
0 8 * * 0 cd /path/to/project && python scripts/rdp_run_scheduled_workflow.py --workflow research_cycle >> /var/log/rdp/research_cycle.log 2>&1
0 10 * * 0 cd /path/to/project && python scripts/rdp_run_scheduled_workflow.py --workflow decision_cycle >> /var/log/rdp/decision_cycle.log 2>&1
```

```powershell
# Windows Task Scheduler (schtasks) 示例
schtasks /create /tn "RDP_DataMaintenance" /tr "python scripts/rdp_run_scheduled_workflow.py --workflow data_maintenance" /sc daily /st 04:00
schtasks /create /tn "RDP_GovernanceCycle" /tr "python scripts/rdp_run_scheduled_workflow.py --workflow governance_cycle" /sc daily /st 07:00
schtasks /create /tn "RDP_ResearchCycle" /tr "python scripts/rdp_run_scheduled_workflow.py --workflow research_cycle" /sc weekly /d SUN /st 08:00
schtasks /create /tn "RDP_DecisionCycle" /tr "python scripts/rdp_run_scheduled_workflow.py --workflow decision_cycle" /sc weekly /d SUN /st 10:00
```

---

## 数据采集迁移到日批

### 背景

2026-04-07 之前, 数据采集走常驻 daemon (`scripts/rdp_realtime_daemon.py` 60s tick)。
该模式存在严重资源浪费:

- 每天 ~7800 次 OKX REST 调用 (4 symbol × 5 timeframe), **99% 浪费**
- 1m candle 每天 1440 bar/symbol, **没有任何 RDP 消费方使用**
- 常驻 Python 进程 + 数据库连接池, 增加运维负担

### 为什么日批就够了

| RDP 消费方 | 实际 cadence | 是否需要 intra-minute 数据 |
|---|---|---|
| `data_maintenance` workflow | daily 04:00 UTC | ❌ 否 |
| `governance_cycle` workflow | daily 07:00 UTC | ❌ 否 |
| `research_cycle` workflow | weekly Sunday | ❌ 否 |
| `decision_cycle` workflow | weekly | ❌ 否 |
| 实盘交易引擎 | 实时 | ✅ 是 — **但走 OKX websocket, 不读 RDP 数据** |

**结论**: 没有任何 RDP 消费方需要 60s tick 频率, daemon 是为不存在的 use case 服务的。

### 新方案: `rdp_run_daily_ingest.py`

**入口脚本**: `scripts/rdp_run_daily_ingest.py`

**工作内容** (一次执行覆盖 24h+ 数据):

1. 对每个 (symbol, timeframe) 调用 `collect_candles_incremental()` 增量拉取 (基于 checkpoint)
2. 对每个 swap symbol 调用 `collect_funding_incremental()` 增量拉取 funding rate
3. 对每个新增 (symbol, timeframe) 重建 Gold replay bars
4. 在 silver 层运行 Gap 检测 (lookback 24h)

**默认配置** (config.py):

- `rolling_candles_symbols`: BTC-USDT, ETH-USDT, BTC-USDT-SWAP, ETH-USDT-SWAP
- `rolling_candles_timeframes`: 15m, 1H (1m/5m 已废弃移除)
- `rolling_funding_symbols`: BTC-USDT-SWAP, ETH-USDT-SWAP

**手动调用**:

```bash
# 标准调用 (符合 cron 推荐)
python scripts/rdp_run_daily_ingest.py

# Dry run, 仅打印计划
python scripts/rdp_run_daily_ingest.py --dry-run

# 跳过 Gold 构建
python scripts/rdp_run_daily_ingest.py --no-gold

# 限制 symbol/tf
python scripts/rdp_run_daily_ingest.py --symbols BTC-USDT-SWAP --timeframes 15m

# 增大回拉窗口 (灾后恢复, 拉最近 30 天的 15m 数据)
python scripts/rdp_run_daily_ingest.py --max-pages 100
```

**通过 workflow 调用** (推荐):

```bash
python scripts/rdp_run_scheduled_workflow.py --workflow data_maintenance
```

`data_maintenance` workflow 已添加 `daily_ingest` 作为第一个 task, allow_failure=false,
失败会立即停止后续任务。

### 资源对比

| 指标 | daemon (旧) | 日批 (新) | 改善 |
|---|---|---|---|
| OKX REST 调用 / 天 (4 symbol) | ~7800 | ~50-80 | **~100×** |
| 常驻进程 | 1 | 0 | 完全消除 |
| 1m timeframe API 流量 | 5760 calls/day | 0 | 砍除无消费者 tf |
| 监控对象 | systemd unit + 健康检查 | 1 个 cron entry | 极简 |
| 失败盲区 | 1m (静默卡死风险) | 24h (cron 自动告警) | 更可控 |

### 灰度迁移建议

1. **第 1 周**: 双跑 — daemon 继续运行, 同时启用 cron 调用 daily_ingest, 对比两边数据完整性
2. **第 2 周**: 停掉 daemon (`systemctl stop rdp-realtime-daemon`), 仅依赖 cron daily_ingest
3. **第 3 周**: 确认无问题后, 删除 daemon systemd unit / 任务计划程序条目

### 回滚预案

如果日批方案出现问题, 可立即回滚:

```bash
# 1. 重新启动 daemon
python scripts/rdp_start.py

# 2. 在 data_maintenance.json 中将 daily_ingest 设置为 enabled=false
```

但需要同时调查根因, 因为 daemon 也有"静默卡死"风险, 不应作为长期方案。

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
