# 21 FS-002 Kill Switch P0 修复记录

> 后续状态：本文件冻结 Phase 3A 的 generation/ack/final-fence 证据。Phase 3L
> 已增加 generation-scoped 15 秒短时交易许可租约；当前 FS-002 裁定与新增验证见
> [32-fs-002-short-lived-trading-permission-lease.md](32-fs-002-short-lived-trading-permission-lease.md)。
> 下文“未引入短时 lease/全分区无固定上界”只描述 Phase 3A 当时状态。

> 日期：2026-08-24  
> 修复前代码基线：`00b6df0f8a8d2665d6cae3e88996843767cd1f56`  
> 工作分支：`codex/fs-002-kill-switch-p0`  
> 验证边界：静态源码、InMemory Redis/NATS 替身、fake OKX client、Windows 全量单测  
> 未执行：真实账户、真实交易所下单、部署、凭证读取、真实 Redis/NATS 四进程注故障  
> 生产决定：**REAL-MONEY PRODUCTION: NO-GO**

## 1. 原始故障与修复前复现

Phase 3A 在修改应用代码前，在隔离替身上重建了 Phase 2 的两条成功利用链。

### 1.1 传播故障与虚假确认

Redis `SET` 和 NATS publish 同时抛错时，原 Gateway halt handler 仍正常返回 `status=halted`，但 execution 本地 Kill Switch 仍为 RUNNING。

```text
handler_returned = {status: halted, halted: true, reason: phase3a_before_fix}
gateway_halted = true
execution_halted = false
```

复现结论：**PASS / 原漏洞已复现**。

### 1.2 最终提交竞态

订单通过早期 `_submission_gate_error`，在异步 `_max_size_gate_error` 中暂停；此时激活 Kill Switch，再释放异步门禁。原代码没有最终复核，仍调用了 `place_order`。

```text
kill_halted_before_place_order = true
place_order_calls = 1
final_order_status = FILLED
```

复现结论：**PASS / 原漏洞已复现**。

## 2. 根因

1. Gateway 把本进程 cache 更新误表述为 execution 已停止，没有 execution acknowledgement。
2. Redis/NATS 传播是 best-effort，故障被吞后 API 仍能返回成功。
3. 早期准入检查与真实 `place_order` 之间有多个 `await`，唯一真实提交点没有 generation 复核和并发 fence。
4. 恢复路径可因新鲜对账自动调用 `resume()`，与“显式 operator 授权才能重新增加风险”冲突。

## 3. 设计决策

### 3.1 权威和状态机

- execution 是拒绝真实交易所提交的最终权威，Gateway 不是。
- 状态为 `RUNNING -> HALTING -> HALTED` 与 `HALTED -> RESUMING -> RUNNING`；权威不可用时为 `DEGRADED`。
- 只有 execution/monolith 在阻断新风险并排空 submission fence 后才能返回同 generation 的 `HALTED/enforced=true`。
- Redis 是跨进程/重启权威记录；NATS 是低延迟通知与 command/ack 传输，不再单独承担安全性。

### 3.2 最终执行边界

`OKXExecutionAdapter.submit()` 在最后一个、也是当前唯一个 `client.place_order(payload)` 语句周围执行：

1. 核对早期记录的 admission generation；
2. 核对 execution 本地 halt；
3. 读取 Redis 最新权威记录；读失败、空、非法时 fail-closed；
4. 持有 submission fence 直到不可逆的 `place_order` 调用返回。

Halt 先将本地置为 `HALTING`，使新 guard 拒绝，再等待同一 fence 排空，最后线性化为 `HALTED`。已在 effective 线性化点前进入网络调用的订单可完成；该点之后不得再开始新的风险增加提交。

### 3.3 降险与恢复

- cancel 路径不进入风险增加 fence。
- 只有已通过账户仓位/数量语义校验且真实 OKX payload 带 `reduceOnly=true` 的订单可在 halt 期间提交；仅靠 side 或标签不能绕过。
- 恢复必须是对账/恢复检查通过后的显式 operator resume，并先持久化新 generation 的 `resume_authorized=true`。
- 恢复期 Redis 写失败时锁存 `DEGRADED`，不发布/应用 RUNNING。

