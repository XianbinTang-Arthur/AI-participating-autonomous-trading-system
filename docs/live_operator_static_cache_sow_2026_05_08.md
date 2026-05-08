# 操作台静态资源缓存修复 SOW - 2026-05-08

## Business objectives and boundaries

目标是消除实盘操作台在发布后短时间继续执行旧 JavaScript / ES module 的风险，避免用户看到已经修复过的旧错误态。边界限定在 FastAPI UI 静态资源响应头与对应测试，不改变交易决策、执行、风控、数据库读写或认证语义。

## Module responsibilities and domain model

`aats.api.ui` 负责服务操作台 HTML 壳、登录页、CSS、入口 JS 和原生 ES module。HTML 页面保持 `no-store`。CSS/JS/module 属于 operator console deploy-sensitive assets，允许浏览器缓存副本但每次重新加载页面时必须向服务端重新校验。

## Input/output interfaces

输入是浏览器对 `/ui/app.css`、`/ui/app.js`、`/ui/login.js`、`/ui/modules/{path}`、`/favicon.ico` 的 GET 请求。输出仍是原文件内容和原 media type；唯一行为变化是 `Cache-Control` 从短期强缓存改为重新校验缓存。

## Database schema / tables / indexes / constraints

不涉及数据库 schema、表、索引或约束。

## Transactions, Consistency, Concurrency

不涉及事务。并发请求仍由 FastAPI/Starlette `FileResponse` 处理。缓存一致性目标是发布后下一次页面加载必须重新校验静态资源，避免旧模块与新页面壳混用。

## Authorization, Authentication, Data Security

认证边界不变。受保护页面仍复用 `_dashboard_allowed`。静态资源不新增敏感信息输出；不读取或打印任何环境变量、密钥或 token。

## Error Handling and Idempotency

不存在写操作，GET 仍幂等。无效 module path 继续返回 `ui_module_not_found`。响应头变化不改变 404/303/200 语义。

## State Transition and Lifecycle

发布生命周期中，页面壳已经是 `no-store`；本修复让入口 JS 和 module 在页面重新加载时跟随发布版本校验，降低“后端已修但前端仍旧”的观察偏差。

## Caching and Performance

把 operator 静态资源从 `public, max-age=120` 改成 `no-cache, max-age=0, must-revalidate`。这不是 `no-store`：浏览器仍可保存副本，但必须重验证。Starlette `FileResponse` 会继续提供 `etag` / `last-modified`，未变化资源可走 304，性能影响限定在页面加载和刷新页面时的轻量校验。

## Logging, Monitoring, Auditing

不新增日志。上线后通过 HTTP header、`/healthz`、容器健康、gateway 日志和数据库长查询状态做验收。

## Testing Strategy

补充集成测试断言 dashboard HTML/login HTML 保持 `no-store`，CSS/JS/module 返回重新校验缓存头，并继续断言现有资源可服务。

## Migration, Rollback, Compatibility

无需迁移。回滚只需恢复 `STATIC_ASSET_HEADERS` 原值。对浏览器兼容性高，`Cache-Control: no-cache, max-age=0, must-revalidate` 是标准 HTTP 缓存语义。

## Configuration and Environment Isolation

不新增配置项。Windows 开发环境与 WSL2 实盘部署共享同一 Python 代码路径，部署仍使用标准脚本。

## Code Organization and Dependencies

不新增依赖。变更集中在 `aats/api/ui.py` 与已有 dashboard UI 集成测试。

## Documentation and Operations Manual

本 SOW 作为变更说明。操作侧无需改变使用方式；发布后普通刷新即可拉到重新校验后的静态资源。

## Deployment and Acceptance Criteria

验收标准：

1. `/ui/app.js`、`/ui/app.css`、`/ui/login.js`、`/ui/modules/*.js` 返回 `Cache-Control: no-cache, max-age=0, must-revalidate`。
2. `/`、`/ui`、`/login` 页面继续返回 `Cache-Control: no-store`。
3. 相关测试、ruff、单元测试和受影响集成测试通过。
4. 部署后 `/healthz` 正常，容器健康，gateway 近期无新增错误。
