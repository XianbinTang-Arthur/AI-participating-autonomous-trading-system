# FS-019 Operator 登录异步隔离与有界防护 SOW

> 文档状态：现行实施约束  
> 日期：2026-08-24  
> Git 起始基线：`00b6df0f8a8d2665d6cae3e88996843767cd1f56`  
> 工作区：Phase 3A–3I 未提交叠加变更  
> 目标裁定：`CODE REMEDIATED / DISTRIBUTED RATE-LIMIT & LOAD VERIFICATION OPEN`  
> 生产决定：**REAL-MONEY PRODUCTION: NO-GO**

## 1. 背景与问题定义

FS-019 已确认 `POST /auth/login` 是 `async` handler，但在 event loop 中直接执行同步
Operator repository 查询/写入、390,000 轮 PBKDF2 校验与同步审计写入。现有单账户失败
计数与锁定可抑制对一个已知账户的持续尝试，但不能防止多用户名/不存在用户名
的并发请求阻塞 Gateway event loop。

不存在用户当前不执行 KDF，与已存在用户的错误密码存在明显工作量差异；登录输入也没有
明确长度上限。这些问题主要影响 Operator 控制面可用性，在紧急停机和恢复期间可间接
扩大资金风险。

## 2. 目标与非目标

目标：

1. 同步 DB/KDF/审计登录路径完整移出 event loop；
2. 每个 Gateway 应用实例只允许有界数量的并发登录 worker，排队超时失败关闭；
3. worker 已开始后不用 coroutine cancel 伪装取消线程，capacity 直到真实 worker 完成才释放；
4. 增加每进程的 global/client/identity 滑动窗口限流，不信任 `X-Forwarded-For`；
5. 不存在/禁用/损坏 hash 路径消耗固定 dummy PBKDF2 工作，并限制登录输入长度；
6. 保留现有账户锁定、session cookie、角色、审计与失败语义。

非目标：不建立 Redis/全集群限流，不设计 trusted proxy，不替换密码算法，不实施 MFA/
WebAuthn/CAPTCHA，不改用户管理端点，不运行 live、真实数据库或目标负载测试。

## 3. 用户与运行场景

- 正常 Operator 登录：通过每进程限流与专用 capacity，在 worker thread 完成 DB/KDF/审计；
- 错误密码：消耗与正常 hash 同级的 KDF，继续累计现有账户锁定；
- 不存在/禁用用户：返回通用失败，但也执行 dummy KDF，不写用户状态；
- 高并发/慢 DB：最多固定数量 worker 运行；后续请求在短排队超时后返回通用 503；
- 频繁 client/identity：返回通用 429 和 `Retry-After`，不暴露命中的限流维度；
- future proxy：在 trusted proxy 尚未设计前，只使用 ASGI socket client，忽略客户端可伪造的 forwarding header。

## 4. 当前路径与真源

```text
auth_login (async)
  -> transport/session guard
  -> authenticate_operator_user (sync)
       -> operator_repo.get_by_username (sync DB)
       -> verify_password (390k PBKDF2, CPU)
       -> record_login_failure / record_login (sync DB)
  -> operator_repo.get_by_username (sync DB)
  -> OperatorQueryService.record_operator_login[_failure] (sync persistence)
  -> issue token + set cookie
```

真源：`aats/api/auth_routes.py`、`aats/api/auth.py`、`aats/services/operator/passwords.py`、
`aats/storage/operator_repo*.py`、`aats/bootstrap/settings.py`。

## 5. 异步隔离与 capacity 契约

在 `auth_routes.py` 建立每 FastAPI app/每 event loop 的专用 `asyncio.Semaphore`。默认最大并发
4，等待 capacity 默认最多 1 秒。排队超时时不创建新 thread task，返回
`503 operator_login_capacity_exhausted`。

获得 capacity 后，完整同步登录尝试通过 `asyncio.to_thread` 运行。请求取消时用
`asyncio.shield` 保护已开始 worker；semaphore 由 worker task done callback 释放，而不是由请求
coroutine `finally` 提前释放。这避免“响应已超时/取消，底层线程仍运行，但新请求又获得
capacity”导致无界线程。

本阶段不对已开始的 DB/KDF 强制 execution timeout；Python 不能安全取消已运行线程，伪超时会
造成容量提前释放或审计/session 语义歧义。慢/永久挂起由有界 capacity 隔离，目标 DB timeout
属于后续运行设计。

## 6. 每进程多维限流契约

默认滑动窗口 60 秒：

- 每 Gateway 进程 global：60 次；
- 每 ASGI socket client：20 次；
- 每规范化 identity（仅限流 key 使用 `strip().casefold()`）：10 次。

限流检查在创建 worker 之前且无 `await` 地完成，同一 event loop 内原子。限流状态只保留
当前窗口，过期 key 每次清理；global limit 同时给 key 基数提供有界上限。所有维度使用
同一对外错误 `429 operator_login_rate_limited`，不记录原始 IP/用户名或密码。

此限流不跨 worker/进程，不能标记为 production distributed rate limit。多 worker 整体上限约为单进程
上限乘 worker 数，上线前仍需 Redis/proxy 级受信限流或等价控制。

## 7. 密码校验与输入契约

- 当前 hash 保持 `pbkdf2_sha256`/390,000 iterations，不更换算法或存储格式；
- 不存在或禁用用户用固定非秘密 salt/digest 执行 dummy PBKDF2，结果永不作认证依据；
- 损坏/不支持 hash 也先执行 dummy PBKDF2 再失败，不走快速枚举路径；
- 编码 hash 的 iterations 仅允许 `1..1_000_000`，超出范围时不按攻击者/损坏数据指定的无界工作量运行；
- 登录 username 限 `1..128` 字符，password 限 `1..1024` 字符；不在日志或错误中回显。

