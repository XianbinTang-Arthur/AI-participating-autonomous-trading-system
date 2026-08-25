# 32 FS-002 全分区短时交易许可租约整改证据

> 阶段：Phase 3L  
> 日期：2026-08-24  
> 起始 HEAD：`00b6df0f8a8d2665d6cae3e88996843767cd1f56`  
> 分支：`codex/fs-002-kill-switch-p0`  
> 工作区：包含尚未提交的 Phase 3A–3L 变更  
> 当前裁定：`PARTIALLY REMEDIATED / TARGET PARTITION-EXPIRY VERIFICATION OPEN`  
> 生产决定：**REAL-MONEY PRODUCTION: NO-GO**

## 1. 本阶段关闭的代码缺口

Phase 3A 已消除 Gateway 虚假 halt acknowledgement，并在唯一真实
`place_order` 边界增加 generation、Redis authority 与 submission fence。
但长期 `aats:hot:system:kill_switch` RUNNING 记录有 30 天 TTL：如果 Gateway
无法写 Redis halt、NATS command/state 也完全失败，而 execution 仍能读取旧
RUNNING 记录，旧权限没有短时自动收敛上界。

Phase 3L 将“重启恢复事实”和“此刻允许增险”拆成两个 Redis 事实：

- 长期 `kill_switch` key 继续负责恢复、状态机与 operator resume generation；
- Gateway/monolith 为当前 RUNNING generation 维护
  `aats:hot:system:kill_switch_permission:<generation>`；
- permission 使用 Redis 服务器 TTL 15 秒，每 5 秒续租；
- execution 最终 fence 在长期 authority 校验后再读同 generation permission；
- execution、market、decision 永不创建或续租 permission。

完整设计和实施边界见
[`docs/task/fs_002_short_lived_trading_permission_lease_sow_2026_08_24.md`](../../docs/task/fs_002_short_lived_trading_permission_lease_sow_2026_08_24.md)。

## 2. 安全语义

### 2.1 许可签发

只有 `gateway`/`monolith` 且
`fail_closed_on_authority_loss=true` 的 runtime 启动 lease task。每次续租前必须同时满足：

1. 本地为 `RUNNING`、未 halt、`resume_authorized=true`；
2. Redis 长期 authority 是结构完整的 RUNNING；
3. authority generation 与本地 generation 完全一致；
4. permission payload 的 `issued_by` 只能是 `gateway`/`monolith`。

TTL 和续租周期是代码常量，不开放 profile/env 覆盖。permission 安全性依赖
Redis 服务器 TTL，不使用跨主机墙钟判断；`issued_at` 只供诊断。

### 2.2 最终提交

风险增加订单仍在同一 submission fence 内先复核长期 authority，再读取当前
generation permission。读取异常、key 缺失、payload 非 dict、字段非法、generation
不匹配或 issuer 非法均抛固定 `KillSwitchSubmissionBlocked` reason，并锁存
`DEGRADED`。permission 不能创建/切换 generation，也不能替代 operator resume。

cancel 与经过账户事实及最终 payload 验证的 `reduceOnly=true` 降险动作沿用
Phase 3A 独立路径，不被短时 permission 误阻断。

### 2.3 halt、故障与 shutdown

- halt 先同步阻断本地 submission fence，再在传播前尽力删除前一 RUNNING
  generation 的 permission；
- 删除与续租使用同一进程内 I/O lock，awaited halt 删除排在已持锁续租之后；
- Redis 删除失败不恢复权限，旧 key 最迟由服务器 TTL 到期；
- Gateway/monolith 续租失败不会延长 key；达到当前 lease 到期上界后 task
  锁存 `DEGRADED` 并 raise；
- lease task 作为 service-owned critical task 纳入 FS-006 监督；
- runtime shutdown 第一项即取消续租并尽力撤销 permission，长期 authority 不变。

## 3. 代码变更

| 文件 | 变更 |
|---|---|
| `aats/services/governance_engine/kill_switch.py` | generation-scoped permission key、15s/5s lease、owner lifecycle、最终 fence 校验、halt/shutdown 撤销、安全 snapshot |
| `aats/bootstrap/config.py` | peer readiness 后启动 lease；service-owned critical 注册；shutdown 优先停止续租 |
| `aats/services/operator/reconciliation_system_queries.py` | 四进程代理 resume 在 Gateway 激活同 generation permission 后才返回成功 |
| `tests/unit/test_fs002_kill_switch_p0.py` | Phase 3A adapter 回归补充合法 permission fixture；resume 新 generation 后显式签发 |
| `tests/unit/test_fs002_short_lived_permission_lease.py` | 16 项 Phase 3L 确定性对抗、代理恢复与生命周期回归 |

