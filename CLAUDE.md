# CLAUDE.md — AATS 项目操作手册

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](docs/project_positioning.md)。

> 文档状态：现行约束。最后核对：2026-08-23（代码基线 `be9179e`）。运行架构、部署、安全纪律或文档治理变化时必须同步复核。

> 本文件是仓库级操作手册。所有在本仓库工作的编码代理必须遵守此文档约束。

## 项目概述

AATS（AI Participating Autonomous Trading System）— 一个以 AI 为核心受益主体、真金白银运行的加密货币自动交易系统。
AATS 的首要意义是通过自动化交易持续追求长期稳定盈利，为 AI 的长期自主发展积累资本；所有架构、研究、风控、执行、恢复和运维工作都应服从这个目标。
代码当前集成的交易所是 OKX，managed profile 同时覆盖现货与衍生品；仓库的主要生产路径面向衍生品。实际运行中的交易所、账户模式和交易品种属于现场事实，必须在操作当时验证，不能由本文档推断。

## 架构

四个主交易切片，外加 RDP/采集守护进程，Docker 容器部署在 WSL2 中：

| 进程 | 容器名 | 职责 |
|------|--------|------|
| gateway | aats-gateway | API 网关 + 前端 |
| market | aats-market | 行情数据 |
| decision | aats-decision | 决策引擎 |
| execution | aats-execution | 执行引擎 + OKX 适配器 |
| rdp-daemon | aats-rdp-daemon | Research Data Platform |
| liquidations-daemon | aats-liquidations-daemon | derivatives-live liquidation 数据采集（仅该 overlay） |
| microstructure-collector | aats-microstructure-collector | derivatives-live 微观结构数据采集（仅该 overlay） |

基础设施容器：aats-postgres, aats-redis, aats-redis-exporter, aats-nats, aats-jaeger, aats-grafana, aats-prometheus, aats-loki, aats-promtail

“四进程”只表示主交易 role 数量。标准 `derivatives-live` 部署共有 7 个应用容器；`deploy.sh` 当前自动健康门只覆盖其中 gateway/market/decision/execution/rdp-daemon，两个采集器仍需单独核查。

## 关键路径和文件

### 部署

- **部署脚本**: `scripts/deploy.sh` — 7 个编号阶段（提交→同步→停止→构建→清理→基础设施→应用）之后执行健康检查和部署报告
- **WSL2 同步**: `scripts/sync_to_wsl2.sh` — 必须用这个同步代码，**绝对不要用 rsync**
- **Compose 文件目录**: `deploy/wsl2-dev/`
- **主 Dockerfile**: `deploy/wsl2-dev/Dockerfile`

### 配置文件

- **衍生品实盘环境变量**: `.env.derivatives.live`（位于项目根目录，数据库连接在第 19 行）
- **WSL2 环境变量**: `.env.wsl2`（Postgres 密码等基础设施凭证）
- **配置模板**: `configs/templates/`（仅供参考，不是运行时配置）
- **运行时参数**: managed profile 代码基线 + `configs/strategy_profiles/<profile>.yaml` + 允许的 `.env.*` override；RDP active parameters 最后从 Postgres `governance.active_parameter_sets` 注入

### 数据库

- **Postgres 容器名**: `aats-postgres`
- **Postgres 用户**: `admin`
- **基础设施默认数据库**: `aats`；实际交易 profile 使用各自的独立数据库
- **衍生品实盘数据库**: `aats_live_derivatives`
- **RDP 数据库**: `RDP_DATABASE_URL`；容器未显式设置时可复用 `AATS_ACTIVE_PARAMETER_DB_URL`。绝不读取或展示凭证文件内容
- **数据库命名规律**: `aats_live_derivatives`（不是 `aats_derivatives_live`）

### API

- **Gateway 端口**: 8011（衍生品实盘），查看 `AATS_API_PORT` 环境变量
- **认证方式**: Session-based（需先登录获取 cookie）
- **健康检查**: `GET /healthz`
- **系统恢复**: `POST /system/resume`

## 常用部署命令

```bash
# 标准部署（已提交代码后）
bash scripts/deploy.sh --skip-commit

# 带自动提交的部署
bash scripts/deploy.sh --commit "修复描述"

# 无缓存重建
bash scripts/deploy.sh --no-cache --skip-commit

# 指定 profile
bash scripts/deploy.sh --profile derivatives-live --skip-commit
```

## 开发环境

- **Windows 开发路径**: `D:\文件\project\AIParticipatingAutonomousTradingSystem\`
- **Windows Python**: `.venv\Scripts\python.exe`
- **WSL2 发行版**: Ubuntu
- **WSL2 项目路径**: `~/aats`
- **WSL2 Python venv**: `~/aats-venv`
- **集成测试**: 在 WSL2 中用 testcontainers 运行

## 运行测试

```bash
# Windows 单元测试
.venv\Scripts\python.exe -m pytest tests/unit/ -x -q

# 特定模块测试
.venv\Scripts\python.exe -m pytest tests/unit/ -k "test_name" -x -q

# WSL2 集成测试
wsl -d Ubuntu bash -c "cd ~/aats && source ~/aats-venv/bin/activate && pytest tests/integration/ -x -q"
```

## 绝对禁止

1. **绝不用 rsync 同步代码** — 会导致 WSL2 侧 git dirty state，deploy.sh 的 sync 步骤（git pull）会失败
2. **绝不读取或显示凭证文件内容** — `.env.wsl2`、`.env.*.live` 等包含密码的文件只能在进程内管道流转
3. **绝不用 `echo` 输出密码/密钥/token**
4. **绝不手动执行 `docker compose` 命令** — 使用 `scripts/deploy.sh`
5. **绝不将 `.env.*.example` 模板当作运行时配置** — 模板仅供参考
6. **绝不跳过测试声称成功** — 必须实际运行测试并报告结果
7. **重大改动前必须备份+设计+获批准** — 三步走纪律

## OrderState 持久化注意事项

OrderState 有三重持久化：
1. **Postgres 列**: `order_states.status` 和 `execution_orders.state`
2. **Postgres JSON**: `order_states.payload` 和 `execution_orders.raw_payload` 中的 `status` 字段
3. **Redis 缓存**: `aats:hot:order_state:*` keys

修改 order 状态时**三者必须同时更新**，否则系统从 JSON payload 反序列化时会读到旧状态。

## SQLAlchemy 注意事项

- 项目使用 **SQLAlchemy 2.0**
- JSON 列（非 JSONB）访问用 `.as_string()`，**不是** `.astext`（已废弃）
- JSONB 列同样统一使用 `.as_string()`；不要新增 `.astext`

## 语言和编码

- 所有前端展示文本必须使用 **UTF-8 中文**
- 与用户沟通使用 **中文**
- 代码注释和 commit message 可中英混用

## Commit 规范

- 使用语义化前缀: `feat:`, `fix:`, `refactor:`, `docs:`, `test:`, `chore:`
- Commit message 简洁描述"为什么"而非"做了什么"
- 不要 `git add -A`，按文件名精确添加
