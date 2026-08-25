# FS-002 Kill Switch P0 修复设计与实施范围

> 文档状态：实施任务 / 设计冻结  
> 最后核对：2026-08-24（代码基线 `00b6df0f8a8d2665d6cae3e88996843767cd1f56`）  
> 工作分支：`codex/fs-002-kill-switch-p0`  
> 核对范围：静态代码、Phase 2 审计、隔离替身复现  
> 运行时边界：未连接真实账户、未提交交易所订单、未读取 `.env.*` 凭证  
> 生产决定：**REAL-MONEY PRODUCTION: NO-GO**

## 1. 业务目标与边界

唯一目标是关闭 `FS-002`：保证 Kill Switch 一旦由 execution 确认生效，任何新的风险增加订单都不能越过真实交易所提交边界；Gateway 只有取得 execution enforcement acknowledgement 后，才可把 halt 表述为已执行。

本任务不处理同根 K 线前视、Research Factory、部署架构、数据库连接池、schema 治理、UI 重构或其他审计 finding。允许的兼容改动仅限 Kill Switch 状态、operator halt 命令、OKX 最终提交门禁、直接相关测试与审计记录。

## 2. 当前行为与已复现根因

Phase 3A 在未修改代码前重新复现了两条 Phase 2 利用链：

```text
传播故障：
Gateway halt_async -> 本地 halted=True
  -> Redis SET 失败（异常被吞）
  -> NATS publish 失败（异常被吞）
  -> Operator API 返回 {status: halted, halted: true}
  -> execution 仍 halted=False

输出：handler status=halted；gateway_halted=True；execution_halted=False
```

```text
最终提交竞态：
order 通过早期 _submission_gate_error
  -> await _max_size_gate_error
  -> Kill Switch halt
  -> await 返回
  -> 无二次检查，直接 client.place_order

输出：kill_halted_before_place_order=True；place_order_calls=1；order=FILLED
```

结构性根因有三项：

1. Gateway 把本地 cache 更新误表述为 execution 已执行；没有 execution acknowledgement。
2. Redis/NATS 都是 best-effort 传播，execution 正常运行时不持续核对权威记录。
3. 唯一真实提交点前没有 generation fence，也没有与 halt enforcement 协调的原子边界。

## 3. 模块职责与领域模型

| 模块 | 修复后职责 |
|---|---|
| `KillSwitch` | 状态机、transition generation、Redis 权威记录、NATS 通知、restart hydration、风险增加 submission fence |
| Gateway `ReconciliationSystemQueryFacade.halt` | 接受 operator 请求；先传播 halt intent；四进程模式通过 command bridge 等待 execution ack；不自称最终权威 |
| `OperatorCommandClient/Worker` | 复用现有 NATS 请求-响应链路承载 `halt`；response 是 execution ack，不新增消息基础设施 |
| execution `KillSwitch` | 最终 enforcement authority；本地先阻断新风险，再等待已进入不可逆边界的提交排空，随后返回 enforced ack |
| `OKXExecutionAdapter` | 在唯一 `client.place_order()` 出口执行最终 generation/authoritative-state guard；持有 fence 直到提交调用返回 |
| Redis | 跨进程与重启的权威状态记录；风险增加提交读失败/空/非法时 fail-closed |
| NATS | 低延迟通知与 command/ack 传输；不是唯一安全依据 |

### 3.1 状态机

| 状态 | `halted` | 含义 | 是否可向 API 宣称 enforced |
|---|---:|---|---:|
| `RUNNING` | false | 已有明确、持久的 operator resume authority；风险增加提交仍需最终 fence | 否 |
| `HALTING` | true | halt intent 已在本地生效，正在传播或等待既有提交排空 | 否 |
| `HALTED` | true | execution fence 已阻断新风险，所有先前持锁提交已离开不可逆边界 | **是，仅 execution ack** |
| `RESUMING` | true | resume 校验通过但持久权威尚未完成；继续阻断 | 否 |
| `DEGRADED` | true | 权威状态缺失、非法或读取失败；fail-closed | 否 |

“Request Accepted”对应 halt intent 已产生；“Halt Propagating”对应 `HALTING`；“Halt Enforced”只对应 execution 返回同 generation 的 `HALTED` acknowledgement。不存在 `HTTP 200 = 本地 Gateway cache 已改 = 交易已停` 的含混映射。

每次有效 halt/resume transition 带唯一 `generation`。重复 halt 在 `HALTING/HALTED` 状态复用现有 generation，保持幂等。显式 resume 产生新 generation；任何在旧 generation 下开始的 queued/in-flight risk-increasing intent，即使 halt 后又 resume，也不能继续提交，必须重新进入新一轮 admission。

## 4. 输入/输出接口

### 4.1 Kill Switch 状态记录

Redis/NATS payload 在兼容原字段的基础上增加：

```text
halted: bool
reason: str | null
state: RUNNING | HALTING | HALTED | RESUMING | DEGRADED
generation: str
set_at_ts: float
source_role: str
resume_authorized: bool
```

旧的 `halted=true` 记录可保守 hydrate；旧的 `halted=false` 记录没有 `resume_authorized=true`，不得让新 execution 静默进入 RUNNING。

