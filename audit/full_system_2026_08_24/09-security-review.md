# 09 安全审查

> Phase 3G 更新：下文保留 Phase 1/2 修复前证据。当前工作区已把 Gateway 宿主
> mapping 固定到 `127.0.0.1`，本地 launcher 拒绝 live/非 loopback，模拟 evidence
> 会拒绝实际 HostIp 漂移。现有容器和目标网络仍未验证；详见 `27`。
>
> Phase 3H 更新：FS-020 已新增 Host 失败关闭与统一 CSP/frame/nosniff/
> referrer/permissions/COOP/CORP 安全头，HSTS 仅限实际 HTTPS scope。目标
> TLS/proxy/browser 和最外层未捕获 500 仍未验证；详见 `28`。
>
> Phase 3I 更新：FS-019 已把同步 DB/PBKDF2/账户状态/审计链移入有界 worker，
> 增加每进程三维限流、dummy KDF 和输入/hash 上界。分布式限流、trusted proxy、
> 真实数据库与目标负载仍未验证；详见 `29`。

## FS-005 — Gateway 暴露范围宽于“仅本机”声明

- 严重度：P1；置信度：高；类别：network exposure / credential protection
- 状态：原始 finding VERIFIED；Phase 3G `CODE REMEDIATED / TARGET NETWORK VERIFICATION OPEN`
- 位置：`deploy/wsl2-dev/docker-compose.aats.yml:106-123`；`scripts/deploy.sh:208-236`
- 证据：uvicorn 监听 `0.0.0.0`，端口映射为 `host:container` 而非 `127.0.0.1:host:container`。当前 Docker inspect 的 HostIp 为空，验证为所有宿主接口。当前模拟 profile 的 `/login` 通过 HTTP 200 可达，响应无强制 HTTPS。
- Phase 2 现行裁定：**P2 / DOWNGRADED**。all-interface bind 事实保留；live 路径会注入 TLS，并有 auth/Secure cookie/HTTP login guard。当前 HTTP 运行证据只属于 simulated profile，不能证明生产明文暴露；目标网络、防火墙和证书信任仍需验证。完整反证见 `17-p1-adversarial-verification.md`。
- 触发：宿主接入不可信 LAN/VPN/虚拟网卡，或防火墙规则放行端口。
- 后果：登录和控制面攻击面扩大；模拟 profile 的密码可在明文 HTTP 链路上传输。live 标准脚本会生成 TLS，但证书只包含 localhost/127.0.0.1，外部访问会有信任/名称问题。
- 建议：Compose 宿主映射显式 `127.0.0.1`；需要远程运维时使用受控 reverse proxy/VPN/mTLS，不直接扩大 uvicorn 绑定。增加静态和部署后断言。

## FS-019 — 密码校验和同步数据库调用阻塞 async 请求线程

- 严重度：P2；置信度：中高；类别：availability / auth DoS
- 状态：原始 finding VERIFIED；Phase 3I `CODE REMEDIATED / DISTRIBUTED RATE-LIMIT & LOAD VERIFICATION OPEN`
- 原始证据：async login handler 直接调用同步 repository 与 390,000 轮 PBKDF2，没有 thread offload。账户锁定不能消除多用户名/并发连接对 event loop 的阻塞。
- Phase 3I 证据：完整同步登录尝试进入默认最多 4 个 worker，排队 1 秒失败；取消不提前释放 capacity；每进程 60 秒 global/client/identity 60/20/10；缺失/禁用/损坏 hash 用 dummy KDF；输入与 hash iteration 有上界。最终 131 项扩大相关与 4,273 项全量 unit 通过。
- 已降低后果：仓库默认路径不再由单个登录 KDF 直接阻塞 event loop，也不能通过取消请求提前归还 worker capacity。
- 待验：跨 Gateway worker/重启不可绕过的集中限流；trusted proxy client identity；真实 DB/KDF 混合负载、慢连接/连接耗尽、p95/p99、event-loop lag、拒绝率、告警与紧急 Operator SLA。

## FS-020 — HTTP 安全响应头缺失

- 严重度：P2；置信度：高；类别：browser hardening
- 状态：原始 finding VERIFIED；Phase 3H `CODE & ASGI REMEDIATED / TARGET TLS-BROWSER VERIFICATION OPEN`
- 原始证据：app 未配置 TrustedHost/安全头 middleware；当时登录响应只有 no-store/content-type 等，没有 CSP、frame-ancestors/X-Frame-Options、HSTS、nosniff、Referrer-Policy、Permissions-Policy。
- Phase 3H 证据：`GatewayBrowserSecurityMiddleware` 在路由前拒绝非 allowlist/畸形 Host，在 response start 覆盖固定安全头；当前 UI 无 inline script/style/event handler，CSP 不需 `unsafe-inline`/`unsafe-eval`；44 项 focused 和 4,252 项全量 unit 通过。
- 后果：扩大 XSS、点击劫持、内容嗅探和错误代理配置的影响半径。没有发现跨域 CORS 放开，这是正面事实。
- 待验：真实 TLS terminator/proxy 未删除或降级 header；目标浏览器登录/UI/API 无 CSP violation；HTTP/HTTPS/HSTS 与证书/域名一致；未捕获 500 最终响应也有头。

## 认证与授权良好控制

- 密码 PBKDF2-SHA256 390k、随机 16-byte salt、constant-time digest compare。
- session 由 HMAC-SHA256 签名，默认 HttpOnly/Secure/SameSite=Lax，带过期和 session version。
- 账户锁定的 PostgreSQL 更新使用行锁，降低并发绕过。
- read/write/admin 分层；unsafe unauthenticated write 被限制在 local-only/memory 场景；execution ledger 模式要求认证。
- RDP apply/rollback 使用短期 action-bound token、actor match 与部分双人约束。

## 凭证与供应链边界

- 本次未读取或显示任何 `.env.*`；`.gitignore`/`.dockerignore` 排除 secrets，Compose 以 env_file 注入。
- 仍需人工确认历史 Git、镜像层、日志、trace attributes、crash dump 和备份中无凭证；本次未做秘密扫描封板。
- 依赖只有下界/少量上界，没有 lock/hash；当前 CVE 状态 UNKNOWN。详见 12。
- 自签证书私钥位于 runtime 目录并 chmod 600；证书生成和信任分发未做现场验证。
