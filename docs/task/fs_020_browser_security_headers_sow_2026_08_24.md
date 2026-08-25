# FS-020 浏览器安全响应头与 Host 失败关闭 SOW

> 文档状态：现行实施约束  
> 日期：2026-08-24  
> Git 起始基线：`00b6df0f8a8d2665d6cae3e88996843767cd1f56`  
> 工作区：Phase 3A–3G 未提交叠加变更  
> 目标裁定：`CODE & ASGI REMEDIATED / TARGET TLS-BROWSER VERIFICATION OPEN`  
> 生产决定：**REAL-MONEY PRODUCTION: NO-GO**

## 1. 背景与问题定义

FS-020 已验证 Gateway 响应缺少统一 CSP、frame protection、nosniff、Referrer-Policy、Permissions-Policy 与 HSTS，也没有 Host allowlist。认证、Secure/HttpOnly cookie 和 live TLS 降低了部分风险，但不能替代浏览器纵深防御。

Phase 3G 已把当前 Gateway 网络入口限定为本机。FS-020 继续收口浏览器响应和 Host 请求头，防止点击劫持、内容嗅探、错误来源加载与 Host header 污染扩大控制面风险。

## 2. 目标与非目标

目标：

1. 所有正常 HTTP 响应、HTTPException/认证失败和 invalid Host 响应带统一安全头；
2. CSP 与当前无 inline script/style、全同源 UI 兼容；
3. 当前本机 Host 明确 allowlist，未知 Host 返回 400；
4. HSTS 只对实际 HTTPS ASGI scope 添加；
5. 用独立 ASGI 测试证明 HTML/JSON/error/Host/HTTP/HTTPS 契约。

非目标：不修复 FS-019 PBKDF2/event-loop、全局/IP rate limit、远程 proxy、PKI、证书信任或浏览器无障碍；不启用 live；不连接任何外部系统。

## 3. 用户与运行场景

- 本地模拟 UI：Host 为 `127.0.0.1` 或 `localhost`，HTTP 可用但无 HSTS；
- IPv6 loopback：Host 为 `[::1]`，允许；
- 单元/ASGI 测试：Host 为 `testserver`，允许；
- 未知/畸形 Host：在路由处理前 400，仍带安全头；
- future HTTPS：只有 ASGI `scheme=https` 时带 HSTS；future 远程域名必须新建受控配置与验证任务。

## 4. 安全头契约

固定最小策略：

- `Content-Security-Policy`: default none；script/style/connect/font/form 仅 self；image 允许 self/data；object/base/frame ancestor 禁止；
- `X-Frame-Options: DENY`；
- `X-Content-Type-Options: nosniff`；
- `Referrer-Policy: no-referrer`；
- `Permissions-Policy` 禁止 camera、microphone、geolocation、payment、usb、serial；
- `Cross-Origin-Opener-Policy: same-origin`；
- `Cross-Origin-Resource-Policy: same-origin`；
- HTTPS 才有 `Strict-Transport-Security: max-age=31536000`。

## 5. Host 校验契约

当前 allowlist：`127.0.0.1`、`localhost`、`::1`、`testserver`。允许合法端口后缀和大小写/localhost 尾点规范化；拒绝空值、userinfo、path/query/fragment、无效端口、all-interface、非本机域名和伪前缀/后缀。

不提供 `*` 或环境变量通配 override。远程域名只能在独立设计中与 proxy/TLS/Host trust 同时引入。

## 6. 当前 UI 兼容性证据

已静态核对 `login.html` 与 `dashboard-shell.html`：没有 inline `<script>`、`<style>` 或 `style=`；JS 为 `/ui/*.js` ES modules，CSS 为 `/ui/app.css`，API `fetch()` 使用同源路径，没有外部 font/image/script/worker/WebSocket。

因此无需 `'unsafe-inline'`、`'unsafe-eval'`、外部 origin 或 nonce 例外。若未来新增相关资源，必须先更新 CSP 测试和安全评审。

## 7. 方案设计

