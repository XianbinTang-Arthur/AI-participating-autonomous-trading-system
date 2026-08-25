# 28 FS-020 浏览器安全头与 Host 失败关闭整改

> 日期：2026-08-24  
> Git 起始基线：`00b6df0f8a8d2665d6cae3e88996843767cd1f56`  
> 分支：`codex/fs-002-kill-switch-p0`  
> 工作区：Phase 3A–3H 未提交叠加变更  
> 当前裁定：**CODE & ASGI REMEDIATED / TARGET TLS-BROWSER VERIFICATION OPEN**  
> 生产决定：**REAL-MONEY PRODUCTION: NO-GO**

## 1. 结论

FS-020 的已确认代码缺口已在当前未提交工作区收口：Gateway 新增独立最外层
user middleware，在业务路由前对 Host 失败关闭，并对普通 HTML/JSON、可见
HTTPException/认证错误和 Host 400 响应统一覆盖固定浏览器安全头。HSTS 仅根据
ASGI 实际 `scheme=https` 输出，不信任客户端伪造的 `X-Forwarded-Proto`。

这个结论只关闭仓库代码和隔离 ASGI 契约。本阶段没有启动 WSL2/Docker、没有连接
TLS terminator/proxy/真实浏览器、数据库或交易所，也没有读取 `.env.*`。因此 FS-020
不标记 CLOSED，G7 不放行。

## 2. 原始 finding 与威胁边界

Phase 1/2 基线中，Gateway 未配置 TrustedHost/等价 Host allowlist，响应也缺少 CSP、
frame protection、`nosniff`、Referrer-Policy、Permissions-Policy 和 HSTS。当前未发现跨域
CORS 放开，认证和 Secure/HttpOnly cookie 也是正面补偿控制；但它们不能阻止点击劫持、
内容嗅探、错误资源来源、Host header 污染或 XSS 后的影响半径扩大。

Phase 3G 已收紧宿主绑定，但网络 loopback 与 HTTP 应用层纵深防御是两个独立边界，
不能互相替代。

## 3. 实施范围

事前设计与验收契约：
[`docs/task/fs_020_browser_security_headers_sow_2026_08_24.md`](../../docs/task/fs_020_browser_security_headers_sow_2026_08_24.md)。

代码变更：

1. `aats/api/security_headers.py`：新增安全策略常量、Host 规范化、响应头 writer 和 ASGI middleware；
2. `apps/api_gateway/main.py`：在现有路由/中间件注册完成后注册该安全中间件；
3. `tests/unit/test_fs020_browser_security_headers.py`：新增独立 ASGI 契约、Host 对抗、CSP/UI 兼容和注册测试；
4. 现行架构、部署、Operator、代码导航和审计文档同步更新。

本阶段不修改业务 JSON、路由、数据库、交易、认证令牌或 live profile；不引入新依赖。

## 4. Host 失败关闭契约

当前允许集是：

- `127.0.0.1`；
- `localhost`；
- IPv6 loopback `::1`（HTTP 标准括号表示与内部非括号表示归一）；
- Starlette 隔离测试必需的 `testserver`。

允许合法 `1..65535` 端口后缀、主机名大小写归一和 `localhost.` 尾点归一。以下输入
在业务路由前返回 UTF-8 中文 400，且不回显输入或 allowlist：

- 缺失/空 Host；
- `0.0.0.0`、`::` 等 all-interface；
- 非本机域名或伪 `localhost` 前后缀；
- userinfo、path、query、fragment、空白或非法括号；
- 空、非数字、0 或大于 65535 的端口。

不存在 `*` 或环境变量通配 override。future 远程域名必须与 TLS SAN、trusted proxy、
认证/限流和目标网络验证一并设计，不得临时放宽此列表。

## 5. 安全响应头契约

中间件不接受下游路由的弱策略，在 `http.response.start` 阶段覆盖为：

- `Content-Security-Policy`：`default-src 'none'`；script/style/connect/font/form 仅
  `'self'`；image 仅 `'self' data:`；object/base/frame ancestor 禁止；
- `X-Frame-Options: DENY`；
- `X-Content-Type-Options: nosniff`；
- `Referrer-Policy: no-referrer`；
- `Permissions-Policy`：camera、microphone、geolocation、payment、usb、serial 均禁用；
- `Cross-Origin-Opener-Policy: same-origin`；
- `Cross-Origin-Resource-Policy: same-origin`。

仅当 ASGI scope 的实际 scheme 为 HTTPS 时增加
`Strict-Transport-Security: max-age=31536000`。HTTP 响应如有下游弱 HSTS 会被移除，
避免本地 HTTP 模拟域名被错误缓存为强制 HTTPS。

## 6. 当前 UI 的 CSP 兼容性证据

静态核对 `login.html` 与 `dashboard-shell.html`：

- 无 inline `<script>`、`<style>` 或 `style=`；
- JavaScript 为同源 `/ui/*.js` ES modules；
- CSS 为同源 `/ui/app.css`；
- API `fetch()` 使用同源路径；
- 无必需的外部 font/image/script/worker/WebSocket 来源。

因此当前 CSP 不需要 `'unsafe-inline'`、`'unsafe-eval'`、nonce 或外部 origin 例外。此结论
有静态契约测试防漂移，但尚未在真实浏览器收集 CSP violation。

## 7. 验证结果

### 7.1 聚焦与相关回归