无 PostgreSQL migration、第三方依赖、HTTP/API shape、环境变量或凭证变更。

## 4. 对抗性验证

新增纯内存验证覆盖：

- Gateway 正常续租后 execution 只接受同 generation permission；
- execution 不能启动续租，也不能自行签发；
- permission 缺失、错 generation 或 TTL 到期立即失败关闭；
- 30 天 RUNNING authority 不能替代已过期的 15 秒 permission；
- halt generation 改变后仍删除前一 RUNNING generation key；
- Gateway Redis halt `SET`、permission `DELETE` 与 NATS publish 同时失败时，
  execution 的旧 permission 仍按 TTL 到期并拒绝新风险；
- owner resume 在返回前建立新 generation permission；
- 四进程代理 resume 必须由 Gateway 重读同 generation authority 并激活 permission
  后才向 Operator 返回成功；authority/permission 激活失败返回固定错误；
- authority generation mismatch 不续租旧 generation；
- 连续续租失败到期后 critical task raise，异常正文不进入公开 reason；
- stop 取消 task 并撤销 permission；
- 非严格 research/paper runtime 保持隔离兼容；
- ApplicationRuntime 将 lease 注册为 service-owned critical task；
- 15 秒/5 秒安全周期保持代码常量。

Phase 3A 的正常 halt、最终提交竞态、单/双传输故障、陈旧 worker、queued/concurrent
订单、restart、幂等、resume 与 reduce-only 12 项回归继续通过。

## 5. 测试与静态检查

| 检查 | 结果 |
|---|---|
| Phase 3A + 3L FS-002 focused | `28 passed, 1 warning` |
| KillSwitch、runtime shutdown、FS-006、adapter、lifecycle、operator command 扩大回归 | `225 passed, 1 warning, 20 subtests passed` |
| 全量 unit（仓库内全新 basetemp） | `4312 passed, 30 skipped, 1666 warnings, 85 subtests passed in 103.70s` |

仓库规定的原样命令
`.venv\Scripts\python.exe -m pytest tests/unit/ -x -q` 在 `87 passed` 后因
Windows 系统临时目录 `PermissionError` 中止，没有业务 assertion failure。随后使用
`.pytest_tmp/phase3l_full_01` 完整重跑通过。pytest cache 仍有既存路径警告；其余
warnings 仍主要是 SQLite datetime adapter deprecation 和 LongShort 测试
`AsyncMock` 未 await，本阶段不声称消除了既存 warning debt。

## 6. 已验证、未验证与兼容边界

### 已验证

- 旧 RUNNING 长期状态不能单独越过新 execution fence；
- permission generation、issuer 与 Redis TTL 的代码/内存语义成立；
- execution 无续租能力；
- halt/delete 失败仍有 15 秒服务器 TTL 设计上界；
- lease task 退出可进入现有 critical supervisor；
- 当前全量 unit 与相关运行时关闭路径兼容。

### 未验证

- 真实 Redis 服务器 TTL 精度、Gateway 单向分区及 execution 可读场景；
- 真实 NATS command/state 全断、乱序、重连和四进程时序；
- Gateway crash/kill -9 后目标容器、health/restart/告警的实际秒数；
- event loop stall 对续租的真实影响和外部 supervisor 行为；
- 多 Gateway 或未来多 execution membership/全员 acknowledgement；
- 新旧 revision 滚动混跑；旧 execution 不读取 permission，因此禁止混跑；
- 真实 OKX 在产品/仓位模式下的 `reduceOnly` 最终语义；
- 独立人工 reviewer 对协议、任务所有权和所有 `place_order` 调用点的复核。

### 未执行

没有读取 `.env.*`，没有连接真实账户、交易所、Redis、NATS、Postgres、Docker
或 WSL2，没有运行 integration、部署或任何资金动作。

## 7. 当前裁定

FS-002 的“全分区下旧 RUNNING 权限无限期存在”已在代码协议中收紧为
generation-scoped 15 秒 permission lease；Phase 3L 的隔离故障注入证明旧 permission
到期后不再授权。但真实 Redis/NATS 四进程的单向分区、服务器 TTL 时间界、进程崩溃、
目标 restart/告警、多实例协议与独立复核均未完成，因此状态只能更新为：

```text
PARTIALLY REMEDIATED / TARGET PARTITION-EXPIRY VERIFICATION OPEN
```

G1 保持 `PARTIAL / 未放行`，FS-002 仍是 P0 HARD BLOCKER。
**REAL-MONEY PRODUCTION: NO-GO**。
