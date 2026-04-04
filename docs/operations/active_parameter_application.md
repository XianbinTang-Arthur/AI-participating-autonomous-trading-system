# Active Parameter Set 应用操作指南

## 1. 概述

Active Parameter Set 是 RDP 治理层产出的研究参数回灌到主交易系统的唯一受控通道。

主交易系统启动时读取 `configs/active_parameter_sets/` 目录下的参数文件，
将研究参数注入 family/timeframe 级别的策略配置。

### 参数优先级（从低到高）

```
hardcoded defaults (settings.py)
  < strategy_profiles/*.yaml
  < active parameter set           ← 本机制
  < runtime emergency override
```

## 2. 目录结构

```
configs/active_parameter_sets/
  independent_15m.json      # independent family, 15m timeframe
  independent_1h.json       # independent family, 1h timeframe
  directional_15m.json      # directional family, 15m timeframe
  directional_1h.json       # directional family, 1h timeframe
```

## 3. 文件格式

每个 JSON 文件结构:

```json
{
  "meta": {
    "parameter_set_id": "ps_20260404_072612_a5cc10",
    "family": "independent",
    "timeframe": "15m",
    "status": "active",
    "source_round_id": "20260404_073000_abcd1234",
    "applied_at": "2026-04-04T07:30:00.000000+00:00",
    "applied_by": "apply_active_parameter_set.py",
    "approval_recommendation_id": "rec_20260404_153614_abc123"
  },
  "values": {
    "signal_edge_scale_bps": 12.0,
    "min_confirm_ticks": 2,
    "min_safe_net_edge_bps": 0.0,
    "score_stability_threshold": 2.0
  }
}
```

### meta 字段说明

| 字段 | 必填 | 说明 |
|------|------|------|
| parameter_set_id | Y | 来源 parameter set ID |
| family | Y | 策略 family |
| timeframe | Y | 时间周期 |
| status | Y | 固定为 "active" |
| source_round_id | N | 来源 research round ID |
| applied_at | Y | 应用时间 (UTC ISO) |
| applied_by | Y | 应用来源 (脚本名或操作者) |
| approval_recommendation_id | N | 关联的审批 recommendation |

### values 字段说明

values 包含 family/timeframe 级别的策略参数，key-value 对。
具体字段取决于 family 类型:

**independent family 参数:**
- `signal_edge_scale_bps` — Alpha 信号校准 (8-15 典型)
- `min_confirm_ticks` — 确认 bar 数量 (2-4)
- `min_safe_net_edge_bps` — 最小净边际阈值 (0-5)
- `score_stability_threshold` — 信号稳定性门控 (1.5-3.0)

**directional family 参数:**
- `directional_trend_weight` — 趋势/收益混合权重 (0.5-0.9)
- `taker_fee_bps` — 成本假设 (3-7)
- `slippage_bps` — 滑点假设 (1-3)

## 4. 操作流程

### 4.1 查看可用参数

```bash
python scripts/apply_active_parameter_set.py --action show
```

### 4.2 应用指定 parameter set

```bash
# 预览
python scripts/apply_active_parameter_set.py --action apply --ps-id ps_20260404_072612_a5cc10 --dry-run

# 执行
python scripts/apply_active_parameter_set.py --action apply --ps-id ps_20260404_072612_a5cc10
```

### 4.3 从所有 frozen 参数自动生成

```bash
python scripts/apply_active_parameter_set.py --action apply-frozen
```

### 4.4 查看当前 active sets

```bash
python scripts/apply_active_parameter_set.py --action show-active
```

### 4.5 清除 active set

```bash
python scripts/apply_active_parameter_set.py --action clear --combo independent_15m
```

## 5. 与主系统集成

### 5.1 启动时加载

主系统启动时通过 `aats.bootstrap.active_parameters` 模块自动加载:

```python
from aats.bootstrap.active_parameters import (
    load_all_active_parameter_sets,
    build_settings_overrides,
)

# 加载所有 active sets
active = load_all_active_parameter_sets(project_root=ROOT)

# 构建设置覆盖
overrides = build_settings_overrides(project_root=ROOT)
settings = settings.model_copy(update=overrides)
```

### 5.2 API 查询

```
GET /rdp/parameters/active
```

返回所有 active parameter sets 的摘要。

## 6. 审计追踪

所有参数应用操作记录在:

```
artifacts/governance/application_logs/parameter_application_history.jsonl
```

每行一条 JSON 记录:

```json
{
  "timestamp": "2026-04-04T07:30:00.000000+00:00",
  "action": "apply",
  "combo_key": "independent_15m",
  "parameter_set_id": "ps_20260404_072612_a5cc10",
  "recommendation_id": null
}
```

## 7. 安全规则

1. **参数应用是显式动作**，不会自动隐式更新
2. 只允许 `frozen` 或 `candidate` 状态的参数被应用
3. `draft` 状态参数需先经过 governance 审批
4. 每次应用都有审计日志
5. 文件使用原子写入，防止写入损坏
6. 主系统运行中修改参数需要重启或 reload
