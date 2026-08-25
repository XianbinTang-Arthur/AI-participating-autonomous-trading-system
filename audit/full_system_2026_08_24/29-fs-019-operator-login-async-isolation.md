# 29 FS-019 Operator 登录异步隔离与有界防护整改

> 阶段：Phase 3I  
> 日期：2026-08-24  
> Git 起始基线：`00b6df0f8a8d2665d6cae3e88996843767cd1f56`  
> 工作区：`codex/fs-002-kill-switch-p0` 上 Phase 3A–3I 未提交叠加变更  
> 当前裁定：`CODE REMEDIATED / DISTRIBUTED RATE-LIMIT & LOAD VERIFICATION OPEN`  
> 生产决定：**REAL-MONEY PRODUCTION: NO-GO**

## 1. 执行摘要

FS-019 的已确认代码路径已在当前未提交工作区收口：`POST /auth/login` 不再在
FastAPI event loop 中直接运行同步 Operator repository、390,000 轮 PBKDF2、账户
失败计数/登录状态写入和 Operator 审计写入。完整同步尝试现在进入有界
`asyncio.to_thread` worker；每个 Gateway 应用实例默认最多同时运行 4 个登录 worker，
等待 capacity 超过 1 秒时在创建 worker 前返回固定 503。

同时增加每进程 global/client/identity 三维滑动窗口限流、输入上限、`SecretStr`
密码模型、不存在/禁用/损坏 hash 的 dummy KDF，以及编码 hash iteration 上界。
请求 coroutine 被取消后，已开始的 Python thread 不会被伪装成已取消：capacity 直到
真实 worker 结束才释放，避免取消风暴绕过并发上限。

本阶段只完成代码、隔离测试、配置兼容、相关回归和全量单元测试。没有执行真实
PostgreSQL、WSL2/Docker、反向代理、多 worker、生产等价并发负载或告警验证，亦未读取
任何 `.env.*`。因此不能把 FS-019 标为 CLOSED，也不能把默认限额解释为已验收容量。

## 2. 原始问题与资本风险

原始 `auth_login` 是 `async def`，但其内部按顺序直接执行：

```text
event loop
  -> operator_repo.get_by_username           # 同步 DB
  -> verify_password                         # PBKDF2-SHA256, 390k
  -> record_login[_failure]                  # 同步 DB
  -> OperatorQueryService audit append       # 同步持久化
  -> operator_repo.get_by_username           # 同步 DB
  -> issue token / Set-Cookie
```

账户级失败计数和锁定只能抑制对一个已知账户的持续尝试，不能消除多用户名、随机用户名、
多个 client 或慢数据库对 event loop 的阻塞。Operator Gateway 是 halt、恢复、对账和人工
治理入口；其可用性下降虽不是直接下单漏洞，但会延迟紧急人工处置，因而具有间接资金风险。

原路径对不存在/禁用用户不执行 KDF，与正常账户错误密码存在可观察工作量差；登录字段也
没有明确的仓库级最大长度。损坏 hash 中的 iteration 值若不受控，还可能扩大 CPU 消耗。

## 3. 实施后的控制流

```text
POST /auth/login
  -> Pydantic username 1..128 + SecretStr password
  -> 固定 password 1..1024 校验
  -> auth/session/transport guards
  -> per-process sliding-window limiter
       global 60 / 60s
       ASGI socket client 20 / 60s
       strip+casefold identity 10 / 60s
       reject -> 429 + Retry-After; 不创建 worker
  -> per-app/per-loop semaphore acquire
       max concurrency 4; queue timeout 1s
       timeout -> 503 + Retry-After; 不创建 worker
  -> shielded asyncio.to_thread
       construct OperatorQueryService
       lookup user
       verify real/dummy PBKDF2
       update failure/lockout or login state
       append success/failure audit
       read session_version
  -> worker done callback 才释放 semaphore
  -> event loop issue token + Set-Cookie
```

## 4. 代码变更与契约

