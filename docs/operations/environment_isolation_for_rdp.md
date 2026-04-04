# RDP 环境隔离策略

## 概述

RDP 通过环境变量 `RDP_ENV` 区分不同运行环境，对关键操作实施不同级别的限制。

## 环境定义

| 环境 | `RDP_ENV` 值 | 用途 |
|------|-------------|------|
| 开发 | `dev` (默认) | 本地开发、测试、调试 |
| 预发布 | `staging` | 集成测试、发布前验证 |
| 生产 | `prod` | 正式运行、实际交易参数管理 |

## 环境策略矩阵

| 策略项 | dev | staging | prod |
|--------|-----|---------|------|
| 参数 Apply | 允许 | 允许 (需 gate) | 允许 (需 gate + 审批) |
| 参数 Rollback | 允许 | 允许 | 允许 |
| Workflow 执行 | 允许 | 允许 | 允许 |
| 需要 Gate Pass | 否 | 是 | 是 |
| 需要 Approval | 否 | 否 | 是 |
| 直接 DB 访问 | 允许 | 允许 | 禁止 (用 API) |
| 观察窗口 | 0h | 24h | 72h |

## 设置环境

### Linux / macOS

```bash
# 临时设置
export RDP_ENV=prod

# 在 .bashrc / .zshrc 中持久化
echo 'export RDP_ENV=prod' >> ~/.bashrc
```

### Windows

```powershell
# 临时设置
$env:RDP_ENV = "prod"

# 永久设置
[System.Environment]::SetEnvironmentVariable("RDP_ENV", "prod", "User")
```

### 在 cron / Task Scheduler 中设置

```bash
# crontab 中
RDP_ENV=prod
0 6 * * * cd /path/to/project && python scripts/rdp_run_scheduled_workflow.py --workflow data_maintenance
```

## 守卫机制

### 代码集成

```python
from aats.data_platform.operations.environment_guard import (
    guard_parameter_apply,
    guard_workflow_execution,
    get_current_environment,
    get_observation_window_hours,
)

# 检查是否允许 apply
result = guard_parameter_apply()
if not result.allowed:
    raise PermissionError(result.reason)

# 获取观察窗口
hours = get_observation_window_hours()  # prod=72, staging=24, dev=0
```

### 守卫函数列表

| 函数 | 说明 |
|------|------|
| `guard_parameter_apply()` | 检查参数 apply 权限 |
| `guard_parameter_rollback()` | 检查参数 rollback 权限 |
| `guard_workflow_execution(name)` | 检查 workflow 执行权限 |
| `guard_direct_db_access()` | 检查直接 DB 访问权限 |
| `get_observation_window_hours()` | 获取观察窗口时长 |

### GuardResult 返回值

```python
@dataclass(frozen=True)
class GuardResult:
    allowed: bool       # 是否允许
    environment: str    # 当前环境名
    operation: str      # 操作名
    reason: str         # 原因说明
```

## 数据隔离

### Artifacts 目录

所有环境使用同一个 `artifacts/` 目录结构，但通过以下方式隔离：

1. **dev**: 可以自由读写所有 artifact
2. **staging**: artifact 通过 gate 写入保护
3. **prod**: artifact 通过 gate + approval 双重保护

### 数据库

- **dev/staging**: 允许直接 PostgreSQL 访问
- **prod**: 必须通过 RDP API 层访问，禁止直接连接

### 配置文件

所有环境共享 `configs/rdp_workflows/` 目录下的 workflow 配置。
环境特定的差异通过策略层（`environment_guard.py`）实现，而非配置分叉。

## 安全注意事项

1. **默认环境**: 未设置 `RDP_ENV` 时默认为 `dev`，这意味着新部署默认处于最宽松模式
2. **生产部署**: 部署到生产时务必设置 `RDP_ENV=prod`
3. **跨环境操作**: 不支持从一个环境直接操作另一个环境的 artifact
4. **环境验证**: 关键操作前通过 `get_environment_info()` 确认当前环境

## 查看环境状态

```python
from aats.data_platform.operations.environment_guard import print_environment_status
from pathlib import Path

print_environment_status(Path("."))
```

输出示例：
```
RDP Environment Status
  Environment:    prod
  Is Production:  True
  Artifacts Root: /path/to/project/artifacts
  Config Source:  /path/to/project/configs
  Description:    生产环境: 需要审批、gate 通过、长观察窗口

  Policies:
    Parameter Apply:    Yes
    Parameter Rollback: Yes
    Workflow Execution: Yes
    Require Gate Pass:  Yes
    Require Approval:   Yes
    Direct DB Access:   No
    Observation Window: 72h
```