完整设计与实施边界见 `docs/task/fs_002_kill_switch_p0_remediation_sow_2026_08_24.md`。

## 4. 变更文件

| 文件 | 变更 |
|---|---|
| `aats/services/governance_engine/kill_switch.py` | 状态机、generation、Redis 权威恢复、NATS 事件、execution ack、submission fence、fail-closed guard |
| `aats/services/execution_engine/okx_adapter.py` | 唯一真实 `place_order` 边界的最终 guard；验证后 reduce-only 例外 |
| `aats/services/operator/reconciliation_system_queries.py` | Gateway halt 代理并验证 execution ack；resume 状态语义 |
| `aats/services/operator/query_service.py` | halt generation/timestamp 透传 |
| `aats/schemas/operator_command.py` | execution command 新增 `halt` |
| `aats/bootstrap/config.py` | execution halt command handler 配线 |
| `aats/api/routes.py` | halt timeout/remote/local command 失败的 504/400/503 语义 |
| `aats/services/execution_engine/recovery.py` | 取消新鲜对账后的隐式 auto-resume |
| `tests/unit/test_fs002_kill_switch_p0.py` | 任务要求的 12 项确定性对抗回归 |
| `tests/unit/test_kill_switch_sync.py` | 新状态/恢复权威兼容测试 |
| `tests/unit/test_execution_recovery.py` | 显式 operator resume 预期 |
| `docs/task/fs_002_kill_switch_p0_remediation_sow_2026_08_24.md` | 修复前设计与 SOW |
| `audit/full_system_2026_08_24/15-consolidated-risk-register.md` | FS-002 修复证据/待验证状态 |
| `audit/full_system_2026_08_24/20-go-no-go-gates.md` | G1 代码证据与仍未放行状态 |
| `audit/full_system_2026_08_24/21-fs-002-remediation.md` | 本记录 |

无 DB migration，无新依赖，无凭证/环境变量变更，无部署。

## 5. 验证证据

### 5.1 新 FS-002 与相关回归

```text
.venv\Scripts\python.exe -m pytest <FS-002 + KillSwitch + execution + command + API + risk files> -q ...
257 passed, 27 subtests passed in 13.90s
```

其中 `tests/unit/test_fs002_kill_switch_p0.py` 的 12 项分别覆盖：正常 halt、已证明最终提交竞态、Redis 失败、NATS 失败、双传输失败不虚假 ack、陈旧 worker、queued order、并发订单、halt 后重启、重复 halt、权威 resume 和 reduce-only/伪降险。

### 5.2 最终全量单测

```text
.venv\Scripts\python.exe -m pytest tests/unit/ -q -p no:cacheprovider --basetemp=audit/full_system_2026_08_24/test-tmp/phase3a-fs002-full-final-c
4147 passed, 30 skipped, 1665 warnings, 85 subtests passed in 122.55s
```

30 个 skip 为现有环境/可选集成条件。警告主要是现有 SQLite datetime adapter 在 Python 3.12+ 的 deprecation，以及 `test_long_short_poller.py` 中现有 AsyncMock `raise_for_status` 未 await；本任务未修改这些与 FS-002 无关的路径。

一次中间全量运行曾暴露 paper monolith 不应被 live authority 缺失规则误阻断；最终实现将 bootstrap fail-closed 限定为 `exchange_submission_enabled` 环境能力，同时保留所有可真实提交模式（包括 monolith）在 authority 缺失时的 fail-closed。修正后重跑全量通过。

### 5.3 Lint 与静态旁路扫描

```text
.venv\Scripts\python.exe -m ruff check aats/ --fix
All checks passed!
```

`rg` 扫描当前应用/脚本：

- 真实下单调用仅有 `aats/services/execution_engine/okx_adapter.py` 中一个 `client.place_order(payload)` 语句，位于最终 boundary context 内。
- `OKXRESTClient.place_order()` 是底层 HTTP POST 封装；当前应用代码没有 adapter 之外的调用方。
- OrderManager 的 normal、leg、split/fallback/retry 最终收敛到 `adapter.submit()` / `submit_leg_order() -> submit()`。
- cancel 使用独立 `/trade/cancel-order`，不被风险增加 fence 禁止。
- 应用内只剩 operator reconciliation 路径能调用 `resume_async()`，recovery 不再隐式 resume。