| 文件 | 变更 | 安全意义 |
|---|---|---|
| `aats/api/auth_routes.py` | 登录 payload 边界、三维 limiter、每应用 semaphore、shielded thread worker、固定 429/503 | event loop 隔离；拒绝路径不生成无界 thread；取消不提前归还容量 |
| `aats/api/auth.py` | 不存在 repository、用户不存在或被禁用时执行 dummy KDF | 缩小快速用户名枚举工作量差 |
| `aats/services/operator/passwords.py` | dummy PBKDF2；hash 解析失败关闭；iteration 限 `1..1_000_000` | 损坏/异常 hash 不执行无界攻击者指定工作量 |
| `aats/bootstrap/settings.py` | 六项保护设置及失败关闭校验 | 旧配置有安全默认；0/负值或维度倒挂不能静默关闭保护 |
| `configs/base.yaml` | 默认值和每进程边界说明 | Operator 能看到当前代码默认，但不得把它当目标容量结论 |
| `tests/unit/test_fs019_operator_login_async_isolation.py` | 新的并发、取消、限流、dummy KDF、输入和 session 回归 | 固化 Phase 3I 代码契约 |

保留的外部行为：

- 成功仍返回 200 并设置既有属性的 session cookie；
- 错误凭据仍为 401；已锁账户仍为 429；
- 原有失败计数、锁定、成功清零、session version 和 Operator 登录审计继续生效；
- 不修改 OperatorUser schema、密码 hash 存储格式、token 格式或用户管理 API。

新增的失败语义：

- username/password 越界：固定 `422 operator_login_payload_invalid`；
- 每进程任一限流维度达到上限：固定 `429 operator_login_rate_limited`；
- capacity 排队超时：固定 `503 operator_login_capacity_exhausted`；
- 429/503 带 `Retry-After`，响应不暴露命中的 identity/client 值。

## 5. 并发、取消和线程边界

Python 不能安全终止已经运行的 worker thread。若请求取消时在 coroutine `finally` 立即
释放 semaphore，底层 DB/KDF 仍在执行，新请求却可继续创建 thread，最终绕过并发上限。
当前实现以 worker task 的 done callback 作为唯一正常释放点，并用 `asyncio.shield`
阻止等待者取消向已开始 worker 传播。

本阶段没有为已开始 worker 伪造 execution timeout。慢或永久挂起会占用最多 4 个容量，
其余请求在排队超时后失败关闭；数据库连接、statement 和 pool timeout 必须在目标环境
另行验证。登录使用的 Postgres repository/event store 每次方法调用都从 `sessionmaker`
创建局部 SQLAlchemy Session，没有把一个既有 Session 跨线程复用。

## 6. 限流边界

当前 limiter 是每 FastAPI app、每 event loop、每 OS 进程的内存状态：

- global 封顶同时限制一个窗口内进入 map 的 key 基数；
- client 只使用 ASGI socket peer，不信任可伪造的 `X-Forwarded-For`；
- identity 只为限流 key 做 `strip().casefold()`，不改变 repository 的登录标识语义；
- 所有检查在创建 worker 前、同一 event loop 中无 `await` 完成；
- 结构化 limiter/capacity 日志不包含原始 IP、用户名、密码、hash、cookie 或 token。

这不是分布式限流。若运行 N 个 Gateway worker，整体上限可近似扩大为单进程限额乘 N；
进程重启也会清空窗口。上线前必须在可信 proxy、Redis 或等价集中控制层建立不可绕过的
全局策略，并明确 socket peer 与真实 client 的受信转换规则。

## 7. 密码和时序边界

- 正常 hash 保持 `pbkdf2_sha256`、390,000 iterations、随机 salt；
- dummy KDF 使用固定非秘密 salt 和永不作为认证依据的 digest，只用于匹配工作类别；
- 不存在 repository、用户不存在、用户禁用和损坏/不支持 hash 均失败；
- iteration 超过 1,000,000 时不执行该值指定的工作量，改为固定 dummy 390,000；
- `SecretStr` 阻止 Pydantic model repr 直接显示密码；业务日志/错误不回显密码；
- username 限 128、password 限 1024 字符，越界请求不进入 limiter、DB 或 KDF。

这只缩小代码级工作量差，未做目标硬件的统计时序检验；账户锁定仍有有意的独立 429
语义。不得把 dummy KDF 单元测试表述为消除了所有用户名枚举信号。

## 8. 验证证据

### 8.1 实施迭代

首轮 focused 回归为 `3 failed, 61 passed`：测试替身证明
`OperatorQueryService(runtime)` 构造仍发生在 event loop。实现随即把构造也移入同步
worker，复跑为 `64 passed`。后续引入 `SecretStr` 与真实 route/session 断言后，新文件加
现有 auth 组合为 `26 passed`。

