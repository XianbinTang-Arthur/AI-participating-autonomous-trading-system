# AIParticipatingAutonomousTradingSystem

## 项目定位

这是一个面向加密资产交易的事件驱动交易系统原型，当前重点是：

- 真实市场数据接入
- 受保护的交易决策与风控链路
- 本地组合状态、审计、回放、对账与恢复
- OKX 模拟盘提交流程验证

当前仓库**不支持真实资金自动交易**。  
目前可稳定使用的是：

- 本地演示行情 + 本地 paper execution
- OKX 真实行情 + 本地 paper execution
- OKX 模拟盘受保护提交

## 当前能力边界

### 已支持

- `local_demo`
  - 本地演示行情
  - 本地 paper execution
  - 适合功能联调

- `real_market_paper`
  - OKX 真实市场行情
  - OKX 只读账户快照
  - 本地 paper execution
  - 适合 shadow run 和策略观察

- `guarded_simulated_submit_dry_run`
  - OKX 真实行情
  - OKX 模拟盘账户读取
  - 生成真实下单载荷，但不真正提交

- `guarded_simulated_submit_enabled`
  - OKX 真实行情
  - OKX 模拟盘账户读取
  - OKX 模拟盘真实提交
  - 受风控、对账、健康状态、恢复状态、持久化要求共同约束

### 明确不支持

- 真实资金自动交易
- `autonomous_live`
- 非模拟盘的 OKX 真实提交

代码里保留了部分未来扩展边界，但当前版本会显式阻断真实资金实盘线路。

## 系统主链路

当前系统使用一条共享主链路来覆盖本地演示、真实行情 paper、以及 OKX 模拟盘受保护提交：

1. 市场数据接入
2. 特征计算
3. 决策上下文构建
4. baseline / AI 决策
5. policy 风控
6. risk 风控
7. execution planning
8. order intent -> order state -> fill
9. portfolio snapshot
10. reconciliation / repair / recovery
11. audit / replay / operator control

核心模块大致如下：

- `aats/services/market_gateway`
  - 行情接入与标准化
- `aats/services/feature_engine`
  - 特征计算
- `aats/services/decision_engine`
  - baseline、AI、目标仓位、触发策略
- `aats/services/governance_engine`
  - mode、policy、risk、kill switch、health
- `aats/services/execution_engine`
  - 订单生命周期、OKX / paper adapter、obligation、outbox、recovery
- `aats/services/portfolio_service`
  - 仓位、PnL、snapshot、重建
- `aats/services/reconciliation_service`
  - 对账、回放、修复
- `aats/services/operator`
  - 控制面查询、审计、运行时控制

## 当前策略概览

当前默认策略是以 `baseline_only` 为主的方向性策略，已经做过一轮减噪和费用保护：

- 弱 `flat` 信号默认保持已有衍生品仓位，不再直接平仓
- `entry / scale-in / reversal` 使用分层资格门
- 只在 `trend / breakout` 等更可靠的市场状态里允许新开方向
- 引入交易成本门，避免过弱 edge 直接转成真实交易
- 对同类瞬时失败平仓请求增加冷却时间，避免重复冲击交易所

这套策略当前更适合：

- 模拟盘验证
- shadow run
- 决策/执行/恢复链路观察

它还不等于已验证过正期望的真实资金策略。

## 快速开始

### 1. 创建虚拟环境并安装

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
```

### 2. 准备 PostgreSQL

当前建议统一使用 PostgreSQL 持久化。

执行迁移：

```powershell
psql postgresql://aats:aats@localhost:5432/aats -f migrations/0001_postgres_storage.sql
psql postgresql://aats:aats@localhost:5432/aats -f migrations/0002_execution_and_audit_correlation.sql
psql postgresql://aats:aats@localhost:5432/aats -f migrations/0003_audit_execution_plan_refs.sql
psql postgresql://aats:aats@localhost:5432/aats -f migrations/0004_operator_users.sql
psql postgresql://aats:aats@localhost:5432/aats -f migrations/0005_storage_scope_columns.sql
psql postgresql://aats:aats@localhost:5432/aats -f migrations/0006_order_obligations.sql
psql postgresql://aats:aats@localhost:5432/aats -f migrations/0007_execution_outbox.sql
```

### 3. 选择运行配置

常用配置：

- `AATS_CONFIG_PROFILE=local_demo`
- `AATS_CONFIG_PROFILE=real_market_paper`
- `AATS_CONFIG_PROFILE=guarded_simulated_submit_dry_run`
- `AATS_CONFIG_PROFILE=guarded_simulated_submit_enabled`

### 4. 启动 API 与 UI

```powershell
.\.venv\Scripts\python.exe -m uvicorn apps.api_gateway.main:app --host 127.0.0.1 --port 8000
```

启动后可访问：

- `http://127.0.0.1:8000/ui`
- `http://127.0.0.1:8000/system/health`
- `http://127.0.0.1:8000/system/runtime`
- `http://127.0.0.1:8000/reconciliation/latest`