### 5.4 安全集成测试

```text
.venv\Scripts\python.exe -m pytest tests/integration/test_kill_switch_cross_process.py -x -q
4 skipped
```

本机 Windows venv 缺少可选 `redis` / `testcontainers` 依赖，且未设置 `AATS_RUN_REDIS_INTEGRATION=1`；WSL2 中配置的 `~/aats-venv` 不存在，系统 Python 也缺少 pytest/redis/testcontainers。本轮未为跑测试临时安装依赖，未启动项目 Compose 或部署。

## 6. 修复后对抗复测

```text
.venv\Scripts\python.exe -m pytest \
  tests/unit/test_fs002_kill_switch_p0.py::TestFS002KillSwitchP0::test_02_proven_final_submit_race_is_blocked \
  tests/unit/test_fs002_kill_switch_p0.py::TestFS002KillSwitchP0::test_05_both_transports_fail_without_false_gateway_ack \
  -vv
2 passed in 0.89s
```

- 原最终提交利用：修复前 `place_order_calls=1`；修复后断言 `BLOCKED` 且 `place_order_calls=[]`。**FAIL / 已阻止**。
- 原虚假 halt 利用：修复前 Gateway 正常返回 halted 而 execution 不停；修复后双传输故障导致 `OperatorCommandError`/HTTP 503，Gateway 保持 `HALTING/enforced=false`。**FAIL / 已阻止虚假确认**。

## 7. 剩余假设、残余风险与未知

### 7.1 静态可证明

- 当前 checkout 只有一个真实 `place_order` 调用语句，已被最终 boundary 包裹。
- 当前 Compose/runtime 设计是单 execution 应用进程；代码上每个 adapter 实例都会执行 Redis/generation 最终复核。
- Gateway 成功响应必须验证同 generation 的 execution/monolith `HALTED` ack。

### 7.2 尚未运行验证

- 真实 Redis + 真实 NATS + 真实四进程中的延迟、乱序、断连、重连和进程硬崩溃。
- 未来多 execution worker 的 membership 和全 worker acknowledgement；当前单 ack 不得外推为未来多 worker 的全局 ack。
- 真实 OKX 对各产品/仓位模式 `reduceOnly` 的最终拒单语义；当前只用 payload + 账户快照语义和 fake client 验证。

### 7.3 全分区边界

如果 Gateway 的 Redis halt 写入和所有 NATS command/state 同时完全失败，而 execution 仍能读取 Redis 中旧的、结构完整的 RUNNING generation，则远端 execution 在通信恢复前无法获知这个新 halt intent。这是分布式全分区下的物理不可知边界。修复保证此时 Gateway **不会声称 halt enforced**；若 execution 读 Redis 也失败，最终 guard 会 fail-closed。本轮未引入短时 trading-permission lease，因此不宣称全分区能在固定秒数内自动 halt。

## 8. FS-002 最终状态

**PARTIALLY REMEDIATED / OPEN — 代码与隔离回归已修复，生产等价四进程故障注入和独立人工复核尚未完成。**

已满足的是：虚假成功响应被移除，最终提交竞态被阻断，单传输故障、陈旧 worker、queued/concurrent order、重启、幂等 halt、显式 resume 和 reduce-only 的确定性测试通过。

未满足的是：真实 Redis/NATS/四进程注故障未运行，未来多 worker 无全员 ack 协议，全分区无短时许可租约。根据 Phase 3A “有任何不确定就保持 OPEN”的闭环标准，不将 FS-002 写为 CLOSED。

## 9. 生产决定

**REAL-MONEY PRODUCTION: NO-GO**

除 FS-002 尚待上述运行闭环外，`FS-001/003/006/007/009` 等独立硬阻断仍未关闭。本记录不是部署授权、真实资金许可或上线批准。
