# CLAUDE.md — AATS 项目操作手册

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](docs/project_positioning.md)。

> 文档状态：现行约束。最后核对：2026-08-25（起始 HEAD `00b6df0f8a8d2665d6cae3e88996843767cd1f56`，包含 Phase 3A–3W 整改提交候选）。运行架构、部署、安全纪律或文档治理变化时必须同步复核。

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

“四进程”只表示主交易 role 数量。`derivatives-live` Compose 拓扑共有 7 个应用容器；Phase 3F 已把两个 collector 加入该 profile 的 future required list，但标准部署入口当前硬禁用所有 live profile，不能据此推断它们已运行或健康。

## 关键路径和文件

### 部署

- **部署脚本**: `scripts/deploy.sh` — profile 必填；8 个编号阶段（提交→同步→先构建→停止→清理→基础设施→显式 schema migration/校验→应用）之后执行健康检查、写入模拟证据包并输出非生产报告
- **WSL2 同步**: `scripts/sync_to_wsl2.sh` — 必须用这个同步代码，**绝对不要用 rsync**
- **Compose 文件目录**: `deploy/wsl2-dev/`
- **主 Dockerfile**: `deploy/wsl2-dev/Dockerfile`

### 配置文件

- **衍生品实盘环境变量**: `.env.derivatives.live`（位于项目根目录，数据库连接在第 19 行）
- **WSL2 环境变量**: `.env.wsl2`（Postgres 密码等基础设施凭证）
- **配置模板**: `configs/templates/`（仅供参考，不是运行时配置）
- **运行时参数**: managed profile 代码基线 + `configs/strategy_profiles/<profile>.yaml` + 允许的 `.env.*` override；RDP active parameters 最后从 Postgres `governance.active_parameter_sets` 注入
- **Managed 配置真实性**: Phase 3P 后 strategy YAML 必须是 mapping，runtime defaults 与 YAML 的每个 key 都必须属于 `AATSSettings.model_fields`，未知 key 在 runtime 构建前失败；`strategy_profile_auto_rollback_enabled` 从未有消费者且已删除，不能写成已实现能力
- **Profile recommendation 边界**: `approve/release` 只推进研究治理状态；Phase 3M 后 `profile-recommendations/{id}/apply` 与 `/rollback` 均在授权检查后无写入 `501`。在 execution-owned generation/worker readback 完成前，不得把它们用于运行参数变更
- **回测成交证据边界**: Phase 3N 后只接受带 `next_bar_event_v2` 与 `ohlcv_participation_cap_v2` 的新回测作为当前 bar-proxy 证据；三类订单均受 volume/cap 约束，artifact 必须声明无 L2 depth、spread/queue、impact/latency 校准，不能外推 live 容量或收益
- **Dashboard 无障碍边界**: Phase 3O 后详情抽屉必须保持原生 modal dialog、初始/返回焦点、Escape/backdrop 统一关闭和 reduced-motion CSS/JS 契约；静态/单元通过不能替代目标浏览器、键盘、NVDA/VoiceOver 与 axe 实测

### 数据库

- **Postgres 容器名**: `aats-postgres`
- **Postgres 用户**: `admin`
- **基础设施默认数据库**: `aats`；实际交易 profile 使用各自的独立数据库
- **衍生品实盘数据库**: `aats_live_derivatives`
- **RDP 数据库**: `RDP_DATABASE_URL`；容器未显式设置时可复用 `AATS_ACTIVE_PARAMETER_DB_URL`。绝不读取或展示凭证文件内容
- **数据库命名规律**: `aats_live_derivatives`（不是 `aats_derivatives_live`）
- **连接预算**: `aats/storage/connection_budget.py` 是 SQLAlchemy pool ceiling 的单一真源；四进程声明拓扑上限 150，Compose 普通连接容量 197、名义余量 47。该算术不是负载或运行状态证明；目标压测、瞬时 engine、迁移/恢复/admin 和联合内存预算仍开放。

### API

- **Gateway 端口**: 当前本地衍生品模拟模板为 8001；future 衍生品实盘模板 8011 仍保留但启动禁用，查看 `AATS_API_PORT`
- **Gateway 宿主绑定**: WSL2 Compose 固定 `127.0.0.1`；本地 `start_api.py` 只接受模拟 profile 和 loopback host
- **Gateway 浏览器边界**: 常规、HTTPException/认证失败与 Host 400 响应由最外层 user middleware 覆盖固定 CSP/frame/nosniff/referrer/permissions/COOP/CORP 安全头；Host 仅允许 `127.0.0.1`/`localhost`/`::1`/`testserver`；HSTS 仅在实际 HTTPS scope 下输出；框架最外层未捕获 500 仍须故障注入
- **认证方式**: Session-based（需先登录获取 cookie）
- **登录资源边界**: 同步 DB/PBKDF2/审计完整移入有界 worker；每 Gateway 进程默认并发 4、排队 1 秒，60 秒窗口默认 global/client/identity 为 60/20/10；client 只取 ASGI socket，不信任 `X-Forwarded-For`。这是每进程代码保护，分布式限流和目标负载仍 OPEN
- **健康检查**: `GET /healthz`；关键 task 结束或纳管固定周期 task 成功进度超时会 503，但事件驱动 task/跨进程交易 readiness 仍须另验
- **系统恢复**: `POST /system/resume`

## 常用部署命令

```bash
# 标准本地模拟部署（已提交代码后）
bash scripts/deploy.sh --profile derivatives --skip-commit

# 带自动提交的部署
bash scripts/deploy.sh --profile derivatives --commit "修复描述"

# 无缓存重建
bash scripts/deploy.sh --profile derivatives --no-cache --skip-commit

# 当前真实资金 NO-GO：所有 live profile 在同步/构建/停服/迁移前非零退出，--yes 不能绕过
```

标准模拟 deploy 会在 sync 后自动生成非秘密 runtime readiness generation，注入四主进程并记入模拟证据包。不要把该值写进 `.env.*` 或手工复用到新部署；直接 Compose 缺代次时失败是预期安全语义。代码已收紧 peer barrier，但真 Redis/NATS/Compose 启动重启矩阵仍未验证。

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