### 4.2 Operator command

现有 `ExecutionCommandName` 增加 `halt`。Gateway payload 包含 reason、actor 审计字段、generation 与 request timestamp。execution response 至少包含：

```text
status: halted | already_halted
state: HALTED
halted: true
enforced: true
generation: <same generation>
acknowledged_by: execution
```

command publish、response 或 timeout 失败时，Gateway 返回/抛出的语义不得等价于 enforced；路由在 timeout 时返回 `504`，在无法送达或验证 execution acknowledgement 时返回 `503`。只有同 generation 的 execution `HALTED` 确认才保持成功 `200`。

## 5. 数据库 schema、表、索引与约束

本任务不新增或修改 PostgreSQL schema、表、索引、约束或 migration。Kill Switch 继续使用现有 Redis key `aats:hot:system:kill_switch`，仅演进 value payload；读取对旧 `halted=true` 记录向后兼容，对旧 running 记录采取一次性保守 halt。

## 6. 事务、一致性与并发

### 6.1 最终提交 fence

所有风险增加订单在 adapter 早期记录 `admission_generation`。到 `client.place_order()` 紧前：

1. 获取 execution-local `asyncio.Lock`；
2. 核对本地状态；
3. 读取 Redis 权威记录；读取失败、空值、非法值均将本地锁定为 `DEGRADED` 并拒单；
4. 核对 authoritative state 为 RUNNING、`resume_authorized=true` 且 generation 与 admission generation 一致；
5. 持锁调用 `client.place_order()`；
6. `finally` 释放锁。

`halt_async()` 先把本地状态设为 `HALTING`，因此后续 guard 均拒绝；随后获取并释放同一锁，等所有已经开始的不可逆提交结束，最后切换 `HALTED` 并向 Gateway acknowledgement。由此定义精确并发语义：**halt enforcement 线性化点是 execution 在本地已阻断后成功排空 submission fence 的时刻。**在该线性化点之前已经开始网络提交的订单属于 pre-effective in-flight，不谎称被撤回；线性化点之后没有风险增加订单能开始提交。

### 6.2 多 worker

当前部署只有一个 execution 应用进程，command worker response 即该 execution authority 的 ack。若未来扩展多个 execution worker，单一 ack 不足以宣布全局 enforced；届时必须引入 expected-worker membership 与全员 generation ack。本次代码的每个 adapter 实例均独立执行 Redis + generation 最终 guard，因此一个 stale worker不能仅凭本地 RUNNING 越过提交边界。

## 7. 授权、认证与数据安全

现有 `/system/halt` 继续要求 admin。command payload 复用 actor role/identity/auth source 以保留审计归因。不得记录 API key、session secret、交易所凭证、完整 token 或 `.env.*` 内容。resume 仍经现有 reconciliation/recovery 校验，只允许 operator 路径产生 `resume_authorized=true`。

## 8. 错误处理与幂等

- Halt 是偏安全操作：本地阻断先于外部 I/O；Redis/NATS 某一失败不回滚本地 halt。
- execution ack 丢失：Gateway 返回未确认，不宣称 enforced；execution 实际可能已 halt，后续重复 halt 复用 generation 并安全返回。
- Redis 读取失败/空/非法：已进入最终执行边界的风险增加提交 fail-closed 并锁存 `DEGRADED`；reduce-only 路径不因状态存储故障被无条件禁止。
- NATS 状态通知失败：最终 Redis 读取仍阻断；若 command/ack 失败，API 不返回 enforced。
- Redis halt 写入与 NATS command/state 同时完全失败：Gateway 无法让远端 execution 获知请求，因而 halt 不能成为 effective；API 必须返回失败而不得声称 enforced。若 execution 同时无法读取 Redis，最终 guard fail-closed；如果它仍可读取旧的完整 RUNNING 记录，则在任何通信通道恢复前无法从物理上获知新的 halt intent。这是全分区残余边界，不得隐藏或当作已验证的全局停机。
- 重复 halt：不创建相互竞争的 generation，不解除已有阻断。
- Resume Redis 写失败：保持 halted，不广播/应用 RUNNING。
- 旧 queued order：generation 不匹配时持久化为 BLOCKED，不在 resume 后复活。

## 9. 状态转换与生命周期

```text
RUNNING --halt request--> HALTING --execution drain+ack--> HALTED
   |                         | propagation/authority error
   |                         v
   +----------------------> DEGRADED

HALTED --validated explicit operator resume--> RESUMING
RESUMING --durable authority write--> RUNNING
RESUMING --write/validation failure--> HALTED or DEGRADED
```

Restart 规则：stored HALTED/HALTING/DEGRADED 一律恢复为 blocked；对 `exchange_submission_enabled=true` 的资金风险环境，Redis 不可用、key 缺失、payload 非法一律 DEGRADED，只有含 `resume_authorized=true` 的完整 RUNNING 记录可启动为 RUNNING。不具有真实交易所提交能力的 paper/backtest 环境不因 live authority key 缺失被误标记为人工 halt。NATS 的 resume 通知只有在与 Redis 权威 generation 一致时才可清除本地 halt。

