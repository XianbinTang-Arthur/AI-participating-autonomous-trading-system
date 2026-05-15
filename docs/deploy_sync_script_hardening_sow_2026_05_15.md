# 同步与部署脚本硬化 SOW（2026-05-15）

## Business objectives and boundaries

目标是修复同步脚本、部署脚本和运维文档中已经暴露的过时或风险点，避免 live trading 部署误带无关改动、误报实际部署版本、继续传播旧的 `/mnt/d`/`rsync`/手工 compose 流程。

边界：不改变交易逻辑、不改数据库 schema、不迁移运行时数据、不执行部署。

## Module responsibilities and domain model

- `scripts/deploy.sh`：唯一部署入口，负责提交边界检查、同步、compose 生命周期、健康检查和部署报告。
- `scripts/sync_to_wsl2.sh`：Windows committed HEAD 到 WSL2 native checkout 的 Git 同步入口。
- `deploy/wsl2-dev/*.md` 与 `docs/operations/wsl2_sync_workflow.md`：只记录当前可执行的操作路径，不保留会误导 live 发布的旧流程。
- `tests/unit/test_deploy_scripts.py`：锁定部署/同步脚本关键安全契约。

## Input/output interfaces

输入仍为既有 CLI 参数：`deploy.sh --profile/--commit/--skip-sync/--skip-commit/--timeout` 与 `sync_to_wsl2.sh init/pull/check/status/path/shell`。

输出增强为更明确的错误信息和部署报告：部署报告区分 Windows HEAD 与 WSL deployed HEAD；`--commit` 不再自动 `git add -A`。

## Database schema / tables / indexes / constraints

无 schema 变更。部署脚本仍会同步 Postgres 用户密码，但改成 `psql` 变量引用，降低特殊字符破坏 SQL 的风险。

## Transactions, consistency, concurrency

同步仍使用 Git fetch + ff-only merge。WSL checkout 必须 clean；branch drift 自动修复失败时立即失败，不吞错。

## Authorization, authentication, data security

不读取或打印 `.env.wsl2` / `.env.*.live` 内容。密码只在部署脚本内部流转，不写日志。

## Error handling and idempotency

`deploy.sh --commit` 只提交已经由操作者精确暂存的文件；存在未暂存或未跟踪文件时失败并提示 `git status --short`。

## State transition and lifecycle

不改变容器生命周期顺序。部署仍按 stop/build/prune/infra/app/health/report 顺序运行。

## Caching and performance

无缓存行为变更。

## Logging, monitoring, auditing

部署报告新增 WSL deployed HEAD，便于审计实际发布版本。Windows 与 WSL HEAD 不一致时显式警告。

## Testing strategy

运行 bash 语法检查、deploy 脚本单测、startup prewarm 相关脚本单测，以及 dry-run wrapper 验证。

## Migration, rollback, compatibility

`--commit` 行为收紧是安全性不兼容：自动 staging 被移除。回滚方式是 revert 本次脚本/文档/test commit。

## Configuration and environment isolation

继续使用 `~/aats/.env.wsl2` 作为 WSL2 env 单一真相位置，legacy `deploy/wsl2-dev/.env.wsl2` 只保留 deploy 兼容读取，不作为新文档路径。

## Code organization and dependencies

不引入新依赖。

## Documentation and operations manual

清理 RUNBOOK/README/sync workflow 中暴露的过时命令：`rsync`、直接 `/mnt/d` compose、失效 `aats.scripts.bootstrap_database`、失效 `aats.api.main`、legacy env 路径。

## Deployment and acceptance criteria

本轮不部署。验收以脚本语法、相关单测、dry-run wrapper 和 git diff 检查通过为准。
