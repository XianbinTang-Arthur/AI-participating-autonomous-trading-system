# Frontend UI Contract Fix SOW (2026-05-14)

## Business Objectives And Boundaries

修复 `docs/task/frontend_ui_full_scan_2026_05_14.md` 中确认的 UI 契约问题：前端不再展示后端必然拒绝的管理动作，动态按钮遇到未知动作时 fail-closed，RDP 副本数据状态下不允许直接观察/回滚，并统一权限入口与关键文案。边界限定在 operator dashboard 前端与相关渲染测试，不改变交易决策、下单、风控、RDP 后端业务语义或 API schema。

## Module Responsibilities And Domain Model

- `admin-view.js`：账号治理展示层，负责把后端 `protected_last_admin` 和当前 session identity 转成可点击/禁用状态。
- `action-contract.js`：前端动态 action 的 allowlist 与 fail-closed 渲染 helper。
- `risk-view.js`、`strategy-view.js`、`ai-view.js`、`shadow-drawer.js`：后端动态 client action 的展示层。
- `rdp-control-panel.js`：RDP `ui_action` allowlist 和 release history stale 操作门禁。
- `protected-auth-view.js`、`dashboard-shell.html`、`overview-view.js`：权限入口与文案统一。

## Input/Output Interfaces

输入保持为现有 dashboard bundle payload、`operatorUsers.users[*].protected_last_admin`、session payload 的 `identity/auth_source/role`、RDP `release_history_status` 和动态 `client_action/ui_action`。输出仍是 HTML 字符串；不新增后端字段，不修改 API 路径。

## Database Schema / Tables / Indexes / Constraints

无数据库 schema 变更。

## Transactions, Consistency, Concurrency

前端修复只改变按钮可用性与点击前门禁。后端仍是最终一致性和权限真源；前端禁用状态只是减少无效/危险操作，不替代后端校验。

## Authorization, Authentication, Data Security

账号管理页必须与 `require_admin_access` / operator account 后端拒绝语义一致。当前 session 用户不得看到可点击自删/自停用按钮；最后一个启用管理员不得看到可点击停用、删除或降级角色入口。

## Error Handling And Idempotency

未知动态 action 统一渲染为禁用按钮，title 显示“前端暂不支持此动作”，避免静默刷新或无效点击。已知 action 继续走现有 dispatch。

## State Transition And Lifecycle

不改变系统运行态、RDP lifecycle、账号生命周期的后端状态转换；仅在 UI 层阻止明显不可执行的前置状态。

## Caching And Performance

新增 allowlist 和轻量 helper，对渲染性能影响可忽略；不改变 dashboard bundle 缓存策略。

## Logging, Monitoring, Auditing

不新增日志。未知 action 通过禁用按钮显式暴露，不依赖 console warn。

## Testing Strategy

更新 dashboard UI 集成测试覆盖：管理员危险按钮禁用、protected auth 切换入口、动态 client action fail-closed、RDP stale 观察/回滚禁用、文案统一。继续运行前端相关单元测试和 WSL2 dashboard 集成测试。

## Migration, Rollback, Compatibility

纯前端兼容性修复；回滚即还原相关 JS/HTML/测试文件。旧后端 payload 可继续显示，未知 action 现在禁用而不是刷新。

## Configuration And Environment Isolation

无配置变更，不读取或输出 `.env`。

## Code Organization And Dependencies

新增 `aats/api/static/modules/action-contract.js`，复用现有 `actionButton`，不引入外部依赖。

## Documentation And Operations Manual

本 SOW 与全量扫描报告共同记录修复边界；不更新运行手册。

## Deployment And Acceptance Criteria

验收标准：相关测试通过；所有报告中的 P1/P2/P3 可在源码与渲染测试中确认已处理。部署不在本次用户请求范围内。