## 8. API 与数据契约

保留：正常成功 200 + session cookie；错误凭据 401；已锁账户 429；transport/session 配置失败
语义不变。

新增：

- 输入越界返回固定 `422 operator_login_payload_invalid`，不进入 limiter/KDF；
- 每进程限流返回 429 + `Retry-After`；
- 未获得登录 worker capacity 返回 503 + `Retry-After`。

不修改 OperatorUser 数据模型、数据库 schema、session token 结构或 cookie 属性。

## 9. 控制流与错误语义

```text
transport/session guards
  -> per-process global/client/identity limiter
     -> rejected: generic 429 + Retry-After
  -> bounded semaphore acquire
     -> queue timeout: generic 503 + Retry-After; no worker created
  -> shielded to_thread full synchronous login attempt
     -> missing/disabled/corrupt hash: dummy KDF + generic failure
     -> wrong password: existing failure counter/lockout + audit
     -> success: record login + audit + return session_version
  -> event-loop token issue + Set-Cookie
```

worker 异常保持异常类型向现有 FastAPI/数据库 handler 传播，done callback 只记录非敏感异常
类型并释放 capacity，不吞异常。

## 10. 性能与容量

默认每进程最多 4 个登录 worker，防止登录直接消耗 AnyIO/asyncio 默认线程池的全部
容量。event loop 仅执行小型 limiter/semaphore/token/cookie 操作。滑动窗口 map 的活跃 key 数由 global
limit 和窗口清理间接限定。

默认值只是代码安全起点，不是容量验收结果。目标 p95/p99、event-loop lag、DB pool wait、
KDF CPU 和紧急 Operator 可用性必须在无真实资金写路径的生产等价环境压测。

## 11. 日志、监控与审计

保留现有登录成功/失败 Operator audit event。新增 limiter/capacity/worker failure 结构化事件，只包含
dimension、limit/window/timeout 和 exception type，不包含 IP、username、password、hash、cookie 或 token。

本阶段不新增 Prometheus metric；这是残余 observability 项，不得因日志存在而宣称已告警。

## 12. 测试策略

1. 非 event-loop thread 执行 DB/KDF/审计，且 worker 阻塞时 event loop 仍可调度；
2. concurrency=1 时第二个请求排队超时 503，不创建第二 worker；
3. 取消等待者后 worker 继续持有 capacity，直到真正完成；
4. global/client/identity 各边界、窗口过期、key 清理和通用 429；
5. `X-Forwarded-For` 不影响 client key；
6. 不存在/禁用/损坏 hash 执行 dummy PBKDF2 且仍失败；超大 iterations 不执行攻击者指定工作量；
7. username/password 边界和现有 lockout/success reset/session 语义回归；
8. Ruff、相关 auth/Gateway 回归、全量 unit 和文档链接检查。

不运行 WSL2/Docker/数据库/浏览器/目标负载，分布式 limiter 和真实 p95/p99 保持未验证。

## 13. 迁移、回滚与兼容

新设置有代码默认值，旧配置可加载。输入长度限制会拒绝以前可进入 handler 的空/超长凭据，
这是预期安全收紧。限流/capacity 可能在攻击或配置过紧时使合法 Operator 暂时得到 429/503；
回滚不得恢复 event-loop 同步 KDF。应调整受控上限或实施受信分布式 limiter。

不涉及 DB migration、密码 hash 转换或 session invalidation。

## 14. 配置与环境隔离

新增 `AATS_` 配置：

- `OPERATOR_LOGIN_MAX_CONCURRENCY=4`；
- `OPERATOR_LOGIN_QUEUE_TIMEOUT_SECONDS=1.0`；
- `OPERATOR_LOGIN_RATE_LIMIT_WINDOW_SECONDS=60.0`；
- `OPERATOR_LOGIN_RATE_LIMIT_GLOBAL_ATTEMPTS=60`；
- `OPERATOR_LOGIN_RATE_LIMIT_CLIENT_ATTEMPTS=20`；
- `OPERATOR_LOGIN_RATE_LIMIT_IDENTITY_ATTEMPTS=10`。

整数限额和 concurrency 必须为正，window/timeout 必须大于 0；client/identity 上限不得超过 global。
不允许用 0/负值静默关闭保护。

## 15. 代码组织与依赖

修改 `aats/api/auth_routes.py`、`aats/api/auth.py`、`aats/services/operator/passwords.py`、
`aats/bootstrap/settings.py`、`configs/base.yaml` 和相关测试/现行文档。

仅使用 Python 标准库、FastAPI/Pydantic 现有依赖，不新增包。限流状态是短生命进程内状态，
不写 Postgres/Redis，不改交易热路径。

## 16. 最终裁定边界

本阶段验收后目标状态：

```text
CODE REMEDIATED / DISTRIBUTED RATE-LIMIT & LOAD VERIFICATION OPEN
```

关闭 FS-019 仍需在隔离生产等价多 worker 环境验证：全局/proxy/Redis 限流无绕过且不误封紧急
Operator；并发正确/错误/不存在用户时 event-loop lag、CPU、DB pool、p95/p99 与 capacity 达到明确
阈值；慢 DB/连接耗尽/客户端取消故障注入通过；受信 proxy 的 client identity 语义明确；独立
reviewer 复核。真实资金继续 NO-GO。
