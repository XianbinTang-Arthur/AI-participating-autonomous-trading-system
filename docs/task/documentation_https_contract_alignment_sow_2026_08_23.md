# README 本地 HTTP / Live HTTPS 契约对齐 SOW

> 文档状态：历史任务记录  
> 日期：2026-08-23  
> 代码基线：`be9179ead5be6aba22fbe94e3baf72b9f46eedc3`

## 1. 业务目标与边界

修复单元测试对 README 的过时约束，使文档准确区分 `derivatives` 本地 Uvicorn HTTP 入口与标准 `derivatives_live` 部署的 HTTPS 入口。范围只包含 README 文案和文档契约测试，不改变 API、TLS、认证、profile 或部署行为。

## 2. 模块职责与领域模型

- `scripts/start_api.py`：本地单进程 HTTP 启动器，行为不变；
- `scripts/deploy.sh` 与 WSL2 Compose：标准 live TLS 部署真源，行为不变；
- `README.md`：呈现两类入口及禁止混用边界；
- `tests/unit/test_login_static_assets.py`：验证文档不再把本地入口错误写为 HTTPS 8011。

本变更不新增领域模型。

## 3. 输入/输出接口

无运行时接口变化。文档输出应包含本地 `http://127.0.0.1:8001` 与模板 live `https://127.0.0.1:8011`，并明确 live 不得使用裸 HTTP 本地启动器。

## 4. 数据库 Schema / 表 / 索引 / 约束

不涉及数据库、migration、表、索引或约束。

## 5. 事务、一致性与并发

不涉及运行时事务、并发或一致性逻辑。文档一致性由单元测试约束。

## 6. 授权、认证与数据安全

不改变认证实现。文档继续强调 live 浏览器会话使用 HTTPS，不能把本地 HTTP 当作 live 安全入口；不读取或展示任何凭证。

## 7. 错误处理与幂等

不改变错误处理和幂等行为。若端口/profile 变化，测试应失败并要求重新核对文档，而不是静默接受漂移。

## 8. 状态转换与生命周期

不改变状态机。该 SOW 完成后作为历史任务证据保留，当前事实继续由 README、DEPLOYMENT 和代码真源维护。

## 9. 缓存与性能

不涉及缓存、性能或容量。

## 10. 日志、监控与审计

不改变日志和监控。Git diff、测试结果和本 SOW 构成变更审计证据。

## 11. 测试策略

1. 运行 `tests/unit/test_login_static_assets.py`；
2. 运行完整 `tests/unit/ -x -q`；
3. 运行 Ruff 与 Markdown 链接检查。

## 12. 迁移、回滚与兼容性

无需数据迁移。回滚只需同时恢复 README 和测试断言。公共 API 与现有运行方式保持兼容。

## 13. 配置与环境隔离

不修改 `.env.*`、managed profile 或配置模板。验证只做静态和单元测试，不连接 live 环境。

## 14. 代码组织与依赖

只修改根 README 和现有单元测试，不新增依赖、不重构运行代码。

## 15. 文档与操作手册

README 说明本地 HTTP 与 live HTTPS；详细部署仍以 `DEPLOYMENT.md` 为唯一入口，本地分层测试以 `docs/testing/README.md` 为准。

## 16. 部署与验收标准

本变更不部署。验收条件：目标测试和全量单测通过、链接检查无断链、README 同时包含正确的本地与 live URL 边界。