```text
pytest tests/unit/test_fs020_browser_security_headers.py
       tests/unit/test_login_static_assets.py
       tests/unit/test_api_gateway_dashboard_invalidation.py -q

44 passed, 1 warning in 1.72s
```

覆盖 HTML/JSON/HTTPException、下游弱 header 覆盖、HTTP/HTTPS HSTS、本机/IPv6/畸形/
恶意 Host、parser 边界、当前 UI 无 unsafe inline/event-handler 依赖以及 Gateway main 注册。

### 7.2 静态检查

```text
python -m ruff check aats/ --fix
All checks passed!

python -m ruff check apps/api_gateway/main.py tests/unit/test_fs020_browser_security_headers.py
All checks passed!
```

### 7.3 全量单元测试

首次全量运行在第 88 个用例的 `tmp_path` fixture 阶段被 Windows 用户临时目录权限拒绝：

```text
87 passed, 1 error
PermissionError: [WinError 5] ... AppData\Local\Temp\pytest-of-...
```

首次尝试切换 `--basetemp` 时，仓库内已忽略的父目录尚未建立，因此再次在同一 fixture
以 `FileNotFoundError` 停止：

```text
87 passed, 1 error
FileNotFoundError: [WinError 3] ... .pytest_tmp\fs020_full_20260824_a
```

建立已由 `.gitignore` 排除的 `.pytest_tmp/` 后，使用全新独立 basetemp 首次完整重跑为
4,248 passed。后续 Host parser 再收紧后，又使用不同的全新 basetemp 重跑最终源码：

```text
4252 passed, 30 skipped, 1666 warnings, 85 subtests passed in 95.32s
```

30 个 skip 与 1,666 个 warning 为已有套件基线，不能被本次绿色结果解读为已解决；
其中包含 SQLite datetime adapter deprecation、`long_short_poller` AsyncMock 未 await 和 pytest cache 权限
warning。

## 8. 实施中发现并修复的缺陷

初版 middleware 直接用 HTTP Host parser 规范化内部 allowlist。由于 parser 当时只接受
HTTP 标准括号形式 `[::1]`，内部精确值 `::1` 被误判为非法，导致 middleware stack
构建失败。首轮 focused 结果为：

```text
21 failed, 18 passed, 2 warnings in 3.49s
ValueError: invalid_gateway_allowed_hosts
```

修复将内部 `::1` 与 HTTP `[::1]`/`[::1]:port` 归一到同一精确 loopback 值，并增加
parser 契约用例。该轮修正后 focused 为 40 passed。

文档回填阶段再次对照“畸形 Host 失败关闭”契约时，又发现 parser 会将
`localhost..` 多重尾点规范化为受信值，也会允许 `[localhost]`/
`[127.0.0.1]` 等用 IPv6 方括号包裹的非 IPv6 主机。这些输入不会扩展到外部域名，
但与畸形失败关闭契约不一致。最终实现只允许方括号内的可解析 IPv6 字面量并拒绝
多重尾点，同时补充扩展 IPv6 loopback 正向用例。最终 focused 为 44 passed，全量为
4,252 passed。

## 9. 未验证与残余风险

1. 未在真实 TLS terminator 或 reverse proxy 验证 header 是否被删除、重复、降级或错误合并；
2. 未在目标浏览器验证登录、UI 导航、API、静态资源和 CSP violation；
3. Starlette 最外层 `ServerErrorMiddleware` 生成的未捕获 500 响应可能在 user middleware 之外，
   本阶段没有用真实运行栈证明该特殊响应带头；
4. 当前 Host allowlist 故意不支持远程域名；它不是远程运维解决方案；
5. HSTS 单元契约不证明目标证书 SAN/信任、HTTP→HTTPS 强制、域名和缓存策略正确；
6. Phase 3I 已将 FS-019 同步登录链移入有界 worker 并补每进程限流，但分布式限流、trusted proxy 与 auth 负载仍未验证；
7. FS-005 目标防火墙、LAN/VPN/NAT 与外部不可达性仍 UNKNOWN。

## 10. 目标验证门禁

在不连接真实交易所写路径的隔离目标入口中，至少需要：

1. 从实际 HTTPS URL 检查每个安全头只有预期有效值；
2. 验证 HTTP/证书/SAN/信任/HSTS 策略与域名一致；
3. 用真实浏览器完成登录、页面导航、读取 API 与受控 mutation，收集 CSP violation；
4. 证明未信任/畸形 Host 返回 400，不回显输入，且 proxy 不改写为信任 Host；
5. 对未捕获 500 的最终响应头做故障注入，如缺失则在 proxy 层或框架最外层补齐；
6. 保存脱敏响应头、浏览器 console/CSP 和目标网络证据，由独立 reviewer 复核。

任一项 UNKNOWN 都不能把 FS-020 改为 CLOSED。

## 11. 回滚与兼容边界

未文档化的 iframe、跨源资源、inline script/style 或非本机 Host 客户端会被拒绝，这是预期
安全收紧。如发现合法需求，不得直接删除 CSP 或 Host gate；必须新建受控设计，
用最小定向 allowlist、nonce/hash 或 proxy trust 契约解决。

## 12. 最终裁定

```text
FS-020: CODE & ASGI REMEDIATED / TARGET TLS-BROWSER VERIFICATION OPEN
G7: PARTIAL / NOT RELEASED
REAL-MONEY PRODUCTION: NO-GO
```

本记录不是部署或上线批准，不证明任何现有容器已重建，也不证明目标网络、TLS、
认证、容量、账户、对账或交易风险门已通过。