新增独立 ASGI middleware 模块，在 `http.response.start` 阶段覆盖固定安全头；invalid Host 直接返回 400 并复用相同 header writer。使用 ASGI scope 的 `scheme` 判断 HSTS，不直接信任客户端 `X-Forwarded-Proto`。

在 `apps/api_gateway/main.py` 所有路由/现有 function middleware 注册完成后，把 security middleware 注册为最外层 user middleware。

## 8. API 与数据契约

不改业务 JSON body、status code、路由、数据库或事件。新增的唯一行为变化是未知 Host 返回 400，以及所有 HTTP 响应多出安全头。

invalid Host body 使用简洁 UTF-8 中文，不泄露 allowlist 或内部配置。

## 9. 控制流与错误语义

```text
HTTP request
  -> parse Host
     -> invalid/untrusted: 400 + security headers
     -> allowed: downstream middleware/router
  -> response.start
     -> overwrite fixed browser security headers
     -> scheme=https: add HSTS
     -> scheme=http: ensure no HSTS
```

ASGI non-HTTP scope 原样透传。未捕获应用异常由框架最外层 ServerErrorMiddleware 生成的特殊 500 是否带头需单独验证；不为此引入吞异常的通用 handler。

## 10. 性能与容量

每请求只解析一个短 Host 字符串并写固定 header，时间/空间为 O(1)，不访问 DB、不做密码学、不进入交易热路径。

## 11. 日志、监控与审计

不记录原始恶意 Host，避免日志注入；400 body 不回显输入。是否需要按来源统计 invalid Host 由后续 rate-limit/observability 任务决定。

文档需明确 ASGI/TestClient 通过不等于目标浏览器、TLS terminator 或 proxy 没有删除/覆盖 header。

## 12. 测试策略

新增：

1. 独立 minimal FastAPI + middleware 的 HTTP HTML/JSON 响应头测试；
2. HTTP 无 HSTS、HTTPS 有 HSTS；
3. allowed Host 带端口/IPv6；
4. 空/畸形/all-interface/非本机 Host 返回 400 且仍有头；
5. 下游自定义弱 header 被固定策略覆盖；
6. UI 无 inline script/style 与 CSP 同源契约；
7. main app 确实注册 middleware；
8. 登录/UI/路由相关回归、Ruff、全量 unit 与文档检查。

不启动浏览器或 WSL2；target TLS/browser/proxy 保持未验证。

## 13. 迁移、回滚与兼容

依赖任意 Host header、iframe 嵌入、跨源资源或 inline script/style 的未文档化客户端会失败，这是预期安全收紧。当前 UI 静态核对显示不依赖这些能力。

不得通过删除 CSP/Host gate 回滚来恢复未授权远程访问；应新建设计并提供最小定向 allowlist/nonce/proxy 方案。

## 14. 配置与环境隔离

本阶段不新增配置键。当前 local-only 网络模型使固定 Host allowlist 与 Phase 3G 一致；future remote host 配置必须与 TLS SAN、Trusted proxy 和 target network 验证一起设计，不能单独加域名。

HSTS 不由 profile 名称决定，只由实际 request scheme 决定，防止 HTTP 模拟环境被错误缓存为强制 HTTPS。

## 15. 代码组织与依赖

预计新增 `aats/api/security_headers.py`，修改 `apps/api_gateway/main.py`，新增 FS-020 单元测试并更新现行架构/部署/代码审查/Operations/审计文档。

仅使用 Python/Starlette 现有依赖，不新增包。

## 16. 最终裁定边界

本阶段验收后目标状态：

```text
CODE & ASGI REMEDIATED / TARGET TLS-BROWSER VERIFICATION OPEN
```

关闭 FS-020 仍需在隔离目标入口通过真实 HTTPS/browser/proxy 验证：header 未被删除/重复降级、CSP 无 violation、登录/UI/API 正常、invalid Host 被拒绝、HTTP/HSTS 行为与证书/域名一致，并由独立 reviewer 复核。真实资金继续 NO-GO。