现有 recovery 自动清除 stale reconciliation halt 的 `kill_switch.resume()` 将停止隐式恢复；它可以记录“已具备人工 resume 条件”，但实际恢复仍需显式 operator resume。

## 10. 缓存与性能

风险增加订单每次真实提交增加一次 Redis GET，并在单 execution adapter 内串行跨越 `place_order`。这是低频、资金安全优先的边界；预期增加一个本地 Redis RTT，通常为亚毫秒到数毫秒。持锁时间受 OKX request timeout 约束。reduce-only/cancel 不进入风险增加 fence，紧急降险能力保留。

若未来吞吐需要并发，应使用读写 epoch/fence 或具备原子 compare-and-submit 能力的 execution gateway；不得为了性能移除最终权威读取。

## 11. 日志、监控与审计

新增/强化结构化事件：halt requested、propagation outcome、execution ack、generation mismatch、authoritative state unavailable、order rejected at final fence、halt enforced、resume requested/applied/failed。字段含 generation、process role、reason、timestamp、intent/client order correlation；不含秘密。

API/operator action 记录必须区分 `accepted/propagating/enforced/failed`。`kill_switch.snapshot()` 暴露 state、generation、bootstrapped/subscribed 与最后 authority 检查结果，供后续 readiness 使用；本任务不扩展 UI。

## 12. 测试策略

新增 `tests/unit/test_fs002_kill_switch_p0.py`，确定性覆盖：

1. halt 在订单开始前阻断；
2. 复现 Phase 2 最大尺寸 await 竞态，修复后 `place_order=0`；
3. Redis 写失败、NATS 可用；
4. NATS 失败、Redis 可用，Gateway 不虚假 ack 且最终 guard 阻断；
5. Redis+NATS 双失败，Gateway 不返回 enforced；execution 本地已 effective 后最终边界继续阻断；
6. worker 本地 stale RUNNING，但 Redis HALTED，最终 guard 阻断；
7. queued order 在提交前 halt；
8. 并发 pending submissions 与 halt drain 的线性化语义；
9. HALTED 状态 execution restart 仍 halt；Redis 不可用/空也 halt；
10. 重复 halt 幂等、generation 稳定；
11. resume 只有持久显式 authority 成功后生效，旧 queued generation 仍拒绝；
12. 真实 exchange payload 带 `reduceOnly=true` 且账户快照证明可减仓时可通过；仅标签伪装但可能增仓时拒绝。

另执行现有 Kill Switch、guarded live/simulated、OrderManager、command bridge、operator API、recovery/risk 测试，并以源码扫描证明只有一个真实 `place_order` 出口且已被 fence 包裹。

## 13. 迁移、回滚与兼容

代码不需要 DB migration。Redis payload 为向前兼容新增字段；旧 halted 记录安全兼容，旧 running 记录会保守要求一次显式 resume。API 成功 body 保留 `status/halted/reason`，新增 state/enforced/generation/ack；未确认场景的 HTTP/状态语义有意收紧。

回滚代码会重新暴露 P0，不属于允许的生产回滚方案。若修复导致测试或部署兼容问题，应保持 REAL-MONEY NO-GO、回到设计阶段，不得恢复旧的虚假 halt 成功语义。

## 14. 配置与环境隔离

不新增 live 凭证、环境变量或默认 profile。所有新测试使用 InMemory bus、fake Redis 和 exchange spy，不访问网络、真实 Redis/Postgres/OKX。WSL2 集成测试只在现有测试隔离约束内运行，不执行部署。

## 15. 代码组织与依赖

预计只修改：

- `aats/services/governance_engine/kill_switch.py`
- `aats/services/execution_engine/okx_adapter.py`
- `aats/services/operator/reconciliation_system_queries.py`
- `aats/services/operator/command_bridge.py`（仅必要的 ack metadata/错误语义时）
- `aats/schemas/operator_command.py`
- `aats/bootstrap/config.py`
- `aats/api/routes.py`
- `aats/services/execution_engine/recovery.py`
- 直接相关测试与审计文档

不新增第三方依赖，不改变数据库模型，不重构 unrelated runtime composition。

## 16. 文档、运维与验收标准

验证完成后创建 `audit/full_system_2026_08_24/21-fs-002-remediation.md`，记录修复前/后利用链、文件、命令、结果、未知与残余风险。只有以下条件全部具备才可把 `FS-002` 标为 CLOSED：

- 两条原始利用链均被确定性阻断；
- execution 最终提交边界统一执行 authoritative state + generation fence；
- Redis/NATS 单故障不能放行，双故障不产生虚假 enforced；全分区下的物理不可知边界已明确记录；
- stale worker、queued order、restart、重复 halt、显式 resume、reduce-only 均通过测试；
- 所有直接真实交易所提交路径均已盘点且无旁路；
- 新增与相关测试、全量单测、lint 和最窄安全集成测试通过；
- 独立人工复核仍是关闭后的必要治理步骤。

即使 `FS-002` 关闭，`FS-001/003/006/007/009` 等仍为上线硬阻断，生产决定继续 **REAL-MONEY PRODUCTION: NO-GO**。
