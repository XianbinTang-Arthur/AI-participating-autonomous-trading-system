# 标准部署 WSL 环境路径展开修复 SOW

> 文档状态：实施任务与交付证据
> 最后核对：2026-08-29（起始代码基线 `018dbd5dd8cf9726e0049400773926082896b96a`）
> 核对范围：`scripts/deploy.sh` 的 WSL 命令传输、聚焦单元测试与 derivatives 模拟部署
> 运行时边界：本文不证明部署已成功；以本任务完成后的标准部署证据为准

## 业务目标与边界

修复标准 derivatives 模拟部署在基础设施启动后同步 PostgreSQL 密码时，把
`$HOME/aats/.env.wsl2` 当作字面路径而失败的问题。只恢复标准部署可用性；禁止 live profile、
真实订单、凭证输出、手工 Compose 绕过或对 NATS stream 执行 ACK、reset、recreate、purge。

## 模块职责与领域模型

`scripts/deploy.sh` 继续负责 Windows Git Bash 到 WSL2 的受锁命令传输。`WSL_PROJECT` 保持延迟到
WSL shell 展开的路径表达式；本修复仅保证 `.env.wsl2` 路径在 WSL 命令中使用双引号，从而既展开
`$HOME` 又保留路径边界。

## 输入/输出接口

- 输入：`AATS_WSL2_PROJECT` 或默认 `$HOME/aats`、已解析的 `WSL2_ENV_FILE`。
- 输出：在 WSL 内读取 `POSTGRES_USER` 与 `POSTGRES_PASSWORD`，经 stdin/psql 变量同步密码。
- 不新增 CLI 参数、环境变量、公开 API 或持久化格式。

## 数据库 Schema、表、索引与约束

无 schema、表、索引或约束变更；仍只执行现有 `ALTER USER ... PASSWORD` 操作。

## 事务、一致性与并发

操作继续位于单一标准部署 `flock` 与 readiness lease 监督范围内。失败保持非零退出；不放宽
NATS 两次 preflight、应用 quiescence 或数据库迁移顺序。

## 授权、认证与数据安全

真实环境文件只在 WSL 内读取；密码不进入 Windows 命令行、日志或证据。测试只检查静态引号契约，
不读取 `.env.*` 内容。

## 错误处理与幂等性

保留 `set -euo pipefail` 和 psql `ON_ERROR_STOP`。路径缺失、字段缺失或 psql 失败均终止部署；重复执行
密码同步保持幂等。

## 状态迁移与生命周期

部署状态仍为：构建 → 应用静止 → 第一次 NATS preflight → full-down → 基础设施 → 密码同步 →
schema → 第二次 preflight → app-up → 健康与证据。本修复不跳过任何阶段。

## 缓存与性能

无缓存、连接池或性能行为变化；仅改变两个 `grep` 路径参数的 shell 引号。

## 日志、监控与审计

成功仍只输出“基础设施就绪，密码已同步”，不输出用户名、密码或环境文件内容。最终验收使用标准
部署的 non-secret evidence packet。

## 测试策略

- 聚焦单元测试必须证明用户与密码两个读取路径均使用可展开的双引号。
- 测试必须拒绝旧的单引号字面路径。
- 运行 shell 语法检查、Ruff、聚焦单元测试，并以完整标准 derivatives 模拟部署作为最窄集成验证。

## 迁移、回滚与兼容性

无需数据迁移。回滚是恢复旧两行引号，但会重新引入默认路径失败；不建议回滚。显式绝对
`AATS_WSL2_PROJECT` 与默认 `$HOME/aats` 均保持兼容。

## 配置与环境隔离

继续只使用选定 simulation profile 的根目录 `.env.wsl2` 与 `.env.derivatives`。所有 live profile
继续在任何外部副作用前失败关闭。

## 代码组织与依赖

只修改现有部署脚本及其聚焦测试，不新增运行时依赖或替代部署入口。

## 文档与运维手册

本文件记录修复范围；现行操作仍以 `DEPLOYMENT.md`、`CLAUDE.md` 与标准部署脚本为准。

## 部署与验收标准

1. Windows 与 WSL checkout 为同一干净提交。
2. 两次 NATS preflight 均精确识别 77 个声明 consumer，无 missing/unowned。
3. schema contract 通过，七个 derivatives 应用容器均 `running healthy`。
4. 最终证据绑定本次 commit/generation，且 `production_ready=false`、`trading_ready=false`。
5. 不得据此声称数据连续、盈利能力或真实资金上线就绪。