第一次完整全量在 106.62 秒通过。文档反向核对又发现 username 仍由框架自动生成字段级
422，与固定失败契约不一致；实现将 username 长度校验移入登录入口并复跑 131 项相关和
全量单元测试。下表记录该最终实现的结果。

保留该失败记录是为了证明隔离范围经过对抗验证，而不是只记录最终绿色结果。

### 8.2 最终测试

| 验证 | 最终结果 | 说明 |
|---|---:|---|
| FS-019 新测试 + 现有 auth | `26 passed, 1 warning` | thread、capacity、cancel、limiter、KDF、payload、session |
| FS-019/auth/Gateway 配置子集 | `66 passed, 1 warning` | 登录、Gateway 生命周期和配置相关 |
| settings/RDP 配置兼容 | `143 passed, 1 warning, 2 subtests passed` | 新默认值不破坏旧配置加载 |
| FS-019/020、auth、Gateway 扩大相关回归 | `131 passed, 1 warning` | 相邻 middleware、health、launcher、dashboard invalidation |
| 全量 unit | `4273 passed, 30 skipped, 1666 warnings, 85 subtests passed` | 102.88 秒，无断言失败 |
| Ruff `aats/ --fix` | PASS | 零剩余错误 |
| Ruff apps + FS-019/020 tests | PASS | 零错误 |
| 变更 Markdown 本地链接 | 66 files / 289 links / 0 broken | 只检查当前修改与未跟踪 Markdown |
| `git diff --check` | PASS | 仅输出仓库既有 LF/CRLF 转换提示 |

pytest 的单条 cache warning 是 Windows `.pytest_cache` 目录创建冲突；通过独立
`--basetemp` 完整运行，不是产品断言失败。其余既有警告主要是 Python 3.12+ SQLite
datetime adapter deprecation 与 `test_long_short_poller.py` 的 AsyncMock coroutine 未 await。
这些 warning 属于 FS-021 测试治理欠账，本阶段没有把它们隐藏或写为已修复。

## 9. 未执行与未知项

本阶段明确未执行：

1. WSL2、Docker Compose、任何部署或 live profile；
2. 真实/克隆 PostgreSQL、Redis、NATS 或数据库故障注入；
3. 反向代理、trusted forwarding header、TLS 与目标浏览器；
4. 多 Gateway worker/多进程限流绕过测试；
5. 生产等价 CPU、DB pool、p95/p99、event-loop lag 和紧急 Operator SLA 压测；
6. 慢 DB、连接耗尽、worker 永久挂起、进程重启和告警送达演练；
7. 真实账户、余额、订单、仓位或交易所调用；
8. 独立于实施者的人工安全复核。

因此运行态、生产容量和分布式保护全部保持 `UNKNOWN` 或 `OPEN`。

## 10. 关闭条件

FS-019 只有在下列条件全部满足后才可重新评估 CLOSED：

1. 隔离生产等价环境中启用真实进程数、真实数据库连接池和目标 KDF 硬件；
2. 正确密码、错误密码、不存在用户、锁定用户、慢 DB 与客户端取消混合压测通过；
3. 明确 p95/p99、event-loop lag、CPU、DB pool wait、队列拒绝率和紧急登录 SLA；
4. 可信 proxy/Redis/等价集中限流对多进程、重启、伪造 header 不可绕过；
5. worker 挂起与数据库连接耗尽时 Gateway 其他控制面仍可用且告警可达；
6. 目标 TLS/browser/auth 组合验证通过，且不记录凭证或敏感标识；
7. 独立 reviewer 复核实现、测试、负载报告和补偿控制。

任一项 UNKNOWN 都不能把 FS-019 改为 CLOSED。

## 11. 最终裁定

```text
FS-019: CODE REMEDIATED / DISTRIBUTED RATE-LIMIT & LOAD VERIFICATION OPEN

REAL-MONEY PRODUCTION: NO-GO
```

本阶段降低了登录请求直接阻塞 Gateway event loop 和取消绕过线程容量的代码风险，
但没有证明生产环境中的集中限流、容量、数据库超时或 Operator 可用性。Phase 3I 不构成
部署、真实资金操作或上线授权。
