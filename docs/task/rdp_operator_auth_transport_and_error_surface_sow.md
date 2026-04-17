# RDP / Operator 会话传输与错误呈现修复

## Business objectives and boundaries
- 阻止 live profile 在 HTTP 入口下出现“登录成功但会话未建立”的假成功路径。
- 让 RDP/AI Config 前端明确显示认证或传输层错误，不再伪装成“数据暂未就绪”。
- 保持现有 session / cookie 安全策略，不降低 live profile 的 secure cookie 要求。

## Module responsibilities and domain model
- `aats/api/auth_routes.py`：输出会话传输诊断字段，并在不兼容传输下拒绝登录。
- `aats/api/static/login.js`：根据 transport 兼容性决定是否允许提交登录。
- `aats/api/static/modules/views/ai-config-view.js`：把 RDP panel 的认证错误提升成一等告警。
- `aats/api/static/modules/terms.js`：统一认证/传输错误的中文文案。

## Input/output interfaces
- `GET /auth/session`
  - 新增：`request_scheme`、`secure_cookie_required`、`transport_compatible`、`required_transport`、`auth_blocked_reason`
- `GET /auth/providers`
  - 新增同样的 transport 诊断字段
- `POST /auth/login`
  - 当 secure cookie 需要 HTTPS 且当前请求不是 HTTPS 时，返回 `operator_https_required_for_secure_session`

## Database schema / tables / indexes / constraints
- 无数据库 schema 变更。

## Transactions, Consistency, Concurrency
- 不涉及事务模型变化。

## Authorization, Authentication, Data Security
- live profile 继续要求 `operator_session_cookie_secure=True`
- 不引入 insecure fallback
- 认证失败时优先返回明确错误，而不是签发无效会话

## Error Handling and Idempotency
- 登录在不兼容传输下 fail-fast
- 前端对 `operator_auth_required` / `operator_https_required_for_secure_session` 显式展示

## State Transition and Lifecycle
- `未登录 -> 登录尝试`
  - 若 transport 不兼容：停留在未登录
  - 若 transport 兼容且密码正确：进入已登录

## Caching and Performance
- 仅增加轻量字段与错误展示，无显著性能影响。

## Logging, Monitoring, Auditing
- 保持既有 operator 登录成功/失败审计。
- 不新增敏感日志。

## Testing Strategy
- integration:
  - `tests/integration/test_operator_api.py`
  - `tests/integration/test_dashboard_ui.py`
- unit:
  - `tests/unit/test_login_static_assets.py`

## Migration, Rollback, Compatibility
- 向后兼容现有 public API；仅新增返回字段和新增错误码。
- 如需回滚，可撤销新增 transport 诊断和前端错误展示。

## Configuration and Environment Isolation
- 不修改 live profile 配置。
- 本地/测试环境仍可通过显式 `operator_session_cookie_secure=False` 进行 HTTP 调试。

## Code Organization and Dependencies
- 最小修改现有认证与 AI Config 视图链路。
- 不引入新模块。

## Documentation and Operations Manual
- 运维文档应补充：live profile 的 operator UI 需要 HTTPS 才能建立浏览器会话。

## Deployment and Acceptance Criteria
- lint 通过
- unit tests 通过
- 最窄 integration tests 通过
- HTTP + secure cookie 场景下，登录必须明确失败
- RDP 前端在认证失败时必须显示真实错误，而不是“数据暂未就绪”
