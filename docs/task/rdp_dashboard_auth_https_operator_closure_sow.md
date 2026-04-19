## 背景

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。


RDP 与其它受保护 dashboard panel 当前存在一类系统性问题：

1. 登录接口可能返回成功，但 secure session 在 HTTP 入口下实际无法建立。
2. `/dashboard/bundle` 只返回 panel 级结果，前端缺少统一 auth 摘要，只能靠各 view 自己猜测错误。
3. 多数 view 继续用 fallback/空对象渲染，造成“看起来正常”或“数据暂未就绪”的误导。
4. `derivatives-live` 等 live profile 没有正式 HTTPS operator 入口，secure cookie 策略与访问方式天然冲突。

## 目标

1. `/dashboard/bundle` 输出顶层 auth 摘要，供前端统一判断。
2. 所有受保护 dashboard view 在 auth failure 时统一显示真实错误，不再静默 fallback。
3. live profile 在部署时自动具备本地 HTTPS operator 入口。
4. deploy preflight 明确校验 live profile 的 TLS 入口前提。
5. 补浏览器级 E2E：
   - HTTP + secure cookie：明确失败
   - HTTPS：可建立会话并读取 RDP panel

## 范围与边界

- 保持现有 panel 数据结构与路由基本兼容。
- 不放松 live profile 的 secure cookie 策略。
- 不手动调用 `docker compose`，部署仍走 `scripts/deploy.sh`。
- 不改业务决策逻辑，只修 auth 展示、传输入口与部署链。

## 设计要点

### 后端

- `dashboard_bundle` 增加顶层 `auth`：
  - `access_state`
  - `auth_enabled`
  - `authenticated`
  - `request_scheme`
  - `secure_cookie_required`
  - `transport_compatible`
  - `required_transport`
  - `auth_blocked_reason`
  - `protected_panel_keys`
  - `blocked_panel_keys`
  - `primary_error`
- panel 结果保持原样，顶层 `auth` 只做摘要，不改变 panel 数据。

### 前端

- `fetchDashboardBundle()` 改为返回：
  - `panels`
  - `auth`
  - `timing`
- `state` 新增 `bundleAuth`
- `app.js`：
  - 合并 bundle 顶层 auth 摘要
  - 统一判断当前 view 是否因 auth failure 被阻断
  - 阻断时渲染统一的 auth-blocked 页面，不再继续走各 view 的 fallback
- `shell-renderer`：
  - auth-blocked 时隐藏首页 status ribbon
  - banner 显示真实 auth failure 文案

### 部署

- live profile 自动生成本地 TLS 证书并挂载到 gateway
- `compose_entrypoint.py` 为 uvicorn 注入 TLS 参数
- `deploy.sh`：
  - live profile preflight 检查 openssl 与 TLS 资产
  - health check 自动切换为 HTTPS

## 测试

- API 集成：
  - bundle 顶层 auth 摘要（HTTP 阻断 / HTTPS 成功）
- 前端：
  - auth-blocked 页面展示真实错误
- 浏览器级 E2E：
  - HTTP 登录页显示 HTTPS 要求
  - HTTPS 登录成功后可读取 RDP panel
- 部署单测：
  - TLS runtime 目录忽略
  - deploy preflight / health check 走 HTTPS 分支