## 配置建议

### 最低安全建议

- 持久化存储使用 PostgreSQL
- 模拟盘提交必须启用持久化
- 默认仅交易单一标的
- 默认仅启用 `15m` 决策周期
- 保持 `AATS_GUARDED_EXECUTION_DRY_RUN=false` 仅在 OKX 模拟盘验证时使用
- 不要把任何真实资金交易目标暴露给当前版本

### 交易配置建议

- 初期只交易一个 `allowed_symbol`
- 提高决策最小时间间隔与最小价格变动阈值，减少手续费磨损
- baseline-only 模式下优先验证 `hold / exit / fee gate` 是否符合预期
- 在确认策略经济性之前，不要放大仓位或提高开仓频率

## Operator 认证与安全说明

### 当前认证模型

当 `AATS_OPERATOR_AUTH_ENABLED=true` 时：

- 浏览器会话登录基于 `operator_users` 表
- `viewer` 只能读
- `operator` / `admin` 可执行控制动作
- `write API key` 仍保留为兼容路径，并映射到管理员级写权限

### 已移除的高风险行为

当前版本**已移除运行时自动初始化 operator 用户表的逻辑**：

- 启动时不再从静态文件自动 seed 用户
- 仓库中不再保留 `docs/user.txt`
- API / UI 不再暴露 bootstrap pending / bootstrap configured 一类状态

这样做的原因是：

- 避免数据库清空后再次自动写入固定账号
- 避免静态凭据文件在部署和恢复中被误用
- 避免 operator 认证被隐式回退到文件初始化逻辑

### 当前推荐做法

如果你要在 PostgreSQL 持久化运行时启用浏览器会话登录：

1. 先在 `operator_users` 表中预置至少一个启用的 `admin`
2. 或者临时配置 `AATS_OPERATOR_WRITE_API_KEY`，启动后通过管理接口创建管理员，再移除该 write key

当前版本对这件事是**fail-closed** 的：

- 当使用 PostgreSQL 持久化
- 且开启 `operator_auth`
- 且开启 session 登录
- 且没有已启用的管理员
- 且没有 write API key

系统会直接拒绝启动，防止把运行时带到“看起来启动成功，但实际上没有管理员入口”的危险状态。

### 已有保护

- 禁止删除自己
- 禁止禁用自己
- 禁止移除最后一个启用的管理员
- 当数据库中的用户被禁用后，会话会立刻失效

## 运行时可观测性

重点接口：

- `GET /system/health`
  - 当前健康状态、阻断原因、是否 halted
- `GET /system/runtime`
  - profile、环境能力、policy、recovery posture
- `GET /decision/latest`
- `GET /orders/recent`
- `GET /fills/recent`
- `GET /portfolio/latest`
- `GET /reconciliation/latest`
- `GET /audit/latest`
- `GET /execution/errors`

日志目录默认在 `logs/` 下，包含运行日志和轮转文件。

## 当前工程改进点

近期已经完成的关键修复包括：

- OKX 已接受订单在后续查询失败时不再被误终态化为 `FAILED`
- 多 symbol 账户 open orders / fills 可见性修复
- baseline 与 recovery 的 scope 读取修复
- execution obligation 本地预留模型引入
- execution outbox 持久化链路落地
- reconciliation 与 recovery 的 portfolio snapshot 事件补链
- operator UI 中文化
- strategy `flat -> hold` 与成本门控收紧
- operator 用户表自动 bootstrap 移除

## 已知限制

当前版本仍然有明确边界，不应忽略：

- 不支持真实资金自动交易
- reservation 已落地，但还不是完整总账系统
- 部分下游 projection 仍是最终一致，而不是全链原子
- 真实收益能力仍需继续观察，不应把“可以稳定运行”误解为“已证明正期望”

## 开发与测试

常用测试示例：

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit -q
.\.venv\Scripts\python.exe -m pytest tests/integration/test_operator_api.py -q
.\.venv\Scripts\python.exe -m pytest tests/integration/test_dashboard_ui.py -q
```

如果你在本地跑服务后要手动排查：

- 看 `system/health` 是否 `healthy`
- 看 `reconciliation/latest` 是否 `CLEAN`
- 看 `execution/errors` 是否有新增错误
- 看 UI 是否出现 `halted` / `review_required` / `kill_switch_active`

## 结论

当前仓库已经具备：

- 真实行情下的策略观察能力
- OKX 模拟盘受保护提交流程
- 审计、恢复、对账、operator 控制面

但它的正确使用方式仍然应该是：

- 先做本地验证
- 再做真实行情 paper
- 再做 OKX 模拟盘
- 最后再讨论是否值得为真实资金交易继续补工程和策略验证
